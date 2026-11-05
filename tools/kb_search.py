"""kb_search: full-text search over the knowledge folder (PDF, Word, Markdown, text, CSV) and approved outputs.

    python3 -m kb_search "แรงดันตก"              best matches: file, page or line, and the text around them
    python3 -m kb_search "breaker Icu" --full    the whole matching passages
    python3 -m kb_search "IEC" --in knowledge/engineering
    python3 -m kb_search --show knowledge/pricing/price-list.xlsx    a whole file as indexed (Excel, approved outputs)
    python3 -m kb_search --status                what is indexed

The index is SQLite FTS5 with the trigram tokenizer, which matches any substring of 3 or more characters,
so Thai (written without spaces between words) works without a word-segmentation dictionary. PDF text comes
from `pdftotext` (poppler-utils); pages that are images (scans) are read with tesseract when it is installed
(ocr_text.py). The dashboard keeps the index current; this command only reads it, from the file named by
KB_INDEX. Standard library only, so it runs with any python3 and holds no memory between calls.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocr_text  # noqa: E402
import xlsx_text  # noqa: E402

VERSION = "1.0"
TEXT_EXTS = {".md", ".txt", ".csv", ".json", ".vtt", ".srt", ".html", ".xml", ".yaml", ".yml"}
EXCEL_EXTS = {".xlsx", ".xlsm"}
EXTS = TEXT_EXTS | EXCEL_EXTS | {".pdf", ".docx"}
SKIP_IN_ROOT = {"library": {".docx"}}  # Word copies of approved outputs: the .md next to them is indexed
SHOW_MAX_CHARS = 60_000
IGNORED_PREFIXES = (".", "@", "#", "~$")  # Synology @eaDir, #recycle, Office lock files
CHUNK_CHARS = 1200
OVERLAP = 120              # a match that straddles two passages is still found
MAX_FILE_BYTES = 200 * 1024 * 1024
PDF_TIMEOUT = 180
LOCATION = {"pdf": "หน้า", "docx": "ย่อหน้า", "text": "บรรทัด"}
SOURCE = {"knowledge": "คลังความรู้", "library": "ผลงานที่อนุมัติแล้ว"}
KIND = {"pdf": "PDF", "docx": "Word", "xlsx": "Excel", "text": "ข้อความ"}
_W_T = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|<w:tab/>|</w:p>")
# pdftotext puts stray spaces around Thai vowel and tone marks ("แรงดั นตก", "ไฟฟ้ า"); Thai has no spaces inside
# words, so a space touching a mark with Thai on the other side is dropped
_THAI_MARK = "\u0e31\u0e34-\u0e3a\u0e47-\u0e4e"
_SPACE_AFTER_MARK = re.compile(f"(?<=[{_THAI_MARK}]) +(?=[\u0e01-\u0e5b])")
_SPACE_BEFORE_MARK = re.compile(f"(?<=[\u0e01-\u0e5b]) +(?=[{_THAI_MARK}])")


class SearchError(ValueError):
    """Shown to the user as is."""


def fix_pdf_thai(text: str) -> str:
    return _SPACE_BEFORE_MARK.sub("", _SPACE_AFTER_MARK.sub("", text))


def normalise(text: str) -> str:
    """NFC, and SARA AM written as NIKHAHIT + SARA AA (common in text pulled out of Thai PDFs) made one."""
    return unicodedata.normalize("NFC", text).replace("ํา", "ำ")


# ------------------------------------------------------------------ index file
def connect(index: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        if not Path(index).is_file():
            raise SearchError("ยังไม่มีดัชนีคลังความรู้ (ระบบจะสร้างเองเมื่อมีไฟล์ใน knowledge/)")
        conn = sqlite3.connect(f"file:{index}?mode=ro", uri=True, timeout=10)
    else:
        Path(index).parent.mkdir(parents=True, exist_ok=True)
        # plain rollback journal, not WAL: a read-only reader (Claude's search) then needs nothing but read
        # access, and the indexer's transactions are one file each, so readers wait a moment at most
        conn = sqlite3.connect(str(index), timeout=30)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, root TEXT, rel TEXT, size INTEGER,
                mtime_ns INTEGER, kind TEXT, units INTEGER, chars INTEGER, note TEXT, indexed_at TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, loc UNINDEXED, text,
                tokenize='trigram');
        """)
        if "ocr" not in {r[1] for r in conn.execute("PRAGMA table_info(docs)")}:
            conn.execute("ALTER TABLE docs ADD COLUMN ocr INTEGER DEFAULT 0")  # pages read by OCR (an older index)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ text extraction
