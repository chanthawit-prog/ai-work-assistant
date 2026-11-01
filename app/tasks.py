"""Action items collected from approved meeting summaries (data/tasks/action_items.json)."""
from __future__ import annotations

import re
import threading
from datetime import date
from pathlib import Path

from .store import now_iso, read_json, write_json

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PRIORITIES = ("high", "medium", "low")


def _clean_due(value) -> str | None:
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            date.fromisoformat(value.strip())
            return value.strip()
        except ValueError:
            return None
    return None


def _text(value, limit: int = 500) -> str:
    return str(value).strip()[:limit] if value not in (None, "") else ""


class TaskStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> dict:
        data = read_json(self.path, default=None)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = {"next_id": 1, "items": []}
        return data

    def all(self, include_done: bool = True) -> list[dict]:
        items = self._load()["items"]
        return items if include_done else [i for i in items if i.get("status") != "done"]

    def open_items(self) -> list[dict]:
        return self.all(include_done=False)

    def add_from_meeting(self, job: dict, raw_items: list) -> int:
        """Add the action items Claude extracted; ignore malformed entries instead of failing the approval."""
        with self._lock:
            data = self._load()
            added = 0
            for raw in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(raw, dict) or not _text(raw.get("task")):
                    continue
                priority = _text(raw.get("priority")).lower()
                data["items"].append({
                    "id": f"A-{data['next_id']:04d}",
                    "task": _text(raw.get("task")),
                    "owner": _text(raw.get("owner"), 120) or None,
                    "due": _clean_due(raw.get("due")),
                    "priority": priority if priority in PRIORITIES else "medium",
                    "notes": _text(raw.get("notes"), 1000),
                    "status": "open",
                    "source_job": job["id"],
                    "source_title": job.get("title", ""),
                    "created_at": now_iso(),
                    "done_at": None,
                })
                data["next_id"] += 1
                added += 1
            write_json(self.path, data)
            return added

    def set_status(self, item_id: str, status: str) -> dict:
        if status not in ("open", "done"):
            raise ValueError(status)
        with self._lock:
            data = self._load()
            for item in data["items"]:
                if item["id"] == item_id:
                    item["status"] = status
                    item["done_at"] = now_iso() if status == "done" else None
                    write_json(self.path, data)
                    return item
            raise KeyError(item_id)
