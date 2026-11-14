# Steady — Specification

> An always-on Parkinson's symptom monitor on a Vexo Open band. It screens the wrist every two
> seconds and logs everything to a local database. When it finds a symptom, it talks with the
> wearer through their headphones, in real time, until they are OK. An OpenClaw agent in the
> background handles the slow, careful real-world work: messaging, email, briefings and reports.

| | |
|---|---|
| Band | **03** (`Vexo-HACK-03`), firmware 1.1.0, contract v1.1 |
| Host | macOS laptop (Python 3.11), Bluetooth |
| Cloud services | Deepgram (speech-to-text: Nova-3; text-to-speech: Aura-2), Ollama Cloud (LLM) |
| Agent backend | OpenClaw gateway (local, Node), models via Ollama Cloud |
| Status | Draft v1, 2026-11-14 |

**Medical position.** Steady **tracks and reports symptoms. It does not diagnose, and it never
advises changing medication.** Every spoken and written output says this where it matters.
Thresholds in this document are starting values from the movement-disorder literature, and
they are tuned on band 03 during calibration.

---

## 1. Goals and non-goals

**Goals**
1. **Continuous screening:** sample the wrist at all times and get a full symptom assessment
   every **2 seconds**.
2. **Log every assessment, keep what matters:** every 2-second assessment goes into a local
   SQLite database on the Mac. Only the **newest 2,000** are kept (about 67 minutes); older
   ones are deleted as it goes, **except positive assessments** (a symptom was detected),
   which are kept with their timestamps and all measurements for later analysis.
3. **Respond when something is detected:** log it, then open a **real-time, two-way voice
   conversation**. The band's microphone is the input; the wearer's headphones are the output.
   The conversation continues until the person is OK, or until it escalates.
4. **Delegate slow work:** an **OpenClaw agent** handles messaging, email, briefings and
   reports, with an approval policy appropriate to each action.
5. **Run reliably during a live demo:** survive dropped links and missing services, and work
   fully on the pretend band.

**Non-goals**
- Diagnosis, medication advice, or emergency-services calling (Steady alerts people; it does
  not dial 911).
- Processing on the band itself (its firmware cannot be changed; it only streams).
- Recording or logging while the band is out of Bluetooth range (the band cannot store data by
  itself; gaps are logged honestly).
- Always-on microphone. The mic is **only on during a conversation** (privacy, battery, and the
  rule that the LED is locked while the mic is on).

---

## 2. System overview

Steady is three loops that run at different speeds and share one database and one event bus.

