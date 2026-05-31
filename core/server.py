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
from pydantic import BaseModel, Field
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


API_DESCRIPTION = """
Backend **MobileWallet** — un rassemblement d'**agrégateurs** de paiement Mobile Money.

Chaque agrégateur (DigiKUNTZ aujourd'hui, d'autres demain) est un module exposant
deux capacités :

- **navigateur IA** (`mode=browser`) : un agent Playwright + LLM pilote le checkout,
  exécute le paiement et **déduit** le « curl replay » (requête `/charge` + `/verify`),
  persisté comme *template* réutilisable.
- **curl replay** (`mode=replay`) : rejoue le paiement sans navigateur via le template stocké.
- **auto** (`mode=auto`, défaut) : tente le replay d'abord, puis bascule sur le navigateur
  si le replay est non concluant (`fallback_browser`).

Toute tentative est auditée (table `transactions`) quand Supabase est configuré.
"""

OPENAPI_TAGS = [
    {"name": "system", "description": "Santé et introspection du service."},
    {"name": "payments", "description": "Exécution des paiements via les agrégateurs."},
    {"name": "transactions", "description": "Historique et statut des transactions (Supabase)."},
    {"name": "dev", "description": "Outils de développement (pilotage libre, ping LLM)."},
]

app = FastAPI(
    title="MobileWallet backend",
    version="0.2",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)


class PayRequest(BaseModel):
    amount: int = Field(..., description="Montant en XAF.", examples=[25])
    phone: str = Field(..., description="Numéro Mobile Money (sans +237).", examples=["696080087"])
    network: str = Field(..., description="Réseau : Orangemoney ou MTN.", examples=["Orangemoney"])
    email: str = Field(..., description="Email du payeur.", examples=["client@example.com"])
    sender_name: str = Field("Rauvalia", description="Nom affiché de l'émetteur.")
    callback_url: str = Field("", description="URL de callback (défaut: celle de l'agrégateur).")
    aggregator: str = Field("digikuntz", description="Nom de l'agrégateur (cf. GET /aggregators).")
    mode: str = Field("auto", description="auto | browser | replay.", examples=["auto"])
    fallback_browser: bool = Field(
        True,
        description="En mode auto : basculer sur le navigateur si le replay est non concluant.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "amount": 25, "phone": "696080087", "network": "Orangemoney",
                    "email": "client@example.com", "aggregator": "digikuntz",
                    "mode": "auto", "fallback_browser": True,
                }
            ]
        }
    }


class PayResponse(BaseModel):
    success: bool = Field(..., description="Vrai si le paiement a abouti.")
    message: str = Field("", description="Message clair pour l'utilisateur (succès comme échec).")
    error: str = Field("", description="Message d'erreur technique éventuel.")
    transaction_id: str = Field("", description="Référence transaction de l'agrégateur.")
    payment_status: str = ""
    turns: int = Field(0, description="Nombre de tours de l'agent IA (mode browser).")
    input_tokens: int = 0
    output_tokens: int = 0
    flutterwave_charge_url: str = ""
    flutterwave_charge_body: str = ""
    flutterwave_charge_response: str = ""
    curl_replay: str = Field("", description="Commande curl reproductible déduite.")
    plaintext_payload: str = ""
    public_key: str = ""
    verify_url: str = ""
    verify_request_body: str = ""
    verify_last_response: str = ""
    verify_curl: str = ""
    final_status: str = Field("", description="successful | failed | cancelled | pending | unknown.")
    final_message: str = Field("", description="Message clair pour l'utilisateur final.")
    error_signals: dict = {}
    captured_requests: list[dict] = []


@app.get("/health", tags=["system"], summary="Santé du service")
async def health():
    """Statut du service + liste des agrégateurs enregistrés."""
    return {"status": "ok", "version": "0.2", "aggregators": registry.names()}


@app.get("/aggregators", tags=["system"], summary="Agrégateurs disponibles")
async def list_aggregators():
    """Liste les agrégateurs et, pour chacun, les réseaux exacts acceptés."""
    return {
        "aggregators": [
            {"name": name, "supported_networks": registry.get(name).supported_networks}
            for name in registry.names()
        ]
    }


