"use strict";

/* ---------------------------------------------------------------- helpers */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, cls = "") => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const TYPE_ICON = { "meeting-summary": "meeting", "follow-up": "clock", "sales-brief": "users",
  "proposal-draft": "briefcase", "eng-qa": "setsquare", general: "message" };
const STATUS = {
  queued: ["รอคิว", "queued"],
  running: ["กำลังทำ", "running"],
  needs_review: ["รอตรวจ", "review"],
  done: ["อนุมัติแล้ว", "done"],
  failed: ["ไม่สำเร็จ", "failed"],
  cancelled: ["ยกเลิกแล้ว", "cancelled"],
};
const SOURCES = { dashboard: "สั่งจากหน้าเว็บ", inbox: "จากโฟลเดอร์ inbox", schedule: "งานประจำ" };
const PRIORITY = { high: "สูง", medium: "กลาง", low: "ต่ำ" };
const DAYS = { mon: "จ.", tue: "อ.", wed: "พ.", thu: "พฤ.", fri: "ศ.", sat: "ส.", sun: "อา." };
const QUOTA_WINDOWS = { five_hour: "รอบ 5 ชม.", seven_day: "รายสัปดาห์", seven_day_opus: "รายสัปดาห์ (Opus)", seven_day_sonnet: "รายสัปดาห์ (Sonnet)" };
const PLACEHOLDERS = {
  general: "เช่น สรุปไฟล์แนบเป็นข้อ ๆ, ร่างอีเมลตอบลูกค้า, เปรียบเทียบ spec สองรุ่น",
  "meeting-summary": "แนบ transcript แล้วบอกเพิ่มได้ เช่น เน้นประเด็นงบประมาณ",
  "follow-up": "ไม่ต้องพิมพ์ก็ได้ Claude จะไล่ action item ที่ยังเปิดอยู่และร่างข้อความทวงงานให้",
  "sales-brief": "วัตถุประสงค์ของการพบ เช่น นำเสนอการ upgrade ตู้ MDB",
  "proposal-draft": "requirement ของลูกค้า เช่น feeder 125A 2 ชุด ส่งมอบภายใน 30 วัน",
  "eng-qa": "คำถามหรือโจทย์ เช่น เลือกขนาดสาย CV สำหรับมอเตอร์ 75 kW 400V ระยะ 80 m",
};

let viewToken = 0; // bumps on every navigation so polling loops of the old view stop
let lastStatus = null;
let jobTypesCache = null;

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || res.statusText || "เกิดข้อผิดพลาด ลองใหม่อีกครั้ง");
  return body;
}
const postJSON = (path, data = {}) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
const getJobTypes = async () => (jobTypesCache ||= await api("/api/job-types"));

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.classList.add("out"), 3600);
  setTimeout(() => el.remove(), 4000);
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("th-TH", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const fmtClock = (iso) => (iso ? new Date(iso).toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" }) : "");
const fmtDate = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString("th-TH", { day: "numeric", month: "short", year: "numeric" }) : "—");
function fmtDuration(start, end) {
  if (!start) return "";
  const s = Math.max(0, Math.round(((end ? new Date(end) : new Date()) - new Date(start)) / 1000));
  if (s < 60) return `${s} วินาที`;
  if (s < 3600) return `${Math.floor(s / 60)} นาที ${s % 60} วินาที`;
  return `${Math.floor(s / 3600)} ชม. ${Math.floor((s % 3600) / 60)} นาที`;
}
const fmtMB = (mb) => (mb == null ? "—" : mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`);
const memWaitText = (w) => `รอ RAM ของ NAS ว่าง (เหลือ ${fmtMB(w.available_mb)} ต้องการ ${fmtMB(w.need_mb)})`;
const waitText = (w) => (w.reason === "login" ? "รอเชื่อม Claude subscription (ยังไม่ได้ล็อกอิน)" : memWaitText(w));
/* the server refuses oversized uploads too (MAX_UPLOAD_MB); checking here saves sending them first */
function tooBig(list) {
  const limit = lastStatus && lastStatus.limits && lastStatus.limits.max_upload_mb;
  const total = list.reduce((n, f) => n + f.size, 0) / 1048576;
  if (!limit || total <= limit) return false;
  toast(`ไฟล์รวม ${fmtMB(total)} เกินที่ตั้งไว้ ${limit} MB ต่อครั้ง (MAX_UPLOAD_MB)`, "bad");
  return true;
}
function badge(status) {
  const [label, cls] = STATUS[status] || [status, "queued"];
  return `<span class="badge s-${cls}">${esc(label)}</span>`;
}
const fileUrl = (jobId, area, path, raw = false) =>
  `/api/jobs/${encodeURIComponent(jobId)}/files/${area}/${path.split("/").map(encodeURIComponent).join("/")}${raw ? "?raw=1" : ""}`;

/* runs fn now, then again after the delay it returns; call the returned function to run it again right away */
function poll(fn) {
  const token = viewToken;
  let timer = null;
  let busy = false;
  let again = false;
  const tick = async () => {
    if (token !== viewToken) return;
    if (busy) {
      again = true;
      return;
    }
    busy = true;
    clearTimeout(timer);
    let delay = 4000;
    try {
      delay = (await fn()) ?? delay;
    } catch (err) {
      console.error(err);
    }
    busy = false;
    if (token !== viewToken) return;
    if (again) {
      again = false;
      delay = 0;
    }
    timer = setTimeout(tick, delay);
  };
  tick();
  return tick;
}

/* the output editor while it is open: leaving the page asks first when there are unsaved changes */
let editing = null;
const unsaved = () => Boolean(editing && editing.dirty());
window.addEventListener("beforeunload", (e) => {
  if (unsaved()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

function autoGrow(textarea) {
  const fit = () => {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 320)}px`;
  };
  textarea.addEventListener("input", fit);
  fit();
}

/* ----------------------------------------- minimal, safe markdown (escaped first) */
function renderMarkdown(md) {
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let inCode = false;
  let code = [];
  let list = null;
  let table = [];
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");
  const flushList = () => {
    if (list) html += `</${list}>`;
    list = null;
  };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
    html += `<div class="table-wrap"><table>${rows
      .map((r, i) => {
        const cells = r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|");
        const tag = i === 0 ? "th" : "td";
        return `<tr>${cells.map((c) => `<${tag}>${inline(c.trim())}</${tag}>`).join("")}</tr>`;
      })
      .join("")}</table></div>`;
    table = [];
  };
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inCode) {
        html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
        code = [];
        inCode = false;
      } else {
        flushList();
        flushTable();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      code.push(line);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      flushList();
      table.push(line);
      continue;
    }
    flushTable();
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushList();
      const level = Math.min(m[1].length + 1, 6);
      html += `<h${level}>${inline(m[2])}</h${level}>`;
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      if (list !== "ul") {
        flushList();
        html += "<ul>";
        list = "ul";
      }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      if (list !== "ol") {
        flushList();
        html += "<ol>";
        list = "ol";
      }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*>\s?(.*)$/))) {
      flushList();
      html += `<blockquote>${inline(m[1])}</blockquote>`;
    } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      flushList();
      html += "<hr>";
    } else if (!line.trim()) {
      flushList();
    } else {
      flushList();
      html += `<p>${inline(line)}</p>`;
    }
  }
  if (inCode) html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
  flushList();
  flushTable();
  return html;
}

/* ---------------------------------------------------------------- status bar */
function renderStatus(s) {
  const items = [];
  const c = s.claude;
  if (!c) {
    items.push(`<span class="pill">กำลังตรวจสอบ Claude…</span>`);
  } else if (c.logged_in && c.provider === "fake-claude") {
    items.push(`<span class="pill warn" title="CLAUDE_BIN ชี้ไปที่ fake-claude"><i class="dot"></i>โหมดสาธิต · ยังไม่ได้ใช้ Claude จริง</span>`);
  } else if (c.logged_in) {
    const apiLike = /api|console/i.test(String(c.method || ""));
    const title = `Claude Code ${c.version || ""} · ${c.method || ""} · ตรวจเมื่อ ${fmtTime(c.checked_at)}`;
    items.push(`<span class="pill ${apiLike ? "warn" : "ok"}" title="${esc(title)}"><i class="dot"></i>${
      apiLike ? "ใช้ API key อยู่ ไม่ใช่ subscription" : "Claude พร้อม"}</span>`);
  } else {
    items.push(`<a class="pill bad" href="#/" title="${esc(c.error || "")}"><i class="dot"></i>${
      c.installed ? "Claude ยังไม่ได้ล็อกอิน" : "ไม่พบ Claude Code"} · ดูวิธีเชื่อม</a>`);
  }
  const q = s.quota;
  if (q && q.status) {
    const reset = q.resetsAt ? new Date(q.resetsAt > 1e12 ? q.resetsAt : q.resetsAt * 1000) : null;
    const active = !reset || reset > new Date();
    const sameDay = reset && reset.toDateString() === new Date().toDateString();
    const when = reset && (sameDay ? fmtClock(reset.toISOString()) : fmtTime(reset.toISOString()));
    const win = QUOTA_WINDOWS[q.rateLimitType] ? ` ${QUOTA_WINDOWS[q.rateLimitType]}` : "";
    const pct = typeof q.utilization === "number" ? ` ${Math.round(q.utilization <= 1 ? q.utilization * 100 : q.utilization)}%` : "";
    if (q.status === "rejected" && active) items.push(`<span class="pill bad"><i class="dot"></i>โควต้า${win}เต็ม${when ? ` · รีเซ็ต ${when}` : ""}</span>`);
    else if (q.status === "allowed_warning" && active) items.push(`<span class="pill warn" title="${esc(when ? `รีเซ็ต ${when}` : "")}"><i class="dot"></i>โควต้า${win}ใช้ไป${pct}</span>`);
    else items.push(`<span class="pill ok"><i class="dot"></i>โควต้าปกติ</span>`);
  }
  const m = s.memory;
  if (m && m.low) {
    items.push(`<a class="pill warn" href="#/system" title="งานใหม่จะรอจน RAM ว่างอย่างน้อย ${esc(fmtMB(m.need_mb))}"><i class="dot"></i>RAM NAS เหลือ ${esc(fmtMB(m.available_mb))}</a>`);
  }
  const n = s.counts || {};
  const busy = s.worker.active.length;
  if (busy || n.queued) {
    items.push(`<span class="pill ${busy ? "live" : ""}">${busy ? `<i class="dot"></i>กำลังทำ ${busy} งาน` : "ยังไม่เริ่ม"}${n.queued ? ` · คิว ${n.queued}` : ""}</span>`);
  }
  $("#status").innerHTML = items.join("");
}

const SEND_KEY = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ + Enter" : "Ctrl + Enter";
const SAVE_KEY = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ + S" : "Ctrl + S";

async function refreshStatus(reschedule = true) {
  try {
    lastStatus = await api("/api/status");
    renderStatus(lastStatus);
    renderSetup(lastStatus.claude);
    $("#taskCount").textContent = lastStatus.open_tasks || "";
  } catch (err) {
    $("#status").innerHTML = `<span class="pill bad"><i class="dot"></i>ติดต่อ NAS ไม่ได้ กำลังลองใหม่</span>`;
  }
  if (reschedule) setTimeout(refreshStatus, 5000);
}

/* Claude not connected: an inline panel on the home page, not a modal */
function renderSetup(claude) {
  const el = $("#setup");
  if (!el) return;
  const needed = claude && !claude.logged_in;
  el.hidden = !needed;
  if (!needed || el.dataset.ready) return;
  el.dataset.ready = "1";
  el.innerHTML = `
    <h2>${icon("alert")}${claude.installed ? "ยังไม่ได้เชื่อม Claude subscription" : "ไม่พบ Claude Code ใน container"}</h2>
    <p>งานที่สั่งจะรอในคิวจนกว่าจะเชื่อมเสร็จ ทำวิธีใดวิธีหนึ่ง แล้วกดตรวจสอบอีกครั้ง</p>
    <ol>
      <li><strong>ล็อกอินใน container</strong> (แนะนำ): เปิด Terminal ของ container ใน Container Manager หรือ SSH เข้า NAS แล้วรัน
        <pre>sudo docker exec -it ai-work-assistant claude auth login</pre>
        เปิดลิงก์ที่ขึ้นมาในเบราว์เซอร์ ล็อกอิน แล้วนำรหัสที่ได้มาวางใน terminal</li>
      <li><strong>ใช้ token อายุ 1 ปี</strong>: บนเครื่องที่มี Claude Code รัน <code>claude setup-token</code>
        แล้วใส่ค่าที่ได้ใน <code>CLAUDE_CODE_OAUTH_TOKEN</code> ของไฟล์ <code>.env</code> จากนั้นสั่ง Build project ใหม่</li>
    </ol>
    <p class="small">หน้าเว็บนี้ไม่ขอและไม่เก็บรหัสผ่านหรือ token ของ Claude</p>
    <button type="button" class="btn" id="recheckClaude">${icon("retry")}ตรวจสอบการเชื่อมต่ออีกครั้ง</button>`;
  $("#recheckClaude").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const c = await postJSON("/api/claude/check");
      toast(c.logged_in ? "เชื่อมต่อ Claude ได้แล้ว" : `ยังเชื่อมไม่ได้: ${c.error || "ทำตามขั้นตอนแล้วลองตรวจอีกครั้ง"}`, c.logged_in ? "" : "bad");
      if (lastStatus) renderStatus({ ...lastStatus, claude: c });
      el.dataset.ready = "";
      renderSetup(c);
    } catch (err) {
      toast(err.message, "bad");
    }
    btn.disabled = false;
  });
}

