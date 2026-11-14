# Steady: outbound caregiver phone call with a Deepgram voice agent

> **As built (branch `caregiver-call`):** Steady places the call itself, as recommended below,
> but runs it with **Pipecat 1.16** (Twilio media stream, Deepgram Nova-3 listening, Deepgram
> Aura-2 speaking, Ollama Cloud through its OpenAI-compatible `/v1` endpoint) instead of the
> Deepgram Voice Agent API. That keeps Ollama and the tools in Python, and "press 1" works.
> Code: `src/steady/call/`. Tests: `tests/test_call.py`.
>
> **Trigger:** an urgent (fall) conversation that ends with no answer, or with "needs help".
> Calls `caregiver`, then `backup_caregiver`, and records a `tasks` row (`caregiver_call`, tier 2)
> that ends `done` (someone confirmed) or `failed`. OpenClaw's Telegram `caregiver_alert` (M6) can
> run alongside it; nothing here depends on OpenClaw.
>
> **Setup:**
> 1. `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` (a voice number on
>    the account), plus the existing `DEEPGRAM_API_KEY` and `OLLAMA_API_KEY`.
> 2. `cp config/contacts.example.yaml config/contacts.yaml` and fill in names and phone numbers.
>    On a Twilio trial account, verify each number in the Twilio console first.
> 3. `brew install cloudflared`. Steady opens a quick tunnel to the call server (port 8766) at
>    startup; only the call server is public, never the dashboard API. Or set `call.public_url`.
> 4. `python -m steady doctor`, then `python -m steady call --to caregiver` for a test call.
>    The pretend band (`--sim`) never phones anyone.

Research for the M6 (OpenClaw) owner. Date: 2026-11-14. No code in the repo was changed.
Spec sections read: SPEC.md 5 (policy), 6.3 to 6.6 (voice agent and tools), 7 (OpenClaw action loop), 10 (config and secrets).

Labels used below:
- **Verified**: read on the official docs page or in the source code on GitHub during this session.
- **Unverified**: from search snippets, community posts or memory, or not tested end to end. Check before relying on it.

---

## 1. Recommendation

Use **architecture B, with OpenClaw running in parallel**. Steady's Python process places the call itself. It uses the Twilio REST API plus a small Twilio Media Streams to Deepgram Voice Agent API bridge, so Deepgram handles STT, TTS (Aura-2) and the turn-taking on the call. At the same moment, Steady posts the same `caregiver_alert` task to OpenClaw's `/hooks/agent`, and OpenClaw sends the Telegram message and handles the 10-minute follow-up as already planned.

Why not let OpenClaw's own voice-call plugin place the call:
- **Deepgram can't speak on it.** The plugin supports Deepgram for speech-to-text only. Its telephony TTS comes from Azure, Gradium, ElevenLabs or OpenAI, or falls back to Twilio's Polly `<Say>`. That fails the "Deepgram voice on the call" goal. (Verified in the source.)
- **No press-1 on the streaming path.** Its streaming path has no DTMF handling, so a "press 1 to confirm" doesn't work there.
- **An LLM sits in front of the dial.** Triggering it through a hook puts an LLM agent turn, on the larger, slower model, between the fall and the dial.

**"Triggered through OpenClaw" still holds where an LLM in the loop is acceptable.** Add one `steadyctl call <task_id> --role <role>` command, so OpenClaw can request a call: for its 10-minute follow-up, for a backup caregiver, or for a voice request like "call Priya".

**The urgent first dial never waits on an LLM**, and it still works when OpenClaw is down. This fits the spec's own split, where real-time conversation is on Steady's side of the table (SPEC 7.1).

---

## 2. What exists (findings)

### 2.1 Deepgram's phone path (Verified)

- **Voice Agent API.** One WebSocket, `wss://agent.deepgram.com/v1/agent/converse`, does STT, LLM ("think") and TTS ("speak"). You send one `Settings` message, then stream binary audio both ways.
  - Settings accept `audio.input/output.encoding = "mulaw"` and `sample_rate = 8000`, with `output.container = "none"`. That matches Twilio's 8 kHz mu-law exactly, so no resampling is needed.
  - Message types we need:
    - We send: `Settings`, `InjectAgentMessage` (field `message`, `behavior` `default|queue|interrupt`) and `FunctionCallResponse` (`type`, `id`, `name`, `content`).
    - We receive: `Welcome`, `SettingsApplied`, `ConversationText`, `UserStartedSpeaking`, `FunctionCallRequest` (`functions[]` with `id`, `name`, `arguments` as a JSON string, and `client_side`), plus `Error` and `Warning`.
  - Sessions close at 2 hours (changelog 2026-07-31). That doesn't matter for a short alert call.
