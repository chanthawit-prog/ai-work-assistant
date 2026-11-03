"""Send an approved result by email, from the dashboard, with the Word file attached.

Only you send: a job must be approved first, the form shows what goes out, and nothing is sent without your
confirmation. The SMTP settings (MAIL_*) stay in .env; Claude and its Python never see them.
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .config import Settings

log = logging.getLogger("assistant.mail")
ADDRESS = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$")
MAX_RECIPIENTS = 20
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
TYPES = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".pdf": "application/pdf", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".csv": "text/csv", ".md": "text/markdown", ".txt": "text/plain", ".json": "application/json",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def split_addresses(text: str | list[str] | None) -> list[str]:
    """"a@x.com, b@y.com" or a list -> the addresses, in order, without repeats."""
    if not text:
        return []
    raw = text if isinstance(text, list) else re.split(r"[,\n;]+", text)
    out = []
    for part in raw:
        part = str(part).strip().strip("<>")
        if part and part.lower() not in {o.lower() for o in out}:
            out.append(part)
    return out


def check_addresses(addresses: list[str], label: str) -> None:
    bad = [a for a in addresses if not ADDRESS.match(a)]
    if bad:
        raise ValueError(f"ที่อยู่อีเมล{label}ไม่ถูกต้อง: {', '.join(bad[:3])}")
    if len(addresses) > MAX_RECIPIENTS:
        raise ValueError(f"ส่งได้ครั้งละไม่เกิน {MAX_RECIPIENTS} ที่อยู่")


def find_emails(text: str) -> list[str]:
    """Addresses mentioned in a customer profile, for the To field."""
    found = []
    for match in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text or ""):
        if match.lower() not in {f.lower() for f in found} and not match.lower().endswith(".example"):
            found.append(match)
    return found[:5]


SUBJECT_LINE = re.compile(r"^\s*(หัวข้อ|เรื่อง|subject)\s*:", re.IGNORECASE)


def quote_blocks(markdown: str) -> list[tuple[str, list[str]]]:
    """Every "> " block of a result with the heading above it: (heading text, lines without the marks)."""
    blocks: list[tuple[str, list[str]]] = []
    heading, block = "", []
    for line in (markdown or "").replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s*>", line):
            block.append(re.sub(r"^\s*>\s?", "", line))
            continue
        if block:
            blocks.append((heading, block))
            block = []
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
    if block:
        blocks.append((heading, block))
    return blocks


def drafted_message(markdown: str) -> dict:
    """The message to send from a result (Claude is asked to put what you will copy and send in "> " blocks, one
    block per message): the block whose first line is "หัวข้อ:"/"เรื่อง:"/"Subject:" comes first, then one under
    an email heading, then the longest; a one-line note in a quote loses to a real message."""
    blocks = quote_blocks(markdown)
    if not blocks:
        return {"subject": "", "body": ""}

    def rank(item):
        heading, lines = item
        has_subject = bool(lines and SUBJECT_LINE.match(lines[0]))
        under_email = bool(re.search(r"อีเมล|e-?mail", heading, re.IGNORECASE))
        return (has_subject, under_email, sum(len(l) for l in lines))

    heading, block = max(blocks, key=rank)  # the first of equals wins
    subject = ""
    if SUBJECT_LINE.match(block[0]):
        subject = block[0].split(":", 1)[1].strip()
        block = block[1:]
    body = "\n".join(block).strip("\n")
    body = re.sub(r"\*\*([^*]+)\*\*", r"\1", body)           # bold marks
    body = re.sub(r"(?m)^\s*[-*]\s+", "• ", body)            # bullets
    body = re.sub(r"\n{3,}", "\n\n", body)
    return {"subject": subject, "body": body.strip()}


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.mail_host and self.settings.mail_from)

    def status(self) -> dict:
        s = self.settings
        return {"configured": self.configured, "host": s.mail_host, "port": s.mail_port, "security": s.mail_security,
                "sender": s.mail_from, "user": bool(s.mail_user), "bcc": s.mail_bcc}

    def build(self, to: list[str], cc: list[str], subject: str, body: str, attachments: list[Path]) -> EmailMessage:
        s = self.settings
        msg = EmailMessage()
        msg["From"] = formataddr((s.mail_from_name, s.mail_from)) if s.mail_from_name else s.mail_from
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=s.mail_from.rpartition("@")[2] or None)
        msg.set_content(body, charset="utf-8")
        for path in attachments:
            maintype, _, subtype = TYPES.get(path.suffix.lower(), "application/octet-stream").partition("/")
            msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        return msg

    def send(self, to: list[str], cc: list[str], subject: str, body: str, attachments: list[Path]) -> dict:
        """Deliver through the SMTP server in .env. Raises ValueError with a Thai message when it cannot."""
        if not self.configured:
            raise ValueError("ยังไม่ได้ตั้งค่าอีเมลในไฟล์ .env (MAIL_SMTP_HOST, MAIL_FROM)")
        check_addresses(to, "ผู้รับ")
        check_addresses(cc, "สำเนา")
        if not to:
            raise ValueError("ใส่ที่อยู่ผู้รับก่อน")
        if not subject.strip():
            raise ValueError("ใส่หัวข้ออีเมลก่อน")
        for path in attachments:
            if not path.is_file():
                raise ValueError(f"ไม่พบไฟล์แนบ {path.name}")
        if sum(p.stat().st_size for p in attachments) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"ไฟล์แนบรวมเกิน {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        msg = self.build(to, cc, subject, body, attachments)
        s = self.settings
        recipients = to + cc + ([s.mail_bcc] if s.mail_bcc else [])
        try:
            if s.mail_security == "ssl":
                server = smtplib.SMTP_SSL(s.mail_host, s.mail_port, timeout=30, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(s.mail_host, s.mail_port, timeout=30)
            with server:
                server.ehlo()
                if s.mail_security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if s.mail_user:
                    server.login(s.mail_user, s.mail_password)
                refused = server.send_message(msg, from_addr=s.mail_from, to_addrs=recipients)
        except smtplib.SMTPAuthenticationError:
            raise ValueError("เซิร์ฟเวอร์อีเมลปฏิเสธชื่อผู้ใช้หรือรหัสผ่าน (Gmail/Microsoft 365 ต้องใช้ app password)") from None
        except smtplib.SMTPRecipientsRefused as exc:  # every recipient refused (one refused among others: below)
            raise ValueError("เซิร์ฟเวอร์ปฏิเสธผู้รับ: " + ", ".join(exc.recipients)) from None
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("mail not sent: %r", exc)
            raise ValueError(f"ส่งอีเมลไม่สำเร็จ: {exc}") from None
        if refused:
            raise ValueError("เซิร์ฟเวอร์ปฏิเสธผู้รับ: " + ", ".join(refused))
        return {"to": to, "cc": cc, "subject": subject, "attachments": [p.name for p in attachments],
                "message_id": msg["Message-ID"]}
