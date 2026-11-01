"""Runtime settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    knowledge_dir: Path
    claude_bin: str = "claude"
    claude_model: str = ""
    claude_effort: str = ""
    claude_extra_args: tuple[str, ...] = ()
    max_concurrent_jobs: int = 1
    job_timeout_min: int = 45
    inbox_poll_sec: int = 15
    followup_time: str = "08:00"
    followup_days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")
    allow_python: bool = True
    dashboard_user: str = "admin"
    dashboard_password: str = ""

    # Folders inside DATA_DIR -------------------------------------------------
    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    @property
    def tasks_file(self) -> Path:
        return self.data_dir / "tasks" / "action_items.json"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def prompt_override_dir(self) -> Path:
        return self.data_dir / "config" / "prompts"

    def ensure_dirs(self) -> None:
        for path in (
            self.jobs_dir,
            self.inbox_dir / "meeting",
            self.library_dir,
            self.tasks_file.parent,
            self.state_dir,
            self.prompt_override_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        days = tuple(
            d for d in (x.strip().lower()[:3] for x in os.environ.get("FOLLOWUP_DAYS", "mon,tue,wed,thu,fri").split(","))
            if d in WEEKDAYS
        )
        return cls(
            data_dir=data_dir,
            knowledge_dir=Path(os.environ.get("KNOWLEDGE_DIR", str(data_dir / "knowledge"))),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude").strip() or "claude",
            claude_model=os.environ.get("CLAUDE_MODEL", "").strip(),
            claude_effort=os.environ.get("CLAUDE_EFFORT", "").strip(),
            claude_extra_args=tuple(shlex.split(os.environ.get("CLAUDE_EXTRA_ARGS", ""))),
            max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 1),
            job_timeout_min=_int("JOB_TIMEOUT_MIN", 45),
            inbox_poll_sec=_int("INBOX_POLL_SEC", 15),
            followup_time=os.environ.get("FOLLOWUP_TIME", "08:00").strip(),
            followup_days=days,
            allow_python=_bool("ALLOW_PYTHON", True),
            dashboard_user=os.environ.get("DASHBOARD_USER", "admin").strip() or "admin",
            dashboard_password=os.environ.get("DASHBOARD_PASSWORD", ""),
        )
