# MaoDingCode for VSCode

在编辑器里用对话的方式改代码。界面是 VSCode 的 Webview，干活的是本地那个 Python Agent 进程。

## 它是怎么工作的

```
Webview（聊天界面）  ⇄  extension.js  ⇄  python -m maodingcode --rpc
        postMessage            NDJSON / stdio
```

扩展本身**不实现任何 Agent 逻辑**，它只做三件事：拉起 Python 子进程、
把界面上的操作转成协议命令、把事件转成界面更新。

这样做的好处很实在：CLI 和插件共用同一份引擎，
你在 CLI 里调好的模型配置、权限策略、技能目录，插件里立刻就是一样的，
不会出现"两个实现、两套 bug"。

## 安装（开发模式）

不需要 npm install，不需要编译 —— 扩展是纯 JavaScript。

```bash
# 1. 确认 Python 侧能跑（先跑一次自检，排除环境问题）
cd ..                      # 到 4-code/：它是 maodingcode 包的导入根
python -m maodingcode --help
python maodingcode/tests/smoke_test.py
cd maodingcode             # 回到本目录

# 2. 用 VSCode 打开这个目录
code vscode-extension

# 3. 按 F5（Run Extension）会弹出一个新的「扩展开发宿主」窗口
#    在新窗口里打开你的项目文件夹，点左侧活动栏的 MaoDingCode 图标
```

也可以用打包方式装到当前 VSCode：

```bash
npx @vscode/vsce package          # 生成 maodingcode-0.1.0.vsix
code --install-extension maodingcode-0.1.0.vsix
```

## 使用前的配置

在 VSCode 设置里搜索 `maodingcode`：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `maodingcode.pythonPath` | `python` | Python 解释器路径（**需要 3.11+**）。找不到就用绝对路径。 |
| `maodingcode.workspace` | 空 | Agent 的工作区根目录。留空则用当前打开的文件夹。 |
| `maodingcode.importRoot` | 空 | `maodingcode` 包的导入根（即包含 `maodingcode/` 的那一层，本项目为 `4-code/`）。留空则按扩展位置自动推断 —— **开发模式（F5）下不用管**；用 `.vsix` 装到别处时需要手动填。 |
| `maodingcode.permissionMode` | `suggest` | 权限模式 |
| `maodingcode.autoApprove` | `false` | 自动同意所有确认（等于 auto，谨慎） |
| `maodingcode.noStream` | `false` | 关闭流式输出 |
| `maodingcode.maxSteps` | `25` | 单轮最大步数 |

**API Key 不在这里配** —— 放在工作区根目录的 `.env` 里：

```bash
# <你的项目>/.env
AI_PROVIDER=deepseek
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=sk-xxxx
AI_MODEL_NAME=deepseek-chat
```

这样密钥不会进 settings.json，也不容易被误提交。

## 功能

- **对话改代码** —— 说清楚要改什么，Agent 自己读文件、改文件、跑命令验证
- **流式输出** —— 正文逐字出现，不用干等
- **工具执行可见** —— 每一步调了什么工具、参数是什么、结果如何，都能展开看
- **权限确认卡片** —— 执行命令前会弹卡片，可以「允许 / 全部允许 / 拒绝」
- **发送选中代码** —— 编辑器里选中一段，右键 → "把选中代码发给助手"，自动带上 `文件:行号`
- **统计** —— 点顶栏 ∑ 查看本轮 token、缓存命中率、各阶段耗时、预估成本

## 快捷键与命令

| 操作 | 位置 |
|---|---|
| 打开对话面板 | 命令面板 → `MaoDingCode: 打开对话面板` |
| 发送选中代码 | 编辑器右键菜单 |
| 重启 Agent 进程 | 面板标题栏的刷新图标 |
| 查看本轮统计 | 面板顶栏 ∑ |

## 排查

**面板显示"启动失败"**
1. 检查 `maodingcode.pythonPath` 是否是 Python 3.11+；
2. 打开输出面板（视图 → 输出 → MaoDingCode）看 Python 的报错原文 ——
   其中 `[导入根]` 一行是实际注入的 `PYTHONPATH`，报 `No module named maodingcode` 时先看它；
3. 在终端里手动跑一次（在 `4-code/` 目录下，它是包的导入根）
   `python -m maodingcode --rpc -C <工作区>`，看能不能起来。

**报 `No module named maodingcode`**
扩展会把「导入根」以 `PYTHONPATH` 注入子进程，免得受当前工作区影响。
导入根默认按扩展自身位置推断（`vscode-extension/` 的上一级的上一级），
**开发模式（F5）下就是对的**；如果是 `npx @vscode/vsce package` 装出来的 `.vsix`，
扩展已经不在项目目录里了，需要在设置里显式填 `maodingcode.importRoot`（本项目填到 `4-code/`）。

**连上了但一发消息就报"缺少 API Key"**
`.env` 要放在 `maodingcode.workspace` 指向的目录（或它的上级）里。
配置查找是从工作区向上逐级找 `maodingcode.toml`，
`.env` 则只读工作区根目录那一份。

**改了设置没生效**
配置在进程启动时读取，改完点面板标题栏的重启按钮。

**想中断一个跑飞的任务**
点顶栏的 ■。中断是在轮次边界生效的，不会把正在写的文件切成两半。
