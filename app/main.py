"""Dashboard + job API. Start with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import shutil
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
from .claude_status import check_claude
from .config import Settings
from .convert import READABLE_EXTS, prepare_inputs
from .export_docx import action_items_markdown, markdown_to_docx
from .jobtypes import JOB_TYPES
from .notify import Notifier
from .review import approve, slugify
from .runner import MESSAGE_STATUSES, Runner
from .state import RuntimeState
from .store import AREAS, JOB_ID_RE, JobStore, now_iso, read_json
from .tasks import TaskStore
from .watchers import FollowupScheduler, InboxWatcher

APP_VERSION = "0.3.0"
STATIC_DIR = Path(__file__).parent / "static"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_EXTS = {".md", ".txt", ".json", ".csv", ".vtt", ".srt", ".py", ".log", ".html", ".xml", ".yaml", ".yml"}
MAX_TEXT_BYTES = 2 * 1024 * 1024
MB = 1024 * 1024
UPLOAD_CHUNK = MB
# Each check starts the CLI twice (about 140 MB for a moment), so check every 30 minutes and never while a
# job is running: a running job proves the login works anyway.
CLAUDE_CHECK_EVERY = 1800  # seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("assistant")


class NoteIn(BaseModel):
    note: str = ""


class MessageIn(BaseModel):
    message: str = ""
    feedback: str = ""  # older name used by the first version of the dashboard


class TaskStatusIn(BaseModel):
    status: str


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
        if request.url.path == "/healthz":
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
    inbox = InboxWatcher(store, settings)
    scheduler = FollowupScheduler(store, tasks, state, settings)

    async def refresh_claude() -> dict:
        status = await check_claude(settings)
        state.set("claude", status)
        return status

    async def claude_check_loop() -> None:
        first = True
        while True:
            try:
                if first or not runner.active:  # always once at startup, then only while idle
                    await refresh_claude()
            except Exception:
                log.exception("claude status check failed")
            first = False
            await asyncio.sleep(CLAUDE_CHECK_EVERY)

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
                          (runner.loop(), inbox.loop(), scheduler.loop(), claude_check_loop())]
            log.info("data=%s knowledge=%s claude=%s", settings.data_dir, settings.knowledge_dir, settings.claude_bin)
        yield
        for task in background:
            task.cancel()

    app = FastAPI(title="AI Work Assistant", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store, app.state.runner = settings, store, runner
    app.state.tasks, app.state.inbox, app.state.scheduler, app.state.notifier = tasks, inbox, scheduler, notifier
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
            "started_at": last.get("started_at"),
            "ended_at": last.get("ended_at"),
            "last_step": runner.last_step.get(job["id"]),
            "wait": runner.wait_info(job["id"]) if job.get("status") == "queued" else None,
        }
        if full:
            view.update({k: job.get(k) for k in ("session_id", "instructions", "review")})
            view.update(warnings=job.get("warnings") or [], runs=runs,
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
        return {
            "job": job_view(job),
            "type": job_type.to_dict() if job_type else None,
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
        return {"path": rel, "size": size, "text": path.read_text("utf-8", errors="replace")}

    @app.post("/api/jobs/{job_id}/approve")
    async def approve_job(job_id: str, body: NoteIn):
        return job_view(approve(store, tasks, settings, job_id, body.note))

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
                                       rev=max(1, len(job.get("runs") or [])), modified=modified,
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

    # -- follow-up list --------------------------------------------------------
    @app.get("/api/tasks")
    async def list_tasks(include_done: bool = False):
        items = tasks.all(include_done=include_done)
        return sorted(items, key=lambda i: (i.get("status") == "done", i.get("due") or "9999-12-31", i["id"]))

    @app.post("/api/tasks/{item_id}")
    async def set_task(item_id: str, body: TaskStatusIn):
        return tasks.set_status(item_id, body.status)

    return app
