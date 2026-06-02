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


# ---------------------------------------------------------------------------
# Registre webhook ⇄ polling — une "boîte aux lettres" par transaction.
#
# Le webhook (endpoint POST) et le polling (dans /pay) surveillent le MÊME
# paiement. Le premier qui obtient un verdict terminal le DÉPOSE ici ; l'autre
# le voit et s'arrête. Indexé par transactionId (provider, unique) -> aucune
# collision entre transactions parallèles. État mémoire (process unique).
# ---------------------------------------------------------------------------
class _Registry:
    def __init__(self):
        self._events: dict[str, asyncio.Event] = {}
        self._verdicts: dict[str, dict] = {}

    def register(self, tx_id: str) -> asyncio.Event:
        """Ouvre une boîte pour cette transaction (appelé par le polling)."""
        ev = asyncio.Event()
        self._events[tx_id] = ev
        return ev

    def deliver(self, tx_id: str, verdict: dict) -> bool:
        """Dépose un verdict terminal (appelé par le webhook OU le polling).

        Retourne True si une boîte attendait (donc on a réveillé l'autre).
        """
        self._verdicts[tx_id] = verdict
        ev = self._events.get(tx_id)
        if ev is not None and not ev.is_set():
            ev.set()
            return True
        return False

    def take_verdict(self, tx_id: str) -> dict | None:
        return self._verdicts.get(tx_id)

    def close(self, tx_id: str):
        """Ferme la boîte (le polling l'appelle en fin de transaction)."""
        self._events.pop(tx_id, None)
        self._verdicts.pop(tx_id, None)


registry = _Registry()


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
    # Ouvre la boîte du registre : si le WEBHOOK reçoit le terminal avant nous,
    # il le dépose ici et on s'arrête immédiatement (pas d'attente du prochain tick).
    ev = registry.register(transaction_id)
    log.info("status_poll: début polling %s (max %ds, intervalle %ss)",
             transaction_id, timeout_s, interval_s)
    try:
        while time.monotonic() < deadline:
            # Le webhook a-t-il déjà livré un verdict terminal ?
            if ev.is_set():
                v = registry.take_verdict(transaction_id)
                if v:
                    log.info("status_poll: %s — verdict reçu par WEBHOOK: %s",
                             transaction_id, v.get("status"))
                    return v
            i += 1
            res = await fetch_status(transaction_id)
            if res is not None:
                last = res
                log.info("status_poll %s (#%d): %s (raw=%s)",
                         transaction_id, i, res["internal"], res["raw"])
                if res["internal"] in _TERMINAL:
                    verdict = {"status": res["internal"], "raw": res["raw"], "data": res["data"]}
                    # On a détecté en PREMIER : on dépose pour réveiller un
                    # éventuel doublon, mais surtout on conclut.
                    registry.deliver(transaction_id, verdict)
                    return verdict
            # Attente interrompue tôt si le webhook livre pendant le sleep.
            try:
                await asyncio.wait_for(ev.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass

        # Délai dépassé sans terminal. pending 17min -> expired (annulé opérateur).
        log.info("status_poll: %s — délai %ds écoulé sans verdict terminal -> expired",
                 transaction_id, timeout_s)
        return {
            "status": "expired",
            "raw": (last or {}).get("raw", ""),
            "data": (last or {}).get("data"),
        }
    finally:
        registry.close(transaction_id)
