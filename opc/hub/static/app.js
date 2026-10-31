/* OPC Hub — vanilla ES2022, no build step. */
"use strict";

/* ------------------------------------------------------------------ */
/* 基础工具 / 状态                                                     */
/* ------------------------------------------------------------------ */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const fmtDate = (s) => {
  if (!s) return "";
  const d = new Date(s);
  if (Number.isNaN(+d)) return String(s).slice(0, 10);
  const now = new Date(), diff = (now - d) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
};
const fmtNum = (n) => (n >= 10000 ? (n / 10000).toFixed(1) + "万" : String(n ?? 0));
const icon = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const TOKEN_KEY = "opc_token";
const SID_KEY = "opc_sid";
const TRACK_KEY = "opc_track";
const DRAFT_KEY = "opc_draft";
const OUTBOX_KEY = "opc_outbox_v1";
const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
(function adoptToken() {
  const u = new URL(location.href).searchParams.get("token");
  if (u) {
    localStorage.setItem(TOKEN_KEY, u);
    history.replaceState({}, "", location.pathname + location.hash);
  }
})();
if (!localStorage.getItem(SID_KEY)) {
  localStorage.setItem(SID_KEY, (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).replace(/-/g, "").slice(0, 16));
}
const SID = localStorage.getItem(SID_KEY);

const S = {
  build: "",
  pulse: null,
  attention: null,
  intent: "auto",
  convId: null,
  epoch: 0,
  ask: null,
  tasks: new Map(),
  lastCites: [],
  lastTrace: null,
  graph: null,
  graphData: null,
  graphMode: localStorage.getItem("opc_graph_mode") || "lite",
  graphEdge: localStorage.getItem("opc_graph_edge") || "all",
  paletteMode: "cmd",
  paletteItems: [],
  paletteSel: 0,
  searchSeq: 0,
  tools: null,
  telemetrySummary: null,
  offline: !navigator.onLine,
};

const thread = $("#thread");
const input = $("#input");
const sendBtn = $("#send");
const composer = $("#composer");

/* ------------------------------------------------------------------ */
/* 操作观察（匿名事件，不含正文）                                       */
/* ------------------------------------------------------------------ */
const trackOn = () => localStorage.getItem(TRACK_KEY) !== "0";
let telemetryQueue = [];
let telemetryTimer = null;
const clientInfo = () => [
  navigator.platform || "", navigator.language || "",
  `${screen.width}x${screen.height}`, `${window.devicePixelRatio || 1}x`,
  matchMedia("(display-mode: standalone)").matches ? "standalone" : "browser",
  navigator.userAgent.slice(0, 180),
].join("|");

function track(event, props = {}) {
  if (!trackOn()) return;
  telemetryQueue.push({ event, props, ts: new Date().toISOString() });
  if (telemetryQueue.length >= 20) flushTelemetry(false);
  else if (!telemetryTimer) telemetryTimer = setTimeout(() => flushTelemetry(false), 8000);
}

function flushTelemetry(keepalive = false) {
  if (telemetryTimer) { clearTimeout(telemetryTimer); telemetryTimer = null; }
  if (!telemetryQueue.length || !trackOn()) return;
  const batch = telemetryQueue.splice(0, 100);
  const body = JSON.stringify({ session_id: SID, client: clientInfo(), events: batch });
  const headers = { "Content-Type": "application/json", ...authHeaders() };
  if (keepalive) {
    fetch("/api/telemetry", { method: "POST", headers, body, keepalive: true }).catch(() => {});
  } else {
    fetch("/api/telemetry", { method: "POST", headers, body }).catch(() => {
      telemetryQueue = batch.concat(telemetryQueue).slice(0, 200);
    });
  }
}

function authHeaders(extra) {
  const t = getToken();
  return t ? { ...(extra || {}), "X-OPC-Token": t } : (extra || {});
}

/* ------------------------------------------------------------------ */
/* API / 鉴权                                                          */
/* ------------------------------------------------------------------ */
async function api(path, opts = {}) {
  const { timeout = 30000, _retried = false, ...rest } = opts;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new Error("timeout")), timeout);
  if (rest.signal) {
    if (rest.signal.aborted) ctrl.abort(rest.signal.reason);
    else rest.signal.addEventListener("abort", () => ctrl.abort(rest.signal.reason), { once: true });
  }
  const headers = authHeaders({ ...(rest.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...(rest.headers || {}) });
  let r;
  try {
    r = await fetch(path, { ...rest, headers, signal: ctrl.signal });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === "AbortError") throw new Error("请求超时或已取消");
    throw new Error(navigator.onLine ? "网络请求失败" : "当前离线");
  }
  clearTimeout(timer);
  if (r.status === 401) {
    if (_retried) throw new Error("令牌无效");
    const t = await askText({ title: "需要访问令牌", label: "在电脑上运行 python -m opc token，然后粘贴到这里", placeholder: "OPC_TOKEN", password: true });
    if (t) {
      localStorage.setItem(TOKEN_KEY, t.trim());
      return api(path, { ...opts, _retried: true });
    }
    throw new Error("需要访问令牌");
  }
  const txt = await r.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = txt; }
  if (!r.ok) {
    const detail = data && (data.detail || data.message);
    const friendly = {
      401: "需要访问令牌",
      403: "需要访问令牌或无权限",
      413: "文件超过上限（8MB）",
      415: "不支持的文件类型",
      429: "请求过于频繁，请稍后重试",
    }[r.status];
    if (friendly) throw new Error(friendly + (detail ? `（${String(detail).slice(0, 120)}）` : ""));
    if (detail) throw new Error(String(detail));
    throw new Error(r.statusText || `HTTP ${r.status}`);
  }
  return data;
}

function askText({ title = "输入", label = "", value = "", placeholder = "", password = false, ok = "确定" }) {
  return new Promise((resolve) => {
    const m = document.createElement("div");
    m.className = "modal on";
    m.innerHTML = `<div class="sheet" style="width:min(460px,92vw)">
      <header class="sheet-head"><div class="sheet-title">${esc(title)}</div>
      <button class="icon-btn" data-x aria-label="关闭">${icon("x")}</button></header>
      <div class="sheet-body"><form class="sheet-form" data-f>
        ${label ? `<label>${esc(label)}</label>` : ""}
        <input data-i type="${password ? "password" : "text"}" value="${esc(value)}"
          placeholder="${esc(placeholder)}" autocomplete="off" />
      </form></div>
      <footer class="sheet-foot">
        <button class="btn ghost" data-x>取消</button>
        <button class="btn primary" data-ok>${esc(ok)}</button>
      </footer></div>`;
    document.body.appendChild(m);
    const inp = $("[data-i]", m);
    const close = (v) => { m.remove(); resolve(v); };
    const submit = () => close(inp.value.trim() || null);
    $("[data-ok]", m).onclick = submit;
    $$("[data-x]", m).forEach((b) => b.onclick = () => close(null));
    $("[data-f]", m).onsubmit = (e) => { e.preventDefault(); submit(); };
    m.addEventListener("click", (e) => { if (e.target === m) close(null); });
    setTimeout(() => { inp.focus(); inp.select(); }, 20);
    inp.addEventListener("keydown", (e) => { if (e.key === "Escape") close(null); });
  });
}

function confirmDialog({ title = "确认", body = "", ok = "确定", danger = false }) {
  return new Promise((resolve) => {
    const m = document.createElement("div");
    m.className = "modal on";
    m.innerHTML = `<div class="sheet" style="width:min(460px,92vw)">
      <header class="sheet-head"><div class="sheet-title">${esc(title)}</div>
      <button class="icon-btn" data-x aria-label="关闭">${icon("x")}</button></header>
      <div class="sheet-body" style="font-size:13px;color:var(--muted)">${esc(body)}</div>
      <footer class="sheet-foot"><button class="btn ghost" data-x>取消</button>
      <button class="btn ${danger ? "danger" : "primary"}" data-ok>${esc(ok)}</button></footer></div>`;
    document.body.appendChild(m);
    const close = (v) => { m.remove(); resolve(v); };
    $("[data-ok]", m).onclick = () => close(true);
    $$("[data-x]", m).forEach((b) => b.onclick = () => close(false));
    m.addEventListener("click", (e) => { if (e.target === m) close(false); });
  });
}

/* ------------------------------------------------------------------ */
/* Markdown 渲染（块级解析 + 行内处理，先转义再生成受控标签）             */
/* ------------------------------------------------------------------ */
function renderInline(raw, cites = []) {
  const stash = [];
  const keep = (html) => `\u0000${stash.push(html) - 1}\u0000`;
  let s = String(raw ?? "");
  s = s.replace(/`([^`]+)`/g, (_, c) => keep(`<code>${esc(c)}</code>`));
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) =>
    keep(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`));
  s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_, target, label) =>
    keep(`<span class="wikilink" title="${esc(target)}">${esc(label || target)}</span>`));
  s = s.replace(/https?:\/\/[^\s<]+/g, (u) =>
    keep(`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a>`));
  s = esc(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  if (cites && cites.length) {
    s = s.replace(/\[(\d+)\]/g, (m, n) => {
      const c = cites.find((x) => String(x.n) === String(n));
      if (!c) return m;
      return `<button class="cite-inline" data-cite="${esc(n)}" title="${esc(c.title || "")}">[${esc(n)}]</button>`;
    });
  }
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => stash[+i] ?? "");
}

