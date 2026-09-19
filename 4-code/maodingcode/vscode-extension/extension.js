/**
 * MaoDingCode VSCode 扩展的主进程。
 *
 * 架构很简单，一句话：**把 VSCode 的 Webview 接到 Python Agent 进程的 stdio 上。**
 *
 *     Webview（前端界面）  ⇄  extension.js（本文件）  ⇄  python -m maodingcode --rpc
 *               postMessage               NDJSON / stdio
 *
 * 为什么让 Python 跑在子进程里，而不是用 JS 重写一遍 Agent？
 *   - 复用同一份引擎，CLI 和插件行为天然一致，不会出现"两个实现、两个 bug"；
 *   - 用户换个模型、改个权限策略，两边同时生效；
 *   - 前端崩了、重载了，Agent 状态还在。
 *
 * 用纯 JS 写、不做 TypeScript 编译，是刻意的：扩展本身只有几百行，
 * 加一套构建链会让"改一行 → 重新打包 → 重载"的循环变得很重。
 */

const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");

/** @type {AgentProcess | undefined} */
let agent;
/** @type {ChatViewProvider | undefined} */
let provider;

// --------------------------------------------------------------------- Agent 进程
class AgentProcess {
  constructor(context, workspace) {
    this.context = context;
    this.workspace = workspace;
    this.child = undefined;
    this.buffer = "";
    this._emitter = new vscode.EventEmitter();
    this.onEvent = this._emitter.event;
    this.output = vscode.window.createOutputChannel("MaoDingCode");
  }

  get running() {
    return Boolean(this.child && this.child.exitCode === null && !this.child.killed);
  }

  /** 组装启动命令。参数从 VSCode 配置读，改配置后重启进程即可生效。 */
  buildArgs() {
    const cfg = vscode.workspace.getConfiguration("maodingcode");
    const args = ["-m", "maodingcode", "--rpc", "-C", this.workspace];
    const mode = cfg.get("autoApprove") ? "auto" : cfg.get("permissionMode") || "suggest";
    args.push("--mode", mode);
    if (cfg.get("noStream")) {
      args.push("--no-stream");
    }
    const maxSteps = Number(cfg.get("maxSteps") || 0);
    if (maxSteps > 0) {
      args.push("--max-steps", String(maxSteps));
    }
    return args;
  }

  start() {
    if (this.running) {
      return;
    }
    const cfg = vscode.workspace.getConfiguration("maodingcode");
    const python = cfg.get("pythonPath") || "python";
    const args = this.buildArgs();

    // maodingcode 包的导入根 = 项目目录的上一级。
    // 本项目把「项目根」与「包目录」拍平成了一层（见 maodingcode/README.md），
    // 所以 `python -m maodingcode` 要求导入根在 sys.path 上。
    // 必须显式注入 PYTHONPATH：否则 -m 以「用户工作区」为基准解析模块，
    // 只有在工作区恰好等于导入根时才起得来。前面的位置保证优先于用户环境里的同名项。
    // 默认按扩展自身位置推断（开发模式下正确）；用 .vsix 装到别处时靠 importRoot 覆盖。
    const configuredImportRoot = cfg.get("importRoot");
    const importRoot = configuredImportRoot
      ? path.resolve(configuredImportRoot)
      : path.resolve(__dirname, "..", "..");
    const pythonPath = [importRoot, process.env.PYTHONPATH]
      .filter(Boolean)
      .join(path.delimiter);

    this.output.appendLine(`[启动] ${python} ${args.join(" ")}`);
    this.output.appendLine(`[工作区] ${this.workspace}`);
    this.output.appendLine(`[导入根] ${importRoot}`);

    try {
      this.child = cp.spawn(python, args, {
        cwd: this.workspace,
        env: {
          ...process.env,
          PYTHONPATH: pythonPath,
          PYTHONIOENCODING: "utf-8",
          PYTHONUNBUFFERED: "1",
        },
        windowsHide: true,
      });
    } catch (err) {
      this._emit({ type: "fatal", message: `无法启动 Python：${err.message}` });
      return;
    }

    this.child.stdout.setEncoding("utf8");
    this.child.stderr.setEncoding("utf8");

    this.child.stdout.on("data", (chunk) => this._onStdout(chunk));
    this.child.stderr.on("data", (chunk) => {
      this.output.append(chunk);
    });
    this.child.on("error", (err) => {
      this._emit({
        type: "fatal",
        message:
          `启动失败：${err.message}\n` +
          `请检查设置 maodingcode.pythonPath 指向 Python 3.11+ 解释器。`,
      });
    });
    this.child.on("exit", (code, signal) => {
      this.output.appendLine(`[退出] code=${code} signal=${signal}`);
      this._emit({ type: "exited", code, signal });
      this.child = undefined;
    });
  }

