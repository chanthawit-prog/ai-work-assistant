# ติดตั้งบน Synology DS925+

## 0. สิ่งที่ต้องมี

- DSM 7.2 ขึ้นไป และแพ็กเกจ **Container Manager** (ติดตั้งจาก Package Center)
- Claude subscription: Pro, Max หรือ seat ของ Team/Enterprise
- แรม: Claude ประมวลผลบน cloud งานหนึ่งใช้ RAM บน NAS ราว 200–300 MB ระบบตั้งค่ามาให้ทำงานได้บนเครื่อง 4–6 GB
  ที่ swap เต็มง่าย (ดูข้อ 13) ถ้ารันแอปอื่นบน NAS ด้วย แนะนำเพิ่มเป็น 8–16 GB
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
| `MEM_LIMIT`, `MIN_FREE_MEM_MB` | เพดาน RAM ของ container และ RAM ว่างขั้นต่ำก่อนเริ่มงาน ค่าตั้งต้นเหมาะกับ RAM 6 GB (ข้อ 13) |

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

## 8. แจ้งเตือนเมื่องานรอตรวจ (ไม่บังคับ)

ตั้ง `DASHBOARD_URL` เป็นที่อยู่ที่ใช้เปิด dashboard (เช่น `http://192.168.1.20:8080`) เพื่อให้ข้อความมีลิงก์กดเปิดงาน
แล้วเลือกช่องทางใดช่องทางหนึ่งหรือหลายช่องทาง จากนั้นสั่ง Build project ใหม่ และไปที่หน้า **ระบบ → ส่งข้อความทดสอบ**

| ช่องทาง | ตั้งค่าอย่างไร | ค่าใน `.env` |
|---|---|---|
| **Synology Chat** | ติดตั้งแพ็กเกจ Synology Chat Server → ในแอป Chat กดรูปโปรไฟล์ → Integration → Incoming Webhooks → Create แล้วคัดลอก Webhook URL | `NOTIFY_SYNOLOGY_CHAT_URL` |
| **LINE** | สร้าง Messaging API channel ที่ LINE Developers Console → ออก Channel access token (long-lived) → เพิ่ม Official Account เป็นเพื่อน แล้วนำ user ID ของคุณ (หน้า Basic settings) หรือ group ID มาใส่ | `NOTIFY_LINE_TOKEN`, `NOTIFY_LINE_TO` |
| **Webhook อื่น** | URL ของระบบที่รับ JSON ได้ เช่น Teams Workflows, Slack, n8n | `NOTIFY_WEBHOOK_URL` |

LINE Notify ปิดบริการไปตั้งแต่ 31 มี.ค. 2025 จึงต้องใช้ Messaging API ข้อความที่ส่งมีเพียงชื่องาน ชนิดงาน และลิงก์
เลือกเหตุการณ์ที่จะแจ้งได้ที่ `NOTIFY_ON` (`needs_review`, `failed`, `quota_wait`)

## 9. แม่แบบ Word ของบริษัท (ไม่บังคับ)

ปุ่ม **ดาวน์โหลด Word** ใช้รูปแบบตั้งต้นแบบเอกสาร PEC: A4, ฟอนต์ **Anuphan** (เนื้อความ 10 pt ตาราง 9 pt
หัวข้อ 20 / 15 / 12 pt ตามตัวสร้างรายงานของ PEC) หัวข้อสีน้ำเงินเข้ม ถ้าต้องการหัวกระดาษ ท้ายกระดาษ และโลโก้ของบริษัท
ให้บันทึกเอกสาร Word ที่มีหัว-ท้ายกระดาษไว้ที่ `knowledge/templates/report-template.docx`
ระบบจะใช้ส่วนหัว ส่วนท้าย และ style จากไฟล์นั้น แล้วใส่เนื้อหาของ Claude ลงไปแทนเนื้อหาเดิม

ฟอนต์ Anuphan (Regular และ Bold) ถูกฝังไว้ในไฟล์ Word (ไฟล์ใหญ่ขึ้นราว 140 KB) ลูกค้าที่ไม่มีฟอนต์นี้ก็เห็นเอกสารถูกต้อง
ถ้าเครื่องในบริษัทติดตั้ง Anuphan ไว้ทุกเครื่องแล้วและต้องการไฟล์เล็กลง ตั้ง `DOCX_EMBED_FONTS=false`
Anuphan ไม่มีอักษรกรีกและสัญลักษณ์บางตัว (เช่น Ω φ π Σ μ Δ ✔ ☐) ตัวอักษรเหล่านั้น Word จะแสดงด้วยฟอนต์อื่นแทน

