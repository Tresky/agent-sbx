// The sbx portal. One file, no framework, no build step: a hash router and
// one render function per page. Every text from the server goes into the DOM
// as text, never as HTML.
"use strict";

// --- DOM ----------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    // CSSOM, not a style attribute: the page's CSP refuses inline style attributes.
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  add(el, children);
  return el;
}

// Like append(), but it skips null and false and flattens arrays: a page
// builds its children with `cond ? node : null` and with map().
function add(el, ...children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }

const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z"/>',
  box: '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  folder: '<path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M15 8l2 2"/>',
  check: '<path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  book: '<path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H20v15H5.5A1.5 1.5 0 0 0 4 19.5z"/><path d="M4 19.5A1.5 1.5 0 0 0 5.5 21H20v-3"/>',
  terminal: '<path d="m5 8 4 4-4 4M12 17h7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/>',
  play: '<path d="M7 5v14l11-7z"/>',
  power: '<path d="M12 3v8M6.3 7a8 8 0 1 0 11.4 0"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  upload: '<path d="M12 20V9M7 14l5-5 5 5M5 4h14"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.innerHTML = ICONS[name] || "";   // a fixed string from this file, never data
  return svg;
}

// --- the API ------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("the portal does not answer. Is `sbx web` still running?", 0);
  }
  let data = {};
  try { data = await resp.json(); } catch (e) { /* an empty or non-JSON body */ }
  if (!resp.ok) {
    if (resp.status === 401) signedOut();
    throw new ApiError(data.error || `${resp.status} ${resp.statusText}`, resp.status);
  }
  return data;
}
const GET = (p) => api("GET", p);
const POST = (p, b) => api("POST", p, b);
const PUT = (p, b) => api("PUT", p, b);
const DELETE = (p, b) => api("DELETE", p, b);
const enc = encodeURIComponent;

function signedOut() {
  const main = document.getElementById("main");
  add(clear(main), h("div", { class: "card" }, h("div", { class: "card-body stack" },
    h("h1", {}, "The portal signed you out"),
    h("p", {}, "The portal started again, so the old session ended. Run ", h("code", {}, "sbx web"),
      " in a terminal; it opens the current link."))));
  stopTimers();
}

// --- formats ------------------------------------------------------------------

function bytes(n) {
  if (n === null || n === undefined || isNaN(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}
function pct(x) { return x === null || x === undefined || isNaN(x) ? "–" : `${Math.round(x * 100)}%`; }
function duration(sec) {
  if (!sec && sec !== 0) return "–";
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), hh = Math.floor(m / 60), d = Math.floor(hh / 24);
  if (d) return `${d}d ${hh % 24}h`;
  if (hh) return `${hh}h ${m % 60}m`;
  return `${m}m ${sec % 60}s`;
}
function ago(ts) {
  if (!ts) return "–";
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "–"; }
function daysUntil(iso) {
  if (!iso) return null;
  const d = new Date(iso + "T00:00:00");
  const t = new Date(); t.setHours(0, 0, 0, 0);
  return Math.round((d - t) / 86400000);
}

// --- small parts ----------------------------------------------------------------

function pill(text, kind, dot) {
  return h("span", { class: `pill ${kind || ""}` }, dot ? h("span", { class: "dot" }) : null, text);
}
function statusPill(status) {
  const kind = { running: "ok", stopped: "", paused: "warn", suspended: "warn" }[status] ?? "warn";
  return pill(status || "unknown", kind, true);
}
function profilePill(p) { return pill(p, p === "agent" ? "info" : ""); }
function jobPill(status) {
  const kind = { running: "info running", ok: "ok", failed: "fail", cancelled: "warn", interrupted: "warn" }[status] || "";
  const text = { ok: "done" }[status] || status;
  return pill(text, kind, true);
}
function checkPill(s) { return pill(s === "ok" ? "ok" : s, { ok: "ok", WARN: "warn", FAIL: "fail" }[s]); }
function stateTpl(state) {
  const kind = { current: "ok", "OUT OF DATE": "warn", "not built": "", "no definition": "warn" }[state] ?? "";
  return pill(state === "OUT OF DATE" ? "out of date" : state, kind);
}

function meter(frac, label) {
  if (frac === null || frac === undefined || isNaN(frac)) return h("span", { class: "dim" }, "–");
  const cls = frac > .9 ? "max" : frac > .7 ? "hi" : "";
  return h("div", { class: "meter" },
    h("div", { class: "bar" }, h("i", { class: cls, style: `width:${Math.min(100, Math.max(1, frac * 100))}%` })),
    h("span", {}, label ?? pct(frac)));
}

function btn(label, onclick, opts = {}) {
  return h("button", { class: opts.class || "", onclick, disabled: opts.disabled, title: opts.title, type: "button" },
    opts.icon ? icon(opts.icon) : null, label);
}

function banner(kind, ...content) { return h("div", { class: `banner ${kind}` }, h("div", {}, ...content)); }

function card(title, body, opts = {}) {
  return h("div", { class: "card" + (opts.class ? " " + opts.class : "") },
    title !== null ? h("div", { class: "card-head" }, h("h2", {}, title), opts.actions || null) : null,
    h("div", { class: "card-body" + (opts.flush ? " flush" : "") }, body));
}

function kv(pairs) {
  return h("div", { class: "kv" }, pairs.filter(Boolean).map(([k, v]) => [h("div", {}, k), h("div", {}, v ?? "–")]));
}

function table(headers, rows, opts = {}) {
  if (!rows.length) return h("div", { class: "empty" }, opts.empty || "Nothing here yet.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, headers.map((c) => h("th", { class: c.startsWith("#") ? "num" : "" }, c.replace(/^#/, ""))))),
    h("tbody", {}, rows)));
}

function copyLine(text) {
  return h("div", { class: "copy-line" }, h("code", {}, text),
    btn("", async () => {
      try { await navigator.clipboard.writeText(text); toast("Copied", text, "ok"); }
      catch (e) { toast("The copy failed", String(e), "fail"); }
    }, { class: "ghost small", icon: "copy", title: "Copy" }));
}

function toast(title, text, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, h("b", {}, title), text ? h("div", { class: "small" }, text) : null);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), kind === "fail" ? 9000 : 4500);
}

function failToast(e) { toast("Failed", e.message || String(e), "fail"); }

function loading() { return h("div", { class: "loading" }, h("span", { class: "spin" })); }

function errorBox(e) { return banner("fail", h("b", {}, "Error. "), e.message || String(e)); }

// A menu of actions behind a "more" button.
function menu(items) {
  const wrap = h("div", { class: "menu" });
  const list = h("div", { class: "menu-list", hidden: true });
  const toggle = (e) => { e.stopPropagation(); list.hidden = !list.hidden; };
  for (const it of items.filter(Boolean)) {
    if (it === "-") { add(list, h("hr")); continue; }
    add(list, h("button", {
      type: "button", class: it.danger ? "danger" : "",
      onclick: (e) => { e.stopPropagation(); list.hidden = true; it.run(); },
    }, it.icon ? icon(it.icon) : null, it.label));
  }
  add(wrap, btn("", toggle, { class: "ghost small", icon: "more", title: "More" }), list);
  return wrap;
}

// --- modals -----------------------------------------------------------------------

function modal({ title, body, foot, wide, onclose }) {
  const overlay = h("div", { class: "overlay" });
  const close = () => { overlay.remove(); document.removeEventListener("keydown", esc); onclose && onclose(); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
  const box = h("div", { class: "modal" + (wide ? " wide" : "") },
    h("div", { class: "modal-head" }, h("h2", {}, title), btn("", close, { class: "ghost small", icon: "x" })),
    h("div", { class: "modal-body" }, body),
    foot ? h("div", { class: "modal-foot" }, typeof foot === "function" ? foot(close) : foot) : null);
  add(overlay, box);
  document.body.append(overlay);
  const first = box.querySelector("input, textarea, select");
  if (first) setTimeout(() => first.focus(), 30);
  return { close, box };
}

// A question before an action. `typed` makes the user type a word first,
// for an action that cannot be undone.
function confirmDialog({ title, text, action = "Continue", danger = false, typed = null }) {
  return new Promise((resolve) => {
    let done = false;
    const input = typed ? h("input", { placeholder: typed, autocomplete: "off" }) : null;
    const ok = h("button", { class: danger ? "danger solid" : "primary", type: "button", disabled: !!typed }, action);
    if (input) input.addEventListener("input", () => { ok.disabled = input.value.trim() !== typed; });
    const m = modal({
      title,
      body: [h("div", {}, text), input ? h("label", { class: "field" }, h("span", {}, `Type ${typed} to confirm`), input) : null],
      foot: (close) => [btn("Cancel", close), ok],
      onclose: () => { if (!done) resolve(false); },
    });
    ok.addEventListener("click", () => { done = true; m.close(); resolve(true); });
    if (input) input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !ok.disabled) ok.click(); });
  });
}

// --- jobs -----------------------------------------------------------------------

const watched = new Map();   // job id -> callbacks for its end
let knownRunning = new Set();

function lineClass(line) {
  if (line.startsWith("==> ") || line.startsWith("== ")) return "l-info";
  if (line.startsWith("WARNING:") || line.startsWith("  WARN")) return "l-warn";
  if (line.startsWith("sbx: error:") || line.startsWith("error:") || line.startsWith("  FAIL")) return "l-err";
  if (line.startsWith("+ ")) return "l-dim";
  return "";
}

function appendLines(pre, lines) {
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  for (const line of lines) {
    const cls = lineClass(line);
    add(pre, cls ? h("span", { class: cls }, line) : line, "\n");
  }
  if (stick) pre.scrollTop = pre.scrollHeight;
}

// Follow a job's log into `pre` until it ends. Returns a stop function.
function followJob(id, pre, onUpdate) {
  let since = 0, stopped = false, timer = null;
  const tick = async () => {
    if (stopped) return;
    try {
      const j = await GET(`/api/jobs/${id}?since=${since}`);
      if (j.lines.length) appendLines(pre, j.lines);
      since = j.since + j.lines.length;
      onUpdate && onUpdate(j);
      if (j.status === "running") timer = setTimeout(tick, 900);
    } catch (e) {
      if (!stopped) timer = setTimeout(tick, 3000);
    }
  };
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}

// Start watching a job that the portal just started: a modal with its live
// log, and a toast when it ends, even after the modal is closed.
function showJob(job, opts = {}) {
  if (opts.onDone) watched.set(job.id, opts.onDone);
  knownRunning.add(job.id);
  refreshSide();
  const pre = h("pre", { class: "log" });
  const status = h("span", {}, jobPill(job.status));
  const cancel = btn("Cancel", async () => {
    if (!(await confirmDialog({ title: "Cancel the job?", text: "The command stops at once. A half-made VM can stay; remove it then.", action: "Cancel the job", danger: true }))) return;
    try { await POST(`/api/jobs/${job.id}/cancel`); } catch (e) { failToast(e); }
  }, { class: "danger small" });
  let stop = null;
  const m = modal({
    title: job.title, wide: true,
    body: [h("div", { class: "row" }, status, h("code", { class: "small" }, job.command), h("div", { class: "spacer" }), cancel), pre],
    foot: (close) => [h("a", { class: "btn", href: `#/activity/${job.id}`, onclick: close }, "Open in Activity"),
      btn(opts.closeLabel || "Close", close, { class: "primary" })],
    onclose: () => stop && stop(),
  });
  stop = followJob(job.id, pre, (j) => {
    add(clear(status), jobPill(j.status));
    cancel.hidden = j.status !== "running";
  });
  return m;
}

async function startJob(promise, opts = {}) {
  try {
    const { job } = await promise;
    if (opts.quiet) {
      knownRunning.add(job.id);
      if (opts.onDone) watched.set(job.id, opts.onDone);
      toast("Started", job.title);
      refreshSide();
    } else {
      showJob(job, opts);
    }
    return job;
  } catch (e) {
    failToast(e);
    return null;
  }
}

// One poll for the whole page: the running count in the sidebar, and a toast
// for each job that ends.
async function pollJobs() {
  try {
    const { jobs } = await GET("/api/jobs");
    const running = new Set(jobs.filter((j) => j.status === "running").map((j) => j.id));
    for (const id of knownRunning) {
      if (running.has(id)) continue;
      const j = jobs.find((x) => x.id === id);
      if (!j) continue;
      const kind = j.status === "ok" ? "ok" : j.status === "failed" ? "fail" : "warn";
      toast(j.status === "ok" ? "Done" : j.status === "failed" ? "Failed" : "Stopped", j.title, kind);
      const cb = watched.get(id);
      watched.delete(id);
      if (cb) cb(j);
      if (currentRefresh) currentRefresh();
    }
    knownRunning = running;
    setCount("activity", running.size, true);
  } catch (e) { /* the next poll tries again */ }
}

// --- the Terminal ------------------------------------------------------------------

async function inTerminal(args, why) {
  try {
    const r = await POST("/api/terminal", { args });
    toast("Opened in Terminal", why || r.command, "ok");
  } catch (e) { failToast(e); }
}

function terminalBlock(args, text) {
  return h("div", { class: "stack" },
    text ? h("div", { class: "small muted" }, text) : null,
    h("div", { class: "row" }, btn("Open in Terminal", () => inTerminal(args), { icon: "terminal" })),
    copyLine("sbx " + args.map((a) => /^[\w@%+=:,./-]+$/.test(a) ? a : `'${a.replace(/'/g, "'\\''")}'`).join(" ")));
}

// --- the router ----------------------------------------------------------------------

let timers = [];
let currentRefresh = null;
function every(ms, fn) { timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms)); }
// A timer is an interval id, or a function that stops something else.
function stopTimers() { timers.forEach((t) => typeof t === "function" ? t() : clearInterval(t)); timers = []; currentRefresh = null; }

