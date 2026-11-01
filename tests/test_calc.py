"""pec_calc: every formula checked against a hand calculation, plus tables, sign-off and the CLI."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import pec_calc as pc  # noqa: E402

TABLE = """# title: สาย CV 4 แกน บน cable tray (ตัวอย่าง)
# source: ค่าทดสอบ
size_mm2,ampacity_a,mv_per_a_m
35,125,1.10
50,150,0.82
70,190,0.57
95,230,0.42
120,265,0.34
"""


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    tables = tmp_path / "engineering" / "tables"
    tables.mkdir(parents=True)
    (tables / "cv-4c-tray.csv").write_text(TABLE, encoding="utf-8")
    monkeypatch.setenv("PEC_CALC_KNOWLEDGE", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- formulas (C1–C9 in docs/calc-tools.md)
def test_c1_motor_current_three_phase():
    assert pc.load_current(kw=75, v=400, pf=0.85, eff=0.94)["result"]["amps"] == pytest.approx(135.49, abs=0.01)
    assert pc.load_current(kw=75, v=380, pf=0.85, eff=0.94)["result"]["amps"] == pytest.approx(142.62, abs=0.01)


def test_c2_current_from_kva_and_single_phase():
    assert pc.load_current(kva=1000, v=400)["result"]["amps"] == pytest.approx(1443.38, abs=0.01)
    assert pc.load_current(kw=2.2, v=230, phase=1, pf=0.9)["result"]["amps"] == pytest.approx(10.63, abs=0.01)


def test_c3_resistance_corrected_to_operating_temperature():
    assert pc.resistance(35, "cu", 90) == pytest.approx(0.524 * (1 + 0.00393 * 70))
    assert pc.resistance(240, "al", 20) == pytest.approx(0.125)


def test_c4_voltage_drop_three_phase():
    res = pc.voltage_drop(100, 100, size=35)
    assert res["result"]["volts"] == pytest.approx(10.57, abs=0.01)
    assert res["result"]["percent"] == pytest.approx(2.64, abs=0.01) and res["verdict"]["pass"]


def test_c5_voltage_drop_single_phase_and_parallel_runs():
    res = pc.voltage_drop(16, 30, size=2.5, temp=70, pf=1.0, v=230, phase=1)
    assert res["result"]["volts"] == pytest.approx(2 * 16 * 0.03 * 7.41 * (1 + 0.00393 * 50), abs=0.01)
    assert not res["verdict"]["pass"]  # 3.70 % > 3 %
    one, two = pc.voltage_drop(200, 50, size=95), pc.voltage_drop(200, 50, size=95, runs=2)
    assert two["result"]["volts"] == pytest.approx(one["result"]["volts"] / 2, abs=0.01)


def test_c6_voltage_drop_from_mv_per_a_m():
    res = pc.voltage_drop(135.5, 80, mv_per_am=0.57)
    assert res["result"]["volts"] == pytest.approx(6.18, abs=0.01) and res["result"]["percent"] == pytest.approx(1.54, abs=0.01)


def test_c7_transformer_fault_current():
    assert pc.transformer_isc(1000, v=400, z=6)["result"]["isc_ka"] == pytest.approx(24.06, abs=0.01)
    assert pc.transformer_isc(1000, v=400, z=6, source_mva=500)["result"]["isc_ka"] == pytest.approx(23.28, abs=0.01)
    assert pc.transformer_isc(1000, v=400, z=6, c=1.05)["result"]["isc_ka"] == pytest.approx(25.26, abs=0.01)


def test_c8_power_factor_correction():
    assert pc.pf_correction(100, 0.75, 0.95)["result"]["kvar"] == pytest.approx(55.3, abs=0.05)


def test_c9_breaker_rating():
    assert pc.breaker_rating(135.5)["result"]["rating_a"] == 200
    assert pc.breaker_rating(128)["result"]["rating_a"] == 160  # exactly 160 A needed
    assert pc.breaker_rating(100, factor=1.0)["result"]["rating_a"] == 100


# ---------------------------------------------------------------- cable selection
def test_cable_with_company_table(knowledge):
    res = pc.select_cable(135.5, 80, table="cv-4c-tray")
    assert res["result"]["size_mm2"] == 70 and res["result"]["design_amps"] == pytest.approx(169.4, abs=0.05)
    assert res["summary"] == "เลือกสาย 70 mm²" and not res["warnings"]
    assert pc.select_cable(135.5, 80, table="cv-4c-tray", derate=0.85)["result"]["size_mm2"] == 95


def test_cable_without_ampacity_says_so():
    res = pc.select_cable(135.5, 80)
    assert res["result"]["size_mm2"] == 35
    assert "ยังไม่ได้ตรวจกระแส" in res["summary"] and res["warnings"]


def test_cable_nothing_fits(knowledge):
    res = pc.select_cable(400, 200, table="cv-4c-tray")
    assert res["result"]["size_mm2"] is None and not res["verdict"]["pass"]
    assert any("--runs 2" in w for w in res["warnings"])


def test_table_with_excel_bom_and_aliases(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("﻿Size,Ampacity,mV\n50,150,0.82\n35,125,1.1\n", encoding="utf-8")
    data = pc.load_table(str(path))
    assert [r["size_mm2"] for r in data["rows"]] == [35, 50] and data["columns"] == ["ampacity_a", "mv_per_a_m", "size_mm2"]
    (tmp_path / "bad.csv").write_text("size,colour\n35,red\n", encoding="utf-8")
    with pytest.raises(pc.CalcError):
        pc.load_table(str(tmp_path / "bad.csv"))


# ---------------------------------------------------------------- input checks and sign-off
@pytest.mark.parametrize("call", [
    lambda: pc.load_current(kw=-5),
    lambda: pc.load_current(kw=10, kva=10),
    lambda: pc.load_current(kw=10, pf=1.2),
    lambda: pc.voltage_drop(10, 5, size=3),
    lambda: pc.voltage_drop(10, 5),
    lambda: pc.pf_correction(100, 0.95, 0.9),
    lambda: pc.breaker_rating(10000),
])
def test_bad_input_is_refused(call):
    with pytest.raises(pc.CalcError):
        call()


def test_sign_off_counts_only_for_its_version(knowledge):
    assert pc.voltage_drop(100, 100, size=35)["verification"]["status"] == "unverified"
    signoff = knowledge / "engineering" / "calc-verified.json"
    signoff.write_text(json.dumps({"vdrop": {"by": "วิศวกร ก.", "date": "2026-11-15", "version": pc.VERSION}}), encoding="utf-8")
    check = pc.voltage_drop(100, 100, size=35)["verification"]
    assert check["status"] == "verified" and check["by"] == "วิศวกร ก."
    assert "ตรวจแล้วโดย วิศวกร ก." in pc.render(pc.voltage_drop(100, 100, size=35))
    signoff.write_text(json.dumps({"vdrop": {"by": "x", "date": "2025-01-01", "version": "0.9"}}), encoding="utf-8")
    assert pc.voltage_drop(100, 100, size=35)["verification"]["status"] == "outdated"


# ---------------------------------------------------------------- command line, as Claude runs it
def run_cli(*args, knowledge_dir=None):
    env = {**os.environ, "PYTHONPATH": str(TOOLS)}
    if knowledge_dir:
        env["PEC_CALC_KNOWLEDGE"] = str(knowledge_dir)
    return subprocess.run([sys.executable, "-m", "pec_calc", *args], capture_output=True, text=True, env=env,
                          cwd=str(ROOT), timeout=30)


def test_cli_text_json_and_errors(knowledge):
    out = run_cli("cable", "--amps", "135.5", "--length", "80", "--table", "cv-4c-tray", knowledge_dir=knowledge)
    assert out.returncode == 0 and "เลือกสาย 70 mm²" in out.stdout and "สถานะการตรวจสอบ" in out.stdout
    data = json.loads(run_cli("isc", "--kva", "1000", "--json").stdout)
    assert data["result"]["isc_ka"] == pytest.approx(24.06, abs=0.01) and data["tool"] == "pec_calc"
    listing = run_cli("tables", knowledge_dir=knowledge)
    assert "cv-4c-tray" in listing.stdout
    bad = run_cli("vdrop", "--amps", "10", "--length", "5", "--size", "3")
    assert bad.returncode == 2 and "ข้อผิดพลาด" in bad.stderr
