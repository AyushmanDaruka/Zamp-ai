/* Clearpath AP — vanilla JS front end. Everything rendered from the API; all untrusted text goes through esc(). */
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const tip = $("#tip");

// ───────────── utils ─────────────
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function inr(x, dec = true) {
  if (x === null || x === undefined || isNaN(x)) return "—";
  const neg = x < 0; x = Math.abs(x);
  let [w, f] = x.toFixed(2).split(".");
  let last3 = w.slice(-3), rest = w.slice(0, -3);
  if (rest) last3 = "," + last3;
  rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return (neg ? "-₹" : "₹") + rest + last3 + (dec && f !== "00" ? "." + f : "");
}
function inrShort(x) {
  if (!x) return "₹0";
  if (x >= 1e7) return "₹" + (x / 1e7).toFixed(2) + " Cr";
  if (x >= 1e5) return "₹" + (x / 1e5).toFixed(2) + " L";
  return inr(x, false);
}
const qty = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-IN"));
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) + ", " + new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}
const ms = (m) => (m == null ? "" : m < 1000 ? m + " ms" : (m / 1000).toFixed(1) + " s");
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

const DEC = {
  APPROVED: { label: "Approved", icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M3.5 8.5l3 3 6-7"/></svg>' },
  PARTIALLY_APPROVED: { label: "Partially approved", icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="8" r="5.5"/><path d="M8 2.5v11" /><path d="M8 2.5a5.5 5.5 0 010 11z" fill="currentColor"/></svg>' },
  ON_HOLD: { label: "On hold", icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M6 4v8M10 4v8"/></svg>' },
  REJECTED: { label: "Rejected", icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>' },
  ERROR: { label: "Error", icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M8 4v5M8 11.5v.5"/></svg>' },
};
const STATUS_COLOR = { APPROVED: "var(--good)", PARTIALLY_APPROVED: "var(--warn)", ON_HOLD: "var(--serious)", REJECTED: "var(--critical)", ERROR: "var(--text-3)" };
const badge = (d) => `<span class="badge ${esc(d)}">${(DEC[d] || DEC.ERROR).icon}${esc((DEC[d] || { label: d }).label)}</span>`;
const CODE_LABEL = {
  DUPLICATE_INVOICE: "Duplicate invoice", DUPLICATE_FILE: "Same file resent", BANK_MISMATCH: "Bank details changed",
  PRICE_VARIANCE: "Price above PO", QTY_EXCEEDS_RECEIPT: "Billed > received / PO", WRONG_TAX_TYPE: "Wrong GST type",
  GSTIN_MISMATCH: "GSTIN invalid / mismatch", UNKNOWN_VENDOR: "Vendor not onboarded", VENDOR_BLOCKED: "Vendor blocked",
  LINE_ARITHMETIC: "Arithmetic error", SUBTOTAL_MISMATCH: "Subtotal mismatch", TOTAL_MISMATCH: "Total mismatch",
  MISSING_INVOICE_NUMBER: "No invoice number", SCAN_UNCERTAIN: "Scan unreadable", EXTRACTION_UNVERIFIED: "Unverified extraction",
  PO_AMBIGUOUS: "PO ambiguous", NO_PO: "No PO", PO_NOT_FOUND: "PO not found", LINE_NOT_ON_PO: "Item not on PO",
  BUYER_GSTIN: "Billed to wrong GSTIN", TAX_CALC: "GST miscalculated", CFO_LIMIT: "Above approval limit", MISSING_DATE: "No date", FUTURE_DATE: "Future-dated",
};
const CHECK_IC = { pass: "✓", warn: "!", fail: "✕", info: "i" };

// tooltip
function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (e) => {
      tip.innerHTML = el.getAttribute("data-tip");
      tip.style.opacity = 1;
      const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = x + "px";
      tip.style.top = e.clientY + 14 + "px";
    });
    el.addEventListener("mouseleave", () => (tip.style.opacity = 0));
  });
}

// theme
(function theme() {
  let t = null;
  try { t = localStorage.getItem("theme"); } catch (e) {}
  if (t) document.documentElement.dataset.theme = t;
  $("#themeBtn").onclick = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const nxt = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = nxt;
    try { localStorage.setItem("theme", nxt); } catch (e) {}
  };
})();

// ───────────── run state ─────────────
let live = null; // {stages:[{id,label}], byStage:{}, result, filename, pageUrl, running}
let SAMPLES = null;

function newState() {
  return { stages: [], byStage: {}, result: null, filename: "", pageUrl: null, running: true, open: new Set(), runId: null };
}
function applyEvent(st, ev) {
  if (ev.type === "run") { st.stages = ev.stages; st.runId = ev.id; st.filename = ev.filename; }
  else if (ev.type === "stage") {
    st.byStage[ev.stage] = ev;
    if (ev.state === "done" && (ev.status === "fail" || ev.status === "warn")) st.open.add(ev.stage);
  } else if (ev.type === "result") { st.result = ev.run; st.running = false; }
}

async function streamRun(url, opts, st, rerender) {
  let res;
  try { res = await fetch(url, { method: "POST", ...opts }); } catch (e) { st.error = "Network error: " + e.message; st.running = false; rerender(); return; }
  if (!res.ok) { st.error = (await res.text()) || res.statusText; st.running = false; rerender(); return; }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) { applyEvent(st, JSON.parse(line)); rerender(); }
    }
  }
  if (buf.trim()) { applyEvent(st, JSON.parse(buf)); }
  st.running = false;
  rerender();
}

