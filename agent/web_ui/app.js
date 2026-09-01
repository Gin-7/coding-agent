/* Coding Agent Web UI —— 事件流浏览器渲染器（工作区树 / 会话 / 发送-中断 / 设置弹窗） */
"use strict";

const chatCol = document.getElementById("chat-col");
const scrollEl = document.getElementById("chat-scroll");
const input = document.getElementById("input");
const btnRun = document.getElementById("btn-run");
const permissionSelect = document.getElementById("permission-select");

let running = false;
let lastAssistant = null;
let lastCommandPre = null;
let assistantRaw = "";
let turn = null;             // 当前 agent 对话合并框（指向 .turn-body，外层 .agent-turn 由 _turnEl 引用）
let turnCounter = 0;         // 主会话回合序号（与后端 RunResult 计数对齐，分叉定位用）
let currentSessionFile = ""; // 当前会话文件名（分叉 API 参数）
let lastActiveRoot = "";     // 上次活动工作区（用于文件树随工作区切换）
const toolCards = new Map();
const bgRows = new Map();    // 后台任务 task_id -> { row, status }
const subRows = new Map();   // 子agent subagent_id -> { row, status }
const subEvents = new Map(); // 子agent subagent_id -> [event dict]（回放兜底）
const subRenderState = new Map(); // 子agent subagent_id -> 详情渲染态 {col,scroll,turn,...}
let currentTaskId = null;    // 当前在详情面板打开的后台任务
let currentSubId = null;     // 当前在详情面板打开的子agent
let currentOpenFile = null;  // 当前在预览面板打开的文件（data-path，与 .file-item 对齐）
let lastCmdToolEl = null;   // 最近一次 run_command 工具卡片（命令输出归入其展开详情）
let fileDir = ".";
let browseDir = "";
let expandedRoots = new Set();
let activeRoot = "";

/* ---------- 基础 ---------- */
function setRunning(v) {
  running = v;
  btnRun.classList.toggle("running", v);
  btnRun.title = v ? "停止" : "发送";
}
function scrollToBottom(scroll) {
  scroll = scroll || scrollEl;
  const near = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 260;
  if (near) scroll.scrollTop = scroll.scrollHeight;
}
function showEmpty(show) { document.getElementById("empty").style.display = show ? "" : "none"; }

/* 上下文窗口环形指示器：根据当前 token 数与预算，更新进度环（无中心文字） */
const ctxRing = document.getElementById("ctx-ring");
const ctxRingFg = ctxRing ? ctxRing.querySelector(".ring-fg") : null;
const ctxTip = document.getElementById("ctx-tip");
const RING_CIRC = 2 * Math.PI * 10;  // r=10
/* 模型完整窗口：百万用 M、其余用 K（如 1000000 -> 1M，128000 -> 128K） */
function fmtWindow(n) {
  if (!n || n <= 0) return "0";
  if (n >= 1000000) {
    const m = n / 1000000;
    return (m % 1 === 0 ? m.toFixed(0) : m.toFixed(1)) + "M";
  }
  const k = n / 1000;
  return (k % 1 === 0 ? k.toFixed(0) : k.toFixed(1)) + "K";
}
function updateContextRing(tokens, budget, window) {
  if (!ctxRingFg || !budget || budget <= 0) return;
  // 分母选择：完整窗口已知且预算未被 cap 限制（budget≈窗口*0.9）时，用「模型完整窗口」
  // 做分母，使百分比直观表示"占窗口比例"——达 90%（安全余量 10%）即临近压缩；
  // 否则（cap 生效或窗口未知）用预算 budget 做分母（压缩在占满 budget 时触发）。
  const denom = (window && window > 0 && budget >= window * 0.8) ? window : budget;
  const pct = Math.max(0, Math.min(1, tokens / denom));
  // 环进度（dashoffset 越小越满）
  ctxRingFg.style.strokeDashoffset = String(RING_CIRC * (1 - pct));
  // 环进度固定用主题色，不随百分比变色
  ctxRingFg.style.stroke = "var(--accent)";
  // 悬停提示：仅自定义气泡（去掉原生 title 重复），格式「上下文已使用 x%（used / 窗口）」
  const pctInt = Math.round(pct * 100);
  const tip = "上下文已使用 " + pctInt + "%（" + fmtWindow(tokens) + " / " + fmtWindow(denom) + "）";
  if (ctxTip) ctxTip.textContent = tip;
}
/* 悬停：高亮环 + 上方显示提示 */
if (ctxRing) {
  ctxRing.addEventListener("mouseenter", () => { if (ctxTip) ctxTip.classList.add("show"); });
  ctxRing.addEventListener("mouseleave", () => { if (ctxTip) ctxTip.classList.remove("show"); });
}
/* ---------- 消息操作按钮（复制 / 分叉，悬浮显示） ---------- */
const ICON_COPY = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 3.5H4A1.5 1.5 0 0 0 2.5 5v6.5"/></svg>';
const ICON_FORK = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="4" cy="3.5" r="1.5"/><circle cx="4" cy="12.5" r="1.5"/><circle cx="12" cy="6" r="1.5"/><path d="M4 5v6"/><path d="M12 7.5c0 1.8-1.6 2.7-3.6 3.3"/></svg>';
const ICON_CHECK = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.2 3L13 4.5"/></svg>';

/* 复制文本到剪贴板：优先 Clipboard API，失败降级 execCommand；btn 短暂显示对勾反馈 */
async function copyText(text, btn) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (e2) {}
    document.body.removeChild(ta);
  }
  if (btn) {
    const old = btn.innerHTML;
    btn.innerHTML = ICON_CHECK;
    btn.classList.add(ok ? "ok" : "err");
    setTimeout(() => { btn.innerHTML = old; btn.classList.remove("ok", "err"); }, 1100);
  }
}

function makeActBtn(icon, title, onClick) {
  const b = document.createElement("button");
  b.className = "msg-act-btn"; b.type = "button"; b.title = title; b.innerHTML = icon;
  b.addEventListener("click", e => { e.stopPropagation(); onClick(b); });
  return b;
}

/* 从主会话第 N 个回合（body._turnIndex）分叉出新会话：后端截断事件流+消息快照，
   成功后前端切到新会话并回放分叉点之前的完整历史。 */
async function forkTurn(body) {
  const idx = body && body._turnIndex;
  if (!idx || !body._turnEl || !body._turnEl.classList.contains("done")) return;  // 未完成回合无消息快照
  if (running) { addSystem("⚠ 任务执行中无法分叉", "error"); return; }
  if (!currentSessionFile) return;
  try {
    const r = await fetch("/api/session/fork", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: currentSessionFile, turn: idx })
    });
    const d = await r.json();
    if (d.ok) {
      currentSessionFile = d.filename;
      const m = await getJSON("/api/session/events?filename=" + encodeURIComponent(d.filename));
      replayEvents(m.events || []);
      loadTree(); loadFiles();
    } else {
      addSystem("⚠ " + (d.message || "分叉失败"), "error");
    }
  } catch (e) { addSystem("⚠ 分叉请求失败", "error"); }
}

function addBubble(cls, text, parent, scroll) {
  const b = document.createElement("div");
  b.className = "bubble " + cls;
  if (text !== undefined) b.innerHTML = renderMarkdown(text);
  if (cls === "user" && text !== undefined) {
    const raw = String(text);                       // 复制原始输入，而非渲染后的 HTML
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.appendChild(makeActBtn(ICON_COPY, "复制消息", btn => copyText(raw, btn)));
    b.appendChild(bar);
  }
  (parent || chatCol).appendChild(b);
  scrollToBottom(scroll);
  return b;
}
function addSystem(text, cls, parent, scroll) {
  const b = document.createElement("div");
  b.className = "note " + (cls || "");
  b.textContent = text;
  (parent || chatCol).appendChild(b);
  scrollToBottom(scroll);
}
/* agent 回合容器：外层 .agent-turn 承载悬浮按钮条，内层 .turn-body 承载内容。
   返回 body——调用方 appendChild 到 body 即可，无需感知双层结构。
   withFork=true 时（仅主会话回合）附加分叉按钮，回合完成（RunResult）后才可点。 */
