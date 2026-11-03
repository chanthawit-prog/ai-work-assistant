"""Phase 6: editing a result yourself (no Claude run) and the follow-up list you can edit, with a calendar feed."""
import asyncio

from fastapi.testclient import TestClient

from app.jobtypes import JOB_TYPES
from app.main import create_app
from app.prompts import task_message
from app.review import CHANGED_MESSAGE, approve, save_edit
from app.store import NEEDS_REVIEW, QUEUED, revision


def make_meeting(store, text="สมชายจะส่งแบบภายในวันศุกร์"):
    job = store.create("meeting-summary", "ประชุมโครงการ ABC")
    store.add_file(job["id"], "input", "transcript.txt", text.encode("utf-8"))
    return job


def test_edit_a_result_yourself_then_claude_builds_on_it(settings):
    app = create_app(settings, start_background=False)
    runner = app.state.runner
    with TestClient(app) as client:
        job_id = make_meeting(app.state.store)["id"]
        asyncio.run(runner.run_job(job_id))
        base = f"/api/jobs/{job_id}/files/output"
        summary = client.get(f"{base}/summary.md").json()
        assert summary["editable"] is True and len(summary["sha"]) == 64
        assert client.get(f"{base}/action_items.json").json()["editable"] is False
        assert client.get(f"/api/jobs/{job_id}").json()["job"]["rev"] == 1

        edited = summary["text"] + "\n## หมายเหตุจากผู้ตรวจ\n- ราคานี้ยืนราคา 30 วัน\n"
        res = client.put(f"{base}/summary.md", json={"text": edited, "base_sha": summary["sha"]})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["changed"] is True and body["job"]["rev"] == 2 and body["job"]["status"] == "needs_review"
        assert body["job"]["edits"][0]["file"] == "summary.md"
        path = app.state.store.job_dir(job_id) / "output" / "summary.md"
        assert path.read_text("utf-8") == edited
        steps = client.get(f"/api/jobs/{job_id}").json()["steps"]
        assert steps[-1]["kind"] == "edit" and "summary.md" in steps[-1]["text"] and "Rev. 2" in steps[-1]["text"]

        # saving the same text again changes nothing; an outdated copy is refused, not merged
        fresh = client.get(f"{base}/summary.md").json()
        again = client.put(f"{base}/summary.md", json={"text": edited, "base_sha": fresh["sha"]}).json()
        assert again["changed"] is False and again["job"]["rev"] == 2
        stale = client.put(f"{base}/summary.md", json={"text": "ทับของใหม่", "base_sha": summary["sha"]})
        assert stale.status_code == 409 and stale.json()["detail"] == CHANGED_MESSAGE
        assert path.read_text("utf-8") == edited

        # only text results, only inside output/, and not huge
        assert client.put(f"{base}/action_items.json", json={"text": "[]"}).status_code == 409
        assert client.put(f"{base}/..%2Fjob.json", json={"text": "{}"}).status_code == 404  # browsers drop "../"
        assert client.put(f"{base}/missing.md", json={"text": "x"}).status_code == 404
        assert client.put(f"{base}/summary.md", json={"text": "ก" * 800_000}).status_code == 413

        # while Claude works on it, the file is Claude's
        client.post(f"/api/jobs/{job_id}/messages", data={"message": "เพิ่มประเด็นงบประมาณ"})
        busy = client.put(f"{base}/summary.md", json={"text": "แก้ระหว่างรอคิว"})
        assert busy.status_code == 409 and "รอตรวจ" in busy.json()["detail"]

        asyncio.run(runner.run_job(job_id))
        job = app.state.store.get(job_id)
        assert job["status"] == NEEDS_REVIEW and job["pending_edits"] == []
        assert job["runs"][-1]["edited"] == ["summary.md"]
        text = path.read_text("utf-8")
        assert "ราคานี้ยืนราคา 30 วัน" in text and "เพิ่มประเด็นงบประมาณ" in text  # the fake Claude kept the edit
        assert revision(job) == 3

        approved = client.post(f"/api/jobs/{job_id}/approve", json={"note": ""}).json()
        library = settings.data_dir / approved["review"]["archived"][0]
        assert "ราคานี้ยืนราคา 30 วัน" in library.read_text("utf-8")
        assert client.put(f"{base}/summary.md", json={"text": "หลังอนุมัติ"}).status_code == 409


