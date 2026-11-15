# Steady

**An always-on Parkinson's symptom monitor on a Vexo Open wristband that talks to you, and calls for help when you can't.**

Steady screens the wrist every 2 seconds for tremor and falls. When it finds something, it checks in by voice through your headphones. If you don't answer, it phones your caregiver. It tracks and reports symptoms; it never diagnoses or advises on medication.

## What it does

1. **Screens all day.** The band streams motion at 208 Hz. Every 2 s Steady computes features, decides what the arm is doing (rest, active, walking, not worn), and runs detectors for **rest tremor**, **action tremor** and **falls**. Every assessment is logged to a local SQLite database.
2. **Checks in by voice.** A moderate tremor lasting 60 s, or any possible fall, opens a spoken conversation: the band's microphone goes to Deepgram, an Ollama model with tools answers, and Deepgram's voice speaks in the headphones. Steady always finishes speaking before it listens, and keeps what you said meanwhile. A tap on a calm wrist means "yes, I'm OK".
3. **Holds back when it should.** Cooldowns, at most 3 check-ins an hour, quiet hours at night, nothing while the band is off the wrist. A fall interrupts any other conversation.
4. **Calls the caregiver.** If a check-in (a tremor or a fall) gets no answer twice, or you say you need help, Steady phones your caregiver (then a backup) using **Pipecat + Twilio + Deepgram**. They can ask questions, then press 1 or say yes to confirm they'll check on you. Afterwards a text and an email tell them what happened and how the call went.
5. **Sends messages for you.** Say "text Priya I'm running late" and **OpenClaw** writes it; Steady sends it by iMessage or email after your yes. A daily brief goes to your caregiver at 20:00.

## Built with

| | How Steady uses it |
|---|---|
| **Vexo Open band** | Band 03 over USB-C or Bluetooth: 208 Hz accelerometer and gyroscope, tap and free-fall events, step activity, the microphone (Opus), and the LED as status light. The firmware is untouched; all analysis runs on the Mac. |
| **Deepgram** | **Nova-3** live speech-to-text on the band's Opus audio (interim results, end-of-utterance signals), and **Aura-2** streaming text-to-speech, sentence by sentence, so the first words play while the model is still writing (about 0.5 s to first sound on band 03). On the phone call, the same models at 8 kHz through Pipecat. |
| **Pipecat** | The caregiver phone call is a Pipecat 1.16 pipeline: Twilio media stream, press-1 keypad, Deepgram STT, Steady's own turn-taking (a turn ends after a 1 s pause in the words, so a noisy room doesn't confuse it), Ollama with tools (`confirm_checking`, `get_latest_status`, `nothing_for_me`, `end_call`), Deepgram TTS. |
| **OpenClaw** | Writes the wording of spoken requests, the daily brief, and the incident report after a call. Locked to a `steady` skill and a `steadyctl` CLI: it never sees a phone number or an address, and Steady delivers only to an allowlist. |
| **Ollama Cloud** | The voice agent's model (fast, tool-calling) for check-ins and calls. |
| **Twilio** | The phone line for the caregiver call, reached through a `cloudflared` tunnel that exposes only the call server. |

## How it fits together

```mermaid
flowchart LR
  B[Vexo band<br/>motion 208 Hz, taps, mic] --> S[Screening<br/>every 2 s]
  S --> DB[(SQLite)]
  S --> E[Episodes<br/>tremor, fall]
  E --> P[Policy<br/>severity, cooldowns,<br/>quiet hours]
  P --> V[Voice check-in<br/>Deepgram STT + Ollama + Deepgram TTS]
  V -- no answer / I need help --> C[Caregiver call<br/>Pipecat + Twilio + Deepgram]
  C --> O
  V -- "text Priya..." --> O[OpenClaw<br/>writes the message]
  O --> M[iMessage / email<br/>allowlist only]
```

## Pipecat: the caregiver call

When a check-in goes unanswered, Steady places a Twilio call and hands the line to a Pipecat 1.16 pipeline ([`src/steady/call/bot.py`](src/steady/call/bot.py)):

