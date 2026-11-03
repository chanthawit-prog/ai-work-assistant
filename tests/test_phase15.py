"""Phase 15: scanned PDFs read with tesseract, in the knowledge index and as job inputs."""
import asyncio
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import kb_search  # noqa: E402
import ocr_text  # noqa: E402

from app import convert, readiness  # noqa: E402
from app.knowledge import ocr_config  # noqa: E402
from app.main import create_app  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
needs_ocr = pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm") and shutil.which("pdftotext")),
                               reason="tesseract and poppler-utils are not both installed")
NO_TESSERACT = {"tesseract": False, "pdftoppm": True, "version": "", "langs": []}


def test_text_cleanup_needs_no_tesseract():
    assert ocr_text.clean("แรง ดัน ตก  Icu  36 kA \n\n\n\nข้อ กําหนด") == "แรงดันตก Icu 36 kA\n\nข้อกำหนด"
    assert ocr_text.scant_pages(["", "x" * 39, "x" * 40]) == [1, 2]
    assert ocr_text.as_text("a.pdf", ocr_text.OcrResult(total=1)).startswith("ข้อความที่อ่านด้วย OCR จากไฟล์ a.pdf (0 หน้า จาก 1 หน้า)")
    assert ocr_text.OcrResult(total=1).note.startswith("ภาพสแกน อ่านด้วย OCR แล้วไม่พบข้อความ")


@needs_ocr
def test_a_scanned_pdf_is_read_cached_and_capped(tmp_path):
    cfg = ocr_text.OcrConfig(cache_dir=tmp_path / "ocr")
    pdf = FIXTURES / "scanned-th.pdf"
    texts = ocr_text.page_texts(pdf)
    assert len(texts) == 2 and ocr_text.scant_pages(texts) == [1, 2] and ocr_text.page_count(pdf) == 2
    result = ocr_text.ocr_pages(pdf, [1, 2], cfg, total=2)
    assert [p for p, _ in result.pages] == [1, 2] and result.from_cache == 0 and result.note == ""
    page1, page2 = dict(result.pages)[1], dict(result.pages)[2]
    assert "NSX160F" in page1 and "ยืนราคา" in page1 and "125,000" in page1
    assert "แรงดันตก" in page2 and "derating 0.87" in page2 and "as-built" in page2
    assert "  " not in page2 and "ํา" not in page1  # tidy spacing, SARA AM as one character
    cache = list((tmp_path / "ocr").glob("*.json"))
    assert len(cache) == 1 and set(json.loads(cache[0].read_text("utf-8"))["pages"]) == {"1", "2"}
    again = ocr_text.ocr_pages(pdf, [1, 2], cfg, total=2)
    assert again.from_cache == 2 and again.pages == result.pages and again.seconds < 1
    capped = ocr_text.ocr_pages(pdf, [1, 2], replace(cfg, max_pages=1), total=2)
    assert len(capped.pages) == 1 and capped.skipped == 1 and "OCR_MAX_PAGES" in capped.note
    text = ocr_text.as_text("x.pdf", capped)
    assert text.startswith("ข้อความที่อ่านด้วย OCR จากไฟล์ x.pdf (1 หน้า จาก 2 หน้า)") and "=== หน้า 1 ===" in text


@needs_ocr
def test_a_stop_between_pages_keeps_what_was_read(tmp_path):
    cfg = ocr_text.OcrConfig(cache_dir=tmp_path / "ocr")
    pdf = FIXTURES / "scanned-th.pdf"
    asked = []

    def stop():
        asked.append(1)
        return len(asked) > 1  # the first page goes through, then a job starts

    with pytest.raises(ocr_text.OcrStopped):
        ocr_text.ocr_pages(pdf, [1, 2], cfg, total=2, should_stop=stop)
    cached = json.loads(next((tmp_path / "ocr").glob("*.json")).read_text("utf-8"))
    assert list(cached["pages"]) == ["1"]
    result = ocr_text.ocr_pages(pdf, [1, 2], cfg, total=2)
    assert result.from_cache == 1 and len(result.pages) == 2


