import asyncio
import datetime as dt
import json
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from steady import config as config_mod
from steady.actions import delivery as delivery_mod
from steady.actions import tasks as tasks_mod
from steady.actions.approval import tier_for
from steady.actions.brief import BriefScheduler, due
from steady.actions.contacts import Contact, Contacts, Sender, Smtp, mask
from steady.actions.delivery import Delivery
from steady.actions.openclaw import OpenClawHook
from steady.actions.tasks import TaskError, TaskManager
from steady.bus import Bus
from steady.store.db import Writer

PRIYA = Contact("caregiver", "Priya", phone="+14155550123", email="priya@example.com")
PATEL = Contact("doctor", "Dr. Patel", email="patel@clinic.example")
BOOK = Contacts([PRIYA, PATEL], Sender("Akshay", "+14155550100", "akshay@example.com"),
                Smtp("smtp.example.com", 587, "akshay@example.com"))


class FakeGateway:
    """Stands in for OpenClaw's POST /hooks/agent."""

    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((str(req.url), req.headers.get("authorization"), json.loads(req.content)))
        if self.fail > 0:
            self.fail -= 1
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True, "runId": f"run-{len(self.calls)}"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "hook-secret")
    monkeypatch.setenv("STEADY_API_TOKEN", "api-secret")
    monkeypatch.setattr(delivery_mod, "OUTBOX", tmp_path / "outbox.jsonl")
    monkeypatch.setattr(tasks_mod, "RETRY_S", (0.01, 0.01, 0.01))
    cfg = config_mod.load(overrides={"storage.db_path": str(tmp_path / "a.db"), "delivery.mode": "dry_run",
                                     "brief.fallback_s": 0.3, "openclaw.steadyctl": "/x/steadyctl"})
    return cfg, tmp_path


async def make(cfg, gw, book=BOOK):
    writer = Writer(cfg.db_path, interval=0.02)
    await writer.start()
    bus = Bus()
    hook = OpenClawHook(cfg, transport=httpx.MockTransport(gw.handler))
    return TaskManager(cfg, bus, writer, hook=hook, contacts=book), bus, writer


async def settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0.02)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def outbox(tmp):
    f = tmp / "outbox.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


# ---- the two paths, and nothing else --------------------------------------------------------

def test_only_two_paths_exist():
    assert tier_for("message_contact", "voice") == 1 and tier_for("email_contact", "voice") == 1
    assert tier_for("daily_brief", "schedule") == 0
    for task_type, origin in [("daily_brief", "voice"), ("message_contact", "schedule"),
                              ("caregiver_alert", "voice"), ("general_request", "voice")]:
        with pytest.raises(ValueError):
            tier_for(task_type, origin)


def test_there_is_no_emergency_path(env):
    cfg, _ = env

    async def go():
        tm, _, writer = await make(cfg, FakeGateway())
        with pytest.raises(TaskError, match="Unknown task type"):
            await tm.create("caregiver_alert", "help", to="caregiver")
        with pytest.raises(TaskError):
            await tm.create("daily_brief", "now", to="caregiver", origin="voice")     # a model cannot trigger the brief
        assert not hasattr(tm, "watch_escalations")
        await writer.close()

    run(go())
    from steady.agent.tools import SPECS
    spec = next(s for s in SPECS if s["function"]["name"] == "request_task")["function"]["parameters"]["properties"]
    assert spec["type"]["enum"] == ["message_contact", "email_contact"] and "urgency" not in spec


# ---- the allowlist --------------------------------------------------------------------------

