"use strict";

const RUN_ID = location.pathname.split("/")[2];
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (n) => (n >= 0 ? "+" : "−") + "$" + Math.abs(Number(n)).toFixed(3);
const nfmt = (x) => Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 });

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

function argSummary(args) {
  const parts = [];
  if (args.section) parts.push("section=" + args.section);
  if (args.sections) parts.push(args.sections);
  if (args.per_section != null) parts.push("cap=" + args.per_section);
  if (args.min_profit != null) parts.push("min$" + args.min_profit);
  if (args.min_volume != null) parts.push("vol≥$" + args.min_volume);
  if (args.max_validations != null) parts.push("mv=" + args.max_validations);
  if (args.no_llm) parts.push("no-llm");
  return parts.join("  ·  ") || "defaults";
}

function badge(r) {
  const v = r.validation;
  if (v && !(v.same_event && v.equivalent_payoff))
    return { cls: "different", txt: "different event ✕" };
  if (!isPriced(r)) return { cls: "different", txt: "unpriced" };
  // No validation ran (e.g. --no-llm, or beyond the --max-validations cap): the
  // same-event/equivalent-payoff check is what makes profit=1-cost risk-free, so
  // an unvalidated pair is NOT a confirmed arb no matter how the prices confirm.
  if (!v) return { cls: "unvalidated", txt: "⚠ unvalidated" };
  if (r.confirmed) return { cls: "confirmed", txt: "confirmed ✓" };
  return { cls: "unconfirmed", txt: "⚠ unconfirmed" };
}

function legHtml(leg) {
  const unconf = leg.confirmed
    ? ""
    : '<span class="unconf" title="Gamma mid + haircut, not an executable ask">~</span>';
  return `<div class="leg"><span class="b"><span class="buy">BUY</span> ${esc(leg.platform)} ${esc(leg.side)} @ $${Number(leg.price).toFixed(3)}${unconf}</span>
    <span class="f">+ fee $${Number(leg.fee).toFixed(4)}</span></div>`;
}

