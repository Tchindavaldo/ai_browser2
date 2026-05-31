"""DeepSeek LLM client with OpenAI-compatible API and SSE streaming."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx


@dataclass
class LlmConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    max_tokens: int = 4096

    def resolve_key(self) -> str:
        if self.api_key:
            return self.api_key
        env_var = "DEEPSEEK_API_KEY" if self.provider == "deepseek" else "ANTHROPIC_API_KEY"
        key = os.environ.get(env_var, "")
        if not key:
            raise ValueError(f"Missing {env_var} environment variable")
        return key


@dataclass
class LlmResponse:
    text: str = ""
    success: bool = True
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class LlmClient:
    """Async DeepSeek/OpenAI-compatible client with SSE streaming."""

    def __init__(self, config: LlmConfig | None = None):
        self.config = config or LlmConfig()
        self._client = httpx.AsyncClient(timeout=120.0)

    async def close(self):
        await self._client.aclose()

    async def send(
        self,
        system_prompt: str,
        user_content: list[dict],
        on_token: Callable | None = None,
    ) -> LlmResponse:
        """Send a message and stream the response.

        user_content is a list of content blocks:
          [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]
        """
        api_key = self.config.resolve_key()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_content})

        body = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "response_format": {"type": "json_object"},
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        url = f"{self.config.base_url}/chat/completions"
        result = LlmResponse()

        try:
            async with self._client.stream(
                "POST", url, json=body, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    result.success = False
                    result.error = f"HTTP {resp.status_code}: {error_body.decode()[:300]}"
                    return result

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]  # strip "data: "
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        # Check for error
                        if "error" in chunk:
                            result.success = False
                            result.error = chunk["error"].get("message", str(chunk["error"]))
                            return result
                        # Extract delta
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                result.text += content
                                if on_token:
                                    await on_token(content) if callable(on_token) else None
                        # Extract usage if present
                        usage = chunk.get("usage")
                        if usage:
                            result.input_tokens = usage.get("prompt_tokens", 0)
                            result.output_tokens = usage.get("completion_tokens", 0)
                    except json.JSONDecodeError:
                        continue

        except httpx.TimeoutException:
            result.success = False
            result.error = "LLM request timed out"
        except Exception as e:
            result.success = False
            result.error = str(e)

        return result
