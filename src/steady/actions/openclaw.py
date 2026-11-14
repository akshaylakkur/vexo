"""Steady -> OpenClaw: the gateway's webhook (POST {hooks_url}, Bearer OPENCLAW_HOOK_TOKEN).

The hook message carries no instructions of its own and no personal data: only the task
number and how to fetch it. OpenClaw wraps hook payloads as untrusted external content,
and that is fine: what to do comes from the `steady` skill, the task comes from Steady's
authenticated API.
"""
from __future__ import annotations

import logging

import httpx

from ..config import Config, secret

log = logging.getLogger("steady.actions")


class HookError(Exception):
    pass


class OpenClawHook:
    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = str(cfg.get("openclaw.hooks_url", "http://127.0.0.1:18789/hooks/agent"))
        self.model = cfg.get("openclaw.model")
        self.timeout_s = int(cfg.get("openclaw.agent_timeout_s", 300))
        self.steadyctl = cfg.get("openclaw.steadyctl") or "steadyctl"
        self._transport = transport
        self._cfg = cfg

    def message(self, task_id: int, task_type: str) -> str:
        return (f"Steady task {task_id} is waiting ({task_type.replace('_', ' ')}). "
                f"Use the steady skill to handle it: start with `{self.steadyctl} task get {task_id}`.")

    async def dispatch(self, task_id: int, task_type: str) -> str | None:
        """Start an OpenClaw agent run for this task. Returns the run id."""
        token = secret("OPENCLAW_HOOK_TOKEN", required=False)
        if not token:
            raise HookError("OPENCLAW_HOOK_TOKEN is not set (run: python -m steady openclaw setup)")
        body: dict = {"message": self.message(task_id, task_type), "name": "Steady",
                      "timeoutSeconds": self.timeout_s}
        if self.model:
            body["model"] = self.model
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                r = await client.post(self.url, json=body, headers={"Authorization": "Bearer " + token})
        except httpx.HTTPError as e:
            raise HookError(f"OpenClaw gateway not reachable at {self.url} ({type(e).__name__})") from None
        if r.status_code != 200:
            raise HookError(f"OpenClaw refused the task (status {r.status_code})")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if data.get("ok") is False:
            raise HookError("OpenClaw did not accept the task")
        return data.get("runId")