function renderMarkdown(src, cites = []) {
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const isBlank = (l) => /^\s*$/.test(l || "");

  function paragraph(start) {
    const buf = [];
    while (start < lines.length && !isBlank(lines[start])
      && !/^\s*(#{1,6}\s|```|>\s?|[-*+]\s+|\d+\.\s+|\|)/.test(lines[start])
      && !/^\s*([-*_])\1{2,}\s*$/.test(lines[start])) {
      buf.push(lines[start]); start++;
    }
    if (buf.length) out.push(`<p>${buf.map((l) => renderInline(l, cites)).join("<br>")}</p>`);
    return start;
  }

  function list(start, ordered) {
    const re = ordered ? /^(\s*)\d+\.\s+(.*)$/ : /^(\s*)[-*+]\s+(.*)$/;
    const items = [];
    let j = start;
    while (j < lines.length) {
      const m = lines[j].match(re);
      if (!m) {
        if (isBlank(lines[j]) && j + 1 < lines.length && re.test(lines[j + 1])) { j++; continue; }
        break;
      }
      items.push({ indent: m[1].length, text: m[2] });
      j++;
    }
    const render = (arr, base) => {
      const tag = ordered ? "ol" : "ul";
      let html = `<${tag}>`;
      for (let k = 0; k < arr.length; k++) {
        const it = arr[k];
        let text = it.text;
        let box = "";
        const task = text.match(/^\[([ xX])\]\s+(.*)$/);
        if (task) {
          box = `<input type="checkbox" disabled ${task[1].toLowerCase() === "x" ? "checked" : ""}>`;
          text = task[2];
        }
        html += `<li>${box}${renderInline(text, cites)}`;
        const child = [];
        while (k + 1 < arr.length && arr[k + 1].indent > it.indent) { child.push(arr[++k]); }
        if (child.length) html += render(child, it.indent);
        html += "</li>";
      }
      return html + `</${tag}>`;
    };
    out.push(render(items, 0));
    return j;
  }

  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) { i++; continue; }
    if (/^\s*```/.test(line)) {
      const lang = line.replace(/^\s*```/, "").trim();
      const buf = []; i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
      if (i < lines.length) i++;
      out.push(`<pre${lang ? ` data-lang="${esc(lang)}"` : ""}><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) {
      const lv = clamp(h[1].length, 1, 4);
      out.push(`<h${lv}>${renderInline(h[2], cites)}</h${lv}>`); i++; continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, "")); i++;
      }
      out.push(`<blockquote>${buf.map((l) => renderInline(l, cites)).join("<br>")}</blockquote>`);
      continue;
    }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const header = lines[i].trim().replace(/^\||\|$/g, "").split("|").map((x) => x.trim());
      const sep = (lines[i + 1] || "").trim();
      if (/^\|?[\s:|-]+\|?$/.test(sep) && sep.includes("-")) {
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|").map((x) => x.trim()));
          i++;
        }
        out.push(`<table><thead><tr>${header.map((c) => `<th>${renderInline(c, cites)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${renderInline(c, cites)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        continue;
      }
    }
    if (/^\s*[-*+]\s+/.test(line)) { i = list(i, false); continue; }
    if (/^\s*\d+\.\s+/.test(line)) { i = list(i, true); continue; }
    i = paragraph(i);
  }
  return out.join("\n");
}

/* ------------------------------------------------------------------ */
/* Toast / 弹层                                                        */
/* ------------------------------------------------------------------ */
let toastTimer = null;
function toast(msg, { ms = 2400, action = null } = {}) {
  const el = $("#toast");
  el.innerHTML = `<span>${esc(msg)}</span>${action ? `<button type="button">${esc(action.label)}</button>` : ""}`;
  if (action) $("button", el).onclick = () => { action.fn(); el.classList.remove("on"); };
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), ms);
}

function openSheet({ title, meta = "", body = "", foot = [], className = "" }) {
  const modal = $("#modal"), sheet = $("#sheet");
  modal.classList.add("on");
  sheet.className = "sheet " + className;
  $("#modalTitle").textContent = title || "详情";
  $("#modalMeta").textContent = meta || "";
  $("#modalBody").innerHTML = body;
  const f = $("#modalFoot");
  f.innerHTML = "";
  for (const b of foot) {
    const btn = document.createElement("button");
    btn.className = "btn " + (b.kind || "ghost");
    btn.textContent = b.label;
    btn.onclick = b.fn;
    f.appendChild(btn);
  }
  $("#modalClose").onclick = closeSheet;
  modal.onclick = (e) => { if (e.target === modal) closeSheet(); };
  setTimeout(() => { const first = $("button,input,textarea,a", sheet); if (first) first.focus(); }, 40);
  return sheet;
}
function closeSheet() { $("#modal").classList.remove("on"); }

function setLive(text) { $("#srLive").textContent = text; }

function showStop(text = "正在生成回答…") {
  $("#stopBar").hidden = false;
  $("#stopText").textContent = text;
}
function hideStop() { $("#stopBar").hidden = true; }

/* ------------------------------------------------------------------ */
/* 线程 / 消息                                                         */
/* ------------------------------------------------------------------ */
function hero() {
  thread.innerHTML = `<div class="hero">
    <div class="logo-wrap"><img src="/static/logo.svg" alt="" /></div>
    <h1>今天，记点什么？</h1>
    <p>说一句话，自动判断「记」或「问」。内容会提炼成实体与概念，写进 Obsidian。</p>
    <div class="suggest">
      <button data-hero="meet">${icon("clock")}导入会议</button>
      <button data-hero="ask">${icon("spark")}问：最近一场会讲了什么</button>
      <button data-hero="graph">${icon("graph")}看图谱</button>
      <button data-hero="inbox">${icon("inbox")}看待办</button>
      <button data-hero="palette">${icon("command")}全部命令</button>
    </div>
  </div>`;
  $$("[data-hero]", thread).forEach((b) => b.onclick = () => {
    const a = b.dataset.hero;
    if (a === "meet") showMeetings();
    else if (a === "ask") { input.value = "最近一场会讲了什么？"; input.focus(); }
    else if (a === "graph") openDrawer("graph");
    else if (a === "inbox") openDrawer("inbox");
    else openPalette("cmd");
  });
}

function clearHero() { const h = $(".hero", thread); if (h) thread.innerHTML = ""; }
function scrollBottom(force = false) {
  const near = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  if (force || near) thread.scrollTop = thread.scrollHeight;
}
function addUser(text) {
  const el = document.createElement("div");
  el.className = "turn user";
  el.innerHTML = `<div class="body"></div>`;
  $(".body", el).textContent = text;
  thread.appendChild(el);
  scrollBottom(true);
  return el;
}
function addAI() {
  const el = document.createElement("div");
  el.className = "turn ai";
  el.innerHTML = `<div class="body"><span class="typing"><span class="spin"></span>准备…</span></div>`;
  thread.appendChild(el);
  scrollBottom(true);
  return { turn: el, body: $(".body", el) };
}
function sysLine(text, kind = "") {
  const el = document.createElement("div");
  el.className = "sys-line " + kind;
  el.innerHTML = `${kind === "error" ? icon("alert") : ""}<span>${esc(text)}</span>`;
  thread.appendChild(el);
  scrollBottom(true);
}

function citationsHtml(cites) {
  if (!cites || !cites.length) return "";
  return `<div class="cites">` + cites.map((c) =>
    `<button class="cite" data-cite="${esc(c.n)}" title="${esc(c.title || "")}">
      <span class="n">[${esc(c.n)}]</span><span class="t">${esc(c.title || "来源")}</span>
    </button>`).join("") + `</div>`;
}

function traceHtml(t) {
  if (!t) return "";
  const tokens = t.usage && (t.usage.total_tokens || (t.usage.prompt_tokens || 0) + (t.usage.completion_tokens || 0));
  const rows = [];
  if (t.search_query) rows.push(`<div class="trace-row"><span class="k">检索</span><span class="v">${esc(t.search_query)}</span></div>`);
  if (t.expansions && t.expansions.length) rows.push(`<div class="trace-row"><span class="k">扩展</span><span class="v trace-chips">${t.expansions.map((x) => `<span class="trace-chip">${esc(x)}</span>`).join("")}</span></div>`);
  const meta = [];
  if (t.hits) meta.push(`命中 ${t.hits} 段`);
  if (t.latency_ms) meta.push(`${(t.latency_ms / 1000).toFixed(1)}s`);
  if (t.first_token_ms) meta.push(`首字 ${(t.first_token_ms / 1000).toFixed(1)}s`);
  if (t.model) meta.push(t.model);
  if (tokens) meta.push(`${tokens} tokens`);
  if (meta.length) rows.push(`<div class="trace-row"><span class="k">过程</span><span class="v">${meta.map(esc).join(" · ")}</span></div>`);
  if (t.sensitive) rows.push(`<div class="trace-row"><span class="k">隐私</span><span class="v"><span class="trace-badge">${t.masked ? "已脱敏后送模型" : "命中敏感词"}</span></span></div>`);
  if (!rows.length) return "";
  return `<details class="trace"><summary>${icon("chevron-right")}<span>检索轨迹</span><span style="margin-left:auto;color:var(--faint)">${esc(meta.slice(0, 3).join(" · "))}</span></summary><div class="trace-body">${rows.join("")}</div></details>`;
}

function actionsHtml() {
  return `<div class="acts">
    <button class="act" data-act="copy">${icon("copy")}复制</button>
    <button class="act" data-act="save">${icon("save")}存为笔记</button>
    <button class="act" data-act="regen">${icon("refresh")}再答一次</button>
    <button class="act" data-act="related">${icon("link")}找相关</button>
    <button class="act" data-act="mindmap">${icon("map")}建导图</button>
    <button class="act" data-act="synth">${icon("spark")}加综述</button>
    <button class="act" data-act="actions">${icon("list-plus")}行动项</button>
    <button class="act" data-act="feedback-up" data-rating="up" aria-label="有帮助" title="有帮助">${icon("thumb-up")}</button>
    <button class="act" data-act="feedback-down" data-rating="down" aria-label="没帮助" title="没帮助">${icon("thumb-down")}</button>
  </div>`;
}

function bindTurnActions(turn, ctx) {
  $$("[data-act]", turn).forEach((b) => {
    b.onclick = async () => {
      const act = b.dataset.act;
      try {
        if (act === "copy") {
          await copyText(ctx.answer);
          toast("已复制");
          track("answer_action", { action: "copy" });
        } else if (act === "save") {
          if (b.dataset.done) return;
          const r = await api("/api/answer/save", { method: "POST", body: JSON.stringify({ question: ctx.question, answer: ctx.answer, citations: ctx.cites || [] }) });
          b.dataset.done = "1"; b.classList.add("on");
          toast(`已存为笔记${r.saved && r.saved.path ? "：" + r.saved.path : ""}`);
          track("answer_action", { action: "save", ok: true });
        } else if (act === "regen") {
          track("answer_action", { action: "regen" });
          ask(ctx.question, { regenerate: true });
        } else if (act === "related") {
          const c = (ctx.cites || []).find((x) => x.note_id);
          if (!c) { toast("需要引用一篇知识页才能找相关"); return; }
          const r = await api(`/api/related?note_id=${c.note_id}&limit=8`);
          const items = r.items || [];
          openSheet({
            title: "相关笔记",
            meta: `与《${c.title || "当前页"}》共享实体/概念`,
            body: items.length ? items.map((x) => `
              <div class="inbox-item"><div class="ii-head">${icon("source")}${esc(x.title)}</div>
              <div class="ii-meta">共享 ${x.overlap} 个实体/概念${x.path ? ` · ${esc(x.path)}` : ""}</div>
              <div class="ii-acts"><button class="btn" data-rel="${x.id}">打开</button></div></div>`).join("")
              : `<div class="empty">没有找到相关笔记</div>`,
            foot: [{ label: "关闭", fn: closeSheet }],
          });
          $$("[data-rel]").forEach((b) => {
            b.onclick = () => { closeSheet(); openNoteSheet(Number(b.dataset.rel)); };
          });
          track("answer_action", { action: "related", n: items.length });
        } else if (act === "mindmap") {
          const c = (ctx.cites || []).find((x) => x.source_id);
          if (!c) { toast("这条回答没有可导出导图的来源"); return; }
          const r = await api(`/api/mindmap/${c.source_id}`, { method: "POST" });
          toast(`已生成：${r.path}`);
          track("answer_action", { action: "mindmap" });
        } else if (act === "synth") {
          const first = (ctx.cites || [])[0];
          const topic = await askText({
            title: "加入综述", label: "主题（实体/概念名）",
            value: (first && first.title) || "", ok: "生成",
          });
          if (!topic) return;
          const r = await api(`/api/synthesize?name=${encodeURIComponent(topic)}`,
                             { method: "POST", timeout: 120000 });
          toast(`综述已生成${r.path ? "：" + r.path : ""}`);
          track("answer_action", { action: "synth" });
        } else if (act === "actions") {
          const items = [...String(ctx.answer || "").matchAll(/^\s*[-*]\s*\[ \]\s*(.+)$/gm)].map((m) => m[1].trim());
          if (!items.length) { toast("这段回答里没有未完成行动项"); return; }
          const r = await api("/api/actions/append", { method: "POST", body: JSON.stringify({ items }) });
          toast(`已加入行动项：${r.added} 条${r.skipped ? `，跳过重复 ${r.skipped}` : ""}`);
          track("answer_action", { action: "actions", n: items.length, added: r.added });
        } else if (act === "feedback-up" || act === "feedback-down") {
          const rating = b.dataset.rating;
          await api("/api/feedback", { method: "POST", body: JSON.stringify({ target: "answer", rating, props: { chars: (ctx.answer || "").length } }) });
          $$("[data-act^=feedback]", turn).forEach((x) => x.classList.toggle("on", x === b));
          toast(rating === "up" ? "谢谢，已记录" : "已记录，会用于后续优化");
          track("feedback", { rating });
        }
      } catch (e) { toast(e.message); }
    };
  });
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
}

/* ------------------------------------------------------------------ */
/* 问答                                                                */
/* ------------------------------------------------------------------ */
async function ask(question, { regenerate = false } = {}) {
  if (!question.trim()) return;
  if (S.ask) { toast("上一轮还在生成，先停止或等它结束"); return; }
  clearHero();
  addUser(question);
  const { turn, body } = addAI();
  turn.__question = question;
  const epoch = S.epoch;
  const ctrl = new AbortController();
  const state = { question, turn, body, ctrl, epoch, buf: "", cites: [], trace: {}, started: performance.now(), first: null, done: false, stopped: false };
  S.ask = state;
  showStop("正在理解问题…");
  setLive("正在生成回答");
  track("ask_start", { regenerate, has_conv: !!S.convId, chars: question.length });

  let raf = 0;
  const paint = () => {
    raf = 0;
    if (!state.buf) return;
    let s = $(".stream-text", body);
    if (!s) { body.innerHTML = `<div class="stream-text"></div>`; s = $(".stream-text", body); }
    s.textContent = state.buf;
    scrollBottom();
  };
  const schedule = () => { if (!raf) raf = requestAnimationFrame(paint); };

  try {
    const res = await fetch("/api/ask/stream", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ question, save: false, conversation_id: S.convId }),
      signal: ctrl.signal,
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const dataLines = chunk.split("\n").filter((l) => l.startsWith("data:"));
        if (!dataLines.length) continue;
        let ev;
        try { ev = JSON.parse(dataLines.map((l) => l.slice(5).trim()).join("\n")); } catch { continue; }
        if (epoch !== S.epoch) return;
        if (ev.type === "conversation") S.convId = ev.id;
        else if (ev.type === "status") {
          $("#stopText").textContent = ev.text || "处理中…";
          body.innerHTML = `<span class="typing"><span class="spin"></span>${esc(ev.text || "处理中")}</span>`;
        } else if (ev.type === "citations") {
          state.cites = ev.citations || [];
          state.trace = { ...ev };
          turn.__cites = state.cites;
          $("#stopText").textContent = `已检索 ${ev.hits || state.cites.length} 段，正在生成…`;
          body.innerHTML = `<div class="stream-text"></div><div class="cites" hidden></div>`;
        } else if (ev.type === "delta") {
          if (state.first === null) {
            state.first = Math.round(performance.now() - state.started);
            track("ask_first_token", { ms: state.first });
          }
          state.buf += ev.text || "";
          schedule();
        } else if (ev.type === "error") {
          state.error = ev.text || "生成失败";
        } else if (ev.type === "done") {
          state.done = true;
          state.doneData = ev;
        }
      }
    }
    if (raf) cancelAnimationFrame(raf);
    if (state.buf) paint();
    const answer = state.buf;
    if (!answer.trim() && !state.error) state.error = "没有返回内容";
    if (state.error) {
      body.innerHTML = `<div class="card" style="border-color:rgba(251,113,133,.3)"><div class="card-title" style="color:var(--bad)">${esc(state.error)}</div><div class="card-meta">可以重试，或先检查模型预算 / 网络</div><div class="card-acts"><button class="btn" data-retry>${icon("refresh")}重试</button></div></div>`;
      $("[data-retry]", body).onclick = () => ask(question);
      track("ask_error", { message: String(state.error).slice(0, 80) });
      setLive("回答失败");
      return;
    }
    const dd = state.doneData || {};
    state.trace = { ...state.trace, ...dd, hits: state.trace.hits, search_query: state.trace.search_query, expansions: state.trace.expansions, sensitive: state.trace.sensitive, masked: state.trace.masked };
    body.innerHTML = `<div class="md"></div>${citationsHtml(state.cites)}${traceHtml(state.trace)}${actionsHtml()}`;
    $(".md", body).innerHTML = renderMarkdown(answer, state.cites);
    turn.__answer = answer;
    turn.__cites = state.cites;
    bindTurnActions(turn, { question, answer, cites: state.cites });
    setLive("回答完成");
    track("ask_done", {
      ms: dd.latency_ms || Math.round(performance.now() - state.started),
      first_ms: dd.first_token_ms || state.first || 0,
      chars: answer.length,
      citations: state.cites.length,
      tokens: (dd.usage && (dd.usage.total_tokens || 0)) || 0,
      model: dd.model || "",
    });
  } catch (e) {
    if (raf) cancelAnimationFrame(raf);
    if (e.name === "AbortError") {
      state.stopped = true;
      if (state.buf) {
        body.innerHTML = `<div class="md"></div><div class="sys-line">已停止生成，已生成内容保留</div>`;
        $(".md", body).innerHTML = renderMarkdown(state.buf, state.cites);
      } else {
        body.innerHTML = `<div class="sys-line">已停止生成</div>`;
      }
      track("ask_stop", { ms: Math.round(performance.now() - state.started), chars: state.buf.length });
    } else {
      body.innerHTML = `<div class="card" style="border-color:rgba(251,113,133,.3)"><div class="card-title" style="color:var(--bad)">${esc(e.message)}</div><div class="card-acts"><button class="btn" data-retry>${icon("refresh")}重试</button></div></div>`;
      $("[data-retry]", body).onclick = () => ask(question);
      track("ask_error", { message: String(e.message).slice(0, 80) });
    }
  } finally {
    S.ask = null;
    hideStop();
    loadConversations();
  }
}

function stopAsk() {
  if (S.ask) S.ask.ctrl.abort();
}

/* ------------------------------------------------------------------ */
/* 捕获 / 任务                                                         */
/* ------------------------------------------------------------------ */
function taskCard(title, subtitle = "已收下") {
  const el = document.createElement("div");
  el.className = "task-card";
  el.innerHTML = `<div class="task-head"><span class="spin"></span><span class="task-title">${esc(title)}</span><span class="task-state">排队中</span></div>
    <div class="bar"><span style="width:2%"></span></div>
    <div class="stage-line"><span class="stage">${esc(subtitle)}</span><span class="pct">2%</span></div>
    <div class="task-acts"></div>`;
  thread.appendChild(el);
  scrollBottom(true);
  return el;
}
function setTaskState(card, state, stage, pct, failed = false) {
  if (!card) return;
  card.classList.toggle("failed", failed || state === "failed");
  card.classList.toggle("cancelled", state === "cancelled");
  card.classList.toggle("done", state === "done");
  const s = $(".task-state", card), st = $(".stage", card), p = $(".pct", card), bar = $(".bar", card);
  if (s) s.textContent = state;
  if (st) st.textContent = stage || "";
  if (p) p.textContent = `${clamp(pct || 0, 0, 100)}%`;
  if (bar) {
    const fill = $("span", bar);
    if (fill) fill.style.width = `${clamp(pct || 0, 0, 100)}%`;
  }
}
function taskActions(card, actions) {
  const box = $(".task-acts", card);
  if (!box) return;
  box.innerHTML = "";
  for (const a of actions) {
    const b = document.createElement("button");
    b.className = "btn " + (a.kind || "ghost");
    b.innerHTML = `${a.icon ? icon(a.icon) : ""}${esc(a.label)}`;
    b.onclick = a.fn;
    box.appendChild(b);
  }
}

function updateTaskStrip() {
  const strip = $("#taskStrip");
  const active = [...S.tasks.entries()].filter(([, t]) => !t.finished);
  strip.hidden = !active.length;
  strip.innerHTML = active.map(([id, t]) =>
    `<button class="task-chip" data-job="${id}" title="${esc(t.title)}"><span class="spin"></span><span class="t">${esc(t.title)}</span><span class="pct">${t.pct || 0}%</span></button>`).join("");
  $$(".task-chip", strip).forEach((b) => b.onclick = () => {
    const t = S.tasks.get(Number(b.dataset.job));
    if (t && t.card) t.card.scrollIntoView({ behavior: "smooth", block: "center" });
  });
}

async function watchJob(jobId, card, { title = "", onDone = null, retry = null } = {}) {
  const info = { title, card, pct: 0, finished: false };
  S.tasks.set(jobId, info);
  updateTaskStrip();
  let delay = 700;
  const started = performance.now();
  const cancelBtn = () => taskActions(card, [{
    label: "取消", kind: "ghost", icon: "x", fn: async () => {
      try {
        await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
        setTaskState(card, "cancelling", "正在取消…", info.pct);
        track("job_cancel", { kind: title });
      } catch (e) { toast(e.message); }
    },
  }]);
  cancelBtn();
  for (;;) {
    if (performance.now() - started > 15 * 60 * 1000) {
      setTaskState(card, "failed", "超时：任务仍在后台运行", info.pct, true);
      info.finished = true; updateTaskStrip();
      return;
    }
    await sleep(document.hidden ? 5000 : delay);
    delay = Math.min(3000, Math.round(delay * 1.25));
    let job;
    try { job = await api(`/api/jobs/${jobId}`, { timeout: 15000 }); }
    catch (e) { setTaskState(card, "failed", e.message, info.pct, true); info.finished = true; updateTaskStrip(); return; }
    const pct = clamp(job.progress || 0, 0, 100);
    info.pct = pct; info.title = title || job.kind;
    updateTaskStrip();
    setTaskState(card, job.status === "running" ? "running" : job.status, job.stage || job.status, pct);
    if (job.status === "done") {
      info.finished = true; updateTaskStrip();
      taskActions(card, []);
      if (onDone) onDone(job.result || {}, card);
      else setTaskState(card, "done", "完成", 100);
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      info.finished = true; updateTaskStrip();
      setTaskState(card, job.status, job.error || (job.status === "cancelled" ? "已取消" : "失败"), pct, job.status === "failed");
      if (retry) {
        taskActions(card, [{ label: "重试", kind: "primary", icon: "refresh", fn: async () => { card.remove(); retry(); } }]);
      } else if (job.status === "failed") {
        taskActions(card, [{ label: "重试任务", kind: "ghost", icon: "refresh", fn: async () => {
          try { await api(`/api/jobs/${jobId}/retry`, { method: "POST" }); watchJob(jobId, card, { title, onDone, retry }); }
          catch (e) { toast(e.message); }
        } }]);
      }
      track(job.status === "failed" ? "job_failed" : "job_cancelled", { kind: title, job: jobId });
      return;
    }
  }
}

function resultCard(result, { card = null, sourceId = null } = {}) {
  const ents = (result.entities || []).map((e) => `<span class="tag">${esc(e.name || e)}</span>`).join("");
  const cons = (result.concepts || []).map((c) => `<span class="tag">${esc(c.name || c)}</span>`).join("");
  const kp = (result.key_points || []).slice(0, 6).map((p) => `<div>· ${esc(p)}</div>`).join("");
  const el = document.createElement("div");
  el.className = "turn ai";
  el.innerHTML = `<div class="body">
    ${result.summary ? `<div class="md">${renderMarkdown(result.summary)}</div>` : ""}
    ${kp ? `<div class="card"><div class="card-meta" style="color:var(--muted)">${kp}</div></div>` : ""}
    <div class="card">
      <div class="card-title">${esc(result.title || "已入库")}</div>
      <div class="card-meta">${esc(result.path || "")}${(result.actions && result.actions.length) ? ` · 行动项 ${result.actions.length} 条` : ""}</div>
      <div class="tags">${ents}${cons}</div>
      <div class="card-acts">
        ${result.path ? `<button class="btn" data-r="open">${icon("external")}打开笔记</button>` : ""}
        ${(sourceId || result.source_id) ? `<button class="btn" data-r="map">${icon("map")}建导图</button>` : ""}
        <button class="btn" data-r="ask">${icon("spark")}就这条提问</button>
      </div>
    </div>
  </div>`;
  const sid = sourceId || result.source_id;
  const open = () => result.path ? openObsidian(result.path) : toast("还没有生成知识页");
  $$("[data-r]", el).forEach((b) => b.onclick = async () => {
    if (b.dataset.r === "open") open();
    else if (b.dataset.r === "map") { try { const r = await api(`/api/mindmap/${sid}`, { method: "POST" }); toast(`已生成 Canvas：${r.path}`); } catch (e) { toast(e.message); } }
    else { input.value = `关于《${result.title || ""}》，`; input.focus(); }
  });
  if (card && card.parentNode) card.replaceWith(el);
  else { thread.appendChild(el); scrollBottom(true); }
  return el;
}

async function captureNote(text, { echo = true } = {}) {
  if (!text.trim()) return;
  if (echo) { clearHero(); addUser(text); }
  const card = taskCard(text.trim().slice(0, 48) + (text.length > 48 ? "…" : ""), "已收下 · 等待提炼");
  const t0 = performance.now();
  if (!navigator.onLine) {
    addOutbox(text);
    setTaskState(card, "cancelled", "离线保存到本机，联网后自动提交", 0);
    taskActions(card, [{ label: "立即重试", kind: "primary", icon: "wifi", fn: () => { card.remove(); captureNote(text, { echo: false }); } }]);
    track("offline_capture", { chars: text.length });
    return;
  }
  try {
    const r = await api("/api/capture?background=true", {
      method: "POST", timeout: 15000, body: JSON.stringify({ text }),
    });
    track("capture_ack", { ms: Math.round(performance.now() - t0), chars: text.length });
    setTaskState(card, "running", "已收下 · 正在提炼", 2);
    watchJob(r.job_id, card, {
      title: "记一条",
      onDone: (result) => {
        resultCard(result, { card });
        track("capture_done", { ms: Math.round(performance.now() - t0), entities: (result.entities || []).length, concepts: (result.concepts || []).length });
        refreshPulse();
      },
      retry: () => captureNote(text, { echo: false }),
    });
  } catch (e) {
    setTaskState(card, "failed", e.message, 0, true);
    taskActions(card, [{ label: "重试", kind: "primary", icon: "refresh", fn: () => { card.remove(); captureNote(text, { echo: false }); } }]);
  }
}

function addOutbox(text) {
  const list = JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]");
  list.push({ id: Date.now() + Math.random().toString(16).slice(2), text, ts: Date.now() });
  localStorage.setItem(OUTBOX_KEY, JSON.stringify(list.slice(-100)));
  updatePulseLine();
}
async function flushOutbox() {
  const list = JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]");
  if (!list.length || !navigator.onLine) return;
  const pending = [];
  for (const item of list) {
    try {
      const r = await api("/api/capture?background=true", { method: "POST", body: JSON.stringify({ text: item.text }) });
      const card = taskCard(item.text.slice(0, 48), "联网后补交 · 等待提炼");
      watchJob(r.job_id, card, { title: "离线补交", onDone: (result) => { resultCard(result, { card }); refreshPulse(); } });
    } catch { pending.push(item); }
  }
  localStorage.setItem(OUTBOX_KEY, JSON.stringify(pending));
  updatePulseLine();
}

/* ------------------------------------------------------------------ */
/* 语音 / 上传 / 会议                                                  */
/* ------------------------------------------------------------------ */
let recorder = null, chunks = [];
async function toggleMic() {
  const btn = $("#mic");
  if (recorder && recorder.state === "recording") { recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia) { toast("当前浏览器不支持录音"); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorder = new MediaRecorder(stream);
    chunks = [];
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      btn.classList.remove("rec");
      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      if (blob.size > 1000) await uploadVoice(blob);
    };
    recorder.start();
    btn.classList.add("rec");
    sysLine("录音中…再点一下结束（最长 60 秒）");
    track("voice_start");
    setTimeout(() => { if (recorder && recorder.state === "recording") recorder.stop(); }, 60000);
  } catch (e) {
    toast("无法访问麦克风：" + e.message + "（局域网 http 需要 HTTPS）");
  }
}
async function uploadVoice(blob) {
  const card = taskCard("语音转写", "上传录音…");
  try {
    const fd = new FormData();
    fd.append("file", blob, "voice.webm");
    const r = await api("/api/voice", { method: "POST", body: fd, timeout: 30000 });
    watchJob(r.job_id, card, {
      title: "语音转写",
      onDone: (result) => {
        const el = document.createElement("div");
        el.className = "turn ai";
        el.innerHTML = `<div class="body"><div class="md">${renderMarkdown("**转写**\n\n" + (result.transcript || ""))}</div></div>`;
        card.replaceWith(el);
        if (result.title) resultCard(result);
        track("voice_done", { chars: (result.transcript || "").length });
        refreshPulse();
      },
    });
    track("voice_upload", { bytes: blob.size });
  } catch (e) { setTaskState(card, "failed", e.message, 0, true); }
}

async function uploadFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  for (const f of list) {
    const card = taskCard(f.name, "上传中…");
    try {
      const fd = new FormData();
      fd.append("file", f, f.name);
      const r = await api("/api/upload", { method: "POST", body: fd, timeout: 60000 });
      setTaskState(card, "running", "已上传 · 等待入库", 5);
      if (r.job_id) {
        watchJob(r.job_id, card, {
          title: f.name,
          onDone: (result) => { resultCard(result, { card }); track("upload_done", { bytes: f.size }); refreshPulse(); },
        });
      } else { setTaskState(card, "done", "已保存", 100); }
      track("upload", { bytes: f.size, type: f.type ? f.type.slice(0, 40) : "" });
    } catch (e) { setTaskState(card, "failed", e.message, 0, true); }
  }
}

async function showMeetings() {
  const sheet = openSheet({
    title: "导入会议",
    meta: "iMeeting 本地记录",
    body: `<div class="empty"><span class="spin"></span><div>读取本地 iMeeting…</div></div>`,
    foot: [{ label: "关闭", fn: closeSheet }],
  });
  try {
    const r = await api("/api/meetings?limit=20");
    const rows = (r.meetings || []).map((m) => `
      <div class="row" style="cursor:default">
        <div class="row-main">
          <div class="t">#${m.id} ${esc(m.title)}</div>
          <div class="m">${esc(m.status)} · ${Math.round((m.duration_ms || 0) / 60000)} min</div>
        </div>
        <button class="btn" data-mid="${m.id}" data-mtitle="${esc(m.title)}">导入</button>
      </div>`).join("");
    $("#modalBody", sheet).innerHTML = rows || `<div class="empty">${icon("inbox")}暂无会议</div>`;
    $$("[data-mid]", sheet).forEach((b) => b.onclick = async () => {
      const id = Number(b.dataset.mid), title = b.dataset.mtitle;
      b.disabled = true; b.textContent = "已排队";
      closeSheet();
      clearHero();
      const card = taskCard(`会议 #${id}`, "已排队 · 等待转写");
      try {
        const r = await api(`/api/meetings/${id}/ingest?background=true`, { method: "POST" });
        watchJob(r.job_id, card, {
          title: `会议 #${id}`,
          onDone: (result) => { resultCard(result, { card, sourceId: id }); track("meeting_done", { id }); refreshPulse(); },
        });
        track("meeting_ingest", { id });
      } catch (e) { setTaskState(card, "failed", e.message, 0, true); }
    });
  } catch (e) {
    $("#modalBody", sheet).innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ */
/* 抽屉：会话                                                          */
/* ------------------------------------------------------------------ */
async function loadConversations() {
  const box = $("#pane-conv");
  if (!box) return;
  try {
    const r = await api("/api/conversations?limit=50");
    const items = r.items || [];
    box.innerHTML = `<div class="pane-head"><h2>对话</h2><button class="icon-btn sm" id="convNew" title="新对话">${icon("plus")}</button></div>
      <input class="search-input" id="convSearch" placeholder="搜会话…" />
      <div id="convList"></div>`;
    $("#convNew", box).onclick = () => { newThread(); closeDrawer(); };
    let all = items;
    const render = (q = "") => {
      const list = all.filter((c) => !q || String(c.title || "").toLowerCase().includes(q.toLowerCase()));
      $("#convList", box).innerHTML = list.map((c) => `
        <div class="row ${c.id === S.convId ? "on" : ""}" data-cid="${c.id}" role="button" tabindex="0">
          <div class="row-main"><div class="t">${esc(c.title || "未命名")}</div>
          <div class="m">${c.n} 条 · ${fmtDate(c.updated_at)} · ${esc(String(c.last || "").slice(0, 30))}</div></div>
          <div class="row-actions">
            <button class="icon-btn sm" data-cact="rename" title="重命名">${icon("edit")}</button>
            <button class="icon-btn sm" data-cact="delete" title="删除">${icon("trash")}</button>
          </div>
        </div>`).join("") || `<div class="empty">${icon("chat")}还没有会话</div>`;
      $$(".row[data-cid]", box).forEach((row) => {
        row.onclick = (e) => { if (e.target.closest("[data-cact]")) return; openConversation(row.dataset.cid); };
      });
      $$("[data-cact]", box).forEach((b) => b.onclick = async (e) => {
        e.stopPropagation();
        const cid = b.closest(".row").dataset.cid;
        const c = all.find((x) => x.id === cid);
        if (b.dataset.cact === "rename") {
          const t = await askText({ title: "重命名会话", value: c?.title || "", ok: "保存" });
          if (t) { await api(`/api/conversations/${cid}`, { method: "PATCH", body: JSON.stringify({ title: t }) }); loadConversations(); }
        } else {
          const ok = await confirmDialog({ title: "删除会话", body: "这会删除该会话的全部消息，不影响知识库。", ok: "删除", danger: true });
          if (ok) { await api(`/api/conversations/${cid}`, { method: "DELETE" }); if (S.convId === cid) newThread(); loadConversations(); }
        }
        track("conversation_action", { action: b.dataset.cact });
      });
    };
    render();
    $("#convSearch", box).oninput = (e) => render(e.target.value);
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function openConversation(id) {
  if (S.ask) S.ask.ctrl.abort();
  S.epoch++;
  try {
    const c = await api("/api/conversations/" + id);
    S.convId = c.id;
    thread.innerHTML = "";
    for (const m of c.messages) {
      if (m.role === "user") addUser(m.content);
      else {
        const { turn, body } = addAI();
        turn.__answer = m.content;
        turn.__cites = m.citations || [];
        body.innerHTML = `<div class="md"></div>${citationsHtml(turn.__cites)}`;
        $(".md", body).innerHTML = renderMarkdown(m.content, turn.__cites);
        bindTurnActions(turn, { question: c.messages[c.messages.indexOf(m) - 1]?.content || "", answer: m.content, cites: turn.__cites });
      }
    }
    if (!c.messages.length) hero();
    closeDrawer();
    scrollBottom(true);
    track("conversation_open", { messages: c.messages.length });
  } catch (e) { toast("载入失败：" + e.message); }
}

function newThread() {
  if (S.ask) S.ask.ctrl.abort();
  S.epoch++;
  S.convId = null;
  thread.innerHTML = "";
  hero();
  input.focus();
  track("conversation_new");
}

/* ------------------------------------------------------------------ */
/* 抽屉：来源                                                          */
/* ------------------------------------------------------------------ */
let sourceQuery = "";
async function loadSources() {
  const box = $("#pane-sources");
  if (!box) return;
  box.innerHTML = `<div class="pane-head"><h2>来源</h2><span class="m" style="font-size:11px;color:var(--faint)" id="srcCount">…</span></div>
    <input class="search-input" id="srcSearch" placeholder="搜标题 / 摘要…" value="${esc(sourceQuery)}" />
    <div class="filters" id="srcKinds">
      <button class="chip on" data-kind="">全部</button>
      <button class="chip" data-kind="meeting">会议</button>
      <button class="chip" data-kind="note">笔记</button>
      <button class="chip" data-kind="file">文件</button>
      <button class="chip" data-kind="web">网页</button>
    </div>
    <div id="srcList"><div class="empty"><span class="spin"></span></div></div>`;
  let kind = "";
  const fetchSources = async () => {
    try {
      const rows = await api(`/api/sources?limit=150&kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(sourceQuery)}`);
      $("#srcCount", box).textContent = `${rows.length} 条`;
      $("#srcList", box).innerHTML = rows.map((s) => `
        <div class="row" data-sid="${s.id}" role="button" tabindex="0">
          <div class="row-main">
            <div class="t">${esc(s.title)}</div>
            <div class="m">#${s.id} · ${esc(s.kind)} · ${fmtDate(s.captured_at)} · ${s.note_path ? "已提炼" : "未提炼"}</div>
          </div>
          <div class="row-actions"><button class="icon-btn sm" data-sopen title="在 Obsidian 打开">${icon("external")}</button></div>
        </div>`).join("") || `<div class="empty">${icon("source")}还没有来源</div>`;
      $$(".row[data-sid]", box).forEach((row) => {
        row.onclick = (e) => { if (e.target.closest("[data-sopen]")) return; openSource(Number(row.dataset.sid)); };
      });
      $$("[data-sopen]", box).forEach((b) => b.onclick = (e) => {
        e.stopPropagation();
        const s = rows.find((x) => x.id === Number(b.closest(".row").dataset.sid));
        if (s?.note_path) openObsidian(s.note_path); else toast("还没有知识页");
      });
    } catch (e) {
      $("#srcList", box).innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  };
  const search = $("#srcSearch", box);
  let timer;
  search.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { sourceQuery = search.value.trim(); fetchSources(); }, 250);
  };
  $$("#srcKinds .chip", box).forEach((b) => b.onclick = () => {
    $$("#srcKinds .chip", box).forEach((x) => x.classList.toggle("on", x === b));
    kind = b.dataset.kind;
    fetchSources();
    track("source_filter", { kind });
  });
  await fetchSources();
}

async function openSource(id) {
  const sheet = openSheet({ title: "来源", meta: "#" + id, body: `<div class="empty"><span class="spin"></span></div>`, foot: [{ label: "关闭", fn: closeSheet }] });
  try {
    const r = await api("/api/sources/" + id);
    const s = r.source || {}, n = r.note || {}, d = r.document || {};
    $("#modalTitle", sheet).textContent = s.title || "来源";
    $("#modalMeta", sheet).textContent = `#${id} · ${s.kind} · ${fmtDate(s.captured_at)}`;
    $("#modalBody", sheet).innerHTML = `
      <div class="tags" style="margin:0 0 12px"><span class="tag">${esc(s.kind)}</span>${s.uri ? `<span class="tag">${esc(String(s.uri).slice(0, 60))}</span>` : ""}</div>
      ${n.summary ? `<div class="md">${renderMarkdown(n.summary)}</div>` : ""}
      ${d.excerpt ? `<div class="section-label">原文片段</div><div class="md" style="color:var(--muted)">${renderMarkdown(d.excerpt)}</div>` : ""}
      ${!n.id ? `<div class="empty">这条来源还没有提炼成知识页</div>` : ""}`;
    const foot = [];
    if (n.path) foot.push({ label: "打开笔记", kind: "primary", fn: () => openNoteSheet(n.id) });
    if (n.path) foot.push({ label: "在 Obsidian 打开", fn: () => openObsidian(n.path) });
    foot.push({ label: "建导图", fn: async () => { try { const x = await api(`/api/mindmap/${id}`, { method: "POST" }); toast(`已生成：${x.path}`); } catch (e) { toast(e.message); } } });
    foot.push({ label: "就这条提问", fn: () => { closeSheet(); input.value = `关于《${s.title || ""}》，`; input.focus(); } });
    $("#modalFoot", sheet).innerHTML = "";
    for (const f of foot) {
      const b = document.createElement("button"); b.className = "btn " + (f.kind || "ghost"); b.textContent = f.label; b.onclick = f.fn; $("#modalFoot", sheet).appendChild(b);
    }
    track("source_open", { id, kind: s.kind });
  } catch (e) { $("#modalBody", sheet).innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function openNoteSheet(noteId) {
  if (!noteId) return;
  try {
    const n = await api("/api/notes/" + noteId);
    const sheet = openSheet({
      title: n.title || "笔记", meta: n.path || "",
      body: `<div class="md"></div>`,
      foot: [
        ...(n.path ? [{ label: "在 Obsidian 打开", kind: "primary", fn: () => openObsidian(n.path) }] : []),
        { label: "就这条提问", fn: () => { closeSheet(); input.value = `关于《${n.title || ""}》，`; input.focus(); } },
      ],
    });
    $(".md", sheet).innerHTML = renderMarkdown(n.body_md || n.summary || "");
  } catch (e) { toast(e.message); }
}

async function openObsidian(path) {
  if (!path) { toast("没有可打开的页面"); return; }
  try { const r = await api("/api/open?path=" + encodeURIComponent(path)); location.href = r.uri; }
  catch (e) { toast("打开失败：" + e.message); }
}

/* ------------------------------------------------------------------ */
/* 抽屉：注记                                                          */
/* ------------------------------------------------------------------ */
let noteAnchor = { anchor_kind: "none" };
let noteScope = "recent";
async function loadNotes() {
  const box = $("#pane-notes");
  if (!box) return;
  try { noteAnchor = await api("/api/anchor"); } catch { noteAnchor = { anchor_kind: "none" }; }
  const a = noteAnchor || { anchor_kind: "none" };
  const label = a.anchor_kind === "meeting"
    ? `${esc(a.anchor_title || ("会议 " + a.anchor_ref))}${a.anchor_pos ? " · " + esc(a.anchor_pos) : ""}`
    : (a.anchor_kind === "source" ? esc(a.anchor_title || a.anchor_ref) : "无活动锚点（可手填位置）");
  box.innerHTML = `<div class="pane-head"><h2>注记</h2><span class="m" style="font-size:11px;color:var(--faint)">开会 / 看视频 / 读论文</span></div>
    <div class="note-anchor">${icon("note")}<span>${label}</span></div>
    <textarea class="note-editor" id="noteText" rows="3" placeholder="听到 / 读到的疑问或要点…（Ctrl+Enter 记下）"></textarea>
    <div class="note-tools">
      <input class="search-input" id="notePos" placeholder="位置 12:34 / p3" value="${a.anchor_pos ? esc(a.anchor_pos) : ""}" />
      <button class="btn primary" id="noteSave">${icon("plus")}记下</button>
    </div>
    <div class="filters" id="noteScopes">
      <button class="chip ${noteScope === "recent" ? "on" : ""}" data-scope="recent">最近</button>
      <button class="chip ${noteScope === "focused" ? "on" : ""}" data-scope="focused">重点</button>
      <button class="chip ${noteScope === "open" ? "on" : ""}" data-scope="open">未回应</button>
    </div>
    <div id="noteList"><div class="empty"><span class="spin"></span></div></div>`;
  $("#noteText").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveNote(); } };
  $("#noteSave").onclick = saveNote;
  $$("#noteScopes .chip", box).forEach((b) => b.onclick = () => { noteScope = b.dataset.scope; loadNotes(); track("note_scope", { scope: noteScope }); });
  try {
    const r = await api(`/api/annotations?scope=${noteScope}&limit=80`);
    const list = r.items || [];
    $("#noteList", box).innerHTML = list.map(noteRow).join("") || `<div class="empty">${icon("note")}还没有注记</div>`;
    bindNoteRows(box);
  } catch (e) { $("#noteList", box).innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
function noteRow(n) {
  const pos = n.anchor_pos ? ` · ${esc(n.anchor_pos)}` : "";
  const where = n.anchor_kind === "none" ? "无锚点" : `${esc(n.anchor_kind)}:${esc(n.anchor_ref || "")}${pos}`;
  return `<div class="note-item ${n.focused ? "focused" : ""}" data-aid="${n.id}">
    <div class="note-text">${n.kind === "reply" ? "↳ " : ""}${esc(n.text)} ${n.focused ? `<span class="star">★</span>` : ""}</div>
    <div class="note-meta"><span>#${n.id}</span><span>${where}</span><span>${esc(n.status)}</span>${n.anchor_title ? `<span>${esc(n.anchor_title)}</span>` : ""}${n.meta && n.meta.ctx ? `<span>同时：${esc(n.meta.ctx)}</span>` : ""}</div>
    <div class="note-acts">
      <button data-nact="focus">${n.focused ? "取消重点" : "标重点"}</button>
      <button data-nact="reply">回应</button>
      <button data-nact="archive">归档</button>
    </div>
  </div>`;
}
function bindNoteRows(root) {
  $$("[data-nact]", root).forEach((b) => b.onclick = async () => {
    const item = b.closest("[data-aid]"), aid = Number(item.dataset.aid);
    try {
      if (b.dataset.nact === "focus") {
        const on = b.textContent.includes("标重点");
        await api(`/api/annotations/${aid}/focus`, { method: "POST", body: JSON.stringify({ on }) });
        loadNotes();
      } else if (b.dataset.nact === "reply") {
        const old = $(".reply-box", item); if (old) { old.remove(); return; }
        const box = document.createElement("div");
        box.className = "reply-box";
        box.innerHTML = `<textarea placeholder="回应 / 补充…"></textarea><button class="btn primary">发送</button>`;
        item.appendChild(box);
        const ta = $("textarea", box); ta.focus();
        const send = async () => {
          const text = ta.value.trim(); if (!text) return;
          await api(`/api/annotations/${aid}/reply`, { method: "POST", body: JSON.stringify({ text }) });
          track("note_reply", { id: aid });
          loadNotes();
        };
        $("button", box).onclick = send;
        ta.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } };
      } else {
        await api(`/api/annotations/${aid}/archive`, { method: "POST", body: "{}" });
        loadNotes();
      }
    } catch (e) { toast(e.message); }
  });
}
async function saveNote() {
  const text = ($("#noteText") || {}).value?.trim();
  if (!text) return;
  const pos = ($("#notePos") || {}).value?.trim();
  const body = { text, kind: "question", auto_anchor: true };
  if (pos) body.anchor_pos = pos;
  if (noteAnchor && noteAnchor.anchor_kind && noteAnchor.anchor_kind !== "none") {
    body.anchor_kind = noteAnchor.anchor_kind;
    body.anchor_ref = noteAnchor.anchor_ref;
    if (noteAnchor.anchor_title) body.anchor_title = noteAnchor.anchor_title;
    if (noteAnchor.anchor_source_id) body.anchor_source_id = noteAnchor.anchor_source_id;
  }
  try {
    const r = await api("/api/annotations", { method: "POST", body: JSON.stringify(body) });
    $("#noteText").value = "";
    toast(`已注记 #${r.id}${r.anchor_pos ? " @" + r.anchor_pos : ""}`);
    track("note_save", { anchored: body.anchor_kind !== "none", chars: text.length });
    loadNotes();
  } catch (e) { toast("记录失败：" + e.message); }
}
async function quickAnnotate(text) {
  const r = await api("/api/annotations", { method: "POST", body: JSON.stringify({ text, kind: "question", auto_anchor: true }) });
  toast(`已注记 #${r.id}${r.anchor_pos ? " @" + r.anchor_pos : ""}`);
  track("note_quick", { chars: text.length });
  return r;
}

/* ------------------------------------------------------------------ */
/* 抽屉：待办 / 注意力中心                                             */
/* ------------------------------------------------------------------ */
async function loadAttention() {
  const box = $("#pane-inbox");
  if (!box) return;
  try {
    const a = await api("/api/attention?limit=8");
    S.attention = a;
    const c = a.counts || {};
    const block = (title, iconName, count, items, render) => `
      <div class="section-label">${title} · ${count}</div>
      ${count ? items.map(render).join("") : `<div class="empty" style="padding:10px">没有需要处理的项</div>`}`;
    box.innerHTML = `<div class="pane-head"><h2>待处理</h2><button class="icon-btn sm" id="attnRefresh" title="刷新">${icon("refresh")}</button></div>
      <div class="pane-sub">失败任务、本体冲突、草稿页与未回应注记。处理完会自动消失。</div>
      ${block("失败任务", "alert", c.jobs_failed || 0, a.failed || [], (j) => `
        <div class="inbox-item"><div class="ii-head">${icon("alert")}${esc(j.kind)} #${j.id}</div>
        <div class="ii-meta">${esc(j.error || "未知错误")}</div>
        <div class="ii-acts"><button class="btn" data-retry="${j.id}">${icon("refresh")}重试</button></div></div>`)}
      ${block("本体冲突", "lock", c.conflicts || 0, a.conflicts || [], (x) => `
        <div class="inbox-item"><div class="ii-head">${icon("lock")}${esc(x.kind)} #${x.ref_id} · ${esc(x.field)}</div>
        <div class="ii-meta">锁定：${esc(String(x.locked_value || "").slice(0, 60))}<br>新值：${esc(String(x.incoming_value || "").slice(0, 60))}</div>
        <div class="ii-acts"><button class="btn primary" data-adjudicate="${x.id}">让模型仲裁</button><button class="btn ghost" data-dismiss="${x.id}">忽略</button></div></div>`)}
      ${block("草稿页", "source", c.drafts || 0, a.drafts || [], (d) => `
        <div class="inbox-item"><div class="ii-head">${icon("source")}${esc(d.title)}</div>
        <div class="ii-meta">#${d.id} · ${esc(d.source_kind || "未知来源")} · 未发布</div>
        <div class="ii-acts"><button class="btn" data-draft="${d.id}">查看</button>${d.path ? `<button class="btn ghost" data-open="${esc(d.path)}">打开</button>` : ""}</div></div>`)}
      ${block("未回应注记", "note", c.annotations || 0, a.annotations || [], (n) => `
        <div class="inbox-item"><div class="ii-head">${icon("note")}#${n.id}</div>
        <div class="ii-meta">${esc(n.text)}${n.anchor_title ? " · " + esc(n.anchor_title) : ""}</div>
        <div class="ii-acts"><button class="btn" data-gonotes="1">去注记</button></div></div>`)}
      ${c.jobs_pending ? `<div class="section-label">排队任务 · ${c.jobs_pending}</div><div class="pane-sub">交互任务会优先处理；可以在任务卡上取消。</div>` : ""}`;
    $("#attnRefresh", box).onclick = loadAttention;
    $$("[data-retry]", box).forEach((b) => b.onclick = async () => {
      try { await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" }); toast("已重新排队"); track("inbox_retry", {}); setTimeout(loadAttention, 800); }
      catch (e) { toast(e.message); }
    });
    $$("[data-adjudicate]", box).forEach((b) => b.onclick = async () => {
      b.disabled = true; b.textContent = "仲裁中…";
      try { const r = await api(`/api/ontology/conflicts/${b.dataset.adjudicate}/adjudicate`, { method: "POST" }); toast(`仲裁结果：${r.decision || r.status || "完成"}`); track("inbox_adjudicate", {}); loadAttention(); }
      catch (e) { toast(e.message); b.disabled = false; b.textContent = "让模型仲裁"; }
    });
    $$("[data-dismiss]", box).forEach((b) => b.onclick = async () => {
      try { await api(`/api/ontology/conflicts/${b.dataset.dismiss}/dismiss`, { method: "POST" }); loadAttention(); }
      catch (e) { toast(e.message); }
    });
    $$("[data-draft]", box).forEach((b) => b.onclick = () => openNoteSheet(Number(b.dataset.draft)));
    $$("[data-open]", box).forEach((b) => b.onclick = () => openObsidian(b.dataset.open));
    $$("[data-gonotes]", box).forEach((b) => b.onclick = () => switchPane("notes"));
    updateAttentionBadge(c);
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

function updateAttentionBadge(counts) {
  const total = Object.values(counts || {}).reduce((a, b) => a + (Number(b) || 0), 0);
  const b = $("#attentionCount");
  b.hidden = !total;
  b.textContent = total > 99 ? "99+" : String(total);
  b.className = "badge " + (counts?.jobs_failed ? "bad" : total ? "warn" : "");
}

/* ------------------------------------------------------------------ */
/* 抽屉：本体（M2）                                                     */
/* ------------------------------------------------------------------ */
async function loadOntology() {
  const box = $("#pane-ontology");
  if (!box) return;
  box.innerHTML = `<div class="pane-head"><h2>本体</h2>
      <button class="icon-btn sm" id="ontoRefresh" title="刷新">${icon("refresh")}</button></div>
    <div class="pane-sub">先校正类型，再处理待审与合并候选。归并只在你点「合并」时执行。</div>
    <div id="ontoTypes"><div class="empty"><span class="spin"></span></div></div>
    <div id="ontoQueue"></div>
    <div id="ontoCands"></div>`;
  $("#ontoRefresh", box).onclick = loadOntology;
  await Promise.all([loadOntoTypes(), loadOntoQueue(), loadOntoCands()]);
  track("ontology_open", {});
}

async function loadOntoTypes() {
  const box = $("#ontoTypes");
  if (!box) return;
  try {
    const t = await api("/api/ontology/types?kind=entity");
    const max = Math.max(1, ...t.types.map((d) => d.count));
    const bars = t.types.map((d) => `
      <div class="onto-bar"><span class="tb-name">${esc(d.type)}</span>
        <i style="width:${Math.max(2, (d.count / max) * 100).toFixed(1)}%"></i>
        <b>${d.count}</b><em>${(d.ratio * 100).toFixed(1)}%</em></div>`).join("");
    const st = Object.entries(t.status || {}).map(([k, v]) => `${k || "空"}=${v}`).join(" · ");
    box.innerHTML = `<div class="section-label">类型分布 · ${t.total}</div>${bars}
      <div class="onto-meta">枚举外 ${t.outside_enum}（${(t.outside_ratio * 100).toFixed(1)}%） · other ${(t.other_ratio * 100).toFixed(1)}% · locked ${t.locked} · merged ${t.merged}${st ? " · " + st : ""}</div>
      <div class="ii-acts"><button class="btn" id="fixPreview">${icon("spark")}类型校正预览</button></div>
      <div id="fixResult" class="onto-meta"></div>`;
    $("#fixPreview", box).onclick = async () => {
      const btn = $("#fixPreview", box);
      btn.disabled = true; btn.textContent = "校正中…";
      try {
        const r = await api("/api/ontology/typefix",
          { method: "POST", body: JSON.stringify({ apply: false, use_llm: true }) });
        const rule = r.changes.filter((c) => c.by === "rule").length;
        const llmN = r.changes.filter((c) => c.by === "llm").length;
        $("#fixResult", box).innerHTML =
          `提议 <b>${r.proposed}</b> 条（规则 ${rule} / LLM ${llmN}），未落库。`;
        if (r.proposed) {
          const applyBtn = document.createElement("button");
          applyBtn.className = "btn primary";
          applyBtn.textContent = `落库 ${r.proposed} 条`;
          applyBtn.onclick = async () => {
            applyBtn.disabled = true;
            try {
              const done = await api("/api/ontology/typefix",
                { method: "POST", body: JSON.stringify({ apply: true, use_llm: true }) });
              toast(`已校正 ${done.applied} 条`);
              loadOntology();
            } catch (e) { toast(e.message); applyBtn.disabled = false; }
          };
          $("#fixResult", box).appendChild(applyBtn);
        }
        btn.disabled = false; btn.innerHTML = icon("spark") + "类型校正预览";
      } catch (e) {
        toast(e.message); btn.disabled = false; btn.innerHTML = icon("spark") + "类型校正预览";
      }
    };
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function loadOntoQueue() {
  const box = $("#ontoQueue");
  if (!box) return;
  try {
    const q = await api("/api/ontology/queue?limit=12");
    const items = q.items || [];
    box.innerHTML = `<div class="section-label">待审队列 · ${q.total || items.length}</div>` +
      (items.length ? items.map((it) => `
        <div class="inbox-item"><div class="ii-head">${icon("lock")}${esc(it.name)}
          <span class="onto-tag">${esc(it.type || "other")}</span></div>
          <div class="ii-meta">#${it.id} · 来源 ${it.src_count || 0}${it.page_path ? "" : " · 未建页"}</div>
          <div class="ii-acts"><button class="btn primary" data-ok="${it.id}">批准</button>
            <button class="btn ghost" data-no="${it.id}">退场</button></div></div>`).join("")
        : `<div class="empty" style="padding:10px">没有待审实体</div>`);
    const act = async (id, action) => {
      try {
        await api("/api/ontology/approve", {
          method: "POST",
          body: JSON.stringify({ kind: "entity", ident: String(id), action }),
        });
        toast(action === "confirm" ? "已批准" : "已退场");
        loadOntology();
      } catch (e) { toast(e.message); }
    };
    $$("[data-ok]", box).forEach((b) => b.onclick = () => act(b.dataset.ok, "confirm"));
    $$("[data-no]", box).forEach((b) => b.onclick = () => act(b.dataset.no, "reject"));
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function loadOntoCands() {
  const box = $("#ontoCands");
  if (!box) return;
  try {
    const c = await api("/api/ontology/candidates?limit=12");
    const items = c.open || [];
    box.innerHTML = `<div class="section-label">合并候选 · ${items.length}</div>` +
      (items.length ? items.map((it) => `
        <div class="inbox-item"><div class="cand-pair">${esc(it.a_name)}
          <span class="arrow">↔</span>${esc(it.b_name)}
          <span class="cand-score">${(it.score * 100).toFixed(0)}</span></div>
          <div class="ii-meta">${esc(it.reason || "")}</div>
          <div class="ii-acts"><button class="btn primary" data-merge="${it.id}"
            data-a="${it.a_id}" data-b="${it.b_id}">合并</button>
            <button class="btn ghost" data-ignore="${it.id}">忽略</button></div></div>`).join("")
        : `<div class="empty" style="padding:10px">没有合并候选</div>`);
    $$("[data-merge]", box).forEach((b) => b.onclick = async () => {
      b.disabled = true;
      try {
        const r = await api("/api/ontology/merge", {
          method: "POST", body: JSON.stringify({ a: b.dataset.a, b: b.dataset.b }),
        });
        toast(`已合并：移动 ${r.edges_moved} 条边、合并 ${r.edges_collapsed} 条`);
        loadOntology();
      } catch (e) { toast(e.message); b.disabled = false; }
    });
    $$("[data-ignore]", box).forEach((b) => b.onclick = async () => {
      try {
        await api(`/api/ontology/candidates/${b.dataset.ignore}/reject`, { method: "POST" });
        loadOntoCands();
      } catch (e) { toast(e.message); }
    });
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ------------------------------------------------------------------ */
/* 抽屉：图谱                                                          */
/* ------------------------------------------------------------------ */
const GRAPH_POS_KEY = () => `opc_graph_pos_v1:${S.build}:${S.graphMode}`;

async function loadGraph() {
  const cv = $("#canvas");
  if (!cv) return;
  const meta = $("#gmeta");
  if (meta) meta.textContent = "加载图谱…";
  let data;
  try { data = await api("/api/graph?limit=220&min_sources=2"); }
  catch (e) { if (meta) meta.textContent = "图谱加载失败"; return; }
  S.graphData = data;
  await buildGraph(data);
  const gm = data.meta || {};
  if (meta) {
    const shown = gm.shown || {}, total = gm.total || {};
    meta.textContent = `显示 ${(shown.entities || 0) + (shown.concepts || 0) + (shown.sources || 0)} · 概念准入门 ${gm.min_sources || 2}+ 来源 · 隐去单来源概念 ${gm.gated_concepts || 0} 个`;
  }
  track("graph_open", { nodes: data.nodes.length, links: data.links.length, mode: S.graphMode, gated: (data.meta || {}).gated_concepts || 0 });
}

async function buildGraph(data) {
  const cv = $("#canvas");
  if (!cv) return;
  const rect = cv.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(rect.width * dpr);
  cv.height = Math.round(rect.height * dpr);
  const ctx = cv.getContext("2d");
  const W = rect.width, H = rect.height;

  let nodes = data.nodes.map((n, i) => ({
    ...n,
    degree: 0,
    x: W / 2 + Math.cos(i * 2.399) * Math.min(W, H) * 0.22,
    y: H / 2 + Math.sin(i * 2.399) * Math.min(W, H) * 0.22,
  }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = data.links.filter((l) => byId.has(l.source) && byId.has(l.target))
    .map((l) => ({ ...l, a: byId.get(l.source), b: byId.get(l.target) }));
  for (const l of links) { l.a.degree++; l.b.degree++; }

  // 精简模式：按度数保留核心节点，避免一打开就是毛线团
  if (S.graphMode === "lite" && nodes.length > 120) {
    const keep = new Set([...nodes].sort((a, b) => b.degree - a.degree).slice(0, 120).map((n) => n.id));
    nodes = nodes.filter((n) => keep.has(n.id));
    const keepLinks = links.filter((l) => keep.has(l.a.id) && keep.has(l.b.id));
    nodes.forEach((n) => n.degree = 0);
    keepLinks.forEach((l) => { l.a.degree++; l.b.degree++; });
    links.splice(0, links.length, ...keepLinks);
  }

  const cached = JSON.parse(localStorage.getItem(GRAPH_POS_KEY()) || "{}");
  let missing = false;
  for (const n of nodes) {
    if (Array.isArray(cached[n.id])) { n.x = cached[n.id][0]; n.y = cached[n.id][1]; }
    else missing = true;
  }
  if (missing) {
    const pos = await layoutInWorker(nodes, links, W, H);
    if (pos) for (const p of pos) { const n = byId.get(p.id); if (n) { n.x = p.x; n.y = p.y; } }
    const out = {};
    for (const n of nodes) out[n.id] = [Math.round(n.x), Math.round(n.y)];
    try { localStorage.setItem(GRAPH_POS_KEY(), JSON.stringify(out)); } catch { /* ignore quota */ }
  }

  S.graph = {
    cv, ctx, dpr, W, H, nodes, links, byId,
    scale: 1, tx: 0, ty: 0, selected: null, hover: null,
    filter: $("#gfilter")?.value || "all",
    edgeFilter: S.graphEdge, q: "", onlyNeighbors: false,
    drag: null, pan: null, moved: false, active: false, armed: false,
    pointers: new Map(), pinch: null,
  };
  bindGraphOnce();
  fitGraph();
  drawGraph();
}

function layoutInWorker(nodes, links, W, H) {
  const payload = {
    nodes: nodes.map((n) => ({ id: n.id, x: n.x, y: n.y })),
    links: links.map((l) => ({ a: nodes.indexOf(l.a), b: nodes.indexOf(l.b), w: l.weight || 1 })),
    W, H, iters: 260,
  };
  const code = `self.onmessage=(e)=>{const{nodes,links,W,H,iters}=e.data;const k=Math.sqrt((W*H)/Math.max(nodes.length,1))*0.9;let temp=Math.min(W,H)/6;for(let t=0;t<iters;t++){for(const n of nodes){n.dx=0;n.dy=0;}for(let i=0;i<nodes.length;i++){for(let j=i+1;j<nodes.length;j++){const a=nodes[i],b=nodes[j];let dx=a.x-b.x,dy=a.y-b.y;const d2=dx*dx+dy*dy;if(d2>(k*2.4)**2)continue;const d=Math.sqrt(d2)||0.01;const f=(k*k)/d;dx/=d;dy/=d;a.dx+=dx*f;a.dy+=dy*f;b.dx-=dx*f;b.dy-=dy*f;}}for(const l of links){const a=nodes[l.a],b=nodes[l.b];let dx=a.x-b.x,dy=a.y-b.y;const d=Math.hypot(dx,dy)||0.01;const f=(d*d)/k*0.9;dx/=d;dy/=d;a.dx-=dx*f;a.dy-=dy*f;b.dx+=dx*f;b.dy+=dy*f;}for(const n of nodes){n.dx+=(W/2-n.x)*0.02;n.dy+=(H/2-n.y)*0.02;const len=Math.hypot(n.dx,n.dy)||1;const step=Math.min(len,temp);n.x+=(n.dx/len)*step;n.y+=(n.dy/len)*step;n.x=Math.max(24,Math.min(W-24,n.x));n.y=Math.max(24,Math.min(H-24,n.y));}temp*=0.97;}self.postMessage(nodes.map(n=>({id:n.id,x:n.x,y:n.y})));};`;
  if (typeof Worker === "undefined") return Promise.resolve(null);
  return new Promise((resolve) => {
    let w;
    try { w = new Worker(URL.createObjectURL(new Blob([code], { type: "text/javascript" }))); }
    catch { resolve(null); return; }
    const timer = setTimeout(() => { w.terminate(); resolve(null); }, 6000);
    w.onmessage = (e) => { clearTimeout(timer); w.terminate(); resolve(e.data); };
    w.onerror = () => { clearTimeout(timer); w.terminate(); resolve(null); };
    w.postMessage(payload);
  });
}

const g2s = (x, y) => ({ x: x * S.graph.scale + S.graph.tx, y: y * S.graph.scale + S.graph.ty });
const s2g = (x, y) => ({ x: (x - S.graph.tx) / S.graph.scale, y: (y - S.graph.ty) / S.graph.scale });

function graphVisible(n) {
  const G = S.graph;
  if (!G) return false;
  if (G.filter !== "all" && n.group !== G.filter && !(G.filter === "source" && n.group !== "entity" && n.group !== "concept")) return false;
  if (G.q && !String(n.label).toLowerCase().includes(G.q.toLowerCase())) return false;
  if (G.onlyNeighbors && G.selected && !neighbors(G.selected).has(n.id)) return false;
  return true;
}
function edgeVisible(l) {
  const G = S.graph;
  if (!G) return false;
  if (G.edgeFilter === "evidence" && l.rel !== "co_occurs") return false;
  if (G.edgeFilter === "structure" && l.rel === "co_occurs") return false;
  return graphVisible(l.a) && graphVisible(l.b);
}
function neighbors(n) {
  const s = new Set([n.id]);
  for (const l of S.graph.links) {
    if (l.a.id === n.id) s.add(l.b.id);
    if (l.b.id === n.id) s.add(l.a.id);
  }
  return s;
}
function nodeAt(mx, my) {
  if (!S.graph) return null;
  const p = s2g(mx, my);
  let best = null, bd = 16 / S.graph.scale;
  for (const n of S.graph.nodes) {
    if (!graphVisible(n)) continue;
    const d = Math.hypot(n.x - p.x, n.y - p.y);
    if (d < bd) { bd = d; best = n; }
  }
  return best;
}

function fitGraph() {
  const G = S.graph;
  if (!G || !G.nodes.length) return;
  const vis = G.nodes.filter(graphVisible);
  if (!vis.length) return;
  const xs = vis.map((n) => n.x), ys = vis.map((n) => n.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const bw = Math.max(maxX - minX, 60), bh = Math.max(maxY - minY, 60);
  G.scale = clamp(Math.min(G.W / bw, G.H / bh) * 0.82, 0.25, 3);
  G.tx = G.W / 2 - (minX + maxX) / 2 * G.scale;
  G.ty = G.H / 2 - (minY + maxY) / 2 * G.scale;
  drawGraph();
}

function drawGraph() {
  const G = S.graph;
  if (!G) return;
  const { ctx, W, H, dpr } = G;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const sel = G.selected, hood = sel ? neighbors(sel) : null;
  ctx.save();
  ctx.translate(G.tx, G.ty);
  ctx.scale(G.scale, G.scale);

  for (const l of G.links) {
    if (!edgeVisible(l)) continue;
    const hot = sel && (l.a.id === sel.id || l.b.id === sel.id);
    const evidence = l.rel === "co_occurs";
    ctx.strokeStyle = hot ? "rgba(94,234,212,.8)" : evidence ? "rgba(94,234,212,.42)" : l.rel === "related_to" ? "rgba(167,139,250,.26)" : "rgba(122,162,255,.19)";
    ctx.lineWidth = (hot ? 1.9 : 0.8 + Math.min(2, (l.weight || 1) / 4)) / G.scale;
    if (G.edgeFilter === "structure" && evidence) continue;
    ctx.beginPath(); ctx.moveTo(l.a.x, l.a.y); ctx.lineTo(l.b.x, l.b.y); ctx.stroke();
  }

  for (const n of G.nodes) {
    if (!graphVisible(n)) continue;
    const dim = hood && !hood.has(n.id);
    const base = n.group === "entity" ? 4.8 : n.group === "concept" ? 4.0 : 4.4;
    const r = base + Math.min(3.4, (n.degree || 0) / 14);
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    ctx.fillStyle = n.group === "entity" ? "#5eead4" : n.group === "concept" ? "#7aa2ff" : "#94a3b8";
    ctx.globalAlpha = dim ? 0.2 : 0.95;
    ctx.fill();
    ctx.globalAlpha = 1;
    if (n === sel) {
      ctx.beginPath(); ctx.arc(n.x, n.y, r + 4, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(94,234,212,.9)"; ctx.lineWidth = 1.6 / G.scale; ctx.stroke();
    }
  }
  ctx.restore();

  // labels in screen space (constant size + simple collision avoidance)
  const candidates = G.nodes.filter((n) => graphVisible(n))
    .filter((n) => n === sel || n === G.hover || (hood && hood.has(n.id)) || (n.degree || 0) >= 7)
    .sort((a, b) => (a === sel ? -2 : a === G.hover ? -1 : 0) - (b === sel ? -2 : b === G.hover ? -1 : 0) || (b.degree || 0) - (a.degree || 0))
    .slice(0, 34);
  const placed = [];
  ctx.font = "11px -apple-system,Segoe UI,Microsoft YaHei,sans-serif";
  ctx.textBaseline = "middle";
  for (const n of candidates) {
    const p = g2s(n.x, n.y);
    const label = String(n.label).slice(0, 22);
    const w = ctx.measureText(label).width;
    const rect = { x: p.x + 7, y: p.y - 8, w: w + 4, h: 16 };
    if (p.x + rect.w > W - 6 || p.y < 4 || p.y > H - 4) continue;
    if (placed.some((r) => !(rect.x > r.x + r.w || rect.x + rect.w < r.x || rect.y > r.y + r.h || rect.y + rect.h < r.y))) continue;
    placed.push(rect);
    const dim = hood && !hood.has(n.id);
    ctx.globalAlpha = dim ? 0.35 : 1;
    ctx.fillStyle = n === sel ? "#e9eff8" : n === G.hover ? "#dbe7f5" : "rgba(210,224,241,.72)";
    ctx.fillText(label, p.x + 7, p.y);
    ctx.globalAlpha = 1;
  }
  const z = $("#gzoom");
  if (z) z.textContent = Math.round(G.scale * 100) + "%";
}

function bindGraphOnce() {
  const cv = S.graph.cv;
  if (cv.dataset.bound) return;
  cv.dataset.bound = "1";
  const local = (e) => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };

  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    const G = S.graph; if (!G) return;
    const m = local(e);
    const before = s2g(m.x, m.y);
    G.scale = clamp(G.scale * (e.deltaY < 0 ? 1.12 : 0.9), 0.25, 4);
    G.tx = m.x - before.x * G.scale;
    G.ty = m.y - before.y * G.scale;
    drawGraph();
  }, { passive: false });

  cv.addEventListener("pointerdown", (e) => {
    const G = S.graph; if (!G) return;
    cv.setPointerCapture(e.pointerId);
    const m = local(e);
    G.pointers.set(e.pointerId, m);
    G.moved = false;
    if (G.pointers.size === 2) {
      const [a, b] = [...G.pointers.values()];
      G.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), scale: G.scale, cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2, tx: G.tx, ty: G.ty };
      G.drag = null; G.pan = null;
      return;
    }
    const n = nodeAt(m.x, m.y);
    if (n) G.drag = { node: n, ox: n.x, oy: n.y, mx: m.x, my: m.y };
    else G.pan = { tx: G.tx, ty: G.ty, mx: m.x, my: m.y };
  });

  cv.addEventListener("pointermove", (e) => {
    const G = S.graph; if (!G) return;
    const m = local(e);
    if (G.pointers.has(e.pointerId)) G.pointers.set(e.pointerId, m);
    if (G.pinch && G.pointers.size >= 2) {
      const [a, b] = [...G.pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const next = clamp(G.pinch.scale * (d / (G.pinch.d || d)), 0.25, 4);
      const before = s2g(G.pinch.cx, G.pinch.cy);
      G.scale = next;
      G.tx = G.pinch.cx - before.x * next;
      G.ty = G.pinch.cy - before.y * next;
      G.moved = true; drawGraph(); return;
    }
    if (G.drag) {
      const dx = (m.x - G.drag.mx) / G.scale, dy = (m.y - G.drag.my) / G.scale;
      G.drag.node.x = G.drag.ox + dx; G.drag.node.y = G.drag.oy + dy;
      if (Math.hypot(dx, dy) > 2) G.moved = true;
      drawGraph(); return;
    }
    if (G.pan) {
      G.tx = G.pan.tx + (m.x - G.pan.mx); G.ty = G.pan.ty + (m.y - G.pan.my);
      if (Math.abs(m.x - G.pan.mx) + Math.abs(m.y - G.pan.my) > 2) G.moved = true;
      drawGraph(); return;
    }
    const n = nodeAt(m.x, m.y);
    if (n !== G.hover) { G.hover = n; cv.style.cursor = n ? "pointer" : "grab"; drawGraph(); }
  });

  const finish = (e) => {
    const G = S.graph; if (!G) return;
    const m = local(e);
    G.pointers.delete(e.pointerId);
    if (G.pointers.size < 2) G.pinch = null;
    const moved = G.moved;
    G.drag = null; G.pan = null;
    if (moved) { G.moved = false; drawGraph(); return; }
    const hit = nodeAt(m.x, m.y);
    if (hit) {
      if (G.armed && G.selected && hit.id !== G.selected.id) { G.armed = false; linkNodes(G.selected, hit); }
      else selectNode(hit);
    } else {
      G.selected = null; hideGcard(); drawGraph();
    }
    G.moved = false;
  };
  cv.addEventListener("pointerup", finish);
  cv.addEventListener("pointercancel", finish);
  cv.addEventListener("pointerleave", () => { if (S.graph) { S.graph.hover = null; drawGraph(); } });
  cv.addEventListener("dblclick", (e) => {
    const m = local(e); const n = nodeAt(m.x, m.y);
    if (n && n.path) openObsidian(n.path);
  });
  cv.addEventListener("keydown", (e) => {
    const G = S.graph; if (!G) return;
    if (e.key === "+" || e.key === "=") { G.scale = clamp(G.scale * 1.15, .25, 4); drawGraph(); }
    else if (e.key === "-") { G.scale = clamp(G.scale / 1.15, .25, 4); drawGraph(); }
    else if (e.key === "0") fitGraph();
    else if (e.key === "Escape") { G.selected = null; hideGcard(); drawGraph(); }
  });
}

function selectNode(n) {
  const G = S.graph;
  G.selected = n;
  showGcard(n);
  drawGraph();
  track("graph_select", { group: n.group, degree: n.degree || 0 });
}

function showGcard(n) {
  const G = S.graph;
  const card = $("#gcard");
  card.hidden = false;
  $("#gcName").textContent = n.label;
  $("#gcType").textContent = n.group + (n.type && n.type !== n.group ? " · " + n.type : "");
  const rels = G.links.filter((l) => l.a.id === n.id || l.b.id === n.id);
  $("#gcMeta").textContent = `${rels.length} 条关系${n.summary ? " · " + n.summary.slice(0, 110) : ""}`;
  const acts = $("#gcActs"); acts.innerHTML = "";
  const add = (label, iconName, fn, kind = "") => {
    const b = document.createElement("button"); b.className = "btn " + kind;
    b.innerHTML = (iconName ? icon(iconName) : "") + esc(label); b.onclick = fn; acts.appendChild(b);
  };
  if (n.path) add("打开笔记", "external", () => openObsidian(n.path), "primary");
  add("就这个提问", "spark", () => { closeDrawer(); input.value = `关于《${n.label}》，`; input.focus(); });
  if (n.group === "entity" || n.group === "concept") {
    add("重命名", "edit", () => renameNode(n));
    add("连接…", "link", () => { G.armed = true; toast("再点一个节点完成连线"); });
  }
  if (n.degree) add("只看邻居", "eye", () => { G.onlyNeighbors = !G.onlyNeighbors; drawGraph(); });
  const nb = $("#gcNb"); nb.innerHTML = "";
  for (const l of rels.slice(0, 14)) {
    const other = l.a.id === n.id ? l.b : l.a;
    const row = document.createElement("div");
    row.className = "nbrow";
    row.title = l.evidence || "";
    row.innerHTML = `<span class="rel">${esc(l.rel)}</span><span>${esc(other.label)}</span><button class="cut" title="断开">${icon("x")}</button>`;
    $(".cut", row).onclick = (ev) => { ev.stopPropagation(); cutEdge(l.id); };
    row.onclick = (ev) => { if (ev.target.closest(".cut")) return; selectNode(other); };
    nb.appendChild(row);
  }
}
function hideGcard() { const c = $("#gcard"); if (c) c.hidden = true; }

async function renameNode(n) {
  const name = await askText({ title: "重命名节点", value: n.label, label: "只改显示名与页面标题；key 与文件名保持稳定。", ok: "重命名" });
  if (!name || name === n.label) return;
  try {
    await api(`/api/graph/nodes/${n.group}/${n.ref}`, { method: "PATCH", body: JSON.stringify({ name }) });
    toast("已重命名");
    track("graph_rename", { group: n.group });
    await reloadGraph();
  } catch (e) { toast("失败：" + e.message); }
}
async function linkNodes(a, b) {
  if (!["entity", "concept"].includes(a.group) || !["entity", "concept"].includes(b.group)) { toast("只能连接实体/概念"); return; }
  try {
    await api("/api/graph/edges", { method: "POST", body: JSON.stringify({ src_type: a.group, src_id: a.ref, dst_type: b.group, dst_id: b.ref }) });
    toast("已连线"); track("graph_link", {});
    await reloadGraph();
  } catch (e) { toast("失败：" + e.message); }
}
async function cutEdge(edgeId) {
  try { await api("/api/graph/edges/" + edgeId, { method: "DELETE" }); toast("已断开"); track("graph_cut", {}); await reloadGraph(); }
  catch (e) { toast("失败：" + e.message); }
}
async function undoGraph() {
  try { const r = await api("/api/graph/undo", { method: "POST" }); toast(r.undone === "rename" ? "已撤销重命名" : "已撤销上一步"); track("graph_undo", {}); await reloadGraph(); }
  catch (e) { toast("撤销失败：" + e.message); }
}
async function reloadGraph() {
  try { localStorage.removeItem(GRAPH_POS_KEY()); } catch { /* ignore */ }
  hideGcard();
  await loadGraph();
}

function resizeGraph() {
  const G = S.graph;
  if (!G) return;
  const rect = G.cv.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  G.dpr = window.devicePixelRatio || 1;
  G.cv.width = Math.round(rect.width * G.dpr);
  G.cv.height = Math.round(rect.height * G.dpr);
  G.W = rect.width; G.H = rect.height;
  drawGraph();
}

function bindGraphToolbar() {
  const bind = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };
  bind('[data-g="in"]', () => { if (S.graph) { S.graph.scale = clamp(S.graph.scale * 1.2, .25, 4); drawGraph(); } });
  bind('[data-g="out"]', () => { if (S.graph) { S.graph.scale = clamp(S.graph.scale / 1.2, .25, 4); drawGraph(); } });
  bind('[data-g="fit"]', fitGraph);
  bind('[data-g="undo"]', undoGraph);
  bind('[data-g="full"]', () => { const d = $("#drawer"); d.classList.toggle("full"); setTimeout(resizeGraph, 120); });
  $("#gcClose").onclick = () => { if (S.graph) { S.graph.selected = null; hideGcard(); drawGraph(); } };
  const q = $("#gsearch");
  if (q) q.oninput = () => { if (S.graph) { S.graph.q = q.value.trim(); drawGraph(); } };
  const mode = $("#gmode");
  if (mode) {
    mode.value = S.graphMode;
    mode.onchange = async () => {
      S.graphMode = mode.value; localStorage.setItem("opc_graph_mode", S.graphMode);
      await reloadGraph();
    };
  }
  const edge = $("#gedge");
  if (edge) {
    edge.value = S.graphEdge;
    edge.onchange = () => {
      S.graphEdge = edge.value; localStorage.setItem("opc_graph_edge", S.graphEdge);
      if (S.graph) { S.graph.edgeFilter = S.graphEdge; drawGraph(); }
      track("graph_filter", { edge: S.graphEdge });
    };
  }
  const filter = $("#gfilter");
  if (filter) {
    if (S.graph) filter.value = S.graph.filter || "all";
    filter.onchange = () => {
      if (S.graph) { S.graph.filter = filter.value; drawGraph(); }
      track("graph_filter", { node: filter.value });
    };
  }
}

/* ------------------------------------------------------------------ */
/* 命令面板 / 搜索                                                     */
/* ------------------------------------------------------------------ */
const COMMANDS = [
  { id: "new", label: "新对话", icon: "plus", keys: "new chat 清空", run: () => newThread() },
  { id: "meetings", label: "导入会议", icon: "clock", keys: "meeting imeeting 会议", run: () => showMeetings() },
  { id: "sync", label: "扫描 inbox", icon: "inbox", keys: "sync inbox 文件", run: () => runSync() },
  { id: "harvest", label: "回收 AI 会话", icon: "spark", keys: "harvest opencode 会话", run: () => runHarvest() },
  { id: "digest", label: "生成日报", icon: "source", keys: "digest daily 日报 汇总", run: () => runDigest() },
  { id: "actions", label: "同步行动项", icon: "list-plus", keys: "actions todo 行动项", run: () => runActions() },
  { id: "lint", label: "知识库体检", icon: "check", keys: "lint check 体检 孤儿 断链", run: () => runLint() },
  { id: "enrich", label: "补全空壳页", icon: "spark", keys: "enrich 补全 空壳 实体 概念", run: () => runEnrich() },
  { id: "watch", label: "值守状态", icon: "clock", keys: "watch scheduler 值守 定时", run: () => runWatch() },
  { id: "synth", label: "跨来源综述", icon: "spark", keys: "synthesize topic 综述 主题", run: () => promptSynth() },
  { id: "graph", label: "打开图谱", icon: "graph", keys: "graph 图谱 关系", run: () => openDrawer("graph") },
  { id: "conv", label: "历史会话", icon: "chat", keys: "conversation history 会话", run: () => openDrawer("conv") },
  { id: "sources", label: "查看来源", icon: "source", keys: "sources 来源 文件", run: () => openDrawer("sources") },
  { id: "notes", label: "注记", icon: "note", keys: "notes 注记 会议 时间戳", run: () => openDrawer("notes") },
  { id: "inbox", label: "待处理", icon: "inbox", keys: "inbox attention 待办 失败 冲突", run: () => openDrawer("inbox") },
  { id: "ontology", label: "本体治理", icon: "lock", keys: "ontology 本体 类型 合并 治理", run: () => openDrawer("ontology") },
  { id: "settings", label: "设置与连接器", icon: "settings", keys: "settings tools 设置 工具", run: () => openSettings() },
  { id: "caps", label: "能力清单", icon: "list-plus", keys: "caps capabilities 能力 清单 api", run: () => showCapabilities() },
  { id: "help", label: "帮助", icon: "alert", keys: "help 帮助 命令", run: () => showHelp() },
];

function openPalette(mode = "cmd", prefill = "") {
  S.paletteMode = mode;
  $("#palette").classList.add("open");
  $$(".ptab").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  const inp = $("#paletteInput");
  inp.value = prefill;
  inp.placeholder = mode === "search" ? "搜索知识库：标题 / 摘要 / 原文…" : "输入命令，或搜索知识库…";
  renderPalette();
  setTimeout(() => inp.focus(), 20);
  track("palette_open", { mode });
}
function closePalette() { $("#palette").classList.remove("open"); }
function setPaletteMode(mode) {
  S.paletteMode = mode;
  $$(".ptab").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  const inp = $("#paletteInput");
  inp.placeholder = mode === "search" ? "搜索知识库：标题 / 摘要 / 原文…" : "输入命令，或搜索知识库…";
  renderPalette();
}

function renderPalette() {
  const q = ($("#paletteInput")?.value || "").trim();
  const list = $("#paletteList");
  if (S.paletteMode === "cmd") {
    const low = q.toLowerCase();
    S.paletteItems = COMMANDS.filter((c) => !low || c.label.toLowerCase().includes(low) || c.keys.includes(low)).map((c) => ({ type: "cmd", ...c }));
    if (q) S.paletteItems.push({ type: "search", label: `在知识库中搜索「${q}」`, icon: "search", run: () => { setPaletteMode("search"); renderPalette(); } });
    S.paletteSel = clamp(S.paletteSel, 0, Math.max(0, S.paletteItems.length - 1));
    list.innerHTML = S.paletteItems.map((it, i) => `
      <div class="cmd ${i === S.paletteSel ? "sel" : ""}" data-i="${i}">
        <span class="g">${icon(it.icon || "command")}</span>
        <span class="title">${esc(it.label)}</span>
        <span class="sub">${it.type === "cmd" ? "命令" : "搜索"}</span>
      </div>`).join("") || `<div class="palette-empty">没有匹配的命令</div>`;
    bindPaletteItems();
    return;
  }
  // 知识库搜索
  if (!q) {
    list.innerHTML = `<div class="palette-empty">${icon("search")}输入关键词搜索知识库<br><span style="font-size:11px">标题、摘要、原文片段都会命中</span></div>`;
    S.paletteItems = [];
    return;
  }
  const seq = ++S.searchSeq;
  list.innerHTML = `<div class="palette-empty"><span class="spin"></span> 搜索中…</div>`;
  api(`/api/search?q=${encodeURIComponent(q)}&limit=10`).then((r) => {
    if (seq !== S.searchSeq) return;
    const hits = r.hits || [];
    S.paletteItems = hits.map((h) => ({
      type: "hit", ...h,
      label: h.title || h.snippet || "未命名",
      run: () => openHit(h),
    }));
    S.paletteSel = 0;
    list.innerHTML = S.paletteItems.map((it, i) => `
      <div class="cmd ${i === S.paletteSel ? "sel" : ""}" data-i="${i}">
        <span class="g">${icon(it.type === "note" ? "source" : "search")}</span>
        <span class="title">${esc(it.title || "未命名")}<div class="snippet">${esc(it.snippet || "")}</div></span>
        <span class="kind">${esc(it.type || "")}</span>
      </div>`).join("") || `<div class="palette-empty">没有命中<br><span style="font-size:11px">可以按 Enter 直接向知识库提问</span></div>`;
    bindPaletteItems();
    track("search", { q_len: q.length, results: hits.length, mode: "palette" });
  }).catch((e) => { if (seq === S.searchSeq) list.innerHTML = `<div class="palette-empty">${esc(e.message)}</div>`; });
}
function bindPaletteItems() {
  $$("#paletteList .cmd").forEach((el) => el.onclick = () => { const i = Number(el.dataset.i); if (S.paletteItems[i]) runPalette(i); });
}
function runPalette(i) {
  const it = S.paletteItems[i];
  if (!it) {
    if (S.paletteMode === "search") { const q = $("#paletteInput").value.trim(); if (q) { closePalette(); ask(q); } }
    return;
  }
  closePalette();
  it.run();
}
function openHit(h) {
  if (h.type === "note" && h.id) openNoteSheet(h.id);
  else if (h.type === "chunk" && h.id) openSource(h.id);
  else if (h.path) openObsidian(h.path);
  else openSource(h.id);
  track("search_open", { type: h.type || "" });
}

async function runSync() {
  sysLine("扫描 inbox…");
  try {
    const r = await api("/api/sync", { method: "POST", body: JSON.stringify({ limit: 10 }) });
    const res = r.results || [];
    sysLine(res.length ? res.map((x) => `${x.title || x.path} — ${x.error || (x.skipped ? "未变化" : "已入库")}`).join(" · ") : "没有新文件");
    refreshPulse();
  } catch (e) { sysLine(e.message, "error"); }
}
async function runHarvest() {
  sysLine("回收 OpenCode 最近会话…");
  try {
    const r = await api("/api/harvest", { method: "POST", body: JSON.stringify({ tool: "opencode", limit: 2 }) });
    sysLine((r.results || []).map((x) => `${x.title || x.session || "?"} — ${x.error || "已入库"}`).join(" · ") || "没有可回收会话");
    refreshPulse();
  } catch (e) { sysLine(e.message, "error"); }
}
async function runDigest() {
  sysLine("汇总今天的素材…");
  try {
    const r = await api("/api/digest", { method: "POST" });
    sysLine(r.skipped ? "这次没有可汇总的新素材" : `${r.headline || "日报已生成"} → ${r.path}（要点 ${r.points || 0} · 来源 ${r.sources || 0}）`);
    refreshPulse();
  } catch (e) { sysLine(e.message, "error"); }
}
async function runActions() {
  sysLine("汇总行动项…");
  try {
    const r = await api("/api/actions/sync", { method: "POST" });
    const list = await api("/api/actions?limit=10");
    sysLine(`已同步：待办 ${r.todo} / 完成 ${r.done}`);
    for (const i of (list.items || []).slice(0, 8)) sysLine(`☐ ${i.text} — ${i.note}`);
  } catch (e) { sysLine(e.message, "error"); }
}
async function runLint() {
  sysLine("体检中（孤儿 / 断链 / 空壳 / 重复 / 矛盾）…");
  try {
    const r = await api("/api/lint", { method: "POST" });
    sysLine(`体检完成：${r.pages} 页 · 孤儿 ${r.orphans} · 断链 ${r.broken} · 空壳 ${r.stubs} · 重复 ${r.duplicates} · 矛盾 ${(r.conflicts || []).length} · ${r.path}`);
  } catch (e) { sysLine(e.message, "error"); }
}
async function runEnrich() {
  sysLine("补全空壳实体 / 概念页（只用已有关联来源）…");
  try {
    const r = await api("/api/enrich?limit=5", { method: "POST", timeout: 120000 });
    sysLine(`补全完成：${r.filled ?? r.updated ?? 0} 页${r.skipped ? ` · 跳过 ${r.skipped}` : ""}${r.path ? " · " + r.path : ""}`);
    refreshPulse();
  } catch (e) { sysLine(e.message, "error"); }
}
async function runWatch() {
  try {
    const r = await api("/api/watch");
    sysLine(`值守：${r.running ? "运行中" : "未运行"} · 最近一轮 ${fmtDate(r.last?.at)} · 已处理 ${r.last?.processed ?? 0} 个任务${r.last?.error ? " · 错误：" + r.last.error : ""}`);
  } catch (e) { sysLine(e.message, "error"); }
}
async function promptSynth() {
  const name = await askText({ title: "跨来源综述", label: "主题名（会匹配实体/概念与相关来源）", placeholder: "例如：总体能力", ok: "生成" });
  if (!name) return;
  sysLine(`跨来源综述「${name}」…`);
  try {
    const r = await api("/api/synthesize?name=" + encodeURIComponent(name), { method: "POST" });
    sysLine(`${r.subject}（${r.sources} 份来源）→ ${r.path} · 时间线 ${r.timeline} 条 · 分歧 ${r.conflicts} 处`);
  } catch (e) { sysLine(e.message, "error"); }
}
function showHelp() {
  openSheet({
    title: "帮助", meta: "OPC v" + (S.pulse?.version || ""),
    body: `<div class="section-label">输入</div>
      <div class="kv"><span>Enter</span><b>发送</b></div>
      <div class="kv"><span>Shift + Enter</span><b>换行</b></div>
      <div class="kv"><span>Ctrl / ⌘ + Enter</span><b>强制按「问」处理</b></div>
      <div class="kv"><span>/</span><b>命令面板</b></div>
      <div class="kv"><span>Ctrl / ⌘ + K</span><b>命令 + 知识库搜索</b></div>
      <div class="section-label">模式</div>
      <p style="color:var(--muted);font-size:12.5px">自动会按问号、疑问词和 URL 判断；也可以点输入框上方的「记 / 问 / 做」手动指定。离线时捕获会先放进本机队列，联网后自动补交。</p>
      <div class="section-label">快捷命令</div>
      <div class="kv"><span>/ask /note /topic /meeting /sync /digest /lint /graph</span><b>斜杠命令</b></div>`,
    foot: [{ label: "关闭", fn: closeSheet }],
  });
}

/* ------------------------------------------------------------------ */
/* 能力清单（WP-06）                                                    */
/* ------------------------------------------------------------------ */
async function showCapabilities() {
  const sheet = openSheet({
    title: "能力清单",
    meta: "REST / MCP / CLI / UI 同源",
    body: `<div class="empty"><span class="spin"></span> 读取能力注册表…</div>`,
    foot: [{ label: "关闭", fn: closeSheet }],
  });
  try {
    const res = await api("/api/capabilities");
    const rows = (res.capabilities || []).map((c) => `
      <div class="inbox-item">
        <div class="ii-head">${esc(c.label)}<span class="onto-tag">${esc(c.kind)}</span></div>
        <div class="ii-meta">${esc(c.desc || "")}</div>
        <div class="ii-meta">CLI ${esc(c.cli || "-")} · MCP ${esc((c.mcp || []).join(", ") || "-")}${
          c.rest ? ` · ${esc(c.rest.method)} ${esc(c.rest.path)}` : ""}</div>
      </div>`).join("");
    $("#modalBody", sheet).innerHTML =
      `<div class="section-label">共 ${res.total} 项 · ${Object.entries(res.by_kind || {}).map(([k, v]) => `${k} ${v}`).join(" / ")}</div>${rows}`;
  } catch (e) { $("#modalBody", sheet).innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  track("caps_open", {});
}

/* ------------------------------------------------------------------ */
/* 设置                                                                */
/* ------------------------------------------------------------------ */
async function openSettings(refresh = false) {
  const sheet = openSheet({
    title: "设置", meta: "本机优先",
    body: `<div class="empty"><span class="spin"></span> 读取连接器（首次约 2 秒）…</div>`,
    foot: [{ label: "关闭", fn: closeSheet }],
  });
  const [statusRes, summaryRes, pulseRes, semRes] = await Promise.allSettled([
    api(`/api/status?refresh=${refresh}`, { timeout: 40000 }),
    api("/api/telemetry/summary?days=7"),
    api("/api/pulse"),
    api("/api/semantic/status"),
  ]);
  const st = statusRes.status === "fulfilled" ? statusRes.value : { tools: [], stats: {} };
  const sum = summaryRes.status === "fulfilled" ? summaryRes.value : null;
  const sem = semRes.status === "fulfilled" ? semRes.value : null;
  if (pulseRes.status === "fulfilled") S.pulse = pulseRes.value;
  S.tools = st.tools || [];
  S.telemetrySummary = sum;
  const tools = (st.tools || []).map((t) => `
    <div class="tool-row"><span class="dot ${t.ok ? "" : "off"}" style="background:${t.ok ? "var(--ok)" : "var(--bad)"}"></span>
      <span class="name">${esc(t.name)}</span><span class="kind">${esc(t.kind)}</span>
      <span style="font-size:10.5px;color:${t.ok ? "var(--ok)" : "var(--bad)"}">${t.ok ? "在线" : "离线"}</span></div>
    <div class="detail tool-row" style="border:0;padding:0 0 7px 15px">${esc(t.detail || "")}</div>`).join("");
  const stats = st.stats || {};
  const events = (sum?.events || []).slice(0, 12);
  const budget = S.pulse?.budget || {};
  const feedback = sum?.feedback || {};
  const max = Math.max(1, ...(sum?.daily || []).map((d) => d.n));
  const bars = (sum?.daily || []).slice().reverse().map((d) => `<i style="height:${Math.max(3, Math.round((d.n / max) * 30))}px" title="${esc(d.date)} · ${d.n}"></i>`).join("");
  $("#modalBody", sheet).innerHTML = `
    <div class="section-label">知识库</div>
    <div class="kv"><span>来源 / 文档 / 笔记</span><b>${stats.sources || 0} / ${stats.documents || 0} / ${stats.notes || 0}</b></div>
    <div class="kv"><span>实体 / 概念 / 关系</span><b>${stats.entities || 0} / ${stats.concepts || 0} / ${stats.edges || 0}</b></div>
    <div class="kv"><span>待处理</span><b>${(S.pulse?.attention_total || 0)}</b></div>
    <div class="kv"><span>今日 tokens / 预算</span><b>${budget.used || 0} / ${budget.max ? budget.max : "未设上限"}${budget.exhausted ? "（已用尽，批量任务暂停）" : ""}</b></div>
    <div class="kv"><span>答案反馈（近 7 天）</span><b>👍 ${feedback.up || 0} · 👎 ${feedback.down || 0}</b></div>
    <div class="section-label">语义检索</div>
    <div class="setting-row"><div class="sr-main"><div class="sr-title">向量召回（RRF 第四通道）</div>
      <div class="sr-sub" id="semSub">${sem ? `${esc(sem.model)} · ${sem.vectors}/${sem.notes} 条已嵌入` : "状态读取失败"}</div></div>
      <button class="switch ${sem && sem.enabled ? "on" : ""}" id="semSwitch" aria-label="语义检索开关"></button></div>
    <div class="setting-row"><div class="sr-main"><div class="sr-title">建索引</div>
      <div class="sr-sub" id="semHint">${sem ? (sem.ready ? (sem.pending ? `还有 ${sem.pending} 条待嵌入` : "全部已嵌入") : `不可用：${esc(sem.hint)}`) : ""}</div></div>
      <button class="btn ghost" id="semIndex" ${sem && sem.ready ? "" : "disabled"}>开始</button></div>
    <div class="section-label">连接器</div>
    <div>${tools || `<div class="empty">读取失败</div>`}</div>
    <div class="section-label">使用观察（近 7 天 · 只存事件名与计数）</div>
    <div class="setting-row"><div class="sr-main"><div class="sr-title">匿名操作观察</div>
      <div class="sr-sub">用于后续优化交互；不记录正文、查询词或文件名。关闭后立即停止上报。</div></div>
      <button class="switch ${trackOn() ? "on" : ""}" id="trackSwitch" aria-label="匿名操作观察开关"></button></div>
    ${sum ? `<div class="bars">${bars}</div><div class="kv"><span>${sum.total} 个事件 · ${sum.sessions} 个会话</span><b>近 ${sum.days} 天</b></div>
      <div class="event-list">${events.map((e) => `<div class="event-row"><span>${esc(e.event)}</span><b>${e.n}</b></div>`).join("") || `<div class="empty">还没有事件</div>`}</div>` : ""}
    <div class="section-label">关于</div>
    <div class="kv"><span>版本 / 构建</span><b>v${esc(S.pulse?.version || "")} · ${esc(S.build || "")}</b></div>
    <div class="kv"><span>模型</span><b>${esc(S.pulse?.llm || "")}</b></div>
    <div class="kv"><span>访问令牌</span><b>${getToken() ? "已保存（本机）" : "未设置 / 本机免令牌"}</b></div>
    <div class="kv"><span>API 文档</span><a href="/api/docs" target="_blank" rel="noopener">/api/docs</a></div>`;
  const semSwitch = $("#semSwitch", sheet);
  if (semSwitch) semSwitch.onclick = async () => {
    const on = !semSwitch.classList.contains("on");
    semSwitch.classList.toggle("on", on);
    try {
      const r = await api("/api/semantic/toggle", { method: "POST", body: JSON.stringify({ enabled: on }) });
      $("#semSub", sheet).textContent = `${r.model} · ${r.vectors}/${r.notes} 条已嵌入`;
      toast(on ? "语义检索已开启" : "语义检索已关闭");
    } catch (e) {
      semSwitch.classList.toggle("on", !on);
      toast("切换失败：" + e.message);
    }
  };
  const semIndex = $("#semIndex", sheet);
  if (semIndex) semIndex.onclick = async () => {
    const hint = $("#semHint", sheet);
    try {
      const r = await api("/api/semantic/index", { method: "POST", body: JSON.stringify({ limit: 0 }) });
      const jobId = r.job_id;
      for (let i = 0; i < 300; i++) {
        await new Promise((res) => setTimeout(res, 1200));
        const job = await api("/api/jobs/" + jobId);
        hint.textContent = `${job.stage || "排队中"} · ${job.progress || 0}%`;
        if (job.status === "done" || job.status === "failed") {
          if (job.status === "done") {
            hint.textContent = `完成：新嵌 ${job.result?.embedded || 0}，总数 ${job.result?.total_vectors || 0}`;
            toast("语义索引已更新");
          } else {
            hint.textContent = "失败：" + (job.error || "未知错误");
          }
          break;
        }
      }
    } catch (e) { hint.textContent = "失败：" + e.message; }
  };
  $("#trackSwitch", sheet).onclick = () => {
    const on = !trackOn();
    localStorage.setItem(TRACK_KEY, on ? "1" : "0");
    if (!on) telemetryQueue = [];
    $("#trackSwitch", sheet).classList.toggle("on", on);
    toast(on ? "已开启匿名操作观察" : "已关闭匿名操作观察");
    if (on) track("telemetry_on", {});
  };
  $("#modalFoot", sheet).innerHTML = "";
  const refreshBtn = document.createElement("button");
  refreshBtn.className = "btn ghost"; refreshBtn.innerHTML = icon("refresh") + "刷新状态";
  refreshBtn.onclick = () => openSettings(true);
  const close = document.createElement("button");
  close.className = "btn primary"; close.textContent = "完成"; close.onclick = closeSheet;
  $("#modalFoot", sheet).append(refreshBtn, close);
  track("settings_open", { tools: S.tools.length });
}

/* ------------------------------------------------------------------ */
/* 抽屉 / 路由                                                         */
/* ------------------------------------------------------------------ */
function openDrawer(pane) {
  $("#drawer").classList.add("open");
  if (pane) switchPane(pane);
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").classList.remove("full");
}
function switchPane(name) {
  $$("#seg button").forEach((b) => b.classList.toggle("on", b.dataset.pane === name));
  $$(".pane").forEach((p) => p.classList.toggle("on", p.id === "pane-" + name));
  $("#drawer").classList.toggle("graph", name === "graph");
  if (name !== "graph") $("#drawer").classList.remove("full");
  track("pane_open", { pane: name });
  if (name === "graph") setTimeout(() => { if (!S.graph) loadGraph(); else { resizeGraph(); drawGraph(); } }, 80);
  if (name === "sources") loadSources();
  if (name === "notes") loadNotes();
  if (name === "conv") loadConversations();
  if (name === "inbox") loadAttention();
  if (name === "ontology") loadOntology();
}

/* ------------------------------------------------------------------ */
/* 输入 / 路由                                                         */
/* ------------------------------------------------------------------ */
const QUESTION_RE = /(什么|为什么|怎么|如何|哪些|哪个|谁|多少|是不是|有没有|能否|是否|咋|多少|多久|where|what|why|how|which|who|when)\s*[?？]?\s*$/i;
function classify(text) {
  const t = (text || "").trim();
  if (!t) return "empty";
  if (/^https?:\/\//i.test(t)) return "ingest";
  if (t.startsWith("/")) return "command";
  if (S.intent === "note") return "note";
  if (S.intent === "ask") return "ask";
  if (S.intent === "do") return "command";
  if (/[?？]\s*$/.test(t)) return "ask";
  if (t.length <= 80 && QUESTION_RE.test(t)) return "ask";
  return "note";
}
function routeHint() {
  const t = input.value.trim();
  const mode = classify(t);
  const el = $("#routeHint");
  if (!t) { el.textContent = "自动判断「记」或「问」"; return; }
  const map = {
    note: "将记为笔记（Enter 发送）",
    ask: "将提问知识库（Enter 发送）",
    ingest: "将抓取网页 / 文件",
    command: "将执行命令",
    empty: "",
  };
  el.innerHTML = map[mode] || "";
}
function setIntent(intent) {
  S.intent = intent;
  $$("#intentRow .intent").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.intent === intent)));
  localStorage.setItem("opc_intent", intent);
  if (intent === "do") { openPalette("cmd"); S.intent = "auto"; setIntent("auto"); }
  routeHint();
  track("intent_change", { intent });
}

async function route(text) {
  const t = (text || "").trim();
  if (!t) return;
  const mode = classify(t);
  if (mode === "command") {
    const [cmd, ...rest] = t.split(/\s+/);
    const arg = rest.join(" ");
    const slash = {
      "/ask": () => { ask(arg || "最近的知识库讲了什么"); },
      "/note": () => { captureNote(arg); },
      "/topic": () => promptSynth(),
      "/meeting": () => showMeetings(),
      "/sync": () => runSync(),
      "/harvest": () => runHarvest(),
      "/digest": () => runDigest(),
      "/lint": () => runLint(),
      "/graph": () => openDrawer("graph"),
      "/help": () => showHelp(),
      "/inbox": () => openDrawer("inbox"),
      "/ontology": () => openDrawer("ontology"),
      "/settings": () => openSettings(),
    };
    if (slash[cmd]) { addUser(t); track("slash_command", { cmd }); return slash[cmd](); }
    openPalette("cmd", t);
    return;
  }
  if (mode === "ingest") { ingestUrl(t); return; }
  if (mode === "ask") { ask(t); return; }
  captureNote(t);
}

async function ingestUrl(url) {
  addUser(url);
  const card = taskCard("抓取网页", "已提交");
  try {
    const r = await api("/api/ingest", { method: "POST", body: JSON.stringify({ target: url, background: true }) });
    watchJob(r.job_id, card, { title: "抓取网页", onDone: (result) => { resultCard(result, { card }); refreshPulse(); } });
    track("ingest_url", { host: (() => { try { return new URL(url).host; } catch { return ""; } })() });
  } catch (e) { setTaskState(card, "failed", e.message, 0, true); }
}

let routeTimer = null;
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
  localStorage.setItem(DRAFT_KEY, input.value);
  clearTimeout(routeTimer);
  routeTimer = setTimeout(routeHint, 120);
});
input.addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) { S.intent = "ask"; setIntent("ask"); }
    composer.requestSubmit();
  }
  if (e.key === "/" && input.value === "") { e.preventDefault(); openPalette("cmd"); }
  if (e.key === "Escape" && S.ask) stopAsk();
});
composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (sendBtn.disabled) return;
  const t = input.value;
  if (!t.trim()) return;
  if (S.ask) { stopAsk(); toast("已停止上一轮；输入保留，再按 Enter 发送"); return; }
  const mode = classify(t);
  input.value = "";
  input.style.height = "auto";
  localStorage.removeItem(DRAFT_KEY);
  sendBtn.disabled = true;
  try {
    if (mode === "note" && !navigator.onLine) {
      addUser(t); clearHero();
      const card = taskCard(t.slice(0, 48), "离线保存 · 联网后自动提交");
      addOutbox(t);
      setTaskState(card, "cancelled", "已离线保存", 0);
      taskActions(card, [{ label: "立即重试", kind: "primary", icon: "wifi", fn: () => { card.remove(); captureNote(t, { echo: false }); } }]);
      return;
    }
    await route(t);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
});

/* ------------------------------------------------------------------ */
/* 顶栏 / 全局事件                                                     */
/* ------------------------------------------------------------------ */
async function refreshPulse() {
  try {
    const t0 = performance.now();
    const p = await api("/api/pulse", { timeout: 8000 });
    S.pulse = p;
    S.build = p.build || S.build;
    S.offline = false;
    const s = p.stats || {};
    const queued = JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]").length;
    $("#pulseLine").innerHTML = `来源 ${fmtNum(s.sources)} · 实体 ${fmtNum(s.entities)} · 关系 ${fmtNum(s.edges)}${queued ? ` · <span class="offline">离线待交 ${queued}</span>` : ""}`;
    updateAttentionBadge(p.attention || {});
  } catch {
    S.offline = true;
    $("#pulseLine").innerHTML = `<span class="offline">${icon("wifi-off")} 离线</span>`;
  }
}
function updatePulseLine() {
  if (S.pulse) {
    const s = S.pulse.stats || {};
    const queued = JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]").length;
    $("#pulseLine").innerHTML = `来源 ${fmtNum(s.sources)} · 实体 ${fmtNum(s.entities)} · 关系 ${fmtNum(s.edges)}${queued ? ` · <span class="offline">离线待交 ${queued}</span>` : ""}`;
  }
}

$("#attentionBtn").onclick = () => openDrawer("inbox");
$("#searchBtn").onclick = () => openPalette("cmd");
$("#settingsBtn").onclick = () => openSettings();
$("#drawerBtn").onclick = () => openDrawer("conv");
$("#drawerClose").onclick = closeDrawer;
$("#seg").addEventListener("click", (e) => { const b = e.target.closest("button[data-pane]"); if (b) switchPane(b.dataset.pane); });
$("#mic").onclick = toggleMic;
$("#stopBtn").onclick = stopAsk;
$("#attach").onclick = () => $("#fileInput").click();
$("#fileInput").onchange = (e) => { uploadFiles(e.target.files); e.target.value = ""; };
$("#anno").onclick = async () => {
  const t = input.value.trim();
  if (!t) { openDrawer("notes"); return; }
  input.value = ""; input.style.height = "auto";
  try { await quickAnnotate(t); } catch (e) { toast("记录失败：" + e.message); }
};
$("#modalClose").onclick = closeSheet;
$$(".intent").forEach((b) => b.onclick = () => setIntent(b.dataset.intent));
$$(".ptab").forEach((b) => b.onclick = () => setPaletteMode(b.dataset.mode));
$("#paletteInput").addEventListener("input", renderPalette);
$("#paletteInput").addEventListener("keydown", (e) => {
  if (e.isComposing) return;
  if (e.key === "Escape") return closePalette();
  if (e.key === "Tab") { e.preventDefault(); setPaletteMode(S.paletteMode === "cmd" ? "search" : "cmd"); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!S.paletteItems.length) return;
    S.paletteSel = (S.paletteSel + (e.key === "ArrowDown" ? 1 : -1) + S.paletteItems.length) % S.paletteItems.length;
    $$("#paletteList .cmd").forEach((el, i) => el.classList.toggle("sel", i === S.paletteSel));
    return;
  }
  if (e.key === "Enter") {
    e.preventDefault();
    if (S.paletteItems.length) runPalette(S.paletteSel);
    else if (S.paletteMode === "search") { const q = $("#paletteInput").value.trim(); if (q) { closePalette(); ask(q); } }
  }
});
$("#palette").addEventListener("click", (e) => { if (e.target.id === "palette") closePalette(); });
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeSheet(); });

