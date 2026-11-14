"""The voice agent: keeps the conversation's messages and turns each user turn into a
stream of words to speak, running tool calls in between (SPEC 6.2)."""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from .ollama import OllamaClient
from .tools import Toolbox

log = logging.getLogger("steady.agent")
MAX_ROUNDS = 4
NOTE_TOOLS = {"log_entry", "record_wellbeing", "end_conversation"}   # their results do not change the reply


class VoiceAgent:
    def __init__(self, llm: OllamaClient, tools: Toolbox, system: str) -> None:
        self.llm = llm
        self.tools = tools
        self.messages: list[dict] = [{"role": "system", "content": system}]

    async def respond(self, user_text: str | None = None, note: str | None = None) -> AsyncIterator[str]:
        """Add what the person said (and/or a [system note]) and stream the reply."""
        parts = [p for p in (note, user_text) if p]
        if parts:
            self.messages.append({"role": "user", "content": "\n".join(parts)})
        for _ in range(MAX_ROUNDS):
            content, calls = "", []
            try:
                async for kind, value in self.llm.chat_stream(self.messages, self.tools.specs):
                    if kind == "text":
                        content += value
                        yield value
                    else:
                        calls += value
            except BaseException:
                # Cut off (the person interrupted, or an error): keep what was written so far,
                # so the model remembers it spoke. mark_interrupted trims it to what was heard.
                self.messages.append({"role": "assistant", "content": content})
                raise
            message: dict = {"role": "assistant", "content": content}
            if calls:
                message["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]
            self.messages.append(message)
            if not calls:
                return
            failed = False
            for c in calls:
                result = await self.tools.call(c["name"], c["arguments"])
                failed = failed or "error" in result
                log.info("tool %s(%s) -> %s", c["name"], c["arguments"], result)
                self.messages.append({"role": "tool", "tool_name": c["name"], "content": json.dumps(result)})
            if self.tools.state.ended and content.strip():
                return                      # it already said goodbye in the same turn
            if content.strip() and not failed and all(c["name"] in NOTE_TOOLS for c in calls):
                return                      # it spoke and only took notes: nothing more to say
            if content.strip():
                yield " "                   # keep words of two rounds apart

    def mark_interrupted(self, spoken: str) -> None:
        """The person talked over the reply: remember only what was actually said aloud."""
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                m["content"] = (spoken.strip() + " [interrupted by the person]").strip()
                break
