"""DigiKUNTZ payment agent — creates transaction then drives Flutterwave checkout."""

import json
import logging
from dataclasses import dataclass

import httpx

from agent.browser import BrowserController
from agent.llm_client import LlmClient
from agent.reasoning_loop import ReasoningLoop
from agent import classifier

log = logging.getLogger("ai_browser2")

DIGIKUNTZ_BASE = "https://app.digikuntz.com/dev"
DIGIKUNTZ_USER_ID = "USERID-REDACTED"
DIGIKUNTZ_SECRET = "SK-REDACTED"
DEFAULT_CALLBACK = "https://app.digikuntz.com/callback"


@dataclass
class PaymentRequest:
    amount: int  # XAF
    phone: str
    network: str  # MTN or Orange
    email: str
    sender_name: str = "Rauvalia"
    callback_url: str = DEFAULT_CALLBACK


@dataclass
class PaymentResult:
    success: bool = False
    error: str = ""
    transaction_id: str = ""
    payment_status: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # The real prize: captured Flutterwave charge request
    flutterwave_charge_url: str = ""
    flutterwave_charge_body: str = ""
    flutterwave_charge_response: str = ""
    curl_replay: str = ""
    # Plaintext data BEFORE encryption (the real gold)
    plaintext_payload: str = ""
    public_key: str = ""
    # Verify endpoint for curl replay polling
    verify_url: str = ""
    verify_request_body: str = ""
    verify_last_response: str = ""
    verify_curl: str = ""
    # USSD validation result after waiting
    final_status: str = ""
    final_message: str = ""
    # Console + network error signals captured around the Pay click
    error_signals: dict = None
    # All captured network requests summary
    captured_requests: list[dict] = None

    def __post_init__(self):
        if self.captured_requests is None:
            self.captured_requests = []
        if self.error_signals is None:
            self.error_signals = {}