def test_edit_keeps_windows_line_breaks(parts):
    store, _tasks, _state, _runner = parts
    job = make_meeting(store)
    store.update(job["id"], status=NEEDS_REVIEW)
    path = store.job_dir(job["id"]) / "output" / "notes.txt"
    path.write_bytes("บรรทัด 1\r\nบรรทัด 2\r\n".encode("utf-8"))
    _job, changed = save_edit(store, job["id"], "notes.txt", "บรรทัด 1\nบรรทัด 2\n")  # as a browser sends it
    assert changed is False
    _job, changed = save_edit(store, job["id"], "notes.txt", "บรรทัด 1\nบรรทัด 2 แก้แล้ว\n")
    assert changed is True and path.read_bytes() == "บรรทัด 1\r\nบรรทัด 2 แก้แล้ว\r\n".encode("utf-8")


def test_claude_is_told_which_files_you_edited(settings):
    job_type = JOB_TYPES["proposal-draft"]
    job = {"title": "ใบเสนอราคา ACME", "params": {}, "instructions": ""}
    resumed = task_message(job, job_type, settings, [], [], feedback="ลดราคา 5%", resumed=True,
                           edited=["proposal.md"])
    assert "ผู้ใช้แก้ไฟล์ผลลัพธ์เหล่านี้ด้วยตนเอง" in resumed and "- output/proposal.md" in resumed
    assert resumed.index("ลดราคา 5%") < resumed.index("output/proposal.md")
    fresh = task_message(job, job_type, settings, [], [], feedback="ลดราคา 5%", edited=["proposal.md"])
    assert "คงส่วนที่ผู้ใช้แก้ไว้" in fresh
    assert "ด้วยตนเอง" not in task_message(job, job_type, settings, [], [], feedback="ลดราคา 5%", resumed=True)


