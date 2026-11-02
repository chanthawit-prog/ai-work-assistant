"""Phase 8: how hard Claude thinks per job, Claude kept out of the app's files, and deleting jobs."""
import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from app.jobtypes import JOB_TYPES
from app.main import create_app
from app.runner import DENY_RULES


def flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_thinking_level_follows_the_job_then_the_setting_then_the_type(parts):
    store, _tasks, _state, runner = parts
    general, eng = JOB_TYPES["general"], JOB_TYPES["eng-qa"]
    assert general.effort == "medium" and JOB_TYPES["follow-up"].effort == "low" and eng.effort == "high"
    assert all(t.effort for t in JOB_TYPES.values())  # never Claude Code's own default: it thought for minutes
    plain = store.create("general", "อีเมล", "ร่างอีเมล")
    assert runner.effort_for(plain, general) == "medium"
    assert flag(runner.build_command(general, effort=runner.effort_for(plain, general)), "--effort") == "medium"
    assert flag(runner.build_command(eng, effort=runner.effort_for(plain, eng)), "--effort") == "high"
    chosen = store.create("eng-qa", "เร็ว ๆ", "x", {"effort": "low"})
    assert runner.effort_for(chosen, eng) == "low"
    runner.settings = replace(runner.settings, claude_effort="high")  # CLAUDE_EFFORT in .env
    assert runner.effort_for(plain, general) == "high" and runner.effort_for(chosen, eng) == "low"


def test_claude_cannot_read_or_change_the_app_files(parts):
    _store, _tasks, _state, runner = parts
    cmd = runner.build_command(JOB_TYPES["general"])
    rules = flag(cmd, "--disallowedTools").split(",")
    assert rules == DENY_RULES
    assert {"Read(./job.json)", "Read(./events.jsonl)", "Read(./steps.jsonl)", "Edit(./job.json)"} <= set(rules)


def test_the_level_is_chosen_on_the_form_and_recorded_per_run(settings):
    app = create_app(settings, start_background=False)
    store, runner = app.state.store, app.state.runner
    with TestClient(app) as client:
        assert client.post("/api/jobs", data={"type": "general", "instructions": "x", "effort": "turbo"}).status_code == 400
        job = client.post("/api/jobs", data={"type": "general", "instructions": "ร่างอีเมล", "effort": "low"}).json()
        assert job["params"]["effort"] == "low"
        asyncio.run(runner.run_job(job["id"]))
        assert store.get(job["id"])["runs"][-1]["effort"] == "low"

        # a message can change it for the next runs; "auto" goes back to the job type's default
        client.post(f"/api/jobs/{job['id']}/messages", data={"message": "ละเอียดขึ้น", "effort": "high"})
        assert store.get(job["id"])["params"]["effort"] == "high"
        asyncio.run(runner.run_job(job["id"]))
        client.post(f"/api/jobs/{job['id']}/messages", data={"message": "ตามปกติ", "effort": "auto"})
        assert "effort" not in store.get(job["id"])["params"]
        asyncio.run(runner.run_job(job["id"]))
        assert [r["effort"] for r in store.get(job["id"])["runs"]] == ["low", "high", "medium"]
        client.post(f"/api/jobs/{job['id']}/messages", data={"message": "ไม่ส่งระดับ"})  # older clients
        assert "effort" not in store.get(job["id"])["params"]
        asyncio.run(runner.run_job(job["id"]))

        eng = client.post("/api/jobs", data={"type": "eng-qa", "instructions": "สาย 75 kW"}).json()
        asyncio.run(runner.run_job(eng["id"]))
        assert store.get(eng["id"])["runs"][-1]["effort"] == "high"
        email = client.post(f"/api/jobs/{eng['id']}/handoff", json={"type": "general", "instructions": "อีเมล",
                                                                   "effort": "low"}).json()
        assert email["params"]["effort"] == "low"
        assert client.post(f"/api/jobs/{eng['id']}/handoff", json={"type": "general", "instructions": "x",
                                                                   "effort": "xhigh"}).status_code == 400
        deep = client.post("/api/jobs", data={"type": "eng-qa", "instructions": "ยาก", "effort": "max"}).json()
        assert deep["params"]["effort"] == "max"


# -- deleting jobs ----------------------------------------------------------------------------------------------
def test_deleted_jobs_wait_in_the_trash_then_go(settings):
    from datetime import datetime, timedelta

    app = create_app(settings, start_background=False)
    store, runner = app.state.store, app.state.runner
    with TestClient(app) as client:
        queued = client.post("/api/jobs", data={"type": "general", "instructions": "รอคิว"}).json()
        refused = client.delete(f"/api/jobs/{queued['id']}")
        assert refused.status_code == 409 and "ยกเลิก" in refused.json()["detail"]

        done = client.post("/api/jobs", data={"type": "eng-qa", "instructions": "สาย 75 kW"}).json()
        asyncio.run(runner.run_job(done["id"]))
        follow = client.post(f"/api/jobs/{done['id']}/handoff", json={"type": "general", "instructions": "อีเมล"}).json()
        res = client.delete(f"/api/jobs/{done['id']}")
        assert res.status_code == 200 and res.json()["days"] == 30
        assert res.json()["trash"] == f"trash/jobs/{done['id']}"
        assert client.get(f"/api/jobs/{done['id']}").status_code == 404
        assert done["id"] not in [j["id"] for j in client.get("/api/jobs").json()]
        kept = settings.data_dir / "trash" / "jobs" / done["id"]
        assert (kept / "job.json").is_file() and (kept / "output" / "answer.md").is_file()
        assert (kept / ".deleted_at").is_file()
        # the job it was handed on to says its origin is gone
        assert client.get(f"/api/jobs/{follow['id']}").json()["job"]["origin"]["status"] is None
        assert client.delete(f"/api/jobs/{done['id']}").status_code == 404

        # moving the folder back (File Station) restores the job
        kept.rename(settings.jobs_dir / done["id"])
        assert client.get(f"/api/jobs/{done['id']}").json()["job"]["status"] == "needs_review"
        client.delete(f"/api/jobs/{done['id']}")

    # emptied after 30 days
    old = settings.data_dir / "trash" / "jobs" / "old"
    old.mkdir(parents=True)
    (old / ".deleted_at").write_text((datetime.now().astimezone() - timedelta(days=31)).isoformat())
    assert store.purge_trash() == ["old"] and not old.exists() and kept.exists()
    assert store.purge_trash(now=datetime.now().astimezone() + timedelta(days=31)) == [done["id"]]
