"""Recurring jobs ("งานประจำ"): the same request sent to Claude on a weekly timetable, for example a
customer pipeline review every Monday 08:30 or a weekly report every Friday 16:00.

Kept in DATA_DIR/config/schedules.json, so they survive rebuilds and are backed up with the data.
A scheduled job is an ordinary job: it waits in the same queue, for the same free RAM and quota.

When a run is due:
* it is due once per scheduled day, at or after its time; if the NAS was off at that time it runs when
  the NAS is back, on the same day only (no pile of missed runs);
* a new or edited schedule never runs for a time that had already passed when it was saved;
* if the schedule's latest job (scheduled or run by hand) is still queued or running, today's run is skipped.

`last_run` is the timetable's bookkeeping (which day ran, or why it was skipped); `last_job` is the newest job
the schedule created, whether on time or with "รันตอนนี้".
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

from .config import WEEKDAYS
from .jobtypes import JOB_TYPES
from .store import JobStore, now_iso, read_json, write_json

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
INCLUDES = {
    "tasks": "รายการติดตามงานที่ยังเปิดอยู่",
    "recent": "ผลงานที่อนุมัติใน 7 วันล่าสุด",
}
MAX_SCHEDULES = 50


def schedulable_types() -> list[str]:
    """Job types that can run unattended: everything that does not need files from you."""
    return [t.id for t in JOB_TYPES.values() if not any(f.kind == "files" and f.required for f in t.fields)]


def run_time(schedule: dict) -> time:
    hour, minute = (int(x) for x in schedule["time"].split(":"))
    return time(hour, minute)


def occurrence(schedule: dict, day, tz) -> datetime | None:
    """The run time on `day`, if `day` is one of the schedule's days."""
    if WEEKDAYS[day.weekday()] not in schedule["days"]:
        return None
    return datetime.combine(day, run_time(schedule), tzinfo=tz)


def is_due(schedule: dict, now: datetime) -> bool:
    if not schedule.get("enabled"):
        return False
    today = occurrence(schedule, now.date(), now.tzinfo)
    if today is None or now < today:
        return False
    if (schedule.get("last_run") or {}).get("date") == now.date().isoformat():
        return False
    return today >= datetime.fromisoformat(schedule["active_from"])


def next_run(schedule: dict, now: datetime) -> datetime | None:
    if not schedule.get("enabled"):
        return None
    if is_due(schedule, now):
        return now
    active_from = datetime.fromisoformat(schedule["active_from"])
    for offset in range(0, 8):
        when = occurrence(schedule, (now + timedelta(days=offset)).date(), now.tzinfo)
        if when is None or when <= now or when < active_from:
            continue
        if offset == 0 and (schedule.get("last_run") or {}).get("date") == now.date().isoformat():
            continue
        return when
    return None


def create_job(store: JobStore, schedule: dict, now: datetime) -> dict:
    params = {"schedule_id": schedule["id"], "schedule_name": schedule["name"],
              "include": list(schedule.get("include") or [])}
    if schedule.get("customer"):
        params["customer"] = schedule["customer"]
    title = f"{schedule['name']} · {now:%d/%m/%Y}"
    return store.create(schedule["type"], title, schedule.get("instructions", ""), params, source="schedule")


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> dict:
        data = read_json(self.path, default=None)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = {"next_id": 1, "items": []}
        return data

    def all(self) -> list[dict]:
        return self._load()["items"]

    def get(self, schedule_id: str) -> dict | None:
        return next((s for s in self.all() if s.get("id") == schedule_id), None)

    @staticmethod
    def clean(data: dict) -> dict:
        """Check what the form sent; raise ValueError with a message the dashboard can show."""
        name = str(data.get("name") or "").strip()[:120]
        type_id = str(data.get("type") or "")
        instructions = str(data.get("instructions") or "").strip()[:8000]
        customer = str(data.get("customer") or "").strip()[:120]
        days = [d for d in WEEKDAYS if d in {str(x).lower()[:3] for x in data.get("days") or []}]
        clock = str(data.get("time") or "").strip()
        include = [k for k in INCLUDES if k in set(data.get("include") or [])]
        if not name:
            raise ValueError("ตั้งชื่องานประจำก่อน")
        if type_id not in schedulable_types():
            raise ValueError("งานชนิดนี้ตั้งเป็นงานประจำไม่ได้ (งานที่ต้องแนบไฟล์ทุกครั้ง เช่น สรุปประชุม)")
        job_type = JOB_TYPES[type_id]
        values = {"instructions": instructions, "customer": customer, "title": name}
        for field in job_type.fields:
            if field.required and field.kind != "files" and not values.get(field.name):
                raise ValueError(f"กรุณากรอก: {field.label}")
        if not days:
            raise ValueError("เลือกวันอย่างน้อย 1 วัน")
        match = TIME_RE.match(clock)
        if not match:
            raise ValueError("เวลาไม่ถูกต้อง ใช้รูปแบบ 08:30")
        return {"name": name, "type": type_id, "instructions": instructions,
                "customer": customer if any(f.name == "customer" for f in job_type.fields) else "",
                "days": days, "time": f"{int(match.group(1)):02d}:{match.group(2)}", "include": include,
                "enabled": bool(data.get("enabled", True))}

    def create(self, data: dict) -> dict:
        values = self.clean(data)
        with self._lock:
            store = self._load()
            if len(store["items"]) >= MAX_SCHEDULES:
                raise ValueError(f"ตั้งงานประจำได้สูงสุด {MAX_SCHEDULES} รายการ")
            schedule = {"id": f"S-{store['next_id']:04d}", **values, "created_at": now_iso(),
                        "updated_at": now_iso(), "active_from": now_iso(), "last_run": None, "last_job": None}
            store["next_id"] += 1
            store["items"].append(schedule)
            write_json(self.path, store)
            return schedule

    def update(self, schedule_id: str, data: dict) -> dict:
        values = self.clean(data)
        with self._lock:
            store = self._load()
            schedule = next((s for s in store["items"] if s.get("id") == schedule_id), None)
            if schedule is None:
                raise KeyError(schedule_id)
            # a time that has already passed when you save must not fire right away
            schedule.update(values, updated_at=now_iso(), active_from=now_iso())
            write_json(self.path, store)
            return schedule

    def delete(self, schedule_id: str) -> None:
        with self._lock:
            store = self._load()
            items = [s for s in store["items"] if s.get("id") != schedule_id]
            if len(items) == len(store["items"]):
                raise KeyError(schedule_id)
            store["items"] = items
            write_json(self.path, store)

    def _set(self, schedule_id: str, **values) -> None:
        with self._lock:
            store = self._load()
            for schedule in store["items"]:
                if schedule.get("id") == schedule_id:
                    schedule.update(values)
            write_json(self.path, store)

    def mark_run(self, schedule_id: str, day: str, job_id: str | None, skipped: str | None = None) -> None:
        """Today's occurrence is dealt with: a job was created, or it was skipped (and why)."""
        values = {"last_run": {"date": day, "at": now_iso(), "skipped": skipped}}
        if job_id:
            values["last_job"] = {"job_id": job_id, "at": now_iso(), "manual": False}
        self._set(schedule_id, **values)

    def mark_manual(self, schedule_id: str, job_id: str) -> None:
        """A job made with "รันตอนนี้": shown as the latest job, but the timetable is left alone."""
        self._set(schedule_id, last_job={"job_id": job_id, "at": now_iso(), "manual": True})