@app.post(
    "/pay",
    response_model=PayResponse,
    tags=["payments"],
    summary="Exécuter un paiement",
    responses={
        404: {"description": "Agrégateur inconnu."},
        400: {"description": "Mode invalide."},
        422: {"description": "Réseau non supporté (renvoie la liste exacte attendue)."},
        409: {"description": "Mode replay sans template (lancer mode=browser d'abord)."},
        502: {"description": "Replay échoué et fallback navigateur désactivé."},
    },
)
async def pay(req: PayRequest):
    """Exécute un paiement via l'agrégateur et le mode demandés.

    - **auto** (défaut) : replay d'abord ; bascule navigateur si non concluant
      (selon `fallback_browser`).
    - **browser** : flux IA complet ; déduit et persiste le template curl.
    - **replay** : rejoue via le template stocké (409 si absent).
    """
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

    cls = registry.get(req.aggregator)
    if cls is None:
        raise HTTPException(
            404,
            {
                "error": "unknown_aggregator",
                "message": f"Agrégateur '{req.aggregator}' inconnu.",
                "supported_aggregators": registry.names(),
            },
        )
    if req.mode not in ("auto", "browser", "replay"):
        raise HTTPException(
            400,
            {
                "error": "invalid_mode",
                "message": f"Mode '{req.mode}' invalide.",
                "supported_modes": ["auto", "browser", "replay"],
            },
        )

    agg = cls(browser=browser, llm=llm, db=db)

    # Validate the network against this aggregator's supported list. On failure,
    # echo back the exact accepted values (422).
    canonical_network = agg.normalize_network(req.network)
    if canonical_network is None:
        raise HTTPException(
            422,
            {
                "error": "invalid_network",
                "message": f"Réseau '{req.network}' non supporté par l'agrégateur '{req.aggregator}'.",
                "aggregator": req.aggregator,
                "supported_networks": agg.supported_networks,
            },
        )

    payment = PaymentRequest(
        amount=req.amount,
        phone=req.phone,
        network=canonical_network,
        email=req.email,
        sender_name=req.sender_name,
        callback_url=req.callback_url,
    )

    # Refuse a new payment on a number that already has one in flight.
    if await db.has_pending(req.aggregator, req.phone):
        raise HTTPException(
            409,
            {
                "error": "pending_exists",
                "message": f"Une transaction est déjà en cours (pending) sur le numéro {req.phone}.",
                "aggregator": req.aggregator,
                "phone": req.phone,
            },
        )

    async def _run_browser_and_save():
        """Browser flow + deduce/persist the curl template from the capture."""
        res = await agg.pay_via_browser(payment)
        charge = browser.get_charge_request(agg.charge_request_matcher)
        if charge:
            verify_reqs = browser.get_verify_requests(agg.verify_request_matcher)
            template = agg.extract_curl_template(
                charge, verify_reqs[-1] if verify_reqs else None, res.public_key
            )
            if template:
                await db.save_template(req.aggregator, template)
        return res

    # Resolve the template / 409 paths BEFORE creating the pending row, so a
    # rejected request never leaves a dangling 'pending' transaction.
    template = None
    if req.mode == "replay":
        template = await db.load_template(req.aggregator)
        if template is None:
            raise HTTPException(
                409,
                f"No stored curl template for '{req.aggregator}'. Run mode='browser' once to deduce it.",
            )
    elif req.mode == "auto":
        template = await db.load_template(req.aggregator)
        if template is None and not req.fallback_browser:
            raise HTTPException(
                409,
                f"No template for '{req.aggregator}' and fallback_browser=false. Run mode='browser' first.",
            )

    # Insert the audit row as 'pending' now; update it with the verdict at the end.
    tx_id = await db.insert_pending(req.aggregator, req.mode, payment)
    result = None
    try:
        if req.mode == "replay":
            result = await agg.replay(payment, template)
        elif req.mode == "browser":
            result = await _run_browser_and_save()
        else:  # auto: replay first, fall back to browser if inconclusive
            if template is not None:
                try:
                    result = await agg.replay(payment, template)
                except Exception as e:  # noqa: BLE001
                    log.warning("auto: replay raised (%s)", e)
                    result = None
                inconclusive = result is None or result.final_status in ("unknown", "timeout", "error", "")
                if inconclusive and req.fallback_browser:
                    log.info("auto: replay inconclusive -> falling back to browser")
                    result = await _run_browser_and_save()
                elif result is None:
                    raise HTTPException(502, "Replay failed and browser fallback disabled (fallback_browser=false)")
            else:
                result = await _run_browser_and_save()
    finally:
        # Always settle the pending row so it never stays stuck 'pending'.
        if result is None:
            result = PaymentResult()
            result.final_status = "error"
            result.final_message = "Paiement interrompu (erreur serveur)."
        await db.update_transaction(tx_id, result)

    return PayResponse(
        success=result.success,
        message=result.final_message or result.error,
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


@app.get("/transactions", tags=["transactions"], summary="Historique des transactions")
async def list_transactions(aggregator: str | None = None, limit: int = 50):
    """Dernières transactions auditées (vide si Supabase non configuré).

    Filtre optionnel par `aggregator`.
    """
    return {"transactions": await db.list_transactions(aggregator=aggregator, limit=limit)}


@app.get(
    "/status/{transaction_ref}",
    tags=["transactions"],
    summary="Statut d'une transaction",
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_status(transaction_ref: str):
    """Récupère une transaction stockée par sa référence."""
    tx = await db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    return tx


class DriveRequest(BaseModel):
    url: str
    objective: str
    phone: str = ""
    network: str = ""


@app.post("/drive", tags=["dev"], summary="Pilotage libre du navigateur (dev)")
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


@app.post("/test-llm", tags=["dev"], summary="Ping du LLM (dev)")
async def test_llm():
    """Quick test: send a simple prompt to DeepSeek and return the response."""
    if not llm:
        raise HTTPException(500, "Not initialized")

    resp = await llm.send(
        system_prompt="Reponds en JSON: {\"status\": \"ok\", \"message\": \"...\"}",
        user_content=[{"type": "text", "text": "Dis bonjour en une phrase."}],
    )
    return {"success": resp.success, "error": resp.error, "text": resp.text}
