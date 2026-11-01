"""Reviewing a job: your own edits to the result, and approving it, which archives the result in the library
(with a Word copy, ready to send) and, for meetings, adds the action items to the follow-up list."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .config import Settings
from .export_docx import markdown_to_docx
from .jobtypes import JOB_TYPES
from .store import DONE, NEEDS_REVIEW, JobStore, now_iso, read_json, revision, safe_filename
from .tasks import TaskStore

log = logging.getLogger("assistant.review")
# an edit and an approval of the same result never overlap, so the library always gets the version you saw;
# a separate lock from the store's, so the Word conversion never holds up a running job
_review_lock = threading.Lock()
EDITABLE_EXTS = (".md", ".txt")
CHANGED_MESSAGE = ("ไฟล์นี้ถูกแก้ไปแล้วหลังจากคุณเปิดแก้ (จากหน้าต่างอื่นหรือเครื่องอื่น) จึงยังไม่ได้บันทึก "
                   "คัดลอกข้อความที่แก้ไว้เก็บก่อน แล้วกดยกเลิกเพื่อโหลดฉบับล่าสุด")


def file_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _keep_newlines(text: str, old: bytes) -> str:
    """Browsers send line breaks as LF: keep CRLF in a file that had it, so an unchanged file stays unchanged."""
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if b"\r\n" in old else text


def _write_text(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_edit(store: JobStore, job_id: str, rel: str, text: str, base_sha: str = "") -> tuple[dict, bool]:
    """Save your own edit of an output while the job waits for review: no Claude run, no quota.

    `base_sha` is the file's hash when you opened it; if the file changed since, nothing is saved. Each saved
    edit raises the revision, appears in the conversation, and is pointed out to Claude with your next
    message so it works on your version. Returns (job, changed)."""
    path = store.file_path(job_id, "output", rel)
    if path.suffix.lower() not in EDITABLE_EXTS:
        raise ValueError("แก้ไขเองได้เฉพาะไฟล์ .md และ .txt")
    with _review_lock, store.lock:  # the status check and the write happen before a message can start a run
        job = store.get(job_id)
        if job is None or not path.is_file():
            raise KeyError(rel)
        if job["status"] != NEEDS_REVIEW:
            raise ValueError("แก้ไขเองได้เฉพาะงานที่รอตรวจ (ระหว่างที่ Claude ทำงานหรือหลังอนุมัติแล้วแก้ไม่ได้)")
        old = path.read_bytes()
        if base_sha and base_sha != file_sha(old):
            raise ValueError(CHANGED_MESSAGE)
        data = _keep_newlines(text, old).encode("utf-8")
        if data == old:
            return job, False
        _write_text(path, data)
        rel = path.relative_to(store.area_dir(job_id, "output").resolve()).as_posix()
        job["edits"] = list(job.get("edits") or []) + [{"file": rel, "at": now_iso(), "size": len(data)}]
        job["pending_edits"] = sorted(set(job.get("pending_edits") or []) | {rel})
        store.save(job)
        store.append_step(job_id, {"run": len(job.get("runs") or []), "kind": "edit",
                                   "text": f"คุณแก้ไข {rel} เอง · Rev. {revision(job)}"})
    return job, True


def slugify(text: str) -> str:
    slug = re.sub(r"-+", "-", safe_filename(text).replace(" ", "-")).strip("-")
    return slug[:60] or "job"


def approve(store: JobStore, tasks: TaskStore, settings: Settings, job_id: str, note: str = "") -> dict:
    with _review_lock:
        return _approve(store, tasks, settings, job_id, note)


def _approve(store: JobStore, tasks: TaskStore, settings: Settings, job_id: str, note: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] != NEEDS_REVIEW:
        raise ValueError("อนุมัติได้เฉพาะงานที่รอตรวจ")
    job_type = JOB_TYPES[job["type"]]
    job_dir = store.job_dir(job_id)

    archived = []
    source = job_dir / job_type.primary_output
    if source.is_file():
        library = settings.library_dir / job_type.id
        library.mkdir(parents=True, exist_ok=True)
        name = f"{job['created_at'][:10]}_{slugify(job['title'])}_{job_id[-4:]}{source.suffix}"
        shutil.copy2(source, library / name)
        archived.append(f"library/{job_type.id}/{name}")
        if settings.library_docx and source.suffix.lower() == ".md":
            # the same Word file the dashboard's download button makes, so the library (and Synology Drive)
            # holds the finished document; approval never fails because of it
            try:
                modified = datetime.fromtimestamp(source.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                data = markdown_to_docx(source.read_text("utf-8", errors="replace"), title=job["title"],
                                        file_name=source.name, rev=revision(job),
                                        modified=modified, template=settings.report_template,
                                        embed_fonts=settings.docx_embed_fonts)
                (library / name).with_suffix(".docx").write_bytes(data)
                archived.append(f"library/{job_type.id}/{Path(name).with_suffix('.docx').name}")
            except Exception:
                log.exception("Word copy of %s failed", job_id)

    added = 0
    if job_type.id == "meeting-summary":
        added = tasks.add_from_meeting(job, read_json(job_dir / "output" / "action_items.json", default=[]))

    with store.lock:  # a message sent meanwhile has put the job back in the queue: do not undo that
        job = store.get(job_id)
        if job is None or job["status"] != NEEDS_REVIEW:
            raise ValueError("งานนี้เปลี่ยนสถานะไปแล้วระหว่างอนุมัติ ลองโหลดหน้าใหม่")
        job["status"] = DONE
        job["review"] = {"decision": "approved", "at": now_iso(), "note": note.strip(),
                         "archived": archived, "tasks_added": added}
        store.save(job)
    return job
