const STAGE_ORDER = [
  "INGEST", "EXTRACT_TEXT", "EXTRACT_FIELDS", "VALIDATE", "VENDOR_CHECK",
  "PO_MATCH", "DUPLICATE_CHECK", "TOLERANCE_CHECK", "DECISION",
];
const ICONS = { ok: "✓", warn: "!", fail: "✕", info: "i" };

const state = { file: null, dashFilter: "ALL", runs: [], pos: [] };

const $ = (id) => document.getElementById(id);
const money = (v) =>
  v === null || v === undefined || isNaN(v)
    ? "—"
    : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------------- tabs ---------------- */
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "dashboard") loadDashboard();
    if (btn.dataset.tab === "reference") loadReference();
  });
});

/* ---------------- input ---------------- */
const dropzone = $("dropzone");
dropzone.addEventListener("click", () => $("fileInput").click());
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
});
$("fileInput").addEventListener("change", (e) => {
  if (e.target.files.length) selectFile(e.target.files[0]);
});

function selectFile(file, sampleEl) {
  state.file = file;
  $("selectedFile").textContent = file.name;
  $("selectedFile").classList.remove("hidden");
  $("runBtn").disabled = false;
  document.querySelectorAll(".sample-item").forEach((el) => el.classList.remove("selected"));
  if (sampleEl) sampleEl.classList.add("selected");
}

/* ---------------- samples ---------------- */
async function loadSamples() {
  const items = await (await fetch("/api/sample-invoices")).json();
  const list = $("sampleList");
  list.innerHTML = "";
  items.forEach((item) => {
    const div = document.createElement("div");
    div.className = "sample-item";
    const tag = item.expect
      ? `<span class="expect-tag ${item.expect}">${esc(item.expect.replace("_", " "))}</span>` : "";
    div.innerHTML = `
      <div class="si-top">
        <span class="si-label">${esc(item.label || item.filename)}</span>${tag}
      </div>
      ${item.note ? `<div class="si-note">${esc(item.note)}</div>` : ""}
      <div class="si-file">${esc(item.filename)}</div>`;
    div.addEventListener("click", async () => {
      const blob = await (await fetch("/api/sample-invoices/" + encodeURIComponent(item.filename))).blob();
      selectFile(new File([blob], item.filename, { type: "application/pdf" }), div);
    });
    list.appendChild(div);
  });
}
loadSamples();

/* ---------------- pipeline run ---------------- */
function resetStages() {
  const list = $("stageList");
  list.innerHTML = "";
  STAGE_ORDER.forEach((name, i) => {
    const row = document.createElement("div");
    row.className = "stage-row is-pending";
    row.id = "stage-" + name;
    row.innerHTML = `
      <div class="stage-icon">${i + 1}</div>
      <div class="stage-body">
        <div class="stage-name-row">
          <span class="stage-name">${name}</span>
          <span class="stage-ms"></span>
        </div>
        <div class="stage-detail">Waiting…</div>
      </div>`;
    list.appendChild(row);
  });
  setActive(STAGE_ORDER[0]);
  setProgress(0);
}

function setActive(name) {
  const row = $("stage-" + name);
  if (!row) return;
  row.classList.remove("is-pending");
  row.classList.add("is-active");
  row.querySelector(".stage-icon").className = "stage-icon running";
  row.querySelector(".stage-icon").textContent = "";
  row.querySelector(".stage-detail").textContent = "Running…";
}

function setProgress(done) {
  const pct = Math.round((done / STAGE_ORDER.length) * 100);
  $("progressBar").style.width = pct + "%";
  $("pipelineProgress").textContent = done === 0 ? "running…" : `${done} / ${STAGE_ORDER.length} stages`;
}

function applyStage(stage, index) {
  const row = $("stage-" + stage.name);
  if (!row) return;
  row.className = "stage-row lvl-" + stage.status;
  const icon = row.querySelector(".stage-icon");
  icon.className = "stage-icon " + stage.status;
  icon.textContent = ICONS[stage.status] || "";
  row.querySelector(".stage-detail").textContent = stage.detail;
  if (stage.ms !== undefined) row.querySelector(".stage-ms").textContent = stage.ms + " ms";
  setProgress(index + 1);
  const next = STAGE_ORDER[index + 1];
  if (next) setActive(next);
}