document.addEventListener("click", (e) => {
  const cite = e.target.closest ? e.target.closest("[data-cite]") : null;
  if (!cite) return;
  const turnEl = cite.closest(".turn");
  const cites = turnEl && turnEl.__cites ? turnEl.__cites : S.lastCites;
  const c = (cites || []).find((x) => String(x.n) === String(cite.dataset.cite));
  if (c) openCitation(c);
});

async function openCitation(c) {
  track("cite_click", { n: c.n, type: c.type || (c.source_id ? "chunk" : "note") });
  if (c.type === "chunk" || (c.source_id && !c.note_id)) {
    if (c.source_id) return openSource(c.source_id);
    return openSheet({ title: c.title || "原文片段", meta: c.path || "", body: `<div class="md">${renderMarkdown(c.snippet || "这条引用来自原文片段。")}</div>`, foot: [{ label: "关闭", fn: closeSheet }] });
  }
  if (c.note_id) return openNoteSheet(c.note_id);
  if (c.path) return openSheet({
    title: c.title || "引用", meta: c.path,
    body: `<div class="md">${renderMarkdown(c.snippet || "这条引用来自原文片段，去 Obsidian 里看完整记录。")}</div>`,
    foot: [{ label: "在 Obsidian 打开", kind: "primary", fn: () => openObsidian(c.path) }, { label: "关闭", fn: closeSheet }],
  });
  openSheet({ title: c.title || "引用", body: `<div class="md">${renderMarkdown(c.snippet || "没有可预览的内容。")}</div>`, foot: [{ label: "关闭", fn: closeSheet }] });
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches && e.target.matches('.row[role="button"]')) {
    e.preventDefault(); e.target.click(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette("cmd"); }
  if (e.key === "Escape") { closePalette(); closeSheet(); closeDrawer(); if (S.ask) stopAsk(); }
  if (e.key === "Tab") {
    const root = $("#modal.on") || $("#palette.open");
    if (!root) return;
    const focusable = $$('button,input,textarea,select,a[href]', root)
      .filter((el) => !el.disabled && el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});

window.addEventListener("online", () => { S.offline = false; flushOutbox(); refreshPulse(); toast("已恢复联网"); });
window.addEventListener("offline", () => { S.offline = true; updatePulseLine(); toast("当前离线：捕获会先存在本机"); });
window.addEventListener("pagehide", () => { flushTelemetry(true); });
document.addEventListener("visibilitychange", () => { if (document.hidden) flushTelemetry(true); });
window.addEventListener("error", (e) => { track("js_error", { message: String(e.message || "").slice(0, 100) }); });
window.addEventListener("unhandledrejection", (e) => { track("js_rejection", { message: String(e.reason?.message || e.reason || "").slice(0, 100) }); });
window.addEventListener("resize", () => { if (S.graph) resizeGraph(); });

/* 拖拽上传 */
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault(); dragDepth++; $("#dropZone").hidden = false; $("#field").classList.add("drag");
});
window.addEventListener("dragover", (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
window.addEventListener("dragleave", (e) => {
  if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) { $("#dropZone").hidden = true; $("#field").classList.remove("drag"); }
});
window.addEventListener("drop", (e) => {
  if (!e.dataTransfer || !e.dataTransfer.files?.length) return;
  e.preventDefault(); dragDepth = 0; $("#dropZone").hidden = true; $("#field").classList.remove("drag");
  uploadFiles(e.dataTransfer.files);
});

/* 粘贴文件 */
input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); uploadFiles(files); }
});