const ROUTES = [
  [/^\/?$/, pageOverview],
  [/^\/sandboxes$/, pageSandboxes],
  [/^\/sandboxes\/([a-z0-9-]+)(?:\/([a-z-]+))?$/, pageSandbox],
  [/^\/new$/, pageNew],
  [/^\/templates$/, pageTemplates],
  [/^\/templates\/import$/, pageImport],
  [/^\/templates\/versions$/, pageVersions],
  [/^\/templates\/([a-z][a-z0-9-]*)$/, pageTemplate],
  [/^\/projects$/, pageProjects],
  [/^\/projects\/([^/]+)$/, pageProject],
  [/^\/tokens$/, pageTokens],
  [/^\/checks$/, pageChecks],
  [/^\/activity$/, pageActivity],
  [/^\/activity\/([0-9a-f-]+)$/, pageJob],
  [/^\/settings(?:\/([a-z-]+))?$/, pageSettings],
  [/^\/docs(?:\/([A-Za-z-]+))?$/, pageDocs],
];

function hashParts() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, q] = raw.split("?");
  return { path, query: new URLSearchParams(q || "") };
}

async function render() {
  stopTimers();
  const { path, query } = hashParts();
  const main = document.getElementById("main");
  markNav(path);
  for (const [re, fn] of ROUTES) {
    const m = path.match(re);
    if (!m) continue;
    add(clear(main), loading());
    try {
      await fn(main, m.slice(1).map((x) => x && decodeURIComponent(x)), query);
    } catch (e) {
      add(clear(main), errorBox(e));
    }
    return;
  }
  add(clear(main), errorBox(new Error("no such page")));
}

function go(hash) { location.hash = hash; }

function page(main, { title, sub, crumbs, actions }, ...content) {
  document.title = `${title} · sbx`;
  add(clear(main), [
    h("div", { class: "head" },
      h("div", { class: "titles" },
        crumbs ? h("div", { class: "crumbs" }, crumbs.map((c, i) => [i ? " / " : "", c[1] ? h("a", { href: c[1] }, c[0]) : c[0]])) : null,
        h("h1", {}, title),
        sub ? h("div", { class: "sub" }, sub) : null),
      actions ? h("div", { class: "actions" }, actions) : null),
    content]);
}

// --- the sidebar ----------------------------------------------------------------------

const NAV = [
  ["overview", "#/", "Overview", "home"],
  ["sandboxes", "#/sandboxes", "Sandboxes", "box"],
  ["templates", "#/templates", "Templates", "layers"],
  ["projects", "#/projects", "Projects", "folder"],
  ["tokens", "#/tokens", "Tokens", "key"],
  "-",
  ["checks", "#/checks", "Checks", "check"],
  ["activity", "#/activity", "Activity", "activity"],
  ["settings", "#/settings", "Settings", "gear"],
  ["docs", "#/docs", "Docs", "book"],
];
const counts = {};

function buildSide() {
  const side = document.getElementById("side");
  add(clear(side), 
    h("a", { class: "brand", href: "#/" }, h("div", { class: "brand-mark" }, "sbx"),
      h("div", {}, h("div", { class: "brand-name" }, "sbx portal"), h("div", { class: "brand-sub" }, "sandboxes on Proxmox"))),
    h("nav", { class: "nav" }, NAV.map((n) => n === "-" ? h("div", { class: "nav-sep" }) :
      h("a", { href: n[1], "data-nav": n[0] }, icon(n[3]), n[2], h("span", { class: "count", "data-count": n[0], hidden: true })))),
    h("div", { class: "side-foot" },
      h("div", { class: "row", id: "side-host" }, h("span", {}, "Proxmox:"), h("span", {}, "…")),
      h("button", { class: "theme-btn", onclick: cycleTheme, id: "theme-btn" }, themeLabel())));
}

function markNav(path) {
  const top = path.split("/")[1] || "overview";
  const key = top === "new" ? "sandboxes" : top;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === key));
}

function setCount(key, n, hot) {
  const el = document.querySelector(`[data-count="${key}"]`);
  if (!el) return;
  el.hidden = !n;
  el.textContent = n;
  el.classList.toggle("hot", !!hot && n > 0);
}

async function refreshSide() {
  pollJobs();
}

async function sideHost() {
  const row = document.getElementById("side-host");
  try {
    const o = await GET("/api/overview");
    setCount("sandboxes", (o.sandboxes || []).length);
    const host = o.config ? (new URL(o.config.pve_api || "https://-").hostname || "not set") : "not set";
    const ok = o.config && !o.errors.pve;
    add(clear(row), h("span", { class: `pill ${ok ? "ok" : "fail"}`, title: o.errors.pve || "" }, h("span", { class: "dot" })),
      h("span", { title: o.errors.pve || `Proxmox ${o.pve_version || ""}` }, host));
  } catch (e) { /* the page shows the error */ }
}

// The theme: the system's, light, or dark. Kept in this browser only.
function theme() { try { return localStorage.getItem("sbx-theme") || "auto"; } catch (e) { return "auto"; } }
function applyTheme() {
  const t = theme();
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}
function themeLabel() { return `Theme: ${{ auto: "system", light: "light", dark: "dark" }[theme()]}`; }
function cycleTheme() {
  const next = { auto: "light", light: "dark", dark: "auto" }[theme()];
  try { localStorage.setItem("sbx-theme", next); } catch (e) { /* private window */ }
  applyTheme();
  document.getElementById("theme-btn").textContent = themeLabel();
}

// --- charts ---------------------------------------------------------------------------

function spark(series, opts = {}) {
  // series: arrays of numbers or null. One shared scale.
  const all = series.flat().filter((v) => v !== null && v !== undefined && !isNaN(v));
  const max = opts.max ?? Math.max(1e-9, ...all);
  const n = Math.max(...series.map((s) => s.length), 2);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 100 40");
  svg.setAttribute("preserveAspectRatio", "none");
  const mk = (tag, attrs) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  };
  add(svg, mk("line", { x1: 0, x2: 100, y1: 39.5, y2: 39.5, class: "axis", "vector-effect": "non-scaling-stroke" }));
  series.forEach((s, si) => {
    let d = "", area = "", start = null, last = null;
    s.forEach((v, i) => {
      const x = (i / (n - 1)) * 100;
      if (v === null || v === undefined || isNaN(v)) {
        if (start !== null && si === 0) area += `L${last},40L${start},40Z`;
        start = null; return;
      }
      const y = 39 - (Math.min(v, max) / max) * 36;
      if (start === null) { d += `M${x},${y}`; if (si === 0) area += `M${x},40L${x},${y}`; start = x; }
      else { d += `L${x},${y}`; if (si === 0) area += `L${x},${y}`; }
      last = x;
    });
    if (start !== null && si === 0) area += `L${last},40L${start},40Z`;
    if (si === 0 && area) add(svg, mk("path", { d: area, class: "area" }));
    if (d) add(svg, mk("path", { d, class: "line" + (si ? " b" : "") }));
  });
  return svg;
}

function chart(title, now, svg) {
  return h("div", { class: "chart" }, h("div", { class: "ch-head" }, h("span", {}, title), h("b", {}, now)), svg);
}

// --- markdown ------------------------------------------------------------------------

function mdInline(text) {
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)\s]+\))|(\*[^*\s][^*]*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (m[1]) out.push(h("code", {}, t.slice(1, -1)));
    else if (m[2]) out.push(h("strong", {}, mdInline(t.slice(2, -2))));
    else if (m[4]) out.push(h("em", {}, t.slice(1, -1)));
    else {
      const [, label, href] = t.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      out.push(mdLink(label, href));
    }
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function mdLink(label, href) {
  if (/^https?:\/\//.test(href)) return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, mdInline(label));
  const doc = href.split("#")[0].match(/(?:^|\/)([A-Za-z-]+)\.md$/);
  if (doc) return h("a", { href: `#/docs/${doc[1]}` }, mdInline(label));
  if (href.startsWith("#")) return h("span", {}, mdInline(label));
  return h("span", { title: href }, mdInline(label));
}

function markdown(text) {
  const root = h("div", { class: "md" });
  const lines = text.split("\n");
  let i = 0;
  const isTable = (l) => /^\s*\|.*\|\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      add(root, h("pre", { class: "code plain" }, buf.join("\n")));
      continue;
    }
    const hm = l.match(/^(#{1,4})\s+(.*)$/);
    if (hm) { add(root, h("h" + hm[1].length, {}, mdInline(hm[2]))); i++; continue; }
    if (isTable(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const head = cells(l);
      i += 2;
      const rows = [];
      while (i < lines.length && isTable(lines[i])) rows.push(cells(lines[i++]));
      add(root, h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, mdInline(c))))),
        h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, mdInline(c)))))))));
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
      const ordered = /^\s*\d+\./.test(l);
      const list = h(ordered ? "ol" : "ul");
      let item = null;
      while (i < lines.length && (/^\s*([-*]|\d+\.)\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && item))) {
        const li = lines[i].match(/^(\s*)([-*]|\d+\.)\s+(.*)$/);
        if (li && li[1].length < 2) { item = h("li", {}, mdInline(li[3])); add(list, item); }
        else if (li) { add(item, h("br"), "– ", ...mdInline(li[3])); }
        else add(item, " ", ...mdInline(lines[i].trim()));
        i++;
      }
      add(root, list);
      continue;
    }
    if (/^>\s?/.test(l)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      add(root, h("blockquote", {}, mdInline(buf.join(" "))));
      continue;
    }
    if (!l.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*([-*]|\d+\.)\s+|>)/.test(lines[i]) && !isTable(lines[i])) buf.push(lines[i++].trim());
    add(root, h("p", {}, mdInline(buf.join(" "))));
  }
  return root;
}

// --- pages: overview -------------------------------------------------------------

async function pageOverview(main) {
  const draw = async () => {
    const o = await GET("/api/overview");
    const boxes = o.sandboxes || [];
    const running = boxes.filter((b) => b.status === "running").length;
    const expired = boxes.filter((b) => b.expired);
    const tpls = (o.templates || []).filter((t) => t.versions.length);
    const stale = (o.templates || []).filter((t) => t.state === "OUT OF DATE");
    const c = o.claude || {};
    const banners = [];
    if (o.config_error) banners.push(banner("fail", h("b", {}, "The settings do not load. "), o.config_error, " ",
      h("a", { href: "#/settings" }, "Open Settings")));
    if (o.errors && o.errors.pve) banners.push(banner("fail", h("b", {}, "Proxmox does not answer. "), o.errors.pve, " ",
      h("a", { href: "#/checks" }, "Run the checks")));
    if (c.warning) banners.push(banner("warn", c.warning, " ", h("a", { href: "#/tokens" }, "Open Tokens")));
    if (expired.length) banners.push(banner("warn", `${expired.length} sandbox(es) expired: ${expired.map((b) => b.name).join(", ")}. `,
      h("a", { href: "#/sandboxes" }, "Remove them on the Sandboxes page")));

    const stat = (label, value, note, href) => h("div", { class: "card stat" }, h("div", { class: "label" }, label),
      href ? h("a", { class: "value", href }, value) : h("div", { class: "value" }, value), h("div", { class: "note" }, note));

    const sbTable = table(["Name", "Status", "Profile", "Template", "Project", "CPU", "Memory", "Expires"],
      boxes.map((b) => h("tr", {},
        h("td", {}, h("a", { href: `#/sandboxes/${b.name}` }, b.name)),
        h("td", {}, statusPill(b.status)), h("td", {}, profilePill(b.profile)),
        h("td", {}, b.template), h("td", {}, b.project || h("span", { class: "dim" }, "–")),
        h("td", {}, b.status === "running" ? meter(b.cpu) : h("span", { class: "dim" }, "–")),
        h("td", {}, b.status === "running" && b.maxmem ? meter(b.mem / b.maxmem, bytes(b.mem)) : h("span", { class: "dim" }, "–")),
        h("td", {}, expiryText(b)))), { empty: h("div", {}, "No sandbox yet. ", h("a", { href: "#/new" }, "Make one")) });

    const checks = h("div", { class: "checklist compact" }, (o.checks || []).map((ck) => h("div", { class: "item" },
      checkPill(ck.status), h("div", { class: "name" }, ck.name), h("div", { class: "detail" }, ck.detail))));

    const acts = (o.jobs || []).length ? h("div", { class: "checklist compact" }, o.jobs.map((j) => h("div", { class: "item" },
      jobPill(j.status), h("div", { class: "name" }, h("a", { href: `#/activity/${j.id}` }, j.title)),
      h("div", { class: "detail small" }, ago(j.started))))) : h("div", { class: "empty" }, "Nothing has run from the portal yet.");

    page(main, {
      title: "Overview",
      sub: o.config ? `${o.config.pve_api || "no Proxmox host set"} · domain ${o.config.domain}` + (o.pve_version ? ` · Proxmox ${o.pve_version}` : "") : "",
      actions: [h("a", { class: "btn primary", href: "#/new" }, icon("plus"), "New sandbox"),
        h("a", { class: "btn", href: "#/checks" }, icon("check"), "Run the checks")],
    },
    banners.length ? h("div", { class: "stack", style: "margin-bottom:14px" }, banners) : null,
    h("div", { class: "grid cols-4" },
      stat("Sandboxes", `${running} / ${boxes.length}`, "running / all", "#/sandboxes"),
      stat("Expired", String(expired.length), expired.length ? "remove them with a clean-up" : "none", "#/sandboxes"),
      stat("Templates", `${tpls.length}`, stale.length ? `${stale.length} out of date` : "built, all current", "#/templates"),
      stat("Claude token", c.stored ? "stored" : "none", c.stored ? (c.expires ? `expires ${c.expires}` : "no expiry date on record") : "sandboxes ask for a sign-in", "#/tokens")),
    card("Sandboxes", sbTable, { flush: true, class: "mt", actions: h("a", { href: "#/sandboxes", class: "small" }, "All sandboxes") }),
    h("div", { class: "grid cols-2 mt" },
      card("This Mac", checks, { flush: true, actions: h("a", { href: "#/checks", class: "small" }, "All checks") }),
      card("Recent activity", acts, { flush: true, actions: h("a", { href: "#/activity", class: "small" }, "All activity") })));
  };
  currentRefresh = draw;
  await draw();
  every(10000, draw);
}

