"""Word export, notifications, follow-ups with files, and the system page API."""
import asyncio
import io
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from docx import Document
from docx.oxml.ns import qn
from fastapi.testclient import TestClient

from app.export_docx import action_items_markdown, markdown_to_docx, parse_markdown
from app.main import create_app

SAMPLE = """# สรุปประชุมโครงการ MDB

- วันที่ 1 พ.ย. 2569
- ผู้เข้าร่วม: **สมชาย**, วิภา

## มติที่ประชุม
ใช้ breaker ยี่ห้อเดิม เพื่อให้ `spare part` ตรงกัน

| งาน | ผู้รับผิดชอบ |
|---|---|
| แก้ single line diagram | สมชาย |

> ไฟล์นี้เป็นตัวอย่าง
"""


def text_of(doc):
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        parts += [c.text for row in table.rows for c in row.cells]
    return "\n".join(parts)


def test_markdown_parser_blocks():
    kinds = [b[0] for b in parse_markdown(SAMPLE)]
    assert kinds == ["h", "ul", "h", "p", "table", "quote"]


def test_docx_export_has_thai_fonts_tables_and_title_block():
    data = markdown_to_docx(SAMPLE, title="ประชุม MDB", file_name="summary.md", rev=2, modified="2026-11-01T10:00:00+07:00")
    doc = Document(io.BytesIO(data))
    body = text_of(doc)
    assert "สรุปประชุมโครงการ MDB" in body and "แก้ single line diagram" in body
    assert "Rev." in body and "summary.md" in body and "01/11/2026 10:00" in body
    rfonts = doc.styles["Normal"].element.rPr.find(qn("w:rFonts"))
    assert rfonts.get(qn("w:cs")) == "Anuphan"  # Thai is a complex script in Word
    heading = doc.styles["Heading 1"].element.rPr.find(qn("w:rFonts"))
    assert heading.get(qn("w:asciiTheme")) is None and heading.get(qn("w:ascii")) == "Anuphan"  # no theme font override
    assert doc.sections[0].page_width.cm > 20.9  # A4
    header_fill = doc.tables[0].cell(0, 0)._tc.tcPr.find(qn("w:shd")).get(qn("w:fill"))
    assert header_fill == "E4E7EC"


def test_docx_uses_company_template(tmp_path):
    template = tmp_path / "report-template.docx"
    base = Document()
    base.sections[0].header.paragraphs[0].text = "บริษัทตัวอย่าง จำกัด"
    base.add_paragraph("เนื้อหาตัวอย่างในแม่แบบ")
    base.save(template)
    doc = Document(io.BytesIO(markdown_to_docx("# หัวข้อ\n\nเนื้อหา", title="t", file_name="a.md", rev=1,
                                               modified=None, template=template)))
    assert doc.sections[0].header.paragraphs[0].text == "บริษัทตัวอย่าง จำกัด"
    assert "เนื้อหาตัวอย่างในแม่แบบ" not in text_of(doc)


def test_action_items_markdown_table():
    md = action_items_markdown([{"task": "ส่งแบบ", "owner": None, "due": "2026-11-05", "priority": "high"}], "ประชุม")
    assert "| ส่งแบบ | ยังไม่ระบุ | 2026-11-05 | สูง |" in md


class Capture(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Capture.received.append({"path": self.path, "type": self.headers.get("Content-Type"),
                                 "auth": self.headers.get("Authorization"), "body": body.decode()})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def start_capture():
    Capture.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_notifications_on_review_and_test_endpoint(settings):
    server, base = start_capture()
    try:
        s = replace(settings, dashboard_url="http://nas.local:8080", notify_synology_url=f"{base}/syno",
                    notify_webhook_url=f"{base}/hook", notify_line_token="tkn", notify_line_to="U123",
                    notify_line_api=f"{base}/line")
        app = create_app(s, start_background=False)
        with TestClient(app) as client:
            job_id = client.post("/api/jobs", data={"type": "general", "instructions": "ทดสอบแจ้งเตือน"}).json()["id"]

            async def run_and_wait():
                await app.state.runner.run_job(job_id)
                await asyncio.gather(*app.state.runner._background)

            asyncio.run(run_and_wait())
            by_path = {r["path"]: r for r in Capture.received}
            syno = json.loads(parse_qs(by_path["/syno"]["body"])["payload"][0])
            assert syno["text"].startswith("งานรอตรวจ: ทดสอบแจ้งเตือน") and f"#/job/{job_id}" in syno["text"]
            hook = json.loads(by_path["/hook"]["body"])
            assert hook["event"] == "needs_review" and hook["job"]["url"] == f"http://nas.local:8080/#/job/{job_id}"
            line = json.loads(by_path["/line"]["body"])
            assert by_path["/line"]["auth"] == "Bearer tkn" and line["to"] == "U123"

            Capture.received.clear()
            assert client.post("/api/notify/test").json() == {"synology": "ok", "line": "ok", "webhook": "ok"}
            assert len(Capture.received) == 3
    finally:
        server.shutdown()


def test_notify_failure_is_reported_not_raised(settings):
    s = replace(settings, notify_webhook_url="http://127.0.0.1:9/nothing-listens-here")
    with TestClient(create_app(s, start_background=False)) as client:
        result = client.post("/api/notify/test").json()
        assert result["webhook"].startswith("error")


def test_follow_up_with_files_and_export_endpoint(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", data={"type": "general", "instructions": "สรุปเอกสาร"}).json()["id"]
        asyncio.run(app.state.runner.run_job(job_id))
        res = client.post(f"/api/jobs/{job_id}/messages", data={"message": "ดู spec เพิ่มด้วย"},
                          files=[("files", ("spec รุ่นใหม่.txt", "รายละเอียด spec".encode(), "text/plain"))])
        assert res.status_code == 200 and res.json()["status"] == "queued"
        asyncio.run(app.state.runner.run_job(job_id))
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert "spec รุ่นใหม่.txt" in [f["path"] for f in detail["files"]["input"]]
        user_steps = [s for s in detail["steps"] if s["kind"] == "user"]
        assert user_steps[-1]["files"] == ["spec รุ่นใหม่.txt"]
        assert detail["job"]["runs"][-1]["files"] == ["spec รุ่นใหม่.txt"]

        files_only = client.post(f"/api/jobs/{job_id}/messages", files=[("files", ("x.txt", b"x", "text/plain"))])
        assert files_only.status_code == 200
        asyncio.run(app.state.runner.run_job(job_id))

        exported = client.get(f"/api/jobs/{job_id}/export/result.md")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert "filename*=UTF-8''" in exported.headers["content-disposition"]
        assert "Rev." in text_of(Document(io.BytesIO(exported.content)))
        assert client.get(f"/api/jobs/{job_id}/export/../job.json").status_code == 404


def test_system_endpoint(settings):
    with TestClient(create_app(settings, start_background=False)) as client:
        client.post("/api/jobs", data={"type": "general", "instructions": "x"})
        s = client.get("/api/system").json()
        assert s["version"]["app"] and s["storage"]["jobs"] == 1 and s["storage"]["knowledge_files"] == 1
        assert s["notify"]["channels"] == [] and s["worker"]["max"] == 1
        assert client.post("/api/notify/test").status_code == 409
