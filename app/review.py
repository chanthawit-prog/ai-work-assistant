"""Approving a job: archive the result in the library (with a Word copy, ready to send) and, for meetings,
add the action items to the follow-up list."""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import Settings
from .export_docx import markdown_to_docx
from .jobtypes import JOB_TYPES
from .store import DONE, NEEDS_REVIEW, JobStore, now_iso, read_json, safe_filename
from .tasks import TaskStore

log = logging.getLogger("assistant.review")

def slugify(text: str) -> str:
    slug = re.sub(r"-+", "-", safe_filename(text).replace(" ", "-")).strip("-")
    return slug[:60] or "job"


def approve(store: JobStore, tasks: TaskStore, settings: Settings, job_id: str, note: str = "") -> dict:
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
                                        file_name=source.name, rev=max(1, len(job.get("runs") or [])),
                                        modified=modified, template=settings.report_template,
                                        embed_fonts=settings.docx_embed_fonts)
                (library / name).with_suffix(".docx").write_bytes(data)
                archived.append(f"library/{job_type.id}/{Path(name).with_suffix('.docx').name}")
            except Exception:
                log.exception("Word copy of %s failed", job_id)

    added = 0
    if job_type.id == "meeting-summary":
        added = tasks.add_from_meeting(job, read_json(job_dir / "output" / "action_items.json", default=[]))

    job["status"] = DONE
    job["review"] = {"decision": "approved", "at": now_iso(), "note": note.strip(),
                     "archived": archived, "tasks_added": added}
    store.save(job)
    return job
