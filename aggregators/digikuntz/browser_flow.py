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
            f"- AVANT DE RECHARGER, REGARDE TON HISTORIQUE D'ACTIONS: as-tu DEJA "
            f"clique sur Payer/submit dans un tour precedent ? Si OUI, tu NE DOIS "
            f"PLUS JAMAIS recharger ni re-remplir ni re-cliquer Payer — sinon tu "
            f"envoies un 2e paiement (double debit). Apres le clic Payer, le "
            f"formulaire disparait et un LOADER s affiche, l iframe peut se "
            f"detacher: c est NORMAL, c est le paiement qui se traite. Dans ce cas "
            f"tu ATTENDS (action wait) et tu LIS le resultat (USSD, succes, echec), "
            f"tu ne recharges pas.\n"
            f"- IFRAME DETACHEE (frame_detached_count > 0 dans le header du tour): "
            f"cette regle ne vaut QUE si tu n as PAS encore clique Payer. Si tu vois "
            f"'iframe s est detachee Nx' ET 0 elements interactifs ET que tu n as "
            f"PAS encore clique Payer ET que v3/checkout/initialize a deja reussi, "
            f"RECHARGE IMMEDIATEMENT sans attendre — le formulaire n apparaitra jamais "
            f"dans ce cas car Vue.js a demarré dans un état corrompu. Mais si tu as "
            f"deja clique Payer, NE RECHARGE PAS (voir regle ci-dessus).\n"
            f"- COMMENT SAVOIR SI LE FORMULAIRE VA APPARAITRE: si l iframe ne s est PAS "
            f"detachee, regarde les DERNIERES REPONSES HTTP. Si tu vois "
            f"'v3/checkout/initialize' [200] et scripts en cours, attends 3-5s. "
            f"Si tu vois 'v3/checkout/initialize' [200] ET plus aucun script en cours "
            f"ET toujours 0 elements apres 10s d attente — recharge.\n"
            f"- ERREURS DE CHARGEMENT = RECUPERABLES, JAMAIS un echec final: si tu vois "
            f"'Impossible de recuperer les reseaux', un loader bloque, un select reseau "
            f"vide, ou une erreur reseau AVANT d'avoir clique Payer, ET qu'il n'y a plus "
            f"de requetes en cours, tu n'as PAS encore paye. Solutions:\n"
            f"    a) attends quelques secondes (action wait) puis reverifie le select reseau,\n"
            f"    b) si toujours vide/en erreur apres attente ET aucune requete en cours, "
            f"recharge la page de paiement (action \"reload\"), puis recommence depuis l'etape 1,\n"
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
            f"  d) Tu es redirige vers une page inconnue apres Payer → conclus avec ce que tu vois\n"
            f"  e) Si le header du tour affiche 'DÉLAI DÉPASSÉ' (plafond 17 min atteint) "
            f"et que tu as deja clique Payer sans voir de succes → conclus failed "
            f"(le delai operateur est ecoule, la transaction ne sera plus validee)\n\n"
            f"Dans objective_result, mets toujours un JSON: "
            f"{{\"final_url\": \"...\", \"status\": \"success|error|ussd_sent|pending\", "
            f"\"message\": \"ce qui est affiche a l'ecran\"}}"
        )

    async def decide_browser_outcome(self, req, loop_result, result, session=None) -> None:
        """DigiKUNTZ-specific outcome decision after the reasoning loop.

        Operates in place on `result` (sets final_status/final_message/
        payment_status/error_signals). Keeps the USSD watch loop + classifier/LLM
        verdict semantics here, as the generic runner stays provider-agnostic.

        `session` is the isolated BrowserSession of this transaction (its own tab
        + network capture). All page/capture reads go through it so concurrent
        payments stay isolated.
        """
        # Toutes les lectures page/réseau passent par la session isolée.
        sb = session if session is not None else self.browser
        # 6. Parse agent immediate result
        agent_status = "unknown"
        agent_message = ""
        if loop_result.success and loop_result.result:
            try:
                agent_result = json.loads(loop_result.result) if isinstance(loop_result.result, str) else loop_result.result
                agent_status = agent_result.get("status", "unknown")
                agent_message = agent_result.get("message", "")
                # URL guard redirect: extract real status from payment-done URL.
                final_url = agent_result.get("final_url", "")
                if agent_status == "redirected" and "payment-done" in final_url:
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(final_url).query)
                    url_status = qs.get("status", ["unknown"])[0]
                    tx_ref = qs.get("tx_ref", [""])[0]
                    if url_status == "successful":
                        result.final_status = "successful"
                        result.final_message = "Paiement reussi."
                    elif url_status == "failed":
                        result.final_status = "failed"
                        result.final_message = "Paiement échoué (solde insuffisant ou refus opérateur)."
                    else:
                        result.final_status = url_status or "unknown"
                        result.final_message = f"Redirection payment-done: status={url_status}"
                    if tx_ref and not result.transaction_id:
                        result.transaction_id = tx_ref
                    result.payment_status = result.final_status
                    log.info("URL guard: payment-done status=%s tx_ref=%s", url_status, tx_ref)
                    return
            except (json.JSONDecodeError, Exception):
                pass

        # 6b. Gather error signals from console + network (NOT page render).
        #     When a network (e.g. Orange OM) is down, the Flutterwave page can
        #     show an error and close instantly — the render watcher misses it,
        #     but the console error / failed request / HTTP 4xx-5xx response on
        #     the /charge endpoint is captured here.
        error_signals = sb.get_error_signals()
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

        charge_req = sb.get_flutterwave_charge()
        charge_body = charge_req.response_body if charge_req else ""
        signals_text = json.dumps(error_signals, ensure_ascii=False) if has_error_signal else ""

        # Si l'IA a déjà conclu avec un statut définitif (elle a vu payment-done,
        # un message d'echec/succes clair), on respecte sa conclusion sans entrer
        # dans le watch USSD — l'IA a la vision complète, le code ne la contourne pas.
        agent_concluded = agent_status in ("error", "failed", "success", "cancelled")
        if agent_concluded:
            log.info("Agent a conclu status=%s — skip USSD watch", agent_status)

        # USSD can also be inferred from the /charge response body (e.g. "dial").
        # Mais seulement si l'IA n'a pas déjà conclu définitivement.
        if not ussd_detected and not agent_concluded:
            cb_hit = classifier.classify(charge_body)
            if cb_hit and cb_hit[0] == "ussd_sent":
                ussd_detected = True
                log.info("USSD inferred from charge response (%r)", cb_hit[1])

        if ussd_detected and not agent_concluded:
            # Fenêtre d'attente de la validation USSD: ALIGNÉE sur le replay
            # (step4_poll_verify) = retry_window_s (1020s = 17 min, le délai avant
            # que l'opérateur auto-annule). À 60s on concluait 'timeout' trop tôt
            # alors que la transaction était encore validable côté opérateur —
            # statut/conclusion incohérents entre navigateur et replay.
            watch_budget = settings.retry_window_s
            log.info("USSD sent! Watching for page change (budget=%ds, react on change)...",
                     watch_budget)
            # WATCH PASSIF (option 3): le code se contente d'OBSERVER si l'écran
            # bouge (changement de texte ou redirection d'URL) — il ne JUGE
            # jamais (pas de classifier sur les signaux réseau bruts, qui causait
            # de faux 'network_down' sur un simple asset ERR_ABORTED). Dès que la
            # page change, on redonne la main à l'IA: ELLE lit le nouvel état et
            # conclut succès / refus / attente.
            await sb.watch_page_changes()
            decided = False
            for i in range(watch_budget):  # plafond = délai opérateur; break dès qu'il se passe qqch
                await asyncio.sleep(1)

                # (a) Le texte de la page a changé OU l'URL a redirigé ? -> l'IA lit.
                changed_text = None
                watcher_status = await sb.get_page_status()
                if watcher_status and watcher_status.get("status") in ("changed", "redirected"):
                    changed_text = watcher_status.get("message", "")

                # (b) Redirection hors du checkout = page de résultat -> l'IA lit.
                if changed_text is None:
                    try:
                        current_url = await sb.current_url()
                        if ("flutterwave.com" not in current_url
                                and "checkout-v3-ui-prod" not in current_url
                                and "ravepay" not in current_url):
                            changed_text = f"redirected to {current_url}"
                    except Exception as e:
                        log.warning("Poll error: %s", e)

                if changed_text is not None:
                    log.info("Page changée (poll %d) — l'IA interprète: %s",
                             i + 1, changed_text[:200])
                    # _interpret demande au LLM de juger le texte RÉEL de la page
                    # (ussd_sent=True: un échec ici = refus utilisateur / solde,
                    # pas un problème réseau). Le code ne décide pas.
                    status, msg = await self._interpret(changed_text, req.network, ussd_sent=True)
                    result.final_status = status
                    result.final_message = msg
                    decided = True
                    break

            if not decided:
                # Budget écoulé (17 min) sans validation : même verdict que le
                # replay — l'opérateur a auto-annulé la transaction non validée
                # dans le délai. 'cancelled' (et non 'timeout') pour cohérence
                # de statut entre les deux moteurs.
                result.final_status = "cancelled"
                result.final_message = (
                    "Transaction annulée par l'opérateur "
                    "(USSD non validé dans le délai imparti)."
                )
                log.info("USSD validation timeout (%ds) -> cancelled", settings.retry_window_s)
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
                # Use the authoritative source text (the one that matched the
                # keyword) as context for _friendly, NOT agent_message which
                # may be a generic "loader error" unrelated to the real cause.
                result.final_message = self._friendly(
                    status, req.network, src[:300]
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
