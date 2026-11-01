"""Running lean on a NAS with little RAM, and the Anuphan font in Word exports."""
import asyncio
import io
import os
import subprocess
import sys
import zipfile
from dataclasses import replace

from docx import Document
from fastapi.testclient import TestClient

from app import runner as runner_module
from app import sysmon
from app.export_docx import FONT_DIR, _obfuscate, markdown_to_docx
from app.jobtypes import JOB_TYPES
from app.main import create_app
from app.runner import OOM_MESSAGES, Runner, claude_env
from app.store import FAILED, NEEDS_REVIEW, QUEUED

MEMINFO = """MemTotal:        6029312 kB
MemFree:          201000 kB
MemAvailable:     512000 kB
Buffers:           20000 kB
Cached:           400000 kB
SwapTotal:       2097148 kB
SwapFree:          104857 kB
"""


def fake_host(available_mb, total_mb=5888):
    return lambda *a, **k: {"total_mb": total_mb, "available_mb": available_mb, "used_pct": 90,
                            "swap_total_mb": 2047, "swap_used_mb": 1945, "swap_used_pct": 95}


# ---------------------------------------------------------------- readings
def test_host_memory_reads_ram_and_swap(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO)
    mem = sysmon.host_memory(path)
    assert mem["total_mb"] == 5888 and mem["available_mb"] == 500
    assert mem["swap_total_mb"] == 2047 and mem["swap_used_pct"] == 95
    assert sysmon.host_memory(tmp_path / "missing") is None


def test_container_memory_cgroup_v2(tmp_path):
    (tmp_path / "memory.current").write_text(str(700 * 2**20))
    (tmp_path / "memory.max").write_text(str(1536 * 2**20))
    (tmp_path / "memory.peak").write_text(str(900 * 2**20))
    (tmp_path / "memory.stat").write_text(f"anon 1\ninactive_file {200 * 2**20}\n")
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\n")
    mem = sysmon.container_memory(tmp_path)
    assert mem == {"cgroup": 2, "used_mb": 500, "limit_mb": 1536, "peak_mb": 900, "oom_kills": 1, "swap_mb": None}
    (tmp_path / "memory.max").write_text("max")
    assert sysmon.container_memory(tmp_path)["limit_mb"] is None


def test_container_memory_cgroup_v1_without_limit(tmp_path):
    base = tmp_path / "memory"
    base.mkdir()
    (base / "memory.usage_in_bytes").write_text(str(400 * 2**20))
    (base / "memory.limit_in_bytes").write_text("9223372036854771712")  # "unlimited"
    (base / "memory.max_usage_in_bytes").write_text(str(450 * 2**20))
    (base / "memory.stat").write_text(f"cache 5\ntotal_inactive_file {100 * 2**20}\n")
    (base / "memory.oom_control").write_text("oom_kill_disable 0\nunder_oom 0\noom_kill 2\n")
    (base / "memory.memsw.usage_in_bytes").write_text(str(410 * 2**20))
    mem = sysmon.container_memory(tmp_path, host_total_mb=5888)
    assert mem == {"cgroup": 1, "used_mb": 300, "limit_mb": None, "peak_mb": 450, "oom_kills": 2, "swap_mb": 10}
    assert sysmon.container_memory(tmp_path / "nothing") is None


def test_process_tree_counts_children():
    child = subprocess.Popen([sys.executable, "-c", "import time; x = bytearray(30 * 2**20); time.sleep(5)"])
    try:
        for _ in range(50):  # wait until the child has touched its 30 MB
            if (sysmon.process_tree_mb(child.pid) or 0) >= 30:
                break
            asyncio.run(asyncio.sleep(0.05))
        alone = sysmon.process_tree_mb(child.pid)
        together = sysmon.process_tree_mb(os.getpid())
        assert alone >= 30 and together >= alone
        assert sysmon.process_tree_mb(child.pid, root_peak=True) >= alone  # high-water mark ≥ current
    finally:
        child.kill()
        child.wait()
    assert sysmon.process_tree_mb(child.pid) is None