  /** stdout 是 NDJSON：一行一个 JSON。必须按行切，不能假设一次 data 是一整条。 */
  _onStdout(chunk) {
    this.buffer += chunk;
    let index;
    while ((index = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (!line) {
        continue;
      }
      try {
        this._emit(JSON.parse(line));
      } catch (err) {
        this.output.appendLine(`[解析失败] ${line.slice(0, 200)}`);
      }
    }
  }

  _emit(event) {
    this._emitter.fire(event);
  }

  send(payload) {
    if (!this.running) {
      return false;
    }
    try {
      this.child.stdin.write(JSON.stringify(payload) + "\n");
      return true;
    } catch (err) {
      this.output.appendLine(`[发送失败] ${err.message}`);
      return false;
    }
  }

  stop() {
    if (!this.child) {
      return;
    }
    try {
      this.child.stdin.end();
    } catch (err) {
      /* 已经关了就算了 */
    }
    const child = this.child;
    this.child = undefined;
    setTimeout(() => {
      if (child.exitCode === null && !child.killed) {
        child.kill();
      }
    }, 1500);
  }

  dispose() {
    this.stop();
    this._emitter.dispose();
    this.output.dispose();
  }
}

// --------------------------------------------------------------------- Webview
class ChatViewProvider {
  constructor(context, getAgent) {
    this.context = context;
    this.getAgent = getAgent;
    this.view = undefined;
    this.webviewReady = false;
    // 进程可能在界面还没建好之前就发了 ready，先缓存住
    this.pending = [];
  }

  resolveWebviewView(webviewView) {
    this.view = webviewView;
    this.webviewReady = false;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, "media")],
    };
    webviewView.webview.html = this._html(webviewView.webview);

    webviewView.webview.onDidReceiveMessage((msg) => this._onMessage(msg));

    webviewView.onDidDispose(() => {
      this.view = undefined;
      this.webviewReady = false;
    });
  }

  _onMessage(msg) {
    const agent = this.getAgent();
    if (!agent) {
      return;
    }
    switch (msg.type) {
      case "webviewReady":
        this.webviewReady = true;
        // 界面重载时把缓存的事件补发，避免"进度条卡住"
        for (const event of this.pending.splice(0)) {
          this.post(event);
        }
        this.post({ type: "status", connected: agent.running, workspace: agent.workspace });
        break;
      case "run":
        this.pending.length = 0;
        if (!agent.send({ cmd: "run", prompt: msg.prompt })) {
          this.post({ type: "fatal", message: "Agent 进程没有在运行，请先重启。" });
        }
        break;
      case "abort":
        agent.send({ cmd: "abort" });
        break;
      case "confirm":
        agent.send({ cmd: "confirm_response", id: msg.id, allow: msg.allow, all: msg.all });
        break;
      case "command":
        this.pending.length = 0;
        agent.send({ cmd: msg.cmd });
        break;
      case "openOutput":
        agent.output.show();
        break;
      default:
        break;
    }
  }

  /** 把 Agent 的事件推给界面。界面还没建好就先缓存。 */
  post(event) {
    if (!this.view || !this.webviewReady) {
      if (event.type !== "delta") {
        this.pending.push(event);
      }
      return;
    }
    this.view.webview.postMessage(event);
  }

  reveal() {
    if (this.view) {
      this.view.show(true);
    } else {
      vscode.commands.executeCommand("maodingcode.chat.focus");
    }
    return this;
  }

  _html(webview) {
    const asset = (name) =>
      webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, "media", name));
    const nonce = String(Date.now()) + Math.random().toString(36).slice(2);
    const csp = [
      "default-src 'none'",
      "style-src " + webview.cspSource,
      "script-src 'nonce-" + nonce + "'",
      "img-src " + webview.cspSource + " data:",
    ].join("; ");

    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<link href="${asset("style.css")}" rel="stylesheet">
