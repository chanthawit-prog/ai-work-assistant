"""The sample knowledge pack (examples/knowledge) is what a new install starts from: it must index cleanly, show its
customer, keep its .example addresses out of the email form, and be seen by pec_calc."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import kb_search  # noqa: E402

from app import customers  # noqa: E402
from app.config import Settings  # noqa: E402
from app.mail import find_emails  # noqa: E402
from app.main import create_app  # noqa: E402

EXAMPLES = ROOT / "examples" / "knowledge"


def test_the_sample_pack_has_the_folders_the_guide_names():
    for rel in ("README.md", "customers/ตัวอย่าง-ลูกค้า/profile.md", "pricing/price-list-ตัวอย่าง.csv",
                "engineering/แนวทางเลือกขนาดสาย-ตัวอย่าง.md", "engineering/tables/cv-4c-tray-example.csv",
                "engineering/calc-verified.example.json", "templates/README.md", "templates/proposal-ตัวอย่าง.md"):
        assert (EXAMPLES / rel).is_file(), rel
    for path in EXAMPLES.rglob("*"):
        assert not path.name.startswith((".", "@", "#")), path  # names the indexer and inbox would skip


def test_the_sample_pack_indexes_cleanly_and_is_searchable(tmp_path):
    index = tmp_path / "i.sqlite"
    stats = kb_search.refresh(index, {"knowledge": EXAMPLES})
    status = kb_search.status(index)
    assert stats["errors"] == 0 and status["files"] >= 8 and status["notes"] == []
    assert kb_search.search(index, "NSX160F")["results"][0]["rel"] == "pricing/price-list-ตัวอย่าง.csv"
    assert kb_search.search(index, "ยืนราคา")["results"]  # the proposal template's conditions


def test_sample_customer_shows_up_and_its_addresses_stay_out_of_the_email_form(tmp_path):
    assert list(customers.folders(EXAMPLES)) == ["ตัวอย่าง-ลูกค้า"]
    profile = (EXAMPLES / "customers" / "ตัวอย่าง-ลูกค้า" / "profile.md").read_text("utf-8")
    assert "@customer.example" in profile and find_emails(profile) == []
    settings = Settings(data_dir=tmp_path / "data", knowledge_dir=EXAMPLES, claude_bin=str(ROOT / "scripts" / "fake-claude"),
                        followup_time="08:00", mail_host="127.0.0.1", mail_from="sales@pec.co.th")
    settings.ensure_dirs()
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        assert [c["name"] for c in client.get("/api/customers").json()] == ["ตัวอย่าง-ลูกค้า"]
        job = client.post("/api/jobs", data={"type": "proposal-draft", "customer": "ตัวอย่าง-ลูกค้า",
                                             "instructions": "feeder 125 A"}).json()
        asyncio.run(app.state.runner.run_job(job["id"]))
        client.post(f"/api/jobs/{job['id']}/approve", json={"note": ""})
        assert client.get(f"/api/jobs/{job['id']}/email").json()["to"] == []
    assert not list(EXAMPLES.rglob("*.converted.json")) and not list((EXAMPLES / "templates").glob("*.txt"))  # nothing written


def test_pec_calc_lists_the_example_table():
    out = subprocess.run([sys.executable, "-m", "pec_calc", "tables"], cwd=str(ROOT / "tools"), capture_output=True,
                         text=True, timeout=60, env={**os.environ, "PEC_CALC_KNOWLEDGE": str(EXAMPLES)})
    assert out.returncode == 0 and "cv-4c-tray-example" in out.stdout
