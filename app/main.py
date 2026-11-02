"""Dashboard + job API. Start with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import secrets
import shutil
import time
from collections import Counter
from datetime import datetime
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from . import sysmon
from .calendar_feed import tasks_ics
from .claude_status import check_claude
from .config import Settings
from .convert import READABLE_EXTS, prepare_inputs
from .export_docx import action_items_markdown, markdown_to_docx
from .handoff import HANDOFF_TYPES, default_files, handoff
from .jobtypes import JOB_TYPES
from .knowledge import KnowledgeIndex, kb_search
from .notify import Notifier
from .review import EDITABLE_EXTS, approve, file_sha, save_edit, slugify
from .runner import MESSAGE_STATUSES, Runner
from .schedules import INCLUDES, ScheduleStore, next_run, schedulable_types
from .schedules import create_job as create_scheduled_job
from .state import RuntimeState
from .store import AREAS, JOB_ID_RE, JobStore, now_iso, read_json, revision
from .tasks import TaskStore
from .watchers import FollowupScheduler, InboxWatcher, RecurringScheduler

APP_VERSION = "0.6.0"
STATIC_DIR = Path(__file__).parent / "static"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_EXTS = {".md", ".txt", ".json", ".csv", ".vtt", ".srt", ".py", ".log", ".html", ".xml", ".yaml", ".yml"}
MAX_TEXT_BYTES = 2 * 1024 * 1024
MB = 1024 * 1024
UPLOAD_CHUNK = MB
# Each check starts the CLI twice (about 140 MB for a moment), so check every 30 minutes and never while a
# job is running: a running job proves the login works anyway. While jobs wait for a login, check every
# minute, so they start by themselves soon after `claude auth login`.
CLAUDE_CHECK_EVERY = 1800  # seconds
LOGIN_RECHECK = 60

# reachable without the dashboard password: the healthcheck, and what a phone fetches (without credentials)
# to add the dashboard to its home screen. None of these files holds any data.
PUBLIC_PATHS = frozenset({"/healthz", "/static/manifest.webmanifest", "/static/favicon.svg", "/static/icon-192.png",
                          "/static/icon-512.png", "/static/icon-maskable-512.png", "/static/apple-touch-icon.png"})
# the calendar feed: calendar apps cannot send the dashboard password, so its address holds a secret instead
CALENDAR_PREFIX = "/calendar/"
mimetypes.add_type("application/manifest+json", ".webmanifest")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("assistant")


class NoteIn(BaseModel):
    note: str = ""


class MessageIn(BaseModel):
    message: str = ""
    feedback: str = ""  # older name used by the first version of the dashboard


class TaskStatusIn(BaseModel):
    status: str


class TaskIn(BaseModel):
    task: str = ""
    owner: str | None = None
    due: str | None = None
    priority: str = "medium"
    notes: str = ""


class HandoffIn(BaseModel):
    type: str
    instructions: str = ""
    title: str = ""
    customer: str | None = None  # None: the customer of the job you continue from
    files: list[str] = []        # output files to pass on; empty: the job type's main outputs
    inputs: list[str] = []       # files attached to the original job to pass on as well (a photo, the transcript)


class EditIn(BaseModel):
    text: str
    base_sha: str = ""  # the file's sha256 when you opened it (from GET .../files/output/...)


class ScheduleIn(BaseModel):
    name: str = ""
    type: str = "general"
    instructions: str = ""
    customer: str = ""
    days: list[str] = []
    time: str = ""
    include: list[str] = []
    enabled: bool = True


class UploadTooLarge(Exception):
    pass


def stage_uploads(staging: Path, uploads: list[UploadFile], max_bytes: int) -> list[tuple[str, Path]]:
    """Copy uploads to disk 1 MB at a time. The multipart parser has already spooled each one to a temp
    file; reading it whole with `await f.read()` would put the entire file in RAM, which a 6 GB NAS
    pays for in swap."""
    staging.mkdir(parents=True, exist_ok=True)
    saved, total = [], 0
    for i, upload in enumerate(uploads):
        path = staging / f"{i:03d}.part"
        upload.file.seek(0)
        with open(path, "wb") as out:
            while chunk := upload.file.read(UPLOAD_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLarge
                out.write(chunk)
        saved.append((upload.filename, path))
    return saved


class UploadLimit:
    """Refuse an oversized upload from its Content-Length, before the body is read or spooled anywhere."""

    def __init__(self, app, max_bytes: int, message: str):
        self.app, self.max_bytes, self.message = app, max_bytes, message

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            length = dict(scope["headers"]).get(b"content-length", b"")
            if length.isdigit() and int(length) > self.max_bytes + 256 * 1024:  # room for the form fields
                await JSONResponse({"detail": self.message}, status_code=413)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BasicAuth(BaseHTTPMiddleware):
    """Optional password for the dashboard (DASHBOARD_PASSWORD). Keep the NAS on your LAN/VPN anyway."""

    def __init__(self, app, user: str, password: str):
        super().__init__(app)
        self.user = user.encode()
        self.password = password.encode()

    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith(CALENDAR_PREFIX):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).partition(b":")
            except ValueError:
                user = password = b""
            if secrets.compare_digest(user, self.user) and secrets.compare_digest(password, self.password):
                return await call_next(request)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="AI Work Assistant", charset="UTF-8"'})


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings)
    tasks = TaskStore(settings.tasks_file)
    state = RuntimeState(settings.state_dir / "runtime.json")
    notifier = Notifier(settings)
    runner = Runner(store, tasks, state, settings, notifier=notifier)
    knowledge = KnowledgeIndex(settings, runner)
    runner.kb = knowledge
    inbox = InboxWatcher(store, settings)
    scheduler = FollowupScheduler(store, tasks, state, settings)
    schedules = ScheduleStore(settings.data_dir / "config" / "schedules.json")
    recurring = RecurringScheduler(store, schedules)

    async def refresh_claude() -> dict:
        status = await check_claude(settings)
        state.set("claude", status)
        return status

    async def claude_check_loop() -> None:
        last = None
        while True:
            try:
                every = LOGIN_RECHECK if runner.login_wait else CLAUDE_CHECK_EVERY
                if last is None or (not runner.active and time.monotonic() - last >= every):
                    await refresh_claude()  # always once at startup, then only while idle
                    last = time.monotonic()
            except Exception:
                log.exception("claude status check failed")
            await asyncio.sleep(15)

    max_upload = settings.max_upload_mb * MB
    too_large = f"ไฟล์ใหญ่เกินไป ส่งได้รวมไม่เกิน {settings.max_upload_mb} MB ต่อครั้ง (ตั้งได้ที่ MAX_UPLOAD_MB)"

    async def receive_uploads(uploads: list[UploadFile]) -> tuple[Path | None, list[tuple[str, Path]]]:
        """Stream uploads into a staging folder on the data volume (in a thread). The caller then moves them
        into the job with plain renames, without awaiting, so the worker never sees a half-made job."""
        if not uploads:
            return None, []
        if sum(u.size or 0 for u in uploads) > max_upload:
            raise HTTPException(413, too_large)
        staging = settings.state_dir / "uploads" / secrets.token_hex(8)
        try:
            return staging, await asyncio.to_thread(stage_uploads, staging, uploads, max_upload)
        except UploadTooLarge:
            shutil.rmtree(staging, ignore_errors=True)
            raise HTTPException(413, too_large) from None
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.ensure_dirs()
        shutil.rmtree(settings.state_dir / "uploads", ignore_errors=True)  # leftovers of an interrupted upload
        background = []
        if start_background:
            background = [asyncio.create_task(c) for c in
                          (runner.loop(), inbox.loop(), scheduler.loop(), recurring.loop(), claude_check_loop(),
                           knowledge.loop())]
            log.info("data=%s knowledge=%s claude=%s", settings.data_dir, settings.knowledge_dir, settings.claude_bin)
        yield
        for task in background:
            task.cancel()

    app = FastAPI(title="AI Work Assistant", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store, app.state.runner = settings, store, runner
    app.state.tasks, app.state.inbox, app.state.scheduler, app.state.notifier = tasks, inbox, scheduler, notifier
    app.state.schedules, app.state.recurring, app.state.knowledge = schedules, recurring, knowledge
    if settings.dashboard_password:
        app.add_middleware(BasicAuth, user=settings.dashboard_user, password=settings.dashboard_password)
    app.add_middleware(UploadLimit, max_bytes=max_upload, message=too_large)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(KeyError)
    async def _not_found(_request, _exc):
        return JSONResponse({"detail": "ไม่พบข้อมูลที่ขอ"}, status_code=404)

    @app.exception_handler(ValueError)
    async def _conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def linked(job_id: str | None, fallback_title: str = "") -> dict:
        """A job this one continues from, or continues into: its current title and status (None if deleted)."""
        other = store.get(job_id) if job_id else None
        if other is None:
            return {"id": job_id, "title": fallback_title or job_id, "status": None, "type_label": ""}
        other_type = JOB_TYPES.get(other["type"])
        return {"id": other["id"], "title": other["title"], "status": other["status"],
                "type_label": other_type.label if other_type else other["type"]}

    def job_view(job: dict, full: bool = True) -> dict:
        """`full=False` is the slim version the job list polls every few seconds."""
        job_type = JOB_TYPES.get(job["type"])
        runs = job.get("runs") or []
        last = runs[-1] if runs else {}
        view = {
            **{k: job.get(k) for k in ("id", "type", "title", "status", "source", "created_at", "updated_at",
                                       "error", "not_before", "params")},
            "type_label": job_type.label if job_type else job["type"],
            "icon": job_type.icon if job_type else "•",
            "run_count": len(runs),
            "rev": revision(job),
            "started_at": last.get("started_at"),
            "ended_at": last.get("ended_at"),
            "last_step": runner.last_step.get(job["id"]),
            "wait": runner.wait_info(job["id"]) if job.get("status") == "queued" else None,
        }
        if full:
            view.update({k: job.get(k) for k in ("session_id", "instructions", "review")})
            params = job.get("params") or {}
            view.update(origin=linked(params["from_job"], params.get("from_title", "")) if params.get("from_job") else None,
                        handoffs=[linked(h.get("job_id")) for h in job.get("handoffs") or []])
            view.update(warnings=job.get("warnings") or [], runs=runs, edits=job.get("edits") or [],
                        can_message=job.get("status") in MESSAGE_STATUSES)
        return view

    # -- pages & status --------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "time": now_iso()}

    @app.get("/api/status")
    async def status():
        jobs = store.list(limit=1000)
        return {
            "claude": state.get("claude"),
            "quota": state.get("quota"),
            "worker": {"active": list(runner.active), "max": settings.max_concurrent_jobs},
            "counts": Counter(j.get("status") for j in jobs),
            "open_tasks": len(tasks.open_items()),
            "followup": {"time": settings.followup_time, "days": list(settings.followup_days)},
            "memory": memory_summary(),
            "limits": {"max_upload_mb": settings.max_upload_mb},
            "server_time": now_iso(),
        }

    def memory_summary() -> dict | None:
        host = sysmon.host_memory()
        if host is None:
            return None
        need = settings.min_free_mem_mb
        return {**host, "need_mb": need, "low": bool(need and host["available_mb"] < need)}

    @app.post("/api/claude/check")
    async def claude_check():
        return await refresh_claude()

    @app.get("/api/job-types")
    async def job_types():
        return [t.to_dict() for t in JOB_TYPES.values()]

    # -- jobs ------------------------------------------------------------------
    @app.get("/api/jobs")
    async def list_jobs(status: str = ""):
        return [job_view(j, full=False) for j in store.list() if not status or j.get("status") == status]

    @app.post("/api/jobs")
    async def create_job(type: str = Form(...), title: str = Form(""), instructions: str = Form(""),
                         customer: str = Form(""), files: list[UploadFile] = File(default=[])):
        job_type = JOB_TYPES.get(type)
        if job_type is None:
            raise HTTPException(400, "ไม่รู้จักชนิดงานนี้")
        values = {"title": title.strip(), "instructions": instructions.strip(), "customer": customer.strip()}
        uploads = [f for f in files if f.filename]
        names = [f.filename for f in uploads]
        for field in job_type.fields:
            if field.required and (not names if field.kind == "files" else not values.get(field.name)):
                raise HTTPException(400, f"กรุณากรอก: {field.label}")
        if job_type.id == "meeting-summary" and not any(Path(n).suffix.lower() in READABLE_EXTS for n in names):
            raise HTTPException(400, "ต้องมีไฟล์ข้อความอย่างน้อย 1 ไฟล์ (.txt .vtt .srt .docx .pdf) — ไฟล์เสียงต้องถอดความก่อน")
        first_line = values["instructions"].split("\n")[0].strip()[:90]
        stem = Path(names[0]).stem if names else ""
        default_title = ((stem or first_line) if job_type.id == "meeting-summary" else (first_line or stem)) or job_type.label
        params = {"customer": values["customer"]} if values["customer"] else {}
        staging, staged = await receive_uploads(uploads)
        try:
            # Create the job and move its files in without awaiting in between, so the worker never sees it half-made.
            job = store.create(job_type.id, values["title"] or default_title, values["instructions"], params)
            for name, path in staged:
                store.adopt_file(job["id"], "input", name, path)
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
        warnings = prepare_inputs(store.area_dir(job["id"], "input"))
        if warnings:
            job = store.update(job["id"], warnings=warnings)
        return job_view(job)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, after: int = 0):
        job = store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        steps, total = store.read_steps(job_id, after=max(0, after))
        job_type = JOB_TYPES.get(job["type"])
        can_handoff = job.get("status") in ("needs_review", "done")
        return {
            "job": job_view(job),
            "type": job_type.to_dict() if job_type else None,
            "handoff": {"types": list(HANDOFF_TYPES), "files": default_files(store, job)} if can_handoff else None,
            "steps": steps,
            "next": total,
            "partial_text": runner.partial.get(job_id),
            "files": {area: store.list_files(job_id, area) for area in AREAS},
        }

    @app.get("/api/jobs/{job_id}/files/{area}/{rel:path}")
    async def get_file(job_id: str, area: str, rel: str, raw: bool = False):
        path = store.file_path(job_id, area, rel)
        if not path.is_file():
            raise KeyError(rel)
        size = path.stat().st_size
        if raw or path.suffix.lower() not in TEXT_EXTS or size > MAX_TEXT_BYTES:
            if not raw:
                return {"path": rel, "size": size, "binary": True}
            return FileResponse(path, filename=path.name)
        data = path.read_bytes()
        return {"path": rel, "size": size, "sha": file_sha(data), "text": data.decode("utf-8", errors="replace"),
                "editable": area == "output" and path.suffix.lower() in EDITABLE_EXTS}

    @app.put("/api/jobs/{job_id}/files/output/{rel:path}")
    async def edit_output(job_id: str, rel: str, body: EditIn):
        """Save your own edit of a result that waits for review. No Claude run, no quota."""
        if len(body.text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise HTTPException(413, f"ข้อความยาวเกิน {MAX_TEXT_BYTES // MB} MB")
        job, changed = await asyncio.to_thread(save_edit, store, job_id, rel, body.text, body.base_sha)
        return {"job": job_view(job), "changed": changed}

    @app.post("/api/jobs/{job_id}/handoff")
    async def handoff_job(job_id: str, body: HandoffIn):
        """Start the next job from this result (no await inside: the worker must not see the new job half-made)."""
        job = handoff(store, job_id, body.type, body.instructions, body.title, body.customer, body.files, body.inputs)
        return job_view(job)

    @app.post("/api/jobs/{job_id}/approve")
    async def approve_job(job_id: str, body: NoteIn):
        # in a thread: approval now also writes a Word file
        return job_view(await asyncio.to_thread(approve, store, tasks, settings, job_id, body.note))

    @app.post("/api/jobs/{job_id}/messages")
    async def message_job(job_id: str, message: str = Form(""), files: list[UploadFile] = File(default=[])):
        """A follow-up in the same job: text, files, or both. Claude resumes its earlier session."""
        runner.check_can_message(job_id)
        uploads = [f for f in files if f.filename]
        if not message.strip() and not uploads:
            raise ValueError("พิมพ์ข้อความหรือแนบไฟล์ก่อนส่ง")
        staging, staged = await receive_uploads(uploads)
        try:
            runner.check_can_message(job_id)  # still true after the upload?
            names = [store.adopt_file(job_id, "input", name, path) for name, path in staged]
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
        if names:
            warnings = prepare_inputs(store.area_dir(job_id, "input"))
            if warnings:
                job = store.get(job_id)
                store.update(job_id, warnings=sorted(set(job.get("warnings") or []) | set(warnings)))
        return job_view(runner.send_message(job_id, message, files=names))

    @app.post("/api/jobs/{job_id}/revise")
    async def revise_job(job_id: str, body: MessageIn):  # JSON form, kept for scripts written against v0.1
        return job_view(runner.send_message(job_id, body.message or body.feedback))

    @app.get("/api/jobs/{job_id}/export/{rel:path}")
    async def export_docx(job_id: str, rel: str):
        """Download an output as a Word document styled like a PEC report."""
        job = store.get(job_id)
        path = store.file_path(job_id, "output", rel)
        if job is None or not path.is_file():
            raise KeyError(rel)
        if path.suffix.lower() in (".md", ".txt"):
            md = path.read_text("utf-8", errors="replace")
        elif path.name.endswith("action_items.json"):
            md = action_items_markdown(read_json(path, default=[]), job["title"])
        else:
            raise ValueError("ส่งออกเป็น Word ได้เฉพาะไฟล์ .md .txt และ action_items.json")
        modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        data = await asyncio.to_thread(markdown_to_docx, md, title=job["title"], file_name=rel,
                                       rev=revision(job), modified=modified,
                                       template=settings.report_template, embed_fonts=settings.docx_embed_fonts)
        filename = f"{slugify(job['title'])}_{Path(rel).stem}.docx"
        return Response(data, media_type=DOCX_TYPE, headers={
            "Content-Disposition": f"attachment; filename=\"export.docx\"; filename*=UTF-8''{quote(filename)}"})

    # -- system & notifications ---------------------------------------------------
    @app.post("/api/notify/test")
    async def notify_test():
        if not notifier.channels:
            raise ValueError("ยังไม่ได้ตั้งค่าช่องทางแจ้งเตือนในไฟล์ .env")
        return await notifier.send("ทดสอบการแจ้งเตือนจาก AI Work Assistant: ตั้งค่าถูกต้องแล้ว", {"event": "test"})

    @app.get("/api/system")
    async def system():
        def count_files(root: Path, cap: int = 5000) -> int:
            n = 0
            for p in root.rglob("*") if root.is_dir() else []:
                if p.is_file() and not p.name.startswith((".", "@")):
                    n += 1
                    if n >= cap:
                        break
            return n
        disk = shutil.disk_usage(settings.data_dir) if settings.data_dir.exists() else None
        jobs_dir = settings.jobs_dir
        return {
            "version": {"app": APP_VERSION, "claude": (state.get("claude") or {}).get("version")},
            "claude": state.get("claude"),
            "quota": state.get("quota"),
            "worker": {"active": list(runner.active), "max": settings.max_concurrent_jobs,
                       "timeout_min": settings.job_timeout_min, "model": settings.claude_model,
                       "effort": settings.claude_effort, "allow_python": settings.allow_python},
            "followup": {"time": settings.followup_time, "days": list(settings.followup_days)},
            "notify": {"channels": notifier.channels, "on": list(settings.notify_on),
                       "dashboard_url": settings.dashboard_url},
            "storage": {"data_dir": str(settings.data_dir), "knowledge_dir": str(settings.knowledge_dir),
                        "knowledge_files": await asyncio.to_thread(count_files, settings.knowledge_dir),
                        "jobs": sum(1 for p in jobs_dir.iterdir() if JOB_ID_RE.match(p.name)) if jobs_dir.exists() else 0,
                        "free_gb": round(disk.free / 1e9, 1) if disk else None,
                        "total_gb": round(disk.total / 1e9, 1) if disk else None,
                        "report_template": settings.report_template.is_file(),
                        "embed_fonts": settings.docx_embed_fonts},
            "memory": {**sysmon.snapshot(),
                       "settings": {"min_free_mb": settings.min_free_mem_mb, "wait_max_min": settings.mem_wait_max_min,
                                    "low_memory": settings.claude_low_memory, "max_upload_mb": settings.max_upload_mb,
                                    "max_jobs": settings.max_concurrent_jobs},
                       "runs": state.get("mem_runs") or [],
                       "waiting": runner.wait_info(runner.mem_wait["job_id"]) if runner.mem_wait else None},
        }

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str):
        return job_view(runner.retry(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        return job_view(await runner.cancel(job_id))

    # -- recurring jobs (งานประจำ) ---------------------------------------------------
    def schedule_view(item: dict, now: datetime) -> dict:
        job_type = JOB_TYPES.get(item["type"])
        last = item.get("last_job") or {}
        job = store.get(last["job_id"]) if last.get("job_id") else None
        upcoming = next_run(item, now)
        return {**item, "type_label": job_type.label if job_type else item["type"],
                "next_run": upcoming.isoformat(timespec="seconds") if upcoming else None,
                "last_job_info": item.get("last_job"),
                "last_job": {"id": job["id"], "title": job["title"], "status": job["status"]} if job else None}

    @app.get("/api/schedules")
    async def list_schedules():
        now = datetime.now().astimezone()
        return {"items": [schedule_view(s, now) for s in schedules.all()],
                "types": schedulable_types(), "includes": INCLUDES,
                "followup": {"time": settings.followup_time, "days": list(settings.followup_days)}}

    @app.post("/api/schedules")
    async def add_schedule(body: ScheduleIn):
        return schedule_view(schedules.create(body.model_dump()), datetime.now().astimezone())

    @app.put("/api/schedules/{schedule_id}")
    async def edit_schedule(schedule_id: str, body: ScheduleIn):
        return schedule_view(schedules.update(schedule_id, body.model_dump()), datetime.now().astimezone())

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str):
        schedules.delete(schedule_id)
        return {"ok": True}

    @app.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: str):
        """Run now, outside the timetable. Today's scheduled run still happens (unless this job is unfinished)."""
        item = schedules.get(schedule_id)
        if item is None:
            raise KeyError(schedule_id)
        job = create_scheduled_job(store, item, datetime.now().astimezone())
        schedules.mark_manual(schedule_id, job["id"])
        return job_view(job)

    # -- knowledge search -----------------------------------------------------------
    @app.get("/api/knowledge")
    async def knowledge_status():
        info = await asyncio.to_thread(knowledge.status)
        return {**info, "enabled": settings.kb_enabled, "running": knowledge.running, "last": knowledge.last,
                "pdftotext": shutil.which("pdftotext") is not None, "refresh_min": settings.kb_refresh_min}

    @app.get("/api/knowledge/search")
    async def knowledge_search(q: str = "", limit: int = 10, within: str = ""):
        try:
            found = await asyncio.to_thread(kb_search.search, knowledge.path, q, max(1, min(limit, 30)), within)
        except kb_search.SearchError as exc:
            raise HTTPException(400, str(exc)) from None
        keep = ("path", "source", "rel", "kind", "loc", "loc_label", "excerpt", "text")
        return {"query": found["query"], "terms": found["terms"],
                "results": [{k: r[k] for k in keep} for r in found["results"]]}

    @app.post("/api/knowledge/refresh")
    async def knowledge_refresh():
        if not settings.kb_enabled:
            raise ValueError("ปิดการค้นคลังความรู้อยู่ (KB_SEARCH=false ในไฟล์ .env)")
        knowledge.request()
        return {"queued": True, "busy": knowledge.busy(), "running": knowledge.running}

    @app.get("/api/knowledge/file/{rel:path}")
    async def knowledge_file(rel: str):
        """Open a search result: knowledge/<path> or library/<path>, read-only, never outside those folders."""
        root_name, _, sub = rel.partition("/")
        root = knowledge.roots.get(root_name)
        if root is None or not sub:
            raise KeyError(rel)
        base = root.resolve()
        path = (base / sub).resolve()
        if base not in path.parents or not path.is_file():
            raise KeyError(rel)
        return FileResponse(path, filename=path.name)

    # -- follow-up list --------------------------------------------------------
    @app.get("/api/tasks")
    async def list_tasks(include_done: bool = False):
        items = tasks.all(include_done=include_done)
        return sorted(items, key=lambda i: (i.get("status") == "done", i.get("due") or "9999-12-31", i["id"]))

    @app.post("/api/tasks")
    async def add_task(body: TaskIn):
        return tasks.add(body.model_dump())

    @app.post("/api/tasks/{item_id}")
    async def set_task(item_id: str, body: TaskStatusIn):
        return tasks.set_status(item_id, body.status)

    @app.put("/api/tasks/{item_id}")
    async def edit_task(item_id: str, body: TaskIn):
        return tasks.update(item_id, body.model_dump(exclude_unset=True))

    @app.delete("/api/tasks/{item_id}")
    async def delete_task(item_id: str):
        tasks.delete(item_id)
        return {"ok": True}

    # -- calendar feed of due dates ---------------------------------------------
    def calendar_info() -> dict:
        token = state.get("calendar_token")
        return {"enabled": bool(token), "path": f"{CALENDAR_PREFIX}{token}.ics" if token else None}

    @app.get("/api/calendar")
    async def calendar_status():
        return calendar_info()

    @app.post("/api/calendar")
    async def calendar_new_link():
        """Make the feed address, or replace it: the old address stops working at once."""
        state.set("calendar_token", secrets.token_urlsafe(24))
        return calendar_info()

    @app.delete("/api/calendar")
    async def calendar_off():
        state.set("calendar_token", None)
        return calendar_info()

    @app.get(CALENDAR_PREFIX + "{name}", include_in_schema=False)
    async def calendar_feed(name: str, request: Request):
        token = state.get("calendar_token")
        given = name[:-4] if name.endswith(".ics") else ""
        if not token or not secrets.compare_digest(given.encode(), token.encode()):
            raise KeyError(name)
        base = settings.dashboard_url or str(request.base_url)
        body = tasks_ics(tasks.open_items(), base)
        return Response(body, media_type="text/calendar; charset=utf-8", headers={
            "Content-Disposition": 'inline; filename="followup.ics"', "Cache-Control": "no-cache"})

    return app
