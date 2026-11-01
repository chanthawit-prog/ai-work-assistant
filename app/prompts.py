"""Build the system prompt (house rules + job-type rules) and the task message for one run.

Prompt files live in app/prompts/. To customise one without rebuilding the image, copy it to
DATA_DIR/config/prompts/<same name>.md on the NAS and edit it there; that copy wins.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Settings
from .jobtypes import JobType

BUNDLED_DIR = Path(__file__).parent / "prompts"
# what the files the app puts in context/ are, so Claude does not have to guess
CONTEXT_NOTES = {
    "action_items.json": "รายการติดตามงาน (action item) ที่ยังเปิดอยู่ จากการประชุมที่ผู้ใช้อนุมัติแล้ว",
    "recent/": "ผลงานที่ผู้ใช้ตรวจและอนุมัติแล้วใน 7 วันล่าสุด แยกโฟลเดอร์ตามชนิดงาน",
    "meetings/": "สรุปประชุมที่อนุมัติแล้วซึ่งกล่าวถึงลูกค้ารายนี้",
}
THAI_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]


def load_prompt(name: str, settings: Settings) -> str:
    override = settings.prompt_override_dir / name
    path = override if override.is_file() else BUNDLED_DIR / name
    return path.read_text("utf-8")


def _fill(text: str, settings: Settings) -> str:
    return text.replace("{KNOWLEDGE_DIR}", str(settings.knowledge_dir))


def system_prompt(job_type: JobType, settings: Settings) -> str:
    base = _fill(load_prompt("_base.md", settings), settings)
    rules = _fill(load_prompt(f"{job_type.id}.md", settings), settings)
    return f"{base.strip()}\n\n{rules.strip()}\n"


def today_text(now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    return f"{now:%Y-%m-%d} (วัน{THAI_DAYS[now.weekday()]}) เวลา {now:%H:%M}"


def task_message(job: dict, job_type: JobType, settings: Settings, inputs: list[str],
                 context: list[str], feedback: str | None = None, resumed: bool = False,
                 new_files: list[str] | None = None) -> str:
    outputs = [f"- {o}" for o in job_type.outputs]
    attached = ["", "ไฟล์ที่แนบมากับข้อความนี้ (อยู่ใน input/):", *[f"- input/{f}" for f in new_files]] if new_files else []
    if feedback and resumed:
        return "\n".join([
            "ข้อความใหม่จากผู้ใช้:",
            feedback.strip(),
            *attached,
            "",
            "ถ้าเป็นคำถาม ให้ตอบได้เลย ถ้าเป็นคำขอให้แก้หรือทำเพิ่ม ให้ทำในโฟลเดอร์ output/",
            "โดยไฟล์ผลลัพธ์หลักต้องยังอยู่ครบ:",
            *outputs,
            "",
            "ตอบกลับสั้น ๆ ว่าทำอะไรไป หรือคำตอบของคำถาม",
        ])

    lines = [
        f"งาน: {job_type.label}",
        f"ชื่องาน: {job['title']}",
        f"วันนี้: {today_text()}",
    ]
    params = job.get("params") or {}
    if params.get("schedule_id"):
        lines.append(f"ที่มา: งานประจำ \"{params.get('schedule_name') or params['schedule_id']}\" ที่ระบบสร้างให้ตามเวลา "
                     "ให้เขียนผลสำหรับรอบนี้ (อ้างอิงวันที่วันนี้)")
    customer = params.get("customer")
    if customer:
        lines.append(f"ลูกค้า: {customer}")
    lines += ["", "ไฟล์ input:"] + ([f"- input/{f}" for f in inputs] or ["- (ไม่มี)"])
    if context:
        lines += ["", "ข้อมูลประกอบที่ระบบเตรียมไว้:"] + [f"- context/{f}" for f in context]
        notes = [f"- context/{key} คือ{text}" for key, text in CONTEXT_NOTES.items()
                 if any(f == key or f.startswith(key) for f in context)]
        if notes:
            lines += ["", "ความหมายของข้อมูลประกอบ:", *notes]
    lines += ["", f"คลังความรู้ (อ่านอย่างเดียว): {settings.knowledge_dir}"]
    if job.get("instructions"):
        lines += ["", "คำสั่ง / คำถามจากผู้ใช้:", job["instructions"]]
    if feedback:
        lines += ["", "ข้อความเพิ่มเติมจากผู้ใช้ (หลังรอบก่อนหน้า):", feedback.strip()]
    lines += ["", "ไฟล์ผลลัพธ์ที่ต้องสร้าง:", *outputs]
    return "\n".join(lines)
