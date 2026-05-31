"""MobileWallet backend — REST server routing payments through aggregators.

A payment is dispatched by (aggregator, mode):
  - mode="browser": full AI-driven Playwright flow; on success the deduced curl
    template is persisted to the DB for later replay.
  - mode="replay": reproduce the payment via the stored template, no browser.
Every attempt is audited in the transactions table (when Supabase is configured).
"""

import logging
import sys

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager

# Importing the aggregators package registers all aggregators in the registry.
import aggregators.digikuntz  # noqa: F401
from core.base import PaymentRequest
from core.browser import BrowserController
from core.config import settings
from core.db import db
from core.llm_client import LlmClient, LlmConfig
from core import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ai_browser2")

# Global state
browser: BrowserController | None = None
llm: LlmClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser, llm

    # Start browser (visible by default so you can watch!)
    headless = settings.headless
    browser = BrowserController(headless=headless)
    await browser.start()

    # Init LLM client
    llm = LlmClient(LlmConfig(
        provider="deepseek",
        model=settings.llm_model,
        api_key=settings.deepseek_api_key,
    ))

    log.info("AI Browser 2 ready! Browser=%s, Model=%s",
             "headless" if headless else "visible",
             llm.config.model)
    yield

    # Cleanup
    await llm.close()
    await browser.stop()


app = FastAPI(title="MobileWallet backend", lifespan=lifespan)


class PayRequest(BaseModel):
    amount: int
    phone: str
    network: str
    email: str
    sender_name: str = "Rauvalia"
    callback_url: str = ""
    aggregator: str = "digikuntz"
    mode: str = "browser"  # "browser" | "replay"


class PayResponse(BaseModel):
    success: bool
    error: str = ""
    transaction_id: str = ""
    payment_status: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    flutterwave_charge_url: str = ""
    flutterwave_charge_body: str = ""
    flutterwave_charge_response: str = ""
    curl_replay: str = ""
    plaintext_payload: str = ""
    public_key: str = ""
    verify_url: str = ""
    verify_request_body: str = ""
    verify_last_response: str = ""
    verify_curl: str = ""
    final_status: str = ""
    final_message: str = ""
    error_signals: dict = {}
    captured_requests: list[dict] = []


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.2", "aggregators": registry.names()}


@app.get("/aggregators")
async def list_aggregators():
    """List the available aggregators (registered modules)."""
    return {"aggregators": registry.names()}


@app.post("/pay", response_model=PayResponse)
async def pay(req: PayRequest):
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

    cls = registry.get(req.aggregator)
    if cls is None:
        raise HTTPException(404, f"Unknown aggregator '{req.aggregator}'. Available: {registry.names()}")
    if req.mode not in ("browser", "replay"):
        raise HTTPException(400, f"Unknown mode '{req.mode}' (use 'browser' or 'replay')")

    agg = cls(browser=browser, llm=llm, db=db)
    payment = PaymentRequest(
        amount=req.amount,
        phone=req.phone,
        network=req.network,
        email=req.email,
        sender_name=req.sender_name,
        callback_url=req.callback_url,
    )

    if req.mode == "replay":
        template = db.load_template(req.aggregator)
        if template is None:
            raise HTTPException(
                409,
                f"No stored curl template for '{req.aggregator}'. Run mode='browser' once to deduce it.",
            )
        result = await agg.replay(payment, template)
    else:
        result = await agg.pay_via_browser(payment)
        # Deduce + persist the curl template from the captured browser flow.
        charge = browser.get_charge_request(agg.charge_request_matcher)
        if charge:
            verify_reqs = browser.get_verify_requests(agg.verify_request_matcher)
            template = agg.extract_curl_template(
                charge, verify_reqs[-1] if verify_reqs else None, result.public_key
            )
            if template:
                db.save_template(req.aggregator, template)

    # Audit the attempt (no-op if Supabase unconfigured).
    db.save_transaction(req.aggregator, req.mode, payment, result)

    return PayResponse(
        success=result.success,
        error=result.error,
        transaction_id=result.transaction_id,
        payment_status=result.payment_status,
        turns=result.turns,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        flutterwave_charge_url=result.flutterwave_charge_url,
        flutterwave_charge_body=result.flutterwave_charge_body,
        flutterwave_charge_response=result.flutterwave_charge_response,
        curl_replay=result.curl_replay,
        plaintext_payload=result.plaintext_payload,
        public_key=result.public_key,
        verify_url=result.verify_url,
        verify_request_body=result.verify_request_body,
        verify_last_response=result.verify_last_response,
        verify_curl=result.verify_curl,
        final_status=result.final_status,
        final_message=result.final_message,
        error_signals=result.error_signals,
        captured_requests=result.captured_requests,
    )


@app.get("/transactions")
async def list_transactions(limit: int = 50):
    """Recent transaction audit rows (empty if Supabase not configured)."""
    return {"transactions": db.list_transactions(limit=limit)}


@app.get("/status/{transaction_ref}")
async def transaction_status(transaction_ref: str):
    """Look up a stored transaction by its reference."""
    tx = db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    return tx


class DriveRequest(BaseModel):
    url: str
    objective: str
    phone: str = ""
    network: str = ""


@app.post("/drive")
async def drive(req: DriveRequest):
    """Drive the browser to a URL and let the AI agent handle it."""
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

    from core.reasoning_loop import ReasoningLoop

    # Capture all network requests
    browser.start_capture()
    await browser.goto(req.url)
    loop = ReasoningLoop(browser, llm, max_turns=15)
    result = await loop.run(req.objective)
    captured = browser.stop_capture()

    # Find Flutterwave charge
    charge = browser.get_flutterwave_charge()

    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "turns": result.turns,
        "input_tokens": result.total_input_tokens,
        "output_tokens": result.total_output_tokens,
        "flutterwave_charge": {
            "url": charge.url if charge else "",
            "method": charge.method if charge else "",
            "request_body": charge.request_body if charge else "",
            "response_body": charge.response_body[:1000] if charge else "",
            "curl_replay": charge.to_curl() if charge else "",
        },
        "all_requests": [
            {"method": r.method, "url": r.url[:200], "status": r.status}
            for r in captured
        ],
    }


@app.post("/test-llm")
async def test_llm():
    """Quick test: send a simple prompt to DeepSeek and return the response."""
    if not llm:
        raise HTTPException(500, "Not initialized")

    resp = await llm.send(
        system_prompt="Reponds en JSON: {\"status\": \"ok\", \"message\": \"...\"}",
        user_content=[{"type": "text", "text": "Dis bonjour en une phrase."}],
    )
    return {"success": resp.success, "error": resp.error, "text": resp.text}
