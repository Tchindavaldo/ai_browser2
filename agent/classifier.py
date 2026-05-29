"""Keyword-based payment status classifier with self-learning.

Strategy:
  1. Compare a page text / charge response against a known keyword list
     (built from our real Flutterwave test responses).
  2. If a keyword matches -> return its status instantly (no LLM call).
  3. If nothing matches -> caller asks the LLM, which returns a status
     AND a new keyword. That keyword is persisted via add_keyword() so the
     list grows and future identical cases are matched without the LLM.

Statuses: successful | failed | cancelled | network_down | ussd_sent | pending
"""

import json
import logging
import os
import threading

log = logging.getLogger("ai_browser2")

_KEYWORDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keywords.json")
_lock = threading.Lock()

# Seed keywords harvested from real tests. Each entry: status -> [substrings].
# Order of statuses matters: cancelled/failed are checked before pending so an
# explicit refusal wins over a generic "pending".
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "successful": [
        "transaction successful",
        "paiement reussi",
        "paiement réussi",
        "payment successful",
        "approved",
        "00-approved",
        "success-completed",
    ],
    "cancelled": [
        "cancelled",
        "annule",
        "annulé",
        "transaction canceled",
        "refus",
        "rejected by user",
        "declined by user",
    ],
    "failed": [
        "transaction failed",
        "la transaction a echoue",
        "la transaction a échoué",
        "flw_err",
        "r1",
        "echoue",
        "échoué",
        "insufficient",
        "solde insuffisant",
        "declined",
        "do not honor",
    ],
    "network_down": [
        "network unavailable",
        "reseau indisponible",
        "réseau indisponible",
        "service unavailable",
        "operator down",
        "timeout from operator",
    ],
    "ussd_sent": [
        "150*50",
        "#150",
        "dial",
        "composez",
        "compose",
        "pending validation",
        "success-pending-validation",
        "dear customer, please dial",
    ],
    "pending": [
        "pending",
        "processing",
        "en cours",
        "veuillez patienter",
    ],
}

# Evaluation order: definitive outcomes first, ussd/pending last.
_ORDER = ["successful", "cancelled", "network_down", "failed", "ussd_sent", "pending"]


def _load() -> dict[str, list[str]]:
    data = {k: list(v) for k, v in DEFAULT_KEYWORDS.items()}
    try:
        if os.path.exists(_KEYWORDS_PATH):
            with open(_KEYWORDS_PATH, "r", encoding="utf-8") as f:
                learned = json.load(f)
            for status, kws in learned.items():
                data.setdefault(status, [])
                for kw in kws:
                    if kw not in data[status]:
                        data[status].append(kw)
    except Exception as e:
        log.warning("Could not load learned keywords: %s", e)
    return data


def classify(text: str) -> tuple[str, str] | None:
    """Return (status, matched_keyword) if a known keyword matches, else None."""
    if not text:
        return None
    low = text.lower()
    kw_map = _load()
    for status in _ORDER:
        for kw in kw_map.get(status, []):
            if kw and kw.lower() in low:
                return status, kw
    return None


def add_keyword(status: str, keyword: str) -> bool:
    """Persist a new keyword learned from the LLM. Returns True if added."""
    keyword = (keyword or "").strip()
    if not status or not keyword or len(keyword) < 2:
        return False
    with _lock:
        learned = {}
        try:
            if os.path.exists(_KEYWORDS_PATH):
                with open(_KEYWORDS_PATH, "r", encoding="utf-8") as f:
                    learned = json.load(f)
        except Exception:
            learned = {}
        learned.setdefault(status, [])
        # Don't duplicate against defaults or existing learned ones.
        existing = set(k.lower() for k in DEFAULT_KEYWORDS.get(status, []))
        existing |= set(k.lower() for k in learned[status])
        if keyword.lower() in existing:
            return False
        learned[status].append(keyword)
        try:
            with open(_KEYWORDS_PATH, "w", encoding="utf-8") as f:
                json.dump(learned, f, ensure_ascii=False, indent=2)
            log.info("Learned new keyword [%s]: %r", status, keyword)
            return True
        except Exception as e:
            log.warning("Could not persist keyword: %s", e)
            return False


# ---- Friendly network messaging ----

def network_label(network: str) -> str:
    n = (network or "").lower()
    if "orange" in n:
        return "Orange Money"
    if "mtn" in n:
        return "MTN Mobile Money"
    return network or "ce réseau"


def alt_network(network: str) -> str:
    n = (network or "").lower()
    if "orange" in n:
        return "MTN"
    if "mtn" in n:
        return "Orange"
    return "un autre réseau"


def network_failure_message(network: str) -> str:
    """Clear user-facing message when a mobile-money network is failing."""
    return (
        f"Le réseau {network_label(network)} est actuellement dérangé. "
        f"Veuillez réessayer avec {alt_network(network)}."
    )
