"""Playwright browser controller — screenshots, DOM, actions, network capture.

Concurrency model (feature/browser-concurrency):
  - `BrowserSession` = ONE tab + all the per-transaction state (network capture,
    active frame, console/inflight diagnostics, event handlers). Every action and
    snapshot lives here, so two payments running at once never corrupt each other.
  - `BrowserController` = a POOL: it owns the Playwright `Browser` instance(s),
    hands out isolated sessions (`acquire_session`) and reclaims them
    (`release_session`). When a Chrome reaches `max_tabs` open tabs, a fresh Chrome
    is launched for the next session.
"""

import base64
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Response

log = logging.getLogger("ai_browser2")


@dataclass
class CapturedRequest:
    """A captured HTTP request/response pair."""
    url: str = ""
    method: str = ""
    status: int = 0
    request_headers: dict = field(default_factory=dict)
    request_body: str = ""
    response_headers: dict = field(default_factory=dict)
    response_body: str = ""
    timestamp: float = 0.0

    def to_curl(self) -> str:
        """Generate a curl command to replay this request."""
        parts = [f"curl -X {self.method} '{self.url}'"]
        for k, v in self.request_headers.items():
            if k.lower() in ("host", "content-length", "connection"):
                continue
            parts.append(f"-H '{k}: {v}'")
        if self.request_body:
            parts.append(f"--data-raw '{self.request_body}'")
        return " \\\n  ".join(parts)


@dataclass
class DomSnapshot:
    url: str = ""
    outer_html: str = ""           # DOM du frame actif (iframe si on est dedans)
    page_outer_html: str = ""      # DOM de la page principale (toujours présent)
    screenshot_b64: str = ""
    interactive_elements: list[dict] = field(default_factory=list)
    url_history: list[str] = field(default_factory=list)   # URLs visitées depuis le début
    elapsed_s: float = 0.0                                  # secondes depuis le début du loop
    deadline_exceeded: bool = False                         # plafond 17min dépassé (l'IA doit conclure)
    frame_detached_count: int = 0  # combien de fois l'iframe s'est détachée depuis le début
    # Réseau — vision complète comme DevTools
    pending_requests: list[dict] = field(default_factory=list)
    recent_responses: list[dict] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)


