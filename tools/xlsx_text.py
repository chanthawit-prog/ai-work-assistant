"""xlsx_text: the cell values of an Excel workbook (.xlsx / .xlsm), with the standard library only.

Each sheet is streamed (iterparse), so a large price list needs little RAM. Text and numbers come out as
stored (a formula gives the value Excel saved with the file), dates as YYYY-MM-DD; formatting, charts and
images are left out. Old binary .xls files are not supported: save them as .xlsx or CSV.

    python3 -m xlsx_text BOQ.xlsx              every sheet as CSV
    python3 -m xlsx_text BOQ.xlsx --sheet BOQ  one sheet
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import posixpath
import re
import sys
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
MAX_ROWS = 100_000          # per sheet: enough for any price list, a guard against runaway files
# uncompressed size limits of the XML parts: a zip bomb (a few KB that unpack to GBs) must not fill the NAS's RAM
MB = 1024 * 1024
LIMITS = {"xl/sharedStrings.xml": 128 * MB, "xl/styles.xml": 32 * MB, "xl/workbook.xml": 16 * MB,
          "xl/_rels/workbook.xml.rels": 16 * MB}
MAX_SHEET_XML = 256 * MB
# Excel's built-in number formats that show dates or times (ECMA-376 18.8.30), including the East Asian ids
# and the Thai ones (71-81: ว/ด/ปปปป, ช:นน ...), which Thai Excel uses without writing the format code
BUILTIN_DATE = set(range(14, 18)) | {22} | set(range(27, 37)) | set(range(50, 59)) | {71, 72, 73, 74, 77, 81}
BUILTIN_TIME = set(range(18, 22)) | {45, 46, 47, 75, 76, 78, 79, 80}
# quoted text, escapes, [colour] / [$-locale], AM/PM, and the word General (LibreOffice saves it as a custom format)
_FORMAT_NOISE = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]|am/pm|a/p|general', re.IGNORECASE)


class XlsxError(ValueError):
    """The file is not a readable .xlsx workbook."""


def _open(zf: zipfile.ZipFile, name: str, limit: int | None = None):
    """Open a part of the workbook, refusing one that unpacks to more than its limit."""
    size = zf.getinfo(name).file_size
    if size > (limit or LIMITS.get(name, MAX_SHEET_XML)):
        raise XlsxError(f"{name} ใหญ่ผิดปกติ ({size // MB:,} MB เมื่อแตกไฟล์)")
    return zf.open(name)


def _column(ref: str) -> int:
    """"A1" -> 0, "BC12" -> 54."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    out = []
    for _event, el in ET.iterparse(_open(zf, "xl/sharedStrings.xml"), events=("end",)):
        if el.tag == NS + "si":
            parts = []
            for child in el:  # plain <t>, or rich-text runs <r><t>; skip phonetic guides <rPh>
                if child.tag == NS + "t":
                    parts.append(child.text or "")
                elif child.tag == NS + "r":
                    parts += [t.text or "" for t in child.iter(NS + "t")]
            out.append("".join(parts))
            el.clear()
    return out


def _date_styles(zf: zipfile.ZipFile) -> dict[int, str]:
    """{cell style index: "date" or "time"} for the styles whose number format shows one."""
    if "xl/styles.xml" not in zf.namelist():
        return {}
    root = ET.parse(_open(zf, "xl/styles.xml")).getroot()
    custom = {int(n.get("numFmtId", "-1")): n.get("formatCode", "") for n in root.iter(NS + "numFmt")}
    xfs = root.find(NS + "cellXfs")
    kinds = {}
    for i, xf in enumerate(xfs if xfs is not None else []):
        fmt = int(xf.get("numFmtId", "0"))
        kind = _format_kind(custom[fmt]) if fmt in custom else "date" if fmt in BUILTIN_DATE else "time" if fmt in BUILTIN_TIME else None
        if kind:
            kinds[i] = kind
    return kinds


def _format_kind(code: str) -> str | None:
    """"date", "time" or None (a number). Digit placeholders make it a number ("0.00E+00"); d, y and the
    Buddhist-year codes b and e make it a date; h or s alone make it a time ("h:mm")."""
    code = _FORMAT_NOISE.sub("", code.split(";")[0])
    if any(ch in code for ch in "0#?"):
        return None
    letters = {ch.lower() for ch in code if ch.isalpha()}
    if letters & set("dybe"):
        return "date"
    return "time" if letters & set("hs") else None


def _is_date_format(code: str) -> bool:
    return _format_kind(code) is not None


