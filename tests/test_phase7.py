"""Phase 7: handing a result on to the next job, and photo inputs."""
import asyncio

from fastapi.testclient import TestClient

from app.convert import prepare_inputs
from app.jobtypes import JOB_TYPES
from app.main import create_app
from app.prompts import task_message
from app.review import approve


def finished(app, type_id, title, instructions="", params=None, files=None):
    """A job that Claude (the fake) has finished: it waits for review."""
    store = app.state.store
    job = store.create(type_id, title, instructions, params or {})
    for name, data in (files or {}).items():
        store.add_file(job["id"], "input", name, data)
    asyncio.run(app.state.runner.run_job(job["id"]))
    assert store.get(job["id"])["status"] == "needs_review"
    return job["id"]


def test_hand_an_engineering_answer_on_to_a_quotation(settings):
    app = create_app(settings, start_background=False)
    store = app.state.store
    with TestClient(app) as client:
        source = finished(app, "eng-qa", "สายมอเตอร์ปั๊ม 75 kW", "เลือกสายและ breaker ระยะ 80 m")
        detail = client.get(f"/api/jobs/{source}").json()
        assert detail["handoff"]["files"] == ["answer.md"] and "proposal-draft" in detail["handoff"]["types"]
        url = f"/api/jobs/{source}/handoff"

        missing_customer = client.post(url, json={"type": "proposal-draft", "instructions": "ร่างใบเสนอราคา"})
        assert missing_customer.status_code == 409 and "ลูกค้า" in missing_customer.json()["detail"]
        assert client.post(url, json={"type": "proposal-draft", "customer": "ACME"}).status_code == 409  # no requirement
        assert client.post(url, json={"type": "meeting-summary"}).status_code == 409
        assert client.post(url, json={"type": "nope"}).status_code == 409
        assert client.post(url, json={"type": "general", "instructions": "x", "files": ["../job.json"]}).status_code == 404
        assert client.post(url, json={"type": "general", "instructions": "x", "files": ["missing.md"]}).status_code == 404
        assert client.post(url, json={"type": "general", "instructions": "x", "inputs": ["../job.json"]}).status_code == 404

        res = client.post(url, json={"type": "proposal-draft", "customer": "ACME",
                                     "instructions": "ร่างใบเสนอราคาตามสเปกในคำตอบนี้"})
        assert res.status_code == 200, res.text
        new = res.json()
        assert new["type"] == "proposal-draft" and new["status"] == "queued" and new["source"] == "handoff"
        assert new["title"] == "ร่างใบเสนอราคา · สายมอเตอร์ปั๊ม 75 kW"
        assert new["params"]["subject"] == "สายมอเตอร์ปั๊ม 75 kW"
        assert new["params"]["customer"] == "ACME" and new["params"]["from_job"] == source
        assert new["params"]["from_files"] == ["answer.md"] and new["params"]["from_approved"] is False
        copied = store.job_dir(new["id"]) / "input" / "answer.md"
        assert copied.read_bytes() == (store.job_dir(source) / "output" / "answer.md").read_bytes()

        # the two jobs point at each other
        assert client.get(f"/api/jobs/{source}").json()["job"]["handoffs"] == [
            {"id": new["id"], "title": new["title"], "status": "queued", "type_label": "ร่างใบเสนอราคา"}]
        origin = client.get(f"/api/jobs/{new['id']}").json()["job"]["origin"]
        assert origin["id"] == source and origin["status"] == "needs_review"
        assert client.get(f"/api/jobs/{new['id']}").json()["handoff"] is None  # queued: nothing to pass on yet
        assert client.post(f"/api/jobs/{new['id']}/handoff", json={"type": "general", "instructions": "x"}).status_code == 409

        # Claude is told where the files came from, and that they are not approved yet
        message = task_message(store.get(new["id"]), JOB_TYPES["proposal-draft"], settings, ["answer.md"], [])
        assert 'ต่อจากงาน: "สายมอเตอร์ปั๊ม 75 kW" (ถามงานวิศวกรรม)' in message
        assert "ยังไม่ได้อนุมัติ" in message and "ไฟล์ผลงานที่ส่งต่อมา: input/answer.md" in message
        asyncio.run(app.state.runner.run_job(new["id"]))
        assert store.get(new["id"])["status"] == "needs_review"

        # the next step keeps the subject of the first job, so titles (and library file names) never pile up
        email = client.post(f"/api/jobs/{new['id']}/handoff", json={"type": "general", "instructions": "ร่างอีเมล"}).json()
        assert email["title"] == "งานทั่วไป · สายมอเตอร์ปั๊ม 75 kW" and email["params"]["subject"] == "สายมอเตอร์ปั๊ม 75 kW"
        assert email["params"]["customer"] == "ACME"


