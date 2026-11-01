"""Check that the Claude Code CLI is installed and signed in (`claude --version`, `claude auth status --json`).

The dashboard only reads this status. Signing in is done by you, through Claude Code's own flow
(`claude auth login` inside the container, or a token from `claude setup-token`); this app never
asks for, stores or forwards your Claude credentials.
"""
from __future__ import annotations

import asyncio
import json
from asyncio.subprocess import PIPE

from .config import Settings
from .runner import claude_env
from .store import now_iso


async def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE, env=claude_env())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def check_claude(settings: Settings) -> dict:
    status = {"checked_at": now_iso(), "installed": False, "logged_in": False,
              "method": None, "provider": None, "version": None, "error": None}
    try:
        _, out = await _run([settings.claude_bin, "--version"], timeout=30)
        status["installed"] = True
        status["version"] = (out.strip().splitlines() or [""])[0][:80]
    except FileNotFoundError:
        status["error"] = f"ไม่พบคำสั่ง {settings.claude_bin} (ติดตั้ง Claude Code ใน container แล้วหรือยัง)"
        return status
    except (OSError, asyncio.TimeoutError) as exc:
        status["error"] = f"เรียก {settings.claude_bin} ไม่สำเร็จ: {exc!r}"
        return status
    try:
        _, out = await _run([settings.claude_bin, "auth", "status", "--json"], timeout=30)
        data = json.loads(out)
        status["logged_in"] = bool(data.get("loggedIn"))
        status["method"] = data.get("authMethod")
        status["provider"] = data.get("apiProvider")
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError, AttributeError) as exc:
        status["error"] = f"อ่านสถานะการล็อกอินไม่ได้: {exc!r}"
    return status