/* ---------------------------------------------------------------- home: composer + job lists */
let showAllRecent = false;
let jobQuery = "";

async function showHome() {
  const types = await getJobTypes();
  $("#view").innerHTML = `
    <section class="setup" id="setup" hidden aria-live="polite"></section>
    <section class="compose" aria-labelledby="composeTitle">
      <h1 id="composeTitle">สั่งงาน Claude</h1>
      <p class="lede">พิมพ์สิ่งที่อยากให้ทำ แนบไฟล์ได้ Claude ทำงานบน cloud ด้วย subscription ของคุณ แล้วส่งผลกลับมาให้ตรวจที่นี่</p>
      <form id="composer" class="composer" novalidate>
        <fieldset class="type-picker">
          <legend class="sr-only">ชนิดงาน</legend>
          ${types.map((t) => `
            <label class="type-option">
              <input type="radio" name="type" value="${esc(t.id)}" ${t.id === "general" ? "checked" : ""}>
              <span>${icon(TYPE_ICON[t.id] || "message")}${esc(t.label)}</span>
            </label>`).join("")}
        </fieldset>
        <p class="type-desc" id="typeDesc"></p>
        <div class="customer" id="customerField" hidden>
          <label for="cCustomer">ชื่อลูกค้า</label>
          <input id="cCustomer" type="text" autocomplete="off" placeholder="เช่น ACME (ตรงกับชื่อโฟลเดอร์ใน knowledge/customers/)" oninput="this.removeAttribute('aria-invalid')">
        </div>
        <textarea id="cMessage" rows="3" aria-labelledby="composeTitle"></textarea>
        <ul class="attachments" id="cFiles"></ul>
        <div class="composer-bar">
          <button type="button" class="btn quiet" id="cAttach">${icon("paperclip")}แนบไฟล์</button>
          <input type="file" id="cFileInput" multiple hidden>
          <span class="hint" id="cHint"></span>
          <button type="submit" class="btn primary" id="cSend">ส่งให้ Claude${icon("arrow-up")}</button>
        </div>
      </form>
    </section>
    <div class="list-tools">
      <label class="search">${icon("search")}<span class="sr-only">ค้นหางาน</span>
        <input type="search" id="jobSearch" placeholder="ค้นหางาน: ชื่องาน ลูกค้า หรือชนิดงาน" value="${esc(jobQuery)}" autocomplete="off"></label>
    </div>
    <div id="lists" class="lists" aria-busy="true">${skeletonRows()}</div>`;
  let lastJobs = null;
  $("#jobSearch").addEventListener("input", (e) => {
    jobQuery = e.target.value;
    if (lastJobs) renderLists(lastJobs);
  });
  if (lastStatus) renderSetup(lastStatus.claude);

  const form = $("#composer");
  const message = $("#cMessage");
  const files = [];
  autoGrow(message);
  const currentType = () => types.find((t) => t.id === form.elements.type.value);
  const renderFiles = () => {
    $("#cFiles").innerHTML = files.map((f, i) => `<li>${icon("file")}<span>${esc(f.name)}</span>
      <button type="button" class="icon-btn" data-remove="${i}" aria-label="เอาไฟล์ ${esc(f.name)} ออก">${icon("x")}</button></li>`).join("");
  };
  const syncType = () => {
    const t = currentType();
    message.placeholder = PLACEHOLDERS[t.id] || "";
    $("#typeDesc").textContent = t.description;
    $("#customerField").hidden = !t.fields.some((f) => f.name === "customer");
    const needsFiles = t.fields.some((f) => f.kind === "files" && f.required);
    $("#cHint").textContent = needsFiles ? "งานนี้ต้องแนบไฟล์ transcript" : `${SEND_KEY} เพื่อส่ง`;
  };
  form.addEventListener("change", (e) => { if (e.target.name === "type") syncType(); });
  syncType();

  $("#cAttach").onclick = () => $("#cFileInput").click();
  $("#cFileInput").onchange = (e) => {
    files.push(...e.target.files);
    e.target.value = "";
    renderFiles();
  };
  $("#cFiles").onclick = (e) => {
    const b = e.target.closest("[data-remove]");
    if (!b) return;
    files.splice(Number(b.dataset.remove), 1);
    renderFiles();
  };
  form.addEventListener("dragover", (e) => { e.preventDefault(); form.classList.add("dropping"); });
  form.addEventListener("dragleave", () => form.classList.remove("dropping"));
  form.addEventListener("drop", (e) => {
    e.preventDefault();
    form.classList.remove("dropping");
    files.push(...e.dataTransfer.files);
    renderFiles();
  });
  message.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) form.requestSubmit();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const t = currentType();
    const text = message.value.trim();
    const customer = $("#cCustomer").value.trim();
    for (const f of t.fields) {
      const missing = f.kind === "files" ? !files.length : f.name === "customer" ? !customer : f.name === "instructions" ? !text : false;
      if (f.required && missing) {
        toast(f.kind === "files" ? "งานนี้ต้องแนบไฟล์ก่อนส่ง" : `ใส่${f.label}ก่อนส่ง`, "bad");
        const target = f.name === "customer" ? $("#cCustomer") : f.kind === "files" ? $("#cAttach") : message;
        if (target.tagName === "INPUT") target.setAttribute("aria-invalid", "true");
        target.focus();
        return;
      }
    }
    const firstLine = text.split("\n")[0].slice(0, 90);
    const stem = files[0] ? files[0].name.replace(/\.[^.]+$/, "") : "";
    const byType = {
      general: firstLine || stem,
      "meeting-summary": stem || firstLine,
      "sales-brief": customer ? `${t.label} ${customer}` : firstLine,
      "proposal-draft": customer ? `${t.label} ${customer}` : firstLine,
      "eng-qa": firstLine || stem,
      "follow-up": `${t.label} ${new Date().toLocaleDateString("th-TH", { day: "numeric", month: "short" })}`,
    };
    const title = byType[t.id] || firstLine || stem || t.label;
    if (tooBig(files)) return;
    const fd = new FormData();
    fd.append("type", t.id);
    fd.append("title", title);
    fd.append("instructions", text);
    fd.append("customer", customer);
    files.forEach((f) => fd.append("files", f, f.name));
    const send = $("#cSend");
    send.disabled = true;
    send.classList.add("busy");
    try {
      const job = await api("/api/jobs", { method: "POST", body: fd });
      location.hash = `#/job/${encodeURIComponent(job.id)}`;
    } catch (err) {
      toast(err.message, "bad");
      send.disabled = false;
      send.classList.remove("busy");
    }
  });

  poll(async () => {
    const jobs = await api("/api/jobs");
    lastJobs = jobs;
    renderLists(jobs);
    return jobs.some((j) => j.status === "running" || j.status === "queued") ? 2000 : 6000;
  });
}

function skeletonRows(n = 4) {
  return `<span class="sr-only">กำลังโหลดรายการงาน</span><ul class="skeleton" aria-hidden="true">${
    "<li><span></span><span></span><span></span></li>".repeat(n)}</ul>`;
}

function jobRow(j) {
  let extra = "";
  if (j.status === "running" && j.last_step) extra = `<span class="job-live">${esc(j.last_step)}</span>`;
  else if (j.status === "queued" && j.not_before) extra = `<span class="job-live">รอโควต้าถึง ${esc(fmtTime(j.not_before))}</span>`;
  else if (j.status === "queued" && j.wait) extra = `<span class="job-live">${esc(waitText(j.wait))}</span>`;
  else if ((j.status === "failed" || j.status === "cancelled") && j.error) extra = `<span class="job-error">${esc(j.error)}</span>`;
  const took = j.started_at ? ` · ${fmtDuration(j.started_at, j.status === "running" ? null : j.ended_at)}` : "";
  return `<li><a class="job" href="#/job/${encodeURIComponent(j.id)}">
      <span class="job-icon">${icon(TYPE_ICON[j.type] || "message")}</span>
      <span class="job-body">
        <span class="job-title">${esc(j.title)}</span>
        <span class="job-meta">${esc(j.type_label)} · ${esc(fmtTime(j.created_at))}${j.source !== "dashboard" ? ` · ${esc(SOURCES[j.source] || j.source)}` : ""}${took}</span>
        ${extra}
      </span>
      ${badge(j.status)}
    </a></li>`;
}

function renderLists(jobs) {
  const el = $("#lists");
  if (!el) return;
  el.removeAttribute("aria-busy");
  const q = jobQuery.trim().toLowerCase();
  if (q && jobs.length) {
    const hits = jobs.filter((j) => [j.title, j.type_label, j.params && j.params.customer].filter(Boolean).join(" ").toLowerCase().includes(q));
    el.innerHTML = hits.length
      ? `<section class="group" aria-label="ผลการค้นหา"><h2>ผลการค้นหา <span class="group-count">${hits.length}</span></h2>
          <ul class="job-list">${hits.map(jobRow).join("")}</ul></section>`
      : `<div class="empty"><h2>ไม่พบงานที่ตรงกับ “${esc(jobQuery.trim())}”</h2><p>ลองค้นด้วยชื่อลูกค้า ชื่อไฟล์ หรือชนิดงาน เช่น สรุปประชุม</p></div>`;
    return;
  }
  if (!jobs.length) {
    el.innerHTML = `<div class="empty">
      <h2>ยังไม่มีงาน</h2>
      <p>พิมพ์สั่งงานในกล่องด้านบนได้เลย หรือวางไฟล์ transcript ลงโฟลเดอร์ <code>inbox/meeting</code> บน NAS แล้วระบบจะสร้างงานสรุปประชุมให้เอง</p>
    </div>`;
    return;
  }
  const review = jobs.filter((j) => j.status === "needs_review");
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued");
  const recent = jobs.filter((j) => !review.includes(j) && !active.includes(j));
  const shown = showAllRecent ? recent : recent.slice(0, 12);
  const section = (title, list, note = "") => (list.length ? `
    <section class="group" aria-label="${esc(title)}">
      <h2>${esc(title)} <span class="group-count">${list.length}</span></h2>${note}
      <ul class="job-list">${list.map(jobRow).join("")}</ul>
    </section>` : "");
  el.innerHTML =
    section("รอคุณตรวจ", review, `<p class="group-note">อ่านผลงานแล้วกดอนุมัติ หรือพิมพ์บอก Claude ให้แก้</p>`) +
    section("กำลังทำ", active) +
    section("ล่าสุด", shown) +
    (recent.length > shown.length ? `<button type="button" class="btn quiet more" id="showAll">ดูงานทั้งหมด (${recent.length})</button>` : "");
  const more = $("#showAll");
  if (more) more.onclick = () => { showAllRecent = true; renderLists(jobs); };
}

