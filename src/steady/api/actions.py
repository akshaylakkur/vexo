"""Endpoints for OpenClaw, called through steadyctl (SPEC 9). All need STEADY_API_TOKEN."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..actions.tasks import TaskError
from .auth import require_token

if TYPE_CHECKING:
    from ..app import Steady


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    subject: Optional[str] = Field(default=None, max_length=200)


class StatusBody(BaseModel):
    ok: bool
    note: str = Field(default="", max_length=1000)


def router(steady: "Steady") -> APIRouter:
    r = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    def tasks():
        if getattr(steady, "tasks", None) is None:
            raise HTTPException(503, "Tasks are not available in this steadyd.")
        return steady.tasks

    def refuse(e: TaskError):
        return HTTPException(409, str(e))

    @r.get("/tasks/{task_id}")
    async def get_task(task_id: int) -> dict:
        try:
            return await tasks().claim(task_id)
        except TaskError as e:
            raise refuse(e) from None

    @r.post("/tasks/{task_id}/send")
    async def send(task_id: int, body: SendBody) -> dict:
        try:
            return await tasks().send(task_id, body.text, body.subject)
        except TaskError as e:
            raise refuse(e) from None

    @r.post("/tasks/{task_id}/status")
    async def status(task_id: int, body: StatusBody) -> dict:
        try:
            return await tasks().finish(task_id, body.ok, body.note)
        except TaskError as e:
            raise refuse(e) from None

    @r.get("/tasks")
    async def list_tasks(limit: int = 20) -> list:
        items = sorted(tasks().tasks.values(), key=lambda t: -t.created_at)[:limit]
        return [t.public() for t in items]

    @r.get("/contacts")
    async def contacts() -> list:
        return tasks().contacts.public()

    @r.get("/summary")
    async def summary(period: str = "today") -> dict:
        from ..summary import as_text, summarize
        s = await summarize(steady, period)
        return {**s, "text": as_text(s)}

    @r.post("/brief")
    async def brief(force: bool = False) -> dict:
        b = getattr(steady, "brief", None)
        if b is None:
            raise HTTPException(503, "The brief is not available in this steadyd.")
        return await b.send_now(force=force)

    return r