```
Twilio audio in → Keypad → Deepgram STT → Ears → context → Ollama → Mouth → Deepgram TTS → Twilio audio out
```

| Step | What it does |
|---|---|
| `FastAPIWebsocketTransport` + `TwilioFrameSerializer` | Twilio's media stream (8 kHz μ-law) in and out; hangs up the call when the pipeline ends |
| `Keypad` | Pressing 1 confirms at once, whatever the model is doing |
| `DeepgramSTTService` | Nova-3 transcribes the caregiver, boosted for the wearer's and caregiver's names |
| `Ears` | Steady's own turn-taking: a turn ends after a 1 s pause in the words, so a noisy room can't stall it |
| `OLLamaLLMService` | Ollama Cloud with four tools only: `confirm_checking`, `get_latest_status` (what the band shows right now), `nothing_for_me` (background talk), `end_call` |
| `Mouth` + `DeepgramTTSService` | Aura-2 speaks; everything Steady says on the call is logged |

Steady speaks first, says what happened, answers questions only from the facts and the band's live status, and never gives medical advice. Nobody answering twice ends the call, then Steady tries the backup caregiver. Each call is a `tasks` row that ends `done` (who confirmed, and how) or `failed` (why).

## Detection

- **Rest tremor:** a 3.5 to 7.5 Hz rhythm in the gyroscope while the arm is at rest, strong enough and concentrated in one peak. An episode starts after 3 of 5 positive windows (about 10 s).
- **Action tremor:** a faster 7.5 to 12 Hz postural rhythm, or a 4 to 12 Hz rhythm that dominates a movement.
- **Fall:** free-fall, a hard impact, then stillness for 10 s.

**Validated** by playing 19 labelled wrist conditions into the pretend band through the full pipeline:

| Result | |
|---|---|
| Symptom phases detected | 7 of 7 |
| Look-alikes left alone (typing, walking, gesturing, vibration...) | 9 of 9 |

Known limits: a tremor while walking is missed, and vigorous tooth brushing or a gentle voluntary 5 Hz rhythm can be flagged.

## Run it

Needs Python 3.11+, macOS, the band, and headphones. The full demo, start to finish, is in [`docs/go-live.md`](docs/go-live.md).

```bash
git clone https://github.com/akshaylakkur/vexo && cd vexo
git clone https://github.com/VexoAI-Org/vexo-open-kit.git
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install cloudflared                                   # caregiver calls
cp config/contacts.example.yaml config/contacts.yaml       # who may be contacted
```

`.env` (never committed): `DEEPGRAM_API_KEY`, `OLLAMA_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, plus `OPENCLAW_HOOK_TOKEN` and `STEADY_API_TOKEN` for OpenClaw (see `openclaw/README.md`).

```bash
export PYTHONPATH=src
python -m steady doctor                      # checks keys, models, band, Twilio, tunnel, contacts
python -m steady run                         # the always-on service on band 03
python -m steady run --sim                   # same, on a pretend band
python -m steady talk --episode rest_tremor  # one check-in conversation now
python -m steady call --to caregiver --fall  # rehearse the real caregiver call
python -m steady validate                    # the labelled detector validation
```

## Safety and privacy

- **Not a diagnosis.** Steady tracks and reports; it never says what a symptom means or suggests changing medication.
- **Local first.** All sensor data stays in SQLite on the Mac. Only speech goes to Deepgram, and the wearer is told so in the first conversation of the day.
- **Allowlist.** Only numbers and addresses in `config/contacts.yaml` can be contacted; models only see names.
- **Narrow exposure.** Only the call server is public, through the tunnel, with a one-time token per call. The pretend band never phones anyone.

## Project layout

```
src/steady/
  band/        connection, reconnects, ring buffer
  screening/   features, context, detectors, episodes, rollups
  policy/      when a finding becomes a check-in
  voice/       Deepgram STT and TTS, the conversation state machine
  agent/       the Ollama voice agent and its tools
  call/        the caregiver phone call (Pipecat + Twilio)
  actions/     OpenClaw tasks, iMessage and email delivery, daily brief
  validate/    the labelled validation protocol
tests/         102 tests (no network needed)
SPEC.md        the full design
```
