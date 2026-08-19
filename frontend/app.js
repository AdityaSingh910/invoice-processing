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
  ["verdictBar", "poCard", "reasonCard", "fieldsCard", "auditCard"].forEach((id) => $(id).classList.add("hidden"));
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
    ${row("Currency", esc(e.currency || "—"))}
    ${row("Extraction route", esc(e.extraction_method))}`;
  $("fieldsCard").classList.remove("hidden");

  // Decision details. A run that has just finished has no human ruling yet, so
  // the review bar is offered whenever the rules held it.
  const justRun = { id: r.run_id, status: r.status, automated_decision: r.status,
                    human_decision: null };
  $("auditBody").innerHTML = auditHTML(r.audit, justRun) + reviewBarHTML(justRun);
  $("auditCard").classList.remove("hidden");
}

const row = (k, v) => `<tr><td>${k}</td><td>${v}</td></tr>`;

/* ---------------- audit trail ----------------

   Everything rendered here was computed by the Python rule engine and stored
   with the run. Nothing on this screen is generated, summarised or reworded by
   a model -- the point of the panel is that a reviewer sees the same numbers the
   decision was made from. */

const CUR = { USD: "$", EUR: "€", GBP: "£", INR: "₹", JPY: "¥" };

/* Money in the currency the invoice was actually denominated in, rather than
   assuming dollars -- a trail that prints $ next to a rupee figure is wrong. */
function amt(v, currency) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const sym = CUR[String(currency || "").toUpperCase()] || "";
  const n = Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return sym ? sym + n : `${n} ${esc(currency || "")}`.trim();
}

function auditHTML(a, run) {
  if (!a) {
    return `<p class="audit-empty">No audit trail was stored for this run.</p>`;
  }
  const cur = (a.invoice && a.invoice.currency) || "USD";
  const po = a.purchase_order || {};
  const c = a.comparison || {};
  const ex = a.extraction || {};
  const decision = a.automated_decision || "";

  // Source of the PO record. Never invented: the backend stores null when the
  // data layer does not know, and that is shown as unknown rather than blank.
  const source = po.po_number
    ? (po.source_file
        ? `${esc(po.source_file)}${po.source_row != null ? `, row ${po.source_row}` : ", row unknown"}`
        : "not recorded")
    : "—";

  const ruleRows = (a.rules || []).map((r) => `
    <li class="audit-rule ${r.passed ? "pass" : "fail"}">
      <span class="audit-rule-mark">${r.passed ? "✓" : "✗"}</span>
      <span class="audit-rule-name">${esc(r.name)}</span>
      <span class="audit-rule-detail">${esc(r.detail || "")}</span>
    </li>`).join("");

  // The comparison block only means anything when a PO was actually bound.
  const comparison = po.po_number ? `
    <div class="audit-section">Values compared</div>
    <table class="kv-table audit-compare">
      ${row("Invoice total", `<b>${amt(c.invoice_total, cur)}</b>`)}
      ${row("PO amount", amt(c.po_amount, po.po_currency || cur))}
      ${row("Already consumed", amt(c.consumed_before, po.po_currency || cur))}
      ${row("PO remaining", `<b>${amt(c.po_remaining, po.po_currency || cur)}</b>`)}
      ${row("Variance", `<span class="${Number(c.variance) > 0 ? "audit-over" : ""}">${amt(c.variance, cur)}</span>`)}
      ${row("Tolerance used", amt(c.tolerance, cur))}
    </table>` : "";

  return `
    <div class="audit-head">
      <span class="status-pill ${decision}">${esc(decision.replace("_", " "))}</span>
      <span class="audit-reason">${esc(a.reason || "")}</span>
    </div>

    ${humanDecisionHTML(run)}

    <div class="audit-cols">
      <div>
        <div class="audit-section">Invoice</div>
        <table class="kv-table">
          ${row("Invoice #", esc((a.invoice || {}).invoice_number || "—"))}
          ${row("Vendor", esc((a.invoice || {}).vendor || "—"))}
          ${row("Total", amt((a.invoice || {}).total, cur))}
          ${row("Read by", esc(ex.method || ex.route || "—"))}
        </table>
      </div>
      <div>
        <div class="audit-section">Matched purchase order</div>
        <table class="kv-table">
          ${row("PO", esc(po.po_number || "none"))}
          ${row("Matched via", esc(po.matched_via || "—"))}
          ${row("PO status", esc(po.po_status || "—"))}
          ${row("Source", source)}
        </table>
      </div>
    </div>

    ${comparison}

    <div class="audit-section">Rules</div>
    <ul class="audit-rules">${ruleRows}</ul>`;
}

/* The history, once a person has ruled on a run. Shown above the evidence so a
   reviewer opening it later sees immediately that it was already decided. */
function humanDecisionHTML(run) {
  if (!run || !run.human_decision) return "";
  const who = run.reviewed_by ? esc(run.reviewed_by) : "an unattributed reviewer";
  const when = run.reviewed_at ? new Date(run.reviewed_at).toLocaleString() : "";
  return `
    <div class="audit-human">
      <div class="audit-human-line">
        <span class="status-pill ${run.final_decision === "HUMAN_APPROVED" ? "APPROVED" : "REJECTED"}">
          ${esc(String(run.final_decision || "").replace(/_/g, " "))}</span>
        <span>Reviewed by <b>${who}</b>${when ? ` on ${esc(when)}` : ""}</span>
      </div>
      ${run.review_note ? `<div class="audit-human-note">${esc(run.review_note)}</div>` : ""}
      <div class="audit-human-foot">The automated decision above is unchanged and kept on record.</div>
    </div>`;
}

/* ACCEPT / REJECT, offered only for a run the rules held for review and that
   nobody has ruled on yet -- matching what the API will actually allow. */
function reviewBarHTML(run) {
  if (!run || !run.id) return "";
  const automated = run.automated_decision || run.status;
  if (automated !== "NEEDS_REVIEW" || run.human_decision) return "";
  return `
    <div class="review-bar" data-run="${run.id}">
      <div class="review-copy">
        <b>Human review</b>
        <span>Check the evidence above, then record a decision. The automated verdict is kept either way.</span>
      </div>
      <input class="review-who" type="text" placeholder="your name (optional)" aria-label="Reviewer name" />
      <div class="review-actions">
        <button class="btn review-accept" data-decision="ACCEPTED">Accept</button>
        <button class="btn review-reject" data-decision="REJECTED">Reject</button>
      </div>
    </div>`;
}

/* One delegated listener, so it works for the run view and the dashboard modal
   without either needing to wire anything up. */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".review-accept, .review-reject");
  if (!btn) return;
  const bar = btn.closest(".review-bar");
  const runId = Number(bar.dataset.run);
  const reviewer = (bar.querySelector(".review-who") || {}).value || "";

  bar.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const res = await fetch(`/api/runs/${runId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: btn.dataset.decision, reviewer }),
    });
    const body = await res.json();
    if (!body.ok) {
      bar.insertAdjacentHTML("beforeend", `<div class="review-error">${esc(body.error || "review failed")}</div>`);
      bar.querySelectorAll("button").forEach((b) => (b.disabled = false));
      return;
    }
    bar.outerHTML = `<div class="review-done">Recorded: <b>${esc(body.final_decision.replace(/_/g, " "))}</b></div>`;
    if (typeof loadDashboard === "function") loadDashboard();
  } catch (err) {
    bar.querySelectorAll("button").forEach((b) => (b.disabled = false));
    bar.insertAdjacentHTML("beforeend", `<div class="review-error">${esc(String(err))}</div>`);
  }
});

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
      <td><span class="status-pill ${r.status}">${r.status.replace("_", " ")}</span>${
        // A run a person ruled on reads as APPROVED/REJECTED like any other.
        // The chip is the only thing on this row that says a human decided it.
        r.human_decision ? `<span class="human-chip" title="${esc(String(r.final_decision || "").replace(/_/g, " "))} by ${esc(r.reviewed_by || "an unattributed reviewer")}">human</span>` : ""
      }</td>
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

    <div class="modal-section">Decision details</div>
    ${auditHTML(r.audit, r)}
    ${reviewBarHTML(r)}

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
