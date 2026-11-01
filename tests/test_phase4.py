"""Found by running the Docker image: jobs before `claude auth login`, file permissions, OOM wording."""
import asyncio
import stat

from fastapi.testclient import TestClient

from app import sysmon
from app.main import create_app
from app.runner import LOGIN_MESSAGE, OOM_MESSAGES
from app.store import FAILED, NEEDS_REVIEW, QUEUED, write_json


def test_jobs_wait_while_claude_is_not_logged_in(parts):
    store, _tasks, state, runner = parts
    state.set("claude", {"installed": True, "logged_in": False, "method": "none"})
    job = store.create("general", "ก่อนล็อกอิน", "สรุปเอกสาร")

    async def scenario():
        runner.tick()
        assert not runner.active and runner.login_wait and runner.wait_info(job["id"]) == {"reason": "login"}
        state.set("claude", {"installed": True, "logged_in": True, "method": "claude.ai"})
        runner.tick()
        assert job["id"] in runner.active and not runner.login_wait
        await asyncio.gather(*runner.active.values())

    asyncio.run(scenario())
    assert store.get(job["id"])["status"] == NEEDS_REVIEW


def test_unknown_or_missing_claude_never_blocks(parts):
    _store, _tasks, state, runner = parts
    assert runner.login_ok()  # never checked yet: let the run itself tell
    state.set("claude", {"installed": False, "logged_in": False})
    assert runner.login_ok()  # not installed: the job fails with "command not found", which says more


def test_auth_failure_becomes_thai_instructions_and_holds_the_queue(parts, monkeypatch):
    store, _tasks, state, runner = parts
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "auth")
    first = store.create("general", "งานแรก", "x")
    second = store.create("general", "งานที่สอง", "y")
    asyncio.run(runner.run_job(first["id"]))
    failed = store.get(first["id"])
    assert failed["status"] == FAILED and failed["error"] == LOGIN_MESSAGE
    assert "claude auth login" in LOGIN_MESSAGE and state.get("claude")["logged_in"] is False

    async def scenario():
        runner.tick()
        assert not runner.active and runner.wait_info(second["id"]) == {"reason": "login"}

    asyncio.run(scenario())
    assert store.get(second["id"])["status"] == QUEUED


def test_waiting_for_login_shows_in_the_api(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        app.state.runner.state.set("claude", {"installed": True, "logged_in": False})
        job = client.post("/api/jobs", data={"type": "general", "instructions": "สรุป"}).json()
        app.state.runner.tick()
        listed = client.get("/api/jobs").json()[0]
        assert listed["id"] == job["id"] and listed["wait"] == {"reason": "login"}


def test_json_files_are_readable_by_file_station(tmp_path):
    path = tmp_path / "job.json"
    write_json(path, {"ok": True})
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_container_oom_quotes_the_cap(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    counts = iter([0, 1])
    monkeypatch.setattr(sysmon, "container_memory", lambda *a, **k: {"oom_kills": next(counts), "limit_mb": 200})
    monkeypatch.setenv("FAKE_CLAUDE_CRASH", "kill")
    job = store.create("general", "เกินเพดาน", "x")
    asyncio.run(runner.run_job(job["id"]))
    error = store.get(job["id"])["error"]
    assert error.startswith(OOM_MESSAGES["container"]) and error.endswith("(เพดาน 200 MB)")


def test_dashboard_can_be_added_to_a_home_screen_even_with_a_password(settings):
    from dataclasses import replace
    locked = replace(settings, dashboard_password="secret")
    with TestClient(create_app(locked, start_background=False)) as client:
        assert client.get("/api/status").status_code == 401
        manifest = client.get("/static/manifest.webmanifest")
        assert manifest.status_code == 200 and manifest.headers["content-type"].startswith("application/manifest+json")
        data = manifest.json()
        assert data["display"] == "standalone" and data["start_url"] == "/"
        for icon in data["icons"]:
            assert client.get(icon["src"]).status_code == 200
        assert client.get("/static/apple-touch-icon.png").status_code == 200
        assert client.get("/static/app.js").status_code == 401  # everything else stays behind the password
        page = client.get("/", auth=("admin", "secret")).text
        assert 'rel="manifest"' in page and 'rel="apple-touch-icon"' in page