class BrowserSession:
    """One isolated tab + its full per-transaction state.

    Created and owned by `BrowserController`. The reasoning loop and the runner
    drive a session (not the global controller), so concurrent payments stay
    isolated: each has its own captured_requests, active frame, console buffer,
    inflight map and frame-detach counter.
    """

    def __init__(self, controller: "BrowserController", context: BrowserContext, sid: str):
        self._controller = controller
        self._context = context
        self.sid = sid
        self.page: Page | None = None
        self._active_frame = None
        # Network capture
        self.captured_requests: list[CapturedRequest] = []
        self._capture_enabled = False
        self._capture_filter = ""
        # Console messages + network failures (for error detection)
        self.frame_detached_count: int = 0
        self.console_messages: list[dict] = []
        self.failed_requests: list[dict] = []
        # In-flight tracking: requests started but not yet finished/failed.
        self._inflight: dict[str, dict] = {}
        # Navigation memory
        self._original_url: str | None = None

    async def open(self):
        """Create the tab and wire every browser event to THIS session."""
        self.page = await self._new_instrumented_page()
        self._active_frame = self.page
        return self

    async def _new_instrumented_page(self) -> Page:
        """Create a new page with ALL browser events attached to this session.

        Single place to wire events — any new page (initial or reload tab) goes
        through here so the AI's snapshot is always complete and isolated.
        """
        page = await self._context.new_page()
        page.on("response", self._on_response)
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("requestfailed", self._on_request_failed)
        page.on("request", self._on_request_started)
        page.on("requestfinished", self._on_request_finished)
        return page

    def _on_console(self, msg):
        try:
            entry = {
                "type": msg.type,            # 'error', 'warning', 'log', ...
                "text": msg.text,
                "timestamp": time.time(),
            }
            self.console_messages.append(entry)
            if msg.type in ("error", "warning"):
                log.info("[%s] Console[%s]: %s", self.sid, msg.type, msg.text[:200])
        except Exception:
            pass

    def _on_page_error(self, error):
        try:
            self.console_messages.append({
                "type": "pageerror",
                "text": str(error),
                "timestamp": time.time(),
            })
            log.info("[%s] PageError: %s", self.sid, str(error)[:200])
        except Exception:
            pass

    def _on_request_failed(self, request):
        try:
            failure = request.failure
            entry = {
                "url": request.url,
                "method": request.method,
                "error": failure if isinstance(failure, str) else (failure or "failed"),
                "timestamp": time.time(),
            }
            self.failed_requests.append(entry)
            self._inflight.pop(request.url, None)
            log.info("[%s] RequestFailed: %s %s — %s",
                     self.sid, entry["method"], entry["url"][:100], entry["error"])
        except Exception:
            pass

    def _on_request_started(self, request):
        try:
            self._inflight[request.url] = {
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "started_at": time.time(),
            }
        except Exception:
            pass

    def _on_request_finished(self, request):
        try:
            self._inflight.pop(request.url, None)
        except Exception:
            pass

    def get_pending_requests(self, min_age_s: float = 0.0) -> list[dict]:
        """Requests started but never finished/failed (stalled assets).

        `min_age_s` filters to those in flight for at least that long — useful
        to flag truly stuck assets (e.g. JS bundles) vs requests just started.
        """
        now = time.time()
        out = []
        for entry in self._inflight.values():
            age = now - entry["started_at"]
            if age >= min_age_s:
                out.append({**entry, "age_s": round(age, 1)})
        return sorted(out, key=lambda e: e["age_s"], reverse=True)

    def reset_diagnostics(self):
        """Clear console + failed-request buffers (call before clicking Pay)."""
        self.console_messages.clear()
        self.failed_requests.clear()

    def get_error_signals(self) -> dict:
        """Collect error signals from console + network for LLM interpretation.

        Returns console errors/warnings, failed network requests, and any
        HTTP error responses (status >= 400) captured during the charge.
        """
        # Drop benign console noise that is not a payment signal.
        _NOISE = (
            "autofocus", "favicon", "mixpanel", "preload",
            "download the react", "deprecat", "sourcemap",
            "google analytics", "gtag", "stripe.js",
        )
        console_errors = [
            m for m in self.console_messages
            if m["type"] in ("error", "warning", "pageerror")
            and not any(n in m["text"].lower() for n in _NOISE)
        ]
        http_errors = [
            {"url": r.url, "status": r.status, "body": r.response_body[:500]}
            for r in self.captured_requests
            if r.status >= 400
        ]
        return {
            "console_errors": console_errors,
            "failed_requests": list(self.failed_requests),
            "http_errors": http_errors,
        }

    async def watch_page_changes(self):
        """Install a MutationObserver to detect page changes in real-time."""
        frame = self._active_frame or self.page
        try:
            await frame.evaluate("""
                (function() {
                    if (window._pageWatcherInstalled) return;
                    window._pageWatcherInstalled = true;
                    window._pageStatus = null;

                    var lastText = (document.body.innerText || '').substring(0, 500);
                    const observer = new MutationObserver(function(mutations) {
                        var currentText = (document.body.innerText || '').substring(0, 500);
                        if (currentText !== lastText) {
                            window._pageStatus = {
                                status: 'changed',
                                message: currentText,
                                previous: lastText,
                                timestamp: Date.now()
                            };
                            lastText = currentText;
                        }
                    });
                    observer.observe(document.body, {childList: true, subtree: true, characterData: true});

                    var lastUrl = location.href;
                    setInterval(function() {
                        if (location.href !== lastUrl) {
                            window._pageStatus = {status: 'redirected', message: 'Redirected to ' + location.href, timestamp: Date.now()};
                            lastUrl = location.href;
                        }
                    }, 500);
                })();
            """)
            log.info("[%s] Page change watcher installed", self.sid)
        except Exception as e:
            log.warning("[%s] Failed to install page watcher: %s", self.sid, e)

    async def get_page_status(self) -> dict | None:
        """Check if the page watcher detected a status change."""
        frame = self._active_frame or self.page
        try:
            return await frame.evaluate("window._pageStatus")
        except Exception:
            return None

    async def hook_crypto(self):
        """Monkey-patch cryptico.encrypt to capture plaintext before encryption.
        Must be called AFTER the iframe content has loaded cryptico.js."""
        frame = self._active_frame or self.page
        try:
            await frame.evaluate("""
                (function() {
                    function patchCryptico() {
                        if (typeof window.cryptico === 'undefined' ||
                            typeof window.cryptico.encrypt !== 'function') {
                            return false;
                        }
                        if (window._crypticoPatched) return true;
                        const originalEncrypt = window.cryptico.encrypt;
                        window._capturedPlaintexts = [];
                        window.cryptico.encrypt = function(plaintext, publicKey) {
                            try {
                                window._capturedPlaintexts.push({
                                    timestamp: Date.now(),
                                    plaintext: plaintext,
                                    publicKey: publicKey
                                });
                            } catch(e) {}
                            return originalEncrypt.apply(this, arguments);
                        };
                        window._crypticoPatched = true;
                        return true;
                    }
                    if (!patchCryptico()) {
                        var attempts = 0;
                        var timer = setInterval(function() {
                            attempts++;
                            if (patchCryptico() || attempts > 50) {
                                clearInterval(timer);
                            }
                        }, 200);
                    }
                })();
            """)
            log.info("[%s] Crypto hook installed in active frame", self.sid)
        except Exception as e:
            log.warning("[%s] Failed to install crypto hook: %s", self.sid, e)

    async def get_captured_plaintexts(self) -> list[dict]:
        """Retrieve any plaintext data captured by the crypto hook."""
        frame = self._active_frame or self.page
        try:
            result = await frame.evaluate("window._capturedPlaintexts || []")
            if result:
                log.info("[%s] Captured %d plaintext payloads", self.sid, len(result))
            return result
        except Exception:
            return []

    def start_capture(self, url_filter: str = ""):
        """Start capturing network requests. Optional URL substring filter."""
        self.captured_requests.clear()
        self._capture_filter = url_filter
        self._capture_enabled = True
        log.info("[%s] Network capture started (filter=%s)", self.sid, url_filter or "*")

    def stop_capture(self) -> list[CapturedRequest]:
        """Stop capturing and return captured requests."""
        self._capture_enabled = False
        log.info("[%s] Network capture stopped: %d requests", self.sid, len(self.captured_requests))
        return self.captured_requests.copy()

    def get_charge_request(self, matcher: Callable[[CapturedRequest], bool]) -> CapturedRequest | None:
        """Find the first captured request matching `matcher` (the aggregator's
        charge predicate). Generic — each aggregator supplies its own matcher."""
        for r in self.captured_requests:
            if matcher(r):
                return r
        return None

    def get_verify_requests(self, matcher: Callable[[CapturedRequest], bool]) -> list[CapturedRequest]:
        """Find all captured requests matching `matcher` (the aggregator's verify
        predicate). Generic — each aggregator supplies its own matcher."""
        return [r for r in self.captured_requests if matcher(r)]

    # --- Flutterwave default matchers (used by DigiKUNTZ; thin wrappers over the
    #     generic methods above so legacy callers keep working). ---
    @staticmethod
    def _flutterwave_charge_matcher(r: "CapturedRequest") -> bool:
        return (
            r.method == "POST"
            and ("flutterwave" in r.url or "ravepay" in r.url)
            and "/charge" in r.url
        )

    @staticmethod
    def _flutterwave_verify_matcher(r: "CapturedRequest") -> bool:
        return r.method == "POST" and "ravepay" in r.url and "/verify/" in r.url

    def get_flutterwave_charge(self) -> CapturedRequest | None:
        """Find the Flutterwave charge POST request in captured requests."""
        return self.get_charge_request(self._flutterwave_charge_matcher)

    def get_flutterwave_verify_requests(self) -> list[CapturedRequest]:
        """Find all Flutterwave verify/mpesa polling requests."""
        return self.get_verify_requests(self._flutterwave_verify_matcher)

    async def _on_response(self, response: Response):
        """Callback for every network response."""
        if not self._capture_enabled:
            return

        request = response.request
        url = request.url

        if self._capture_filter and self._capture_filter not in url:
            return

        cap = CapturedRequest()
        cap.url = url
        cap.method = request.method
        cap.status = response.status
        cap.timestamp = time.time()

        try:
            cap.request_headers = dict(await request.all_headers())
        except Exception:
            cap.request_headers = {}

        try:
            cap.request_body = request.post_data or ""
        except Exception:
            cap.request_body = ""

        try:
            cap.response_headers = dict(await response.all_headers())
        except Exception:
            cap.response_headers = {}

        try:
            body_bytes = await response.body()
            cap.response_body = body_bytes.decode("utf-8", errors="replace")[:2000]
        except Exception:
            cap.response_body = ""

        self.captured_requests.append(cap)
        log.info("[%s] Captured: %s %s [%d] body=%d",
                 self.sid, cap.method, cap.url[:100], cap.status, len(cap.response_body))

    async def goto(self, url: str, wait_until: str = "domcontentloaded"):
        """Navigate to a URL."""
        log.info("[%s] Navigating to %s", self.sid, url)
        self._original_url = url  # Keep the full URL (before redirects)
        await self.page.goto(url, wait_until=wait_until, timeout=30000)
        self._active_frame = self.page

    async def reload_payment(self):
        """Recovery action: open the same payment URL in a NEW tab, close the
        old one, then re-enter the checkout iframe.

        Much more reliable than page.reload() because Flutterwave's checkout
        sometimes gets stuck on its internal loader after a reload."""
        url = self._original_url or self.page.url
        log.info("[%s] Action: reload_payment — opening new tab for %s", self.sid, url)

        try:
            new_page = await self._new_instrumented_page()

            old_page = self.page
            self.page = new_page
            self._active_frame = new_page
            try:
                await old_page.close()
            except Exception:
                pass

            await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await new_page.wait_for_timeout(5000)

            for _ in range(3):
                try:
                    await self.enter_iframe("iframe")
                    frame = self._active_frame or self.page
                    await frame.wait_for_selector("#phone", timeout=10000)
                    await self.hook_crypto()
                    log.info("[%s] reload_payment: form ready in new tab", self.sid)
                    return True
                except Exception as e:
                    log.warning("[%s] reload_payment re-enter attempt: %s", self.sid, e)
                    self._active_frame = self.page
                    await self.page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("[%s] reload_payment new tab failed: %s", self.sid, e)

        return False

    async def enter_iframe(self, selector: str = "iframe"):
        """Switch context into the first matching iframe."""
        handle = await self.page.wait_for_selector(selector, timeout=15000)
        frame = await handle.content_frame()
        if frame:
            self._active_frame = frame
            log.info("[%s] Entered iframe: %s", self.sid, selector)
            return True
        log.warning("[%s] Could not enter iframe: %s", self.sid, selector)
        return False

    async def snapshot(self, max_html: int = 100_000) -> DomSnapshot:
        """Take a full snapshot: screenshot + DOM + interactive elements."""
        snap = DomSnapshot()
        snap.url = self.page.url

        screenshot_bytes = await self.page.screenshot(full_page=False)
        snap.screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        # Try active frame first, fall back to page if frame is detached
        frame = self._active_frame or self.page
        try:
            html = await frame.content()
        except Exception:
            # Frame was detached/navigated — re-enter iframe or use main page
            self.frame_detached_count += 1
            log.warning("[%s] Active frame detached, re-entering iframe", self.sid)
            try:
                await self.enter_iframe("iframe")
                frame = self._active_frame
                try:
                    await frame.wait_for_selector("#phone", timeout=8000)
                except Exception:
                    pass
                html = await frame.content()
                await self.hook_crypto()
            except Exception:
                frame = self.page
                self._active_frame = self.page
                html = await frame.content()

        if len(html) > max_html:
            html = html[:max_html] + "\n<!-- truncated -->"
        snap.outer_html = html

        # DOM de la page principale (toujours, même quand on est dans un iframe).
        # Critique quand la page redirige vers payment-done : l'IA voit le texte
        # "failed"/"success" affiché là, pas seulement l'URL.
        try:
            page_html = await self.page.content()
            if len(page_html) > 8000:
                page_html = page_html[:8000] + "\n<!-- truncated -->"
            snap.page_outer_html = page_html
        except Exception:
            snap.page_outer_html = ""

        snap.interactive_elements = await self._get_interactive_elements()
        snap.frame_detached_count = self.frame_detached_count

        # Réseau — vision complète comme DevTools.
        pending = self.get_pending_requests(min_age_s=1.0)
        snap.pending_requests = [
            {"type": p.get("resource_type", ""), "url": p["url"][-80:], "age_s": p["age_s"]}
            for p in pending[:10]
        ]
        _ASSET_EXTS = (".js", ".css", ".png", ".svg", ".ttf", ".woff", ".ico")
        snap.recent_responses = [
            {
                "method": r.method,
                "status": r.status,
                "url": r.url[-120:],
                "body": "" if any(r.url.endswith(e) for e in _ASSET_EXTS)
                        else r.response_body[:500],
            }
            for r in self.captured_requests[-20:]
        ]
        snap.failed_requests = [
            {"method": f.get("method", ""), "url": f["url"][-100:], "error": f.get("error", "")}
            for f in list(self.failed_requests)[-5:]
        ]
        signals = self.get_error_signals()
        snap.console_errors = [
            e.get("text", "")[:200] for e in signals.get("console_errors", [])
        ][-5:]

        return snap

    async def _get_interactive_elements(self) -> list[dict]:
        """Extract all interactive elements with their properties."""
        js = """
        (() => {
            const selectors = 'a, button, input, select, textarea, [role="button"], [role="link"], [onclick]';
            const els = document.querySelectorAll(selectors);
            const results = [];
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                results.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    name: el.getAttribute('name') || '',
                    type: el.getAttribute('type') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    value: el.value || '',
                    text: (el.innerText || el.textContent || '').trim().slice(0, 100),
                    href: el.getAttribute('href') || '',
                    classes: el.className || '',
                    visible: rect.width > 0 && rect.height > 0,
                    rect: { x: Math.round(rect.x), y: Math.round(rect.y),
                            w: Math.round(rect.width), h: Math.round(rect.height) }
                });
            }
            return results;
        })()
        """
        frame = self._active_frame or self.page
        try:
            return await frame.evaluate(js)
        except Exception as e:
            log.warning("[%s] Failed to get interactive elements: %s", self.sid, e)
            return []

    # ---- Actions the AI agent can call ----

    async def click(self, selector: str):
        frame = self._active_frame or self.page
        log.info("[%s] Action: click(%s)", self.sid, selector)
        await frame.click(selector, timeout=10000)

    async def fill(self, selector: str, value: str):
        frame = self._active_frame or self.page
        log.info("[%s] Action: fill(%s, %s)", self.sid, selector, value)
        await frame.fill(selector, value, timeout=10000)

    async def select(self, selector: str, value: str):
        frame = self._active_frame or self.page
        log.info("[%s] Action: select(%s, %s)", self.sid, selector, value)
        await frame.select_option(selector, value, timeout=10000)

    async def scroll(self, pixels: int = 500):
        frame = self._active_frame or self.page
        log.info("[%s] Action: scroll(%d)", self.sid, pixels)
        await frame.evaluate(f"window.scrollBy(0, {pixels})")

    async def wait(self, selector: str | None = None, ms: int = 2000):
        frame = self._active_frame or self.page
        if selector:
            log.info("[%s] Action: wait for %s", self.sid, selector)
            await frame.wait_for_selector(selector, timeout=ms)
        else:
            log.info("[%s] Action: wait %dms", self.sid, ms)
            await frame.wait_for_timeout(ms)

    async def current_url(self) -> str:
        return self.page.url

    async def close(self):
        """Close this session's tab (the controller calls this on release)."""
        try:
            if self.page:
                await self.page.close()
        except Exception:
            pass


