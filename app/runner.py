"""Run jobs with the official Claude Code CLI (`claude -p`), signed in with your own Claude subscription.

The model work happens on Claude's servers; this process only starts the CLI in the job folder,
feeds it the task, and records its stream-json events so the dashboard can show progress.

Subscription rules this module follows:
* it runs the unmodified `claude` binary and never touches your credentials;
* it never uses `--bare` (bare mode ignores subscription logins);
* it removes ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from the CLI environment, because an API key
  would take precedence over the subscription and be billed per token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
from asyncio.subprocess import PIPE
from datetime import datetime

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
MAX_QUOTA_WAITS = 3
MAX_STDERR_LINES = 40
BASE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
MESSAGE_STATUSES = (NEEDS_REVIEW, DONE, FAILED, CANCELLED)  # states in which you can talk to Claude again


def claude_env() -> dict:
    env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(key, None)
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


class Runner:
    def __init__(self, store: JobStore, tasks: TaskStore, state: RuntimeState, settings: Settings):
        self.store = store
        self.tasks = tasks
        self.state = state
        self.settings = settings
        self.active: dict[str, asyncio.Task] = {}
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.cancel_reasons: dict[str, str] = {}
        self.last_step: dict[str, str] = {}
        self.partial: dict[str, str] = {}  # text Claude is typing right now (from --include-partial-messages)

    # -- building a run ------------------------------------------------------
    def build_command(self, job_type: JobType, resume_session: str | None = None) -> list[str]:
        tools, allowed = list(BASE_TOOLS), list(BASE_TOOLS)
        if job_type.allow_python and self.settings.allow_python:
            tools.append("Bash")
            allowed.append("Bash(python3 *)")
        cmd = [
            self.settings.claude_bin, "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--permission-mode", "acceptEdits",
            "--permission-prompts", "none",
            "--tools", ",".join(tools),
            "--allowedTools", ",".join(allowed),
            "--append-system-prompt", system_prompt(job_type, self.settings),
        ]
        if self.settings.knowledge_dir.is_dir():
            cmd += ["--add-dir", str(self.settings.knowledge_dir)]
        if self.settings.claude_model:
            cmd += ["--model", self.settings.claude_model]
        if self.settings.claude_effort:
            cmd += ["--effort", self.settings.claude_effort]
        if resume_session:
            cmd += ["--resume", resume_session]
        return cmd + list(self.settings.claude_extra_args)

    def prepare_context(self, job: dict, job_type: JobType) -> list[str]:
        """Copy in material Claude needs but should not be able to modify."""
        ctx = self.store.area_dir(job["id"], "context")
        ctx.mkdir(parents=True, exist_ok=True)
        if job_type.id == "follow-up":
            write_json(ctx / "action_items.json", self.tasks.open_items())
        elif job_type.id == "sales-brief":
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

    # -- running -------------------------------------------------------------
    def _step(self, job_id: str, run: int, kind: str, text: str) -> None:
        self.store.append_step(job_id, {"run": run, "kind": kind, "text": text})
        self.last_step[job_id] = text

    @staticmethod
    def request_text(job: dict, job_type: JobType) -> str:
        """What the user asked for, as shown in the conversation."""
        parts = []
        if job_type.id != "general" or not job.get("instructions"):
            parts.append(f"{job_type.label}: {job['title']}")
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
        resume = job.get("session_id") if feedback else None
        run_no = len(job["runs"]) + 1
        kind = "message" if feedback else ("retry" if job["runs"] else "initial")

        warnings = prepare_inputs(job_dir / "input")
        inputs = [f["path"] for f in self.store.list_files(job_id, "input")]
        context = self.prepare_context(job, job_type)
        prompt = task_message(job, job_type, self.settings, inputs, context, feedback=feedback, resumed=bool(resume))
        cmd = self.build_command(job_type, resume_session=resume)

        run = {"n": run_no, "kind": kind, "feedback": feedback, "started_at": now_iso(), "ended_at": None,
               "exit_code": None, "model": None, "result": None, "usage": {}}
        self.store.update(job_id, status=RUNNING, runs=job["runs"] + [run], pending_feedback=None, error=None,
                          not_before=None, warnings=sorted(set(job.get("warnings", [])) | set(warnings)))
        if kind == "retry":
            self._step(job_id, run_no, "app", "เริ่มทำงานนี้ใหม่อีกครั้ง")
        else:
            text = feedback if kind == "message" else self.request_text(job, job_type)
            self.store.append_step(job_id, {"run": run_no, "kind": "user", "text": text,
                                            "files": inputs if kind == "initial" else []})

        meta = RunMeta()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(job_dir), env=claude_env(), stdin=PIPE, stdout=PIPE, stderr=PIPE, limit=STREAM_LIMIT)
        except OSError as exc:
            self._finish(job_id, run_no, None, meta, error=f"เรียกคำสั่ง {self.settings.claude_bin} ไม่ได้: {exc}")
            return
        self.procs[job_id] = proc
        if job_id in self.cancel_reasons:  # cancelled while we were preparing
            asyncio.create_task(self._terminate(proc))
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
        try:
            await asyncio.wait_for(stderr_task, timeout=10)
        except asyncio.TimeoutError:
            stderr_task.cancel()
        self.procs.pop(job_id, None)
        self._finish(job_id, run_no, exit_code, meta)

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
            known_session = meta.session_id
            meta.observe(event)
            if meta.session_id and meta.session_id != known_session:
                self.store.update(job_id, session_id=meta.session_id)
            if event.get("type") == "rate_limit_event":
                self.state.set_quota(event.get("rate_limit_info") or {})
            for step in summarize(event, job_dir):
                self._step(job_id, run_no, step["kind"], step["text"])

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
            if line and shown < MAX_STDERR_LINES:
                shown += 1
                self.store.append_event(job_id, run_no, {"type": "_stderr", "text": line[:4000]})
                self._step(job_id, run_no, "stderr", line[:300])

    def _finish(self, job_id: str, run_no: int, exit_code: int | None, meta: RunMeta, error: str | None = None) -> None:
        job = self.store.get(job_id)
        job_type = JOB_TYPES[job["type"]]
        job_dir = self.store.job_dir(job_id)
        run = job["runs"][-1]
        run.update(ended_at=now_iso(), exit_code=exit_code, model=meta.model, session_id=meta.session_id,
                   result=(meta.result or {}).get("result"), usage=meta.usage_summary())
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
                status, message = QUEUED, f"โควต้า subscription เต็ม ระบบจะเริ่มใหม่เองเวลา {resume_at:%H:%M}"
            else:
                status, message = FAILED, "โควต้า subscription เต็มหลายรอบติดกัน"
        elif not meta.succeeded or (exit_code or 0) != 0:
            message = meta.error_text() or (f"claude จบการทำงานด้วยรหัส {exit_code}" if exit_code else "")
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
        self.store.append_step(job_id, step)
        self.last_step.pop(job_id, None)
        self.partial.pop(job_id, None)

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
            asyncio.create_task(self._terminate(proc))
        return job

    def retry(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in (FAILED, CANCELLED):
            raise ValueError("ลองใหม่ได้เฉพาะงานที่ไม่สำเร็จหรือถูกยกเลิก")
        return self.store.update(job_id, status=QUEUED, session_id=None, error=None, not_before=None,
                                 quota_waits=0, pending_feedback=None)

    def send_message(self, job_id: str, message: str) -> dict:
        """Talk to Claude again in the same job: ask a follow-up, request changes, or continue after a failure.
        Claude resumes its earlier session, so it remembers the files and the conversation."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in MESSAGE_STATUSES:
            raise ValueError("ส่งข้อความได้เมื่อ Claude ทำรอบปัจจุบันเสร็จแล้ว")
        if not message.strip():
            raise ValueError("พิมพ์ข้อความก่อนส่ง")
        return self.store.update(job_id, status=QUEUED, pending_feedback=message.strip(), error=None,
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
            self.procs.pop(job_id, None)
            try:
                self.store.update(job_id, status=FAILED, error=f"ข้อผิดพลาดภายในระบบ: {exc}")
            except Exception:
                log.exception("could not mark job %s as failed", job_id)

    async def loop(self) -> None:
        self.recover()
        while True:
            try:
                free = self.settings.max_concurrent_jobs - len(self.active)
                for job in self.store.runnable(datetime.now().astimezone()) if free > 0 else []:
                    if free <= 0:
                        break
                    if job["id"] not in self.active:
                        self._start(job["id"])
                        free -= 1
            except Exception:
                log.exception("worker loop error")
            await asyncio.sleep(2)

