"""AI reasoning loop: snapshot -> LLM -> actions -> repeat."""

import json
import logging
from dataclasses import dataclass, field

from .browser import BrowserController, DomSnapshot
from .llm_client import LlmClient, LlmResponse

log = logging.getLogger("ai_browser2")

SYSTEM_PROMPT = """Tu es un agent browser IA. A chaque tour tu recois:
- Un screenshot PNG de la page
- Le DOM HTML (peut etre tronque)
- La liste JSON des elements interactifs (input, button, select, lien)
- L'historique des actions que tu as deja effectuees
- L'objectif de l'utilisateur

Tu reponds UNIQUEMENT en JSON valide, sans markdown, sans prose autour.
Schema exact:
{
  "thought": string,
  "actions": [
    {
      "type": "click"|"fill"|"select"|"scroll"|"navigate"|"wait"|"reload",
      "selector": string (CSS, requis pour click/fill/select),
      "value": string (texte pour fill, option pour select, URL pour navigate),
      "scroll_pixels": int (scroll uniquement)
    }, ...
  ],
  "objective_reached": bool,
  "objective_result": string (resume final si reached=true),
  "await_change": bool (optionnel)
}

Regles: utilise des selecteurs CSS courts et stables (prefere [data-id],
[name], #id a .class generique). N'inclus jamais d'action si tu n'es pas
sur de l'element. Si l'objectif est atteint, mets objective_reached=true
et un actions:[] vide.

await_change: mets-le a true quand tu as fini d'agir et que tu dois ATTENDRE
qu'un evenement exterieur change la page (ex: tu attends que l'utilisateur
valide le USSD sur son telephone). Le systeme attendra alors qu'un changement
reel de page survienne avant de te redonner la main — inutile de faire des
'wait' repetes. Tu peux mettre await_change=true avec actions:[] vide."""


@dataclass
class AgentDecision:
    thought: str = ""
    actions: list[dict] = field(default_factory=list)
    objective_reached: bool = False
    objective_result: str = ""
    await_change: bool = False  # l'IA attend un changement de page avant de re-juger
    raw_json: str = ""

    @classmethod
    def parse(cls, text: str) -> "AgentDecision":
        """Parse LLM JSON response into a decision."""
        d = cls(raw_json=text)
        try:
            # Strip any markdown wrapping
            clean = text.strip()
            if clean.startswith("```"):
                first_nl = clean.index("\n")
                last_fence = clean.rfind("```")
                clean = clean[first_nl + 1 : last_fence].strip()

            obj = json.loads(clean)
            d.thought = obj.get("thought", "")
            d.actions = obj.get("actions", [])
            d.objective_reached = obj.get("objective_reached", False)
            d.objective_result = obj.get("objective_result", "")
            d.await_change = obj.get("await_change", False)
        except (json.JSONDecodeError, ValueError) as e:
            log.error("Failed to parse LLM response: %s\nText: %s", e, text[:500])
            d.thought = f"PARSE ERROR: {e}"
        return d


@dataclass
class LoopResult:
    success: bool = False
    result: str = ""
    error: str = ""
    turns: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # Per-turn trace of what the AI saw/thought/did — for live console logs and
    # the persisted, queryable trace (GET /transactions/{ref}/trace).
    trace: list[dict] = field(default_factory=list)