function expiryText(b) {
  if (!b.expires) return h("span", { class: "dim" }, "never");
  const d = daysUntil(b.expires);
  if (b.expired) return pill(`expired ${b.expires}`, "fail");
  if (d <= 1) return pill(d === 0 ? "today" : "tomorrow", "warn");
  return h("span", { title: b.expires }, `in ${d} days`);
}

// --- pages: sandboxes ------------------------------------------------------------------

const sbFilter = { text: "", profile: "all", status: "all" };

async function pageSandboxes(main) {
  const body = h("div");
  const search = h("input", { class: "search", placeholder: "Filter by name, project, template", value: sbFilter.text });
  const seg = (key, options) => {
    const el = h("div", { class: "seg" });
    const drawSeg = () => {
      add(clear(el), options.map(([v, label]) => h("button", {
        type: "button", class: sbFilter[key] === v ? "on" : "",
        onclick: () => { sbFilter[key] = v; drawSeg(); drawTable(); },
      }, label)));
    };
    drawSeg();
    return el;
  };
  let data = null;
  const cleanBtn = h("span");
  const drawTable = () => {
    if (!data) return;
    const q = sbFilter.text.toLowerCase();
    const rows = data.sandboxes.filter((b) =>
      (sbFilter.profile === "all" || b.profile === sbFilter.profile) &&
      (sbFilter.status === "all" || (sbFilter.status === "running" ? b.status === "running" : b.status !== "running")) &&
      (!q || [b.name, b.project, b.template].some((x) => (x || "").toLowerCase().includes(q))));
    const expired = data.sandboxes.filter((b) => b.expired);
    add(clear(cleanBtn), expired.length ? btn(`Remove ${expired.length} expired`, async () => {
      if (!(await confirmDialog({ title: "Remove the expired sandboxes?", danger: true, action: "Destroy them",
        text: `This destroys ${expired.map((b) => b.name).join(", ")} and their snapshots. It cannot be undone.` }))) return;
      startJob(POST("/api/gc"), { onDone: () => drawData() });
    }, { class: "danger", icon: "trash" }) : null);
    add(clear(body), card(null, table(
      ["Name", "Status", "Profile", "Template", "Project", "CPU", "Memory", "Disk", "Uptime", "Expires", ""],
      rows.map((b) => h("tr", {},
        h("td", {}, h("a", { href: `#/sandboxes/${b.name}` }, h("b", {}, b.name)), h("div", { class: "small dim" }, `VM ${b.vmid}`)),
        h("td", {}, statusPill(b.status), b.lock ? [" ", pill(b.lock, "warn")] : null),
        h("td", {}, profilePill(b.profile)),
        h("td", {}, h("a", { href: `#/templates/${b.template}` }, b.template)),
        h("td", {}, b.project ? h("a", { href: `#/projects/${enc(b.project)}` }, b.project) : h("span", { class: "dim" }, "–")),
        h("td", {}, b.status === "running" ? meter(b.cpu) : h("span", { class: "dim" }, "–")),
        h("td", {}, b.status === "running" && b.maxmem ? meter(b.mem / b.maxmem, bytes(b.mem)) : h("span", { class: "dim" }, "–")),
        h("td", { class: "nowrap" }, bytes(b.maxdisk)),
        h("td", { class: "nowrap" }, b.status === "running" ? duration(b.uptime) : "–"),
        h("td", {}, expiryText(b)),
        h("td", { class: "right" }, sandboxMenu(b, drawData)))),
      { empty: data.sandboxes.length ? "No sandbox matches the filter." : h("div", {}, "No sandbox yet. ", h("a", { href: "#/new" }, "Make one")) }),
    { flush: true }));
  };
  const drawData = async () => {
    // A redraw would close a menu that the user has open.
    if (document.querySelector(".menu-list:not([hidden])")) return;
    try { data = await GET("/api/sandboxes"); setCount("sandboxes", data.sandboxes.length); drawTable(); }
    catch (e) { add(clear(body), errorBox(e)); }
  };
  search.addEventListener("input", () => { sbFilter.text = search.value; drawTable(); });
  page(main, {
    title: "Sandboxes", sub: "Each sandbox is a linked clone of a template, on its own bridge.",
    actions: [cleanBtn, h("a", { class: "btn primary", href: "#/new" }, icon("plus"), "New sandbox")],
  },
  h("div", { class: "row", style: "margin-bottom:12px" }, search,
    seg("profile", [["all", "All"], ["agent", "Agent"], ["personal", "Personal"]]),
    seg("status", [["all", "All"], ["running", "Running"], ["stopped", "Stopped"]]),
    h("div", { class: "spacer" }), btn("Refresh", drawData, { class: "ghost small", icon: "refresh" })),
  body);
  add(body, loading());
  currentRefresh = drawData;
  await drawData();
  every(5000, drawData);
}

function powerAction(b, action, after) {
  const labels = { start: "Start", shutdown: "Shut down", stop: "Stop", reboot: "Reboot" };
  return async () => {
    if (action === "stop" && !(await confirmDialog({ title: `Stop ${b.name}?`, action: "Stop", danger: true,
      text: "Stop pulls the power: unsaved work in the sandbox is lost. Shut down asks the guest to stop cleanly." }))) return;
    startJob(POST(`/api/sandboxes/${b.name}/power`, { action }), { quiet: true, onDone: after });
    toast(`${labels[action]}: ${b.name}`, "The sandbox changes state in a few seconds.");
  };
}

async function destroySandbox(b, after) {
  if (!(await confirmDialog({ title: `Destroy ${b.name}?`, danger: true, action: "Destroy", typed: b.name,
    text: [`This destroys VM ${b.vmid}, its disk and its snapshots. `, h("b", {}, "It cannot be undone."),
      b.profile === "personal" ? " Work that is not pushed is lost." : ""] }))) return false;
  startJob(DELETE(`/api/sandboxes/${b.name}`), { onDone: after });
  return true;
}

function sandboxMenu(b, after) {
  const running = b.status === "running";
  return menu([
    running && { label: "Open a shell in Terminal", icon: "terminal", run: () => inTerminal(["ssh", b.name]) },
    !running && { label: "Start", icon: "play", run: powerAction(b, "start", after) },
    running && { label: "Shut down", icon: "power", run: powerAction(b, "shutdown", after) },
    running && { label: "Reboot", icon: "refresh", run: powerAction(b, "reboot", after) },
    running && { label: "Stop (pull the power)", icon: "power", run: powerAction(b, "stop", after) },
    { label: "Take a snapshot", icon: "camera", run: () => snapshotDialog(b, after) },
    "-",
    { label: "Destroy…", icon: "trash", danger: true, run: () => destroySandbox(b, after) },
  ]);
}