- **Function calling** is supported.
  - Functions are defined under `agent.think.functions` (`name`, `description`, `parameters` as JSON Schema).
  - A function without an `endpoint` is client-side: Deepgram sends us `FunctionCallRequest` and we answer with `FunctionCallResponse`, echoing the `id`.
  - A function with an `endpoint` is called by Deepgram's servers. We shouldn't use that, because Steady is local.
- **BYO LLM (Ollama Cloud).**
  - `agent.think.provider.type = "open_ai"` plus `agent.think.endpoint = {url, headers}`. Deepgram's docs say "Any LLM endpoint that conforms to the OpenAI Chat Completions API format will work".
  - Ollama documents an OpenAI-compatible cloud base URL `https://ollama.com/v1`, with `Authorization: Bearer $OLLAMA_API_KEY`, and lists tools and streaming as supported on `/v1/chat/completions`.
  - So `url = https://ollama.com/v1/chat/completions` should work. **Unverified end to end:** nobody has tested Deepgram's open_ai provider against Ollama Cloud's tool-call stream. Test it in 10 minutes before relying on it.
  - Managed alternative: a Deepgram-hosted "Standard" model (for example `gpt-4o-mini`, `gpt-4.1-mini`, `claude-haiku-4-5`, `gemini-2.5-flash`), with no extra key.
- **Official outbound Twilio reference repo (Python, MIT):** `deepgram-devs/deepgram-voice-agent-outbound-telephony`.
  - Stack: Starlette and uvicorn, `deepgram-sdk==6.0.0`, `twilio==9.10.2`.
  - It places the call with Twilio REST and inline TwiML `<Connect><Stream url="wss://host/twilio"/></Connect>`, then bridges the Twilio WebSocket to the Voice Agent.
  - Its Settings: input and output mulaw at 8000, listen `flux-general-en` (v2), think `open_ai`/`gpt-4o-mini` by default, speak Deepgram `aura-2-thalia-en`.
  - It also covers barge-in (sends a Twilio `clear` event on `UserStartedSpeaking`), async answering-machine detection with an Aura-2 voicemail message, and a silence monitor that uses `InjectAgentMessage`.
  - Local runs use ngrok or zrok with `SERVER_EXTERNAL_URL`.
  - The repo has only 7 stars and was last pushed in March 2026. Treat it as a crib sheet, not a dependency.
