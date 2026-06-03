"""DigiKUNTZ aggregator — implements the core Aggregator interface.

Adapter that exposes the existing browser_flow (AI-driven) and replay_flow
(no-browser curl replay) behind the common `Aggregator` ABC, and registers
itself in the registry under the name "digikuntz".
"""

import json
import logging

from core.base import Aggregator, CurlTemplate, PaymentRequest, PaymentResult
from core.browser import CapturedRequest, BrowserSession
from core.browser_runner import run_browser_flow
from core.config import settings
from core.upstream_errors import classify_upstream_error
from core.registry import register

from . import browser_flow
from . import replay_flow

log = logging.getLogger("ai_browser2")




class DigikuntzAggregator(Aggregator):
    name = "digikuntz"
    # Canonical network values expected by DigiKUNTZ/Flutterwave.
    supported_networks = ["Orangemoney", "MTN"]

    @property
    def _agent(self) -> "browser_flow.DigikuntzAgent":
        # Reuse one agent bound to this aggregator's browser/llm for callbacks.
        agent = getattr(self, "_agent_cache", None)
        if agent is None:
            agent = browser_flow.DigikuntzAgent(self.browser, self.llm)
            self._agent_cache = agent
        return agent

    # --- transaction creation ---
    async def create_transaction(self, req: PaymentRequest) -> dict:
        return await self._agent._create_transaction(req)

    # --- browser-IA hooks ---
    def browser_objective(self, req: PaymentRequest) -> str:
        return self._agent.browser_objective(req)

    async def decide_browser_outcome(self, req, loop_result, result, session=None) -> None:
        await self._agent.decide_browser_outcome(req, loop_result, result, session=session)

    async def finalize_after_close(self, req, result) -> None:
        await self._agent.finalize_after_close(req, result)

    def network_label(self, network: str) -> str:
        return replay_flow.network_label(network)

    def charge_request_matcher(self, r: CapturedRequest) -> bool:
        return BrowserSession._flutterwave_charge_matcher(r)

    def verify_request_matcher(self, r: CapturedRequest) -> bool:
        return BrowserSession._flutterwave_verify_matcher(r)

    def checkout_url_predicate(self, url: str) -> bool:
        """True while the URL is still on the Flutterwave checkout (not redirected)."""
        return any(
            host in url
            for host in ("flutterwave.com", "checkout-v3-ui-prod", "ravepay")
        )

    # --- template extraction (browser mode -> DB) ---
    def extract_curl_template(
        self, charge: CapturedRequest, verify: "CapturedRequest | None", public_key: str
    ) -> CurlTemplate | None:
        if not charge:
            return None
        # Best-effort payload skeleton from the captured charge body (keys only,
        # values blanked) so replay can re-fill it from the request.
        payload_skeleton: dict = {}
        try:
            body = json.loads(charge.request_body) if charge.request_body else {}
            if isinstance(body, dict):
                payload_skeleton = {k: "" for k in body}
        except (json.JSONDecodeError, TypeError):
            pass
        return CurlTemplate(
            charge_url=charge.url or settings.digikuntz.flw_charge_url,
            verify_url=verify.url if verify else settings.digikuntz.flw_verify_url,
            init_url=settings.digikuntz.flw_init_url,
            upgrade_url=settings.digikuntz.flw_upgrade_url,
            hosted_pay_url="https://api.ravepay.co/flwv3-pug/getpaidx/api/hosted_pay",
            headers=dict(replay_flow.HEADERS),
            payload_skeleton=payload_skeleton,
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
    async def pay_via_browser(self, req: PaymentRequest, tx_id: int | None = None) -> PaymentResult:
        return await run_browser_flow(self, req, tx_id=tx_id)

    # --- replay mode (no browser) ---
    async def replay(self, req: PaymentRequest, template: CurlTemplate) -> PaymentResult:
        """Reproduce the payment using replay_flow steps (no browser).

        The stored template is turned into a per-call ReplayConfig passed
        explicitly to step2/3/4 — no module globals are mutated, so concurrent
        replays never share state. step1..step4 keep their tuned retry/poll logic.
        """
        result = PaymentResult()
        # Per-call config from the template (URLs/headers/pubkey). Isolated.
        cfg = replay_flow.ReplayConfig.from_template(template)
        try:
            tx = await replay_flow.step1_create_transaction(
                req.amount, req.phone, req.email, req.sender_name
            )
        except Exception as e:  # noqa: BLE001 — surface any creation failure
            result.error = f"digikuntz create_transaction error: {e}"
            result.error_code = classify_upstream_error(e) or ""
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
                checkout = await replay_flow.step2_initialize_checkout(payment_link, cfg=cfg)
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
            cfg=cfg,
        )
        charge_resp = charge.get("charge_response", {})
        result.flutterwave_charge_response = str(charge_resp)[:1000]

        verdict = self.interpret_status("charge", charge_resp, req.network)
        if verdict:
            result.final_status, result.final_message = verdict
            result.success = result.final_status == "successful"
            result.payment_status = result.final_status
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

        # [DEBUG ussd-sent-timestamp] /charge a renvoyé un flw_ref => l'USSD vient
        # d'être envoyé au client. C'est l'instant de référence (replay) qu'on
        # veut valider avant d'en faire la base du calcul anti-doublon.
        import time as _t, datetime as _dt
        _ussd_at = _t.time()
        log.info("🕒 [ussd-sent] replay: USSD envoyé à %s (flw_ref=%s)",
                 _dt.datetime.fromtimestamp(_ussd_at).isoformat(timespec="seconds"), flw_ref)

        verify = await replay_flow.step4_poll_verify(charge["modalauditid"], flw_ref, cfg=cfg)
        log.info("🕒 [ussd-sent] replay: verdict %ds après l'envoi USSD",
                 int(_t.time() - _ussd_at))

        # Le polling ne s'arrête que sur un verdict terminal (pas de timeout) :
        # interpret_verify le traduit ici. Fallback unknown si réponse inattendue.
        verdict = self.interpret_status("verify", verify, req.network)
        if verdict:
            result.final_status, result.final_message = verdict
        else:
            result.final_status = verify.get("data", {}).get("status", "unknown")
            result.final_message = f"Statut Flutterwave: {result.final_status}"

        result.success = result.final_status == "successful"
        result.payment_status = result.final_status
        return result


register(DigikuntzAggregator.name, DigikuntzAggregator)
