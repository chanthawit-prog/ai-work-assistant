"use strict";

// ---------- helpers ----------
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATUS = {
  queued: ["รอคิว", "queued"],
  running: ["กำลังทำ", "running"],
  needs_review: ["รอตรวจ", "review"],
  done: ["เสร็จแล้ว", "done"],
  failed: ["ไม่สำเร็จ", "failed"],
  cancelled: ["ยกเลิก", "cancelled"],
};
const SOURCES = { dashboard: "สร้างจากหน้าเว็บ", inbox: "จากโฟลเดอร์ inbox", schedule: "ตั้งเวลาอัตโนมัติ" };
const STEP_ICONS = { start: "🚀", text: "💬", tool: "🔧", warn: "⚠️", error: "❌", done: "✅", app: "⚙️", stderr: "🖥️" };
const DAYS = { mon: "จ.", tue: "อ.", wed: "พ.", thu: "พฤ.", fri: "ศ.", sat: "ส.", sun: "อา." };
const PRIORITY = { high: "สูง", medium: "กลาง", low: "ต่ำ" };
const QUOTA_WINDOWS = { five_hour: "รอบ 5 ชม.", seven_day: "รายสัปดาห์", seven_day_opus: "รายสัปดาห์ (Opus)", seven_day_sonnet: "รายสัปดาห์ (Sonnet)" };

let viewToken = 0; // bumps on every navigation so polling loops of the old view stop
let lastStatus = null;

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || res.statusText || "เกิดข้อผิดพลาด");
  return body;
}
const postJSON = (path, data = {}) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function toast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("th-TH", { dateStyle: "medium", timeStyle: "short" }) : "");
const fmtClock = (iso) => (iso ? new Date(iso).toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const fmtDate = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString("th-TH", { dateStyle: "medium" }) : "—");
function fmtDuration(start, end) {
  if (!start) return "";
  const s = Math.max(0, Math.round(((end ? new Date(end) : new Date()) - new Date(start)) / 1000));
  if (s < 60) return `${s} วินาที`;
  if (s < 3600) return `${Math.floor(s / 60)} นาที ${s % 60} วินาที`;
  return `${Math.floor(s / 3600)} ชม. ${Math.floor((s % 3600) / 60)} นาที`;
}
function badge(status) {
  const [label, cls] = STATUS[status] || [status, "queued"];
  return `<span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span>`;
}
const fileUrl = (jobId, area, path, raw = false) =>
  `/api/jobs/${encodeURIComponent(jobId)}/files/${area}/${path.split("/").map(encodeURIComponent).join("/")}${raw ? "?raw=1" : ""}`;

function poll(fn) {
  const token = viewToken;
  const tick = async () => {
    if (token !== viewToken) return;
    let delay = 4000;
    try {
      delay = (await fn()) ?? delay;
    } catch (err) {
      console.error(err);
    }
    if (token === viewToken) setTimeout(tick, delay);
  };
  tick();
}

// ---------- minimal, safe markdown (input is escaped first) ----------
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