@needs_ocr
def test_the_index_reads_scans_and_mixed_files(tmp_path):
    k = tmp_path / "knowledge"
    k.mkdir()
    shutil.copy(FIXTURES / "scanned-th.pdf", k / "ใบเสนอราคา-สแกน.pdf")
    shutil.copy(FIXTURES / "mixed-th.pdf", k / "ผสม.pdf")      # page 1 has text, page 2 is a scan
    shutil.copy(FIXTURES / "thai-2pages.pdf", k / "ข้อความ.pdf")
    index = tmp_path / "i.sqlite"
    cfg = ocr_text.OcrConfig(cache_dir=tmp_path / "ocr")
    stats = kb_search.refresh(index, {"knowledge": k}, ocr=cfg)
    assert stats["added"] == 3 and not stats["errors"] and not stats["stopped"]
    status = kb_search.status(index)
    assert status["ocr_files"] == 2 and status["ocr_pages"] == 3 and status["notes"] == []
    hits = kb_search.search(index, "NSX160F")["results"]
    assert {h["rel"] for h in hits} == {"ใบเสนอราคา-สแกน.pdf", "ผสม.pdf"}
    assert [h["loc"] for h in hits if h["rel"] == "ผสม.pdf"] == [2]
    first = kb_search.search(index, "ขอบเขตของเอกสาร", within="knowledge/ผสม.pdf")["results"]
    assert first and first[0]["loc"] == 1  # the text page stayed as pdftotext read it
    assert kb_search.search(index, "แรงดันตก")["results"]  # Thai from the scan is searchable
    assert kb_search.refresh(index, {"knowledge": k}, ocr=cfg)["unchanged"] == 3  # nothing read twice

    # a job starts between OCR pages: the file waits for the next refresh, the index stays consistent
    shutil.copy(FIXTURES / "scanned-th.pdf", k / "อีก.pdf")
    asked = []

    def stop():
        asked.append(1)
        return len(asked) >= 2

    other = replace(cfg, cache_dir=tmp_path / "ocr2")
    stopped = kb_search.refresh(index, {"knowledge": k}, should_stop=stop, ocr=other)
    assert stopped["stopped"] and kb_search.status(index)["files"] == 3
    done = kb_search.refresh(index, {"knowledge": k}, ocr=other)
    assert done["added"] == 1 and kb_search.status(index)["ocr_files"] == 3

    # an index made before OCR existed gets the column on the next refresh
    old = tmp_path / "old.sqlite"
    conn = kb_search.connect(old)
    conn.execute("ALTER TABLE docs DROP COLUMN ocr")
    conn.commit()
    conn.close()
    assert kb_search.status(old)["ocr_files"] == 0
    kb_search.refresh(old, {"knowledge": k}, ocr=cfg)
    assert kb_search.status(old)["ocr_files"] == 3


