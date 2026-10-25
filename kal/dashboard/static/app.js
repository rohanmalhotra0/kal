/* kal dashboard. Polls /api/state every 10 seconds and draws it.
   Everything from the server goes in with textContent, never innerHTML:
   market titles come from Kalshi and must never run as code. */
"use strict";

const POLL_MS = 10000;
const $ = (id) => document.getElementById(id);
let state = null;
let chart = null;
let refreshing = false;

const KINDS = {
  arbitrage: ["Math", "good", "The profit is locked in by math."],
  statistical: ["History", "warn", "Based on what past markets did. It can lose."],
  speculative: ["Risky", "bad", "A bet on a guess. It can lose."],
};
const MODES = [["off", "Off"], ["paper", "Practice"], ["live", "Real"]];
const EDGE_STATUS = { live: ["Real", "good"], candidate: ["Practice", "warn"], retired: ["Retired", "off"] };
const JOB_NAMES = { trade: "Trade check", research: "Research", report: "Report" };

// ---------- small helpers ----------

function el(tag, props, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false || kid === "") continue;
    n.append(kid instanceof Node ? kid : String(kid));
  }
  return n;
}

const isNum = (v) => typeof v === "number" && isFinite(v);
const money = (v) => (isNum(v) ? (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2) : "—");
const cents = (p) => (isNum(p) ? Math.round(p * 100) + "¢" : "?");
const pct = (v) => (isNum(v) ? (v > 0 ? "+" : "") + Math.round(v * 100) + "%" : "—");

function signed(v) {
  if (!isNum(v)) return "—";
  const sign = v > 0.004 ? "+" : v < -0.004 ? "-" : "";
  return sign + "$" + Math.abs(v).toFixed(2);
}

function tone(node, v) {
  node.classList.toggle("up", isNum(v) && v > 0.004);
  node.classList.toggle("down", isNum(v) && v < -0.004);
}

function span(seconds) {
  const s = Math.abs(seconds);
  if (s < 90) return "1 min";
  if (s < 3600) return Math.round(s / 60) + " min";
  if (s < 2 * 86400) return Math.round(s / 3600) + " h";
  return Math.round(s / 86400) + " days";
}

// Relative times use the server's clock, so a wrong clock on the phone doesn't matter.
const nowTs = () => (state && isNum(state.now) ? state.now : Date.now() / 1000);
const ago = (ts) => (isNum(ts) ? span(nowTs() - ts) + " ago" : "never");
const until = (ts) => (isNum(ts) && ts - nowTs() > 30 ? "in " + span(ts - nowTs()) : "soon");

function when(ts) {
  if (!isNum(ts)) return "";
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function td(label, ...kids) {
  return el("td", { "data-label": label }, ...kids);
}

// The first cell of a row is its name: on a phone it needs no label in front.
function leadTd(label, ...kids) {
  return el("td", { "data-label": label, class: "lead" }, ...kids);
}

function emptyRow(cols, text) {
  return el("tr", { class: "empty" }, el("td", { colspan: cols }, text));
}

function pill(text, kind, title) {
  return el("span", { class: "pill " + (kind || ""), title: title || null }, text);
}

function say(text, bad) {
  const m = $("msg");
  m.textContent = text || "";
  m.classList.toggle("bad", !!bad);
}

const anyLive = (s) => (s.strategies || []).some((x) => x.mode === "live");
const overridden = (s, key) => (s.env_overrides || []).includes(key);
const realMoneyQuestion = (amount) => `The bot will trade real money, up to ${money(amount)}. Continue?`;

// ---------- talking to the server ----------

async function api(path, body) {
  const opts = { credentials: "same-origin", cache: "no-store", headers: {} };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = {};
  try {
    data = await r.json();
  } catch (e) {
    data = {};
  }
  if (!r.ok) throw new Error(data.error || data.why || `Something went wrong (${r.status}).`);
  return data;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
    $("offline").hidden = true;
    render(state);
  } catch (e) {
    $("offline").hidden = false;
  } finally {
    refreshing = false;
  }
}

async function save(changes, okText) {
  let ok = false;
  try {
    const r = await api("/api/config", changes);
    say([okText, ...(r.notes || [])].join(" "), (r.notes || []).length > 0);
    ok = true;
  } catch (e) {
    say(e.message, true);
  }
  await refresh();
  return ok;
}

// ---------- actions ----------

