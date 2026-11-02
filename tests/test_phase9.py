"""Phase 9: customers, their page, and their history for Claude."""
import asyncio

from fastapi.testclient import TestClient

from app import customers
from app.calendar_feed import tasks_ics
from app.jobtypes import JOB_TYPES
from app.main import create_app
from app.prompts import task_message
from app.review import approve


def run(app, job_id):
    asyncio.run(app.state.runner.run_job(job_id))
    return app.state.store.get(job_id)


def test_one_customer_however_the_name_is_typed(settings):
    app = create_app(settings, start_background=False)
    store, tasks = app.state.store, app.state.tasks
    with TestClient(app) as client:
        # knowledge/customers/ACME/profile.md exists (conftest); jobs typed "acme " and "ACME" are the same customer
        quote = client.post("/api/jobs", data={"type": "proposal-draft", "customer": "acme ",
                                               "instructions": "feeder 125A 2 ชุด"}).json()
        run(app, quote["id"])
        meeting = store.create("meeting-summary", "ประชุม ACME", "", {"customer": "ACME"})
        store.add_file(meeting["id"], "input", "transcript.txt", "สมชายจะส่งแบบภายในวันศุกร์".encode())
        run(app, meeting["id"])
        approve(store, tasks, settings, meeting["id"])
        assert all(i["customer"] == "ACME" for i in tasks.open_items())  # action items carry the customer
        client.post("/api/jobs", data={"type": "eng-qa", "instructions": "สาย 75 kW", "customer": "Beta Co"})
        client.post("/api/tasks", json={"task": "โทรหา Gamma", "customer": "Gamma"})

        listing = client.get("/api/customers").json()
        names = [c["name"] for c in listing]
        assert names[0] in ("ACME", "Beta Co") and set(names) == {"ACME", "Beta Co", "Gamma"}
        acme = next(c for c in listing if c["name"] == "ACME")
        assert acme["profile"] is True and acme["jobs"] == 2 and acme["waiting"] == 1 and acme["open_tasks"] == 2

        page = client.get("/api/customers/acme").json()  # any spelling finds it; the folder name is shown
        assert page["name"] == "ACME" and page["profile"]["folder"] == "customers/ACME"
        assert page["profile"]["file"] == "profile.md" and "ACME" in page["profile"]["text"]
        assert page["profile"]["files"] == ["profile.md"]
        assert {j["id"] for j in page["jobs"]} == {quote["id"], meeting["id"]}
        assert [d["job_id"] for d in page["documents"]] == [meeting["id"], meeting["id"]]  # .md and Word
        assert {d["path"].rsplit(".", 1)[1] for d in page["documents"]} == {"md", "docx"}
        assert len(page["tasks"]) == 2
        doc = client.get("/api/knowledge/file/" + page["documents"][0]["path"])
        assert doc.status_code == 200
        gamma = client.get("/api/customers/Gamma").json()
        assert gamma["profile"] is None and gamma["jobs"] == [] and gamma["tasks"][0]["task"] == "โทรหา Gamma"
        assert client.get("/api/customers/nobody").status_code == 404
        assert client.get("/api/customers/..%2F..%2Fetc").status_code == 404


def test_follow_up_items_can_name_a_customer(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        item = client.post("/api/tasks", json={"task": "ส่งแบบ", "due": "2026-11-20"}).json()
        assert item["customer"] is None
        assert client.put(f"/api/tasks/{item['id']}", json={"customer": " ACME "}).json()["customer"] == "ACME"
        assert client.put(f"/api/tasks/{item['id']}", json={"task": "ส่งแบบ rev.2"}).json()["customer"] == "ACME"
        feed = tasks_ics(app.state.tasks.open_items()).replace("\r\n ", "")
        assert "ลูกค้า: ACME" in feed


def test_quotes_and_briefs_get_the_customers_approved_history(settings):
    app = create_app(settings, start_background=False)
    store, tasks, runner = app.state.store, app.state.tasks, app.state.runner
    first = store.create("proposal-draft", "ใบเสนอราคา feeder ACME", "feeder 125A", {"customer": "ACME"})
    run(app, first["id"])
    approve(store, tasks, settings, first["id"])
    other = store.create("proposal-draft", "ใบเสนอราคา Beta", "x", {"customer": "Beta"})
    run(app, other["id"])
    approve(store, tasks, settings, other["id"])

    second = store.create("proposal-draft", "ใบเสนอราคาเพิ่ม ACME", "เพิ่ม feeder อีก 1 ชุด", {"customer": "Acme"})
    context = runner.prepare_context(second, JOB_TYPES["proposal-draft"])
    history = [c for c in context if c.startswith("customer-history/")]
    assert len(history) == 1 and history[0].endswith(".md") and first["id"][-4:] in history[0]  # not Beta's
    message = task_message(second, JOB_TYPES["proposal-draft"], settings, [], context)
    assert "context/customer-history/ คือผลงานที่ผู้ใช้อนุมัติแล้วของลูกค้ารายนี้" in message

    brief = store.create("sales-brief", "เตรียมพบ ACME", "", {"customer": "ACME"})
    assert any(c.startswith("customer-history/") for c in runner.prepare_context(brief, JOB_TYPES["sales-brief"]))
    email = store.create("general", "อีเมล ACME", "x", {"customer": "ACME"})
    assert not any(c.startswith("customer-history/") for c in runner.prepare_context(email, JOB_TYPES["general"]))
    assert customers.history_files(store, settings, store.create("proposal-draft", "ไม่มีลูกค้า", "x")) == []


def test_the_apps_effort_setting_never_reaches_claude_as_a_variable(monkeypatch):
    from app.runner import claude_env
    from app.review import slugify

    # the test machine's own Claude Code session had CLAUDE_EFFORT=max: it was read as this app's setting and
    # passed on, so every job ran at max. The app passes --effort itself, and keeps the variable out.
    monkeypatch.setenv("CLAUDE_EFFORT", "max")
    assert "CLAUDE_EFFORT" not in claude_env()
    assert slugify("ร่างอีเมลส่งใบเสนอราคา · สายและ breaker มอเตอร์") == "ร่างอีเมลส่งใบเสนอราคา-สายและ-breaker-มอเตอร์"
