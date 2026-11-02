"""The follow-up list (data/tasks/action_items.json): action items from approved meeting summaries, and items
you add yourself. You can edit or delete any of them; approving a meeting again never brings back an item you
deleted, nor overwrites one you edited."""
from __future__ import annotations

import re
import threading
from datetime import date
from pathlib import Path

from .store import now_iso, read_json, write_json

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PRIORITIES = ("high", "medium", "low")
FIELDS = ("task", "owner", "due", "priority", "notes", "customer")
MAX_DISMISSED = 500  # meeting items you deleted, remembered so approving the meeting again skips them


def _clean_due(value) -> str | None:
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            date.fromisoformat(value.strip())
            return value.strip()
        except ValueError:
            return None
    return None


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _text(value, limit: int = 500) -> str:
    return str(value).strip()[:limit] if value not in (None, "") else ""


def clean_fields(raw: dict) -> dict:
    """Validate what you typed on the dashboard. Raises ValueError with a message for the form."""
    task = _text(raw.get("task"))
    if not task:
        raise ValueError("กรอกงานที่ต้องทำก่อน")
    due = _clean_due(raw.get("due"))
    if raw.get("due") not in (None, "") and due is None:
        raise ValueError("วันที่กำหนดเสร็จไม่ถูกต้อง (ใช้รูปแบบ ปปปป-ดด-วว เช่น 2026-11-20)")
    priority = _text(raw.get("priority")).lower() or "medium"
    if priority not in PRIORITIES:
        raise ValueError("ความสำคัญต้องเป็น สูง กลาง หรือ ต่ำ")
    return {"task": task, "owner": _text(raw.get("owner"), 120) or None, "due": due, "priority": priority,
            "notes": _text(raw.get("notes"), 1000), "customer": _text(raw.get("customer"), 120) or None}


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
        """Add the action items Claude extracted; ignore malformed entries instead of failing the approval.

        Approving the same meeting again (after a follow-up message) replaces its open items, and skips items
        you already ticked off, edited or deleted, so nothing is duplicated and nothing you changed is lost."""
        with self._lock:
            data = self._load()
            data["items"] = [i for i in data["items"] if not (i.get("source_job") == job["id"]
                                                               and i.get("status") != "done" and not i.get("edited_at"))]
            kept = [i for i in data["items"] if i.get("source_job") == job["id"]]
            skip = {_norm(t) for i in kept for t in (i["task"], i.get("source_task")) if t}
            skip |= {d["task"] for d in data.get("dismissed", []) if d.get("source_job") == job["id"]}
            added = 0
            for raw in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(raw, dict) or not _text(raw.get("task")) or _norm(_text(raw.get("task"))) in skip:
                    continue
                priority = _text(raw.get("priority")).lower()
                data["items"].append({
                    "id": f"A-{data['next_id']:04d}",
                    "task": _text(raw.get("task")),
                    "owner": _text(raw.get("owner"), 120) or None,
                    "due": _clean_due(raw.get("due")),
                    "priority": priority if priority in PRIORITIES else "medium",
                    "notes": _text(raw.get("notes"), 1000),
                    "customer": _text((job.get("params") or {}).get("customer"), 120) or None,
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

    def _find(self, data: dict, item_id: str) -> dict:
        for item in data["items"]:
            if item["id"] == item_id:
                return item
        raise KeyError(item_id)

    def add(self, raw: dict) -> dict:
        """An item you add yourself (not from a meeting)."""
        fields = clean_fields(raw)
        with self._lock:
            data = self._load()
            item = {"id": f"A-{data['next_id']:04d}", **fields, "status": "open", "source_job": None,
                    "source_title": "", "created_at": now_iso(), "done_at": None}
            data["items"].append(item)
            data["next_id"] += 1
            write_json(self.path, data)
            return item

    def update(self, item_id: str, raw: dict) -> dict:
        """Change any of task, owner, due, priority, notes (fields left out stay as they are)."""
        with self._lock:
            data = self._load()
            item = self._find(data, item_id)
            fields = clean_fields({**{k: item.get(k) for k in FIELDS}, **{k: raw[k] for k in FIELDS if k in raw}})
            if all(item.get(k) == v for k, v in fields.items()):
                return item
            if item.get("source_job") and fields["task"] != item["task"] and not item.get("source_task"):
                item["source_task"] = item["task"]  # how the meeting put it, so approving it again skips it
            item.update(fields, edited_at=now_iso())
            write_json(self.path, data)
            return item

    def delete(self, item_id: str) -> dict:
        with self._lock:
            data = self._load()
            item = self._find(data, item_id)
            data["items"].remove(item)
            if item.get("source_job"):
                dismissed = data.setdefault("dismissed", [])
                dismissed += [{"source_job": item["source_job"], "task": _norm(t)}
                              for t in (item["task"], item.get("source_task")) if t]
                data["dismissed"] = dismissed[-MAX_DISMISSED:]
            write_json(self.path, data)
            return item

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