// ───────────── run panel ─────────────
function renderRunPanel(st, { live: isLive } = {}) {
  const total = st.stages.length || 8;
  const done = Object.values(st.byStage).filter((e) => e.state === "done").length;
  const r = st.result;
  const d = r && r.result && r.result.decision;
  const prov = (st.byStage.verify && st.byStage.verify.data && st.byStage.verify.data.provenance) || {};
  const fields = (st.byStage.extract && st.byStage.extract.data && st.byStage.extract.data.fields) || (r && r.result && r.result.fields);
  const page = st.pageUrl || (r && r.id && r.status ? `/api/runs/${r.id}/page.jpg` : null);

  return `
  <div class="run-head">
    <div class="file">${esc(st.filename || "Invoice")}</div>
    ${r ? badge(r.decision) : st.error ? badge("ERROR") : `<span class="badge neutral">Running · stage ${Math.min(done + 1, total)} of ${total}</span>`}
    ${r ? `<span class="faint small">${ms(r.duration_ms)} end-to-end · ${esc(r.doc_kind || "")}</span>` : ""}
    <div style="flex:1"></div>
    ${r && isLive && r.decision === "ERROR" ? `<button class="btn sm primary" data-retry>Retry run</button>` : ""}
    ${r && isLive && r.decision !== "ERROR" ? `<a class="btn sm" href="#/runs/${esc(r.id)}">Open run record</a>` : ""}
  </div>
  <div class="progress"><div style="width:${(done / total) * 100}%"></div></div>
  ${st.error ? `<div class="decision ERROR"><div class="headline">Run failed</div><p class="reason">${esc(st.error)}</p></div>` : ""}
  <div class="run-layout">
    <div>
      ${d ? decisionCard(r, d) : ""}
      <div class="card">
        <div class="card-head"><h2>Pipeline</h2><span class="faint small">click a stage for its checks</span></div>
        <div class="stages">${st.stages.map((s, i) => stageRow(st, s, i)).join("")}</div>
      </div>
    </div>
    <div class="sticky">
      <div class="card">
        <div class="card-head"><h2>Document</h2><span class="faint small">${Object.keys(prov).length ? "highlights = where each value was verified" : ""}</span></div>
        <div class="card-pad">${docPreview(page, prov)}</div>
      </div>
      ${fields ? fieldsCard(fields, prov) : ""}
    </div>
  </div>`;
}

function stageRow(st, s, i) {
  const ev = st.byStage[s.id];
  const state = !ev ? "pending" : ev.state === "running" ? "running" : ev.status;
  const open = st.open.has(s.id) ? "open" : "";
  return `<div class="stage ${state} ${open}" data-stage="${esc(s.id)}">
    <div class="stage-row">
      <div class="dot">${state === "pass" ? "✓" : state === "fail" ? "✕" : state === "warn" ? "!" : state === "running" ? "" : i + 1}</div>
      <div style="min-width:0"><div class="stage-title">${esc(s.label)}</div>
        <div class="stage-sum">${ev && ev.state === "done" ? esc(ev.summary) : ev ? "working…" : ""}</div></div>
      <div class="stage-ms">${ev && ev.ms != null ? ms(ev.ms) : ""}</div>
    </div>
    <div class="stage-body">${ev && ev.checks ? ev.checks.map(checkRow).join("") : ""}${ev ? stageExtra(s.id, ev) : ""}</div>
  </div>`;
}
const checkRow = (c) => `<div class="check ${esc(c.status)}"><div class="ic">${CHECK_IC[c.status] || ""}</div><div><span class="lbl">${esc(c.label)}</span> — <span class="det">${esc(c.detail)}</span></div></div>`;

function stageExtra(sid, ev) {
  const data = ev.data || {};
  if (sid === "match") {
    let h = "";
    if (data.candidates && data.candidates.length) {
      h += `<div class="section"><h3>PO candidates (no PO on invoice)</h3><div class="table-wrap"><table><thead><tr><th>PO</th><th class="num">Line similarity</th><th class="num">Open-qty fit</th><th class="num">Score</th></tr></thead><tbody>${data.candidates
        .map((c, i) => `<tr><td>${esc(c.po_number)} ${i === 0 && data.inferred ? '<span class="prov ok">chosen</span>' : ""}</td><td class="num">${c.line_similarity.toFixed(2)}</td><td class="num">${c.open_qty_fit.toFixed(2)}</td><td class="num"><b>${c.score.toFixed(2)}</b></td></tr>`)
        .join("")}</tbody></table></div></div>`;
    }
    if (data.lines && data.lines.length) {
      h += `<div class="section"><h3>Line-level 3-way match</h3><div class="table-wrap"><table><thead><tr><th>Invoice line → PO line</th><th class="num">Billed</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Prev. billed</th><th class="num">Billable</th><th class="num">Price Δ</th></tr></thead><tbody>${data.lines
        .map((l) => `<tr><td><span class="check ${esc(l.status)}" style="display:inline"><span class="ic">${CHECK_IC[l.status]}</span></span> ${esc(l.invoice_line)}<div class="faint small">→ ${esc(l.po_line || "not on PO")}</div></td>
          <td class="num">${qty(l.inv_qty)}</td><td class="num">${qty(l.po_qty)}</td><td class="num">${qty(l.received)}</td><td class="num">${qty(l.prev_billed)}</td><td class="num"><b>${qty(l.billable_qty)}</b></td>
          <td class="num">${l.price_var_pct == null ? "—" : (l.price_var_pct > 0 ? "+" : "") + l.price_var_pct.toFixed(1) + "%"}</td></tr>`)
        .join("")}</tbody></table></div></div>`;
    }
    return h;
  }
  return "";
}

