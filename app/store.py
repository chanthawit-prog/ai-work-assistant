"""Job storage on the NAS: one folder per job, plain files you can also browse in File Station.

    jobs/<job-id>/
        job.json       status, runs, review ...
        input/         files given by the user (or moved in from the inbox)
        context/       extra material the app copies in before a run
        output/        what Claude writes
        events.jsonl   raw `claude -p --output-format stream-json` events (for debugging)
        steps.jsonl    the same events, summarised for the dashboard timeline
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings

QUEUED = "queued"
RUNNING = "running"
NEEDS_REVIEW = "needs_review"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
STATUSES = (QUEUED, RUNNING, NEEDS_REVIEW, DONE, FAILED, CANCELLED)

AREAS = ("input", "context", "output")
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[a-z]+(?:-[a-z]+)*-[0-9a-f]{4}$")
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Write atomically so a crash never leaves half a file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)  # mkstemp makes owner-only files; File Station and SMB users must be able to read them
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def revision(job: dict) -> int:
    """The document revision in the title block and the Word file: one per Claude run plus one per edit
    you saved yourself on the dashboard."""
    return max(1, len(job.get("runs") or []) + len(job.get("edits") or []))


def safe_filename(name: str) -> str:
    """Keep Thai and other Unicode characters; drop path parts and characters that are unsafe on SMB."""
    name = Path(name.replace("\\", "/")).name
    name = _BAD_CHARS.sub("_", name).strip().lstrip(".").strip()
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 140] + dot + ext) if dot and len(ext) <= 8 else name[:150]
    return name or "file"


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[int, int, dict]] = {}  # path -> (mtime_ns, size, job)

    @property
    def lock(self) -> threading.RLock:
        """Hold it to read a job, act on it and save it without another request changing it in between."""
        return self._lock

    def _read_job_file(self, path: Path) -> dict | None:
        """The dashboard polls often; re-read job.json only when it changed on disk."""
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        cached = self._cache.get(str(path))
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return copy.deepcopy(cached[2])
        data = read_json(path)
        if not isinstance(data, dict):
            return None
        self._cache[str(path)] = (stat.st_mtime_ns, stat.st_size, data)
        return copy.deepcopy(data)

    # -- paths ---------------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id or ""):
            raise KeyError(job_id)
        return self.settings.jobs_dir / job_id

    def area_dir(self, job_id: str, area: str) -> Path:
        if area not in AREAS:
            raise KeyError(area)
        return self.job_dir(job_id) / area

    def file_path(self, job_id: str, area: str, rel: str) -> Path:
        """Resolve a file inside input/ context/ output/ and refuse anything that escapes it."""
        base = self.area_dir(job_id, area).resolve()
        path = (base / rel).resolve()
        if base != path and base not in path.parents:
            raise KeyError(rel)
        return path

    # -- jobs ----------------------------------------------------------------
    def new_job_id(self, type_id: str) -> str:
        return f"{datetime.now():%Y%m%d-%H%M%S}-{type_id}-{secrets.token_hex(2)}"

    def create(
        self,
        type_id: str,
        title: str,
        instructions: str = "",
        params: dict | None = None,
        source: str = "dashboard",
    ) -> dict:
        with self._lock:
            job_id = self.new_job_id(type_id)
            while self.job_dir(job_id).exists():
                job_id = self.new_job_id(type_id)
            for area in AREAS:
                (self.job_dir(job_id) / area).mkdir(parents=True, exist_ok=True)
            job = {
                "id": job_id,
                "type": type_id,
                "title": title.strip() or type_id,
                "instructions": instructions.strip(),
                "params": params or {},
                "source": source,
                "status": QUEUED,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "session_id": None,
                "runs": [],
                "pending_feedback": None,
                "not_before": None,
                "quota_waits": 0,
                "warnings": [],
                "error": None,
                "review": None,
            }
            self.save(job)
            return job

    def get(self, job_id: str) -> dict | None:
        try:
            return self._read_job_file(self.job_dir(job_id) / "job.json")
        except KeyError:
            return None

    def save(self, job: dict) -> None:
        with self._lock:
            job["updated_at"] = now_iso()
            path = self.job_dir(job["id"]) / "job.json"
            write_json(path, job)
            stat = path.stat()
            self._cache[str(path)] = (stat.st_mtime_ns, stat.st_size, copy.deepcopy(job))

    def update(self, job_id: str, **changes: Any) -> dict:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(changes)
            self.save(job)
            return job

    def list(self, limit: int = 300) -> list[dict]:
        jobs_dir = self.settings.jobs_dir
        if not jobs_dir.exists():
            return []
        names = sorted((p.name for p in jobs_dir.iterdir() if JOB_ID_RE.match(p.name)), reverse=True)
        jobs = []
        for name in names[:limit]:
            job = self._read_job_file(jobs_dir / name / "job.json")
            if job:
                jobs.append(job)
        return jobs

    def runnable(self, now: datetime) -> list[dict]:
        """Queued jobs whose quota wait (if any) is over, oldest first."""
        ready = []
        for job in self.list(limit=2000):
            if job.get("status") != QUEUED:
                continue
            not_before = job.get("not_before")
            if not_before and datetime.fromisoformat(not_before) > now:
                continue
            ready.append(job)
        return sorted(ready, key=lambda j: j["id"])

    # -- files ---------------------------------------------------------------
    def _new_path(self, job_id: str, area: str, filename: str) -> Path:
        """A safe, unused name: `ใบเสนอราคา.pdf`, then `ใบเสนอราคา (2).pdf` ..."""
        target_dir = self.area_dir(job_id, area)
        target_dir.mkdir(parents=True, exist_ok=True)
        name = safe_filename(filename)
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        candidate, n = name, 2
        while (target_dir / candidate).exists():
            candidate = f"{stem} ({n}){dot}{ext}" if dot else f"{name} ({n})"
            n += 1
        return target_dir / candidate

    def add_file(self, job_id: str, area: str, filename: str, data: bytes) -> str:
        target = self._new_path(job_id, area, filename)
        target.write_bytes(data)
        return target.name

    def adopt_file(self, job_id: str, area: str, filename: str, source: Path) -> str:
        """Move a file that is already on disk (an upload streamed to a staging folder) into the job."""
        target = self._new_path(job_id, area, filename)
        try:
            os.replace(source, target)
        except OSError:  # staging folder on another file system
            shutil.move(str(source), target)
        return target.name

    def copy_file(self, job_id: str, area: str, filename: str, source: Path) -> str:
        """Copy a file that must stay where it is (another job's output) into this job."""
        target = self._new_path(job_id, area, filename)
        shutil.copy2(source, target)
        return target.name

    def list_files(self, job_id: str, area: str) -> list[dict]:
        base = self.area_dir(job_id, area)
        if not base.exists():
            return []
        files = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                stat = path.stat()
                files.append({"path": path.relative_to(base).as_posix(), "size": stat.st_size,
                              "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")})
        return files

    # -- event log & timeline ------------------------------------------------
    def append_event(self, job_id: str, run: int, event: dict) -> None:
        line = json.dumps({"run": run, "t": now_iso(), "e": event}, ensure_ascii=False)
        with self._lock, open(self.job_dir(job_id) / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def append_step(self, job_id: str, step: dict) -> None:
        step = {"t": now_iso(), **step}
        with self._lock, open(self.job_dir(job_id) / "steps.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(step, ensure_ascii=False) + "\n")

    def read_steps(self, job_id: str, after: int = 0) -> tuple[list[dict], int]:
        path = self.job_dir(job_id) / "steps.jsonl"
        if not path.exists():
            return [], 0
        lines = path.read_text("utf-8").splitlines()
        steps = []
        for line in lines[after:]:
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return steps, len(lines)