async function togglePower() {
  if (!state) return;
  const cfg = state.config;
  const turnOn = !cfg.enabled;
  if (turnOn) {
    const live = anyLive(state);
    if (live && !(cfg.budget > 0)) {
      say("First, pick how much money the bot can use.", true);
      $("budget").focus();
      return;
    }
    if (live && cfg.env === "prod" && !window.confirm(realMoneyQuestion(cfg.budget))) return;
  }
  await save({ enabled: turnOn }, turnOn ? "The bot is on." : "The bot is off.");
}

async function saveBudget(e) {
  e.preventDefault();
  if (!state) return;
  const raw = $("budget").value.trim();
  const v = Number(raw);
  if (raw === "" || !isFinite(v) || v < 0) {
    say("Type an amount, like 300.", true);
    return;
  }
  const cfg = state.config;
  if (cfg.enabled && cfg.env === "prod" && anyLive(state) && v > cfg.budget
      && !window.confirm(realMoneyQuestion(v))) return;
  if (await save({ budget: v }, `Saved. The bot can use up to ${money(v)}.`)) $("budget").blur();
}

async function setEnv(e) {
  const env = e.target.value;
  await save({ env }, env === "demo" ? "Now using Kalshi's practice site." : "Now using real money.");
}

async function setMode(strat, select) {
  const mode = select.value;
  const cfg = state.config;
  if (mode === "live" && cfg.enabled && cfg.env === "prod" && !window.confirm(realMoneyQuestion(cfg.budget))) {
    select.value = strat.mode;
    return;
  }
  const label = (MODES.find(([v]) => v === mode) || [mode, mode])[1];
  if (!(await save({ strategies: { [strat.name]: { mode } } }, `${strat.title}: ${label}.`))) {
    select.value = strat.mode;
  }
}

async function resetTrip() {
  const q = overridden(state, "enabled")
    ? "Clear the stop? The bot will start again."
    : "Clear the stop? The bot stays off until you turn it on.";
  if (!window.confirm(q)) return;
  try {
    await api("/api/reset-trip", {});
    say("Stop cleared.");
  } catch (e) {
    say(e.message, true);
  }
  await refresh();
}

async function runNow(kind, button) {
  if (kind === "trade" && state && !state.config.enabled) {
    say("The bot is off. Turn it on first.", true);
    return;
  }
  button.disabled = true;
  try {
    await api("/api/run", { kind });
    say(kind === "trade" ? "Checking markets now." : "Research started. It can take a while.");
  } catch (e) {
    say(e.message, true);
  } finally {
    button.disabled = false;
  }
  setTimeout(refresh, 1500);
}

async function selfTest(button) {
  const out = $("selftest-out");
  button.disabled = true;
  out.replaceChildren(el("li", {}, "Running. This can take a minute."));
  try {
    const r = await api("/api/selftest");
    const checks = r.checks || [];
    out.replaceChildren(...(checks.length ? checks.map((c) => el("li", {},
      pill(c.ok ? "OK" : "Problem", c.ok ? "good" : "bad"),
      el("span", {}, el("strong", {}, String(c.check || "")), c.detail ? ": " + c.detail : ""))) : [el("li", {}, "No checks ran.")]));
  } catch (e) {
    out.replaceChildren(el("li", { class: "down" }, e.message));
  } finally {
    button.disabled = false;
  }
}

// ---------- drawing ----------

function render(s) {
  const cfg = s.config;
  const on = !!cfg.enabled;
  const tripped = !!cfg.disabled_reason;

  const where = $("where");
  where.textContent = cfg.env === "demo" ? "Practice (demo)" : "Real money";
  where.className = "pill " + (cfg.env === "demo" ? "warn" : "");

  $("trip").hidden = !tripped;
  $("trip-reason").textContent = cfg.disabled_reason || "";
  $("nokey").hidden = !!s.has_key;
  $("problems").hidden = !(s.errors && s.errors.length);
  $("problems-text").textContent = (s.errors || []).join(" ");

  const power = $("power");
  power.setAttribute("aria-checked", on ? "true" : "false");
  power.disabled = tripped;
  $("power-word").textContent = on ? "ON" : "OFF";
  $("power-label").textContent = tripped ? "The bot is stopped" : on ? "The bot is on" : "The bot is off";
  renderNext(s);

  const b = $("budget");
  if (document.activeElement !== b) b.value = isNum(cfg.budget) ? String(cfg.budget) : "";
  b.disabled = overridden(s, "budget");
  $("budget-note").textContent = overridden(s, "budget")
    ? "Set on the server (KAL_BUDGET)."
    : `The bot can never lose more than this. Free to use now: ${money((s.money || {}).available)}.`;

  const envLocked = overridden(s, "env");
  for (const r of document.querySelectorAll('input[name="env"]')) {
    r.checked = r.value === cfg.env;
    r.disabled = on || envLocked;
  }
  $("env-note").textContent = envLocked ? "Set on the server (KAL_ENV)." : on ? "Turn the bot off to switch." : "";

  renderCards(s);
  renderChart(s);
  if (!$("strategies").contains(document.activeElement)) renderStrategies(s);
  renderResearch(s);
  renderTrades(s);
  renderRuns(s);
}