def test_contacts_file_is_validated(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("""
sender: {name: Akshay, phone: "+1 (415) 555-0100", email: akshay@example.com}
contacts:
  caregiver: {name: Priya, phone: "+14155550123", email: priya@example.com}
  doctor: {name: Dr. Patel, email: patel@clinic.example}
  bad_phone: {name: Sam, phone: "555-0100"}
  injected: {name: Eve, email: "eve@example.com\\nBcc: x@evil.example"}
""")
    book = Contacts.load(f)
    assert [c.role for c in book.items] == ["caregiver", "doctor"]            # the two bad entries are dropped
    assert book.sender.phone == "+14155550100"
    assert len(book.problems) == 4
    assert book.allows("imessage", "+14155550123") and book.allows("email", "patel@clinic.example")
    assert not book.allows("imessage", "+14155559999") and not book.allows("email", "priya@example.co")
    assert not book.allows("email", "+14155550123")                           # right address, wrong channel
    assert book.find("Priya") is book.find("caregiver") and book.find("patel").role == "doctor"
    assert book.find("Bob") is None
    public = json.dumps(book.public())
    assert "+1415" not in public and "@" not in public                        # models never see an address
    assert mask("+14155550123") == "+1***23" and mask("priya@example.com") == "p***@example.com"
    assert Contacts.load(tmp_path / "missing.yaml").items == []


def test_delivery_refuses_an_address_that_is_not_in_the_file(env):
    cfg, tmp = env
    stranger = Contact("caregiver", "Priya", phone="+14155559999")            # same name, another number
    res = asyncio.run(Delivery(cfg, BOOK).send(stranger, "imessage", "hi"))
    assert not res.ok and "no approved imessage address" in res.detail and outbox(tmp) == []
    assert not asyncio.run(Delivery(cfg, BOOK).send(PATEL, "imessage", "hi")).ok      # the doctor has no phone


def test_imessage_goes_through_messages_as_arguments_not_a_shell(env, monkeypatch):
    cfg, _ = env
    cfg.raw["delivery"]["mode"] = "live"
    calls = []

    async def fake_exec(*argv, **kw):
        calls.append(argv)

        class P:
            returncode = 0

            async def communicate(self, data=None):
                calls.append(data)
                return b"", None
        return P()

    monkeypatch.setattr(delivery_mod.sys, "platform", "darwin")
    monkeypatch.setattr(delivery_mod.asyncio, "create_subprocess_exec", fake_exec)
    text = 'Running late"; do shell script "rm -rf ~" --'
    res = asyncio.run(Delivery(cfg, BOOK).send(PRIYA, "imessage", text))
    assert res.ok and calls[0] == ("/usr/bin/osascript", "-", "+14155550123", text)
    assert b"item 2 of argv" in calls[1] and text.encode() not in calls[1]     # the text is data, never script
    no_sender = Contacts([PRIYA], Sender("Akshay"))
    res = asyncio.run(Delivery(cfg, no_sender).send(PRIYA, "imessage", "hi"))
    assert not res.ok and "sender.phone" in res.detail


def test_email_is_always_from_the_configured_sender(env, monkeypatch):
    cfg, _ = env
    cfg.raw["delivery"]["mode"] = "live"
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["server"] = (host, port)

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent.update(msg=msg, from_addr=from_addr, to=to_addrs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(delivery_mod.smtplib, "SMTP", FakeSMTP)
    d = Delivery(cfg, BOOK)
    res = asyncio.run(d.send(PATEL, "email", "Weekly update.", subject="Tremor update\nBcc: x@evil.example"))
    assert not res.ok and "not set up yet" in res.detail                       # no password yet: nothing is sent
    monkeypatch.setenv("STEADY_SMTP_PASSWORD", "app-pw")
    res = asyncio.run(d.send(PATEL, "email", "Weekly update.", subject="Tremor update\nBcc: x@evil.example"))
    assert res.ok and sent["server"] == ("smtp.example.com", 587) and sent["tls"]
    assert sent["login"] == ("akshay@example.com", "app-pw")
    assert sent["from_addr"] == "akshay@example.com" and sent["to"] == ["patel@clinic.example"]
    assert sent["msg"]["From"] == "Akshay <akshay@example.com>" and "Bcc" not in sent["msg"]
    assert sent["msg"]["Subject"] == "Tremor update Bcc: x@evil.example"       # header injection flattened


# ---- path 1: the wearer asks ----------------------------------------------------------------

def test_message_needs_a_yes_then_goes_through_openclaw(env):
    cfg, tmp = env
    gw = FakeGateway()

    async def go():
        tm, bus, writer = await make(cfg, gw)
        events = bus.subscribe("task.updated")
        t = await tm.create("message_contact", "tell her I'm having a shaky afternoon", to="Priya")
        assert t["needs_confirmation"] and "Priya by text message" in t["confirm_prompt"]
        await settle()
        assert gw.calls == []                                       # nothing leaves before the yes
        with pytest.raises(TaskError, match="not been approved"):
            await tm.claim(t["id"])
        with pytest.raises(TaskError):
            await tm.send(t["id"], "hi")
        await tm.confirm(t["id"], True)
        await settle()
        url, auth, body = gw.calls[0]
        assert url.endswith("/hooks/agent") and auth == "Bearer hook-secret"
        assert f"task get {t['id']}" in body["message"]
        assert "shaky" not in body["message"] and "+1415" not in json.dumps(body)   # only a task number travels
        got = await tm.claim(t["id"])
        assert got["recipient"] == {"name": "Priya", "role": "caregiver"} and got["goes_out_as"] == ["a text message"]
        assert got["wearer"] == "Akshay" and "+1415" not in json.dumps(got) and "@" not in json.dumps(got)
        with pytest.raises(TaskError, match="Nothing was delivered"):
            await tm.finish(t["id"], True)
        sent = await tm.send(t["id"], "Hi Priya, I'm having a shaky afternoon.")
        assert sent == {"ok": True, "to": "Priya", "sent_as": ["imessage"], "not_sent": {}}
        with pytest.raises(TaskError, match="already sent"):
            await tm.send(t["id"], "and another thing")             # one message per task
        assert (await tm.finish(t["id"], True, "sent"))["status"] == "done"
        assert (await tm.finish(t["id"], False))["status"] == "done"            # idempotent
        statuses = []
        while not events.empty():
            statuses.append(events.get_nowait()[1]["status"])
        await writer.close()
        return statuses

    statuses = run(go())
    assert statuses == ["needs_confirmation", "confirmed", "sent", "running", "running", "done"]
    assert [(o["channel"], o["to"]) for o in outbox(tmp)] == [("imessage", "+14155550123")]
    db = sqlite3.connect(cfg.db_path)
    assert db.execute("SELECT type, tier, status FROM tasks").fetchall() == [("message_contact", 1, "done")]
    assert db.execute("SELECT ok, channel, by FROM deliveries").fetchall() == [(1, "imessage", "openclaw")]


def test_a_no_cancels_and_an_unknown_person_is_refused(env):
    cfg, tmp = env
    gw = FakeGateway()

    async def go():
        tm, _, writer = await make(cfg, gw)
        t = await tm.create("email_contact", "send my weekly report", to="doctor")
        out = await tm.confirm(t["id"], False)
        with pytest.raises(TaskError, match="Approved contacts: Priya \\(caregiver\\), Dr. Patel \\(doctor\\)"):
            await tm.create("message_contact", "hi", to="Bob")
        with pytest.raises(TaskError, match="no approved phone number"):
            await tm.create("message_contact", "hi", to="doctor")
        await settle()
        await writer.close()
        return out

    assert run(go())["status"] == "cancelled" and gw.calls == [] and outbox(tmp) == []


def test_voice_tools_request_and_confirm_only_after_the_person_answers(env):
    cfg, _ = env
    from steady.agent.tools import ConversationState, Toolbox

    async def go():
        tm, _, writer = await make(cfg, FakeGateway())
        state = ConversationState(trigger="double_tap", id=3)
        tools = Toolbox(SimpleNamespace(tasks=tm, cfg=cfg, writer=writer), state)
        listed = await tools.call("list_contacts", {})
        alert = await tools.call("request_task", {"type": "caregiver_alert", "to": "Priya", "request": "help", "urgency": "urgent"})
        asked = await tools.call("request_task", {"type": "message_contact", "to": "Priya", "request": "running late"})
        bad = await tools.call("request_task", {"type": "message_contact", "to": "Bob", "request": "hi"})
        early = await tools.call("confirm_task", {"task_id": asked["task_id"], "confirmed": True})
        state.user_turns += 1                                                    # ... the person answers
        yes = await tools.call("confirm_task", {"task_id": asked["task_id"], "confirmed": True})
        await writer.close()
        return listed, alert, asked, bad, early, yes

    listed, alert, asked, bad, early, yes = run(go())
    assert listed["contacts"][0] == {"role": "caregiver", "name": "Priya", "can_receive": ["imessage", "email"]}
    assert "error" in alert                                                      # no emergency tool
    assert asked["needs_confirmation"] and asked["recipient"] == "Priya"
    assert "error" in bad and "Bob" in bad["error"]
    assert "error" in early and "not answered yet" in early["error"]            # the model cannot say yes for the person
    assert yes["status"] == "confirmed"


# ---- path 2: the end-of-day brief -----------------------------------------------------------

def test_brief_is_due_once_a_day_after_its_time():
    day = dt.datetime(2026, 11, 14)
    assert not due(day.replace(hour=19, minute=59), "20:00", None)
    assert due(day.replace(hour=20, minute=0), "20:00", None)
    assert due(day.replace(hour=23, minute=30), "20:00", "2026-11-13")          # missed at 20:00: still goes today
    assert not due(day.replace(hour=21), "20:00", "2026-11-14")                 # already sent today


def brief_steady(cfg, writer, tm):
    return SimpleNamespace(cfg=cfg, writer=writer, tasks=tm, screener=None)


def test_brief_goes_to_the_configured_contact_without_asking(env):
    cfg, tmp = env
    cfg.raw["brief"]["via"] = ["imessage", "email"]
    gw = FakeGateway()

    async def go():
        tm, _, writer = await make(cfg, gw)
        hour = float(int(time.time() // 3600) * 3600)
        writer.insert("hourly_rollups", {"hour_start": hour, "worn_s": 3600, "rest_s": 1800, "rest_tremor_s": 240, "steps": 900, "falls": 0})
        writer.insert("episodes", {"kind": "rest_tremor", "status": "closed", "started_at": time.time() - 600, "duration_s": 120, "severity": "concern"})
        writer.insert("logs", {"t": time.time() - 300, "kind": "medication", "data_json": json.dumps({"details": "levodopa at 8"}), "source": "voice"})
        b = BriefScheduler(brief_steady(cfg, writer, tm), tm)
        assert await b.last_sent() is None
        out = await b.send_now()
        await settle()
        t = tm.tasks[out["task_id"]]
        got = await tm.claim(t.id)
        await tm.send(t.id, "Steady brief for Akshay: tremor for 240 seconds today.", subject="Steady brief for Akshay")
        await tm.finish(t.id, True, "sent the brief")
        last = await b.last_sent()
        await writer.close()
        return out, t, got, last

    out, t, got, last = run(go())
    assert out["sent"] and out["to"] == "Priya" and out["via"] == ["imessage", "email"]
    assert t.tier == 0 and t.origin == "schedule" and len(gw.calls) == 1        # no yes needed: it was set up in config
    ctx = got["context"]
    assert "240 seconds" in ctx["summary_text"] and "13.3%" in ctx["summary_text"]
    assert ctx["episodes"][0]["what"] == "rest tremor" and ctx["logged_today"][0]["details"] == "levodopa at 8"
    assert got["goes_out_as"] == ["a text message", "an email"]
    assert [(o["channel"], o["to"]) for o in outbox(tmp)] == [("imessage", "+14155550123"), ("email", "priya@example.com")]
    assert last == dt.date.today().isoformat() and t.status == "done"


def test_brief_skips_an_empty_day_and_falls_back_to_plain_numbers(env):
    cfg, tmp = env
    gw = FakeGateway()

    async def go():
        tm, _, writer = await make(cfg, gw)
        b = BriefScheduler(brief_steady(cfg, writer, tm), tm)
        empty = await b.send_now()                                   # nothing tracked: no brief
        hour = float(int(time.time() // 3600) * 3600)
        writer.insert("hourly_rollups", {"hour_start": hour, "worn_s": 1200, "rest_s": 600, "rest_tremor_s": 60, "steps": 10, "falls": 0})
        watcher = asyncio.create_task(tm.watch())
        out = await b.send_now()
        await asyncio.sleep(1.6)                                     # OpenClaw never writes it ...
        watcher.cancel()
        t = tm.tasks[out["task_id"]]
        await writer.close()
        return empty, t

    empty, t = run(go())
    assert empty == {"sent": False, "reason": "nothing was tracked today"}
    assert t.status == "done" and t.deliveries[0]["by"] == "steady_plain"
    assert outbox(tmp)[0]["text"].startswith("Steady brief for Akshay. Today: monitored for 20.0 minutes")


def test_hook_is_retried_then_a_voice_task_fails(env):
    cfg, _ = env
    gw = FakeGateway(fail=10)

    async def go():
        tm, _, writer = await make(cfg, gw)
        t = await tm.create("message_contact", "hi", to="Priya")
        await tm.confirm(t["id"], True)
        await asyncio.sleep(0.4)
        await writer.close()
        return tm.tasks[t["id"]]

    t = run(go())
    assert len(gw.calls) == 4 and t.status == "failed" and "status 503" in t.note


# ---- local API ------------------------------------------------------------------------------

def test_api_needs_the_token_and_relays_refusals(env):
    cfg, _ = env
    from steady.api.actions import router

    loop = asyncio.new_event_loop()
    tm, _, writer = loop.run_until_complete(make(cfg, FakeGateway()))
    t = loop.run_until_complete(tm.create("message_contact", "hi", to="Priya"))
    app = FastAPI()
    app.include_router(router(SimpleNamespace(tasks=tm, cfg=cfg, writer=writer)))

    async def call(method, path, token="api-secret", **kw):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, path, headers={"Authorization": f"Bearer {token}"} if token else {}, **kw)

    r_none = loop.run_until_complete(call("GET", f"/api/tasks/{t['id']}", token=None))
    r_bad = loop.run_until_complete(call("GET", f"/api/tasks/{t['id']}", token="wrong"))
    r_unapproved = loop.run_until_complete(call("GET", f"/api/tasks/{t['id']}"))
    r_contacts = loop.run_until_complete(call("GET", "/api/contacts"))
    r_speak = loop.run_until_complete(call("POST", "/api/speak", json={"text": "hi"}))
    loop.run_until_complete(writer.close())
    loop.close()
    assert r_none.status_code == 401 and r_bad.status_code == 401
    assert r_unapproved.status_code == 409 and "not been approved" in r_unapproved.json()["detail"]
    assert r_contacts.status_code == 200 and "+1415" not in r_contacts.text and "@" not in r_contacts.text
    assert r_speak.status_code in (404, 405)                                    # no such endpoint any more
