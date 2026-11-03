"""ocr_text: text from scanned PDFs, for the knowledge index and for files attached to a job.

A page that pdftotext finds (almost) no text on is an image: it is rendered by pdftoppm (poppler-utils) at
300 dpi in grey and read by tesseract with the Thai and English models (OCR_LANGS). Pages are done one at a
time, with tesseract held to two threads, so a 6 GB NAS stays usable; the text of every page read is cached
in DATA_DIR/state/ocr/<sha256 of the file>.json, so a file is read once even when it is attached again or the
index is rebuilt, and a run that is stopped between pages (a job starts) carries on from there next time.
Standard library only, like the other tools.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1"                 # cached pages made by another version are read again
DPI = 300                     # 200 dpi misreads small Latin text in Thai documents ("Icu" became a Thai word)
PAGE_TIMEOUT = 180            # tesseract, one page (a dense A4 scan takes 5-15 s on a NAS)
RENDER_TIMEOUT = 120          # pdftoppm, one page
TEXT_TIMEOUT = 180            # pdftotext, whole file
THREADS = "2"                 # OMP_THREAD_LIMIT: tesseract otherwise takes every core
SCANT_CHARS = 40              # a page with fewer characters from pdftotext is treated as an image
DEFAULT_LANGS = "tha+eng"
DEFAULT_MAX_PAGES = 40
_THAI = "ก-๛"
_SPACE_IN_THAI = re.compile(f"(?<=[{_THAI}]) +(?=[{_THAI}])")  # tesseract spaces Thai words apart, and sometimes letters
_tools_cache: dict | None = None


class OcrStopped(Exception):
    """should_stop() answered yes between pages; what was read is in the cache."""


@dataclass
class OcrConfig:
    enabled: bool = True
    langs: str = DEFAULT_LANGS
    max_pages: int = DEFAULT_MAX_PAGES
    cache_dir: Path | None = None


@dataclass
class OcrResult:
    total: int                                  # pages in the PDF
    pages: list[tuple[int, str]] = field(default_factory=list)  # (page number, text) for the pages read
    skipped: int = 0                            # image pages beyond max_pages
    seconds: float = 0.0
    from_cache: int = 0                         # how many of the pages came from the cache

    @property
    def note(self) -> str:
        if not self.pages:
            return "ภาพสแกน อ่านด้วย OCR แล้วไม่พบข้อความ (หน้าว่างหรือภาพไม่ชัด)"
        if self.skipped:
            return (f"ภาพสแกน {self.total} หน้า อ่านด้วย OCR เฉพาะ {len(self.pages)} หน้าแรก "
                    f"(เพิ่ม OCR_MAX_PAGES ในไฟล์ .env ถ้าต้องการทั้งหมด)")
        return ""


# ------------------------------------------------------------------ what is installed
def tools(refresh: bool = False) -> dict:
    """tesseract (version and languages) and pdftoppm, looked up once: they change only with a new image."""
    global _tools_cache
    if _tools_cache is not None and not refresh:
        return _tools_cache
    tess = shutil.which("tesseract")
    info = {"tesseract": bool(tess), "pdftoppm": shutil.which("pdftoppm") is not None, "version": "", "langs": []}
    if tess:
        try:
            out = subprocess.run([tess, "--version"], capture_output=True, timeout=20)
            first = (out.stdout + out.stderr).decode("utf-8", "replace").strip().splitlines()
            info["version"] = first[0].replace("tesseract", "").strip() if first else ""
            langs = subprocess.run([tess, "--list-langs"], capture_output=True, timeout=20)
            lines = (langs.stdout + langs.stderr).decode("utf-8", "replace").splitlines()
            info["langs"] = sorted(l.strip() for l in lines if l.strip() and " " not in l.strip() and l.strip() != "osd")
        except (OSError, subprocess.SubprocessError):
            pass
    _tools_cache = info
    return info


def usable(config: OcrConfig | None) -> tuple[bool, str]:
    """(True, "") when OCR can run here; otherwise (False, why not), in Thai, for notes and the readiness check."""
    if config is None or not config.enabled:
        return False, "ปิด OCR อยู่ (OCR=false ในไฟล์ .env)"
    info = tools()
    if not info["tesseract"]:
        return False, "ไม่ได้ติดตั้ง OCR (tesseract) สั่ง Build image ใหม่เพื่อติดตั้ง"
    if not info["pdftoppm"]:
        return False, "ไม่พบ pdftoppm (poppler-utils) สั่ง Build image ใหม่เพื่อติดตั้ง"
    missing = [l for l in config.langs.split("+") if l and l not in info["langs"]]
    if missing:
        return False, f"ไม่มีข้อมูลภาษา {', '.join(missing)} ของ tesseract (ติดตั้ง tesseract-ocr-{missing[0]})"
    return True, ""


# ------------------------------------------------------------------ the PDF
def page_texts(pdf: Path) -> list[str]:
    """The text pdftotext finds on each page ([] when pdftotext is missing or the file cannot be opened)."""
    if not shutil.which("pdftotext"):
        return []
    proc = subprocess.run(["pdftotext", "-enc", "UTF-8", "-q", str(pdf), "-"], capture_output=True, timeout=TEXT_TIMEOUT)
    pages = proc.stdout.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    if proc.returncode != 0 and not any(p.strip() for p in pages):
        return []
    return pages


def page_count(pdf: Path, fallback: int = 0) -> int:
    if shutil.which("pdfinfo"):
        try:
            out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
            match = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
            if match:
                return int(match.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    return fallback


def scant_pages(texts: list[str]) -> list[int]:
    """Page numbers (from 1) that pdftotext found fewer than SCANT_CHARS characters on: images to read by OCR."""
    return [i for i, text in enumerate(texts, start=1) if len(text.strip()) < SCANT_CHARS]


def clean(text: str) -> str:
    """NFC, SARA AM as one character, no spaces inside Thai runs, tidy blank lines."""
    text = unicodedata.normalize("NFC", text).replace("ํา", "ำ")
    text = _SPACE_IN_THAI.sub("", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ------------------------------------------------------------------ the cache
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_path(config: OcrConfig, pdf: Path) -> Path | None:
    if config.cache_dir is None:
        return None
    return Path(config.cache_dir) / f"{file_sha256(pdf)}.json"


def _load_cache(path: Path | None, config: OcrConfig) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("version") != VERSION or data.get("langs") != config.langs or data.get("dpi") != DPI:
        return {}
    return {int(k): v for k, v in (data.get("pages") or {}).items()}


def _save_cache(path: Path | None, config: OcrConfig, total: int, pages: dict[int, str]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": VERSION, "langs": config.langs, "dpi": DPI, "total": total,
                                   "pages": {str(k): v for k, v in sorted(pages.items())}}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written only costs time next run


# ------------------------------------------------------------------ reading pages
def read_page(pdf: Path, page: int, langs: str, workdir: Path) -> str:
    """Render one page and read it. Raises subprocess.SubprocessError / OSError when a tool fails."""
    prefix = workdir / f"p{page}"
    subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-f", str(page), "-l", str(page), "-singlefile", "-png",
                    str(pdf), str(prefix)], capture_output=True, timeout=RENDER_TIMEOUT, check=True)
    png = prefix.with_suffix(".png")
    if not png.is_file():
        raise OSError(f"pdftoppm wrote no image for page {page}")
    cmd = ["tesseract", str(png), "-", "-l", langs, "--psm", "3", "--dpi", str(DPI)]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10", *cmd]
    env = {**os.environ, "OMP_THREAD_LIMIT": THREADS}
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=PAGE_TIMEOUT, env=env)
    finally:
        png.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise subprocess.SubprocessError(f"tesseract exit {proc.returncode}: "
                                         f"{proc.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    return clean(proc.stdout.decode("utf-8", errors="replace"))


def ocr_pages(pdf: Path, pages: list[int], config: OcrConfig, total: int = 0, should_stop=None) -> OcrResult:
    """Read the given pages (image pages, from 1), the first max_pages of them, cached ones first.
    `should_stop()` is asked between pages; a yes raises OcrStopped, with the pages read so far cached."""
    started = time.monotonic()
    wanted = pages[:max(1, config.max_pages)]
    result = OcrResult(total=total or len(pages), skipped=max(0, len(pages) - len(wanted)))
    cache_path = _cache_path(config, pdf)
    cached = _load_cache(cache_path, config)
    todo = [p for p in wanted if p not in cached]
    result.from_cache = len(wanted) - len(todo)
    if todo:
        with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
            for page in todo:
                if should_stop and should_stop():
                    _save_cache(cache_path, config, result.total, cached)
                    raise OcrStopped()
                cached[page] = read_page(pdf, page, config.langs, Path(tmp))
                _save_cache(cache_path, config, result.total, cached)  # after every page: a stop keeps it
    result.pages = [(p, cached[p]) for p in wanted if cached.get(p, "").strip()]
    result.seconds = time.monotonic() - started
    return result


def as_text(pdf_name: str, result: OcrResult) -> str:
    """The .ocr.txt file put next to a scanned PDF attached to a job."""
    head = (f"ข้อความที่อ่านด้วย OCR จากไฟล์ {pdf_name} ({len(result.pages)} หน้า"
            f"{f' จาก {result.total} หน้า' if result.skipped or len(result.pages) < result.total else ''})\n"
            "ภาพสแกนอ่านด้วยโปรแกรม ตัวสะกดและตัวเลขอาจคลาดเคลื่อน ค่าที่สำคัญให้เปิดดู PDF ต้นฉบับประกอบ\n")
    body = "".join(f"\n=== หน้า {page} ===\n{text}\n" for page, text in result.pages)
    return head + body
