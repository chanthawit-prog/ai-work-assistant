import asyncio
import base64
import io
import os
import time
import zipfile
from dataclasses import replace
from datetime import datetime

from fastapi.testclient import TestClient

from app.convert import docx_to_text
from app.main import create_app
from app.store import FAILED, safe_filename
from app.watchers import FollowupScheduler, InboxWatcher


def client_for(settings):
    return TestClient(create_app(settings, start_background=False))


def test_create_run_view_and_approve_over_http(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        assert len(client.get("/api/job-types").json()) == 6
        res = client.post("/api/jobs", data={"type": "meeting-summary", "title": "ประชุมทีม"},
                          files=[("files", ("บันทึก ประชุม.txt", "ข้อความประชุม".encode(), "text/plain"))])
        assert res.status_code == 200, res.text
        job_id = res.json()["id"]
        assert client.get("/api/jobs").json()[0]["status"] == "queued"

        asyncio.run(app.state.runner.run_job(job_id))
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["job"]["status"] == "needs_review"
        assert [f["path"] for f in detail["files"]["input"]] == ["บันทึก ประชุม.txt"]
        assert {f["path"] for f in detail["files"]["output"]} == {"summary.md", "action_items.json"}
        assert detail["next"] == len(detail["steps"]) > 0
        assert client.get(f"/api/jobs/{job_id}?after={detail['next']}").json()["steps"] == []

        summary = client.get(f"/api/jobs/{job_id}/files/output/summary.md").json()
        assert summary["text"].startswith("# ประชุมทีม")
        raw = client.get(f"/api/jobs/{job_id}/files/input/บันทึก ประชุม.txt?raw=1")
        assert raw.status_code == 200 and raw.content.decode() == "ข้อความประชุม"
        assert client.get(f"/api/jobs/{job_id}/files/output/../job.json").status_code == 404
        assert client.get("/api/jobs/../../etc/passwd").status_code == 404

        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409  # not failed
        assert detail["job"]["can_message"] is True
        assert client.post(f"/api/jobs/{job_id}/messages", data={"message": "  "}).status_code == 409
        queued = client.post(f"/api/jobs/{job_id}/messages", data={"message": "สรุปให้สั้นลง"}).json()
        assert queued["status"] == "queued"
        assert client.post(f"/api/jobs/{job_id}/revise", json={"feedback": "อีกข้อ"}).status_code == 409  # already queued
        asyncio.run(app.state.runner.run_job(job_id))
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["job"]["status"] == "needs_review" and len(detail["job"]["runs"]) == 2
        assert detail["partial_text"] is None
        approved = client.post(f"/api/jobs/{job_id}/approve", json={"note": ""}).json()
        assert approved["status"] == "done" and approved["review"]["tasks_added"] == 2
        items = client.get("/api/tasks").json()
        assert len(items) == 2
        client.post(f"/api/tasks/{items[0]['id']}", json={"status": "done"})
        assert len(client.get("/api/tasks").json()) == 1
        assert len(client.get("/api/tasks?include_done=true").json()) == 2

        status = client.get("/api/status").json()
        assert status["counts"]["done"] == 1 and status["open_tasks"] == 1


def test_validation_errors(settings):
    with client_for(settings) as client:
        res = client.post("/api/jobs", data={"type": "meeting-summary"},
                          files=[("files", ("meeting.m4a", b"\x00\x01", "audio/mp4"))])
        assert res.status_code == 400 and "ไฟล์เสียง" in res.json()["detail"]
        res = client.post("/api/jobs", data={"type": "sales-brief", "title": "x"})
        assert res.status_code == 400 and "ชื่อลูกค้า" in res.json()["detail"]
        assert client.post("/api/jobs", data={"type": "nope"}).status_code == 400
        assert client.get("/api/jobs/20260101-000000-general-abcd").status_code == 404


def test_dashboard_password(settings):
    locked = replace(settings, dashboard_password="s3cret")
    with client_for(locked) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/status").status_code == 401
        token = base64.b64encode(b"admin:s3cret").decode()
        assert client.get("/api/status", headers={"Authorization": f"Basic {token}"}).status_code == 200
        assert client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_inbox_watcher_creates_job_after_file_settles(settings):
    from app.store import JobStore

    store = JobStore(settings)
    watcher = InboxWatcher(store, settings)
    inbox = settings.inbox_dir / "meeting"
    old = time.time() - 60
    for name in ("ประชุม 1 พ.ย..txt", "voice.m4a"):
        path = inbox / name
        path.write_bytes(b"content")
        os.utime(path, (old, old))
    (inbox / "@eaDir").mkdir()
    assert watcher.scan_once() == []  # first sight: wait until size is stable
    created = watcher.scan_once()
    assert len(created) == 2 and not any(p.is_file() for p in inbox.iterdir())
    jobs = {j["title"]: j for j in store.list()}
    assert jobs["ประชุม 1 พ.ย."]["status"] == "queued" and jobs["ประชุม 1 พ.ย."]["source"] == "inbox"
    assert jobs["voice"]["status"] == FAILED and "ถอด" in jobs["voice"]["error"]


def test_followup_scheduler_runs_once_per_day(parts):
    store, tasks, state, _runner = parts
    scheduler = FollowupScheduler(store, tasks, state, store.settings)
    monday_9am = datetime(2026, 11, 2, 9, 0)
    assert scheduler.tick(monday_9am) is None  # no open items yet, but the day is marked
    state.set("followup_last", None)
    tasks.add_from_meeting({"id": "20261101-000000-meeting-summary-abcd", "title": "x"}, [{"task": "ส่งแบบ"}])
    assert scheduler.tick(datetime(2026, 11, 2, 7, 59)) is None  # too early
    job_id = scheduler.tick(monday_9am)
    assert job_id and store.get(job_id)["type"] == "follow-up"
    assert scheduler.tick(datetime(2026, 11, 2, 15, 0)) is None  # once per day
    assert scheduler.tick(datetime(2026, 11, 7, 9, 0)) is None  # Saturday is off


def test_docx_and_filename_helpers(tmp_path):
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           '<w:p><w:r><w:t>สมชาย: เริ่มประชุม</w:t></w:r></w:p><w:p><w:r><w:t>วิภา:</w:t><w:tab/><w:t>รับทราบ</w:t></w:r></w:p>'
           '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    path = tmp_path / "t.docx"
    path.write_bytes(buf.getvalue())
    assert docx_to_text(path) == "สมชาย: เริ่มประชุม\nวิภา:\tรับทราบ\n"
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("รายงาน:ผล?.pdf") == "รายงาน_ผล_.pdf"
    assert safe_filename(".hidden") == "hidden"