/* ---------------------------------------------------------------- job: conversation + output */
function showJob(id) {
  $("#view").innerHTML = `
    <a class="back" href="#/">${icon("chevron-left")}งานทั้งหมด</a>
    <header class="job-head" id="jobHead" aria-busy="true">${skeletonRows(1)}</header>
    <div class="job-grid">
      <section class="convo" aria-label="บทสนทนากับ Claude">
        <ol class="thread" id="thread"></ol>
        <div class="live" id="live" aria-live="polite"></div>
        <label for="replyText" class="reply-label">ข้อความถึง Claude</label>
        <form class="reply" id="reply" novalidate>
          <ul class="attachments" id="replyFiles"></ul>
          <div class="reply-row">
            <button type="button" class="icon-btn" id="replyAttach" aria-label="แนบไฟล์">${icon("paperclip")}</button>
            <input type="file" id="replyFileInput" multiple hidden>
            <textarea id="replyText" rows="2"></textarea>
            <button type="submit" class="btn primary" id="replySend" aria-label="ส่งข้อความ">${icon("arrow-up")}</button>
          </div>
        </form>
        <p class="reply-hint" id="replyHint"></p>
      </section>
      <section class="output" aria-labelledby="outTitle">
        <div class="output-head"><h2 id="outTitle">ผลลัพธ์</h2><div class="tabs" id="outTabs" role="tablist"></div>
          <div class="out-tools" id="outTools"></div></div>
        <div class="sheet" id="outView"><div class="doc"><p class="muted">ไฟล์ผลลัพธ์จะแสดงที่นี่เมื่อ Claude เขียนเสร็จ</p></div></div>
        <div class="files" id="inFiles"></div>
      </section>
    </div>`;

  const steps = [];
  const openGroups = new Set();
  let after = 0;
  let headSig = "";
  let filesSig = "";
  let selected = null;
  let selectedSig = "";
  let outputs = [];
  let job = null;
  let rendered = false;

  const reply = $("#reply");
  const replyText = $("#replyText");
  autoGrow(replyText);
  replyText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) reply.requestSubmit();
  });
  const replyFiles = [];
  const renderReplyFiles = () => {
    $("#replyFiles").innerHTML = replyFiles.map((f, i) => `<li>${icon("file")}<span>${esc(f.name)}</span>
      <button type="button" class="icon-btn" data-remove="${i}" aria-label="เอาไฟล์ ${esc(f.name)} ออก">${icon("x")}</button></li>`).join("");
  };
  $("#replyAttach").onclick = () => $("#replyFileInput").click();
  $("#replyFileInput").onchange = (e) => { replyFiles.push(...e.target.files); e.target.value = ""; renderReplyFiles(); };
  $("#replyFiles").onclick = (e) => {
    const b = e.target.closest("[data-remove]");
    if (b) { replyFiles.splice(Number(b.dataset.remove), 1); renderReplyFiles(); }
  };
  reply.addEventListener("dragover", (e) => { e.preventDefault(); reply.classList.add("dropping"); });
  reply.addEventListener("dragleave", () => reply.classList.remove("dropping"));
  reply.addEventListener("drop", (e) => {
    e.preventDefault();
    reply.classList.remove("dropping");
    if (!replyText.disabled) { replyFiles.push(...e.dataTransfer.files); renderReplyFiles(); }
  });
  reply.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = replyText.value.trim();
    if (!text && !replyFiles.length) return replyText.focus();
    if (unsaved()) return toast("บันทึกหรือยกเลิกที่แก้ไว้ในผลลัพธ์ก่อน แล้วค่อยส่งข้อความถึง Claude", "bad");
    if (tooBig(replyFiles)) return;
    const fd = new FormData();
    fd.append("message", text);
    replyFiles.forEach((f) => fd.append("files", f, f.name));
    const send = $("#replySend");
    send.disabled = true;
    send.classList.add("busy");
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/messages`, { method: "POST", body: fd });
      replyText.value = "";
      replyText.dispatchEvent(new Event("input"));
      replyFiles.length = 0;
      renderReplyFiles();
      headSig = "";
      if (editing) closeEditor(false);
    } catch (err) {
      toast(err.message, "bad");
    }
    send.classList.remove("busy");
    send.disabled = false;
  });
  $("#thread").addEventListener("toggle", (e) => {
    const key = e.target.dataset && e.target.dataset.group;
    if (key) e.target.open ? openGroups.add(key) : openGroups.delete(key);
  }, true);

  const selectOutput = (path) => {
    selected = path;
    $("#outTabs").innerHTML = outputs.map((f) => `<button type="button" role="tab" aria-selected="${f.path === selected}"
      data-path="${esc(f.path)}">${esc(f.path)}</button>`).join("");
    if (editing) return; // the editor stays until you save or cancel
    const cur = outputs.find((f) => f.path === selected);
    const sig = cur ? `${cur.modified}|${cur.size}|${job.status}|${job.rev}` : "";
    if (cur && sig !== selectedSig) {
      selectedSig = sig;
      loadOutput(id, cur, job, startEdit);
    }
  };
  /* your own edit of a result that waits for review: saved straight to the file, no Claude run */
  const startEdit = (file, f) => openEditor(id, file, job, f, (saved, newJob) => closeEditor(saved, newJob));
  const closeEditor = (saved, newJob) => {
    editing = null;
    if (newJob) job = newJob;
    selectedSig = "";
    selectOutput(selected);
    if (saved) {
      headSig = "";
      refresh();
    }
  };
  $("#outTabs").onclick = (e) => {
    const b = e.target.closest("button[data-path]");
    if (!b || (editing && b.dataset.path === selected)) return;
    if (unsaved() && !confirm("ยังไม่ได้บันทึกที่แก้ไว้ ทิ้งที่แก้แล้วเปิดไฟล์อื่นหรือไม่?")) return;
    editing = null;
    selectedSig = "";
    selectOutput(b.dataset.path);
  };

  const refresh = poll(async () => {
    const data = await api(`/api/jobs/${encodeURIComponent(id)}?after=${after}`);
    job = data.job;
    const running = job.status === "running" || job.status === "queued";

    const sig = `${job.status}|${job.updated_at}`;
    const changed = sig !== headSig;
    if (changed) {
      headSig = sig;
      $("#jobHead").removeAttribute("aria-busy");
      renderJobHead(job);
      renderReplyState(job);
    }

    if (data.steps.length) {
      steps.push(...data.steps);
      renderThread(steps, job, openGroups, rendered && running);
      rendered = true;
    }
    after = data.next;
    renderLive(job, data.partial_text, steps);

    const order = ((data.type && data.type.outputs) || []).map((o) => o.replace(/^output\//, ""));
    const rank = (p) => (order.includes(p) ? order.indexOf(p) : 99);
    outputs = data.files.output.slice().sort((a, b) => rank(a.path) - rank(b.path) || a.path.localeCompare(b.path));
    const fsig = JSON.stringify(data.files);
    if (fsig !== filesSig || changed) {
      if (outputs.length) selectOutput(outputs.some((f) => f.path === selected) ? selected : outputs[0].path);
      if (fsig !== filesSig) renderFiles(id, data.files);
      filesSig = fsig;
    }
    return running ? 1200 : 8000;
  });
}

function renderThread(steps, job, openGroups, autoscroll = false) {
  const thread = $("#thread");
  const nearBottom = autoscroll && window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
  const items = [];
  let group = null;
  steps.forEach((st, i) => {
    if (st.kind === "user") {
      group = null;
      items.push(`<li class="msg user"><div class="bubble">${esc(st.text).replace(/\n/g, "<br>")}${
        st.files && st.files.length ? `<ul class="msg-files">${st.files.map((f) => `<li>${icon("file")}${esc(f)}</li>`).join("")}</ul>` : ""}</div>
        <span class="msg-time">${esc(fmtClock(st.t))}</span></li>`);
    } else if (st.kind === "text") {
      group = null;
      items.push(`<li class="msg claude"><span class="who">Claude</span><div class="bubble doc-inline">${renderMarkdown(st.text)}</div>
        <span class="msg-time">${esc(fmtClock(st.t))}</span></li>`);
    } else if (st.kind === "status") {
      group = null;
      const ic = st.level === "ok" ? "check" : st.level === "error" ? "x" : "alert";
      items.push(`<li class="status-line ${esc(st.level)}">${icon(ic)}${esc(st.text)}${
        st.detail ? `<span class="status-detail">· ${esc(st.detail)}</span>` : ""}</li>`);
    } else if (st.kind === "edit") {
      group = null;
      items.push(`<li class="status-line edit">${icon("pencil")}${esc(st.text)}<span class="status-detail">· ${esc(fmtClock(st.t))}</span></li>`);
    } else if (st.kind === "start" || st.kind === "result") {
      // model shows in the header, timing on the status line
    } else {
      if (!group) {
        group = { key: `${st.run}-${i}`, entries: [] };
        items.push(group);
      }
      group.entries.push(st);
    }
  });
  const lastGroup = items.filter((x) => typeof x !== "string").pop();
  const running = job.status === "running";
  thread.innerHTML = items.map((x) => {
    if (typeof x === "string") return x;
    const last = x.entries[x.entries.length - 1];
    const live = running && x === lastGroup;
    const open = openGroups.has(x.key) || live;
    const warn = x.entries.some((e) => e.kind === "warn" || e.kind === "error");
    return `<li class="activity${live ? " is-live" : ""}${warn ? " has-warn" : ""}">
      <details data-group="${esc(x.key)}" ${open ? "open" : ""}>
        <summary>${icon("tool")}<span class="activity-sum">${live ? "กำลังทำ" : "ทำไป"} ${x.entries.length} ขั้นตอน</span>
          <span class="activity-last">${esc(last.text)}</span></summary>
        <ol>${x.entries.map((e) => `<li class="k-${esc(e.kind)}"><span class="t">${esc(fmtClock(e.t))}</span><span>${esc(e.text)}</span></li>`).join("")}</ol>
      </details></li>`;
  }).join("");
  if (nearBottom) window.scrollTo({ top: document.body.scrollHeight });
}

function renderLive(job, partial, steps) {
  const live = $("#live");
  if (!live) return;
  if (job.status === "running" && partial) {
    live.innerHTML = `<div class="msg claude typing"><span class="who">Claude กำลังพิมพ์</span>
      <div class="bubble">${esc(partial).replace(/\n/g, "<br>")}<span class="caret" aria-hidden="true"></span></div></div>`;
  } else if (job.status === "running") {
    live.innerHTML = `<p class="working"><span class="spinner" aria-hidden="true"></span>Claude กำลังทำงาน · ${esc(fmtDuration(job.started_at))}</p>`;
  } else if (job.status === "queued") {
    live.innerHTML = job.not_before
      ? `<p class="working wait">${icon("clock")}โควต้าเต็ม จะเริ่มเองเวลา ${esc(fmtTime(job.not_before))}</p>`
      : job.wait && job.wait.reason === "login"
        ? `<p class="working wait">${icon("clock")}รอเชื่อม Claude subscription ก่อน ดูวิธีเชื่อมที่<a href="#/">หน้าแรก</a> งานจะเริ่มเองเมื่อเชื่อมได้</p>`
      : job.wait
        ? `<p class="working wait">${icon("clock")}${esc(memWaitText(job.wait))} จะเริ่มเองไม่เกิน ${esc(fmtClock(job.wait.start_by))}</p>`
        : `<p class="working wait">${icon("clock")}รอคิว Claude จะเริ่มเมื่อถึงคิว</p>`;
  } else {
    live.innerHTML = "";
  }
}

function renderReplyState(job) {
  const text = $("#replyText");
  const send = $("#replySend");
  const hints = {
    needs_review: ["บอก Claude ว่าจะให้แก้หรือทำอะไรเพิ่ม…", "พอใจแล้วกด อนุมัติ ด้านบน หรือพิมพ์สั่งแก้ได้เลย Claude จำบริบทเดิมได้"],
    done: ["ถามต่อ หรือสั่งงานต่อจากผลนี้…", "ถ้าส่งข้อความ งานนี้จะกลับไปรอตรวจอีกครั้ง"],
    failed: ["บอก Claude ว่าจะให้ทำอย่างไรต่อ…", "หรือกด ลองใหม่ เพื่อเริ่มงานนี้ตั้งแต่ต้น"],
    cancelled: ["บอก Claude ว่าจะให้ทำอย่างไรต่อ…", ""],
    running: ["Claude กำลังทำงาน ส่งข้อความได้เมื่อรอบนี้เสร็จ", ""],
    queued: ["รอคิวอยู่ ส่งข้อความได้เมื่อรอบนี้เสร็จ", ""],
  };
  const [placeholder, hint] = hints[job.status] || ["", ""];
  text.placeholder = placeholder;
  text.disabled = !job.can_message;
  send.disabled = !job.can_message;
  $("#replyAttach").disabled = !job.can_message;
  $("#replyHint").textContent = hint;
}

function renderJobHead(job) {
  const runs = job.runs || [];
  const last = runs[runs.length - 1] || {};
  const usage = last.usage || {};
  const notices = [];
  if (job.error && job.status !== "queued") notices.push(`<p class="notice error">${icon("alert")}<span>${esc(job.error)}</span></p>`);
  if (job.warnings && job.warnings.length) notices.push(`<p class="notice warn">${icon("alert")}<span>${job.warnings.map(esc).join("<br>")}</span></p>`);
  if (job.review && job.status === "done") {
    const r = job.review;
    notices.push(`<p class="notice ok">${icon("check")}<span>อนุมัติเมื่อ ${esc(fmtTime(r.at))}${
      r.tasks_added ? ` · เพิ่ม ${r.tasks_added} รายการเข้า <a href="#/tasks">ติดตามงาน</a>` : ""}${
      r.archived && r.archived.length ? `<br>เก็บไว้ที่ <code>${r.archived.map(esc).join(", ")}</code>` : ""}</span></p>`);
  }
  const meta = [
    job.type_label,
    `สร้าง ${fmtTime(job.created_at)}`,
    job.source !== "dashboard" ? SOURCES[job.source] || job.source : "",
    job.params && job.params.customer ? `ลูกค้า ${job.params.customer}` : "",
    runs.length > 1 ? `${runs.length} รอบ` : "",
    last.model || "",
    usage.num_turns ? `${usage.num_turns} turns` : "",
    last.started_at && last.ended_at ? `ใช้เวลา ${fmtDuration(last.started_at, last.ended_at)}` : "",
  ].filter(Boolean).map(esc).join('<span class="sep">·</span>');

  const actions = [];
  if (job.status === "needs_review") actions.push(`<button type="button" class="btn primary" data-act="approve">${icon("check")}อนุมัติผลงาน</button>`);
  if (job.status === "failed" || job.status === "cancelled") actions.push(`<button type="button" class="btn" data-act="retry">${icon("retry")}ลองใหม่ตั้งแต่ต้น</button>`);
  if (job.status === "queued" || job.status === "running") actions.push(`<button type="button" class="btn danger" data-act="ask-cancel">${icon("x")}ยกเลิกงาน</button>`);

  $("#jobHead").innerHTML = `
    <div class="job-head-main">
      <div class="title-row"><h1>${esc(job.title)}</h1>${badge(job.status)}</div>
      <p class="job-meta">${meta}</p>
    </div>
    <div class="actions">${actions.join("")}</div>
    ${notices.join("")}`;

  async function onAction(e) {
    const btn = e.currentTarget;
    const act = btn.dataset.act;
    const base = `/api/jobs/${encodeURIComponent(job.id)}`;
    if (act === "ask-cancel") {
      btn.closest(".actions").innerHTML = `<span class="confirm-text">หยุด Claude ตอนนี้? ไฟล์ที่เขียนไปแล้วยังอยู่</span>
        <button type="button" class="btn danger" data-act="cancel">${icon("x")}ยืนยันยกเลิกงาน</button>
        <button type="button" class="btn quiet" data-act="keep">ทำต่อ</button>`;
      $$("#jobHead [data-act]").forEach((b) => b.addEventListener("click", onAction));
      $("#jobHead [data-act=keep]").focus();
      return;
    }
    if (act === "keep") return renderJobHead(job);
    if (act === "approve" && unsaved()) return toast("บันทึกหรือยกเลิกที่แก้ไว้ก่อน แล้วค่อยอนุมัติ", "bad");
    btn.disabled = true;
    try {
      if (act === "approve") {
        await postJSON(`${base}/approve`, { note: "" });
        toast(job.type === "meeting-summary" ? "อนุมัติแล้ว เพิ่ม action item เข้ารายการติดตามงาน" : "อนุมัติแล้ว เก็บผลงานไว้ใน library");
      } else if (act === "cancel") {
        await postJSON(`${base}/cancel`);
        toast("กำลังยกเลิก…");
      } else if (act === "retry") {
        await postJSON(`${base}/retry`);
        toast("ใส่กลับเข้าคิวแล้ว");
      }
      route();
    } catch (err) {
      btn.disabled = false;
      toast(err.message, "bad");
    }
  }
  $$("#jobHead [data-act]").forEach((btn) => btn.addEventListener("click", onAction));
}

function actionItemsTable(items) {
  if (!Array.isArray(items)) return null;
  if (!items.length) return `<p class="muted">ไม่มี action item</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>งาน</th><th>ผู้รับผิดชอบ</th><th>กำหนดเสร็จ</th><th>ความสำคัญ</th></tr></thead><tbody>${items
    .map((i) => `<tr><td>${esc(i.task)}${i.notes ? `<div class="muted small">${esc(i.notes)}</div>` : ""}</td><td>${esc(i.owner || "ยังไม่ระบุ")}</td>
      <td class="num">${esc(i.due ? fmtDate(i.due) : "ยังไม่กำหนด")}</td><td>${esc(PRIORITY[i.priority] || i.priority || "")}</td></tr>`)
    .join("")}</tbody></table></div>`;
}

async function loadOutput(jobId, file, job, onEdit) {
  const view = $("#outView");
  if (!view) return;
  const path = file.path;
  const tools = $("#outTools");
  try {
    const f = await api(fileUrl(jobId, "output", path));
    if (editing) return; // opened while this file was loading
    const canEdit = Boolean(f.editable && onEdit && job && job.status === "needs_review");
    if (tools) {
      tools.innerHTML = canEdit ? `<button type="button" class="btn" id="editOut">${icon("pencil")}แก้ไขเอง</button>` : "";
      if (canEdit) $("#editOut").onclick = () => onEdit(file, f);
    }
    // the title block: what an engineer reads first on any drawing sheet
    const block = `<dl class="title-block">
        <div><dt>ไฟล์</dt><dd>${esc(path)}</dd></div>
        <div><dt>งาน</dt><dd>${esc(job ? job.title : "")}</dd></div>
        <div class="rev"><dt>Rev.</dt><dd>${esc(job ? job.rev || 1 : 1)}</dd></div>
        <div><dt>แก้ไขล่าสุด</dt><dd class="num">${esc(fmtTime(file.modified))}</dd></div>
        <div class="path"><code>jobs/${esc(jobId)}/output/${esc(path)}</code>
          <span class="doc-links">${/\.(md|txt)$/i.test(path) || path.endsWith("action_items.json")
            ? `<a href="/api/jobs/${encodeURIComponent(jobId)}/export/${path.split("/").map(encodeURIComponent).join("/")}" download>${icon("word")}ดาวน์โหลด Word</a>` : ""}
          <a href="${fileUrl(jobId, "output", path, true)}" target="_blank" rel="noopener">${icon("download")}เปิดไฟล์</a></span></div>
      </dl>`;
    if (f.binary) {
      view.innerHTML = `<div class="doc"><p>ไฟล์ชนิดนี้แสดงในหน้าเว็บไม่ได้ กดเปิดไฟล์ด้านล่างเพื่อดาวน์โหลด</p></div>${block}`;
      return;
    }
    const ext = path.split(".").pop().toLowerCase();
    let html;
    if (ext === "md") html = renderMarkdown(f.text);
    else if (ext === "json") {
      let parsed = null;
      try { parsed = JSON.parse(f.text); } catch (e) { /* show as text */ }
      html = (path.endsWith("action_items.json") && actionItemsTable(parsed))
        || `<pre>${esc(parsed !== null ? JSON.stringify(parsed, null, 2) : f.text)}</pre>`;
    } else html = `<pre>${esc(f.text)}</pre>`;
    view.innerHTML = `<article class="doc">${html}</article>${block}`;
  } catch (err) {
    if (tools) tools.innerHTML = "";
    view.innerHTML = `<div class="doc"><p class="muted">เปิดไฟล์ไม่ได้: ${esc(err.message)}</p></div>`;
  }
}

function openEditor(jobId, file, job, f, close) {
  const view = $("#outView");
  const md = /\.md$/i.test(file.path);
  $("#outTools").innerHTML = "";
  view.innerHTML = `
    <form class="editor" id="editor" novalidate>
      <div class="editor-bar">
        <p class="editor-note">${icon("pencil")}<span>แก้ <b>${esc(file.path)}</b> เอง ไม่ใช้โควต้า Claude · บันทึกแล้วเป็น Rev. ${esc((job.rev || 1) + 1)}</span></p>
        <div class="editor-actions">
          ${md ? `<button type="button" class="btn quiet" id="edPreview" aria-pressed="false">${icon("eye")}ดูตัวอย่าง</button>` : ""}
          <button type="button" class="btn quiet" id="edCancel">ยกเลิก</button>
          <button type="submit" class="btn primary" id="edSave">${icon("check")}บันทึก</button>
        </div>
      </div>
      <p class="notice error" id="edError" role="alert" hidden></p>
      <label class="sr-only" for="edText">เนื้อหาไฟล์ ${esc(file.path)}</label>
      <textarea id="edText" class="editor-text" spellcheck="false"></textarea>
      <article class="doc editor-preview" id="edPrev" hidden></article>
      <p class="editor-hint"><span class="kbd-hint">${esc(SAVE_KEY)} เพื่อบันทึก · </span>Claude จะรู้ว่าคุณแก้ไฟล์นี้เมื่อคุณส่งข้อความถึง Claude ครั้งถัดไป</p>
    </form>`;
  const form = $("#editor");
  const text = $("#edText");
  text.value = f.text;
  const original = text.value; // as the browser holds it (line breaks normalised)
  editing = { hash: location.hash, path: file.path, dirty: () => text.value !== original };

  const preview = $("#edPreview");
  if (preview) preview.onclick = () => {
    const on = preview.getAttribute("aria-pressed") !== "true";
    preview.setAttribute("aria-pressed", String(on));
    preview.innerHTML = on ? `${icon("pencil")}กลับไปแก้` : `${icon("eye")}ดูตัวอย่าง`;
    if (on) $("#edPrev").innerHTML = renderMarkdown(text.value);
    $("#edPrev").hidden = !on;
    text.hidden = on;
    if (!on) text.focus();
  };
  $("#edCancel").onclick = () => {
    if (editing && editing.dirty() && !confirm("ทิ้งที่แก้ไว้หรือไม่?")) return;
    close(false);
  };
  text.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const save = $("#edSave");
    const error = $("#edError");
    save.disabled = true;
    save.classList.add("busy");
    try {
      const res = await api(fileUrl(jobId, "output", file.path), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text.value, base_sha: f.sha }) });
      toast(res.changed ? `บันทึกแล้ว เป็น Rev. ${res.job.rev}` : "ไม่มีอะไรเปลี่ยน ไม่ได้บันทึก");
      close(res.changed, res.job);
    } catch (err) {
      error.innerHTML = `${icon("alert")}<span>${esc(err.message)}</span>`;
      error.hidden = false;
      save.disabled = false;
      save.classList.remove("busy");
    }
  });
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  form.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
  text.focus({ preventScroll: true });
  text.setSelectionRange(0, 0);
  text.scrollTop = 0;
}

function renderFiles(jobId, files) {
  const list = (area, label) => (files[area].length ? `<h3>${label}</h3><ul>${files[area]
    .map((f) => `<li><a href="${fileUrl(jobId, area, f.path, true)}" target="_blank" rel="noopener">${icon("file")}${esc(f.path)}</a>
      <span class="size">${(f.size / 1024).toFixed(1)} KB</span></li>`).join("")}</ul>` : "");
  $("#inFiles").innerHTML = list("input", "ไฟล์ที่ส่งให้ Claude") + list("context", "ข้อมูลที่ระบบเตรียมให้");
}

/* ---------------------------------------------------------------- follow-up list */
let showDone = false;
let taskItems = [];

function showTasks() {
  const f = lastStatus && lastStatus.followup;
  const days = f ? f.days.join(",") : "";
  const dayText = days === "mon,tue,wed,thu,fri" ? "วันจันทร์ถึงศุกร์" : days === "mon,tue,wed,thu,fri,sat,sun" ? "วัน" : f ? f.days.map((d) => DAYS[d] || d).join(" ") : "";
  const schedule = f && f.time ? ` ทุก${dayText} เวลา ${f.time}` : " เมื่อตั้งค่า FOLLOWUP_TIME";
  $("#view").innerHTML = `
    <section class="tasks" aria-labelledby="tasksTitle">
      <div class="page-head">
        <div><h1 id="tasksTitle">ติดตามงาน</h1>
        <p class="lede">action item จากประชุมที่อนุมัติแล้ว และรายการที่คุณเพิ่มเอง ระบบให้ Claude ไล่ดูและร่างข้อความทวงงาน${esc(schedule)}</p></div>
        <div class="head-actions">
          <button type="button" class="btn" id="calOpen" aria-expanded="false" aria-controls="calPanel">${icon("calendar")}ปฏิทิน</button>
          <button type="button" class="btn primary" id="taskAdd">${icon("plus")}เพิ่มรายการ</button>
        </div>
      </div>
      <div id="calPanel" hidden></div>
      <div id="taskForm"></div>
      <label class="switch"><input type="checkbox" id="showDone" ${showDone ? "checked" : ""}> แสดงรายการที่เสร็จแล้ว</label>
      <div id="taskTable" aria-busy="true">${skeletonRows(3)}</div>
    </section>`;
  $("#showDone").onchange = (e) => {
    showDone = e.target.checked;
    route();
  };
  $("#taskAdd").onclick = () => openTaskForm(null);
  $("#calOpen").onclick = () => toggleCalendar();
  poll(async () => {
    taskItems = await api(`/api/tasks?include_done=${showDone}`);
    renderTasks(taskItems);
    return 15000;
  });
}

function renderTasks(items) {
  const el = $("#taskTable");
  if (!el) return;
  el.removeAttribute("aria-busy");
  if (!items.length) {
    el.innerHTML = `<div class="empty"><h2>ยังไม่มีรายการ</h2><p>อนุมัติงานสรุปประชุมแล้ว action item จะมาอยู่ที่นี่ พร้อมกำหนดส่งและผู้รับผิดชอบ หรือกด เพิ่มรายการ เพื่อจดงานที่ต้องตามเอง</p></div>`;
    return;
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const daysLeft = (i) => (i.due ? Math.round((new Date(`${i.due}T00:00:00`) - today) / 86400000) : null);
  const groups = [
    ["เลยกำหนด", (i) => i.status !== "done" && daysLeft(i) !== null && daysLeft(i) < 0],
    ["ภายใน 7 วัน", (i) => i.status !== "done" && daysLeft(i) !== null && daysLeft(i) >= 0 && daysLeft(i) <= 7],
    ["หลังจากนั้น", (i) => i.status !== "done" && daysLeft(i) !== null && daysLeft(i) > 7],
    ["ยังไม่มีกำหนด", (i) => i.status !== "done" && daysLeft(i) === null],
    ["เสร็จแล้ว", (i) => i.status === "done"],
  ];
  const row = (i) => {
    const done = i.status === "done";
    const d = daysLeft(i);
    const cls = done || d === null ? "" : d < 0 ? "overdue" : d <= 3 ? "soon" : "";
    const hint = done || d === null ? "" : d < 0 ? `เลยมา ${-d} วัน` : d === 0 ? "วันนี้" : `อีก ${d} วัน`;
    const source = i.source_job
      ? `<a href="#/job/${encodeURIComponent(i.source_job)}">${esc(i.source_title || i.source_job)}</a>`
      : `<span class="muted">เพิ่มเอง</span>`;
    return `<tr class="${done ? "done" : ""}">
      <td class="check"><input type="checkbox" data-id="${esc(i.id)}" ${done ? "checked" : ""} aria-label="ทำเครื่องหมายว่า ${esc(i.task)} เสร็จแล้ว"></td>
      <td class="task" data-label="งาน"><span class="task-id">${esc(i.id)}</span> ${esc(i.task)}${i.notes ? `<div class="muted small">${esc(i.notes)}</div>` : ""}</td>
      <td data-label="ผู้รับผิดชอบ">${esc(i.owner || "ยังไม่ระบุ")}</td>
      <td class="due ${cls}" data-label="กำหนดเสร็จ"><span class="num">${esc(i.due ? fmtDate(i.due) : "ยังไม่กำหนด")}</span>${hint ? ` <span class="small">(${esc(hint)})</span>` : ""}</td>
      <td data-label="ความสำคัญ"><span class="prio p-${esc(i.priority)}">${esc(PRIORITY[i.priority] || i.priority)}</span></td>
      <td class="small" data-label="ที่มา">${source}</td>
      <td class="edit"><button type="button" class="icon-btn" data-edit="${esc(i.id)}" aria-label="แก้ไข ${esc(i.id)} ${esc(i.task)}">${icon("pencil")}</button></td></tr>`;
  };
  el.innerHTML = groups.map(([title, test]) => {
    const list = items.filter(test);
    return list.length ? `<section class="group"><h2>${esc(title)} <span class="group-count">${list.length}</span></h2>
      <div class="table-wrap"><table><thead><tr><th class="check"><span class="sr-only">เสร็จ</span></th><th>งาน</th><th>ผู้รับผิดชอบ</th><th>กำหนดเสร็จ</th><th>ความสำคัญ</th><th>ที่มา</th><th class="edit"><span class="sr-only">แก้ไข</span></th></tr></thead>
      <tbody>${list.map(row).join("")}</tbody></table></div></section>` : "";
  }).join("");
  $$("input[data-id]", el).forEach((cb) => cb.addEventListener("change", async () => {
    try {
      await postJSON(`/api/tasks/${encodeURIComponent(cb.dataset.id)}`, { status: cb.checked ? "done" : "open" });
      toast(cb.checked ? "ทำเครื่องหมายว่าเสร็จแล้ว" : "เปิดรายการนี้อีกครั้ง");
      refreshTasks();
    } catch (err) {
      toast(err.message, "bad");
    }
  }));
  $$("button[data-edit]", el).forEach((b) => b.addEventListener("click", () => {
    const item = taskItems.find((x) => x.id === b.dataset.edit);
    if (item) openTaskForm(item);
  }));
}

async function refreshTasks() {
  try {
    taskItems = await api(`/api/tasks?include_done=${showDone}`);
    renderTasks(taskItems);
    refreshStatus(false);
  } catch (err) {
    toast(err.message, "bad");
  }
}

const isoDay = (offset) => {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

function openTaskForm(item) {
  const holder = $("#taskForm");
  const v = item || { task: "", owner: "", due: "", priority: "medium", notes: "" };
  holder.innerHTML = `
    <form class="schedule-form task-form" id="taskFormEl" novalidate>
      <h2>${item ? `แก้ไข <span class="task-id">${esc(item.id)}</span>` : "เพิ่มรายการติดตาม"}</h2>
      <div class="field"><label for="tTask">งานที่ต้องทำ</label>
        <textarea id="tTask" rows="2" maxlength="500" placeholder="เช่น ส่งใบเสนอราคาตู้ MDB ฉบับแก้ไขให้ ACME"></textarea></div>
      <div class="field-row">
        <div class="field"><label for="tOwner">ผู้รับผิดชอบ</label>
          <input id="tOwner" type="text" maxlength="120" autocomplete="off" placeholder="เช่น คุณสมชาย (จัดซื้อ ACME)"></div>
        <div class="field"><label for="tDue">กำหนดเสร็จ</label>
          <input id="tDue" type="date">
          <div class="quick-dates" role="group" aria-label="เลือกวันเร็ว">
            <button type="button" class="btn quiet" data-day="0">วันนี้</button>
            <button type="button" class="btn quiet" data-day="1">พรุ่งนี้</button>
            <button type="button" class="btn quiet" data-day="7">อีก 7 วัน</button>
            <button type="button" class="btn quiet" data-day="">ไม่กำหนด</button>
          </div></div>
      </div>
      <div class="field"><span class="label" id="tPrioLabel">ความสำคัญ</span>
        <div class="chips" role="radiogroup" aria-labelledby="tPrioLabel">${["high", "medium", "low"].map((p) => `
          <label class="type-option"><input type="radio" name="tPrio" value="${p}"><span>${esc(PRIORITY[p])}</span></label>`).join("")}</div></div>
      <div class="field"><label for="tNotes">หมายเหตุ</label>
        <textarea id="tNotes" rows="2" maxlength="1000" placeholder="ไม่บังคับ"></textarea></div>
      ${item && item.source_job ? `<p class="small muted">ที่มา: <a href="#/job/${encodeURIComponent(item.source_job)}">${esc(item.source_title || item.source_job)}</a>
        · ที่แก้ไว้จะไม่ถูกทับ แม้อนุมัติประชุมนี้อีกครั้ง</p>` : ""}
      <div class="form-actions" id="tActions">
        <button type="submit" class="btn primary" id="tSave">${icon("check")}บันทึก</button>
        <button type="button" class="btn quiet" id="tCancel">ยกเลิก</button>
        ${item ? `<button type="button" class="btn quiet danger-text push-end" id="tDelete">${icon("trash")}ลบรายการนี้</button>` : ""}
      </div>
    </form>`;
  const form = $("#taskFormEl");
  $("#tTask").value = v.task || "";
  $("#tOwner").value = v.owner || "";
  $("#tDue").value = v.due || "";
  $("#tNotes").value = v.notes || "";
  $$("input[name=tPrio]", form).forEach((r) => (r.checked = r.value === (v.priority || "medium")));
  $$("[data-day]", form).forEach((b) => b.addEventListener("click", () => {
    $("#tDue").value = b.dataset.day === "" ? "" : isoDay(Number(b.dataset.day));
  }));
  const close = () => (holder.innerHTML = "");
  $("#tCancel").onclick = close;
  const del = $("#tDelete");
  if (del) del.onclick = () => {
    $("#tActions").innerHTML = `<span class="confirm-text">ลบ ${esc(item.id)} ถาวร?${item.source_job ? " อนุมัติประชุมนี้อีกครั้งก็จะไม่กลับมา" : ""}</span>
      <button type="button" class="btn danger" id="tDeleteYes">${icon("trash")}ยืนยันลบ</button>
      <button type="button" class="btn quiet" id="tDeleteNo">ไม่ลบ</button>`;
    $("#tDeleteNo").onclick = () => openTaskForm(item);
    $("#tDeleteYes").onclick = async (e) => {
      e.currentTarget.disabled = true;
      try {
        await api(`/api/tasks/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        toast(`ลบ ${item.id} แล้ว`);
        close();
        refreshTasks();
      } catch (err) {
        toast(err.message, "bad");
        e.currentTarget.disabled = false;
      }
    };
    $("#tDeleteNo").focus();
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const task = $("#tTask");
    if (!task.value.trim()) {
      task.setAttribute("aria-invalid", "true");
      toast("กรอกงานที่ต้องทำก่อน", "bad");
      return task.focus();
    }
    task.removeAttribute("aria-invalid");
    const body = { task: task.value.trim(), owner: $("#tOwner").value.trim() || null, due: $("#tDue").value || null,
      priority: form.elements.tPrio.value || "medium", notes: $("#tNotes").value.trim() };
    const save = $("#tSave");
    save.disabled = true;
    try {
      const saved = await api(item ? `/api/tasks/${encodeURIComponent(item.id)}` : "/api/tasks", {
        method: item ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast(item ? `บันทึก ${saved.id} แล้ว` : `เพิ่ม ${saved.id} แล้ว`);
      close();
      refreshTasks();
    } catch (err) {
      toast(err.message, "bad");
      save.disabled = false;
    }
  });
  $("#tTask").focus();
  holder.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