// ---------- header chips ----------
function renderChips(s) {
  const chips = [];
  const c = s.claude;
  if (!c) {
    chips.push(`<span class="chip"><span class="dot"></span>กำลังตรวจสอบ Claude…</span>`);
  } else if (c.logged_in && c.provider === "fake-claude") {
    chips.push(`<span class="chip warn" title="CLAUDE_BIN ชี้ไปที่ fake-claude"><span class="dot"></span>โหมดสาธิต · ยังไม่ได้ใช้ Claude จริง</span>`);
  } else if (c.logged_in) {
    const apiLike = /api|console/i.test(String(c.method || ""));
    const title = `Claude Code ${c.version || ""} · ${c.method || ""} · ตรวจเมื่อ ${fmtTime(c.checked_at)}`;
    chips.push(`<span class="chip ${apiLike ? "warn" : "ok"}" title="${esc(title)}"><span class="dot"></span>${
      apiLike ? "Claude พร้อม · ใช้ API key (ไม่ใช่ subscription)" : "Claude พร้อม · subscription"}</span>`);
  } else {
    chips.push(`<span class="chip bad" data-action="login-help" title="${esc(c.error || "")}"><span class="dot"></span>${
      c.installed ? "Claude ยังไม่ได้ล็อกอิน" : "ไม่พบ Claude Code"} · ดูวิธีแก้</span>`);
  }
  const q = s.quota;
  if (q && q.status) {
    const reset = q.resetsAt ? new Date(q.resetsAt > 1e12 ? q.resetsAt : q.resetsAt * 1000) : null;
    const active = !reset || reset > new Date();
    const sameDay = reset && reset.toDateString() === new Date().toDateString();
    const when = reset && (sameDay
      ? reset.toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" })
      : reset.toLocaleString("th-TH", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }));
    const resetText = when ? ` · รีเซ็ต ${when}` : "";
    const windowName = QUOTA_WINDOWS[q.rateLimitType] ? ` ${QUOTA_WINDOWS[q.rateLimitType]}` : "";
    const pct = typeof q.utilization === "number" ? ` ${Math.round(q.utilization <= 1 ? q.utilization * 100 : q.utilization)}%` : "";
    if (q.status === "rejected" && active) chips.push(`<span class="chip bad"><span class="dot"></span>โควต้า${windowName}เต็ม${resetText}</span>`);
    else if (q.status === "allowed_warning" && active) chips.push(`<span class="chip warn"><span class="dot"></span>โควต้า${windowName}ใกล้เต็ม${pct}${resetText}</span>`);
    else chips.push(`<span class="chip ok"><span class="dot"></span>โควต้าปกติ</span>`);
  }
  const n = s.counts || {};
  chips.push(`<span class="chip"><span class="dot"></span>กำลังทำ ${s.worker.active.length}/${s.worker.max} · คิว ${n.queued || 0} · รอตรวจ ${n.needs_review || 0}</span>`);
  $("#chips").innerHTML = chips.join("");
}

async function refreshStatus() {
  try {
    lastStatus = await api("/api/status");
    renderChips(lastStatus);
    $("#taskCount").textContent = lastStatus.open_tasks || "";
  } catch (err) {
    $("#chips").innerHTML = `<span class="chip bad"><span class="dot"></span>ติดต่อ NAS ไม่ได้</span>`;
  }
  setTimeout(refreshStatus, 5000);
}

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-action=login-help]")) $("#loginHelp").showModal();
});
$("#recheckClaude").addEventListener("click", async () => {
  try {
    const c = await postJSON("/api/claude/check");
    toast(c.logged_in ? "เชื่อมต่อ Claude ได้แล้ว" : "ยังไม่ได้ล็อกอิน: " + (c.error || "ลองใหม่อีกครั้ง"));
    if (lastStatus) renderChips({ ...lastStatus, claude: c });
  } catch (err) {
    toast(err.message);
  }
});

// ---------- jobs list ----------
let jobFilter = "";
const FILTERS = [["", "ทั้งหมด"], ["running", "กำลังทำ"], ["queued", "รอคิว"], ["needs_review", "รอตรวจ"], ["done", "เสร็จแล้ว"], ["failed", "ไม่สำเร็จ"]];

function showJobs() {
  $("#view").innerHTML = `
    <div class="filters">${FILTERS.map(([v, l]) => `<button data-f="${v}" class="${v === jobFilter ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="jobList" class="list"><p class="muted">กำลังโหลด…</p></div>`;
  $("#view .filters").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    jobFilter = b.dataset.f;
    route();
  });
  poll(async () => {
    const jobs = await api(`/api/jobs${jobFilter ? `?status=${jobFilter}` : ""}`);
    renderJobList(jobs);
    return jobs.some((j) => j.status === "running" || j.status === "queued") ? 2000 : 6000;
  });
}

