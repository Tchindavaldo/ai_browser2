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

import asyncio
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
    async def has_pending(self, aggregator: str, phone: str) -> bool:
        """True if a transaction for (aggregator, phone) is still 'pending'.

        Used to block launching a new payment on a number that already has one
        in flight. Safe (False) if Supabase is disabled.
        """
        if not self.enabled:
            return False

        def _select():
            res = (
                self._client.table("transactions")
                .select("id")
                .eq("aggregator", aggregator)
                .eq("phone", phone)
                .eq("status", "pending")
                .limit(1)
                .execute()
            )
            return bool(res.data)

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("has_pending failed: %s", e)
            return False

    async def last_transaction_for_number(
        self, aggregator: str, phone: str
    ) -> dict | None:
        """Most recent transaction for (aggregator, phone), or None.

        Returns the full row (id, status, created_at, ...) so the /pay guard can
        decide: a still-'pending' row blocks (confirm/cancel), and a recent
        non-success row within the retry window blocks with a wait time. Safe
        (None) if Supabase is disabled.
        """
        if not self.enabled:
            return None

        def _select():
            res = (
                self._client.table("transactions")
                .select("*")
                .eq("aggregator", aggregator)
                .eq("phone", phone)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("last_transaction_for_number failed: %s", e)
            return None

    async def insert_pending(
        self, aggregator: str, mode: str, req: PaymentRequest
    ) -> int | None:
        """Insert the transaction as 'pending' at the start; return its id."""
        if not self.enabled:
            return None
        row = {
            "aggregator": aggregator,
            "mode": mode,
            "amount": req.amount,
            "phone": req.phone,
            "network": req.network,
            "email": req.email,
            "status": "pending",
            "success": False,
        }

        def _insert():
            res = self._client.table("transactions").insert(row).execute()
            return res.data[0]["id"] if res.data else None

        try:
            return await asyncio.to_thread(_insert)
        except Exception as e:  # noqa: BLE001
            log.warning("insert_pending failed: %s", e)
            return None

    async def update_transaction(self, tx_id: int, result: PaymentResult) -> None:
        """Update a pending row with the final verdict once the payment settles."""
        if not self.enabled or tx_id is None:
            return
        patch = {
            "transaction_ref": result.transaction_id,
            "status": result.final_status or result.payment_status or "unknown",
            "message": result.final_message,
            "success": result.success,
            "charge_response": result.flutterwave_charge_response[:5000],
            "error_signals": result.error_signals,
        }

        def _update():
            self._client.table("transactions").update(patch).eq("id", tx_id).execute()

        try:
            await asyncio.to_thread(_update)
        except Exception as e:  # noqa: BLE001
            log.warning("update_transaction failed: %s", e)

        # Persist the per-turn AI trace into its dedicated table (browser mode).
        await self.save_trace(tx_id, result.trace)

    async def save_trace(self, tx_id: int, trace: list[dict]) -> None:
        """Insert one transaction_traces row per AI turn, linked to tx_id."""
        if not self.enabled or tx_id is None or not trace:
            return
        rows = [
            {
                "transaction_id": tx_id,
                "turn": e.get("turn"),
                "url": e.get("url"),
                "elements": e.get("elements"),
                "thought": e.get("thought"),
                "actions": e.get("actions"),
                "objective_reached": e.get("objective_reached", False),
                "error": e.get("error"),
            }
            for e in trace
        ]

        def _insert():
            self._client.table("transaction_traces").insert(rows).execute()

        try:
            await asyncio.to_thread(_insert)
        except Exception as e:  # noqa: BLE001
            log.warning("save_trace failed: %s", e)

    async def get_traces(self, transaction_id: int) -> list[dict]:
        """Return the per-turn trace rows for a transaction, ordered by turn."""
        if not self.enabled:
            return []

        def _select():
            res = (
                self._client.table("transaction_traces")
                .select("*")
                .eq("transaction_id", transaction_id)
                .order("turn")
                .execute()
            )
            return res.data or []

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_traces failed: %s", e)
            return []

    async def cancel_pending(self, tx_id: int) -> dict | None:
        """Force-settle a stuck 'pending' transaction to 'cancelled'.

        Only acts on rows still 'pending' (never overwrites a settled verdict).
        Returns the updated row, or None if not found / not pending / disabled.
        """
        if not self.enabled or tx_id is None:
            return None

        patch = {
            "status": "cancelled",
            "message": "Transaction annulée manuellement (déblocage d'un pending bloqué).",
            "success": False,
        }

        def _update():
            res = (
                self._client.table("transactions")
                .update(patch)
                .eq("id", tx_id)
                .eq("status", "pending")
                .execute()
            )
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_update)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_pending failed: %s", e)
            return None

    async def list_transactions(self, aggregator: str | None = None, limit: int = 50) -> list[dict]:
        if not self.enabled:
            return []

        def _select():
            q = self._client.table("transactions").select("*")
            if aggregator:
                q = q.eq("aggregator", aggregator)
            res = q.order("created_at", desc=True).limit(limit).execute()
            return res.data or []

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("list_transactions failed: %s", e)
            return []

    async def get_transaction(self, transaction_ref: str) -> dict | None:
        if not self.enabled:
            return None

        def _select():
            res = (
                self._client.table("transactions")
                .select("*")
                .eq("transaction_ref", transaction_ref)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_transaction failed: %s", e)
            return None

    # --- curl templates (deduced by browser mode, reused by replay mode) ---
    async def save_template(
        self, aggregator: str, template: CurlTemplate, force: bool = False
    ) -> dict | None:
        """Append a NEW active version ONLY if it differs from the current active
        one (or always, when force=True). Never overwrites: the previous version
        is deactivated and kept as history. Rows stay grouped per aggregator
        (one active each)."""
        if not self.enabled:
            return None
        payload = dataclasses.asdict(template)

        def _save_if_changed():
            client = self._client
            # Current active template for this aggregator (if any).
            cur = (
                client.table("curl_templates")
                .select("id, template")
                .eq("aggregator", aggregator)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            if not force and cur.data and cur.data[0].get("template") == payload:
                return cur.data[0]  # identical -> nothing to add
            # Deactivate the old active version, then insert the new active one.
            if cur.data:
                client.table("curl_templates").update({"is_active": False}).eq(
                    "id", cur.data[0]["id"]
                ).execute()
            res = (
                client.table("curl_templates")
                .insert({"aggregator": aggregator, "template": payload, "is_active": True})
                .execute()
            )
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_save_if_changed)
        except Exception as e:  # noqa: BLE001
            log.warning("save_template failed: %s", e)
            return None

    async def load_template(self, aggregator: str) -> CurlTemplate | None:
        """Load the active template for this aggregator (the one replay uses)."""
        if not self.enabled:
            return None

        def _select():
            res = (
                self._client.table("curl_templates")
                .select("template")
                .eq("aggregator", aggregator)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            return res.data[0].get("template", {}) if res.data else None

        try:
            data: dict[str, Any] | None = await asyncio.to_thread(_select)
            if data:
                fields = {f.name for f in dataclasses.fields(CurlTemplate)}
                return CurlTemplate(**{k: v for k, v in data.items() if k in fields})
        except Exception as e:  # noqa: BLE001
            log.warning("load_template failed: %s", e)
        return None


# Single shared instance.
db = Database()