/* the calendar feed: a private address calendar apps can subscribe to (no dashboard password) */
async function toggleCalendar(force) {
  const panel = $("#calPanel");
  const btn = $("#calOpen");
  const open = force ?? panel.hidden;
  panel.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (!open) return;
  try {
    renderCalendar(await api("/api/calendar"));
  } catch (err) {
    toast(err.message, "bad");
  }
}

function renderCalendar(cal) {
  const panel = $("#calPanel");
  if (!panel) return;
  const url = cal.path ? `${location.origin}${cal.path}` : "";
  const webcal = url.replace(/^https?:/, "webcal:");
  panel.innerHTML = cal.enabled ? `
    <section class="cal-panel" aria-labelledby="calTitle">
      <h2 id="calTitle">${icon("calendar")}กำหนดส่งในปฏิทินของคุณ</h2>
      <p class="small">รายการที่ยังไม่เสร็จและมีกำหนดเสร็จ จะขึ้นเป็นนัดทั้งวันในปฏิทิน พร้อมเตือน 9 โมงเช้าวันก่อนกำหนด
        ติ๊กเสร็จแล้วจะหายจากปฏิทินในรอบอัปเดตถัดไป (แอปปฏิทินดึงใหม่เองทุกหนึ่งถึงหลายชั่วโมง)</p>
      <div class="cal-link"><code id="calUrl">${esc(url)}</code>
        <button type="button" class="btn" id="calCopy">${icon("copy")}คัดลอกลิงก์</button></div>
      <div class="form-actions">
        <a class="btn primary" href="${esc(webcal)}">${icon("calendar")}เปิดในแอปปฏิทิน</a>
        <a class="btn quiet" href="${esc(url)}" download="followup.ics">${icon("download")}ดาวน์โหลด .ics</a>
      </div>
      <details class="kb-notes"><summary>วิธีเพิ่มในแต่ละแอป</summary><ul>
        <li><b>iPhone / iPad / Mac</b>: กด เปิดในแอปปฏิทิน แล้วกด สมัครรับ (Subscribe)</li>
        <li><b>Outlook บน Windows</b>: ปฏิทิน › เพิ่มปฏิทิน › จากอินเทอร์เน็ต แล้ววางลิงก์</li>
        <li><b>Android</b>: ใช้แอป ICSx⁵ (ฟรี) วางลิงก์นี้ แล้วปฏิทินในเครื่องจะเห็นรายการ</li>
        <li>มือถือต้องอยู่ในวง LAN ของออฟฟิศหรือต่อ VPN เดียวกับ dashboard จึงจะอัปเดตได้
          ส่วน Google Calendar และ Outlook.com ดึงจากอินเทอร์เน็ต จึงใช้ได้เฉพาะเมื่อเปิด NAS ออกอินเทอร์เน็ต ซึ่งไม่แนะนำ</li>
        <li>ดาวน์โหลด .ics คือนำเข้าครั้งเดียว ไม่อัปเดตตาม</li>
      </ul></details>
      <p class="notice warn">${icon("alert")}<span>ใครมีลิงก์นี้ดูรายการติดตามงานได้โดยไม่ต้องใส่รหัสผ่าน อย่าส่งต่อ ถ้าลิงก์หลุด กด สร้างลิงก์ใหม่ ลิงก์เดิมจะใช้ไม่ได้ทันที</span></p>
      <div class="form-actions">
        <button type="button" class="btn quiet" data-cal="new">${icon("retry")}สร้างลิงก์ใหม่</button>
        <button type="button" class="btn quiet danger-text" data-cal="off">${icon("x")}ปิดลิงก์ปฏิทิน</button>
      </div>
    </section>` : `
    <section class="cal-panel" aria-labelledby="calTitle">
      <h2 id="calTitle">${icon("calendar")}ดูกำหนดส่งในปฏิทินมือถือหรือ Outlook</h2>
      <p class="small">สร้างลิงก์ปฏิทินส่วนตัว แล้วสมัครรับในแอปปฏิทิน รายการที่มีกำหนดเสร็จจะขึ้นข้างนัดประชุมของคุณ และอัปเดตเองเมื่อรายการเปลี่ยน
        ลิงก์นี้เปิดได้โดยไม่ต้องใส่รหัสผ่าน dashboard (แอปปฏิทินใส่รหัสผ่านไม่ได้) จึงเป็นรหัสลับยาวที่ยกเลิกได้ทุกเมื่อ</p>
      <div class="form-actions"><button type="button" class="btn primary" data-cal="new">${icon("calendar")}สร้างลิงก์ปฏิทิน</button></div>
    </section>`;
  const copy = $("#calCopy");
  if (copy) copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(url);
      toast("คัดลอกลิงก์แล้ว");
    } catch (err) { /* http:// pages may not get the clipboard: select the text instead */
      getSelection().selectAllChildren($("#calUrl"));
      toast("เลือกลิงก์ไว้แล้ว กดคัดลอก (Ctrl + C) ได้เลย");
    }
  };
  $$("[data-cal]", panel).forEach((b) => b.addEventListener("click", async () => {
    const act = b.dataset.cal;
    if (act === "new" && cal.enabled && !confirm("สร้างลิงก์ใหม่? ลิงก์เดิมจะใช้ไม่ได้ ต้องสมัครรับในแอปปฏิทินใหม่")) return;
    if (act === "off" && !confirm("ปิดลิงก์ปฏิทิน? แอปปฏิทินที่สมัครรับไว้จะดึงข้อมูลไม่ได้อีก")) return;
    b.disabled = true;
    try {
      const next = await api("/api/calendar", { method: act === "new" ? "POST" : "DELETE" });
      toast(act === "off" ? "ปิดลิงก์ปฏิทินแล้ว" : cal.enabled ? "สร้างลิงก์ใหม่แล้ว ลิงก์เดิมใช้ไม่ได้แล้ว" : "สร้างลิงก์ปฏิทินแล้ว");
      renderCalendar(next);
    } catch (err) {
      toast(err.message, "bad");
      b.disabled = false;
    }
  }));
}

