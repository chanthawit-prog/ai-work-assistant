"""Background helpers: the inbox folder watcher, the daily follow-up schedule and recurring jobs."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import WEEKDAYS, Settings
from .convert import has_readable_input, prepare_inputs
from .schedules import ScheduleStore, create_job, is_due
from .state import RuntimeState
from .store import FAILED, QUEUED, RUNNING, JobStore, safe_filename
from .tasks import TaskStore

log = logging.getLogger("assistant.watchers")

# Synology and Office leave helper files around: @eaDir, #recycle, .DS_Store, ~$lock files
IGNORED_PREFIXES = (".", "@", "#", "~$")
SETTLE_SECONDS = 5


class InboxWatcher:
    """Drop a transcript into DATA_DIR/inbox/meeting/ (SMB, File Station or Synology Drive) and a
    meeting-summary job is created for it. A file is picked up once its size stops changing."""

    def __init__(self, store: JobStore, settings: Settings):
        self.store = store
        self.settings = settings
        self._seen: dict[str, tuple[int, float]] = {}

    @property
    def folder(self) -> Path:
        return self.settings.inbox_dir / "meeting"

    def scan_once(self) -> list[str]:
        created: list[str] = []
        if not self.folder.is_dir():
            return created
        now = time.time()
        present = set()
        for path in sorted(self.folder.iterdir()):
            if not path.is_file() or path.name.startswith(IGNORED_PREFIXES):
                continue
            key, stat = str(path), path.stat()
            present.add(key)
            signature = (stat.st_size, stat.st_mtime)
            if self._seen.get(key) == signature and now - stat.st_mtime >= SETTLE_SECONDS:
                created.append(self._create_job(path))
                self._seen.pop(key, None)
            else:
                self._seen[key] = signature
        for key in list(self._seen):
            if key not in present:
                self._seen.pop(key)
        return created

    def _create_job(self, path: Path) -> str:
        job = self.store.create("meeting-summary", title=path.stem, source="inbox")
        input_dir = self.store.area_dir(job["id"], "input")
        shutil.move(str(path), str(input_dir / safe_filename(path.name)))
        warnings = prepare_inputs(input_dir)
        if not has_readable_input(input_dir):
            self.store.update(job["id"], status=FAILED, warnings=warnings,
                              error="ไม่มีไฟล์ที่ Claude อ่านได้ " + " ".join(warnings))
        elif warnings:
            self.store.update(job["id"], warnings=warnings)
        log.info("inbox: created %s from %s", job["id"], path.name)
        return job["id"]

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.scan_once)  # moving and converting a dropped file must not block the loop
            except Exception:
                log.exception("inbox scan failed")
            await asyncio.sleep(self.settings.inbox_poll_sec)


class FollowupScheduler:
    """Creates one follow-up job per scheduled day (FOLLOWUP_TIME / FOLLOWUP_DAYS) when items are open."""

    def __init__(self, store: JobStore, tasks: TaskStore, state: RuntimeState, settings: Settings):
        self.store = store
        self.tasks = tasks
        self.state = state
        self.settings = settings

    def _due(self, now: datetime) -> bool:
        try:
            hour, minute = (int(x) for x in self.settings.followup_time.split(":"))
        except ValueError:
            return False
        return WEEKDAYS[now.weekday()] in self.settings.followup_days and (now.hour, now.minute) >= (hour, minute)

    def tick(self, now: datetime | None = None) -> str | None:
        now = now or datetime.now()
        today = now.date().isoformat()
        if not self._due(now) or self.state.get("followup_last") == today:
            return None
        self.state.set("followup_last", today)
        if not self.tasks.open_items():
            return None
        job = self.store.create("follow-up", title=f"ติดตามงานประจำวัน {now:%d/%m/%Y}", source="schedule")
        log.info("schedule: created follow-up job %s", job["id"])
        return job["id"]

    async def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("follow-up schedule failed")
            await asyncio.sleep(30)


class RecurringScheduler:
    """Creates the jobs of งานประจำ when they fall due (see app/schedules.py for the rules)."""

    def __init__(self, store: JobStore, schedules: ScheduleStore):
        self.store = store
        self.schedules = schedules

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now().astimezone()
        today = now.date().isoformat()
        created = []
        for schedule in self.schedules.all():
            try:
                due = is_due(schedule, now)
            except (KeyError, TypeError, ValueError):  # a hand-edited entry must not stop the others
                log.warning("schedule %s is malformed, skipped", schedule.get("id"))
                continue
            if not due:
                continue
            last_id = (schedule.get("last_job") or {}).get("job_id")
            last = self.store.get(last_id) if last_id else None
            if last and last.get("status") in (QUEUED, RUNNING):
                self.schedules.mark_run(schedule["id"], today, None, skipped="รอบก่อนยังไม่เสร็จ จึงข้ามรอบนี้")
                log.info("schedule %s skipped: %s is still %s", schedule["id"], last_id, last.get("status"))
                continue
            job = create_job(self.store, schedule, now)
            self.schedules.mark_run(schedule["id"], today, job["id"])
            log.info("schedule %s created job %s", schedule["id"], job["id"])
            created.append(job["id"])
        return created

    async def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("recurring schedule failed")
            await asyncio.sleep(30)
