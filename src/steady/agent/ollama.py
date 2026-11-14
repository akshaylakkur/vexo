"""Ollama Cloud chat client: streaming text and tool calls (SPEC 6.2).

One persistent HTTPS connection (no new handshake per turn). The key travels in the
Authorization header and is never logged. If the first model fails before it has said
anything, the fallback model is tried once.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("steady.agent")
BASE = "https://ollama.com"


class LLMError(Exception):
    pass


class OllamaClient:
    def __init__(self, key: str, model: str, fallback: str | None = None, base: str = BASE,
                 transport: httpx.AsyncBaseTransport | None = None, temperature: float = 0.4) -> None:
        self.model = model
        self.fallback = fallback
        self.temperature = temperature
        self._http = httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer " + key},
                                       timeout=httpx.Timeout(30.0, connect=10.0), transport=transport)
        self.last_first_token_ms: float | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None
                          ) -> AsyncIterator[tuple[str, Any]]:
        """Yields ("text", piece) as the model writes and ("tool_calls", [..]) when it calls tools."""
        models = [self.model] + ([self.fallback] if self.fallback and self.fallback != self.model else [])
        for i, model in enumerate(models):
            emitted = False
            try:
                async for item in self._stream(model, messages, tools):
                    emitted = True
                    yield item
                return
            except LLMError as e:
                if emitted or i == len(models) - 1:
                    raise
                log.warning("model %s failed (%s); trying %s", model, e, models[i + 1])

    async def _stream(self, model: str, messages: list[dict], tools: list[dict] | None):
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True, "think": False,
                                "options": {"temperature": self.temperature}}
        if tools:
            body["tools"] = tools
        t0 = time.perf_counter()
        self.last_first_token_ms = None
        try:
            async with self._http.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise LLMError(f"Ollama answered status {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise LLMError("Ollama reported an error")
                    msg = chunk.get("message") or {}
                    text = msg.get("content") or ""
                    if text:
                        if self.last_first_token_ms is None:
                            self.last_first_token_ms = (time.perf_counter() - t0) * 1000
                        yield "text", text
                    if msg.get("tool_calls"):
                        yield "tool_calls", [
                            {"name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}
                            for c in msg["tool_calls"]]
                    if chunk.get("done"):
                        return
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach Ollama ({type(e).__name__})") from None
        except (ValueError, KeyError) as e:
            raise LLMError(f"Ollama sent something unexpected ({type(e).__name__})") from None


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}
