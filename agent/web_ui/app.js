/* Coding Agent Web UI —— 事件流的浏览器渲染器 */
"use strict";

const chat = document.getElementById("chat");
const input = document.getElementById("input");
const btnRun = document.getElementById("btn-run");
const btnInterrupt = document.getElementById("btn-interrupt");
const statusText = document.getElementById("status-text");
const statusDot = document.getElementById("status-dot");

let running = false;
let lastAssistant = null;
let lastCommandPre = null;
const toolCards = new Map();

/* ---------- 基础 ---------- */
function setRunning(v) {
  running = v;
  btnRun.disabled = v;
  btnInterrupt.disabled = !v;
  statusText.textContent = v ? "运行中" : "空闲";
  statusDot.className = v ? "on" : "";
}
function scrollToBottom() {
  const near = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 260;
  if (near) chat.scrollTop = chat.scrollHeight;
}
function addBubble(cls, text) {
  const b = document.createElement("div");
  b.className = "bubble " + cls;
  if (text !== undefined) b.textContent = text;
  chat.appendChild(b);
  scrollToBottom();
  return b;
}
function addSystem(text, cls) {
  const b = document.createElement("div");
  b.className = "note " + (cls || "");
  b.textContent = text;
  chat.appendChild(b);
  scrollToBottom();
}

/* ---------- 事件渲染 ---------- */
function render(ev) {
  switch (ev.type) {
    case "UserMessage":
      addBubble("user", ev.content);
      lastAssistant = null; lastCommandPre = null;
      break;
    case "TextDelta":
      if (!lastAssistant) lastAssistant = addBubble("assistant", "");
      lastAssistant.textContent += ev.text;
      scrollToBottom();
      break;
    case "ToolCallEvent": {
      const card = document.createElement("div");
      card.className = "tool-card";
      const head = document.createElement("div");
      head.className = "tool-head";
      head.innerHTML = "<span>🔧</span><span>" + esc(ev.name) + "</span>";
      const argsPre = document.createElement("pre");
      argsPre.className = "tool-args";
      argsPre.textContent = safeJson(ev.arguments);
      const resultDiv = document.createElement("pre");
      resultDiv.className = "tool-result hidden";
      head.addEventListener("click", () => card.classList.toggle("open"));
      card.appendChild(head);
      card.appendChild(argsPre);
      card.appendChild(resultDiv);
      chat.appendChild(card);
      toolCards.set(ev.call_id, { card, resultDiv });
      scrollToBottom();
      break;
    }
    case "CommandOutput":
      if (!lastCommandPre) {
        lastCommandPre = document.createElement("pre");
        lastCommandPre.className = "cmd-output";
        chat.appendChild(lastCommandPre);
      }
      lastCommandPre.textContent += ev.text;
      scrollToBottom();
      break;
    case "ToolResultEvent": {
      const t = toolCards.get(ev.call_id);
      if (t) {
        t.resultDiv.textContent = (ev.ok ? "✓ " : "✗ ") + ev.output;
        t.resultDiv.classList.remove("hidden");
        t.card.classList.add("open");
        t.card.dataset.ok = ev.ok ? "ok" : "fail";
      }
      lastCommandPre = null;
      break;
    }
    case "StepEvent":
      addSystem("step " + ev.step + "/" + ev.max_steps, "step");
      break;
    case "TrimmedEvent":
      addSystem("[上下文] 预算紧张，已裁剪最老的 " + ev.rounds + " 轮工具调用");
      break;
    case "CompactedEvent":
      addSystem(ev.summarized
        ? "[上下文] 已把早期 " + ev.messages_removed + " 条消息压缩为摘要"
        : "[上下文] 已丢弃早期 " + ev.messages_removed + " 条消息");
      break;
    case "ErrorEvent":
      addSystem("⚠ " + ev.message, "error");
      break;
    case "FinishEvent":
      addBubble("finish", "✅ " + ev.summary);
      break;
    case "Notice":
      addSystem(ev.message);
      break;
    case "AskConfirm":
      showConfirm(ev.name, ev.desc);
      break;
    case "RunResult":
      setRunning(false);
      if (ev.status === "finished") addSystem("[完成] " + (ev.summary || ""), "ok");
      else addSystem("[" + ev.status + "] " + (ev.message || ""), "error");
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
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function safeJson(o) {
  try { return JSON.stringify(o, null, 2); } catch (e) { return String(o); }
}

/* ---------- 审批弹窗 ---------- */
function showConfirm(name, desc) {
  document.getElementById("confirm-title").textContent =
    name === "plan" ? "计划审批" : "允许执行 " + name + " ？";
  document.getElementById("confirm-desc").textContent = desc;
  document.getElementById("confirm-modal").classList.remove("hidden");
}
function answerConfirm(approved) {
  document.getElementById("confirm-modal").classList.add("hidden");
  fetch("/api/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved: approved }),
  });
}
document.getElementById("btn-approve").addEventListener("click", () => answerConfirm(true));
document.getElementById("btn-reject").addEventListener("click", () => answerConfirm(false));

/* ---------- 提交 / 中断 ---------- */
function submit() {
  const task = input.value.trim();
  if (!task || running) return;
  input.value = "";
  setRunning(true);
  fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: task }),
  })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { addSystem("⚠ " + (d.message || "启动失败"), "error"); setRunning(false); }
    })
    .catch(() => { addSystem("⚠ 无法连接服务器", "error"); setRunning(false); });
}
btnRun.addEventListener("click", submit);
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
});
btnInterrupt.addEventListener("click", () =>
  fetch("/api/interrupt", { method: "POST" }));

