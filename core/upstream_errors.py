"""Détection et présentation des pannes d'API amont (agrégateur).

Quand l'API d'un agrégateur (DigiKUNTZ aujourd'hui) est indisponible — 5xx,
timeout, DNS, connexion refusée — on ne veut PAS renvoyer le détail technique
(URL interne, code HTTP brut, stacktrace) au dev qui intègre nos endpoints. On
lui renvoie un code machine stable + un message FR clair. Le détail reste en
logs et en BD (table transaction_errors).
"""

import httpx

# Code machine exposé au dev intégrateur.
NETWORK_UNAVAILABLE = "network_unavailable"

# Message FR clair (sans détail technique).
NETWORK_UNAVAILABLE_MESSAGE = (
    "Service de paiement temporairement indisponible. "
    "Merci de réessayer dans quelques instants."
)


def is_upstream_unavailable(exc: BaseException) -> bool:
    """True si l'exception traduit une indisponibilité de l'API amont.

    Couvre: réponses 5xx (HTTPStatusError), et les erreurs réseau httpx
    (timeout, connexion, DNS) regroupées sous RequestError.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response is not None and exc.response.status_code >= 500
    if isinstance(exc, httpx.RequestError):
        # ConnectError, ConnectTimeout, ReadTimeout, PoolTimeout, etc.
        return True
    return False


def classify_upstream_error(exc: BaseException) -> str | None:
    """Retourne le code machine si `exc` est une panne amont, sinon None."""
    return NETWORK_UNAVAILABLE if is_upstream_unavailable(exc) else None
