"""Phase 10: Claude's python3 in a jail, and the dashboard's own defences."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.jobtypes import JOB_TYPES

TOOLS = Path(__file__).resolve().parents[1] / "tools"
REFUSED = "ผู้ช่วยงานไม่อนุญาต"


def jailed(job_dir: Path, code: str, *private: Path, extra_env: dict | None = None, module: list | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIWA_")}
    # pytest's own temp folders are under /tmp, which the jail leaves writable: give the job its own temp folder,
    # so "outside the job folder" really is outside everything Python may write to (as under /data on the NAS)
    temp = job_dir.parents[2] / "tmp"
    temp.mkdir(exist_ok=True)
    env.update(PYTHONPATH=str(TOOLS), AIWA_PYTHON_JAIL=str(job_dir), PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(temp),
               AIWA_JAIL_NO_READ=os.pathsep.join(str(p) for p in private), **(extra_env or {}))
    args = [sys.executable, "-m", *module] if module else [sys.executable, "-c", code]
    return subprocess.run(args, cwd=job_dir, env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def folders(tmp_path):
    jobs = tmp_path / "data" / "jobs"
    job = jobs / "20261102-120000-eng-qa-ab12"
    (job / "output").mkdir(parents=True)
    (job / "input").mkdir()
    (job / "input" / "spec.txt").write_text("มอเตอร์ 75 kW", encoding="utf-8")
    other = jobs / "20261101-090000-proposal-draft-cd34"
    other.mkdir(parents=True)
    (other / "job.json").write_text('{"customer": "Beta"}', encoding="utf-8")
    login = tmp_path / "claude-home"
    login.mkdir()
    (login / ".credentials.json").write_text('{"token": "secret"}', encoding="utf-8")
    return job, jobs, login


@pytest.mark.parametrize("what, code", [
    ("network", "import socket; socket.create_connection(('example.com', 443))"),
    ("name lookup", "import socket; socket.gethostbyname('example.com')"),
    ("web request", "import urllib.request; urllib.request.urlopen('http://example.com')"),
    ("other program", "import subprocess; subprocess.run(['echo', 'hi'])"),
    ("shell", "import os; os.system('echo hi')"),
    ("unguarded python", "import os, sys; os.execv(sys.executable, [sys.executable, '-I', '-c', 'print(1)'])"),
    ("native code", "import ctypes; ctypes.CDLL(None)"),
    ("write elsewhere", "open('../../outside.txt', 'w').write('x')"),
    ("rename out", "import os; open('output/a.md', 'w').write('x'); os.rename('output/a.md', '../../a.md')"),
    ("process environment", "open('/proc/self/environ', 'rb').read()"),
])
def test_python_in_a_job_cannot_reach_out(folders, what, code):
    job, jobs, login = folders
    result = jailed(job, code, login, jobs)
    assert result.returncode != 0 and REFUSED in result.stderr, (what, result.stderr[-300:])
    assert not (job.parent.parent / "outside.txt").exists()


def test_secrets_and_other_jobs_are_unreadable_but_the_job_itself_is_fine(folders):
    job, jobs, login = folders
    assert REFUSED in jailed(job, f"open({str(login / '.credentials.json')!r}).read()", login, jobs).stderr
    other = jobs / "20261101-090000-proposal-draft-cd34" / "job.json"
    assert REFUSED in jailed(job, f"open({str(other)!r}).read()", login, jobs).stderr
    ok = jailed(job, "import csv; print(open('input/spec.txt', encoding='utf-8').read());"
                     "open('output/calc.md', 'w').write(str(round(172000 * 1.07, 2))); print('done')", login, jobs)
    assert ok.returncode == 0 and "มอเตอร์ 75 kW" in ok.stdout and "done" in ok.stdout, ok.stderr
    assert (job / "output" / "calc.md").read_text() == "184040.0"
    calc = jailed(job, "", login, jobs, module=["pec_calc", "current", "--kw", "75", "--v", "400", "--phase", "3",
                                                 "--pf", "0.86", "--eff", "0.954"])
    assert calc.returncode == 0 and "131.9" in calc.stdout, calc.stderr
    free = subprocess.run([sys.executable, "-c", "import socket; socket.socket(); print('ok')"],
                          env={**os.environ, "PYTHONPATH": str(TOOLS)}, capture_output=True, text=True)
    assert free.stdout.strip() == "ok"  # no AIWA_PYTHON_JAIL: an ordinary Python


def test_knowledge_search_still_works_in_the_jail(folders, tmp_path):
    sys.path.insert(0, str(TOOLS))
    import kb_search

    job, jobs, login = folders
    knowledge = tmp_path / "knowledge"
    (knowledge / "engineering").mkdir(parents=True)
    (knowledge / "engineering" / "guide.md").write_text("แรงดันตกไม่เกิน 3% สำหรับวงจรย่อย", encoding="utf-8")
    index = tmp_path / "data" / "state" / "kb.sqlite"
    index.parent.mkdir(parents=True)
    kb_search.refresh(index, {"knowledge": knowledge})
    found = jailed(job, "", login, jobs, index.parent, extra_env={"KB_INDEX": str(index)},
                   module=["kb_search", "แรงดันตก"])
    assert found.returncode == 0 and "guide.md" in found.stdout, found.stderr


def test_jobs_run_jailed_and_without_the_apps_secrets(parts, settings, monkeypatch, tmp_path):
    store, _tasks, _state, runner = parts
    monkeypatch.setenv("NOTIFY_LINE_TOKEN", "line-secret")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "dash-secret")
    dump = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLAUDE_DUMP_ENV", str(dump))
    job = store.create("eng-qa", "สาย 75 kW", "เลือกสาย")
    asyncio.run(runner.run_job(job["id"]))
    env = json.loads(dump.read_text())
    assert env["AIWA_PYTHON_JAIL"] == str(store.job_dir(job["id"]))
    private = env["AIWA_JAIL_NO_READ"].split(os.pathsep)
    assert str(settings.state_dir) in private and str(settings.jobs_dir) in private
    assert "NOTIFY_LINE_TOKEN" not in env and "DASHBOARD_PASSWORD" not in env
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(TOOLS)

    dump.unlink()
    plain = store.create("meeting-summary", "ประชุม", "")  # no python, no index: no Bash, no jail needed
    store.add_file(plain["id"], "input", "t.txt", b"x")
    asyncio.run(runner.run_job(plain["id"]))
    assert "AIWA_PYTHON_JAIL" not in json.loads(dump.read_text())
    cmd = runner.build_command(JOB_TYPES["meeting-summary"], kb=False)
    assert "Bash" not in cmd[cmd.index("--tools") + 1]


# -- the dashboard's own defences ----------------------------------------------------------------------------
from dataclasses import replace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


def test_other_sites_cannot_make_your_browser_act_here(settings):
    app = create_app(replace(settings, dashboard_url="https://nas.example.com"), start_background=False)
    with TestClient(app) as client:
        task = {"task": "x"}
        evil = client.post("/api/tasks", json=task, headers={"Origin": "http://evil.example"})
        assert evil.status_code == 403 and "เว็บไซต์อื่น" in evil.json()["detail"]
        form = client.post("/api/jobs", data={"type": "general", "instructions": "ใช้โควต้าฉัน"},
                           headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"})
        assert form.status_code == 403 and client.get("/api/jobs").json() == []  # the classic CSRF form post
        assert client.post("/api/tasks", json=task, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/api/tasks", json=task, headers={"Origin": "null"}).status_code == 403
        assert client.delete("/api/jobs/x", headers={"Origin": "http://evil.example"}).status_code == 403
        # the dashboard itself, a reverse proxy in front of it, and scripts all pass
        assert client.post("/api/tasks", json=task, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert client.post("/api/tasks", json=task, headers={"Origin": "http://testserver"}).status_code == 200
        assert client.post("/api/tasks", json=task, headers={"Origin": "https://nas.example.com"}).status_code == 200
        assert client.post("/api/tasks", json=task, headers={"Origin": "https://dsm.example.com",
                                                             "X-Forwarded-Host": "dsm.example.com"}).status_code == 200
        assert client.post("/api/tasks", json=task).status_code == 200
        assert client.get("/api/tasks", headers={"Origin": "http://evil.example"}).status_code == 200  # reading


def test_security_headers_and_sandboxed_files(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        page = client.get("/")
        csp = page.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
        assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" not in client.get("/api/status").headers  # JSON needs no page policy
        assert client.get("/api/status").headers["x-content-type-options"] == "nosniff"
        store = app.state.store
        job = store.create("general", "html", "x")
        store.add_file(job["id"], "output", "page.html", b"<script>fetch('/api/jobs', {method:'DELETE'})</script>")
        raw = client.get(f"/api/jobs/{job['id']}/files/output/page.html?raw=1")
        assert raw.headers["content-security-policy"] == "sandbox" and "attachment" in raw.headers["content-disposition"]


def test_guessing_the_password_gets_slowed_down(settings):
    from app.main import BasicAuth

    app = create_app(replace(settings, dashboard_password="right"), start_background=False)
    good = {"Authorization": "Basic " + __import__("base64").b64encode(b"admin:right").decode()}
    bad = {"Authorization": "Basic " + __import__("base64").b64encode(b"admin:wrong").decode()}
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401  # no password sent: not counted
        assert all(client.get("/api/status", headers=bad).status_code == 401 for _ in range(10))
        assert client.get("/api/status", headers=bad).status_code == 429
        locked = client.get("/api/status", headers=good)
        assert locked.status_code == 429 and locked.headers["retry-after"] == "600"
    guard = BasicAuth(None, "admin", "right")
    guard.failures["10.0.0.9"] = __import__("collections").deque([0.0] * 10)
    assert guard.locked("10.0.0.9", 100.0) and not guard.locked("10.0.0.9", 601.0)  # the window passes


def test_zip_bombs_are_refused_before_they_unpack(tmp_path, monkeypatch):
    import zipfile

    sys.path.insert(0, str(TOOLS))
    import kb_search
    import xlsx_text

    from app.convert import docx_to_text, prepare_inputs

    bomb = tmp_path / "input" / "bomb.docx"
    bomb.parent.mkdir()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b" " * (65 * 1024 * 1024))  # ~65 KB on disk, 65 MB unpacked
    assert bomb.stat().st_size < 1024 * 1024
    with pytest.raises(ValueError):
        docx_to_text(bomb)
    assert any("bomb.docx" in w for w in prepare_inputs(bomb.parent))
    assert kb_search.extract(bomb)[1] == []

    sample = Path(__file__).parent / "fixtures" / "boq-sample.xlsx"
    assert xlsx_text.read_workbook(sample)  # a normal workbook reads as before
    monkeypatch.setattr(xlsx_text, "MAX_SHEET_XML", 10)  # pretend each sheet unpacked to more than the limit
    with pytest.raises(xlsx_text.XlsxError):
        xlsx_text.read_workbook(sample)
