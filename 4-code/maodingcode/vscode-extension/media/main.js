/**
 * Webview 前端。只做一件事：把扩展转发过来的 Agent 事件渲染成聊天气泡。
 *
 * 渲染原则：
 *   - 模型输出一律走 textContent，不用 innerHTML —— webview 里的内容
 *     来自模型和文件，直接拼 HTML 等于开了个注入口子；
 *   - 代码块只在"消息结束"后做一次解析，避免流式过程中反复重排导致抖动；
 *   - 工具卡片默认折叠，出错时自动展开 —— 正常流程不该刷屏。
 */

const vscode = acquireVsCodeApi();

const log = document.getElementById("log");
const input = document.getElementById("input");
const btnSend = document.getElementById("btnSend");
const btnAbort = document.getElementById("btnAbort");
const btnClear = document.getElementById("btnClear");
const btnStats = document.getElementById("btnStats");
const modelLabel = document.getElementById("model");
const dot = document.getElementById("dot");
const hint = document.getElementById("hint");

let running = false;
let streamBubble = null; // 正在流式输出的气泡
let streamText = "";
let tools = new Map(); // 工具卡片：name -> 元素，用于配对 start 与 result

// --------------------------------------------------------------------- 小工具
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollToBottom() {
  log.scrollTop = log.scrollHeight;
}

function setRunning(value) {
  running = value;
  btnSend.disabled = value;
  btnAbort.disabled = !value;
  hint.textContent = value ? "执行中…点 ■ 可中断" : "";
}

// --------------------------------------------------------------------- 渲染
function addUser(text) {
  const box = el("div", "msg user");
  box.appendChild(el("span", "role", "你"));
  box.appendChild(document.createTextNode(text));
  log.appendChild(box);
  scrollToBottom();
}

function addNotice(text, isError) {
  log.appendChild(el("div", "notice" + (isError ? " error" : ""), text));
  scrollToBottom();
}

/** 把一个气泡里的纯文本按 ``` 围栏切成 文本 + <pre> 片段。 */
function renderRich(box, text) {
  // 清掉除 role 标签之外的内容
  while (box.childNodes.length > 1) {
    box.removeChild(box.lastChild);
  }
  const parts = text.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      // 围栏内容：第一行可能是语言标记，去掉
      const body = part.replace(/^[a-zA-Z0-9_+-]*\n/, "");
      box.appendChild(el("pre", null, body.replace(/\n$/, "")));
    } else if (part) {
      box.appendChild(document.createTextNode(part));
    }
  });
}

function ensureStreamBubble() {
  if (streamBubble) return streamBubble;
  const box = el("div", "msg assistant cursor");
  box.appendChild(el("span", "role", "MaoDingCode"));
  box.appendChild(document.createTextNode(""));
  log.appendChild(box);
  streamBubble = box;
  streamText = "";
  return box;
}

function appendDelta(text) {
  const box = ensureStreamBubble();
  streamText += text;
  box.lastChild.nodeValue = streamText;
  scrollToBottom();
}

function finishStream() {
  if (!streamBubble) return;
  streamBubble.classList.remove("cursor");
  renderRich(streamBubble, streamText);
  streamBubble = null;
  streamText = "";
  scrollToBottom();
}

// --------------------------------------------------------------------- 工具卡
function toolStart(event) {
  finishStream();
  const card = el("div", "tool");
  const head = el("div", "head");
  head.appendChild(el("span", "name", event.name || "tool"));
  const meta = el("span", "meta", "运行中…");
  meta.insertBefore(el("span", "spin", "◐"), meta.firstChild);
  head.appendChild(meta);
  card.appendChild(head);
  card.appendChild(el("div", "body", formatArgs(event.args)));
  card.addEventListener("click", () => card.classList.toggle("collapsed"));
  card.classList.add("collapsed");
  log.appendChild(card);
  tools.set(event.name, card);
  scrollToBottom();
}

function toolResult(event) {
  const card = tools.get(event.name);
  if (!card) return;
  tools.delete(event.name);
  card.classList.remove("collapsed");
  card.classList.add(event.ok ? "ok" : "fail");
  const meta = card.querySelector(".meta");
  meta.textContent = event.ok
    ? "完成 " + Number(event.elapsed || 0).toFixed(2) + "s"
    : "失败";
  meta.insertBefore(el("span", null, event.ok ? "✓ " : "✗ "), meta.firstChild);
  card.querySelector(".body").textContent = String(event.output || "");
  if (event.ok) {
    card.classList.add("collapsed"); // 成功的折叠，失败的一直展开
  }
  scrollToBottom();
}

function formatArgs(args) {
  if (!args) return "";
  try {
    return JSON.stringify(args, null, 2);
  } catch (err) {
    return String(args);
  }
}

