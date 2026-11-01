"""Small input conversions so Claude can read common meeting files.

Claude Code reads text, PDF and images directly. Word files (.docx, e.g. a Teams transcript) are
zip archives, so we extract their text into a .txt file next to the original.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

TEXT_EXTS = {".txt", ".md", ".vtt", ".srt", ".csv", ".json"}
READABLE_EXTS = TEXT_EXTS | {".pdf", ".docx", ".png", ".jpg", ".jpeg"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".mp4", ".webm", ".mov", ".wma"}


def docx_to_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
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


def prepare_inputs(input_dir: Path) -> list[str]:
    """Convert .docx files to .txt (once). Returns warnings for files Claude cannot read."""
    warnings = []
    for path in sorted(input_dir.iterdir()) if input_dir.exists() else []:
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext == ".docx":
            target = path.with_suffix(".txt")
            if not target.exists():
                try:
                    target.write_text(docx_to_text(path), encoding="utf-8")
                except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
                    warnings.append(f"อ่านไฟล์ Word ไม่ได้: {path.name}")
        elif ext in AUDIO_EXTS:
            warnings.append(f"{path.name} เป็นไฟล์เสียง/วิดีโอ ต้องถอดเป็นข้อความก่อน (เช่น transcript จาก Teams)")
        elif ext not in READABLE_EXTS:
            warnings.append(f"{path.name}: Claude อาจอ่านไฟล์ชนิดนี้ไม่ได้")
    return warnings


def has_readable_input(input_dir: Path) -> bool:
    return any(p.is_file() and p.suffix.lower() in READABLE_EXTS for p in input_dir.iterdir()) if input_dir.exists() else False
