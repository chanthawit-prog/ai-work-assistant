# ติดตั้งบน Synology DS925+

## 0. สิ่งที่ต้องมี

- DSM 7.2 ขึ้นไป และแพ็กเกจ **Container Manager** (ติดตั้งจาก Package Center)
- Claude subscription: Pro, Max หรือ seat ของ Team/Enterprise
- แรม 4 GB ที่ติดมากับเครื่องพอสำหรับทำทีละงาน ถ้ารันแอปอื่นบน NAS ด้วย แนะนำเพิ่มเป็น 8–16 GB
- DS925+ ไม่ต้องมี GPU เพราะ Claude ประมวลผลบน cloud ส่วน NAS แค่ส่งงานและเก็บไฟล์

## 1. เตรียมโฟลเดอร์

1. **Control Panel → Shared Folder → Create** ตั้งชื่อ `ai-assistant` (เปิด Snapshot ไว้ด้วยจะดีมาก)
2. ใน File Station สร้างโฟลเดอร์ย่อย `data`, `knowledge` และ `claude-home` ไว้ข้างใน
3. สร้างโฟลเดอร์สำหรับโค้ด เช่น `/docker/ai-work-assistant` (shared folder `docker` มักถูกสร้างตอนติดตั้ง Container Manager)

## 2. นำโค้ดไปไว้บน NAS

- **วิธีง่าย**: เปิด repo บน GitHub → **Code → Download ZIP** → แตกไฟล์ → อัปโหลดทั้งหมดเข้า `/docker/ai-work-assistant` ด้วย File Station
- **ผ่าน SSH** (ถ้าคุ้นเคย): `git clone` repo นี้ไปที่ `/volume1/docker/ai-work-assistant` (repo เป็น private จึงต้องใช้ token ของ GitHub)

## 3. ตั้งค่า `.env`

คัดลอก `.env.example` เป็น `.env` ไว้ในโฟลเดอร์โค้ด แล้วแก้ค่าเหล่านี้ (แก้ผ่าน SSH หรือแพ็กเกจ Text Editor ก็ได้)

| ค่า | ความหมาย |
|---|---|
| `DATA_PATH`, `KNOWLEDGE_PATH`, `CLAUDE_HOME_PATH` | path ของโฟลเดอร์จากข้อ 1 (ค่าตั้งต้นคือ `/volume1/ai-assistant/...`) |
| `DASHBOARD_PASSWORD` | รหัสผ่านหน้าเว็บ (user ตั้งต้นคือ `admin`) **ควรตั้ง** |
| `FOLLOWUP_TIME`, `FOLLOWUP_DAYS` | เวลาที่ระบบสร้างงาน "ติดตามงาน" อัตโนมัติ |
| `CLAUDE_MODEL`, `CLAUDE_EFFORT` | เว้นว่างไว้ก่อนได้ (ใช้ค่าตั้งต้นของแพ็กเกจ) |
| `CLAUDE_BIN` | `claude` สำหรับใช้งานจริง หรือ `/opt/app/scripts/fake-claude` เพื่อทดลองระบบ |

## 4. สร้าง Project ใน Container Manager

1. **Container Manager → Project → Create**
2. Project name: `ai-work-assistant` และ Path: `/docker/ai-work-assistant`
3. เลือกใช้ `docker-compose.yml` ที่มีอยู่ในโฟลเดอร์ แล้วกด Next จนเสร็จ (ไม่ต้องตั้ง Web Station)
4. ครั้งแรกระบบจะ build image หลายนาที (ดาวน์โหลด Node.js, Python และ Claude Code) จากนั้นจะ start ให้เอง

## 5. ล็อกอิน Claude (ทำครั้งเดียว)

### วิธี A: ล็อกอินใน container (แนะนำ)

1. เปิด SSH ชั่วคราว: **Control Panel → Terminal & SNMP → Enable SSH service**
2. SSH เข้า NAS แล้วรัน

   ```bash
   sudo docker exec -it ai-work-assistant claude auth login
   ```

3. คัดลอกลิงก์ที่ขึ้นใน terminal ไปเปิดในเบราว์เซอร์ ล็อกอินด้วยบัญชี Claude ของคุณ แล้วนำรหัสที่ได้มาวางกลับใน terminal
4. ตรวจสอบด้วย `sudo docker exec ai-work-assistant claude auth status --text`
5. ปิด SSH ได้ ข้อมูลล็อกอินถูกเก็บใน `claude-home/` และยังอยู่แม้จะ build image ใหม่