// --------------------------------------------------------------------- 确认卡
function needsConfirm(event) {
  finishStream();
  const card = el("div", "confirm");
  card.appendChild(el("div", "title", "需要确认：" + event.tool));
  card.appendChild(el("div", "reason", event.reason || ""));
  card.appendChild(el("pre", null, formatArgs(event.args)));

  const buttons = el("div", "buttons");
  const mk = (label, allow, all, cls) => {
    const b = el("button", cls, label);
    b.addEventListener("click", () => {
      card.classList.add("done");
      vscode.postMessage({ type: "confirm", id: event.id, allow, all });
    });
    return b;
  };
  buttons.appendChild(mk("允许", true, false, "primary"));
  buttons.appendChild(mk("全部允许", true, true, "secondary"));
  buttons.appendChild(mk("拒绝", false, false, "secondary"));
  card.appendChild(buttons);

  log.appendChild(card);
  scrollToBottom();
}

function showStats(data) {
  if (!data) return;
  const cost = data.cost === null || data.cost === undefined ? "未配置单价" : data.cost;
  const lines = [
    "步数        " + data.steps,
    "工具调用    " + data.tool_calls + "（失败 " + data.failed_tools + "）",
    "输入 token  " + data.prompt_tokens + "（命中缓存 " + data.cached_tokens + "）",
    "输出 token  " + data.completion_tokens,
    "缓存命中率  " + Math.round((data.cache_hit_rate || 0) * 100) + "%",
    "耗时        模型 " + data.model_seconds + "s / 工具 " + data.tool_seconds + "s",
    "预估成本    " + cost,
  ];
  log.appendChild(el("div", "stats", lines.join("\n")));
  scrollToBottom();
}

// --------------------------------------------------------------------- 事件入口
window.addEventListener("message", (ev) => {
  const e = ev.data || {};
  switch (e.type) {
    case "ready":
      dot.className = "dot on";
      modelLabel.textContent =
        e.model + " · " + (e.permissionMode || "suggest") + " · " + (e.tools || 0) + " 工具";
      modelLabel.title = "工作区：" + e.workspace;
      addNotice(
        "已连接" + (e.streaming ? "（流式）" : "（非流式）") + "，工作区 " + e.workspace
      );
      break;

    case "status":
      dot.className = "dot " + (e.connected ? "on" : "off");
      break;

    case "user_message":
      // 由前端自己插入过了，这里忽略，避免重复
      break;

    case "delta":
      appendDelta(String(e.text || ""));
      break;

    case "model_response":
      if (e.tool_calls && e.tool_calls.length) {
        finishStream();
      }
      break;

    case "tool_start":
      toolStart(e);
      break;

    case "tool_result":
      toolResult(e);
      break;

    case "needs_confirm":
      needsConfirm(e);
      break;

    case "assistant_message":
      // 最终正文：流式时已经渲染过；非流式时在这里补上
      if (!streamBubble && e.text) {
        const box = el("div", "msg assistant");
        box.appendChild(el("span", "role", "MaoDingCode"));
        log.appendChild(box);
        renderRich(box, String(e.text));
        scrollToBottom();
      }
      break;

    case "done":
      finishStream();
      setRunning(false);
      if (e.reason && e.reason !== "completed") {
        addNotice("结束原因：" + e.reason + (e.content ? " — " + e.content : ""), true);
      }
      if (e.cost) {
        showStats(e.cost);
      }
      break;

    case "error":
      finishStream();
      addNotice("错误：" + (e.message || ""), true);
      break;

    case "warning":
      addNotice("提示：" + (e.message || ""));
      break;

    case "fatal":
      finishStream();
      setRunning(false);
      dot.className = "dot off";
      addNotice(String(e.message || "启动失败"), true);
      break;

    case "info":
      addNotice(String(e.message || ""));
      break;

    case "cost":
      showStats(e.data);
      break;

    case "config":
      log.appendChild(el("div", "stats", JSON.stringify(e.data, null, 2)));
      scrollToBottom();
      break;

    case "prefill":
      input.value = (input.value || "") + String(e.text || "");
      input.focus();
      break;

    case "exited":
      finishStream();
      setRunning(false);
      dot.className = "dot off";
      addNotice("Agent 进程已退出（code=" + e.code + "）。用「重启 Agent 进程」重连。", true);
      break;

    default:
      break;
  }
});

// --------------------------------------------------------------------- 交互
function send() {
  const text = (input.value || "").trim();
  if (!text || running) return;
  addUser(text);
  input.value = "";
  setRunning(true);
  vscode.postMessage({ type: "run", prompt: text });
}

btnSend.addEventListener("click", send);

btnAbort.addEventListener("click", () => {
  vscode.postMessage({ type: "abort" });
  addNotice("已请求中断");
});

btnClear.addEventListener("click", () => {
  vscode.postMessage({ type: "command", cmd: "clear" });
  log.innerHTML = "";
  addNotice("对话已清空");
});

btnStats.addEventListener("click", () => {
  vscode.postMessage({ type: "command", cmd: "cost" });
});

input.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    send();
  }
});

setRunning(false);
vscode.postMessage({ type: "webviewReady" });