## 10. ใช้งานจากนอกออฟฟิศ

ติดตั้ง **Tailscale** (มีใน Package Center) หรือเปิด **VPN Server** แล้วเข้า dashboard ผ่าน VPN
อย่าเปิด port 8080 ออกอินเทอร์เน็ตโดยตรง

## 11. อัปเดต

- **Claude Code**: สั่ง **Build** project ใหม่ใน Container Manager ระบบจะติดตั้งเวอร์ชันล่าสุดให้
- **โค้ดของระบบ**: อัปโหลดไฟล์ใหม่ทับ (หรือ `git pull`) แล้วสั่ง Build

## 12. สำรองข้อมูล

- เปิด **Snapshot Replication** ให้ shared folder `ai-assistant`
- ใช้ **Hyper Backup** สำรอง `data/` และ `knowledge/` ส่วน `claude-home/` มีข้อมูลล็อกอิน ให้ตัดออกจากการสำรองหรือเปิดการเข้ารหัส

## 13. หน่วยความจำและ swap (RAM 6 GB)

งานหนึ่งงาน Claude Code ใช้ RAM บน NAS ไม่มาก: งานสรุปประชุมที่ทดลองใช้สูงสุดราว **220 MB** ส่วนหน้า dashboard ใช้ราว
60–80 MB ค่าจริงของเครื่องคุณดูได้ที่หน้า **ระบบ → หน่วยความจำ** (ทุกงานบันทึก RAM สูงสุดไว้ และแสดงต่อท้ายข้อความ
"ผลงานพร้อมให้ตรวจ") ระบบตั้งค่าเหล่านี้ไว้ให้แล้ว

| กลไก | ทำอะไร | ปรับที่ |
|---|---|---|
| ทำทีละงาน | ไม่มีงาน Claude สองงานแย่ง RAM กัน | `MAX_CONCURRENT_JOBS=1` |
| รอ RAM ว่างก่อนเริ่ม | ถ้า RAM ว่างของ NAS ต่ำกว่า 700 MB งานจะรอในคิว (ขึ้นว่า "รอ RAM ของ NAS ว่าง") ครบ 20 นาทีแล้วเริ่มเลย | `MIN_FREE_MEM_MB`, `MEM_WAIT_MAX_MIN` |
| เพดาน RAM ของ container | 1.5 GB และ container นี้ไม่ใช้ swap งานที่ใช้เกินจะถูกหยุดพร้อมข้อความ "หน่วยความจำไม่พอ" แทนที่ NAS ทั้งเครื่องจะช้า | `MEM_LIMIT` |
| ถ้า RAM ของ NAS หมดจริง | kernel จะหยุด Claude ก่อนบริการของ DSM | `oom_score_adj` ใน `docker-compose.yml` |
| Claude โหมดประหยัด | `BUN_OPTIONS=--smol` ให้ Claude Code (สร้างบน Bun) เก็บกวาดหน่วยความจำบ่อยขึ้น, `MALLOC_ARENA_MAX=2`, ไม่โหลด MCP server, ปิด telemetry | `CLAUDE_LOW_MEMORY` |
| ไฟล์แนบไม่ค้างใน RAM | เขียนลงดิสก์ทีละ 1 MB จำกัดรวม 200 MB ต่อครั้ง | `MAX_UPLOAD_MB` |
| ตรวจการล็อกอินห่างขึ้น | ทุก 30 นาที และไม่ตรวจระหว่างที่มีงาน (การตรวจแต่ละครั้งเปิด Claude Code ขึ้นมาชั่วครู่) | – |

แก้ค่าใน `.env` แล้วสั่ง **Build** project ใหม่ทุกครั้ง (`MEM_LIMIT` ถูกอ่านตอนสร้าง container)
ถ้า Container Manager เตือนว่า *kernel does not support swap limit* ไม่เป็นไร เพดาน RAM ยังทำงาน เพียงแต่คุม swap ไม่ได้

**เรื่อง swap เต็ม** ตัวเลข swap ที่สูงไม่ได้แปลว่ากำลังมีปัญหาเสมอไป Linux ย้ายหน่วยความจำของโปรแกรมที่ไม่ได้ใช้ลง swap
แล้วไม่ย้ายกลับจนกว่าจะถูกเรียกใช้ ปัญหาจริงคือเมื่อ **RAM ว่างต่ำ** และ NAS ช้าหรือดิสก์ทำงานตลอด ซึ่งส่วนใหญ่มาจากฝั่ง DSM

