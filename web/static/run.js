"use strict";

const RUN_ID = location.pathname.split("/")[2];
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (n) => (n >= 0 ? "+" : "") + Number(n).toFixed(3);

// Normalize the two blob shapes into a common row list + unmatched list.
//   scan: [ opp, ... ]
//   max : { matched: [ rec, ... ], unmatched: [ title, ... ] }
function normalize(result) {
  if (Array.isArray(result)) return { rows: result, unmatched: [] };
  if (result && Array.isArray(result.matched))
    return { rows: result.matched, unmatched: result.unmatched || [] };
  return { rows: [], unmatched: [] };
}

// Priced? A max "matched" rec without an opp has no profit/confirmed fields.
function isPriced(r) {
  return typeof r.profit === "number";
}

function sortRows(rows) {
  return rows.slice().sort((a, b) => {
    const pa = isPriced(a) ? a.profit : -Infinity;
    const pb = isPriced(b) ? b.profit : -Infinity;
    return pb - pa;
  });
}

function badge(r) {
  const v = r.validation;
  if (v && !(v.same_event && v.equivalent_payoff))
    return { cls: "different", txt: "different event ❌" };
  if (!isPriced(r)) return { cls: "different", txt: "unpriced" };
  // No validation ran (e.g. --no-llm, or beyond the --max-validations cap): the
  // same-event/equivalent-payoff check is what makes profit=1-cost risk-free, so
  // an unvalidated pair is NOT a confirmed arb no matter how the prices confirm.
  if (!v) return { cls: "unvalidated", txt: "⚠ unvalidated" };
  if (r.confirmed) return { cls: "confirmed", txt: "confirmed ✅" };
  return { cls: "unconfirmed", txt: "⚠ unconfirmed" };
}

function legHtml(leg) {
  const unconf = leg.confirmed ? "" : '<span class="unconf" title="Gamma mid + haircut, not an executable ask">~</span>';
  return `<span class="leg"><b>BUY ${esc(leg.platform)} ${esc(leg.side)}</b> @ $${Number(leg.price).toFixed(3)}${unconf}
    <span class="plat">+ fee $${Number(leg.fee).toFixed(4)}</span></span>`;
}

function sizingHtml(s) {
  if (!s) return "";
  const n = (x) => Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 });
  let line = `SIZE: max <b>~${n(s.recommended_size)} pairs</b>
    (edge-exhaustion ${n(s.edge_exhaustion_size)}, depth ceiling ${n(s.depth_ceiling_size)}, book cap ${n(s.max_fillable)})`;
  if (s.recommended_size > 0) {
    line += ` → <b>$${Number(s.profit_at_recommended).toLocaleString(undefined, { maximumFractionDigits: 2 })}</b> profit`;
  }
  return `<div class="sizing">${line}</div>`;
}

