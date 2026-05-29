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
  "objective_result": string (resume final si reached=true)
}

Regles: utilise des selecteurs CSS courts et stables (prefere [data-id],
[name], #id a .class generique). N'inclus jamais d'action si tu n'es pas
sur de l'element. Si l'objectif est atteint, mets objective_reached=true
et un actions:[] vide."""


@dataclass
class AgentDecision:
    thought: str = ""
    actions: list[dict] = field(default_factory=list)
    objective_reached: bool = False
    objective_result: str = ""
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


class ReasoningLoop:
    """Drive the browser via an LLM reasoning loop."""

    def __init__(
        self,
        browser: BrowserController,
        llm: LlmClient,
        max_turns: int = 20,
    ):
        self.browser = browser
        self.llm = llm
        self.max_turns = max_turns
        self.action_history: list[str] = []
        self._running = False

    async def run(self, objective: str) -> LoopResult:
        """Execute the reasoning loop until objective reached or max turns."""
        self._running = True
        result = LoopResult()

        for turn in range(self.max_turns):
            if not self._running:
                result.error = "stopped"
                break

            result.turns = turn + 1
            log.info("=== Turn %d/%d ===", turn + 1, self.max_turns)

            # 1. Snapshot
            try:
                snap = await self.browser.snapshot()
                log.info(
                    "Snapshot: url=%s html=%d screenshot=%d elements=%d",
                    snap.url,
                    len(snap.outer_html),
                    len(snap.screenshot_b64),
                    len(snap.interactive_elements),
                )
            except Exception as e:
                log.error("Snapshot failed: %s", e)
                result.error = f"snapshot failed: {e}"
                break

            # 2. Build prompt and send to LLM
            user_content = self._build_user_content(snap, objective, turn)
            llm_resp = await self.llm.send(SYSTEM_PROMPT, user_content)

            result.total_input_tokens += llm_resp.input_tokens
            result.total_output_tokens += llm_resp.output_tokens

            if not llm_resp.success:
                log.error("LLM error: %s", llm_resp.error)
                result.error = f"LLM error: {llm_resp.error}"
                break

            log.info("LLM response (%d chars): %s", len(llm_resp.text), llm_resp.text[:300])

            # 3. Parse decision
            decision = AgentDecision.parse(llm_resp.text)
            log.info("Thought: %s", decision.thought[:200])

            if decision.objective_reached:
                log.info("Objective reached: %s", decision.objective_result)
                result.success = True
                result.result = decision.objective_result
                break

            if not decision.actions:
                log.warning("No actions in decision, skipping turn")
                continue

            # 4. Execute actions
            for action in decision.actions:
                await self._execute_action(action)

        else:
            result.error = "max turns reached"

        self._running = False
        log.info(
            "Loop done: success=%s turns=%d tokens=%d/%d",
            result.success,
            result.turns,
            result.total_input_tokens,
            result.total_output_tokens,
        )
        return result

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

        text = (
            f"TOUR #{turn}\n\n"
            f"OBJECTIF: {objective}\n\n"
            f"URL ACTUELLE: {snap.url}\n\n"
            f"ELEMENTS INTERACTIFS (JSON):\n{elements_json}\n\n"
            f"HISTORIQUE D'ACTIONS:\n{history_str}\n\n"
            f"DOM (outerHTML, peut etre tronque):\n{snap.outer_html}\n\n"
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
            log.info("Action OK: %s", label)
        except Exception as e:
            self.action_history.append(f"{label} [FAIL: {e}]")
            log.warning("Action FAIL: %s -> %s", label, e)
