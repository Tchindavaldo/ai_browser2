"""DigiKUNTZ payment agent — creates transaction then drives Flutterwave checkout."""

import json
import logging

import httpx

from core.base import PaymentRequest, PaymentResult
from core.browser import BrowserController
from core.llm_client import LlmClient
from core import classifier

log = logging.getLogger("ai_browser2")

# Config loaded from central settings (.env).
from core.config import settings

_dk = settings.digikuntz
DIGIKUNTZ_BASE = _dk.base
DIGIKUNTZ_USER_ID = _dk.user_id
DIGIKUNTZ_SECRET = _dk.secret
DEFAULT_CALLBACK = _dk.callback_url


class DigikuntzAgent:
    """End-to-end: create digiKUNTZ transaction -> AI drives Flutterwave checkout."""

    def __init__(self, browser: BrowserController, llm: LlmClient):
        self.browser = browser
        self.llm = llm

    def browser_objective(self, req: PaymentRequest) -> str:
        """The detailed FR objective handed to the reasoning loop."""
        return (
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
            f"L'objectif est ATTEINT dans l'un de ces cas — agis IMMEDIATEMENT:\n"
            f"  a) Tu vois '#150*50#' ou 'USSD' ou 'dial' apres le clic Payer → ussd_sent\n"
            f"  b) La page affiche un message de succes ou echec apres Payer → success ou error\n"
            f"  c) L'URL de la page principale (pas l'iframe) contient 'payment-done' ou "
            f"'payments.digikuntz.com' → c'est la page de resultat DigiKUNTZ, LIS son contenu "
            f"(status=failed/success dans l'URL ou le texte) et conclus IMMEDIATEMENT\n"
            f"  d) Tu es redirige vers une page inconnue apres Payer → conclus avec ce que tu vois\n\n"
            f"Dans objective_result, mets toujours un JSON: "
            f"{{\"final_url\": \"...\", \"status\": \"success|error|ussd_sent|pending\", "
            f"\"message\": \"ce qui est affiche a l'ecran\"}}"
        )

    async def decide_browser_outcome(self, req, loop_result, result) -> None:
        """DigiKUNTZ-specific outcome decision after the reasoning loop.

        Operates in place on `result` (sets final_status/final_message/
        payment_status/error_signals). Keeps the USSD watch loop + classifier/LLM
        verdict semantics here, as the generic runner stays provider-agnostic.
        """
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

        # Final status is decided; the generic runner captures charge/verify and
        # sets success/captured_requests.
        result.payment_status = result.final_status

    def _friendly(self, status: str, network: str, raw: str,
                  ussd_sent: bool = False) -> str:
        """Produce a clear user-facing message.

        The SAME 'failed' status means different things:
          - insufficient funds (code 51 / "solde insuffisant" / "Insufficient
            Fund") -> tell the user to top up, whether or not USSD was sent.
          - other failure AFTER the USSD was sent -> refusal by the user.
          - other failure BEFORE any USSD (at /charge) -> operator/network
            problem -> name the network and suggest the alternative.
        """
        if status == "successful":
            return "Paiement reussi."
        if status == "cancelled":
            return "Paiement annule / refuse par l'utilisateur sur le USSD."
        if status == "network_down":
            return classifier.network_failure_message(network)
        if status == "failed":
            low = (raw or "").lower()
            # Insufficient funds is a balance issue, never a network problem.
            if ("insufficient" in low or "solde insuffisant" in low
                    or "insufficient fund" in low or '"51"' in low
                    or "chargeresponsecode\":\"51" in low):
                return ("Solde insuffisant sur le compte "
                        f"{classifier.network_label(network)}. "
                        "Veuillez recharger puis reessayer.")
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
                    "callbackUrl": req.callback_url or DEFAULT_CALLBACK,
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
