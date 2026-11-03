"""Is this installation ready? One list of checks, each saying in Thai what to do about it.

Run it after installing or updating: System page → ความพร้อมของระบบ, or in the container
    sudo docker exec ai-work-assistant python3 -m app.readiness
(exit code 1 when something must be fixed).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import sysmon
from .config import Settings

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
GUIDE = "docs/setup-ds925.md"
REFUSED = "ผู้ช่วยงานไม่อนุญาต"


def item(key: str, label: str, status: str, detail: str, fix: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "fix": fix}


def check_claude(claude: dict | None, settings: Settings) -> list[dict]:
    label = "Claude Code และการล็อกอิน"
    if not claude:
        return [item("claude", label, WARN, "ยังไม่ได้ตรวจ", "กด ตรวจสอบการเชื่อมต่อ ในหน้าระบบ")]
    if claude.get("provider") == "fake-claude" or settings.claude_bin.endswith("fake-claude"):
        return [item("claude", label, WARN, "โหมดสาธิต: CLAUDE_BIN ชี้ไปที่ fake-claude งานจะไม่ใช้ Claude จริง",
                     "ลบบรรทัด CLAUDE_BIN ในไฟล์ .env (หรือใส่ claude) แล้ว Build ใหม่")]
    if not claude.get("installed"):
        return [item("claude", label, FAIL, claude.get("error") or "ไม่พบคำสั่ง claude",
                     "สั่ง Build project ใหม่ใน Container Manager (Dockerfile ติดตั้ง Claude Code ให้)")]
    if not claude.get("logged_in"):
        return [item("claude", label, FAIL, f"ติดตั้งแล้ว ({claude.get('version') or '?'}) แต่ยังไม่ได้ล็อกอิน",
                     "sudo docker exec -it ai-work-assistant claude auth login แล้วทำตามข้อ 5 ของคู่มือ")]
    return [item("claude", label, OK, f"ล็อกอินแล้ว ({claude.get('method') or 'subscription'}) · {claude.get('version') or ''}")]


def _count_files(root: Path, cap: int = 5000) -> int:
    n = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.name.startswith((".", "@", "#")):
            n += 1
            if n >= cap:
                break
    return n


def check_folders(settings: Settings) -> list[dict]:
    out = []
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / f".write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        free = shutil.disk_usage(settings.data_dir).free / 1e9
        out.append(item("data", "โฟลเดอร์ข้อมูล", OK if free >= 2 else WARN,
                        f"{settings.data_dir} เขียนได้ · พื้นที่ว่าง {free:.1f} GB",
                        "" if free >= 2 else "พื้นที่ว่างไม่ถึง 2 GB ลบงานเก่าที่ไม่ใช้ หรือขยาย volume"))
    except OSError as exc:
        out.append(item("data", "โฟลเดอร์ข้อมูล", FAIL, f"เขียน {settings.data_dir} ไม่ได้: {exc}",
                        f"ตรวจ DATA_PATH ในไฟล์ .env และสิทธิ์ของโฟลเดอร์ใน File Station (ข้อ 1 ของ {GUIDE})"))
    knowledge = settings.knowledge_dir
    if not knowledge.is_dir():
        out.append(item("knowledge", "คลังความรู้", FAIL, f"ไม่พบ {knowledge}",
                        f"ตรวจ KNOWLEDGE_PATH ในไฟล์ .env (ข้อ 7 ของ {GUIDE})"))
    else:
        files = _count_files(knowledge)
        if not files:
            out.append(item("knowledge", "คลังความรู้", WARN, "ยังไม่มีไฟล์",
                            "วางคู่มือออกแบบ price list และข้อมูลลูกค้า (customers/<ชื่อ>/profile.md) ตามข้อ 7"))
        elif os.access(knowledge, os.W_OK):
            out.append(item("knowledge", "คลังความรู้", WARN, f"{files:,} ไฟล์ แต่ระบบเขียนลงคลังความรู้ได้",
                            "mount แบบอ่านอย่างเดียว (:ro) อย่างใน docker-compose.yml เพื่อให้ Claude แก้คลังความรู้ไม่ได้"))
        else:
            out.append(item("knowledge", "คลังความรู้", OK, f"{files:,} ไฟล์ · อ่านอย่างเดียว"))
    home = Path.home()
    if os.access(home, os.W_OK):
        out.append(item("claude_home", "ที่เก็บการล็อกอิน Claude", OK, f"{home} เขียนได้ (อยู่ต่อแม้ Build ใหม่ถ้า mount เป็น volume)"))
    else:
        out.append(item("claude_home", "ที่เก็บการล็อกอิน Claude", FAIL, f"{home} เขียนไม่ได้",
                        "ตรวจ CLAUDE_HOME_PATH ในไฟล์ .env และสิทธิ์ของโฟลเดอร์ claude-home"))
    return out


def check_tools(settings: Settings) -> list[dict]:
    """pdftotext, and the python3 that Claude will run: it must load the jail and find pec_calc."""
    out = []
    out.append(item("pdftotext", "อ่าน PDF ในคลังความรู้", OK if shutil.which("pdftotext") else WARN,
                    "พบ pdftotext" if shutil.which("pdftotext") else "ไม่พบ pdftotext ค้นข้อความใน PDF ไม่ได้",
                    "" if shutil.which("pdftotext") else "สั่ง Build image ใหม่ (Dockerfile ติดตั้ง poppler-utils ให้)"))
    if not settings.allow_python:
        out.append(item("python", "Python ของ Claude", INFO, "ปิดอยู่ (ALLOW_PYTHON=false) งานคำนวณจะไม่รัน python3"))
        return out
    python = shutil.which("python3")
    if not python:
        return out + [item("python", "Python ของ Claude", FAIL, "ไม่พบ python3", "สั่ง Build image ใหม่")]
    with tempfile.TemporaryDirectory() as jail:
        env = {**os.environ, "PYTHONPATH": str(TOOLS_DIR), "AIWA_PYTHON_JAIL": jail, "PYTHONDONTWRITEBYTECODE": "1"}

        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([python, *args], cwd=jail, env=env, capture_output=True, text=True, timeout=60)

        try:
            fence = run("-c", "import socket; socket.socket()")
            calc = run("-m", "pec_calc", "current", "--kw", "10", "--v", "400", "--phase", "3")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return out + [item("python", "รั้ว Python ของ Claude", FAIL, f"รัน {python} ไม่ได้: {exc}", "สั่ง Build image ใหม่")]
    if fence.returncode != 0 and REFUSED in fence.stderr:
        out.append(item("python_jail", "รั้ว Python ของ Claude", OK,
                        f"{python} ต่อเครือข่าย เรียกโปรแกรมอื่น และเขียนนอกโฟลเดอร์งานไม่ได้"))
    else:
        out.append(item("python_jail", "รั้ว Python ของ Claude", FAIL,
                        f"{python} ยังสร้างการเชื่อมต่อเครือข่ายได้ ({(fence.stderr or fence.stdout).strip()[-120:] or 'ไม่มี error'})",
                        "ตั้ง ALLOW_PYTHON=false ในไฟล์ .env ไว้ก่อน แล้วตรวจว่า tools/sitecustomize.py อยู่ใน image"))
    if calc.returncode == 0:
        out.append(item("pec_calc", "เครื่องมือคำนวณ pec_calc", OK, "python3 -m pec_calc ทำงานได้"))
    else:
        out.append(item("pec_calc", "เครื่องมือคำนวณ pec_calc", FAIL, (calc.stderr or calc.stdout).strip()[-160:],
                        "ตรวจว่าโฟลเดอร์ tools/ อยู่ใน image (Dockerfile: COPY tools ./tools)"))
    return out


def valid_time(value: str) -> bool:
    try:
        hour, minute = (int(x) for x in value.split(":"))
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def check_settings(settings: Settings, channels: list[str], kb_files: int | None) -> list[dict]:
    out = []
    if settings.dashboard_password:
        out.append(item("password", "รหัสผ่าน dashboard", OK, "ตั้งแล้ว"))
    else:
        out.append(item("password", "รหัสผ่าน dashboard", WARN, "ยังไม่ได้ตั้ง ใครในวง LAN ก็เปิดและสั่งงานได้",
                        "ตั้ง DASHBOARD_PASSWORD ในไฟล์ .env แล้ว Build ใหม่"))
    now = datetime.now().astimezone()
    offset = now.utcoffset() or timedelta(0)
    hours = offset.total_seconds() / 3600
    zone = f"UTC{hours:+g}"
    if hours == 7:
        out.append(item("timezone", "เขตเวลา", OK, f"{now:%H:%M} ({zone})"))
    elif hours == 0:
        out.append(item("timezone", "เขตเวลา", WARN, f"{now:%H:%M} ({zone}) งานประจำและติดตามงานจะเริ่มผิดเวลา",
                        "ตั้ง TZ=Asia/Bangkok (docker-compose.yml ตั้งไว้แล้ว ถ้าลบออกให้ใส่คืน)"))
    else:
        out.append(item("timezone", "เขตเวลา", INFO, f"{now:%H:%M} ({zone})"))
    for name, value in (("FOLLOWUP_TIME", settings.followup_time), ("DIGEST_TIME", settings.digest_time)):
        if value and not valid_time(value):
            out.append(item(name.lower(), f"เวลาใน {name}", FAIL, f'"{value}" ไม่ใช่เวลา',
                            f"ใส่เป็น ชั่วโมง:นาที 24 ชั่วโมง เช่น 07:30 ในไฟล์ .env"))
    host = sysmon.host_memory()
    box = sysmon.container_memory(host_total_mb=host["total_mb"] if host else None)
    if box is None:
        out.append(item("memory", "หน่วยความจำ", INFO, "อ่านค่า RAM ของ container ไม่ได้ (ไม่ได้รันใน container?)"))
    elif not box.get("limit_mb"):
        out.append(item("memory", "หน่วยความจำ", WARN, "ยังไม่ได้จำกัด RAM ของ container",
                        "ตั้ง MEM_LIMIT ในไฟล์ .env (ค่าตั้งต้นใน docker-compose.yml คือ 1536m) ข้อ 13 ของคู่มือ"))
    else:
        free = f" · NAS ว่าง {host['available_mb']:,} MB" if host else ""
        short = host and settings.min_free_mem_mb and host["available_mb"] < settings.min_free_mem_mb
        out.append(item("memory", "หน่วยความจำ", WARN if short else OK, f"เพดาน container {box['limit_mb']:,} MB{free}",
                        "RAM ว่างน้อยกว่าที่งานต้องใช้ งานจะรอจนว่าง ปิดแพ็กเกจที่ไม่ใช้ใน DSM" if short else ""))
    if channels and not settings.dashboard_url:
        out.append(item("notify", "แจ้งเตือน", WARN, f"ตั้งไว้ ({', '.join(channels)}) แต่ข้อความจะไม่มีลิงก์เปิดงาน",
                        "ตั้ง DASHBOARD_URL เป็นที่อยู่ที่เปิด dashboard (เช่น http://192.168.1.20:8080)"))
    elif channels:
        out.append(item("notify", "แจ้งเตือน", OK, f"ส่งทาง {', '.join(channels)} · กดส่งข้อความทดสอบได้ในหน้าระบบ"))
    else:
        out.append(item("notify", "แจ้งเตือน", INFO, "ไม่ได้ตั้ง (ไม่บังคับ) ข้อ 8 ของคู่มือ"))
    if settings.mail_host and settings.mail_from and settings.mail_security not in ("starttls", "ssl", "none"):
        out.append(item("mail", "ส่งอีเมลจาก dashboard", WARN, f"MAIL_SMTP_SECURITY={settings.mail_security} ไม่รู้จัก",
                        "ใช้ starttls (port 587), ssl (port 465) หรือ none (เซิร์ฟเวอร์ใน LAN ที่ไม่เข้ารหัส)"))
    elif settings.mail_host and settings.mail_from:
        out.append(item("mail", "ส่งอีเมลจาก dashboard", OK,
                        f"ผ่าน {settings.mail_host}:{settings.mail_port} จาก {settings.mail_from} · กดส่งอีเมลทดสอบได้ในหน้าระบบ"))
    elif settings.mail_host or settings.mail_from:
        out.append(item("mail", "ส่งอีเมลจาก dashboard", WARN, "ตั้งค่าไม่ครบ",
                        "ต้องมีทั้ง MAIL_SMTP_HOST และ MAIL_FROM ในไฟล์ .env (ข้อ 8.2 ของคู่มือ)"))
    else:
        out.append(item("mail", "ส่งอีเมลจาก dashboard", INFO, "ไม่ได้ตั้ง (ไม่บังคับ) ข้อ 8.2 ของคู่มือ"))
    if not settings.kb_enabled:
        out.append(item("kb", "ดัชนีค้นคลังความรู้", INFO, "ปิดอยู่ (KB_SEARCH=false)"))
    elif kb_files:
        out.append(item("kb", "ดัชนีค้นคลังความรู้", OK, f"ค้นได้ {kb_files:,} ไฟล์"))
    else:
        out.append(item("kb", "ดัชนีค้นคลังความรู้", INFO,
                        "ยังว่าง ระบบทำดัชนีเองเมื่อไม่มีงานและ RAM ว่าง (กด อัปเดตดัชนีตอนนี้ ในหน้าระบบได้)"))
    return out


def run_checks(settings: Settings, claude: dict | None, channels: list[str], kb_files: int | None) -> dict:
    items = (check_claude(claude, settings) + check_folders(settings) + check_tools(settings)
             + check_settings(settings, channels, kb_files))
    worst = FAIL if any(i["status"] == FAIL for i in items) else WARN if any(i["status"] == WARN for i in items) else OK
    return {"status": worst, "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), "items": items}


def main() -> int:
    """`python3 -m app.readiness` inside the container."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from .claude_status import check_claude as probe_claude
    from .knowledge import KnowledgeIndex
    from .notify import Notifier

    settings = Settings.from_env()
    claude = asyncio.run(probe_claude(settings))
    try:
        kb_files = KnowledgeIndex(settings).status()["files"]
    except Exception:  # an unreadable index is reported as empty, never a crash of the check
        kb_files = None
    result = run_checks(settings, claude, Notifier(settings).channels, kb_files)
    marks = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}
    for i in result["items"]:
        print(f"{marks[i['status']]} {i['label']}: {i['detail']}")
        if i["fix"]:
            print(f"    → {i['fix']}")
    summary = {OK: "พร้อมใช้งาน", WARN: "ใช้งานได้ แต่มีข้อควรแก้", FAIL: "ยังไม่พร้อม แก้ข้อที่มีเครื่องหมาย ✗ ก่อน"}
    print(f"\n{summary[result['status']]}")
    return 1 if result["status"] == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