$("runBtn").addEventListener("click", run);

async function run() {
  if (!state.file) return;
  $("runBtn").disabled = true;
  $("runBtnLabel").textContent = "Running…";
  ["verdictBar", "poCard", "reasonCard", "fieldsCard"].forEach((id) => $(id).classList.add("hidden"));
  resetStages();

  const fd = new FormData();
  fd.append("file", state.file);

  let seen = 0;
  try {
    const resp = await fetch("/api/runs/stream", { method: "POST", body: fd });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const evt = JSON.parse(line.slice(6));
        if (evt.type === "stage") applyStage(evt.stage, seen++);
        if (evt.type === "final") showResult(evt.result);
      }
    }
  } catch (err) {
    $("pipelineProgress").textContent = "error";
    alert("Run failed: " + err.message);
  }
  $("runBtn").disabled = false;
  $("runBtnLabel").textContent = "Run process";
}

/* ---------------- result ---------------- */
function showResult(r) {
  const pm = r.po_match;

  // verdict bar
  const bar = $("verdictBar");
  bar.className = "verdict " + r.status;
  $("verdictBadge").className = "verdict-badge " + r.status;
  $("verdictBadge").textContent = r.status.replace("_", " ");
  $("verdictFile").textContent = r.filename;
  $("verdictSub").textContent =
    `run #${r.run_id} · ${r.extracted.vendor_name || "unknown vendor"}` +
    (r.extracted.invoice_number ? ` · ${r.extracted.invoice_number}` : "");
  $("verdictTotals").innerHTML = `
    <div class="vt"><div class="vt-num">${money(r.extracted.total)}</div><div class="vt-lbl">invoice total</div></div>
    ${pm.po_number ? `<div class="vt"><div class="vt-num">${money(pm.remaining_before)}</div><div class="vt-lbl">PO available</div></div>` : ""}`;
  bar.classList.remove("hidden");

  // PO balance
  if (pm.po_number) {
    $("poMatchedVia").textContent = pm.matched_via + " match";
    $("poBalance").innerHTML = balanceHTML(pm, r.status);
    $("poCard").classList.remove("hidden");
  } else {
    $("poCard").classList.add("hidden");
  }

  // reasoning
  $("reasonList").innerHTML = r.reasons.map((x) => {
    const level = typeof x === "string" ? "info" : x.level || "info";
    const text = typeof x === "string" ? x : x.text;
    return `<li class="reason-item lvl-${level}">
      <span class="reason-dot ${level}">${ICONS[level] || "i"}</span>
      <span class="reason-text">${esc(text)}</span></li>`;
  }).join("");
  $("reasonCard").classList.remove("hidden");

  // extracted fields
  const e = r.extracted;
  const req = (v) => (v ? esc(v) : '<span class="kv-missing">missing</span>');
  $("extractedTable").innerHTML = `
    ${row("Vendor", req(e.vendor_name))}
    ${row("Invoice #", req(e.invoice_number))}
    ${row("Date", e.invoice_date ? esc(e.invoice_date) : "—")}
    ${row("PO refs", (e.po_references || []).join(", ") || "—")}
    ${row("Subtotal", money(e.subtotal))}
    ${row("Tax", money(e.tax))}
    ${row("Total", e.total !== null && e.total !== undefined ? `<b>${money(e.total)}</b>` : req(null))}
    ${row("Line items", (e.line_items || []).length || "—")}
    ${row("Extraction", esc(e.extraction_method))}
    ${row("Text layer", e.has_text_layer ? "embedded" : (e.ocr_succeeded ? "OCR recovered" : '<span class="kv-missing">none</span>'))}`;
  $("fieldsCard").classList.remove("hidden");
}

const row = (k, v) => `<tr><td>${k}</td><td>${v}</td></tr>`;

/* The segmented bar is the visual heart of the split-PO story: it shows what
   earlier approved invoices already consumed, what this invoice claims, and
   whether that claim fits inside what's left on the PO. */
