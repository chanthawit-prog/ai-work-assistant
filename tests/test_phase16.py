"""Phase 16: asking the knowledge base from the dashboard (one short Claude call over the passages found)."""
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

from app import ask as ask_module  # noqa: E402
from app.ask import memory_room, question_terms  # noqa: E402
from app.main import create_app  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def test_question_terms():
    assert question_terms("แรงดันตกในวงจรย่อยต้องไม่เกินเท่าไร") == ["แรงดันตก", "วงจรย่อย", "เกิน"]
    assert question_terms("ราคา MCCB 125A Schneider เท่าไหร่ครับ") == ["MCCB", "125A", "Schneider", "ราคา"]
    assert question_terms("ACME ใช้ breaker ยี่ห้ออะไร") == ["ACME", "breaker", "ยี่ห้อ"]
    assert question_terms("เท่าไร") == ["เท่าไร"]  # nothing but a question word: searched as typed
    assert question_terms("what is the Icu of NSX160F?") == ["Icu", "NSX160F"]
    assert question_terms("mccb MCCB 125a 125A") == ["mccb", "125a"]  # no repeats, case aside
    assert len(question_terms(" ".join(f"คำ{i}" for i in range(30)))) == 12


def test_search_any_ranks_passages_with_more_of_the_terms_first(tmp_path):
    k = tmp_path / "k"
    k.mkdir()
    (k / "a.md").write_text("แรงดันตกในวงจรย่อยต้องไม่เกินร้อยละ 3\n", encoding="utf-8")
    (k / "b.md").write_text("วงจรย่อยของแสงสว่างใช้สาย 2.5 ตร.มม.\n", encoding="utf-8")
    (k / "c.md").write_text("ไม่เกี่ยวอะไรเลย\n", encoding="utf-8")
    index = tmp_path / "i.sqlite"
    kb_search.refresh(index, {"knowledge": k})
    found = kb_search.search_any(index, ["แรงดันตก", "วงจรย่อย", "เกิน"], 5)
    assert [r["rel"] for r in found["results"]] == ["a.md", "b.md"]
    assert found["results"][0]["matched"] == ["แรงดันตก", "วงจรย่อย", "เกิน"] and found["results"][1]["matched"] == ["วงจรย่อย"]
    assert found["results"][0]["loc_label"] == "บรรทัด 1" and "แรงดันตก" in found["results"][0]["excerpt"]
    assert kb_search.search_any(index, ["วงจรย่อย"], 5, within="knowledge/b")["results"][0]["rel"] == "b.md"
    with pytest.raises(kb_search.SearchError):
        kb_search.search_any(index, ["ab"], 5)
    assert kb_search.search(index, "แรงดันตก วงจรย่อย")["results"][0]["rel"] == "a.md"  # the AND search as before


