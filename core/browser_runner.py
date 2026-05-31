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

import logging

from core.base import Aggregator, PaymentRequest, PaymentResult
from core.reasoning_loop import ReasoningLoop

log = logging.getLogger("ai_browser2")


async def run_browser_flow(
    aggregator: Aggregator,
    req: PaymentRequest,
    *,
    max_turns: int = 22,
) -> PaymentResult:
    """Drive the full AI checkout for `aggregator` and return a PaymentResult."""
    browser = aggregator.browser
    llm = aggregator.llm
    result = PaymentResult()

    # 1. Create the transaction (aggregator-specific API call).
    log.info("Creating transaction: %d XAF, %s, %s", req.amount, req.network, req.phone)
    try:
        tx = await aggregator.create_transaction(req)
    except Exception as e:  # noqa: BLE001
        result.error = f"{aggregator.name} API error: {e}"
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
    try:
        await browser.enter_iframe("iframe")
        try:
            frame = getattr(browser, "_active_frame", browser.page)
            await frame.wait_for_selector("#phone", timeout=6000)
        except Exception:
            pass  # form not ready yet — the agent will recover
        await browser.hook_crypto()
        log.info("Entered iframe (agent will handle loader if form not ready)")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not enter iframe yet (%s) — agent will retry", e)
        browser._active_frame = browser.page

    # 4. Run the reasoning loop on the aggregator's objective.
    browser.reset_diagnostics()
    loop = ReasoningLoop(browser, llm, max_turns=max_turns)
    loop_result = await loop.run(aggregator.browser_objective(req))
    result.turns = loop_result.turns
    result.input_tokens = loop_result.total_input_tokens
    result.output_tokens = loop_result.total_output_tokens

    # 5. Capture plaintext payload from the crypto hook.
    plaintexts = await browser.get_captured_plaintexts()
    if plaintexts:
        last = plaintexts[-1]
        result.plaintext_payload = last.get("plaintext", "")
        result.public_key = last.get("publicKey", "")
    else:
        log.warning("No plaintext captured from crypto hook")

    # 6. Aggregator-specific outcome decision (USSD watch, classifier, LLM...).
    await aggregator.decide_browser_outcome(req, loop_result, result)

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

    captured = browser.stop_capture()
    result.captured_requests = [
        {"method": r.method, "url": r.url[:200], "status": r.status} for r in captured
    ]

    if not result.success:
        result.success = result.final_status in ("successful", "completed", "ussd_sent")
    if loop_result.error and not result.error:
        result.error = loop_result.error
    return result
