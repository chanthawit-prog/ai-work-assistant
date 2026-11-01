"""Turn `claude -p --output-format stream-json` events into short Thai timeline steps.

Event shapes follow the Agent SDK message types (system/init, assistant, user, result,
system/api_retry, rate_limit_event ...). Unknown events are kept in events.jsonl but not shown.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


QUOTA_WINDOWS = {"five_hour": "รอบ 5 ชั่วโมง", "seven_day": "รายสัปดาห์",
                 "seven_day_opus": "รายสัปดาห์ (Opus)", "seven_day_sonnet": "รายสัปดาห์ (Sonnet)"}


def _trunc(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split()) if limit <= 300 else str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_path(path: Any, job_dir: str, knowledge_dir: str = "") -> str:
    """Show paths the way the user knows them: relative to the job folder, or knowledge/..."""
    path = str(path or "").rstrip("/")
    jd = job_dir.rstrip("/")
    if path == jd:
        return "โฟลเดอร์งาน"
    if path.startswith(jd + "/"):
        return path[len(jd) + 1:]
    kd = knowledge_dir.rstrip("/")
    if kd and (path == kd or path.startswith(kd + "/")):
        return "knowledge" + path[len(kd):]
    return path


def reset_time_text(resets_at: Any) -> str:
    """resetsAt is a Unix timestamp; accept seconds or milliseconds."""
    try:
        ts = float(resets_at)
    except (TypeError, ValueError):
        return ""
    if ts > 1e12:
        ts /= 1000
    return datetime.fromtimestamp(ts).astimezone().strftime("%d/%m %H:%M")


def describe_tool(name: str, inp: dict, job_dir: str, knowledge_dir: str = "") -> str:
    inp = inp if isinstance(inp, dict) else {}
    path = _short_path(inp.get("file_path") or inp.get("path") or "", job_dir, knowledge_dir)
    if name == "Read":
        return f"อ่านไฟล์ {path}"
    if name == "Write":
        return f"เขียนไฟล์ {path}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"แก้ไขไฟล์ {path}"
    if name == "Glob":
        return f"ค้นหาไฟล์ {inp.get('pattern', '')}" + (f" ใน {path}" if path else "")
    if name == "Grep":
        return f"ค้นหาข้อความ “{_trunc(inp.get('pattern', ''), 80)}”" + (f" ใน {path}" if path else "")
    if name == "Bash":
        command = str(inp.get("command", "")).replace(job_dir.rstrip("/"), "<โฟลเดอร์งาน>")
        if knowledge_dir:
            command = command.replace(knowledge_dir.rstrip("/"), "knowledge")
        return f"รันคำสั่ง: {_trunc(command, 160)}"
    if name == "WebSearch":
        return f"ค้นเว็บ: {_trunc(inp.get('query', ''), 120)}"
    if name == "WebFetch":
        return f"เปิดเว็บ: {_trunc(inp.get('url', ''), 120)}"
    if name in ("Agent", "Task"):
        return f"มอบงานย่อยให้ subagent: {_trunc(inp.get('description', ''), 120)}"
    if name == "TodoWrite":
        return "จัดลำดับขั้นตอนการทำงาน"
    return f"ใช้เครื่องมือ {name}"


def _tool_result_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def summarize(event: dict, job_dir: str, knowledge_dir: str = "") -> list[dict]:
    """Return zero or more {kind, text} steps for one stream-json event."""
    etype = event.get("type")
    sub = event.get("parent_tool_use_id") is not None
    prefix = "↳ " if sub else ""

    if etype == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            model = event.get("model") or "?"
            return [{"kind": "start", "text": f"Claude เริ่มทำงาน · model {model}"}]
        if subtype == "api_retry":
            err = event.get("error") or event.get("error_category") or "unknown"
            delay = round((event.get("retry_delay_ms") or 0) / 1000)
            return [{"kind": "warn", "text": f"เรียก Claude ไม่สำเร็จ ({err}) จะลองใหม่ครั้งที่ "
                                             f"{event.get('attempt', '?')}/{event.get('max_retries', '?')} ในอีก {delay} วินาที"}]
        if subtype == "permission_denied":
            tool = event.get("tool_name") or event.get("tool") or "?"
            return [{"kind": "warn", "text": f"ไม่อนุญาตให้ใช้เครื่องมือ {tool} (ตามที่ตั้งค่าไว้)"}]
        if subtype == "task_started" and not event.get("ambient"):
            return [{"kind": "tool", "text": f"เริ่มงานย่อย: {_trunc(event.get('description', ''), 120)}"}]
        return []

    if etype == "assistant":
        steps = []
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                steps.append({"kind": "text", "text": prefix + _trunc(block["text"], 1200)})
            elif block.get("type") == "tool_use":
                steps.append({"kind": "tool", "text": prefix + describe_tool(block.get("name", ""), block.get("input"), job_dir, knowledge_dir)})
        if event.get("error"):
            steps.append({"kind": "warn", "text": f"Claude แจ้งข้อผิดพลาด: {event['error']}"})
        return steps

    if etype == "user":
        content = (event.get("message") or {}).get("content")
        steps = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    steps.append({"kind": "warn", "text": prefix + "เครื่องมือแจ้งข้อผิดพลาด: "
                                  + _trunc(_tool_result_text(block.get("content")), 240)})
        return steps

    if etype == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        status = info.get("status")
        when = reset_time_text(info.get("resetsAt"))
        pct = info.get("utilization")
        pct_text = f" ({round(float(pct) * 100) if float(pct) <= 1 else round(float(pct))}%)" if isinstance(pct, (int, float)) else ""
        window = QUOTA_WINDOWS.get(info.get("rateLimitType"), "")
        name = "โควต้า subscription" + (f" {window}" if window else "")
        if status == "allowed_warning":
            return [{"kind": "warn", "text": f"{name} ใกล้เต็ม{pct_text}" + (f", รีเซ็ต {when}" if when else "")}]
        if status == "rejected":
            extra = " ต้องซื้อ usage เพิ่ม" if info.get("errorCode") == "credits_required" else ""
            return [{"kind": "error", "text": f"{name} เต็มแล้ว" + (f", รีเซ็ต {when}" if when else "") + extra}]
        return []

    if etype == "result":
        secs = round((event.get("duration_ms") or 0) / 1000)
        turns = event.get("num_turns", "?")
        if event.get("subtype") == "success" and not event.get("is_error"):
            return [{"kind": "result", "text": f"Claude ทำงานเสร็จ · {turns} รอบ · {secs} วินาที"}]
        detail = "; ".join(event.get("errors") or []) or _trunc(event.get("result", ""), 300)
        return [{"kind": "error", "text": f"Claude ทำงานไม่สำเร็จ ({event.get('subtype', 'error')})" + (f": {detail}" if detail else "")}]

    return []


class RunMeta:
    """Facts collected from one run's event stream."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.model: str | None = None
        self.result: dict | None = None
        self.rate_limit: dict | None = None

    def observe(self, event: dict) -> None:
        if event.get("session_id") and not self.session_id:
            self.session_id = event["session_id"]
        if event.get("type") == "system" and event.get("subtype") == "init":
            self.model = event.get("model")
        elif event.get("type") == "result":
            self.result = event
            if event.get("session_id"):
                self.session_id = event["session_id"]
        elif event.get("type") == "rate_limit_event":
            self.rate_limit = event.get("rate_limit_info") or {}

    @property
    def succeeded(self) -> bool:
        return bool(self.result) and self.result.get("subtype") == "success" and not self.result.get("is_error")

    @property
    def quota_reset_at(self) -> float | None:
        """Unix time when a rejected subscription quota resets, if the run hit the limit."""
        info = self.rate_limit or {}
        if info.get("status") != "rejected" or info.get("resetsAt") is None:
            return None
        try:
            ts = float(info["resetsAt"])
        except (TypeError, ValueError):
            return None
        return ts / 1000 if ts > 1e12 else ts

    def error_text(self) -> str:
        if not self.result:
            return ""
        errors = self.result.get("errors") or []
        return "; ".join(map(str, errors)) or _trunc(self.result.get("result", ""), 500)

    def usage_summary(self) -> dict:
        if not self.result:
            return {}
        usage = self.result.get("usage") or {}
        return {
            "num_turns": self.result.get("num_turns"),
            "duration_ms": self.result.get("duration_ms"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
        }