function docPreview(src, prov) {
  if (!src) return `<div class="doc"><div class="doc-empty">Preview appears when the run completes</div></div>`;
  const LBL = { invoice_number: "Invoice #", grand_total: "Total", vendor_gstin: "Vendor GSTIN", bank_account_number: "Bank A/c", po_reference: "PO", subtotal: "Subtotal", invoice_date: "Date" };
  const boxes = Object.entries(prov || {})
    .filter(([k, v]) => v && v.bbox && LBL[k])
    .map(([k, v]) => {
      const [x0, y0, x1, y1] = v.bbox;
      const bad = v.status === "not_found" || v.status === "conflict";
      return `<div class="hl ${bad ? "bad" : ""}" style="left:${x0 * 100 - 0.4}%;top:${y0 * 100 - 0.3}%;width:${(x1 - x0) * 100 + 0.8}%;height:${(y1 - y0) * 100 + 0.6}%"><span>${LBL[k]}</span></div>`;
    })
    .join("");
  return `<div class="doc"><img src="${esc(src)}" alt="Invoice page" loading="lazy" />${boxes}</div>`;
}

function fieldsCard(f, prov) {
  const P = (k) => {
    const p = prov[k];
    if (!p) return "";
    if (p.status === "grounded") return '<span class="prov ok" title="Found verbatim in the PDF text layer">on page ✓</span>';
    if (p.status === "consensus") return '<span class="prov ok" title="Two independent reads agree">2 reads agree ✓</span>';
    if (p.status === "not_found") return '<span class="prov bad">not on page</span>';
    if (p.status === "conflict") return `<span class="prov bad" title="${esc(p.read_1)} vs ${esc(p.read_2)}">reads disagree</span>`;
    return "";
  };
  const row = (k, label, v) => `<div class="field"><span class="k">${label}</span><span class="v">${v}${P(k)}</span></div>`;
  const tax = f.igst ? `IGST ${inr(f.igst)}` : f.cgst || f.sgst ? `CGST ${inr(f.cgst)} + SGST ${inr(f.sgst)}` : "—";
  return `<div class="card" style="margin-top:16px"><div class="card-head"><h2>Extracted fields</h2><span class="faint small">${f.line_items ? f.line_items.length : 0} line items</span></div>
  <div class="card-pad" style="padding-top:6px"><div class="fields">
    ${row("vendor_name", "Vendor", esc(f.vendor_name || "—"))}
    ${row("vendor_gstin", "Vendor GSTIN", `<span class="mono">${esc(f.vendor_gstin || "—")}</span>`)}
    ${row("invoice_number", "Invoice #", `<span class="mono">${esc(f.invoice_number || "— missing —")}</span>`)}
    ${row("invoice_date", "Date", esc(f.invoice_date || "—"))}
    ${row("po_reference", "PO on invoice", esc(f.po_reference || "— none —"))}
    ${row("buyer_gstin", "Billed-to GSTIN", `<span class="mono">${esc(f.buyer_gstin || "—")}</span>`)}
    ${row("subtotal", "Taxable value", inr(f.subtotal))}
    ${row("tax", "GST", tax)}
    ${row("grand_total", "Grand total", `<b>${inr(f.grand_total)}</b>`)}
    ${row("bank_account_number", "Remit-to A/c", `<span class="mono">${esc(f.bank_account_number || "—")}</span>`)}
    ${row("bank_ifsc", "IFSC", `<span class="mono">${esc(f.bank_ifsc || "—")}</span>`)}
    ${row("rates_include_tax", "Rates incl. GST", f.rates_include_tax ? "Yes" : "No")}
  </div>${f.notes ? `<div class="callout" style="margin-top:12px"><b>Note printed on invoice:</b> ${esc(f.notes)}</div>` : ""}</div></div>`;
}

