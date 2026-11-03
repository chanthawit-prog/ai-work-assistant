"""Phase 13: defects found in the full re-check (backend side; the browser checks live in the trial notes)."""
from fastapi.testclient import TestClient

from app.main import create_app


def test_titles_and_customer_names_have_a_length_cap(settings):
    """A 5,000-character customer name (no spaces) made the customers list 40,000 px wide and would become a
    folder name; names are capped like the follow-up and schedule forms already cap them."""
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        job = client.post("/api/jobs", data={"type": "general", "instructions": "x", "title": "ย" * 400,
                                             "customer": "y" * 5000}).json()
        assert len(job["title"]) == 150 and len(job["params"]["customer"]) == 120
        names = [c["name"] for c in client.get("/api/customers").json()]
        assert all(len(n) <= 120 for n in names)
        app.state.store.update(job["id"], status="needs_review")
        app.state.store.add_file(job["id"], "output", "result.md", b"x")
        new = client.post(f"/api/jobs/{job['id']}/handoff", json={"type": "general", "instructions": "x",
                                                                   "customer": "z" * 500}).json()
        assert len(new["params"]["customer"]) == 120


def test_an_event_in_an_unexpected_shape_does_not_fail_the_run(parts):
    """`summarize` raised on `message` or `rate_limit_info` that were not dicts, and the exception ended the
    whole run (with Claude left running). Such an event is now logged as one warning and the rest goes on."""
    import asyncio
    import json

    from app.events import RunMeta

    store, _tasks, state, runner = parts
    job = store.create("general", "แปลก", "x")
    store.update(job["id"], runs=[{"n": 1, "kind": "initial"}])
    lines = [
        {"type": "assistant", "message": "not a dict"},
        {"type": "rate_limit_event", "rate_limit_info": "x"},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": "ยังทำต่อ"}]}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "utilization": 0.1}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "duration_ms": 5, "result": "ok",
         "usage": {}, "session_id": "s-1"},
    ]

    class Proc:
        def __init__(self):
            self.stdout = asyncio.StreamReader()  # needs the running loop
            for event in lines:
                self.stdout.feed_data(json.dumps(event).encode() + b"\n")
            self.stdout.feed_eof()

    meta = RunMeta()

    async def read():
        await runner._read_stdout(job["id"], 1, Proc(), meta, str(store.job_dir(job["id"])))

    asyncio.run(read())
    steps, _ = store.read_steps(job["id"])
    kinds = [s["kind"] for s in steps]
    assert kinds.count("warn") == 2 and "text" in kinds  # two odd events, the good ones still shown
    assert meta.succeeded and meta.session_id == "s-1" and state.get("quota")["status"] == "allowed"


def test_huge_numbers_in_date_columns_do_not_crash_the_workbook_reader():
    """An id or a phone number in a date-formatted column (1700000000 days from 1900) raised OverflowError,
    which neither the job's conversion nor the knowledge indexer caught."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import xlsx_text

    assert xlsx_text._number("1700000000", "date", False) == "1700000000"
    assert xlsx_text._number("1e999", None, False) == "1e999"
    assert xlsx_text._number("1e9", "time", False) == "1e9"
    assert xlsx_text._number("45000.5", "date", False) == "2023-03-15 12:00"  # ordinary dates still read
    assert xlsx_text._number("1e15", None, False) == "1e15"  # beyond what fits as an int: shown as stored


def test_a_message_waits_for_an_approval_in_progress(parts):
    """Approving writes the library copy, the Word file and the follow-up items, then re-checks the status under
    the store lock; a message sent meanwhile used to slip in between. Messages now wait for the review lock."""
    import threading
    import time

    from app.review import review_lock
    from app.store import NEEDS_REVIEW

    store, _tasks, _state, runner = parts
    job = store.create("general", "แข่งกัน", "x")
    store.update(job["id"], status=NEEDS_REVIEW)
    done = []
    with review_lock:  # an approval is in progress
        t = threading.Thread(target=lambda: done.append(runner.send_message(job["id"], "แก้หน่อย")["status"]))
        t.start()
        time.sleep(0.2)
        assert done == []  # the message waits
    t.join(5)
    assert done == ["queued"]


def test_a_job_cut_by_a_restart_is_marked_and_can_be_retried(parts):
    """The container stopped while Claude was working (checked live too: kill -9 of the server, restart)."""
    from app.store import FAILED, NEEDS_REVIEW, QUEUED, RUNNING, now_iso

    store, _tasks, _state, runner = parts
    cut = store.create("general", "ถูกตัด", "x")
    store.update(cut["id"], status=RUNNING, runs=[{"n": 1, "kind": "initial", "started_at": now_iso(), "ended_at": None}])
    fine = store.create("general", "ไม่เกี่ยว", "x")
    store.update(fine["id"], status=NEEDS_REVIEW)
    runner.recover()
    cut = store.get(cut["id"])
    assert cut["status"] == FAILED and "รีสตาร์ท" in cut["error"] and cut["runs"][-1]["ended_at"]
    assert store.get(fine["id"])["status"] == NEEDS_REVIEW
    assert runner.retry(cut["id"])["status"] == QUEUED
