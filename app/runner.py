"""Run jobs with the official Claude Code CLI (`claude -p`), signed in with your own Claude subscription.

The model work happens on Claude's servers; this process only starts the CLI in the job folder,
feeds it the task, and records its stream-json events so the dashboard can show progress.

Subscription rules this module follows:
* it runs the unmodified `claude` binary and never touches your credentials;
* it never uses `--bare` (bare mode ignores subscription logins);
* it removes ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from the CLI environment, because an API key
  would take precedence over the subscription and be billed per token.

Memory (a DS925+ with 6 GB fills its swap easily):
* a job starts only when the NAS has MIN_FREE_MEM_MB of RAM available; otherwise it keeps its place in
  the queue and starts anyway after MEM_WAIT_MAX_MIN, with a warning in its timeline;
* the CLI runs with BUN_OPTIONS=--smol (the native Claude Code binary is built on Bun; --smol makes its
  garbage collector run more often), MALLOC_ARENA_MAX=2, no non-essential traffic and no MCP servers;
* each run records the peak memory of the CLI and its child processes, and a run killed for lack of
  memory (OOM) gets a message that says so instead of a bare exit code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
from asyncio.subprocess import PIPE
from datetime import datetime, timedelta
from pathlib import Path

from . import customers, sysmon
from .config import Settings
from .convert import prepare_inputs
from .events import RunMeta, summarize
from .jobtypes import JOB_TYPES, JobType
from .prompts import system_prompt, task_message
from .state import RuntimeState
from .store import CANCELLED, DONE, FAILED, NEEDS_REVIEW, QUEUED, RUNNING, JobStore, now_iso, read_json, write_json
from .tasks import TaskStore

log = logging.getLogger("assistant.runner")

STREAM_LIMIT = 32 * 1024 * 1024  # stream-json lines can be large (tool results)
RECENT_DAYS = 7          # "ผลงานที่อนุมัติใน 7 วันล่าสุด" for recurring jobs
RECENT_MAX_FILES = 40
RECENT_MAX_BYTES = 3 * 1024 * 1024
RECENT_EXTS = {".md", ".txt", ".json", ".csv"}
MAX_QUOTA_WAITS = 3
MAX_STDERR_LINES = 40
BASE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"  # pec_calc and kb_search, run by Claude with python3
KB_RULE = "Bash(python3 -m kb_search *)"
# the python3 forms Claude may run in calculation jobs: all of them load tools/sitecustomize.py, the jail (no network,
# no other programs, no writing outside the job folder). `python3 -I` or `-S` would skip it, so they are not allowed.
PYTHON_RULES = ["Bash(python3 -c *)", "Bash(python3 -m *)", "Bash(python3 output/*)", "Bash(python3 ./output/*)",
                "Bash(cd *)"]
# the app's own settings that Claude and its tools have no use for (passwords, notification tokens)
PRIVATE_ENV_PREFIXES = ("NOTIFY_", "DASHBOARD_")
# the app's own files in the job folder: Claude neither sees them (Glob leaves denied files out) nor spends tokens
# reading them, and cannot overwrite them
INTERNAL_FILES = ("job.json", "events.jsonl", "steps.jsonl")
DENY_RULES = [f"{tool}(./{name})" for tool in ("Read", "Edit") for name in INTERNAL_FILES]


def kb_prompt(python: bool) -> str:
    """Told to Claude when the knowledge index holds files."""
    lines = [
        "", "## ค้นคลังความรู้ (kb_search)",
        "ค้นข้อความในไฟล์ PDF, Word, Markdown และ CSV ของคลังความรู้ รวมทั้งผลงานที่ผู้ใช้อนุมัติแล้ว ด้วยคำสั่ง",
        '`python3 -m kb_search "คำค้น"` (หลายคำ = ต้องมีครบทุกคำ · `--full` ดูข้อความเต็ม · `--in knowledge/engineering` จำกัดโฟลเดอร์)',
        "- ผลลัพธ์บอกไฟล์ หน้า (PDF) หรือบรรทัด และข้อความรอบคำที่พบ ค้นก่อนเปิด PDF ทั้งไฟล์ แล้วอ่านเฉพาะหน้าที่เกี่ยวข้องด้วย Read",
        "- ไฟล์ Excel (.xlsx) และผลงานที่อนุมัติแล้วเปิดด้วย Read ไม่ได้ ให้ใช้ `python3 -m kb_search --show <ไฟล์>` อ่านทั้งไฟล์",
        "- ถ้าไม่พบ ลองคำที่สั้นลง คำพ้อง หรือคำภาษาอังกฤษที่เอกสารน่าจะใช้",
        "- อ้างอิงชื่อไฟล์และหน้าของข้อมูลที่นำมาใช้",
    ]
    if not python:
        lines.append("- ในงานนี้ Bash ใช้ได้เฉพาะคำสั่ง python3 -m kb_search เท่านั้น")
    return "\n".join(lines) + "\n"
MESSAGE_STATUSES = (NEEDS_REVIEW, DONE, FAILED, CANCELLED)  # states in which you can talk to Claude again
MEM_FIRST_SAMPLE_SEC = 0.5  # skip the instant between exec and the runtime loading
MEM_SAMPLE_SEC = 1
MEM_HISTORY = 20
OOM_TEXT = re.compile(r"out of memory|cannot allocate memory|allocation failed|ENOMEM", re.IGNORECASE)
AUTH_TEXT = re.compile(r"not logged in|please run /login|authentication_failed|invalid api key|"
                       r"oauth token (?:has )?expired|token has expired", re.IGNORECASE)
LOGIN_MESSAGE = ("Claude ยังไม่ได้ล็อกอิน หรือการล็อกอินหมดอายุ: บน NAS รัน "
                 "sudo docker exec -it ai-work-assistant claude auth login (ดู docs/setup-ds925.md ข้อ 5) "
                 "แล้วกด 'ลองใหม่' งานอื่นในคิวจะรอและเริ่มเองเมื่อเชื่อมได้")
OOM_MESSAGES = {
    "container": "หน่วยความจำไม่พอ: Claude ใช้ RAM ครบเพดานของ container (MEM_LIMIT) จึงถูกหยุดกลางคัน "
                 "กด 'ลองใหม่' หรือเพิ่ม MEM_LIMIT ในไฟล์ .env",
    "killed": "Claude ถูกระบบหยุดกลางคัน (SIGKILL) มักเกิดจาก RAM ของ NAS เต็ม "
              "กด 'ลองใหม่' เมื่อ NAS ว่างขึ้น หรือปิดแอปที่ไม่ได้ใช้บน DSM",
    "runtime": "Claude หยุดเพราะหน่วยความจำไม่พอ (out of memory) กด 'ลองใหม่' เมื่อ NAS ว่างขึ้น",
}


def claude_env(low_memory: bool = True) -> dict:
    env = dict(os.environ)
    # CLAUDE_EFFORT is this app's own setting (it becomes --effort); Claude Code may read a variable of the same name,
    # so it must not leak through and override the level chosen for the job
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_EFFORT"):
        env.pop(key, None)
    for key in [k for k in env if k.startswith(PRIVATE_ENV_PREFIXES)]:
        env.pop(key)
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["DISABLE_AUTOUPDATER"] = "1"
    if low_memory:
        options = env.get("BUN_OPTIONS", "").split()
        if "--smol" not in options:
            env["BUN_OPTIONS"] = " ".join(options + ["--smol"])
        env.setdefault("MALLOC_ARENA_MAX", "2")
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")  # no telemetry, error reports, updater
    return env


class Runner:
    def __init__(self, store: JobStore, tasks: TaskStore, state: RuntimeState, settings: Settings, notifier=None):
        self.notifier = notifier
        self._background: set[asyncio.Task] = set()
        self.store = store
        self.tasks = tasks
        self.state = state
        self.settings = settings
        self.active: dict[str, asyncio.Task] = {}
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.cancel_reasons: dict[str, str] = {}
        self.last_step: dict[str, str] = {}
        self.partial: dict[str, str] = {}  # text Claude is typing right now (from --include-partial-messages)
        self.peak_mem: dict[str, int] = {}  # MB, CLI + child processes, for the run in progress
        self.oom_hint: set[str] = set()     # jobs whose CLI printed "out of memory"
        self.mem_wait: dict | None = None   # the job at the head of the queue waiting for free RAM
        self.forced_start: dict[str, str] = {}
        self.login_wait = False             # queued jobs are held because Claude Code is not signed in
        self.kb = None                      # KnowledgeIndex, set by the app

    # -- building a run ------------------------------------------------------
    def kb_ready(self) -> bool:
        return self.kb is not None and self.kb.ready()

    def jail_env(self, job_dir) -> dict:
        """What tools/sitecustomize.py needs: the one folder Python may write to, and what it may not read
        (Claude's login, the app's state, the other jobs and the trash)."""
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        private = [home, Path.home() / ".claude.json", self.settings.state_dir, self.settings.jobs_dir,
                   self.settings.data_dir / "trash"]
        return {"AIWA_PYTHON_JAIL": str(job_dir), "AIWA_JAIL_NO_READ": os.pathsep.join(str(p) for p in private),
                "PYTHONDONTWRITEBYTECODE": "1"}

    def effort_for(self, job: dict, job_type: JobType) -> str:
        """The job's own choice (เร็ว ปกติ ละเอียด), else CLAUDE_EFFORT, else the job type's default ("" = Claude Code's)."""
        return (job.get("params") or {}).get("effort") or self.settings.claude_effort or job_type.effort

    def build_command(self, job_type: JobType, resume_session: str | None = None, kb: bool | None = None,
                      effort: str | None = None) -> list[str]:
        kb = self.kb_ready() if kb is None else kb
        effort = (self.settings.claude_effort or job_type.effort) if effort is None else effort
        python = job_type.allow_python and self.settings.allow_python
        tools, allowed = list(BASE_TOOLS), list(BASE_TOOLS)
        if python:
            tools.append("Bash")
            # Claude often writes `cd <job folder> && python3 ...`; each part of a chain must match a rule
            allowed += PYTHON_RULES
        elif kb:
            tools.append("Bash")
            allowed.append(KB_RULE)  # every job may search the knowledge index, and run nothing else
        cmd = [
            self.settings.claude_bin, "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--permission-mode", "acceptEdits",
            "--permission-prompts", "none",
            "--tools", ",".join(tools),
            "--allowedTools", ",".join(allowed),
            "--disallowedTools", ",".join(DENY_RULES),
            "--strict-mcp-config",  # no MCP servers: none are needed, and each one is another process in RAM
            "--append-system-prompt", system_prompt(job_type, self.settings) + (kb_prompt(python) if kb else ""),
        ]
        if self.settings.knowledge_dir.is_dir():
            cmd += ["--add-dir", str(self.settings.knowledge_dir)]
        if self.settings.claude_model:
            cmd += ["--model", self.settings.claude_model]
        if effort:
            cmd += ["--effort", effort]
        if resume_session:
            cmd += ["--resume", resume_session]
        return cmd + list(self.settings.claude_extra_args)

    def prepare_context(self, job: dict, job_type: JobType) -> list[str]:
        """Copy in material Claude needs but should not be able to modify."""
        ctx = self.store.area_dir(job["id"], "context")
        ctx.mkdir(parents=True, exist_ok=True)
        include = set((job.get("params") or {}).get("include") or [])
        if job_type.id == "follow-up" or "tasks" in include:
            write_json(ctx / "action_items.json", self.tasks.open_items())
        if "recent" in include:
            self._copy_recent(ctx / "recent")
        if job_type.id in ("sales-brief", "proposal-draft"):
            # the customer's memory: what was approved for them before (quotations, meetings, answers), newest first
            history = customers.history_files(self.store, self.settings, job)
            if history:
                dest = ctx / "customer-history"
                dest.mkdir(exist_ok=True)
                for path in history:
                    shutil.copy2(path, dest / path.name)
        if job_type.id == "sales-brief":
            customer = str((job.get("params") or {}).get("customer", "")).strip().lower()
            meetings = self.settings.library_dir / "meeting-summary"
            if customer and meetings.is_dir():
                dest = ctx / "meetings"
                dest.mkdir(exist_ok=True)
                found = [p for p in sorted(meetings.glob("*.md"), reverse=True)
                         if customer in p.read_text("utf-8", errors="ignore").lower()][:20]
                for path in found:
                    shutil.copy2(path, dest / path.name)
        return [p.relative_to(ctx).as_posix() for p in sorted(ctx.rglob("*")) if p.is_file()]

    def _copy_recent(self, dest) -> None:
        """Approved outputs of the last RECENT_DAYS days, newest first, within a size budget."""
        library = self.settings.library_dir
        if not library.is_dir():
            return
        since = datetime.now().timestamp() - RECENT_DAYS * 86400
        files = sorted((p for p in library.rglob("*") if p.is_file() and p.suffix.lower() in RECENT_EXTS
                        and not p.name.startswith((".", "@")) and p.stat().st_mtime >= since),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        used = 0
        for path in files[:RECENT_MAX_FILES]:
            size = path.stat().st_size
            if used + size > RECENT_MAX_BYTES:
                continue
            target = dest / path.relative_to(library)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            used += size

    # -- running -------------------------------------------------------------
    def _step(self, job_id: str, run: int, kind: str, text: str) -> None:
        self.store.append_step(job_id, {"run": run, "kind": kind, "text": text})
        self.last_step[job_id] = text

    @staticmethod
    def request_text(job: dict, job_type: JobType) -> str:
        """What the user asked for, as shown in the conversation."""
        parts = []
        if job_type.id != "general" or not job.get("instructions"):  # a hand-off title already starts with the label
            parts.append(job["title"] if job["title"].startswith(job_type.label) else f"{job_type.label}: {job['title']}")
        customer = (job.get("params") or {}).get("customer")
        if customer:
            parts.append(f"ลูกค้า: {customer}")
        if job.get("instructions"):
            parts.append(job["instructions"])
        return "\n".join(parts)

    async def run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job or job["status"] != QUEUED:
            return
        job_type = JOB_TYPES.get(job["type"])
        if job_type is None:
            self.store.update(job_id, status=FAILED, error=f"ไม่รู้จักชนิดงาน {job['type']}")
            return
        job_dir = self.store.job_dir(job_id)
        feedback = job.get("pending_feedback")
        new_files = list(job.get("pending_files") or [])
        edited = list(job.get("pending_edits") or [])  # outputs you edited yourself since Claude last saw them
        resume = job.get("session_id") if feedback else None
        run_no = len(job["runs"]) + 1
        kind = "message" if feedback else ("retry" if job["runs"] else "initial")

        # file work in a thread: converting a big workbook or copying a customer's history must not freeze the
        # dashboard and the other loops (the Docker healthcheck would restart the container)
        warnings = await asyncio.to_thread(prepare_inputs, job_dir / "input")
        inputs = [f["path"] for f in self.store.list_files(job_id, "input")]
        context = await asyncio.to_thread(self.prepare_context, job, job_type)
        prompt = task_message(job, job_type, self.settings, inputs, context, feedback=feedback, resumed=bool(resume),
                              new_files=new_files, edited=edited)
        kb = self.kb_ready()
        effort = self.effort_for(job, job_type)
        cmd = self.build_command(job_type, resume_session=resume, kb=kb, effort=effort)

        run = {"n": run_no, "kind": kind, "feedback": feedback, "files": new_files, "edited": edited,
               "effort": effort or "default", "started_at": now_iso(), "ended_at": None, "exit_code": None,
               "model": None, "result": None, "usage": {}}
        self.store.update(job_id, status=RUNNING, runs=job["runs"] + [run], pending_feedback=None, pending_files=[],
                          pending_edits=[], error=None, not_before=None,
                          warnings=sorted(set(job.get("warnings", [])) | set(warnings)))
        if kind == "retry":
            self._step(job_id, run_no, "app", "เริ่มทำงานนี้ใหม่อีกครั้ง")
        else:
            text = feedback if kind == "message" else self.request_text(job, job_type)
            self.store.append_step(job_id, {"run": run_no, "kind": "user", "text": text,
                                            "files": inputs if kind == "initial" else new_files})
        forced = self.forced_start.pop(job_id, None)
        if forced:
            self._step(job_id, run_no, "warn", forced)

        meta = RunMeta()
        oom_before = (sysmon.container_memory() or {}).get("oom_kills")
        env = claude_env(self.settings.claude_low_memory)
        python = job_type.allow_python and self.settings.allow_python
        if python or kb:
            # `python3 -m pec_calc ...` / `python3 -m kb_search ...` (and `import pec_calc`) work from the job folder,
            # and every python3 starts inside the jail of tools/sitecustomize.py
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(TOOLS_DIR), env.get("PYTHONPATH", "")) if p)
            env.update(self.jail_env(job_dir))
        if python:
            env["PEC_CALC_KNOWLEDGE"] = str(self.settings.knowledge_dir)  # company tables and the team's sign-off
        if kb:
            env["KB_INDEX"] = str(self.kb.path)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(job_dir), env=env, stdin=PIPE, stdout=PIPE, stderr=PIPE, limit=STREAM_LIMIT)
        except OSError as exc:
            self._finish(job_id, run_no, None, meta, error=f"เรียกคำสั่ง {self.settings.claude_bin} ไม่ได้: {exc}")
            return
        self.procs[job_id] = proc
        mem_task = asyncio.create_task(self._watch_memory(job_id, proc))
        if job_id in self.cancel_reasons:  # cancelled while we were preparing
            self._keep(asyncio.create_task(self._terminate(proc)))
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

        stderr_task = asyncio.create_task(self._read_stderr(job_id, run_no, proc))
        try:
            await asyncio.wait_for(self._read_stdout(job_id, run_no, proc, meta, str(job_dir)),
                                   timeout=self.settings.job_timeout_min * 60)
        except asyncio.TimeoutError:
            self.cancel_reasons.setdefault(job_id, "timeout")
            await self._terminate(proc)
        exit_code = await proc.wait()
        mem_task.cancel()
        try:
            await asyncio.wait_for(stderr_task, timeout=10)
        except asyncio.TimeoutError:
            stderr_task.cancel()
        self.procs.pop(job_id, None)
        self._finish(job_id, run_no, exit_code, meta, oom=self._oom_reason(job_id, exit_code, oom_before))

    async def _watch_memory(self, job_id: str, proc) -> None:
        """Sample the memory of the CLI and the processes it starts (python3, rg ...); keep the peak.
        The CLI itself counts at its high-water mark, so a peak between two samples is not missed."""
        await asyncio.sleep(MEM_FIRST_SAMPLE_SEC)
        while proc.returncode is None:
            try:
                mb = sysmon.process_tree_mb(proc.pid, root_peak=True)
            except Exception:  # never let a reading break the job
                mb = None
            if mb:
                self.peak_mem[job_id] = max(self.peak_mem.get(job_id, 0), mb)
            await asyncio.sleep(MEM_SAMPLE_SEC)

    def _oom_reason(self, job_id: str, exit_code: int | None, oom_before: int | None) -> tuple[str, int | None] | None:
        """Why the run died for lack of memory, if it did, with the container's cap (MB) for the message."""
        hinted = job_id in self.oom_hint
        self.oom_hint.discard(job_id)
        reading = sysmon.container_memory() or {}
        after, limit = reading.get("oom_kills"), reading.get("limit_mb")
        if oom_before is not None and after is not None and after > oom_before:
            return "container", limit  # the kernel's OOM killer acted inside this container (MEM_LIMIT reached)
        if exit_code in (-9, 137) and job_id not in self.cancel_reasons:
            return "killed", limit  # SIGKILL that we did not send: usually the NAS ran out of memory
        return ("runtime", limit) if hinted else None

    async def _read_stdout(self, job_id: str, run_no: int, proc, meta: RunMeta, job_dir: str) -> None:
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:  # one line longer than STREAM_LIMIT
                self._step(job_id, run_no, "warn", "ข้าม event ที่ยาวเกินไป 1 รายการ")
                continue
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.store.append_event(job_id, run_no, {"type": "_text", "text": line[:4000]})
                self._step(job_id, run_no, "app", line[:300])
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "stream_event":  # token deltas: show live, don't log
                self._track_partial(job_id, event)
                continue
            if event.get("type") == "assistant" and event.get("parent_tool_use_id") is None:
                self.partial.pop(job_id, None)
            self.store.append_event(job_id, run_no, event)
            try:  # one event in a shape this code does not expect must not fail the whole run
                known_session = meta.session_id
                meta.observe(event)
                if meta.session_id and meta.session_id != known_session:
                    self.store.update(job_id, session_id=meta.session_id)
                if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
                    self.state.set_quota(event["rate_limit_info"])
                for step in summarize(event, job_dir, str(self.settings.knowledge_dir)):
                    self._step(job_id, run_no, step["kind"], step["text"])
            except Exception:
                log.exception("event of job %s not understood: %s", job_id, line[:300])
                self._step(job_id, run_no, "warn", f"อ่าน event จาก Claude Code ไม่ได้ 1 รายการ ({event.get('type')})")

    def _track_partial(self, job_id: str, event: dict) -> None:
        if event.get("parent_tool_use_id") is not None:
            return
        inner = event.get("event") or {}
        if inner.get("type") == "message_start":
            self.partial.pop(job_id, None)
        elif inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                self.partial[job_id] = (self.partial.get(job_id, "") + str(delta.get("text", "")))[-20000:]

    async def _read_stderr(self, job_id: str, run_no: int, proc) -> None:
        shown = 0
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if OOM_TEXT.search(line):
                self.oom_hint.add(job_id)
            if line and shown < MAX_STDERR_LINES:
                shown += 1
                self.store.append_event(job_id, run_no, {"type": "_stderr", "text": line[:4000]})
                self._step(job_id, run_no, "stderr", line[:300])

    def _finish(self, job_id: str, run_no: int, exit_code: int | None, meta: RunMeta, error: str | None = None,
                oom: tuple[str, int | None] | None = None) -> None:
        job = self.store.get(job_id)
        job_type = JOB_TYPES[job["type"]]
        job_dir = self.store.job_dir(job_id)
        run = job["runs"][-1]
        peak = self.peak_mem.pop(job_id, None)
        run.update(ended_at=now_iso(), exit_code=exit_code, model=meta.model, session_id=meta.session_id,
                   result=(meta.result or {}).get("result"), usage=meta.usage_summary(), peak_mem_mb=peak)
        reason = self.cancel_reasons.pop(job_id, None)

        if error:
            status, message = FAILED, error
        elif reason == "cancel":
            status, message = CANCELLED, "ยกเลิกโดยผู้ใช้"
        elif reason == "timeout":
            status, message = FAILED, f"หมดเวลา {self.settings.job_timeout_min} นาที (ตั้งได้ที่ JOB_TIMEOUT_MIN)"
        elif meta.quota_reset_at and not meta.succeeded:
            if job.get("quota_waits", 0) < MAX_QUOTA_WAITS:
                resume_at = datetime.fromtimestamp(meta.quota_reset_at + 60).astimezone()
                job["quota_waits"] = job.get("quota_waits", 0) + 1
                job["not_before"] = resume_at.isoformat(timespec="seconds")
                if run["kind"] == "message":
                    job["pending_feedback"] = run.get("feedback")
                    job["pending_files"] = run.get("files") or []
                job["pending_edits"] = sorted(set(job.get("pending_edits") or []) | set(run.get("edited") or []))
                status, message = QUEUED, f"โควต้า subscription เต็ม ระบบจะเริ่มใหม่เองเวลา {resume_at:%H:%M}"
            else:
                status, message = FAILED, "โควต้า subscription เต็มหลายรอบติดกัน"
        elif oom and not meta.succeeded:
            # a sudden spike is killed between two samples, so quote the container's cap, not the sampled peak
            reason, limit = oom
            status, message = FAILED, OOM_MESSAGES[reason] + (f" (เพดาน {limit} MB)" if reason == "container" and limit else "")
        elif not meta.succeeded or (exit_code or 0) != 0:
            message = meta.error_text() or (f"claude จบการทำงานด้วยรหัส {exit_code}" if exit_code else "")
            if AUTH_TEXT.search(message):
                message = LOGIN_MESSAGE
                self._mark_logged_out()
            status, message = FAILED, message or "Claude ทำงานไม่สำเร็จ (ดูรายละเอียดใน events.jsonl)"
        else:
            missing = [o for o in job_type.outputs if not (job_dir / o).is_file()]
            if missing:
                status, message = FAILED, "Claude ไม่ได้สร้างไฟล์ผลลัพธ์: " + ", ".join(missing)
            else:
                status, message = NEEDS_REVIEW, None
                job["warnings"] = sorted(set(job.get("warnings", [])) | set(self._check_outputs(job_dir, job_type)))

        job["status"] = status
        job["error"] = message
        self.store.save(job)
        level, text = {NEEDS_REVIEW: ("ok", "ผลงานพร้อมให้ตรวจ"), QUEUED: ("warn", message or ""),
                       CANCELLED: ("warn", message or ""), FAILED: ("error", message or "")}[status]
        step = {"run": run_no, "kind": "status", "level": level, "text": text}
        usage = meta.usage_summary()
        if status == NEEDS_REVIEW and usage.get("duration_ms"):
            step["detail"] = f"{usage.get('num_turns') or '?'} turns · {round(usage['duration_ms'] / 1000)} วินาที"
            if peak:
                step["detail"] += f" · RAM สูงสุด {peak} MB"
        self.store.append_step(job_id, step)
        if peak:
            history = list(self.state.get("mem_runs") or [])[-(MEM_HISTORY - 1):]
            self.state.set("mem_runs", history + [{"job": job_id, "peak_mb": peak, "status": status, "at": now_iso()}])
        self.last_step.pop(job_id, None)
        self._notify(job, {NEEDS_REVIEW: "needs_review", FAILED: "failed", QUEUED: "quota_wait"}.get(status))
        self.partial.pop(job_id, None)

    def _keep(self, task: asyncio.Task) -> None:
        """Hold a reference to a fire-and-forget task: the event loop keeps only weak ones, so an unreferenced
        task can be garbage-collected before it finishes (asyncio documentation)."""
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _notify(self, job: dict, event: str | None) -> None:
        if not event or self.notifier is None or not self.notifier.channels:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.notifier.job_event(job, event))
        except RuntimeError:  # no loop (sync callers in tests)
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    def _check_outputs(job_dir, job_type: JobType) -> list[str]:
        warnings = []
        for rel in job_type.outputs:
            if rel.endswith(".json"):
                data = read_json(job_dir / rel, default=None)
                if data is None:
                    warnings.append(f"{rel} ไม่ใช่ JSON ที่ถูกต้อง")
                elif rel.endswith("action_items.json") and not isinstance(data, list):
                    warnings.append(f"{rel} ควรเป็นรายการ (JSON array)")
        return warnings

    # -- control -------------------------------------------------------------
    async def _terminate(self, proc) -> None:
        """SIGINT lets Claude Code end the turn cleanly; escalate if it does not exit."""
        for sig, wait in ((signal.SIGINT, 10), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            if proc.returncode is not None:
                return
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                continue

    async def cancel(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in (QUEUED, RUNNING):
            raise ValueError("ยกเลิกได้เฉพาะงานที่อยู่ในคิวหรือกำลังทำ")
        if job_id not in self.active:
            return self.store.update(job_id, status=CANCELLED, error="ยกเลิกโดยผู้ใช้", not_before=None)
        self.cancel_reasons[job_id] = "cancel"
        proc = self.procs.get(job_id)
        if proc is not None:
            self._keep(asyncio.create_task(self._terminate(proc)))
        return job

    def retry(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in (FAILED, CANCELLED):
            raise ValueError("ลองใหม่ได้เฉพาะงานที่ไม่สำเร็จหรือถูกยกเลิก")
        return self.store.update(job_id, status=QUEUED, session_id=None, error=None, not_before=None,
                                 quota_waits=0, pending_feedback=None)

    def check_can_message(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in MESSAGE_STATUSES:
            raise ValueError("ส่งข้อความได้เมื่อ Claude ทำรอบปัจจุบันเสร็จแล้ว")
        return job

    def send_message(self, job_id: str, message: str, files: list[str] | None = None) -> dict:
        """Talk to Claude again in the same job: ask a follow-up, request changes, or continue after a failure.
        Claude resumes its earlier session, so it remembers the files and the conversation. Waits for an approval
        or an edit of the same job to finish first (the review lock), so the two never interleave."""
        from .review import review_lock  # review imports the store too; imported here to keep the modules apart

        with review_lock:
            return self._send_message(job_id, message, files)

    def _send_message(self, job_id: str, message: str, files: list[str] | None) -> dict:
        self.check_can_message(job_id)
        files = list(files or [])
        if not message.strip() and not files:
            raise ValueError("พิมพ์ข้อความหรือแนบไฟล์ก่อนส่ง")
        text = message.strip() or "ดูไฟล์ที่แนบมาเพิ่ม แล้วปรับผลลัพธ์ให้สอดคล้อง"
        return self.store.update(job_id, status=QUEUED, pending_feedback=text, pending_files=files, error=None,
                                 not_before=None, quota_waits=0)

    revise = send_message

    # -- worker loop ---------------------------------------------------------
    def recover(self) -> None:
        """Jobs that were running when the container stopped cannot be continued automatically."""
        for job in self.store.list(limit=5000):
            if job.get("status") == RUNNING:
                if job.get("runs") and not job["runs"][-1].get("ended_at"):
                    job["runs"][-1]["ended_at"] = now_iso()
                job["status"] = FAILED
                job["error"] = "ระบบรีสตาร์ทระหว่างที่งานกำลังทำ กด 'ลองใหม่' เพื่อเริ่มอีกครั้ง"
                self.store.save(job)

    def _start(self, job_id: str) -> None:
        task = asyncio.create_task(self._run_safely(job_id))
        self.active[job_id] = task
        task.add_done_callback(lambda _t: self.active.pop(job_id, None))

    async def _run_safely(self, job_id: str) -> None:
        try:
            await self.run_job(job_id)
        except Exception as exc:  # keep the worker alive whatever happens in one job
            log.exception("job %s crashed", job_id)
            proc = self.procs.pop(job_id, None)
            if proc is not None and proc.returncode is None:  # do not leave Claude running (and spending quota) unwatched
                self._keep(asyncio.create_task(self._terminate(proc)))
            self.peak_mem.pop(job_id, None)
            self.forced_start.pop(job_id, None)
            try:
                self.store.update(job_id, status=FAILED, error=f"ข้อผิดพลาดภายในระบบ: {exc}")
            except Exception:
                log.exception("could not mark job %s as failed", job_id)

    def login_ok(self) -> bool:
        """The dashboard's setup panel promises that jobs wait in the queue until Claude is connected: hold
        them while the last check found Claude Code installed but not signed in."""
        status = self.state.get("claude") or {}
        return not (status.get("installed") and status.get("logged_in") is False)

    def _mark_logged_out(self) -> None:
        """A run just failed for lack of a login: hold the rest of the queue right away."""
        status = dict(self.state.get("claude") or {})
        status.update(installed=True, logged_in=False, checked_at=now_iso(),
                      error="Claude Code ตอบว่ายังไม่ได้ล็อกอิน หรือการล็อกอินหมดอายุ")
        self.state.set("claude", status)

    def memory_gate(self, job_id: str) -> bool:
        """True when this job may start now. While the NAS is short of RAM it keeps its place at the head
        of the queue; after MEM_WAIT_MAX_MIN it starts anyway, with a warning in its timeline."""
        need = self.settings.min_free_mem_mb
        host = sysmon.host_memory() if need > 0 else None
        if host is None or host["available_mb"] >= need:
            self.mem_wait = None
            return True
        wait = self.mem_wait if self.mem_wait and self.mem_wait["job_id"] == job_id else None
        if wait is None:
            start_by = datetime.now().astimezone() + timedelta(minutes=self.settings.mem_wait_max_min)
            wait = self.mem_wait = {"job_id": job_id, "since": now_iso(), "clock": time.monotonic(), "need_mb": need,
                                    "start_by": start_by.isoformat(timespec="seconds")}
            log.info("job %s waits for memory: %s MB available, %s MB wanted", job_id, host["available_mb"], need)
        wait["available_mb"] = host["available_mb"]
        if time.monotonic() - wait["clock"] < self.settings.mem_wait_max_min * 60:
            return False
        self.mem_wait = None
        self.forced_start[job_id] = (f"เริ่มงานทั้งที่ RAM ของ NAS ว่างเพียง {host['available_mb']} MB "
                                     f"(ตั้งไว้ {need} MB) เพราะรอครบ {self.settings.mem_wait_max_min} นาทีแล้ว")
        return True

    def wait_info(self, job_id: str) -> dict | None:
        if self.login_wait:
            return {"reason": "login"}
        wait = self.mem_wait
        if not wait or wait["job_id"] != job_id:
            return None
        return {"reason": "memory", **{k: wait.get(k) for k in ("available_mb", "need_mb", "since", "start_by")}}

    def tick(self) -> None:
        """One scheduling pass: start queued jobs while there is a free slot and enough RAM."""
        free = self.settings.max_concurrent_jobs - len(self.active)
        ready = self.store.runnable(datetime.now().astimezone()) if free > 0 else []
        self.login_wait = bool(ready) and not self.login_ok()
        if self.login_wait:
            self.mem_wait = None
            return
        if self.mem_wait and self.mem_wait["job_id"] not in {j["id"] for j in ready}:
            self.mem_wait = None  # cancelled, or a slot is busy again
        for job in ready:
            if free <= 0:
                break
            if job["id"] in self.active:
                continue
            if not self.memory_gate(job["id"]):
                break  # keep the queue order; look again on the next tick
            self._start(job["id"])
            free -= 1

    async def loop(self) -> None:
        self.recover()
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("worker loop error")
            await asyncio.sleep(2)

