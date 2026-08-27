/* Coding Agent Web UI —— 事件流的浏览器渲染器（多工作区 / 多会话 / 主题） */
"use strict";

const chatCol = document.getElementById("chat-col");
const scrollEl = document.getElementById("chat");
const input = document.getElementById("input");
const btnRun = document.getElementById("btn-run");
const btnInterrupt = document.getElementById("btn-interrupt");
const statusText = document.getElementById("status-text");
const statusDot = document.getElementById("status-dot");

let running = false;
let lastAssistant = null;
let lastCommandPre = null;
const toolCards = new Map();
let fileDir = ".";        // 右侧文件面板当前目录
let browseDir = "";       // 文件夹选择器当前目录
let currentActiveSession = "";

/* ---------- 基础 ---------- */
function setRunning(v) {
  running = v;
  btnRun.disabled = v;
  btnInterrupt.disabled = !v;
  statusText.textContent = v ? "运行中" : "空闲";
  statusDot.className = v ? "on" : "";
}
function scrollToBottom() {
  const near = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 260;
  if (near) scrollEl.scrollTop = scrollEl.scrollHeight;
}
function showEmpty(show) {
  document.getElementById("empty").style.display = show ? "" : "none";
}
function addBubble(cls, text) {
  const b = document.createElement("div");
  b.className = "bubble " + cls;
  if (text !== undefined) b.textContent = text;
  chatCol.appendChild(b);
  scrollToBottom();
  return b;
}
function addSystem(text, cls) {
  const b = document.createElement("div");
  b.className = "note " + (cls || "");
  b.textContent = text;
  chatCol.appendChild(b);
  scrollToBottom();
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function safeJson(o) { try { return JSON.stringify(o, null, 2); } catch (e) { return String(o); } }

/* ---------- 会话历史转写渲染 ---------- */
function renderTranscript(msgs) {
  chatCol.innerHTML = "";
  showEmpty(msgs.length === 0);
  for (const m of msgs || []) {
    if (m.role === "system") continue;
    if (m.role === "user") { addBubble("user", m.content); continue; }
    if (m.role === "assistant") {
      if (m.content) addBubble("assistant", m.content);
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
        chatCol.appendChild(card);
      }
      continue;
    }
    if (m.role === "tool") {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "↳ " + (m.content || "").slice(0, 120);
      chatCol.appendChild(note);
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
      lastAssistant = null; lastCommandPre = null;
      break;
    case "TextDelta":
      if (!lastAssistant) { lastAssistant = addBubble("assistant", ""); lastAssistant.classList.add("running"); }
      lastAssistant.textContent += ev.text;
      scrollToBottom();
      break;
    case "ToolCallEvent": {
      const card = document.createElement("div");
      card.className = "tool-card";
      const head = document.createElement("div");
      head.className = "tool-head";
      head.innerHTML = "<span class=\"caret\">▸</span><span>🔧</span><span>" + esc(ev.name) + "</span>";
      const argsPre = document.createElement("pre");
      argsPre.className = "tool-args";
      argsPre.textContent = safeJson(ev.arguments);
      const resultDiv = document.createElement("pre");
      resultDiv.className = "tool-result";
      resultDiv.style.display = "none";
      head.addEventListener("click", () => card.classList.toggle("open"));
      card.appendChild(head); card.appendChild(argsPre); card.appendChild(resultDiv);
      chatCol.appendChild(card);
      toolCards.set(ev.call_id, { card, resultDiv });
      scrollToBottom();
      break;
    }
    case "CommandOutput":
      if (!lastCommandPre) {
        lastCommandPre = document.createElement("pre");
        lastCommandPre.className = "cmd-output";
        chatCol.appendChild(lastCommandPre);
      }
      lastCommandPre.textContent += ev.text;
      scrollToBottom();
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
    case "StepEvent": addSystem("step " + ev.step + "/" + ev.max_steps, "step"); break;
    case "TrimmedEvent": addSystem("[上下文] 预算紧张，已裁剪最老的 " + ev.rounds + " 轮工具调用"); break;
    case "CompactedEvent":
      addSystem(ev.summarized ? "[上下文] 已把早期 " + ev.messages_removed + " 条消息压缩为摘要"
                              : "[上下文] 已丢弃早期 " + ev.messages_removed + " 条消息");
      break;
    case "ErrorEvent": addSystem("⚠ " + ev.message, "error"); break;
    case "FinishEvent": addBubble("finish", "✅ " + ev.summary); break;
    case "Notice": addSystem(ev.message); break;
    case "AskConfirm": showConfirm(ev.name, ev.desc); break;
    case "SessionsChanged": loadSessions(); loadWorkspaceName(); break;
    case "RunResult":
      setRunning(false);
      if (lastAssistant) lastAssistant.classList.remove("running");
      if (ev.status === "finished") addSystem("[完成] " + (ev.summary || ""), "ok");
      else if (ev.status !== "interrupted") addSystem("[" + ev.status + "] " + (ev.message || ""), "error");
      if (ev.steps != null) {
        const u = ev.usage || {};
        addSystem("[统计] 步骤 " + ev.steps + " | 输入 " + (u.prompt || 0) +
                  " / 输出 " + (u.completion || 0) + " tokens");
      }
      lastAssistant = null; lastCommandPre = null;
      loadSessions(); loadFiles();
      break;
  }
}

/* ---------- 设置抽屉 ---------- */
const settingsEl = document.getElementById("settings");
document.getElementById("btn-settings").addEventListener("click", () => {
  settingsEl.classList.remove("hidden");
});
document.getElementById("btn-settings-close").addEventListener("click", () => settingsEl.classList.add("hidden"));
document.querySelectorAll(".menu-item[data-tab]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".menu-item[data-tab]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

/* ---------- 主题 ---------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("agent-theme", t);
  document.querySelectorAll('input[name="theme"]').forEach(r => { r.checked = (r.value === t); });
  document.getElementById("btn-theme").textContent = (t === "dark" ? "☀️" : "🌙");
}
(function initTheme() {
  const t = localStorage.getItem("agent-theme") || "dark";
  applyTheme(t);
})();
document.getElementById("btn-theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(cur);
});
document.querySelectorAll('input[name="theme"]').forEach(r => r.addEventListener("change", () => applyTheme(r.value)));

/* ---------- 审批 ---------- */
function showConfirm(name, desc) {
  document.getElementById("confirm-title").textContent =
    name === "plan" ? "计划审批" : "允许执行 " + name + " ？";
  document.getElementById("confirm-desc").textContent = desc;
  document.getElementById("confirm-modal").classList.remove("hidden");
}
function answerConfirm(approved) {
  document.getElementById("confirm-modal").classList.add("hidden");
  fetch("/api/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved: approved }) });
}
document.getElementById("btn-approve").addEventListener("click", () => answerConfirm(true));
document.getElementById("btn-reject").addEventListener("click", () => answerConfirm(false));

/* ---------- 提交 / 中断 ---------- */
function submit() {
  const task = input.value.trim();
  if (!task || running) return;
  input.value = "";
  setRunning(true);
  fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: task }) })
    .then(r => r.json())
    .then(d => { if (!d.ok) { addSystem("⚠ " + (d.message || "启动失败"), "error"); setRunning(false); } })
    .catch(() => { addSystem("⚠ 无法连接服务器", "error"); setRunning(false); });
}
btnRun.addEventListener("click", submit);
input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
btnInterrupt.addEventListener("click", () => fetch("/api/interrupt", { method: "POST" }));

