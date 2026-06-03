"""MobileWallet backend — REST server routing payments through aggregators.

A payment is dispatched by (aggregator, mode):
  - mode="browser": full AI-driven Playwright flow; on success the deduced curl
    template is persisted to the DB for later replay.
  - mode="replay": reproduce the payment via the stored template, no browser.
Every attempt is audited in the transactions table (when Supabase is configured).
"""

import dataclasses
import logging
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

# Importing the aggregators package registers all aggregators in the registry.
import aggregators.digikuntz  # noqa: F401
from core.base import CurlTemplate, PaymentRequest, PaymentResult
from core.browser import BrowserController
from core.config import settings
from core.db import db
from core.error_tracking import build_errors
from core.llm_client import LlmClient, LlmConfig
from core.upstream_errors import (
    NETWORK_UNAVAILABLE, OPERATOR_UNAVAILABLE, UPSTREAM_CODES, UPSTREAM_MESSAGES,
)
from core import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
# Quiet the noisy third-party loggers so the AI workflow stands out in the
# console. Each HTTP call to DeepSeek/Supabase and every access line used to
# drown the agent's thoughts/actions.
for noisy in ("httpx", "httpcore", "uvicorn.access", "hpack", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("ai_browser2")


def _seconds_since(created_at) -> float | None:
    """Seconds elapsed since a Supabase timestamp (ISO str), or None if unknown.

    Supabase returns created_at as an ISO-8601 string (UTC, often with a 'Z' or
    +00:00 offset). Returns None on any parse failure so the guard degrades to
    'allow' rather than blocking on a malformed value.
    """
    if not created_at:
        return None
    try:
        s = created_at.replace("Z", "+00:00") if isinstance(created_at, str) else created_at
        dt = datetime.fromisoformat(s) if isinstance(s, str) else s
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError) as e:
        log.warning("_seconds_since parse failed for %r: %s", created_at, e)
        return None


def _fmt_duration(seconds: int) -> str:
    """Human-friendly FR duration for the retry message (e.g. '12 min 30 s')."""
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m and s:
        return f"{m} min {s} s"
    if m:
        return f"{m} min"
    return f"{s} s"


# Global state
browser: BrowserController | None = None
llm: LlmClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser, llm

    # Start browser pool (visible by default so you can watch!). The max-tabs
    # threshold comes from env, then is overridden by the DB value if present.
    headless = settings.headless
    max_tabs = await db.get_max_tabs(default=settings.max_tabs_per_browser)
    browser = BrowserController(headless=headless, max_tabs=max_tabs)
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
    {"name": "transactions", "description": "Gestion des transactions exposée au client."},
    {"name": "admin", "description": "Audit/debug réservé à l'admin backend (non documenté pour le client)."},
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
    """Vue CLIENT (dev qui intègre l'API) — strictement l'essentiel métier.

    Tout le détail technique (charge_url, plaintext, curl, trace, tokens…) est
    réservé à l'admin (voir PayResponseDebug + ?debug=true). Ne JAMAIS ajouter de
    champ interne ici.
    """
    success: bool = Field(..., description="Vrai si le paiement a abouti.")
    status: str = Field("", description="successful | ussd_sent | failed | cancelled | pending.")
    message: str = Field("", description="Message clair pour l'utilisateur final.")
    transaction_id: str = Field("", description="Référence transaction de l'agrégateur.")
    code: str = Field("", description="Code machine en cas d'erreur (ex. network_unavailable, operator_unavailable). Vide si OK.")


class PayResponseDebug(PayResponse):
    """Vue ADMIN/DEBUG — la vue client + tout le détail technique.

    Renvoyée uniquement quand l'admin passe ?debug=true (paramètre non documenté
    dans Swagger). À terme: endpoint admin dédié protégé (cf.
    todo/migrer-vue-admin-endpoint-dedie.md).
    """
    error: str = Field("", description="Message d'erreur technique éventuel.")
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
    final_status: str = ""
    final_message: str = ""
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