def _sheets(zf: zipfile.ZipFile) -> tuple[list[tuple[str, str, bool]], bool]:
    workbook = ET.parse(_open(zf, "xl/workbook.xml")).getroot()
    rels = ET.parse(_open(zf, "xl/_rels/workbook.xml.rels")).getroot()
    targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(PKG_REL)}
    props = workbook.find(NS + "workbookPr")
    date1904 = props is not None and props.get("date1904") in ("1", "true")
    sheets = []
    for sheet in workbook.iter(NS + "sheet"):
        target = targets.get(sheet.get(R_ID))
        if not target:
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        sheets.append((sheet.get("name", ""), path, sheet.get("state") in ("hidden", "veryHidden")))
    return sheets, date1904


def _number(text: str, kind: str | None, date1904: bool) -> str:
    try:
        value = float(text)
    except ValueError:
        return text
    if not math.isfinite(value) or abs(value) >= 1e15 or (kind and not 0 <= value < 2_958_466):  # 9999-12-31
        return text  # too big for a date, a time or an int: shown as stored (an id or a phone number in a date column)
    if kind == "time":  # the time of day in the fraction
        seconds = round((value % 1) * 86400)
        hours, rest = divmod(seconds % 86400, 3600)
        return f"{hours:02d}:{rest // 60:02d}" + (f":{rest % 60:02d}" if rest % 60 else "")
    if kind == "date":
        if date1904:
            moment = datetime(1904, 1, 1) + timedelta(days=value)
        else:  # serial 60 is Excel's imaginary 29 Feb 1900
            moment = datetime(1899, 12, 30 if value >= 61 else 31) + timedelta(days=value)
        return moment.strftime("%Y-%m-%d") if value == int(value) else moment.strftime("%Y-%m-%d %H:%M")
    if value == int(value):
        return str(int(value))
    return format(value, ".15g")


def _rows(zf: zipfile.ZipFile, path: str, strings: list[str], dates: dict[int, str], date1904: bool, max_rows: int):
    """(row number, [cell text ...]) for each non-empty row; stops after max_rows rows (then yields None)."""
    count, last = 0, 0
    for _event, el in ET.iterparse(_open(zf, path, MAX_SHEET_XML), events=("end",)):
        if el.tag != NS + "row":
            continue
        number = int(el.get("r") or last + 1)
        last = number
        cells, col = {}, -1
        for c in el.iter(NS + "c"):
            col = _column(c.get("r")) if c.get("r") else col + 1
            kind, v = c.get("t", "n"), c.find(NS + "v")
            if kind == "s":
                text = strings[int(v.text)] if v is not None and v.text and int(v.text) < len(strings) else ""
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(NS + "t"))
            elif kind == "b":
                text = "TRUE" if v is not None and v.text == "1" else "FALSE"
            elif v is None or v.text is None:
                text = ""
            elif kind in ("str", "e"):
                text = v.text
            else:
                text = _number(v.text, dates.get(int(c.get("s", "0"))), date1904)
            if text.strip():
                cells[col] = text
        el.clear()
        if cells:
            count += 1
            if count > max_rows:
                yield None
                return
            yield number, [cells.get(i, "") for i in range(max(cells) + 1)]


def read_workbook(path, max_rows: int = MAX_ROWS) -> list[dict]:
    """[{"name", "hidden", "rows": [(row number, [values])], "truncated"}] for every sheet, in workbook order."""
    try:
        with zipfile.ZipFile(path) as zf:
            strings, dates = _shared_strings(zf), _date_styles(zf)
            sheets, date1904 = _sheets(zf)
            out = []
            for name, sheet_path, hidden in sheets:
                rows, truncated = [], False
                if sheet_path in zf.namelist():
                    for row in _rows(zf, sheet_path, strings, dates, date1904, max_rows):
                        if row is None:
                            truncated = True
                            break
                        rows.append(row)
                out.append({"name": name, "hidden": hidden, "rows": rows, "truncated": truncated})
            return out
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, ArithmeticError) as exc:
        raise XlsxError(str(exc)) from None


def to_csv(rows: list[tuple[int, list[str]]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for _number_, values in rows:
        writer.writerow(values)
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python3 -m xlsx_text", description="แสดงค่าในไฟล์ Excel (.xlsx) เป็น CSV")
    parser.add_argument("file")
    parser.add_argument("--sheet", help="ชื่อชีต (ไม่ระบุ = ทุกชีต)")
    args = parser.parse_args(argv)
    try:
        sheets = read_workbook(args.file)
    except (XlsxError, OSError) as exc:
        print(f"ข้อผิดพลาด: อ่านไฟล์ Excel ไม่ได้ ({exc})", file=sys.stderr)
        return 2
    chosen = [s for s in sheets if not args.sheet or s["name"] == args.sheet]
    if not chosen:
        print(f"ข้อผิดพลาด: ไม่พบชีต {args.sheet} (มี {', '.join(s['name'] for s in sheets)})", file=sys.stderr)
        return 2
    for sheet in chosen:
        if len(chosen) > 1:
            print(f"# ชีต {sheet['name']}")
        sys.stdout.write(to_csv(sheet["rows"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
