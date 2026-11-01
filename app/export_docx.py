"""Turn Claude's Markdown output into a Word document that looks like a PEC report.

* Anuphan, the PEC house font, for Thai and Latin: body 10 pt, tables 9 pt, headings 20 / 15 / 12 pt,
  the sizes PEC's own report generator uses. Code keeps a monospace font, with Anuphan for any Thai in it.
* Anuphan Regular and Bold are embedded in the file (Word's "Embed fonts in the file"), so the document
  looks the same on a customer's PC that does not have Anuphan installed. Turn off with DOCX_EMBED_FONTS=false.
* Headings in PEC navy, table headers on the light grey PEC uses for note-style tables, A4 page.
* Ends with the same title block the dashboard shows: file, job, Rev., date.
* Put a company template at KNOWLEDGE_DIR/templates/report-template.docx and its header, footer,
  logo and styles are used instead of the built-in ones.
"""
from __future__ import annotations

import io
import re
import uuid
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from lxml import etree

DOC_FONT = "Anuphan"
MONO_FONT = "Consolas"
FONT_DIR = Path(__file__).parent / "assets" / "fonts"
EMBED_FILES = {"embedRegular": "Anuphan-Regular.ttf", "embedBold": "Anuphan-Bold.ttf"}
OBFUSCATED_FONT = "application/vnd.openxmlformats-officedocument.obfuscatedFont"
FONT_TABLE = "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"
REL_FONT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# w:settings children that come before w:embedTrueTypeFonts (ECMA-376 CT_Settings sequence)
_SETTINGS_BEFORE_EMBED = {qn(f"w:{t}") for t in (
    "writeProtection", "view", "zoom", "removePersonalInformation", "removeDateAndTime", "doNotDisplayPageBoundaries",
    "displayBackgroundShape", "printPostScriptOverText", "printFractionalCharacterWidth", "printFormsData")}
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
              italic: bool | None = None, color: RGBColor | None = None, cs_name: str | None = None) -> None:
    """Set fonts for Latin *and* complex scripts: Thai is a complex script in Word (w:cs, w:szCs, w:bCs).
    `cs_name` gives Thai its own font, e.g. a monospace font for code with Anuphan for Thai comments."""
    if name:
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.insert(0, rfonts)
        for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            rfonts.attrib.pop(qn(attr), None)  # a theme font would win over the name we set
        for attr in ("w:ascii", "w:hAnsi"):
            rfonts.set(qn(attr), name)
        for attr in ("w:eastAsia", "w:cs"):
            rfonts.set(qn(attr), cs_name or name)
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


def _add_inline(paragraph, text: str, base_bold: bool = False, size: float | None = None) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = paragraph.add_run(part[2:-2])
            _font_xml(run._element.get_or_add_rPr(), bold=True, size=size)
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            _font_xml(run._element.get_or_add_rPr(), name=MONO_FONT, cs_name=DOC_FONT, size=min(size or 9, 9))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            _font_xml(run._element.get_or_add_rPr(), italic=True, size=size)
        else:
            run = paragraph.add_run(part)
            if base_bold or size:
                _font_xml(run._element.get_or_add_rPr(), bold=True if base_bold else None, size=size)


def _page_field(paragraph, **font) -> None:
    run = paragraph.add_run()
    if font:
        _font_xml(run._element.get_or_add_rPr(), **font)
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
    # sizes follow PEC's report generator (body 10, cells 9, note headings 20); Anuphan's own line gap
    # (win ascent + descent = 1.44 em) leaves room for Thai marks at single spacing
    defaults = doc.styles.element.find(qn("w:docDefaults"))
    rpr_default = defaults.find(qn("w:rPrDefault")) if defaults is not None else None
    rpr = rpr_default.find(qn("w:rPr")) if rpr_default is not None else None
    if rpr is not None:
        _font_xml(rpr, name=DOC_FONT, size=10)
        lang = rpr.find(qn("w:lang"))
        if lang is not None:
            lang.set(qn("w:bidi"), "th-TH")  # complex-script language: Thai line breaking and proofing
    _style(doc, "Normal", name=DOC_FONT, size=10, color=GRAY_700)
    _style(doc, "Title", name=DOC_FONT, size=20, bold=True, color=NAVY)
    _style(doc, "Heading 1", name=DOC_FONT, size=15, bold=True, color=NAVY)
    _style(doc, "Heading 2", name=DOC_FONT, size=12, bold=True, color=NAVY)
    _style(doc, "Heading 3", name=DOC_FONT, size=10.5, bold=True, color=GRAY_700)
    for name in ("List Bullet", "List Number", "Quote"):
        _style(doc, name, name=DOC_FONT, size=10)
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
            _add_inline(cell.paragraphs[0], row[c] if c < len(row) else "", base_bold=(r == 0), size=9)
            if r == 0:
                _shade(cell, HEAD_FILL)
    doc.add_paragraph()