@app.post("/webhook/digikuntz", tags=["system"],
          summary="Webhook DigiKUNTZ (callback statut)", include_in_schema=False)
async def digikuntz_webhook(payload: dict):
    """Callback serveur DigiKUNTZ : POST {id, status, data} à chaque changement.

    On répond 200 TOUT DE SUITE (sinon DigiKUNTZ retry), puis on MAJ la BD et on
    dépose le verdict terminal dans le registre pour débloquer le polling en
    cours (le premier — webhook ou polling — qui a le terminal gagne, l'autre est
    idempotent). Identifié par l'id provider -> aucune collision en parallèle.
    Ne fonctionne que si ce backend a une URL publique joignable (cf.
    todo/webhook-digikuntz.md).
    """
    from aggregators.digikuntz import status_poll
    provider_id = str(payload.get("id", ""))
    raw = payload.get("status", "")
    internal = status_poll.STATUS_MAP.get(raw, raw or "unknown")
    log.info("🪝 Webhook DigiKUNTZ: id=%s status=%s -> %s", provider_id, raw, internal)

    if provider_id and internal in ("successful", "failed", "cancelled"):
        # Débloque le polling en cours pour cette transaction (registre mémoire).
        status_poll.registry.deliver(
            provider_id, {"status": internal, "raw": raw, "data": payload.get("data")})
        # MAJ la ligne BD si on la retrouve (settle redondant mais idempotent).
        row = await db.get_transaction_by_provider_id(provider_id)
        if row:
            await db.update_status_by_provider_id(provider_id, internal,
                                                  message=f"Webhook DigiKUNTZ: {raw}")
    return {"received": True}


class MaxTabsRequest(BaseModel):
    max_tabs: int = Field(..., ge=1, le=200,
                          description="Nb max d'onglets par instance Chrome.",
                          examples=[20])


@app.get("/config/max-tabs", tags=["system"], summary="Seuil d'onglets par navigateur")
async def get_max_tabs():
    """Nombre max d'onglets par Chrome avant d'en lancer un nouveau (concurrence)."""
    pools = len(browser._browsers) if browser else 0
    return {"max_tabs": browser.max_tabs if browser else None, "open_browsers": pools}


