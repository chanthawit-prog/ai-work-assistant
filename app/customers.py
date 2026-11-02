"""Customers: everything about one customer in one place.

A customer is a folder in knowledge/customers/ (its profile and files), and/or a name given to jobs (the customer
field) and follow-up items. Names match ignoring case and extra spaces, so "ACME" and "acme " are one customer;
the folder name, when there is one, is the name shown.
"""
from __future__ import annotations

from pathlib import Path

from .store import DONE, NEEDS_REVIEW, JobStore
from .tasks import TaskStore

HIDDEN = (".", "@", "#", "~$")  # Synology and Office helper files
PROFILE_NAMES = ("profile.md", "README.md", "readme.md")
MAX_PROFILE_BYTES = 200 * 1024
HISTORY_TYPES = ("meeting-summary", "proposal-draft", "eng-qa", "sales-brief", "general")


def key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def folders(knowledge_dir: Path) -> dict[str, Path]:
    """{customer key: folder} for knowledge/customers/<name>/."""
    root = knowledge_dir / "customers"
    if not root.is_dir():
        return {}
    return {key(p.name): p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(HIDDEN)}


def job_customer(job: dict) -> str:
    return key((job.get("params") or {}).get("customer"))


def summary(store: JobStore, tasks: TaskStore, knowledge_dir: Path) -> list[dict]:
    """Every customer, the ones with recent work first, then the others by name."""
    found: dict[str, dict] = {}

    def entry(k: str, name: str) -> dict:
        return found.setdefault(k, {"name": name, "key": k, "profile": False, "jobs": 0, "waiting": 0,
                                    "open_tasks": 0, "last": None})

    for k, folder in folders(knowledge_dir).items():
        entry(k, folder.name)["profile"] = True
    for job in store.list(limit=2000):
        k = job_customer(job)
        if not k:
            continue
        c = entry(k, str(job["params"]["customer"]).strip())
        c["jobs"] += 1
        c["waiting"] += job.get("status") == NEEDS_REVIEW
        c["last"] = max(c["last"] or "", job.get("updated_at") or job.get("created_at") or "")
    for item in tasks.open_items():
        k = key(item.get("customer"))
        if k:
            entry(k, str(item["customer"]).strip())["open_tasks"] += 1
    active = sorted((c for c in found.values() if c["last"]), key=lambda c: c["last"], reverse=True)
    quiet = sorted((c for c in found.values() if not c["last"]), key=lambda c: c["name"].casefold())
    return active + quiet


def detail(store: JobStore, tasks: TaskStore, knowledge_dir: Path, name: str) -> dict | None:
    """Profile, files, jobs (newest first), approved documents and open follow-up items of one customer."""
    k = key(name)
    if not k:
        return None
    folder = folders(knowledge_dir).get(k)
    jobs = [j for j in store.list(limit=2000) if job_customer(j) == k]
    items = [i for i in tasks.open_items() if key(i.get("customer")) == k]
    if folder is None and not jobs and not items:
        return None
    profile = None
    if folder is not None:
        files = [p.relative_to(folder).as_posix() for p in sorted(folder.rglob("*"))
                 if p.is_file() and not any(part.startswith(HIDDEN) for part in p.relative_to(folder).parts)]
        main = next((folder / n for n in PROFILE_NAMES if (folder / n).is_file()), None)
        if main is None:
            main = next((folder / f for f in files if f.lower().endswith(".md")), None)
        text = None
        if main is not None and main.stat().st_size <= MAX_PROFILE_BYTES:
            text = main.read_text("utf-8", errors="replace")
        profile = {"folder": f"customers/{folder.name}", "file": main.name if main else None, "text": text,
                   "files": files}
    documents = []
    for job in jobs:
        review = job.get("review") or {}
        if job.get("status") != DONE:
            continue
        for path in review.get("archived") or []:
            documents.append({"path": path, "job_id": job["id"], "title": job["title"], "type": job["type"],
                              "approved_at": review.get("at")})
    if folder is not None:
        shown = folder.name
    elif jobs:
        shown = str(jobs[0]["params"]["customer"]).strip()
    else:
        shown = str(items[0]["customer"]).strip()
    return {"name": shown, "key": k, "profile": profile, "jobs": jobs, "documents": documents, "tasks": items}


def library_path(settings, rel: str) -> Path | None:
    """"library/<type>/<file>" as stored in a job's review -> the file in LIBRARY_DIR (None if it points elsewhere)."""
    head, _, rest = rel.partition("/")
    if head != "library" or not rest:
        return None
    base = settings.library_dir.resolve()
    path = (base / rest).resolve()
    return path if base in path.parents else None


def history_files(store: JobStore, settings, job: dict, limit: int = 8) -> list[Path]:
    """Approved results of earlier jobs for the same customer (newest first): the customer's memory that a
    quotation or a sales brief gets in context/customer-history/."""
    k = job_customer(job)
    if not k:
        return []
    found = []
    for other in store.list(limit=2000):  # newest first
        if other["id"] == job["id"] or other.get("status") != DONE or job_customer(other) != k:
            continue
        if other["type"] not in HISTORY_TYPES:
            continue
        for rel in (other.get("review") or {}).get("archived") or []:
            path = library_path(settings, rel)
            if path is not None and path.suffix.lower() == ".md" and path.is_file():
                found.append(path)
        if len(found) >= limit:
            break
    return found[:limit]
