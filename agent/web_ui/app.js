/* Coding Agent Web UI —— 事件流浏览器渲染器（工作区树 / 会话 / 发送-中断 / 设置弹窗） */
"use strict";

const chatCol = document.getElementById("chat-col");
const scrollEl = document.getElementById("chat-scroll");
const input = document.getElementById("input");
const btnRun = document.getElementById("btn-run");

let running = false;
let lastAssistant = null;
let lastCommandPre = null;
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
  btnRun.querySelector(".btn-label").textContent = v ? "中断" : "发送";
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

/* ---------- 会话历史转写 ---------- */
function renderTranscript(msgs) {
  chatCol.innerHTML = "";
  showEmpty(!msgs || msgs.length === 0);
  let turn = null;
  for (const m of msgs || []) {
    if (m.role === "system") continue;
    if (m.role === "user") { addBubble("user", m.content); turn = makeTurn(); continue; }
    if (m.role === "assistant") {
      if (!turn) turn = makeTurn();
      if (m.content) {
        const t = document.createElement("div");
        t.className = "assistant-text";
        t.textContent = m.content;
        turn.appendChild(t);
      }
      for (const tc of m.tool_calls || []) {
        const fn = tc.function || {};
        const card = document.createElement("div");
        card.className = "tool-card open";
        const head = document.createElement("div");
        head.className = "tool-head";
        head.innerHTML = "<span class=\"caret\">▸</span><span>🔧</span><span>" + esc(fn.name || "tool") + "</span>";
        const argsPre = document.createElement("pre");
        argsPre.className = "tool-args";
        try { argsPre.textContent = safeJson(JSON.parse(fn.arguments || "{}")); } catch (e) { argsPre.textContent = fn.arguments || ""; }
        card.appendChild(head); card.appendChild(argsPre);
        turn.appendChild(card);
      }
      continue;
    }
    if (m.role === "tool") {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "↳ " + (m.content || "").slice(0, 120);
      (turn || chatCol).appendChild(note);
    }
  }
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
        lastAssistant.className = "assistant-text";
        lastAssistant.classList.add("running");
        (turn || chatCol).appendChild(lastAssistant);
      }
      lastAssistant.textContent += ev.text; scrollToBottom();
      break;
    case "ToolCallEvent": {
      const card = document.createElement("div");
      card.className = "tool-card";
      const head = document.createElement("div");
      head.className = "tool-head";
      head.innerHTML = "<span class=\"caret\">▸</span><span>🔧</span><span>" + esc(ev.name) + "</span>";
      const argsPre = document.createElement("pre");
      argsPre.className = "tool-args"; argsPre.textContent = safeJson(ev.arguments);
      const resultDiv = document.createElement("pre");
      resultDiv.className = "tool-result"; resultDiv.style.display = "none";
      head.addEventListener("click", () => card.classList.toggle("open"));
      card.appendChild(head); card.appendChild(argsPre); card.appendChild(resultDiv);
      (turn || chatCol).appendChild(card); toolCards.set(ev.call_id, { card, resultDiv }); scrollToBottom();
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
      const t = toolCards.get(ev.call_id);
      if (t) {
        t.resultDiv.textContent = (ev.ok ? "✓ " : "✗ ") + ev.output;
        t.resultDiv.style.display = "";
        t.card.classList.add("open");
        t.card.dataset.ok = ev.ok ? "ok" : "fail";
      }
      lastCommandPre = null;
      break;
    }
    case "StepEvent": addSystem("step " + ev.step + "/" + ev.max_steps, "step", turn); break;
    case "TrimmedEvent": addSystem("[上下文] 预算紧张，已裁剪最老的 " + ev.rounds + " 轮工具调用", "", turn); break;
    case "CompactedEvent":
      addSystem(ev.summarized ? "[上下文] 已把早期 " + ev.messages_removed + " 条消息压缩为摘要"
                              : "[上下文] 已丢弃早期 " + ev.messages_removed + " 条消息", "", turn);
      break;
    case "ErrorEvent": addSystem("⚠ " + ev.message, "error", turn); break;
    case "FinishEvent": addBubble("finish", "✅ " + ev.summary, turn); break;
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
      lastAssistant = null; lastCommandPre = null; turn = null;
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
        '<svg class="caret" viewBox="0 0 24 24"><path fill="currentColor" d="M9 6l6 6-6 6z"/></svg>' +
        '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6z"/></svg>' +
        "<span>" + esc(ws.name) + "</span>";
      header.addEventListener("click", () => {
        const o = expandedRoots.has(ws.root);
        if (o) expandedRoots.delete(ws.root); else expandedRoots.add(ws.root);
        header.classList.toggle("open", !o);
        sessionsEl.style.display = !o ? "block" : "none";
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
        se.textContent = s.name;
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
    `<div class="browse-item" data-path="${esc(e.path)}">📁 ${esc(e.name)}</div>`).join("");
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
      (fileDir !== "." ? `<div class="file-item" data-path="${esc(parentOf(fileDir))}">↩ ..</div>` : "") +
      d.entries.map(e => `<div class="file-item ${e.dir ? "dir" : ""}" data-path="${esc(joinPath(fileDir, e.name))}">` +
        (e.dir ? "📁 " : "📄 ") + esc(e.name) + (e.dir ? "" : " (" + e.size + " B)") + "</div>").join("");
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