class ReasoningLoop:
    """Drive the browser via an LLM reasoning loop."""

    def __init__(
        self,
        browser: BrowserController,
        llm: LlmClient,
        max_turns: int = 20,
        checkout_url_predicate: "callable | None" = None,
        max_elapsed_s: float | None = None,
    ):
        self.browser = browser
        self.llm = llm
        self.max_turns = max_turns
        # Optional guard: if the URL leaves the checkout (predicate returns False),
        # the loop stops immediately and records the redirect as the outcome.
        self.checkout_url_predicate = checkout_url_predicate
        # Plafond de sécurité (secondes). Au-delà, le header du tour signale
        # "délai dépassé" pour que l'IA conclue elle-même ; si elle ne conclut
        # toujours pas, la boucle s'arrête pour ne pas laisser /pay pendre.
        self.max_elapsed_s = max_elapsed_s
        self.action_history: list[str] = []
        self._running = False

    async def run(self, objective: str) -> LoopResult:
        """Execute the reasoning loop until objective reached or max turns."""
        import time as _time
        self._running = True
        self._started_at = _time.time()
        self._url_history: list[str] = []
        result = LoopResult()

        for turn in range(self.max_turns):
            if not self._running:
                result.error = "stopped"
                break

            result.turns = turn + 1
            n = turn + 1
            log.info("┌─ 🤖 IA tour %d/%d ─────────────────────", n, self.max_turns)

            # 1. Snapshot
            try:
                snap = await self.browser.snapshot()
                # Inject loop-level context the browser doesn't know about
                snap.elapsed_s = round(_time.time() - self._started_at, 1)
                if self.max_elapsed_s is not None and snap.elapsed_s > self.max_elapsed_s:
                    snap.deadline_exceeded = True
                if not self._url_history or self._url_history[-1] != snap.url:
                    self._url_history.append(snap.url)
                snap.url_history = list(self._url_history)
                log.info("│ 👁  vu: %d éléments interactifs | url=%s%s",
                         len(snap.interactive_elements), snap.url,
                         " | ⏱ DÉLAI DÉPASSÉ" if snap.deadline_exceeded else "")
            except Exception as e:
                log.error("│ ❌ snapshot échoué: %s", e)
                result.error = f"snapshot failed: {e}"
                result.trace.append({"turn": n, "error": f"snapshot failed: {e}"})
                break

            # 2. Build prompt and send to LLM
            user_content = self._build_user_content(snap, objective, turn)
            llm_resp = await self.llm.send(SYSTEM_PROMPT, user_content)

            result.total_input_tokens += llm_resp.input_tokens
            result.total_output_tokens += llm_resp.output_tokens

            if not llm_resp.success:
                log.error("│ ❌ LLM erreur: %s", llm_resp.error)
                result.error = f"LLM error: {llm_resp.error}"
                result.trace.append({"turn": n, "error": f"LLM error: {llm_resp.error}"})
                break

            # 3. Parse decision
            decision = AgentDecision.parse(llm_resp.text)
            log.info("│ 🧠 pensée: %s", decision.thought[:300])

            # Record this turn's trace entry.
            entry = {
                "turn": n,
                "url": snap.url,
                "elements": len(snap.interactive_elements),
                "thought": decision.thought,
                "actions": [self._action_label(a) for a in decision.actions],
                "objective_reached": decision.objective_reached,
            }
            result.trace.append(entry)

            if decision.objective_reached:
                log.info("│ 🏁 objectif atteint: %s", str(decision.objective_result or "")[:200])
                log.info("└────────────────────────────────────────")
                result.success = True
                result.result = decision.objective_result
                break

            if not decision.actions and not decision.await_change:
                log.warning("│ ⚠️  aucune action proposée, tour ignoré")
                log.info("└────────────────────────────────────────")
                continue

            # 4. Execute actions
            for action in decision.actions:
                await self._execute_action(action)
            log.info("└────────────────────────────────────────")

            # Filet de sécurité 17 min : l'IA a vu "DÉLAI DÉPASSÉ" dans le header
            # de ce tour et a quand même choisi d'agir plutôt que de conclure. On
            # ne décide PAS du verdict ici — on arrête juste la boucle pour ne pas
            # laisser /pay pendre. L'outcome (decide_browser_outcome) tranchera.
            if snap.deadline_exceeded:
                log.warning("│ ⏱ délai (%ss) dépassé sans conclusion de l'IA — arrêt boucle",
                            self.max_elapsed_s)
                result.error = "deadline exceeded"
                break

            # ATTENTE PASSIVE entre deux regards de l'IA : si l'IA a demandé
            # await_change (ex. USSD envoyé, elle attend la validation), le code
            # OBSERVE jusqu'au prochain changement de page (fait mécanique, sans
            # juger) au lieu de la rappeler en boucle. Elle reprend la main et
            # décide au tour suivant. Économise les tokens; l'IA reste seule juge.
            if decision.await_change:
                remaining = None
                if self.max_elapsed_s is not None:
                    remaining = self.max_elapsed_s - (_time.time() - self._started_at)
                    if remaining <= 0:
                        # Délai global dépassé: l'IA le verra (deadline_exceeded)
                        # au prochain tour et conclura. On reboucle sans attendre.
                        continue
                budget = min(remaining, 1020) if remaining is not None else 1020
                baseline = (snap.outer_html or "")[:500]
                log.info("│ ⏳ attente passive d'un changement de page (max %ds)…", int(budget))
                changed = await self.browser.wait_for_page_change(budget, baseline=baseline)
                if changed is None:
                    log.info("│ ⏳ aucun changement dans le budget — l'IA réévaluera")

        else:
            result.error = "max turns reached"

        self._running = False
        log.info("✅ IA terminée: succès=%s tours=%d tokens=%d/%d",
                 result.success, result.turns,
                 result.total_input_tokens, result.total_output_tokens)
        return result

    @staticmethod
    def _action_label(action: dict) -> str:
        t = action.get("type", "")
        sel = action.get("selector", "")
        val = action.get("value", "")
        label = f"{t} {sel}".strip()
        if val:
            label += f" = {val}"
        return label

    def stop(self):
        self._running = False

    def _build_user_content(
        self, snap: DomSnapshot, objective: str, turn: int
    ) -> list[dict]:
        """Build the multimodal user content blocks for the LLM."""
        content = []

        # Image block — only if using a vision-capable model
        # DeepSeek V4 Flash/Pro text models don't support image_url.
        # TODO: enable when using deepseek-v4-vision or similar.
        # if snap.screenshot_b64:
        #     content.append(
        #         {
        #             "type": "image_url",
        #             "image_url": {
        #                 "url": f"data:image/png;base64,{snap.screenshot_b64}"
        #             },
        #         }
        #     )

        # Text block
        elements_json = json.dumps(snap.interactive_elements, ensure_ascii=False)
        history_str = "\n".join(f"- {a}" for a in self.action_history) or "(aucune)"

        # Réseau — vision complète DevTools (uniquement si non vide)
        network_lines = []
        if snap.pending_requests:
            network_lines.append("REQUETES EN COURS (pas encore terminees):")
            for r in snap.pending_requests:
                network_lines.append(f"  [{r['age_s']}s] {r['type']} ...{r['url']}")
        if snap.recent_responses:
            network_lines.append("DERNIERES REPONSES HTTP:")
            for r in snap.recent_responses:
                line = f"  {r['method']} {r['status']} ...{r['url']}"
                if r.get("body"):
                    line += f"\n    body: {r['body']}"
                network_lines.append(line)
        if snap.failed_requests:
            network_lines.append("REQUETES ECHOUEES:")
            for r in snap.failed_requests:
                network_lines.append(f"  {r['method']} {r['error']} ...{r['url']}")
        if snap.console_errors:
            network_lines.append("ERREURS CONSOLE:")
            for e in snap.console_errors:
                network_lines.append(f"  {e}")
        network_section = ("\n" + "\n".join(network_lines) + "\n") if network_lines else ""

        # Historique d'URLs (uniquement si navigation multiple)
        url_history_str = ""
        if len(snap.url_history) > 1:
            url_history_str = "\nHISTORIQUE URLS: " + " → ".join(snap.url_history) + "\n"

        frame_info = (f" | iframe s'est détachée {snap.frame_detached_count}x"
                      if snap.frame_detached_count > 0 else "")
        # Fait observable: Payer a-t-il déjà été cliqué ? Deux preuves —
        #  (a) un POST /charge dans les réponses réseau (paiement réellement parti),
        #  (b) un clic submit/Payer réussi dans l'historique d'actions (même AVANT
        #      que le /charge n'apparaisse — c'est le cas qui causait le double-clic).
        # Si l'une est vraie, on l'affiche en gros pour que l'IA NE recharge PAS.
        # Le code ne décide pas, il rapporte le fait.
        charge_sent = any(
            r.get("method") == "POST" and "/charge" in r.get("url", "")
            for r in snap.recent_responses
        )
        pay_clicked = any(
            ("click" in a.lower()) and ("[OK]" in a)
            and ("submit" in a.lower() or "btn" in a.lower()
                 or "payer" in a.lower() or "pay" in a.lower())
            for a in self.action_history
        )
        charge_info = ""
        if charge_sent or pay_clicked:
            charge_info = (
                "\n\n⚠️ PAIEMENT DÉJÀ SOUMIS: tu as déjà cliqué Payer (voir "
                "HISTORIQUE D'ACTIONS / appel /charge). NE RECHARGE PAS, ne "
                "re-remplis pas, ne re-clique pas Payer — tu enverrais un 2e "
                "paiement (double débit). Le formulaire disparaît et un loader "
                "s'affiche: c'est NORMAL. ATTENDS (wait) et LIS le résultat "
                "(USSD #150*50#, succès, ou échec)."
            )
        deadline_info = ""
        if snap.deadline_exceeded:
            mins = int(snap.elapsed_s // 60)
            deadline_info = (
                f"\n\n⏱ DÉLAI DÉPASSÉ: {mins} min écoulées sans verdict final. "
                f"Le délai opérateur Mobile Money est dépassé — la transaction ne "
                f"sera plus validée. Si tu as déjà cliqué Payer et qu'aucun résultat "
                f"de succès n'est apparu, CONCLUS MAINTENANT: objective_reached=true "
                f"avec objective_result {{\"status\":\"failed\",\"message\":\"Délai de "
                f"validation dépassé ({mins} min) — transaction non confirmée à temps\"}}."
            )
        text = (
            f"TOUR #{turn} | {snap.elapsed_s}s depuis le debut{frame_info}{charge_info}{deadline_info}\n\n"
            f"OBJECTIF: {objective}\n\n"
            f"URL ACTUELLE: {snap.url}"
            f"{url_history_str}"
            f"{network_section}\n"
            f"ELEMENTS INTERACTIFS (JSON):\n{elements_json}\n\n"
            f"HISTORIQUE D'ACTIONS:\n{history_str}\n\n"
            f"DOM PAGE PRINCIPALE:\n{snap.page_outer_html}\n\n"
            f"DOM FRAME ACTIF (iframe si dedans):\n{snap.outer_html}\n\n"
            f"Reponds maintenant en JSON pur selon le schema."
        )
        content.append({"type": "text", "text": text})

        return content

    async def _execute_action(self, action: dict):
        """Execute a single action from the LLM decision."""
        action_type = action.get("type", "")
        selector = action.get("selector", "")
        value = action.get("value", "")

        label = f"{action_type} {selector}"
        if value:
            label += f" = {value}"

        try:
            if action_type == "click":
                await self.browser.click(selector)
            elif action_type == "fill":
                await self.browser.fill(selector, value)
            elif action_type == "select":
                await self.browser.select(selector, value)
            elif action_type == "scroll":
                pixels = action.get("scroll_pixels", 500)
                await self.browser.scroll(pixels)
            elif action_type == "reload":
                await self.browser.reload_payment()
            elif action_type == "navigate":
                await self.browser.goto(value)
            elif action_type == "wait":
                await self.browser.wait(selector or None, 2000)
            else:
                log.warning("Unknown action type: %s", action_type)
                label = f"UNKNOWN:{action_type}"

            self.action_history.append(f"{label} [OK]")
            log.info("│ 👉 action: %s ✓", label)
        except Exception as e:
            self.action_history.append(f"{label} [FAIL: {e}]")
            log.warning("│ 👉 action: %s ✗ (%s)", label, e)
