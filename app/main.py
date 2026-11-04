"""Dashboard + job API. Start with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import asyncio
import base64
import logging
from collections import deque
import mimetypes
import secrets
import shutil
import time
from collections import Counter
from datetime import datetime
from urllib.parse import quote, urlsplit
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from . import customers, sysmon
from .calendar_feed import tasks_ics
from .claude_status import check_claude
from .config import Settings
from .convert import READABLE_EXTS, prepare_inputs
from .export_docx import action_items_markdown, markdown_to_docx
from .handoff import HANDOFF_TYPES, default_files, handoff
from .jobtypes import EFFORTS, JOB_TYPES
from .knowledge import KnowledgeIndex, kb_search, ocr_status
from .ask import Asker
from .mail import Mailer, drafted_message, find_emails, split_addresses
from .notify import Notifier
from .readiness import run_checks
from .review import EDITABLE_EXTS, approve, file_sha, save_edit, slugify
from .runner import MESSAGE_STATUSES, Runner
from .schedules import INCLUDES, ScheduleStore, next_run, schedulable_types
from .schedules import create_job as create_scheduled_job
from .state import RuntimeState
from .store import AREAS, DONE, JOB_ID_RE, TRASH_DAYS, JobStore, now_iso, read_json, revision
from .tasks import TaskStore
from .today import DigestScheduler, summary as today_summary
from .watchers import FollowupScheduler, InboxWatcher, RecurringScheduler

APP_VERSION = "0.14.0"
STATIC_DIR = Path(__file__).parent / "static"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# files from jobs and the knowledge folder are downloads, never pages of the dashboard: an HTML or SVG file
# (written by Claude from someone's document, say) cannot run scripts with your login
SANDBOXED = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
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
    customer: str | None = None


class HandoffIn(BaseModel):
    type: str
    instructions: str = ""
    title: str = ""
    customer: str | None = None  # None: the customer of the job you continue from
    effort: str = ""             # low | medium | high | max; empty or "auto": the new job type's default
    files: list[str] = []        # output files to pass on; empty: the job type's main outputs
    inputs: list[str] = []       # files attached to the original job to pass on as well (a photo, the transcript)


class AskIn(BaseModel):
    question: str = ""
    within: str = ""


class EmailIn(BaseModel):
    to: str | list[str] = ""
    cc: str | list[str] = ""
    subject: str = ""
    body: str = ""
    attachments: list[str] = []  # "library/<type>/<file>" of this job's approval, or "output/<file>"


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


def clean_effort(value: str | None) -> str:
    """เร็ว ปกติ ละเอียด ละเอียดที่สุด = low medium high max; "auto" (or empty) = the job type's default. Forms send
    "auto", because FastAPI reads an empty form field as one that was not sent."""
    value = (value or "").strip().lower()
    if value == "auto":
        return ""
    if value and value not in EFFORTS:
        raise HTTPException(400, "ระดับการคิดต้องเป็น low, medium, high หรือ max")
    return value


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
    """Refuse an oversized request from its Content-Length, before the body is read or spooled anywhere:
    uploads (POST forms) up to MAX_UPLOAD_MB, and JSON bodies (PUT: an edited result, a follow-up item) up to a
    few MB, so a huge request cannot fill the NAS's RAM."""

    JSON_MAX = 4 * MB

    def __init__(self, app, max_bytes: int, message: str):
        self.app, self.max_bytes, self.message = app, max_bytes, message

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH"):
            length = dict(scope["headers"]).get(b"content-length", b"")
            limit = self.max_bytes + 256 * 1024 if scope["method"] == "POST" else self.JSON_MAX  # room for the form fields
            if length.isdigit() and int(length) > limit:
                message = self.message if scope["method"] == "POST" else f"ข้อมูลที่ส่งใหญ่เกิน {self.JSON_MAX // MB} MB"
                await JSONResponse({"detail": message}, status_code=413)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BasicAuth(BaseHTTPMiddleware):
    """Optional password for the dashboard (DASHBOARD_PASSWORD). Keep the NAS on your LAN/VPN anyway.

    After MAX_FAILURES wrong passwords from one address within LOCKOUT seconds, that address gets 429 until the
    window has passed: guessing the password by brute force on the LAN takes forever."""

    MAX_FAILURES = 10
    LOCKOUT = 600  # seconds

    def __init__(self, app, user: str, password: str):
        super().__init__(app)
        self.user = user.encode()
        self.password = password.encode()
        self.failures: dict[str, deque] = {}

    def locked(self, client: str, now: float) -> bool:
        recent = self.failures.get(client)
        while recent and now - recent[0] > self.LOCKOUT:
            recent.popleft()
        return bool(recent) and len(recent) >= self.MAX_FAILURES

    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith(CALENDAR_PREFIX):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        client, now = (request.client.host if request.client else "?"), time.monotonic()
        if header.startswith("Basic "):
            if self.locked(client, now):
                return JSONResponse({"detail": "ใส่รหัสผ่านผิดหลายครั้ง รอ 10 นาทีแล้วลองใหม่"}, status_code=429,
                                    headers={"Retry-After": str(self.LOCKOUT)})
            try:
                user, _, password = base64.b64decode(header[6:]).partition(b":")
            except ValueError:
                user = password = b""
            if secrets.compare_digest(user, self.user) and secrets.compare_digest(password, self.password):
                return await call_next(request)
            self.failures.setdefault(client, deque()).append(now)
            if len(self.failures) > 1000:  # a flood of addresses: forget the oldest
                self.failures.pop(next(iter(self.failures)))
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="AI Work Assistant", charset="UTF-8"'})