@needs_ocr
def test_job_inputs_get_ocr_text_and_the_timeline_says_so(settings):
    app = create_app(settings, start_background=False)
    store = app.state.store
    with TestClient(app) as client:
        assert client.get("/api/knowledge").json()["ocr"]["usable"] is True
        with (FIXTURES / "scanned-th.pdf").open("rb") as scan, (FIXTURES / "thai-2pages.pdf").open("rb") as text:
            job = client.post("/api/jobs", data={"type": "general", "instructions": "สรุปใบเสนอราคา"},
                              files=[("files", ("ใบเสนอราคา.pdf", scan, "application/pdf")),
                                     ("files", ("มาตรฐาน.pdf", text, "application/pdf"))]).json()
        asyncio.run(app.state.runner.run_job(job["id"]))
        detail = client.get(f"/api/jobs/{job['id']}").json()
        names = [f["path"] for f in detail["files"]["input"]]
        assert "ใบเสนอราคา.ocr.txt" in names and "มาตรฐาน.ocr.txt" not in names  # only the scan needed OCR
        ocr_file = store.job_dir(job["id"]) / "input" / "ใบเสนอราคา.ocr.txt"
        text = ocr_file.read_text("utf-8")
        assert text.startswith("ข้อความที่อ่านด้วย OCR จากไฟล์ ใบเสนอราคา.pdf (2 หน้า)")
        assert "=== หน้า 1 ===" in text and "NSX160F" in text and "แรงดันตก" in text
        steps = [s["text"] for s in detail["steps"] if s["kind"] == "app"]
        assert any("OCR 2 หน้า" in s for s in steps) and any("ใบเสนอราคา.ocr.txt" in s for s in steps)
        assert not detail["job"]["warnings"]
        assert list((settings.state_dir / "ocr").glob("*.json"))  # the cache the index shares
        stamp = ocr_file.stat().st_mtime_ns

        # another round: the file is not read again; a scan beyond the page cap is said so
        client.post(f"/api/jobs/{job['id']}/message", json={"text": "เพิ่มเติม"})
        asyncio.run(app.state.runner.run_job(job["id"]))
        assert ocr_file.stat().st_mtime_ns == stamp
        assert len([s for s in client.get(f"/api/jobs/{job['id']}").json()["steps"] if "OCR 2 หน้า" in s["text"]]) == 1

    capped = replace(settings, ocr_max_pages=1)
    inp = settings.data_dir / "cap"
    inp.mkdir()
    shutil.copy(FIXTURES / "scanned-th.pdf", inp / "scan.pdf")
    warnings = convert.prepare_inputs(inp, ocr_config(capped))
    assert warnings == ["scan.pdf มี 2 หน้า อ่านด้วย OCR เฉพาะ 1 หน้าแรก (เพิ่ม OCR_MAX_PAGES ในไฟล์ .env ถ้าต้องการทั้งหมด)"]
    assert "=== หน้า 2 ===" not in (inp / "scan.ocr.txt").read_text("utf-8")


def test_without_tesseract_the_scan_is_flagged_not_read(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_text, "_tools_cache", dict(NO_TESSERACT))
    inp = tmp_path / "input"
    inp.mkdir()
    shutil.copy(FIXTURES / "scanned-th.pdf", inp / "scan.pdf")
    warnings = convert.prepare_inputs(inp, ocr_config(settings))
    if shutil.which("pdftotext"):
        assert len(warnings) == 1 and "ภาพสแกน 2 หน้า" in warnings[0] and "ไม่ได้ติดตั้ง OCR" in warnings[0]
    assert not (inp / "scan.ocr.txt").exists()
    item = {i["key"]: i for i in readiness.run_checks(settings, None, [], 0)["items"]}["ocr"]
    assert item["status"] == "info" and "Build" in item["fix"]
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        ocr = client.get("/api/knowledge").json()["ocr"]
        assert ocr["usable"] is False and "tesseract" in ocr["reason"] and ocr["installed"] is False
    if shutil.which("pdftotext"):
        k = tmp_path / "k"
        k.mkdir()
        shutil.copy(FIXTURES / "scanned-th.pdf", k / "scan.pdf")
        shutil.copy(FIXTURES / "mixed-th.pdf", k / "mixed.pdf")
        index = tmp_path / "i.sqlite"
        kb_search.refresh(index, {"knowledge": k}, ocr=ocr_config(settings))
        notes = {n["path"]: n["note"] for n in kb_search.status(index)["notes"]}
        assert "แทบไม่มีข้อความ (ภาพสแกน)" in notes["knowledge/scan.pdf"] and "tesseract" in notes["knowledge/scan.pdf"]
        assert notes["knowledge/mixed.pdf"].startswith("มี 1 หน้าที่เป็นภาพสแกน")
        assert kb_search.status(index)["ocr_files"] == 0

    off = replace(settings, ocr_enabled=False)
    assert {i["key"]: i for i in readiness.run_checks(off, None, [], 0)["items"]}["ocr"]["status"] == "info"
    assert "OCR=false" in ocr_text.usable(ocr_config(off))[1]
    monkeypatch.setattr(ocr_text, "_tools_cache", {"tesseract": True, "pdftoppm": True, "version": "5.3", "langs": ["eng"]})
    assert "tha" in ocr_text.usable(ocr_config(settings))[1]
    assert {i["key"]: i for i in readiness.run_checks(settings, None, [], 0)["items"]}["ocr"]["status"] == "warn"
