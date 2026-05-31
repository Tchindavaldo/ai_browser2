"""Supabase persistence layer (optional).

Stores a transaction audit row per payment attempt and the reusable per-aggregator
curl template. Degrades gracefully: if SUPABASE_URL/KEY are not set, `Database`
is disabled and all calls are safe no-ops so the server still runs locally.

Tables (see schema/supabase.sql):
  - transactions(id, aggregator, mode, amount, phone, network, email,
      transaction_ref, status, message, success, charge_response, error_signals,
      created_at)
  - curl_templates(id, aggregator, template jsonb, created_at, updated_at)
"""

import dataclasses
import logging
from typing import Any

from core.base import CurlTemplate, PaymentRequest, PaymentResult
from core.config import settings

log = logging.getLogger("ai_browser2")


class Database:
    def __init__(self) -> None:
        self._client = None
        if settings.supabase_url and settings.supabase_key:
            try:
                from supabase import create_client

                self._client = create_client(settings.supabase_url, settings.supabase_key)
                log.info("Supabase connected")
            except Exception as e:  # noqa: BLE001 — never block startup on DB
                log.warning("Supabase init failed (%s); persistence disabled", e)
        else:
            log.info("Supabase not configured; persistence disabled")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # --- transactions audit ---
    def save_transaction(
        self, aggregator: str, mode: str, req: PaymentRequest, result: PaymentResult
    ) -> dict | None:
        if not self.enabled:
            return None
        row = {
            "aggregator": aggregator,
            "mode": mode,
            "amount": req.amount,
            "phone": req.phone,
            "network": req.network,
            "email": req.email,
            "transaction_ref": result.transaction_id,
            "status": result.final_status or result.payment_status,
            "message": result.final_message,
            "success": result.success,
            "charge_response": result.flutterwave_charge_response[:5000],
            "error_signals": result.error_signals,
        }
        try:
            res = self._client.table("transactions").insert(row).execute()
            return res.data[0] if res.data else None
        except Exception as e:  # noqa: BLE001
            log.warning("save_transaction failed: %s", e)
            return None

    def list_transactions(self, limit: int = 50) -> list[dict]:
        if not self.enabled:
            return []
        try:
            res = (
                self._client.table("transactions")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception as e:  # noqa: BLE001
            log.warning("list_transactions failed: %s", e)
            return []

    def get_transaction(self, transaction_ref: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            res = (
                self._client.table("transactions")
                .select("*")
                .eq("transaction_ref", transaction_ref)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:  # noqa: BLE001
            log.warning("get_transaction failed: %s", e)
            return None

    # --- curl templates (deduced by browser mode, reused by replay mode) ---
    def save_template(self, aggregator: str, template: CurlTemplate) -> dict | None:
        if not self.enabled:
            return None
        row = {"aggregator": aggregator, "template": dataclasses.asdict(template)}
        try:
            res = (
                self._client.table("curl_templates")
                .upsert(row, on_conflict="aggregator")
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:  # noqa: BLE001
            log.warning("save_template failed: %s", e)
            return None

    def load_template(self, aggregator: str) -> CurlTemplate | None:
        if not self.enabled:
            return None
        try:
            res = (
                self._client.table("curl_templates")
                .select("template")
                .eq("aggregator", aggregator)
                .limit(1)
                .execute()
            )
            if res.data:
                data: dict[str, Any] = res.data[0].get("template", {})
                fields = {f.name for f in dataclasses.fields(CurlTemplate)}
                return CurlTemplate(**{k: v for k, v in data.items() if k in fields})
        except Exception as e:  # noqa: BLE001
            log.warning("load_template failed: %s", e)
        return None


# Single shared instance.
db = Database()
