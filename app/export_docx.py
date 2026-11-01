"""Turn Claude's Markdown output into a Word document that looks like a PEC report.

* Body text in TH Sarabun New 16 pt, the Thai office standard (Word substitutes if it is not installed).
* Headings in PEC navy, table headers on the light grey PEC uses for note-style tables, A4 page.
* Ends with the same title block the dashboard shows: file, job, Rev., date.
* Put a company template at KNOWLEDGE_DIR/templates/report-template.docx and its header, footer,
  logo and styles are used instead of the built-in ones.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

THAI_FONT = "TH Sarabun New"
MONO_FONT = "Consolas"
NAVY = RGBColor(0x0E, 0x1F, 0x57)      # BLUE_900
BLUE = RGBColor(0x18, 0x3B, 0xB2)      # BLUE_650
GRAY_700 = RGBColor(0x34, 0x40, 0x54)
GRAY_500 = RGBColor(0x66, 0x70, 0x85)
HEAD_FILL = "E4E7EC"                   # GRAY_200
CODE_FILL = "F2F4F8"                   # GRAY_100
RULE = "122A7B"                        # BLUE_800

_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|(?<![\w*])\*[^*\s][^*]*\*(?![\w*]))")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}")


# ------------------------------------------------------------------ low-level formatting
def _font_xml(rpr, name: str | None = None, size: float | None = None, bold: bool | None = None,
              italic: bool | None = None, color: RGBColor | None = None) -> None:
    """Set fonts for Latin *and* complex scripts: Thai is a complex script in Word (w:cs, w:szCs, w:bCs)."""
    if name:
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.insert(0, rfonts)
        for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            rfonts.set(qn(attr), name)
    if bold is not None:
        for tag in ("w:b", "w:bCs"):
            el = rpr.find(qn(tag))
            if el is None:
                el = OxmlElement(tag)
                rpr.append(el)
            el.set(qn("w:val"), "1" if bold else "0")
    if italic is not None:
        for tag in ("w:i", "w:iCs"):
            el = rpr.find(qn(tag))
            if el is None:
                el = OxmlElement(tag)
                rpr.append(el)
            el.set(qn("w:val"), "1" if italic else "0")
    if color is not None:
        el = rpr.find(qn("w:color"))
        if el is None:
            el = OxmlElement("w:color")
            rpr.append(el)
        el.set(qn("w:val"), str(color))
    if size:
        for tag in ("w:sz", "w:szCs"):
            el = rpr.find(qn(tag))
            if el is None:
                el = OxmlElement(tag)
                rpr.append(el)
            el.set(qn("w:val"), str(int(size * 2)))


def _style(doc, style_name: str, **font) -> None:
    try:
        style = doc.styles[style_name]
    except KeyError:
        return
    _font_xml(style.element.get_or_add_rPr(), **font)


def _shade(cell_or_par, fill: str) -> None:
    props = cell_or_par._tc.get_or_add_tcPr() if hasattr(cell_or_par, "_tc") else cell_or_par._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    props.append(shd)


def _bottom_rule(paragraph, color: str = RULE, size: int = 12) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("w:val", "single"), ("w:sz", str(size)), ("w:space", "1"), ("w:color", color)):
        bottom.set(qn(key), value)
    borders.append(bottom)
    ppr.append(borders)


def _add_inline(paragraph, text: str, base_bold: bool = False) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = paragraph.add_run(part[2:-2])
            _font_xml(run._element.get_or_add_rPr(), bold=True)
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            _font_xml(run._element.get_or_add_rPr(), name=MONO_FONT, size=12)
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            _font_xml(run._element.get_or_add_rPr(), italic=True)
        else:
            run = paragraph.add_run(part)
            if base_bold:
                _font_xml(run._element.get_or_add_rPr(), bold=True)


def _page_field(paragraph) -> None:
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)


# ------------------------------------------------------------------ markdown → blocks
def parse_markdown(md: str) -> list[tuple]:
    blocks: list[tuple] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(("p", " ".join(x.strip() for x in para)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para()
            code, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(code)))
        elif re.match(r"^\s*\|.*\|\s*$", line):
            flush_para()
            rows = []
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                if not _TABLE_SEP.match(lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append(("table", rows))
            continue
        elif m := re.match(r"^(#{1,6})\s+(.*)$", line):
            flush_para()
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
        elif re.match(r"^\s*[-*+]\s+", line):
            flush_para()
            items = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]))
                i += 1
            blocks.append(("ul", items))
            continue
        elif re.match(r"^\s*\d+[.)]\s+", line):
            flush_para()
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]))
                i += 1
            blocks.append(("ol", items))
            continue
        elif m := re.match(r"^\s*>\s?(.*)$", line):
            flush_para()
            blocks.append(("quote", m.group(1)))
        elif re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            flush_para()
            blocks.append(("hr",))
        elif not stripped:
            flush_para()
        else:
            para.append(line)
        i += 1
    flush_para()
    return blocks


# ------------------------------------------------------------------ document
def _new_document(template: Path | None):
    if template and template.is_file():
        doc = Document(str(template))
        body = doc.element.body
        for child in list(body):
            if child.tag != qn("w:sectPr"):
                body.remove(child)
        return doc, True
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.left_margin = section.right_margin = Cm(2.2)
    section.top_margin, section.bottom_margin = Cm(2.0), Cm(2.0)
    _style(doc, "Normal", name=THAI_FONT, size=16, color=GRAY_700)
    _style(doc, "Title", name=THAI_FONT, size=26, bold=True, color=NAVY)
    _style(doc, "Heading 1", name=THAI_FONT, size=20, bold=True, color=NAVY)
    _style(doc, "Heading 2", name=THAI_FONT, size=18, bold=True, color=NAVY)
    _style(doc, "Heading 3", name=THAI_FONT, size=16, bold=True, color=GRAY_700)
    for name in ("List Bullet", "List Number", "Quote"):
        _style(doc, name, name=THAI_FONT, size=16)
    doc.styles["Normal"].paragraph_format.space_after = Pt(4)
    return doc, False


def _paragraph(doc, style: str):
    try:
        return doc.add_paragraph(style=style)
    except KeyError:
        return doc.add_paragraph()


def _table(doc, rows: list[list[str]]) -> None:
    if not rows:
        return
    width = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=width)
    try:
        table.style = doc.styles["Table Grid"]
    except KeyError:
        pass
    for r, row in enumerate(rows):
        for c in range(width):
            cell = table.cell(r, c)
            cell.text = ""
            _add_inline(cell.paragraphs[0], row[c] if c < len(row) else "", base_bold=(r == 0))
            if r == 0:
                _shade(cell, HEAD_FILL)
    doc.add_paragraph()


def markdown_to_docx(md: str, *, title: str, file_name: str, rev: int, modified: str | None,
                     template: Path | None = None) -> bytes:
    doc, templated = _new_document(template)
    for block in parse_markdown(md):
        kind = block[0]
        if kind == "h":
            level = block[1]
            p = _paragraph(doc, "Title" if level == 1 else f"Heading {min(level - 1, 3)}")
            _add_inline(p, block[2])
            if level == 1:
                _bottom_rule(p)
        elif kind == "p":
            _add_inline(doc.add_paragraph(), block[1])
        elif kind in ("ul", "ol"):
            for item in block[1]:
                _add_inline(_paragraph(doc, "List Bullet" if kind == "ul" else "List Number"), item)
        elif kind == "table":
            _table(doc, block[1])
        elif kind == "code":
            p = doc.add_paragraph()
            _shade(p, CODE_FILL)
            run = p.add_run(block[1])
            _font_xml(run._element.get_or_add_rPr(), name=MONO_FONT, size=11)
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.8)
            _shade(p, "EEF3FF")
            _add_inline(p, block[1])
        elif kind == "hr":
            _bottom_rule(doc.add_paragraph(), color="D0D5DD", size=6)

    # the title block, as on the dashboard sheet
    when = (datetime.fromisoformat(modified) if modified else datetime.now()).strftime("%d/%m/%Y %H:%M")
    doc.add_paragraph()
    block = doc.add_table(rows=2, cols=4)
    try:
        block.style = doc.styles["Table Grid"]
    except KeyError:
        pass
    for c, (label, value) in enumerate((("ไฟล์", file_name), ("งาน", title), ("Rev.", str(rev)), ("แก้ไขล่าสุด", when))):
        head, cell = block.cell(0, c), block.cell(1, c)
        head.text, cell.text = "", ""
        run = head.paragraphs[0].add_run(label)
        _font_xml(run._element.get_or_add_rPr(), size=12, color=GRAY_500)
        _shade(head, HEAD_FILL)
        run = cell.paragraphs[0].add_run(value)
        _font_xml(run._element.get_or_add_rPr(), size=14, bold=(label == "Rev."), color=NAVY)

    if not templated:
        footer = doc.sections[0].footer.paragraphs[0]
        run = footer.add_run(f"{title} · Rev. {rev} · หน้า ")
        _font_xml(run._element.get_or_add_rPr(), name=THAI_FONT, size=12, color=GRAY_500)
        _page_field(footer)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def action_items_markdown(items: list, title: str) -> str:
    rows = ["| งาน | ผู้รับผิดชอบ | กำหนดเสร็จ | ความสำคัญ | หมายเหตุ |", "|---|---|---|---|---|"]
    labels = {"high": "สูง", "medium": "กลาง", "low": "ต่ำ"}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            cells = [item.get("task") or "", item.get("owner") or "ยังไม่ระบุ", item.get("due") or "ยังไม่กำหนด",
                     labels.get(item.get("priority"), item.get("priority") or ""), item.get("notes") or ""]
            rows.append("| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |")
    return f"# Action items: {title}\n\n" + "\n".join(rows) + "\n"
