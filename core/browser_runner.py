"""Generic browser-IA orchestration shared by all aggregators.

Extracts the universal skeleton of an AI-driven checkout:
  create_transaction -> start capture -> navigate -> enter iframe -> hook crypto
  -> ReasoningLoop.run(objective) -> capture plaintext + charge/verify requests.

The aggregator supplies the specifics through its interface:
  - browser_objective(req): the natural-language objective for the loop
  - charge_request_matcher / verify_request_matcher: select captured requests
  - decide_browser_outcome(req, loop_result, result): the aggregator-specific
    outcome decision (e.g. DigiKUNTZ's USSD watch loop + classifier/LLM verdict).

The USSD/operator-specific semantics stay inside the aggregator (per plan §2).
"""

import json
import logging

from core.base import Aggregator, PaymentRequest, PaymentResult
from core.config import settings
from core.reasoning_loop import ReasoningLoop
from core.upstream_errors import classify_upstream_error

log = logging.getLogger("ai_browser2")


async def run_browser_flow(
    aggregator: Aggregator,
    req: PaymentRequest,
    *,
    max_turns: int = 22,
) -> PaymentResult:
    """Drive the full AI checkout for `aggregator` and return a PaymentResult.

    Acquires an isolated `BrowserSession` (its own tab + network capture + active
    frame) from the controller's pool so concurrent payments never collide, and
    releases it when done.
    """
    llm = aggregator.llm
    result = PaymentResult()

    # 0. Acquire an isolated browser session (one tab) for THIS transaction.
    browser = await aggregator.browser.acquire_session()
    try:
        return await _run_browser_flow_in_session(aggregator, req, browser, max_turns)
    finally:
        await aggregator.browser.release_session(browser)


