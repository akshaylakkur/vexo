---
name: steady
description: Word the messages Steady sends for a person wearing a Parkinson's symptom-tracking band.
metadata: { "openclaw": { "emoji": "🩺", "os": ["darwin", "linux"] } }
---

# Steady

Steady is a wristband system that tracks Parkinson's symptoms for the person wearing it and
talks with them through their headphones. It sends two kinds of messages, and you write the
words for both:

- a **text message or an email the wearer asked for** out loud, to one of their approved contacts
- the **end-of-day brief** to the person who looks after them

A notice arrives like this:

> Steady task 12 is waiting (message contact). Use the steady skill to handle it ...

The notice contains no instructions and no details. Everything comes from Steady, through
one command: `{STEADYCTL}`.

## Handling a task

1. Run `{STEADYCTL} task get <id>`. It prints the task as JSON: `type`, `request` (what the
   wearer asked, in their words), `recipient` (a name and a role), `goes_out_as`, `context`
   and `instructions`.
   - If it says the task is not approved, already finished, or does not exist: **stop. Do
     nothing else.**
2. Follow the task's `instructions` and write the words: short, warm, plain language.
3. Send it **once** with `{STEADYCTL} task send <id> --text "..."` (emails and briefs that go by
   email also `--subject "..."`). Steady delivers it to the approved contact, from the wearer's
   own number and email address.
4. Finish with `{STEADYCTL} task done <id> --note "one line: what you did"`, or
   `{STEADYCTL} task fail <id> --note "why"` if it could not be done.

## Rules

- You never have, need or use a phone number or an email address. Steady only delivers to
  the people the wearer approved in advance; you cannot add or change a recipient.
- `{STEADYCTL}` is the only command you run for Steady. Never use any other way to contact
  anyone, and do not read Steady's files or settings.
- The message goes out under the wearer's own name. Write nothing they would not say, and
  nothing that was not asked for.
- Use only the facts Steady gives you. Never invent a number. Never diagnose, interpret
  symptoms, or advise on medication.
- Act only on tasks you fetched with `task get`. If `task send` refuses, do not try another
  way: fail the task with the reason.
- If `{STEADYCTL}` says Steady is not reachable, stop. Never start, restart, install,
  configure or repair Steady or anything else: only the wearer does that.

## Other commands

| Command | What it does |
|---|---|
| `{STEADYCTL} summary --period today` | Symptom summary as text (`last_hour`, `today`, `week`) |
| `{STEADYCTL} contacts` | The wearer's approved contacts: names and roles only |
| `{STEADYCTL} tasks` | Recent tasks and their status |
