"""Keeps the knowledge search index (tools/kb_search.py) current.

What is indexed: KNOWLEDGE_DIR (design guides, standards, price lists ... as PDF, Word, Markdown, CSV) and the
library of approved outputs. The index lives in DATA_DIR/state/kb.sqlite.

When: a minute after start, then every KB_REFRESH_MIN minutes, and only while no job is running and the NAS has
MIN_FREE_MEM_MB of RAM available. It pauses between files the moment a job starts, so on a small NAS indexing
never competes with Claude; unchanged files are skipped, so a refresh with nothing new takes a moment.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from . import sysmon
from .config import Settings
from .store import now_iso

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
import kb_search  # noqa: E402  (tools/ is plain scripts, shared with Claude's own python3 runs)
import ocr_text  # noqa: E402

log = logging.getLogger("assistant.knowledge")
FIRST_DELAY = 60  # seconds after start, so the dashboard and any waiting job come first


def ocr_config(settings: Settings) -> ocr_text.OcrConfig:
    """How scanned pages are read, for the index and for job inputs; the cache is shared by both."""
    return ocr_text.OcrConfig(enabled=settings.ocr_enabled, langs=settings.ocr_langs, max_pages=settings.ocr_max_pages,
                              cache_dir=settings.state_dir / "ocr")


def ocr_status(settings: Settings) -> dict:
    """For the System page and the readiness check: whether OCR can run here, and with what."""
    config = ocr_config(settings)
    ok, why = ocr_text.usable(config)
    info = ocr_text.tools()
    return {"enabled": settings.ocr_enabled, "usable": ok, "reason": why, "installed": info["tesseract"],
            "version": info["version"], "installed_langs": info["langs"], "langs": settings.ocr_langs,
            "max_pages": settings.ocr_max_pages}


class KnowledgeIndex:
    def __init__(self, settings: Settings, runner=None):
        self.settings = settings
        self.runner = runner
        self.path = settings.state_dir / "kb.sqlite"
        self.running = False
        self.last: dict | None = None  # outcome of the last refresh
        self._wake = asyncio.Event()

    @property
    def roots(self) -> dict[str, Path]:
        return {"knowledge": self.settings.knowledge_dir, "library": self.settings.library_dir}

    def busy(self) -> bool:
        """True while a job runs or RAM is short: the indexer stops between files and tries again later."""
        if self.runner is not None and self.runner.active:
            return True
        need = self.settings.min_free_mem_mb
        host = sysmon.host_memory() if need else None
        return bool(host and host["available_mb"] < need)

    def status(self) -> dict:
        return kb_search.status(self.path)

    def ready(self) -> bool:
        """Worth telling Claude about: the index exists and holds at least one file."""
        if not self.settings.kb_enabled:
            return False
        try:
            return kb_search.status(self.path)["files"] > 0
        except Exception:  # a damaged index must never stop a job
            log.exception("knowledge index unreadable")
            return False

    async def refresh(self) -> dict | None:
        if self.running or not self.settings.kb_enabled or self.busy():
            return None
        self.running = True
        try:
            stats = await asyncio.to_thread(kb_search.refresh, self.path, self.roots, self.busy,
                                            ocr_config(self.settings))
            self.last = {**stats, "at": now_iso()}
            if stats["added"] or stats["updated"] or stats["removed"]:
                log.info("knowledge index: %s", stats)
            return self.last
        finally:
            self.running = False

    def request(self) -> None:
        """Refresh as soon as the system is idle (the dashboard's "update now" button)."""
        self._wake.set()

    async def loop(self) -> None:
        try:  # the first refresh waits a minute, unless "update now" is pressed before that
            await asyncio.wait_for(self._wake.wait(), timeout=FIRST_DELAY)
        except asyncio.TimeoutError:
            pass
        while True:
            try:
                done = await self.refresh()
                if done is not None and not done.get("stopped"):
                    self._wake.clear()  # a requested refresh has happened
            except Exception:
                log.exception("knowledge index refresh failed")
            if self._wake.is_set():  # requested, but a job is running or RAM is short: try again soon
                await asyncio.sleep(30)
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.kb_refresh_min * 60)
            except asyncio.TimeoutError:
                pass
