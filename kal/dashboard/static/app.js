/* kal dashboard. Polls /api/state every 10 seconds and draws the Home tab and the
   banners. The other tabs fetch their own data, and only while they are open.
   Everything from the server goes in with textContent, never innerHTML:
   market titles come from Kalshi and must never run as code. */
"use strict";

const POLL_MS = 10000;
const BUSY_POLL_MS = 3000;   // while a backtest runs, check on it more often
const PAGES = ["home", "trades", "research", "test"];
const $ = (id) => document.getElementById(id);
let state = null;
let chart = null;
let refreshing = false;
let page = "home";
const loading = {};          // page -> its data is being fetched now
const drawn = {};            // page -> what it last drew, so a poll with no news redraws nothing

const KINDS = {
  arbitrage: ["Math", "good", "The profit is locked in by math."],
  statistical: ["History", "warn", "Based on what past markets did. It can lose."],
  speculative: ["Risky", "bad", "A bet on a guess. It can lose."],
};
const MODES = [["off", "Off"], ["paper", "Practice"], ["live", "Real"]];
const EDGE_STATUS = { live: ["Real", "good"], candidate: ["Practice", "warn"], retired: ["Retired", "off"] };
const JOB_NAMES = { trade: "Trade check", research: "Research", report: "Report", health: "Health check",
                    maintenance: "Backup", summary: "Daily summary" };
const TRADE_STATE = {
  open: ["Open", ""],
  won: ["Won", "good"],
  lost: ["Lost", "bad"],
  split: ["Split payout", "warn"],
  unknown: ["Unclear result", "warn"],
  no_fill: ["No fill", "off"],
  refused: ["Refused", "bad"],
  checking: ["Checking", "warn"],
  never_placed: ["Not placed", "off"],
};
const THEME_NAMES = { auto: "Auto", light: "Light", dark: "Dark" };

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
const money = (v) => (isNum(v) ? (v < -0.004 ? "-$" : "$") + Math.abs(v).toFixed(2) : "—");
const cents = (p) => (isNum(p) ? Math.round(p * 100) + "¢" : "?");
const num = (v) => (isNum(v) ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : "?");
const sideText = (s) => (s === "yes" ? "Yes" : s === "no" ? "No" : String(s || "?"));
const rangeText = (r) => `${cents(r.lo)} to ${cents(r.hi)}`;
// A backtest row or an edge is one side (Yes or No) in one price range, or both sides mixed.
const buyText = (r) => (r.side ? `${sideText(r.side)}, ${rangeText(r)}` : rangeText(r));
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

function roiPct(v) {
  if (!isNum(v)) return "—";
  const p = v * 100;
  return (p > 0.04 ? "+" : "") + (Math.abs(p) < 10 ? p.toFixed(1) : p.toFixed(0)) + "%";
}

function signed(v) {
  if (!isNum(v)) return "—";
  const sign = v > 0.004 ? "+" : v < -0.004 ? "-" : "";
  return sign + "$" + Math.abs(v).toFixed(2);
}

function tone(node, v) {
  node.classList.toggle("up", isNum(v) && v > 0.004);
  node.classList.toggle("down", isNum(v) && v < -0.004);
}

