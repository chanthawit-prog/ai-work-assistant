import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.runner import Runner  # noqa: E402
from app.state import RuntimeState  # noqa: E402
from app.store import JobStore  # noqa: E402
from app.tasks import TaskStore  # noqa: E402

FAKE_CLAUDE = str(ROOT / "scripts" / "fake-claude")


@pytest.fixture(autouse=True)
def fast_fake(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0")
    for name in ("FAKE_CLAUDE_FAIL", "FAKE_CLAUDE_RATE_LIMIT", "FAKE_CLAUDE_SKIP_OUTPUTS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path):
    knowledge = tmp_path / "knowledge"
    (knowledge / "customers" / "ACME").mkdir(parents=True)
    (knowledge / "customers" / "ACME" / "profile.md").write_text("ACME โรงงานผลิตชิ้นส่วน", encoding="utf-8")
    s = Settings(data_dir=tmp_path / "data", knowledge_dir=knowledge, claude_bin=FAKE_CLAUDE, followup_time="08:00")
    s.ensure_dirs()
    return s


@pytest.fixture
def parts(settings):
    store = JobStore(settings)
    tasks = TaskStore(settings.tasks_file)
    state = RuntimeState(settings.state_dir / "runtime.json")
    runner = Runner(store, tasks, state, settings)
    return store, tasks, state, runner
