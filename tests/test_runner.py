import asyncio
import json
from datetime import datetime

from app.events import summarize
from app.jobtypes import JOB_TYPES
from app.review import approve
from app.runner import claude_env
from app.store import CANCELLED, DONE, FAILED, NEEDS_REVIEW, QUEUED


def make_meeting(store, text="สมชายจะส่งแบบภายในวันศุกร์"):
    job = store.create("meeting-summary", "ประชุมโครงการ ABC")
    store.add_file(job["id"], "input", "transcript.txt", text.encode("utf-8"))
    return job


def test_meeting_job_runs_and_approval_feeds_followup(parts, settings):
    store, tasks, state, runner = parts
    job = make_meeting(store)
    asyncio.run(runner.run_job(job["id"]))

    job = store.get(job["id"])
    assert job["status"] == NEEDS_REVIEW, job["error"]
    assert job["session_id"]
    assert job["runs"][0]["kind"] == "initial" and job["runs"][0]["exit_code"] == 0
    out = store.job_dir(job["id"]) / "output"
    assert (out / "summary.md").is_file() and (out / "action_items.json").is_file()
    steps, total = store.read_steps(job["id"])
    texts = [s["text"] for s in steps]
    assert any(t.startswith("อ่านไฟล์ input/transcript.txt") for t in texts)
    assert any("Claude ทำงานเสร็จ" in t for t in texts)
    assert total == len(steps)
    assert state.get("quota")["status"] == "allowed"

    job = approve(store, tasks, settings, job["id"], note="ok")
    assert job["status"] == DONE and job["review"]["tasks_added"] == 2
    assert len(tasks.open_items()) == 2 and tasks.open_items()[0]["id"] == "A-0001"
    assert list((settings.library_dir / "meeting-summary").glob("*.md"))


def test_follow_up_message_resumes_the_same_session(parts):
    store, _tasks, _state, runner = parts
    job = make_meeting(store)
    asyncio.run(runner.run_job(job["id"]))
    session = store.get(job["id"])["session_id"]

    runner.send_message(job["id"], "เพิ่มประเด็นงบประมาณ")
    cmd = runner.build_command(JOB_TYPES["meeting-summary"], resume_session=session)
    assert cmd[cmd.index("--resume") + 1] == session
    asyncio.run(runner.run_job(job["id"]))
    job = store.get(job["id"])
    assert job["status"] == NEEDS_REVIEW
    assert [r["kind"] for r in job["runs"]] == ["initial", "message"]
    assert job["session_id"] == session and job["pending_feedback"] is None
    summary = (store.job_dir(job["id"]) / "output" / "summary.md").read_text("utf-8")
    assert "เพิ่มประเด็นงบประมาณ" in summary

    steps, _ = store.read_steps(job["id"])
    users = [s for s in steps if s["kind"] == "user"]
    assert users[0]["text"].startswith("สรุปประชุม: ประชุมโครงการ ABC") and users[0]["files"] == ["transcript.txt"]
    assert users[1]["text"] == "เพิ่มประเด็นงบประมาณ"
    # live token deltas are shown while running but never written to the event log
    events = (store.job_dir(job["id"]) / "events.jsonl").read_text("utf-8")
    assert '"stream_event"' not in events and job["id"] not in runner.partial


def test_message_after_approval_reapproves_without_duplicate_tasks(parts, settings):
    store, tasks, _state, runner = parts
    job = make_meeting(store)
    asyncio.run(runner.run_job(job["id"]))
    approve(store, tasks, settings, job["id"])
    tasks.set_status("A-0001", "done")

    runner.send_message(job["id"], "ช่วยเพิ่มรายละเอียดหน่อย")
    asyncio.run(runner.run_job(job["id"]))
    job = approve(store, tasks, settings, job["id"])
    items = tasks.all()
    assert [i["status"] for i in items] == ["done", "open"]  # ticked item kept, open item replaced, no duplicates
    assert job["review"]["tasks_added"] == 1


def test_partial_text_tracking(parts):
    _store, _tasks, _state, runner = parts
    runner._track_partial("j", {"event": {"type": "message_start"}, "parent_tool_use_id": None})
    for word in ("กำลัง", "พิมพ์"):
        runner._track_partial("j", {"parent_tool_use_id": None, "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": word}}})
    runner._track_partial("j", {"parent_tool_use_id": "toolu_x", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "subagent"}}})
    assert runner.partial["j"] == "กำลังพิมพ์"