function oppHtml(r) {
  const pm = r.polymarket || {};
  const ks = r.kalshi || {};
  const b = badge(r);
  const priced = isPriced(r);
  const v = r.validation;
  const validated = !!(v && v.same_event && v.equivalent_payoff);
  // Only a validated, positive edge is shown as green "net profit". An
  // unvalidated edge is muted and labeled as such — it is not a confirmed arb.
  const profitCls = priced && r.profit > 0 ? (validated ? "pos" : "pending") : "neg";
  const kindTxt = !validated
    ? "unvalidated edge"
    : r.profit_kind === "net_profit" ? "net profit" : "indicative edge";

  const profitBlock = priced
    ? `<div class="profit ${profitCls}">$${money(r.profit)}<span class="kind">${kindTxt}${
        typeof r.roi === "number" ? " · ROI " + (r.roi * 100).toFixed(1) + "%" : ""
      }</span></div>`
    : `<div class="profit"><span class="kind">not priced</span></div>`;

  const legs = Array.isArray(r.legs) && r.legs.length
    ? `<div class="legs"><span class="legs-lab">To capture, buy both legs:</span>${r.legs.map(legHtml).join("")}${
        typeof r.cost === "number"
          ? `<span class="leg-total">total cost $${Number(r.cost).toFixed(3)} → $${money(r.profit)}/pair</span>`
          : ""
      }</div>`
    : "";

  const llm = v
    ? `<div class="llm"><b>LLM:</b> same_event=${v.same_event} · equivalent_payoff=${v.equivalent_payoff} · conf=${Number(v.confidence).toFixed(2)}<br>${esc(v.reasoning)}${
        (v.caveats || []).map((c) => `<br><span style="color:var(--warn)">caveat:</span> ${esc(c)}`).join("")
      }</div>`
    : "";

  const boundNote = r.kalshi_fee_is_bound
    ? `<span class="tag" title="Kalshi fee is an upper bound at size 1">fee upper-bound</span>`
    : "";

  return `<div class="opp ${b.cls}">
    <div class="opp-top">
      ${profitBlock}
      <div>
        <span class="badge ${b.cls}">${b.txt}</span>
        ${typeof r.similarity === "number" ? `<span class="tag">sim ${r.similarity.toFixed(2)}</span>` : ""}
        ${boundNote}
      </div>
    </div>
    ${legs}
    <div class="qrow"><span class="lab">PM</span> ${esc(pm.question)} ${pm.url ? `<a href="${esc(pm.url)}" target="_blank" rel="noopener">↗</a>` : ""}</div>
    <div class="qrow"><span class="lab">KS</span> ${esc(ks.question)} ${ks.url ? `<a href="${esc(ks.url)}" target="_blank" rel="noopener">↗</a>` : ""}</div>
    ${sizingHtml(r.sizing)}
    ${llm}
  </div>`;
}

function renderMeta(run) {
  const el = $("runmeta");
  const kv = (k, v, extra = "") => `<div class="kv ${extra}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  el.innerHTML =
    `<div class="runid-box kv"><div class="k">run id</div><div class="v">${esc(run.id)}</div></div>` +
    kv("type", esc(run.type)) +
    kv("status", `<span class="status ${run.status}">${run.status}</span>`) +
    kv("launched by", esc(run.launched_by)) +
    kv("real LLM calls", run.llm_calls) +
    kv("started", esc(run.started_at ? run.started_at.replace("T", " ").slice(0, 19) : "–"));
}

function render(run) {
  renderMeta(run);
  const body = $("body");

  if (run.status === "running" || run.status === "queued") {
    body.innerHTML = `<div class="empty"><span class="spin"></span> Run is ${run.status}. This page updates automatically when it finishes.</div>`;
    return true; // keep polling
  }
  if (run.status === "failed") {
    body.innerHTML = `<div class="opp different"><b style="color:var(--danger)">Run failed</b> (exit ${run.exit_code}).
      <pre class="mono" style="white-space:pre-wrap;color:var(--muted);font-size:.8rem;margin-top:.6rem">${esc(run.error_tail || "no error output captured")}</pre></div>`;
    return false;
  }
  if (run.status === "cancelled") {
    body.innerHTML = `<div class="empty">Run was cancelled.</div>`;
    return false;
  }

  // done
  const { rows, unmatched } = normalize(run.result);
  if (!rows.length) {
    body.innerHTML = `<div class="empty">No candidate opportunities returned.</div>`;
  } else {
    const html = sortRows(rows).map(oppHtml).join("");
    const unmatchedHtml = unmatched.length
      ? `<div class="section-head">No counterpart found (${unmatched.length})</div>
         <div class="unmatched">${unmatched.map((t) => "• " + esc(t)).join("<br>")}</div>`
      : "";
    body.innerHTML = `<div class="section-head">${rows.length} candidate${rows.length === 1 ? "" : "s"}, sorted by profit</div>${html}${unmatchedHtml}`;
  }
  return false;
}

async function poll() {
  try {
    const r = await fetch("/runs/" + RUN_ID);
    if (r.status === 404) {
      $("body").innerHTML = '<div class="empty">Run not found.</div>';
      return;
    }
    const run = await r.json();
    const keepGoing = render(run);
    if (keepGoing) setTimeout(poll, 2000);
  } catch (e) {
    setTimeout(poll, 3000);
  }
}

poll();
