"""Polling du statut d'une transaction DigiKUNTZ — source de vérité du verdict.

Une fois l'USSD demandé au client (le navigateur a fini son travail), le statut
final ne vient PLUS du navigateur : on interroge directement l'API DigiKUNTZ
(`GET {base}/transaction?transactionId=...`) jusqu'à un statut terminal ou
l'expiration du délai opérateur (17 min). Mécanisme calqué sur le backend
MoobilPay (checkPaymentStatusHandler / paymentEventRegistry).

Statuts DigiKUNTZ -> internes :
  payin_pending -> pending | payin_success -> successful
  payin_error   -> failed  | payin_closed  -> cancelled
"""

import asyncio
import logging
import time

import httpx

from core.config import settings

log = logging.getLogger("ai_browser2")

_dk = settings.digikuntz

# Mapping statut provider -> statut interne (mêmes valeurs que le reste du code).
STATUS_MAP = {
    "payin_pending": "pending",
    "payin_success": "successful",
    "payin_error": "failed",
    "payin_closed": "cancelled",
}

_TERMINAL = {"successful", "failed", "cancelled"}


def _headers() -> dict:
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "x-user-id": _dk.user_id,
        "x-secret-key": _dk.secret,
    }


async def fetch_status(transaction_id: str) -> dict | None:
    """Un appel statut DigiKUNTZ. Retourne {internal, raw, data} ou None si échec.

    `internal` = statut interne mappé ; `raw` = statut provider brut ;
    `data` = payload provider (peut contenir le détail). None = erreur réseau/HTTP
    (l'appelant continue de poller — un hoquet ne doit pas conclure).
    """
    url = f"{_dk.base}/transaction"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"transactionId": transaction_id},
                                    headers=_headers())
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("status_poll: appel échoué pour %s (%s)", transaction_id, type(e).__name__)
        return None
    raw = body.get("status", "")
    return {
        "internal": STATUS_MAP.get(raw, raw or "unknown"),
        "raw": raw,
        "data": body.get("data"),
    }


async def poll_until_terminal(
    transaction_id: str, timeout_s: int | None = None, interval_s: float = 5.0
) -> dict:
    """Poll le statut jusqu'à un verdict terminal ou l'expiration du délai.

    - terminal (successful/failed/cancelled) -> on renvoie {status, raw, data}.
    - 17 min écoulées sans terminal (toujours pending) -> FAIT opérateur
      universel: le délai est dépassé, on renvoie 'expired'. (Comme convenu, un
      pending qui dure 17 min = annulé par l'opérateur.)
    Le délai par défaut = settings.retry_window_s (1020s).
    """
    if timeout_s is None:
        timeout_s = settings.retry_window_s
    deadline = time.monotonic() + timeout_s
    last = None
    i = 0
    log.info("status_poll: début polling %s (max %ds, intervalle %ss)",
             transaction_id, timeout_s, interval_s)
    while time.monotonic() < deadline:
        i += 1
        res = await fetch_status(transaction_id)
        if res is not None:
            last = res
            if res["internal"] != (last or {}).get("_logged"):
                log.info("status_poll %s (#%d): %s (raw=%s)",
                         transaction_id, i, res["internal"], res["raw"])
            if res["internal"] in _TERMINAL:
                return {"status": res["internal"], "raw": res["raw"], "data": res["data"]}
        await asyncio.sleep(interval_s)

    # Délai dépassé sans terminal. pending 17min -> expired (annulé opérateur).
    log.info("status_poll: %s — délai %ds écoulé sans verdict terminal -> expired",
             transaction_id, timeout_s)
    return {
        "status": "expired",
        "raw": (last or {}).get("raw", ""),
        "data": (last or {}).get("data"),
    }
