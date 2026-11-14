"""Twilio, the phone line: place an outbound call, and the TwiML that streams it to Steady.

Plain REST over httpx (no Twilio SDK). The auth token only ever goes in the Authorization
header; nothing here logs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

import httpx

from ..config import secret

API = "https://api.twilio.com/2010-04-01"
FINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}


class TwilioError(Exception):
    pass


@dataclass(frozen=True)
class TwilioCreds:
    account_sid: str
    auth_token: str
    from_number: str

    def __repr__(self) -> str:                      # keep the token out of logs and tracebacks
        return f"TwilioCreds({self.account_sid[:6]}..., from {self.from_number})"

    @classmethod
    def load(cls) -> "TwilioCreds | None":
        values = [secret(n, required=False) for n in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")]
        return cls(*values) if all(values) else None


class TwilioClient:
    def __init__(self, creds: TwilioCreds, http: httpx.AsyncClient | None = None) -> None:
        self.creds = creds
        self._http = http or httpx.AsyncClient(timeout=15)

    def _url(self, path: str) -> str:
        return f"{API}/Accounts/{self.creds.account_sid}{path}"

    @property
    def _auth(self) -> tuple[str, str]:
        return self.creds.account_sid, self.creds.auth_token

    async def call(self, to: str, twiml_url: str, status_url: str, ring_s: int = 25, max_s: int = 180) -> str:
        """Start a call. Twilio fetches `twiml_url` when it is answered. Returns the call SID."""
        r = await self._http.post(self._url("/Calls.json"), auth=self._auth, data={
            "To": to, "From": self.creds.from_number,
            "Url": twiml_url, "Method": "POST",
            "StatusCallback": status_url, "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "Timeout": str(ring_s),             # seconds to ring before "no-answer"
            "TimeLimit": str(max_s),            # hard cap on the call's length
        })
        if r.status_code >= 300:
            try:
                why = r.json().get("message", "")
            except ValueError:
                why = r.text[:200]
            raise TwilioError(f"Twilio refused the call ({r.status_code}): {why}")
        return r.json()["sid"]

    async def close(self) -> None:
        await self._http.aclose()


def twiml_stream(ws_url: str, token: str) -> str:
    """TwiML: connect the answered call's audio to our websocket, carrying the call's token."""
    return ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f'<Stream url={quoteattr(ws_url)}><Parameter name="token" value={quoteattr(token)}/></Stream>'
            '</Connect></Response>')