/* ---------- 工作区 ---------- */
async function loadWorkspaceName() {
  try {
    const meta = await getJSON("/api/workspace");
    const name = meta.root.split(/[\\/]/).filter(Boolean).pop() || meta.root;
    document.getElementById("ws-name").textContent = name;
    document.getElementById("ws-name").title = meta.root;
  } catch (e) { }
}
async function loadSessions() {
  try {
    const meta = await getJSON("/api/workspace");
    const sel = document.getElementById("session-select");
    sel.innerHTML = meta.sessions.map(s =>
      `<option value="${esc(s.filename)}">${esc(s.name)}</option>`).join("");
    sel.value = meta.active;
    currentActiveSession = meta.active;
    if (!dataLoadedOnce) {
      dataLoadedOnce = true;
      const d = await getJSON("/api/session/messages?filename=" + encodeURIComponent(meta.active));
      renderTranscript(d.messages || []);
    }
  } catch (e) { }
}
let dataLoadedOnce = false;
document.getElementById("session-select").addEventListener("change", async e => {
  const fn = e.target.value;
  if (!fn || fn === currentActiveSession) return;
  const r = await fetch("/api/session/select", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: fn }) });
  const d = await r.json();
  if (d.ok) {
    dataLoadedOnce = true;
    currentActiveSession = fn;
    const m = await getJSON("/api/session/messages?filename=" + encodeURIComponent(fn));
    renderTranscript(m.messages || []);
  } else {
    addSystem("⚠ " + (d.message || "切换失败"), "error");
    loadSessions();
  }
});
document.getElementById("btn-session-new").addEventListener("click", async () => {
  await fetch("/api/session/new", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  dataLoadedOnce = false;       // 新建会话：回到空状态
  await loadSessions();
  showEmpty(true);
  chatCol.innerHTML = "";
});

/* ---------- 文件夹选择器 ---------- */
const wsModal = document.getElementById("workspace-modal");
document.getElementById("btn-workspace").addEventListener("click", openBrowseFileSystem);
function openBrowseFileSystem() {
  browseDir = "";
  renderBrowse();
  wsModal.classList.remove("hidden");
}
async function renderBrowse() {
  const d = await getJSON("/api/fs/browse?path=" + encodeURIComponent(browseDir));
  if (d.error) return;
  document.getElementById("browse-path").textContent = d.isRoot ? "计算机" : (d.path || "/");
  document.getElementById("browse-list").innerHTML = d.entries.map(e =>
    `<div class="browse-item dir" data-path="${esc(e.path)}">📁 ${esc(e.name)}</div>`).join("");
  document.querySelectorAll(".browse-item").forEach(x => {
    x.addEventListener("click", () => { browseDir = x.dataset.path; renderBrowse(); });
  });
}
document.getElementById("btn-browse-up").addEventListener("click", () => {
  if (!browseDir) return;
  const parts = browseDir.replace(/\\/g, "/").split("/").filter(Boolean);
  parts.pop();
  browseDir = parts.join("/");
  renderBrowse();
});
document.getElementById("btn-workspace-cancel").addEventListener("click", () => wsModal.classList.add("hidden"));
document.getElementById("btn-workspace-choose").addEventListener("click", async () => {
  const dir = browseDir;
  if (!dir) return;
  const r = await fetch("/api/workspace", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: dir }) });
  const d = await r.json();
  if (d.ok) {
    wsModal.classList.add("hidden");
    fileDir = ".";
    dataLoadedOnce = false;
    loadWorkspaceName(); loadSessions(); loadFiles();
    chatCol.innerHTML = "";
    showEmpty(true);
  } else {
    addSystem("⚠ " + (d.message || "切换工作区失败"), "error");
  }
});