```
                ┌──────────────────────────── steadyd (one Python asyncio process) ─────────────────────────────┐
 BAND 03        │                                                                                                │
 ┌──────────┐   │  ① SCREENING LOOP (every 2 s, always on)                                                       │
 │ MOTION2  │───┼─▶ Ingest ─▶ Ring buffer ─▶ Preprocess ─▶ Features ─▶ Context ─▶ Detectors ─▶ Episode tracker ──┐ │
 │ 208 Hz   │   │   (60 s raw)                                                         │                         │ │
 │ EVENT    │───┼─▶ (taps, free-fall) ────────────────────────────────────────────────┘                         │ │
 │ ACTIVITY │───┼─▶ (steps, cadence)                       every window ─▶ DB writer ─▶ SQLite (data/steady.db) │ │
 │ AUDIO*   │   │                                                                                 ▲              │ │
 │ LED      │◀──┼── LED manager                                                                   │              │ │
 └──────────┘   │                                    episodes / band events                       │              │ │
     *only in   │                                              ▼                                  │              │ │
   conversation │  Policy engine: log only? speak? escalate?  ◀────────────────────────────────────┼──────────────┘ │
                │        │ open conversation                     │ create task                   │                │
                │        ▼                                       ▼                               │                │
                │  ② VOICE LOOP (real time, on demand)    ③ ACTION LOOP (slow, background)       │                │
                │  band mic ─▶ Deepgram STT ─▶ Voice agent    Task manager ─▶ OpenClaw hook client ─┼──▶ OpenClaw    │
                │   (Opus)      (Nova-3)      (Ollama, tools)       ▲                             │    gateway     │
                │  🎧 ◀─ player ◀─ Deepgram TTS ◀─ sentences        │ status callbacks            │    (+ steady   │
                │                  (Aura-2)                         └── Local API (127.0.0.1) ◀───┴────  skill)     │
                └────────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Loop | Speed | Purpose | Main tech |
|---|---|---|---|
| ① Screening | Every 2 s, all day | Measure, classify context, detect symptoms, log | `vexo_open` async `Band`, numpy/scipy, SQLite |
| ② Voice | Sub-second turns, minutes long | Talk with the person until they are OK | Deepgram live STT + Aura-2 TTS, Ollama `/api/chat` with tools, `sounddevice` |
| ③ Action | Seconds to hours | Messaging, email, briefings, reports, reminders | OpenClaw hooks + `steady` skill + `steadyctl` CLI |

**One process owns the band.** The band accepts one Bluetooth connection at a time, so
`steadyd` holds it, and everything else (the dashboard, OpenClaw) goes through `steadyd`'s
local API. Nothing else connects to the band.

### 2.1 Processes

| Process | Started by | Listens on |
|---|---|---|
| `steadyd` (`python -m steady run`) | User / launch script | `127.0.0.1:8765` (local API + dashboard) |
| OpenClaw gateway | `openclaw gateway` | Loopback only; hooks enabled with a dedicated token |
| `caffeinate -i -w <steadyd pid>` | `steadyd` on startup | Keeps the Mac from idle-sleeping while Steady runs (lid must stay open) |

---

## 3. Band configuration and sampling

### 3.1 Settings applied on every connect (and re-applied after every reconnect)

| Setting | Value | Why |
|---|---|---|
| Fast motion stream | `configure(motion=208, sensors="both")` | From 208 Hz up, the band sends **every sensor sample** (no skipped samples). At ≤100 Hz it sub-samples without filtering, so fast content would alias into the tremor band. 2,810 B/s, allowed over Bluetooth, and within the contract's advice when audio also runs. |
| Ranges | `set_motion(accel_range=8, gyro_range=1000)` | 8 g so fall impacts aren't clipped at 2 g. 1000 °/s covers fast wrist turns in a fall. The stream resolution (1 mg, 0.1 °/s) is unchanged. |
| Gestures | `configure(events=True)` (all 7 gestures, default tap threshold 800 mg) | Free-fall feeds the fall detector. Taps and double-taps are the wearer's buttons (§6.6). |
| Steps | `configure(activity=1)` | Walking state, cadence and steps, every second. |
| Battery | `configure(battery=60)` | One reading a minute. |
| Signal | `configure(link=5)` | Signal strength every 5 s. Shows range problems on the dashboard. |
| Clock | Set by the library on connect | Every event has real time. |
| Mic | **Off** in screening; `configure(audio=True)` only during a conversation | Privacy. The LED stays usable. |
| LED | Screening: green "breathe", 4 s period, brightness 0.08 | Tells the wearer Steady is watching, using little power. See §6.7 for all LED states. |

**Eco mode** (battery < 25% and not charging, or `sampling.mode: eco`): motion at 100 Hz both
sensors, decimated on the host to 50 Hz. Detectors still work; impact peaks are less exact.
This trades accuracy for battery.

### 3.2 Bandwidth

| Stream | Screening | Conversation |
|---|---|---|
| Motion 208 Hz both | 2,810 B/s | 2,810 B/s |
| Audio (Opus) | 0 | ~3,200 B/s |
| Everything else | < 100 B/s | < 100 B/s |
| **Total** | **~2.9 kB/s** | **~6.1 kB/s** (contract: OK; watch for `stream-dropped`) |

If a `stream-dropped` notice arrives during a conversation, motion falls back to **100 Hz**
(the next rate the band offers below 208) for the rest of the conversation, then returns to
208 Hz.

### 3.3 Connection supervision

- Connect with `Band.connect(3, auto_reconnect=True)` (async API, `from vexo_open import Band`).
- The library retries for 2 minutes after a dropped link. After it gives up, `steadyd`'s
  **supervisor** keeps calling `Band.connect` with backoff (5 s, 10 s, 20 s, then every 30 s),
  forever. The wearer has to double-tap to make the band findable again, so a LED/voice cue
  isn't possible; the dashboard shows "Band out of range — double-tap the band".
- Every disconnected stretch is written to `connectivity_gaps` (start, end, reason).
- The band number **3** lives in one place (`config/steady.yaml: band.number`). The library
  checks it on connect (`wrong-band` closes the link).

---

## 4. The screening loop

### 4.1 Ingest

- The `motion` handler only **appends** each sample (`t`, `n`, `ax..gz`) to a numpy ring buffer
  holding **60 s at 208 Hz** (12,480 × 7 floats). No work happens in the handler.
- Samples are placed in time by `t`/`time` (the band's measured period), never by counting at
  the nominal rate.
- A jump in `n` = lost samples. These are counted per window.
- `EVENT` and `ACTIVITY` events go onto the event bus and into `band_events`.

### 4.2 Window schedule

- Every **2 s** (hop), the screener takes the **last 4 s** (window) from the ring buffer.
  Each window is half new, half shared with the previous one.
- 4 s gives **0.25 Hz frequency resolution**, enough to tell a 5 Hz tremor from 7 Hz.
- The analysis runs in a worker thread (`loop.run_in_executor`) and takes a few milliseconds,
  so it never blocks audio.
- **Validity:** a window with more than 10% lost samples, or that spans a reconnect, is
  stored with `valid=0` and skipped by every detector.

### 4.3 Preprocessing (per window)

1. Resample onto a uniform grid at 208 Hz using `t` (linear interpolation over small gaps).
2. **Anti-alias low-pass** at 20 Hz (4th-order Butterworth, zero-phase), then **decimate ×4 →
   52 Hz** (208 samples per window). The 208 Hz raw signal is kept for the fall detector only.
3. **Gravity:** low-pass the accelerometer at 0.3 Hz → gravity vector `g`. Dynamic
   acceleration = `a − g`. Orientation (pitch and roll) comes from `g`.
4. Gyroscope, in °/s, is used per axis. **Spectra are computed per axis and summed**, never on
   the magnitude (that would distort the frequencies).
5. Hann taper, then FFT (zero-padded to 512 points to place peaks precisely; the true
   resolution is still 0.25 Hz).

### 4.4 Features computed for every window

| Feature | Definition | Used by |
|---|---|---|
| `valid_frac` | Share of expected samples present | All |
| `acc_dyn_rms` | RMS of dynamic acceleration (g) | Context, non-wear, freeze |
| `gyro_rms` | RMS of gyro magnitude (°/s) | Context, non-wear |
| `vol_power` | Gyro power in 0.5–3 Hz (voluntary movement) | Context |
| `p_total` | Gyro power in 0.5–15 Hz | Tremor ratios |
| `dom_freq` | Frequency of the largest gyro spectral peak in 1–15 Hz | Tremor |
| `peak_prom` | Peak power ÷ median spectrum power (1–15 Hz) | Tremor (is it rhythmic?) |
| `rest_ratio` | Power 3.5–7.5 Hz ÷ `p_total` | Rest tremor |
| `action_ratio` | Power 4–12 Hz ÷ `p_total` | Action tremor |
| `tremor_amp` | RMS of gyro band-passed 3.5–12 Hz (°/s) | Tremor severity |
| `dysk_power`, `dysk_flat` | Power in 1–4 Hz; spectral flatness in 1–4 Hz | Dyskinesia |
| `freeze_index` | Accel power 3–8 Hz ÷ 0.5–3 Hz | Freezing |
| `vigor` | 95th percentile of gyro magnitude, plus accel RMS in 0.2–4 Hz | Slowness |
| `arm_swing_deg` | Walking only: angle range per stride on the main swing axis | Gait |
| `step_reg` | Walking only: autocorrelation at the stride lag (0.8–1.6 s) | Gait |
| `pitch`, `roll` | From gravity | Falls, context |
| `activity_state`, `cadence`, `steps_delta` | Latest band `ACTIVITY` frame | Context, gait |
| `battery`, `rssi` | Latest values | Status |

### 4.5 Context classification (runs first, every window)

Detectors only run in the context they're designed for. This is the main defence against
false alarms (brushing teeth is not a tremor).

| Context | Rule (evaluated in this order) |
|---|---|
| `not_worn` | `gyro_rms < 0.5 °/s` **and** `acc_dyn_rms < 3 mg`, sustained for **10 min** (a worn wrist always moves a little). Ends at the first window above those values. |
| `walking` | Band activity state is walk or run **and** cadence > 60 steps/min |
| `rest` | `vol_power` below `rest_threshold` (calibrated: personal resting baseline p90 × 1.5) |
| `active` | Everything else |

### 4.6 Detectors

Each detector returns, for each window, **positive / negative / not applicable**, plus metrics.
Per-person values come from calibration (§4.9).

#### Rest tremor (primary signal), context `rest`
A window is positive when **all five** hold (as built in M2):
1. **Right rhythm:** `dom_freq` between **3.5 and 7.5 Hz** and `rest_ratio` ≥ **0.5**
2. **One steady rhythm:** `peak_conc` ≥ **0.4** (share of 0.5–15 Hz power within ±1 Hz of the
   peak) and `peak_prom` ≥ **5**. `peak_conc` was added because sensor noise alone often
   scores high on `peak_prom`.
3. **Strong enough:** `tremor_amp` ≥ **4 °/s**, or 3 × the calibrated noise floor if larger
4. **Not a vigorous action:** `tremor_amp` ≤ **150 °/s** and `acc_dyn_rms` ≤ **0.35 g**
   (brushing teeth is in the right frequency range but far too violent)
5. **Context:** the arm is at rest (never judged while walking)

Severity by `tremor_amp` (provisional; tuned on band 03): **mild** < 10 °/s, **moderate**
10–30 °/s, **marked** > 30 °/s.

#### Action / postural tremor
The same checks 2–4, with `action_ratio` ≥ 0.5, in two situations: the arm is held or
resting and the rhythm is **7.5–12 Hz** (fast postural tremor, the essential-tremor range),
or the arm is in use (`active`) and a **4–12 Hz** rhythm dominates the movement. This is
reported, not diagnosed.

**Known limits of one wrist sensor:** a rest tremor during walking is not judged, and a
gentle voluntary rhythm of 3.5–7.5 Hz (whisking, light scrubbing) looks the same as a tremor.
The voice check-in is what resolves the second case.

#### Dyskinesia (experimental), contexts `rest` and `active`
Positive when `dysk_power` ≥ 4 × resting baseline **and** `dysk_flat` ≥ 0.6 (broad, no single
peak) **and** no tremor detector is positive. An episode needs **60 s** of mostly positive windows.

#### Slowness (bradykinesia), aggregate, contexts `active`
Not per window. Every **30 min** block with at least 2 min of active windows:
`z = (median vigor − baseline mean) ÷ baseline SD`. **Slow period** when `z < −1.5` for **two
blocks in a row**. Baseline = calibration day, then a rolling 7-day baseline by hour of day.

#### Gait and arm swing, context `walking`
Per walking window: `arm_swing_deg`, `step_reg`, cadence. Aggregated per walking bout
(≥ 20 s of walking). **Reduced swing:** bout median < **0.7 × baseline**. **Irregular steps:**
`step_reg` < **0.5**. Trend data only: logged, and included in reports. Never triggers voice.

#### Freezing of gait (experimental), gated by recent walking
Runs its own short analysis (2 s windows, every 0.5 s) only while walking, or within 5 s after
walking. Positive when: cadence collapses (< 30 steps/min, or activity state goes `still`)
**while** `acc_dyn_rms` stays > 0.05 g (trembling in place, not stopped) **and**
`freeze_index` > **2.5** (calibrated) for ≥ **2 s**. Accuracy from a wrist is lower than from an
ankle, and the UI labels it experimental.

#### Fall, on raw 208 Hz data, any context
Triggered by a band `free-fall` event **or** host-side `|a| < 0.35 g` for ≥ 80 ms. Then:
1. **Impact:** `|a|` peak ≥ **3.0 g** within 1.0 s after the free-fall.
2. **Aftermath (10 s):** `acc_dyn_rms` < 0.08 g (lying still) **or** orientation changed > 45°
   from before the fall.

Free-fall + impact + aftermath → **`fall_suspected` (urgent)**. Impact without aftermath →
`hard_impact` (logged only). On any fall trigger, ±30 s of raw data is saved to
`data/clips/<episode_id>.npz`.

### 4.7 Episode tracker

Window-level positives become **episodes** using hysteresis, so a single noisy window
doesn't trigger anything:

| Detector | Start when | End when | Minimum to count |
|---|---|---|---|
| Rest tremor | 3 of the last 5 windows positive (~10 s) | 3 negative windows in a row | 10 s |
| Action tremor | 3 of 5 | 3 negative | 10 s |
| Dyskinesia | 20 of 30 (~60 s) | 10 negative | 60 s |
| Freezing | 4 of 4 sub-windows (2 s) | 2 s of normal walking or of stillness | 2 s |
| Fall | Immediately (single event) | — | — |
| Slow period | 2 blocks | 1 normal block | 60 min |

Each episode gets one row in `episodes`. The row is **opened** at start (and the bus gets
`episode.started`), **updated** every window (duration, peak and mean metrics), and **closed**
at end (`episode.ended`). Severity can only go up during an episode, and each increase emits
`episode.escalated`.

### 4.8 Logging

- **Every window** (valid or not, every 2 s) → one `windows` row: features, context, detector
  flags (bitmask), battery, signal.
- **Rolling window of 2,000.** Once a minute (and at shutdown), assessments older than the
  newest 2,000 (`storage.keep_last_assessments`) are deleted **unless they are positive**
  (`flags != 0`). Positive rows are never deleted: timestamp, context and every measurement
  stay for analysis. The table therefore holds at most ~2,030 routine rows plus all positives.
- Writes are **batched** by a single writer task (one transaction per second). SQLite runs in WAL
  mode, so the API can read while the writer writes.
- **Hourly rollups** (time worn, rest time, tremor time and %, mean tremor amplitude,
  dyskinesia time, median vigor, steps, walking time, median arm swing, freezes, falls) →
  `hourly_rollups`. Reports and the voice agent read these, not raw windows.
- **Retention:** routine windows: the newest 2,000 only. Positive windows, rollups and
  episodes: kept indefinitely. Hourly rollups are computed from every assessment before it is
  deleted, so long-term totals (for example "tremor for 12% of resting time") stay correct.
  Raw clips are kept for falls and freezes only.

### 4.9 Calibration (`python -m steady calibrate`)

A guided, spoken calibration (~8 minutes, through the headphones), saved in `baselines`:

| Step | Duration | Gives |
|---|---|---|
| Band flat on the table | 60 s | Sensor **noise floor** (tremor-band RMS) |
| Sitting, arm resting on the lap | 3 min | **Rest threshold**, resting `dysk_power` |
| Normal desk activity (typing, reaching) | 2 min | **Vigor** baseline (first estimate) |
| Normal walk | 2 min | **Arm swing**, cadence, `step_reg`, freeze-index baseline |

Without calibration, the conservative defaults in `config/steady.yaml` apply and the
dashboard shows "uncalibrated".

---

## 5. Policy engine: what happens after a detection

Every detection is logged (§4.8). The policy engine decides whether it **also** needs a voice
conversation and/or an OpenClaw task.

### 5.1 Severity levels

| Level | Meaning | Default actions |
|---|---|---|
| `info` | Measured, nothing notable | Log only |
| `notice` | A symptom episode, mild | Log + include in the daily brief |
| `concern` | A clear, sustained or rising symptom | Log + **voice check-in** |
| `urgent` | Possible fall, or the person asks for help | Log + **immediate voice check-in** + escalation path |

### 5.2 Trigger rules (defaults; all in `config/steady.yaml`)

| Finding | Becomes | Voice? | OpenClaw? |
|---|---|---|---|
| `fall_suspected` | urgent | **Yes, at once**; interrupts any other conversation | Caregiver alert if needed (§6.5) |
| Freezing episode | concern | Yes; opens with a walking rhythm cue (§6.4) | Logged in the brief |
| Rest/action tremor, **moderate or worse**, ≥ 60 s | concern | Yes | Logged in the brief |
| Rest/action tremor, mild, or < 60 s | notice | No | Brief |
| Tremor time in the last hour > 2 × baseline hour | concern | Yes (once per hour) | Brief |
| Dyskinesia ≥ 3 min | concern | Yes | Brief |
| Slow period | notice → concern if 3 blocks | At concern | Brief |
| Suspected wearing-off (symptoms rising in the 60 min before a scheduled dose) | concern | Yes: "Your next dose is at 2:00. How are you feeling?" | Brief |
| Reduced arm swing, irregular steps | notice | No | Weekly report |
| Band double-tap | user request | Yes (person starts talking) | Depends on the request |

### 5.3 Guardrails against annoyance

- **Cooldown per finding type:** 30 min (tremor 45 min). An escalation in severity overrides
  the cooldown.
- **At most 3 non-urgent check-ins per hour.** Extra findings are logged and mentioned in the
  next conversation ("I also noticed...").
- **Quiet hours** (default 22:00–07:00): only `urgent` speaks.
- **One conversation at a time.** An urgent finding pre-empts a non-urgent conversation: the
  agent says "Sorry — I need to check something important", and the new conversation context
  takes over.
- **Not worn → no voice** (except a fall detected just before removal).
- **Demo mode** (`mode: demo`): cooldowns 2 min, tremor triggers voice at ≥ 20 s and any
  severity, and dashboard history is seeded.

**As built in M5** (`policy/rules.py`, `policy/engine.py`): the fall and tremor rules, every
guardrail above, and the double-tap trigger. The tracker publishes `episode.updated` every
window, so a tremor check-in opens the moment the episode reaches 60 s. Each episode gets one
decision once it reaches `concern`. Falls ignore cooldowns, quiet hours and the hourly limit.
A finding held back by quiet hours, the hourly limit, a running conversation or a band out of
reach is mentioned at the start of the next conversation ("I also noticed..."); one held back
by its cooldown is not. Pre-emption closes the running conversation as `preempted`, and the
urgent one opens with the apology. Rules for freezing, dyskinesia, slow periods, wearing-off
and "tremor time > 2 × baseline" wait for their detectors (M8 calibration for the baseline);
seeded demo history is M8.

---

## 6. The voice loop

### 6.1 Pipeline

```
 band AUDIO (Opus 20 ms) ──▶ STT (Deepgram live, nova-3, Opus passthrough)
                                │ interim text, final sentence, SpeechStarted, UtteranceEnd
                                ▼
                         Turn manager ──▶ Voice agent (Ollama /api/chat, stream, tools)
                                                 │ tokens
                                                 ▼
                                        Sentence splitter ──▶ TTS (Deepgram Aura-2 WebSocket)
                                                                     │ PCM 24 kHz, chunks
                                                                     ▼
                                                    Player (sounddevice → headphones)
