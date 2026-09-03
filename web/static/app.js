"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---- launch form -----------------------------------------------------------

function syncType() {
  const t = $("type").value;
  document.querySelectorAll(".fields").forEach((el) => {
    el.style.display = el.dataset.for === t ? "" : "none";
  });
  const isMax = t === "max";
  $("max-warn").style.display = isMax ? "flex" : "none";
  $("mv-note").textContent = isMax ? "required unless --no-llm" : "default 15";
  $("launch-kind").textContent = t;
  document.querySelectorAll("#type-seg button").forEach((b) => {
    b.classList.toggle("on", b.dataset.type === t);
  });
}

function collectArgs() {
  const t = $("type").value;
  const args = {};
  const num = (id) => {
    const v = $(id).value.trim();
    return v === "" ? null : v;
  };
  if (t === "scan") {
    if ($("sections").value.trim()) args.sections = $("sections").value.trim();
    if (num("per_section")) args.per_section = num("per_section");
    if (num("min_profit")) args.min_profit = num("min_profit");
  } else {
    args.section = $("section").value;
    if (num("min_volume")) args.min_volume = num("min_volume");
  }
  if (num("size")) args.size = num("size");
  if (num("max_validations") !== null) args.max_validations = num("max_validations");
  if ($("no_llm").checked) args.no_llm = true;
  return { type: t, args };
}

async function launch() {
  const btn = $("launch");
  const msg = $("msg");
  msg.className = "msg";
  msg.textContent = "";
  const body = collectArgs();

  // Client-side guardrail mirroring the plan: max needs a cap or --no-llm.
  if (body.type === "max" && !body.args.no_llm && body.args.max_validations == null) {
    msg.className = "msg err";
    msg.textContent = "Set a Max LLM validations cap for a max run (or check --no-llm).";
    return;
  }

  btn.disabled = true;
  try {
    const res = await fetch("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      msg.className = "msg err";
      msg.textContent = data.detail || "launch failed";
    } else {
      msg.className = "msg ok";
      msg.textContent = "Launched " + data.id + " — running…";
      refresh();
    }
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = "network error: " + e;
  } finally {
    btn.disabled = false;
  }
}

// ---- usage -----------------------------------------------------------------

async function loadUsage() {
  try {
    const r = await fetch("/usage");
    const d = await r.json();
    $("usage-num").textContent = d.month_to_date_llm_calls;
  } catch (_) {
    $("usage-num").textContent = "?";
  }
}

// ---- run list --------------------------------------------------------------

function fmtDuration(a, b) {
  if (!a) return "–";
  const start = new Date(a).getTime();
  const end = b ? new Date(b).getTime() : Date.now();
  const s = Math.max(0, Math.round((end - start) / 1000));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  return m + "m " + (s % 60) + "s";
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

async function cancelRun(id, ev) {
  ev.stopPropagation();
  if (!confirm("Cancel run " + id + "?")) return;
  await fetch("/runs/" + id, { method: "DELETE" });
  refresh();
}

function renderRuns(runs) {
  if (!runs.length) {
    $("runlist").innerHTML = '<div class="ledger"><div class="empty">No runs yet.</div></div>';
    return;
  }
  const rows = runs.map((r) => {
    const active = r.status === "running" || r.status === "queued";
    const link =
      r.status === "done"
        ? `<a href="/runs/${r.id}/view">open →</a>`
        : active
        ? `<button class="mini-btn" onclick="cancelRun('${r.id}', event)">cancel</button>`
        : `<span class="mono" style="color:var(--faint)">—</span>`;
    return `<tr onclick="if(!['A','BUTTON'].includes(event.target.tagName)){location.href='/runs/${r.id}/view'}">
      <td><div class="type">${esc(r.type)}</div><div class="args">${esc(argSummary(r.args))}</div></td>
      <td><span class="chip ${esc(r.status)}"><i></i>${esc(r.status)}</span></td>
      <td class="by">${esc(r.launched_by || "–")}</td>
      <td class="r">${fmtDuration(r.started_at, r.finished_at)}</td>
      <td class="r">${r.llm_calls}</td>
      <td class="link-cell">${link}</td>
    </tr>`;
  });
  $("runlist").innerHTML = `<div class="ledger"><table>
    <thead><tr>
      <th>Job</th><th>Status</th><th>Launched by</th><th class="r">Duration</th><th class="r">LLM</th><th></th>
    </tr></thead>
    <tbody>${rows.join("")}</tbody></table></div>`;
}

let _pollTimer = null;
async function refresh() {
  try {
    const r = await fetch("/runs?limit=50");
    const d = await r.json();
    renderRuns(d.runs);
    loadUsage();
    // Poll while anything is active; otherwise idle (plan §3: no streaming).
    const anyActive = d.runs.some((x) => x.status === "running" || x.status === "queued");
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(refresh, anyActive ? 2000 : 8000);
  } catch (_) {
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(refresh, 5000);
  }
}

// ---- init ------------------------------------------------------------------

document.querySelectorAll("#type-seg button").forEach((b) => {
  b.addEventListener("click", () => {
    $("type").value = b.dataset.type;
    syncType();
  });
});
$("launch").addEventListener("click", launch);
syncType();
loadUsage();
refresh();
