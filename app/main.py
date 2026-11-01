"""Dashboard + job API. Start with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .claude_status import check_claude
from .config import Settings
from .convert import READABLE_EXTS, prepare_inputs
from .jobtypes import JOB_TYPES
from .review import approve
from .runner import MESSAGE_STATUSES, Runner
from .state import RuntimeState
from .store import AREAS, JobStore, now_iso
from .tasks import TaskStore
from .watchers import FollowupScheduler, InboxWatcher

STATIC_DIR = Path(__file__).parent / "static"
TEXT_EXTS = {".md", ".txt", ".json", ".csv", ".vtt", ".srt", ".py", ".log", ".html", ".xml", ".yaml", ".yml"}
MAX_TEXT_BYTES = 2 * 1024 * 1024
CLAUDE_CHECK_EVERY = 600  # seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("assistant")


class NoteIn(BaseModel):
    note: str = ""


class MessageIn(BaseModel):
    message: str = ""
    feedback: str = ""  # older name used by the first version of the dashboard


class TaskStatusIn(BaseModel):
    status: str


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
    runner = Runner(store, tasks, state, settings)
    inbox = InboxWatcher(store, settings)
    scheduler = FollowupScheduler(store, tasks, state, settings)

    async def refresh_claude() -> dict:
        status = await check_claude(settings)
        state.set("claude", status)
        return status

    async def claude_check_loop() -> None:
        while True:
            try:
                await refresh_claude()
            except Exception:
                log.exception("claude status check failed")
            await asyncio.sleep(CLAUDE_CHECK_EVERY)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.ensure_dirs()
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
    app.state.tasks, app.state.inbox, app.state.scheduler = tasks, inbox, scheduler
    if settings.dashboard_password:
        app.add_middleware(BasicAuth, user=settings.dashboard_user, password=settings.dashboard_password)
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
            "server_time": now_iso(),
        }

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
        uploads = [(f.filename, await f.read()) for f in files if f.filename]
        for field in job_type.fields:
            if field.required and (not uploads if field.kind == "files" else not values.get(field.name)):
                raise HTTPException(400, f"กรุณากรอก: {field.label}")
        if job_type.id == "meeting-summary" and not any(Path(n).suffix.lower() in READABLE_EXTS for n, _ in uploads):
            raise HTTPException(400, "ต้องมีไฟล์ข้อความอย่างน้อย 1 ไฟล์ (.txt .vtt .srt .docx .pdf) — ไฟล์เสียงต้องถอดความก่อน")
        default_title = Path(uploads[0][0]).stem if uploads else job_type.label
        params = {"customer": values["customer"]} if values["customer"] else {}
        # Create the job and write its files without awaiting in between, so the worker never sees it half-made.
        job = store.create(job_type.id, values["title"] or default_title, values["instructions"], params)
        for name, data in uploads:
            store.add_file(job["id"], "input", name, data)
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
    async def message_job(job_id: str, body: MessageIn):
        return job_view(runner.send_message(job_id, body.message or body.feedback))

    app.add_api_route("/api/jobs/{job_id}/revise", message_job, methods=["POST"])

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
