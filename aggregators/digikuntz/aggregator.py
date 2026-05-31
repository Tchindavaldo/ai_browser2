"""DigiKUNTZ aggregator — implements the core Aggregator interface.

Adapter that exposes the existing browser_flow (AI-driven) and replay_flow
(no-browser curl replay) behind the common `Aggregator` ABC, and registers
itself in the registry under the name "digikuntz".
"""

import logging

from core.base import Aggregator, CurlTemplate, PaymentRequest, PaymentResult
from core.browser import CapturedRequest
from core.config import settings
from core.registry import register

from . import browser_flow
from . import replay_flow

log = logging.getLogger("ai_browser2")


class DigikuntzAggregator(Aggregator):
    name = "digikuntz"

    # --- transaction creation ---
    async def create_transaction(self, req: PaymentRequest) -> dict:
        agent = browser_flow.DigikuntzAgent(self.browser, self.llm)
        return await agent._create_transaction(req)

    # --- browser-IA hooks ---
    def browser_objective(self, req: PaymentRequest) -> str:
        # The detailed objective is built inside DigikuntzAgent.pay; exposed here
        # for the generic engine / introspection.
        return (
            f"Payer {req.amount} XAF via {self.network_label(req.network)} "
            f"sur le numero {req.phone} (checkout Flutterwave dans l'iframe)."
        )

    def network_label(self, network: str) -> str:
        return replay_flow.network_label(network)

    def charge_request_matcher(self, r: CapturedRequest) -> bool:
        return self.browser._flutterwave_charge_matcher(r)

    def verify_request_matcher(self, r: CapturedRequest) -> bool:
        return self.browser._flutterwave_verify_matcher(r)

    # --- template extraction (browser mode -> DB) ---
    def extract_curl_template(
        self, charge: CapturedRequest, verify: "CapturedRequest | None", public_key: str
    ) -> CurlTemplate | None:
        if not charge:
            return None
        return CurlTemplate(
            charge_url=charge.url,
            verify_url=verify.url if verify else settings.digikuntz.flw_verify_url,
            init_url=settings.digikuntz.flw_init_url,
            upgrade_url=settings.digikuntz.flw_upgrade_url,
            headers=dict(replay_flow.HEADERS),
            public_key_rsa=public_key,
            flw_pub_key=settings.digikuntz.flw_pub_key,
        )

    # --- result interpretation ---
    def interpret_status(self, stage: str, resp: dict, network: str):
        if stage == "charge":
            return replay_flow.interpret_charge(resp, network)
        if stage == "verify":
            return replay_flow.interpret_verify(resp, network)
        if stage == "ping":
            return replay_flow.interpret_ping(resp, network)
        return None

    # --- browser mode (full AI-driven flow) ---
    async def pay_via_browser(self, req: PaymentRequest) -> PaymentResult:
        agent = browser_flow.DigikuntzAgent(self.browser, self.llm)
        return await agent.pay(req)

    # --- replay mode (no browser) ---
    async def replay(self, req: PaymentRequest, template: CurlTemplate) -> PaymentResult:
        """Reproduce the payment using replay_flow steps (no browser).

        Orchestrates step1..step4 directly, mirroring replay_flow.main() but
        returning a PaymentResult instead of printing.
        """
        result = PaymentResult()
        try:
            tx = await replay_flow.step1_create_transaction(
                req.amount, req.phone, req.email, req.sender_name
            )
        except Exception as e:  # noqa: BLE001 — surface any creation failure
            result.error = f"digikuntz create_transaction error: {e}"
            return result

        tx_ref = tx.get("transactionRef", "")
        payment_link = tx.get("paymentLink", "")
        total = int(tx.get("paymentWithTaxes", req.amount))
        result.transaction_id = tx_ref
        if not payment_link:
            result.error = f"No paymentLink in response: {tx}"
            return result

        # RSA public key: prefer the stored template, else (re)initialize.
        public_key_rsa = template.public_key_rsa if template else ""
        if not public_key_rsa:
            try:
                checkout = await replay_flow.step2_initialize_checkout(payment_link)
                public_key_rsa = checkout.get("public_key", "")
            except Exception as e:  # noqa: BLE001
                log.warning("replay step2 failed (%s), will rely on fallback key", e)

        charge = await replay_flow.step3_charge(
            amount=total,
            phone=req.phone,
            network=req.network,
            email=req.email,
            firstname="API",
            lastname=f"Call: {req.sender_name}",
            tx_ref=tx_ref,
            public_key_rsa=public_key_rsa,
        )
        charge_resp = charge.get("charge_response", {})
        result.flutterwave_charge_response = str(charge_resp)[:1000]

        verdict = self.interpret_status("charge", charge_resp, req.network)
        if verdict:
            result.final_status, result.final_message = verdict
            result.success = result.final_status == "successful"
            return result

        # Extract flw_ref then poll verify (17-min clock budget lives in step4).
        charge_data = charge_resp.get("data", {})
        flw_ref = ""
        if isinstance(charge_data, dict):
            flw_ref = charge_data.get("flw_ref", "")
            if not flw_ref:
                nested = charge_data.get("data", {})
                if isinstance(nested, dict):
                    flw_ref = nested.get("flw_reference", "")
        if not flw_ref:
            result.error = "No flw_ref in charge response"
            return result

        verify = await replay_flow.step4_poll_verify(charge["modalauditid"], flw_ref)
        verdict = self.interpret_status("verify", verify, req.network)
        if verdict:
            result.final_status, result.final_message = verdict
        elif verify.get("status") == "timeout":
            if verify.get("got_any_status"):
                result.final_status = "cancelled"
                result.final_message = "Transaction annulée par l'opérateur (non validée dans le délai)."
            else:
                result.final_status = "unknown"
                result.final_message = "Flutterwave injoignable; statut à confirmer via l'API DigiKUNTZ."
        else:
            result.final_status = verify.get("data", {}).get("status", "unknown")
            result.final_message = f"Statut Flutterwave: {result.final_status}"

        result.success = result.final_status == "successful"
        result.payment_status = result.final_status
        return result


register(DigikuntzAggregator.name, DigikuntzAggregator)