```

### 6.2 Components

**Speech-to-text (`voice/stt.py`).** A subclass of the kit's `DeepgramSpeech`
(`vexo-open-kit/examples/python/speech.py`). The kit file is not edited.
- Sends the band's Opus packets as they are (no decoding).
- Options: `model=nova-3`, `endpointing=300` (ms of silence ends a sentence),
  `utterance_end_ms=1000`, `vad_events=true`, `keyterm` for medication names from config.
  `mip_opt_out=true`, interim results and the Deepgram address stay fixed by the kit.
- The subclass extends `_message` to also surface **`SpeechStarted`** (for interruptions) and
  **`UtteranceEnd`** (as a backup end-of-turn signal). The kit's callback runs on its own thread;
  results are passed to the asyncio loop with `loop.call_soon_threadsafe`.
- Keeps the kit's printed line saying that sound goes to Deepgram.

**Voice agent (`agent/voice_agent.py`).** Ollama Cloud, `POST https://ollama.com/api/chat`
with `stream: true` and `tools`. Authenticated with `Authorization: Bearer $OLLAMA_API_KEY`.
- Model: `voice_model` in config (a fast, tool-capable model). At startup `steadyd` checks it
  exists via `GET /api/tags` and refuses to start with a clear message if it doesn't.
- Context per turn: system prompt (§6.3) + conversation so far + a **situation card** (the
  triggering episode, current context, the last hour's rollup, medication schedule, names of
  contacts).
