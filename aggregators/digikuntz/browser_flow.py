"""DigiKUNTZ payment agent — creates transaction then drives Flutterwave checkout."""

import json
import logging

import httpx

from core.base import PaymentRequest, PaymentResult
from core.browser import BrowserController
from core.llm_client import LlmClient
from core import classifier
from . import status_poll

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
            f"TON TRAVAIL S'ARRETE DES QUE L'USSD EST DEMANDE — conclus IMMEDIATEMENT "
            f"(objective_reached=true) dans l'un de ces cas:\n"
            f"  a) Tu vois '#150*50#' / 'USSD' / 'dial' / 'composez' / 'en cours de "
            f"traitement' / 'autoriser ce paiement' apres le clic Payer → status "
            f"'ussd_sent'. Le client doit valider sur son telephone ; ce n'est PAS "
            f"a toi d'attendre la validation — le backend suit le statut ensuite. "
            f"NE recharge pas, NE re-clique pas, conclus tout de suite.\n"
            f"  b) La page affiche un ECHEC / refus / solde insuffisant AVANT tout "
            f"USSD → status 'error'.\n"
            f"  c) Page de resultat DigiKUNTZ (URL 'payment-done' / "
            f"'payments.digikuntz.com') deja affichee → LIS le status et conclus.\n"
            f"  d) Header 'DÉLAI DÉPASSÉ' (plafond) sans USSD ni succes → 'error'.\n"
            f"N'utilise PAS await_change : tu n'attends jamais la validation USSD.\n\n"
            f"Dans objective_result, mets toujours un JSON: "
            f"{{\"final_url\": \"...\", \"status\": \"ussd_sent|success|error\", "
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

        # Erreurs console/réseau, exposées (pas pour décider — pour l'audit).
        result.error_signals = sb.get_error_signals()

        # L'IA pilote désormais TOUTE la phase d'attente USSD DANS la boucle de
        # raisonnement (elle reste active après Payer, attend les changements via
        # await_change, et conclut elle-même succès/échec). Le code n'a donc plus
        # de watch qui juge. Ici on se contente de RECUEILLIR la conclusion.

        # Cas 1 — le plafond de la boucle IA a été atteint SANS que l'USSD soit
        # envoyé (page cassée, checkout qui ne répond pas…). Aucun paiement n'a
        # abouti côté opérateur -> 'failed' (relançable tout de suite). Ce n'est
        # PAS un verdict de paiement, juste le constat mécanique que la boucle a
        # dû s'arrêter avant d'aboutir.
        if loop_result.error == "deadline exceeded":
            result.final_status = "failed"
            result.final_message = (
                "Le checkout n'a pas abouti à temps (aucune demande USSD envoyée). "
                "Vous pouvez relancer un paiement."
            )
            log.info("Plafond boucle IA atteint sans USSD -> failed")
            result.payment_status = result.final_status
            return

        # Cas 2 — USSD demandé au client : le navigateur a FINI son travail.
        # Le statut final ne vient PAS de l'écran (coûteux, peu fiable) mais du
        # POLLING verify Flutterwave (source de vérité), jusqu'à un verdict
        # terminal (sans timeout : l'opérateur finit toujours par trancher). Le
        # webhook, s'il est joignable, met à jour en parallèle (cf. /webhook).
        ussd_detected = (
            agent_status in ("ussd_sent", "pending")
            or "150*50" in agent_message
            or "USSD" in agent_message.upper()
            or "*126" in agent_message
        )
        # Cas 2 — USSD demandé : le navigateur a FINI son travail. On ne poll PAS
        # ici (la tab est encore ouverte) : on EXTRAIT les params verify/mpesa
        # capturés et on les pose sur result.poll_after_close. Le runner ferme la
        # session (tab) PUIS appelle finalize_after_close() qui poll en HTTP pur,
        # sans navigateur. Le statut DigiKUNTZ (payin_*) traîne (reste pending même
        # après échec), donc on s'appuie sur le verify Flutterwave (fiable).
        if ussd_detected:
            vp = status_poll.extract_verify_params(sb.captured_requests)
            if vp:
                # Instant de l'envoi USSD = horodatage de la requête /charge
                # capturée (c'est elle qui déclenche le push USSD). Base du calcul
                # anti-doublon (la fenêtre opérateur court depuis là).
                charge_req = sb.get_flutterwave_charge()
                if charge_req and getattr(charge_req, "timestamp", 0):
                    result.ussd_sent_at = charge_req.timestamp
                log.info("USSD demandé — navigateur fermé, polling verify différé "
                         "(flw_ref=%s)", vp["flw_ref"])
                result.poll_after_close = {
                    "verify_params": vp,
                    "provider_id": result.provider_transaction_id or None,
                }
                # Statut provisoire : le navigateur a bien envoyé l'USSD. Le
                # verdict définitif viendra de finalize_after_close (après close).
                result.final_status = "ussd_sent"
                result.final_message = (
                    "USSD envoyé au client. Validation en attente sur le téléphone."
                )
                result.payment_status = result.final_status
                return
            log.warning("USSD détecté mais flw_ref introuvable — fallback conclusion IA")

        # Cas 3 — l'IA a conclu un statut métier AVANT l'USSD (échec immédiat au
        # charge, solde insuffisant lu à l'écran…). On respecte sa conclusion.
        if loop_result.success and agent_status not in ("unknown", ""):
            mapping = {"success": "successful", "error": "failed"}
            result.final_status = mapping.get(agent_status, agent_status)
            result.final_message = agent_message or result.final_status
            result.payment_status = result.final_status
            log.info("Verdict de l'IA (avant USSD): %s", result.final_status)
            return

        # Cas 4 — pas de conclusion nette : dernier recours LLM sur le texte/charge.
        charge_req = sb.get_flutterwave_charge()
        charge_body = charge_req.response_body if charge_req else ""
        combined = "\n".join(filter(None, [
            f"Message lu par l'agent: {agent_message}" if agent_message else "",
            f"Reponse /charge: {charge_body}" if charge_body else "",
        ]))[:3000] or (agent_message or "(aucune information)")
        log.info("IA sans conclusion nette -> LLM juge le texte/charge")
        status, msg = await self._interpret(combined, req.network)
        result.final_status = status or "unknown"
        result.final_message = msg
        result.payment_status = result.final_status

    async def finalize_after_close(self, req, result) -> None:
        """Verdict final après fermeture du navigateur (tab déjà fermée).

        Appelé par le runner quand decide_browser_outcome a posé
        result.poll_after_close (USSD demandé). Poll verify/mpesa Flutterwave en
        HTTP pur — aucun navigateur — jusqu'à terminal ou délai opérateur ; écoute
        aussi le webhook DigiKUNTZ via le registre (le premier des deux gagne).
        """
        spec = result.poll_after_close
        if not spec:
            return
        vp = spec["verify_params"]
        log.info("finalize_after_close — polling verify Flutterwave (flw_ref=%s, "
                 "navigateur déjà fermé)", vp["flw_ref"])
        poll = await status_poll.poll_verify_flutterwave(
            vp, req.network, provider_id=spec.get("provider_id"))
        result.final_status = poll["status"]
        result.final_message = poll["message"]
        result.settled_by = poll.get("settled_by")
        result.payment_status = result.final_status
        # Validation USSD par le client (verify -> successful) : on horodate
        # l'instant pour la garde anti-doublon après paiement réussi.
        if result.final_status == "successful":
            import time as _t
            result.validated_at = _t.time()
        log.info("Verdict polling verify (post-close): %s", result.final_status)

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
            # `id` (racine) = l'identifiant pour le polling statut DigiKUNTZ
            # (GET /transaction?transactionId=). On le propage dans inner sous
            # 'providerTransactionId' pour le runner/outcome.
            inner = dict(data.get("data", data))
            provider_id = data.get("id") or inner.get("id")
            if provider_id:
                inner["providerTransactionId"] = provider_id
            return inner