/* ---------- 右侧文件面板 ---------- */
async function loadFiles() {
  try {
    const d = await getJSON("/api/files?path=" + encodeURIComponent(fileDir));
    if (d.error) { addSystem("⚠ " + d.error, "error"); return; }
    document.getElementById("files").innerHTML =
      (fileDir !== "."
        ? `<div class="file-item dir" data-path="${esc(parentOf(fileDir))}">↩ ..</div>`
        : "") +
      d.entries.map(e =>
        `<div class="file-item ${e.dir ? "dir" : "file"}" data-path="${esc(joinPath(fileDir, e.name))}">` +
        (e.dir ? "📁 " : "📄 ") + esc(e.name) + (e.dir ? "" : " (" + e.size + " B)") + "</div>").join("");
    document.querySelectorAll(".file-item").forEach(x => {
      x.addEventListener("click", () => { if (x.dataset.path) { fileDir = x.dataset.path; loadFiles(); } });
    });
  } catch (e) { }
}
document.getElementById("btn-file-up").addEventListener("click", () => {
  fileDir = parentOf(fileDir);
  loadFiles();
});

/* ---------- 工具（设置面板） ---------- */
async function loadTools() {
  try {
    const t = await getJSON("/api/tools");
    document.getElementById("tools").innerHTML = Object.entries(t)
      .map(([n, d]) => `<div class="tool-item" title="${esc(d)}"><b>${esc(n)}</b>${esc(d)}</div>`).join("");
  } catch (e) { }
}

/* ---------- 工具函数 ---------- */
async function getJSON(url) { const r = await fetch(url); return r.json(); }
function joinPath(a, b) { return a === "." ? b : a + "/" + b; }
function parentOf(p) { const i = p.lastIndexOf("/"); return i < 0 ? "." : p.slice(0, i) || "."; }

/* ---------- 事件流 ---------- */
const es = new EventSource("/api/events");
es.onmessage = e => { try { render(JSON.parse(e.data)); } catch (err) { } };

loadTools(); loadFiles(); loadWorkspaceName(); loadSessions();