def ask_app(settings):
    k = settings.knowledge_dir
    (k / "engineering").mkdir()
    shutil.copy(FIXTURES / "thai-2pages.pdf", k / "engineering" / "มาตรฐาน.pdf")
    (k / "pricing").mkdir()
    (k / "pricing" / "price-list.csv").write_text(
        "code,item,unit_price_thb\nMCCB-125,MCCB Schneider NSX160F 125A 3P 36kA,18500\n", encoding="utf-8")
    return create_app(settings, start_background=False)


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext (poppler-utils) not installed")
def test_a_question_is_answered_from_the_index_with_sources(settings):
    app = ask_app(settings)
    with TestClient(app) as client:
        r = client.post("/api/ask", json={"question": "แรงดันตก"})
        assert r.status_code == 409 and "ดัชนี" in r.json()["detail"]  # nothing indexed yet
        asyncio.run(app.state.knowledge.refresh())
        r = client.post("/api/ask", json={"question": "แรงดันตกในวงจรย่อยต้องไม่เกินเท่าไร"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["terms"] == ["แรงดันตก", "วงจรย่อย", "เกิน"]
        assert d["sources"][0]["rel"] == "engineering/มาตรฐาน.pdf" and d["sources"][0]["loc_label"] == "หน้า 2"
        assert "[1]" in d["answer"] and d["sources"][0]["cited"] is True and d["model"] == "fake-model"
        assert d["usage"]["input_tokens"] == 900 and d["id"] and d["at"] and d["seconds"] >= 0
        r2 = client.post("/api/ask", json={"question": "ราคา MCCB 125A Schneider เท่าไหร่", "within": "knowledge/pricing"})
        assert r2.status_code == 200 and {s["rel"] for s in r2.json()["sources"]} == {"pricing/price-list.csv"}
        assert r2.json()["sources"][0]["loc_label"].startswith("บรรทัด")
        none = client.post("/api/ask", json={"question": "xyzzy"}).json()
        assert none["no_sources"] is True and not none["sources"] and "ไม่ใช้โควต้า" in none["answer"]
        items = client.get("/api/ask/history").json()["items"]
        assert [i["question"] for i in items] == ["xyzzy", "ราคา MCCB 125A Schneider เท่าไหร่", "แรงดันตกในวงจรย่อยต้องไม่เกินเท่าไร"]
        assert client.get("/api/ask/history?limit=1").json()["items"][0]["question"] == "xyzzy"
        # refusals
        assert client.post("/api/ask", json={"question": "  "}).status_code == 409
        assert client.post("/api/ask", json={"question": "x" * 1001}).status_code == 409
        assert client.post("/api/ask", json={"question": "แรงดันตก", "within": "/etc"}).status_code == 409
        assert client.post("/api/ask", json={"question": "แรงดันตก"}, headers={"Origin": "http://evil.example"}).status_code == 403
        # the history keeps the last 100
        asker = app.state.asker
        for i in range(120):
            asker._remember({"question": f"q{i}", "at": "x", "answer": ""})
        assert len(asker.history(200)) == 100 and asker.history(1)[0]["question"] == "q119"
    off = create_app(replace(settings, kb_enabled=False), start_background=False)
    with TestClient(off) as client:
        assert "ปิดการค้น" in client.post("/api/ask", json={"question": "แรงดันตก"}).json()["detail"]


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext (poppler-utils) not installed")
def test_claude_failures_become_clear_messages_and_secrets_stay_home(settings, monkeypatch):
    app = ask_app(settings)
    with TestClient(app) as client:
        asyncio.run(app.state.knowledge.refresh())
        monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", "1")
        r = client.post("/api/ask", json={"question": "แรงดันตก"})
        assert r.status_code == 409 and "โควต้า" in r.json()["detail"]
        assert app.state.runtime.get("quota")["status"] == "rejected"  # the status pill learns about it too
        monkeypatch.delenv("FAKE_CLAUDE_RATE_LIMIT")
        monkeypatch.setenv("FAKE_CLAUDE_FAIL", "auth")
        r = client.post("/api/ask", json={"question": "แรงดันตก"})
        assert r.status_code == 409 and "ล็อกอิน" in r.json()["detail"]
        monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
        r = client.post("/api/ask", json={"question": "แรงดันตก"})
        assert r.status_code == 409 and "simulated failure" in r.json()["detail"]
        monkeypatch.delenv("FAKE_CLAUDE_FAIL")
        assert client.get("/api/ask/history").json()["items"] == []  # failures are not history
        dump = settings.data_dir / "env.json"
        monkeypatch.setenv("FAKE_CLAUDE_DUMP_ENV", str(dump))
        monkeypatch.setenv("MAIL_SMTP_PASSWORD", "secret")
        monkeypatch.setenv("NOTIFY_LINE_TOKEN", "secret")
        monkeypatch.setenv("CLAUDE_EFFORT", "max")
        assert client.post("/api/ask", json={"question": "แรงดันตก"}).status_code == 200
        env = json.loads(dump.read_text("utf-8"))
        assert not any(k.startswith(("MAIL_", "NOTIFY_", "DASHBOARD_")) for k in env) and "CLAUDE_EFFORT" not in env
        assert env.get("DISABLE_AUTOUPDATER") == "1"


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext (poppler-utils) not installed")
def test_one_question_at_a_time(settings, monkeypatch):
    app = ask_app(settings)
    asyncio.run(app.state.knowledge.refresh())
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "1")
    asker = app.state.asker

    async def both():
        first = asyncio.create_task(asker.ask("แรงดันตก"))
        await asyncio.sleep(0.3)
        with pytest.raises(ValueError, match="รอสักครู่"):
            await asker.ask("แรงดันตก")
        return await first

    assert "[1]" in asyncio.run(both())["answer"]


def test_memory_room_refuses_when_the_container_is_nearly_full(settings, monkeypatch):
    monkeypatch.setattr(ask_module.sysmon, "host_memory", lambda: {"available_mb": 5000})
    monkeypatch.setattr(ask_module.sysmon, "container_memory", lambda: {"limit_mb": 1000, "used_mb": 800})
    ok, why = memory_room(settings)
    assert not ok and "MEM_LIMIT" in why
    monkeypatch.setattr(ask_module.sysmon, "container_memory", lambda: None)
    assert memory_room(settings) == (True, "")
    monkeypatch.setattr(ask_module.sysmon, "host_memory", lambda: {"available_mb": 300})
    assert "RAM ของ NAS" in memory_room(replace(settings, min_free_mem_mb=600))[1]