function snapshotDialog(b, after) {
  const input = h("input", { value: "", placeholder: "clean", pattern: "[A-Za-z][A-Za-z0-9_-]*" });
  const m = modal({
    title: `Snapshot ${b.name}`,
    body: [h("label", { class: "field" }, h("span", {}, "Label"), input,
      h("span", { class: "hint" }, "A letter first, then letters, digits, - and _. The default is clean; a label that exists is refused.")),
    h("div", { class: "small muted" }, "The snapshot has the disk only, not the memory. After a rollback the sandbox starts again.")],
    foot: (close) => [btn("Cancel", close), btn("Take the snapshot", () => {
      close();
      startJob(POST(`/api/sandboxes/${b.name}/snapshots`, { label: input.value.trim() || "clean" }), { onDone: after });
    }, { class: "primary", icon: "camera" })],
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") m.box.querySelector(".modal-foot .primary").click(); });
}

// --- pages: one sandbox --------------------------------------------------------------

const SB_TABS = [["", "Overview"], ["ports", "Ports"], ["system", "System"], ["logs", "Logs"], ["snapshots", "Snapshots"],
  ["remote", "Remote Control"], ["config", "Config"], ["tasks", "Tasks"]];

async function pageSandbox(main, [name, tab]) {
  tab = tab || "";
  let d = await GET(`/api/sandboxes/${name}`);
  const b = d.sandbox;
  const tabBody = h("div");
  const running = b.status === "running";
  const reload = () => render();
  const actions = [
    running ? btn("Shell", () => inTerminal(["ssh", b.name]), { icon: "terminal", title: "sbx ssh in Terminal" }) : null,
    !running ? btn("Start", powerAction(b, "start", reload), { icon: "play", class: "primary" }) : null,
    running ? btn("Shut down", powerAction(b, "shutdown", reload), { icon: "power" }) : null,
    btn("Snapshot", () => snapshotDialog(b, reload), { icon: "camera" }),
    menu([
      running && { label: "Reboot", icon: "refresh", run: powerAction(b, "reboot", reload) },
      running && { label: "Stop (pull the power)", icon: "power", run: powerAction(b, "stop", reload) },
      d.herdr && { label: "Add to the herdr sidebar", run: () => startJob(POST(`/api/sandboxes/${b.name}/herdr`)) },
      d.herdr && b.project && { label: "Build the herdr panes again", run: () => startJob(POST(`/api/sandboxes/${b.name}/layout`, { replace: true })) },
      d.herdr && { label: "A herdr window on it (Terminal)", icon: "terminal", run: () => inTerminal(["herdr", b.name, "--attach"]) },
      { label: "Change the expiry…", run: () => expiryDialog(b, reload) },
      d.gpu_mapping && { label: "Give it the GPU", run: () => startJob(POST(`/api/sandboxes/${b.name}/gpu`, { action: "attach" }), { onDone: reload }) },
      d.gpu_mapping && { label: "Take the GPU away", run: () => startJob(POST(`/api/sandboxes/${b.name}/gpu`, { action: "detach" }), { onDone: reload }) },
      "-",
      { label: "Destroy…", icon: "trash", danger: true, run: async () => { if (await destroySandbox(b, () => go("#/sandboxes"))) { /* the job modal shows */ } } },
    ]),
  ];
  const url = `${b.https ? "https" : "http"}://${b.fqdn}`;
  page(main, {
    title: b.hostname,
    crumbs: [["Sandboxes", "#/sandboxes"], [b.name]],
    sub: h("span", { class: "row", style: "gap:8px" }, statusPill(b.status), profilePill(b.profile),
      h("span", { class: "mono small" }, `${url}:<port>`), h("span", { class: "dim small" }, `VM ${b.vmid} on ${b.node}`)),
    actions,
  },
  d.running_job ? banner("info", h("b", {}, "A job runs on this sandbox: "), d.running_job.title, " ",
    h("a", { href: `#/activity/${d.running_job.id}` }, "Follow it")) : null,
  h("div", { class: "tabs", style: d.running_job ? "margin-top:14px" : "" }, SB_TABS
    .filter(([id]) => id !== "remote" || d.remote_control_allowed)
    .map(([id, label]) => h("a", { href: `#/sandboxes/${b.name}${id ? "/" + id : ""}`, class: id === tab ? "on" : "" }, label))),
  tabBody);

  const need = (what) => running ? null : banner("info", `${b.name} is ${b.status}. Start it to see ${what}.`);
  const tabs = {
    "": () => sbOverview(tabBody, d, reload),
    ports: () => need("its ports") || sbPorts(tabBody, b),
    system: () => need("its system") || sbSystem(tabBody, b),
    logs: () => need("its logs") || sbLogs(tabBody, b),
    snapshots: () => sbSnapshots(tabBody, d, reload),
    remote: () => need("Remote Control") || sbRemote(tabBody, b),
    config: () => sbConfig(tabBody, d),
    tasks: () => sbTasks(tabBody, b),
  };
  const out = await (tabs[tab] || tabs[""])();
  if (out instanceof Node) add(tabBody, out);
}

function sbOverview(el, d, reload) {
  const b = d.sandbox, conf = d.config || {}, cur = d.current || {};
  const disk = (conf.scsi0 || "").match(/size=([^,]+)/);
  const facts = kv([
    ["Name", b.hostname],
    ["Address", h("span", { class: "mono" }, b.fqdn)],
    ["IP address", d.address || h("span", { class: "dim" }, b.status === "running" ? "the guest agent has none yet" : "–")],
    ["Profile", [profilePill(b.profile), " ", h("span", { class: "small muted" }, b.profile === "agent" ? "internet only" : "internet and LAN")]],
    ["Template", h("a", { href: `#/templates/${b.template}` }, b.template)],
    ["Project", b.project ? h("a", { href: `#/projects/${enc(b.project)}` }, b.project) : "–"],
    ["Expires", h("span", { class: "row", style: "gap:8px" }, expiryText(b), btn("Change", () => expiryDialog(b, reload), { class: "small ghost" }))],
    ["VM", `${b.vmid} on node ${b.node}`],
    ["Size", `${conf.cores || "?"} cores · ${conf.memory ? bytes(conf.memory * 1048576) : "?"} memory · ${disk ? disk[1] : "?"} disk`],
    ["Uptime", b.status === "running" ? duration(cur.uptime ?? b.uptime) : "–"],
    ["HTTPS", b.https ? pill("certificate from mkcert", "ok") : pill("http only", "warn")],
    conf.hostpci0 ? ["GPU", pill("attached", "info")] : null,
    ["Tags", h("span", { class: "row", style: "gap:4px" }, b.tags.map((t) => h("span", { class: "tag" }, t)))],
  ]);

  const rrd = d.rrd || [];
  const cpu = rrd.map((p) => p.cpu), mem = rrd.map((p) => p.mem);
  const nin = rrd.map((p) => p.netin), nout = rrd.map((p) => p.netout);
  const dr = rrd.map((p) => p.diskread), dw = rrd.map((p) => p.diskwrite);
  const lastOf = (a) => [...a].reverse().find((v) => v !== null && v !== undefined);
  const metrics = rrd.length ? h("div", { class: "grid cols-2" },
    chart("CPU", pct(lastOf(cpu)), spark([cpu], { max: 1 })),
    chart("Memory", bytes(lastOf(mem)), spark([mem], { max: (rrd.find((p) => p.maxmem) || {}).maxmem })),
    chart("Network in / out", `${bytes(lastOf(nin))}/s · ${bytes(lastOf(nout))}/s`, spark([nin, nout])),
    chart("Disk read / write", `${bytes(lastOf(dr))}/s · ${bytes(lastOf(dw))}/s`, spark([dr, dw])))
    : h("div", { class: "empty" }, d.errors.rrd || "No data yet.");

  const reach = h("div", { class: "stack" },
    copyLine(`sbx ssh ${b.name}`), copyLine(`ssh ${b.hostname}`),
    copyLine(`${b.https ? "https" : "http"}://${b.fqdn}:3000`),
    h("div", { class: "small muted" }, "Each port in the sandbox is direct, with the same number. The Ports tab lists what listens."));

  add(el, h("div", { class: "grid", style: "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" },
    card("Facts", facts),
    h("div", {}, card("The last hour", metrics), card("Reach it", reach))));
}

function expiryDialog(b, after) {
  const mode = h("select", {}, h("option", { value: "days" }, "In a number of days"), h("option", { value: "date" }, "On a date"),
    h("option", { value: "never" }, "Never"));
  const days = h("input", { type: "number", min: 0, max: 3650, value: 3 });
  const date = h("input", { type: "date", value: b.expires || "" });
  const fields = h("div");
  const drawFields = () => add(clear(fields), mode.value === "days" ? h("label", { class: "field" }, h("span", {}, "Days from today"), days)
    : mode.value === "date" ? h("label", { class: "field" }, h("span", {}, "The date"), date) : h("div", { class: "small muted" }, "The sandbox stays until you destroy it. `sbx gc` leaves it alone."));
  mode.addEventListener("change", drawFields);
  drawFields();
  modal({
    title: `The expiry of ${b.name}`,
    body: [h("div", { class: "small muted" }, "The expiry is a tag on the VM. `sbx gc` and the clean-up on the Sandboxes page destroy a sandbox after its date."),
      h("label", { class: "field" }, h("span", {}, "Expires"), mode), fields],
    foot: (close) => [btn("Cancel", close), btn("Save", async () => {
      const body = mode.value === "never" ? { never: true } : mode.value === "days" ? { days: days.value } : { date: date.value };
      try { await POST(`/api/sandboxes/${b.name}/expiry`, body); close(); toast("Saved", "The new expiry is set.", "ok"); after(); }
      catch (e) { failToast(e); }
    }, { class: "primary" })],
  });
}

async function sbPorts(el, b) {
  let showAll = false, data = null;
  const list = h("div");
  const draw = () => {
    const ports = data.ports.filter((p) => showAll || !p.system);
    add(clear(list), ports.length ? h("div", { class: "port-list" }, ports.map((p) => h("div", { class: "port" },
      h("div", { class: "num" }, p.port),
      h("div", { class: "what" }, h("div", {}, p.process || h("span", { class: "dim" }, "unknown process")),
        h("div", { class: "small dim mono" }, p.addresses.join(", "))),
      p.system ? pill("system") : null,
      p.system ? null : h("a", { class: "btn small", href: p.url, target: "_blank", rel: "noopener noreferrer" }, icon("ext"), "Open")))) :
      h("div", { class: "empty" }, "Nothing listens yet. A dev server on 127.0.0.1 appears here when it starts."));
  };
  const load = async () => {
    try { data = await GET(`/api/sandboxes/${b.name}/ports`); draw(); } catch (e) { add(clear(list), errorBox(e)); }
  };
  add(el, card("Listening ports", list, {
    actions: [h("label", { class: "check small" }, h("input", { type: "checkbox", onchange: (e) => { showAll = e.target.checked; data && draw(); } }), "Show system ports"),
      btn("Refresh", load, { class: "ghost small", icon: "refresh" })],
  }), h("div", { class: "small muted mt" }, "A port on 127.0.0.1 reaches the Mac through the port mirror, with https when the sandbox has a certificate. A port on 0.0.0.0 (Docker) is reachable as it is, over http."));
  add(list, loading());
  await load();
  every(8000, load);
}

async function sbSystem(el, b) {
  add(el, loading());
  let s;
  try { s = await GET(`/api/sandboxes/${b.name}/system`); } catch (e) { add(clear(el), errorBox(e)); return; }
  const sec = s.sections;
  const mem = (sec.memory || "").split(/\s+/);
  add(clear(el), h("div", { class: "grid cols-2" },
    card("System", kv([
      ["OS", (sec.os || "").split("\n").join(" · ")],
      ["Uptime", sec.uptime],
      ["Load", sec.load],
      ["Disk /", sec.disk ? (() => { const c = sec.disk.split(/\s+/); return `${c[2]} used of ${c[1]} (${c[4]})`; })() : "–"],
      ["Memory", mem.length > 2 ? `${mem[2]} MB used of ${mem[1]} MB` : "–"],
      ["Claude Code", sec.claude === "signed-in" ? pill("signed in with the Claude token", "ok") : pill("no token", "warn")],
      ["Recipe", sec.recipe ? h("span", { class: "mono small" }, sec.recipe) : "–"],
    ])),
    card("Template", sec.template ? h("pre", { class: "code" }, sec.template) : h("div", { class: "dim" }, "no /etc/sbx/template"))),
  card("Repositories in ~/code", table(["Repository", "Branch", "#Changed files", "Last commit"], s.repos.map((r) => h("tr", {},
    h("td", {}, h("b", {}, r.name)), h("td", { class: "mono" }, r.branch),
    h("td", { class: "num" }, r.changed ? pill(String(r.changed), "warn") : "0"), h("td", { class: "mono small" }, r.last))),
  { empty: "No clone in ~/code." }), { flush: true, class: "mt" }),
  card("Docker containers", table(["Name", "Image", "Status"], s.containers.map((c) => h("tr", {},
    h("td", {}, c.name), h("td", { class: "mono small" }, c.image),
    h("td", {}, pill(c.status, /^Up/.test(c.status) ? "ok" : ""))))), { flush: true, class: "mt" }),
  sec.services ? card("Failed services", h("pre", { class: "code" }, sec.services), { class: "mt" }) : null);
}

const logState = { which: "recipe", lines: "300", auto: false };

async function sbLogs(el, b) {
  const { logs } = await GET("/api/logs");
  const pick = h("select", {}, logs.map((l) => h("option", { value: l.id, selected: l.id === logState.which }, l.title)));
  const lines = h("select", {}, ["100", "300", "1000", "3000"].map((n) => h("option", { value: n, selected: n === logState.lines }, `${n} lines`)));
  const pre = h("pre", { class: "log", style: "max-height:70vh" });
  const auto = h("input", { type: "checkbox", checked: logState.auto });
  const load = async () => {
    logState.which = pick.value; logState.lines = lines.value;
    try {
      const r = await GET(`/api/sandboxes/${b.name}/logs/${pick.value}?lines=${lines.value}`);
      const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30 || !pre.textContent;
      clear(pre);
      appendLines(pre, (r.text || "(empty)").replace(/\n$/, "").split("\n"));
      if (atEnd) pre.scrollTop = pre.scrollHeight;
    } catch (e) { add(clear(pre), e.message); }
  };
  pick.addEventListener("change", load);
  lines.addEventListener("change", load);
  auto.addEventListener("change", () => { logState.auto = auto.checked; });
  add(el, h("div", { class: "row", style: "margin-bottom:10px" },
    h("div", { style: "width:220px" }, pick), h("div", { style: "width:140px" }, lines),
    btn("Refresh", load, { icon: "refresh" }),
    h("label", { class: "check small" }, auto, "Refresh every 3 seconds")), pre);
  await load();
  every(3000, () => { if (logState.auto) load(); });
}

function sbSnapshots(el, d, reload) {
  const b = d.sandbox;
  const snaps = (d.snapshots || []).slice().sort((x, y) => (y.snaptime || 0) - (x.snaptime || 0));
  add(el, card("Snapshots", d.errors.snapshots ? errorBox(new Error(d.errors.snapshots)) : table(["Label", "Taken", "Description", "Parent", ""],
    snaps.map((s) => h("tr", {},
      h("td", {}, h("b", {}, s.name)), h("td", {}, when(s.snaptime)), h("td", { class: "small" }, s.description || "–"),
      h("td", { class: "dim" }, s.parent || "–"),
      h("td", { class: "right nowrap" },
        btn("Roll back", async () => {
          if (!(await confirmDialog({ title: `Roll ${b.name} back to ${s.name}?`, danger: true, action: "Roll back",
            text: "Every change after the snapshot is lost: files, packages, running processes. The sandbox starts again after it." }))) return;
          startJob(POST(`/api/sandboxes/${b.name}/snapshots/${enc(s.name)}/rollback`), { onDone: reload });
        }, { class: "small", icon: "undo" }), " ",
        btn("Delete", async () => {
          if (!(await confirmDialog({ title: `Delete the snapshot ${s.name}?`, danger: true, action: "Delete",
            text: "The sandbox stays as it is. You cannot roll back to this snapshot after this." }))) return;
          startJob(DELETE(`/api/sandboxes/${b.name}/snapshots/${enc(s.name)}`), { quiet: true, onDone: reload });
        }, { class: "small danger", icon: "trash" })))),
    { empty: b.profile === "agent" ? "No snapshot. An agent sandbox gets 'clean' after its recipe succeeds." : "No snapshot yet." }),
  { flush: true, actions: btn("Take a snapshot", () => snapshotDialog(b, reload), { icon: "camera", class: "small" }) }));
}

async function sbRemote(el, b) {
  add(el, loading());
  let st;
  try { st = await GET(`/api/sandboxes/${b.name}/remote-control`); } catch (e) { add(clear(el), errorBox(e)); return; }
  const modes = ["acceptEdits", "default", "plan", "bypassPermissions"];
  const mode = h("select", {}, modes.map((m) => h("option", { value: m, selected: m === (st.mode || "acceptEdits") }, m)));
  const body = h("div", { class: "stack" });
  const reload = () => { clear(el); sbRemote(el, b); };
  add(clear(el), card("Claude Code Remote Control", body), h("div", { class: "small muted mt" },
    "Remote Control lets claude.ai/code and the Claude app drive this sandbox. It needs a full claude.ai sign-in in the sandbox, so it exists for personal sandboxes only."));
  add(body, kv([
    ["Server", h("span", { class: "mono small" }, st.status)],
    ["Sign-in", st.signed_in ? pill("signed in to claude.ai", "ok") : pill("not signed in", "warn")],
    ["Permission mode", h("div", { style: "max-width:240px" }, mode)],
  ]));
  if (st.signed_in) {
    add(body, h("div", { class: "row" },
      btn(/^active/.test(st.status) ? "Restart the server" : "Start the server", () =>
        startJob(POST(`/api/sandboxes/${b.name}/remote-control`, { action: "enable", mode: mode.value }), { onDone: reload }), { class: "primary", icon: "play" }),
      btn("Stop the server", () => startJob(POST(`/api/sandboxes/${b.name}/remote-control`, { action: "off" }), { onDone: reload }), { icon: "power" }),
      h("a", { class: "btn", href: `#/sandboxes/${b.name}/logs`, onclick: () => { logState.which = "remote-control"; } }, "Its log")));
    return;
  }
  const step = h("div", { class: "stack" });
  add(body, step);
  add(step, h("div", {}, "Sign the sandbox in: one click in the browser, and one code to paste here."),
    h("div", { class: "row" }, btn("Start the sign-in", async (e) => {
      e.target.disabled = true;
      try {
        const r = await POST(`/api/sandboxes/${b.name}/remote-control`, { action: "login" });
        if (r.signed_in) { reload(); return; }
        const code = h("input", { type: "password", placeholder: "the code the page shows", autocomplete: "off" });
        add(clear(step), 
          h("div", {}, "1. Open the sign-in page, and click Authorize."),
          h("div", { class: "row" }, h("a", { class: "btn primary", href: r.url, target: "_blank", rel: "noopener noreferrer" }, icon("ext"), "Open the sign-in page")),
          copyLine(r.url),
          h("div", {}, "2. Copy the code that the page shows, and paste it here."),
          h("div", { class: "row" }, h("div", { style: "flex:1;max-width:360px" }, code),
            btn("Sign in and start the server", () => {
              if (!code.value.trim()) { toast("The code is empty", "", "warn"); return; }
              startJob(POST(`/api/sandboxes/${b.name}/remote-control`, { action: "code", code: code.value.trim(), mode: mode.value }), { onDone: reload });
            }, { class: "primary" })));
      } catch (err) { e.target.disabled = false; failToast(err); }
    }, { class: "primary" })));
}

function sbConfig(el, d) {
  const conf = d.config || {};
  add(el, card("Proxmox configuration", d.errors.config ? errorBox(new Error(d.errors.config)) :
    table(["Key", "Value"], Object.keys(conf).sort().map((k) => h("tr", {},
      h("td", { class: "mono small nowrap" }, k),
      h("td", { class: "mono small", style: "overflow-wrap:anywhere" }, k === "sshkeys" ? decodeURIComponent(String(conf[k])) : String(conf[k]))))),
  { flush: true }), h("div", { class: "small muted mt" }, "Read only. The token can change the size and the options; `sbx new` sets them."));
}

async function sbTasks(el, b) {
  const r = await GET(`/api/sandboxes/${b.name}/tasks`);
  add(el, card("Proxmox tasks", r.error ? banner("warn", "The token cannot read the task list: ", r.error) :
    table(["Started", "Type", "Status", "Took", "User", ""], (r.tasks || []).map((t) => h("tr", {},
      h("td", { class: "nowrap" }, when(t.starttime)), h("td", {}, t.type),
      h("td", {}, t.status ? pill(t.status, t.status === "OK" ? "ok" : "fail") : pill("running", "info", true)),
      h("td", {}, t.endtime ? duration(t.endtime - t.starttime) : "–"), h("td", { class: "small dim" }, t.user),
      h("td", { class: "right" }, btn("Log", async () => {
        try {
          const l = await GET(`/api/tasks/${enc(t.node)}/${enc(t.upid)}`);
          modal({ title: `${t.type} · ${when(t.starttime)}`, wide: true, body: h("pre", { class: "log" }, l.lines.join("\n")) });
        } catch (e) { failToast(e); }
      }, { class: "small" })))), { empty: "No task for this VM." }), { flush: true }));
}

// --- pages: new sandbox ----------------------------------------------------------------

async function pageNew(main, _, query) {
  const [ov, tp, pj] = await Promise.all([GET("/api/overview"), GET("/api/templates"), GET("/api/projects")]);
  const cfg = ov.config || {};
  const built = tp.templates.filter((t) => t.versions.length);
  const st = { profile: cfg.default_profile || "agent", inputs: [], decisions: {} };

  const name = h("input", { placeholder: "lab", autocomplete: "off", spellcheck: "false" });
  const nameHint = h("span", { class: "hint" }, "Lowercase letters, digits and single hyphens. The VM is sbx-<name>.");
  const profileSeg = h("div", { class: "seg" });
  const profileNote = h("div", { class: "small muted" });
  const template = h("select", {}, h("option", { value: "" }, `Default (${cfg.default_template || (built.length === 1 ? built[0].name : "the project's, then default_template")})`),
    built.map((t) => h("option", { value: t.name }, `${t.name}${t.description ? " — " + t.description : ""}`)));
  const project = h("select", {}, h("option", { value: "" }, "None: a plain sandbox"),
    pj.projects.map((p) => h("option", { value: p.name }, p.name)), h("option", { value: "__other" }, "Another checkout or git URL…"));
  const other = h("input", { placeholder: "~/code/app or git@github.com:me/app.git", hidden: true });
  const branch = h("input", { placeholder: "the checkout's branch" });
  const from = h("input", { placeholder: "a local checkout, for a git URL" });
  const inputsBox = h("div");
  const cores = h("input", { type: "number", min: 1, placeholder: String(cfg.cores || 8) });
  const memory = h("input", { type: "number", min: 256, step: 256, placeholder: String(cfg.memory_mb || 6144) });
  const disk = h("input", { type: "number", min: 1, placeholder: "the template's size" });
  const ttl = h("input", { type: "number", min: 0 });
  const gpu = h("input", { type: "checkbox" });
  const herdr = h("input", { type: "checkbox", checked: true });
  const claude = h("input", { type: "checkbox", checked: true });
  const rc = h("input", { type: "checkbox", checked: true });
  const rcMode = h("select", {}, ["", "acceptEdits", "default", "plan", "bypassPermissions"].map((m) => h("option", { value: m }, m || "the default of config.toml")));
  const rcRow = h("div", { class: "wide" });
  const preview = h("div");
  const problems = h("div");

  const drawProfile = () => {
    add(clear(profileSeg), ["agent", "personal"].map((p) => h("button", { type: "button", class: st.profile === p ? "on" : "",
      onclick: () => { st.profile = p; drawProfile(); drawInputs(); update(); } }, p === "agent" ? "Agent" : "Personal")));
    add(clear(profileNote), st.profile === "agent"
      ? "For an AI agent with full permissions: the internet only, no LAN, no tailnet. Each input needs a decision. A 'clean' snapshot after the recipe."
      : "For your own work: the internet and the LAN, your SSH agent for the clones, and no expiry.");
    ttl.placeholder = st.profile === "agent" ? `${cfg.agent_ttl_days ?? 3} (0 = never)` : "never (0)";
    add(clear(rcRow), st.profile === "personal" ? h("div", { class: "row" },
      h("label", { class: "check" }, rc, "Start a Claude Remote Control server"), h("div", { style: "width:260px" }, rcMode),
      h("span", { class: "hint" }, "The sign-in needs a code; do it on the sandbox's Remote Control tab after.")) : null);
  };

  const projectValue = () => project.value === "__other" ? other.value.trim() : project.value;

  const drawInputs = () => {
    clear(inputsBox);
    const items = st.inputs.filter((i) => i.kind !== "repo");
    const repos = st.inputs.filter((i) => i.kind === "repo");
    if (!projectValue()) return;
    if (project.value === "__other") {
      add(inputsBox, h("div", { class: "small muted" }, "For a checkout that the portal does not list, `sbx new` reads the inputs itself. An agent sandbox then needs --with or --without for each; add the project first to choose here."));
      return;
    }
    if (!st.inputs.length) { add(inputsBox, h("div", { class: "small muted" }, st.loaded ? "The recipe declares no inputs." : "Reading the project…")); return; }
    add(inputsBox, table(["Input", "Kind", "Found", st.profile === "agent" ? "Send it?" : "Goes in"],
      items.map((i) => {
        const allowed = i.agent_allowed;
        let control;
        if (st.profile === "agent") {
          if (!allowed) { st.decisions[i.name] = "without"; control = pill("never to an agent", "warn"); }
          else {
            const radio = (v, label) => h("label", { class: "check small" }, h("input", { type: "radio", name: `in-${i.name}`, value: v,
              checked: st.decisions[i.name] === v, onchange: () => { st.decisions[i.name] = v; update(); } }), label);
            control = h("div", { class: "row", style: "gap:14px" }, radio("with", "Send"), radio("without", i.placeholder ? "Withhold (placeholder)" : "Withhold"));
          }
        } else {
          control = i.action === "send" ? pill("sent", "info") : i.action === "placeholder" ? pill("placeholder") : pill("not sent");
        }
        return h("tr", {}, h("td", {}, h("b", {}, i.name), i.secret ? [" ", pill("secret", "warn")] : null,
          i.about ? h("div", { class: "small dim" }, i.about) : null),
          h("td", {}, i.kind), h("td", { class: "small" }, i.state, h("div", { class: "dim" }, i.source)), h("td", {}, control));
      })));
    if (repos.length) add(inputsBox, h("div", { class: "small muted mt" }, "Also cloned: " + repos.map((r) => r.url).join(", ")));
  };

  const loadProject = async () => {
    st.inputs = []; st.decisions = {}; st.loaded = false;
    other.hidden = project.value !== "__other";
    drawInputs(); update();
    if (!project.value || project.value === "__other") return;
    try {
      const p = await GET(`/api/projects/${enc(project.value)}`);
      st.inputs = p.inputs; st.loaded = true;
      if (p.manifest && p.manifest.template && !template.value) template.value = p.manifest.template;
    } catch (e) { failToast(e); }
    drawInputs(); update();
  };

  const body = () => {
    const b = { name: name.value.trim(), profile: st.profile, template: template.value, cores: cores.value, memory: memory.value,
      disk: disk.value, ttl: ttl.value, gpu: gpu.checked, no_herdr: !herdr.checked, no_claude: !claude.checked };
    const p = projectValue();
    if (p) {
      Object.assign(b, { project: p, branch: branch.value.trim(), from: from.value.trim() });
      b.with = Object.keys(st.decisions).filter((k) => st.decisions[k] === "with");
      b.without = Object.keys(st.decisions).filter((k) => st.decisions[k] === "without");
    }
    if (st.profile === "personal") { b.no_remote_control = !rc.checked; if (rc.checked && rcMode.value) b.remote_control_mode = rcMode.value; }
    return b;
  };

  const errorsOf = (b) => {
    const out = [];
    if (!b.name) out.push("a name");
    else if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(b.name.replace(/^sbx-/, "")) || b.name.includes("--")) out.push("a valid name");
    if (st.profile === "agent" && project.value && project.value !== "__other") {
      const open = st.inputs.filter((i) => i.kind !== "repo" && i.agent_allowed && !st.decisions[i.name]).map((i) => i.name);
      if (open.length) out.push(`a decision for ${open.join(", ")}`);
    }
    return out;
  };

  const submit = h("button", { class: "primary", type: "button" }, icon("plus"), "Make the sandbox");
  const update = () => {
    const b = body();
    const args = ["sbx", "new", b.name || "<name>"];
    if (b.profile !== cfg.default_profile) args.push("--profile", b.profile);
    if (b.template) args.push("--template", b.template);
    if (b.project) args.push("--project", b.project);
    if (b.branch) args.push("--branch", b.branch);
    if (b.from) args.push("--from", b.from);
    (b.with || []).forEach((x) => args.push("--with", x));
    (b.without || []).forEach((x) => args.push("--without", x));
    for (const [k, f] of [["cores", "--cores"], ["memory", "--memory"], ["disk", "--disk"], ["ttl", "--ttl"]]) if (b[k] !== "") args.push(f, b[k]);
    if (b.gpu) args.push("--gpu");
    if (b.no_herdr) args.push("--no-herdr");
    if (b.no_claude) args.push("--no-claude");
    if (b.no_remote_control) args.push("--no-remote-control");
    if (b.remote_control_mode) args.push("--remote-control-mode", b.remote_control_mode);
    add(clear(preview), copyLine(args.map((a) => /^[\w@%+=:,./<>~-]+$/.test(a) ? a : `'${a}'`).join(" ")));
    const errs = errorsOf(b);
    submit.disabled = errs.length > 0;
    add(clear(problems), errs.length ? h("div", { class: "small muted" }, "Still needed: " + errs.join("; ") + ".") : null);
    nameHint.textContent = b.name ? `The VM is sbx-${b.name.replace(/^sbx-/, "")}; its address is sbx-${b.name.replace(/^sbx-/, "")}.${cfg.domain || ""}` : "Lowercase letters, digits and single hyphens.";
  };

  submit.addEventListener("click", async () => {
    const b = body();
    const job = await startJob(POST("/api/sandboxes", b), {
      closeLabel: "Close (it continues)",
      onDone: (j) => { if (j.status === "ok") go(`#/sandboxes/${b.name.replace(/^sbx-/, "")}`); },
    });
    if (job) submit.disabled = true;
  });

  for (const el of [name, other, branch, from, cores, memory, disk, ttl]) el.addEventListener("input", update);
  for (const el of [template, gpu, herdr, claude, rc, rcMode]) el.addEventListener("change", update);
  project.addEventListener("change", loadProject);
  other.addEventListener("input", () => { drawInputs(); update(); });

  drawProfile();
  page(main, { title: "New sandbox", crumbs: [["Sandboxes", "#/sandboxes"], ["New"]],
    sub: "The portal runs `sbx new` with these options. Everything that can fail is checked before a VM is made." },
  h("div", { class: "grid", style: "grid-template-columns:minmax(0,1fr)" },
    card("The sandbox", h("div", { class: "form-grid" },
      h("label", { class: "field" }, h("span", {}, "Name"), name, nameHint),
      h("label", { class: "field" }, h("span", {}, "Template"), template, h("span", { class: "hint" }, built.length ? `${built.length} built` : "No template is built yet.")),
      h("div", { class: "field wide" }, h("span", { class: "small", style: "font-weight:550;color:var(--text-2)" }, "Profile"), h("div", { class: "row" }, profileSeg), profileNote))),
    card("The project", h("div", { class: "form-grid" },
      h("label", { class: "field" }, h("span", {}, "Project"), project, other,
        h("span", { class: "hint" }, "The sandbox clones the pushed state of the branch and runs the recipe.")),
      h("label", { class: "field" }, h("span", {}, "Branch"), branch),
      h("label", { class: "field wide" }, h("span", {}, "File inputs from (for a git URL)"), from),
      h("div", { class: "wide" }, inputsBox))),
    card("Size and options", h("div", { class: "form-grid" },
      h("label", { class: "field" }, h("span", {}, "Cores"), cores),
      h("label", { class: "field" }, h("span", {}, "Memory (MB)"), memory),
      h("label", { class: "field" }, h("span", {}, "Grow the disk to (GB)"), disk),
      h("label", { class: "field" }, h("span", {}, "Expires after (days)"), ttl),
      h("div", { class: "wide row", style: "gap:20px" },
        h("label", { class: "check" }, herdr, "Add to the herdr sidebar"),
        h("label", { class: "check" }, claude, "Sign Claude Code in with the Claude token"),
        cfg.gpu_mapping ? h("label", { class: "check" }, gpu, "Give it the host GPU") : null),
      rcRow)),
    card("The command", h("div", { class: "stack" }, preview, problems, h("div", { class: "row" }, submit)))));
  if (query.get("project")) { project.value = query.get("project"); loadProject(); }
  update();
  name.focus();
}

// --- pages: templates --------------------------------------------------------------------

async function pageTemplates(main) {
  const t = await GET("/api/templates");
  const stale = t.templates.filter((x) => x.state === "OUT OF DATE" || (x.source && !x.versions.length));
  page(main, {
    title: "Templates", sub: "One template per kind of project. A sandbox is a linked clone of the newest version.",
    actions: [
      btn("New definition", () => newTemplateDialog(t.templates), { icon: "plus" }),
      h("a", { class: "btn", href: "#/templates/import" }, icon("upload"), "Import"),
      h("a", { class: "btn", href: "#/templates/versions" }, "Versions"),
      menu([
        { label: "Rebuild each that is not current (Terminal)", icon: "terminal", run: () => inTerminal(["template", "rebuild", "--changed"]) },
        { label: "Remove the old versions (Terminal)", icon: "terminal", run: () => inTerminal(["template", "prune"]) },
      ]),
    ],
  },
  t.errors.pve ? banner("fail", h("b", {}, "Proxmox does not answer, so the built versions are unknown. "), t.errors.pve) : null,
  t.errors.definitions ? banner("fail", h("b", {}, "A definition does not load. "), t.errors.definitions) : null,
  stale.length ? banner("warn", `${stale.map((x) => x.name).join(", ")}: not built or out of date. A build needs the host's root password, so it runs in Terminal. `,
    btn("Rebuild them in Terminal", () => inTerminal(["template", "rebuild", "--changed"]), { class: "small", icon: "terminal" })) : null,
  card(null, table(["Template", "Definition", "State", "Newest version", "Sandboxes", "Description", ""],
    t.templates.map((x) => h("tr", {},
      h("td", {}, h("a", { href: `#/templates/${x.name}` }, h("b", {}, x.name)), x.default ? [" ", pill("default", "info")] : null),
      h("td", {}, x.source ? pill(x.source) : h("span", { class: "dim" }, "none")),
      h("td", {}, stateTpl(x.state)),
      h("td", { class: "small" }, x.versions.length ? `VM ${x.versions.at(-1).vmid}` + (x.versions.length > 1 ? ` (+${x.versions.length - 1} old)` : "") : "–"),
      h("td", {}, x.sandboxes.length ? String(x.sandboxes.length) : h("span", { class: "dim" }, "0")),
      h("td", { class: "small" }, x.description),
      h("td", { class: "right" }, x.source ? btn("Rebuild", () => inTerminal(["template", "rebuild", x.name]), { class: "small", icon: "terminal", title: "Opens in Terminal: the build asks for the host's root password" }) : null))),
    { empty: "No definition and no template." }), { flush: true, class: stale.length || t.errors.pve ? "mt" : "" }),
  card("Components", table(["Component", "Where", "What it installs", "Needs first", "Used by"],
    t.components.map((c) => h("tr", {},
      h("td", {}, h("button", { type: "button", class: "linkbtn", onclick: () => componentDialog(c.name) }, h("b", {}, c.name))),
      h("td", {}, pill(c.local ? "local" : "shared")), h("td", { class: "small" }, c.description),
      h("td", { class: "small" }, c.requires.join(", ") || "–"),
      h("td", { class: "small" }, t.templates.filter((x) => x.components.includes(c.name)).map((x) => x.name).join(", ") || "–")))),
  { flush: true, class: "mt" }));
}

function newTemplateDialog(templates) {
  const name = h("input", { placeholder: "myapp", autocomplete: "off" });
  const from = h("select", {}, h("option", { value: "" }, "Empty"), templates.filter((t) => t.source).map((t) => h("option", { value: t.name }, t.name)));
  modal({
    title: "New template definition",
    body: [h("label", { class: "field" }, h("span", {}, "Name"), name, h("span", { class: "hint" }, "Lowercase letters, digits and hyphens. A local definition of a shared name hides the shared one.")),
      h("label", { class: "field" }, h("span", {}, "Start from"), from),
      h("div", { class: "small muted" }, "The portal writes templates/local/<name>.toml. You edit it, then build it.")],
    foot: (close) => [btn("Cancel", close), btn("Write it", async () => {
      try { await POST("/api/templates", { name: name.value.trim(), from: from.value }); close(); go(`#/templates/${name.value.trim()}`); }
      catch (e) { failToast(e); }
    }, { class: "primary" })],
  });
}

async function componentDialog(name) {
  try {
    const c = await GET(`/api/components/${name}`);
    modal({
      title: `Component ${c.name}`, wide: true,
      body: [kv([["File", h("span", { class: "mono" }, c.path)], ["Where", pill(c.local ? "local" : "shared")],
        ["Installs", c.description], ["Needs first", c.requires.join(", ") || "–"], ["Used by", c.used_by.join(", ") || "–"]]),
      h("div", { class: "small muted" }, "A component runs as root in the template build."),
      h("pre", { class: "code plain" }, c.text)],
    });
  } catch (e) { failToast(e); }
}

async function pageTemplate(main, [name]) {
  const t = await GET(`/api/templates/${name}`);
  const def = t.definition, parsed = t.parsed;
  const editor = def && def.local ? h("textarea", { rows: Math.min(34, Math.max(12, def.text.split("\n").length + 2)), spellcheck: "false" }) : null;
  if (editor) editor.value = def.text;
  const saveRow = editor ? h("div", { class: "row" },
    btn("Save", async () => {
      try { const r = await PUT(`/api/templates/${name}`, { text: editor.value }); toast("Saved", `New fingerprint ${r.fingerprint}. Rebuild to use it.`, "ok"); render(); }
      catch (e) { failToast(e); }
    }, { class: "primary" }),
    btn("Revert", () => { editor.value = def.text; }),
    h("span", { class: "hint" }, "The portal saves only a definition that loads. A change takes effect at the next build.")) : null;

  const newest = (t.versions || []).at(-1);
  page(main, {
    title: `Template ${name}`, crumbs: [["Templates", "#/templates"], [name]],
    sub: h("span", { class: "row", style: "gap:8px" }, stateTpl(t.state), def ? pill(def.local ? "local" : "shared") : pill("no definition", "warn"),
      t.default ? pill("default_template", "info") : null, parsed ? h("span", { class: "small muted" }, parsed.description) : null),
    actions: [
      def ? btn("Rebuild in Terminal", () => inTerminal(["template", "rebuild", name]), { icon: "terminal", class: "primary" }) : null,
      def ? h("a", { class: "btn", href: `/api/templates/${name}/export`, download: `${name}.sbx-template.toml` }, icon("download"), "Export") : null,
      menu([
        def && !def.local && { label: "Make a local copy to edit", run: async () => {
          try { await POST("/api/templates", { name, from: name }); toast("Written", `templates/local/${name}.toml hides the shared one now.`, "ok"); render(); } catch (e) { failToast(e); }
        } },
        !def && { label: "Write a definition for it", run: async () => { try { await POST("/api/templates", { name }); render(); } catch (e) { failToast(e); } } },
        { label: "Remove the unused versions (Terminal)", icon: "terminal", run: () => inTerminal(["template", "rm", name]) },
        def && def.local && "-",
        def && def.local && { label: "Delete the local definition…", danger: true, icon: "trash", run: async () => {
          if (!(await confirmDialog({ title: `Delete templates/local/${name}.toml?`, danger: true, action: "Delete",
            text: t.shadows_shared ? "The shared definition of the same name applies again." : "The built versions stay on the host until you remove them." }))) return;
          try { await DELETE(`/api/templates/${name}`); toast("Deleted", `templates/local/${name}.toml`, "ok"); t.shadows_shared ? render() : go("#/templates"); } catch (e) { failToast(e); }
        } },
      ]),
    ],
  },
  t.errors.definition ? banner("fail", h("b", {}, "The definition does not load. "), t.errors.definition) : null,
  t.errors.pve ? banner("fail", h("b", {}, "Proxmox does not answer. "), t.errors.pve) : null,
  t.shadows_shared ? banner("info", "This local definition hides the shared definition of the same name.") : null,
  h("div", { class: "grid", style: "grid-template-columns:minmax(0,3fr) minmax(0,2fr);margin-top:14px" },
    h("div", {},
      def ? card(def.path, editor ? h("div", { class: "stack" }, editor, saveRow) : h("div", { class: "stack" },
        h("pre", { class: "code plain" }, def.text),
        h("div", { class: "small muted" }, "A shared definition is in git. Make a local copy to change it for this Mac."))) : card("Definition", h("div", { class: "empty" }, "This template is built, but this Mac has no definition of it.")),
      parsed ? card("Build settings", table(["Variable", "Value"], Object.entries(parsed.build_env).map(([k, v]) => h("tr", {},
        h("td", { class: "mono small" }, k), h("td", { class: "mono small" }, v || "–")))), { flush: true, class: "mt" }) : null),
    h("div", {},
      parsed ? card("What it has", kv([
        ["Components", h("span", { class: "row", style: "gap:4px" }, pill("core"), parsed.components.map((c) =>
          h("button", { type: "button", class: "linkbtn", onclick: () => componentDialog(c) }, pill(c, "info"))))],
        ["apt", parsed.apt.join(" ") || "–"],
        ["Build VM", `${parsed.cores} cores · ${parsed.memory_mb} MB · ${parsed.disk_gb} GB disk`],
        ["Image", parsed.image_url || "the default Ubuntu cloud image"],
        ["Fingerprint", h("span", { class: "mono small" }, parsed.fingerprint)],
        Object.keys(parsed.derived || {}).length ? ["From projects", h("span", { class: "small" },
          Object.entries(parsed.derived).map(([tb, keys]) => Object.entries(keys).map(([k, v]) => `[${tb}] ${k}: ${v.join(" ")}`)).flat().join("; "))] : null,
      ])) : null,
      card("Built versions", table(["Version", "VM", "Fingerprint", "Sandboxes"], (t.versions || []).slice().reverse().map((v) => h("tr", {},
        h("td", {}, v.vm_name, v === newest ? [" ", pill("newest", "ok")] : null), h("td", {}, v.vmid),
        h("td", {}, h("span", { class: "mono small" }, v.fingerprint), parsed && v.fingerprint === parsed.fingerprint ? [" ", pill("current", "ok")] : null),
        h("td", { class: "small" }, v.sandboxes.length ? v.sandboxes.map((n, i) => [i ? ", " : "", h("a", { href: `#/sandboxes/${n}` }, n)]) : "–"))),
      { empty: "Not built. Rebuild it in Terminal: the build asks for the host's root password." }), { flush: true, class: "mt" }),
      card("Build it", terminalBlock(["template", "rebuild", name], "A build takes 15 to 40 minutes and asks for the host's root password once. Sandboxes keep the version they have."), { class: "mt" }))));
}

async function pageImport(main) {
  const text = h("textarea", { rows: 12, placeholder: "Paste an exported template file here, or choose a file." });
  const file = h("input", { type: "file", accept: ".toml,text/plain" });
  const asName = h("input", { placeholder: "keep the exported name" });
  const force = h("input", { type: "checkbox" });
  const review = h("div");
  file.addEventListener("change", () => {
    const f = file.files[0];
    if (!f) return;
    const r = new FileReader();
    r.onload = () => { text.value = r.result; };
    r.readAsText(f);
  });
  const doReview = async () => {
    add(clear(review), loading());
    let p;
    try { p = await POST("/api/templates/import", { text: text.value, as: asName.value.trim(), force: force.checked }); }
    catch (e) { add(clear(review), errorBox(e)); return; }
    const agree = h("input", { type: "checkbox" });
    const go_ = h("button", { class: "primary", type: "button", disabled: true }, "Write these files");
    agree.addEventListener("change", () => { go_.disabled = !agree.checked || p.problems.length > 0; });
    go_.addEventListener("click", async () => {
      try {
        const r = await POST("/api/templates/import", { text: text.value, as: asName.value.trim(), force: force.checked, apply: true, digest: p.digest });
        add(clear(review), card("Imported", h("div", { class: "stack" },
          h("div", {}, `Wrote ${r.written.join(", ")}.`),
          h("div", { class: "row" }, h("a", { class: "btn", href: `#/templates/${r.name}` }, `Open template ${r.name}`)),
          terminalBlock(["template", "rebuild", r.name], "Build it: it asks for the host's root password."))));
      } catch (e) { failToast(e); }
    });
    add(clear(review), card(`Review: template ${p.name}` + (p.name !== p.exported_as ? ` (exported as ${p.exported_as})` : ""), h("div", { class: "stack" },
      p.problems.map((x) => banner("fail", h("b", {}, "Problem. "), x)),
      p.warnings.map((x) => banner("warn", x)),
      p.same.map((x) => h("div", { class: "small muted" }, `Unchanged: ${x}: this Mac has the same already.`)),
      p.writes.length ? p.writes.map((w) => h("div", { class: "file-block" },
        h("div", { class: "fb-head" }, w.path, w.component ? pill("runs as root in the build", "warn") : null),
        h("pre", { class: "code plain" }, w.text))) : h("div", {}, "Nothing to import: this Mac has the same template already."),
      p.writes.length && !p.problems.length ? [
        p.writes.some((w) => w.component) ? banner("warn", h("b", {}, "Read each script. "),
          "A component runs as root in the template build, and its result is in every sandbox of the template.") : null,
        h("label", { class: "check" }, agree, "I read each file above, and I want these files written."),
        h("div", { class: "row" }, go_)] : null)));
  };
  page(main, { title: "Import a template", crumbs: [["Templates", "#/templates"], ["Import"]],
    sub: "A template file holds a definition and the scripts of its own components. It goes to templates/local/ and template/components/local/." },
  card("The file", h("div", { class: "stack" }, text,
    h("div", { class: "form-grid" }, h("label", { class: "field" }, h("span", {}, "Or choose a file"), file),
      h("label", { class: "field" }, h("span", {}, "Import it as"), asName),
      h("label", { class: "check wide" }, force, "Replace a definition or a component of the same name")),
    h("div", { class: "row" }, btn("Review the files", doReview, { class: "primary" })))),
  h("div", { class: "mt" }, review));
}

async function pageVersions(main) {
  page(main, { title: "Versions", crumbs: [["Templates", "#/templates"], ["Versions"]],
    sub: "The Ruby, Node and Go versions and the Docker images that the projects need, by template. A build caches them." }, loading());
  const r = await GET("/api/versions");
  page(main, { title: "Versions", crumbs: [["Templates", "#/templates"], ["Versions"]],
    sub: "The Ruby, Node and Go versions and the Docker images that the projects need, by template. A build caches them.",
    actions: [btn("Save them for the next build", () => startJob(POST("/api/versions")), { class: "primary" })] },
  card(null, h("pre", { class: "code plain", style: "max-height:none" }, r.text)));
}

// --- pages: projects ------------------------------------------------------------------------

async function pageProjects(main) {
  const r = await GET("/api/projects");
  page(main, { title: "Projects", sub: "The projects that this Mac has used. A project carries its recipe in .sandbox/.",
    actions: [btn("Add a project", addProjectDialog, { icon: "plus", class: "primary" })] },
  r.error ? banner("warn", "Proxmox does not answer, so the sandboxes of each project are unknown: ", r.error) : null,
  card(null, table(["Project", "Checkout", "Origin", "Recipe", "Git token", "Sandboxes"], r.projects.map((p) => h("tr", {},
    h("td", {}, h("a", { href: `#/projects/${enc(p.name)}` }, h("b", {}, p.name))),
    h("td", { class: "mono small" }, p.checkout || "–", p.checkout && !p.checkout_exists ? [" ", pill("gone", "fail")] : null),
    h("td", { class: "mono small" }, p.url || "–"),
    h("td", {}, p.manifest ? pill("sandbox.toml", "ok") : h("span", { class: "dim" }, "none")),
    h("td", {}, p.token ? pill("in the keychain", "ok") : h("span", { class: "dim" }, "none")),
    h("td", { class: "small" }, p.sandboxes.length ? p.sandboxes.map((n, i) => [i ? ", " : "", h("a", { href: `#/sandboxes/${n}` }, n)]) : "–"))),
  { empty: "No project yet. Add a checkout, or make a sandbox with a project." }), { flush: true, class: r.error ? "mt" : "" }));
}

function addProjectDialog() {
  const path = h("input", { placeholder: "~/code/app", autocomplete: "off" });
  modal({
    title: "Add a project",
    body: [h("label", { class: "field" }, h("span", {}, "Checkout or git URL"), path,
      h("span", { class: "hint" }, "The portal runs `sbx project add`. After that, the project works by name."))],
    foot: (close) => [btn("Cancel", close), btn("Add", () => { close(); startJob(POST("/api/projects", { path: path.value.trim() }), { onDone: () => render() }); }, { class: "primary" })],
  });
}

async function pageProject(main, [name]) {
  const p = await GET(`/api/projects/${enc(name)}`);
  const token = h("input", { type: "password", autocomplete: "off", placeholder: "github_pat_…" });
  const host = h("input", { placeholder: "github.com" });
  const user = h("input", { placeholder: "x-access-token" });
  const noCheck = h("input", { type: "checkbox" });
  // A sandbox that runs already has no git credential until the token is pushed.
  const running = p.sandboxes.filter((b) => b.status === "running");
  const pushPicks = running.map((b) => ({ b, el: h("input", { type: "checkbox" }) }));
  const pushed = () => pushPicks.filter((x) => x.el.checked).map((x) => x.b.name);
  page(main, {
    title: name, crumbs: [["Projects", "#/projects"], [name]],
    sub: p.url || p.checkout,
    actions: [h("a", { class: "btn primary", href: `#/new?project=${enc(name)}` }, icon("plus"), "New sandbox"),
      menu([{ label: "Forget the project…", danger: true, icon: "trash", run: async () => {
        if (!(await confirmDialog({ title: `Forget ${name}?`, action: "Forget", danger: true,
          text: "The portal removes it from the list. The checkout, the bindings file and the keychain token stay." }))) return;
        try { await DELETE(`/api/projects/${enc(name)}`); go("#/projects"); } catch (e) { failToast(e); }
      } }])],
  },
  Object.entries(p.errors).filter(([k]) => k !== "pve").map(([k, v]) => banner("fail", h("b", {}, `${k}: `), v)),
  h("div", { class: "grid cols-2", style: "margin-top:14px" },
    card("The project", kv([
      ["Checkout", p.checkout ? [h("span", { class: "mono small" }, p.checkout), p.checkout_exists ? null : [" ", pill("gone", "fail")]] : "–"],
      ["Origin", p.url ? h("span", { class: "mono small" }, p.url) : "–"],
      ["Branch", p.branch || "–"],
      ["Not pushed", p.unpushed && p.unpushed !== "0" ? pill(`${p.unpushed} commit(s): a sandbox clones the pushed state`, "warn") : "–"],
      ["Recipe", p.manifest ? h("span", { class: "mono small" }, p.manifest.setup) : h("span", { class: "dim" }, "no .sandbox/sandbox.toml")],
      ["Env file", p.manifest ? h("span", { class: "mono small" }, p.manifest.env_file) : "–"],
      ["Template", p.manifest && p.manifest.template ? h("a", { href: `#/templates/${p.manifest.template}` }, p.manifest.template) : "the default"],
      ["herdr panes", p.layout ? pill(".sandbox/herdr.toml", "ok") : "–"],
    ])),
    card("Git access for an agent sandbox", h("div", { class: "stack" },
      kv([["Token", p.git.token ? pill("in the keychain", "ok") : pill("none", "warn")],
        ["Source", p.git.source || "none: public repositories only"],
        ["Must cover", h("div", {}, p.git.repos.map((r) => h("div", { class: "mono small" }, r)))]]),
      h("hr"),
      h("div", { class: "small muted" }, "A fine-grained token with Contents: Read (or Read and write, to push) on each repository above. The portal sends it to `sbx git-token` over stdin; it goes into the keychain and nowhere else."),
      h("div", { class: "form-grid" },
        h("label", { class: "field wide" }, h("span", {}, "New token"), token),
        h("label", { class: "field" }, h("span", {}, "Git host"), host),
        h("label", { class: "field" }, h("span", {}, "User name"), user),
        h("label", { class: "check wide" }, noCheck, "Store it without a check against the git host"),
        pushPicks.length ? h("div", { class: "wide stack" }, h("span", { class: "small muted" }, "Also install it into these running sandboxes:"),
          h("div", { class: "row", style: "gap:16px" }, pushPicks.map(({ b, el }) => h("label", { class: "check small" }, el, b.name)))) : null),
      h("div", { class: "row" },
        btn("Check and store", () => {
          if (!token.value.trim()) { toast("The token is empty", "", "warn"); return; }
          const body = { token: token.value, host: host.value.trim(), username: user.value.trim(), no_check: noCheck.checked, push: pushed() };
          token.value = "";
          startJob(POST(`/api/projects/${enc(name)}/git-token`, body), { onDone: () => render() });
        }, { class: "primary" }),
        p.git.token && running.length ? btn("Send the stored token", () => {
          if (!pushed().length) { toast("Tick a sandbox first", "The token goes to the running sandboxes that you tick above.", "warn"); return; }
          startJob(POST(`/api/projects/${enc(name)}/git-token/push`, { sandboxes: pushed() }));
        }, { title: "sbx git-token --push: the token in the keychain, into the ticked sandboxes" }) : null,
        p.git.token ? btn("Remove the token", async () => {
          if (!(await confirmDialog({ title: "Remove the git token?", danger: true, action: "Remove", text: "An agent sandbox of this project can then clone public repositories only." }))) return;
          startJob(DELETE(`/api/projects/${enc(name)}/git-token`), { onDone: () => render() });
        }, { class: "danger" }) : null)))),
  card("Inputs", p.manifest ? table(["Input", "Kind", "Required", "Secret", "Agent", "Where from", "State", "About"], p.inputs.map((i) => h("tr", {},
    h("td", {}, h("b", {}, i.name)), h("td", {}, i.kind), h("td", {}, i.required ? "yes" : "no"),
    h("td", {}, i.kind === "repo" ? "–" : i.secret ? pill("secret", "warn") : "no"),
    h("td", {}, i.agent_allowed ? "allowed" : pill("never", "warn")),
    h("td", { class: "small" }, i.source), h("td", { class: "small" }, /MISSING|REFUSED/.test(i.state) ? pill(i.state, "fail") : i.state),
    h("td", { class: "small" }, i.about || "–"))), { empty: "The recipe declares no inputs." }) : h("div", { class: "empty" }, "No manifest in the checkout."),
  { flush: true, class: "mt" }),
  card("Bindings", h("div", { class: "stack" }, kv([["File", h("span", { class: "mono small" }, p.bindings.path + (p.bindings.exists ? "" : " (none)"))]]),
    p.bindings.inputs.length ? table(["Input", "From", "Detail"], p.bindings.inputs.map((x) => h("tr", {},
      h("td", {}, x.name), h("td", {}, x.kind), h("td", { class: "mono small" }, x.kind === "value" ? "a literal value (not shown)" : x.detail)))) : null,
    h("div", { class: "small muted" }, "A binding says where an input's value comes from on this Mac. Edit the file by hand; docs/projects.md explains it.")), { class: "mt" }),
  p.manifest_text ? card(".sandbox/sandbox.toml", h("pre", { class: "code plain" }, p.manifest_text), { class: "mt" }) : null,
  card("Sandboxes", table(["Name", "Status", "Profile", "Expires"], p.sandboxes.map((b) => h("tr", {},
    h("td", {}, h("a", { href: `#/sandboxes/${b.name}` }, b.name)), h("td", {}, statusPill(b.status)), h("td", {}, profilePill(b.profile)),
    h("td", {}, expiryText(b)))), { empty: "No sandbox of this project." }), { flush: true, class: "mt" }));
}

// --- pages: tokens -------------------------------------------------------------------------

async function pageTokens(main) {
  const [c, pj, sb] = await Promise.all([GET("/api/claude-token"), GET("/api/projects"), GET("/api/sandboxes").catch(() => ({ sandboxes: [] }))]);
  const tok = h("input", { type: "password", autocomplete: "off", placeholder: "sk-ant-oat01-…" });
  page(main, { title: "Tokens", sub: "Every token lives in the macOS keychain. The portal shows whether one is there, never its value." },
  c.warning ? banner("warn", c.warning) : null,
  h("div", { class: "grid cols-2", style: c.warning ? "margin-top:14px" : "" },
    card("Claude Code", h("div", { class: "stack" },
      kv([["Token", c.stored ? pill("stored", "ok") : pill("none", "warn")], ["Expires", c.expires || "–"],
        ["Keychain item", h("span", { class: "mono small" }, "sbx-claude-token")]]),
      h("div", { class: "small muted" }, "One token from your Claude subscription signs Claude Code in, in each sandbox. `claude setup-token` makes it; it lasts one year."),
      h("div", { class: "row" },
        btn("Make one in Terminal", () => inTerminal(["claude-token"]), { icon: "terminal", class: "primary", title: "Runs claude setup-token; it opens the browser" }),
        c.stored ? btn("Send it to the sandboxes", () => pushDialog(sb.sandboxes), {}) : null,
        c.stored ? btn("Remove", async () => {
          if (!(await confirmDialog({ title: "Remove the Claude token?", danger: true, action: "Remove",
            text: "The token leaves the keychain and each running sandbox. Claude Code in the sandboxes asks for a sign-in after this." }))) return;
          startJob(DELETE("/api/claude-token"), { onDone: () => render() });
        }, { class: "danger" }) : null),
      h("hr"),
      h("label", { class: "field" }, h("span", {}, "Or paste a token that `claude setup-token` printed"), tok),
      h("div", { class: "row" }, btn("Store it and send it", () => {
        if (!tok.value.trim()) { toast("The token is empty", "", "warn"); return; }
        const v = tok.value; tok.value = "";
        startJob(POST("/api/claude-token", { token: v }), { onDone: () => render() });
      })))),
    card("Proxmox API", h("div", { class: "stack" },
      kv([["Keychain item", h("span", { class: "mono small" }, "sbx-pve-token")], ["Scope", "the sandbox pool, the templates, the two sandbox bridges"]]),
      h("div", { class: "small muted" }, "`sbx setup` makes this token, one per Mac. The checks prove that its rights are no wider than they must be."),
      h("div", { class: "row" }, h("a", { class: "btn", href: "#/checks" }, icon("check"), "Check the token scope"))))),
  card("Git tokens, one per project", table(["Project", "Token", "Keychain item", ""], pj.projects.map((p) => h("tr", {},
    h("td", {}, h("a", { href: `#/projects/${enc(p.name)}` }, p.name)),
    h("td", {}, p.token ? pill("stored", "ok") : h("span", { class: "dim" }, "none")),
    h("td", { class: "mono small" }, `sbx-git-${p.name}`),
    h("td", { class: "right" }, h("a", { class: "btn small", href: `#/projects/${enc(p.name)}` }, p.token ? "Change" : "Add")))),
  { empty: "No project yet." }), { flush: true, class: "mt" }));
}

function pushDialog(boxes) {
  const running = boxes.filter((b) => b.status === "running");
  const picks = running.map((b) => ({ b, el: h("input", { type: "checkbox" }) }));
  modal({
    title: "Send the Claude token",
    body: [h("div", { class: "small muted" }, "The token goes to each running sandbox that has one already. Tick a sandbox to give it the token for the first time."),
      picks.length ? h("div", { class: "stack" }, picks.map(({ b, el }) => h("label", { class: "check" }, el, b.name, " ", profilePill(b.profile)))) : h("div", { class: "empty" }, "No sandbox runs.")],
    foot: (close) => [btn("Cancel", close), btn("Send", () => {
      close();
      startJob(POST("/api/claude-token/push", { sandboxes: picks.filter((x) => x.el.checked).map((x) => x.b.name) }));
    }, { class: "primary" })],
  });
}

// --- pages: checks --------------------------------------------------------------------------

async function pageChecks(main) {
  const out = h("div");
  const run = async () => {
    add(clear(out), h("div", { class: "loading" }, h("span", { class: "spin" }), " Checking the settings, the API, DNS and the tailnet path…"));
    try {
      const r = await GET("/api/checks");
      const all = r.groups.flatMap((g) => g.checks);
      const f = all.filter((c) => c.status === "FAIL").length, w = all.filter((c) => c.status === "WARN").length;
      add(clear(out), 
        banner(f ? "fail" : w ? "warn" : "ok", h("b", {}, `${f} failed, ${w} warnings, ${all.length - f - w} ok. `),
          f ? "Each failed line names the next step." : w ? "A warning is a limit, not a stop." : "The setup works.",
          h("span", { class: "muted small" }, ` (${r.seconds ?? "?"} s)`)),
        r.groups.map((g) => card(g.title, h("div", { class: "checklist" }, g.checks.map((c) => h("div", { class: "item" },
          checkPill(c.status), h("div", { class: "name" }, c.name), h("div", { class: "detail" }, c.detail)))), { flush: true, class: "mt" })));
    } catch (e) { add(clear(out), errorBox(e)); }
  };
  page(main, { title: "Checks", sub: "The checks of `sbx doctor`: this Mac, the Proxmox API and its token, DNS, and the path over the tailnet.",
    actions: [btn("Run again", run, { icon: "refresh", class: "primary" })] },
  out,
  h("div", { class: "grid cols-2 mt" },
    card("Prove the isolation", h("div", { class: "stack" },
      h("div", { class: "small" }, "It makes one sandbox in each profile, proves that the agent sandbox cannot reach the LAN or this Mac, and removes both. About two minutes."),
      h("div", { class: "row" }, btn("Run the isolation test", async () => {
        if (!(await confirmDialog({ title: "Run the isolation test?", action: "Make two probe sandboxes",
          text: "It makes the sandboxes doctor-a and doctor-p, probes from them, and destroys them after." }))) return;
        startJob(POST("/api/checks/isolation"));
      })))),
    card("Setup", h("div", { class: "stack" },
      h("div", { class: "small" }, "The setup asks for the host's root password and stops at steps in the Tailscale admin console, so it runs in Terminal. It is safe to run again."),
      h("div", { class: "row" }, btn("sbx setup", () => inTerminal(["setup"]), { icon: "terminal" }),
        btn("sbx setup --mac-only", () => inTerminal(["setup", "--mac-only"]), { icon: "terminal" })),
      h("div", { class: "small muted" }, "--mac-only sets up this Mac for a host that is set up already, and does not change the host.")))));
  await run();
}

// --- pages: activity ------------------------------------------------------------------------

const actFilter = { status: "all" };

async function pageActivity(main) {
  const body = h("div");
  const seg = h("div", { class: "seg" });
  let jobs = [];
  const draw = () => {
    add(clear(seg), [["all", "All"], ["running", "Running"], ["failed", "Failed"]].map(([v, l]) =>
      h("button", { type: "button", class: actFilter.status === v ? "on" : "", onclick: () => { actFilter.status = v; draw(); } }, l)));
    const rows = jobs.filter((j) => actFilter.status === "all" || j.status === actFilter.status);
    add(clear(body), card(null, table(["Status", "What", "Command", "Started", "#Took"], rows.map((j) => h("tr", { style: "cursor:pointer", onclick: () => go(`#/activity/${j.id}`) },
      h("td", {}, jobPill(j.status)), h("td", {}, h("a", { href: `#/activity/${j.id}` }, j.title)),
      h("td", { class: "mono small", style: "max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, j.command),
      h("td", { class: "nowrap", title: when(j.started) }, ago(j.started)),
      h("td", { class: "num" }, duration((j.ended || Date.now() / 1000) - j.started)))),
    { empty: "Nothing has run from the portal yet." }), { flush: true }));
  };
  const load = async () => { try { jobs = (await GET("/api/jobs")).jobs; draw(); } catch (e) { add(clear(body), errorBox(e)); } };
  page(main, { title: "Activity", sub: "Every change that the portal made, with the full output. The last 300 are kept in ~/.config/sbx/portal/jobs/." },
    h("div", { class: "row", style: "margin-bottom:12px" }, seg), body);
  currentRefresh = load;
  await load();
  every(3000, load);
}

async function pageJob(main, [id]) {
  const j = await GET(`/api/jobs/${id}?since=999999999`);
  const pre = h("pre", { class: "log", style: "max-height:72vh" });
  const status = h("span", {}, jobPill(j.status));
  const took = h("span", { class: "small muted" });
  const cancel = btn("Cancel", async () => {
    if (!(await confirmDialog({ title: "Cancel the job?", danger: true, action: "Cancel the job", text: "The command stops at once." }))) return;
    try { await POST(`/api/jobs/${id}/cancel`); } catch (e) { failToast(e); }
  }, { class: "danger" });
  page(main, { title: j.title, crumbs: [["Activity", "#/activity"], [when(j.started)]],
    sub: h("span", { class: "row", style: "gap:10px" }, status, took, j.target ? h("span", { class: "tag" }, j.target) : null), actions: [cancel] },
  j.command.startsWith("sbx ") ? copyLine(j.command) : h("div", { class: "mono small muted" }, j.command),
  h("div", { class: "mt" }, pre));
  const stop = followJob(id, pre, (x) => {
    add(clear(status), jobPill(x.status));
    took.textContent = `${when(x.started)} · ${duration((x.ended || Date.now() / 1000) - x.started)}` + (x.code !== null && x.code !== undefined ? ` · exit ${x.code}` : "");
    cancel.hidden = x.status !== "running";
    if (!x.line_count && x.status !== "running") { add(clear(pre), "(no output)"); }
  });
  timers.push(stop);
}

// --- pages: settings ------------------------------------------------------------------------

const SET_TABS = [["", "Mac settings"], ["host", "Host settings"], ["file", "config.toml"], ["guide", "The guide"]];

async function pageSettings(main, [tab]) {
  tab = tab || "";
  const s = await GET("/api/settings");
  const body = h("div");
  page(main, { title: "Settings", sub: `Three layers, lowest first: host/defaults.conf, host/local.conf, then ${s.files["config.toml"].path}.` },
    s.error ? banner("fail", h("b", {}, "The settings do not load. "), s.error) : null,
    h("div", { class: "tabs", style: s.error ? "margin-top:14px" : "" }, SET_TABS.map(([id, label]) =>
      h("a", { href: `#/settings${id ? "/" + id : ""}`, class: id === tab ? "on" : "" }, label))),
    body);
  if (tab === "host") return settingsHost(body, s);
  if (tab === "file") return settingsFile(body, s);
  if (tab === "guide") {
    const g = await GET("/api/guide").catch((e) => ({ text: e.message }));
    add(body, card(null, h("pre", { class: "code plain", style: "max-height:none" }, g.text)));
    return;
  }
  settingsMac(body, s);
}

function settingsMac(el, s) {
  const changed = {};
  const saveBtn = h("button", { class: "primary", type: "button", disabled: true }, "Save the changes");
  const note = h("span", { class: "hint" });
  const mark = () => { const n = Object.keys(changed).length; saveBtn.disabled = !n; note.textContent = n ? `${n} change(s) not saved` : ""; };
  const editor = (row) => {
    const shown = row.value;
    if (!row.editable) return h("span", { class: "mono small" }, JSON.stringify(shown));
    let input;
    if (row.key === "default_profile") {
      input = h("select", {}, ["agent", "personal"].map((v) => h("option", { value: v, selected: v === shown }, v)));
    } else if (row.key === "default_template") {
      input = h("input", { value: shown ?? "", placeholder: "the only built template" });
    } else if (row.type === "int") {
      input = h("input", { type: "number", value: shown ?? "" });
    } else if (row.type === "list") {
      input = h("input", { class: "mono", value: JSON.stringify(shown ?? []), spellcheck: "false" });
    } else {
      input = h("input", { value: shown ?? "", spellcheck: "false" });
    }
    input.addEventListener("input", () => {
      let v = input.value;
      if (row.type === "int") v = v === "" ? null : Number(v);
      if (row.type === "list") { try { v = JSON.parse(v || "[]"); input.style.borderColor = ""; } catch (e) { input.style.borderColor = "var(--fail)"; return; } }
      if (JSON.stringify(v) === JSON.stringify(shown)) delete changed[row.key]; else changed[row.key] = v;
      mark();
    });
    input.addEventListener("change", () => input.dispatchEvent(new Event("input")));
    return input;
  };
  const origin = (o) => pill(o, { "config.toml": "info", "local.conf": "ok" }[o] || "");
  saveBtn.addEventListener("click", async () => {
    try { await PUT("/api/settings", { values: changed }); toast("Saved", "config.toml is written and loads.", "ok"); render(); }
    catch (e) { failToast(e); }
  });
  const mac = s.mac.filter((r) => r.editable), shared = s.mac.filter((r) => !r.editable);
  add(el, card("This Mac (config.toml)", table(["Setting", "Value", "From", ""], mac.map((r) => h("tr", {},
    h("td", { style: "width:38%" }, h("div", { class: "mono" }, r.key), h("div", { class: "small dim" }, mdInline(r.description))),
    h("td", {}, editor(r), r.default_text ? h("div", { class: "hint" }, "Default: ", mdInline(r.default_text)) : null),
    h("td", {}, origin(r.origin)),
    h("td", { class: "right" }, r.origin === "config.toml" ? btn("Reset", async () => {
      if (!(await confirmDialog({ title: `Reset ${r.key}?`, text: "The line leaves config.toml, and the default applies.", action: "Reset" }))) return;
      try { await PUT("/api/settings", { values: { [r.key]: null } }); toast("Reset", r.key, "ok"); render(); } catch (e) { failToast(e); }
    }, { class: "small ghost", icon: "undo" }) : null)))), {
    flush: true, actions: [note, saveBtn],
  }),
  h("div", { class: "small muted mt" }, "A command setting (a list, such as pve_token_command) names a command that prints a secret. The portal runs it only where the CLI does, and never shows what it prints."),
  card("Shared with the host (host/local.conf)", table(["Setting", "Value", "From"], shared.map((r) => h("tr", {},
    h("td", { style: "width:38%" }, h("div", { class: "mono" }, r.key), h("div", { class: "small dim" }, mdInline(r.description))),
    h("td", { class: "mono small" }, JSON.stringify(r.value)),
    h("td", {}, origin(r.origin))))), { flush: true, class: "mt",
    actions: h("span", { class: "hint" }, "The host scripts read these too. `sbx setup` changes them.") }));
}

function settingsHost(el, s) {
  add(el, card("Host settings", table(["Key", "Value", "Default", "Mac key", "Meaning"], s.host.map((r) => h("tr", {},
    h("td", { class: "mono small" }, r.key),
    h("td", { class: "mono small" }, r.local !== null && r.local !== undefined ? [r.local, " ", pill("local.conf", "ok")] : r.default),
    h("td", { class: "mono small dim" }, r.default || "–"),
    h("td", { class: "mono small" }, r.mac_key || "–"),
    h("td", { class: "small" }, mdInline(r.description))))), { flush: true }),
  h("div", { class: "grid cols-2 mt" },
    card("Change them", terminalBlock(["setup"], "host/local.conf holds the values of this setup, and the host has a copy. `sbx setup` proposes them and changes the host; run it again to change one.")),
    card(s.files["local.conf"].path, s.files["local.conf"].exists ? h("pre", { class: "code plain" }, s.files["local.conf"].text) : h("div", { class: "empty" }, "No local.conf: the shared defaults apply."))));
}

function settingsFile(el, s) {
  const f = s.files["config.toml"];
  const ta = h("textarea", { rows: 26, spellcheck: "false" });
  ta.value = f.text;
  add(el, card(f.path, h("div", { class: "stack" }, ta, h("div", { class: "row" },
    btn("Save", async () => {
      try { await PUT("/api/settings/raw", { text: ta.value }); toast("Saved", "config.toml is written and loads.", "ok"); render(); }
      catch (e) { failToast(e); }
    }, { class: "primary" }),
    btn("Revert", () => { ta.value = f.text; }),
    h("span", { class: "hint" }, "The portal saves the file only when the whole setup loads with it.")))),
  card(s.files["defaults.conf"].path, h("pre", { class: "code plain" }, s.files["defaults.conf"].text), { class: "mt" }));
}

// --- pages: docs --------------------------------------------------------------------------

async function pageDocs(main, [name]) {
  name = name || "README";
  const [list, d] = await Promise.all([GET("/api/docs"), GET(`/api/docs/${name}`)]);
  page(main, { title: "Docs" },
    h("div", { class: "doc-layout" },
      h("nav", { class: "doc-list" }, list.docs.map((x) => h("a", { href: `#/docs/${x.name}`, class: x.name === name ? "on" : "" }, x.title))),
      card(null, markdown(d.text))));
  main.scrollTop = 0;
  window.scrollTo(0, 0);
}

// --- start ------------------------------------------------------------------------------

function start() {
  applyTheme();
  // One listener closes every open menu; a menu stops its own clicks.
  document.addEventListener("click", () => document.querySelectorAll(".menu-list").forEach((l) => { l.hidden = true; }));
  // The first visit comes with ?t=<token>; the cookie holds it now.
  if (location.search.includes("t=")) history.replaceState(null, "", "/" + location.hash);
  buildSide();
  window.addEventListener("hashchange", render);
  render();
  sideHost();
  pollJobs();
  setInterval(() => { if (!document.hidden) pollJobs(); }, 3000);
  setInterval(() => { if (!document.hidden) sideHost(); }, 30000);
}

start();