- Tool calls run in-process (§6.5). If a tool takes > 700 ms, the agent speaks a short filler
  ("One moment").

**Sentence splitter (`voice/sentences.py`).** Buffers streamed tokens. Sends text to TTS at
sentence ends (`. ! ?`), or at a comma after ≥ 60 characters, or after 1.2 s without a boundary.
The first sentence is sent as early as possible.

**Text-to-speech (`voice/tts.py`).** One persistent WebSocket per conversation:
`wss://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&sample_rate=24000`,
header `Authorization: Token $DEEPGRAM_API_KEY`. Messages: `Speak` (text), `Flush` (end of
a sentence), `Clear` (drop queued audio on interruption), `Close`. Binary messages are PCM.

**Player (`voice/player.py`).** `sounddevice.OutputStream`, 24 kHz mono int16, 20 ms blocks,
fed from a queue. The output device is matched by name from config (`audio.output_device`, for
example "AirPods"). If not found, it falls back to the system default and logs a warning.
`stop()` clears the queue immediately (≤ 20 ms).

### 6.3 Voice agent prompt (outline)

- Role: "You are Steady, a calm, warm assistant speaking through the wearer's headphones."
- Style: short sentences, one question at a time, no lists, no medical jargon; speak like a
  kind nurse on the phone.
- Purpose of each conversation: find out if the person is OK, help with what they need, log
  what they say, and end when they're OK.