- **Other examples.** **Unverified (search results only, not opened):** a Twilio blog post with an outbound walkthrough, and a Node.js Twilio plus Voice Agent example in `deepgram/examples` (PR #104).

### 2.2 Twilio facts (Verified unless marked)

- **Bidirectional streams.** `<Connect><Stream>` creates a bidirectional stream. The `start` event carries `streamSid`, `callSid`, `customParameters` (from `<Parameter>` nouns) and `mediaFormat` `audio/x-mulaw` at 8000.
- **Messages we send.** Audio goes back as `{"event":"media","streamSid":...,"media":{"payload":<base64>}}`. To cut off playback on barge-in, send `{"event":"clear","streamSid":...}`.
- **DTMF.** Key presses arrive as `{"event":"dtmf","dtmf":{"track":"inbound","digit":"1"}}`, on bidirectional streams and inbound only. This is the press-1 acknowledgement.
- **Stopping a stream.** The only way to stop a bidirectional stream is to end the call.
- **Trial accounts:**
  - Calls go only to verified numbers (up to 5), within the sign-up country.
  - The trial includes 75 voice minutes and expires after 30 days.
  - Deepgram's docs say trial accounts play a disclaimer before connecting.
  - **Unverified (community reports only):** the trial prompt asks the callee to "press any key" before your TwiML runs.
  - Upgrading removes the restrictions.
- **WebSocket signing.** **Unverified:** whether Twilio signs the WebSocket handshake with `X-Twilio-Signature`. The sketch uses a per-call secret token instead.

### 2.3 OpenClaw voice-call support (Verified from docs.openclaw.ai and github.com/openclaw/openclaw source)

- **The plugin exists.** `@openclaw/voice-call` runs inside the Gateway.
  - Install with `openclaw plugins install @openclaw/voice-call`.
  - Config lives under `plugins.entries.voice-call.config`.
  - Providers: `twilio` (Programmable Voice plus Media Streams), `telnyx` (Call Control v2), `plivo`, `mock`.
- **Key config:**
  - `provider`, `fromNumber`, `toNumber`, and `twilio.accountSid` / `twilio.authToken` (SecretRef supported).
  - `serve.port` / `serve.path` (example 3334, `/voice/webhook`).
  - Public exposure, pick one: `publicUrl`, `tunnel: { provider: "ngrok" }`, or `tailscale: { mode: "funnel" }`. Setup refuses loopback or private URLs.
  - Outbound behavior: `outbound.defaultMode: notify | conversation` and `outbound.notifyHangupDelaySec` (default 3).
  - Limits: `maxDurationSeconds` (300), `maxConcurrentCalls` (1), `staleCallReaperSeconds` (120).
  - Optional: `responseModel`, and `twilio.record` (off by default).
- **Ways to trigger a call:**
  - **CLI:** `openclaw voicecall call --to "+1..." --message "..." [--mode notify|conversation] [--brief <json>]`. Mode defaults to `conversation` for `call`. When a Gateway is running, the CLI delegates to it and prints `{callId}`. Also available: `continue`, `speak`, `dtmf`, `end`, `status --json`, `tail`, `smoke --to ... --yes`, and `setup --json`.
  - **Agent tool** `voice_call`, with actions `initiate_call` (message, to?, mode?, dtmfSequence?), `continue_call`, `speak_to_user`, `send_dtmf`, `end_call` and `get_status`.
  - **Gateway RPC:** `voicecall.initiate`, `voicecall.start`, `voicecall.continue` (blocks and returns the transcript), `voicecall.speak`, `voicecall.dtmf`, `voicecall.end` and `voicecall.status`.
  - **Unverified:** these can probably be called with `openclaw gateway call <method> --params '<json>'`. The docs index lists those flags, but the method page wasn't read.
  - There is no dedicated HTTP trigger. From outside, the options are the CLI, Gateway RPC, or `/hooks/agent` with an agent turn that uses `voice_call`.
- **Deepgram inside OpenClaw:**
  - The bundled `deepgram` extension declares only `mediaUnderstandingProviders` and `realtimeTranscriptionProviders`. Setting `streaming.provider: "deepgram"` gives Deepgram STT on calls.
  - **There is no Deepgram TTS.** It's absent from `extensions/deepgram/openclaw.plugin.json` and from the docs' TTS provider list.
  - `src/telephony-tts.ts` handles Azure, Gradium, ElevenLabs and OpenAI output formats. `src/providers/twilio.ts` falls back to TwiML `<Say voice=Polly...>` when telephony TTS isn't configured or no stream is active.
- **Press-1.**
  - Its Twilio `<Gather>` is `input="speech"`.
  - `src/media-stream.ts` has no DTMF handling. `providers/twilio.ts` does parse a `Digits` param into a `call.dtmf` event, but no path was found that collects digits on an outbound streaming call.
  - So acknowledgement on that path is spoken, and is judged by the OpenClaw agent.
- **Hooks (`/hooks/agent`):**
  - Body: `message`, `name`, `agentId`, `sessionKey` (only if `allowRequestSessionKey`), `deliver` (default true), `channel` plus `to` (both required for direct delivery; `"last"` gives a 400), `waitForCompletion`, `model`, `thinking` and `timeoutSeconds`.
  - Auth: `Authorization: Bearer <hook token>` or `x-openclaw-token`. A query-string token is rejected.
  - `Idempotency-Key` is supported for retries.
  - The response is `{ok, runId}`, plus `completion` when waiting. A 200 means the run was admitted, not that anything was delivered.

---

## 3. A vs B

| | A: OpenClaw places the call (voice-call plugin) | B: Steady places the call (Twilio + Deepgram Voice Agent), OpenClaw in parallel |
|---|---|---|
| Deepgram on the call | STT only. TTS is OpenAI, ElevenLabs, Azure, Gradium or Twilio Polly | STT, TTS (Aura-2) and turn-taking, all Deepgram |
| Hackathon effort | Low if you accept non-Deepgram TTS: config, a tunnel, a skill update. About 1 to 2 h (estimate) | Medium: about 150 to 250 lines in Steady, plus a tunnel and a Twilio account. About 3 to 5 h (estimate) |
| Time from escalation to dial | Via hook: an LLM agent turn on the larger model, with at least 2 tool rounds (`steadyctl task get`, `voice_call`), probably 10 to 40 s (estimate, unverified). Via Steady running the CLI directly: about 1 to 2 s | About 1 s (one Twilio REST call). No LLM before the greeting |
| Turn latency on the call | STT (Deepgram), then OpenClaw `responseModel`, then TTS, then Twilio | Single Deepgram pipeline. Function calls are client-side and local |
| Reliability for a fall | Depends on the OpenClaw Gateway, its agent loop and its LLM. Acknowledgement is judged by an LLM | Depends on Steady (already up), Twilio, Deepgram and the tunnel. Press-1 is handled in code, with no LLM. Still works if OpenClaw is down |
| Acknowledgement back to Steady | The agent must run `steadyctl task done` after reading the transcript | Steady sets task `done` directly when it gets DTMF "1" or the `acknowledge_alert` function |
| Secrets | Twilio SID and token in OpenClaw config, a TTS vendor key, and `DEEPGRAM_API_KEY` in the OpenClaw env | Twilio SID, token and number in Steady `.env`, beside `DEEPGRAM_API_KEY`. OpenClaw never sees them |
| Public exposure | Plugin webhook port through ngrok or Tailscale Funnel (built in) | A separate small Steady listener (not the main API) through cloudflared or ngrok |

**Fallback option if the team insists that OpenClaw places the call (A-direct).**
- Steady runs `openclaw voicecall call --to <phone> --message "<script>" --mode notify` as a subprocess the moment it escalates. There's no LLM before the dial, and no Python telephony code.
- For conversation mode, set `streaming: { enabled: true, provider: "deepgram" }` and a TTS provider (OpenAI or ElevenLabs).
- Accept that the voice is not Deepgram's, and that acknowledgement is spoken and then judged by the agent.

---

## 4. Architecture sketch (recommended B)

```
 band ── fall_suspected ──▶ policy (urgent) ──▶ headphone check-in (6.4)
                                                   │ 2nd silence
                                                   ▼
                                    ESCALATING: create task #42 caregiver_alert, tier 2 (no confirm)
                                                   │
                      ┌────────────────────────────┴─────────────────────────────┐
                      ▼                                                            ▼
     Steady CallLadder(task 42)                                 POST 127.0.0.1:18789/hooks/agent
     rung 1 caregiver, rung 2 backup_caregiver,                 "STEADY TASK #42 ..." (unchanged spec 7.2)
     rung 3 caregiver again (config)                            OpenClaw: Telegram message now,
                      │                                         follow-up after 10 min unless task shows ack
                      ▼
     Twilio REST: Calls.create(To, From, Twiml=<Connect><Stream url=wss://PUBLIC/twilio/stream>
                                 <Parameter name="token" value=one-time/>, Timeout=25,
                                 StatusCallback=https://PUBLIC/twilio/status?token=...)
                      │ caregiver answers
                      ▼
     Twilio ──wss──▶ cloudflared/ngrok ──▶ Steady call bridge (127.0.0.1:8766, separate uvicorn app)
                      │  mulaw 8k both ways, base64 in JSON on the Twilio side, raw bytes on the Deepgram side
                      ▼
     Deepgram Voice Agent (wss://agent.deepgram.com/v1/agent/converse)
       listen: flux-general-en  think: managed Standard LLM or BYO Ollama Cloud  speak: aura-2-thalia-en
       greeting = scripted alert (facts from the task), functions: acknowledge_alert, end_call
                      │
     ack paths:  Twilio "dtmf" digit "1"  ──┐
                 FunctionCallRequest         ├─▶ Steady: task #42 done (note "Priya acknowledged by phone 14:05:12, pressed 1")
                 acknowledge_alert ─────────┘     ladder cancelled, headphones: "Priya knows. Stay where you are."
                                                  agent says thanks and ends the call (REST status=completed)
     no-ack paths: status callback no-answer, busy, failed or canceled, or completed without ack,
                   or ack timer expired ─▶ next rung
     all rungs fail ─▶ task failed ─▶ spec 7.5 (headphone message, macOS notification, keep retrying).
                                      Telegram from OpenClaw is already out.
```

Task lifecycle mapping (SPEC 7.5):
- The task becomes `sent` when the hook is admitted or the first call is created, and `running` when Twilio reports ringing or answered.
- It becomes `done` on acknowledgement, and `failed` when the ladder is exhausted.
- `steadyctl task get 42` should expose `{ack: {by, channel: "phone", at}}`. The OpenClaw skill should then skip its 10-minute follow-up and just log.

---

## 5. What the caregiver call should do

1. **Greeting** (Deepgram `agent.greeting`, under 20 s, spoken at once). For example:
   "Hi Priya, this is Steady, the automated fall monitor for Raj. At 2:04 pm his band detected a possible fall, and he hasn't answered me for two minutes. If you think he needs emergency help, hang up and call 911. Press 1, or say 'got it', to confirm you received this."
2. **Acknowledgement:**
   - DTMF "1" is handled in code, with no LLM.
   - A spoken yes ("yes", "ok", "got it", "on my way") makes the LLM call `acknowledge_alert(eta_minutes?)`.
   - As a belt-and-braces fallback, run a simple keyword check on `ConversationText` with role user.
   - After acknowledgement the agent says: "Thank you. I'll tell Raj you know. Goodbye." It hangs up after `AgentAudioDone`, or after 5 s.
3. **Questions.** The prompt contains only the situation card: fall time, last motion, the wearer's last words if any, and whether the band is still worn. The agent answers in one or two sentences. It must never diagnose, and never say emergency services have been called.
4. **No acknowledgement.** If 10 s pass after the greeting without an ack, re-prompt once with `InjectAgentMessage` (`behavior: "queue"`): "Please press 1 or say yes if you got this." After 45 s in total with no ack, end the call and move to the next rung.
5. **Voicemail.** Without answering-machine detection, the greeting simply plays to voicemail. That works as a message left, but it counts as no ack, and the ladder continues. Optional: Twilio async AMD ($0.0075 per call), as in the Deepgram reference repo.
6. **Ladder and fallbacks.** The default ladder is caregiver (25 s ring), then backup_caregiver, then caregiver again. Telegram and SMS from OpenClaw go out in parallel at t = 0, not after the calls, so a missed call never delays the text.
7. **Timing against the spec.** Each rung is about 25 s of ringing plus about 45 s of call, roughly 70 s. Two rungs fit inside the spec's 3-minute urgent timeout; three don't. Either cap the ladder at 2 rungs, or give phone tasks their own timeout (for example 5 min).

---

## 6. Minimal code sketch (Python; fits Steady's FastAPI, uvicorn, httpx and websockets stack)

This sketch is untested. It needs no new dependency: the Twilio REST call goes through httpx with basic auth. If you want `twilio.request_validator.RequestValidator` for the status callback, add `twilio`.

```python
# src/steady/actions/phone.py  (sketch)
import asyncio, base64, json, os, secrets
import httpx, websockets
from fastapi import FastAPI, Request, WebSocket

DG_AGENT = "wss://agent.deepgram.com/v1/agent/converse"
SID, TOK, FROM = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"], os.environ["TWILIO_FROM_NUMBER"]
PUBLIC = os.environ["STEADY_CALL_PUBLIC_URL"].rstrip("/")          # https://xxxx.trycloudflare.com
NO_ANSWER = {"no-answer", "busy", "failed", "canceled"}

class PhoneCaller:
    def __init__(self, on_ack):                 # on_ack(task_id, how) -> marks task done, speaks to wearer
        self.on_ack, self.calls = on_ack, {}     # token -> {task_id, facts, result: Future, sid}

    async def call(self, to: str, task_id: int, facts: dict, ring_s=25, talk_s=45) -> str:
        token = secrets.token_urlsafe(16)
        fut = asyncio.get_running_loop().create_future()
        self.calls[token] = {"task_id": task_id, "facts": facts, "result": fut}
        host = PUBLIC.removeprefix("https://")
        twiml = (f'<Response><Connect><Stream url="wss://{host}/twilio/stream">'
                 f'<Parameter name="token" value="{token}"/></Stream></Connect></Response>')
        async with httpx.AsyncClient(auth=(SID, TOK), timeout=10) as h:
            r = await h.post(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json", data={
                "To": to, "From": FROM, "Twiml": twiml, "Timeout": ring_s,
                "StatusCallback": f"{PUBLIC}/twilio/status?token={token}",
                "StatusCallbackEvent": ["answered", "completed"]})
            r.raise_for_status()
            self.calls[token]["sid"] = r.json()["sid"]
        try:
            return await asyncio.wait_for(fut, ring_s + talk_s)       # "acked" | "no_ack"
        except asyncio.TimeoutError:
            await self.hangup(token); return "no_ack"
        finally:
            self.calls.pop(token, None)

    async def hangup(self, token):
        c = self.calls.get(token)
        if c and c.get("sid"):
            async with httpx.AsyncClient(auth=(SID, TOK), timeout=10) as h:
                await h.post(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls/{c['sid']}.json",
                             data={"Status": "completed"})

    def ack(self, token, how):
        c = self.calls.get(token)
        if c and not c["result"].done():
            c["result"].set_result("acked"); self.on_ack(c["task_id"], how)

def settings(f: dict) -> dict:
    return {"type": "Settings",
        "audio": {"input": {"encoding": "mulaw", "sample_rate": 8000},
                  "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"}},
        "agent": {
            "listen": {"provider": {"type": "deepgram", "version": "v2", "model": "flux-general-en"}},
            "think": {
                "provider": {"type": "open_ai", "model": "gpt-4o-mini", "temperature": 0.2},   # managed (Standard tier)
                # BYO Ollama Cloud instead (unverified end to end):
                # "provider": {"type": "open_ai", "model": "<ollama cloud model>"},
                # "endpoint": {"url": "https://ollama.com/v1/chat/completions",
                #              "headers": {"authorization": "Bearer " + os.environ["OLLAMA_API_KEY"]}},
                "prompt": (f"You are Steady, an automated fall monitor calling {f['caregiver']} about {f['wearer']}. "
                           f"Facts: possible fall at {f['fall_time']}; no answer from {f['wearer']} for {f['silent_min']} min; "
                           f"last words: {f.get('last_words') or 'none'}. Answer in one or two short sentences. "
                           "Never diagnose. Never say emergency services were called. If they confirm they got the "
                           "alert, call acknowledge_alert. If they say it's an emergency, tell them to call 911 now."),
                "functions": [{"name": "acknowledge_alert",
                               "description": "Caregiver confirmed they received the alert (yes, ok, got it, on my way).",
                               "parameters": {"type": "object", "properties": {"eta_minutes": {"type": "integer"}}}}]},
            "speak": {"provider": {"type": "deepgram", "model": "aura-2-thalia-en"}},
            "greeting": (f"Hi {f['caregiver']}, this is Steady, the automated fall monitor for {f['wearer']}. "
                         f"At {f['fall_time']} his band detected a possible fall, and he hasn't answered me for "
                         f"{f['silent_min']} minutes. If he needs emergency help, hang up and call 911. "
                         "Press 1, or say got it, to confirm you received this.")}}

def make_bridge_app(caller: PhoneCaller) -> FastAPI:     # serve on 127.0.0.1:8766; only this app goes through the tunnel
    app = FastAPI()

    @app.post("/twilio/status")
    async def status(request: Request, token: str):
        # TODO: validate X-Twilio-Signature (twilio.request_validator) before trusting the form
        form = await request.form()
        c = caller.calls.get(token)
        if c and not c["result"].done() and (form.get("CallStatus") in NO_ANSWER or form.get("CallStatus") == "completed"):
            c["result"].set_result("no_ack")
        return {}

    @app.websocket("/twilio/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        token = stream_sid = None
        async for raw in ws.iter_text():
            m = json.loads(raw)
            if m["event"] == "start":
                stream_sid, token = m["start"]["streamSid"], m["start"]["customParameters"].get("token")
                break
        if token not in caller.calls:                 # unknown or replayed stream
            await ws.close(); return
        async with websockets.connect(DG_AGENT, additional_headers={
                "Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]}) as dg:
            await dg.send(json.dumps(settings(caller.calls[token]["facts"])))

            async def from_twilio():
                async for raw in ws.iter_text():
                    m = json.loads(raw)
                    if m["event"] == "media":
                        await dg.send(base64.b64decode(m["media"]["payload"]))
                    elif m["event"] == "dtmf" and m["dtmf"]["digit"] == "1":
                        caller.ack(token, "pressed 1")
                        await dg.send(json.dumps({"type": "InjectAgentMessage", "behavior": "interrupt",
                                                  "message": "Thank you. I'll let him know you got this. Goodbye."}))
                    elif m["event"] == "stop":
                        return

            async def from_deepgram():
                async for msg in dg:
                    if isinstance(msg, bytes):
                        await ws.send_text(json.dumps({"event": "media", "streamSid": stream_sid,
                                                       "media": {"payload": base64.b64encode(msg).decode()}}))
                        continue
                    e = json.loads(msg)
                    if e["type"] == "UserStartedSpeaking":
                        await ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                    elif e["type"] == "FunctionCallRequest":
                        for fn in e["functions"]:
                            if fn["name"] == "acknowledge_alert":
                                caller.ack(token, "said yes")
                            await dg.send(json.dumps({"type": "FunctionCallResponse", "id": fn["id"],
                                                      "name": fn["name"], "content": "ok"}))

            done, pending = await asyncio.wait([asyncio.create_task(from_twilio()),
                                                asyncio.create_task(from_deepgram())],
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending: t.cancel()
    return app

async def call_ladder(caller, contacts, task_id, facts, rungs=("caregiver", "backup_caregiver")) -> bool:
    for role in rungs:
        c = contacts.get(role)
        if not c or not c.get("phone"):
            continue
        if await caller.call(c["phone"], task_id, {**facts, "caregiver": c["name"]}) == "acked":
            return True
    return False                                   # caller marks the task failed (spec 7.5)
```

Wiring notes:
- **Start the bridge.** Run `make_bridge_app` on its own `uvicorn.Server`, next to the existing API server in `app.py`, bound to `127.0.0.1:8766`. Only that port goes through the tunnel. The main API stays on loopback and is never exposed (SPEC 7.6).
- **Run the ladder** as an asyncio task from the ESCALATING state, at the same time as the existing hook POST.
- **On acknowledgement** (`on_ack`), do four things:
  - mark the task `done` with a note;
  - cancel the 3-minute timeout;
  - say the confirmation in the headphones ("Priya knows. Stay where you are.");
  - optionally POST a second hook message ("TASK #42 acknowledged by phone at 14:05, no follow-up needed"), or let the skill read it from `steadyctl task get`.
- **Hang up after the goodbye.** The sketch doesn't do this yet. After the DTMF or function acknowledgement, wait for the next `AgentAudioDone`, then call `caller.hangup(token)`.

---

## 7. Setup steps for the M6 owner

1. **Twilio.**
   - Create an account and get a US local voice number ($1.15/month).
   - On a trial: verify each caregiver number (SMS code, up to 5), and expect the trial disclaimer before the call connects.
   - Upgrade before the demo to drop the disclaimer and the verified-only limit.
2. **`.env`.** These go beside the existing keys and must never be committed or printed:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_FROM_NUMBER` (E.164)
   - `STEADY_CALL_PUBLIC_URL`
3. **`config/contacts.yaml`.** Add `phone` (E.164) to `caregiver` and `backup_caregiver`. Mirror the field in `contacts.example.yaml`.
4. **`config/steady.yaml`** (suggested, no secrets): `phone: { enabled: true, listen_port: 8766, ring_seconds: 25, talk_seconds: 45, ladder: [caregiver, backup_caregiver], think: managed }`.
5. **Tunnel**, pick one:
   - `cloudflared tunnel --url http://127.0.0.1:8766`: no account needed, but the `trycloudflare.com` URL changes every run, so update `STEADY_CALL_PUBLIC_URL`.
   - `ngrok http 8766`: needs an account and authtoken.
   - **Unverified:** both are commonly used with Twilio WebSockets, so prove it in the smoke test.
   - Add a `/twilio/health` GET and a `python -m steady doctor` check that fetches it through the public URL.
6. **Smoke test.** Add a dev command (for example `python -m steady call-test --to +1...`) that runs one rung with fake facts. Pass criteria:
   - the greeting is heard in the Aura-2 voice;
   - pressing 1 marks the test task `done`;
   - saying "got it" in a second run marks it `done`;
   - letting it ring out returns `no_ack`.
7. **Caregiver phone.** Save the Twilio number as a contact ("Steady ALERT") and allow it through Focus and Silence Unknown Callers. Otherwise iOS may silence it.
8. **OpenClaw skill.**
   - Keep the hook and Telegram flow as planned.
   - In `openclaw/skills/steady/SKILL.md`, add: "if `task get` shows `ack.channel == phone`, do not send the 10-minute follow-up; log only".
   - Optional: `steadyctl call <id> --role <role>` for OpenClaw-requested calls (tier 1 when not urgent, because it reaches another person).
9. **Demo mode.** Same flow. Optionally set a shorter ring (20 s) and a single rung.

If the team picks A-direct instead:
- Run `openclaw plugins install @openclaw/voice-call`.
- Configure `provider: "twilio"`, `twilio.accountSid` / `authToken`, `fromNumber`, `tunnel: { provider: "ngrok" }` or `tailscale: { mode: "funnel" }`, `outbound.defaultMode: "notify"`, `streaming: { enabled: true, provider: "deepgram" }`, and a `tts` provider (OpenAI or ElevenLabs).
- Run `openclaw voicecall setup --json`, then `openclaw voicecall smoke --to +1... --yes`.
- Steady calls `openclaw voicecall call --to ... --message ... --mode notify`.

---

## 8. Costs (Verified prices; usage numbers are estimates)

| Item | Price |
|---|---|
| Deepgram Voice Agent, Standard (managed LLM), pay as you go | $0.075/min (Growth $0.068) |
| Deepgram Voice Agent, Custom BYO LLM (Ollama) | $0.065/min (Growth $0.059), plus Ollama Cloud usage |
| Deepgram new account credit | $200 free |
| Twilio outbound to US | $0.014/min |
| Twilio Media Streams | $0.0044/min |
| Twilio AMD (optional) | $0.0075/call |
| Twilio US local number | $1.15/month |
| Twilio trial | 75 voice minutes, 30 days |

What this adds up to:
- **All in:** about $0.09 to $0.10 per connected minute.
- **One fall alert:** with 1 to 2 calls of about 1 minute each, roughly $0.10 to $0.25.
- **A hackathon:** effectively free, on the Deepgram credit plus the Twilio trial or a $20 top-up. **Unverified:** the minimum top-up amount.

---

## 9. Open risks

1. **BYO Ollama behind Deepgram think is untested.** Tool-call streaming compatibility is unverified. Mitigation: default to the managed Standard LLM. The acknowledgement never depends on the LLM, because DTMF is handled in code.
2. **Trial friction.** The disclaimer is documented by Deepgram. A "press any key" prompt before TwiML is unverified. Both add seconds and confuse a stressed caregiver. Upgrade before the demo.
3. **Unknown-number screening.** iOS Silence Unknown Callers, carrier spam labels on new numbers, and Do Not Disturb can all stop the call getting through. Mitigation: a saved contact, and Telegram in parallel.
4. **Tunnel fragility.** Quick-tunnel URLs change on every run, and the Mac can sleep or lose network. A dead tunnel means Twilio can't open the stream, and the caregiver hears silence or an error. Mitigation:
   - a startup health check through the public URL;
   - treat "stream never started within 10 s of answered" as `no_ack` and move to the next rung;
   - keep Telegram in parallel;
   - `caffeinate` during the demo.
5. **Public endpoint security.** WebSocket handshake signing by Twilio is unverified. Mitigation:
   - a one-time token per call, rejected after use;
   - validate `X-Twilio-Signature` on `/twilio/status`;
   - expose only the bridge app, never the main API.
6. **Spec timing.** A 3-rung ladder exceeds the 3-minute urgent timeout (SPEC 7.5). Decide: cap at 2 rungs, or give phone tasks a separate timeout.
7. **Concurrency.** The wearer's headphone conversation (its own Deepgram STT and TTS sockets) and the phone agent run at the same time. Deepgram concurrency limits on pay as you go are unverified. Run one call at a time (a sequential ladder).
8. **Consent and recording.**
   - Don't record (Twilio `record` stays off).
   - The greeting says it's automated, and call audio goes to Deepgram for processing.
   - Get each caregiver's written opt-in to automated alert calls at setup.
   - **Unverified (from memory, not legal advice):** US TCPA rules treat AI-generated voices as "artificial" (FCC ruling, 2024), and need prior express consent unless the call is for emergency purposes.
9. **Not a 911 replacement.**
   - Never dial 911 from Twilio Programmable Voice in this project. **Unverified:** Twilio's own support for emergency calling on Programmable Voice.
   - The agent must never claim it called emergency services. The greeting tells the caregiver to call 911 themselves.
   - The dashboard and README should state that Steady is not a medical alert or emergency service.
10. **If option A is chosen:**
    - Deepgram TTS isn't available.
    - Acknowledgement is spoken only, and judged by an LLM.
    - The defaults `maxDurationSeconds: 300` and `maxConcurrentCalls: 1` apply.
    - The plugin's ngrok or Funnel tunnel adds another moving part inside the Gateway.

---

## 10. Sources

Deepgram:
- https://developers.deepgram.com/docs/outbound-telephony-agent
- https://github.com/deepgram-devs/deepgram-voice-agent-outbound-telephony (files read: `telephony/call_manager.py`, `voice_agent/agent_config.py`, `voice_agent/session.py`, `requirements.txt`)
- https://developers.deepgram.com/docs/configure-voice-agent
- https://developers.deepgram.com/docs/voice-agent-llm-models
- https://developers.deepgram.com/docs/voice-agent-function-call-request
- https://developers.deepgram.com/docs/voice-agent-inject-agent-message
- https://developers.deepgram.com/reference/voice-agent/voice-agent
- https://developers.deepgram.com/changelog/2026/7/31
- https://deepgram.com/pricing

OpenClaw:
- https://docs.openclaw.ai/plugins/voice-call
- https://docs.openclaw.ai/plugins/voice-call/configuration
- https://docs.openclaw.ai/plugins/voice-call/realtime-and-streaming
- https://docs.openclaw.ai/plugins/voice-call/tts-and-inbound-calls
- https://docs.openclaw.ai/plugins/voice-call/security-and-interfaces
- https://docs.openclaw.ai/tools/tts
- https://docs.openclaw.ai/automation/cron-jobs/webhooks
- https://docs.openclaw.ai/cli/gateway (index only)
- https://cdn.jsdelivr.net/npm/@openclaw/voice-call@2026.7.1/README.md
- https://github.com/openclaw/openclaw (files read: `extensions/deepgram/openclaw.plugin.json`, `extensions/voice-call/skills/voice-call/SKILL.md`, `extensions/voice-call/src/cli.ts`, `src/telephony-tts.ts`, `src/providers/twilio.ts`, `src/media-stream.ts`)

Twilio:
- https://www.twilio.com/docs/voice/media-streams/websocket-messages
- https://www.twilio.com/docs/usage/tutorials/how-to-use-your-free-trial-account
- https://www.twilio.com/en-us/voice/pricing/us

Ollama:
- https://docs.ollama.com/api/openai-compatibility

Not opened (search results only):
- https://www.twilio.com/en-us/blog/partners/integrations/building-an-outbound-voice-agent-with-twilio-and-deepgram
- https://github.com/deepgram/examples/pull/104
