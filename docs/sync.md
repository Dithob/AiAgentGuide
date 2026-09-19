# 多端同步（错题池 / 组卷参数 / 答题历史）

> 操作步骤的精简版在 [`../README.md`](../README.md)；这里是完整版 + 设计要点。

## 为什么需要

答题状态存在浏览器 `localStorage` 里，换设备就没了。题库正文由 git 管，
但「你错过哪些题」必须跨端带上。

## 怎么用（每台设备各做一次）

1. 打开测验页 → 点工具栏右侧 **☁️ 同步** → 展开面板
2. 去 GitHub → Settings → Developer settings → Personal access tokens 生成 token，
   **只勾 `gist` 一项**（classic token；fine-grained 选 `Gists: write`）
3. 把 token 粘进面板 → 点「连接」→ 自动上传，之后每台设备填同一个 token 即可互通

## 同步范围

| 同步（走 Gist） | 存储 key |
| :--- | :--- |
| 错题池 | `aag_quiz_wrong_v3` |
| 组卷参数 | `aag_quiz_compose_v1` |
| 答题历史（每次交卷的时间/正确率） | `aag_quiz_hist_v1` |

**不同步**：题库正文（git 管）、答题卡折叠态（纯 UI 偏好）。

## 设计要点

- **凭证隔离**：token / gistId 存在 `aag_quiz_cs_v1`，**绝不上云、绝不写进页面文件**。
  上云 payload 只含 `{app, version, exportedAt, wrong, compose, hist}`。
- **冲突策略**：拒绝覆盖 + 手动确认。云端比本机新时，自动上传静默暂停并提示你手动下载。
- **安静通道**：同步动作自身写状态（更新 `syncedAt`）标记为 silent，
  不更新 `lastLocalWrite`，否则会形成 `写状态→判定有改动→上传→写状态` 的死循环烧光限流。
- **设备关联**：新设备只有 token、没有 gistId 时，按 Gist 描述自动找回同一份（取 `updated_at` 最新），
  避免误建第二个 Gist 导致「A 传的 B 看不到」。
- **无需后端**：`api.github.com` 官方支持任意 origin 的 CORS 且放行 `Authorization` 头，浏览器可直连。
  `file://` 双击打开也能用（拦截的是本地文件 fetch，远程 HTTPS 允许）。

> ⚠️ token 存在 localStorage，任何能跑在该页面的脚本都能读走 → **务必只勾 `gist` 权限**。
> 仓库是 public，但 token 不在文件里，只在你的浏览器里。
