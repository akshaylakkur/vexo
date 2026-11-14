"""Bearer-token check for the endpoints OpenClaw (via steadyctl) may call."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from ..config import secret


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = secret("STEADY_API_TOKEN", required=False)
    if not expected:
        raise HTTPException(503, "STEADY_API_TOKEN is not set: run `python -m steady openclaw setup`.")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Missing or wrong token.")
