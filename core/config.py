"""Centralized configuration loaded from environment (.env).

All secrets and per-aggregator endpoints live here, read from environment
variables. The real `.env` (gitignored) holds the actual secrets; see
.env.example for the full list of keys. Import `settings` and read attributes
instead of hardcoding constants in aggregator modules.
"""

import os
from dataclasses import dataclass, field

try:
    # Load .env if python-dotenv is installed (no-op if file absent).
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv optional in environments that inject env directly
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass
class DigikuntzConfig:
    base: str = field(default_factory=lambda: _env("DIGIKUNTZ_BASE", "https://app.digikuntz.com/dev"))
    user_id: str = field(default_factory=lambda: _env("DIGIKUNTZ_USER_ID"))
    secret: str = field(default_factory=lambda: _env("DIGIKUNTZ_SECRET"))
    callback_url: str = field(default_factory=lambda: _env("DIGIKUNTZ_CALLBACK_URL", "https://app.digikuntz.com/callback"))

    # Flutterwave (used by the DigiKUNTZ replay flow). These act as DEFAULTS;
    # a DB template overrides them when present.
    flw_pub_key: str = field(default_factory=lambda: _env("FLW_PUB_KEY"))
    flw_charge_url: str = field(default_factory=lambda: _env("FLW_CHARGE_URL", "https://api.ravepay.co/flwv3-pug/getpaidx/api/charge?use_polling=1"))
    flw_verify_url: str = field(default_factory=lambda: _env("FLW_VERIFY_URL", "https://api.ravepay.co/flwv3-pug/getpaidx/api/verify/mpesa"))
    flw_init_url: str = field(default_factory=lambda: _env("FLW_INIT_URL", "https://api.ravepay.co/v3/checkout/initialize"))
    flw_upgrade_url: str = field(default_factory=lambda: _env("FLW_UPGRADE_URL", "https://api.ravepay.co/v2/checkout/upgrade"))


@dataclass
class Settings:
    # LLM
    deepseek_api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-v4-flash"))

    # Supabase
    supabase_url: str = field(default_factory=lambda: _env("SUPABASE_URL"))
    supabase_key: str = field(default_factory=lambda: _env("SUPABASE_KEY"))

    # Runtime
    headless: bool = field(default_factory=lambda: _env("HEADLESS", "0") == "1")
    port: int = field(default_factory=lambda: int(_env("PORT", "7332")))

    # Fenêtre de retry / délai opérateur (secondes) — DÉPEND DE L'OPÉRATEUR :
    # le délai avant qu'un opérateur Mobile Money auto-annule une transaction non
    # validée diffère. Observé : Orange ~17 min, MTN ~10 min (+1 min de marge).
    # Sert (a) au polling verify (budget d'attente), (b) au plafond boucle
    # navigateur, (c) à la garde anti-doublon par numéro côté /pay.
    retry_window_s: int = field(default_factory=lambda: int(_env("RETRY_WINDOW_S", "1020")))
    retry_window_orange_s: int = field(default_factory=lambda: int(_env("RETRY_WINDOW_ORANGE_S", "1020")))
    retry_window_mtn_s: int = field(default_factory=lambda: int(_env("RETRY_WINDOW_MTN_S", "600")))

    def retry_window_for(self, network: str) -> int:
        """Délai opérateur (s) selon le réseau. Orange 17min, MTN 10min ;
        fallback retry_window_s pour un réseau inconnu."""
        n = (network or "").lower()
        if "orange" in n:
            return self.retry_window_orange_s
        if "mtn" in n:
            return self.retry_window_mtn_s
        return self.retry_window_s

    # Concurrence navigateur : nb max d'onglets par instance Chrome avant d'en
    # lancer une nouvelle. Valeur par défaut depuis l'env ; surchargée au boot
    # par la valeur persistée en BD (app_settings) et modifiable via API.
    max_tabs_per_browser: int = field(
        default_factory=lambda: int(_env("MAX_TABS_PER_BROWSER", "15"))
    )

    # Per-aggregator config
    digikuntz: DigikuntzConfig = field(default_factory=DigikuntzConfig)


# Single shared instance loaded once at import.
settings = Settings()