function renderNext(s) {
  const sc = s.scheduler;
  const parts = [];
  if (!sc || !sc.alive) {
    parts.push("The timer is not running here.");
  } else {
    const t = sc.trade, r = sc.research;
    if (t && t.running) parts.push("Checking markets now.");
    else if (t && s.config.enabled) parts.push(`Next check: ${until(t.next)}.`);
    if (r && r.running) parts.push("Research is running.");
    else if (r) parts.push(`Next research: ${until(r.next)}.`);
  }
  $("next").textContent = parts.join(" ");
}

function renderCards(s) {
  const a = s.account;
  const real = !!(s.has_key && a && a.live);
  const pos = s.positions || {};
  const m = s.money || {};
  const today = s.today || {};
  const noData = s.has_key ? "No data yet" : "Needs a Kalshi key";

  $("c-cash").textContent = real ? money(a.cash) : "—";
  $("c-cash-sub").textContent = real ? `In Kalshi, ${ago(a.ts)}` : noData;
  $("c-value").textContent = real ? money(a.value) : "—";
  $("c-value-sub").textContent = real ? "What the bot has now" : noData;
  $("c-bets").textContent = real ? money(a.in_bets) : "—";
  $("c-bets-sub").textContent = `${pos.live || 0} open`;

  const t = $("c-today");
  t.textContent = signed(today.pnl);
  tone(t, today.pnl);

  const p = $("c-paper");
  p.textContent = signed(m.paper_profit);
  tone(p, m.paper_profit);
  $("c-paper-sub").textContent = `Today ${signed(today.paper_pnl)}. ${pos.paper || 0} open.`;
}

function chartOptions() {
  const text = cssVar("--muted");
  const grid = cssVar("--line");
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    elements: { point: { radius: 0 }, line: { borderWidth: 3, tension: 0.2 } },
    plugins: {
      legend: { labels: { color: text, font: { size: 15 } } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } },
    },
    scales: {
      x: { ticks: { color: text, maxTicksLimit: 5, font: { size: 13 } }, grid: { display: false } },
      y: { ticks: { color: text, font: { size: 13 }, callback: (v) => "$" + v }, grid: { color: grid } },
    },
  };
}

function renderChart(s) {
  const note = $("chart-note");
  const rows = s.equity || [];
  const datasets = [];
  if (rows.some((r) => r.equity)) {
    datasets.push({ label: "Real", data: rows.map((r) => r.equity), borderColor: cssVar("--chart-real") });
  }
  if (rows.some((r) => r.paper_equity)) {
    datasets.push({ label: "Practice", data: rows.map((r) => r.paper_equity),
                    borderColor: cssVar("--chart-paper"), borderDash: [8, 5] });
  }
  let problem = "";
  if (typeof Chart === "undefined") problem = "The chart could not load.";
  else if (rows.length < 2 || !datasets.length) problem = "No history yet.";
  note.hidden = !problem;
  note.textContent = problem;
  $("chart").hidden = !!problem;
  if (problem) return;

  const labels = rows.map((r) => when(r.ts));
  if (!chart) {
    chart = new Chart($("chart"), { type: "line", data: { labels, datasets }, options: chartOptions() });
  } else {
    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.options = chartOptions();
    chart.update("none");
  }
}

function paperText(p) {
  if (!p || !p.trades) return "No practice trades yet.";
  const done = p.settled || 0;
  const parts = [`${p.trades} trades`, `${done} finished`];
  if (done) parts.push(`${p.wins || 0} won`, signed(p.pnl));
  return parts.join(". ") + ".";
}

function renderStrategies(s) {
  const body = $("strategies").tBodies[0];
  const rows = (s.strategies || []).map((x) => {
    const [kname, ktone, ktip] = KINDS[x.kind] || ["Unknown", "off", ""];
    const select = el("select", { "aria-label": `Mode for ${x.title}`, disabled: !x.installed,
                                  onchange: (e) => setMode(x, e.target) },
      MODES.map(([v, label]) => el("option", { value: v, selected: x.mode === v }, label)));
    let hint = "";
    if (x.mode === "live" && !s.has_key) hint = "No key, so it trades practice.";
    else if (x.mode === "live" && x.kind !== "arbitrage") hint = "Real money only once it proves itself.";
    return el("tr", {},
      leadTd("Strategy", el("div", { class: "name" }, x.title), el("div", { class: "desc" }, x.description)),
      td("Type", pill(kname, ktone, ktip)),
      td("Mode", select, hint ? el("div", { class: "hint" }, hint) : null),
      td("Practice results", paperText(x.paper)));
  });
  body.replaceChildren(...(rows.length ? rows : [emptyRow(4, "No strategies found.")]));
}