/* 命令面板外点关闭 */
document.addEventListener("click", (e) => {
  const trackEl = e.target.closest ? e.target.closest("[data-track]") : null;
  if (trackEl) track(trackEl.dataset.track, { id: trackEl.dataset.id || "" });
});

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
(async function boot() {
  try {
    S.intent = localStorage.getItem("opc_intent") || "auto";
    if (!["auto", "note", "ask", "do"].includes(S.intent)) S.intent = "auto";
    $$("#intentRow .intent").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.intent === S.intent)));
    hero();
    const draft = localStorage.getItem(DRAFT_KEY);
    if (draft) { input.value = draft; input.dispatchEvent(new Event("input")); }
    await refreshPulse();
    loadConversations();
    flushOutbox();
    bindGraphToolbar();
    setInterval(refreshPulse, 30000);
    // FR-815：局域网 http 不是安全上下文，麦克风会被浏览器禁用——启动即提示一次
    if (!window.isSecureContext
        && !["127.0.0.1", "localhost", "[::1]"].includes(location.hostname)
        && !sessionStorage.getItem("opc_https_hint")) {
      sessionStorage.setItem("opc_https_hint", "1");
      toast("局域网 http 下麦克风不可用：手机语音需要 HTTPS 反代", { ms: 9000 });
    }
    const params = new URLSearchParams(location.search);
    const intentParam = params.get("intent");
    if (["note", "ask", "auto"].includes(intentParam)) {
      S.intent = intentParam;
      $$("#intentRow .intent").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.intent === S.intent)));
    }
    const q = params.get("q");
    if (q) setTimeout(() => { input.value = q; composer.requestSubmit(); }, 300);
    track("app_open", { standalone: matchMedia("(display-mode: standalone)").matches });
  } catch (e) {
    toast("初始化失败：" + e.message);
  }
})();

/* Service Worker */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").then((reg) => {
    reg.addEventListener("updatefound", () => {
      const nw = reg.installing;
      if (!nw) return;
      nw.addEventListener("statechange", () => {
        if (nw.state === "installed" && navigator.serviceWorker.controller) {
          toast("新版本已就绪", { ms: 8000, action: { label: "刷新", fn: () => location.reload() } });
          track("sw_update");
        }
      });
    });
  }).catch(() => {});
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.type === "update") toast("界面已更新", { ms: 6000, action: { label: "刷新", fn: () => location.reload() } });
  });
}
