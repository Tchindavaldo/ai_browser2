"""Construit les entrées de la table transaction_errors à partir d'un résultat.

Distingue le MOTEUR (browser/replay) et la SOURCE de l'erreur — essentiel côté
navigateur où l'échec peut venir de l'IA, du navigateur, ou de la transaction.
On ne réinterprète RIEN : on persiste le verdict que l'IA (ou le replay) a déjà
posé, en y ajoutant une catégorie dérivée du statut/contexte pour le requêtage.

Chaque entrée: {engine, source, category, message, detail, turn}.
"""

from core.base import PaymentResult

# Statut final -> catégorie d'erreur (pour transaction_errors.category).
_STATUS_CATEGORY = {
    "failed": "payment_failed",
    "cancelled": "cancelled",
    "network_down": "network_down",
    "timeout": "timeout",
    "error": "error",
    "unknown": "unknown",
}

# Statuts considérés comme NON-erreur (rien à logger).
_OK_STATUSES = {"successful", "completed", "ussd_sent", "pending"}


def _category_from_message(message: str, fallback: str) -> str:
    """Affine la catégorie depuis le message clair (sans réinterpréter le verdict)."""
    m = (message or "").lower()
    if "solde insuffisant" in m or "insufficient" in m:
        return "insufficient_funds"
    if "dérangé" in m or "derange" in m or "réseau" in m and "indispon" in m:
        return "network_down"
    if "délai" in m or "delai" in m or "timed out" in m or "non validée" in m:
        return "timeout"
    if "checkout" in m and ("repond" in m or "répond" in m):
        return "form_load"
    if "authentification" in m or "session de paiement perdue" in m:
        return "session_lost"
    return fallback


def build_errors(result: PaymentResult, engine: str) -> list[dict]:
    """Dérive les entrées transaction_errors d'un résultat (vide si succès).

    - engine='replay' : transaction déterministe -> au plus 1 entrée
      (source='transaction' si le paiement a échoué, sinon 'replay' pour un
      échec technique du flux — ex. pas de flw_ref).
    - engine='browser' : on distingue
        source='ai'          : l'agent n'a pas conclu / a échoué à raisonner,
        source='browser'     : formulaire jamais chargé (form_load_diagnostic),
        source='transaction' : le paiement lui-même a échoué (verdict de l'IA).
    """
    status = (result.final_status or result.payment_status or "").lower()
    is_failure = bool(status) and status not in _OK_STATUSES

    errors: list[dict] = []

    if engine == "replay":
        if result.error and not status:
            # Échec technique du flux replay (ex. pas de flw_ref, API create KO).
            errors.append({
                "engine": "replay",
                "source": "replay",
                "category": "flow_error",
                "message": result.error,
                "detail": {"charge_response": result.flutterwave_charge_response[:2000]},
                "turn": None,
            })
        elif is_failure:
            errors.append({
                "engine": "replay",
                "source": "transaction",
                "category": _category_from_message(
                    result.final_message, _STATUS_CATEGORY.get(status, "error")
                ),
                "message": result.final_message or status,
                "detail": {"charge_response": result.flutterwave_charge_response[:2000]},
                "turn": None,
            })
        return errors

    # engine == 'browser'
    # 1) Erreur navigateur: le formulaire n'a jamais chargé (diagnostic turn-0).
    if result.form_load_diagnostic:
        errors.append({
            "engine": "browser",
            "source": "browser",
            "category": "form_load",
            "message": "Formulaire de paiement non chargé au 1er affichage.",
            "detail": result.form_load_diagnostic,
            "turn": 0,
        })

    # 2) Erreur IA: l'agent n'a pas conclu (loop error) alors qu'on n'a pas de
    #    verdict de transaction — on ne sait pas si le paiement est parti.
    if result.error and not is_failure:
        errors.append({
            "engine": "browser",
            "source": "ai",
            "category": "ai_no_conclusion",
            "message": result.error,
            "detail": {"error_signals": result.error_signals or {}},
            "turn": None,
        })

    # 3) Erreur transaction: l'IA a conclu un échec après le paiement.
    if is_failure:
        errors.append({
            "engine": "browser",
            "source": "transaction",
            "category": _category_from_message(
                result.final_message, _STATUS_CATEGORY.get(status, "error")
            ),
            "message": result.final_message or status,
            "detail": {"error_signals": result.error_signals or {}},
            "turn": None,
        })

    return errors
