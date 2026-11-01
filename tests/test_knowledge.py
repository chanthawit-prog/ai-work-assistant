"""Knowledge search: the index (tools/kb_search.py), the background indexer, Claude's access, the API."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.jobtypes import JOB_TYPES
from app.knowledge import KnowledgeIndex, kb_search
from app.main import create_app
from app.runner import KB_RULE

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
needs_pdftotext = pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext (poppler-utils) not installed")


def make_knowledge(base: Path) -> dict:
    k, lib = base / "knowledge", base / "library"
    (k / "engineering").mkdir(parents=True)
    (k / "pricing").mkdir()
    (k / "@eaDir").mkdir()
    (lib / "proposal-draft").mkdir(parents=True)
    (k / "engineering" / "guide.md").write_text(
        "# แนวทางเลือกสาย\n\n- สายต้องรับกระแสได้ไม่น้อยกว่า 1.25 เท่า\n- แรงดันตกจากตู้ถึงมอเตอร์ไม่เกิน 3%\n", encoding="utf-8")
    (k / "pricing" / "ราคา.csv").write_bytes(
        "รหัส,รายการ,ราคา\nMCCB-160,เบรกเกอร์ 160A 3P 36kA,12500\n".encode("cp874"))  # Thai Windows encoding
    doc = Document()
    doc.add_paragraph("ข้อตกลงการรับประกันอุปกรณ์ 24 เดือน")
    doc.add_paragraph("ส่งมอบภายใน 45 วันหลังได้รับใบสั่งซื้อ")
    doc.save(k / "engineering" / "เงื่อนไข.docx")
    (k / "@eaDir" / "guide.md").write_text("แรงดันตก ในไฟล์ระบบของ Synology", encoding="utf-8")  # must be ignored
    (lib / "proposal-draft" / "2026-11-01_acme.md").write_text("# ใบเสนอราคา ACME\nสาย CV 50 mm² แรงดันตก 2.5%",
                                                                encoding="utf-8")
    return {"knowledge": k, "library": lib}


@pytest.fixture
def kb(tmp_path):
    roots = make_knowledge(tmp_path)
    index = tmp_path / "state" / "kb.sqlite"
    kb_search.refresh(index, roots)
    return index, roots


# ---------------------------------------------------------------- index and search
def test_thai_without_spaces_csv_in_cp874_and_word(kb):
    index, _ = kb
    hits = kb_search.search(index, "แรงดันตก")["results"]
    assert {h["path"] for h in hits} == {"knowledge/engineering/guide.md", "library/proposal-draft/2026-11-01_acme.md"}
    assert kb_search.search(index, "เบรกเกอร์ 160A")["results"][0]["rel"] == "pricing/ราคา.csv"
    word = kb_search.search(index, "รับประกันอุปกรณ์")["results"][0]
    assert word["kind"] == "docx" and word["loc_label"] == "ย่อหน้า 1"
    assert kb_search.search(index, "ใบสั่งซื้อ")["results"][0]["loc_label"] == "ย่อหน้า 2"


def test_all_terms_short_terms_and_folder_filter(kb):
    index, _ = kb
    assert kb_search.search(index, "แรงดันตก มอเตอร์")["results"][0]["rel"] == "engineering/guide.md"
    assert kb_search.search(index, "แรงดันตก ไม่มีคำนี้แน่นอน")["results"] == []
    assert [h["rel"] for h in kb_search.search(index, "kA")["results"]] == ["pricing/ราคา.csv"]  # 2 letters: scanned
    only_lib = kb_search.search(index, "แรงดันตก", within="library")["results"]
    assert [h["source"] for h in only_lib] == ["library"]
    with pytest.raises(kb_search.SearchError):
        kb_search.search(index, "   ")


def test_line_numbers_point_at_the_match(tmp_path):
    k = tmp_path / "knowledge"
    k.mkdir()
    lines = [f"บรรทัดที่ {i} ข้อความทั่วไปสำหรับเติมให้ไฟล์ยาวพอจะแบ่งเป็นหลายช่วง" for i in range(1, 121)]
    lines[87] = "บรรทัดที่ 88 ข้อกำหนดพิเศษ: ห้ามใช้สาย PVC ในห้องเครื่อง"
    (k / "spec.txt").write_text("\n".join(lines), encoding="utf-8")
    index = tmp_path / "i.sqlite"
    kb_search.refresh(index, {"knowledge": k})
    hit = kb_search.search(index, "ห้ามใช้สาย PVC")["results"][0]
    assert hit["loc"] == 88 and hit["loc_label"] == "บรรทัด 88"
    assert len(kb_search.search(index, "ข้อความทั่วไป", limit=50)["results"]) > 3  # really split into passages


def test_sara_am_written_as_two_marks_still_matches(tmp_path):
    k = tmp_path / "knowledge"
    k.mkdir()
    (k / "x.txt").write_text("งานที่ต้องทําให้เสร็จ", encoding="utf-8")  # NIKHAHIT + SARA AA
    index = tmp_path / "i.sqlite"
    kb_search.refresh(index, {"knowledge": k})
    assert kb_search.search(index, "ต้องทำ")["results"]


@needs_pdftotext
def test_pdf_pages_and_thai_spacing(tmp_path):
    k = tmp_path / "knowledge"
    k.mkdir()
    shutil.copy(FIXTURES / "thai-2pages.pdf", k / "มาตรฐาน.pdf")
    index = tmp_path / "i.sqlite"
    kb_search.refresh(index, {"knowledge": k})
    hit = kb_search.search(index, "แรงดันตก")["results"][0]  # pdftotext writes "แรงดั นตก"
    assert hit["kind"] == "pdf" and hit["loc"] == 2 and hit["loc_label"] == "หน้า 2"
    assert hit["file"] == str(k / "มาตรฐาน.pdf")
    assert kb_search.search(index, "ไฟฟ้าแรงต่ำ")["results"][0]["loc"] == 1
    assert kb_search.fix_pdf_thai("ทดสอบระบบค้นหา ไม่ใช่") == "ทดสอบระบบค้นหา ไม่ใช่"  # a real word gap stays


@needs_pdftotext
def test_pdf_without_text_is_flagged(tmp_path):
    k = tmp_path / "knowledge"
    k.mkdir()
    blank = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
             b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
    (k / "scan.pdf").write_bytes(blank)
    index = tmp_path / "i.sqlite"
    kb_search.refresh(index, {"knowledge": k})
    notes = kb_search.status(index)["notes"]
    assert notes and notes[0]["path"] == "knowledge/scan.pdf" and "สแกน" in notes[0]["note"]


def test_refresh_is_incremental_and_can_pause(kb):
    index, roots = kb
    again = kb_search.refresh(index, roots)
    assert again["added"] == again["updated"] == again["removed"] == 0 and again["unchanged"] == 4
    guide = roots["knowledge"] / "engineering" / "guide.md"
    guide.write_text("# แนวทางใหม่\nใช้สาย XLPE เท่านั้น", encoding="utf-8")
    (roots["knowledge"] / "pricing" / "ราคา.csv").unlink()
    (roots["knowledge"] / "new.txt").write_text("เอกสารใหม่", encoding="utf-8")
    stopped = kb_search.refresh(index, roots, should_stop=lambda: True)
    assert stopped["stopped"] and stopped["removed"] == 0  # paused: nothing is dropped
    done = kb_search.refresh(index, roots)
    assert (done["updated"], done["added"], done["removed"]) == (1, 1, 1)
    assert kb_search.search(index, "XLPE")["results"] and not kb_search.search(index, "เบรกเกอร์")["results"]
    status = kb_search.status(index)
    assert status["files"] == 4 and status["by_root"] == {"knowledge": 3, "library": 1}


def test_a_missing_root_does_not_wipe_its_entries(kb):
    index, roots = kb
    kb_search.refresh(index, {"knowledge": roots["knowledge"], "library": roots["library"].parent / "not-mounted"})
    assert kb_search.status(index)["by_root"]["library"] == 1


def test_cli_prints_paths_claude_can_read(kb):
    index, roots = kb
    env = {**os.environ, "PYTHONPATH": str(ROOT / "tools"), "KB_INDEX": str(index)}
    out = subprocess.run([sys.executable, "-m", "kb_search", "แรงดันตก"], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0
    assert str(roots["knowledge"] / "engineering" / "guide.md") in out.stdout and "ผลงานที่อนุมัติแล้ว: proposal-draft" in out.stdout
    full = subprocess.run([sys.executable, "-m", "kb_search", "ACME", "--full"], capture_output=True, text=True, env=env, timeout=30)
    assert "สาย CV 50 mm²" in full.stdout
    missing = subprocess.run([sys.executable, "-m", "kb_search", "x"], capture_output=True, text=True,
                             env={**env, "KB_INDEX": str(index.parent / "nope.sqlite")}, timeout=30)
    assert missing.returncode == 2 and "ยังไม่มีดัชนี" in missing.stderr


# ---------------------------------------------------------------- the app around it
def test_indexer_waits_for_idle(parts, settings):
    _store, _tasks, _state, runner = parts
    make_knowledge(settings.data_dir / "seed")
    shutil.copytree(settings.data_dir / "seed" / "knowledge" / "engineering", settings.knowledge_dir / "engineering")
    index = KnowledgeIndex(settings, runner)
    runner.active["job"] = object()  # a job is running
    assert index.busy() and asyncio.run(index.refresh()) is None and not index.ready()
    runner.active.clear()
    stats = asyncio.run(index.refresh())
    assert stats["added"] == 3 and index.ready()  # guide.md, เงื่อนไข.docx and the fixture's ACME profile.md


def test_jobs_get_the_search_command_only_when_there_is_something_to_search(parts, settings, tmp_path, monkeypatch):
    store, _tasks, _state, runner = parts
    runner.kb = KnowledgeIndex(settings, runner)
    general = runner.build_command(JOB_TYPES["general"])
    assert "Bash" not in general[general.index("--tools") + 1]  # nothing indexed yet
    asyncio.run(runner.kb.refresh())  # the conftest knowledge folder has customers/ACME/profile.md
    general = runner.build_command(JOB_TYPES["general"])
    assert general[general.index("--tools") + 1].endswith(",Bash")
    assert general[general.index("--allowedTools") + 1].split(",")[-1] == KB_RULE
    prompt = general[general.index("--append-system-prompt") + 1]
    assert "python3 -m kb_search" in prompt and "ใช้ได้เฉพาะคำสั่ง python3 -m kb_search" in prompt
    eng = runner.build_command(JOB_TYPES["eng-qa"])
    assert "Bash(python3 *)" in eng[eng.index("--allowedTools") + 1]
    assert "ใช้ได้เฉพาะคำสั่ง" not in eng[eng.index("--append-system-prompt") + 1]

    dump = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLAUDE_DUMP_ENV", str(dump))
    job = store.create("general", "ค้นเอกสาร", "หาเงื่อนไขการรับประกัน")
    asyncio.run(runner.run_job(job["id"]))
    env = json.loads(dump.read_text("utf-8"))
    assert env["KB_INDEX"] == str(runner.kb.path) and str(ROOT / "tools") in env["PYTHONPATH"]


def test_knowledge_api(settings):
    (settings.knowledge_dir / "engineering").mkdir()
    (settings.knowledge_dir / "engineering" / "guide.md").write_text("แรงดันตกไม่เกิน 3%", encoding="utf-8")
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        assert client.get("/api/knowledge").json()["files"] == 0
        assert client.post("/api/knowledge/refresh").json()["queued"] is True
        asyncio.run(app.state.knowledge.refresh())
        status = client.get("/api/knowledge").json()
        assert status["files"] == 2 and status["enabled"] and status["updated_at"]
        found = client.get("/api/knowledge/search", params={"q": "แรงดันตก"}).json()
        assert found["results"][0]["path"] == "knowledge/engineering/guide.md" and found["terms"] == ["แรงดันตก"]
        assert client.get("/api/knowledge/search", params={"q": " "}).status_code == 400
        opened = client.get("/api/knowledge/file/knowledge/engineering/guide.md")
        assert opened.status_code == 200 and "แรงดันตก" in opened.text
        for bad in ("knowledge/../../etc/passwd", "secrets/x", "knowledge/", "library/nothing.md"):
            assert client.get(f"/api/knowledge/file/{bad}").status_code == 404