/* ---------------------------------------------------------------- recurring jobs (งานประจำ) */
const DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_FULL = { mon: "จันทร์", tue: "อังคาร", wed: "พุธ", thu: "พฤหัสบดี", fri: "ศุกร์", sat: "เสาร์", sun: "อาทิตย์" };
function daysText(days) {
  const list = DAY_ORDER.filter((d) => days.includes(d));
  if (list.length === 7) return "ทุกวัน";
  if (list.join(",") === "mon,tue,wed,thu,fri") return "วันจันทร์–ศุกร์";
  if (list.length === 1) return `ทุกวัน${DAY_FULL[list[0]]}`;
  return `ทุก ${list.map((d) => DAYS[d]).join(" ")}`;
}
const SCHEDULE_PRESETS = [
  { label: "สรุปงานประจำสัปดาห์", name: "สรุปงานประจำสัปดาห์", type: "general", days: ["fri"], time: "16:00", include: ["tasks", "recent"],
    instructions: "สรุปงานของสัปดาห์นี้จากผลงานที่อนุมัติแล้วและรายการติดตามงาน: งานที่เสร็จ งานที่ค้างและติดอะไร ประเด็นที่ต้องตัดสินใจ และสิ่งที่ต้องทำสัปดาห์หน้า เขียนเป็นรายงานสั้นที่ส่งหัวหน้าได้ทันที" },
  { label: "ทบทวนลูกค้าที่ควรติดตาม", name: "ทบทวนลูกค้าที่ควรติดตาม", type: "general", days: ["mon"], time: "08:30", include: ["recent"],
    instructions: "ไล่ดูข้อมูลลูกค้าในคลังความรู้ (customers/) และผลงานที่อนุมัติใน 7 วันล่าสุด แล้วสรุปลูกค้าที่ควรติดต่อสัปดาห์นี้ เรียงตามความสำคัญ พร้อมเหตุผล สิ่งที่ควรเสนอ และร่างข้อความติดต่อสั้น ๆ" },
];
let scheduleData = null;