class DigikuntzAgent:
    """End-to-end: create digiKUNTZ transaction -> AI drives Flutterwave checkout."""

    def __init__(self, browser: BrowserController, llm: LlmClient):
        self.browser = browser
        self.llm = llm

    async def pay(self, req: PaymentRequest) -> PaymentResult:
        result = PaymentResult()

        # 1. Create transaction via digiKUNTZ API
        log.info(
            "Creating digiKUNTZ transaction: %d XAF, %s, %s",
            req.amount, req.network, req.phone,
        )
        try:
            tx = await self._create_transaction(req)
        except Exception as e:
            result.error = f"digikuntz API error: {e}"
            log.error(result.error)
            return result

        result.transaction_id = tx.get("transactionRef", "")
        payment_link = tx.get("paymentLink", "")

        if not payment_link:
            result.error = f"No paymentLink in response: {tx}"
            log.error(result.error)
            return result

        log.info("Transaction created: ref=%s link=%s", result.transaction_id, payment_link)

        # 2. Start network capture BEFORE navigating
        self.browser.start_capture()

        # 3. Navigate to payment link and enter the Flutterwave iframe.
        #    FAST + non-blocking: we just enter the iframe (don't block waiting
        #    for the form). If only a loader is showing, the AGENT handles it
        #    itself from turn 1 (wait / reload), instead of us blocking ~100s.
        await self.browser.goto(payment_link)
        await self.browser.wait(ms=3000)
        try:
            await self.browser.enter_iframe("iframe")
            # Best-effort: hook crypto + short wait for the form, but DON'T block.
            try:
                frame = getattr(self.browser, '_active_frame', self.browser.page)
                await frame.wait_for_selector("#phone", timeout=6000)
            except Exception:
                pass  # form not ready yet — the agent will recover
            await self.browser.hook_crypto()
            log.info("Entered iframe (agent will handle loader if form not ready)")
        except Exception as e:
            log.warning("Could not enter iframe yet (%s) — agent will retry", e)
            self.browser._active_frame = self.browser.page

        # 4. Let the AI agent drive Flutterwave checkout autonomously
        objective = (
            f"Tu es sur la page de paiement Flutterwave dans un iframe. "
            f"Ton objectif: remplir le formulaire mobile money et payer. "
            f"ETAPES dans cet ordre:\n"
            f"1. Attends que le select reseau soit charge (il peut prendre quelques secondes)\n"
            f"2. Selectionne le reseau \"{req.network}\" (Orange Money ou MTN Mobile Money) dans le select\n"
            f"3. Remplis le numero de telephone: {req.phone.replace('+237', '')}\n"
            f"4. Clique sur le bouton Pay/Payer\n"
            f"5. Attends 5 secondes et lis le resultat\n\n"
            f"IMPORTANT:\n"
            f"- Si le bouton Pay est disabled, c'est que le reseau n'est pas selectionne. Selectionne-le d'abord.\n"
            f"- ERREURS DE CHARGEMENT = RECUPERABLES, JAMAIS un echec final: si tu vois "
            f"'Impossible de recuperer les reseaux', un loader bloque, un select reseau "
            f"vide, ou une erreur reseau AVANT d'avoir clique Payer, tu n'as PAS encore "
            f"paye. Tu dois trouver une solution toi-meme pour pouvoir payer:\n"
            f"    a) attends quelques secondes (action wait) puis reverifie le select reseau,\n"
            f"    b) si toujours vide/en erreur, recharge la page de paiement (action "
            f"\"reload\") pour relancer le checkout, puis recommence depuis l'etape 1,\n"
            f"    c) repete (attendre / recharger) 2 a 3 fois MAXIMUM. Si apres 3 reloads le "
            f"formulaire ne s'affiche toujours pas (0 elements interactifs, loader en boucle), "
            f"ARRETE et conclus: objective_reached=true avec objective_result "
            f"{{\"status\":\"error\",\"message\":\"Le checkout Flutterwave ne repond pas "
            f"apres plusieurs tentatives de rechargement\"}}.\n"
            f"- NE conclus PAS objective_reached tant que tu n'as pas reellement clique "
            f"Payer ET obtenu un vrai resultat du serveur (USSD #150*50#, succes, ou un "
            f"message d'echec renvoye APRES le clic Payer). Une erreur de chargement avant "
            f"Pay n'est jamais un resultat final.\n"
            f"- Ne navigue JAMAIS vers une autre URL. Tu peux seulement RECHARGER la page "
            f"de paiement actuelle via l'action \"reload\".\n"
            f"- Si une action echoue, reessaie avec un selecteur different ou recharge.\n"
            f"- TRES IMPORTANT: si la page devient une page de CONNEXION/LOGIN/"
            f"INSCRIPTION/AUTHENTIFICATION (champs email+mot de passe, bouton "
            f"'Se connecter'/'Sign in'/'Login', ou URL contenant 'auth', 'login', "
            f"'signin', 'account'), NE remplis AUCUN identifiant et NE te connecte "
            f"PAS. Cela veut dire que la session de paiement a expire ou echoue. "
            f"Termine TOUT DE SUITE: objective_reached=true avec objective_result "
            f"{{\"status\":\"error\",\"message\":\"redirection vers page "
            f"d'authentification — session de paiement perdue\"}}.\n"
            f"- Ne perds jamais de vue ton objectif: payer via mobile money. Tu ne "
            f"dois JAMAIS creer de compte ni te connecter.\n\n"
            f"L'objectif est ATTEINT quand tu vois le resultat apres Pay "
            f"(succes, erreur, USSD prompt, ou redirection). "
            f"Dans objective_result, mets un JSON: "
            f"{{\"final_url\": \"...\", \"status\": \"success|error|ussd_sent|pending\", "
            f"\"message\": \"ce qui est affiche a l'ecran\"}}"
        )

        # Clear console/network diagnostic buffers right before the agent acts
        # so any error signals belong to the Pay action.
        self.browser.reset_diagnostics()

        loop = ReasoningLoop(self.browser, self.llm, max_turns=22)
        loop_result = await loop.run(objective)

        # 5. Capture plaintext payload from crypto hook
        plaintexts = await self.browser.get_captured_plaintexts()
        if plaintexts:
            last = plaintexts[-1]
            result.plaintext_payload = last.get("plaintext", "")
            result.public_key = last.get("publicKey", "")
            log.info("Captured plaintext payload: %s", result.plaintext_payload[:500])
            log.info("Public key: %s", result.public_key[:100])
        else:
            log.warning("No plaintext captured from crypto hook")

        # 6. Parse agent immediate result
        agent_status = "unknown"
        agent_message = ""
        if loop_result.success and loop_result.result:
            try:
                agent_result = json.loads(loop_result.result)
                agent_status = agent_result.get("status", "unknown")
                agent_message = agent_result.get("message", "")
            except (json.JSONDecodeError, Exception):
                pass

        # 6b. Gather error signals from console + network (NOT page render).
        #     When a network (e.g. Orange OM) is down, the Flutterwave page can
        #     show an error and close instantly — the render watcher misses it,
        #     but the console error / failed request / HTTP 4xx-5xx response on
        #     the /charge endpoint is captured here.
        error_signals = self.browser.get_error_signals()
        result.error_signals = error_signals
        has_error_signal = bool(
            error_signals["console_errors"]
            or error_signals["failed_requests"]
            or error_signals["http_errors"]
        )
        ussd_detected = (
            agent_status == "ussd_sent"
            or "150*50" in agent_message
            or "USSD" in agent_message.upper()
        )

        # 7. Decide the outcome.
        #  - The MOST authoritative evidence is the text the agent actually read
        #    on the page after Pay (agent_message) and the raw /charge response.
        #    We classify those FIRST (keywords), not the console noise.
        #  - If USSD was sent, watch for a page change and react the instant it
        #    happens (validation OR refusal) — no fixed 60s wait.
        import asyncio

        charge_req = self.browser.get_flutterwave_charge()
        charge_body = charge_req.response_body if charge_req else ""
        signals_text = json.dumps(error_signals, ensure_ascii=False) if has_error_signal else ""

        # USSD can also be inferred from the /charge response body (e.g. "dial").
        if not ussd_detected:
            cb_hit = classifier.classify(charge_body)
            if cb_hit and cb_hit[0] == "ussd_sent":
                ussd_detected = True
                log.info("USSD inferred from charge response (%r)", cb_hit[1])

        if ussd_detected:
            log.info("USSD sent! Watching for page change (react on change, no fixed wait)...")
            await self.browser.watch_page_changes()
            for i in range(60):  # safety cap 60s, but we break the instant anything changes
                await asyncio.sleep(1)

                # React the moment the page changes (validation or refusal).
                watcher_status = await self.browser.get_page_status()
                if watcher_status and watcher_status.get("status") in ("changed", "redirected"):
                    page_text = watcher_status.get("message", "")
                    log.info("Page changed (poll %d): %s", i + 1, page_text[:200])
                    # ussd_sent=True: a failure here is a refusal/insufficient
                    # funds, NOT a network problem.
                    status, msg = await self._interpret(page_text, req.network, ussd_sent=True)
                    result.final_status = status
                    result.final_message = msg
                    break

                # Also react to a late error signal (charge rejected after USSD).
                late = self.browser.get_error_signals()
                late_text = json.dumps(late, ensure_ascii=False)
                hit = classifier.classify(late_text)
                if hit and hit[0] in ("failed", "cancelled", "network_down"):
                    result.error_signals = late
                    result.final_status = hit[0]
                    result.final_message = self._friendly(
                        hit[0], req.network, late_text[:300], ussd_sent=True)
                    log.info("Late error matched %r -> %s (poll %d)", hit[1], hit[0], i + 1)
                    break

                # Fallback: URL redirect away from checkout = done.
                try:
                    current_url = await self.browser.current_url()
                    if "flutterwave.com" not in current_url and \
                       "checkout-v3-ui-prod" not in current_url and \
                       "ravepay" not in current_url:
                        status, msg = await self._interpret(
                            f"redirected to {current_url}", req.network, ussd_sent=True
                        )
                        result.final_status = status or "redirected"
                        result.final_message = msg or f"Redirected to {current_url}"
                        break
                except Exception as e:
                    log.warning("Poll error: %s", e)
            else:
                result.final_status = "timeout"
                result.final_message = "L'utilisateur n'a pas valide le USSD dans le delai imparti."
                log.info("USSD validation timeout")
        else:
            # No USSD. Classify the authoritative texts in priority order:
            #   1. the message the agent read on the page after Pay
            #   2. the /charge response body
            #   3. the captured error signals (console/network/HTTP)
            decided = None
            for src in (agent_message, charge_body, signals_text):
                hit = classifier.classify(src)
                if hit and hit[0] != "pending":
                    decided = (hit[0], hit[1], src)
                    break

            if decided:
                status, kw, src = decided
                log.info("Keyword %r matched -> %s", kw, status)
                result.final_status = status
                result.final_message = self._friendly(
                    status, req.network, (agent_message or src)[:300]
                )
            else:
                # Nothing matched any known keyword -> ask the LLM on the
                # combined evidence; it returns a verdict + a new keyword to learn.
                combined = "\n".join(filter(None, [
                    f"Message lu par l'agent: {agent_message}" if agent_message else "",
                    f"Reponse /charge: {charge_body}" if charge_body else "",
                    f"Signaux d'erreur: {signals_text}" if signals_text else "",
                ]))[:3000] or (agent_message or "(aucune information)")
                log.info("No keyword match on agent/charge/signals -> LLM")
                status, msg = await self._interpret(combined, req.network)
                result.final_status = status or agent_status or "unknown"
                result.final_message = msg

        # 8. Stop capture and extract Flutterwave charge request
        captured = self.browser.stop_capture()

        result.turns = loop_result.turns
        result.input_tokens = loop_result.total_input_tokens
        result.output_tokens = loop_result.total_output_tokens
        result.payment_status = result.final_status

        # Summarize all captured requests
        result.captured_requests = [
            {"method": r.method, "url": r.url[:200], "status": r.status}
            for r in captured
        ]

        # Find the Flutterwave charge request
        charge = self.browser.get_flutterwave_charge()
        if charge:
            result.flutterwave_charge_url = charge.url
            result.flutterwave_charge_body = charge.request_body
            result.flutterwave_charge_response = charge.response_body[:1000]
            result.curl_replay = charge.to_curl()
            log.info("Flutterwave charge captured: %s", charge.url)
        else:
            log.warning("No Flutterwave /charges request captured!")

        # Find the verify/mpesa polling requests (for curl replay without browser)
        verify_reqs = self.browser.get_flutterwave_verify_requests()
        if verify_reqs:
            last_verify = verify_reqs[-1]
            result.verify_url = last_verify.url
            result.verify_request_body = last_verify.request_body
            result.verify_last_response = last_verify.response_body[:1000]
            result.verify_curl = last_verify.to_curl()
            log.info("Verify endpoint captured: %s (%d polls)", last_verify.url, len(verify_reqs))
            log.info("Verify body: %s", last_verify.request_body[:300])
            log.info("Verify last response: %s", last_verify.response_body[:300])
        else:
            log.warning("No verify requests captured")

        result.success = result.final_status in ("successful", "completed", "ussd_sent")
        if loop_result.error and not result.error:
            result.error = loop_result.error

        return result

    def _friendly(self, status: str, network: str, raw: str,
                  ussd_sent: bool = False) -> str:
        """Produce a clear user-facing message.

        The SAME 'failed' status means different things depending on whether
        the USSD prompt was already sent:
          - failure BEFORE any USSD (at /charge)  -> operator/network problem
            -> name the network and suggest the alternative.
          - failure AFTER the USSD was sent        -> the user refused or had
            insufficient funds -> do NOT blame the network.
        """
        if status == "successful":
            return "Paiement reussi."
        if status == "cancelled":
            return "Paiement annule / refuse par l'utilisateur sur le USSD."
        if status == "network_down":
            return classifier.network_failure_message(network)
        if status == "failed":
            if ussd_sent:
                return ("Paiement echoue apres le USSD (refus de l'utilisateur "
                        "ou solde insuffisant). Veuillez reessayer.")
            # Failed at the charge stage, USSD never sent => operator down.
            return classifier.network_failure_message(network)
        return raw

    async def _interpret(self, text: str, network: str,
                         ussd_sent: bool = False) -> tuple[str, str]:
        """Classify a payment outcome.

        1. Try the keyword list first (instant, no LLM cost).
        2. If nothing matches, ask the LLM for {status, reason, keyword},
           persist the new keyword, and return its verdict.
        """
        hit = classifier.classify(text)
        if hit:
            status, kw = hit
            log.info("Keyword match %r -> %s", kw, status)
            return status, self._friendly(status, network, text[:300], ussd_sent)

        # No keyword matched -> ask the LLM and learn from its answer.
        log.info("No keyword match, asking LLM to classify + propose keyword")
        llm_resp = await self.llm.send(
            system_prompt=(
                "Tu analyses le resultat d'un paiement mobile money Flutterwave. "
                "On te donne le texte de la page OU les signaux d'erreur (console, "
                "reseau, reponses HTTP). Determine le statut et propose UN mot-cle "
                "court et distinctif present dans ce texte qui permettra de "
                "reconnaitre ce cas la prochaine fois sans IA. "
                "Reponds UNIQUEMENT en JSON: "
                "{\"status\": \"successful|failed|cancelled|network_down|pending|unknown\", "
                "\"reason\": \"explication courte\", \"keyword\": \"mot-cle a memoriser\"}"
            ),
            user_content=[{"type": "text", "text": (
                f"Reseau: {network}.\n\nTexte/signaux:\n{text}"
            )}],
        )
        if llm_resp.success and llm_resp.text:
            try:
                verdict = json.loads(llm_resp.text)
                status = verdict.get("status", "unknown")
                reason = verdict.get("reason", "")
                keyword = verdict.get("keyword", "")
                # Only learn keywords for definitive verdicts, never "unknown".
                if keyword and status in (
                    "successful", "failed", "cancelled", "network_down", "ussd_sent"
                ):
                    classifier.add_keyword(status, keyword)
                msg = self._friendly(status, network, reason or text[:300], ussd_sent)
                log.info("LLM verdict: %s — kw=%r — %s", status, keyword, reason)
                return status, msg
            except (json.JSONDecodeError, Exception):
                pass
        return "unknown", text[:300]

    async def _create_transaction(self, req: PaymentRequest) -> dict:
        """POST to digiKUNTZ API to create a payment transaction."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DIGIKUNTZ_BASE}/transaction",
                json={
                    "estimation": req.amount,
                    "raisonForTransfer": "Rauvalia auto",
                    "userEmail": req.email,
                    "userPhone": req.phone.replace("+237", ""),
                    "userCountry": "CM",
                    "senderName": req.sender_name,
                    "callbackUrl": req.callback_url,
                },
                headers={
                    "x-user-id": DIGIKUNTZ_USER_ID,
                    "x-secret-key": DIGIKUNTZ_SECRET,
                    "content-type": "application/json",
                    "accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            # Response: {"id":"...", "status":"payin_pending", "data":{"paymentLink":"...", "transactionRef":"..."}}
            inner = data.get("data", data)
            return inner
