"""Playwright browser controller — screenshots, DOM, actions, network capture."""

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Request, Response

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


class BrowserController:
    """Wraps a single Playwright browser + page for the agent to drive."""

    def __init__(self, headless: bool = False):
        self._headless = headless
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        # Network capture
        self.captured_requests: list[CapturedRequest] = []
        self._capture_enabled = False
        # Console messages + network failures (for error detection)
        self.frame_detached_count: int = 0  # incremented each time the active iframe detaches
        self.console_messages: list[dict] = []
        self.failed_requests: list[dict] = []
        # In-flight tracking: requests started but not yet finished/failed.
        # Used to detect assets that stall forever (form-load debugging).
        self._inflight: dict[str, dict] = {}

    async def start(self):
        self._pw = await async_playwright().start()
        launch_kwargs = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        # In GUI mode on Windows, use installed Chrome to avoid spawn issues
        # with Playwright's bundled Chromium.
        if not self._headless:
            launch_kwargs["channel"] = "chrome"
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        self.page = await self._new_instrumented_page()
        log.info("Browser started (headless=%s)", self._headless)

    async def _new_instrumented_page(self):
        """Create a new page with ALL browser events attached.

        Single place to wire events — any new page (initial or reload tab)
        must go through here so the AI's snapshot is always complete.
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
                log.info("Console[%s]: %s", msg.type, msg.text[:200])
        except Exception:
            pass

    def _on_page_error(self, error):
        try:
            self.console_messages.append({
                "type": "pageerror",
                "text": str(error),
                "timestamp": time.time(),
            })
            log.info("PageError: %s", str(error)[:200])
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
            log.info("RequestFailed: %s %s — %s",
                     entry["method"], entry["url"][:100], entry["error"])
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
        frame = getattr(self, '_active_frame', self.page)
        try:
            await frame.evaluate("""
                (function() {
                    if (window._pageWatcherInstalled) return;
                    window._pageWatcherInstalled = true;
                    window._pageStatus = null;

                    // Watch for DOM changes
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

                    // Also watch for navigation (redirect = payment done)
                    var lastUrl = location.href;
                    setInterval(function() {
                        if (location.href !== lastUrl) {
                            window._pageStatus = {status: 'redirected', message: 'Redirected to ' + location.href, timestamp: Date.now()};
                            lastUrl = location.href;
                        }
                    }, 500);
                })();
            """)
            log.info("Page change watcher installed")
        except Exception as e:
            log.warning("Failed to install page watcher: %s", e)

    async def get_page_status(self) -> dict | None:
        """Check if the page watcher detected a status change."""
        frame = getattr(self, '_active_frame', self.page)
        try:
            return await frame.evaluate("window._pageStatus")
        except Exception:
            return None

    async def hook_crypto(self):
        """Monkey-patch cryptico.encrypt to capture plaintext before encryption.
        Must be called AFTER the iframe content has loaded cryptico.js."""
        frame = getattr(self, '_active_frame', self.page)
        try:
            await frame.evaluate("""
                (function() {
                    // Wait for cryptico to be available, then patch
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
                    // Try immediately, then retry with interval
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
            log.info("Crypto hook installed in active frame")
        except Exception as e:
            log.warning("Failed to install crypto hook: %s", e)

    async def get_captured_plaintexts(self) -> list[dict]:
        """Retrieve any plaintext data captured by the crypto hook."""
        frame = getattr(self, '_active_frame', self.page)
        try:
            result = await frame.evaluate("window._capturedPlaintexts || []")
            if result:
                log.info("Captured %d plaintext payloads", len(result))
            return result
        except Exception:
            return []

    def start_capture(self, url_filter: str = ""):
        """Start capturing network requests. Optional URL substring filter."""
        self.captured_requests.clear()
        self._capture_filter = url_filter
        self._capture_enabled = True
        log.info("Network capture started (filter=%s)", url_filter or "*")

    def stop_capture(self) -> list[CapturedRequest]:
        """Stop capturing and return captured requests."""
        self._capture_enabled = False
        log.info("Network capture stopped: %d requests", len(self.captured_requests))
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

        # Apply filter
        if hasattr(self, '_capture_filter') and self._capture_filter:
            if self._capture_filter not in url:
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
        log.info("Captured: %s %s [%d] body=%d",
                 cap.method, cap.url[:100], cap.status, len(cap.response_body))

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        log.info("Browser stopped")

    async def goto(self, url: str, wait_until: str = "domcontentloaded"):
        """Navigate to a URL."""
        log.info("Navigating to %s", url)
        self._original_url = url  # Keep the full URL (before redirects)
        await self.page.goto(url, wait_until=wait_until, timeout=30000)
        # Reset active frame to main page
        self._active_frame = self.page

    async def reload_payment(self):
        """Recovery action: open the same payment URL in a NEW tab, close the
        old one, then re-enter the checkout iframe.

        Much more reliable than page.reload() because Flutterwave's checkout
        sometimes gets stuck on its internal loader after a reload."""
        # Use the ORIGINAL payment URL (with the token), not the current URL
        # which may have been stripped by Flutterwave's redirect.
        url = getattr(self, '_original_url', None) or self.page.url
        log.info("Action: reload_payment — opening new tab for %s", url)

        try:
            # Open a fresh tab with the same URL
            new_page = await self._new_instrumented_page()

            # Close the old stuck tab
            old_page = self.page
            self.page = new_page
            self._active_frame = new_page
            try:
                await old_page.close()
            except Exception:
                pass

            # Navigate in the new tab
            await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await new_page.wait_for_timeout(5000)

            # Re-enter iframe and wait for the actual form
            for _ in range(3):
                try:
                    await self.enter_iframe("iframe")
                    frame = getattr(self, '_active_frame', self.page)
                    await frame.wait_for_selector("#phone", timeout=10000)
                    await self.hook_crypto()
                    log.info("reload_payment: form ready in new tab")
                    return True
                except Exception as e:
                    log.warning("reload_payment re-enter attempt: %s", e)
                    self._active_frame = self.page
                    await self.page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("reload_payment new tab failed: %s", e)

        return False

    async def enter_iframe(self, selector: str = "iframe"):
        """Switch context into the first matching iframe."""
        handle = await self.page.wait_for_selector(selector, timeout=15000)
        frame = await handle.content_frame()
        if frame:
            self._active_frame = frame
            log.info("Entered iframe: %s", selector)
            return True
        log.warning("Could not enter iframe: %s", selector)
        return False

    async def snapshot(self, max_html: int = 100_000) -> DomSnapshot:
        """Take a full snapshot: screenshot + DOM + interactive elements."""
        snap = DomSnapshot()
        snap.url = self.page.url

        # Screenshot as base64 PNG
        screenshot_bytes = await self.page.screenshot(full_page=False)
        snap.screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        # Try active frame first, fall back to page if frame is detached
        frame = getattr(self, '_active_frame', self.page)
        try:
            html = await frame.content()
        except Exception:
            # Frame was detached/navigated — re-enter iframe or use main page
            self.frame_detached_count += 1
            log.warning("Active frame detached, re-entering iframe")
            try:
                await self.enter_iframe("iframe")
                frame = self._active_frame
                # Wait for actual content, not just the loader
                try:
                    await frame.wait_for_selector("#phone", timeout=8000)
                except Exception:
                    pass
                html = await frame.content()
                # Re-install crypto hook in new frame
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

        # Interactive elements from active frame
        snap.interactive_elements = await self._get_interactive_elements()
        snap.frame_detached_count = self.frame_detached_count

        # Réseau — vision complète comme DevTools.
        pending = self.get_pending_requests(min_age_s=1.0)
        snap.pending_requests = [
            {"type": p.get("resource_type", ""), "url": p["url"][-80:], "age_s": p["age_s"]}
            for p in pending[:10]
        ]
        # Dernières réponses HTTP — body complet pour les requêtes importantes
        # (charge, verify, payment-done), tronqué pour les assets statiques.
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
        # Requêtes échouées (DNS/abort/timeout)
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
        frame = getattr(self, '_active_frame', self.page)
        try:
            return await frame.evaluate(js)
        except Exception as e:
            log.warning("Failed to get interactive elements: %s", e)
            return []

    # ---- Actions the AI agent can call ----

    async def click(self, selector: str):
        frame = getattr(self, '_active_frame', self.page)
        log.info("Action: click(%s)", selector)
        await frame.click(selector, timeout=10000)

    async def fill(self, selector: str, value: str):
        frame = getattr(self, '_active_frame', self.page)
        log.info("Action: fill(%s, %s)", selector, value)
        await frame.fill(selector, value, timeout=10000)

    async def select(self, selector: str, value: str):
        frame = getattr(self, '_active_frame', self.page)
        log.info("Action: select(%s, %s)", selector, value)
        await frame.select_option(selector, value, timeout=10000)

    async def scroll(self, pixels: int = 500):
        frame = getattr(self, '_active_frame', self.page)
        log.info("Action: scroll(%d)", pixels)
        await frame.evaluate(f"window.scrollBy(0, {pixels})")

    async def wait(self, selector: str | None = None, ms: int = 2000):
        frame = getattr(self, '_active_frame', self.page)
        if selector:
            log.info("Action: wait for %s", selector)
            await frame.wait_for_selector(selector, timeout=ms)
        else:
            log.info("Action: wait %dms", ms)
            await frame.wait_for_timeout(ms)

    async def current_url(self) -> str:
        return self.page.url
