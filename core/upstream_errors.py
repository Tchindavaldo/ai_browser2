"""Détection et présentation des pannes d'API amont (agrégateur).

Quand l'API d'un agrégateur (DigiKUNTZ aujourd'hui) est indisponible — 5xx,
timeout, DNS, connexion refusée — on ne veut PAS renvoyer le détail technique
(URL interne, code HTTP brut, stacktrace) au dev qui intègre nos endpoints. On
lui renvoie un code machine stable + un message FR clair. Le détail reste en
logs et en BD (table transaction_errors).
"""

import httpx

# Codes machine exposés au dev intégrateur. Tous deux = panne temporaire d'un
# service amont (l'intégrateur peut réessayer), mais distincts pour qu'il sache
# QUOI est en panne :
#   - network_unavailable  : notre fournisseur de paiement (API DigiKUNTZ) est down.
#   - operator_unavailable : le réseau Mobile Money de l'opérateur (Orange/MTN)
#     est dérangé (détecté au /charge, avant tout USSD).
NETWORK_UNAVAILABLE = "network_unavailable"
OPERATOR_UNAVAILABLE = "operator_unavailable"

# Messages FR clairs (sans détail technique).
NETWORK_UNAVAILABLE_MESSAGE = (
    "Service de paiement temporairement indisponible. "
    "Merci de réessayer dans quelques instants."
)
OPERATOR_UNAVAILABLE_MESSAGE = (
    "Le réseau Mobile Money de l'opérateur est momentanément indisponible. "
    "Merci de réessayer dans quelques instants."
)

# Codes amont qui doivent produire une réponse HTTP 503 propre côté /pay.
UPSTREAM_CODES = {NETWORK_UNAVAILABLE, OPERATOR_UNAVAILABLE}

# Map code -> message, pour /pay.
UPSTREAM_MESSAGES = {
    NETWORK_UNAVAILABLE: NETWORK_UNAVAILABLE_MESSAGE,
    OPERATOR_UNAVAILABLE: OPERATOR_UNAVAILABLE_MESSAGE,
}


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