async function showSchedules() {
  $("#view").innerHTML = `
    <section class="schedules" aria-labelledby="schTitle">
      <div class="page-head">
        <div><h1 id="schTitle">งานประจำ</h1>
        <p class="lede">ให้ Claude ทำงานเดิมซ้ำตามวันและเวลาที่ตั้ง เช่น สรุปงานทุกวันศุกร์ ผลงานจะรอให้ตรวจเหมือนงานปกติ และใช้โควต้า subscription เหมือนกัน</p></div>
        <button type="button" class="btn primary" id="schAdd">${icon("plus")}เพิ่มงานประจำ</button>
      </div>
      <div id="schForm"></div>
      <div id="schList" aria-busy="true">${skeletonRows(2)}</div>
    </section>`;
  $("#schAdd").addEventListener("click", () => openScheduleForm(null));
  poll(async () => {
    scheduleData = await api("/api/schedules");
    renderSchedules();
    return 30000;
  });
}

function renderSchedules() {
  const el = $("#schList");
  if (!el || !scheduleData) return;
  el.removeAttribute("aria-busy");
  const { items, followup, includes } = scheduleData;
  const builtin = followup && followup.time ? `
    <li class="schedule builtin">
      <span class="job-icon">${icon("clock")}</span>
      <div class="schedule-body">
        <div class="schedule-title">ติดตามงานประจำวัน <span class="badge s-queued">ระบบ</span></div>
        <div class="job-meta">ติดตามงาน · ${esc(daysText(followup.days))} เวลา ${esc(followup.time)} · สร้างเมื่อมีรายการค้างเท่านั้น</div>
        <div class="schedule-last">ปรับเวลาได้ที่ FOLLOWUP_TIME และ FOLLOWUP_DAYS ในไฟล์ .env</div>
      </div>
    </li>` : "";
  const row = (s) => {
    const lastLines = [];
    if (s.last_job) {
      const when = s.last_job_info && s.last_job_info.at ? fmtTime(s.last_job_info.at) : s.last_job.title;
      lastLines.push(`ล่าสุด <a href="#/job/${encodeURIComponent(s.last_job.id)}">${esc(when)}</a>${s.last_job_info && s.last_job_info.manual ? " (สั่งเอง)" : ""} ${badge(s.last_job.status)}`);
    }
    if (s.last_run && s.last_run.skipped) lastLines.push(`รอบ ${esc(fmtDate(s.last_run.date))} ข้ามไป: ${esc(s.last_run.skipped)}`);
    const lastText = lastLines.join("<br>");
    const extra = (s.include || []).map((k) => includes[k] || k);
    return `<li class="schedule ${s.enabled ? "" : "off"}" data-id="${esc(s.id)}">
      <span class="job-icon">${icon(TYPE_ICON[s.type] || "message")}</span>
      <div class="schedule-body">
        <div class="schedule-title">${esc(s.name)}${s.enabled ? "" : ` <span class="badge s-cancelled">ปิดอยู่</span>`}</div>
        <div class="job-meta">${esc(s.type_label)}${s.customer ? ` · ${esc(s.customer)}` : ""} · ${esc(daysText(s.days))} เวลา ${esc(s.time)}${extra.length ? ` · แนบ${esc(extra.join(" และ "))}` : ""}</div>
        <div class="schedule-next">${s.enabled ? (s.next_run ? `ครั้งถัดไป ${esc(fmtTime(s.next_run))}` : "") : "ไม่ทำงานจนกว่าจะเปิดอีกครั้ง"}</div>
        ${lastText ? `<div class="schedule-last">${lastText}</div>` : ""}
        <div class="actions">
          <button type="button" class="btn" data-act="run">${icon("arrow-up")}รันตอนนี้</button>
          <button type="button" class="btn quiet" data-act="edit">แก้ไข</button>
          <button type="button" class="btn quiet" data-act="toggle">${s.enabled ? "ปิดชั่วคราว" : "เปิดใช้"}</button>
          <button type="button" class="btn quiet danger-text" data-act="ask-delete">ลบ</button>
        </div>
      </div>
    </li>`;
  };
  if (!items.length) {
    el.innerHTML = `${builtin ? `<ul class="schedule-list">${builtin}</ul>` : ""}
      <div class="empty"><h2>ยังไม่มีงานประจำ</h2>
        <p>กด "เพิ่มงานประจำ" แล้วเลือกตัวอย่างได้ เช่น สรุปงานทุกวันศุกร์ 16:00 หรือทบทวนลูกค้าที่ควรติดตามทุกวันจันทร์เช้า</p></div>`;
    return;
  }
  el.innerHTML = `<ul class="schedule-list">${builtin}${items.map(row).join("")}</ul>`;
  $$(".schedule[data-id] [data-act]", el).forEach((btn) => btn.addEventListener("click", onScheduleAction));
}

async function onScheduleAction(e) {
  const btn = e.currentTarget;
  const li = btn.closest(".schedule");
  const s = scheduleData.items.find((x) => x.id === li.dataset.id);
  if (!s) return;
  const act = btn.dataset.act;
  const path = `/api/schedules/${encodeURIComponent(s.id)}`;
  try {
    if (act === "run") {
      btn.disabled = true;
      const job = await postJSON(`${path}/run`);
      toast(`ส่ง "${s.name}" เข้าคิวแล้ว`);
      location.hash = `#/job/${encodeURIComponent(job.id)}`;
    } else if (act === "edit") {
      openScheduleForm(s);
    } else if (act === "toggle") {
      await api(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...s, enabled: !s.enabled }) });
      toast(s.enabled ? "ปิดงานประจำนี้ชั่วคราวแล้ว" : "เปิดใช้งานประจำนี้แล้ว");
      scheduleData = await api("/api/schedules");
      renderSchedules();
    } else if (act === "ask-delete") {
      const box = btn.closest(".actions");
      box.innerHTML = `<span class="confirm-text">ลบงานประจำนี้? งานที่สร้างไปแล้วยังอยู่</span>
        <button type="button" class="btn danger" data-act="delete">${icon("x")}ยืนยันลบ</button>
        <button type="button" class="btn quiet" data-act="keep">ไม่ลบ</button>`;
      $$("[data-act]", box).forEach((b) => b.addEventListener("click", onScheduleAction));
      $("[data-act=keep]", box).focus();
    } else if (act === "keep") {
      renderSchedules();
    } else if (act === "delete") {
      await api(path, { method: "DELETE" });
      toast("ลบงานประจำแล้ว");
      scheduleData = await api("/api/schedules");
      renderSchedules();
    }
  } catch (err) {
    toast(err.message, "bad");
    btn.disabled = false;
  }
}