/* ---------- 侧栏 ---------- */
async function loadTools() {
  try {
    const r = await fetch("/api/tools");
    const t = await r.json();
    document.getElementById("tools").innerHTML = Object.entries(t)
      .map(([n, d]) => `<div class="tool-item" title="${esc(d)}"><b>${esc(n)}</b>${esc(d.slice(0, 36))}</div>`)
      .join("");
  } catch (e) { /* 忽略 */ }
}
let filePath = ".";
async function loadFiles() {
  try {
    const r = await fetch("/api/files?path=" + encodeURIComponent(filePath));
    const d = await r.json();
    if (d.error) return;
    const html = (filePath !== "."
      ? `<div class="file-item dir" data-path="${esc(parentOf(filePath))}">↩ ..</div>`
      : "") +
      d.entries.map(e =>
        `<div class="file-item ${e.dir ? "dir" : "file"}" data-path="${esc(joinPath(filePath, e.name))}">` +
        (e.dir ? "📁 " : "📄 ") + esc(e.name) +
        (e.dir ? "" : " (" + e.size + " B)") + "</div>").join("");
    document.getElementById("files").innerHTML = html;
    document.querySelectorAll(".file-item").forEach(x => {
      x.addEventListener("click", () => { if (x.dataset.path) { filePath = x.dataset.path; loadFiles(); } });
    });
  } catch (e) { /* 忽略 */ }
}
async function loadSessions() {
  try {
    const r = await fetch("/api/sessions");
    const s = await r.json();
    document.getElementById("sessions").innerHTML = s
      .map(f => `<div class="session-item" data-name="${esc(f.name)}">${esc(f.name)}</div>`)
      .join("");
    document.querySelectorAll(".session-item").forEach(x => {
      x.addEventListener("click", () =>
        fetch("/api/resume", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session: x.dataset.name }),
        }));
    });
  } catch (e) { /* 忽略 */ }
}
function joinPath(a, b) { return a === "." ? b : a + "/" + b; }
function parentOf(p) { const i = p.lastIndexOf("/"); return i < 0 ? "." : p.slice(0, i) || "."; }

/* ---------- 事件流 ---------- */
const es = new EventSource("/api/events");
es.onmessage = e => { try { render(JSON.parse(e.data)); } catch (err) { /* 忽略坏帧 */ } };

loadTools(); loadFiles(); loadSessions();