def test_failure_and_retry(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    job = make_meeting(store)
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    asyncio.run(runner.run_job(job["id"]))
    job = store.get(job["id"])
    assert job["status"] == FAILED and "simulated failure" in job["error"]

    monkeypatch.delenv("FAKE_CLAUDE_FAIL")
    try:
        runner.send_message("20260101-000000-general-abcd", "x")
    except KeyError:
        pass
    runner.retry(job["id"])
    asyncio.run(runner.run_job(job["id"]))
    job = store.get(job["id"])
    assert job["status"] == NEEDS_REVIEW and job["runs"][-1]["kind"] == "retry"


def test_missing_outputs_fail_the_job(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    job = make_meeting(store)
    monkeypatch.setenv("FAKE_CLAUDE_SKIP_OUTPUTS", "1")
    asyncio.run(runner.run_job(job["id"]))
    job = store.get(job["id"])
    assert job["status"] == FAILED and "output/summary.md" in job["error"]


def test_quota_rejection_requeues_until_reset(parts, monkeypatch):
    store, _tasks, state, runner = parts
    job = make_meeting(store)
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", "1")
    asyncio.run(runner.run_job(job["id"]))
    job = store.get(job["id"])
    assert job["status"] == QUEUED and job["quota_waits"] == 1
    assert datetime.fromisoformat(job["not_before"]) > datetime.now().astimezone()
    assert store.runnable(datetime.now().astimezone()) == []
    assert state.get("quota")["status"] == "rejected"


def test_cancel_running_job(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    job = make_meeting(store)
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "1")

    async def scenario():
        runner._start(job["id"])
        for _ in range(50):
            await asyncio.sleep(0.1)
            if job["id"] in runner.procs:
                break
        await runner.cancel(job["id"])
        await asyncio.wait_for(runner.active[job["id"]], timeout=20)

    asyncio.run(scenario())
    job = store.get(job["id"])
    assert job["status"] == CANCELLED


def test_cancel_queued_job(parts):
    store, _tasks, _state, runner = parts
    job = make_meeting(store)
    asyncio.run(runner.cancel(job["id"]))
    assert store.get(job["id"])["status"] == CANCELLED


def test_command_uses_subscription_safely(parts, monkeypatch):
    _store, _tasks, _state, runner = parts
    cmd = runner.build_command(JOB_TYPES["meeting-summary"])
    assert "--bare" not in cmd  # bare mode ignores subscription logins
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    assert "--include-partial-messages" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,Write,Edit,Glob,Grep"
    eng = runner.build_command(JOB_TYPES["eng-qa"])
    allowed = eng[eng.index("--allowedTools") + 1].split(",")
    assert {"Bash(python3 -c *)", "Bash(python3 -m *)", "Bash(python3 output/*)", "Bash(cd *)"} <= set(allowed)
    assert "Bash(python3 *)" not in allowed  # `python3 -I ...` would skip the jail in tools/sitecustomize.py

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    env = claude_env()
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_followup_job_gets_open_items_as_context(parts):
    store, tasks, _state, runner = parts
    job = make_meeting(store)
    tasks.add_from_meeting(job, [{"task": "ส่งแบบ", "owner": "สมชาย", "due": "2026-11-05"}, {"bad": True}])
    follow = store.create("follow-up", "ติดตามงาน")
    asyncio.run(runner.run_job(follow["id"]))
    ctx = json.loads((store.job_dir(follow["id"]) / "context" / "action_items.json").read_text("utf-8"))
    assert [i["task"] for i in ctx] == ["ส่งแบบ"]
    assert store.get(follow["id"])["status"] == NEEDS_REVIEW


def test_summarize_events():
    job_dir = "/data/jobs/x"
    tool = {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "cable", "path": "/data/knowledge"}}]}}
    assert summarize(tool, job_dir)[0]["text"] == "ค้นหาข้อความ “cable” ใน /data/knowledge"
    assert summarize(tool, job_dir, "/data/knowledge")[0]["text"] == "ค้นหาข้อความ “cable” ใน knowledge"
    glob = {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "name": "Glob", "input": {"pattern": "**/*", "path": "/data/jobs/x"}}]}}
    assert summarize(glob, job_dir)[0]["text"] == "ค้นหาไฟล์ **/* ใน โฟลเดอร์งาน"
    bash = {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "cd /data/jobs/x/output && python3 calc.py /data/knowledge/a.csv"}}]}}
    assert summarize(bash, job_dir, "/data/knowledge")[0]["text"] == "รันคำสั่ง: cd <โฟลเดอร์งาน>/output && python3 calc.py knowledge/a.csv"
    write = {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": "/data/jobs/x/output/a.md"}}]}}
    assert summarize(write, job_dir)[0]["text"] == "เขียนไฟล์ output/a.md"
    retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 5, "retry_delay_ms": 2000, "error": "rate_limit"}
    assert "rate_limit" in summarize(retry, job_dir)[0]["text"]
    warn = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "utilization": 0.9}}
    assert summarize(warn, job_dir)[0]["kind"] == "warn"
    assert summarize({"type": "stream_event"}, job_dir) == []
    # shape observed from Claude Code 2.1.316 with a subscription login
    real = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1793527200,
                                                            "rateLimitType": "seven_day", "utilization": 0.89}}
    assert summarize(real, job_dir)[0]["text"].startswith("โควต้า subscription รายสัปดาห์ ใกล้เต็ม (89%), รีเซ็ต ")


def test_store_cache_sees_external_edits_and_is_copy_safe(parts):
    store = parts[0]
    job = store.create("general", "x")
    path = store.job_dir(job["id"]) / "job.json"
    data = json.loads(path.read_text("utf-8"))
    data["title"] = "แก้ด้วยมือ"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert store.get(job["id"])["title"] == "แก้ด้วยมือ"
    copy_ = store.get(job["id"])
    copy_["title"] = "mutated"
    assert store.list()[0]["title"] == "แก้ด้วยมือ"