function renderResearch(s) {
  const r = s.research || {};
  const sc = s.scheduler || {};
  const last = r.last;
  let line = last && isNum(last.ts) ? `Last run: ${ago(last.ts)}. ${last.text || ""}` : "Research has not run yet.";
  if (sc.research && sc.research.running) line = "Research is running now.";
  $("research-last").textContent = line;

  const edges = r.edges || { counts: {}, top: [], total: 0 };
  const counts = edges.counts || {};
  $("research-counts").replaceChildren(...Object.entries(EDGE_STATUS)
    .filter(([k]) => counts[k])
    .map(([k, [label, kind]]) => el("li", {}, pill(`${label}: ${counts[k]}`, kind))));

  const w = r.words;
  $("research-words").textContent = w
    ? `Mention words: ${w.promoted ? "real money" : "practice only"}. ${w.words || 0} words known.`
    : "";

  const body = $("edges").tBodies[0];
  const rows = (edges.top || []).map((e) => {
    const [label, kind] = EDGE_STATUS[e.status] || [e.status || "?", "off"];
    const range = `${cents(e.lo)} to ${cents(e.hi)}`;
    const hours = isNum(e.hours_before_close) ? `, ${e.hours_before_close} h before close` : "";
    const trades = isNum(e.trades) ? `, ${e.trades} trades` : "";
    return el("tr", {},
      leadTd("Series", el("div", { class: "name" }, e.series || e.id), e.note ? el("div", { class: "desc" }, e.note) : null),
      td("Price range", range + hours),
      td("Past result", `${pct(e.roi)} (worst case ${pct(e.roi_lo)})${trades}`),
      td("Status", pill(label, kind)));
  });
  body.replaceChildren(...(rows.length ? rows : [emptyRow(4, "No edges found yet.")]));
}

function resultText(status) {
  const st = String(status || "");
  if (st === "accepted") return "Placed";
  if (st === "executed") return "Bought";
  if (st.startsWith("missed")) return "No fill";
  return st.charAt(0).toUpperCase() + st.slice(1);
}

function renderTrades(s) {
  const names = Object.fromEntries((s.strategies || []).map((x) => [x.name, x.title]));
  const rows = (s.trades || []).map((t) => el("tr", {},
    td("When", when(t.ts)),
    td("Strategy", names[t.strategy] || t.strategy),
    td("What", t.title || t.event || ""),
    td("Money", t.mode === "live" ? pill("Real", "good") : pill("Practice", "warn")),
    td("Cost", money(t.cost)),
    td("Result", resultText(t.status))));
  $("trades").tBodies[0].replaceChildren(...(rows.length ? rows : [emptyRow(6, "No trades yet.")]));
}

function renderRuns(s) {
  const rows = (s.runs || []).map((r) => el("tr", {},
    td("When", when(r.ts)),
    td("Job", JOB_NAMES[r.kind] || r.kind),
    td("How it went", pill(r.ok ? "OK" : "Problem", r.ok ? "good" : "bad"), " ", r.text,
       (r.notes || []).length ? el("details", {}, el("summary", {}, "More"),
                                   el("ul", {}, r.notes.map((n) => el("li", {}, n)))) : null)));
  $("runs").tBodies[0].replaceChildren(...(rows.length ? rows : [emptyRow(3, "Nothing has run yet.")]));
}

// ---------- start ----------

document.addEventListener("DOMContentLoaded", () => {
  $("power").addEventListener("click", togglePower);
  $("budget-form").addEventListener("submit", saveBudget);
  for (const r of document.querySelectorAll('input[name="env"]')) r.addEventListener("change", setEnv);
  $("reset").addEventListener("click", resetTrip);
  $("check-now").addEventListener("click", (e) => runNow("trade", e.currentTarget));
  $("research-now").addEventListener("click", (e) => runNow("research", e.currentTarget));
  $("selftest").addEventListener("click", (e) => selfTest(e.currentTarget));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  if (dark.addEventListener) dark.addEventListener("change", () => state && renderChart(state));
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
});