async function openScheduleForm(s) {
  const types = (await getJobTypes()).filter((t) => scheduleData.types.includes(t.id));
  const v = s || { name: "", type: "general", instructions: "", customer: "", days: ["mon", "tue", "wed", "thu", "fri"], time: "08:30", include: [], enabled: true };
  const holder = $("#schForm");
  holder.innerHTML = `
    <form class="schedule-form" id="schFormEl" novalidate>
      <h2>${s ? "แก้ไขงานประจำ" : "งานประจำใหม่"}</h2>
      ${s ? "" : `<div class="presets"><span class="small muted">เริ่มจากตัวอย่าง:</span>${SCHEDULE_PRESETS.map((p, i) =>
        `<button type="button" class="btn quiet" data-preset="${i}">${icon("repeat")}${esc(p.label)}</button>`).join("")}</div>`}
      <div class="field"><label for="sName">ชื่องานประจำ</label>
        <input id="sName" type="text" maxlength="120" autocomplete="off" placeholder="เช่น สรุปงานประจำสัปดาห์"></div>
      <div class="field"><span class="label" id="sTypeLabel">ชนิดงาน</span>
        <div class="chips" role="radiogroup" aria-labelledby="sTypeLabel">${types.map((t) => `
          <label class="type-option"><input type="radio" name="sType" value="${esc(t.id)}"><span>${icon(TYPE_ICON[t.id] || "message")}${esc(t.label)}</span></label>`).join("")}</div></div>
      <div class="field" id="sCustomerField" hidden><label for="sCustomer">ชื่อลูกค้า</label>
        <input id="sCustomer" type="text" autocomplete="off" placeholder="ตรงกับชื่อโฟลเดอร์ใน knowledge/customers/"></div>
      <div class="field"><label for="sInstr">สิ่งที่ให้ Claude ทำทุกรอบ</label>
        <textarea id="sInstr" rows="4" placeholder="เช่น สรุปงานที่เสร็จและงานค้างของสัปดาห์นี้ เป็นรายงานสั้นส่งหัวหน้า"></textarea></div>
      <div class="field"><span class="label" id="sDaysLabel">วัน</span>
        <div class="chips" role="group" aria-labelledby="sDaysLabel">${DAY_ORDER.map((d) => `
          <label class="type-option"><input type="checkbox" name="sDay" value="${d}"><span>${esc(DAY_FULL[d])}</span></label>`).join("")}</div></div>
      <div class="field"><span class="label" id="sTimeLabel">เวลา</span>
        <div class="time-pick" role="group" aria-labelledby="sTimeLabel">
          <select id="sHour" aria-label="ชั่วโมง">${Array.from({ length: 24 }, (_, h) => `<option>${String(h).padStart(2, "0")}</option>`).join("")}</select>
          <span aria-hidden="true">:</span>
          <select id="sMinute" aria-label="นาที">${Array.from({ length: 12 }, (_, m) => `<option>${String(m * 5).padStart(2, "0")}</option>`).join("")}</select>
          <span class="muted small">น.</span>
        </div></div>
      <div class="field"><span class="label">ข้อมูลที่แนบให้ Claude ทุกรอบ</span>
        ${Object.entries(scheduleData.includes).map(([k, label]) => `
          <label class="check-option"><input type="checkbox" name="sInclude" value="${esc(k)}"> ${esc(label)}</label>`).join("")}</div>
      <div class="form-actions">
        <button type="submit" class="btn primary" id="sSave">${icon("check")}บันทึก</button>
        <button type="button" class="btn quiet" id="sCancel">ยกเลิก</button>
      </div>
    </form>`;
  const form = $("#schFormEl");
  const fill = (x) => {
    $("#sName").value = x.name || "";
    $("#sInstr").value = x.instructions || "";
    $("#sCustomer").value = x.customer || "";
    /* 24-hour dropdowns: a native time input follows the browser's locale and may show "04:00 PM" */
    const [hh, mm] = (x.time || "08:30").split(":");
    const minute = $("#sMinute");
    if (![...minute.options].some((o) => o.value === mm)) minute.add(new Option(mm, mm));
    $("#sHour").value = hh.padStart(2, "0");
    minute.value = mm;
    $$("input[name=sType]", form).forEach((r) => (r.checked = r.value === (x.type || "general")));
    $$("input[name=sDay]", form).forEach((c) => (c.checked = (x.days || []).includes(c.value)));
    $$("input[name=sInclude]", form).forEach((c) => (c.checked = (x.include || []).includes(c.value)));
    syncType();
  };
  const syncType = () => {
    const t = types.find((x) => x.id === (form.elements.sType.value || "general"));
    $("#sCustomerField").hidden = !(t && t.fields.some((f) => f.name === "customer"));
  };
  $$("input[name=sType]", form).forEach((r) => r.addEventListener("change", syncType));
  $$("[data-preset]", form).forEach((b) => b.addEventListener("click", () => fill(SCHEDULE_PRESETS[Number(b.dataset.preset)])));
  $("#sCancel").addEventListener("click", () => (holder.innerHTML = ""));
  fill(v);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      name: $("#sName").value.trim(),
      type: form.elements.sType.value,
      instructions: $("#sInstr").value.trim(),
      customer: $("#sCustomer").value.trim(),
      days: $$("input[name=sDay]:checked", form).map((c) => c.value),
      time: `${$("#sHour").value}:${$("#sMinute").value}`,
      include: $$("input[name=sInclude]:checked", form).map((c) => c.value),
      enabled: s ? s.enabled : true,
    };
    const save = $("#sSave");
    save.disabled = true;
    try {
      const saved = await api(s ? `/api/schedules/${encodeURIComponent(s.id)}` : "/api/schedules", {
        method: s ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast(saved.next_run ? `บันทึกแล้ว ครั้งถัดไป ${fmtTime(saved.next_run)}` : "บันทึกแล้ว");
      holder.innerHTML = "";
      scheduleData = await api("/api/schedules");
      renderSchedules();
    } catch (err) {
      toast(err.message, "bad");
      save.disabled = false;
    }
  });
  $("#sName").focus();
  holder.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

/* ---------------------------------------------------------------- system */
const CHANNELS = { synology: "Synology Chat", line: "LINE", webhook: "Webhook" };
const EVENTS = { needs_review: "งานรอตรวจ", failed: "งานไม่สำเร็จ", quota_wait: "โควต้าเต็ม" };

function quotaWindows(q) {
  if (!q) return [];
  const w = q.unifiedWindows && typeof q.unifiedWindows === "object" ? Object.entries(q.unifiedWindows) : [];
  const list = w.map(([name, v]) => ({ name, ...(v && typeof v === "object" ? v : {}) }));
  if (!list.length && (q.utilization != null || q.rateLimitType)) list.push({ name: q.rateLimitType || "current", utilization: q.utilization, resetsAt: q.resetsAt });
  return list;
}

function memorySection(m) {
  if (!m) return "";
  const h = m.host;
  const c = m.container;
  const cfg = m.settings || {};
  const meter = (label, pct, text) => {
    const level = pct == null ? "" : pct >= 92 ? "bad" : pct >= 80 ? "warn" : "ok";
    return `<li><div class="meter-head"><span>${esc(label)}</span><span class="num">${text}</span></div>
      <div class="meter ${level}" role="img" aria-label="${esc(label)} ใช้ไป ${pct ?? 0}%"><span style="width:${Math.min(100, pct ?? 0)}%"></span></div></li>`;
  };
  const meters = [];
  if (h) {
    meters.push(meter("RAM ของ NAS", h.used_pct, `ใช้ไป ${h.used_pct}% · ว่าง ${esc(fmtMB(h.available_mb))} จาก ${esc(fmtMB(h.total_mb))}`));
    if (h.swap_total_mb) meters.push(meter("Swap ของ NAS", h.swap_used_pct, `ใช้ไป ${h.swap_used_pct}% · ${esc(fmtMB(h.swap_used_mb))} จาก ${esc(fmtMB(h.swap_total_mb))}`));
  }
  if (c && c.limit_mb) meters.push(meter("Container นี้", Math.round((100 * c.used_mb) / c.limit_mb), `${esc(fmtMB(c.used_mb))} จากเพดาน ${esc(fmtMB(c.limit_mb))}`));
  const runs = m.runs || [];
  const last = runs[runs.length - 1];
  const most = runs.reduce((a, r) => Math.max(a, r.peak_mb || 0), 0);
  const row = (k, v) => `<div><dt>${esc(k)}</dt><dd>${v}</dd></div>`;
  const notes = [];
  if (m.waiting) notes.push(`<p class="notice warn">${icon("clock")}<span>${esc(memWaitText(m.waiting))} งานจะเริ่มเองไม่เกิน ${esc(fmtClock(m.waiting.start_by))}</span></p>`);
  if (h && h.swap_used_pct >= 80) {
    notes.push(`<p class="notice warn">${icon("alert")}<span>Swap ใช้ไป ${h.swap_used_pct}% ข้อมูลที่ย้ายลง swap ไปแล้วจะค้างอยู่จนกว่าจะถูกเรียกใช้ ตัวเลขนี้จึงสูงได้แม้ตอนนี้ RAM ว่าง
      ถ้า NAS ช้าลงด้วย ให้ปิดแพ็กเกจที่ไม่ได้ใช้บน DSM หรือเพิ่ม RAM (ดู docs/setup-ds925.md หัวข้อหน่วยความจำ)</span></p>`);
  }
  return `<section class="sys-section"><h2>หน่วยความจำ</h2>${notes.join("")}
    ${meters.length ? `<ul class="meters">${meters.join("")}</ul>` : `<p class="muted">อ่านข้อมูลหน่วยความจำของเครื่องนี้ไม่ได้</p>`}
    <dl class="facts">
      ${row("เริ่มงานเมื่อ RAM ว่างอย่างน้อย", cfg.min_free_mb ? `${esc(fmtMB(cfg.min_free_mb))} · รอไม่เกิน ${cfg.wait_max_min} นาที` : "ไม่ได้ตั้ง (เริ่มทันที)")}
      ${row("เพดาน RAM ของ container", c ? (c.limit_mb ? esc(fmtMB(c.limit_mb)) : `<span class="muted">ไม่ได้จำกัด (ตั้ง MEM_LIMIT ใน .env)</span>`) : "ไม่ทราบ")}
      ${row("Claude โหมดประหยัด RAM", cfg.low_memory ? "เปิด" : "ปิด")}
      ${row("RAM ที่งานใช้สูงสุด", last ? `ล่าสุด ${esc(fmtMB(last.peak_mb))} · สูงสุดใน ${runs.length} งานหลังสุด ${esc(fmtMB(most))}` : `<span class="muted">จะแสดงหลังงานแรก</span>`)}
      ${c && c.oom_kills ? row("ถูกหยุดเพราะ RAM เต็ม", `${c.oom_kills} ครั้ง ตั้งแต่ container เริ่ม`) : ""}
      ${row("ไฟล์แนบต่อครั้ง", `ไม่เกิน ${cfg.max_upload_mb} MB`)}
    </dl></section>`;
}