function renderJobList(jobs) {
  const el = $("#jobList");
  if (!el) return;
  if (!jobs.length) {
    el.innerHTML = `<div class="empty panel">ยังไม่มีงาน${jobFilter ? "ในสถานะนี้" : ""}<br><br>
      <a class="btn primary" href="#/new">➕ สร้างงาน</a>
      <p class="small">หรือวางไฟล์ transcript ลงโฟลเดอร์ <code>inbox/meeting</code> บน NAS แล้วระบบจะสร้างงานสรุปประชุมให้เอง</p></div>`;
    return;
  }
  el.innerHTML = jobs
    .map((j) => {
      let extra = "";
      if (j.status === "running" && j.last_step) extra = `<div class="live">⏳ ${esc(j.last_step)}</div>`;
      else if (j.status === "queued" && j.not_before) extra = `<div class="live">รอโควต้าถึง ${esc(fmtTime(j.not_before))}</div>`;
      else if ((j.status === "failed" || j.status === "cancelled") && j.error) extra = `<div class="err">${esc(j.error)}</div>`;
      const took = j.started_at ? ` · ใช้เวลา ${fmtDuration(j.started_at, j.status === "running" ? null : j.ended_at)}` : "";
      return `<a class="job" href="#/job/${encodeURIComponent(j.id)}">
        <span class="icon">${j.icon}</span>
        <div><div class="title">${esc(j.title)}</div>
          <div class="meta">${esc(j.type_label)} · ${esc(fmtTime(j.created_at))} · ${esc(SOURCES[j.source] || j.source)}${took}</div>${extra}</div>
        ${badge(j.status)}</a>`;
    })
    .join("");
}