def test_hand_off_from_an_approved_quotation_keeps_the_customer(settings):
    app = create_app(settings, start_background=False)
    store = app.state.store
    with TestClient(app) as client:
        source = finished(app, "proposal-draft", "ใบเสนอราคา feeder ACME", "feeder 125A 2 ชุด", {"customer": "ACME"})
        approve(store, app.state.tasks, settings, source)
        email = client.post(f"/api/jobs/{source}/handoff", json={
            "type": "general", "instructions": "ร่างอีเมลส่งใบเสนอราคานี้ถึงลูกค้า",
            "title": "ร่างอีเมลส่งใบเสนอราคา · ใบเสนอราคา feeder ACME"}).json()
        assert email["title"] == "ร่างอีเมลส่งใบเสนอราคา · ใบเสนอราคา feeder ACME"
        assert email["params"]["customer"] == "ACME" and email["params"]["from_approved"] is True
        message = task_message(store.get(email["id"]), JOB_TYPES["general"], settings, ["proposal.md"], [])
        assert "ผู้ใช้อนุมัติแล้ว" in message and "ลูกค้า: ACME" in message
        brief = client.post(f"/api/jobs/{source}/handoff", json={"type": "sales-brief"}).json()
        assert brief["params"]["customer"] == "ACME"  # required, and carried over without typing it again
        handoffs = client.get(f"/api/jobs/{source}").json()["job"]["handoffs"]
        assert [h["id"] for h in handoffs] == [email["id"], brief["id"]]


def test_hand_off_passes_only_the_files_you_pick(settings):
    app = create_app(settings, start_background=False)
    store = app.state.store
    with TestClient(app) as client:
        source = finished(app, "meeting-summary", "ประชุม ACME", files={"transcript.txt": "สมชายจะส่งแบบ".encode()})
        assert client.get(f"/api/jobs/{source}").json()["handoff"]["files"] == ["summary.md", "action_items.json"]
        new = client.post(f"/api/jobs/{source}/handoff", json={
            "type": "general", "instructions": "ร่างอีเมลสรุปประชุม", "files": ["summary.md"]}).json()
        assert [f["path"] for f in store.list_files(new["id"], "input")] == ["summary.md"]
        # the original transcript too, when you want Claude to check the summary against it
        both = client.post(f"/api/jobs/{source}/handoff", json={
            "type": "general", "instructions": "ตรวจสรุปกับ transcript", "files": ["summary.md"],
            "inputs": ["transcript.txt"]}).json()
        assert both["params"]["from_files"] == ["summary.md"] and both["params"]["from_inputs"] == ["transcript.txt"]
        assert sorted(f["path"] for f in store.list_files(both["id"], "input")) == ["summary.md", "transcript.txt"]
        message = task_message(store.get(both["id"]), JOB_TYPES["general"], settings, [], [])
        assert "ไฟล์ต้นฉบับที่แนบในงานนั้น: input/transcript.txt" in message


def test_photos_are_inputs_and_heic_gets_a_clear_message(tmp_path):
    for name in ("nameplate.jpg", "site.webp", "diagram.gif", "IMG_0001.HEIC"):
        (tmp_path / name).write_bytes(b"x")
    warnings = prepare_inputs(tmp_path)
    assert len(warnings) == 1 and "IMG_0001.HEIC" in warnings[0] and "JPG" in warnings[0]
