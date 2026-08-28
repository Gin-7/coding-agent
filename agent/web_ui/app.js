/* Coding Agent Web UI —— 事件流浏览器渲染器（工作区树 / 会话 / 发送-中断 / 设置弹窗） */
"use strict";

const chatCol = document.getElementById("chat-col");
const scrollEl = document.getElementById("chat-scroll");
const input = document.getElementById("input");
const btnRun = document.getElementById("btn-run");

let running = false;
let lastAssistant = null;
let lastCommandPre = null;
let assistantRaw = "";
let turn = null;             // 当前 agent 对话合并框
let lastActiveRoot = "";     // 上次活动工作区（用于文件树随工作区切换）
const toolCards = new Map();
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
function scrollToBottom() {
  const near = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 260;
  if (near) scrollEl.scrollTop = scrollEl.scrollHeight;
}
function showEmpty(show) { document.getElementById("empty").style.display = show ? "" : "none"; }
function addBubble(cls, text, parent) {
  const b = document.createElement("div");
  b.className = "bubble " + cls;
  if (text !== undefined) b.textContent = text;
  (parent || chatCol).appendChild(b);
  scrollToBottom();
  return b;
}
function addSystem(text, cls, parent) {
  const b = document.createElement("div");
  b.className = "note " + (cls || "");
  b.textContent = text;
  (parent || chatCol).appendChild(b);
  scrollToBottom();
}
function makeTurn() {
  const t = document.createElement("div");
  t.className = "agent-turn";
  chatCol.appendChild(t);
  return t;
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
};
function ic(name, cls) { return '<span class="ic ' + (cls || "svg14") + '">' + ICON[name] + "</span>"; }
function addIconNote(icon, text, cls, parent) {
  const b = document.createElement("div");
  b.className = "note " + (cls || "");
  b.innerHTML = ic(icon) + "<span>" + esc(text) + "</span>";
  (parent || chatCol).appendChild(b); scrollToBottom();
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
function addToolInline(name, args, parent) {
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

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].replace(/\s+$/, "");        // 去尾部空白
    const fence = line.match(/^```(.*)$/);
    if (fence) {                                       // 代码块：整段抽取
      flushPara(); flushList();
      const buf = [];
      while (++i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i]);
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
    if (isItem(line)) {                                // 列表项（连续项合并为一个列表）
      flushPara();
      const ordered = /^\d+\.\s/.test(line);
      if (listBuf.length && ordered !== listOrdered) flushList();
      listOrdered = ordered;
      listBuf.push(line.replace(/^[-*]\s+/, "").replace(/^\d+\.\s+/, ""));
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
      turn = makeTurn();               // 每个 agent 对话合并为一个框
      lastAssistant = null; lastCommandPre = null;
      break;
    case "TextDelta":
      if (!lastAssistant) {
        lastAssistant = document.createElement("div");
        lastAssistant.className = "assistant-text" + (replay ? "" : " running");
        (turn || chatCol).appendChild(lastAssistant);
        assistantRaw = "";
      }
      assistantRaw += ev.text;
      lastAssistant.innerHTML = renderMarkdown(assistantRaw);
      scrollToBottom();
      break;
    case "StepEvent":
      lastAssistant = null; lastCommandPre = null;   // 重置分段，使每步文本独立成块
      addSystem("step " + ev.step + "/" + ev.max_steps, "step", turn);
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
      addSystem(ev.summarized ? "[上下文] 已把早期 " + ev.messages_removed + " 条消息压缩为摘要"
                              : "[上下文] 已丢弃早期 " + ev.messages_removed + " 条消息", "", turn);
      break;
    case "ErrorEvent": addIconNote("warn", ev.message, "error", turn); break;
    case "FinishEvent": {
      // finish 不再作为工具卡片，直接以正文气泡呈现（与主体内容同款样式、支持 markdown）
      const d = document.createElement("div");
      d.className = "assistant-text";
      d.innerHTML = renderMarkdown(ev.summary || "");
      (turn || chatCol).appendChild(d); scrollToBottom();
      break;
    }
    case "Notice": addSystem(ev.message, "", turn); break;
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
      lastAssistant = null; lastCommandPre = null; turn = null; assistantRaw = "";
      lastCmdToolEl = null;
      if (!replay) { loadTree(); loadFiles(); }
      break;
  }
}

/* ---------- 回放：重放全量事件日志，复用 render ---------- */
function replayEvents(events) {
  chatCol.innerHTML = "";
  turn = null; lastAssistant = null; lastCommandPre = null; assistantRaw = "";
  lastCmdToolEl = null; toolCards.clear();
  toolCards.clear();
  for (const ev of events || []) {
    if (ev && ev.type === "MessagesDump") continue;  // 仅快照，渲染用不到
    render(ev, { replay: true });
  }
  if (lastAssistant) lastAssistant.classList.remove("running");
  scrollToBottom();
  loadFiles();  // 仅刷新文件面板；会话树由调用方负责（避免递归触发再次回放）
}

/* ---------- 侧边栏工作区 / 会话树 ---------- */
let dataLoadedOnce = false;
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
        '<button class="ws-add" title="新建会话">' + ICON.plus + "</button>";
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
      const sessionsEl = document.createElement("div");
      sessionsEl.className = "tree-sessions";
      sessionsEl.style.display = open ? "block" : "none";
      if (ws.sessions.length === 0) {
        const empty = document.createElement("div");
        empty.className = "tree-empty";
        empty.textContent = "无会话";
        sessionsEl.appendChild(empty);
      }
      for (const s of ws.sessions) {
        const se = document.createElement("div");
        se.className = "tree-session" + ((ws.is_active && s.filename === ws.active) ? " active" : "");
        const name = document.createElement("span");
        name.className = "session-name";
        name.textContent = s.name;
        se.appendChild(name);
        const time = document.createElement("span");
        time.className = "session-time";
        time.textContent = s.mtime ? timeAgo(s.mtime * 1000) : "";
        se.appendChild(time);
        se.title = s.filename;
        se.addEventListener("click", () => selectSession(ws.root, s.filename));
        sessionsEl.appendChild(se);
      }
      group.appendChild(header); group.appendChild(sessionsEl);
      tree.appendChild(group);
    }
    if (!dataLoadedOnce) {
      dataLoadedOnce = true;
      const d = await getJSON("/api/workspace");
      const m = d.active ? await getJSON("/api/session/events?filename=" + encodeURIComponent(d.active)) : null;
      replayEvents(m ? m.events : []);
    }
    // 会话名头部 + 文件树随工作区切换
    const aw = list.find(w => w.is_active);
    const as = aw && (aw.sessions || []).find(s => s.filename === aw.active);
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

/* ---------- 审批 ---------- */
function showConfirm(name, desc) {
  document.getElementById("confirm-title").textContent = name === "plan" ? "计划审批" : "允许执行 " + name + " ？";
  document.getElementById("confirm-desc").textContent = desc;
  document.getElementById("confirm-modal").classList.remove("hidden");
}
function answerConfirm(approved) {
  document.getElementById("confirm-modal").classList.add("hidden");
  fetch("/api/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved }) });
}
document.getElementById("btn-approve").addEventListener("click", () => answerConfirm(true));
document.getElementById("btn-reject").addEventListener("click", () => answerConfirm(false));

/* ---------- 发送 / 中断 ---------- */
function submit() {
  const task = input.value.trim();
  if (!task || running) return;
  input.value = "";
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
      document.getElementById("file-view").hidden = false;
      rightPanelEl.classList.add("view-open");
      ensureRpOpenMinWidth();
      return;
    }
    nameEl.textContent = d.name + (d.truncated ? "（已截断到前 4000 行）" : "");
    contentEl.textContent = d.content;
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
document.getElementById("btn-file-up").addEventListener("click", () => { fileDir = parentOf(fileDir); loadFiles(); });
document.getElementById("btn-file-view-close").addEventListener("click", () => {
  document.getElementById("file-view").hidden = true;
  rightPanelEl.classList.remove("view-open");
  // 保存打开态宽度（用内联终值，避开 width .2s 过渡的中间帧）
  const openW = currentRpWidth();
  setRpWidth(RP_W_OPEN_KEY, openW);
  // 回到打开文件前的关闭态宽度
  const closedW = getRpWidth(RP_W_KEY, null);
  if (closedW !== null) rightPanelEl.style.width = closedW + "px";
  // 清除预览记录（刷新后不再自动恢复）
  localStorage.removeItem(RP_VIEW_OPEN_KEY);
});

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
