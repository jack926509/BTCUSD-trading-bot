import os
import asyncio
import anthropic

from src.ai.prompt_builder import PromptBuilder
from src.ai.response_parser import ResponseParser


class ClaudeClient:
    def __init__(self, config: dict):
        self._client  = anthropic.AsyncAnthropic()
        self._builder = PromptBuilder()
        self._parser  = ResponseParser()

        ai_cfg        = config.get("ai", {})
        self._model   = ai_cfg.get("model", "claude-haiku-4-5-20251001")
        self._max_tok = ai_cfg.get("max_tokens", 600)
        self._timeout = ai_cfg.get("timeout_seconds", 10)

    async def describe(self, symbol: str, signal) -> str:
        """
        非同步取得結構描述。
        使用 prompt cache 節省費用（system prompt 部分重複率高）。
        """
        system_prompt, user_content = self._builder.build_description_prompt(symbol, signal)

        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tok,
                    system=[
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_content}],
                ),
                timeout=self._timeout,
            )
            raw = response.content[0].text if response.content else ""
            return self._parser.parse_description(raw)
        except asyncio.TimeoutError:
            return "（Claude 描述超時）"
        except Exception as e:
            return f"（Claude 描述失敗：{e}）"
