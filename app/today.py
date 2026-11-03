"""วันนี้: what needs you today, worked out by the app itself (no Claude, no quota).

The home page shows it at the top. With DIGEST_TIME set, the same summary also goes out every morning on
FOLLOWUP_DAYS through the notification channels (Synology Chat, LINE, webhook), when there is something to report.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from .config import WEEKDAYS, Settings
from .jobtypes import JOB_TYPES
from .prompts import THAI_DAYS
from .schedules import ScheduleStore, occurrence
from .state import RuntimeState
from .store import NEEDS_REVIEW, QUEUED, RUNNING, JobStore
from .tasks import TaskStore

log = logging.getLogger("assistant.today")
SHOWN = 5  # items per list; the counts cover the rest
LATE_WINDOW = timedelta(hours=3)  # after a NAS restart, a summary hours after DIGEST_TIME is no longer a morning one
THAI_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def _due(item: dict) -> date | None:
    try:
        return date.fromisoformat(item["due"]) if item.get("due") else None
    except ValueError:
        return None


def _compact(item: dict, today: date) -> dict:
    due = _due(item)
    return {k: item.get(k) for k in ("id", "task", "owner", "due", "priority", "customer", "source_job")} | {
        "days": (due - today).days if due else None}


def summary(store: JobStore, tasks: TaskStore, schedules: ScheduleStore, settings: Settings,
            now: datetime | None = None, followup_last: str | None = None) -> dict:
    now = now or datetime.now().astimezone()
    today = now.date()
    jobs = store.list(limit=300)
    waiting = [{"id": j["id"], "title": j["title"], "type": j["type"],
                "type_label": JOB_TYPES[j["type"]].label if j["type"] in JOB_TYPES else j["type"]}
               for j in jobs if j.get("status") == NEEDS_REVIEW]
    items = [i for i in tasks.open_items() if _due(i)]
    by_due = sorted(items, key=lambda i: (_due(i), i["id"]))
    overdue = [i for i in by_due if _due(i) < today]
    due_today = [i for i in by_due if _due(i) == today]
    week = [i for i in by_due if today < _due(i) <= today + timedelta(days=7)]

    runs = []
    for s in schedules.all():
        try:
            when = occurrence(s, today, now.tzinfo) if s.get("enabled") else None
            started = when is not None and when >= datetime.fromisoformat(s["active_from"])
        except (KeyError, TypeError, ValueError):  # a hand-edited entry must not break the home page
            continue
        if started:
            last = s.get("last_run") or {}
            ran_today = last.get("date") == today.isoformat()
            runs.append({"name": s.get("name") or s["id"], "time": f"{when:%H:%M}", "schedule_id": s["id"],
                         "done": ran_today and not last.get("skipped"), "skipped": bool(ran_today and last.get("skipped"))})
    if settings.followup_time and WEEKDAYS[today.weekday()] in settings.followup_days and tasks.open_items():
        runs.append({"name": "ติดตามงานอัตโนมัติ (ร่างข้อความทวงงาน)", "time": settings.followup_time,
                     "schedule_id": None, "done": followup_last == today.isoformat(), "skipped": False})
    runs.sort(key=lambda r: r["time"])

    return {
        "date": today.isoformat(),
        "label": f"วัน{THAI_DAYS[today.weekday()]} {today.day} {THAI_MONTHS[today.month - 1]}",
        "waiting": waiting[:SHOWN], "waiting_count": len(waiting),
        "running": sum(j.get("status") == RUNNING for j in jobs), "queued": sum(j.get("status") == QUEUED for j in jobs),
        "overdue": [_compact(i, today) for i in overdue[:SHOWN]], "overdue_count": len(overdue),
        "due_today": [_compact(i, today) for i in due_today[:SHOWN]], "due_today_count": len(due_today),
        "week": [_compact(i, today) for i in week[:SHOWN]], "week_count": len(week),
        "runs": runs,
    }


def _line(item: dict) -> str:
    who = f" ({item['owner']})" if item.get("owner") else ""
    customer = f" · ลูกค้า {item['customer']}" if item.get("customer") else ""
    late = f" · เลยมา {-item['days']} วัน" if item.get("days") is not None and item["days"] < 0 else ""
    return f"• {item['id']} {item['task']}{who}{customer}{late}"


def digest_text(s: dict, settings: Settings) -> str | None:
    """The morning message, or None when there is nothing to report."""
    parts = []
    if s["overdue_count"]:
        parts += [f"เลยกำหนด {s['overdue_count']} รายการ", *[_line(i) for i in s["overdue"][:3]]]
    if s["due_today_count"]:
        parts += [f"ครบกำหนดวันนี้ {s['due_today_count']} รายการ", *[_line(i) for i in s["due_today"][:3]]]
    if s["waiting_count"]:
        parts += [f"รอคุณตรวจ {s['waiting_count']} งาน", *[f"• {w['title']}" for w in s["waiting"][:3]]]
    pending = [r for r in s["runs"] if not r["done"] and not r.get("skipped")]
    if pending:
        parts.append("งานประจำวันนี้: " + ", ".join(f"{r['time']} {r['name']}" for r in pending))
    if not parts:
        return None
    if s["week_count"]:
        parts.append(f"อีก 7 วันครบกำหนดอีก {s['week_count']} รายการ")
    link = [f"เปิดดู: {settings.dashboard_url.rstrip('/')}/#/"] if settings.dashboard_url else []
    return "\n".join([f"สรุปงาน{s['label']}", *parts, *link])


class DigestScheduler:
    """Sends the morning summary at DIGEST_TIME on FOLLOWUP_DAYS, once a day, when a channel is set up."""

    def __init__(self, store: JobStore, tasks: TaskStore, schedules: ScheduleStore, state: RuntimeState,
                 settings: Settings, notifier):
        self.store, self.tasks, self.schedules, self.state = store, tasks, schedules, state
        self.settings, self.notifier = settings, notifier

    def _due(self, now: datetime) -> bool:
        if not self.settings.digest_time or not self.notifier.channels:
            return False
        try:
            hour, minute = (int(x) for x in self.settings.digest_time.split(":"))
            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:  # DIGEST_TIME is not a time of day: never due (the readiness check reports it)
            return False
        return (WEEKDAYS[now.weekday()] in self.settings.followup_days and start <= now < start + LATE_WINDOW
                and self.state.get("digest_last") != now.date().isoformat())

    def build(self, now: datetime | None = None) -> str | None:
        s = summary(self.store, self.tasks, self.schedules, self.settings, now, self.state.get("followup_last"))
        return digest_text(s, self.settings)

    async def tick(self, now: datetime | None = None) -> str | None:
        now = now or datetime.now().astimezone()
        if not self._due(now):
            return None
        self.state.set("digest_last", now.date().isoformat())  # once a day, even if sending fails
        text = self.build(now)
        if text:
            await self.notifier.send(text, {"event": "digest"})
        return text

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("morning summary failed")
            await asyncio.sleep(30)
