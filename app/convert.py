"""Small input conversions so Claude can read common office files.

Claude Code reads text, PDF and images directly. Word files (.docx, e.g. a Teams transcript) are
zip archives, so we extract their text into a .txt file next to the original. Excel workbooks (.xlsx,
e.g. a customer's BOQ) become one CSV per sheet, "<name> - <sheet>.csv".
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
import xlsx_text  # noqa: E402  (shared with the knowledge index and with Claude's own python3 runs)

from .store import safe_filename  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

TEXT_EXTS = {".txt", ".md", ".vtt", ".srt", ".csv", ".json"}
EXCEL_EXTS = {".xlsx", ".xlsm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}  # what Claude Code's Read tool shows Claude as a picture
READABLE_EXTS = TEXT_EXTS | EXCEL_EXTS | IMAGE_EXTS | {".pdf", ".docx"}
CONVERTED = ".converted.json"  # which workbooks were already turned into CSV (hidden: not listed as an input)
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".mp4", ".webm", ".mov", ".wma"}


MAX_DOCX_XML = 64 * 1024 * 1024  # unpacked word/document.xml: a zip bomb must not fill the NAS's RAM


def docx_to_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        if zf.getinfo("word/document.xml").file_size > MAX_DOCX_XML:
            raise ValueError("word/document.xml ใหญ่ผิดปกติ")
        xml = zf.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for para in root.iter(f"{W}p"):
        parts = []
        for node in para.iter():
            if node.tag == f"{W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs).strip() + "\n"


def _free_name(folder: Path, name: str) -> Path:
    stem, dot, ext = name.rpartition(".")
    candidate, n = folder / name, 2
    while candidate.exists():
        candidate, n = folder / f"{stem} ({n}){dot}{ext}", n + 1
    return candidate


def excel_to_csv(path: Path) -> tuple[list[str], list[str]]:
    """Write "<stem> - <sheet>.csv" next to the workbook for every sheet that has data.
    Returns (files written, warnings)."""
    sheets = xlsx_text.read_workbook(path)
    written, warnings = [], []
    for sheet in sheets:
        if not sheet["rows"]:
            continue
        label = sheet["name"] + (" (ชีตซ่อน)" if sheet["hidden"] else "")
        target = _free_name(path.parent, safe_filename(f"{path.stem} - {label}.csv"))
        target.write_text(xlsx_text.to_csv(sheet["rows"]), encoding="utf-8")
        written.append(target.name)
        if sheet["truncated"]:
            warnings.append(f"{path.name} ชีต {sheet['name']} ยาวเกิน {xlsx_text.MAX_ROWS:,} แถว ใช้เฉพาะส่วนต้น")
    if not written:
        warnings.append(f"{path.name}: ไม่มีข้อมูลในชีตใดเลย")
    return written, warnings


def prepare_inputs(input_dir: Path) -> list[str]:
    """Convert .docx to .txt and .xlsx to CSV per sheet (once each). Returns warnings for files Claude
    cannot read."""
    warnings = []
    if not input_dir.exists():
        return warnings
    done_file = input_dir / CONVERTED
    try:
        done = json.loads(done_file.read_text("utf-8")) if done_file.is_file() else {}
    except (OSError, ValueError):
        done = {}
    changed = False
    for path in sorted(input_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        ext = path.suffix.lower()
        if ext in EXCEL_EXTS:
            stamp = [path.stat().st_size, path.stat().st_mtime_ns]
            previous = done.get(path.name) or {}
            if previous.get("stamp") == stamp:
                continue
            for old in previous.get("outputs", []):  # a new version of the workbook replaces our old CSVs
                (input_dir / old).unlink(missing_ok=True)
            try:
                written, notes = excel_to_csv(path)
            except (xlsx_text.XlsxError, OSError):
                warnings.append(f"อ่านไฟล์ Excel ไม่ได้: {path.name}")
                continue
            warnings += notes
            done[path.name] = {"stamp": stamp, "outputs": written}
            changed = True
        elif ext == ".xls":
            warnings.append(f"{path.name} เป็น Excel รุ่นเก่า (.xls) Claude อ่านไม่ได้ ให้บันทึกเป็น .xlsx หรือ CSV แล้วแนบใหม่")
        elif ext == ".docx":
            target = path.with_suffix(".txt")
            if not target.exists():
                try:
                    target.write_text(docx_to_text(path), encoding="utf-8")
                except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError):
                    warnings.append(f"อ่านไฟล์ Word ไม่ได้ (เสียหาย หรือใหญ่ผิดปกติเมื่อแตกไฟล์): {path.name}")
        elif ext in (".heic", ".heif"):
            warnings.append(f"{path.name} เป็นรูปแบบ HEIC ของ iPhone ซึ่ง Claude เปิดไม่ได้ ให้ส่งเป็น JPG แทน "
                            "(แนบผ่าน Safari จะแปลงเป็น JPG ให้เอง หรือตั้ง กล้อง › รูปแบบ › ใช้ร่วมกันได้มากที่สุด)")
        elif ext in AUDIO_EXTS:
            warnings.append(f"{path.name} เป็นไฟล์เสียง/วิดีโอ ต้องถอดเป็นข้อความก่อน (เช่น transcript จาก Teams)")
        elif ext not in READABLE_EXTS:
            warnings.append(f"{path.name}: Claude อาจอ่านไฟล์ชนิดนี้ไม่ได้")
    if changed:
        done_file.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    return warnings


def has_readable_input(input_dir: Path) -> bool:
    return any(p.is_file() and p.suffix.lower() in READABLE_EXTS for p in input_dir.iterdir()) if input_dir.exists() else False
