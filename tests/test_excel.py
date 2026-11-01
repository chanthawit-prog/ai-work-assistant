"""Excel workbooks: the reader, attached files turned into CSV, and Excel in the knowledge index."""
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.convert import CONVERTED, prepare_inputs
from app.knowledge import kb_search
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import xlsx_text  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "boq-sample.xlsx"  # written by openpyxl, re-saved by LibreOffice


def workbook(sheet_xml: str, *, shared: str = "", styles: str = "", date1904: bool = False) -> bytes:
    """A minimal .xlsx put together by hand, for cases the fixture does not cover."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    props = '<workbookPr date1904="1"/>' if date1904 else ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", f'<workbook {ns} {rel}>{props}'
                                       '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{sheet_xml}</sheetData></worksheet>")
        if shared:
            zf.writestr("xl/sharedStrings.xml", f"<sst {ns}>{shared}</sst>")
        if styles:
            zf.writestr("xl/styles.xml", f"<styleSheet {ns}>{styles}</styleSheet>")
    return buf.getvalue()


# ---------------------------------------------------------------- reader
def test_reads_values_formula_results_dates_and_hidden_sheets():
    sheets = xlsx_text.read_workbook(FIXTURE)
    assert [(s["name"], s["hidden"]) for s in sheets] == [("BOQ", False), ("หมายเหตุ", False), ("lookup", True)]
    boq = dict(sheets[0]["rows"])
    assert boq[1][:6] == ["ลำดับ", "รายการ", "จำนวน", "ราคาต่อหน่วย", "รวม", "ส่งมอบ"]
    assert boq[2][:6] == ["1", "Main breaker 400A 3P 50kA", "1", "48500", "48500", "2026-12-15"]
    assert boq[2][7] == "0.000123"  # 0.00E+00 is a number format, not a date
    assert boq[3][4] == "156800" and boq[4][3:5] == ["4500.5", "13501.5"]  # formulas: the saved results
    assert sheets[1]["rows"] == [(1, ["ราคายังไม่รวม VAT 7%"]), (3, ["ยืนราคา 30 วัน"])]  # empty row 2 skipped
    assert sheets[2]["rows"] == [(1, ["rate", "0.07"])]


@pytest.mark.parametrize("code, is_date", [
    ("General", False), ("dd/mm/yyyy", True), ("#,##0.00", False), ("0.00E+00", False),
    ("[$-th-TH,107]d mmm yyyy", True), ("[$-1070000]d/mm/bbbb", True), ("h:mm AM/PM", True), ('0 "ชิ้น"', False),
])
def test_date_formats(code, is_date):
    assert xlsx_text._is_date_format(code) is is_date


def test_inline_and_rich_text_booleans_errors_and_the_1904_calendar():
    shared = '<si><r><t>สาย </t></r><r><t>CV</t></r><rPh><t>ignored</t></rPh></si>'
    styles = ('<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="20"/><xf numFmtId="71"/></cellXfs>')
    sheet = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>พิมพ์ในเซลล์</t></is></c>'
             '<c r="C1" t="b"><v>1</v></c><c r="E1" t="e"><v>#DIV/0!</v></c></row>'
             '<row r="2"><c r="A2" s="1"><v>0</v></c><c r="B2" s="1"><v>1.5</v></c>'
             '<c r="C2" s="2"><v>0.75</v></c><c r="D2" s="3"><v>1</v></c></row>')
    path = Path(os.environ.get("TMPDIR", "/tmp")) / "hand-made-test.xlsx"
    path.write_bytes(workbook(sheet, shared=shared, styles=styles, date1904=True))
    try:
        rows = dict(xlsx_text.read_workbook(path)[0]["rows"])
    finally:
        path.unlink()
    assert rows[1] == ["สาย CV", "พิมพ์ในเซลล์", "TRUE", "", "#DIV/0!"]
    # 1904 calendar: serial 0 is 1 Jan 1904; a time format gives the time of day; 71 is Thai Excel's ว/ด/ปปปป
    assert rows[2] == ["1904-01-01", "1904-01-02 12:00", "18:00", "1904-01-02"]


def test_row_cap_and_broken_files(tmp_path):
    assert xlsx_text.read_workbook(FIXTURE, max_rows=2)[0]["truncated"] is True
    (tmp_path / "bad.xlsx").write_bytes(b"not a zip")
    with pytest.raises(xlsx_text.XlsxError):
        xlsx_text.read_workbook(tmp_path / "bad.xlsx")


def test_cli_prints_csv():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "tools")}
    out = subprocess.run([sys.executable, "-m", "xlsx_text", str(FIXTURE), "--sheet", "หมายเหตุ"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0 and out.stdout == "ราคายังไม่รวม VAT 7%\nยืนราคา 30 วัน\n"
    bad = subprocess.run([sys.executable, "-m", "xlsx_text", str(FIXTURE), "--sheet", "ไม่มี"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert bad.returncode == 2 and "BOQ" in bad.stderr


# ---------------------------------------------------------------- attached workbooks
def test_attached_workbook_becomes_csv_per_sheet_once(tmp_path):
    shutil.copy(FIXTURE, tmp_path / "BOQ ลูกค้า.xlsx")
    (tmp_path / "old.xls").write_bytes(b"x")
    warnings = prepare_inputs(tmp_path)
    assert [w for w in warnings if "old.xls" in w] and not [w for w in warnings if "BOQ" in w]
    names = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".csv")
    assert names == ["BOQ ลูกค้า - BOQ.csv", "BOQ ลูกค้า - lookup (ชีตซ่อน).csv", "BOQ ลูกค้า - หมายเหตุ.csv"]
    assert (tmp_path / "BOQ ลูกค้า - BOQ.csv").read_text("utf-8").splitlines()[2].startswith("2,สาย CV 4x70 ตร.มม.,160,980,156800")
    marker = json.loads((tmp_path / CONVERTED).read_text("utf-8"))
    assert sorted(marker["BOQ ลูกค้า.xlsx"]["outputs"]) == names
    prepare_inputs(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".csv") == names  # not converted twice

    small = workbook('<row r="1"><c r="A1" t="inlineStr"><is><t>ฉบับแก้ไข</t></is></c></row>')
    (tmp_path / "BOQ ลูกค้า.xlsx").write_bytes(small)  # a new version replaces the old CSVs
    prepare_inputs(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".csv") == ["BOQ ลูกค้า - Data.csv"]


def test_uploading_a_workbook_through_the_api(settings):
    with TestClient(create_app(settings, start_background=False)) as client:
        res = client.post("/api/jobs", data={"type": "proposal-draft", "customer": "ACME", "instructions": "ราคาตาม BOQ"},
                          files=[("files", ("BOQ.xlsx", FIXTURE.read_bytes(), "application/vnd.ms-excel"))])
        assert res.status_code == 200, res.text
        job = client.get(f"/api/jobs/{res.json()['id']}").json()
        assert {f["path"] for f in job["files"]["input"]} == {"BOQ.xlsx", "BOQ - BOQ.csv", "BOQ - หมายเหตุ.csv",
                                                             "BOQ - lookup (ชีตซ่อน).csv"}
        assert job["job"]["warnings"] == []


# ---------------------------------------------------------------- Excel in the knowledge index
def test_excel_in_the_knowledge_index(tmp_path):
    k = tmp_path / "knowledge"
    (k / "pricing").mkdir(parents=True)
    shutil.copy(FIXTURE, k / "pricing" / "ราคา.xlsx")
    rows = "".join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>MCCB-{i:03d} เบรกเกอร์รุ่นทดสอบ {i}A ยาวพอให้แบ่งช่วง</t></is></c>'
                   f'<c r="B{i}"><v>{i * 100}</v></c></row>' for i in range(2, 202))
    header = '<row r="1"><c r="A1" t="inlineStr"><is><t>รายการ</t></is></c><c r="B1" t="inlineStr"><is><t>ราคา</t></is></c></row>'
    (k / "pricing" / "big.xlsx").write_bytes(workbook(header + rows))
    lib = tmp_path / "library" / "proposal-draft"
    lib.mkdir(parents=True)
    (lib / "a.md").write_text("ใบเสนอราคา", encoding="utf-8")
    (lib / "a.docx").write_bytes(b"not indexed: the Word copy of a.md")
    index = tmp_path / "i.sqlite"
    kb_search.refresh(index, {"knowledge": k, "library": tmp_path / "library"})

    hit = kb_search.search(index, "สาย CV 4x70")["results"][0]
    assert hit["kind"] == "xlsx" and hit["loc_label"] == "ชีต BOQ · แถว 3"
    deep = kb_search.search(index, "MCCB-187")["results"][0]
    assert deep["loc_label"] == "ชีต Data · แถว 187" and deep["text"].startswith("[หัวตาราง] รายการ | ราคา")
    assert kb_search.status(index)["by_kind"] == {"xlsx": 2, "text": 1}  # library a.docx skipped

    shown = kb_search.render_show(kb_search.show(index, "pricing/big.xlsx"))
    assert shown.count("[หัวตาราง]") == 0 and shown.count("[แถว 1] รายการ | ราคา") == 1
    assert "[แถว 201] MCCB-201" in shown and shown.count("[แถว 100]") == 1
    by_abs = kb_search.show(index, str(k / "pricing" / "ราคา.xlsx"))
    assert by_abs["path"] == "knowledge/pricing/ราคา.xlsx"
    with pytest.raises(kb_search.SearchError):
        kb_search.show(index, "nothing.xlsx")


# ---------------------------------------------------------------- approved work as Word in the library
def test_approval_saves_a_word_copy_in_the_library(parts, settings):
    import asyncio
    from dataclasses import replace

    from docx import Document

    from app.review import approve
    store, tasks, _state, runner = parts
    job = store.create("general", "สรุปสเปกตู้ MDB", "สรุป")
    asyncio.run(runner.run_job(job["id"]))
    done = approve(store, tasks, settings, job["id"])
    archived = done["review"]["archived"]
    assert [Path(a).suffix for a in archived] == [".md", ".docx"]
    word = settings.data_dir / archived[1]
    doc = Document(str(word))
    assert doc.styles["Normal"].font.name == "Anuphan" and "สรุปสเปกตู้ MDB" in "\n".join(
        c.text for t in doc.tables for row in t.rows for c in row.cells)  # the title block

    other = store.create("general", "ไม่เอา Word", "สรุป")
    asyncio.run(runner.run_job(other["id"]))
    plain = approve(store, tasks, replace(settings, library_docx=False), other["id"])
    assert [Path(a).suffix for a in plain["review"]["archived"]] == [".md"]
