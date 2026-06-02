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


def extract_verify_params(captured) -> dict | None:
    """Extrait modalauditid + flw_ref + PBFPubKey des requêtes verify/mpesa
    capturées par le navigateur, pour pouvoir poller verify EN HTTP après la
    fermeture du navigateur (comme le replay). Retourne None si introuvable.

    `captured` = liste de CapturedRequest (session.captured_requests).
    """
    import json as _json
    for r in reversed(captured):  # la plus récente d'abord
        if "/verify/mpesa" in r.url and r.method == "POST" and r.request_body:
            try:
                body = _json.loads(r.request_body)
            except (ValueError, TypeError):
                continue
            flw_ref = body.get("flw_ref")
            modalauditid = body.get("modalauditid")
            if flw_ref and modalauditid:
                return {
                    "modalauditid": modalauditid,
                    "flw_ref": flw_ref,
                    "pub_key": body.get("PBFPubKey", _dk.flw_pub_key),
                }
    return None


async def poll_verify_flutterwave(
    verify_params: dict, network: str, timeout_s: int | None = None,
    provider_id: str | None = None,
) -> dict:
    """Polling verify/mpesa Flutterwave PROPRE AU NAVIGATEUR.

    Boucle indépendante de celle du replay (step4_poll_verify) — volontairement
    dupliquée pour que toute évolution côté navigateur (ex. coordination webhook
    via le registre) NE TOUCHE PAS le replay, et inversement. Même comportement
    de fond (poll verify, interpret_verify), mais autonome.

    Si `provider_id` est fourni, on écoute AUSSI le registre : si le webhook
    DigiKUNTZ livre un verdict terminal pendant qu'on poll, on s'arrête net
    (option 2). Le premier des deux (poll verify OU webhook) qui a un terminal
    gagne. 17min sans terminal -> expired.
    """
    from . import replay_flow  # pour interpret_verify + ReplayConfig (lecture seule)
    if timeout_s is None:
        timeout_s = settings.retry_window_for(network)  # Orange 17min / MTN 10min
    cfg = replay_flow.ReplayConfig.defaults()
    if verify_params.get("pub_key"):
        cfg.pub_key = verify_params["pub_key"]
    modalauditid = verify_params["modalauditid"]
    flw_ref = verify_params["flw_ref"]

    ev = registry.register(provider_id) if provider_id else None
    log.info("poll_verify_flutterwave (nav): flw_ref=%s (max %ds, webhook=%s)",
             flw_ref, timeout_s, bool(provider_id))

    start = time.monotonic()
    deadline = start + timeout_s
    i = 0
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while time.monotonic() < deadline:
                # (a) le webhook a-t-il déjà livré le verdict ? -> on s'arrête net
                if ev is not None and ev.is_set():
                    v = registry.take_verdict(provider_id)
                    if v and v.get("status") in _TERMINAL:
                        log.info("poll_verify_flutterwave: verdict WEBHOOK %s", v["status"])
                        return {"status": v["status"],
                                "message": self_friendly_msg(v["status"], network)}
                i += 1
                try:
                    resp = await client.post(
                        cfg.verify_url,
                        json={"modalauditid": modalauditid,
                              "PBFPubKey": cfg.pub_key, "flw_ref": flw_ref},
                        headers=cfg.headers,
                    )
                    data = resp.json()
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(2)
                    continue

                status = data.get("data", {}).get("status", "pending")
                code = data.get("data", {}).get("chargeResponseCode", "")
                elapsed = int(time.monotonic() - start)
                log.info("verify (nav) poll %d (t+%ds): status=%s code=%s",
                         i, elapsed, status, code)

                verdict = replay_flow.interpret_verify(data, network)
                if verdict and verdict[0] in _TERMINAL:
                    # On a le verdict en premier : on le dépose pour un webhook
                    # tardif (idempotence) et on conclut.
                    if provider_id:
                        registry.deliver(provider_id,
                                         {"status": verdict[0], "raw": status, "data": data})
                    return {"status": verdict[0], "message": verdict[1]}

                # Attente du prochain tick, interrompue tôt si le webhook livre.
                if ev is not None:
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(2)

        # 17min écoulées sans terminal = délai opérateur dépassé.
        return {"status": "expired",
                "message": ("Délai de validation dépassé (17 min). La demande a "
                            "expiré côté opérateur. Vous pouvez relancer un paiement.")}
    finally:
        if provider_id:
            registry.close(provider_id)


def self_friendly_msg(status: str, network: str) -> str:
    """Message FR court selon le statut terminal (verdict reçu par webhook)."""
    if status == "successful":
        return "Paiement validé avec succès."
    if status == "cancelled":
        return "Paiement annulé / refusé sur le USSD."
    return "Paiement échoué (refus ou solde insuffisant)."
