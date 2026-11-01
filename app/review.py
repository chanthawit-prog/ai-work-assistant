"""Approving a job: archive the result in the library and, for meetings, add the action items to the follow-up list."""
from __future__ import annotations

import re
import shutil

from .config import Settings
from .jobtypes import JOB_TYPES
from .store import DONE, NEEDS_REVIEW, JobStore, now_iso, read_json, safe_filename
from .tasks import TaskStore


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

    added = 0
    if job_type.id == "meeting-summary":
        added = tasks.add_from_meeting(job, read_json(job_dir / "output" / "action_items.json", default=[]))

    job["status"] = DONE
    job["review"] = {"decision": "approved", "at": now_iso(), "note": note.strip(),
                     "archived": archived, "tasks_added": added}
    store.save(job)
    return job
