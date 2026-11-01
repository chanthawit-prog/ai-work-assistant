"""pec_calc: standard low-voltage electrical calculations, with the formula, the values used, the
assumptions and the references printed next to every result so an engineer can check them.

    python3 -m pec_calc current --kw 75 --v 400 --pf 0.85 --eff 0.94
    python3 -m pec_calc vdrop --amps 135.5 --length 80 --size 70
    python3 -m pec_calc cable --amps 135.5 --length 80 --factor 1.25 --limit 3 --table cv-4c-tray
    python3 -m pec_calc isc --kva 1000 --v 400 --z 6
    python3 -m pec_calc pfc --kw 100 --pf-from 0.75 --pf-to 0.95
    python3 -m pec_calc breaker --amps 135.5 --factor 1.25
    python3 -m pec_calc tables

Add --json for machine-readable output. Standard library only, so it runs with any python3 and keeps
nothing in memory between calls.

Built-in data: maximum DC resistance of class 2 (stranded) conductors at 20 °C, IEC 60228 Table 2.
Company data: CSV tables in <knowledge>/engineering/tables/ that the engineering team owns
(ampacity for an installation method, mV/A/m). Sign-off per function: <knowledge>/engineering/calc-verified.json.
The knowledge folder comes from PEC_CALC_KNOWLEDGE or KNOWLEDGE_DIR.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
from pathlib import Path

VERSION = "1.0"
SQRT3 = math.sqrt(3)

# IEC 60228 Table 2, class 2 (stranded) plain conductors: maximum DC resistance at 20 °C, ohm/km
R20 = {
    "cu": {1.5: 12.1, 2.5: 7.41, 4: 4.61, 6: 3.08, 10: 1.83, 16: 1.15, 25: 0.727, 35: 0.524, 50: 0.387,
           70: 0.268, 95: 0.193, 120: 0.153, 150: 0.124, 185: 0.0991, 240: 0.0754, 300: 0.0601, 400: 0.0470,
           500: 0.0366, 630: 0.0283},
    "al": {16: 1.91, 25: 1.20, 35: 0.868, 50: 0.641, 70: 0.443, 95: 0.320, 120: 0.253, 150: 0.206,
           185: 0.164, 240: 0.125, 300: 0.100, 400: 0.0778, 500: 0.0605, 630: 0.0469},
}
ALPHA = {"cu": 0.00393, "al": 0.00403}  # temperature coefficient of resistance at 20 °C, 1/K
MATERIAL = {"cu": "ทองแดง", "al": "อะลูมิเนียม"}
BREAKER_RATINGS = (6, 10, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250, 320, 400, 500, 630, 800,
                   1000, 1250, 1600, 2000, 2500, 3200, 4000, 5000, 6300)
REF_R20 = "IEC 60228 ตาราง 2: ความต้านทาน DC สูงสุดของตัวนำตีเกลียว (class 2) ที่ 20°C"
REF_ALPHA = "สัมประสิทธิ์อุณหภูมิของความต้านทานที่ 20°C: ทองแดง 0.00393, อะลูมิเนียม 0.00403 ต่อ °C (IEC 60287-1-1)"
COLUMN_ALIASES = {"size_mm2": ("size_mm2", "size", "mm2", "sqmm"), "ampacity_a": ("ampacity_a", "ampacity", "amps", "a"),
                  "mv_per_a_m": ("mv_per_a_m", "mv", "mv/a/m", "mv_a_m")}


class CalcError(ValueError):
    """Bad input; the message is shown to the user as is."""


# ------------------------------------------------------------------ company data and sign-off
def knowledge_dir() -> Path | None:
    for key in ("PEC_CALC_KNOWLEDGE", "KNOWLEDGE_DIR"):
        if os.environ.get(key):
            return Path(os.environ[key])
    return None


def tables_dir() -> Path | None:
    base = knowledge_dir()
    return base / "engineering" / "tables" if base else None


def verification(function: str) -> dict:
    """Whether the team has checked this function, from <knowledge>/engineering/calc-verified.json:
    {"vdrop": {"by": "ชื่อวิศวกร", "date": "2026-11-10", "version": "1.0", "note": "..."}}.
    A sign-off counts only for the version it was given for; a new version needs a new check."""
    base = knowledge_dir()
    path = base / "engineering" / "calc-verified.json" if base else None
    try:
        data = json.loads(path.read_text("utf-8")) if path and path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    entry = data.get(function) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return {"status": "unverified"}
    status = "verified" if str(entry.get("version")) == VERSION else "outdated"
    return {"status": status, "by": entry.get("by"), "date": entry.get("date"), "version": entry.get("version"),
            "note": entry.get("note", "")}


def load_table(name: str) -> dict:
    """A company table: CSV with `# title:` / `# source:` comment lines, then columns
    size_mm2 and ampacity_a and/or mv_per_a_m (Excel "CSV UTF-8" files work)."""
    path = Path(name)
    if not path.is_file():
        folder = tables_dir()
        path = folder / (name if name.endswith(".csv") else f"{name}.csv") if folder else path
    if not path.is_file():
        raise CalcError(f"ไม่พบตาราง {name} (วางไฟล์ CSV ไว้ที่ knowledge/engineering/tables/)")
    meta, body = {}, []
    for line in path.read_text("utf-8-sig").splitlines():
        if line.lstrip().startswith("#"):
            key, _, value = line.lstrip("# ").partition(":")
            if value:
                meta[key.strip().lower()] = value.strip()
        elif line.strip():
            body.append(line)
    reader = csv.DictReader(io.StringIO("\n".join(body)))
    names = {}
    for column in reader.fieldnames or []:
        for key, aliases in COLUMN_ALIASES.items():
            if column.strip().lower() in aliases:
                names[key] = column
    if "size_mm2" not in names or not ({"ampacity_a", "mv_per_a_m"} & set(names)):
        raise CalcError(f"ตาราง {path.name} ต้องมีคอลัมน์ size_mm2 และ ampacity_a หรือ mv_per_a_m")
    rows = []
    for raw in reader:
        try:
            row = {key: float(raw[column]) for key, column in names.items() if (raw.get(column) or "").strip()}
        except ValueError:
            raise CalcError(f"ตาราง {path.name} มีค่าที่ไม่ใช่ตัวเลข: {raw}") from None
        if "size_mm2" in row:
            rows.append(row)
    if not rows:
        raise CalcError(f"ตาราง {path.name} ไม่มีข้อมูล")
    return {"name": path.stem, "path": str(path), "title": meta.get("title", path.stem), "source": meta.get("source", ""),
            "columns": sorted(names), "rows": sorted(rows, key=lambda r: r["size_mm2"])}


# ------------------------------------------------------------------ helpers
def _positive(label: str, value) -> float:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise CalcError(f"{label} ต้องเป็นตัวเลขที่มากกว่า 0")
    return float(value)


def _fraction(label: str, value) -> float:
    value = _positive(label, value)
    if value > 1:
        raise CalcError(f"{label} ต้องไม่เกิน 1")
    return value


def _phase(value) -> int:
    if value not in (1, 3):
        raise CalcError("ระบบไฟต้องเป็น 1 หรือ 3 เฟส")
    return value


def fmt(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:,.0f}"


def result(function: str, title: str, inputs: dict, formula: str, steps: list, values: dict, summary: str,
           verdict: dict | None = None, assumptions=(), references=(), warnings=()) -> dict:
    return {"tool": "pec_calc", "version": VERSION, "function": function, "title": title, "inputs": inputs,
            "formula": formula, "steps": list(steps), "result": values, "summary": summary, "verdict": verdict,
            "assumptions": list(assumptions), "references": list(references), "warnings": list(warnings),
            "verification": verification(function)}


# ------------------------------------------------------------------ calculations
def load_current(kw: float | None = None, kva: float | None = None, v: float = 400, phase: int = 3,
                 pf: float = 0.85, eff: float = 1.0) -> dict:
    """I = P / (√3·V·pf·η) three-phase or P / (V·pf·η) single-phase; from kVA: I = S / (√3·V) or S / V."""
    if (kw is None) == (kva is None):
        raise CalcError("ระบุ --kw หรือ --kva อย่างใดอย่างหนึ่ง")
    v, phase = _positive("แรงดัน", v), _phase(phase)
    k, k_text = (SQRT3, "√3 × ") if phase == 3 else (1.0, "")
    assumptions = ["3 เฟส: V คือแรงดันระหว่างสาย (line-to-line) และโหลดสมดุล" if phase == 3
                   else "1 เฟส: V คือแรงดันระหว่างสายเฟสกับนิวทรัล"]
    if kva is not None:
        kva = _positive("kVA", kva)
        amps = kva * 1000 / (k * v)
        formula = f"I = S / ({k_text}V)"
        steps = [f"I = {fmt(kva)} × 1000 / ({k_text}{fmt(v)}) = {fmt(amps, 1)} A"]
        inputs = {"kva": kva, "v": v, "phase": phase}
    else:
        kw, pf, eff = _positive("kW", kw), _fraction("pf", pf), _fraction("ประสิทธิภาพ (eff)", eff)
        amps = kw * 1000 / (k * v * pf * eff)
        formula = f"I = P / ({k_text}V × pf × η)"
        steps = [f"I = {fmt(kw)} × 1000 / ({k_text}{fmt(v)} × {fmt(pf, 3)} × {fmt(eff, 3)}) = {fmt(amps, 1)} A"]
        inputs = {"kw": kw, "v": v, "phase": phase, "pf": pf, "eff": eff}
        if eff < 1:
            assumptions.append("P คือกำลังที่เพลาของมอเตอร์ จึงหารด้วยประสิทธิภาพ η (ถ้าเป็นกำลังไฟฟ้าขาเข้าให้ใช้ --eff 1)")
    return result("current", "กระแสโหลด", inputs, formula, steps, {"amps": round(amps, 2)}, f"I = {fmt(amps, 1)} A",
                  assumptions=assumptions, references=["กำลังไฟฟ้ากระแสสลับ 1 เฟส และ 3 เฟสสมดุล"])


def resistance(size: float, material: str = "cu", temp: float = 90) -> float:
    """Conductor resistance at `temp` °C from the IEC 60228 value at 20 °C, ohm/km."""
    table = R20.get(material)
    if table is None:
        raise CalcError("วัสดุตัวนำต้องเป็น cu หรือ al")
    if size not in table:
        raise CalcError(f"ไม่มีขนาด {fmt(size)} mm² ในตาราง IEC 60228 ({MATERIAL[material]}) "
                        f"ขนาดที่มี: {', '.join(fmt(s) for s in table)}")
    return table[size] * (1 + ALPHA[material] * (temp - 20))


def voltage_drop(amps: float, length: float, size: float | None = None, material: str = "cu", temp: float = 90,
                 x: float = 0.08, pf: float = 0.85, v: float = 400, phase: int = 3, runs: int = 1,
                 mv_per_am: float | None = None, limit: float = 3.0) -> dict:
    """ΔV = √3·I·L·(R cosφ + X sinφ) three-phase (2·I·L·(...) single-phase), or ΔV = mV/A/m × I × L / 1000."""
    amps, length, v, phase = _positive("กระแส", amps), _positive("ความยาว", length), _positive("แรงดัน", v), _phase(phase)
    limit = _positive("เกณฑ์แรงดันตก", limit)
    if not isinstance(runs, int) or runs < 1:
        raise CalcError("จำนวนชุดสายขนานต้องเป็นจำนวนเต็มตั้งแต่ 1")
    per_run = amps / runs
    inputs = {"amps": amps, "length_m": length, "v": v, "phase": phase, "runs": runs, "limit_percent": limit}
    assumptions, values = [], {}
    if mv_per_am is not None:
        mv_per_am = _positive("mV/A/m", mv_per_am)
        volts = mv_per_am * per_run * length / 1000
        formula = "ΔV = (mV/A/m) × I × L / 1000"
        steps = [f"ΔV = {fmt(mv_per_am, 3)} × {fmt(per_run, 1)} × {fmt(length)} / 1000 = {fmt(volts)} V"]
        inputs["mv_per_a_m"] = mv_per_am
        references = ["ค่า mV/A/m จากตารางที่ระบุ (ตารางของบริษัทหรือผู้ผลิตสาย)"]
        assumptions.append("ค่า mV/A/m ของตารางรวมผลของจำนวนเฟส อุณหภูมิตัวนำ และ power factor ตามเงื่อนไขของตารางแล้ว")
    else:
        if size is None:
            raise CalcError("ระบุขนาดสาย --size (mm²) หรือค่า --mv-per-am")
        pf, x = _fraction("pf", pf), float(x)
        if x < 0:
            raise CalcError("X ต้องไม่ติดลบ")
        r = resistance(size, material, temp)
        sin = math.sqrt(1 - pf * pf)
        k, k_text = (SQRT3, "√3") if phase == 3 else (2.0, "2")
        volts = k * per_run * (length / 1000) * (r * pf + x * sin)
        formula = f"ΔV = {k_text} × I × L × (R cosφ + X sinφ)"
        steps = [f"R({fmt(temp)}°C) = {R20[material][size]:g} × (1 + {ALPHA[material]} × ({fmt(temp)} − 20)) = {r:.4f} Ω/km",
                 f"ΔV = {k_text} × {fmt(per_run, 1)} A × {length / 1000:.3f} km × ({r:.4f} × {fmt(pf, 3)} + {x:g} × {sin:.3f})"
                 f" = {fmt(volts)} V"]
        inputs.update(size_mm2=size, material=material, temp_c=temp, x_ohm_per_km=x, pf=pf)
        values["r_ohm_per_km"] = round(r, 4)
        references = [REF_R20, REF_ALPHA]
        assumptions += [f"X = {x:g} Ω/km (ค่าทั่วไปของสายหลายแกนแรงต่ำ สายแกนเดี่ยววางห่างกันจะมีค่าสูงกว่านี้)",
                        f"อุณหภูมิตัวนำ {fmt(temp)}°C (สาย XLPE/CV ใช้งานเต็มพิกัด 90°C, PVC 70°C)",
                        "ไม่คิด skin effect และ proximity effect ซึ่งมีผลมากขึ้นกับสายขนาดใหญ่"]
    if runs > 1:
        assumptions.append(f"เดินสายขนาน {runs} ชุด กระแสแบ่งเท่ากันชุดละ {fmt(per_run, 1)} A")
    percent = volts / v * 100
    ok = percent <= limit
    values.update(volts=round(volts, 2), percent=round(percent, 2))
    label = f"สาย {fmt(size)} mm² {MATERIAL.get(material, '')}" if size else "ตามค่า mV/A/m"
    return result("vdrop", f"แรงดันตก {label} ยาว {fmt(length)} m", inputs, formula, steps, values,
                  f"ΔV = {fmt(volts)} V = {fmt(percent)}% ของ {fmt(v)} V",
                  verdict={"pass": ok, "text": f"{'ผ่าน' if ok else 'ไม่ผ่าน'}เกณฑ์ไม่เกิน {fmt(limit)}%"},
                  assumptions=assumptions, references=references)


def select_cable(amps: float, length: float, v: float = 400, phase: int = 3, pf: float = 0.85, limit: float = 3.0,
                 factor: float = 1.25, derate: float = 1.0, table: str | None = None, material: str = "cu",
                 temp: float = 90, x: float = 0.08, runs: int = 1) -> dict:
    """Smallest size that carries amps × factor (after derating, from a company ampacity table) and keeps
    the voltage drop within the limit. Without a table only the voltage drop can be checked."""
    amps, length = _positive("กระแส", amps), _positive("ความยาว", length)
    factor, derate = _positive("ตัวคูณกระแสออกแบบ", factor), _positive("ตัวลดพิกัด (derate)", derate)
    design = amps * factor
    data = load_table(table) if table else None
    rows = data["rows"] if data else [{"size_mm2": s} for s in sorted(R20.get(material, R20["cu"]))]
    checked, chosen, warnings = [], None, []
    for row in rows:
        size = row["size_mm2"]
        capacity = row["ampacity_a"] * derate * runs if "ampacity_a" in row else None
        amp_ok = None if capacity is None else capacity >= design
        if "mv_per_a_m" in row:
            drop = voltage_drop(amps, length, mv_per_am=row["mv_per_a_m"], v=v, phase=phase, runs=runs, limit=limit)
        elif size in R20.get(material, {}):
            drop = voltage_drop(amps, length, size=size, material=material, temp=temp, x=x, pf=pf, v=v, phase=phase,
                                runs=runs, limit=limit)
        else:
            continue
        percent = drop["result"]["percent"]
        ok = percent <= limit and amp_ok is not False
        checked.append({"size_mm2": size, "capacity_a": round(capacity, 1) if capacity is not None else None,
                        "ampacity_ok": amp_ok, "vdrop_percent": percent, "vdrop_ok": percent <= limit, "ok": ok})
        if ok and chosen is None:
            chosen = size
    steps = [f"กระแสออกแบบ = {fmt(amps, 1)} × {fmt(factor)} = {fmt(design, 1)} A"]
    for c in checked:
        amp_text = ("ไม่ได้ตรวจกระแส" if c["capacity_a"] is None else
                    f"รับได้ {fmt(c['capacity_a'], 1)} A {'≥' if c['ampacity_ok'] else '<'} {fmt(design, 1)} A")
        steps.append(f"{fmt(c['size_mm2'])} mm²: {amp_text}, แรงดันตก {fmt(c['vdrop_percent'])}% "
                     f"{'≤' if c['vdrop_ok'] else '>'} {fmt(limit)}% → {'ผ่าน' if c['ok'] else 'ไม่ผ่าน'}")
    references = [f"ตาราง {data['title']} ({data['name']}.csv){' · ' + data['source'] if data['source'] else ''}"] if data else []
    references += [REF_R20, REF_ALPHA] if not data or "mv_per_a_m" not in data["columns"] else []
    assumptions = [f"ตัวคูณกระแสออกแบบ {fmt(factor)} (ค่าตั้งต้น 1.25 ตามแนวปฏิบัติทั่วไปสำหรับโหลดต่อเนื่องและมอเตอร์ตัวเดียว "
                   "ให้ปรับตามมาตรฐานและแนวทางของบริษัท)"]
    if derate != 1:
        assumptions.append(f"ตัวลดพิกัดรวม {fmt(derate, 3)} (อุณหภูมิแวดล้อม การจัดกลุ่มสาย ฯลฯ) คูณกับค่าในตาราง")
    if runs > 1:
        assumptions.append(f"เดินสายขนาน {runs} ชุด")
    if not (data and "ampacity_a" in data["columns"]):
        warnings.append("ยังไม่ได้ตรวจกระแสที่สายรับได้ (ampacity) ต้องตรวจกับตารางตามวิธีติดตั้งจริง "
                        "หรือใส่ตารางของบริษัทด้วย --table")
    if chosen is None:
        warnings.append("ไม่มีขนาดในตารางที่ผ่านทุกเกณฑ์ ลองเดินสายขนานด้วย --runs 2 หรือใช้ตารางสายขนาดใหญ่ขึ้น")
    amp_checked = bool(data) and "ampacity_a" in data["columns"]
    if chosen is None:
        summary = "ไม่มีขนาดที่ผ่านเกณฑ์"
    elif amp_checked:
        summary = f"เลือกสาย {fmt(chosen)} mm²{f' × {runs} ชุด' if runs > 1 else ''}"
    else:  # never let a voltage-drop-only answer read like a full cable selection
        summary = (f"ขนาดเล็กสุดที่แรงดันตกผ่าน {fmt(chosen)} mm²{f' × {runs} ชุด' if runs > 1 else ''} "
                   "(ยังไม่ได้ตรวจกระแสที่สายรับได้)")
    inputs = {"amps": amps, "length_m": length, "v": v, "phase": phase, "pf": pf, "limit_percent": limit,
              "factor": factor, "derate": derate, "table": data["name"] if data else None, "material": material,
              "runs": runs}
    return result("cable", f"เลือกขนาดสาย ยาว {fmt(length)} m", inputs,
                  "กระแสที่สายรับได้ × ตัวลดพิกัด ≥ I × ตัวคูณ และ แรงดันตก ≤ เกณฑ์", steps,
                  {"size_mm2": chosen, "design_amps": round(design, 1), "checked": checked}, summary,
                  verdict={"pass": chosen is not None, "text": summary}, assumptions=assumptions,
                  references=references, warnings=warnings)


def transformer_isc(kva: float, v: float = 400, z: float = 6.0, source_mva: float | None = None,
                    c: float = 1.0) -> dict:
    """Three-phase fault current at the transformer's LV terminals: Ik = c·V / (√3·(Zt + Zs))."""
    kva, v, z = _positive("kVA", kva), _positive("แรงดัน", v), _positive("อิมพีแดนซ์ %Z", z)
    c = _positive("ตัวประกอบแรงดัน c", c)
    full_load = kva * 1000 / (SQRT3 * v)
    z_tx = z / 100 * v * v / (kva * 1000)
    z_src = v * v / (_positive("กำลังลัดวงจรด้านแหล่งจ่าย (MVA)", source_mva) * 1e6) if source_mva else 0.0
    isc = c * v / (SQRT3 * (z_tx + z_src))
    steps = [f"I_FL = {fmt(kva)} × 1000 / (√3 × {fmt(v)}) = {fmt(full_load, 1)} A",
             f"Z_tx = {fmt(z)}% × {fmt(v)}² / ({fmt(kva)} × 1000) = {z_tx * 1000:.3f} mΩ"]
    if source_mva:
        steps.append(f"Z_source = {fmt(v)}² / ({fmt(source_mva)} × 10⁶) = {z_src * 1000:.3f} mΩ")
    steps.append(f"Ik = {fmt(c, 3)} × {fmt(v)} / (√3 × {(z_tx + z_src) * 1000:.3f} mΩ) = {fmt(isc / 1000)} kA")
    assumptions = ["รวมอิมพีแดนซ์แบบขนาด ไม่แยก R และ X (ค่าประมาณที่ขั้วแรงต่ำของหม้อแปลง)",
                   "ไม่รวมกระแสจากมอเตอร์ (motor contribution) และอิมพีแดนซ์ของสายหรือบัสบาร์หลังหม้อแปลง",
                   f"c = {fmt(c, 3)} (IEC 60909 ใช้ c_max 1.05 หรือ 1.10 สำหรับระบบแรงต่ำ ตามพิกัดความคลาดเคลื่อนแรงดัน)"]
    if not source_mva:
        assumptions.append("แหล่งจ่ายด้านแรงสูงมีกำลังไม่จำกัด (infinite bus) จึงได้ค่าสูงสุด เหมาะกับการตรวจ Icu")
    return result("isc", f"กระแสลัดวงจร 3 เฟส ที่ขั้วหม้อแปลง {fmt(kva)} kVA",
                  {"kva": kva, "v": v, "z_percent": z, "source_mva": source_mva, "c": c},
                  "Ik = c × V / (√3 × (Z_tx + Z_source))", steps,
                  {"full_load_a": round(full_load, 1), "isc_ka": round(isc / 1000, 2)},
                  f"Ik ≈ {fmt(isc / 1000)} kA (กระแสพิกัด {fmt(full_load, 1)} A)", assumptions=assumptions,
                  references=["IEC 60909-0 วิธีอย่างง่าย: Ik = c·Un / (√3·Zk)"],
                  warnings=["Icu ของ breaker ต้องไม่น้อยกว่ากระแสลัดวงจร ณ จุดติดตั้ง"])