### วิธี B: ใช้ token อายุ 1 ปี

1. บนคอมพิวเตอร์ที่ติดตั้ง Claude Code ไว้ รัน `claude setup-token` แล้วอนุมัติในเบราว์เซอร์
2. นำ token ที่ได้ไปใส่ใน `CLAUDE_CODE_OAUTH_TOKEN=` ในไฟล์ `.env`
3. ใน Container Manager สั่ง **Build** project ใหม่ เพื่อให้ container อ่านค่าใหม่

ทั้งสองวิธีเป็นขั้นตอนของ Anthropic เอง หน้า dashboard ไม่เคยขอหรือเก็บรหัสผ่าน Claude ของคุณ

## 6. เปิดใช้งาน

1. เปิด `http://<IP ของ NAS>:8080` แล้วใส่ user/password ที่ตั้งไว้
2. ป้ายมุมขวาบนต้องเป็นสีเขียว **"Claude พร้อม · subscription"** ถ้ายังแดงให้กดที่ป้ายเพื่อดูวิธีแก้ แล้วกด "ตรวจสอบอีกครั้ง"
3. ทดสอบ: **สร้างงาน → งานทั่วไป →** "สรุปสั้น ๆ ว่าคุณช่วยงานอะไรได้บ้าง" แล้วดู timeline ขยับระหว่างที่ Claude ทำงาน

## 7. เตรียมคลังความรู้

วางเอกสารที่ Claude ควรใช้อ้างอิงไว้ใน `knowledge/`

| โฟลเดอร์ | ใส่อะไร |
|---|---|
| `customers/<ชื่อลูกค้า>/` | ข้อมูลลูกค้า ประวัติโครงการ อีเมลสำคัญ (ชื่อโฟลเดอร์ใช้กรอกในงาน "เตรียมพบลูกค้า") |
| `pricing/` | price list แนะนำให้ export Excel เป็น `.csv` เพราะระบบนี้อ่าน `.xlsx` ตรง ๆ ไม่ได้ |
| `engineering/` | design guide มาตรฐานที่บริษัทมีสิทธิ์ใช้ calc sheet และรายงานเก่า (PDF ได้) |
| `templates/` | template ของ proposal หรือเอกสารอื่น |

ชนิดไฟล์ที่ Claude อ่านได้ดี: `.md .txt .csv .pdf` และรูปภาพ ส่วนไฟล์ Word ในงานสรุปประชุมระบบจะแปลงเป็นข้อความให้อัตโนมัติ

## 8. ใช้งานจากนอกออฟฟิศ

ติดตั้ง **Tailscale** (มีใน Package Center) หรือเปิด **VPN Server** แล้วเข้า dashboard ผ่าน VPN
อย่าเปิด port 8080 ออกอินเทอร์เน็ตโดยตรง

## 9. อัปเดต

- **Claude Code**: สั่ง **Build** project ใหม่ใน Container Manager ระบบจะติดตั้งเวอร์ชันล่าสุดให้
- **โค้ดของระบบ**: อัปโหลดไฟล์ใหม่ทับ (หรือ `git pull`) แล้วสั่ง Build

## 10. สำรองข้อมูล

- เปิด **Snapshot Replication** ให้ shared folder `ai-assistant`
- ใช้ **Hyper Backup** สำรอง `data/` และ `knowledge/` ส่วน `claude-home/` มีข้อมูลล็อกอิน ให้ตัดออกจากการสำรองหรือเปิดการเข้ารหัส

## แก้ปัญหา

| อาการ | วิธีแก้ |
|---|---|
| ป้ายแดง "Claude ยังไม่ได้ล็อกอิน" | ทำข้อ 5 แล้วกด "ตรวจสอบอีกครั้ง" |
| งานค้างที่ "รอคิว" | ดูป้ายโควต้า และงานที่กำลังทำอยู่ (ค่าตั้งต้นทำทีละงาน) |
| "Claude ไม่ได้สร้างไฟล์ผลลัพธ์" | กด "สั่งแก้แล้วทำต่อ" แล้วบอกให้เขียนไฟล์ให้ครบ |
| วางไฟล์ใน inbox แล้วไม่มีงาน | ชื่อไฟล์ห้ามขึ้นต้นด้วย `.` `@` `#` และรอประมาณ 20–30 วินาที |
| อยากดูรายละเอียดทั้งหมด | `data/jobs/<job-id>/events.jsonl` หรือ Container Manager → Container → Log |