async def _run_browser_flow_in_session(
    aggregator: Aggregator,
    req: PaymentRequest,
    browser,  # BrowserSession
    max_turns: int,
) -> PaymentResult:
    """Body of the flow, running entirely on one isolated session."""
    llm = aggregator.llm
    result = PaymentResult()

    # 1. Create the transaction (aggregator-specific API call).
    log.info("Creating transaction: %d XAF, %s, %s", req.amount, req.network, req.phone)
    try:
        tx = await aggregator.create_transaction(req)
    except Exception as e:  # noqa: BLE001
        result.error = f"{aggregator.name} API error: {e}"
        result.error_code = classify_upstream_error(e) or ""
        log.error(result.error)
        return result

    result.transaction_id = tx.get("transactionRef", "")
    payment_link = tx.get("paymentLink", "")
    if not payment_link:
        result.error = f"No paymentLink in response: {tx}"
        log.error(result.error)
        return result
    log.info("Transaction created: ref=%s link=%s", result.transaction_id, payment_link)

    # 2. Capture network BEFORE navigating.
    browser.start_capture()

    # 3. Navigate + enter iframe + hook crypto (non-blocking; agent recovers).
    await browser.goto(payment_link)
    await browser.wait(ms=3000)
    form_ready = False
    try:
        await browser.enter_iframe("iframe")
        try:
            frame = getattr(browser, "_active_frame", browser.page)
            await frame.wait_for_selector("#phone", timeout=6000)
            form_ready = True
        except Exception:
            pass  # form not ready yet — the agent will recover
        await browser.hook_crypto()
        log.info("Entered iframe (agent will handle loader if form not ready)")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not enter iframe yet (%s) — agent will retry", e)
        browser._active_frame = browser.page

    # DEBUG (form-load): when the form isn't ready on first paint, snapshot WHY —
    # stalled assets (pending requests), console errors, failed/HTTP errors — so
    # the cause is visible in the trace instead of being hidden by the agent's
    # recovery (reload). Recorded as a synthetic turn-0 trace entry.
    if not form_ready:
        pending = browser.get_pending_requests(min_age_s=2.0)
        signals = browser.get_error_signals()
        diag = {
            "turn": 0,
            "phase": "form_not_ready",
            "pending_requests": [
                {"url": p["url"][:160], "type": p.get("resource_type", ""),
                 "age_s": p["age_s"]}
                for p in pending[:15]
            ],
            "failed_requests": signals.get("failed_requests", [])[:10],
            "console_errors": [e.get("text", "")[:200]
                               for e in signals.get("console_errors", [])][:10],
            "http_errors": [{"url": h["url"][:160], "status": h["status"]}
                            for h in signals.get("http_errors", [])][:10],
        }
        log.warning("🔍 FORM NON PRÊT — diagnostic chargement:")
        log.warning("   pending(>2s): %d | failed: %d | console_err: %d | http_err: %d",
                    len(pending), len(diag["failed_requests"]),
                    len(diag["console_errors"]), len(diag["http_errors"]))
        for p in diag["pending_requests"][:8]:
            log.warning("   ⏳ %ss %s %s", p["age_s"], p["type"], p["url"])
        result.form_load_diagnostic = diag

    # 4. Run the reasoning loop on the aggregator's objective.
    browser.reset_diagnostics()
    loop = ReasoningLoop(
        browser, llm, max_turns=max_turns,
        checkout_url_predicate=aggregator.checkout_url_predicate,
        max_elapsed_s=settings.retry_window_s,
    )
    loop_result = await loop.run(aggregator.browser_objective(req))
    result.turns = loop_result.turns
    result.input_tokens = loop_result.total_input_tokens
    result.output_tokens = loop_result.total_output_tokens
    result.trace = loop_result.trace
    # Surface the form-load diagnostic as a turn-0 entry in the trace.
    if result.form_load_diagnostic:
        d = result.form_load_diagnostic
        result.trace = [{
            "turn": 0,
            "url": payment_link,
            "elements": 0,
            "thought": "[DEBUG form-load] formulaire non prêt au 1er affichage",
            "actions": [],
            "objective_reached": False,
            "error": json.dumps({
                "pending_requests": d["pending_requests"],
                "failed_requests": d["failed_requests"],
                "console_errors": d["console_errors"],
                "http_errors": d["http_errors"],
            }, ensure_ascii=False)[:4000],
        }] + (result.trace or [])

    # 5. Capture plaintext payload from the crypto hook.
    plaintexts = await browser.get_captured_plaintexts()
    if plaintexts:
        last = plaintexts[-1]
        result.plaintext_payload = last.get("plaintext", "")
        result.public_key = last.get("publicKey", "")
    else:
        log.warning("No plaintext captured from crypto hook")

    # 6. Aggregator-specific outcome decision (USSD watch, classifier, LLM...).
    #    Pass THIS session so the outcome reads the right tab's capture/page.
    await aggregator.decide_browser_outcome(req, loop_result, result, session=browser)

    # 7. Capture charge + verify requests (the deduced curl replay).
    charge_req = browser.get_charge_request(aggregator.charge_request_matcher)
    if charge_req:
        result.flutterwave_charge_url = charge_req.url
        result.flutterwave_charge_body = charge_req.request_body
        result.flutterwave_charge_response = charge_req.response_body[:1000]
        result.curl_replay = charge_req.to_curl()
    verify_reqs = browser.get_verify_requests(aggregator.verify_request_matcher)
    if verify_reqs:
        last_verify = verify_reqs[-1]
        result.verify_url = last_verify.url
        result.verify_request_body = last_verify.request_body
        result.verify_last_response = last_verify.response_body[:1000]
        result.verify_curl = last_verify.to_curl()

    # Build the reusable curl template now, while we still hold the session's
    # captured requests (the session is released as soon as the flow returns).
    # /pay persists it from result.curl_template.
    if charge_req:
        try:
            result.curl_template = aggregator.extract_curl_template(
                charge_req, verify_reqs[-1] if verify_reqs else None, result.public_key
            )
        except Exception as e:  # noqa: BLE001
            log.warning("extract_curl_template failed: %s", e)

    captured = browser.stop_capture()
    result.captured_requests = [
        {"method": r.method, "url": r.url[:200], "status": r.status} for r in captured
    ]

    if not result.success:
        result.success = result.final_status in ("successful", "completed", "ussd_sent")
    if loop_result.error and not result.error:
        result.error = loop_result.error
    return result
