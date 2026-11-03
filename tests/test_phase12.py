"""Phase 12: วันนี้ on the home page, and the morning summary."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.schedules import ScheduleStore
from app.state import RuntimeState
from app.store import NEEDS_REVIEW
from app.today import DigestScheduler, digest_text, summary

BKK = timezone(timedelta(hours=7))
MONDAY = datetime(2026, 11, 9, 8, 0, tzinfo=BKK)  # a Monday, later than any schedule's active_from


def setup_day(parts, settings):
    store, tasks, _state, _runner = parts
    for task, due in (("โทรตาม PO", "2026-11-06"), ("ส่งแบบ", "2026-11-09"), ("เช็คราคา", "2026-11-12"),
                      ("ไกลหน่อย", "2026-11-30"), ("ไม่มีกำหนด", None)):
        tasks.add({"task": task, "due": due, "owner": "สมชาย" if task == "โทรตาม PO" else None,
                   "customer": "ACME" if task == "ส่งแบบ" else None})
    done = tasks.add({"task": "เสร็จแล้ว", "due": "2026-11-01"})
    tasks.set_status(done["id"], "done")
    schedules = ScheduleStore(settings.data_dir / "config" / "schedules.json")
    schedules.create({"name": "สรุปงานประจำสัปดาห์", "type": "general", "instructions": "สรุปงาน",
                      "days": ["mon"], "time": "16:00", "include": [], "enabled": True})
    schedules.create({"name": "ปิดไว้", "type": "general", "instructions": "x", "days": ["mon"], "time": "09:00",
                      "include": [], "enabled": False})
    job = store.create("general", "ร่างอีเมล ACME", "x")
    store.update(job["id"], status=NEEDS_REVIEW)
    return schedules


def test_today_lists_what_is_late_due_and_scheduled(parts, settings):
    store, tasks, _state, _runner = parts
    schedules = setup_day(parts, settings)
    s = summary(store, tasks, schedules, settings, MONDAY)
    assert s["label"] == "วันจันทร์ 9 พ.ย."
    assert s["overdue_count"] == 1 and s["overdue"][0]["task"] == "โทรตาม PO" and s["overdue"][0]["days"] == -3
    assert s["due_today_count"] == 1 and s["due_today"][0]["customer"] == "ACME"
    assert s["week_count"] == 1 and s["week"][0]["task"] == "เช็คราคา"  # the 30th is further than 7 days
    assert s["waiting_count"] == 1 and s["waiting"][0]["title"] == "ร่างอีเมล ACME"
    assert [(r["time"], r["name"], r["done"]) for r in s["runs"]] == [
        ("08:00", "ติดตามงานอัตโนมัติ (ร่างข้อความทวงงาน)", False), ("16:00", "สรุปงานประจำสัปดาห์", False)]
    later = summary(store, tasks, schedules, settings, MONDAY, followup_last="2026-11-09")
    assert later["runs"][0]["done"] is True
    weekly = next(x for x in schedules.all() if x["name"] == "สรุปงานประจำสัปดาห์")
    schedules.mark_run(weekly["id"], "2026-11-09", None, skipped="รอบก่อนยังไม่เสร็จ จึงข้ามรอบนี้")
    skipped = summary(store, tasks, schedules, settings, MONDAY)["runs"][1]
    assert skipped["skipped"] is True and skipped["done"] is False
    sunday = summary(store, tasks, schedules, settings, MONDAY - timedelta(days=1))
    assert sunday["runs"] == []  # neither the weekly job nor the weekday follow-up runs on Sunday


def test_the_morning_message(parts, settings):
    store, tasks, _state, _runner = parts
    schedules = setup_day(parts, settings)
    text = digest_text(summary(store, tasks, schedules, settings, MONDAY), replace(settings, dashboard_url="http://nas:8080"))
    lines = text.splitlines()
    assert lines[0] == "สรุปงานวันจันทร์ 9 พ.ย."
    assert "เลยกำหนด 1 รายการ" in lines and any("โทรตาม PO (สมชาย)" in l and "เลยมา 3 วัน" in l for l in lines)
    assert "ครบกำหนดวันนี้ 1 รายการ" in lines and any("ส่งแบบ" in l and "ลูกค้า ACME" in l for l in lines)
    assert "รอคุณตรวจ 1 งาน" in lines and "• ร่างอีเมล ACME" in lines
    assert any(l.startswith("งานประจำวันนี้: 08:00") and "16:00 สรุปงานประจำสัปดาห์" in l for l in lines)
    assert lines[-1] == "เปิดดู: http://nas:8080/#/"
    empty = summary(store.__class__(replace(settings, data_dir=settings.data_dir / "empty")), tasks.__class__(
        settings.data_dir / "empty" / "tasks.json"), ScheduleStore(settings.data_dir / "empty" / "s.json"), settings, MONDAY)
    assert digest_text(empty, settings) is None  # nothing to report: no message


class FakeNotifier:
    channels = ["webhook"]

    def __init__(self):
        self.sent = []

    async def send(self, text, payload=None):
        self.sent.append((text, payload))
        return {"webhook": "ok"}


def test_sent_once_each_working_morning(parts, settings):
    store, tasks, _state, _runner = parts
    schedules = setup_day(parts, settings)
    notifier = FakeNotifier()
    state = RuntimeState(settings.state_dir / "digest.json")
    digest = DigestScheduler(store, tasks, schedules, state, replace(settings, digest_time="07:30"), notifier)
    assert asyncio.run(digest.tick(MONDAY.replace(hour=7, minute=0))) is None  # not yet
    late = DigestScheduler(store, tasks, schedules, RuntimeState(settings.state_dir / "late.json"),
                           replace(settings, digest_time="07:30"), notifier)
    assert asyncio.run(late.tick(MONDAY.replace(hour=17, minute=30))) is None  # NAS restarted in the evening
    assert notifier.sent == []
    assert asyncio.run(digest.tick(MONDAY.replace(hour=7, minute=31))).startswith("สรุปงานวันจันทร์")
    assert asyncio.run(digest.tick(MONDAY.replace(hour=9))) is None  # once a day
    assert len(notifier.sent) == 1 and notifier.sent[0][1] == {"event": "digest"}
    assert asyncio.run(digest.tick(MONDAY + timedelta(days=5, hours=1))) is None  # Saturday: not a working day
    assert asyncio.run(digest.tick(MONDAY + timedelta(days=1))) is not None  # Tuesday morning again
    off = DigestScheduler(store, tasks, schedules, RuntimeState(settings.state_dir / "off.json"), settings, notifier)
    assert asyncio.run(off.tick(MONDAY)) is None  # DIGEST_TIME not set


def test_today_api_and_send_now(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        app.state.tasks.add({"task": "ส่งแบบ", "due": datetime.now().astimezone().date().isoformat()})
        today = client.get("/api/today").json()
        assert today["due_today_count"] == 1 and today["label"].startswith("วัน")
        no_channel = client.post("/api/notify/digest")
        assert no_channel.status_code == 409 and "ช่องทางแจ้งเตือน" in no_channel.json()["detail"]
        assert client.get("/api/system").json()["notify"]["digest_time"] == ""


def test_a_bad_digest_time_never_fires_and_is_reported(parts, settings):
    from app import readiness

    store, tasks, _state, _runner = parts
    schedules = setup_day(parts, settings)
    notifier = FakeNotifier()
    bad = DigestScheduler(store, tasks, schedules, RuntimeState(settings.state_dir / "bad.json"),
                          replace(settings, digest_time="25:00"), notifier)
    assert asyncio.run(bad.tick(MONDAY)) is None and notifier.sent == []  # no ValueError every 30 s in the loop
    items = {i["key"]: i for i in readiness.run_checks(replace(settings, digest_time="25:00", followup_time="8"), None, [], 0)["items"]}
    assert items["digest_time"]["status"] == "fail" and items["followup_time"]["status"] == "fail"
    assert "digest_time" not in {i["key"] for i in readiness.run_checks(replace(settings, digest_time="07:30"), None, [], 0)["items"]}