def test_a_quota_wait_keeps_the_edit_notice_for_the_next_try(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    job = make_meeting(store)
    asyncio.run(runner.run_job(job["id"]))
    path = store.job_dir(job["id"]) / "output" / "summary.md"
    save_edit(store, job["id"], "summary.md", path.read_text("utf-8") + "\nแก้เอง\n")
    runner.send_message(job["id"], "ตรวจอีกรอบ")
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", "1")
    asyncio.run(runner.run_job(job["id"]))
    job = store.get(job["id"])
    assert job["status"] == QUEUED and job["pending_feedback"] == "ตรวจอีกรอบ"
    assert job["pending_edits"] == ["summary.md"]


def test_approve_uses_the_revision_with_your_edits(parts, settings, monkeypatch):
    import app.review as review

    store, tasks, _state, runner = parts
    job = make_meeting(store)
    asyncio.run(runner.run_job(job["id"]))
    path = store.job_dir(job["id"]) / "output" / "summary.md"
    job, _ = save_edit(store, job["id"], "summary.md", path.read_text("utf-8") + "\nแก้เอง\n")
    assert revision(job) == 2
    seen = {}
    real = review.markdown_to_docx
    monkeypatch.setattr(review, "markdown_to_docx", lambda md, **kw: seen.update(kw) or real(md, **kw))
    approve(store, tasks, settings, job["id"])
    assert seen["rev"] == 2


# -- follow-up list you can edit, and its calendar feed -----------------------------------------------------
def test_follow_up_items_can_be_added_edited_and_deleted(settings):
    with TestClient(create_app(settings, start_background=False)) as client:
        assert client.post("/api/tasks", json={"task": "  "}).status_code == 409
        assert client.post("/api/tasks", json={"task": "x", "due": "20/11/2026"}).status_code == 409
        assert client.post("/api/tasks", json={"task": "x", "priority": "urgent"}).status_code == 409
        item = client.post("/api/tasks", json={"task": "โทรตาม PO จาก ACME", "owner": "เรา", "due": "2026-11-20",
                                               "priority": "high"}).json()
        assert item["id"] == "A-0001" and item["source_job"] is None and item["status"] == "open"

        edited = client.put("/api/tasks/A-0001", json={"due": "2026-11-25", "notes": "ลูกค้าขอเลื่อน"}).json()
        assert edited["due"] == "2026-11-25" and edited["task"] == "โทรตาม PO จาก ACME" and edited["edited_at"]
        cleared = client.put("/api/tasks/A-0001", json={"due": None, "owner": ""}).json()
        assert cleared["due"] is None and cleared["owner"] is None
        assert client.put("/api/tasks/A-0001", json={"task": ""}).status_code == 409
        assert client.put("/api/tasks/A-9999", json={"task": "x"}).status_code == 404

        assert client.get("/api/status").json()["open_tasks"] == 1
        assert client.delete("/api/tasks/A-0001").json() == {"ok": True}
        assert client.get("/api/tasks?include_done=true").json() == []
        assert client.delete("/api/tasks/A-0001").status_code == 404
        assert client.post("/api/tasks", json={"task": "งานใหม่"}).json()["id"] == "A-0002"  # ids are never reused


def test_approving_a_meeting_again_keeps_your_edits_and_deletions(parts, settings):
    store, tasks, _state, runner = parts
    job = store.create("meeting-summary", "ประชุมโครงการ ABC")
    raw = [{"task": "ส่งแบบ", "owner": "สมชาย", "due": "2026-11-06"},
           {"task": "เช็คราคา", "owner": "สมศรี"},
           {"task": "นัดเข้าไซต์", "owner": "เรา"}]
    assert tasks.add_from_meeting(job, raw) == 3
    tasks.update("A-0001", {"due": "2026-11-13"})                 # changed the date
    tasks.update("A-0002", {"task": "เช็คราคาสายไฟ CV 4x95"})      # reworded
    tasks.delete("A-0003")                                         # not needed
    assert tasks.add_from_meeting(job, raw + [{"task": "งานใหม่จากรอบสอง"}]) == 1
    items = {i["id"]: i for i in tasks.all()}
    assert items["A-0001"]["due"] == "2026-11-13"
    assert items["A-0002"]["task"] == "เช็คราคาสายไฟ CV 4x95" and items["A-0002"]["source_task"] == "เช็คราคา"
    assert "A-0003" not in items
    assert [i["task"] for i in items.values()][-1] == "งานใหม่จากรอบสอง"


def test_calendar_feed_is_private_and_valid(settings, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Bangkok")
    settings_url = settings.__class__(**{**settings.__dict__, "dashboard_url": "http://nas.local:8080",
                                         "dashboard_password": "secret"})
    app = create_app(settings_url, start_background=False)
    tasks = app.state.tasks
    job = {"id": "20261101-090000-meeting-summary-ab12", "title": "ประชุม ACME, รอบ 2; สรุป"}
    tasks.add_from_meeting(job, [{"task": "ส่งแบบไฟฟ้า, ฉบับแก้ไข; Rev.2 " + "ยาวมาก" * 20, "owner": "สมชาย",
                                  "due": "2026-11-06", "priority": "high", "notes": "บรรทัด 1\nบรรทัด 2"},
                                 {"task": "ยังไม่มีกำหนด"}])
    tasks.add({"task": "เสร็จแล้ว", "due": "2026-11-07"})
    tasks.set_status("A-0003", "done")
    auth = {"Authorization": "Basic " + __import__("base64").b64encode(b"admin:secret").decode()}
    with TestClient(app) as client:
        assert client.get("/api/calendar").status_code == 401  # managing the link needs the password
        assert client.get("/api/calendar", headers=auth).json() == {"enabled": False, "path": None}
        assert client.get("/calendar/anything.ics").status_code == 404  # no link made yet
        path = client.post("/api/calendar", headers=auth).json()["path"]
        assert path.startswith("/calendar/") and path.endswith(".ics") and len(path) > 40

        feed = client.get(path)  # no password: calendar apps cannot send one
        assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")
        assert client.get(path.replace(".ics", "x.ics")).status_code == 404
        assert client.get(path[:-4]).status_code == 404

        raw_bytes = feed.content
        assert raw_bytes.count(b"\r\n") == raw_bytes.count(b"\n")  # CRLF everywhere
        assert all(len(line) <= 75 for line in raw_bytes.split(b"\r\n"))  # folded by octets
        text = raw_bytes.decode("utf-8")  # folding never split a Thai character
        unfolded = text.replace("\r\n ", "")
        assert unfolded.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and unfolded.endswith("END:VCALENDAR\r\n")
        assert unfolded.count("BEGIN:VEVENT") == 1  # only open items with a due date
        assert "DTSTART;VALUE=DATE:20261106\r\n" in unfolded and "DTEND;VALUE=DATE:20261107\r\n" in unfolded
        assert "SUMMARY:ส่งแบบไฟฟ้า\\, ฉบับแก้ไข\\; Rev.2" in unfolded
        assert "หมายเหตุ: บรรทัด 1\\nบรรทัด 2" in unfolded and "ที่มา: ประชุม ACME\\, รอบ 2\\; สรุป" in unfolded
        assert "PRIORITY:1\r\n" in unfolded and "TRIGGER:-PT15H" in unfolded
        assert f"URL:http://nas.local:8080/#/job/{job['id']}\r\n" in unfolded
        assert "UID:A-0001-" in unfolded and "@ai-work-assistant" in unfolded

        new_path = client.post("/api/calendar", headers=auth).json()["path"]
        assert new_path != path and client.get(path).status_code == 404 and client.get(new_path).status_code == 200
        assert client.delete("/api/calendar", headers=auth).json()["enabled"] is False
        assert client.get(new_path).status_code == 404