function makeTurn(parent, withFork) {
  const t = document.createElement("div");
  t.className = "agent-turn";
  const body = document.createElement("div");
  body.className = "turn-body";
  t.appendChild(body);
  const bar = document.createElement("div");
  bar.className = "msg-actions";
  bar.appendChild(makeActBtn(ICON_COPY, "复制回复", btn => copyText(body._replyText || "", btn)));
  if (withFork) {
    const forkBtn = makeActBtn(ICON_FORK, "从此回合分叉新会话", () => forkTurn(body));
    forkBtn.classList.add("btn-fork");
    bar.appendChild(forkBtn);
  }
  t.appendChild(bar);
  body._turnEl = t; body._actions = bar;
  body._replyText = "";
  (parent || chatCol).appendChild(t);
  return body;
}
function updateChatHeader(name) {
  document.getElementById("chat-session-name").textContent = name || "—";
}
function onWorkspaceChanged() {
  lastActiveRoot = activeRoot;
  fileDir = ".";
  loadFiles();
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function safeJson(o) { try { return JSON.stringify(o, null, 2); } catch (e) { return String(o); } }
async function getJSON(url) { const r = await fetch(url); return r.json(); }
function stopBackground(taskId, btn) {
  if (btn) btn.disabled = true;
  fetch("/api/background/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: taskId }) }).catch(() => {});
}
function joinPath(a, b) { return a === "." ? b : a + "/" + b; }
function parentOf(p) { const i = p.lastIndexOf("/"); return i < 0 ? "." : p.slice(0, i) || "."; }

/* ---------- 矢量图标 ---------- */
const ICON = {
  chevron: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M9 6l6 6-6 6"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" d="M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M12 5v14M5 12h14"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 19V5M5 12l7-7 7 7"/></svg>',
  wrench: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M21 6a5 5 0 0 1-6.9 4.6L7.5 17.2a2 2 0 1 1-2.8-2.8l6.6-6.6A5 5 0 0 1 19.2 2.8l-3 3 2 2 3-3A5 5 0 0 1 21 6z"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20 6L9 17l-5-5"/></svg>',
  checkCircle: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M8.5 12l2.5 2.5 4.5-4.5"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 3L2 20h20L12 3z"/><path stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M12 10v4M12 17h.01"/></svg>',
  dots: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>',
  pin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 3h6l-1 7 3 2.5V15H7v-2.5L10 10z"/></svg>',
};
function ic(name, cls) { return '<span class="ic ' + (cls || "svg14") + '">' + ICON[name] + "</span>"; }
function addIconNote(icon, text, cls, parent, scroll) {
  const b = document.createElement("div");
  b.className = "note " + (cls || "");
  b.innerHTML = ic(icon) + "<span>" + esc(text) + "</span>";
  (parent || chatCol).appendChild(b); scrollToBottom(scroll);
  return b;
}

/* ---------- 每工具的矢量图标 ---------- */
const TOOL_ICON = {
  read_file: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" d="M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"/><path fill="none" stroke="currentColor" stroke-width="1.8" d="M8 13h8M8 17h6"/></svg>',
  write_file: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M12 20h9"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
  edit_file: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M4 20h4l10-10-4-4L4 16zM13 6l4 4"/></svg>',
  undo_file: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M9 14L4 9l5-5"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M4 9h10a6 6 0 0 1 6 6v1"/></svg>',
  run_command: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M4 6l6 6-6 6M12 19h8"/></svg>',
  git_status: '<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.4" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="18" cy="6" r="2.4" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="18" r="2.4" fill="none" stroke="currentColor" stroke-width="1.8"/><path fill="none" stroke="currentColor" stroke-width="1.8" d="M6 8.4V14a4 4 0 0 0 4 4h2"/></svg>',
  git_diff: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M7 4v16M7 8h6M7 16h6M17 4v16"/></svg>',
  git_commit: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path fill="none" stroke="currentColor" stroke-width="1.8" d="M3 12h5.5M15.5 12H21"/></svg>',
  git_log: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M6 4h12M6 10h12M6 16h12"/></svg>',
  glob: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="1.8"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M21 21l-4.3-4.3"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="1.8"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M21 21l-4.3-4.3"/></svg>',
  list_dir: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>',
  finish: '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20 6L9 17l-5-5"/></svg>',
};
function toolIcon(name) { return TOOL_ICON[name] || ICON.wrench; }
function toolBrief(name, args) {
  if (!args) return "";
  for (const k of ["path", "command", "pattern", "message", "file"]) {
    if (args[k] !== undefined && args[k] !== "") {
      const v = String(args[k]);
      return v.length > 40 ? v.slice(0, 40) + "…" : v;
    }
  }
  const v = Object.values(args)[0];
  return v !== undefined ? String(v).slice(0, 40) : "";
}
function addToolInline(name, args, parent, scroll) {
  const el = document.createElement("div");
  el.className = "tool-inline";
  el.innerHTML = '<span class="tool-ic">' + toolIcon(name) + '</span><span class="tool-name">' + esc(name) + '</span>';
  if (args && Object.keys(args).length) {
    const b = document.createElement("span");
    b.className = "tool-brief";
    b.textContent = toolBrief(name, args);
    el.appendChild(b);
  }
  const caret = document.createElement("span");
  caret.className = "tool-caret";
  el.appendChild(caret);
  const details = document.createElement("div");
  details.className = "tool-details";
  const argsPre = document.createElement("pre");
  argsPre.className = "tool-args";
  argsPre.textContent = args && Object.keys(args).length ? JSON.stringify(args, null, 2) : "";
  const outPre = document.createElement("pre");
  outPre.className = "tool-out-detail";
  details.appendChild(argsPre);
  details.appendChild(outPre);
  el.appendChild(details);
  el.addEventListener("click", () => el.classList.toggle("expanded"));
  (parent || chatCol).appendChild(el);
  scrollToBottom(scroll);
  el._out = outPre;
  return el;
}

/* ---------- 轻量 markdown 渲染（零依赖，逐行解析） ---------- */
function renderMarkdown(text) {
  // 1) 换行归一：CRLF→LF；3+ 连续换行压成 2（保留段落分隔，去掉冗余空行）
  let src = String(text)
    .replace(/\r\n?/g, "\n")
    .replace(/\n{3,}/g, "\n\n");
  // 2) 去掉「夹在两个列表项之间」的空行，避免每个列表项被拆成独立 <ul>
  src = src.replace(/^([-*] .*)\n\n(?=[-*] )/gm, "$1\n")
           .replace(/^(\d+\. .*)\n\n(?=\d+\. )/gm, "$1\n");

  const lines = src.split("\n");
  const blocks = [];   // 代码块原文（稍后还原，避免被行内规则破坏）

  // 行内：先转义，再套 加粗 / 斜体 / 行内代码 / 链接
  const inline = (raw) => esc(raw)
    .replace(/`([^`]+)`/g, '<code class="md-inline">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  let html = "";
  let para = [];                 // 当前段落的若干行
  let listBuf = [], listOrdered = false;
  const flushPara = () => {
    if (!para.length) return;
    const t = para.join("\n").trim();
    if (t) html += "<p>" + inline(t).replace(/\n/g, "<br>") + "</p>";
    para = [];
  };
  const flushList = () => {
    if (!listBuf.length) return;
    html += (listOrdered ? '<ol class="md-ul">' : '<ul class="md-ul">')
          + listBuf.map(li => "<li>" + inline(li) + "</li>").join("")
          + (listOrdered ? "</ol>" : "</ul>");
    listBuf = []; listOrdered = false;
  };
  const isItem = (l) => /^[-*] /.test(l) || /^\d+\.\s/.test(l);
  // 表格列对齐解析（:--- / :---: / ---: / ---）
  const parseTableAlign = (sep) => sep.replace(/^\s*\|?/, "").replace(/\|?\s*$/, "").split("|").map(c => {
    c = c.trim(); if (!c) return "";
    const l = c.startsWith(":"), r = c.endsWith(":");
    if (l && r) return "center"; if (r) return "right"; if (l) return "left"; return "";
  });
  const alignAttr = (a, k) => a[k] ? ` style="text-align:${a[k]}"` : "";

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].replace(/\s+$/, "");        // 去尾部空白
    const fence = line.match(/^\s*(```|~~~)(.*)$/);
    if (fence) {                                       // 围栏代码块（支持前导空格与 ~~~ 波浪围栏）
      flushPara(); flushList();
      const closeRe = /^\s*(```|~~~)\s*$/;
      const buf = [];
      while (++i < lines.length && !closeRe.test(lines[i].replace(/\s+$/, ""))) buf.push(lines[i]);
      blocks.push(buf.join("\n"));
      html += "\u0000B" + (blocks.length - 1) + "\u0000";
      continue;
    }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {       // 标题
      flushPara(); flushList();
      const h = Math.min(m[1].length + 1, 4);
      html += `<h${h}>${inline(m[2])}</h${h}>`;
      continue;
    }
    // 表格（GFM）：表头行 + 分隔行（|---|）+ 数据行
    const tblM = line.match(/^\s*\|(.+)\|\s*$/);
    const isSepRow = (l) => /^\s*\|?[\s:|-]+\|?\s*$/.test(l) && l.includes("-");
    if (tblM && i + 1 < lines.length && isSepRow(lines[i + 1].replace(/\s+$/, ""))) {
      flushPara(); flushList();
      const headers = tblM[1].split("|").map(c => c.trim());
      const aligns = parseTableAlign(lines[i + 1]);
      const rows = [];
      let j = i + 2;
      while (j < lines.length) {
        const mm = lines[j].replace(/\s+$/, "").match(/^\s*\|(.+)\|\s*$/);
        if (!mm) break;
        rows.push(mm[1].split("|").map(c => c.trim()));
        j++;
      }
      let t = '<table class="md-table"><thead><tr>'
        + headers.map((h, k) => `<th${alignAttr(aligns, k)}>${inline(h)}</th>`).join("")
        + '</tr></thead><tbody>';
      for (const r of rows) t += '<tr>' + r.map((c, k) => `<td${alignAttr(aligns, k)}>${inline(c)}</td>`).join("") + '</tr>';
      t += '</tbody></table>';
      html += t;
      i = j - 1;   // 跳过已处理行（循环尾 i++ 会再 +1）
      continue;
    }
    if (isItem(line)) {                                // 列表项（连续项合并为一个列表）
      flushPara();
      const ordered = /^\d+\.\s/.test(line);
      if (listBuf.length && ordered !== listOrdered) flushList();
      listOrdered = ordered;
      listBuf.push(line.replace(/^[-*]\s+/, "").replace(/^\d+\.\s+/, ""));
      continue;
    }
    if (/^( {4,}|\t)/.test(line) && !isItem(line)) {   // 缩进代码块（4 空格或 tab，无围栏）
      flushPara(); flushList();
      const buf = [];
      const dedent = (l) => l.startsWith("\t") ? l.slice(1) : l.replace(/^ {1,4}/, "");
      while (i < lines.length) {
        const cur = lines[i];
        const trimmed = cur.replace(/\s+$/, "");
        if (trimmed === "") { buf.push(""); i++; continue; }   // 空行保留在代码块内
        if (/^( {4,}|\t)/.test(cur)) { buf.push(dedent(cur)); i++; continue; }
        break;                                                 // 非缩进非空行结束代码块
      }
      while (buf.length && buf[buf.length - 1] === "") buf.pop();
      blocks.push(buf.join("\n"));
      html += "\u0000B" + (blocks.length - 1) + "\u0000";
      continue;
    }
    if (line.trim() === "") { flushPara(); flushList(); continue; }  // 空行 = 段落边界
    para.push(line);
  }
  flushPara(); flushList();

  // 3) 还原代码块（仅转义，不再做行内 markdown）
  html = html.replace(/\u0000B(\d+)\u0000/g, (m, i) => '<pre class="md-code">' + esc(blocks[+i]) + "</pre>");
  return html;
}
function timeAgo(ms) {
  if (!ms) return "";
  const d = Date.now() - ms;
  if (d < 60 * 1000) return "刚刚";
  if (d < 3600 * 1000) return Math.floor(d / 60000) + " 分钟前";
  if (d < 86400 * 1000) return Math.floor(d / 3600000) + " 小时前";
  return Math.floor(d / 86400000) + " 天前";
}
/* ---------- 事件渲染（实时 / 回放共用同一函数） ---------- */
/* 实时 SSE 与回放（重放全量事件日志）都走这里，保证两种视图 100% 一致。
   opts.replay=true 时屏蔽交互副作用（审批弹窗改为注记、不在每次 RunResult
   重刷文件树），并去掉流式“运行态”以呈现静态完成样式。 */
function render(ev, opts) {
  const replay = !!(opts && opts.replay);
  switch (ev.type) {
    case "UserMessage":
      showEmpty(false);
      addBubble("user", ev.content);
      turn = makeTurn(null, true);      // 每个 agent 对话合并为一个框（带复制/分叉按钮条）
      turn._turnIndex = ++turnCounter;  // 回合序号与后端 RunResult 计数对齐（分叉定位用）
      lastAssistant = null; lastCommandPre = null;
      break;
    case "TextDelta":
      if (!lastAssistant) {
        lastAssistant = document.createElement("div");
        lastAssistant.className = "assistant-text" + (replay ? "" : " running");
        (turn || chatCol).appendChild(lastAssistant);
        assistantRaw = "";
        if (turn && turn._replyText) turn._replyText += "\n\n";   // 回合内多个文本块之间补空行
      }
      assistantRaw += ev.text;
      if (turn) turn._replyText += ev.text;
      lastAssistant.innerHTML = renderMarkdown(assistantRaw);
      scrollToBottom();
      break;
    case "StepEvent":
      lastAssistant = null; lastCommandPre = null;   // 重置分段，使每步文本独立成块
      addSystem("step " + ev.step + "/" + ev.max_steps, "step", turn);
      break;
    case "ContextUsageEvent":
      updateContextRing(ev.tokens, ev.budget, ev.window);
      break;
    case "ToolCallEvent": {
      if (ev.name === "finish") break;   // finish 以正文气泡渲染，不画工具卡片
      const el = addToolInline(ev.name, ev.arguments, turn || chatCol);
      toolCards.set(ev.call_id, el);
      if (ev.name === "run_command") lastCmdToolEl = el;   // 命令实时输出归入该卡片展开详情
      scrollToBottom();
      break;
    }
    case "CommandOutput":
      // 命令实时输出归入 run_command 工具卡片的展开详情（不在消息气泡中直接显示）
      if (lastCmdToolEl && lastCmdToolEl._out) {
        lastCmdToolEl._out.textContent += ev.text; scrollToBottom();
      } else {
        if (!lastCommandPre) {
          lastCommandPre = document.createElement("pre");
          lastCommandPre.className = "cmd-output";
          (turn || chatCol).appendChild(lastCommandPre);
        }
        lastCommandPre.textContent += ev.text; scrollToBottom();
      }
      break;
    case "ToolResultEvent": {
      if (ev.name === "finish") { lastCmdToolEl = null; lastCommandPre = null; break; }  // finish 无工具卡片
      const el = toolCards.get(ev.call_id);
      if (el) {
        // 成功不单独着色（与工具描述同色），仅失败标红；结果只在展开详情中显示
        if (!ev.ok) el.classList.add("fail");
        if (el._out) {
          const s = el._out.textContent || "";
          if (ev.name === "run_command" && s.length > 0) {
            // 命令输出已流式写入，仅补一行退出状态，避免与 stdout 重复
            const st = String(ev.output || "").split("\n")[0];
            el._out.textContent = s + (st ? "\n" + st : "");
          } else {
            el._out.textContent = ev.output || s;
          }
        }
      }
      lastCmdToolEl = null;
      lastCommandPre = null;
      break;
    }
    case "TrimmedEvent": addSystem("[上下文] 预算紧张，已裁剪最老的 " + ev.rounds + " 轮工具调用", "", turn); break;
    case "CompactedEvent":
      addSystem(ev.summarized ? "[上下文] 已把早期对话压缩为摘要"
                              : "[上下文] 已丢弃早期对话记录", "", turn);
      break;
    case "ErrorEvent": addIconNote("warn", ev.message, "error", turn); break;
    case "FinishEvent": {
      // finish 不再作为工具卡片，直接以正文气泡呈现（与主体内容同款样式、支持 markdown）
      const d = document.createElement("div");
      d.className = "assistant-text";
      d.innerHTML = renderMarkdown(ev.summary || "");
      (turn || chatCol).appendChild(d); scrollToBottom();
      if (turn && ev.summary) turn._replyText += (turn._replyText ? "\n\n" : "") + ev.summary;
      break;
    }
    case "Notice": addSystem(ev.message, "", turn); break;
    // 后台任务：聊天区只留一条轻注记，详情统一在右侧栏列表 + 预览面板承载
    case "BackgroundStarted": {
      addBgRow({ task_id: ev.task_id, command: ev.command, status: "running" });
      if (!replay) addSystem("[后台] " + (ev.command || ev.task_id) + " 已启动", "", turn);
      break;
    }
    case "BackgroundOutput": {
      const be = bgRows.get(ev.task_id);
      if (be) be.output += ev.text;   // 缓存输出，供服务端重启后兜底查看
      if (currentTaskId === ev.task_id && !document.getElementById("task-detail").hidden) {
        const out = document.getElementById("td-out");
        out.textContent += ev.text; out.scrollTop = out.scrollHeight;
      }
      break;
    }
    case "BackgroundStatus": {
      const tail = ev.exit_code != null ? "（退出码 " + ev.exit_code + "）" : "";
      setBgStatus(ev.task_id, ev.status, ev.exit_code);
      if (!replay) addSystem("[后台] " + ev.task_id + " " + (ev.status === "stopped" ? "已停止" : ev.status) + tail, ev.status === "done" ? "ok" : "", turn);
      break;
    }
    // 子 agent：聊天区只留轻注记，运行态/对话在右侧栏列表 + 预览面板承载
    case "SubagentStarted": {
      addSubRow(ev);
      if (!replay) addSystem("[子agent " + ev.subagent_id + "] " + (ev.name || ""), "", turn);
      break;
    }
    case "SubagentEvent": {
      if (subEvents.has(ev.subagent_id)) subEvents.get(ev.subagent_id).push(ev.event);
      if (currentSubId === ev.subagent_id && !document.getElementById("sub-detail").hidden) {
        renderSubagent(ev.subagent_id, ev.event);
      }
      break;
    }
    case "SubagentStatus": {
      setSubStatus(ev.subagent_id, ev.status);
      if (!replay) addSystem("[子agent " + ev.subagent_id + "] " + (ev.status || ""), ev.status === "done" ? "ok" : (ev.status === "error" ? "error" : ""), turn);
      break;
    }
    case "AskConfirm":
      // 回放时审批已发生，仅作注记；实时才弹窗等待
      if (replay) addIconNote("warn", "审批点：" + ev.name + (ev.desc ? " — " + ev.desc : ""), "note", turn);
      else showConfirm(ev.name, ev.desc);
      break;
    case "SessionsChanged":
      if (!replay) loadTree();
      break;
    case "RunResult":
      if (!replay) {
        setRunning(false);
        if (lastAssistant) lastAssistant.classList.remove("running");
      }
      if (ev.status === "finished") addSystem("[完成]", "ok", turn);
      else if (ev.status !== "interrupted") addSystem("[" + ev.status + "] " + (ev.message || ""), "error", turn);
      if (ev.steps != null) {
        const u = ev.usage || {};
        addSystem("[统计] 步骤 " + ev.steps + " | 输入 " + (u.prompt || 0) + " / 输出 " + (u.completion || 0) + " tokens", "", turn);
      }
      if (turn && turn._turnEl) turn._turnEl.classList.add("done");   // 回合完成：消息快照已落盘，启用分叉
      lastAssistant = null; lastCommandPre = null; turn = null; assistantRaw = "";
      lastCmdToolEl = null;
      if (!replay) { loadTree(); loadFiles(); }
      break;
  }
}

/* ---------- 回放：重放全量事件日志，复用 render ---------- */
function replayEvents(events) {
  chatCol.innerHTML = "";
  updateContextRing(0, 1);  // 先归零，回放中由 ContextUsageEvent 刷新到真实使用率
  turn = null; lastAssistant = null; lastCommandPre = null; assistantRaw = "";
  turnCounter = 0;   // 回放从零重建，回合序号同步归零（分叉定位对齐后端 RunResult 计数）
  lastCmdToolEl = null; toolCards.clear();
  toolCards.clear();
  // 清空上一会话的任务列表（后台任务 / 子agent 随会话重建）
  bgRows.clear(); subRows.clear(); subEvents.clear(); subRenderState.clear();
  currentTaskId = null; currentSubId = null; currentOpenFile = null;
  document.getElementById("bg-list").innerHTML = "";
  document.getElementById("sub-list").innerHTML = "";
  updateCount("bg"); updateCount("sub");
  for (const ev of events || []) {
    if (ev && ev.type === "MessagesDump") continue;  // 仅快照，渲染用不到
    render(ev, { replay: true });
  }
  if (lastAssistant) lastAssistant.classList.remove("running");
  scrollEl.scrollTop = scrollEl.scrollHeight;   // 回放结束直接跳到底部（切换会话 / 初始加载均定位到最新消息）
  loadFiles();  // 仅刷新文件面板；会话树由调用方负责（避免递归触发再次回放）
}

/* ---------- 右栏任务区域（后台任务 / 子agent 列表 + 详情面板） ---------- */
function updateCount(kind) {
  const n = kind === "bg" ? bgRows.size : subRows.size;
  document.getElementById(kind + "-count").textContent = String(n);
}
function autoOpenRegion(kind) {
  const body = document.getElementById(kind + "-body");
  if (body.hidden) { body.hidden = false; body.previousElementSibling.classList.add("open"); }
}
function addBgRow(task) {
  const id = task.task_id;
  if (bgRows.has(id)) { setBgStatus(id, task.status, task.exit_code); return; }
  const row = document.createElement("div");
  row.className = "task-item bg-item";
  row.dataset.id = id;
  row.innerHTML = '<span class="ti-ic">⌁</span><span class="ti-cmd"></span><span class="ti-status running">运行中</span>';
  row.querySelector(".ti-cmd").textContent = task.command || id;
  row.title = task.command || id;
  row.addEventListener("click", () => openTaskDetail(id));
  document.getElementById("bg-list").appendChild(row);
  bgRows.set(id, { row, statusEl: row.querySelector(".ti-status"),
                   command: task.command || id, output: "", rawStatus: task.status || "running", exit: task.exit_code });
  updateCount("bg"); autoOpenRegion("bg");
}
function setBgStatus(id, status, exit) {
  const e = bgRows.get(id);
  const tail = exit != null ? "（退出码 " + exit + "）" : "";
  const done = status === "done" || status === "stopped";
  const text = (status === "stopped" ? "已停止" : status === "done" ? "已完成" : status) + tail;
  if (e) { e.statusEl.textContent = text; e.statusEl.className = "ti-status " + (done ? "ended" : "running"); e.rawStatus = status; e.exit = exit; }
  if (currentTaskId === id && !document.getElementById("task-detail").hidden) {
    const s = document.getElementById("td-status");
    s.textContent = text; s.className = "bg-status " + (done ? "ended" : "running");
    const stop = document.getElementById("td-stop"); stop.disabled = true; stop.style.display = "none";
  }
}
function addSubRow(ev) {
  const id = ev.subagent_id;
  if (subRows.has(id)) { setSubStatus(id, ev.status); return; }
  const row = document.createElement("div");
  row.className = "task-item sub-item";
  row.dataset.id = id;
  row.innerHTML = '<span class="ti-ic">◈</span><span class="ti-cmd"></span><span class="ti-status running">运行中</span>';
  row.querySelector(".ti-cmd").textContent = ev.name || id;
  row.title = ev.prompt || ev.name || id;
  row.addEventListener("click", () => openSubDetail(id));
  document.getElementById("sub-list").appendChild(row);
  subRows.set(id, { row, status: row.querySelector(".ti-status") });
  subEvents.set(id, []);
  updateCount("sub"); autoOpenRegion("sub");
}
function setSubStatus(id, status) {
  const e = subRows.get(id);
  const map = { done: "已完成", error: "失败", interrupted: "已中断", running: "运行中" };
  const text = map[status] || status;
  const done = status !== "running";
  if (e) { e.status.textContent = text; e.status.className = "ti-status " + (done ? "ended" : "running"); }
  if (currentSubId === id && !document.getElementById("sub-detail").hidden) {
    const st = subRenderState.get(id);
    if (st) addSystem("[状态] " + text, done ? "ok" : "error", st.col, st.scroll);
  }
}
function ensureDetailOpen() {
  if (!rightPanelEl.classList.contains("view-open")) {
    localStorage.setItem(RP_W_KEY, String(currentRpWidth()));
  }
  rightPanelEl.classList.add("view-open");
  ensureRpOpenMinWidth();
}
function showDetail(kind) {
  document.getElementById("file-view").hidden = kind !== "file";
  document.getElementById("task-detail").hidden = kind !== "task";
  document.getElementById("sub-detail").hidden = kind !== "sub";
  document.getElementById("rp-view").hidden = false;
  ensureDetailOpen();
}
// 高亮当前选中项：清除文件树 / 任务列表里所有 .active，再点亮 row（可为 null 表示全部取消）
function markActive(row) {
  document.querySelectorAll(".file-item.active, .task-item.active").forEach(x => x.classList.remove("active"));
  if (row) row.classList.add("active");
}
function closeDetailPanel() {
  // 保存打开态宽度（用内联终值，避开 width .2s 过渡的中间帧），供下次打开文件沿用
  setRpWidth(RP_W_OPEN_KEY, currentRpWidth());
  document.getElementById("file-view").hidden = true;
  document.getElementById("task-detail").hidden = true;
  document.getElementById("sub-detail").hidden = true;
  document.getElementById("rp-view").hidden = true;
  rightPanelEl.classList.remove("view-open");
  const closedW = getRpWidth(RP_W_KEY, null);
  if (closedW !== null) rightPanelEl.style.width = closedW + "px";
  currentTaskId = null; currentSubId = null; currentOpenFile = null;
  markActive(null);
  localStorage.removeItem(RP_VIEW_OPEN_KEY);
}
async function openTaskDetail(id) {
  currentTaskId = id; currentSubId = null; currentOpenFile = null;
  markActive(bgRows.get(id) ? bgRows.get(id).row : null);
  let task = null;
  try {
    const d = await getJSON("/api/background/tasks");
    task = (d.tasks || []).find(t => t.task_id === id);
  } catch (e) { }
  // 服务端重启后内存态丢失时的兜底：用本会话流式缓存渲染（命令/输出/状态来自回放事件）
  if (!task) {
    const c = bgRows.get(id);
    if (c) task = { task_id: id, command: c.command, status: c.rawStatus || "done",
                    exit_code: c.exit, output: c.output };
  }
  if (!task) return;
  document.getElementById("td-cmd").textContent = task.command || id;
  const out = document.getElementById("td-out");
  out.textContent = task.output || "";
  const stop = document.getElementById("td-stop");
  const done = task.status === "done" || task.status === "stopped";
  stop.style.display = done ? "none" : "";
  stop.disabled = done;
  stop.onclick = () => stopBackground(id, stop);
  const s = document.getElementById("td-status");
  s.textContent = done ? (task.status === "stopped" ? "已停止" : "已完成") : "运行中";
  s.className = "bg-status " + (done ? "ended" : "running");
  showDetail("task");
  out.scrollTop = out.scrollHeight;
}
async function openSubDetail(id) {
  currentSubId = id; currentTaskId = null; currentOpenFile = null;
  markActive(subRows.get(id) ? subRows.get(id).row : null);
  try {
    const d = await getJSON("/api/subagents");
    const sub = (d.subagents || []).find(s => s.subagent_id === id);
    const events = (sub && sub.events) ? sub.events : (subEvents.get(id) || []);
    const sdCol = document.getElementById("sd-col");
    sdCol.innerHTML = "";
    const st = { col: sdCol, scroll: sdCol, turn: null, lastAssistant: null,
                 lastCommandPre: null, assistantRaw: "", lastCmdToolEl: null, toolCards: new Map() };
    subRenderState.set(id, st);
    document.getElementById("sd-name").textContent = (sub && sub.name) || id;
    showDetail("sub");
    if (sub && sub.prompt) { addBubble("user", sub.prompt, sdCol, sdCol); st.turn = makeTurn(sdCol); }
    for (const ev of events) renderSubagent(id, ev);
  } catch (e) { }
}
/* 子 agent 详情：对话式渲染（与主 agent 同一套 CSS / 辅助函数） */
function renderSubagent(subId, ev) {
  const st = subRenderState.get(subId);
  if (!st) return;
  const col = st.col, scroll = st.scroll;
  switch (ev.type) {
    case "UserMessage":
      st.lastAssistant = null; st.lastCommandPre = null;
      addBubble("user", ev.content, col, scroll);
      st.turn = makeTurn(col);
      break;
    case "TextDelta":
      if (!st.lastAssistant) {
        st.lastAssistant = document.createElement("div");
        st.lastAssistant.className = "assistant-text";
        (st.turn || col).appendChild(st.lastAssistant);
        st.assistantRaw = "";
      }
      st.assistantRaw += ev.text;
      st.lastAssistant.innerHTML = renderMarkdown(st.assistantRaw);
      scrollToBottom(scroll);
      break;
    case "StepEvent":
      st.lastAssistant = null; st.lastCommandPre = null;
      // 与主 agent 保持一致：step 注记写入同一对话框（st.turn）内、与内容交错，而非独立追加到列尾
      addSystem("step " + ev.step + "/" + ev.max_steps, "step", st.turn || col, scroll);
      break;
    case "ToolCallEvent": {
      if (ev.name === "finish") break;
      const el = addToolInline(ev.name, ev.arguments, st.turn || col, scroll);
      st.toolCards.set(ev.call_id, el);
      if (ev.name === "run_command") st.lastCmdToolEl = el;
      break;
    }
    case "CommandOutput":
      if (st.lastCmdToolEl && st.lastCmdToolEl._out) {
        st.lastCmdToolEl._out.textContent += ev.text; scrollToBottom(scroll);
      } else {
        if (!st.lastCommandPre) {
          st.lastCommandPre = document.createElement("pre");
          st.lastCommandPre.className = "cmd-output";
          (st.turn || col).appendChild(st.lastCommandPre);
        }
        st.lastCommandPre.textContent += ev.text; scrollToBottom(scroll);
      }
      break;
    case "ToolResultEvent": {
      if (ev.name === "finish") { st.lastCmdToolEl = null; st.lastCommandPre = null; break; }
      const el = st.toolCards.get(ev.call_id);
      if (el) {
        if (!ev.ok) el.classList.add("fail");
        if (el._out) {
          const s = el._out.textContent || "";
          if (ev.name === "run_command" && s.length > 0) {
            const stt = String(ev.output || "").split("\n")[0];
            el._out.textContent = s + (stt ? "\n" + stt : "");
          } else { el._out.textContent = ev.output || s; }
        }
      }
      st.lastCmdToolEl = null; st.lastCommandPre = null;
      break;
    }
    case "FinishEvent": {
      const d = document.createElement("div");
      d.className = "assistant-text";
      d.innerHTML = renderMarkdown(ev.summary || "");
      (st.turn || col).appendChild(d); scrollToBottom(scroll);
      break;
    }
    case "ErrorEvent": addIconNote("warn", ev.message, "error", col, scroll); break;
    case "Notice": addSystem(ev.message, "", col, scroll); break;
    case "TrimmedEvent": addSystem("[上下文] 裁剪最老 " + ev.rounds + " 轮", "", col, scroll); break;
    case "CompactedEvent": addSystem(ev.summarized ? "[上下文] 压缩为摘要" : "[上下文] 丢弃早期消息", "", col, scroll); break;
  }
}

/* ---------- 侧边栏工作区 / 会话树 ---------- */
let dataLoadedOnce = false;
const archivedOpen = new Set();  // 已归档折叠区的展开状态（按工作区 root）
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

/* ---------- 树节点三点菜单（工作区 / 会话共用，视觉与权限下拉一致） ---------- */
const ctxMenu = document.getElementById("ctx-menu");
function closeCtxMenu() { if (ctxMenu) ctxMenu.hidden = true; }
function openCtxMenu(items, anchor) {
  ctxMenu.innerHTML = "";
  for (const it of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "cb-opt" + (it.danger ? " danger" : "");
    b.textContent = it.label;
    b.addEventListener("click", e => {
      e.stopPropagation();
      if (it.confirm && !b.dataset.armed) {
        b.dataset.armed = "1";  // 危险操作二次确认：再点一次才执行
        b.textContent = it.confirm;
        b.classList.add("armed");
        return;
      }
      closeCtxMenu();
      if (it.onClick) it.onClick();
    });
    ctxMenu.appendChild(b);
  }
  ctxMenu.hidden = false;
  // 定位：锚点下方右对齐；放不下则翻转到上方
  const r = anchor.getBoundingClientRect();
  const mw = ctxMenu.offsetWidth, mh = ctxMenu.offsetHeight;
  let top = r.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
  let left = Math.max(8, r.right - mw);
  if (left + mw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - mw - 8);
  ctxMenu.style.top = top + "px";
  ctxMenu.style.left = left + "px";
}
document.addEventListener("mousedown", e => { if (ctxMenu && !ctxMenu.hidden && !ctxMenu.contains(e.target)) closeCtxMenu(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && ctxMenu && !ctxMenu.hidden) closeCtxMenu(); });
window.addEventListener("blur", closeCtxMenu);

/* ---------- 内联重命名（工作区 / 会话共用）：Enter 提交，Esc / 失焦取消 ---------- */
function beginInlineEdit(nameEl, current, onCommit) {
  const inp = document.createElement("input");
  inp.className = "inline-rename";
  inp.value = current;
  let finished = false;
  const done = commit => {
    if (finished) return;
    finished = true;
    const v = inp.value.trim();
    if (commit && v && v !== current) onCommit(v);
    else nameEl.textContent = current;
  };
  nameEl.textContent = "";
  nameEl.appendChild(inp);
  inp.focus();
  inp.select();
  inp.addEventListener("keydown", e => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); done(true); }
    else if (e.key === "Escape") done(false);
  });
  inp.addEventListener("blur", () => done(false));
  inp.addEventListener("click", e => e.stopPropagation());
}

/* ---------- 工作区 / 会话管理操作 ---------- */
async function renameWorkspace(ws, name) {
  const d = await postJSON("/api/workspace/rename", { path: ws.root, name });
  if (d.ok) loadTree(); else addSystem("⚠ " + (d.message || "重命名失败"), "error");
}
async function deleteWorkspace(ws) {
  const d = await postJSON("/api/workspace/delete", { path: ws.root });
  if (d.ok) { dataLoadedOnce = false; loadTree(); }  // 可能切回默认工作区，重放当前会话
  else addSystem("⚠ " + (d.message || "删除失败"), "error");
}
async function renameSession(ws, s, name) {
  const d = await postJSON("/api/session/rename", { root: ws.root, filename: s.filename, name });
  if (d.ok) loadTree(); else { addSystem("⚠ " + (d.message || "重命名失败"), "error"); loadTree(); }
}
async function flagSession(url, ws, s, v) {
  const key = url.endsWith("/pin") ? "pinned" : "archived";
  const d = await postJSON(url, { root: ws.root, filename: s.filename, [key]: v });
  if (!d.ok) addSystem("⚠ " + (d.message || "操作失败"), "error");
  loadTree();
}

/* 会话行：名字 +（置顶标识）+ 相对时间，悬浮时时间让位给三点菜单按钮 */
function makeSessionRow(ws, s) {
  const se = document.createElement("div");
  se.className = "tree-session" + ((ws.is_active && s.filename === ws.active) ? " active" : "") + (s.archived ? " archived" : "");
  const name = document.createElement("span");
  name.className = "session-name";
  name.textContent = s.name;
  se.appendChild(name);
  if (s.pinned) {
    const pin = document.createElement("span");
    pin.className = "session-pin";
    pin.title = "已置顶";
    pin.innerHTML = ICON.pin;
    se.appendChild(pin);
  }
  const time = document.createElement("span");
  time.className = "session-time";
  time.textContent = s.mtime ? timeAgo(s.mtime * 1000) : "";
  se.appendChild(time);
  const gear = document.createElement("button");
  gear.type = "button";
  gear.className = "session-gear";
  gear.title = "会话选项";
  gear.innerHTML = ICON.dots;
  gear.addEventListener("click", e => {
    e.stopPropagation();
    const items = [{ label: "重命名", onClick: () => beginInlineEdit(name, s.name, v => renameSession(ws, s, v)) }];
    if (!s.archived) {
      items.push({ label: s.pinned ? "取消置顶" : "置顶", onClick: () => flagSession("/api/session/pin", ws, s, !s.pinned) });
      items.push({ label: "归档", onClick: () => flagSession("/api/session/archive", ws, s, true) });
    } else {
      items.push({ label: "取消归档", onClick: () => flagSession("/api/session/archive", ws, s, false) });
    }
    openCtxMenu(items, gear);
  });
  se.appendChild(gear);
  se.title = s.filename;
  se.addEventListener("click", () => selectSession(ws.root, s.filename));
  return se;
}

async function loadTree() {
  try {
    const list = await getJSON("/api/workspaces");
    const tree = document.getElementById("tree");
    tree.innerHTML = "";
    for (const ws of list) {
      if (ws.is_active) activeRoot = ws.root;
      const open = expandedRoots.has(ws.root);
      const group = document.createElement("div");
      const header = document.createElement("div");
      header.className = "tree-ws" + (open ? " open" : "");
      header.innerHTML =
        '<span class="caret">' + ICON.chevron + "</span>" +
        '<span class="ws-ic">' + ICON.folder + "</span>" +
        '<span class="ws-name">' + esc(ws.name) + "</span>" +
        '<button type="button" class="ws-gear" title="工作区选项">' + ICON.dots + "</button>" +
        '<button type="button" class="ws-add" title="新建会话">' + ICON.plus + "</button>";
      const wsNameEl = header.querySelector(".ws-name");
      header.addEventListener("click", () => {
        const o = expandedRoots.has(ws.root);
        if (o) expandedRoots.delete(ws.root); else expandedRoots.add(ws.root);
        header.classList.toggle("open", !o);
        sessionsEl.style.display = !o ? "block" : "none";
      });
      header.querySelector(".ws-add").addEventListener("click", e => {
        e.stopPropagation();
        newSessionInWorkspace(ws.root);
      });
      header.querySelector(".ws-gear").addEventListener("click", e => {
        e.stopPropagation();
        openCtxMenu([
          { label: "重命名", onClick: () => beginInlineEdit(wsNameEl, ws.name, v => renameWorkspace(ws, v)) },
          { label: "删除工作区", danger: true, confirm: "确认删除？", onClick: () => deleteWorkspace(ws) },
        ], e.currentTarget);
      });
      const sessionsEl = document.createElement("div");
      sessionsEl.className = "tree-sessions";
      sessionsEl.style.display = open ? "block" : "none";
      const sessions = ws.sessions || [];
      const live = sessions.filter(s => !s.archived);
      const archived = sessions.filter(s => s.archived);
      if (live.length === 0) {
        const empty = document.createElement("div");
        empty.className = "tree-empty";
        empty.textContent = "无会话";
        sessionsEl.appendChild(empty);
      }
      for (const s of live) sessionsEl.appendChild(makeSessionRow(ws, s));
      if (archived.length) {
        // 归档区：默认折叠，点击标题展开 / 收起
        const head = document.createElement("div");
        head.className = "tree-archived-head" + (archivedOpen.has(ws.root) ? " open" : "");
        head.innerHTML = '<span class="caret">' + ICON.chevron + '</span><span class="ah-title">已归档</span><span class="ah-count">' + archived.length + "</span>";
        const archivedEl = document.createElement("div");
        archivedEl.className = "tree-archived";
        archivedEl.style.display = archivedOpen.has(ws.root) ? "block" : "none";
        head.addEventListener("click", () => {
          const o = archivedOpen.has(ws.root);
          if (o) archivedOpen.delete(ws.root); else archivedOpen.add(ws.root);
          head.classList.toggle("open", !o);
          archivedEl.style.display = !o ? "block" : "none";
        });
        for (const s of archived) archivedEl.appendChild(makeSessionRow(ws, s));
        sessionsEl.appendChild(head);
        sessionsEl.appendChild(archivedEl);
      }
      group.appendChild(header); group.appendChild(sessionsEl);
      tree.appendChild(group);
    }
    if (!dataLoadedOnce) {
      dataLoadedOnce = true;
      const d = await getJSON("/api/workspace");
      currentSessionFile = d.active || "";   // 首次加载记录当前会话（分叉 API 需要）
      const m = d.active ? await getJSON("/api/session/events?filename=" + encodeURIComponent(d.active)) : null;
      replayEvents(m ? m.events : []);
    }
    // 会话名头部 + 文件树随工作区切换
    const aw = list.find(w => w.is_active);
    const as = aw && (aw.sessions || []).find(s => s.filename === aw.active);
    if (aw && aw.active) currentSessionFile = aw.active;
    updateChatHeader(as ? as.name : "—");
    if (activeRoot && activeRoot !== lastActiveRoot) { onWorkspaceChanged(); }
  } catch (e) { }
}
async function selectSession(root, filename) {
  if (running) { addSystem("⚠ 任务执行中无法切换会话", "error"); return; }
  try {
    const meta = await getJSON("/api/workspace");
    if (meta.root.replace(/\\/g, "/").replace(/\/$/, "") !== root.replace(/\\/g, "/").replace(/\/$/, "")) {
      const r = await fetch("/api/workspace", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: root }) });
      if (!r.ok) { addSystem("⚠ 切换工作区失败", "error"); return; }
    }
    const r2 = await fetch("/api/session/select", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename }) });
    const d2 = await r2.json();
    if (d2.ok) {
      currentSessionFile = filename;
      const m = await getJSON("/api/session/events?filename=" + encodeURIComponent(filename));
      replayEvents(m.events || []);
      loadTree(); loadFiles();
    } else {
      addSystem("⚠ " + (d2.message || "切换会话失败"), "error");
      loadTree();
    }
  } catch (e) { }
}
document.getElementById("btn-add-workspace").addEventListener("click", () => { browseDir = ""; renderBrowse(); document.getElementById("workspace-modal").classList.remove("hidden"); });
// 点击文件夹选择器外部关闭
document.getElementById("workspace-modal").addEventListener("click", e => {
  if (e.target === document.getElementById("workspace-modal")) document.getElementById("workspace-modal").classList.add("hidden");
});
async function newSessionInWorkspace(root) {
  const r = await fetch("/api/workspace/session/new", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: root }) });
  const d = await r.json();
  if (d.ok) {
    dataLoadedOnce = false;
    await loadTree();
    chatCol.innerHTML = "";
    showEmpty(true);
  } else { addSystem("⚠ " + (d.message || "新建会话失败"), "error"); }
}

/* ---------- 设置弹窗 ---------- */
const settingsEl = document.getElementById("settings");
document.getElementById("btn-settings").addEventListener("click", () => settingsEl.classList.remove("hidden"));
document.getElementById("btn-settings-close").addEventListener("click", () => settingsEl.classList.add("hidden"));
// 点击面板外部关闭
settingsEl.addEventListener("click", e => { if (e.target === settingsEl) settingsEl.classList.add("hidden"); });
document.querySelectorAll(".st[data-tab]").forEach(btn => btn.addEventListener("click", () => {
  document.querySelectorAll(".st[data-tab]").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  document.querySelectorAll(".stab").forEach(t => t.classList.remove("active"));
  document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
}));
// 收起 / 展开左右侧边栏
const sidebarEl = document.getElementById("sidebar");
const rightPanelEl = document.getElementById("right-panel");
const rpResizer = document.getElementById("rp-resizer");
const btnRightExpand = document.getElementById("btn-right-expand");
function setLeftCollapsed(v, persist) {
  sidebarEl.classList.toggle("collapsed", v);
  if (persist) saveSettings({ sidebar_collapsed: v });
}
function setRightCollapsed(v, persist) {
  rightPanelEl.classList.toggle("collapsed", v);
  btnRightExpand.hidden = !v;
  rpResizer.hidden = v;
  if (persist) saveSettings({ right_collapsed: v });
}
document.getElementById("btn-left-collapse").addEventListener("click", () => setLeftCollapsed(true, true));
document.getElementById("btn-left-expand").addEventListener("click", () => setLeftCollapsed(false, true));
document.getElementById("btn-settings-rail").addEventListener("click", () => settingsEl.classList.remove("hidden"));
document.getElementById("btn-right-collapse").addEventListener("click", () => setRightCollapsed(true, true));
btnRightExpand.addEventListener("click", () => setRightCollapsed(false, true));

/* ---------- 右侧文件栏宽度可拖拽 ----------
   关闭态（只显示文件树）与打开态（预览文件）分别持久化宽度：
   - agent-rp-width      未打开文件时的宽度
   - agent-rp-width-open 打开文件预览时的宽度
   打开文件再关闭后，侧边栏回到打开文件前的关闭态宽度。 */
const RP_W_KEY = "agent-rp-width";
const RP_W_OPEN_KEY = "agent-rp-width-open";
const RP_VIEW_OPEN_KEY = "agent-rp-view-open";   // 当前正在预览的文件路径（刷新后可恢复）
const RP_MIN = 180, RP_MAX = 900, RP_OPEN_MIN = 420;
function getRpWidth(key, fallback) {
  const w = parseInt(localStorage.getItem(key) || "", 10);
  return Number.isFinite(w) && w >= RP_MIN && w <= RP_MAX ? w : fallback;
}
// 读内联目标宽度（不受 transition 动画中间帧干扰）；无内联时退回布局宽度
function currentRpWidth() {
  const w = parseInt((rightPanelEl.style.width || "").replace("px", ""), 10);
  if (Number.isFinite(w) && w > 0) return w;
  return Math.round(rightPanelEl.getBoundingClientRect().width);
}
function setRpWidth(key, px) {
  if (Number.isFinite(px) && px >= RP_MIN && px <= RP_MAX) localStorage.setItem(key, String(Math.round(px)));
}
function persistCurrentRpWidth() {
  const open = rightPanelEl.classList.contains("view-open");
  setRpWidth(open ? RP_W_OPEN_KEY : RP_W_KEY, currentRpWidth());
}
(function initRpWidth() {
  const w = getRpWidth(RP_W_KEY, null);
  if (w !== null) rightPanelEl.style.width = w + "px";
})();
rpResizer.addEventListener("mousedown", e => {
  if (rightPanelEl.classList.contains("collapsed")) return;
  e.preventDefault();
  const startX = e.clientX;
  const startW = currentRpWidth();
  rpResizer.classList.add("active");
  rightPanelEl.classList.add("resizing");
  document.body.classList.add("resizing");
  function onMove(ev) {
    const open = rightPanelEl.classList.contains("view-open");
    const min = open ? RP_OPEN_MIN : 200, max = RP_MAX;
    let nw = startW + (startX - ev.clientX);
    nw = Math.max(min, Math.min(max, nw));
    rightPanelEl.style.width = nw + "px";
  }
  function onUp() {
    rpResizer.classList.remove("active");
    rightPanelEl.classList.remove("resizing");
    document.body.classList.remove("resizing");
    persistCurrentRpWidth();   // 用内联终值，避开 transition 中间帧
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  }
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
});

/* ---------- 设置持久化（服务端 /api/settings，localStorage 仅作主题首帧缓存防闪屏） ---------- */
function saveSettings(patch) {
  fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }).catch(() => { });
}
function applyTheme(t, persist) {
  document.documentElement.dataset.theme = t;
  if (persist) { localStorage.setItem("agent-theme", t); saveSettings({ theme: t }); }
  document.querySelectorAll('input[name="theme"]').forEach(r => { r.checked = (r.value === t); });
}
(function initTheme() { applyTheme(localStorage.getItem("agent-theme") || "dark", false); })();
document.querySelectorAll('input[name="theme"]').forEach(r => r.addEventListener("change", () => applyTheme(r.value, true)));
document.getElementById("model-input").addEventListener("change", e => {
  saveSettings({ model: e.target.value.trim() });
});
document.getElementById("model-url-input").addEventListener("change", e => {
  saveSettings({ model_url: e.target.value.trim() });
});
document.getElementById("model-key-input").addEventListener("change", e => {
  saveSettings({ model_key: e.target.value.trim() });
});
document.getElementById("max-context-input").addEventListener("change", e => {
  const v = parseInt(e.target.value, 10);
  saveSettings({ max_context_tokens: (Number.isFinite(v) && v > 0) ? v : 0 });
});
document.getElementById("max-tokens-input").addEventListener("change", e => {
  const v = parseInt(e.target.value, 10);
  saveSettings({ max_tokens: (Number.isFinite(v) && v >= 256) ? v : 8192 });
});
document.getElementById("max-steps-input").addEventListener("change", e => {
  const v = parseInt(e.target.value, 10);
  saveSettings({ max_steps: (Number.isFinite(v) && v >= 1) ? Math.min(v, 500) : 50 });
});

/* ---------- 审批（非阻塞浮层：不遮罩全屏，可继续浏览 / 滚动 / 操作，可收起为小条） ---------- */
function showConfirm(name, desc) {
  const t = document.getElementById("confirm-toast");
  t.classList.remove("collapsed");
  document.getElementById("confirm-title").textContent = name === "plan" ? "计划审批" : "允许执行 " + name + " ？";
  document.getElementById("confirm-desc").textContent = desc;
  t.classList.remove("hidden");
}
function answerConfirm(approved) {
  document.getElementById("confirm-toast").classList.add("hidden");
  fetch("/api/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved }) });
}
document.getElementById("btn-approve").addEventListener("click", () => answerConfirm(true));
document.getElementById("btn-reject").addEventListener("click", () => answerConfirm(false));
document.getElementById("confirm-collapse").addEventListener("click", () => {
  document.getElementById("confirm-toast").classList.toggle("collapsed");
});

/* ---------- 发送 / 中断 ---------- */
function submit() {
  const task = input.value.trim();
  if (!task || running) return;
  input.value = "";
  autoGrow();
  setRunning(true);
  fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task }) })
    .then(r => r.json())
    .then(d => { if (!d.ok) { setRunning(false); addSystem("⚠ " + (d.message || "启动失败"), "error"); } })
    .catch(() => { setRunning(false); addSystem("⚠ 无法连接服务器", "error"); });
}
btnRun.addEventListener("click", () => {
  if (!running) submit();
  else fetch("/api/interrupt", { method: "POST" });
});
input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });

/* ---------- 输入框动态高度（上限 220px）+ 权限/计划控件 ---------- */
function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 220) + "px";
}
input.addEventListener("input", autoGrow);
autoGrow();
/* ---- 自定义权限下拉（替代原生 select） ---- */
const PERM_LABELS = { auto: "自动编辑", ask: "变更前确认", plan: "计划模式" };
const permissionMenu = document.getElementById("permission-menu");
const permissionDropdown = document.getElementById("permission-dropdown");
function setPermission(val, save) {
  if (!PERM_LABELS[val]) val = "auto";
  permissionSelect.querySelector(".cb-value").textContent = PERM_LABELS[val];
  permissionMenu.querySelectorAll(".cb-opt").forEach(o => o.classList.toggle("selected", o.dataset.value === val));
  permissionMenu.hidden = true;
  permissionSelect.setAttribute("aria-expanded", "false");
  if (save) saveSettings({ permission: val });
}
permissionSelect.addEventListener("click", (e) => {
  e.stopPropagation();
  const open = permissionMenu.hidden;
  permissionMenu.hidden = !open;
  permissionSelect.setAttribute("aria-expanded", String(open));
});
permissionMenu.querySelectorAll(".cb-opt").forEach(opt => {
  opt.addEventListener("click", () => setPermission(opt.dataset.value, true));
});
document.addEventListener("click", (e) => {
  if (!permissionDropdown.contains(e.target)) {
    permissionMenu.hidden = true;
    permissionSelect.setAttribute("aria-expanded", "false");
  }
});

/* ---------- 文件夹选择器 ---------- */
async function renderBrowse() {
  const d = await getJSON("/api/fs/browse?path=" + encodeURIComponent(browseDir));
  if (d.error) return;
  document.getElementById("browse-path").textContent = d.isRoot ? "计算机" : (d.path || "/");
  const canChoose = !!browseDir && !/^[A-Za-z]:[\\/]?$/.test(browseDir.replace(/\\/g, "/"));
  document.getElementById("browse-hint").style.display = canChoose ? "none" : "";
  document.getElementById("browse-list").innerHTML = d.entries.map(e =>
    `<div class="browse-item" data-path="${esc(e.path)}">` + ic("folder") + "<span>" + esc(e.name) + "</span></div>").join("");
  document.querySelectorAll(".browse-item").forEach(x => x.addEventListener("click", () => { browseDir = x.dataset.path; renderBrowse(); }));
}
function isChoosableFolder(p) { return !!p && !/^[A-Za-z]:[\\/]?$/.test(p.replace(/\\/g, "/")); }
document.getElementById("btn-browse-up").addEventListener("click", () => {
  if (!browseDir) return;
  const parts = browseDir.replace(/\\/g, "/").split("/").filter(Boolean);
  parts.pop(); browseDir = parts.join("/"); renderBrowse();
});
document.getElementById("btn-workspace-cancel").addEventListener("click", () => document.getElementById("workspace-modal").classList.add("hidden"));
document.getElementById("btn-workspace-choose").addEventListener("click", async () => {
  if (!isChoosableFolder(browseDir)) {
    document.getElementById("browse-hint").style.display = "";
    return;
  }
  const r = await fetch("/api/workspace", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: browseDir }) });
  const d = await r.json();
  if (d.ok) {
    document.getElementById("workspace-modal").classList.add("hidden");
    dataLoadedOnce = false;
    await loadTree();   // 自动刷新会话树、会话名头部、文件树（随工作区切换）
  } else { addSystem("⚠ " + (d.message || "切换工作区失败"), "error"); }
});

/* ---------- 右侧文件面板 ---------- */
async function loadFiles() {
  try {
    const d = await getJSON("/api/files?path=" + encodeURIComponent(fileDir));
    if (d.error) return;
    document.getElementById("files").innerHTML =
      (fileDir !== "." ? `<div class="file-item dir" data-path="${esc(parentOf(fileDir))}">${ic("up")}..</div>` : "") +
      d.entries.map(e => `<div class="file-item ${e.dir ? "dir" : ""}" data-path="${esc(joinPath(fileDir, e.name))}">` +
        (e.dir ? ic("folder") : ic("file")) + "<span>" + esc(e.name) + "</span>" +
        (e.dir ? "" : " <span class=\"size\">(" + e.size + " B)</span>") + "</div>").join("");
    document.querySelectorAll(".file-item").forEach(x => x.addEventListener("click", () => {
      if (!x.dataset.path) return;
      if (x.classList.contains("dir")) { fileDir = x.dataset.path; loadFiles(); }   // 目录：进入
      else { openFile(x.dataset.path); }                                            // 文件：预览
    }));
    // 重渲染后恢复当前打开文件的选中高亮
    if (currentOpenFile) {
      document.querySelectorAll(".file-item").forEach(x => { if (x.dataset.path === currentOpenFile) x.classList.add("active"); });
    }
  } catch (e) { }
}
function ensureRpOpenMinWidth() {
  // 打开文件预览时，确保文件树 + 预览区都有基本空间（内联宽度可能压得很窄）
  if (currentRpWidth() < RP_OPEN_MIN) {
    rightPanelEl.style.width = RP_OPEN_MIN + "px";
    persistCurrentRpWidth();   // 改宽后立即写回对应键，否则会与用户拖好的宽度不一致
  }
}
async function openFile(rel) {
  try {
    const d = await getJSON("/api/file?path=" + encodeURIComponent(rel));
    const nameEl = document.getElementById("file-view-name");
    const contentEl = document.getElementById("file-view-content");
    if (d.error) {
      nameEl.textContent = "预览失败";
      contentEl.textContent = "⚠ " + d.error;
      document.getElementById("task-detail").hidden = true;
      document.getElementById("sub-detail").hidden = true;
      document.getElementById("rp-view").hidden = false;
      document.getElementById("file-view").hidden = false;
      currentOpenFile = rel;
      let fileRowE = null;
      document.querySelectorAll(".file-item").forEach(x => { if (x.dataset.path === rel) fileRowE = x; });
      markActive(fileRowE);
      rightPanelEl.classList.add("view-open");
      ensureRpOpenMinWidth();
      return;
    }
    nameEl.textContent = d.name + (d.truncated ? "（已截断到前 4000 行）" : "");
    contentEl.textContent = d.content;
    document.getElementById("task-detail").hidden = true;
    document.getElementById("sub-detail").hidden = true;
    document.getElementById("rp-view").hidden = false;
    currentTaskId = null; currentSubId = null; currentOpenFile = rel;
    // 高亮文件树中对应项（data-path 与 file-item 一致，rel 即点击时的 dataset.path）
    let fileRow = null;
    document.querySelectorAll(".file-item").forEach(x => { if (x.dataset.path === rel) fileRow = x; });
    markActive(fileRow);
    document.getElementById("file-view").hidden = false;
    const wasOpen = rightPanelEl.classList.contains("view-open");
    if (!wasOpen) {
      // 记录关闭态宽度（关闭预览时还原到该宽度）
      const closedW = currentRpWidth();
      localStorage.setItem(RP_W_KEY, String(closedW));
    }
    rightPanelEl.classList.add("view-open");
    if (!wasOpen) {
      // 打开文件态宽度：优先用上次打开态宽度（已持久化），否则继承当前关闭态宽度，
      // 并立即写入，避免「首次打开被 ensureRpOpenMinWidth 改宽却不持久」导致下次不一致
      const savedOpen = getRpWidth(RP_W_OPEN_KEY, null);
      const w = savedOpen !== null ? savedOpen : currentRpWidth();
      rightPanelEl.style.width = w + "px";
      localStorage.setItem(RP_W_OPEN_KEY, String(w));
    }
    ensureRpOpenMinWidth();
    // 持久化正在预览的文件路径，刷新页面后可自动恢复预览与宽度
    localStorage.setItem(RP_VIEW_OPEN_KEY, rel);
  } catch (e) { }
}
document.getElementById("btn-file-view-close").addEventListener("click", closeDetailPanel);
// 任务区域（后台任务 / 子agent）折叠 / 展开
document.querySelectorAll(".tr-head").forEach(h => h.addEventListener("click", () => {
  const region = h.dataset.region;
  const body = document.getElementById(region + "-body");
  const open = body.hidden;
  body.hidden = !open;
  h.classList.toggle("open", open);
}));
// 详情面板关闭按钮
document.querySelectorAll(".detail-close").forEach(b => b.addEventListener("click", closeDetailPanel));

/* ---------- 工具（设置面板） ---------- */
async function loadTools() {
  try {
    const t = await getJSON("/api/tools");
    document.getElementById("tools").innerHTML = Object.entries(t)
      .map(([n, d]) => `<div class="tool-item" title="${esc(d)}"><b>${esc(n)}</b>${esc(d.slice(0, 22))}</div>`).join("");
  } catch (e) { }
}

/* ---------- 事件流 ---------- */
const es = new EventSource("/api/events");
es.onmessage = e => { try { render(JSON.parse(e.data)); } catch (err) { } };

/* ---------- 启动时拉取服务端设置（主题 / 侧栏布局） ---------- */
async function loadSettings() {
  try {
    const s = await getJSON("/api/settings");
    if (s.theme === "dark" || s.theme === "light") applyTheme(s.theme, false);
    if (typeof s.sidebar_collapsed === "boolean") setLeftCollapsed(s.sidebar_collapsed, false);
    if (typeof s.right_collapsed === "boolean") setRightCollapsed(s.right_collapsed, false);
    if (typeof s.model === "string" && s.model.trim()) document.getElementById("model-input").value = s.model;
    if (typeof s.model_url === "string") document.getElementById("model-url-input").value = s.model_url;
    if (typeof s.model_key === "string") document.getElementById("model-key-input").value = s.model_key;
    if (typeof s.max_context_tokens === "number") document.getElementById("max-context-input").value = s.max_context_tokens;
    if (typeof s.max_tokens === "number") document.getElementById("max-tokens-input").value = s.max_tokens;
    if (typeof s.max_steps === "number") document.getElementById("max-steps-input").value = s.max_steps;
    if (s.permission === "auto" || s.permission === "ask" || s.permission === "plan") setPermission(s.permission, false);
  } catch (e) { }
}

loadTree(); loadFiles(); loadTools();
loadSettings().then(() => {
  // 刷新后恢复上次打开的文件预览与宽度（仅当右侧栏处于展开状态）
  if (!rightPanelEl.classList.contains("collapsed")) {
    const rel = localStorage.getItem(RP_VIEW_OPEN_KEY);
    if (rel) openFile(rel);
  }
});