<title>MaoDingCode</title>
</head>
<body>
  <div id="app">
    <header id="bar">
      <span id="dot" class="dot off"></span>
      <span id="model">连接中…</span>
      <span class="spacer"></span>
      <button id="btnAbort" class="ghost" title="中断当前任务">■</button>
      <button id="btnStats" class="ghost" title="查看 token 与成本">∑</button>
      <button id="btnClear" class="ghost" title="清空对话">🗑</button>
    </header>
    <main id="log"></main>
    <footer id="composer">
      <textarea id="input" rows="3" placeholder="描述你要做的改动…（Enter 发送 / Shift+Enter 换行）"></textarea>
      <div class="row">
        <span id="hint" class="hint"></span>
        <button id="btnSend" class="primary">发送</button>
      </div>
    </footer>
  </div>
  <script nonce="${nonce}" src="${asset("main.js")}"></script>
</body>
</html>`;
  }
}

// --------------------------------------------------------------------- 激活
function activate(context) {
  const workspace = resolveWorkspace();
  agent = new AgentProcess(context, workspace);
  provider = new ChatViewProvider(context, () => agent);

  agent.onEvent((event) => {
    if (event.type === "ready") {
      provider.post({
        type: "ready",
        model: event.model,
        workspace: event.workspace,
        permissionMode: event.permission_mode,
        streaming: event.streaming,
        tools: event.tools,
        skills: event.skills,
      });
      return;
    }
    provider.post(event);
  });

  agent.start();

  context.subscriptions.push(
    agent,
    vscode.window.registerWebviewViewProvider("maodingcode.chat", provider, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand("maodingcode.openChat", () => provider.reveal()),
    vscode.commands.registerCommand("maodingcode.restart", () => {
      agent.stop();
      setTimeout(() => agent.start(), 300);
      vscode.window.showInformationMessage("MaoDingCode：Agent 进程已重启");
    }),
    vscode.commands.registerCommand("maodingcode.showStats", () => {
      agent.send({ cmd: "cost" });
    }),
    vscode.commands.registerCommand("maodingcode.sendSelection", () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage("没有打开的编辑器");
        return;
      }
      const text = editor.document.getText(editor.selection);
      if (!text.trim()) {
        vscode.window.showWarningMessage("没有选中的代码");
        return;
      }
      const rel = vscode.workspace.asRelativePath(editor.document.uri);
      const start = editor.selection.start.line + 1;
      const end = editor.selection.end.line + 1;
      provider.reveal().post({
        type: "prefill",
        text: `参考 ${rel}:${start}-${end}：\n\n\`\`\`\n${text}\n\`\`\`\n\n`,
      });
    }),
    vscode.commands.registerCommand("maodingcode.sendFile", () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        return;
      }
      const rel = vscode.workspace.asRelativePath(editor.document.uri);
      provider.reveal().post({ type: "prefill", text: `看一下 ${rel} ` });
    }),
  );
}

function resolveWorkspace() {
  const configured = vscode.workspace.getConfiguration("maodingcode").get("workspace");
  if (configured && configured.trim()) {
    return configured.trim();
  }
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length > 0) {
    return folders[0].uri.fsPath;
  }
  return process.cwd();
}

function deactivate() {
  if (agent) {
    agent.dispose();
    agent = undefined;
  }
  provider = undefined;
}

module.exports = { activate, deactivate };