# ---------------------------------------------------------------- lean CLI
def test_claude_env_low_memory(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    monkeypatch.setenv("BUN_OPTIONS", "--inspect=0")
    env = claude_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["BUN_OPTIONS"] == "--inspect=0 --smol" and env["MALLOC_ARENA_MAX"] == "2"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    monkeypatch.setenv("BUN_OPTIONS", "--smol")
    assert claude_env()["BUN_OPTIONS"] == "--smol"  # not twice
    monkeypatch.delenv("BUN_OPTIONS")
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    plain = claude_env(low_memory=False)
    assert "BUN_OPTIONS" not in plain and "MALLOC_ARENA_MAX" not in plain


def test_command_loads_no_mcp_servers(parts):
    _store, _tasks, _state, runner = parts
    assert "--strict-mcp-config" in runner.build_command(JOB_TYPES["general"])


# ---------------------------------------------------------------- waiting for free RAM
def test_job_waits_for_free_ram_then_starts(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    job = store.create("general", "สรุปสั้น ๆ", "ช่วยสรุป")
    monkeypatch.setattr(sysmon, "host_memory", fake_host(420))

    async def scenario():
        runner.tick()
        assert not runner.active and store.get(job["id"])["status"] == QUEUED
        wait = runner.wait_info(job["id"])
        assert wait["available_mb"] == 420 and wait["need_mb"] == 700 and wait["start_by"]
        monkeypatch.setattr(sysmon, "host_memory", fake_host(2400))
        runner.tick()
        assert job["id"] in runner.active and runner.mem_wait is None
        await asyncio.gather(*runner.active.values())

    asyncio.run(scenario())
    done = store.get(job["id"])
    assert done["status"] == NEEDS_REVIEW, done["error"]
    steps, _ = store.read_steps(job["id"])
    assert not any(s.get("kind") == "warn" for s in steps)


def test_job_starts_anyway_after_waiting_long_enough(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    job = store.create("general", "งานด่วน", "ช่วยสรุป")
    monkeypatch.setattr(sysmon, "host_memory", fake_host(300))

    async def scenario():
        runner.tick()
        assert runner.mem_wait and not runner.active
        runner.mem_wait["clock"] -= runner.settings.mem_wait_max_min * 60 + 1  # pretend the wait is over
        runner.tick()
        assert job["id"] in runner.active
        await asyncio.gather(*runner.active.values())

    asyncio.run(scenario())
    steps, _ = store.read_steps(job["id"])
    warn = [s["text"] for s in steps if s.get("kind") == "warn"]
    assert warn and "300 MB" in warn[0] and store.get(job["id"])["status"] == NEEDS_REVIEW


def test_gate_off_and_unreadable_meminfo_never_block(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    monkeypatch.setattr(sysmon, "host_memory", lambda *a, **k: None)
    assert runner.memory_gate("x")
    monkeypatch.setattr(sysmon, "host_memory", fake_host(10))
    off = Runner(store, runner.tasks, runner.state, replace(runner.settings, min_free_mem_mb=0))
    assert off.memory_gate("x")


def test_cancelled_waiting_job_clears_the_wait(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    job = store.create("general", "ยกเลิก", "x")
    monkeypatch.setattr(sysmon, "host_memory", fake_host(100))

    async def scenario():
        runner.tick()
        assert runner.mem_wait
        await runner.cancel(job["id"])
        runner.tick()
        assert runner.mem_wait is None

    asyncio.run(scenario())


# ---------------------------------------------------------------- peak memory and OOM
def test_run_records_peak_memory(parts, monkeypatch):
    store, _tasks, state, runner = parts
    monkeypatch.setattr(runner_module, "MEM_FIRST_SAMPLE_SEC", 0.2)
    monkeypatch.setattr(runner_module, "MEM_SAMPLE_SEC", 0.05)
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.2")
    job = store.create("general", "วัด RAM", "x")
    asyncio.run(runner.run_job(job["id"]))
    done = store.get(job["id"])
    peak = done["runs"][-1]["peak_mem_mb"]
    assert done["status"] == NEEDS_REVIEW and isinstance(peak, int) and peak > 0
    steps, _ = store.read_steps(job["id"])
    assert f"RAM สูงสุด {peak} MB" in steps[-1]["detail"]
    assert state.get("mem_runs")[-1]["peak_mb"] == peak


def test_sigkill_is_reported_as_memory_problem(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    monkeypatch.setenv("FAKE_CLAUDE_CRASH", "kill")
    job = store.create("general", "โดน OOM", "x")
    asyncio.run(runner.run_job(job["id"]))
    done = store.get(job["id"])
    assert done["status"] == FAILED and done["error"].startswith(OOM_MESSAGES["killed"])


def test_out_of_memory_on_stderr_is_reported(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    monkeypatch.setenv("FAKE_CLAUDE_CRASH", "oom")
    job = store.create("general", "heap เต็ม", "x")
    asyncio.run(runner.run_job(job["id"]))
    done = store.get(job["id"])
    assert done["status"] == FAILED and done["error"].startswith(OOM_MESSAGES["runtime"])


def test_container_oom_kill_counter_wins(parts, monkeypatch):
    store, _tasks, _state, runner = parts
    counts = iter([0, 1])
    monkeypatch.setattr(sysmon, "container_memory", lambda *a, **k: {"oom_kills": next(counts)})
    monkeypatch.setenv("FAKE_CLAUDE_CRASH", "kill")
    job = store.create("general", "เกินเพดาน", "x")
    asyncio.run(runner.run_job(job["id"]))
    assert store.get(job["id"])["error"].startswith(OOM_MESSAGES["container"])


# ---------------------------------------------------------------- uploads and API
def test_uploads_stream_to_disk_and_respect_the_limit(settings):
    big = os.urandom(3 * 2**20 + 123)
    with TestClient(create_app(settings, start_background=False)) as client:
        res = client.post("/api/jobs", data={"type": "general", "instructions": "ดูไฟล์แนบ"},
                          files=[("files", ("แบบ ตู้ MDB.pdf", big, "application/pdf"))])
        assert res.status_code == 200, res.text
        job_id = res.json()["id"]
        saved = settings.jobs_dir / job_id / "input" / "แบบ ตู้ MDB.pdf"
        assert saved.read_bytes() == big
        assert not any((settings.state_dir / "uploads").glob("*"))  # staging cleaned up

    small = replace(settings, max_upload_mb=1)
    with TestClient(create_app(small, start_background=False)) as client:
        res = client.post("/api/jobs", data={"type": "general", "instructions": "x"},
                          files=[("files", ("big.bin", big, "application/octet-stream"))])
        assert res.status_code == 413 and "MAX_UPLOAD_MB" in res.json()["detail"]
        assert [j["id"] for j in client.get("/api/jobs").json()] == [job_id]  # no half-made job added


def test_status_and_system_report_memory(settings, monkeypatch):
    monkeypatch.setattr(sysmon, "host_memory", fake_host(450))
    with TestClient(create_app(settings, start_background=False)) as client:
        status = client.get("/api/status").json()
        assert status["memory"]["low"] is True and status["memory"]["need_mb"] == 700
        assert status["limits"]["max_upload_mb"] == 200
        system = client.get("/api/system").json()
        assert system["memory"]["host"]["available_mb"] == 450
        assert system["memory"]["settings"]["min_free_mb"] == 700 and system["memory"]["settings"]["low_memory"]
        assert system["storage"]["embed_fonts"] is True


# ---------------------------------------------------------------- Anuphan in Word
MD = "# ใบเสนอราคา ตู้ MDB\n\nราคารวม **48,500 บาท** ±5%\n\n| รายการ | ราคา |\n|---|---|\n| Breaker 400A | 48,500 |\n"


def test_word_export_embeds_anuphan():
    data = markdown_to_docx(MD, title="ใบเสนอราคา", file_name="proposal.md", rev=1, modified=None)
    z = zipfile.ZipFile(io.BytesIO(data))
    fonts = sorted(n for n in z.namelist() if n.startswith("word/fonts/"))
    assert fonts == ["word/fonts/font1.odttf", "word/fonts/font2.odttf"]
    table = z.read("word/fontTable.xml").decode()
    assert 'w:name="Anuphan"' in table and "w:embedRegular" in table and "w:embedBold" in table
    assert "<w:embedTrueTypeFonts/>" in z.read("word/settings.xml").decode()
    assert "obfuscatedFont" in z.read("[Content_Types].xml").decode()
    import re
    key = re.search(r'<w:embedRegular r:id="[^"]+" w:fontKey="(\{[^}]+\})"', table).group(1)
    blob = z.read("word/fonts/font1.odttf")
    assert blob[:32] != (FONT_DIR / "Anuphan-Regular.ttf").read_bytes()[:32]  # really obfuscated
    assert _obfuscate(blob, key) == (FONT_DIR / "Anuphan-Regular.ttf").read_bytes()
    Document(io.BytesIO(data))  # still opens


def test_word_export_without_embedding_and_with_foreign_template(tmp_path):
    plain = markdown_to_docx(MD, title="x", file_name="a.md", rev=1, modified=None, embed_fonts=False)
    assert not [n for n in zipfile.ZipFile(io.BytesIO(plain)).namelist() if n.startswith("word/fonts/")]
    template = tmp_path / "report-template.docx"
    Document().save(template)  # a template whose styles use other fonts
    data = markdown_to_docx(MD, title="x", file_name="a.md", rev=1, modified=None, template=template)
    assert not [n for n in zipfile.ZipFile(io.BytesIO(data)).namelist() if n.startswith("word/fonts/")]
