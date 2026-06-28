---
id: TASK-004
title: "SSR 前端壳:base 布局 + 响应式/e-ink CSS + 轮询/meta-refresh 降级"
status: review
priority: P0
architecture: ARCH-001
dependencies: [TASK-001]
estimated_effort: M
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 SSR 前端壳:Jinja2 base 布局 + 单屏总览容器、响应式 + e-ink CSS(三断点、色+形+文三重状态编码)、以及刷新降级(iPhone/iPad fetch 轮询 30–60s,Kindle `<meta refresh>`)。本卡只做"空壳 + 数据源状态条",模块卡通过新增 partial 往栅格里填卡片。

## 技术规格

### 文件与路由

```
src/panel/web/
├── routes.py                          # GET / 渲染 index.html
├── templates/
│   ├── base.html                      # 页面壳
│   ├── index.html                     # 单屏总览,栅格容器 + include partials
│   └── partials/
│       └── _datasource_status.html    # 数据源状态条(stale/down)
└── static/
    ├── css/panel.css
    └── js/panel.js
```

- `GET /` → 渲染 `index.html`,context 含各 collector 最近运行状态(`repo.get_all_last_runs()` + stale 判定)。
- 静态资源经 `StaticFiles` 挂载于 `/static`(TASK-001 已挂载;确认路径)。
- Jinja2 `Environment` 自动转义开启。

### base.html 约定(权威,模块卡依赖)

- `<head>` 内含:`<meta name="viewport" content="width=device-width, initial-scale=1">`;**条件 meta refresh** —— 当 `is_eink`(由 UA 或查询参 `?eink=1` 判定)为真时输出 `<meta http-equiv="refresh" content="60">`,否则不输出(交给 JS 轮询)。
- 引入 `/static/css/panel.css` 与(非 e-ink 时)`/static/js/panel.js`。
- 定义 Jinja2 block:`{% block head %}` / `{% block content %}` / `{% block scripts %}`。
- 主容器:`<main class="panel-grid" id="panel-grid">`(单屏栅格)。
- 模块 partial 约定:文件命名 `partials/_<module>.html`(如 `_vm_card.html`、`_node_card.html`),`index.html` 用 `{% include "partials/_xxx.html" %}` 注入;每个模块卡块用 `<section class="card" data-module="<name>">` 包裹。

### CSS 规范(panel.css)

三断点(响应式栅格):

| 断点 | 目标设备 | 列数/策略 |
|------|----------|-----------|
| `< 600px` | iPhone 竖屏 / Kindle | 单列堆叠 |
| `600–1024px` | iPad 竖屏 | 2 列 |
| `> 1024px` | iPad 横屏 / 桌面 | 3+ 列(`auto-fill, minmax(...)`) |

栅格用 CSS Grid `repeat(auto-fill, minmax(280px, 1fr))` 配合断点微调。

**色 + 形 + 文 三重状态编码**(e-ink 无彩色,必须形/文兜底):

| 状态 | 颜色 | 形状符号 | 文字 |
|------|------|----------|------|
| ok / online / running | 绿 | ● 实心圆 | "在线"/"运行中" |
| warn / 阈值告警 | 黄/橙 | ◐ 半实心 | "告警" |
| error / unreachable / offline | 红 | ○ 空心圆 | "离线"/"不可达" |
| stale | 灰 | ◌ 虚边圆 | "数据陈旧" |

e-ink 硬约束:

- **禁止** box-shadow、CSS 动画/过渡、半透明叠层、依赖颜色区分的唯一编码。
- 高对比(纯黑文字 / 白底);边框区分卡片而非阴影。
- 字号偏大、行距宽松,保证灰度屏可读。
- 用 `@media (prefers-color-scheme)` 时仍保证 e-ink 灰度可辨;状态符号(●◐○◌)在任何配色下均可读。

定义可复用 class:`.card`、`.status-dot`(+ `.status-ok/.status-warn/.status-error/.status-stale`)、`.metric-bar`(利用率/显存条,供 GPU 卡复用)、`.datasource-banner`。模块卡复用这些 class,不另起命名体系。命名约定:kebab-case,语义化,状态用 `.status-<state>` 修饰符。

### panel.js 渐进增强(无 JS 必须可用)

- 仅在非 e-ink 加载。
- 轮询:`setInterval` 30–60s(从 `data-poll-interval` 读,默认 45s)`fetch('/')` 或后续模块 JSON 端点,替换 DOM(本卡先实现整页/状态条刷新即可;模块卡接 JSON 后细化)。
- **Page Visibility**:`document.hidden` 为真时暂停轮询,可见时恢复并立即刷新一次 —— 保证无访问时零请求(REQ-001 CPU≈0)。
- 任何 JS 失效场景下,SSR 首屏 + e-ink meta refresh 仍提供完整信息。

### 数据源状态条(_datasource_status.html)

渲染 `get_all_last_runs()` 结果:每个 collector 显示 name + 状态(up/down/error/stale)。down/error/stale 用 `.datasource-banner` 醒目提示"数据源异常/陈旧",但**不阻断**其余内容渲染。

## 实现指引

1. `routes.py`:用 `fastapi.templating.Jinja2Templates`(或直接 Jinja2 `Environment`),`GET /` 取 `app.state.repo`(若已接入)渲染;本卡若 repo 未必就绪可容错(无数据时渲染空壳)。
2. `is_eink` 判定:简单 UA 含 "Kindle"/"Silk" 或 `?eink=1`;封装成 helper。
3. `base.html` / `index.html` / partial 按上文 block 与 class 约定写。
4. `panel.css` 实现栅格 + 三断点 + 三重状态编码 class + e-ink 约束;`panel.js` 实现轮询 + Page Visibility。
5. 测试:`GET /` 返回 200 且 HTML 含 `panel-grid`;`?eink=1` 时含 `http-equiv="refresh"` 且不引 JS;默认无 meta refresh。

## 测试要求

- [ ] `GET /` 返回 200,HTML 含 `id="panel-grid"` 主容器与 viewport meta
- [ ] `?eink=1`(或 Kindle UA)时输出 `<meta http-equiv="refresh">` 且不加载 panel.js
- [ ] 默认(非 e-ink)不输出 meta refresh,加载 panel.js
- [ ] 数据源状态条能渲染 up/down/error/stale 四态(模拟 `get_all_last_runs`)
- [ ] CSS 中状态编码同时含颜色、形状符号、文字(grep 校验三者并存)
- [ ] 无 JS 时首屏信息完整(SSR 渲染,不依赖 fetch)

## 完成标准

- [ ] base/index/partial 模板与 block + class 约定就绪,模块卡可直接 include
- [ ] panel.css 三断点 + 三重状态编码 + e-ink 约束(无 shadow/动画)
- [ ] panel.js 轮询 + Page Visibility 暂停;e-ink 走 meta refresh
- [ ] 数据源状态条按降级语义提示且不阻断渲染
- [ ] ruff + pytest 全绿