function decisionCard(r, d) {
  const p = d.payment;
  const reasons = (d.reasons || []).slice(d.decision === "APPROVED" ? 1 : 1);
  return `<div class="decision ${esc(d.decision)}">
    <div class="top">${badge(d.decision)}<div class="headline">${esc(d.headline)}</div></div>
    <p class="reason">${esc(d.decision === "APPROVED" ? d.reasons[0] : d.primary_reason)}</p>
    ${reasons.length ? `<ul>${reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${r.risk_caught > 0 ? `<p class="small" style="margin:10px 0 0"><b>Exposure stopped: ${inr(r.risk_caught)}</b> <span class="muted">— what would have been paid out wrongly without this check</span></p>` : ""}
  </div>
  ${p ? paymentCard(p) : ""}
  ${d.vendor_email ? `<div class="card card-pad" style="margin-bottom:16px"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px"><h3>Drafted email to vendor</h3><button class="btn sm" data-copy>Copy</button></div>
     <div class="small muted" style="margin-bottom:6px">To: ${esc(d.vendor_email.to)} · Subject: ${esc(d.vendor_email.subject)}</div><pre class="email">${esc(d.vendor_email.body)}</pre>
     <div class="faint small" style="margin-top:6px">Draft only — a human sends it. Nothing leaves the building automatically.</div></div>` : ""}
  ${d.internal_tasks && d.internal_tasks.length ? `<div class="card card-pad" style="margin-bottom:16px"><h3>Routed to</h3>${d.internal_tasks.map((t) => `<div class="task"><div class="owner">${esc(t.owner)}</div><div>${esc(t.ask)}</div></div>`).join("")}</div>` : ""}`;
}

function paymentCard(p) {
  return `<div class="card card-pad" style="margin-bottom:16px">
    <h3 style="margin-bottom:10px">Payment proposal</h3>
    <dl class="kv">
      <dt>Approved taxable value</dt><dd>${inr(p.approved_taxable)}</dd>
      <dt>GST (input tax credit)</dt><dd>${inr(p.gst)}</dd>
      <dt>Gross</dt><dd>${inr(p.gross)}</dd>
      <dt>TDS u/s ${esc(p.tds_section)} @ ${p.tds_rate_pct}%</dt><dd>−${inr(p.tds)}</dd>
      <div class="sep"></div>
      <dt><b>Net payable to vendor</b></dt><dd class="strong">${inr(p.net_payable)}</dd>
      ${p.held_amount ? `<dt>Held (credit note requested)</dt><dd>${inr(p.held_amount)}</dd>` : ""}
      <dt>Due date</dt><dd>${esc(p.due_date)} <span class="faint">(${p.terms_days} days${p.msme ? ", MSME ≤45-day rule" : ""})</span></dd>
      <dt>Pay to</dt><dd>${esc(p.pay_to)}</dd>
      <dt>Booking</dt><dd>${esc(p.gl_code)} · ${esc(p.cost_center)}</dd>
    </dl>
    <details style="margin-top:10px"><summary class="small" style="cursor:pointer;color:var(--accent-ink)">Journal entry (ready to post)</summary>
      <div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>Account</th><th class="num">Dr</th><th class="num">Cr</th></tr></thead><tbody>
      ${p.journal.map((j) => `<tr><td>${esc(j.account)}</td><td class="num">${j.dr ? inr(j.dr) : ""}</td><td class="num">${j.cr ? inr(j.cr) : ""}</td></tr>`).join("")}
      </tbody></table></div></details>
  </div>`;
}

function wireRunPanel(root, st, rerender) {
  root.querySelectorAll(".stage-row").forEach((row) => {
    row.onclick = () => {
      const id = row.parentElement.dataset.stage;
      st.open.has(id) ? st.open.delete(id) : st.open.add(id);
      rerender();
    };
  });
  const rt = root.querySelector("[data-retry]");
  if (rt && st.retry) rt.onclick = st.retry;
  const cp = root.querySelector("[data-copy]");
  if (cp) cp.onclick = () => {
    const pre = cp.closest(".card").querySelector("pre");
    navigator.clipboard && navigator.clipboard.writeText(pre.textContent);
    cp.textContent = "Copied";
  };
}

// ───────────── views ─────────────
async function viewRun() {
  if (!SAMPLES) SAMPLES = await api("/api/samples").catch(() => []);
  const demo = SAMPLES.filter((s) => s.kind === "demo");
  const other = SAMPLES.filter((s) => s.kind !== "demo");
  const tagFor = (s, i) => (i === 0 ? '<span class="tag happy">Happy path</span>' : `<span class="tag edge">Edge case ${i}</span>`);
  view.innerHTML = `
  <div class="page-head"><div><h1>Process an invoice</h1>
    <p>Drop a vendor invoice PDF (digital or scanned). It's read by an AI model, every value is verified against the document, then a rules engine checks the vendor master, POs, goods receipts, the AP ledger and GST law, and makes a decision you can audit line by line.</p></div></div>
  <div class="intake">
    <label class="drop" id="drop">
      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" style="color:var(--text-3)"><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5M12 18v-6M9 15l3-3 3 3"/></svg>
      <div class="big">Drop an invoice PDF here</div>
      <div class="muted small">or click to choose a file · max 4 MB</div>
      <input type="file" accept="application/pdf" id="file" hidden />
    </label>
    <div class="card samples">
      <div class="card-head"><h2>Demo invoices</h2><span class="faint small">realistic GST invoices · click to run</span></div>
      ${demo.map((s, i) => `<button class="sample" data-sample="${esc(s.id)}">${tagFor(s, i)}<div><div class="t">${esc(s.title.replace(/^Edge: /, ""))}</div><div class="s">${esc(s.id)}.pdf${s.scanned ? " · scanned" : ""}</div></div><a class="faint small" href="/api/samples/${esc(s.id)}.pdf" target="_blank" onclick="event.stopPropagation()">view PDF</a></button>`).join("")}
      <details class="more"><summary>+ ${other.length} more test invoices (accuracy set)</summary>
        ${other.map((s) => `<button class="sample" data-sample="${esc(s.id)}"><span class="tag">${esc(s.id.split("_")[0])}</span><div><div class="t">${esc(s.title)}</div><div class="s">${esc(s.id)}.pdf</div></div><a class="faint small" href="/api/samples/${esc(s.id)}.pdf" target="_blank" onclick="event.stopPropagation()">view PDF</a></button>`).join("")}
      </details>
    </div>
  </div>
  <div id="runArea" style="margin-top:22px"></div>`;

  const area = $("#runArea");
  const rerender = () => {
    if (!live) { area.innerHTML = ""; return; }
    area.innerHTML = renderRunPanel(live, { live: true });
    wireRunPanel(area, live, rerender);
  };
  rerender();

  const start = (st, url, opts) => {
    st.retry = () => { const n = newState(); n.filename = st.filename; n.pageUrl = st.pageUrl; n.retry = null; start(n, url, opts); };
    live = st;
    rerender();
    area.scrollIntoView({ behavior: "smooth", block: "start" });
    streamRun(url, opts, st, () => { if (live === st && location.hash.startsWith("#/run")) rerender(); });
  };
  view.querySelectorAll("[data-sample]").forEach((b) => (b.onclick = () => {
    if (live && live.running) return;
    const st = newState();
    st.filename = b.dataset.sample + ".pdf";
    st.pageUrl = `/api/samples/${b.dataset.sample}.jpg`;
    start(st, `/api/runs/sample/${encodeURIComponent(b.dataset.sample)}`);
  }));
  const drop = $("#drop"), input = $("#file");
  const go = (file) => {
    if (!file || (live && live.running)) return;
    const st = newState();
    st.filename = file.name;
    const fd = new FormData();
    fd.append("file", file);
    start(st, "/api/runs", { body: fd });
  };
  input.onchange = () => go(input.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); go(e.dataTransfer.files[0]); };
}

async function viewRunDetail(id) {
  view.innerHTML = `<div class="empty">Loading run…</div>`;
  let r;
  try { r = await api(`/api/runs/${encodeURIComponent(id)}`); } catch (e) { view.innerHTML = `<div class="empty">Run not found.</div>`; return; }
  const st = newState();
  (r.events || []).forEach((ev) => applyEvent(st, ev));
  if (!st.stages.length) {
    // seeded history has no event log — rebuild the stage list from the stored result
    st.stages = await api("/api/stages");
  }
  st.result = r; st.running = false; st.filename = r.filename; st.pageUrl = `/api/runs/${r.id}/page.jpg`;
  const rerender = () => {
    view.innerHTML = `<div class="page-head"><div><a href="#/dashboard" class="small">← Dashboard</a><h1 style="margin-top:4px">Run ${esc(r.id.slice(0, 8))}</h1>
      <p>${esc(r.vendor_name || "Unknown vendor")} · ${esc(r.invoice_number || "no invoice no.")} · ${inr(r.total)} · processed ${ago(r.created_at)} (${esc(r.source)})</p></div>
      <a class="btn sm" href="/api/runs/${esc(r.id)}/file.pdf" target="_blank">Original PDF</a></div>` + renderRunPanel(st, {});
    wireRunPanel(view, st, rerender);
  };
  rerender();
}

let dashFilter = { decision: "ALL", q: "" };
async function viewDashboard() {
  view.innerHTML = `<div class="empty">Loading…</div>`;
  const [s, runs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
  const order = ["APPROVED", "PARTIALLY_APPROVED", "ON_HOLD", "REJECTED"];
  const n = s.runs || 0;
  const maxEx = Math.max(1, ...s.exceptions.map((e) => e[1]));
  view.innerHTML = `
  <div class="page-head"><div><h1>Dashboard</h1><p>Every invoice this desk has processed — decisions, what was caught, and where humans were needed.</p></div>
    <div style="display:flex;gap:8px"><a class="btn primary sm" href="#/run">+ Process invoice</a><button class="btn sm" id="resetBtn" title="Remove runs made during the demo; keep seeded history">Reset demo runs</button></div></div>
  <div class="kpis">
    <div class="card kpi"><div class="label">Invoices processed</div><div class="value">${n}</div><div class="sub">${inrShort(s.processed_value)} billed · ${Math.round(s.scanned_share * 100)}% scans</div></div>
    <div class="card kpi"><div class="label">Touchless rate</div><div class="value">${Math.round(s.touchless_rate * 100)}%</div><div class="sub">approved with zero human touch</div></div>
    <div class="card kpi"><div class="label">Exposure stopped</div><div class="value">${inrShort(s.risk_caught)}</div><div class="sub">duplicates, fraud, over-billing</div></div>
    <div class="card kpi"><div class="label">Approved for payment</div><div class="value">${inrShort(s.approved_value)}</div><div class="sub">incl. GST, before TDS</div></div>
    <div class="card kpi"><div class="label">Avg. time per invoice</div><div class="value">${(s.avg_ms / 1000).toFixed(1)}s</div><div class="sub">vs ~10–15 min by hand</div></div>
  </div>
  <div class="charts">
    <div class="card card-pad"><h2 style="margin-bottom:12px">Decision mix</h2>
      <div class="stack">${order.filter((k) => s.by_decision[k]).map((k) => `<div style="width:${(s.by_decision[k] / n) * 100}%;background:${STATUS_COLOR[k]}" data-tip="<b>${DEC[k].label}</b><br>${s.by_decision[k]} of ${n} invoices (${Math.round((s.by_decision[k] / n) * 100)}%)"></div>`).join("")}</div>
      <div class="legend">${order.map((k) => `<span class="item"><span class="sw" style="background:${STATUS_COLOR[k]}"></span>${DEC[k].label} <b>${s.by_decision[k] || 0}</b></span>`).join("")}</div>
      <h3 style="margin:22px 0 8px">Exceptions routed to</h3>
      ${s.owners && s.owners.length ? (() => { const mx = Math.max(...s.owners.map((o) => o[1])); return s.owners.map(([o, c]) => `<div class="hbar" data-tip="<b>${esc(o)}</b><br>${c} invoice${c > 1 ? "s" : ""} waiting on them"><span class="name">${esc(o)}</span><div class="track"><div class="fill" style="width:${(c / mx) * 100}%"></div></div><span class="val">${c}</span></div>`).join(""); })() : '<div class="faint small">Nothing waiting on anyone</div>'}
    </div>
    <div class="card card-pad"><h2 style="margin-bottom:10px">Why invoices weren't auto-approved</h2>
      ${s.exceptions.length ? s.exceptions.map(([code, c]) => `<div class="hbar" data-tip="<b>${esc(CODE_LABEL[code] || code)}</b><br>${c} invoice${c > 1 ? "s" : ""}"><span class="name">${esc(CODE_LABEL[code] || code)}</span><div class="track"><div class="fill" style="width:${(c / maxEx) * 100}%"></div></div><span class="val">${c}</span></div>`).join("") : '<div class="faint">No exceptions yet</div>'}
    </div>
  </div>
  <div class="card">
    <div class="card-head" style="flex-wrap:wrap"><h2>Runs</h2>
      <div class="filters">${["ALL", ...order].map((k) => `<button class="chip ${dashFilter.decision === k ? "on" : ""}" data-f="${k}">${k === "ALL" ? "All" : DEC[k].label}</button>`).join("")}
      <input class="search" id="q" placeholder="Search vendor, invoice #…" value="${esc(dashFilter.q)}" /></div></div>
    <div class="table-wrap"><table><thead><tr><th>When</th><th>Invoice</th><th>Vendor</th><th class="num">Amount</th><th>Decision</th><th>Main reason</th><th>Doc</th><th class="num">Time</th></tr></thead><tbody id="rows"></tbody></table></div>
  </div>`;
  bindTips(view);
  const drawRows = () => {
    const q = dashFilter.q.toLowerCase();
    const rows = runs.filter((r) => (dashFilter.decision === "ALL" || r.decision === dashFilter.decision) && (!q || `${r.vendor_name} ${r.invoice_number} ${r.filename}`.toLowerCase().includes(q)));
    $("#rows").innerHTML = rows.length ? rows.map((r) => {
      const code = (r.summary || {}).primary_code;
      return `<tr class="click" data-id="${esc(r.id)}"><td class="faint" style="white-space:nowrap">${ago(r.created_at)}</td><td class="mono">${esc(r.invoice_number || "—")}</td><td>${esc(r.vendor_name || "—")}</td><td class="num">${inr(r.total)}</td><td>${badge(r.decision)}</td><td class="muted">${r.decision === "APPROVED" ? ((r.summary || {}).inferred_po ? "Clean · PO inferred" : "Clean 3-way match") : esc(CODE_LABEL[code] || code || "—")}</td><td class="faint">${esc(r.doc_kind || "")}</td><td class="num faint">${ms(r.duration_ms)}</td></tr>`;
    }).join("") : `<tr><td colspan="8" class="empty">No runs match</td></tr>`;
    view.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => (location.hash = "#/runs/" + tr.dataset.id)));
  };
  drawRows();
  view.querySelectorAll("[data-f]").forEach((b) => (b.onclick = () => { dashFilter.decision = b.dataset.f; viewDashboard(); }));
  $("#q").oninput = (e) => { dashFilter.q = e.target.value; drawRows(); };
  $("#resetBtn").onclick = async () => {
    if (!confirm("Remove all runs made during the demo (seeded history stays)?")) return;
    await api("/api/reset", { method: "POST" });
    live = null;
    viewDashboard();
  };
}

async function viewMaster() {
  view.innerHTML = `<div class="empty">Loading…</div>`;
  const m = await api("/api/master");
  const pct = (a, b) => (b ? Math.min(100, (a / b) * 100) : 0);
  view.innerHTML = `
  <div class="page-head"><div><h1>Procurement data</h1><p>The system of record the invoice is checked against — in production these are live reads from the ERP (Tally / SAP / Zoho). Buyer: <b>${esc(m.buyer.name)}</b>, GSTIN <span class="mono">${esc(m.buyer.gstin)}</span>, ${esc(m.buyer.state)}.</p></div></div>
  <div class="card" style="margin-bottom:16px"><div class="card-head"><h2>AP policy</h2><span class="faint small">tolerances the rules engine applies</span></div>
    <div class="card-pad"><dl class="kv" style="max-width:640px">
      <dt>Unit-price tolerance vs PO</dt><dd>≤ ${m.policy.price_tolerance_pct}% → approve with note; above → hold for buyer</dd>
      <dt>Cumulative PO overrun</dt><dd>≤ ${m.policy.po_overrun_tolerance_pct}%</dd>
      <dt>Arithmetic rounding slack</dt><dd>₹${m.policy.rounding_tolerance_inr}</dd>
      <dt>Inferred-PO auto-accept</dt><dd>score ≥ ${m.policy.po_inference_min_score} and ${m.policy.po_inference_min_margin} ahead of runner-up</dd>
      <dt>Fuzzy duplicate window</dt><dd>same vendor, amount ±${m.policy.duplicate_amount_pct}%, ${m.policy.duplicate_window_days} days</dd>
      <dt>Auto-approval limit</dt><dd>${inr(m.policy.cfo_approval_above_inr)} (above → CFO)</dd>
      <dt>MSME payment cap</dt><dd>${m.policy.msme_max_payment_days} days (MSMED Act s.15, IT Act s.43B(h))</dd>
    </dl></div></div>
  <div class="card" style="margin-bottom:16px"><div class="card-head"><h2>Vendor master</h2></div><div class="table-wrap"><table>
    <thead><tr><th>Vendor</th><th>GSTIN</th><th>Status</th><th>MSME</th><th class="num">Terms</th><th>TDS</th><th>Verified bank</th></tr></thead><tbody>
    ${m.vendors.map((v) => `<tr><td><b>${esc(v.name)}</b><div class="faint small">${esc(v.id)} · ${esc(v.category)}</div></td><td class="mono">${esc(v.gstin)}</td>
      <td>${v.status === "active" ? '<span class="badge APPROVED">' + DEC.APPROVED.icon + "Active</span>" : '<span class="badge REJECTED">' + DEC.REJECTED.icon + "Blocked</span><div class='faint small' style='max-width:220px'>" + esc(v.block_reason || "") + "</div>"}</td>
      <td>${esc(v.msme || "—")}</td><td class="num">${v.payment_terms_days} d</td><td>${esc(v.tds_section)} @ ${v.tds_rate_pct}%</td><td class="small">${esc(v.bank.bank_name)}<div class="mono faint">••••${esc(v.bank.account_number.slice(-4))} · ${esc(v.bank.ifsc)}</div></td></tr>`).join("")}
    </tbody></table></div></div>
  <div class="card" style="margin-bottom:16px"><div class="card-head"><h2>Purchase orders · goods receipts · billing</h2>
    <div class="legend" style="margin:0"><span class="item"><span class="sw" style="background:var(--series-1)"></span>Billed</span><span class="item"><span class="sw" style="background:color-mix(in srgb, var(--series-1) 35%, var(--surface-3))"></span>Received, not billed</span><span class="item"><span class="sw" style="background:var(--surface-3)"></span>Not received</span></div></div>
    <div class="table-wrap"><table><thead><tr><th>PO / line</th><th class="num">Unit price</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed</th><th style="min-width:160px">Progress</th></tr></thead><tbody>
    ${m.purchase_orders.map((po) => `<tr><td colspan="6" style="background:var(--surface-2)"><b>${esc(po.po_number)}</b> · ${esc(po.vendor_name)} · ${esc(po.date)} · ${inr(po.total)} · <span class="${po.status === "open" ? "" : "faint"}">${esc(po.status)}</span> <span class="faint small">· ${esc(po.gl_code)}</span></td></tr>` +
      po.lines.map((l) => `<tr><td style="padding-left:24px">L${l.line} ${esc(l.description)} <span class="faint small">HSN ${esc(l.hsn)}</span></td><td class="num">${inr(l.unit_price)}</td><td class="num">${qty(l.qty)}</td><td class="num">${qty(l.received)}</td><td class="num">${qty(l.billed)}</td>
        <td><div class="meter" data-tip="${esc(l.description)}<br>Billed ${qty(l.billed)} · received ${qty(l.received)} · ordered ${qty(l.qty)}"><div style="width:${pct(Math.min(l.billed, l.qty), l.qty)}%;background:var(--series-1)"></div><div style="width:${pct(Math.max(0, l.received - l.billed), l.qty)}%;background:color-mix(in srgb, var(--series-1) 35%, var(--surface-3))"></div></div></td></tr>`).join("")).join("")}
    </tbody></table></div></div>
  <div class="card"><div class="card-head"><h2>AP ledger (invoices booked before go-live)</h2></div><div class="table-wrap"><table>
    <thead><tr><th>Invoice</th><th>Vendor</th><th>Date</th><th>PO</th><th class="num">Total</th><th>Status</th></tr></thead><tbody>
    ${m.invoice_ledger.map((l) => `<tr><td class="mono">${esc(l.invoice_number)}</td><td>${esc((m.vendors.find((v) => v.id === l.vendor_id) || {}).name)}</td><td>${esc(l.invoice_date)}</td><td>${esc(l.po_number)}</td><td class="num">${inr(l.total)}</td><td>${esc(l.status)}</td></tr>`).join("")}
    </tbody></table></div></div>`;
  bindTips(view);
}

async function viewEval() {
  view.innerHTML = `<div class="empty">Loading…</div>`;
  const ev = await api("/api/eval");
  const e2e = ev.end_to_end, orc = ev.oracle;
  const pctTxt = (x) => (x == null ? "—" : Math.round(x * 100) + "%");
  const rep = e2e || orc;
  view.innerHTML = `
  <div class="page-head"><div><h1>Accuracy</h1><p>Every test invoice has a ground-truth label (the decision a careful senior AP analyst would make). The harness runs each one in isolation and scores it. Two numbers, on purpose: <b>end-to-end</b> (real AI extraction from the PDF) and <b>rules-only</b> (perfect extraction fed in), so a reading error can always be told apart from a logic error.</p></div></div>
  <div class="kpis" style="grid-template-columns:repeat(4,minmax(0,1fr))">
    <div class="card kpi"><div class="label">Decision accuracy · end-to-end</div><div class="value">${e2e ? pctTxt(e2e.accuracy_when_answered ?? e2e.decision_accuracy) : "—"}</div><div class="sub">${e2e ? `${e2e.rows.filter((r) => r.correct).length}/${e2e.rows.filter((r) => r.got !== "ERROR").length} read by the AI · ${pctTxt(e2e.availability)} model availability (free tier)` : "not run yet"}</div></div>
    <div class="card kpi"><div class="label">Decision accuracy · rules only</div><div class="value">${orc ? pctTxt(orc.decision_accuracy) : "—"}</div><div class="sub">${orc ? `${orc.n} invoices · ground-truth extraction` : "not run yet"}</div></div>
    <div class="card kpi"><div class="label">Field accuracy · end-to-end</div><div class="value">${e2e ? pctTxt(Object.values(e2e.field_accuracy).reduce((a, b) => a + b, 0) / Object.values(e2e.field_accuracy).length) : "—"}</div><div class="sub">invoice #, GSTIN, date, total, PO</div></div>
    <div class="card kpi"><div class="label">Avg. time per invoice</div><div class="value">${e2e ? (e2e.avg_ms / 1000).toFixed(1) + "s" : "—"}</div><div class="sub">incl. model calls${e2e ? " · " + new Date(e2e.generated_at).toLocaleDateString("en-IN") : ""}</div></div>
  </div>
  ${rep ? `<div class="card"><div class="card-head"><h2>Per-invoice results ${e2e ? "(end-to-end)" : "(rules only)"}</h2></div><div class="table-wrap"><table>
    <thead><tr><th>Case</th><th>Doc</th><th>Expected</th><th>Got</th><th></th><th>Fields correct</th><th>Why</th></tr></thead><tbody>
    ${rep.rows.map((r) => `<tr><td><b>${esc(r.id)}</b><div class="faint small">${esc(r.title)}</div></td><td class="faint">${r.scanned ? "scan" : "digital"}</td><td>${badge(r.expected)}</td><td>${badge(r.got)}</td><td>${r.correct ? '<span style="color:var(--good-ink);font-weight:700">✓</span>' : '<span style="color:var(--critical-ink);font-weight:700">✕</span>'}</td>
      <td class="small">${Object.entries(r.fields).map(([k, v]) => `<span class="prov ${v ? "ok" : "bad"}" style="margin:0 3px 3px 0;display:inline-block">${esc(k.replace("_", " "))}</span>`).join("")}</td><td class="muted small" style="max-width:380px">${esc(r.reason || "")}</td></tr>`).join("")}
    </tbody></table></div></div>` : ""}`;
}

function viewHow() {
  const steps = [
    ["Intake & fingerprint", "SHA-256 of the file (catches re-sent files). Detects digital PDF vs scan — scans have no text layer and need a different verification strategy."],
    ["AI extraction", "Gemini reads the PDF/scan into a strict JSON schema. Instructed to copy identifiers character-for-character and return null instead of guessing (e.g. never invent a PO number)."],
    ["Verify extraction", "Don't trust the model. Digital PDFs: every critical value must literally exist in the PDF text layer. Scans: a second, independent read must agree. Plus the invoice's own arithmetic must reconcile (qty×rate, Σlines, +GST = total)."],
    ["Vendor & bank", "Match by GSTIN (then fuzzy name). Validate GSTIN checksum. Vendor must be active. Remit-to bank must equal the verified account on file — a changed account is the #1 invoice-fraud pattern."],
    ["Duplicate detection", "Against the AP ledger and every past run: same file, same number after normalising (SGS/26-27/0087 ≡ SGS-2627-87), or same vendor + amount + window + PO."],
    ["PO · GRN · invoice", "Explicit PO, or infer it from line items + open quantities when missing. Per line: price vs PO (tolerance), billed qty vs received (GRN) minus already billed — so split invoices can't over-bill a PO."],
    ["GST compliance", "Billed to our GSTIN; intra-state → CGST+SGST, inter-state → IGST; tax = taxable × rate. Wrong tax type = lost input tax credit."],
    ["Decision & actions", "Ranked outcomes: Reject (vendor must fix the document) › Hold (an internal owner must answer a specific question) › Partial (pay what's supported) › Approve. Approvals get a payment proposal with TDS, MSME due date and a journal entry; rejections get a drafted vendor email."],
  ];
  view.innerHTML = `
  <div class="page-head"><div><h1>How it works</h1><p><b>AI reads, rules decide, humans resolve.</b> The model is used only where judgment about messy pixels is needed. Every money decision comes from explicit, auditable rules, so each outcome can be explained in one sentence to an auditor.</p></div></div>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-bottom:16px">
    ${steps.map((s, i) => `<div class="card card-pad"><div style="display:flex;gap:10px;align-items:center;margin-bottom:6px"><span class="badge neutral">${i + 1}</span><b>${esc(s[0])}</b></div><div class="muted small">${esc(s[1])}</div></div>`).join("")}
  </div>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))">
    <div class="card card-pad"><h3 style="margin-bottom:8px">Design choices</h3><ul class="muted small" style="margin:0;padding-left:18px;line-height:1.7">
      <li>LLM self-reported confidence is poorly calibrated, so confidence comes from <i>checks</i> (grounding, dual-read, arithmetic, checksums) rather than the model's opinion of itself.</li>
      <li>Holds are internal and routed to a named owner with a specific question; rejections go back to the vendor with exactly what to fix.</li>
      <li>Bank-change holds never email the vendor — verification happens via the phone number already on file.</li>
      <li>Partial approval pays what's provably owed now instead of holding the whole invoice (keeps MSME vendors paid on time).</li>
      <li>Model fallback chain + retries; if extraction fails entirely the invoice goes to a manual queue, never auto-approved.</li>
    </ul></div>
    <div class="card card-pad"><h3 style="margin-bottom:8px">Assumptions</h3><ul class="muted small" style="margin:0;padding-left:18px;line-height:1.7">
      <li>Buyer is a Karnataka manufacturer; all vendor/PO/GRN data is synthetic but structurally realistic.</li>
      <li>Business date fixed at 20 Nov 2026 so demo results are reproducible.</li>
      <li>TDS: 194Q at 0.1% assumes the ₹50L annual threshold is already crossed for goods vendors; 194J(a) 2% for technical services. TDS on taxable value excl. GST.</li>
      <li>Single-page invoices; one GST rate per invoice (18%).</li>
      <li>Held invoices don't reserve PO balance; only approved quantities consume it.</li>
      <li>Production intake would be an AP mailbox; here it's upload or sample.</li>
    </ul></div>
  </div>`;
}

// ───────────── router ─────────────
async function route() {
  const h = location.hash || "#/run";
  const [, page, arg] = h.split("/");
  document.querySelectorAll("nav.tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === (page === "runs" ? "dashboard" : page)));
  tip.style.opacity = 0;
  try {
    if (page === "dashboard") await viewDashboard();
    else if (page === "runs" && arg) await viewRunDetail(arg);
    else if (page === "master") await viewMaster();
    else if (page === "eval") await viewEval();
    else if (page === "how") viewHow();
    else await viewRun();
  } catch (e) {
    view.innerHTML = `<div class="empty">Something went wrong: ${esc(e.message)}</div>`;
  }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
route();