/* knowledge search: the same index Claude searches with `python3 -m kb_search` */
const KB_KIND = { pdf: "PDF", docx: "Word", text: "ข้อความ" };
const KB_SOURCE = { knowledge: "คลังความรู้", library: "ผลงานที่อนุมัติแล้ว" };
function knowledgeSection() {
  return `<section class="sys-section" id="kbSection"><h2>คลังความรู้</h2>
    <div id="kbStatus"><p class="small muted">กำลังโหลด…</p></div>
    <form class="kb-search" id="kbForm" role="search">
      <label class="search">${icon("search")}<span class="sr-only">ค้นในคลังความรู้</span>
        <input type="search" id="kbQuery" placeholder="ค้นใน PDF, Word และไฟล์ข้อความ เช่น แรงดันตก" autocomplete="off"></label>
      <button type="submit" class="btn">ค้น</button>
    </form>
    <div id="kbResults" aria-live="polite"></div>
    <button type="button" class="btn" id="kbRefresh">${icon("retry")}อัปเดตดัชนีตอนนี้</button></section>`;
}
function highlight(text, terms) {
  let out = esc(text);
  for (const t of [...terms].sort((a, b) => b.length - a.length)) {
    const safe = esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (safe) out = out.replace(new RegExp(safe, "gi"), (m) => `<mark>${m}</mark>`);
  }
  return out;
}
async function loadKnowledge() {
  const el = $("#kbStatus");
  if (!el) return;
  const k = await api("/api/knowledge");
  const row = (a, b) => `<div><dt>${esc(a)}</dt><dd>${b}</dd></div>`;
  const kinds = Object.entries(k.by_kind || {}).map(([x, n]) => `${KB_KIND[x] || x} ${n}`).join(" · ");
  const roots = Object.entries(k.by_root || {}).map(([x, n]) => `${KB_SOURCE[x] || x} ${n}`).join(" · ");
  const notes = k.notes || [];
  el.innerHTML = `
    ${k.enabled ? "" : `<p class="notice warn">${icon("alert")}<span>ปิดการค้นอยู่ (KB_SEARCH=false ในไฟล์ .env)</span></p>`}
    ${k.pdftotext ? "" : `<p class="notice warn">${icon("alert")}<span>ไม่พบโปรแกรม pdftotext จึงค้นในไฟล์ PDF ไม่ได้ สั่ง Build image ใหม่เพื่อติดตั้ง</span></p>`}
    <dl class="facts">
      ${row("ไฟล์ที่ค้นได้", k.files ? `${k.files} ไฟล์ · ${esc(kinds)}<br><span class="small muted">${esc(roots)}</span>` : `<span class="muted">ยังไม่มี วางเอกสารไว้ใน knowledge/ แล้วระบบจะทำดัชนีให้เองเมื่อว่าง</span>`)}
      ${row("อัปเดตล่าสุด", `${esc(fmtTime(k.updated_at) || "ยังไม่เคย")}${k.running ? " · กำลังอัปเดต…" : ""} <span class="small muted">(ตรวจไฟล์ใหม่ทุก ${k.refresh_min} นาที เมื่อไม่มีงานที่กำลังทำ)</span>`)}
    </dl>
    ${notes.length ? `<details class="kb-notes"><summary>ไฟล์ที่อ่านไม่ได้หรือได้ไม่ครบ ${notes.length} ไฟล์</summary>
      <ul>${notes.map((n) => `<li><code>${esc(n.path)}</code> ${esc(n.note)}</li>`).join("")}</ul></details>` : ""}`;
}
function wireKnowledge() {
  const form = $("#kbForm");
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("#kbQuery").value.trim();
    const out = $("#kbResults");
    if (!q) return $("#kbQuery").focus();
    out.innerHTML = `<p class="small muted">กำลังค้น…</p>`;
    try {
      const r = await api(`/api/knowledge/search?q=${encodeURIComponent(q)}`);
      out.innerHTML = r.results.length ? `<ol class="kb-results">${r.results.map((x) => `
        <li><a href="/api/knowledge/file/${x.path.split("/").map(encodeURIComponent).join("/")}" target="_blank" rel="noopener">${esc(x.rel)}</a>
          <span class="small muted">· ${esc(x.loc_label)} · ${esc(KB_SOURCE[x.source] || x.source)}</span>
          <p class="kb-excerpt">${highlight(x.excerpt, r.terms)}</p></li>`).join("")}</ol>`
        : `<p class="small muted">ไม่พบ “${esc(q)}” ลองคำที่สั้นลง หรือคำภาษาอังกฤษที่เอกสารน่าจะใช้</p>`;
    } catch (err) {
      out.innerHTML = `<p class="small muted">${esc(err.message)}</p>`;
    }
  });
  $("#kbRefresh").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try {
      const r = await postJSON("/api/knowledge/refresh");
      toast(r.busy ? "จะอัปเดตดัชนีเมื่อไม่มีงานที่กำลังทำและ RAM ว่างพอ" : "เริ่มอัปเดตดัชนีแล้ว");
    } catch (err) { toast(err.message, "bad"); }
    setTimeout(() => loadKnowledge().catch(() => {}), 1500);
  });
}

async function showSystem() {
  $("#view").innerHTML = `<section class="system" aria-labelledby="sysTitle">
      <div class="page-head"><div><h1 id="sysTitle">ระบบ</h1>
        <p class="lede">สถานะการเชื่อมต่อ Claude การใช้งาน subscription การแจ้งเตือน หน่วยความจำ ค้นคลังความรู้ และที่เก็บข้อมูลบน NAS</p></div></div>
      <div id="sysBody" aria-busy="true">${skeletonRows(3)}</div></section>`;
  const s = await api("/api/system");
  const c = s.claude || {};
  const windows = quotaWindows(s.quota);
  const pct = (u) => (typeof u === "number" ? Math.round(u <= 1 ? u * 100 : u) : null);
  const resetText = (t) => (t ? fmtTime(new Date((t > 1e12 ? t : t * 1000)).toISOString()) : "");
  const row = (k, v) => `<div><dt>${esc(k)}</dt><dd>${v}</dd></div>`;
  $("#sysBody").removeAttribute("aria-busy");
  $("#sysBody").innerHTML = `
    <section class="sys-section"><h2>Claude</h2><dl class="facts">
      ${row("สถานะ", c.logged_in ? `<span class="badge s-done">เชื่อมต่อแล้ว</span>` : `<span class="badge s-failed">ยังไม่ได้เชื่อม</span>`)}
      ${row("วิธีล็อกอิน", esc(c.method || "ยังไม่ทราบ"))}
      ${row("Claude Code", esc(c.version || "ยังไม่ทราบ"))}
      ${row("model / effort", esc(`${s.worker.model || "ค่าตั้งต้นของแพ็กเกจ"} / ${s.worker.effort || "ค่าตั้งต้น"}`))}
      ${row("ตรวจล่าสุด", esc(fmtTime(c.checked_at) || "ยังไม่ได้ตรวจ"))}
    </dl><button type="button" class="btn" id="sysCheck">${icon("retry")}ตรวจสอบการเชื่อมต่อ</button></section>

    <section class="sys-section"><h2>การใช้งาน subscription</h2>
      ${windows.length ? `<ul class="meters">${windows.map((w) => {
        const p = pct(w.utilization);
        const level = p == null ? "" : p >= 95 ? "bad" : p >= 75 ? "warn" : "ok";
        return `<li><div class="meter-head"><span>${esc(QUOTA_WINDOWS[w.name] || w.name)}</span>
          <span class="num">${p == null ? "ไม่มีข้อมูล" : `ใช้ไป ${p}%`}${w.resetsAt ? ` · รีเซ็ต ${esc(resetText(w.resetsAt))}` : ""}</span></div>
          <div class="meter ${level}" role="img" aria-label="ใช้ไป ${p ?? 0}%"><span style="width:${Math.min(100, p ?? 0)}%"></span></div></li>`;
      }).join("")}</ul><p class="small muted">ข้อมูลจาก Claude Code ตอนงานล่าสุด (${esc(fmtTime(s.quota.seen_at))})</p>`
      : `<p class="muted">ยังไม่มีข้อมูล จะแสดงหลังจาก Claude ทำงานชิ้นแรก</p>`}</section>

    <section class="sys-section"><h2>การแจ้งเตือน</h2><dl class="facts">
      ${row("ช่องทาง", s.notify.channels.length ? s.notify.channels.map((x) => esc(CHANNELS[x] || x)).join(", ") : `<span class="muted">ยังไม่ได้ตั้งค่า</span>`)}
      ${row("แจ้งเมื่อ", s.notify.on.map((x) => esc(EVENTS[x] || x)).join(", "))}
      ${row("ลิงก์ในข้อความ", s.notify.dashboard_url ? `<code>${esc(s.notify.dashboard_url)}</code>` : `<span class="muted">ตั้ง DASHBOARD_URL เพื่อให้กดเปิดงานได้</span>`)}
    </dl>${s.notify.channels.length ? `<button type="button" class="btn" id="sysNotify">${icon("bell")}ส่งข้อความทดสอบ</button>`
      : `<p class="small muted">ตั้งค่า NOTIFY_SYNOLOGY_CHAT_URL, NOTIFY_LINE_TOKEN + NOTIFY_LINE_TO หรือ NOTIFY_WEBHOOK_URL ในไฟล์ .env แล้วสั่ง Build ใหม่</p>`}</section>

    <section class="sys-section"><h2>การทำงาน</h2><dl class="facts">
      ${row("ทำพร้อมกันสูงสุด", `${s.worker.max} งาน`)}
      ${row("หมดเวลาต่องาน", `${s.worker.timeout_min} นาที`)}
      ${row("ให้ Claude รัน python3", s.worker.allow_python ? "ได้ (งานวิศวกรรมและใบเสนอราคา)" : "ไม่ได้")}
      ${row("ติดตามงานอัตโนมัติ", s.followup.time ? `ทุก ${esc(s.followup.days.map((d) => DAYS[d] || d).join(" "))} เวลา ${esc(s.followup.time)}` : "ปิดอยู่")}
    </dl></section>

    ${memorySection(s.memory)}

    ${knowledgeSection()}

    <section class="sys-section"><h2>ที่เก็บข้อมูล</h2><dl class="facts">
      ${row("โฟลเดอร์งาน", `<code>${esc(s.storage.data_dir)}</code> · ${s.storage.jobs} งาน`)}
      ${row("คลังความรู้", `<code>${esc(s.storage.knowledge_dir)}</code> · ${s.storage.knowledge_files} ไฟล์`)}
      ${row("แม่แบบ Word", s.storage.report_template ? "ใช้ templates/report-template.docx" : `<span class="muted">ใช้รูปแบบตั้งต้น (วาง templates/report-template.docx เพื่อใช้หัวกระดาษบริษัท)</span>`)}
      ${row("ฟอนต์ในไฟล์ Word", s.storage.embed_fonts ? "Anuphan ฝังในไฟล์ เปิดได้แม้เครื่องไม่มีฟอนต์" : "Anuphan (ไม่ฝัง เครื่องที่เปิดต้องติดตั้งฟอนต์)")}
      ${row("พื้นที่ว่าง", s.storage.free_gb != null ? `${s.storage.free_gb} GB จาก ${s.storage.total_gb} GB` : "ไม่ทราบ")}
      ${row("เวอร์ชัน", `AI Work Assistant ${esc(s.version.app)}`)}
    </dl></section>`;
  $("#sysCheck").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try {
      const r = await postJSON("/api/claude/check");
      toast(r.logged_in ? "เชื่อมต่อ Claude ได้แล้ว" : `ยังเชื่อมไม่ได้: ${r.error || "ดูวิธีเชื่อมที่หน้าแรก"}`, r.logged_in ? "" : "bad");
    } catch (err) { toast(err.message, "bad"); }
    route();
  });
  wireKnowledge();
  loadKnowledge().catch((err) => { const el = $("#kbStatus"); if (el) el.innerHTML = `<p class="small muted">${esc(err.message)}</p>`; });
  const notifyBtn = $("#sysNotify");
  if (notifyBtn) notifyBtn.addEventListener("click", async () => {
    notifyBtn.disabled = true;
    try {
      const r = await postJSON("/api/notify/test");
      const failed = Object.entries(r).filter(([, v]) => v !== "ok");
      toast(failed.length ? `ส่งไม่สำเร็จ: ${failed.map(([k, v]) => `${CHANNELS[k] || k} (${v})`).join(", ")}` : "ส่งข้อความทดสอบแล้ว", failed.length ? "bad" : "");
    } catch (err) { toast(err.message, "bad"); }
    notifyBtn.disabled = false;
  });
}

/* ---------------------------------------------------------------- router */
function route() {
  if (unsaved() && !confirm("ยังไม่ได้บันทึกที่แก้ไว้ ออกจากหน้านี้และทิ้งที่แก้หรือไม่?")) {
    if (location.hash !== editing.hash) history.replaceState(null, "", editing.hash);
    return;
  }
  editing = null;
  viewToken += 1;
  const hash = location.hash || "#/";
  const tab = hash.startsWith("#/tasks") ? "tasks" : hash.startsWith("#/schedules") ? "schedules" : hash.startsWith("#/system") ? "system" : "jobs";
  $$(".nav a").forEach((a) => {
    const on = a.dataset.tab === tab;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
  if (lastStatus) refreshStatus(false);
  const fail = (err) => ($("#view").innerHTML = `<p class="muted">${esc(err.message)}</p>`);
  if (hash.startsWith("#/job/")) return showJob(decodeURIComponent(hash.slice(6)));
  if (hash.startsWith("#/tasks")) return showTasks();
  if (hash.startsWith("#/schedules")) return showSchedules().catch(fail);
  if (hash.startsWith("#/system")) return showSystem().catch(fail);
  return showHome().catch(fail);
}

window.addEventListener("hashchange", route);
route();
refreshStatus();