- Hard rules: never diagnose; never suggest changing, adding or skipping medication ("that's
  one for your doctor"); for danger words ("I can't get up", "chest pain", "help"), call
  `request_task(type="caregiver_alert", urgency="urgent")` and tell the person to call
  emergency services if they can.
- Every action outside the conversation goes through a tool. The model never writes code or
  free-form commands.
- First conversation each day starts with: "Just so you know, I'm listening through your band
  and your voice goes to Deepgram to be transcribed."

### 6.4 Conversation state machine

```
 IDLE ──trigger──▶ OPENING ──▶ SPEAKING ◀──────────────┐
                    (mic on,      │ done                 │ agent reply
                     STT+TTS      ▼                      │
                     connect)   LISTENING ──turn end──▶ THINKING
                                  │  ▲                    │ tool calls
                       silence    │  └── interruption ────┘
                       timeouts   ▼
                               CHECKING_SILENCE ──2nd silence──▶ (urgent) ESCALATING ──▶ CLOSING
                                                               (otherwise) CLOSING
                     end_conversation tool ───────────────────────────────▶ CLOSING ──▶ IDLE
```

| State | What happens |
|---|---|
| OPENING | LED manager hands over the LED, then `configure(audio=True)` (light goes solid blue; ~140 ms to first audio). STT and TTS sockets connect in parallel. Target: < 1 s. |
| SPEAKING | The agent speaks first, from the situation card ("Hi, it's Steady. I noticed your hand has been shaking for a couple of minutes. How are you feeling?"). For freezing, the first thing played is a 4-beat walking cue ("step... step... step... step"). |
| LISTENING | Interim text goes to the dashboard. The turn ends on `speech_final` or `UtteranceEnd`. |
| THINKING | Stream the Ollama reply; the first sentence goes to TTS as soon as it's complete. |
| Interruption | While SPEAKING, a `SpeechStarted` followed by ≥ 2 interim words: stop the player, send TTS `Clear`, cancel the LLM stream, go to LISTENING. Headphones prevent the agent hearing itself. If config falls back to speakers, interruptions are off and STT is ignored while speaking. |
| CHECKING_SILENCE | 12 s without speech after a question: the agent asks once more, simpler ("Can you hear me? Just say yes or no."). Second silence: urgent → ESCALATING; otherwise CLOSING with outcome `no_response`. A **single band tap** counts as "yes, I'm OK" at any time. |
| ESCALATING | Creates an urgent `caregiver_alert` task (§7) and tells the person: "I'm letting Priya know. Stay where you are." Keeps listening for 2 more minutes. |
| CLOSING | Agent says goodbye. `configure(audio=False)`; the LED returns to screening green. Conversation row saved with outcome and summary. |

**Limits:** a conversation lasts at most 10 min (it then wraps up politely). The motion stream
and all detectors **keep running during the conversation**, so a fall mid-conversation is still
detected.

### 6.5 Voice agent tools

| Tool | Arguments | Effect |
|---|---|---|
| `get_status` | — | Current context, open episodes, last hour's rollup |
| `get_summary` | `period`: `last_hour` / `today` / `week` | Rollups as short facts with numbers |
| `log_entry` | `kind`: `medication` / `symptom` / `note`, `details` (object: e.g. `{name, dose, taken_at}`) | Row in `logs` |
| `record_wellbeing` | `feeling`: `ok` / `unsure` / `not_ok`, `notes` | Updates the conversation; feeds the outcome |
| `request_task` | `type` (§7.3), `details`, `urgency`: `normal` / `urgent` | Creates an OpenClaw task. Returns `needs_confirmation` with a prompt for tier-1 tasks (§7.4) |
| `confirm_task` | `task_id`, `confirmed` | Confirms or cancels a pending task. A single tap = confirm, a tilt = cancel |
| `end_conversation` | `outcome`: `ok` / `follow_up` / `needs_help` / `no_response`, `summary` | Closes the conversation |

**As built in M4:** `get_status`, `get_summary`, `log_entry`, `record_wellbeing` and
`end_conversation` exist. `request_task` and `confirm_task` arrive with OpenClaw in M6; until
then the agent is told it cannot contact anyone. When the model speaks and only takes notes
in the same turn, no second model call is made, which keeps the reply under about 0.5 s.

The agent must call `end_conversation` to end. The turn manager rejects `outcome=ok` unless the
person said they were OK, or tapped, during this conversation. That's the "until they are OK"
rule.

### 6.6 Band gestures

| Gesture | In screening | In a conversation |
|---|---|---|
| Double-tap | Start a conversation ("Hi, what can I do for you?") | Stop the agent speaking and listen |
| Single tap | — | "Yes / I'm OK / confirm" |
| Tilt (held 2 s) | — | "No / cancel" |

### 6.7 LED states

The band allows a colour only while the mic is off. A small LED manager owns the light and
re-applies it after reconnects.

| State | LED |
|---|---|
| Screening, calibrated | Green, breathe, 4 s, brightness 0.08 |
| Screening, uncalibrated | Amber, breathe, 4 s, brightness 0.08 |
| Detected; conversation about to open | Amber flash × 3 (≤ 1 s), then the mic turns on |
| Conversation (mic on) | Solid blue (forced by the band) |
| OpenClaw task done (speech not wanted) | White flash × 2 |
| Urgent alert sent, after the conversation | Red, blink, 1 s, until a tap |

### 6.8 Latency targets

| Step | Target |
|---|---|
| Detection → first word in the headphones | ≤ 2.5 s |
| Person stops talking → end of turn | 300–500 ms |
| End of turn → first Ollama sentence | ≤ 900 ms |
| Sentence → first audio | ≤ 300 ms |
| **Person stops → agent starts speaking** | **≤ 1.7 s** |
| Interruption → silence | ≤ 150 ms |

Each turn's timings are stored in `turns.latency_ms` and shown on the dashboard.

---

## 7. The action loop (OpenClaw)

### 7.1 Split of responsibilities

| Steady (fast, deterministic) | OpenClaw (slow, careful) |
|---|---|
| Signals, detection, numbers, charts, report **content** | Composing and **sending** messages and emails |
| Real-time conversation | Planning multi-step tasks |
| Deciding *that* something must happen (a task) | Deciding *how* (channel, wording, follow-ups) |
| Approval rules (tiers) | Scheduled jobs (daily brief, reminders) |

OpenClaw never computes symptom metrics itself. It asks Steady for them, so numbers in
messages always match the database.

### 7.2 Integration

**Steady → OpenClaw:** OpenClaw's gateway hooks, `POST /hooks/agent` on loopback, with a
dedicated hook token (`OPENCLAW_HOOK_TOKEN`). Body:

```json
{
  "message": "STEADY TASK #42 (caregiver_alert, urgent). Use the steady skill: run `steadyctl task get 42`, do what it says, then report with `steadyctl task done 42` or `steadyctl task fail 42`.",
  "name": "Steady"
}
```

The response (`{ ok, runId }`) is stored on the task. Only `message` (plus `name`) is relied on;
other hook fields vary between OpenClaw versions and are checked against the installed
version during setup.

**OpenClaw → Steady:** the **`steady` skill** (`openclaw/skills/steady/SKILL.md`) teaches the
agent to use **`steadyctl`**, a small CLI shipped with Steady that calls the local API with
`STEADY_API_TOKEN`:

| Command | Does |
|---|---|
| `steadyctl task get <id>` | Task type, details, recipients, instructions, approval state |
| `steadyctl task done <id> --note "..."` / `task fail <id> --note "..."` | Report the outcome |
| `steadyctl summary --period today\|week` | Symptom summary as text with numbers |
| `steadyctl report daily\|weekly --format md\|pdf` | Generated report file path (Steady writes it) |
| `steadyctl contacts` | Contact roles and addresses (from `config/contacts.yaml`) |
| `steadyctl speak "..."` | Speak a short message in the headphones (≤ 300 characters, at most 1 per minute; queued if a conversation is running) |
| `steadyctl led done` | White double flash |

**Models:** OpenClaw uses its `ollama-cloud` provider with the same `OLLAMA_API_KEY`, and a
**larger** model than the voice agent (`openclaw_model` in config). It's slower, which is fine
for these tasks.

**Channels:** one messaging channel connected in OpenClaw (Telegram is the quickest to set up
for the demo) and one email skill (chosen during setup).

### 7.3 Task types

| Type | Created by | What OpenClaw does |
|---|---|---|
| `caregiver_alert` | Fall escalation, danger words, voice request | Sends the caregiver a short message: what happened, when, the person's last words, and what Steady is doing. Follows up after 10 min if no reply (second contact). |
| `message_contact` | Voice ("Tell Priya I'm having a rough day") | Writes and sends the message to that contact |
| `doctor_brief` | Voice ("Send this week's report to Dr. Patel") | Gets `steadyctl report weekly --format pdf`, writes a short cover email, sends it |
| `daily_brief` | OpenClaw scheduled job, 20:00 | Gets `steadyctl summary --period today`, sends the wearer (and, if configured, the caregiver) a friendly summary; speaks a 2-sentence version |
| `weekly_report` | OpenClaw scheduled job, Sunday 18:00 | Weekly report to the wearer; doctor only if `auto_send_doctor: true` |
| `med_reminder` | OpenClaw scheduled job from `medications` in config | Speaks the reminder; opens a short conversation so the person can confirm ("Did you take it?"), which is logged |
| `general_request` | Voice, anything else outside Steady ("Book a call with my physio next week") | Plans and carries it out within the approval tier |

### 7.4 Approval tiers

| Tier | Rule | Examples |
|---|---|---|
| **0: automatic** | Only to the wearer themselves, or internal | Daily brief to the wearer, speaking a reminder |
| **1: confirm first** | Anything sent to another person, or any booking | Message to a contact, doctor brief, appointments. The voice agent asks; a spoken "yes" or a **single tap** confirms. The task stays `needs_confirmation` until then (expires after 10 min). |
| **2: safety-automatic** | Sent **without** confirmation, because the person may be unable to confirm | `caregiver_alert` after a fall with no response, or danger words |

The tier is decided by Steady (in `actions/approval.py`) from the task type and origin, and is
part of `steadyctl task get`. The skill tells OpenClaw to **refuse** a task whose tier is 1 and
whose state is not `confirmed`.

### 7.5 Task lifecycle

```
created ─▶ (tier 1) needs_confirmation ─▶ confirmed ─▶ sent (hook OK, runId) ─▶ running ─▶ done
   │                     │ cancel/expire                    │ hook error              └▶ failed
   └─(tier 0/2)──────────┴──────────────────────────────────┘ retry ×3 (5 s, 20 s, 60 s) ─▶ failed
```

- **Timeouts:** urgent tasks must reach `done` in 3 min, normal ones in 30 min; otherwise
  `failed`.
- **When an urgent task fails** (OpenClaw down, no channel): Steady says so through the
  headphones ("I couldn't reach Priya. If you need help, please call someone now."), shows a
  macOS notification, and keeps retrying.
- **Idempotency:** the task id is in every message; `task done` on a finished task changes
  nothing.

### 7.6 Security

- Steady's API binds to `127.0.0.1` only, and every request needs
  `Authorization: Bearer $STEADY_API_TOKEN`.
- OpenClaw hooks: loopback only, dedicated token, `allowRequestSessionKey` off.
- What OpenClaw can do to Steady is limited to the `steadyctl` commands above. It cannot
  change the band's settings, read raw audio, or change thresholds.
- Replies from contacts that reach OpenClaw are **untrusted text**. The skill tells the agent
  to never act on instructions inside them, and to only relay them to the wearer
  (`steadyctl speak`).
- `/api/speak` is length- and rate-limited, so a runaway agent can't flood the headphones.

---

## 8. Data model (SQLite, `data/steady.db`, WAL)

```sql
CREATE TABLE band_sessions (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
  band_no INTEGER NOT NULL, firmware TEXT, transport TEXT, end_reason TEXT
);
CREATE TABLE connectivity_gaps (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, reason TEXT
);
CREATE TABLE windows (                         -- one row every 2 s
  id INTEGER PRIMARY KEY, session_id INTEGER, t_start REAL NOT NULL, t_end REAL NOT NULL,
  valid INTEGER NOT NULL, valid_frac REAL, context TEXT,          -- not_worn|walking|rest|active
  activity_state TEXT, cadence REAL, steps_delta INTEGER,
  acc_dyn_rms REAL, gyro_rms REAL, vol_power REAL, p_total REAL,
  dom_freq REAL, peak_prom REAL, rest_ratio REAL, action_ratio REAL, tremor_amp REAL,
  dysk_power REAL, dysk_flat REAL, freeze_index REAL, vigor REAL,
  arm_swing_deg REAL, step_reg REAL, pitch REAL, roll REAL,
  flags INTEGER NOT NULL DEFAULT 0,             -- bit 0 rest tremor, 1 action tremor, 2 dyskinesia, 3 freeze cand., 4 fall cand.
  battery INTEGER, rssi INTEGER
);
CREATE INDEX windows_t ON windows (t_start);
CREATE TABLE band_events (
  id INTEGER PRIMARY KEY, session_id INTEGER, t REAL NOT NULL, kind TEXT NOT NULL, arg INTEGER
);
CREATE TABLE episodes (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL,   -- rest_tremor|action_tremor|dyskinesia|freeze|fall_suspected|hard_impact|slow_period
  status TEXT NOT NULL,                         -- open|closed
  started_at REAL NOT NULL, ended_at REAL, duration_s REAL,
  severity TEXT NOT NULL,                       -- info|notice|concern|urgent
  metrics_json TEXT,                            -- peak/mean freq, amplitude, etc.
  clip_path TEXT, conversation_id INTEGER
);
CREATE INDEX episodes_t ON episodes (started_at);
CREATE TABLE hourly_rollups (
  hour_start REAL PRIMARY KEY, worn_s REAL, rest_s REAL, active_s REAL, walking_s REAL,
  rest_tremor_s REAL, action_tremor_s REAL, tremor_pct_rest REAL, tremor_amp_mean REAL,
  dysk_s REAL, vigor_median REAL, steps INTEGER, arm_swing_median REAL, step_reg_median REAL,
  freezes INTEGER, falls INTEGER, valid_frac REAL
);
CREATE TABLE baselines (
  id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  kind TEXT NOT NULL,                           -- noise_floor|rest|vigor|gait|freeze_index
  value_json TEXT NOT NULL, source TEXT         -- calibration|rolling
);
CREATE TABLE conversations (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
  trigger TEXT NOT NULL,                        -- episode|double_tap|reminder|openclaw
  episode_id INTEGER, outcome TEXT,             -- ok|follow_up|needs_help|no_response|preempted
  summary TEXT, escalated INTEGER DEFAULT 0
);
CREATE TABLE turns (
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, t REAL NOT NULL,
  role TEXT NOT NULL,                           -- user|assistant|tool|system_event
  text TEXT, tool_name TEXT, tool_json TEXT, latency_ms INTEGER
);
CREATE TABLE logs (
  id INTEGER PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL,   -- medication|symptom|note
  data_json TEXT NOT NULL, source TEXT, conversation_id INTEGER
);
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  type TEXT NOT NULL, tier INTEGER NOT NULL, urgency TEXT NOT NULL,
  status TEXT NOT NULL,     -- created|needs_confirmation|confirmed|sent|running|done|failed|cancelled|expired
  payload_json TEXT NOT NULL, openclaw_run_id TEXT, result_note TEXT, attempts INTEGER DEFAULT 0,
  conversation_id INTEGER, episode_id INTEGER
);
```

Personal details (contact names and addresses) live only in `config/contacts.yaml` (not in git)
and on the Mac. Nothing personal is ever written to the band's storage.

---

## 9. Local API (`127.0.0.1:8765`, bearer token)

| Method & path | Purpose | Used by |
|---|---|---|
| `GET /api/status` | Band connection, battery, context, open episodes, conversation state | Dashboard, steadyctl |
| `GET /api/windows?since=` | Recent windows | Dashboard |
| `GET /api/episodes?since=&kind=` | Episodes | Dashboard, steadyctl |
| `GET /api/summary?period=` | Text + numbers summary | steadyctl, voice agent |
| `GET /api/reports/{daily\|weekly}?format=md\|pdf` | Generate a report, return its path | steadyctl |
| `GET /api/tasks/{id}` / `POST /api/tasks/{id}/status` | Task details / outcome | steadyctl |
| `GET /api/contacts` | Contact roles | steadyctl |
| `POST /api/speak` | Queue a short spoken message | steadyctl |
| `POST /api/led` | `done` flash | steadyctl |
| `POST /api/conversation/start` | Start a conversation (for testing and demo) | Dashboard |
| `WS /ws/live` | Live features, detections, transcript, latency (no raw audio) | Dashboard |

**Dashboard** (`src/web/`, served at `/`): live motion trace and frequency spectrum with the
tremor band highlighted, context, episode timeline, live transcript of the current
conversation, task list with states, today's rollup charts, and the band/battery/connection
status. It reads only from the API and never connects to the band.

---

## 10. Configuration and secrets

**`.env`** (already present; never committed, never printed):

| Variable | Used by |
|---|---|
| `DEEPGRAM_API_KEY` | STT and TTS |
| `OLLAMA_API_KEY` | Voice agent; also OpenClaw's `ollama-cloud` provider |
| `OPENCLAW_HOOK_TOKEN` | Steady → OpenClaw hooks (to add) |
| `STEADY_API_TOKEN` | steadyctl → Steady API (generated by `python -m steady init`, written to `.env` by the user) |

**`config/steady.yaml`** (committed, no secrets):

```yaml
band: { number: 3 }
mode: normal                   # normal | demo
sampling: { mode: full }       # full (208 Hz) | eco (100 Hz)
audio: { output_device: "AirPods", fallback_to_speakers_for_urgent: true }
models:
  voice_model: "<fast tool-capable Ollama Cloud model>"     # checked against /api/tags at startup
  openclaw_model: "<larger Ollama Cloud model>"
deepgram: { stt_model: nova-3, tts_voice: aura-2-thalia-en, endpointing_ms: 300, utterance_end_ms: 1000 }
policy:
  quiet_hours: ["22:00", "07:00"]
  cooldown_min: { default: 30, rest_tremor: 45 }
  max_checkins_per_hour: 3
thresholds: { ... all values from §4.6, overridden by calibration ... }
medications:                   # optional; drives reminders and wearing-off detection
  - { name: "carbidopa-levodopa", times: ["08:00", "14:00", "20:00"] }
openclaw: { hooks_url: "http://127.0.0.1:18789/hooks/agent", daily_brief: "20:00", weekly_report: "Sun 18:00" }
```

**`config/contacts.yaml`** (not in git; `contacts.example.yaml` is committed):
roles (`caregiver`, `backup_caregiver`, `doctor`), with name, channel and address.

**`.gitignore`:** `.env`, `vexo-open-kit/`, `data/`, `config/contacts.yaml`, `__pycache__/`.

---

## 11. Code layout

```
vexo/
  SPEC.md
  requirements.txt            bleak, numpy, scipy, sounddevice, httpx, websockets, fastapi, uvicorn, pyyaml, matplotlib
  .env  .gitignore
  vexo-open-kit/              unchanged (library, speech helper)
  config/
    steady.yaml  contacts.example.yaml
  src/
    steady/
      __main__.py             CLI: run | calibrate | init | sim <scenario> | report | doctor (checks keys, models, devices)
      kitpath.py              adds vexo-open-kit/python and examples/python to sys.path
      config.py               loads YAML + .env, validates
      bus.py                  typed events (EpisodeStarted, BandTap, TaskUpdated, ...), asyncio fan-out
      app.py                  wires everything, startup/shutdown order
      band/
        manager.py            connect, configure, supervisor, reconnect re-apply
        ingest.py             ring buffer (numpy), gap counting
        led.py                LED intents, mic lock awareness
      screening/
        preprocess.py  features.py  context.py
        detectors/  tremor.py  dyskinesia.py  slowness.py  gait.py  freeze.py  fall.py  nonwear.py
        episodes.py           hysteresis tracker
        screener.py           2 s scheduler, runs in executor
        rollups.py  calibration.py
      policy/
        engine.py  rules.py   severity, cooldowns, quiet hours, pre-emption
      voice/
        stt.py  tts.py  player.py  sentences.py
        conversation.py       state machine (§6.4)
      agent/
        ollama.py             /api/chat streaming client with tool calls
        voice_agent.py  tools.py  prompts.py  situation.py
      actions/
        tasks.py              lifecycle, retries, timeouts
        approval.py           tiers
        openclaw.py           hook client
      store/
        schema.sql  db.py  writer.py  queries.py
      api/
        server.py  routes.py  auth.py  live.py
      reports/
        summary.py  daily.py  weekly.py  charts.py
      sim/
        scenarios.py          scripted pretend-band days (see §12)
      ctl.py                  steadyctl
    web/
      index.html  app.js  style.css
  openclaw/
    skills/steady/SKILL.md
    config.example.json5      hooks + ollama-cloud provider (no secrets)
    SETUP.md
  tests/
    signals.py                synthetic signal generators
    test_detectors.py  test_episodes.py  test_policy.py  test_conversation.py  test_tasks.py  test_api.py
  data/                       (gitignored) steady.db, clips/, reports/
```

Run with `PYTHONPATH=src python -m steady run` from `vexo/`.

---

## 12. Simulation and testing

**Pretend band.** `python -m steady sim <scenario>` runs the whole system with
`SimTransport(3, auto_tap=False)`, driving it with `play_motion`, `tap`, `set_activity`,
`drop_link` and `set_battery`:

| Scenario | Plays | Expected |
|---|---|---|
| `calm_day` | Normal motion, typing, walking, the sim's own swing | No episodes. **False-positive test.** |
| `rest_tremor` | Rest, then 5 Hz gyro oscillation at 20 °/s for 90 s | Rest tremor episode, moderate, conversation opens |
| `action_tremor` | Activity with 8 Hz component | Action tremor, `fast` |
| `fall` | Free-fall event, 4 g spike, then stillness | `fall_suspected`, urgent conversation; silence → caregiver task |
| `freeze` | Walking, then cadence collapses with 5 Hz trembling | Freeze episode, walking cue |
| `link_loss` | `drop_link(30)` | Gap logged, settings re-applied, no false episodes |
| `brushing_teeth` | Vigorous 3–5 Hz active movement | **No** rest tremor (context is `active`) |

**Voice without a microphone:** `--say "I'm fine, thanks"` (typed lines replace STT), and
`--transcript file.txt` for scripted multi-turn tests. The pretend band can also play the kit's
sample speech for an STT smoke test.

**Unit tests (no network):** detectors on synthetic signals (frequency sweeps, noise,
gravity changes) with **both detection and rejection cases**; episode hysteresis; policy
cooldowns and quiet hours; conversation state machine with fake STT/LLM/TTS; task tiers and
lifecycle with a mock OpenClaw hook server.

**`python -m steady doctor`:** checks both keys work (status code only, never the key),
the configured Ollama models exist, the headphones are found, OpenClaw hooks answer, and the
band can be found.

---

## 13. Implementation plan

Each milestone ends with something that runs. The critical path is **M0 → M1 → M2 → M4 → M5**.
M3 and M6 can be built in parallel once M0 is done.

| # | Milestone | Deliverables | Done when |
|---|---|---|---|
| **M0** | Skeleton | Layout, `config.py`, `.gitignore`, `schema.sql` + writer, event bus, CLI, `doctor` (keys + models) | `python -m steady doctor` passes; DB is created |
| **M1** | Band + logging | `band/manager.py`, ingest ring buffer, screener with features + context, window logging, rollups, LED manager | On the pretend band and on band 03: a `windows` row every 2 s, reconnect works, gaps logged |
| **M2** | Detectors | Rest/action tremor, fall, non-wear, episode tracker; tests + `calm_day`/`rest_tremor`/`fall`/`brushing_teeth` scenarios | All scenario expectations pass; zero episodes on `calm_day` |
| **M3** | Voice output | TTS client, player, sentence splitter, `steady say "..."` | Text is spoken in the headphones; first audio < 300 ms after send |
| **M4** | Voice conversation | STT subclass, Ollama client with tools, voice agent, conversation state machine, interruptions, silence handling | Double-tap → full conversation with band 03; `end_conversation` only when OK; latency ≤ 1.7 s |
| **M5** | Policy | Severity, rules, cooldowns, quiet hours, pre-emption; detections open conversations | `rest_tremor` and `fall` scenarios open the right conversation automatically |
| **M6** | OpenClaw | Local API + auth, `steadyctl`, `steady` skill, hook client, task lifecycle, approval tiers; OpenClaw set up with ollama-cloud + Telegram | Voice "tell Priya..." → confirm by tap → message arrives; fall + silence → automatic alert |
| **M7** | Reports + dashboard | Summary, daily/weekly reports with charts, dashboard live view, OpenClaw scheduled jobs | Daily brief arrives on Telegram with correct numbers |
| **M8** | Real band hardening | Calibration flow, threshold tuning on band 03, battery measurement over 2 h, demo mode, seeded history | Demo script (§14) runs 3 times in a row without a failure |
| Stretch | | Dyskinesia, slowness, freezing, wearing-off, gait trends | Each behind a config flag, labelled experimental |

---

## 14. Demo script (about 3 minutes)

1. **Screening.** Band on the judge's wrist, dashboard on screen: windows logging every 2 s,
   context "rest", flat spectrum. The band breathes green.
2. **Tremor.** The judge rests their arm and fakes a slow shake. The spectrum peak jumps at
   ~5 Hz, an episode opens, the band flashes amber, then turns blue. In the headphones:
   *"Hi, it's Steady. I noticed your hand has been shaking for a bit. How are you feeling?"*
   A short real conversation; the agent logs it and ends when they say they're OK.
3. **Delegation.** The judge double-taps: *"Tell my daughter I'm having a shaky afternoon."*
   Agent: *"Want me to send that to Priya? Tap once to confirm."* Tap. A phone on the table
   buzzes with the Telegram message. Headphones: *"Sent."*
4. **Fall.** The judge drops their arm onto a cushion and stays silent. Headphones: *"Are you
   OK?"*... *"Can you hear me?"*... The phone buzzes: *"Possible fall detected at 3:42 PM, no
   response to voice check. Steady is still listening."* Headphones: *"I've let Priya know."*
5. **Report.** On the dashboard, the episode timeline and a generated daily brief with the
   numbers that were just measured.

Backup: the same script on the pretend band (`python -m steady sim demo`), if Bluetooth or
the venue Wi-Fi fails.

---

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| False alarms (normal movement looks like tremor) | Context gating; four-part tremor rule; hysteresis; `calm_day` and `brushing_teeth` scenarios as tests; cooldowns |
| Thresholds wrong on the real band | Calibration flow (§4.9); all thresholds in config; tune in M8 |
| Bluetooth drops / out of range | Auto-reconnect + supervisor; gaps logged; dashboard prompt to double-tap |
| Mac sleeps | `caffeinate`; lid must stay open (stated in setup) |
| Audio + 208 Hz motion overload the link | Watch `stream-dropped`; fall back to 100 Hz during conversations |
| Battery life on continuous streaming | Measure in M8; eco mode; battery shown on the dashboard |
| Venue network slow or down | Latency shown per turn; pretend-band backup demo; urgent path still logs and notifies locally |
| LLM says something unsafe | Prompt rules; tools-only actions; approval tiers; `end_conversation` check |
| OpenClaw down | Urgent tasks fail loudly (voice + macOS notification); retries |
| Prompt injection via replies | Replies only relayed by voice, never acted on; OpenClaw's power over Steady limited to `steadyctl` |
| Privacy | Mic only in conversations; blue light; spoken notice; `mip_opt_out=true`; raw audio never stored; data stays on the Mac |

---

## 16. Open questions

1. How long is the hackathon? This sets how much of M7/M8 and the stretch detectors fit.
2. Which messaging channel for the caregiver demo (Telegram suggested), and which email setup?
3. Is OpenClaw already installed? Which version (hook fields vary between versions)?
4. Which headphones (device name for `audio.output_device`)?
5. Which Ollama Cloud models does your key have? `doctor` lists them; we pick the fastest
   tool-capable one for voice.
