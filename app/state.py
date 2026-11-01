"""Small runtime state kept in DATA_DIR/state/runtime.json (Claude login check, last quota event, scheduler)."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .store import now_iso, read_json, write_json


class RuntimeState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            data = read_json(self.path, default={})
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._load()[key] = value
            write_json(self.path, self._data)

    def set_quota(self, info: dict) -> None:
        """Remember the latest subscription rate-limit status reported by Claude Code."""
        self.set("quota", {**info, "seen_at": now_iso()})