def _obfuscate(data: bytes, guid: str) -> bytes:
    """ECMA-376 Part 1, 17.8.1: XOR the first 32 bytes of the font with the 16-byte key, which is the
    GUID's hex digits read as bytes from the end backwards. The same call also reverses it."""
    digits = guid.strip("{}").replace("-", "")
    key = bytes(int(digits[i:i + 2], 16) for i in range(30, -1, -2))
    return bytes(b ^ key[i % 16] for i, b in enumerate(data[:32])) + data[32:]


def _embed_fonts(doc, family: str = DOC_FONT, files: dict | None = None) -> bool:
    """Embed TrueType fonts the way Word does: an obfuscated copy per style in word/fonts/, listed in
    fontTable.xml, and w:embedTrueTypeFonts in settings. Anuphan's licence (OFL, fsType 0) allows it."""
    files = {kind: FONT_DIR / name for kind, name in (files or EMBED_FILES).items()}
    if not all(path.is_file() for path in files.values()):
        return False
    package = doc.part.package
    table = next((rel.target_part for rel in doc.part.rels.values()
                  if not rel.is_external and rel.reltype == RT.FONT_TABLE), None)
    if table is None:
        blank = f'<w:fonts xmlns:w="{W_NS}" xmlns:r="{R_NS}"/>'.encode()
        table = Part(PackURI("/word/fontTable.xml"), FONT_TABLE, blank, package)
        doc.part.relate_to(table, RT.FONT_TABLE)
    fonts = parse_xml(table.blob)
    font = next((f for f in fonts.findall(qn("w:font")) if f.get(qn("w:name")) == family), None)
    if font is None:
        font = OxmlElement("w:font")
        font.set(qn("w:name"), family)
        for tag, val in (("w:charset", "DE"), ("w:family", "swiss"), ("w:pitch", "variable")):  # DE = Thai
            el = OxmlElement(tag)
            el.set(qn("w:val"), val)
            font.append(el)
        fonts.append(font)
    elif any(el.tag.split("}")[-1].startswith("embed") for el in font):
        return True  # the company template already carries this font
    used = {str(p.partname) for p in package.iter_parts()}
    n = 1
    for kind, path in files.items():  # embedRegular before embedBold, as the schema orders them
        while f"/word/fonts/font{n}.odttf" in used:
            n += 1
        guid = "{" + str(uuid.uuid4()).upper() + "}"
        part = Part(PackURI(f"/word/fonts/font{n}.odttf"), OBFUSCATED_FONT, _obfuscate(path.read_bytes(), guid), package)
        used.add(str(part.partname))
        el = OxmlElement(f"w:{kind}")
        el.set(qn("r:id"), table.relate_to(part, REL_FONT))
        el.set(qn("w:fontKey"), guid)
        font.append(el)
    table._blob = etree.tostring(fonts, xml_declaration=True, encoding="UTF-8", standalone=True)

    settings = doc.settings.element
    if settings.find(qn("w:embedTrueTypeFonts")) is None:
        index = 0
        for i, child in enumerate(settings):
            if child.tag in _SETTINGS_BEFORE_EMBED:
                index = i + 1
        embed, subset = OxmlElement("w:embedTrueTypeFonts"), OxmlElement("w:saveSubsetFonts")
        settings.insert(index, embed)
        if settings.find(qn("w:saveSubsetFonts")) is None:
            embed.addnext(subset)  # when someone re-saves in Word, keep only the characters used
    return True


def _uses_font(doc, family: str) -> bool:
    return f'"{family}"' in doc.styles.element.xml or f'"{family}"' in doc.element.xml


def markdown_to_docx(md: str, *, title: str, file_name: str, rev: int, modified: str | None,
                     template: Path | None = None, embed_fonts: bool = True) -> bytes:
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
            _font_xml(run._element.get_or_add_rPr(), name=MONO_FONT, cs_name=DOC_FONT, size=9)
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
        _font_xml(run._element.get_or_add_rPr(), size=7.5, color=GRAY_500)
        _shade(head, HEAD_FILL)
        run = cell.paragraphs[0].add_run(value)
        _font_xml(run._element.get_or_add_rPr(), size=9, bold=(label == "Rev."), color=NAVY)

    if not templated:
        footer = doc.sections[0].footer.paragraphs[0]
        run = footer.add_run(f"{title} · Rev. {rev} · หน้า ")
        _font_xml(run._element.get_or_add_rPr(), name=DOC_FONT, size=8, color=GRAY_500)
        _page_field(footer, name=DOC_FONT, size=8, color=GRAY_500)

    # a company template may use its own fonts; embed Anuphan only where the document actually uses it
    if embed_fonts and (not templated or _uses_font(doc, DOC_FONT)):
        _embed_fonts(doc)
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
