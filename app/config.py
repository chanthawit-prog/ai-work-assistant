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
    digest_time: str = ""                  # morning summary through the notification channels ("" = off), e.g. 07:30
    allow_python: bool = True
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    dashboard_url: str = ""                 # used for links in notifications, e.g. http://nas.local:8080
    notify_synology_url: str = ""
    notify_line_token: str = ""
    notify_line_to: str = ""
    notify_line_api: str = "https://api.line.me/v2/bot/message/push"
    notify_webhook_url: str = ""
    notify_on: tuple[str, ...] = ("needs_review", "failed")
    # memory: a DS925+ with 6 GB fills its swap easily, so jobs wait for free RAM and the CLI runs lean
    min_free_mem_mb: int = 700              # start a job only when the NAS has this much RAM available (0 = off)
    mem_wait_max_min: int = 20              # ... but after waiting this long, start anyway
    claude_low_memory: bool = True          # BUN_OPTIONS=--smol, MALLOC_ARENA_MAX=2, no non-essential traffic
    max_upload_mb: int = 200                # per request; uploads are streamed to disk, never held in RAM
    docx_embed_fonts: bool = True           # embed Anuphan in Word exports
    library_docx: bool = True               # approving also saves a Word copy of the result in the library
    kb_enabled: bool = True                 # full-text index of knowledge/ (PDF, Word, text) and approved outputs
    kb_refresh_min: int = 30                # how often to look for new or changed files (only while idle)

    @property
    def report_template(self) -> Path:
        return self.knowledge_dir / "templates" / "report-template.docx"

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
            digest_time=os.environ.get("DIGEST_TIME", "").strip(),
            allow_python=_bool("ALLOW_PYTHON", True),
            dashboard_user=os.environ.get("DASHBOARD_USER", "admin").strip() or "admin",
            dashboard_password=os.environ.get("DASHBOARD_PASSWORD", ""),
            dashboard_url=os.environ.get("DASHBOARD_URL", "").strip(),
            notify_synology_url=os.environ.get("NOTIFY_SYNOLOGY_CHAT_URL", "").strip(),
            notify_line_token=os.environ.get("NOTIFY_LINE_TOKEN", "").strip(),
            notify_line_to=os.environ.get("NOTIFY_LINE_TO", "").strip(),
            notify_line_api=os.environ.get("NOTIFY_LINE_API", "").strip() or "https://api.line.me/v2/bot/message/push",
            notify_webhook_url=os.environ.get("NOTIFY_WEBHOOK_URL", "").strip(),
            notify_on=tuple(x.strip() for x in os.environ.get("NOTIFY_ON", "needs_review,failed").split(",") if x.strip()),
            min_free_mem_mb=_int("MIN_FREE_MEM_MB", 700, minimum=0),
            mem_wait_max_min=_int("MEM_WAIT_MAX_MIN", 20),
            claude_low_memory=_bool("CLAUDE_LOW_MEMORY", True),
            max_upload_mb=_int("MAX_UPLOAD_MB", 200),
            docx_embed_fonts=_bool("DOCX_EMBED_FONTS", True),
            library_docx=_bool("LIBRARY_DOCX", True),
            kb_enabled=_bool("KB_SEARCH", True),
            kb_refresh_min=_int("KB_REFRESH_MIN", 30),
        )