@app.put("/config/max-tabs", tags=["system"], summary="Modifier le seuil d'onglets")
async def set_max_tabs(req: MaxTabsRequest):
    """Change le seuil (persisté en BD, appliqué immédiatement au pool en cours)."""
    if not browser:
        raise HTTPException(500, "Not initialized")
    await db.set_max_tabs(req.max_tabs)   # persiste (no-op si Supabase off)
    browser.max_tabs = req.max_tabs       # effet immédiat sur le pool vivant
    log.info("max_tabs_per_browser -> %d", req.max_tabs)
    return {"max_tabs": browser.max_tabs}


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
        503: {"description": "Service de paiement amont temporairement indisponible "
                             "(code=network_unavailable). Réessayer plus tard."},
    },
)
async def pay(
    req: PayRequest,
    debug: bool = Query(False, include_in_schema=False),
):
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

    # Garde anti-doublon par numéro (vaut pour curl ET navigateur).
    #  - dernière transaction encore 'pending' → bloque : à confirmer ou annuler.
    #  - dernière transaction non-succès ET dans la fenêtre opérateur (selon le
    #    réseau) → bloque avec le temps restant à attendre.
    last = await db.last_transaction_for_number(req.aggregator, req.phone)
    if last:
        status = (last.get("status") or "").lower()
        if status == "pending":
            raise HTTPException(
                409,
                {
                    "error": "pending_exists",
                    "message": (
                        f"Une transaction est déjà en cours (pending) sur le numéro "
                        f"{req.phone}. Veuillez la confirmer ou l'annuler."
                    ),
                    "aggregator": req.aggregator,
                    "phone": req.phone,
                    "transaction_id": last.get("id"),
                },
            )
        # Délai anti-doublon (selon le réseau) : on ne bloque QUE 'cancelled' — c'est le
        # seul cas où un USSD a réellement été envoyé puis non validé (la transaction
        # peut encore se régler côté opérateur, donc on évite un doublon). Tout le
        # reste est rechargeable tout de suite:
        #   - solde insuffisant / refus / échec → l'utilisateur réessaie direct,
        #   - panne API / opérateur down → rien n'a été tenté.
        if status == "cancelled":
            # Le délai court depuis le PASSAGE à cancelled (cancelled_at), pas la
            # création ; fallback created_at si la colonne est absente/vide.
            elapsed = _seconds_since(last.get("cancelled_at") or last.get("created_at"))
            # Délai selon l'opérateur de la transaction stockée, pas un délai unique.
            window = settings.retry_window_for(last.get("network", ""))
            if elapsed is not None and elapsed < window:
                remaining = int(window - elapsed)
                raise HTTPException(
                    409,
                    {
                        "error": "retry_too_soon",
                        "message": (
                            f"Une transaction récente est en cours de validation sur le "
                            f"numéro {req.phone}. Réessayez dans {_fmt_duration(remaining)}."
                        ),
                        "aggregator": req.aggregator,
                        "phone": req.phone,
                        "last_status": status,
                        "retry_after_s": remaining,
                    },
                )

    async def _run_browser_and_save():
        """Browser flow + persist the curl template deduced during the run.

        The runner builds res.curl_template while it still holds the (now
        isolated) browser session; we just persist it here.
        """
        res = await agg.pay_via_browser(payment, tx_id=tx_id)
        if res.curl_template:
            await db.save_template(req.aggregator, res.curl_template)
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
    engine_used = "replay"  # quel moteur a réellement produit le résultat
    try:
        if req.mode == "replay":
            result = await agg.replay(payment, template)
            engine_used = "replay"
        elif req.mode == "browser":
            result = await _run_browser_and_save()
            engine_used = "browser"
        else:  # auto: replay first, fall back to browser if inconclusive
            if template is not None:
                try:
                    result = await agg.replay(payment, template)
                    engine_used = "replay"
                except Exception as e:  # noqa: BLE001
                    log.warning("auto: replay raised (%s)", e)
                    result = None
                inconclusive = result is None or result.final_status in ("unknown", "timeout", "error", "")
                if inconclusive and req.fallback_browser:
                    log.info("auto: replay inconclusive -> falling back to browser")
                    result = await _run_browser_and_save()
                    engine_used = "browser"
                elif result is None:
                    raise HTTPException(502, "Replay failed and browser fallback disabled (fallback_browser=false)")
            else:
                result = await _run_browser_and_save()
                engine_used = "browser"
    finally:
        # Always settle the pending row so it never stays stuck 'pending'.
        if result is None:
            result = PaymentResult()
            result.final_status = "error"
            result.final_message = "Paiement interrompu (erreur serveur)."
        # Réseau opérateur (Orange/MTN) dérangé au /charge, AVANT tout USSD :
        # même traitement qu'une panne amont — la transaction n'a pas atteint
        # l'utilisateur, on l'expose comme 'operator_unavailable'. Vaut pour les
        # DEUX moteurs (browser conclut 'network_down', replay aussi).
        if not result.error_code and result.final_status == "network_down":
            result.error_code = OPERATOR_UNAVAILABLE
        # Panne amont (API down) OU opérateur down : la transaction n'a JAMAIS
        # abouti côté opérateur — on aligne le statut sur le code pour que la
        # garde anti-doublon ne bloque PAS le numéro (rien de débité).
        if result.error_code in UPSTREAM_CODES:
            result.final_status = result.error_code
            result.final_message = UPSTREAM_MESSAGES[result.error_code]
        # Dérive les erreurs détaillées (table transaction_errors) depuis le
        # verdict, en distinguant le moteur et la source (ai/browser/transaction).
        result.errors = build_errors(result, engine_used)
        await db.update_transaction(tx_id, result)

    # Panne amont (API agrégateur OU réseau opérateur) : on a tracé le détail en
    # BD + logs ci-dessus ; au dev intégrateur on renvoie un 503 propre avec un
    # code stable + message FR, SANS exposer l'URL interne ni la stacktrace. Même
    # forme de réponse quel que soit le moteur (browser/replay) et le type de panne.
    if result.error_code in UPSTREAM_CODES:
        log.warning("Upstream indisponible (tx_id=%s, code=%s): %s",
                    tx_id, result.error_code, result.error or result.final_message)
        raise HTTPException(
            503,
            {"code": result.error_code, "message": UPSTREAM_MESSAGES[result.error_code]},
        )

    # Vue CLIENT (par défaut) : l'essentiel métier uniquement.
    client_status = result.final_status or result.payment_status or ""
    client = PayResponse(
        success=result.success,
        status=client_status,
        message=result.final_message or result.error,
        transaction_id=result.transaction_id,
        code=result.error_code or "",
    )
    if not debug:
        return client

    # Vue ADMIN/DEBUG (?debug=true) : la vue client + tout le détail technique.
    # JSONResponse explicite pour contourner le filtrage response_model=PayResponse
    # (qui sinon retirerait les champs debug). Param debug non documenté (Swagger).
    full = PayResponseDebug(
        **client.model_dump(),
        error=result.error,
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
    return JSONResponse(content=full.model_dump())


@app.get("/transactions", tags=["admin"], summary="Historique des transactions (admin)",
         include_in_schema=False)
async def list_transactions(aggregator: str | None = None, limit: int = 50):
    """Dernières transactions auditées (vide si Supabase non configuré).

    Filtre optionnel par `aggregator`.
    """
    return {"transactions": await db.list_transactions(aggregator=aggregator, limit=limit)}


@app.get(
    "/status/{transaction_ref}",
    tags=["admin"],
    summary="Statut d'une transaction (admin)",
    include_in_schema=False,
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_status(transaction_ref: str):
    """Récupère une transaction stockée par sa référence."""
    tx = await db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    return tx


@app.get(
    "/transactions/{transaction_ref}/trace",
    tags=["admin"],
    summary="Trace IA d'une transaction (admin, mode browser)",
    include_in_schema=False,
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_trace(transaction_ref: str):
    """Renvoie la trace tour-par-tour de l'agent IA pour une transaction browser.

    Chaque entrée : ce que l'IA a vu (url, nb d'éléments), sa pensée, les actions
    jouées, et si l'objectif a été atteint. Vide pour un paiement en mode replay.
    """
    tx = await db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    traces = await db.get_traces(tx["id"])
    return {"transaction_ref": transaction_ref, "turns": len(traces), "trace": traces}


@app.get(
    "/transactions/{transaction_ref}/errors",
    tags=["admin"],
    summary="Erreurs détaillées d'une transaction (admin, browser/replay)",
    include_in_schema=False,
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_errors(transaction_ref: str):
    """Renvoie les erreurs détaillées d'une transaction.

    Chaque entrée précise le moteur (`engine`: browser/replay) et la SOURCE
    (`source`: ai / browser / transaction / replay) pour savoir exactement où ça
    a cassé, plus la catégorie, le message clair et les détails. Vide si la
    transaction a réussi.
    """
    tx = await db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    errors = await db.get_errors(tx["id"])
    return {"transaction_ref": transaction_ref, "count": len(errors), "errors": errors}


@app.post(
    "/transactions/{tx_id}/cancel",
    tags=["transactions"],
    summary="Débloquer une transaction pending",
    responses={
        404: {"description": "Aucune transaction 'pending' avec cet id."},
        503: {"description": "Supabase non configuré."},
    },
)
async def cancel_transaction(tx_id: int):
    """Force une transaction restée **pending** à **cancelled**.

    Sert à débloquer une transaction coincée (ex: serveur interrompu avant le
    verdict) qui empêcherait un nouveau paiement sur le même numéro
    (`409 pending_exists`). N'agit que sur une ligne encore `pending` — ne
    réécrit jamais un verdict déjà acté.
    """
    if not db.enabled:
        raise HTTPException(503, "Supabase non configuré.")
    row = await db.cancel_pending(tx_id)
    if row is None:
        raise HTTPException(404, f"Aucune transaction 'pending' avec l'id {tx_id}.")
    return {"cancelled": row}


class TemplateBody(BaseModel):
    """Champs du curl template (tous optionnels — seuls ceux fournis sont posés)."""
    charge_url: str = ""
    verify_url: str = ""
    init_url: str = ""
    upgrade_url: str = ""
    hosted_pay_url: str = ""
    headers: dict = Field(default_factory=dict)
    payload_skeleton: dict = Field(default_factory=dict)
    public_key_rsa: str = ""
    flw_pub_key: str = ""
    force: bool = Field(False, description="Forcer une nouvelle version même si identique à l'actif.")


@app.get(
    "/aggregators/{name}/template",
    tags=["admin"],
    summary="Template curl actif d'un agrégateur (admin)",
    include_in_schema=False,
    responses={404: {"description": "Agrégateur inconnu ou aucun template actif."}},
)
async def get_template(name: str):
    """Renvoie le template de replay actif de l'agrégateur (404 si aucun)."""
    if registry.get(name) is None:
        raise HTTPException(404, {"error": "unknown_aggregator", "supported_aggregators": registry.names()})
    tpl = await db.load_template(name)
    if tpl is None:
        raise HTTPException(404, f"No active template for '{name}'. Run mode='browser' or POST one.")
    return {"aggregator": name, "template": dataclasses.asdict(tpl)}


@app.post(
    "/aggregators/{name}/template",
    tags=["admin"],
    summary="Ajouter/mettre à jour manuellement le template curl (admin)",
    include_in_schema=False,
    responses={
        404: {"description": "Agrégateur inconnu."},
        503: {"description": "Supabase non configuré (persistance désactivée)."},
    },
)
async def set_template(name: str, body: TemplateBody):
    """Ajoute manuellement un template de replay pour l'agrégateur.

    Par défaut **append-si-différent** (comme le mode browser) : si identique à
    l'actif, rien n'est ajouté. `force=true` crée toujours une nouvelle version.
    """
    if registry.get(name) is None:
        raise HTTPException(404, {"error": "unknown_aggregator", "supported_aggregators": registry.names()})
    if not db.enabled:
        raise HTTPException(503, "Supabase non configuré — impossible de persister le template.")
    fields = {f.name for f in dataclasses.fields(CurlTemplate)}
    tpl = CurlTemplate(**{k: v for k, v in body.model_dump().items() if k in fields})
    saved = await db.save_template(name, tpl, force=body.force)
    return {"aggregator": name, "saved": saved, "template": dataclasses.asdict(tpl)}


class DriveRequest(BaseModel):
    url: str
    objective: str
    phone: str = ""
    network: str = ""


@app.post("/drive", tags=["dev"], summary="Pilotage libre du navigateur (dev)",
          include_in_schema=False)
async def drive(req: DriveRequest):
    """Drive the browser to a URL and let the AI agent handle it."""
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

    from core.reasoning_loop import ReasoningLoop

    # Acquire an isolated session for this dev run, release it when done.
    session = await browser.acquire_session()
    try:
        session.start_capture()
        await session.goto(req.url)
        loop = ReasoningLoop(session, llm, max_turns=15)
        result = await loop.run(req.objective)
        captured = session.stop_capture()
        charge = session.get_flutterwave_charge()

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
    finally:
        await browser.release_session(session)


@app.post("/test-llm", tags=["dev"], summary="Ping du LLM (dev)",
          include_in_schema=False)
async def test_llm():
    """Quick test: send a simple prompt to DeepSeek and return the response."""
    if not llm:
        raise HTTPException(500, "Not initialized")

    resp = await llm.send(
        system_prompt="Reponds en JSON: {\"status\": \"ok\", \"message\": \"...\"}",
        user_content=[{"type": "text", "text": "Dis bonjour en une phrase."}],
    )
    return {"success": resp.success, "error": resp.error, "text": resp.text}
