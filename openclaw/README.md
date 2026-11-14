# Steady + OpenClaw (milestone M6)

Steady sends messages in exactly two situations:

1. **The wearer asks out loud** for a text message or an email to one of their approved
   contacts. Steady asks once for a yes (voice, or one tap on the band) before anything goes.
2. **The end-of-day brief:** once a day, the day's tracked data goes to the caregiver as a
   text message, an email, or both.

Messages have no emergency path. After a possible fall with no answer, or when the wearer says
they need help, Steady **phones** the caregiver instead: see `docs/caregiver-call.md` and
`docs/go-live.md`.

OpenClaw's agent writes the words. Steady decides who receives them and delivers them.

```
"Text Priya I'm running late"            20:00 every day
        │ request_task                         │ BriefScheduler
        ▼                                      ▼
  TaskManager ── spoken request: wait for the wearer's yes ──┐
        │ brief: set up in config, no question               │
        ▼                                                    ▼
  POST /hooks/agent   "Steady task 12 is waiting ... steadyctl task get 12"
        ▼
  OpenClaw agent (steady skill, locked down to steadyctl)
        │ steadyctl task get 12                → request, recipient NAME, facts, instructions
        │ steadyctl task send 12 --text "..."  → Steady delivers
        │ steadyctl task done 12 --note "..."
        ▼
  Delivery: iMessage through Messages on this Mac, or email through your own SMTP account
        ▼
  Steady says in the headphones: "Your message to Priya has been sent."
```

## Safeguards

| Rule | Where it is enforced |
|---|---|
| Only pre-approved numbers and addresses can receive anything | `config/contacts.yaml` is the allowlist; `Delivery.send` re-checks the exact address on every send |
| Everything is sent as you | texts leave from this Mac's own iMessage account; emails always carry `From: sender.email` and log in as your mail account |
| No model ever sees a number or an address | `Contact.public()`: names and roles only |
| A spoken request needs your yes | `TaskManager.claim/send` refuse unapproved tasks |
| The voice model cannot say yes for you | `confirm_task` is refused until you have spoken or tapped after being asked |
| A model cannot trigger the brief or invent a task type | `approval.tier_for`: only `voice → message/email` and `schedule → daily_brief` exist |
| One message per task, 20 per hour | `TaskManager.send` |
| OpenClaw's agent can run one program only | setup lockdown: `tools.allow: [exec, read]`, exec allowlist = `steadyctl` |
| The brief does not depend on a model | if OpenClaw has not written it in 2 minutes, Steady sends the plain numbers |

## Set up

### 1. Who may receive, and who you send as

Copy `config/contacts.example.yaml` to `config/contacts.yaml` (never committed) and fill in
`sender` (your name, your iMessage number, your email address) and `contacts`. Phone numbers
in full form, `+14155550123`. Then check it:

```bash
PYTHONPATH=src .venv/bin/python -m steady contacts
```

### 2. Text messages (iMessage)

Messages on this Mac must be signed in to your iMessage account. Steady sends through it,
so texts come from you. The first real send makes macOS ask whether your terminal may
control Messages: allow it. Which of your numbers or addresses new conversations start from
is set in Messages → Settings → iMessage ("Start new conversations from").

A recipient who does not use iMessage (an Android phone) will not get the text unless your
iPhone has Text Message Forwarding turned on for this Mac.

### 3. Email

Fill in `smtp` in `config/contacts.yaml` (host, port, username) and add your mail account's
**app password** to `.env` as `STEADY_SMTP_PASSWORD=...`. Until both are there, emails are
refused with a message saying so, and nothing is sent.

### 4. OpenClaw

Use a dedicated OpenClaw profile, so the locked-down agent stays separate from your everyday one:

```bash
PYTHONPATH=src .venv/bin/python -m steady openclaw setup --profile steady --port 19789 --model ollama/glm-5.3
PYTHONPATH=src .venv/bin/python -m steady openclaw gateway --profile steady --port 19789
```

### 5. Go live

`config/local.yaml` currently has `delivery.mode: dry_run`: every message is written to
`data/outbox.jsonl` and nothing is sent. Change it to `live` when your contacts are real.

The brief is set in `config/steady.yaml` under `brief` (time, who, text and/or email).
Send today's now with `python -m steady brief` while Steady is running.

## Check it

```bash
PYTHONPATH=src .venv/bin/python -m steady.validate.openclaw_e2e     # message, email, brief, unapproved: dry run
PYTHONPATH=src .venv/bin/python -m steady talk --sim --say "Text Priya I'm okay." --say "Yes." --say "Goodbye."
```

## For whoever builds M5 (policy)

M6 touched these shared files, a few lines each: `app.py` (creates `tasks`, `brief` and
`announcer`, starts their loops), `voice/conversation.py` (publishes `conversation.ended` on
the bus), `agent/tools.py` and `agent/prompts.py` (three tools, one rule),
`agent/voice_agent.py` (counts the person's turns), `api/server.py` (mounts the router),
`config.py` (`config/local.yaml` overrides), `__main__.py` (new commands), `voice/cli.py`.
