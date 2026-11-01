"""Recurring jobs (งานประจำ), and the calc toolkit reaching Claude's environment."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.jobtypes import JOB_TYPES
from app.main import create_app
from app.prompts import task_message
from app.runner import TOOLS_DIR
from app.schedules import ScheduleStore, is_due, next_run, schedulable_types
from app.store import NEEDS_REVIEW, QUEUED
from app.watchers import RecurringScheduler

WEEKLY = {"name": "สรุปงานประจำสัปดาห์", "type": "general", "instructions": "สรุปงานที่เสร็จและงานค้าง",
          "days": ["fri"], "time": "16:00", "include": ["tasks", "recent"]}


def local(*args):
    return datetime(*args).astimezone()


def next_weekday(start: datetime, weekday: int) -> datetime:
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def schedule_with(**changes):
    base = {"enabled": True, "days": ["fri"], "time": "16:00", "last_run": None,
            "active_from": local(2026, 11, 2, 9, 0).isoformat()}  # Monday
    return {**base, **changes}


# ---------------------------------------------------------------- timing rules
def test_due_once_on_its_day_at_or_after_its_time():
    s = schedule_with()
    friday = next_weekday(local(2026, 11, 2, 16, 0), 4)
    assert not is_due(s, friday - timedelta(minutes=1))
    assert is_due(s, friday) and is_due(s, friday + timedelta(hours=3))  # NAS back later the same day
    assert not is_due({**s, "last_run": {"date": friday.date().isoformat()}}, friday + timedelta(hours=1))
    assert not is_due(s, friday + timedelta(days=1))  # Saturday: no catching up on missed days
    assert not is_due({**s, "enabled": False}, friday)


def test_saving_after_todays_time_waits_for_the_next_occurrence():
    friday_evening = next_weekday(local(2026, 11, 2, 18, 0), 4)
    s = schedule_with(active_from=friday_evening.isoformat())
    assert not is_due(s, friday_evening)
    upcoming = next_run(s, friday_evening)
    assert upcoming.date() == (friday_evening + timedelta(days=7)).date() and upcoming.hour == 16


def test_next_run_skips_today_after_it_ran_and_handles_weekdays():
    monday = local(2026, 11, 2, 7, 0)
    daily = schedule_with(days=["mon", "tue", "wed", "thu", "fri"], time="08:00",
                          active_from=local(2026, 11, 1, 9, 0).isoformat())
    assert next_run(daily, monday) == local(2026, 11, 2, 8, 0)
    ran = {**daily, "last_run": {"date": "2026-11-02"}}
    assert next_run(ran, local(2026, 11, 2, 9, 0)) == local(2026, 11, 3, 8, 0)
    assert next_run({**daily, "enabled": False}, monday) is None


# ---------------------------------------------------------------- store
def test_store_validates_and_keeps_only_known_fields(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json")
    s = store.create({**WEEKLY, "days": ["Friday"], "time": "7:05", "include": ["tasks", "everything"], "customer": "x"})
    assert s["id"] == "S-0001" and s["days"] == ["fri"] and s["time"] == "07:05"
    assert s["include"] == ["tasks"] and s["customer"] == ""  # general jobs have no customer field
    for bad in ({"name": ""}, {"days": []}, {"time": "25:00"}, {"type": "meeting-summary"},
                {"type": "sales-brief", "customer": ""}, {"type": "general", "instructions": ""}):
        with pytest.raises(ValueError):
            store.create({**WEEKLY, **bad})
    assert "meeting-summary" not in schedulable_types() and "follow-up" in schedulable_types()
    updated = store.update("S-0001", {**WEEKLY, "enabled": False})
    assert updated["enabled"] is False and updated["active_from"] >= s["active_from"]
    store.delete("S-0001")
    assert store.all() == []
    with pytest.raises(KeyError):
        store.delete("S-0001")


# ---------------------------------------------------------------- scheduler
def test_scheduler_creates_one_job_and_skips_while_the_last_is_unfinished(parts, settings):
    store, *_ = parts
    schedules = ScheduleStore(settings.data_dir / "config" / "schedules.json")
    s = schedules.create(WEEKLY)
    friday = next_weekday(datetime.now().astimezone() + timedelta(days=1), 4).replace(hour=16, minute=5, second=0, microsecond=0)
    recurring = RecurringScheduler(store, schedules)

    created = recurring.tick(friday)
    assert len(created) == 1 and recurring.tick(friday + timedelta(minutes=30)) == []
    job = store.get(created[0])
    assert job["source"] == "schedule" and job["status"] == QUEUED and job["instructions"] == WEEKLY["instructions"]
    assert job["params"]["include"] == ["tasks", "recent"] and job["params"]["schedule_id"] == s["id"]
    assert job["title"] == f"{WEEKLY['name']} · {friday:%d/%m/%Y}"

    week_later = friday + timedelta(days=7)
    assert recurring.tick(week_later) == []  # last week's job never ran (still queued)
    stored = schedules.get(s["id"])
    assert stored["last_run"]["skipped"] and stored["last_run"]["date"] == week_later.date().isoformat()
    assert stored["last_job"]["job_id"] == created[0] and stored["last_job"]["manual"] is False

    store.update(created[0], status=NEEDS_REVIEW)
    assert len(recurring.tick(week_later + timedelta(days=7))) == 1


def test_scheduled_job_gets_tasks_and_recent_outputs_as_context(parts, settings):
    store, tasks, _state, runner = parts
    tasks.add_from_meeting({"id": "m1", "title": "ประชุม ABC"}, [{"task": "ส่งแบบ MDB", "owner": "สมชาย", "due": "2026-11-06"}])
    library = settings.library_dir / "meeting-summary"
    library.mkdir(parents=True)
    (library / "new.md").write_text("# สรุปประชุมล่าสุด", encoding="utf-8")
    old = library / "old.md"
    old.write_text("# เก่า", encoding="utf-8")
    ten_days = time.time() - 10 * 86400
    os.utime(old, (ten_days, ten_days))
    job = store.create("general", "สรุปงานประจำสัปดาห์ · 06/11/2026", "สรุปงาน",
                       {"include": ["tasks", "recent"], "schedule_id": "S-0001", "schedule_name": "สรุปงานประจำสัปดาห์"},
                       source="schedule")
    context = runner.prepare_context(job, JOB_TYPES["general"])
    assert context == ["action_items.json", "recent/meeting-summary/new.md"]
    items = json.loads((store.area_dir(job["id"], "context") / "action_items.json").read_text("utf-8"))
    assert items[0]["task"] == "ส่งแบบ MDB"
    message = task_message(store.get(job["id"]), JOB_TYPES["general"], settings, [], context)
    assert 'งานประจำ "สรุปงานประจำสัปดาห์"' in message and "context/recent/meeting-summary/new.md" in message
    assert "context/recent/ คือผลงานที่ผู้ใช้ตรวจและอนุมัติแล้ว" in message and "context/action_items.json คือ" in message


# ---------------------------------------------------------------- API
def test_schedule_api_round_trip(settings):
    with TestClient(create_app(settings, start_background=False)) as client:
        res = client.post("/api/schedules", json=WEEKLY)
        assert res.status_code == 200, res.text
        s = res.json()
        assert s["id"] == "S-0001" and s["next_run"] and s["type_label"] == "งานทั่วไป"
        bad = client.post("/api/schedules", json={**WEEKLY, "days": []})
        assert bad.status_code == 409 and "เลือกวัน" in bad.json()["detail"]
        listing = client.get("/api/schedules").json()
        assert [x["id"] for x in listing["items"]] == ["S-0001"] and "general" in listing["types"]
        assert listing["followup"]["time"] == "08:00" and set(listing["includes"]) == {"tasks", "recent"}

        off = client.put("/api/schedules/S-0001", json={**WEEKLY, "enabled": False}).json()
        assert off["enabled"] is False and off["next_run"] is None
        job = client.post("/api/schedules/S-0001/run").json()
        assert job["source"] == "schedule" and job["status"] == "queued"
        item = client.get("/api/schedules").json()["items"][0]
        assert item["last_run"] is None  # run-now leaves the timetable alone ...
        assert item["last_job"]["id"] == job["id"] and item["last_job_info"]["manual"] is True  # ... but shows the job

        assert client.delete("/api/schedules/S-0001").json() == {"ok": True}
        assert client.delete("/api/schedules/S-0001").status_code == 404
        assert client.post("/api/schedules/S-9999/run").status_code == 404


# ---------------------------------------------------------------- calc toolkit reaches Claude
def test_engineering_jobs_get_pec_calc_on_pythonpath(parts, settings, tmp_path, monkeypatch):
    store, _tasks, _state, runner = parts
    dump = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLAUDE_DUMP_ENV", str(dump))
    eng = store.create("eng-qa", "เลือกสายมอเตอร์", "มอเตอร์ 30 kW")
    asyncio.run(runner.run_job(eng["id"]))
    env = json.loads(dump.read_text("utf-8"))
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(TOOLS_DIR) and (TOOLS_DIR / "pec_calc.py").is_file()
    assert env["PEC_CALC_KNOWLEDGE"] == str(settings.knowledge_dir)
    general = store.create("general", "สรุป", "สรุปเอกสาร")
    asyncio.run(runner.run_job(general["id"]))
    env = json.loads(dump.read_text("utf-8"))
    assert str(TOOLS_DIR) not in env.get("PYTHONPATH", "") and "PEC_CALC_KNOWLEDGE" not in env


def test_unfinished_manual_run_also_skips_the_timetable(parts, settings):
    store, *_ = parts
    schedules = ScheduleStore(settings.data_dir / "config" / "schedules.json")
    s = schedules.create(WEEKLY)
    manual = store.create("general", "สรุปงานประจำสัปดาห์ · สั่งเอง", "x", source="schedule")
    schedules.mark_manual(s["id"], manual["id"])
    friday = next_weekday(datetime.now().astimezone() + timedelta(days=1), 4).replace(hour=16, minute=1, second=0, microsecond=0)
    recurring = RecurringScheduler(store, schedules)
    assert recurring.tick(friday) == [] and schedules.get(s["id"])["last_run"]["skipped"]
    store.update(manual["id"], status=NEEDS_REVIEW)
    assert len(recurring.tick(friday + timedelta(days=7))) == 1


def test_a_broken_entry_does_not_stop_the_others(parts, settings):
    store, *_ = parts
    schedules = ScheduleStore(settings.data_dir / "config" / "schedules.json")
    schedules.create(WEEKLY)
    data = json.loads(schedules.path.read_text("utf-8"))
    data["items"].insert(0, {"id": "S-9999", "enabled": True, "days": ["fri"], "time": "25:99", "active_from": "x"})
    schedules.path.write_text(json.dumps(data), encoding="utf-8")
    friday = next_weekday(datetime.now().astimezone() + timedelta(days=1), 4).replace(hour=16, minute=1, second=0, microsecond=0)
    assert len(RecurringScheduler(store, schedules).tick(friday)) == 1
