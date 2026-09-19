# GitHub Pages 发布与 Jekyll 坑

线上：<https://dithob.github.io/AiAgentGuide/>
发布源 = 分支 `main` / 根目录，线上只提供两个静态页：根 `index.html`（工作台）与 `3-测验/index.html`（测验工具）。

## ⚠️ 别删根目录的 `.nojekyll` 与 `_config.yml`

GitHub Pages 默认会用 Jekyll 处理仓库里所有 Markdown，而笔记里写着 Jinja2 模板语法 `{% if %}` / `{% for %}` / `{{ var }}`，Jekyll 会把它当成 Liquid 模板解析，报：

```
Liquid syntax error (line N): Syntax Error in tag 'if' - Valid syntax: if [expression]
```

2026-09-14 20:29 落笔 ch04（第一条含 Jinja2 语法的记录）后，`pages-build-deployment` 就此连续失败，线上站点一直冻结在 09-14 的版本。现在用两道保险挡住：

| 文件 | 作用 |
| :--- | :--- |
| `.nojekyll` | 让 GitHub 直接跳过 Jekyll 构建（首选手段） |
| `_config.yml` | `exclude` 掉 `0-记录区/`、`1-知识库/`、`2-面试题库/`、`docs/` 等源目录；万一 Jekyll 仍执行，也不会碰这些文件 |

排查线上是否正常：

```bash
gh run list -R Dithob/AiAgentGuide -L 3
```

看 `pages build and deployment` 是否为 `success`。

> 新增顶层文档目录后，记得同步进 `_config.yml` 的 `exclude`。
