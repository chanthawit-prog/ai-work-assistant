"""Phase 11: the readiness check for the first install."""
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app import readiness
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def by_key(result):
    return {i["key"]: i for i in result["items"]}


def test_a_fresh_install_is_checked_part_by_part(settings):
    logged_in = {"installed": True, "logged_in": True, "method": "claude.ai", "version": "2.1.316 (Claude Code)"}
    result = readiness.run_checks(replace(settings, claude_bin="claude"), logged_in, [], 3)
    items = by_key(result)
    assert items["claude"]["status"] == "ok" and "2.1.316" in items["claude"]["detail"]
    assert items["data"]["status"] == "ok"
    assert items["knowledge"]["status"] == "warn" and ":ro" in items["knowledge"]["fix"]  # tmp folder: writable
    assert items["python_jail"]["status"] == "ok"  # python3 on PATH loads tools/sitecustomize.py
    assert items["pec_calc"]["status"] == "ok"
    assert items["password"]["status"] == "warn" and "DASHBOARD_PASSWORD" in items["password"]["fix"]
    assert items["kb"]["status"] == "ok" and items["notify"]["status"] == "info"
    assert items["timezone"]["status"] in ("ok", "warn", "info") and "memory" in items
    assert result["status"] == "warn"  # usable, with things to fix
    for i in result["items"]:
        assert i["status"] == "ok" or i["status"] == "info" or i["fix"], i  # every problem says what to do


def test_what_must_be_fixed_comes_with_the_fix(settings, tmp_path):
    missing = replace(settings, knowledge_dir=tmp_path / "nowhere", dashboard_password="x",
                      dashboard_url="", claude_bin="claude")
    result = readiness.run_checks(missing, {"installed": True, "logged_in": False, "version": "2.1"}, ["line"], 0)
    items = by_key(result)
    assert result["status"] == "fail"
    assert items["claude"]["status"] == "fail" and "claude auth login" in items["claude"]["fix"]
    assert items["knowledge"]["status"] == "fail" and "KNOWLEDGE_PATH" in items["knowledge"]["fix"]
    assert items["notify"]["status"] == "warn" and "DASHBOARD_URL" in items["notify"]["fix"]
    assert items["password"]["status"] == "ok"
    demo = by_key(readiness.run_checks(settings, {"installed": True, "logged_in": True, "provider": "fake-claude"}, [], 0))
    assert demo["claude"]["status"] == "warn" and "fake-claude" in demo["claude"]["detail"]
    assert by_key(readiness.run_checks(settings, None, [], 0))["claude"]["status"] == "warn"


def test_a_python_without_the_jail_is_reported(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(readiness, "TOOLS_DIR", tmp_path)  # sitecustomize.py and pec_calc not on the path
    items = by_key(readiness.run_checks(replace(settings, claude_bin="claude"), {"installed": True, "logged_in": True}, [], 0))
    assert items["python_jail"]["status"] == "fail" and "ALLOW_PYTHON=false" in items["python_jail"]["fix"]
    assert items["pec_calc"]["status"] == "fail"
    off = by_key(readiness.run_checks(replace(settings, allow_python=False), None, [], 0))
    assert off["python"]["status"] == "info" and "python_jail" not in off


def test_from_the_dashboard_and_from_a_terminal(settings):
    app = create_app(settings, start_background=False)
    app.state.runtime.set("claude", {"installed": True, "logged_in": True, "provider": "fake-claude"})
    with TestClient(app) as client:
        result = client.get("/api/readiness").json()
        assert result["status"] in ("warn", "fail") and {"claude", "data", "python_jail"} <= set(by_key(result))
    env = {**os.environ, "DATA_DIR": str(settings.data_dir), "KNOWLEDGE_DIR": str(settings.knowledge_dir),
           "CLAUDE_BIN": str(ROOT / "scripts" / "fake-claude"), "PYTHONPATH": str(ROOT)}
    cli = subprocess.run([sys.executable, "-m", "app.readiness"], cwd=ROOT, env=env, capture_output=True,
                         text=True, timeout=120)
    assert cli.returncode == 0, cli.stderr  # warnings only: the demo mode and the writable test folders
    assert "โหมดสาธิต" in cli.stdout and "รั้ว Python ของ Claude" in cli.stdout and "ใช้งานได้ แต่มีข้อควรแก้" in cli.stdout
