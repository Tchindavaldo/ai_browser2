"""AI Browser 2 — REST server + Playwright + DeepSeek agent."""

import asyncio
import logging
import os
import sys

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager

from agent.browser import BrowserController
from agent.llm_client import LlmClient, LlmConfig
from agents.digikuntz import DigikuntzAgent, PaymentRequest, PaymentResult

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
    headless = os.environ.get("HEADLESS", "0") == "1"
    browser = BrowserController(headless=headless)
    await browser.start()

    # Init LLM client
    llm = LlmClient(LlmConfig(
        provider="deepseek",
        model=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
    ))

    log.info("AI Browser 2 ready! Browser=%s, Model=%s",
             "headless" if headless else "visible",
             llm.config.model)
    yield

    # Cleanup
    await llm.close()
    await browser.stop()


app = FastAPI(title="AI Browser 2", lifespan=lifespan)


class PayRequest(BaseModel):
    amount: int
    phone: str
    network: str
    email: str
    sender_name: str = "Rauvalia"
    callback_url: str = "https://app.digikuntz.com/callback"


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
    return {"status": "ok", "version": "0.1"}


@app.post("/pay", response_model=PayResponse)
async def pay(req: PayRequest):
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

    agent = DigikuntzAgent(browser, llm)
    result = await agent.pay(PaymentRequest(
        amount=req.amount,
        phone=req.phone,
        network=req.network,
        email=req.email,
        sender_name=req.sender_name,
        callback_url=req.callback_url,
    ))

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

    from agent.reasoning_loop import ReasoningLoop

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


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7332"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
