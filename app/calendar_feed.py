"""The follow-up list as a calendar feed (iCalendar, RFC 5545), so due dates show up in the calendar on your phone
or in Outlook next to your meetings.

Each open item with a due date becomes an all-day event on that date, with a reminder at 09:00 the day before
(where the calendar app keeps reminders of subscribed calendars). Ticking an item off removes it from the feed.
Calendar apps fetch the feed without the dashboard password, so it lives at a secret, revocable address:
/calendar/<token>.ics (see app/main.py).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

PRODID = "-//PEC//AI Work Assistant//TH"
PRIORITY = {"high": ("1", "สูง"), "medium": ("5", "กลาง"), "low": ("9", "ต่ำ")}  # iCalendar: 1 highest, 9 lowest


def escape(text: str) -> str:
    """TEXT values: backslash, semicolon, comma and line breaks are escaped (RFC 5545 3.3.11)."""
    text = str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return re.sub(r"\r\n|\r|\n", r"\\n", text)


def fold(line: str) -> str:
    """Lines longer than 75 octets go on as continuation lines (CRLF + space), never splitting a UTF-8 character:
    one Thai character takes three octets."""
    parts, current, size = [], [], 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not parts else 74):  # continuation lines lose one octet to the leading space
            parts.append("".join(current))
            current, size = [], 0
        current.append(ch)
        size += n
    parts.append("".join(current))
    return "\r\n ".join(parts)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _uid(item: dict) -> str:
    created = re.sub(r"\D", "", item.get("created_at") or "")[:14]
    return f"{item['id']}-{created or '0'}@ai-work-assistant"


def tasks_ics(items: list[dict], base_url: str = "", now: datetime | None = None) -> str:
    """The calendar for the open items that have a due date, as text with CRLF line endings."""
    now = now or datetime.now().astimezone()
    base = base_url.rstrip("/")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:ติดตามงาน · ผู้ช่วยงาน",
             "X-WR-CALDESC:กำหนดส่งของรายการติดตามงานที่ยังเปิดอยู่ จาก AI Work Assistant",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for item in items:
        if item.get("status") == "done" or not item.get("due"):
            continue
        try:
            due = date.fromisoformat(item["due"])
        except (TypeError, ValueError):
            continue
        end = date.fromordinal(due.toordinal() + 1)
        level, label = PRIORITY.get(item.get("priority"), PRIORITY["medium"])
        title = item["task"] + (f" ({item['owner']})" if item.get("owner") else "")
        details = [f"รหัส: {item['id']}", f"ผู้รับผิดชอบ: {item.get('owner') or 'ยังไม่ระบุ'}", f"ความสำคัญ: {label}"]
        if item.get("customer"):
            details.append(f"ลูกค้า: {item['customer']}")
        if item.get("source_title"):
            details.append(f"ที่มา: {item['source_title']}")
        if item.get("notes"):
            details.append(f"หมายเหตุ: {item['notes']}")
        link = ""
        if base:
            link = f"{base}/#/job/{item['source_job']}" if item.get("source_job") else f"{base}/#/tasks"
            details += ["", f"เปิดในผู้ช่วยงาน: {base}/#/tasks"]
        changed = item.get("edited_at") or item.get("created_at")
        event = ["BEGIN:VEVENT", f"UID:{_uid(item)}", f"DTSTAMP:{_stamp(now)}",
                 f"DTSTART;VALUE=DATE:{due:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}",
                 f"SUMMARY:{escape(title)}", f"DESCRIPTION:{escape(chr(10).join(details))}",
                 f"PRIORITY:{level}", "TRANSP:TRANSPARENT", "CATEGORIES:ติดตามงาน"]
        if link:
            event.append(f"URL:{link}")
        if changed:
            try:
                event.append(f"LAST-MODIFIED:{_stamp(datetime.fromisoformat(changed))}")
            except ValueError:
                pass
        event += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{escape('พรุ่งนี้ครบกำหนด: ' + item['task'])}",
                  "TRIGGER:-PT15H", "END:VALARM", "END:VEVENT"]
        lines += event
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)