def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp874"):  # cp874 = Thai Windows, e.g. CSV saved by an older Excel
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


MAX_DOCX_XML = 64 * 1024 * 1024  # unpacked word/document.xml: a zip bomb must not fill the NAS's RAM


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        if zf.getinfo("word/document.xml").file_size > MAX_DOCX_XML:
            raise ValueError("word/document.xml ใหญ่ผิดปกติ")
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    out = []
    for match in _W_T.finditer(xml):
        token = match.group(0)
        out.append("\n" if token == "</w:p>" else "\t" if token == "<w:tab/>" else match.group(1))
    return html.unescape("".join(out)).strip()


def extract(path: Path, ocr=None, should_stop=None) -> tuple[str, list[tuple[int, str]], str, int]:
    """(kind, [(page or line, text)], note, pages read by OCR). The note says why nothing or little could be
    read. `ocr` is an ocr_text.OcrConfig: PDF pages that are images are then read with tesseract, and
    `should_stop()` is asked between those pages (ocr_text.OcrStopped when it says yes)."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        if not shutil.which("pdftotext"):
            return "pdf", [], "อ่าน PDF ไม่ได้: ไม่พบโปรแกรม pdftotext (poppler-utils)", 0
        proc = subprocess.run(["pdftotext", "-enc", "UTF-8", "-q", str(path), "-"], capture_output=True,
                              timeout=PDF_TIMEOUT)
        pages = proc.stdout.decode("utf-8", errors="replace").split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        text_chars = sum(len(p.strip()) for p in pages)
        if proc.returncode != 0 and not text_chars:
            return "pdf", [], "อ่าน PDF ไม่ได้ (ไฟล์เสียหายหรือมีรหัสผ่าน)", 0
        note, read = "", 0
        scant = ocr_text.scant_pages(pages)  # pages that are images
        if scant:
            ok, why = ocr_text.usable(ocr)
            if ok:
                result = ocr_text.ocr_pages(path, scant, ocr, total=len(pages), should_stop=should_stop)
                for number, text in result.pages:
                    pages[number - 1] = text
                read, note = len(result.pages), result.note
            elif len(scant) == len(pages):
                note = f"แทบไม่มีข้อความ (ภาพสแกน) {why}"
            else:
                note = f"มี {len(scant)} หน้าที่เป็นภาพสแกน ซึ่งค้นไม่ได้ {why}"
        return "pdf", [(i, normalise(fix_pdf_thai(p))) for i, p in enumerate(pages, start=1) if p.strip()], note, read
    if ext == ".docx":
        try:
            text = _docx_text(path)
        except (zipfile.BadZipFile, KeyError, ValueError):
            return "docx", [], "อ่านไฟล์ Word ไม่ได้ (เสียหาย หรือใหญ่ผิดปกติเมื่อแตกไฟล์)", 0
        return "docx", [(1, normalise(text))], "", 0
    if ext in EXCEL_EXTS:
        try:
            sheets = xlsx_text.read_workbook(path)
        except xlsx_text.XlsxError:
            return "xlsx", [], "อ่านไฟล์ Excel ไม่ได้", 0
        pages = []
        for sheet in sheets:  # one line per row, "[แถว 12] a | b | c", so a match shows its row
            lines = [f"[แถว {n}] " + " | ".join(values) for n, values in sheet["rows"]]
            if lines:
                pages.append((sheet["name"] + (" (ชีตซ่อน)" if sheet["hidden"] else ""), normalise("\n".join(lines))))
        note = "; ".join(f"ชีต {s['name']} ยาวเกิน {xlsx_text.MAX_ROWS:,} แถว ทำดัชนีเฉพาะส่วนต้น" for s in sheets if s["truncated"])
        return "xlsx", pages, note, 0
    return "text", [(1, normalise(_read_text(path)))], "", 0


def _sheet_passages(pages: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Excel: rows grouped into passages of about CHUNK_CHARS; every passage after a sheet's first repeats
    its header row, so a match deep in a price list still shows what the columns are."""
    out = []
    for sheet, text in pages:
        lines = text.split("\n")
        header = "[หัวตาราง] " + lines[0].split("] ", 1)[-1]
        buf, size = [], 0
        for line in lines:
            if buf and size + len(line) > CHUNK_CHARS:
                out.append((sheet, "\n".join(buf)))
                buf, size = [header], len(header)
            buf.append(line[:CHUNK_CHARS * 2])
            size += len(line) + 1
        if buf:
            out.append((sheet, "\n".join(buf)))
    return out