// A signed dollar amount, green if up and red if down. The sign is there too, so
// the color is never the only clue.
function upDown(v) {
  const s = el("span", { class: "amount" }, signed(v));
  tone(s, v);
  return s;
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
const hoursText = (h) => (isNum(h) ? `${num(h)} ${h === 1 ? "hour" : "hours"} before close` : "");

function when(ts) {
  if (!isNum(ts)) return "";
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function day(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !isNaN(d) ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : "";
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

// Each area has its own message line, next to the button that made it.
function note(id, text, bad) {
  const m = $(id);
  if (!m) return;
  m.textContent = text || "";
  m.classList.toggle("bad", !!bad);
}

const say = (text, bad) => note("msg", text, bad);
const anyLive = (s) => (s.strategies || []).some((x) => x.mode === "live");
const realMoneyQuestion = (amount) => `The bot will trade real money, up to ${money(amount)}. Continue?`;
const strategyNames = () => Object.fromEntries(((state && state.strategies) || []).map((x) => [x.name, x.title]));

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
  let ok = false;
  try {
    state = await api("/api/state");
    $("offline").hidden = true;
    render(state);
    ok = true;
  } catch (e) {
    $("offline").hidden = false;
  } finally {
    refreshing = false;
  }
  if (ok) loadPage();
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

// ---------- tabs ----------

const pageFromHash = () => {
  const h = location.hash.replace("#", "");
  return PAGES.includes(h) ? h : "home";
};

function go(name) {
  if (location.hash !== "#" + name) location.hash = name;  // the hashchange listener shows it
  else showPage(name);
}

function showPage(name) {
  page = PAGES.includes(name) ? name : "home";
  for (const p of PAGES) {
    const on = p === page;
    const tab = $("tab-" + p);
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    $("page-" + p).hidden = !on;
  }
  hideTip();
  if (page === "home" && chart) chart.resize();
  loadPage();
}

// Arrow keys move between tabs, the way screen reader users expect.
function tabKeys(e) {
  const i = PAGES.indexOf(page);
  const moves = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: PAGES.length - 1 };
  if (!(e.key in moves)) return;
  e.preventDefault();
  const next = PAGES[(moves[e.key] + PAGES.length) % PAGES.length];
  go(next);
  $("tab-" + next).focus();
}

const LOADERS = { trades: () => loadTrades(), research: () => loadResearch(), test: () => loadBacktests() };

function loadPage() {
  const f = LOADERS[page];
  if (f && !document.hidden) f();
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
  await save({ env }, env === "demo" ? "Now using Kalshi's demo site. Fake money." : "Now using real money.");
}

async function setMode(strat, select) {
  const mode = select.value;
  const cfg = state.config;
  if (mode === "live" && cfg.enabled && cfg.env === "prod" && !window.confirm(realMoneyQuestion(cfg.budget))) {
    select.value = strat.mode;
    return;
  }
  const label = mode === "live" && cfg.env === "demo" ? "Live on the demo site (fake money)"
    : (MODES.find(([v]) => v === mode) || [mode, mode])[1];
  if (!(await save({ strategies: { [strat.name]: { mode } } }, `${strat.title}: ${label}.`))) {
    select.value = strat.mode;
  }
}

async function resetTrip() {
  if (!window.confirm("Clear the stop? The bot stays off until you turn it on.")) return;
  try {
    await api("/api/reset-trip", {});
    say("Stop cleared.");
  } catch (e) {
    say(e.message, true);
  }
  await refresh();
}

async function runNow(kind, button, msgId) {
  if (kind === "trade" && state && !state.config.enabled) {
    note(msgId, "The bot is off. Turn it on first.", true);
    return;
  }
  button.disabled = true;
  try {
    await api("/api/run", { kind });
    note(msgId, kind === "trade" ? "Checking markets now." : "Research started. It can take a while.");
  } catch (e) {
    note(msgId, e.message, true);
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

async function testAlert(button) {
  button.disabled = true;
  note("alert-msg", "Sending.");
  try {
    await api("/api/test-alert", {});
    note("alert-msg", "Sent. Check your phone.");
  } catch (e) {
    note("alert-msg", e.message, true);
  } finally {
    button.disabled = false;
  }
}

// ---------- Home ----------

function render(s) {
  const cfg = s.config;
  const on = !!cfg.enabled;
  const tripped = !!cfg.disabled_reason;
  const demo = cfg.env === "demo";

  const where = $("where");
  where.textContent = demo ? "Demo: fake money" : "Real money";
  where.className = "where " + (demo ? "demo" : "real");
  document.title = demo ? "kal (demo)" : "kal";

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
  for (const r of document.querySelectorAll('input[name="env"]')) {
    r.checked = r.value === cfg.env;
    r.disabled = on;
  }
  $("env-note").textContent = on ? "Turn the bot off to switch." : "Each one keeps its own books.";

  renderMoney(s);
  renderPractice(s);
  renderChart(s);
  if (!$("strategies").contains(document.activeElement)) renderStrategies(s);
  renderAlerts(s);
  renderRuns(s);
  if (page === "research") drawResearchHead();
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

function setMoney(id, v) {
  $(id).textContent = money(v);
}

function renderMoney(s) {
  const m = s.money || {};
  const demo = s.config.env === "demo";
  const books = $("books");
  books.textContent = demo ? "Demo books" : "Real money";
  books.className = "pill " + (demo ? "demo" : "real");

  setMoney("m-budget", m.budget);
  const profit = $("m-profit");
  profit.textContent = signed(m.realized);
  tone(profit, m.realized);
  setMoney("m-capital", m.capital);
  setMoney("m-open", m.open_cost);
  // With a $0 budget the engine trades no real money at all, whatever the books hold.
  setMoney("m-free", m.budget > 0 ? m.available : 0);

  const cap = $("m-cap");
  const real = m.budget > 0;
  cap.textContent = real ? `It can never lose more than ${money(m.max_loss)} in total.`
    : "The budget is $0, so it can't trade real money.";
  cap.classList.toggle("off", !real);
  $("m-notes").replaceChildren(...moneyNotes(s).map((t) => el("li", {}, t)));
}

function moneyNotes(s) {
  const m = s.money || {};
  const a = s.account;
  const out = [];
  if (m.budget > 0 && isNum(m.realized)) {
    if (m.realized < -0.004) {
      out.push(`It has lost ${money(-m.realized)} so far. So it can lose at most ${money(Math.max(0, m.capital))} more.`);
    } else if (m.realized > 0.004) {
      out.push(m.reinvest_profits ? `It also trades with its ${money(m.realized)} profit.`
        : `It keeps its ${money(m.realized)} profit aside. It won't bet it.`);
    }
  }
  if (s.has_key && a && a.live) {
    out.push(`Cash in Kalshi: ${money(a.cash)} (${ago(a.ts)}).`);
    const room = a.cash - (m.cash_floor || 0);
    if (m.budget > 0 && isNum(room) && isNum(m.available) && room < m.available - 0.004) {
      out.push(`Kalshi has less cash than "Free to use". So it can only use ${money(Math.max(0, room))} now.`);
    }
  }
  const t = s.order_test || {};
  if (s.has_key && m.budget > 0) {
    if (t.passed) out.push(`Real-money test order: passed (${ago(t.ts)}).`);
    else if (t.detail) out.push(`Real money waits for the $0.01 test order: ${t.detail}. It tries again every hour.`);
    else out.push("Before its first real trade, it sends one $0.01 test order that can't fill.");
  }
  if (isNum((s.today || {}).pnl)) out.push(`Today: ${signed(s.today.pnl)}.`);
  return out;
}

function renderPractice(s) {
  const m = s.money || {};
  const p = $("p-profit");
  p.textContent = signed(m.paper_profit);
  tone(p, m.paper_profit);
  setMoney("p-value", m.paper_value);
  setMoney("p-open", m.paper_open_cost);
  const pos = s.positions || {};
  const today = s.today || {};
  $("p-sub").textContent = `Pretend money, real results. Today ${signed(today.paper_pnl)}. ${pos.paper || 0} open.`;
}

function chartOptions() {
  const text = cssVar("--muted");
  const grid = cssVar("--line");
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    elements: { point: { radius: 0 }, line: { borderWidth: 2, tension: 0.2 } },
    plugins: {
      legend: { labels: { color: text, font: { size: 18 } } },
      tooltip: { titleFont: { size: 18 }, bodyFont: { size: 18 },
                 callbacks: { label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } },
    },
    scales: {
      x: { ticks: { color: text, maxTicksLimit: 4, font: { size: 18 } }, grid: { display: false } },
      y: { ticks: { color: text, font: { size: 18 }, callback: (v) => "$" + v }, grid: { color: grid } },
    },
  };
}

function renderChart(s) {
  const note_ = $("chart-note");
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
  note_.hidden = !problem;
  note_.textContent = problem;
  $("chart").parentElement.hidden = !!problem;  // no empty box where the chart would be
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
  const demo = s.config.env === "demo";
  const rows = (s.strategies || []).map((x) => {
    const [kname, ktone, ktip] = KINDS[x.kind] || ["Unknown", "off", ""];
    const select = el("select", { "aria-label": `Mode for ${x.title}`, disabled: !x.installed,
                                  onchange: (e) => setMode(x, e.target) },
      MODES.map(([v, label]) => el("option", { value: v, selected: x.mode === v },
                                   v === "live" && demo ? "Live (demo, fake money)" : label)));
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

// A long random topic name, made in this browser and remembered here, so the 3 steps
// show the same name each visit. No look-alike letters (l, 1, o, 0).
let topic = "";
function topicName() {
  if (topic) return topic;
  try {
    topic = localStorage.getItem("kal-ntfy-topic") || "";
  } catch (e) {
    topic = "";
  }
  if (!/^kal-[a-z2-9]{16}$/.test(topic)) {
    const abc = "abcdefghijkmnpqrstuvwxyz23456789";
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    topic = "kal-" + Array.from(bytes, (b) => abc[b % abc.length]).join("");
    try {
      localStorage.setItem("kal-ntfy-topic", topic);
    } catch (e) {
      // a private window: the name just won't be remembered
    }
  }
  return topic;
}

function renderAlerts(s) {
  const on = !!(s.alerts && s.alerts.enabled);
  $("alerts-on").hidden = !on;
  $("alerts-off").hidden = on;
  if (!on) {
    const name = topicName();
    $("topic-name").textContent = name;
    $("topic-line").textContent = "KAL_NTFY_TOPIC=" + name;
  }
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

// ---------- Trades ----------

let tradesLimit = 200;
let tradesData = null;
const tradeFilter = () => (document.querySelector('input[name="trade-filter"]:checked') || {}).value || "all";

async function loadTrades() {
  if (loading.trades) return;
  loading.trades = true;
  try {
    tradesData = await api(`/api/trades?limit=${tradesLimit}`);
    note("trades-msg", "");
    drawTrades();
  } catch (e) {
    note("trades-msg", e.message, true);
  } finally {
    loading.trades = false;
  }
}

function drawTrades() {
  const d = tradesData;
  if (!d) return;
  const names = strategyNames();
  const filter = tradeFilter();
  const key = JSON.stringify([d, filter, names]);
  if (drawn.trades === key) return;
  drawn.trades = key;

  // On Kalshi's demo site the bot's "live" orders are fake money: never call them real.
  const demo = d.env === "demo";
  $("totals-live-title").textContent = demo ? "Demo site (fake money)" : "Real money";
  $("filter-live").textContent = demo ? "Demo" : "Real";
  const totals = d.totals || {};
  totalsTable("totals-live", totals.live, names, demo ? "No demo trades yet." : "No real trades yet.");
  totalsTable("totals-paper", totals.paper, names, "No practice trades yet.");

  const rows = (d.trades || []).filter((t) => filter === "all" || t.kind === filter)
    .map((t) => tradeRow(t, names, demo));
  const empty = { live: demo ? "No demo trades yet." : "No real trades yet.", paper: "No practice trades yet." }[filter]
    || "No trades yet.";
  $("trade-list").tBodies[0].replaceChildren(...(rows.length ? rows : [emptyRow(7, empty)]));
  $("trades-more").hidden = !d.more;
}

function sumTallies(list) {
  const out = { trades: 0, open: 0, settled: 0, wins: 0, pnl: 0, cost: 0, open_cost: 0 };
  for (const t of list) for (const k of Object.keys(out)) out[k] += t[k] || 0;
  return out;
}

function totalsRow(name, t, sum) {
  return el("tr", { class: sum ? "sum" : null },
    leadTd("Strategy", name),
    td("Trades", String(t.trades || 0)),
    td("Open", t.open ? `${t.open}, ${money(t.open_cost)}` : "None"),
    td("Won", t.settled ? `${t.wins} of ${t.settled}` : "None finished yet"),
    td("Profit", t.settled ? upDown(t.pnl) : "—"));
}

function totalsTable(id, totals, names, emptyText) {
  const list = Object.entries(totals || {}).sort((a, b) => b[1].trades - a[1].trades);
  const rows = list.map(([name, t]) => totalsRow(names[name] || name, t));
  if (list.length > 1) rows.push(totalsRow("All", sumTallies(list.map(([, t]) => t)), true));
  $(id).tBodies[0].replaceChildren(...(rows.length ? rows : [emptyRow(5, emptyText)]));
}

function tradeRow(t, names, demo) {
  const [label, kind] = TRADE_STATE[t.state] || [t.status || "?", "off"];
  const real = t.kind === "live";
  const got = isNum(t.filled) ? t.filled : 0;
  const qty = real && got > 0 && isNum(t.count) && got < t.count ? `${num(got)} of ${num(t.count)}`
    : num(real && got > 0 ? got : t.count);
  let profit;
  if (isNum(t.pnl)) profit = upDown(t.pnl);
  else if (t.state === "open") profit = el("span", { class: "muted" }, "Not yet");
  else if (t.state === "checking") profit = el("span", { class: "muted" }, "Not known yet");
  else profit = el("span", { class: "muted" }, "$0.00");
  return el("tr", {},
    leadTd("Market", el("div", { class: "name" }, t.title || t.event || t.ticker || "?"),
      t.ticker ? el("div", { class: "desc" }, t.ticker) : null),
    td("When", when(t.ts)),
    td("Money", !real ? pill("Practice", "warn") : demo ? pill("Demo", "demo") : pill("Real", "real")),
    td("Strategy", names[t.strategy] || t.strategy || "?"),
    td("Bet", `${sideText(t.side)}: ${qty} at ${cents(t.price)}`),
    td("Status", pill(label, kind), t.error ? el("div", { class: "desc" }, t.error) : null),
    td("Profit", profit));
}

// ---------- Research ----------

let researchData = null;
let showRetired = false;

async function loadResearch() {
  if (loading.research) return;
  loading.research = true;
  try {
    researchData = await api("/api/research");
    drawResearch();
  } catch (e) {
    note("research-msg", e.message, true);
  } finally {
    loading.research = false;
  }
}

function drawResearchHead() {
  const sc = (state && state.scheduler) || {};
  const last = (researchData && researchData.last) || (state && state.research && state.research.last);
  let line = "Research has not run yet.";
  if (last && isNum(last.ts)) {
    const text = String(last.text || "").replace(/\.?$/, ".");
    line = `Last run: ${ago(last.ts)}. ${text}`;
    if (last.tested) line += ` It tested ${plural(last.tested, "series", "series")} and found ${plural(last.new || 0, "new idea")}.`;
  }
  if (sc.research && sc.research.running) line = "Research is running now.";
  $("research-last").textContent = line;
}

function drawResearch() {
  drawResearchHead();
  const d = researchData;
  if (!d) return;
  const cfg = (state && state.config) || {};
  const key = JSON.stringify([d, showRetired, cfg.env, cfg.budget, state && state.has_key]);
  if (drawn.research === key) return;
  drawn.research = key;
  $("edge-live-head").textContent = cfg.env === "demo" ? "Demo results" : "Real results";

  const counts = d.counts || {};
  $("research-counts").replaceChildren(...Object.entries(EDGE_STATUS)
    .filter(([k]) => counts[k])
    .map(([k, [label, kind]]) => el("li", {}, pill(`${label}: ${counts[k]}`, kind))));
  const w = d.words;
  $("research-words").textContent = w
    ? `Mention words: ${w.promoted ? "real money" : "practice only"}. ${plural(w.words || 0, "word")} known.` : "";
  if ((d.problems || []).length) note("research-msg", d.problems.join(" "), true);

  const edges = d.edges || [];
  const retired = edges.filter((e) => e.status === "retired").length;
  const rows = edges.filter((e) => showRetired || e.status !== "retired").map((e) => edgeRow(e, d));
  const empty = edges.length ? "No ideas are being tried right now." : "No ideas found yet. Research runs once a day.";
  $("edge-list").tBodies[0].replaceChildren(...(rows.length ? rows : [emptyRow(6, empty)]));
  const btn = $("show-retired");
  btn.hidden = !retired;
  btn.textContent = showRetired ? "Hide retired ideas" : `Show retired ideas (${retired})`;
}

function resultsText(t) {
  if (!t || !t.trades) return ["None yet."];
  return [el("div", {}, `${t.trades} trades. ${t.settled} finished, ${t.wins} won.`),
          t.settled ? el("div", {}, "Profit: ", upDown(t.pnl)) : null];
}

function practiceText(e, d) {
  const p = e.paper || {};
  const out = resultsText(p);
  const v = e.verdict;
  if (v && isNum(v.roi_lo) && p.settled) out.push(el("div", { class: "desc" }, `Worst case so far: ${roiPct(v.roi_lo)}`));
  const need = d.need || {};
  if (e.status === "candidate" && e.side && isNum(need.markets)) {
    const goal = isNum(need.events) ? `${need.markets} finished markets in ${need.events} groups (one series on one day)`
      : `${need.markets} finished markets`;
    const now = isNum(need.events) ? `${p.markets || 0} in ${p.events || 0}` : `${p.markets || 0}`;
    out.push(el("div", { class: "desc" }, `For real money it needs ${goal}, and a profit. So far: ${now}.`));
  }
  return out;
}

// Why an edge may not trade, or trade only on practice: the same checks favorites and
// the engine make.
function liveHint(e, d) {
  if (!e.side && (e.status === "live" || e.status === "candidate")) {
    return "Old idea with no side (Yes or No), so it is never traded.";
  }
  if (e.status !== "live") return "";
  if (d.favorites_mode !== "live") return "Set Favorites to Real to use money.";
  if (state && !state.has_key) return "No key, so it trades practice.";
  if (state && !(state.config.budget > 0)) return "The budget is $0, so it trades practice.";
  return "";
}

function edgeRow(e, d) {
  const [label, kind] = EDGE_STATUS[e.status] || [e.status || "?", "off"];
  const hint = liveHint(e, d);
  const hours = isNum(e.hours_before_close) ? `, ${hoursText(e.hours_before_close)}` : "";
  return el("tr", { class: e.status === "retired" ? "faded" : null },
    leadTd("Idea", el("div", { class: "name" }, e.series || e.id),
      el("div", { class: "desc" }, `${e.side ? `Buy ${sideText(e.side)} at` : "Price"} ${rangeText(e)}${hours}`),
      e.note ? el("div", { class: "desc" }, e.note) : null),
    td("Status", pill(label, kind), hint ? el("div", { class: "hint" }, hint) : null),
    td("Past test", el("div", {}, `ROI ${roiPct(e.roi)}`), el("div", { class: "desc" }, `Worst case ${roiPct(e.roi_lo)}`),
       isNum(e.trades) ? el("div", { class: "desc" },
         `${num(e.trades)} trades${isNum(e.events) ? `, ${num(e.events)} events` : ""}`) : null),
    td("Found", day(e.first_seen) || "?", e.last_tested ? el("div", { class: "desc" }, `Tested ${day(e.last_tested)}`) : null),
    td("Practice results", practiceText(e, d)),
    td(((state && state.config) || {}).env === "demo" ? "Demo results" : "Real results", resultsText(e.live)));
}

// ---------- Test an idea ----------

let btData = null;
let btWasRunning = false;
let btTimer = null;
let btAsked = 0;   // how many times we've asked for the list
let btFloor = 0;   // answers older than this are out of date (asked before "Run test", or overtaken)

async function loadBacktests() {
  const asked = ++btAsked;
  try {
    const d = await api("/api/backtests");
    if (asked < btFloor) return;
    btFloor = asked;  // a slow, older answer arriving later must not undo this one
    btData = d;
    drawBacktests();
  } catch (e) {
    note("bt-msg", e.message, true);
  } finally {
    clearTimeout(btTimer);
    if (page === "test" && btData && btData.running) btTimer = setTimeout(loadBacktests, BUSY_POLL_MS);
  }
}

function drawBacktests() {
  const d = btData;
  const hours = $("bt-hours");
  if (hours.value === "" && document.activeElement !== hours && isNum(d.hours_default)) {
    hours.value = String(d.hours_default);
  }
  if (isNum(d.max_series)) $("bt-series-help").textContent = `Put commas between them. Up to ${d.max_series}.`;
  if (isNum(d.min_trades) && isNum(d.min_events)) {
    $("edge-words").textContent = `The worst case is above 0, over ${d.min_trades} or more trades in ${d.min_events} or more groups (one series on one day).`;
  }

  const run = d.running;
  const failed = d.last && !d.last.ok;
  $("bt-run").disabled = !!run;
  if (run) {
    const age = isNum(d.now) && isNum(run.started) ? ` Started ${span(d.now - run.started)} ago.` : "";
    note("bt-msg", `Testing ${(run.series || []).join(", ")}.${age} It can take a while. You can leave this page.`);
  } else if (btWasRunning) {
    note("bt-msg", failed ? `The test failed: ${d.last.error || "no reason given"}` : "Done. The result is below.", failed);
  } else if (failed && !$("bt-msg").textContent) {
    note("bt-msg", `The last test failed: ${d.last.error || "no reason given"}`, true);
  }
  btWasRunning = !!run;

  const results = d.results || [];
  const key = JSON.stringify(results);
  if (drawn.test === key) return;
  drawn.test = key;
  if (!results.length) {
    $("bt-latest").replaceChildren(el("p", { class: "muted" }, "No tests yet. Pick a series, then press Run test."));
    $("bt-earlier").hidden = true;
    return;
  }
  $("bt-latest").replaceChildren(resultView(results[0]));
  const rest = results.slice(1);
  $("bt-earlier").hidden = !rest.length;
  $("bt-list").replaceChildren(...rest.map((r) => el("details", { class: "result-item" },
    el("summary", {}, resultTitle(r)), resultView(r))));
}

function resultTitle(r) {
  const what = (r.series || []).join(", ") || "?";
  const found = r.edges ? (r.edges === 1 ? "1 edge" : `${r.edges} edges`) : "no edge";
  return `${what}: ${when(r.ts)}, ${found}`;
}

function resultView(r) {
  const rows = r.calibration || [];
  const head = el("p", { class: "result-head" }, el("strong", {}, (r.series || []).join(", ") || "?"),
    isNum(r.hours_before_close) ? `. ${hoursText(r.hours_before_close)}.` : ".", ` ${when(r.ts)}`);
  if (!r.observations) {
    return el("div", { class: "result" }, head,
      el("p", {}, "No settled markets found. Check the series names, or try a different number of hours."));
  }
  const edges = rows.filter((x) => x.edge).length;
  const summary = el("p", { class: "result-sum" }, `${num(r.observations)} markets in ${num(r.events)} events. `,
    edges ? pill(edges === 1 ? "1 edge found" : `${edges} edges found`, "good")
      : "No edge found. That's normal: most prices are fair.");
  const parts = [head, summary];
  if (rows.length) parts.push(roiBars(rows), calibTable(rows));
  if (rows.length && rows.every((x) => !x.side)) {
    parts.push(el("p", { class: "muted" }, "This test mixes Yes and No buys. The bot only trades an edge "
      + "proven on one side, so daily research tests each side on its own."));
  }
  const m = mentionText(r.mentions);
  if (m) parts.push(el("p", { class: "muted" }, m));
  return el("div", { class: "result" }, parts);
}

// Bars grow left (loss) or right (profit) from a zero line. The value is written
// at the end of each row, so neither color nor length is the only clue.
function roiBars(rows) {
  const top = Math.max(0.05, ...rows.map((r) => (isNum(r.roi) ? Math.abs(r.roi) : 0)));
  const scale = Math.min(1, top);  // past 100%, a bar stops at the edge; its number still shows
  const list = el("ul", { class: "bars", "aria-label": "ROI by price range" });
  for (const r of rows) {
    const roi = isNum(r.roi) ? r.roi : 0;
    const bar = el("span", { class: "bar " + (roi >= 0 ? "pos" : "neg") });
    bar.style.width = (Math.min(1, Math.abs(roi) / scale) * 50).toFixed(2) + "%";
    const tip = `${buyText(r)}: ROI ${roiPct(r.roi)}. Worst case ${roiPct(r.roi_lo)}. `
      + `${num(r.trades)} trades.${r.edge ? " Edge." : ""}`;
    list.append(el("li", { class: "bar-row" + (r.edge ? " is-edge" : ""), tabindex: "0", "data-tip": tip },
      el("span", { class: "bar-label" }, r.side ? el("span", {}, sideText(r.side) + ":") : null,
        el("span", {}, rangeText(r)), r.edge ? pill("Edge", "good") : null),
      el("span", { class: "bar-track", "aria-hidden": "true" }, bar),
      el("span", { class: "bar-value" }, roiPct(r.roi))));
  }
  const end = Math.round(scale * 100) + "%";
  list.append(el("li", { class: "bar-row axis", "aria-hidden": "true" }, el("span", {}),
    el("span", { class: "bar-axis" }, el("span", {}, "-" + end), el("span", {}, "0"), el("span", {}, "+" + end)),
    el("span", {})));
  return el("figure", { class: "chart-fig" }, el("figcaption", { class: "label" }, "ROI by price range"), list);
}

function calibTable(rows) {
  const heads = ["Price range", "Trades", "Win rate", "ROI", "Worst case", "Edge"];
  return el("table", { class: "list calib" },
    el("thead", {}, el("tr", {}, heads.map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, rows.map((x) => el("tr", { class: x.edge ? "is-edge" : null },
      leadTd("Price range", buyText(x)),
      td("Trades", num(x.trades)),
      td("Win rate", isNum(x.win_rate) ? Math.round(x.win_rate * 100) + "%" : "—"),
      td("ROI", roiPct(x.roi)),
      td("Worst case", roiPct(x.roi_lo)),
      td("Edge", x.edge ? pill("Edge", "good") : "No")))));
}

function mentionText(m) {
  if (!m || !m.trades) return "";
  const gap = isNum(m.margin) ? ` (a gap of ${Math.round(m.margin * 100)}¢)` : "";
  return `Mention words${gap}: ${num(m.trades)} trades, ROI ${roiPct(m.roi)}, worst case ${roiPct(m.roi_lo)}.`
    + (m.enough ? "" : " Too few trades to judge.");
}

const seriesInBox = () => $("bt-series").value.split(/[\s,]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);

function addSeries(ticker) {
  const have = seriesInBox();
  if (!have.includes(ticker)) have.push(ticker);
  $("bt-series").value = have.join(", ");
}

function foundItem(s) {
  const added = seriesInBox().includes(s.ticker);
  const button = el("button", { class: "btn ghost", type: "button", disabled: added,
                                "aria-label": `Add ${s.ticker} to the test` }, added ? "Added" : "Add");
  button.addEventListener("click", () => {
    addSeries(s.ticker);
    button.textContent = "Added";
    button.disabled = true;
  });
  const about = [s.ticker, s.frequency, s.category].filter(Boolean).join(" · ");
  return el("li", {}, el("div", {}, el("div", { class: "name" }, s.title || s.ticker), el("div", { class: "desc" }, about)),
    button);
}

async function findSeries(e) {
  e.preventDefault();
  const q = $("find").value.trim();
  if (q.length < 2) {
    note("find-msg", "Type at least 2 letters.", true);
    return;
  }
  note("find-msg", "Looking. The first search can take a few seconds.");
  $("find-out").replaceChildren();
  try {
    const d = await api("/api/series?q=" + encodeURIComponent(q));
    const list = d.series || [];
    let text = list.length ? `Found ${d.total}.` : `Nothing matches "${d.query}". Try another word.`;
    if (d.total > list.length) text = `Showing ${list.length} of ${d.total}. Add a word to narrow it down.`;
    note("find-msg", text);
    $("find-out").replaceChildren(...list.map(foundItem));
  } catch (err) {
    note("find-msg", err.message, true);
  }
}

async function runBacktest(e) {
  e.preventDefault();
  const series = seriesInBox();
  const hours = Number($("bt-hours").value);
  if (!series.length) {
    note("bt-msg", "Type a series first. Or find one above.", true);
    $("bt-series").focus();
    return;
  }
  if (!(hours > 0)) {
    note("bt-msg", "Hours before close must be more than 0.", true);
    $("bt-hours").focus();
    return;
  }
  $("bt-run").disabled = true;
  try {
    await api("/api/backtest", { series: series.join(", "), hours_before_close: hours });
    btFloor = btAsked + 1;
    btWasRunning = true;
    note("bt-msg", `Testing ${series.join(", ")}. It can take a while.`);
  } catch (err) {
    note("bt-msg", err.message, true);
    $("bt-run").disabled = false;
  }
  loadBacktests();
}

// ---------- the hover note on chart bars ----------

function showTip(target) {
  const t = $("tip");
  t.textContent = target.getAttribute("data-tip");
  t.hidden = false;
  const r = target.getBoundingClientRect();
  const x = Math.max(8, Math.min(window.innerWidth - t.offsetWidth - 8, r.left + r.width / 2 - t.offsetWidth / 2));
  const y = r.top - t.offsetHeight - 8 < 8 ? r.bottom + 8 : r.top - t.offsetHeight - 8;
  t.style.left = x + "px";
  t.style.top = y + "px";
}

function hideTip() {
  $("tip").hidden = true;
}

const tipOf = (e) => (e.target instanceof Element ? e.target.closest("[data-tip]") : null);

// ---------- colors ----------

function savedTheme() {
  try {
    return localStorage.getItem("kal-theme") || "auto";
  } catch (e) {
    return "auto";
  }
}

function applyTheme(t) {
  if (!THEME_NAMES[t]) t = "auto";
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  const b = $("theme");
  if (b) b.textContent = "Colors: " + THEME_NAMES[t];
}

function nextTheme() {
  const order = ["auto", "light", "dark"];
  const now = document.documentElement.getAttribute("data-theme") || "auto";
  const t = order[(order.indexOf(now) + 1) % order.length];
  applyTheme(t);
  try {
    localStorage.setItem("kal-theme", t);
  } catch (e) {
    // a private window: the choice just won't be remembered
  }
  if (state) renderChart(state);
}

applyTheme(savedTheme());  // as early as possible, so the page doesn't flash the wrong colors

// ---------- start ----------

document.addEventListener("DOMContentLoaded", () => {
  applyTheme(savedTheme());
  $("theme").addEventListener("click", nextTheme);
  $("power").addEventListener("click", togglePower);
  $("budget-form").addEventListener("submit", saveBudget);
  for (const r of document.querySelectorAll('input[name="env"]')) r.addEventListener("change", setEnv);
  $("reset").addEventListener("click", resetTrip);
  $("check-now").addEventListener("click", (e) => runNow("trade", e.currentTarget, "msg"));
  $("research-now").addEventListener("click", (e) => runNow("research", e.currentTarget, "research-msg"));
  $("selftest").addEventListener("click", (e) => selfTest(e.currentTarget));
  $("test-alert").addEventListener("click", (e) => testAlert(e.currentTarget));
  $("find-form").addEventListener("submit", findSeries);
  $("bt-form").addEventListener("submit", runBacktest);
  $("trades-more").addEventListener("click", () => {
    tradesLimit = Math.min(5000, tradesLimit * 2);
    loadTrades();
  });
  for (const r of document.querySelectorAll('input[name="trade-filter"]')) r.addEventListener("change", drawTrades);
  $("show-retired").addEventListener("click", () => {
    showRetired = !showRetired;
    drawResearch();
  });

  for (const p of PAGES) $("tab-" + p).addEventListener("click", () => go(p));
  document.querySelector(".tabs").addEventListener("keydown", tabKeys);
  window.addEventListener("hashchange", () => showPage(pageFromHash()));

  document.addEventListener("pointerover", (e) => { const n = tipOf(e); if (n) showTip(n); });
  document.addEventListener("pointerout", (e) => {
    const n = tipOf(e);
    if (n && !(e.relatedTarget instanceof Node && n.contains(e.relatedTarget))) hideTip();
  });
  document.addEventListener("focusin", (e) => { const n = tipOf(e); if (n) showTip(n); else hideTip(); });
  document.addEventListener("focusout", hideTip);
  window.addEventListener("scroll", hideTip, { passive: true });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  if (dark.addEventListener) dark.addEventListener("change", () => state && renderChart(state));
  showPage(pageFromHash());
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
});
