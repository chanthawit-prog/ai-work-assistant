"""Phase 14: sending an approved result by email, with the Word file attached."""
import asyncio
import base64
import socket
import threading
from dataclasses import replace
from email import message_from_bytes, policy
from email.header import decode_header, make_header

import pytest
from fastapi.testclient import TestClient

from app.mail import Mailer, drafted_message, find_emails, split_addresses
from app.main import create_app
from app.review import approve


class SmtpCapture(threading.Thread):
    """Just enough SMTP to receive what the app sends (no TLS: the test settings use security=none)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self.auth = None

    def run(self):
        while True:
            conn, _ = self.sock.accept()
            try:
                self.serve(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def serve(self, conn):
        reader, writer = conn.makefile("rb"), conn.makefile("wb")

        def say(line):
            writer.write(line.encode() + b"\r\n")
            writer.flush()

        say("220 test ESMTP")
        envelope = {"from": None, "rcpt": []}
        while True:
            line = reader.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                say("250-test")
                say("250 AUTH PLAIN LOGIN")
            elif up.startswith("AUTH PLAIN"):
                self.auth = base64.b64decode(cmd.split(" ", 2)[2]).decode()
                say("235 ok")
            elif up.startswith("MAIL FROM"):
                envelope["from"] = cmd.split(":", 1)[1].strip().strip("<>")
                say("250 ok")
            elif up.startswith("RCPT TO"):
                envelope["rcpt"].append(cmd.split(":", 1)[1].strip().strip("<>"))
                say("250 ok")
            elif up == "DATA":
                say("354 go")
                data = b""
                while True:
                    chunk = reader.readline()
                    if chunk in (b".\r\n", b".\n") or not chunk:
                        break
                    data += chunk
                self.messages.append({**envelope, "data": data})
                envelope = {"from": None, "rcpt": []}
                say("250 queued")
            elif up == "QUIT":
                say("221 bye")
                return
            else:
                say("250 ok")


@pytest.fixture
def smtp():
    server = SmtpCapture()
    server.start()
    return server


def mail_settings(settings, smtp):
    return replace(settings, mail_host="127.0.0.1", mail_port=smtp.port, mail_security="none", mail_user="pec",
                   mail_password="app-password", mail_from="sales@pec.co.th", mail_from_name="ฝ่ายขาย PEC",
                   mail_bcc="copy@pec.co.th")


def header(msg, name):
    return str(make_header(decode_header(msg[name])))


def test_an_approved_result_goes_out_with_the_word_file(settings, smtp):
    app = create_app(mail_settings(settings, smtp), start_background=False)
    store, tasks = app.state.store, app.state.tasks
    (settings.knowledge_dir / "customers" / "ACME" / "profile.md").write_text(
        "ผู้ติดต่อหลัก: คุณประเสริฐ prasert@acme.co.th, สำรอง arunee@acme.co.th", encoding="utf-8")
    with TestClient(app) as client:
        assert client.get("/api/status").json()["limits"]["mail"] is True
        job = client.post("/api/jobs", data={"type": "proposal-draft", "customer": "ACME",
                                             "instructions": "feeder 125A"}).json()
        asyncio.run(app.state.runner.run_job(job["id"]))
        result = store.job_dir(job["id"]) / "output" / "proposal.md"
        result.write_text(result.read_text("utf-8") + "\n## อีเมล\n\n> หัวข้อ: ใบเสนอราคา feeder 125 A\n>\n"
                          "> เรียน คุณประเสริฐ\n>\n> แนบใบเสนอราคา **เลขที่ QT-001** มาด้วยครับ\n> - ยืนราคา 30 วัน\n"
                          "> - ไม่รวม VAT\n>\n> ขอบคุณครับ\n> [ชื่อผู้ส่ง]\n\nหมายเหตุภายใน: ยังไม่ยืนยันราคา MCCB\n", encoding="utf-8")
        before = client.post(f"/api/jobs/{job['id']}/email", json={"to": "prasert@acme.co.th", "subject": "x"})
        assert before.status_code == 409 and "อนุมัติ" in before.json()["detail"]  # not approved yet
        approve(store, tasks, settings, job["id"])

        pre = client.get(f"/api/jobs/{job['id']}/email").json()
        assert pre["can_send"] and pre["sender"] == "sales@pec.co.th"
        assert pre["to"] == ["prasert@acme.co.th", "arunee@acme.co.th"]
        assert pre["subject"] == "ใบเสนอราคา feeder 125 A"
        assert pre["body"].startswith("เรียน คุณประเสริฐ\n\nแนบใบเสนอราคา เลขที่ QT-001 มาด้วยครับ\n• ยืนราคา 30 วัน")
        assert "หมายเหตุภายใน" not in pre["body"]
        assert pre["body"].endswith("ขอบคุณครับ\nฝ่ายขาย PEC") and "[ชื่อผู้ส่ง]" not in pre["body"]  # signed with MAIL_FROM_NAME
        docx = [a for a in pre["attachments"] if a["name"].endswith(".docx")]
        assert len(docx) == 1 and docx[0]["default"] is True and docx[0]["path"].startswith("library/proposal-draft/")
        assert any(a["path"] == "output/proposal.md" and not a["default"] for a in pre["attachments"])

        res = client.post(f"/api/jobs/{job['id']}/email", json={
            "to": "prasert@acme.co.th, arunee@acme.co.th", "cc": "boss@pec.co.th", "subject": pre["subject"],
            "body": pre["body"], "attachments": [docx[0]["path"], "output/proposal.md"]})
        assert res.status_code == 200, res.text
        sent = res.json()["sent"]
        assert sent["to"] == ["prasert@acme.co.th", "arunee@acme.co.th"] and sent["attachments"][0].endswith(".docx")
        assert len(smtp.messages) == 1
        got = smtp.messages[0]
        assert got["from"] == "sales@pec.co.th"
        assert got["rcpt"] == ["prasert@acme.co.th", "arunee@acme.co.th", "boss@pec.co.th", "copy@pec.co.th"]
        assert smtp.auth == "\0pec\0app-password"
        msg = message_from_bytes(got["data"], policy=policy.default)
        assert header(msg, "Subject") == "ใบเสนอราคา feeder 125 A" and "ฝ่ายขาย PEC" in header(msg, "From")
        assert msg["To"] == "prasert@acme.co.th, arunee@acme.co.th" and msg["Cc"] == "boss@pec.co.th"
        parts = list(msg.walk())
        body = next(p for p in parts if p.get_content_type() == "text/plain").get_content()
        assert "เรียน คุณประเสริฐ" in body and "• ยืนราคา 30 วัน" in body
        names = [p.get_filename() for p in parts if p.get_filename()]
        assert len(names) == 2 and names[0].endswith(".docx") and names[1] == "proposal.md"
        word = next(p for p in parts if (p.get_filename() or "").endswith(".docx"))
        assert word.get_content_type() == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert word.get_payload(decode=True)[:2] == b"PK"

        detail = client.get(f"/api/jobs/{job['id']}").json()
        assert detail["email"] is True
        assert detail["job"]["emails"][0]["to"] == ["prasert@acme.co.th", "arunee@acme.co.th"]
        assert detail["steps"][-1]["kind"] == "email" and "prasert@acme.co.th" in detail["steps"][-1]["text"]
        assert client.get(f"/api/jobs/{job['id']}/email").json()["sent"][0]["subject"] == pre["subject"]

        # what is refused, and never reaches the server
        url = f"/api/jobs/{job['id']}/email"
        assert client.post(url, json={"to": "not-an-address", "subject": "x"}).status_code == 409
        assert client.post(url, json={"to": "", "subject": "x"}).status_code == 409
        assert client.post(url, json={"to": "a@b.co", "subject": " "}).status_code == 409
        assert client.post(url, json={"to": "a@b.co", "subject": "x", "attachments": ["library/../../.env"]}).status_code == 404
        assert client.post(url, json={"to": "a@b.co", "subject": "x", "attachments": ["output/../job.json"]}).status_code == 404
        assert client.post(url, json={"to": "a@b.co", "subject": "x", "attachments": ["/etc/passwd"]}).status_code == 404
        assert client.post(url, json={"to": "a@b.co", "subject": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403
        assert len(smtp.messages) == 1

        test = client.post("/api/mail/test").json()
        assert test["to"] == ["sales@pec.co.th"] and smtp.messages[-1]["rcpt"][0] == "sales@pec.co.th"
        system = client.get("/api/system").json()["mail"]
        assert system["configured"] and system["user"] is True and "app-password" not in str(system)


def test_without_settings_the_button_and_the_endpoints_are_off(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        assert client.get("/api/status").json()["limits"]["mail"] is False
        job = app.state.store.create("general", "x", "x")
        app.state.store.update(job["id"], status="done")
        assert client.get(f"/api/jobs/{job['id']}/email").json()["configured"] is False
        assert client.get(f"/api/jobs/{job['id']}").json()["email"] is False
        assert client.post(f"/api/jobs/{job['id']}/email", json={"to": "a@b.co", "subject": "x"}).status_code == 409
        assert client.post("/api/mail/test").status_code == 409
        items = {i["key"]: i for i in client.get("/api/readiness").json()["items"]}
        assert items["mail"]["status"] == "info"
    from app import readiness
    half = replace(settings, mail_host="smtp.example.com")
    assert {i["key"]: i for i in readiness.run_checks(half, None, [], 0)["items"]}["mail"]["status"] == "warn"
    odd = replace(settings, mail_host="smtp.example.com", mail_from="a@b.co", mail_security="tls")
    assert "MAIL_SMTP_SECURITY" in {i["key"]: i for i in readiness.run_checks(odd, None, [], 0)["items"]}["mail"]["detail"]


def test_helpers_and_server_errors(settings, smtp):
    assert split_addresses("a@x.co, B@y.co;a@x.co\nc@z.co") == ["a@x.co", "B@y.co", "c@z.co"]
    assert split_addresses(["<a@x.co>", " "]) == ["a@x.co"]
    assert find_emails("ติดต่อ somchai@acme.co.th หรือ demo@acme.example") == ["somchai@acme.co.th"]
    assert drafted_message("no quote here") == {"subject": "", "body": ""}
    assert drafted_message("> เรื่อง: นัดพบ\n>\n> สวัสดี") == {"subject": "นัดพบ", "body": "สวัสดี"}
    assert drafted_message("> แค่ข้อความ\n> สองบรรทัด\n\n> บล็อกที่สองไม่นับ")["body"] == "แค่ข้อความ\nสองบรรทัด"
    mailer = Mailer(replace(settings, mail_host="127.0.0.1", mail_port=1, mail_security="none", mail_from="a@b.co"))
    with pytest.raises(ValueError, match="ส่งอีเมลไม่สำเร็จ"):  # nothing listens on port 1
        mailer.send(["x@y.co"], [], "x", "y", [])
    with pytest.raises(ValueError, match="ผู้ช่วยงาน|ไม่ถูกต้อง|ไฟล์แนบ"):
        Mailer(mail_settings(settings, smtp)).send(["x@y.co"], [], "x", "y", [settings.data_dir / "missing.docx"])