def pf_correction(kw: float, pf_from: float, pf_to: float) -> dict:
    """Qc = P (tan φ1 − tan φ2)."""
    kw, pf_from, pf_to = _positive("kW", kw), _fraction("pf เดิม", pf_from), _fraction("pf เป้าหมาย", pf_to)
    if pf_to <= pf_from:
        raise CalcError("pf เป้าหมายต้องสูงกว่า pf เดิม")
    t1, t2 = math.tan(math.acos(pf_from)), math.tan(math.acos(pf_to))
    kvar = kw * (t1 - t2)
    steps = [f"tan φ1 = tan(acos {fmt(pf_from, 3)}) = {t1:.4f}", f"tan φ2 = tan(acos {fmt(pf_to, 3)}) = {t2:.4f}",
             f"Qc = {fmt(kw)} × ({t1:.4f} − {t2:.4f}) = {fmt(kvar, 1)} kvar"]
    return result("pfc", "ขนาดคาปาซิเตอร์ปรับปรุง power factor", {"kw": kw, "pf_from": pf_from, "pf_to": pf_to},
                  "Qc = P × (tan φ1 − tan φ2)", steps, {"kvar": round(kvar, 1)}, f"Qc = {fmt(kvar, 1)} kvar",
                  assumptions=["โหลดคงที่ที่กำลัง P ที่ระบุ", "ไม่คิดผลของฮาร์มอนิก ถ้ามีอินเวอร์เตอร์/VSD มากควรพิจารณา detuned reactor"],
                  references=["ความสัมพันธ์ Q = P × tan φ"])