1. ดูว่าอะไรใช้ RAM: **Resource Monitor → Task Manager → Processes** เรียงตาม Memory
2. ปิดหรือถอนแพ็กเกจที่ไม่ได้ใช้ ตัวที่ใช้ RAM มากบ่อย ๆ คือ Synology Photos (จดจำใบหน้า/วัตถุ), Universal Search (สร้างดัชนี),
   Surveillance Station, Active Insight, Plex/Jellyfin
3. **Virtual Machine Manager** จอง RAM เต็มจำนวนที่ตั้งให้ VM ลด RAM ของ VM หรือปิด VM ที่ไม่ได้ใช้
4. container อื่นใน Container Manager: ตั้ง memory limit ให้ทุกตัว (Container → Edit → Resource limitation)
5. ตั้ง Hyper Backup, data scrubbing และการสร้างดัชนีรูปภาพให้ทำนอกเวลางาน
6. ถ้า **Control Panel → Hardware & Power → General** มีตัวเลือก *memory compression* ให้เปิดไว้
7. วิธีที่ได้ผลที่สุดคือเพิ่ม RAM: DS925+ มีช่อง SO-DIMM 2 ช่อง รองรับสูงสุด 32 GB (DDR4 ECC) เลือกรุ่นจาก
   compatibility list ของ Synology ใช้งานร่วมกับแอปอื่นแนะนำ 8–16 GB

ไม่แนะนำให้ปรับ `vm.swappiness` ผ่าน SSH เพราะ DSM ไม่รองรับอย่างเป็นทางการและค่าจะหายเมื่อรีบูต และอย่า mount `/tmp`
ของ container เป็น tmpfs เพราะไฟล์แนบที่กำลังอัปโหลดจะไปอยู่ใน RAM แทนดิสก์

## แก้ปัญหา

| อาการ | วิธีแก้ |
|---|---|
| ป้ายแดง "Claude ยังไม่ได้ล็อกอิน" | ทำข้อ 5 แล้วกด "ตรวจสอบอีกครั้ง" |
| งานค้างที่ "รอคิว" | ดูป้ายโควต้า และงานที่กำลังทำอยู่ (ค่าตั้งต้นทำทีละงาน) |
| งานขึ้นว่า "รอ RAM ของ NAS ว่าง" | RAM ว่างต่ำกว่า `MIN_FREE_MEM_MB` ดูข้อ 13 งานจะเริ่มเองเมื่อ RAM ว่างหรือรอครบ `MEM_WAIT_MAX_MIN` |
| งานไม่สำเร็จ "หน่วยความจำไม่พอ" | กด "ลองใหม่" เมื่อ NAS ว่างขึ้น ถ้าเกิดบ่อยให้เพิ่ม `MEM_LIMIT` (เช่น `2g`) หรือแบ่งไฟล์ใหญ่เป็นหลายงาน |
| swap ของ NAS เต็มตลอด | ทำตามข้อ 13 ฝั่ง DSM และดูหน้า **ระบบ → หน่วยความจำ** ว่างานใช้ RAM เท่าไร |
| ไฟล์แนบใหญ่ส่งไม่ได้ | ขนาดรวมเกิน `MAX_UPLOAD_MB` (ค่าตั้งต้น 200 MB) ไฟล์เสียง/วิดีโอให้ถอดเป็นข้อความก่อน |
| "Claude ไม่ได้สร้างไฟล์ผลลัพธ์" | กด "สั่งแก้แล้วทำต่อ" แล้วบอกให้เขียนไฟล์ให้ครบ |
| วางไฟล์ใน inbox แล้วไม่มีงาน | ชื่อไฟล์ห้ามขึ้นต้นด้วย `.` `@` `#` และรอประมาณ 20–30 วินาที |
| อยากดูรายละเอียดทั้งหมด | `data/jobs/<job-id>/events.jsonl` หรือ Container Manager → Container → Log |
| ไม่ได้รับแจ้งเตือน | หน้า **ระบบ** → ส่งข้อความทดสอบ จะบอกว่าช่องทางไหนส่งไม่สำเร็จและเพราะอะไร |
| Claude บอกว่ารัน python ไม่ได้ | ตรวจว่า `ALLOW_PYTHON=true` (ใช้กับงานวิศวกรรมและใบเสนอราคาเท่านั้น) |