# the dashboard's own page may only load its own files; nothing may frame it
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _netloc(value: str) -> str:
    """"https://NAS.local:443" or "nas.local:443" -> "nas.local" (default ports dropped, lower case)."""
    host = urlsplit(value if "//" in value else f"//{value}").netloc.lower()
    for port in (":80", ":443"):
        if host.endswith(port):
            return host[: -len(port)]
    return host


class Guard:
    """Two defences around every request:
    - cross-site requests: a page on another site cannot make your browser create, change or delete anything here
      (browsers send the dashboard password with them). The browser's Sec-Fetch-Site and Origin headers decide;
      scripts and curl, which send neither, are not affected.
    - security headers on every response, and a Content-Security-Policy on the page itself."""

    def __init__(self, app, dashboard_url: str = ""):
        self.app = app
        self.extra = {_netloc(dashboard_url)} if dashboard_url else set()

    def allowed(self, headers: dict) -> bool:
        site = headers.get(b"sec-fetch-site", b"").decode("latin-1")
        if site in ("same-origin", "none"):
            return True
        origin = headers.get(b"origin", b"").decode("latin-1")
        if origin and origin != "null":
            hosts = {_netloc(headers.get(b"host", b"").decode("latin-1"))} | self.extra
            forwarded = headers.get(b"x-forwarded-host", b"").decode("latin-1").split(",")[0].strip()
            if forwarded:
                hosts.add(_netloc(forwarded))
            return _netloc(origin) in hosts
        return not site and not origin  # no browser headers at all: a script

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] in UNSAFE_METHODS and not self.allowed(dict(scope["headers"])):
            await JSONResponse({"detail": "ปฏิเสธคำขอที่มาจากเว็บไซต์อื่น"}, status_code=403)(scope, receive, send)
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _v in headers}
                for name, value in ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
                                    (b"x-frame-options", b"DENY")):
                    if name not in names:
                        headers.append((name, value))
                kind = dict(headers).get(b"content-type", b"")
                if kind.startswith(b"text/html") and b"content-security-policy" not in names:
                    headers.append((b"content-security-policy", CSP.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings)
    tasks = TaskStore(settings.tasks_file)
    state = RuntimeState(settings.state_dir / "runtime.json")
    notifier = Notifier(settings)
    mailer = Mailer(settings)
    runner = Runner(store, tasks, state, settings, notifier=notifier)
    knowledge = KnowledgeIndex(settings, runner)
    runner.kb = knowledge
    asker = Asker(settings, knowledge, state, runner)
    inbox = InboxWatcher(store, settings)
    scheduler = FollowupScheduler(store, tasks, state, settings)
    schedules = ScheduleStore(settings.data_dir / "config" / "schedules.json")
    recurring = RecurringScheduler(store, schedules)
    digest = DigestScheduler(store, tasks, schedules, state, settings, notifier)

    async def refresh_claude() -> dict:
        status = await check_claude(settings)
        state.set("claude", status)
        return status

    async def trash_loop() -> None:
        """Empty the trash of jobs deleted more than TRASH_DAYS days ago: at start, then every 6 hours."""
        while True:
            try:
                removed = await asyncio.to_thread(store.purge_trash)
                if removed:
                    log.info("trash: removed %d jobs deleted over %d days ago", len(removed), TRASH_DAYS)
            except Exception:
                log.exception("trash purge failed")
            await asyncio.sleep(6 * 3600)

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
                           knowledge.loop(), trash_loop(), digest.loop())]
            log.info("data=%s knowledge=%s claude=%s", settings.data_dir, settings.knowledge_dir, settings.claude_bin)
        yield
        for task in background:
            task.cancel()

    app = FastAPI(title="AI Work Assistant", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store, app.state.runner, app.state.runtime = settings, store, runner, state
    app.state.tasks, app.state.inbox, app.state.scheduler, app.state.notifier = tasks, inbox, scheduler, notifier
    app.state.schedules, app.state.recurring, app.state.knowledge = schedules, recurring, knowledge
    app.state.mailer = mailer
    app.state.asker = asker
    if settings.dashboard_password:
        app.add_middleware(BasicAuth, user=settings.dashboard_user, password=settings.dashboard_password)
    app.add_middleware(UploadLimit, max_bytes=max_upload, message=too_large)
    app.add_middleware(Guard, dashboard_url=settings.dashboard_url)  # outermost: runs before the password check
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
                        emails=job.get("emails") or [],
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
            "limits": {"max_upload_mb": settings.max_upload_mb, "mail": mailer.configured},
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

    @app.get("/api/readiness")
    async def readiness():
        """Is this installation ready? (also: docker exec ai-work-assistant python3 -m app.readiness)"""
        claude = state.get("claude") or await refresh_claude()
        files = None
        if settings.kb_enabled:
            try:
                files = (await asyncio.to_thread(knowledge.status))["files"]
            except Exception:
                log.exception("knowledge index unreadable")
        return await asyncio.to_thread(run_checks, settings, claude, notifier.channels, files)

    @app.get("/api/job-types")
    async def job_types():
        return [t.to_dict() for t in JOB_TYPES.values()]

    # -- jobs ------------------------------------------------------------------
    @app.get("/api/jobs")
    async def list_jobs(status: str = ""):
        return [job_view(j, full=False) for j in store.list() if not status or j.get("status") == status]

    @app.post("/api/jobs")
    async def create_job(type: str = Form(...), title: str = Form(""), instructions: str = Form(""),
                         customer: str = Form(""), effort: str = Form(""), files: list[UploadFile] = File(default=[])):
        job_type = JOB_TYPES.get(type)
        if job_type is None:
            raise HTTPException(400, "ไม่รู้จักชนิดงานนี้")
        # titles name the job everywhere (and the library files); customer names are folder names and page titles
        values = {"title": title.strip()[:150], "instructions": instructions.strip(), "customer": customer.strip()[:120]}
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
        if clean_effort(effort):
            params["effort"] = clean_effort(effort)
        staging, staged = await receive_uploads(uploads)
        try:
            # Create the job and move its files in without awaiting in between, so the worker never sees it half-made.
            job = store.create(job_type.id, values["title"] or default_title, values["instructions"], params)
            for name, path in staged:
                store.adopt_file(job["id"], "input", name, path)
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
        # conversion in a thread: the worker converts anything still missing when the job starts
        warnings = await asyncio.to_thread(prepare_inputs, store.area_dir(job["id"], "input"))
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
            "email": mailer.configured,  # the "ส่งอีเมล" button, once the job is approved
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
            return FileResponse(path, filename=path.name, headers=SANDBOXED)
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
        job = handoff(store, job_id, body.type, body.instructions, body.title, body.customer, body.files, body.inputs,
                      clean_effort(body.effort))
        warnings = await asyncio.to_thread(prepare_inputs, store.area_dir(job["id"], "input"))
        if warnings:
            job = store.update(job["id"], warnings=sorted(set(store.get(job["id"]).get("warnings") or []) | set(warnings)))
        return job_view(job)

    @app.post("/api/jobs/{job_id}/approve")
    async def approve_job(job_id: str, body: NoteIn):
        # in a thread: approval now also writes a Word file
        return job_view(await asyncio.to_thread(approve, store, tasks, settings, job_id, body.note))

    @app.post("/api/jobs/{job_id}/messages")
    async def message_job(job_id: str, message: str = Form(""), effort: str | None = Form(None),
                          files: list[UploadFile] = File(default=[])):
        """A follow-up in the same job: text, files, or both. Claude resumes its earlier session. `effort`, when
        sent, becomes the job's thinking level from this run on ("auto": back to the job type's default)."""
        job = runner.check_can_message(job_id)
        level = None if effort is None else clean_effort(effort)
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
            warnings = await asyncio.to_thread(prepare_inputs, store.area_dir(job_id, "input"))
            if warnings:
                job = store.get(job_id)
                store.update(job_id, warnings=sorted(set(job.get("warnings") or []) | set(warnings)))
        if level is not None:
            params = {k: v for k, v in (store.get(job_id).get("params") or {}).items() if k != "effort"}
            store.update(job_id, params={**params, **({"effort": level} if level else {})})
        return job_view(await asyncio.to_thread(runner.send_message, job_id, message, names))

    @app.post("/api/jobs/{job_id}/revise")
    async def revise_job(job_id: str, body: MessageIn):  # JSON form, kept for scripts written against v0.1
        return job_view(await asyncio.to_thread(runner.send_message, job_id, body.message or body.feedback))

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
    # -- email an approved result -----------------------------------------------
    def email_attachment_path(job: dict, rel: str) -> Path:
        """"library/<type>/<file>" from this job's approval, or "output/<file>" of this job; nothing else."""
        if rel.startswith("library/"):
            if rel not in ((job.get("review") or {}).get("archived") or []):
                raise KeyError(rel)
            path = customers.library_path(settings, rel)
        elif rel.startswith("output/"):
            path = store.file_path(job["id"], "output", rel[len("output/"):])
        else:
            raise KeyError(rel)
        if path is None or not path.is_file():
            raise KeyError(rel)
        return path

    @app.get("/api/jobs/{job_id}/email")
    async def email_prefill(job_id: str):
        """What the email form starts with: the customer's address, the drafted message, the files to attach."""
        job = store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        job_type = JOB_TYPES.get(job["type"])
        draft = {"subject": "", "body": ""}
        if job_type:
            primary = store.job_dir(job_id) / job_type.primary_output
            if primary.is_file() and primary.suffix.lower() in (".md", ".txt"):
                draft = drafted_message(primary.read_text("utf-8", errors="replace"))
        to: list[str] = []
        customer = (job.get("params") or {}).get("customer")
        folder = customers.folders(settings.knowledge_dir).get(customers.key(customer)) if customer else None
        if folder is not None:
            for name in customers.PROFILE_NAMES:
                if (folder / name).is_file():
                    to = find_emails((folder / name).read_text("utf-8", errors="replace"))
                    break
        files = []
        for rel in (job.get("review") or {}).get("archived") or []:
            path = customers.library_path(settings, rel)
            if path is not None and path.is_file():
                files.append({"path": rel, "name": path.name, "size": path.stat().st_size,
                              "default": path.suffix.lower() == ".docx"})
        for f in store.list_files(job_id, "output"):
            files.append({"path": f"output/{f['path']}", "name": f["path"], "size": f["size"], "default": False})
        return {"configured": mailer.configured, "sender": settings.mail_from, "to": to, "cc": [],
                "subject": draft["subject"] or job["title"], "body": draft["body"], "attachments": files,
                "can_send": job.get("status") == DONE, "sent": job.get("emails") or []}

    @app.post("/api/jobs/{job_id}/email")
    async def email_send(job_id: str, body: EmailIn):
        """Send an approved result. Nothing goes out except through this call, made by you from the form."""
        job = store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.get("status") != DONE:
            raise ValueError("ส่งอีเมลได้เฉพาะงานที่อนุมัติแล้ว")
        if not mailer.configured:
            raise ValueError("ยังไม่ได้ตั้งค่าอีเมลในไฟล์ .env (MAIL_SMTP_HOST, MAIL_FROM)")
        attachments = [email_attachment_path(job, rel) for rel in dict.fromkeys(body.attachments)]
        sent = await asyncio.to_thread(mailer.send, split_addresses(body.to), split_addresses(body.cc),
                                       body.subject.strip(), body.body, attachments)
        record = {**sent, "at": now_iso()}
        with store.lock:
            current = store.get(job_id)
            if current is not None:
                current["emails"] = list(current.get("emails") or []) + [record]
                store.save(current)
        attached = f" แนบ {', '.join(record['attachments'])}" if record["attachments"] else ""
        store.append_step(job_id, {"run": len(job.get("runs") or []), "kind": "email",
                                   "text": f"ส่งอีเมล \"{record['subject']}\" ถึง {', '.join(record['to'])}{attached}"})
        return {"sent": record, "job": job_view(store.get(job_id))}

    @app.post("/api/mail/test")
    async def mail_test():
        """An email to the sender address itself, to check the SMTP settings."""
        if not mailer.configured:
            raise ValueError("ยังไม่ได้ตั้งค่าอีเมลในไฟล์ .env (MAIL_SMTP_HOST, MAIL_FROM)")
        return await asyncio.to_thread(mailer.send, [settings.mail_from], [], "ทดสอบการส่งอีเมลจาก AI Work Assistant",
                                       "ตั้งค่าอีเมลถูกต้องแล้ว ส่งใบเสนอราคาและเอกสารที่อนุมัติจากหน้างานได้", [])

    @app.post("/api/notify/test")
    async def notify_test():
        if not notifier.channels:
            raise ValueError("ยังไม่ได้ตั้งค่าช่องทางแจ้งเตือนในไฟล์ .env")
        return await notifier.send("ทดสอบการแจ้งเตือนจาก AI Work Assistant: ตั้งค่าถูกต้องแล้ว", {"event": "test"})

    @app.post("/api/notify/digest")
    async def notify_digest():
        """Send today's summary now (the morning message, to check how it looks)."""
        if not notifier.channels:
            raise ValueError("ยังไม่ได้ตั้งค่าช่องทางแจ้งเตือนในไฟล์ .env")
        text = await asyncio.to_thread(digest.build) or "วันนี้ไม่มีงานที่เลยกำหนด ครบกำหนด หรือรอตรวจ"
        return await notifier.send(text, {"event": "digest"})

    @app.get("/api/today")
    async def today():
        return await asyncio.to_thread(today_summary, store, tasks, schedules, settings, None,
                                       state.get("followup_last"))

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
            "mail": mailer.status(),
            "security": {"password": bool(settings.dashboard_password), "python_jail": True,
                         "calendar_link": bool(state.get("calendar_token"))},
            "claude": state.get("claude"),
            "quota": state.get("quota"),
            "worker": {"active": list(runner.active), "max": settings.max_concurrent_jobs,
                       "timeout_min": settings.job_timeout_min, "model": settings.claude_model,
                       "effort": settings.claude_effort, "allow_python": settings.allow_python},
            "followup": {"time": settings.followup_time, "days": list(settings.followup_days)},
            "notify": {"channels": notifier.channels, "on": list(settings.notify_on), "digest_time": settings.digest_time,
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

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str):
        """Move the job to the trash (DATA_DIR/trash/jobs/). Approved results stay in the library."""
        if job_id in runner.active:
            raise ValueError("งานนี้กำลังทำ ยกเลิกงานก่อน แล้วค่อยลบ")
        target = store.trash(job_id)
        return {"ok": True, "trash": target.relative_to(settings.data_dir).as_posix(), "days": TRASH_DAYS}

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
                "pdftotext": shutil.which("pdftotext") is not None, "refresh_min": settings.kb_refresh_min,
                "ocr": await asyncio.to_thread(ocr_status, settings)}

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

    # -- ask the knowledge base ---------------------------------------------------
    @app.post("/api/ask")
    async def ask(body: AskIn):
        """One question, one short Claude call over the passages the index finds; no job is created."""
        return await asker.ask(body.question, body.within)

    @app.get("/api/ask/history")
    async def ask_history(limit: int = 20):
        return {"items": await asyncio.to_thread(asker.history, max(1, min(limit, 100)))}

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
        return FileResponse(path, filename=path.name, headers=SANDBOXED)

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

    # -- customers ---------------------------------------------------------------
    @app.get("/api/customers")
    async def list_customers():
        return await asyncio.to_thread(customers.summary, store, tasks, settings.knowledge_dir)

    @app.get("/api/customers/{name}")
    async def customer_page(name: str):
        found = await asyncio.to_thread(customers.detail, store, tasks, settings.knowledge_dir, name)
        if found is None:
            raise KeyError(name)
        return {**found, "jobs": [job_view(j, full=False) for j in found["jobs"]]}

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