function sizingHtml(s) {
  if (!s) return "";
  let line = `SIZE — max <b>~${nfmt(s.recommended_size)} pairs</b>
    (edge-exhaustion ${nfmt(s.edge_exhaustion_size)}, depth ${nfmt(s.depth_ceiling_size)}, book cap ${nfmt(s.max_fillable)})`;
  if (s.recommended_size > 0) {
    line += ` → <b>$${Number(s.profit_at_recommended).toLocaleString(undefined, { maximumFractionDigits: 2 })}</b> captured`;
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
  const profitCls = priced ? (r.profit > 0 ? (validated ? "pos" : "pending") : "neg") : "pending";
  const kindTxt = !priced
    ? "not priced"
    : !validated
    ? "unvalidated edge"
    : r.profit_kind === "net_profit"
    ? "net profit / pair"
    : "indicative edge";

  const profitBlock = priced
    ? `<div class="profit"><span class="p ${profitCls}">${money(r.profit)}</span><span class="kind">${kindTxt}${
        typeof r.roi === "number" ? " · ROI " + (r.roi * 100).toFixed(1) + "%" : ""
      }</span></div>`
    : `<div class="profit"><span class="p pending">—</span><span class="kind">not priced</span></div>`;

  const legs = Array.isArray(r.legs) && r.legs.length
    ? `<div class="legs-lab">To capture — buy both legs</div><div class="legs">${r.legs.map(legHtml).join("")}${
        typeof r.cost === "number"
          ? `<span class="leg-total">cost <b>$${Number(r.cost).toFixed(3)}</b> → ${money(r.profit)}/pair</span>`
          : ""
      }</div>`
    : "";

  const llm = v
    ? `<div class="llm"><div class="flags"><span class="h">LLM check</span> same event <span class="${
        v.same_event ? "ok" : "no"
      }">${v.same_event ? "✓" : "✕"}</span> · equivalent payoff <span class="${
        v.equivalent_payoff ? "ok" : "no"
      }">${v.equivalent_payoff ? "✓" : "✕"}</span> · confidence ${Number(v.confidence).toFixed(2)}</div>${esc(v.reasoning)}${
        (v.caveats || []).map((c) => `<div class="caveat">▲ ${esc(c)}</div>`).join("")
      }</div>`
    : `<div class="llm"><span class="h">LLM check</span> not run for this pair — profit shown is arithmetic only, not a confirmed arb.</div>`;

  const boundNote = r.kalshi_fee_is_bound
    ? `<span class="tag" title="Kalshi fee is an upper bound at size 1">fee upper-bound</span>`
    : "";

  return `<div class="opp ${b.cls}"><div class="opp-stripe"></div><div class="opp-in">
    <div class="opp-top">
      ${profitBlock}
      <div class="badges">
        <span class="badge ${b.cls}">${b.txt}</span>
        ${typeof r.similarity === "number" ? `<span class="tag">sim ${r.similarity.toFixed(2)}</span>` : ""}
        ${boundNote}
      </div>
    </div>
    ${legs}
    <div class="qrows">
      <div class="qrow"><span class="lab">PM</span><span class="q">${esc(pm.question)} ${
        pm.url ? `<a href="${esc(pm.url)}" target="_blank" rel="noopener">↗</a>` : ""
      }</span></div>
      <div class="qrow"><span class="lab ks">KS</span><span class="q">${esc(ks.question)} ${
        ks.url ? `<a href="${esc(ks.url)}" target="_blank" rel="noopener">↗</a>` : ""
      }</span></div>
    </div>
    ${sizingHtml(r.sizing)}
    ${llm}
  </div></div>`;
}

// A confirmed arb: validated (same event + equivalent payoff) and a positive edge.
function isConfirmedArb(r) {
  const v = r.validation;
  return isPriced(r) && r.profit > 0 && r.confirmed && !!(v && v.same_event && v.equivalent_payoff);
}

function diagHtml(rows) {
  const priced = rows.filter(isPriced);
  const confirmed = priced.filter(isConfirmedArb);
  const best = priced.reduce((m, r) => Math.max(m, r.profit > 0 ? r.profit : -Infinity), 0);
  const captured = confirmed.reduce((s, r) => s + (r.sizing ? r.sizing.profit_at_recommended : 0), 0);
  const tiles = [
    { k: "Candidates", val: String(rows.length), note: "pairs surfaced", cls: "" },
    { k: "Confirmed arbs", val: String(confirmed.length), note: "validated · positive", cls: confirmed.length ? "pos" : "" },
    { k: "Best edge / pair", val: best > 0 ? money(best) : "—", note: "top confirmed", cls: best > 0 ? "pos" : "" },
    { k: "Captured at size", val: "$" + nfmt(captured), note: "sum of recommended", cls: captured > 0 ? "pos" : "" },
  ];
  return `<div class="diag">${tiles
    .map((d, i) => `<div class="tile ${i === 1 ? "accent" : ""}"><span class="k">${d.k}</span><span class="val ${d.cls}">${d.val}</span><span class="note">${d.note}</span></div>`)
    .join("")}</div>`;
}

function renderHead(run) {
  $("crumb").innerHTML = `Screener / Run <span class="who">${esc(run.id.slice(0, 6))}</span>`;
  const title =
    run.type === "max" ? "Exhaustive section sweep" : "Cross-venue scan";
  $("detail-head").innerHTML =
    `<span class="eyebrow coral">${esc(run.type)} run</span>
     <h1>${title} — ${esc(argSummary(run.args || {}))}</h1>`;

  const kv = (k, v, extra = "") => `<div class="kv ${extra}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $("runmeta").innerHTML =
    kv("run id", esc(run.id), "id") +
    kv("type", esc(run.type)) +
    kv("status", `<span class="chip ${run.status}"><i></i>${run.status}</span>`) +
    kv("launched by", esc(run.launched_by)) +
    kv("real LLM calls", run.llm_calls) +
    kv("started", esc(run.started_at ? run.started_at.replace("T", " ").slice(0, 19) : "–"));
}

function render(run) {
  renderHead(run);
  const body = $("body");

  if (run.status === "running" || run.status === "queued") {
    body.innerHTML = `<div class="empty"><div class="state-note"><span class="chip running"><i></i>${run.status}</span> The desk is working. This view refreshes when the run lands.</div></div>`;
    return true; // keep polling
  }
  if (run.status === "failed") {
    body.innerHTML = `<div class="failbox"><b>Run failed</b> — exit code ${run.exit_code}.
      <pre>${esc(run.error_tail || "no error output captured")}</pre></div>`;
    return false;
  }
  if (run.status === "cancelled") {
    body.innerHTML = `<div class="empty">Run was cancelled before it finished.</div>`;
    return false;
  }

  // done
  const { rows, unmatched } = normalize(run.result);
  if (!rows.length) {
    body.innerHTML = `<div class="empty">No candidate opportunities returned.</div>`;
    return false;
  }
  const oppsHtml = sortRows(rows).map(oppHtml).join("");
  const unmatchedHtml = unmatched.length
    ? `<span class="subsec">No counterpart found (${unmatched.length})</span>
       <div class="unmatched">${unmatched.map((t) => "<div>• " + esc(t) + "</div>").join("")}</div>`
    : "";
  body.innerHTML =
    diagHtml(rows) +
    `<span class="subsec">${rows.length} candidate${rows.length === 1 ? "" : "s"} · sorted by edge</span>` +
    oppsHtml +
    unmatchedHtml;
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
