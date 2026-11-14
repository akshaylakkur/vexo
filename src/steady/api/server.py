"""Steady's local API (SPEC 9): bound to 127.0.0.1 and used only by `steadyctl`, which is
how OpenClaw fetches and delivers tasks. Every endpoint needs the STEADY_API_TOKEN bearer
token. There is no web page and no unauthenticated endpoint.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from .actions import router as actions_router

if TYPE_CHECKING:
    from ..app import Steady


def create_app(steady: "Steady") -> FastAPI:
    app = FastAPI(title="Steady", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(actions_router(steady))
    return app