// ---------- job detail ----------
function showJob(id) {
  $("#view").innerHTML = `
    <a class="back" href="#/">← งานทั้งหมด</a>
    <div id="jobHead" class="panel"><p class="muted">กำลังโหลด…</p></div>
    <div class="grid2">
      <section class="panel"><h2>ความคืบหน้า (สดจาก Claude)</h2>
        <ul id="timeline" class="timeline"><li class="run-sep" data-placeholder>ยังไม่เริ่ม — งานอยู่ในคิว</li></ul>
        <div id="working"></div></section>
      <section>
        <div class="panel"><h2>ผลลัพธ์</h2><div id="outTabs" class="filetabs"></div>
          <div id="outView" class="doc"><p class="muted">ยังไม่มีไฟล์ผลลัพธ์</p></div></div>
        <div class="panel"><h2>ไฟล์ input และข้อมูลประกอบ</h2><div id="inFiles"></div></div>
      </section>
    </div>`;
  let after = 0;
  let lastRun = null;
  let headSig = "";
  let filesSig = "";
  let selected = null;
  let selectedSig = "";

  const selectOutput = (outputs, path) => {
    selected = path;
    $("#outTabs").innerHTML = outputs
      .map((f) => `<button data-path="${esc(f.path)}" class="${f.path === selected ? "active" : ""}">${esc(f.path)}</button>`)
      .join("");
    const cur = outputs.find((f) => f.path === selected);
    const sig = cur ? `${cur.modified}|${cur.size}` : "";
    if (cur && sig !== selectedSig) {
      selectedSig = sig;
      loadOutput(id, cur.path);
    }
  };

  poll(async () => {
    const data = await api(`/api/jobs/${encodeURIComponent(id)}?after=${after}`);
    const job = data.job;

    const sig = `${job.status}|${job.updated_at}`;
    if (sig !== headSig) {
      headSig = sig;
      renderJobHead(job);
    }

    const tl = $("#timeline");
    if (data.steps.length) {
      const nearBottom = tl.scrollHeight - tl.scrollTop - tl.clientHeight < 60;
      tl.querySelector("[data-placeholder]")?.remove();
      for (const st of data.steps) {
        if (st.run !== lastRun) {
          lastRun = st.run;
          tl.insertAdjacentHTML("beforeend", `<li class="run-sep">— รอบที่ ${esc(st.run)} —</li>`);
        }
        tl.insertAdjacentHTML("beforeend", `<li class="k-${esc(st.kind)}"><span>${STEP_ICONS[st.kind] || "•"}</span>
          <div><div class="body">${esc(st.text)}</div><div class="when">${esc(fmtClock(st.t))}</div></div></li>`);
      }
      if (nearBottom) tl.scrollTop = tl.scrollHeight;
    }
    after = data.next;
    $("#working").innerHTML = job.status === "running"
      ? `<div class="working"><span class="spinner"></span>Claude กำลังทำงาน… ${esc(fmtDuration(job.started_at))}</div>` : "";

    const order = ((data.type && data.type.outputs) || []).map((o) => o.replace(/^output\//, ""));
    const rank = (p) => (order.includes(p) ? order.indexOf(p) : 99);
    const outputs = data.files.output.slice().sort((a, b) => rank(a.path) - rank(b.path) || a.path.localeCompare(b.path));
    const fsig = JSON.stringify(data.files);
    if (fsig !== filesSig) {
      filesSig = fsig;
      if (outputs.length) {
        $("#outTabs").onclick = (e) => {
          const b = e.target.closest("button[data-path]");
          if (b) { selectedSig = ""; selectOutput(outputs, b.dataset.path); }
        };
        selectOutput(outputs, outputs.some((f) => f.path === selected) ? selected : outputs[0].path);
      }
      renderInputs(id, data.files);
    }
    return job.status === "running" || job.status === "queued" ? 1500 : 8000;
  });
}

function renderJobHead(job) {
  const runs = job.runs || [];
  const last = runs[runs.length - 1] || {};
  const usage = last.usage || {};
  const notices = [];
  if (job.status === "queued" && job.not_before)
    notices.push(`<div class="notice info">⏳ โควต้า subscription เต็ม ระบบจะเริ่มงานนี้เองเวลา ${esc(fmtTime(job.not_before))}</div>`);
  else if (job.error) notices.push(`<div class="notice error">${esc(job.error)}</div>`);
  if (job.warnings && job.warnings.length)
    notices.push(`<div class="notice warn">${job.warnings.map((w) => `⚠️ ${esc(w)}`).join("<br>")}</div>`);
  if (last.result && (job.status === "needs_review" || job.status === "done"))
    notices.push(`<div class="notice info"><b>ข้อความจาก Claude:</b><br>${esc(last.result).replace(/\n/g, "<br>")}</div>`);
  if (job.review) {
    const r = job.review;
    notices.push(`<div class="notice info">✅ อนุมัติเมื่อ ${esc(fmtTime(r.at))}${r.archived && r.archived.length ? ` · เก็บไว้ที่ <code>${r.archived.map(esc).join(", ")}</code>` : ""}${
      r.tasks_added ? ` · เพิ่ม ${r.tasks_added} รายการเข้า <a href="#/tasks">ติดตามงาน</a>` : ""}</div>`);
  }
  const meta = [
    `สร้างเมื่อ ${fmtTime(job.created_at)}`,
    SOURCES[job.source] || job.source,
    job.params && job.params.customer ? `ลูกค้า: ${job.params.customer}` : "",
    runs.length ? `ทำไปแล้ว ${runs.length} รอบ` : "",
    last.model ? `model ${last.model}` : "",
    usage.num_turns ? `${usage.num_turns} turns` : "",
    last.started_at ? `ใช้เวลา ${fmtDuration(last.started_at, last.ended_at)}` : "",
  ].filter(Boolean).map(esc).join(" · ");

  const buttons = [];
  if (job.status === "needs_review") {
    buttons.push(`<button class="btn good" data-act="approve">✅ อนุมัติ</button>`);
    buttons.push(`<button class="btn" data-act="revise">✏️ ขอแก้ไข</button>`);
  }
  if (job.status === "failed") {
    buttons.push(`<button class="btn primary" data-act="retry">🔁 ลองใหม่</button>`);
    buttons.push(`<button class="btn" data-act="revise">✏️ สั่งแก้แล้วทำต่อ</button>`);
  }
  if (job.status === "cancelled") buttons.push(`<button class="btn primary" data-act="retry">🔁 ลองใหม่</button>`);
  if (job.status === "queued" || job.status === "running") buttons.push(`<button class="btn danger" data-act="cancel">⏹ ยกเลิก</button>`);

  $("#jobHead").innerHTML = `
    <div class="head">
      <div><div class="row">${badge(job.status)}<span class="muted">${job.icon} ${esc(job.type_label)}</span></div>
        <h2>${esc(job.title)}</h2><div class="muted small">${meta}</div></div>
      <div class="row">${buttons.join("")}</div>
    </div>
    ${job.instructions ? `<details class="small"><summary>คำสั่งที่ให้ไว้</summary><pre>${esc(job.instructions)}</pre></details>` : ""}
    ${notices.join("")}
    <div id="reviseBox"></div>`;

  $("#jobHead").querySelectorAll("[data-act]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const act = btn.dataset.act;
      const base = `/api/jobs/${encodeURIComponent(job.id)}`;
      try {
        if (act === "revise") return showReviseBox(job);
        if (act === "cancel" && !confirm("ยกเลิกงานนี้?")) return;
        btn.disabled = true;
        if (act === "approve") {
          await postJSON(`${base}/approve`, { note: "" });
          toast(job.type === "meeting-summary" ? "อนุมัติแล้ว · เพิ่ม action item เข้ารายการติดตามงาน" : "อนุมัติแล้ว · เก็บผลลัพธ์ไว้ใน library");
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
        toast(err.message);
      }
    }),
  );
}

function showReviseBox(job) {
  $("#reviseBox").innerHTML = `<div class="revise">
    <label for="fb">ต้องการให้แก้อะไร</label>
    <textarea id="fb" placeholder="เช่น เพิ่มรายละเอียดงบประมาณ หรือแก้ผู้รับผิดชอบข้อ 2 เป็นคุณสมชาย"></textarea>
    <div class="row-end"><button class="btn" id="fbCancel">ยกเลิก</button><button class="btn primary" id="fbSend">ส่งให้ Claude แก้</button></div></div>`;
  $("#fb").focus();
  $("#fbCancel").onclick = () => ($("#reviseBox").innerHTML = "");
  $("#fbSend").onclick = async () => {
    const feedback = $("#fb").value.trim();
    if (!feedback) return toast("กรุณาระบุสิ่งที่ต้องการให้แก้");
    try {
      await postJSON(`/api/jobs/${encodeURIComponent(job.id)}/revise`, { feedback });
      toast("ส่งข้อแก้ไขแล้ว");
      route();
    } catch (err) {
      toast(err.message);
    }
  };
}

function actionItemsTable(items) {
  if (!Array.isArray(items)) return null;
  if (!items.length) return `<p class="muted">ไม่มี action item</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>งาน</th><th>ผู้รับผิดชอบ</th><th>กำหนดเสร็จ</th><th>ความสำคัญ</th><th>หมายเหตุ</th></tr></thead><tbody>${items
    .map((i) => `<tr><td>${esc(i.task)}</td><td>${esc(i.owner || "—")}</td><td>${esc(i.due ? fmtDate(i.due) : "—")}</td>
      <td>${esc(PRIORITY[i.priority] || i.priority || "")}</td><td>${esc(i.notes || "")}</td></tr>`)
    .join("")}</tbody></table></div>`;
}

async function loadOutput(jobId, path) {
  const view = $("#outView");
  if (!view) return;
  try {
    const f = await api(fileUrl(jobId, "output", path));
    const link = `<p class="small muted"><a href="${fileUrl(jobId, "output", path, true)}" target="_blank" rel="noopener">เปิด/ดาวน์โหลด</a> · บน NAS: <code>jobs/${esc(jobId)}/output/${esc(path)}</code></p>`;
    if (f.binary) {
      view.innerHTML = `<p>ไฟล์นี้แสดงในหน้าเว็บไม่ได้</p>${link}`;
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
    view.innerHTML = html + link;
  } catch (err) {
    view.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function renderInputs(jobId, files) {
  const list = (area, label) =>
    files[area].length
      ? `<p class="small muted">${label}</p><ul class="files">${files[area]
          .map((f) => `<li>📄 <a href="${fileUrl(jobId, area, f.path, true)}" target="_blank" rel="noopener">${esc(f.path)}</a>
            <span class="muted small">${(f.size / 1024).toFixed(1)} KB</span></li>`)
          .join("")}</ul>`
      : "";
  $("#inFiles").innerHTML = list("input", "input/") + list("context", "context/ (ระบบเตรียมให้)") || `<p class="muted">ไม่มีไฟล์</p>`;
}

// ---------- new job ----------
async function showNew() {
  $("#view").innerHTML = `<p class="muted">กำลังโหลด…</p>`;
  const types = await api("/api/job-types");
  $("#view").innerHTML = `
    <div class="panel"><h2>เลือกชนิดงาน</h2>
      <div class="types">${types.map((t) => `<button class="type-card" data-type="${esc(t.id)}"><span class="icon">${t.icon}</span>
        <b>${esc(t.label)}</b><span>${esc(t.description)}</span></button>`).join("")}</div></div>
    <div id="jobForm"></div>
    <div class="panel small muted">💡 สรุปประชุมแบบไม่ต้องเปิดหน้านี้: วางไฟล์ transcript ลงโฟลเดอร์ <code>inbox/meeting</code> บน NAS
      · เอกสารอ้างอิง (ลูกค้า ราคา มาตรฐาน template) วางไว้ในโฟลเดอร์ <code>knowledge/</code></div>`;
  $("#view .types").addEventListener("click", (e) => {
    const card = e.target.closest(".type-card");
    if (!card) return;
    document.querySelectorAll(".type-card").forEach((c) => c.classList.toggle("active", c === card));
    renderForm(types.find((t) => t.id === card.dataset.type));
  });
}

function renderForm(t) {
  const fields = t.fields
    .map((f) => {
      const req = f.required ? ` <span class="req">*</span>` : "";
      const label = `<label for="f_${f.name}">${esc(f.label)}${req}</label>`;
      if (f.kind === "textarea") return `${label}<textarea id="f_${f.name}" name="${f.name}" placeholder="${esc(f.placeholder)}"></textarea>`;
      if (f.kind === "files") return `${label}<input type="file" id="f_${f.name}" name="files" multiple>`;
      return `${label}<input type="text" id="f_${f.name}" name="${f.name}" placeholder="${esc(f.placeholder)}">`;
    })
    .join("");
  $("#jobForm").innerHTML = `<form class="panel" id="newJob">
    <h2>${t.icon} ${esc(t.label)}</h2><p class="muted small">${esc(t.description)}</p>${fields}
    <p class="muted small">ผลลัพธ์: ${t.outputs.map((o) => `<code>${esc(o)}</code>`).join(" ")}</p>
    <div class="row-end"><button type="submit" class="btn primary" id="submitJob">ส่งงานให้ Claude</button></div></form>`;
  $("#newJob").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    fd.append("type", t.id);
    for (const f of t.fields) {
      const missing = f.kind === "files" ? !$("#f_files").files.length : !String(fd.get(f.name) || "").trim();
      if (f.required && missing) return toast(`กรุณากรอก: ${f.label}`);
    }
    const btn = $("#submitJob");
    btn.disabled = true;
    btn.textContent = "กำลังส่ง…";
    try {
      const job = await api("/api/jobs", { method: "POST", body: fd });
      location.hash = `#/job/${encodeURIComponent(job.id)}`;
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
      btn.textContent = "ส่งงานให้ Claude";
    }
  });
  $("#jobForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- follow-up list ----------
let showDone = false;

function showTasks() {
  const f = lastStatus && lastStatus.followup;
  const schedule = f && f.time ? `ทุก ${f.days.map((d) => DAYS[d] || d).join(" ")} เวลา ${f.time}` : "เมื่อเปิดใช้ FOLLOWUP_TIME";
  $("#view").innerHTML = `<div class="panel tasks">
    <div class="head"><h2>ติดตามงาน · action item จากประชุมที่อนุมัติแล้ว</h2>
      <label class="small" style="font-weight:normal;margin:0"><input type="checkbox" id="showDone" ${showDone ? "checked" : ""}> แสดงรายการที่เสร็จแล้ว</label></div>
    <div id="taskTable"><p class="muted">กำลังโหลด…</p></div>
    <p class="muted small">ระบบสร้างงาน "ติดตามงาน" ให้อัตโนมัติ ${esc(schedule)} เพื่อร่างข้อความทวงงานที่ใกล้หรือเลยกำหนด</p></div>`;
  $("#showDone").onchange = (e) => {
    showDone = e.target.checked;
    route();
  };
  poll(async () => {
    renderTasks(await api(`/api/tasks?include_done=${showDone}`));
    return 15000;
  });
}

function renderTasks(items) {
  const el = $("#taskTable");
  if (!el) return;
  if (!items.length) {
    el.innerHTML = `<div class="empty">ยังไม่มีรายการ · อนุมัติงาน "สรุปประชุม" แล้ว action item จะมาอยู่ที่นี่</div>`;
    return;
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  el.innerHTML = `<div class="table-wrap"><table><thead><tr><th></th><th>งาน</th><th>ผู้รับผิดชอบ</th><th>กำหนดเสร็จ</th><th>ความสำคัญ</th><th>ที่มา</th></tr></thead><tbody>${items
    .map((i) => {
      const done = i.status === "done";
      const days = i.due ? Math.round((new Date(`${i.due}T00:00:00`) - today) / 86400000) : null;
      const cls = done || days === null ? "" : days < 0 ? "overdue" : days <= 3 ? "soon" : "";
      const hint = done || days === null ? "" : days < 0 ? ` (เลยมา ${-days} วัน)` : days === 0 ? " (วันนี้)" : "";
      return `<tr class="${done ? "done" : ""}">
        <td><input type="checkbox" data-id="${esc(i.id)}" ${done ? "checked" : ""} title="ทำเครื่องหมายว่าเสร็จ"></td>
        <td><b>${esc(i.id)}</b> ${esc(i.task)}${i.notes ? `<div class="muted small">${esc(i.notes)}</div>` : ""}</td>
        <td>${esc(i.owner || "—")}</td>
        <td class="due ${cls}">${esc(i.due ? fmtDate(i.due) + hint : "—")}</td>
        <td><span class="prio ${esc(i.priority)}">${esc(PRIORITY[i.priority] || i.priority)}</span></td>
        <td class="small"><a href="#/job/${encodeURIComponent(i.source_job)}">${esc(i.source_title || i.source_job)}</a></td></tr>`;
    })
    .join("")}</tbody></table></div>`;
  el.querySelectorAll("input[data-id]").forEach((cb) =>
    cb.addEventListener("change", async () => {
      try {
        await postJSON(`/api/tasks/${encodeURIComponent(cb.dataset.id)}`, { status: cb.checked ? "done" : "open" });
        route();
      } catch (err) {
        toast(err.message);
      }
    }),
  );
}

// ---------- router ----------
function route() {
  viewToken += 1;
  const hash = location.hash || "#/";
  const tab = hash.startsWith("#/new") ? "new" : hash.startsWith("#/tasks") ? "tasks" : "jobs";
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  window.scrollTo(0, 0);
  if (hash.startsWith("#/job/")) return showJob(decodeURIComponent(hash.slice(6)));
  if (hash.startsWith("#/new")) return showNew().catch((err) => ($("#view").innerHTML = `<p class="muted">${esc(err.message)}</p>`));
  if (hash.startsWith("#/tasks")) return showTasks();
  return showJobs();
}

window.addEventListener("hashchange", route);
route();
refreshStatus();