def breaker_rating(amps: float, factor: float = 1.25) -> dict:
    """The next common breaker rating at or above amps × factor."""
    amps, factor = _positive("กระแส", amps), _positive("ตัวคูณ", factor)
    needed = amps * factor
    rating = next((r for r in BREAKER_RATINGS if r >= needed - 1e-9), None)
    if rating is None:
        raise CalcError(f"กระแส {fmt(needed, 1)} A เกินพิกัดในตาราง (สูงสุด {BREAKER_RATINGS[-1]} A)")
    return result("breaker", "พิกัด breaker", {"amps": amps, "factor": factor}, "In ≥ I × ตัวคูณ",
                  [f"I × ตัวคูณ = {fmt(amps, 1)} × {fmt(factor)} = {fmt(needed, 1)} A",
                   f"พิกัดมาตรฐานถัดไปที่ไม่น้อยกว่า {fmt(needed, 1)} A คือ {rating} A"],
                  {"needed_a": round(needed, 1), "rating_a": rating}, f"In = {rating} A",
                  assumptions=[f"ตัวคูณ {fmt(factor)} (ค่าตั้งต้น 1.25 สำหรับโหลดต่อเนื่อง ปรับตามมาตรฐานและแนวทางของบริษัท)"],
                  references=["ค่าพิกัดมาตรฐานทั่วไปของ MCB/MCCB (ตรวจกับแคตตาล็อกผู้ผลิตที่ใช้จริง)"],
                  warnings=["พิกัด breaker ต้องไม่เกินกระแสที่สายรับได้หลังลดพิกัด",
                            "Icu ต้องไม่น้อยกว่ากระแสลัดวงจร ณ จุดติดตั้ง",
                            "วงจรมอเตอร์: ให้ใช้เกณฑ์ของมาตรฐานและผู้ผลิต ซึ่งคิดกระแสเริ่มเดินด้วย"])


