"""The kinds of work the assistant can do. Each type maps to a prompt file in app/prompts/."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Field:
    name: str  # title | instructions | customer | files
    label: str
    kind: str = "text"  # text | textarea | files
    required: bool = False
    placeholder: str = ""


@dataclass(frozen=True)
class JobType:
    id: str
    label: str
    icon: str
    description: str
    fields: tuple[Field, ...]
    outputs: tuple[str, ...]  # paths relative to the job folder; the first one is shown first
    allow_python: bool = False  # lets Claude run `python3` for calculations

    @property
    def primary_output(self) -> str:
        return self.outputs[0]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["fields"] = [asdict(f) for f in self.fields]
        return data


TITLE = Field("title", "ชื่องาน", placeholder="เช่น ประชุมโครงการ ABC ครั้งที่ 3")
FILES = Field("files", "ไฟล์ประกอบ", kind="files")

JOB_TYPES: dict[str, JobType] = {
    t.id: t
    for t in (
        JobType(
            id="meeting-summary",
            label="สรุปประชุม",
            icon="📝",
            description="สรุปมติ action item และประเด็นค้าง จาก transcript หรือบันทึกประชุม",
            fields=(
                TITLE,
                Field("files", "Transcript / บันทึกประชุม (.txt .vtt .srt .docx .pdf)", kind="files", required=True),
                Field("instructions", "คำสั่งเพิ่มเติม", kind="textarea", placeholder="เช่น เน้นประเด็นงบประมาณ"),
            ),
            outputs=("output/summary.md", "output/action_items.json"),
        ),
        JobType(
            id="follow-up",
            label="ติดตามงาน",
            icon="⏰",
            description="ไล่ดู action item ที่ใกล้หรือเลยกำหนด และร่างข้อความทวงงาน (ระบบสร้างให้อัตโนมัติทุกเช้า)",
            fields=(
                Field("title", "ชื่องาน", placeholder="ติดตามงานประจำวัน"),
                Field("instructions", "คำสั่งเพิ่มเติม", kind="textarea"),
            ),
            outputs=("output/follow-up.md",),
        ),
        JobType(
            id="sales-brief",
            label="เตรียมพบลูกค้า",
            icon="🤝",
            description="สรุปข้อมูลลูกค้า ประวัติการคุย ประเด็นค้าง และสิ่งที่ควรเสนอ ก่อนเข้าพบ",
            fields=(
                TITLE,
                Field("customer", "ชื่อลูกค้า", required=True, placeholder="ตรงกับชื่อโฟลเดอร์ใน knowledge/customers/"),
                Field("instructions", "วัตถุประสงค์การพบ / คำสั่งเพิ่มเติม", kind="textarea"),
                FILES,
            ),
            outputs=("output/brief.md",),
        ),
        JobType(
            id="proposal-draft",
            label="ร่างใบเสนอราคา",
            icon="💼",
            description="ร่าง proposal/ใบเสนอราคาจาก requirement โดยดึงราคาจาก price list ในคลังความรู้เท่านั้น",
            fields=(
                TITLE,
                Field("customer", "ชื่อลูกค้า", required=True),
                Field("instructions", "Requirement ของลูกค้า", kind="textarea", required=True),
                FILES,
            ),
            outputs=("output/proposal.md",),
            allow_python=True,
        ),
        JobType(
            id="eng-qa",
            label="ถามงานวิศวกรรม",
            icon="📐",
            description="ตอบคำถาม/ช่วยคำนวณงานออกแบบ โดยค้นจากคลังความรู้และคำนวณด้วย Python พร้อมอ้างอิง",
            fields=(
                TITLE,
                Field("instructions", "คำถาม / โจทย์", kind="textarea", required=True),
                FILES,
            ),
            outputs=("output/answer.md",),
            allow_python=True,
        ),
        JobType(
            id="general",
            label="งานทั่วไป",
            icon="✨",
            description="สั่งงานอื่น ๆ ด้วยข้อความอิสระ เช่น สรุปเอกสาร ร่างอีเมล เปรียบเทียบ spec",
            fields=(
                TITLE,
                Field("instructions", "สิ่งที่ต้องการให้ทำ", kind="textarea", required=True),
                FILES,
            ),
            outputs=("output/result.md",),
        ),
    )
}