function balanceHTML(pm, status) {
  const total = pm.po_amount || 0;
  const consumed = pm.consumed_before || 0;
  const claim = pm.invoice_total || 0;
  const fits = pm.within_tolerance;
  const scale = Math.max(total, consumed + claim) || 1;
  const pct = (v) => (v / scale) * 100;

  const shown = fits ? claim : Math.max(0, pm.remaining_before || 0);
  const over = fits ? 0 : claim - Math.max(0, pm.remaining_before || 0);
  const leftover = Math.max(0, total - consumed - shown);

  const seg = (cls, w, label) =>
    w <= 0.4 ? "" : `<div class="bal-seg ${cls}" style="width:${w}%">${w > 9 ? label : ""}</div>`;

  return `
    <div class="po-head">
      <span class="po-num">${esc(pm.po_number)}</span>
      <span class="po-total">${esc(pm.po_vendor || "")} · ${money(total)} authorised</span>
    </div>
    <div class="bal-bar">
      ${seg("consumed", pct(consumed), money(consumed))}
      ${seg(fits ? "current-ok" : "current-bad", pct(shown), money(shown))}
      ${seg("overflow", pct(over), "+" + money(over))}
      ${seg("remaining", pct(leftover), money(leftover))}
    </div>
    <div class="bal-legend">
      <div class="bl"><span class="bl-dot consumed"></span><span class="bl-txt">Consumed earlier <b>${money(consumed)}</b></span></div>
      <div class="bl"><span class="bl-dot ${fits ? "current-ok" : "current-bad"}"></span><span class="bl-txt">This invoice <b>${money(claim)}</b></span></div>
      <div class="bl"><span class="bl-dot remaining"></span><span class="bl-txt">Remaining after <b>${money(fits ? pm.remaining_after : pm.remaining_before)}</b></span></div>
    </div>
    ${calloutHTML(pm, fits)}`;
}

function calloutHTML(pm, fits) {
  if (!fits) {
    return `<div class="po-callout bad">Over by ${money(pm.diff)} — only ${money(pm.remaining_before)} left on this PO,
      tolerance is ${money(pm.tolerance)}. The vendor is billing beyond what's authorised.</div>`;
  }
  if (pm.is_partial) {
    return `<div class="po-callout good">Partial invoice — accepted. ${money(pm.remaining_after)} stays available
      on ${esc(pm.po_number)} for the next invoice.</div>`;
  }
  return `<div class="po-callout">Matches the remaining balance within tolerance (diff ${money(pm.diff)},
    tolerance ${money(pm.tolerance)}). ${money(pm.remaining_after)} left on this PO.</div>`;
}

/* ---------------- dashboard ---------------- */
document.querySelectorAll(".filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.dashFilter = btn.dataset.status;
    renderRuns();
  });
});
$("refreshDash").addEventListener("click", loadDashboard);

async function loadDashboard() {
  const [runs, ref] = await Promise.all([
    (await fetch("/api/runs")).json(),
    (await fetch("/api/reference")).json(),
  ]);
  state.runs = runs;
  state.pos = ref.purchase_orders;
  renderStats();
  renderRuns();
  renderConsumption();
}

function renderStats() {
  const c = { APPROVED: 0, NEEDS_REVIEW: 0, REJECTED: 0 };
  state.runs.forEach((r) => { c[r.status] = (c[r.status] || 0) + 1; });
  $("dashStats").innerHTML = [
    ["total", state.runs.length, "Total runs"],
    ["APPROVED", c.APPROVED, "Approved"],
    ["NEEDS_REVIEW", c.NEEDS_REVIEW, "Needs review"],
    ["REJECTED", c.REJECTED, "Rejected"],
  ].map(([cls, n, lbl]) =>
    `<div class="stat-card ${cls}"><div class="stat-num">${n}</div><div class="stat-lbl">${lbl}</div></div>`
  ).join("");
}

