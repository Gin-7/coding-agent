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

/* ---------- 轻量 markdown 渲染（零依赖） ---------- */
function renderMarkdown(text) {
  let s = esc(text.replace(/\n{2,}/g, "\n"));  // 压缩连续换行，气泡更紧凑
  // 代码块（优先，含内部换行）
  const blocks = [];
  s = s.replace(/```([\s\S]*?)```/g, (m, c) => { blocks.push(c); return "\u0000B" + (blocks.length - 1) + "\u0000"; });
  // 行内代码
  s = s.replace(/`([^`]+)`/g, "<code class=\"md-inline\">$1</code>");
  // 标题
  s = s.replace(/^### (.+)$/gm, "<h4>$1</h4>");
  s = s.replace(/^## (.+)$/gm, "<h3>$1</h3>");
  s = s.replace(/^# (.+)$/gm, "<h2>$1</h2>");
  // 加粗 / 斜体
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  // 链接
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // 列表
  s = s.replace(/^[-*] (.+)$/gm, "<li>$1</li>");
  s = s.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
  s = s.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul class="md-ul">$1</ul>');
  // 段落（按空行切分）
  s = s.split(/\n{2,}/).map(seg => {
    const t = seg.trim();
    if (!t) return "";
    if (t.startsWith("\u0000B") || t.indexOf("<li>") >= 0 || t.startsWith("<h")) return t;
    return "<p>" + t.replace(/\n/g, "<br>") + "</p>";
  }).join("");
  // 还原代码块
  s = s.replace(/\u0000B(\d+)\u0000/g, (m, i) => '<pre class="md-code">' + blocks[+i] + "</pre>");
  return s;
}
function timeAgo(ms) {
  if (!ms) return "";
  const d = Date.now() - ms;
  if (d < 60 * 1000) return "刚刚";
  if (d < 3600 * 1000) return Math.floor(d / 60000) + " 分钟前";
  if (d < 86400 * 1000) return Math.floor(d / 3600000) + " 小时前";
  return Math.floor(d / 86400000) + " 天前";
}
function isInjectedUserMsg(m) {
  if (m.role !== "user") return false;
  const c = (m.content || "").trim();
  return /^(【规划阶段】|（提示：|请继续：|计划已批准|用户拒绝|【早期对话摘要】|【工具执行结果】|【工作区记忆】|\[工作区记忆\])/.test(c);
}

/* ---------- 会话历史转写 ---------- */
function renderTranscript(msgs) {
  chatCol.innerHTML = "";
  let t = null, hasContent = false;
  const transTools = new Map();
  for (const m of msgs || []) {
    if (m.role === "system" || isInjectedUserMsg(m)) continue;
    if (m.role === "user") {
      if (m.content !== undefined) addBubble("user", m.content);
      t = makeTurn(); hasContent = true; continue;
    }
    if (m.role === "assistant") {
      if (!t) t = makeTurn();
      if (m.content) {
        const el = document.createElement("div");
        el.className = "assistant-text";
        el.innerHTML = renderMarkdown(m.content);
        t.appendChild(el);
      }
      for (const tc of m.tool_calls || []) {
        const fn = tc.function || {};
        let args = {};
        try { args = JSON.parse(fn.arguments || "{}"); } catch (e) { args = {}; }
        const el = addToolInline(fn.name || "tool", args, t);
        if (tc.id) transTools.set(tc.id, el);
      }
      hasContent = true;
      continue;
    }
    if (m.role === "tool") {
      const el = m.tool_call_id ? transTools.get(m.tool_call_id) : null;
      if (el) {
        if (el._out) el._out.textContent = (m.content || "");
      } else {
        const note = document.createElement("div");
        note.className = "tool-out-line";
        note.textContent = (m.content || "").replace(/\s+/g, " ").slice(0, 100);
        (t || chatCol).appendChild(note);
      }
      hasContent = true;
    }
  }
  showEmpty(!hasContent);
  scrollToBottom();
}

/* ---------- 事件渲染 ---------- */
function render(ev) {
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
        lastAssistant.className = "assistant-text running";
        (turn || chatCol).appendChild(lastAssistant);
        assistantRaw = "";
      }
      assistantRaw += ev.text;
      lastAssistant.innerHTML = renderMarkdown(assistantRaw);
      scrollToBottom();
      break;
    case "StepEvent":
      // 每个步骤的文字独立成块，避免上一步工具调用上方显示下一步文字
      lastAssistant = null; lastCommandPre = null;
      addSystem("step " + ev.step + "/" + ev.max_steps, "step", turn);
      break;
    case "ToolCallEvent": {
      const el = addToolInline(ev.name, ev.arguments, turn || chatCol);
      toolCards.set(ev.call_id, el);
      scrollToBottom();
      break;
    }
    case "CommandOutput":
      if (!lastCommandPre) {
        lastCommandPre = document.createElement("pre");
        lastCommandPre.className = "cmd-output";
        (turn || chatCol).appendChild(lastCommandPre);
      }
      lastCommandPre.textContent += ev.text; scrollToBottom();
      break;
    case "ToolResultEvent": {
      const el = toolCards.get(ev.call_id);
      if (el) {
        el.classList.add(ev.ok ? "ok" : "fail");
        if (el._out) el._out.textContent = (ev.ok ? "✓ " : "✗ ") + ev.output;
      }
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
      const b = document.createElement("div");
      b.className = "bubble finish";
      b.innerHTML = ic("checkCircle") + "<span>" + esc(ev.summary) + "</span>";
      (turn || chatCol).appendChild(b); scrollToBottom();
      break;
    }
    case "Notice": addSystem(ev.message, "", turn); break;
    case "AskConfirm": showConfirm(ev.name, ev.desc); break;
    case "SessionsChanged": loadTree(); break;
    case "RunResult":
      setRunning(false);
      if (lastAssistant) lastAssistant.classList.remove("running");
      if (ev.status === "finished") addSystem("[完成] " + (ev.summary || ""), "ok", turn);
      else if (ev.status !== "interrupted") addSystem("[" + ev.status + "] " + (ev.message || ""), "error", turn);
      if (ev.steps != null) {
        const u = ev.usage || {};
        addSystem("[统计] 步骤 " + ev.steps + " | 输入 " + (u.prompt || 0) + " / 输出 " + (u.completion || 0) + " tokens", "", turn);
      }
      lastAssistant = null; lastCommandPre = null; turn = null; assistantRaw = "";
      loadTree(); loadFiles();
      break;
  }
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
      const m = d.active ? await getJSON("/api/session/messages?filename=" + encodeURIComponent(d.active)) : null;
      renderTranscript(m ? m.messages : []);
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
      const m = await getJSON("/api/session/messages?filename=" + encodeURIComponent(filename));
      renderTranscript(m.messages || []);
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
const btnRightExpand = document.getElementById("btn-right-expand");
document.getElementById("btn-left-collapse").addEventListener("click", () => sidebarEl.classList.add("collapsed"));
document.getElementById("btn-left-expand").addEventListener("click", () => sidebarEl.classList.remove("collapsed"));
document.getElementById("btn-settings-rail").addEventListener("click", () => settingsEl.classList.remove("hidden"));
document.getElementById("btn-right-collapse").addEventListener("click", () => { rightPanelEl.classList.add("collapsed"); btnRightExpand.hidden = false; });
btnRightExpand.addEventListener("click", () => { rightPanelEl.classList.remove("collapsed"); btnRightExpand.hidden = true; });

/* ---------- 主题（仅设置面板） ---------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("agent-theme", t);
  document.querySelectorAll('input[name="theme"]').forEach(r => { r.checked = (r.value === t); });
}
(function initTheme() { applyTheme(localStorage.getItem("agent-theme") || "dark"); })();
document.querySelectorAll('input[name="theme"]').forEach(r => r.addEventListener("change", () => applyTheme(r.value)));

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
    document.querySelectorAll(".file-item").forEach(x => x.addEventListener("click", () => { if (x.dataset.path) { fileDir = x.dataset.path; loadFiles(); } }));
  } catch (e) { }
}
document.getElementById("btn-file-up").addEventListener("click", () => { fileDir = parentOf(fileDir); loadFiles(); });

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

loadTree(); loadFiles(); loadTools();