class BrowserController:
    """Pool of Chrome instances handing out isolated `BrowserSession`s.

    Owns the Playwright runtime and one or more `Browser` instances. Each Chrome
    holds up to `max_tabs` open tabs; once full, a new Chrome is launched for the
    next session. Sessions are isolated so concurrent payments never collide.
    """

    def __init__(self, headless: bool = False, max_tabs: int = 20):
        self._headless = headless
        self.max_tabs = max_tabs
        self._pw = None
        # Each pool entry: {"browser": Browser, "context": BrowserContext, "open": int}
        self._browsers: list[dict] = []
        self._sid_counter = 0

    async def start(self):
        """Launch the first Chrome so the service is ready at boot."""
        self._pw = await async_playwright().start()
        await self._launch_browser()
        log.info("Browser pool started (headless=%s, max_tabs=%d)",
                 self._headless, self.max_tabs)

    async def _launch_browser(self) -> dict:
        launch_kwargs = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        # In GUI mode, use installed Chrome to avoid spawn issues with the
        # bundled Chromium.
        if not self._headless:
            launch_kwargs["channel"] = "chrome"
        browser = await self._pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        entry = {"browser": browser, "context": context, "open": 0}
        self._browsers.append(entry)
        log.info("Launched Chrome #%d (pool size=%d)", len(self._browsers), len(self._browsers))
        return entry

    async def acquire_session(self) -> BrowserSession:
        """Open an isolated tab for one transaction.

        Picks a Chrome with spare capacity (< max_tabs open tabs); launches a new
        Chrome if all current ones are full.
        """
        entry = next((b for b in self._browsers if b["open"] < self.max_tabs), None)
        if entry is None:
            entry = await self._launch_browser()
        self._sid_counter += 1
        sid = f"s{self._sid_counter}"
        session = BrowserSession(self, entry["context"], sid)
        session._pool_entry = entry
        await session.open()
        entry["open"] += 1
        log.info("[%s] Session acquise (Chrome a %d/%d onglets)",
                 sid, entry["open"], self.max_tabs)
        return session

    async def release_session(self, session: BrowserSession):
        """Close the session's tab and free its slot in the pool."""
        await session.close()
        entry = getattr(session, "_pool_entry", None)
        if entry is not None:
            entry["open"] = max(0, entry["open"] - 1)
            log.info("[%s] Session libérée (Chrome a %d/%d onglets)",
                     session.sid, entry["open"], self.max_tabs)

    async def stop(self):
        for entry in self._browsers:
            try:
                await entry["browser"].close()
            except Exception:
                pass
        self._browsers.clear()
        if self._pw:
            await self._pw.stop()
        log.info("Browser pool stopped")