def list_tables() -> dict:
    folder = tables_dir()
    found = []
    for path in sorted(folder.glob("*.csv")) if folder and folder.is_dir() else []:
        try:
            data = load_table(str(path))
            found.append({"name": data["name"], "title": data["title"], "source": data["source"],
                          "columns": data["columns"], "rows": len(data["rows"])})
        except CalcError as exc:
            found.append({"name": path.stem, "error": str(exc)})
    return {"tool": "pec_calc", "version": VERSION, "folder": str(folder) if folder else None, "tables": found}


# ------------------------------------------------------------------ output
VERIFY_TEXT = {"verified": "ตรวจแล้วโดย {by} เมื่อ {date}", "outdated": "เคยตรวจในเวอร์ชัน {version} ต้องตรวจใหม่สำหรับเวอร์ชันนี้",
               "unverified": "ยังไม่ผ่านการตรวจโดยทีมวิศวกรรม (ดู docs/calc-tools.md)"}


def render(res: dict) -> str:
    if "tables" in res:
        if not res["tables"]:
            return (f"ยังไม่มีตารางของบริษัทใน {res['folder'] or 'knowledge/engineering/tables/'}\n"
                    "รูปแบบไฟล์: CSV มีคอลัมน์ size_mm2 และ ampacity_a และ/หรือ mv_per_a_m")
        lines = [f"ตารางใน {res['folder']}:"]
        for t in res["tables"]:
            lines.append(f"- {t['name']}: " + (t["error"] if "error" in t else
                         f"{t['title']} · คอลัมน์ {', '.join(t['columns'])} · {t['rows']} แถว"
                         + (f" · ที่มา {t['source']}" if t["source"] else "")))
        return "\n".join(lines)
    lines = [f"■ {res['title']}  (pec_calc {res['version']} · {res['function']})", f"สูตร: {res['formula']}"]
    lines += [f"  {s}" for s in res["steps"]]
    lines.append(f"ผลลัพธ์: {res['summary']}")
    if res["verdict"] and res["verdict"]["text"] != res["summary"]:
        lines.append(f"สรุป: {res['verdict']['text']}")
    for heading, key in (("สมมติฐาน", "assumptions"), ("อ้างอิง", "references"), ("ข้อควรระวัง", "warnings")):
        if res[key]:
            lines.append(f"{heading}:")
            lines += [f"  - {item}" for item in res[key]]
    check = res["verification"]
    lines.append("สถานะการตรวจสอบ: " + VERIFY_TEXT[check["status"]].format(**{k: check.get(k) or "?" for k in ("by", "date", "version")}))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="แสดงผลเป็น JSON")
    parser = argparse.ArgumentParser(prog="python3 -m pec_calc", description="การคำนวณไฟฟ้าแรงต่ำมาตรฐาน พร้อมสูตร สมมติฐาน และอ้างอิง")
    parser.add_argument("--version", action="version", version=f"pec_calc {VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("current", parents=[shared], help="กระแสโหลดจาก kW หรือ kVA")
    p.add_argument("--kw", type=float)
    p.add_argument("--kva", type=float)
    p.add_argument("--v", type=float, default=400, help="แรงดัน (V) 3 เฟสคือ line-to-line ค่าตั้งต้น 400")
    p.add_argument("--phase", type=int, default=3, choices=(1, 3))
    p.add_argument("--pf", type=float, default=0.85)
    p.add_argument("--eff", type=float, default=1.0, help="ประสิทธิภาพ เช่น มอเตอร์ 0.94")

    def cable_args(q, with_size=True):
        q.add_argument("--amps", type=float, required=True, help="กระแสโหลด (A)")
        q.add_argument("--length", type=float, required=True, help="ความยาวสายทางเดียว (m)")
        q.add_argument("--v", type=float, default=400)
        q.add_argument("--phase", type=int, default=3, choices=(1, 3))
        q.add_argument("--pf", type=float, default=0.85)
        q.add_argument("--material", choices=("cu", "al"), default="cu")
        q.add_argument("--temp", type=float, default=90, help="อุณหภูมิตัวนำ (°C) ค่าตั้งต้น 90 สำหรับ XLPE")
        q.add_argument("--x", type=float, default=0.08, help="รีแอกแตนซ์ (Ω/km) ค่าตั้งต้น 0.08")
        q.add_argument("--runs", type=int, default=1, help="จำนวนชุดสายขนาน")
        q.add_argument("--limit", type=float, default=3.0, help="เกณฑ์แรงดันตก (%%) ค่าตั้งต้น 3")
        if with_size:
            q.add_argument("--size", type=float, help="ขนาดสาย (mm²)")
            q.add_argument("--mv-per-am", type=float, dest="mv_per_am", help="ค่า mV/A/m จากตาราง (แทนการคิดจาก R และ X)")

    cable_args(sub.add_parser("vdrop", parents=[shared], help="แรงดันตกในสาย"))
    p = sub.add_parser("cable", parents=[shared], help="เลือกขนาดสายจากกระแสและแรงดันตก")
    cable_args(p, with_size=False)
    p.add_argument("--factor", type=float, default=1.25, help="ตัวคูณกระแสออกแบบ ค่าตั้งต้น 1.25")
    p.add_argument("--derate", type=float, default=1.0, help="ตัวลดพิกัดรวม เช่น 0.87")
    p.add_argument("--table", help="ชื่อตารางใน knowledge/engineering/tables/ (ดูด้วยคำสั่ง tables)")

    p = sub.add_parser("isc", parents=[shared], help="กระแสลัดวงจรที่ขั้วหม้อแปลง")
    p.add_argument("--kva", type=float, required=True)
    p.add_argument("--v", type=float, default=400)
    p.add_argument("--z", type=float, default=6.0, help="อิมพีแดนซ์หม้อแปลง (%%)")
    p.add_argument("--source-mva", type=float, dest="source_mva", help="กำลังลัดวงจรด้านแรงสูง (MVA)")
    p.add_argument("--c", type=float, default=1.0, help="ตัวประกอบแรงดันตาม IEC 60909 เช่น 1.05")

    p = sub.add_parser("pfc", parents=[shared], help="ขนาดคาปาซิเตอร์ปรับปรุง power factor")
    p.add_argument("--kw", type=float, required=True)
    p.add_argument("--pf-from", type=float, required=True, dest="pf_from")
    p.add_argument("--pf-to", type=float, required=True, dest="pf_to")

    p = sub.add_parser("breaker", parents=[shared], help="พิกัด breaker มาตรฐานถัดไป")
    p.add_argument("--amps", type=float, required=True)
    p.add_argument("--factor", type=float, default=1.25)

    sub.add_parser("tables", parents=[shared], help="ตารางของบริษัทที่ใช้ได้")
    return parser


def run(args: argparse.Namespace) -> dict:
    if args.cmd == "current":
        return load_current(kw=args.kw, kva=args.kva, v=args.v, phase=args.phase, pf=args.pf, eff=args.eff)
    if args.cmd == "vdrop":
        return voltage_drop(args.amps, args.length, size=args.size, material=args.material, temp=args.temp, x=args.x,
                            pf=args.pf, v=args.v, phase=args.phase, runs=args.runs, mv_per_am=args.mv_per_am,
                            limit=args.limit)
    if args.cmd == "cable":
        return select_cable(args.amps, args.length, v=args.v, phase=args.phase, pf=args.pf, limit=args.limit,
                            factor=args.factor, derate=args.derate, table=args.table, material=args.material,
                            temp=args.temp, x=args.x, runs=args.runs)
    if args.cmd == "isc":
        return transformer_isc(args.kva, v=args.v, z=args.z, source_mva=args.source_mva, c=args.c)
    if args.cmd == "pfc":
        return pf_correction(args.kw, args.pf_from, args.pf_to)
    if args.cmd == "breaker":
        return breaker_rating(args.amps, factor=args.factor)
    return list_tables()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        res = run(args)
    except CalcError as exc:
        print(f"ข้อผิดพลาด: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