function renderRuns() {
  const body = $("runsTableBody");
  const rows = state.dashFilter === "ALL"
    ? state.runs : state.runs.filter((r) => r.status === state.dashFilter);
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8"><div class="empty-state">
      <div class="empty-icon">○</div><p><strong>No runs to show</strong></p>
      <p class="empty-sub">Process an invoice on the Run tab and it will appear here.</p></div></td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r) => `
    <tr class="clickable" data-id="${r.id}">
      <td class="mono">#${r.id}</td>
      <td>${esc(r.filename)}</td>
      <td>${esc(r.vendor_name || "—")}</td>
      <td class="mono">${esc(r.invoice_number || "—")}</td>
      <td class="num">${money(r.total)}</td>
      <td class="mono">${esc(r.po_number || "—")}</td>
      <td><span class="status-pill ${r.status}">${r.status.replace("_", " ")}</span></td>
      <td>${new Date(r.created_at).toLocaleString()}</td>
    </tr>`).join("");
  body.querySelectorAll("tr.clickable").forEach((tr) => {
    tr.addEventListener("click", () => {
      openModal(state.runs.find((x) => x.id === Number(tr.dataset.id)));
    });
  });
}

function renderConsumption() {
  const used = {};
  state.runs.filter((r) => r.status === "APPROVED" && r.po_number)
    .forEach((r) => { used[r.po_number] = (used[r.po_number] || 0) + (r.total || 0); });

  $("poConsumption").innerHTML = state.pos.map((po) => {
    const u = used[po.po_number] || 0;
    const pct = Math.min(100, (u / po.amount) * 100);
    const cls = u > po.amount ? "over" : (pct >= 99.5 ? "full" : "");
    return `<div class="poc-row">
      <div><div class="poc-name">${esc(po.po_number)}</div><div class="poc-vendor">${esc(po.vendor)}</div></div>
      <div class="poc-track"><div class="poc-fill ${cls}" style="width:${pct}%"></div></div>
      <div class="poc-amt"><b>${money(u)}</b> / ${money(po.amount)}</div>
    </div>`;
  }).join("");
}

/* ---------------- modal ---------------- */
function openModal(r) {
  if (!r) return;
  $("modalBody").innerHTML = `
    <div class="modal-title">
      <h2>${esc(r.filename)}</h2>
      <span class="status-pill ${r.status}">${r.status.replace("_", " ")}</span>
    </div>
    <div class="modal-sub">run #${r.id} · ${esc(r.vendor_name || "unknown vendor")} · ${money(r.total)}
      · ${new Date(r.created_at).toLocaleString()}</div>

    <div class="modal-section">Reasoning</div>
    <ul class="reason-list">${r.reasons.map((x) => {
      const level = typeof x === "string" ? "info" : x.level || "info";
      const text = typeof x === "string" ? x : x.text;
      return `<li class="reason-item lvl-${level}">
        <span class="reason-dot ${level}">${ICONS[level] || "i"}</span>
        <span class="reason-text">${esc(text)}</span></li>`;
    }).join("")}</ul>

    <div class="modal-section">Stages</div>
    <div class="stage-list">${r.stages.map((s) => `
      <div class="stage-row lvl-${s.status}">
        <div class="stage-icon ${s.status}">${ICONS[s.status] || ""}</div>
        <div class="stage-body">
          <div class="stage-name-row"><span class="stage-name">${s.name}</span>
            <span class="stage-ms">${s.ms !== undefined ? s.ms + " ms" : ""}</span></div>
          <div class="stage-detail">${esc(s.detail)}</div>
        </div>
      </div>`).join("")}</div>`;
  $("runModal").classList.remove("hidden");
}
$("closeModal").addEventListener("click", () => $("runModal").classList.add("hidden"));
$("runModal").addEventListener("click", (e) => {
  if (e.target.id === "runModal") $("runModal").classList.add("hidden");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("runModal").classList.add("hidden");
});

/* ---------------- reference ---------------- */
let refLoaded = false;
async function loadReference() {
  if (refLoaded) return;
  const d = await (await fetch("/api/reference")).json();
  document.querySelector("#poRefTable tbody").innerHTML = d.purchase_orders.map((po) => `
    <tr><td class="mono">${esc(po.po_number)}</td><td>${esc(po.vendor)}</td>
    <td class="num">${money(po.amount)}</td>
    <td><span class="status-pill ${po.status}">${esc(po.status)}</span></td></tr>`).join("");
  document.querySelector("#vendorRefTable tbody").innerHTML = d.vendors.map((v) => `
    <tr><td>${esc(v.vendor_name)}</td><td class="mono">${esc(v.vendor_id)}</td>
    <td><span class="status-pill ${v.status}">${esc(v.status)}</span></td></tr>`).join("");
  refLoaded = true;
}