def passages(kind: str, pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Split into passages of about CHUNK_CHARS at line breaks. The location is the page for PDF, the sheet
    for Excel, and the line (paragraph for Word) where the passage starts otherwise."""
    if kind == "xlsx":
        return _sheet_passages(pages)
    out = []
    for page, text in pages:
        lines, buf, start, size = text.split("\n"), [], None, 0
        for number, line in enumerate(lines, start=1):
            while len(line) > CHUNK_CHARS:  # one huge line (a PDF paragraph, minified JSON)
                if buf:
                    out.append((page if kind == "pdf" else start, "\n".join(buf)))
                    buf, size = [], 0
                out.append((page if kind == "pdf" else number, line[:CHUNK_CHARS]))
                line = line[CHUNK_CHARS - OVERLAP:]
            if size + len(line) > CHUNK_CHARS and buf:
                out.append((page if kind == "pdf" else start, "\n".join(buf)))
                tail = buf[-1][-OVERLAP:]  # the end of the previous line, so the passage starts there
                buf, size, start = [tail], len(tail), number - 1
            if not buf:
                start = number
            buf.append(line)
            size += len(line) + 1
        if "".join(buf).strip():
            out.append((page if kind == "pdf" else start, "\n".join(buf)))
    return [(loc, chunk) for loc, chunk in out if chunk.strip()]


# ------------------------------------------------------------------ keeping the index current
def candidates(roots: dict[str, Path]):
    for name, root in roots.items():
        if not root.is_dir():
            continue
        skip = SKIP_IN_ROOT.get(name, set())
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            ext = path.suffix.lower()
            if (ext in EXTS and ext not in skip and not any(p.startswith(IGNORED_PREFIXES) for p in rel.parts)
                    and path.is_file()):
                yield name, rel.as_posix(), path


def refresh(index: Path, roots: dict[str, Path], should_stop=None, ocr=None) -> dict:
    """Index new and changed files, drop deleted ones. `should_stop()` is asked between files (and between
    OCR pages), so a job that starts (or RAM that runs short) pauses the work; the next refresh carries on
    where this one stopped. `ocr` (ocr_text.OcrConfig) lets scanned pages be read with tesseract."""
    stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "errors": 0, "stopped": False}
    conn = connect(index)
    try:
        known = {r["path"]: (r["size"], r["mtime_ns"]) for r in conn.execute("SELECT path, size, mtime_ns FROM docs")}
        seen = set()
        for root, rel, path in candidates(roots):
            key = f"{root}/{rel}"
            seen.add(key)
            st = path.stat()
            if known.get(key) == (st.st_size, st.st_mtime_ns):
                stats["unchanged"] += 1
                continue
            if should_stop and should_stop():
                stats["stopped"] = True
                break
            note, kind, parts, read = "", "text", [], 0
            if st.st_size > MAX_FILE_BYTES:
                note = f"ไฟล์ใหญ่เกิน {MAX_FILE_BYTES // 1024 // 1024} MB จึงไม่ได้ทำดัชนี"
            else:
                try:
                    kind, pages, note, read = extract(path, ocr=ocr, should_stop=should_stop)
                    parts = passages(kind, pages)
                except ocr_text.OcrStopped:  # the pages read so far are cached; this file is finished next time
                    stats["stopped"] = True
                    break
                except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
                    note, stats["errors"] = f"อ่านไฟล์ไม่ได้: {exc}", stats["errors"] + 1
            with conn:
                conn.execute("DELETE FROM chunks WHERE path = ?", (key,))
                conn.executemany("INSERT INTO chunks (path, loc, text) VALUES (?, ?, ?)",
                                 [(key, loc, text) for loc, text in parts])
                units = len({loc for loc, _ in parts}) if kind == "xlsx" else max((loc for loc, _ in parts), default=0)
                conn.execute("INSERT OR REPLACE INTO docs (path, root, rel, size, mtime_ns, kind, units, chars, note, "
                             "indexed_at, ocr) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (key, root, rel, st.st_size, st.st_mtime_ns, kind, units,
                              sum(len(t) for _, t in parts), note, datetime.now().astimezone().isoformat(timespec="seconds"),
                              read))
            stats["updated" if key in known else "added"] += 1
        if not stats["stopped"]:
            scanned = {name for name, root in roots.items() if root.is_dir()}  # a missing volume must not wipe it
            gone = [k for k in known if k not in seen and k.split("/", 1)[0] in scanned]
            with conn:
                for key in gone:
                    conn.execute("DELETE FROM chunks WHERE path = ?", (key,))
                    conn.execute("DELETE FROM docs WHERE path = ?", (key,))
            stats["removed"] = len(gone)
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('roots', ?)",
                         (json.dumps({k: str(v) for k, v in roots.items()}, ensure_ascii=False),))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('updated_at', ?)",
                         (datetime.now().astimezone().isoformat(timespec="seconds"),))
    finally:
        conn.close()
    return stats


# ------------------------------------------------------------------ reading
def status(index: Path) -> dict:
    try:
        conn = connect(index, readonly=True)
    except SearchError:
        return {"files": 0, "chunks": 0, "by_kind": {}, "by_root": {}, "notes": [], "updated_at": None,
                "ocr_files": 0, "ocr_pages": 0}
    try:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        try:
            rows = conn.execute("SELECT root, kind, note, path, ocr FROM docs").fetchall()
        except sqlite3.OperationalError:  # an index from before OCR, not yet refreshed by the writer
            rows = conn.execute("SELECT root, kind, note, path, 0 AS ocr FROM docs").fetchall()
        by_kind, by_root = {}, {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            by_root[r["root"]] = by_root.get(r["root"], 0) + 1
        return {"files": len(rows), "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
                "by_kind": by_kind, "by_root": by_root, "updated_at": meta.get("updated_at"),
                "notes": [{"path": r["path"], "note": r["note"]} for r in rows if r["note"]][:50],
                "ocr_files": sum(1 for r in rows if r["ocr"]), "ocr_pages": sum(r["ocr"] or 0 for r in rows)}
    except sqlite3.OperationalError:  # the very first refresh is creating the tables right now: nothing in it yet
        return {"files": 0, "chunks": 0, "by_kind": {}, "by_root": {}, "notes": [], "updated_at": None,
                "ocr_files": 0, "ocr_pages": 0}
    finally:
        conn.close()


def _terms(query: str) -> list[str]:
    try:
        parts = shlex.split(query)
    except ValueError:
        parts = query.split()
    return [normalise(p).strip() for p in parts if p.strip()]


def _first_hit(text: str, terms: list[str]) -> int:
    low = text.lower()
    hits = [low.find(t.lower()) for t in terms if low.find(t.lower()) >= 0]
    return min(hits) if hits else 0


def _excerpt(text: str, terms: list[str], width: int = 110) -> str:
    at = _first_hit(text, terms)
    start, end = max(0, at - width), min(len(text), at + width)
    piece = " ".join(text[start:end].split())
    return ("…" if start else "") + piece + ("…" if end < len(text) else "")


def search(index: Path, query: str, limit: int = 8, within: str = "") -> dict:
    """Passages that contain every term. Terms of 3+ characters use the trigram index; shorter ones
    (such as "kA" or "IP") are checked on those results, or scanned for if they are all there is."""
    terms = _terms(query)
    if not terms:
        raise SearchError("พิมพ์คำที่ต้องการค้น")
    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t.lower() for t in terms if len(t) < 3]
    conn = connect(index, readonly=True)
    try:
        roots = json.loads((conn.execute("SELECT value FROM meta WHERE key='roots'").fetchone() or ["{}"])[0])
        where, args = [], []
        if within:  # not LIKE: on a trigram table SQLite sends LIKE to the full-text index, which skips `path`
            prefix = within.strip("/")
            where.append("substr(path, 1, ?) = ?")
            args += [len(prefix), prefix]
        if long_terms:
            sql = ("SELECT path, loc, text FROM chunks WHERE chunks MATCH ?" + "".join(f" AND {w}" for w in where)
                   + " ORDER BY bm25(chunks) LIMIT ?")
            match = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            rows = conn.execute(sql, [match, *args, limit * 5]).fetchall()
        else:
            like = " AND ".join("text LIKE ?" for _ in short_terms)
            sql = f"SELECT path, loc, text FROM chunks WHERE {like}" + "".join(f" AND {w}" for w in where) + " LIMIT ?"
            rows = conn.execute(sql, [*(f"%{t}%" for t in short_terms), *args, limit * 5]).fetchall()
        docs = {r["path"]: r for r in conn.execute("SELECT path, root, rel, kind FROM docs")}
    except sqlite3.OperationalError as exc:
        raise SearchError(f"ค้นไม่ได้: {exc}") from None
    finally:
        conn.close()
    results, seen = [], set()
    for r in rows:
        low = r["text"].lower()
        if any(t not in low for t in short_terms) or (r["path"], r["loc"]) in seen:
            continue
        seen.add((r["path"], r["loc"]))
        results.append(_row(r, docs, roots, terms))
        if len(results) >= limit:
            break
    return {"query": query, "terms": terms, "results": results}


def _row(r, docs: dict, roots: dict, terms: list[str]) -> dict:
    """One search result: where the passage is (file, page / sheet and row / line) and an excerpt."""
    doc = docs.get(r["path"])
    root = doc["root"] if doc else r["path"].split("/", 1)[0]
    rel = doc["rel"] if doc else r["path"].split("/", 1)[-1]
    kind = doc["kind"] if doc else "text"
    base = roots.get(root)
    hit = _first_hit(r["text"], terms)
    if kind == "xlsx":  # the location is the sheet; the row is written at the start of the matching line
        line = r["text"][r["text"].rfind("\n", 0, hit) + 1:]
        row = re.match(r"\[แถว (\d+)\]", line)
        loc, label = r["loc"], f"ชีต {r['loc']}" + (f" · แถว {row.group(1)}" if row else "")
    else:  # a passage starts at `loc`; for text and Word, count down to the line of the match itself
        loc = r["loc"] if kind == "pdf" else r["loc"] + r["text"][:hit].count("\n")
        label = f"{LOCATION.get(kind, 'บรรทัด')} {loc}"
    return {"path": r["path"], "source": root, "rel": rel, "kind": kind, "loc": loc, "loc_label": label,
            "file": str(Path(base) / rel) if base else None, "excerpt": _excerpt(r["text"], terms), "text": r["text"]}


def search_any(index: Path, terms: list[str], limit: int = 8, within: str = "") -> dict:
    """Passages that contain any of the terms, the ones with more of them first (then bm25): for a question
    in plain language, whose every phrase rarely sits in one passage. Terms under 3 characters only count
    towards the ranking (the trigram index cannot look them up)."""
    terms = [t for t in (normalise(t).strip() for t in terms) if t]
    long_terms = [t for t in terms if len(t) >= 3]
    if not long_terms:
        raise SearchError("พิมพ์คำที่ต้องการค้น")
    conn = connect(index, readonly=True)
    try:
        roots = json.loads((conn.execute("SELECT value FROM meta WHERE key='roots'").fetchone() or ["{}"])[0])
        where, args = [], []
        if within:
            prefix = within.strip("/")
            where.append("substr(path, 1, ?) = ?")
            args += [len(prefix), prefix]
        sql = ("SELECT path, loc, text, bm25(chunks) AS score FROM chunks WHERE chunks MATCH ?"
               + "".join(f" AND {w}" for w in where) + " ORDER BY bm25(chunks) LIMIT ?")
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
        rows = conn.execute(sql, [match, *args, limit * 8]).fetchall()
        docs = {r["path"]: r for r in conn.execute("SELECT path, root, rel, kind FROM docs")}
    except sqlite3.OperationalError as exc:
        raise SearchError(f"ค้นไม่ได้: {exc}") from None
    finally:
        conn.close()
    ranked, seen = [], set()
    for r in rows:
        if (r["path"], r["loc"]) in seen:
            continue
        seen.add((r["path"], r["loc"]))
        low = r["text"].lower()
        matched = [t for t in terms if t.lower() in low]
        ranked.append((-len(matched), r["score"], len(ranked), r, matched))
    ranked.sort(key=lambda x: x[:3])
    results = []
    for _, _, _, r, matched in ranked[:limit]:
        results.append({**_row(r, docs, roots, matched or terms), "matched": matched})
    return {"terms": terms, "results": results}


def show(index: Path, path: str) -> dict:
    """Everything indexed for one file, in order: how Claude reads an Excel sheet or an approved output it
    cannot open with Read. `path` is "knowledge/...", "library/...", a path inside the knowledge folder, or
    the absolute path a search result printed."""
    conn = connect(index, readonly=True)
    try:
        roots = json.loads((conn.execute("SELECT value FROM meta WHERE key='roots'").fetchone() or ["{}"])[0])
        wanted = path.strip()
        keys = [wanted, f"knowledge/{wanted.lstrip('/')}"]
        for name, base in roots.items():
            if wanted.startswith(base.rstrip("/") + "/"):
                keys.insert(0, f"{name}/{wanted[len(base.rstrip('/')) + 1:]}")
        doc = next((d for k in keys for d in conn.execute("SELECT * FROM docs WHERE path = ?", (k,))), None)
        if doc is None:
            raise SearchError(f"ไม่พบ {path} ในดัชนี (ดูชื่อไฟล์จากผลค้น หรือ --status)")
        parts = conn.execute("SELECT loc, text FROM chunks WHERE path = ? ORDER BY rowid", (doc["path"],)).fetchall()
    finally:
        conn.close()
    return {"path": doc["path"], "kind": doc["kind"], "note": doc["note"], "passages": [(p["loc"], p["text"]) for p in parts]}


def render_show(doc: dict) -> str:
    kind = doc["kind"]
    out, size, last_loc, previous = [f"{doc['path']} ({KIND.get(kind, kind)}) ทั้งไฟล์ตามที่ทำดัชนีไว้"], 0, None, ""
    if doc["note"]:
        out.append(f"หมายเหตุ: {doc['note']}")
    for loc, text in doc["passages"]:
        lines = text.split("\n")
        if loc != last_loc and (kind in ("pdf", "xlsx")):
            out.append(f"--- {'หน้า' if kind == 'pdf' else 'ชีต'} {loc}")
        elif kind == "xlsx" and lines[0].startswith("[หัวตาราง]"):
            lines = lines[1:]  # the repeated header row
        elif previous.endswith(lines[0]):
            lines = lines[1:]  # the overlap carried over from the previous passage
        last_loc, previous = loc, text
        for line in lines:
            size += len(line) + 1
            if size > SHOW_MAX_CHARS:
                out.append(f"... (ยาวเกิน {SHOW_MAX_CHARS:,} ตัวอักษร ส่วนที่เหลือใช้การค้นด้วยคำแทน)")
                return "\n".join(out)
            out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------ command line
def render(found: dict, full: bool) -> str:
    if not found["results"]:
        return (f"ไม่พบ \"{found['query']}\" ในคลังความรู้ ลองใช้คำอื่น คำที่สั้นลง หรือคำภาษาอังกฤษที่ใช้ในเอกสาร"
                "\n(ไฟล์ข้อความธรรมดาค้นด้วย Grep ได้ด้วย)")
    lines = [f"ผลค้น \"{found['query']}\" {len(found['results'])} รายการ (เรียงจากตรงที่สุด)"]
    for i, r in enumerate(found["results"], start=1):
        where = r["file"] if r["source"] == "knowledge" and r["file"] else f"{SOURCE.get(r['source'], r['source'])}: {r['rel']}"
        lines.append(f"{i}. {where} · {r['loc_label']}")
        body = r["text"].strip() if full else r["excerpt"]
        lines += [f"   {line}" for line in body.splitlines() if line.strip()]
    lines.append("อ่านต่อ: ไฟล์ในคลังความรู้เปิดด้วย Read ได้ (PDF ระบุหน้าได้) ส่วนไฟล์ Excel และผลงานที่อนุมัติแล้ว "
                 "ใช้ --show <ไฟล์> หรือ --full")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python3 -m kb_search", description="ค้นข้อความในคลังความรู้ (PDF, Word, ข้อความ) และผลงานที่อนุมัติแล้ว")
    parser.add_argument("query", nargs="*", help="คำค้น หลายคำ = ต้องมีครบทุกคำ ใช้ \"...\" ค้นทั้งวลี")
    parser.add_argument("--limit", type=int, default=8, help="จำนวนผลสูงสุด (ค่าตั้งต้น 8)")
    parser.add_argument("--in", dest="within", default="", help="จำกัดโฟลเดอร์ เช่น knowledge/engineering หรือ library/proposal-draft")
    parser.add_argument("--full", action="store_true", help="แสดงข้อความทั้งช่วง ไม่ใช่แค่รอบคำที่พบ")
    parser.add_argument("--status", action="store_true", help="สรุปว่าทำดัชนีอะไรไว้บ้าง")
    parser.add_argument("--show", metavar="PATH", help="แสดงทั้งไฟล์ตามที่ทำดัชนีไว้ เช่น ไฟล์ Excel หรือผลงานที่อนุมัติแล้ว")
    parser.add_argument("--json", action="store_true", help="แสดงผลเป็น JSON")
    parser.add_argument("--version", action="version", version=f"kb_search {VERSION}")
    args = parser.parse_args(argv)
    index = Path(os.environ.get("KB_INDEX", "/data/state/kb.sqlite"))
    try:
        if args.show:
            doc = show(index, args.show)
            print(json.dumps(doc, ensure_ascii=False, indent=2) if args.json else render_show(doc))
            return 0
        if args.status:
            info = status(index)
            kinds = ", ".join(f"{KIND.get(k, k)} {n}" for k, n in sorted(info["by_kind"].items()))
            print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else
                  f"ดัชนี {info['files']} ไฟล์ ({kinds or 'ยังไม่มี'}) {info['chunks']} ช่วงข้อความ · อัปเดต {info['updated_at'] or '-'}")
            return 0
        found = search(index, " ".join(args.query), limit=max(1, min(args.limit, 50)), within=args.within)
    except SearchError as exc:
        print(f"ข้อผิดพลาด: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(found, ensure_ascii=False, indent=2) if args.json else render(found, args.full))
    return 0


if __name__ == "__main__":
    sys.exit(main())
