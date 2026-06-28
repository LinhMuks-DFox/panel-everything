# 模块 08 · web — SSR 前端

> 适用范围：`src/panel/web/`（路由 + Jinja2 模板 + 静态资源）
> 关联：REQ-001 / REQ-002 · ARCH-001 · TASK-004 / TASK-015 / TASK-017 / TASK-022 / TASK-024 / TASK-033
> 设计基线：ARCH-001「总体架构与基础设施」第 38、80–81、99–103、205–210 行（前端壳、partial 约定、SSR 数据流）

---

## 1. 模块概述与职责

`web` 模块是 Panel Everything 的**服务端渲染（SSR）前端层**。它把后端各采集器/聚合 API 已经算好的数据，在**一次 HTTP 请求内**直接渲染成一个**单屏总览**页面（`GET /`），外加一个**服务器注册管理页**（`GET/POST /servers`）。

它解决的核心问题：在**树莓派（ARM、资源受限）+ 多终端（Kindle e-ink ↔ iPhone ↔ iPad）**的约束下，提供一个**无前端构建链、无 SPA 框架、无访问即 CPU≈0**的监控面板。具体做法：

- **SSR 优先**：所有数据在路由里查好、注入模板上下文，HTML 一次成型。即使关掉 JS（或在 Kindle 上根本不加载 JS），页面信息完整。
- **渐进增强**：`panel.js` 只做「轮询刷新 + 倒计时 + 懒加载趋势图」，是可选叠加；e-ink 设备直接不下发 `<script>`，改用 `<meta http-equiv="refresh">` 降级。
- **e-ink 硬约束**：CSS 禁用 `box-shadow` / `animation` / `transition` / `@keyframes`；所有状态用**色 + 形符（●◐○◌）+ 文字**三重编码，颜色绝不作为唯一指示。
- **三断点响应式**：`<600px` 单列、`600–1024px` 双列、`>1024px` 自动多列。

> 关键定位：本模块**不产生数据、不持有业务逻辑**。它消费 `api/azure.py`、`api/ai_usage.py`、`db.repository`、`db.gpu_repository` 的产出，把聚合结果转成 HTML。状态阈值/陈旧判定的**展示态**逻辑（颜色类、形符）集中在 `routes.py` 的 Jinja2 globals 里，与后端的数据态解耦。

---

## 2. 文件与关键符号清单

### 2.1 `src/panel/web/routes.py` — SSR 路由 + Jinja2 globals/filters

| 符号 | 位置 | 职责 |
|------|------|------|
| `router` | `routes.py:29` | `APIRouter`，无前缀；在 `main.py:144` 被 `include_router` 挂载 |
| `templates` | `routes.py:32` | `Jinja2Templates`，目录指向 `web/templates/` |
| `_vm_status_class(vm)` | `routes.py:39` | VM 电源态 → CSS 后缀 `ok/warn/error/stale`（`is_stale` 优先于一切） |
| `_vm_status_symbol(vm)` | `routes.py:63` | VM 状态 → 形符 `● ◐ ◌ ○` |
| `_util_threshold_class(pct)` | `routes.py:82` | GPU 算力% → `bar-ok/bar-warn/bar-critical`（≥90 critical、≥70 warn） |
| `_mem_threshold_class(pct)` | `routes.py:91` | GPU 显存% → 同上；阈值 90/75；`None → ""` |
| `_datetimeformat(value, fmt)` | `routes.py:113` | Jinja2 filter；datetime → `YYYY-MM-DD HH:MM UTC`，`None → "—"`；naive 视为 UTC |
| `_ai_status_class(p)` | `routes.py:133` | `AiProviderStatus` → `nodata/stale/error/warn/ok`（详见 §9 优先级） |
| `_ai_status_symbol(p)` | `routes.py:156` | AI 状态 → `● ◐ ○ ◌`（注意 error 也渲染 ●） |
| `_ai_pct_warn(p)` / `_ai_pct_error(p)` | `routes.py:174` / `:180` | `used_percent ∈ [70,90)` / `≥90`，供模板加 `data-pct-warn/error` 属性 |
| `_is_eink(request)` | `routes.py:199` | e-ink 检测：`?eink=1` 或 UA 含 `Kindle`/`Silk` |
| `_compute_display_status(run, threshold)` | `routes.py:212` | `CollectorRunRow` → 展示态 `up/down/error/stale`（stale 为读时派生） |
| `index(request)` | `routes.py:239` | **主路由 `GET /`**：聚合全部上下文、渲染 `index.html`（详见 §4） |
| `_FLASH` | `routes.py:395` | 表单提示码 → `(kind, 中文消息)`；避免在 `Location` 头放非 ASCII |
| `_clean(v)` | `routes.py:404` | 空白串归一为 `None` |
| `servers_page(request, flash)` | `routes.py:410` | `GET /servers`：渲染注册列表 + 表单（消费 `gpu_repo.get_all_servers()`） |
| `servers_create(...)` | `routes.py:438` | `POST /servers`：表单 → `ServerIn` → `gpu_repo.insert_server`，PRG 303 重定向 |
| `servers_delete(request, server_id)` | `routes.py:476` | `POST /servers/{id}/delete`：级联删除，PRG 303 重定向 |

> Jinja2 globals 在 `routes.py:103–106`、`:186–189` 注册；filter 在 `:126` 注册。注册为 **globals** 而非 `request` 依赖，所以模板里直接 `vm_status_class(vm)` 调用，无需传 `request`。

### 2.2 `src/panel/web/templates/` — 模板继承树

| 文件 | 职责 |
|------|------|
| `base.html` | 页面壳：`<title>`/`<meta>`、条件 `meta refresh`（e-ink）、`<header>` 导航 + 实时时钟、`.panel-grid` 内容容器（`data-poll-interval="45"`）、条件下发 `panel.js`、`{% block content %}` / `{% block head %}` / `{% block scripts %}` 三个块 |
| `index.html` | 单屏总览：`extends base.html`，依次 `include` 四个 partial + 空占位卡。**模块卡注入约定写在此文件注释里（第 16–24 行）** |
| `servers.html` | 服务器注册管理页（TASK-024）：flash 消息卡 + 注册表单卡 + 已注册列表表格 + 返回链接 |
| `partials/_datasource_status.html` | 顶部数据源状态横幅：每个 collector 一颗 `status-dot` + 名称 + 中文标签；`any_issues` 时整条加 `--issues` 边框 + ⚠ 警告 |
| `partials/_vm_card.html` | Azure VM + GPU 卡（TASK-015）：collector 健康横幅 → VM 列表 → 每 VM 的 GPU 列表（算力/显存进度条、温度/功率、可折叠趋势图 `<details>`） |
| `partials/_node_grid.html` | Tailscale 节点网格容器（TASK-022）：标题含 collector 状态点 + `x/y 在线` 汇总、stale 横幅、error 横幅、`#node-grid` 网格 |
| `partials/_node_card.html` | Tailscale 单节点卡（TASK-022）：`<article.node-card data-node-id data-state>` + 状态点 + 主机名 + exit-node 徽标 + IP/OS/最后在线 + stale 角标 |
| `partials/_ai_card.html` | AI 额度卡（TASK-033）：每个 provider 一张 `<section.card data-module="ai-usage">`，状态点 + 名称 + 手动徽标 + 进度条 + 重置倒计时 + 数据时间 |

### 2.3 `src/panel/web/static/css/panel.css` — 响应式 + e-ink CSS

单文件，无预处理器。按区块组织（头部注释标了所属 ARCH/TASK）：

- **设计 token**（`:root`，`panel.css:14`）：表面/文字/语义状态色、字体、间距、卡片尺寸。
- **基线 + 栅格**：`.panel-grid` 用 `auto-fill minmax(--card-min,1fr)`；`.datasource-banner` 跨满列。
- **三断点**（`panel.css:141/158/165`）：≤599 单列、600–1023 双列、≥1024 多列。
- **状态原语**：`.status-dot`（形符层）、`.status-pill`、`.metric-bar`/`.metric-bar__fill`。
- **各模块区块**：ARCH-002 VM/GPU（`:458`）、ARCH-003 Tailscale 节点网格（`:647`）、注册页（`:792`）、ARCH-004 AI 额度（`:901`）、TASK-017 GPU 趋势（`:1039`）。
- **e-ink 降级**：`@media print`、`@media screen and (max-width:800px) and (color-index:2)`（Kindle 灰度）、`@media (color)`（仅彩色设备才让进度条变色）、`@media (prefers-reduced-motion: reduce)`（防回归）。

### 2.4 `src/panel/web/static/js/panel.js` — 渐进增强

**四个互相独立的 IIFE**，无框架、无构建、无外部依赖。每个 IIFE 自带 Page Visibility 集成与「错误吞掉、保留旧 DOM」的优雅降级。命名前缀（`tailscale*`/`azure*`/`aiUsage*`/`gpuTrend*`）避免变量冲突。

| IIFE | 起始行 | 职责 | 轮询/触发 | 端点 |
|------|--------|------|-----------|------|
| 主轮询 + 时钟 | `panel.js:18` | 整页 `#panel-grid` innerHTML 替换；每秒走 UTC 时钟 | `setInterval` 45s（来自 `data-poll-interval`，clamp 10–300s） | `GET window.location.href` |
| Tailscale | `panel.js:135` | 局部更新节点卡的 class/形符/stale 角标，不重建 DOM | 45s | `GET /api/tailscale/nodes` |
| Azure | `panel.js:264` | 按 `server_id` 原地替换 `.vm-card`，含 GPU 子卡 HTML 构建 | 45s | `GET /api/v1/dashboard/azure` |
| AI 额度 | `panel.js:505` | `resets_at` 倒计时（每分钟）+ 依 `aria-valuenow` 重算阈值属性 | 60s | 无（纯客户端，消费 SSR DOM） |
| GPU 趋势 | `panel.js:606` | `<details>` 展开时懒加载并用 Canvas 2D 画折线（一次性，无动画） | `toggle` 事件，仅首次 | `GET /api/v1/gpu/{server_id}/{gpu_index}/history` |

> 暴露给测试的钩子：`window.gpuTrendDrawMiniChart`（`panel.js:805`），供 `tests/test_gpu_trend.py` 直接驱动绘制逻辑。

---

## 3. 关键数据结构 / 契约

web 模块**不定义自己的数据模型**，它消费以下契约（均在 `src/panel/domain/models.py`）。模板字段名与这些模型字段一一对应——改模型字段名会直接破坏模板。

### 3.1 Azure 仪表盘（供 `_vm_card.html`）

```
DashboardAzureOut (models.py:161)
├─ fetched_at: datetime
├─ collector_status: dict[str, CollectorStatusOut]   # 键 "azure_vm" / "gpu"
└─ vms: list[DashboardVmOut]

DashboardVmOut (models.py:155, 继承 VmStatusOut)
├─ server_id, name, azure_resource_group: int/str/str|None
├─ power_state: str            # "Running"/"Stopped"/"Deallocated"/...
├─ is_stale: bool
└─ gpus: list[GpuMetricOut]

GpuMetricOut (models.py:112)
├─ server_id, gpu_index: int
├─ gpu_name: str | None
├─ util_pct, mem_pct: float | None   # None ⇒ 模板渲染「不可达」
├─ mem_used_mib, mem_total_mib: float | None
├─ temp_c, power_w: float | None
└─ is_stale: bool
```

`CollectorStatusOut.status` 取值 `"up"/"down"/"error"/"unknown"`，模板据此渲染 collector 健康横幅。

### 3.2 AI 用量（供 `_ai_card.html`）

```
AiProviderStatus (models.py:249)
├─ provider, display_name, source_type: str   # source_type: local_jsonl/oauth_api/manual
├─ used_percent: float | None     # None ⇒ 显示「用量未知」
├─ used_value, limit_value: float | None
├─ metric_unit: str               # requests/tokens/unknown
├─ resets_at: str | None          # ISO8601；驱动 data-countdown
├─ window_label: str              # 如 "5h 窗口"
├─ stale: bool, stale_age_label: str | None
├─ collected_at: str | None       # None ⇒ status='no_data'
└─ status: str                    # ok/error/no_data（stale 另由 stale 字段表达）
```

聚合逻辑在 `api/ai_usage.py:get_ai_usage_data(repo)`，返回 `AiUsageResponse.providers`（list）。模板只消费已算好的字段（百分比、单位、倒计时目标），不做任何计算。

### 3.3 Tailscale 节点（供 `_node_card.html`）

路由里**不直接用** `NodeResponse`，而是把 `db.repository.get_all_nodes()` 返回的 `TailscaleNodeRow`（`slots=True` dataclass，不可 `setattr`）包装成 `SimpleNamespace`（`routes.py:339`），补上 `is_stale` 并把 `last_seen_at → last_seen` 改名以匹配模板字段。节点视图字段：`id/hostname/dns_name/tailscale_ips/os/online_state/is_exit_node/last_seen/is_stale/updated_at`。`online_state ∈ {ONLINE, OFFLINE, LONG_OFFLINE}`。

### 3.4 数据源状态条（供 `_datasource_status.html`）

`index()` 把 `repo.get_all_last_runs()`（`list[CollectorRunRow]`）逐条经 `_compute_display_status` 转成 `dict{name,status,ran_at,error}`，注入为 `collector_statuses`。`status` 已是展示态 `up/down/error/stale`。

### 3.5 表单契约（`servers.html` → `ServerIn`）

`POST /servers` 用 FastAPI `Form(...)` 平铺接收字段，在 `servers_create` 里组装成 `ServerIn`（`models.py:59`）：`name`（必填唯一）、`ssh_host/ssh_port/ssh_user/ssh_key_path`、`azure_resource_group/azure_vm_name`、`has_gpu`（`"on"/"true"/"1"/"yes"` → True）、`notes`。**`ssh_key_path` 仅存路径写入 DB，不回传前端**（白名单 `ServerOut` 不含该字段）。

---

## 4. 对外接口与调用关系

### 4.1 路由清单（均 `include_in_schema=False`，不进 OpenAPI）

| 方法 + 路径 | 处理函数 | 返回 |
|-------------|----------|------|
| `GET /` | `index` | `text/html`（`index.html`） |
| `GET /servers` | `servers_page` | `text/html`（`servers.html`） |
| `POST /servers` | `servers_create` | `303` → `/servers?flash=...` |
| `POST /servers/{server_id}/delete` | `servers_delete` | `303` → `/servers?flash=deleted` |

静态资源由 `main.py:139` 通过 `app.mount("/static", StaticFiles(...))` 提供，**不经本模块路由**。

### 4.2 `index()` 数据流（`routes.py:239`）

```
GET /  ──▶ index(request)
  1. is_eink = _is_eink(request)                         # ?eink=1 / Kindle UA
  2. repo / gpu_repo / settings = app.state.*            # 缺失时全程优雅降级
  3. collector_statuses ← repo.get_all_last_runs()       # → _compute_display_status
  4. azure_dashboard   ← api.azure.build_azure_dashboard(repo, gpu_repo)   # 直接调函数，不走 HTTP
  5. nodes_with_stale  ← repo.get_all_nodes() + repo.get_last_run("tailscale")
                          → 包成 SimpleNamespace + per-node is_stale
  6. ai_providers      ← api.ai_usage.get_ai_usage_data(repo).providers
  7. TemplateResponse("index.html", context={...})       # 见下表上下文键
```

注入 `index.html` 的上下文键：`is_eink`、`collector_statuses`、`any_issues`、`now`、`azure_dashboard`、`nodes`、`nodes_online`、`nodes_total`、`collector_status`、`collector_error`、`is_stale`、`stale_seconds`、`ai_providers`。

**关键设计**：聚合逻辑（`build_azure_dashboard`、`get_ai_usage_data`）被 SSR 路由**直接 import 调用**（`routes.py:278`、`:359`），而非自己再发一次 HTTP 请求——避免进程内 HTTP 往返。同一函数也供对应 JSON API 端点复用，保证 SSR 首屏与轮询刷新数据一致。

### 4.3 谁调用 web 模块

- `main.py:144`：`app.include_router(web_routes.router)`，把本模块挂进应用。
- 浏览器/Kindle：直接请求 `GET /`、`GET/POST /servers`。
- `panel.js`：在 SSR 首屏之上轮询三个 JSON 端点做局部更新（见 §2.4）。

### 4.4 web 模块调用谁

| 被调对象 | 用途 |
|----------|------|
| `api.azure.build_azure_dashboard` | Azure VM+GPU 聚合 |
| `api.ai_usage.get_ai_usage_data` | AI 用量聚合 |
| `db.repository.Repository`：`get_all_last_runs` / `get_all_nodes` / `get_last_run` | 数据源状态 + Tailscale 节点 |
| `db.gpu_repository.GpuRepository`：`get_all_servers` / `insert_server` / `delete_server` | 注册页 CRUD |
| `domain.models.ServerIn` | 表单数据校验 |

---

## 5. 与其他模块的依赖

**上游（web 依赖它们）**：

- `api/azure.py`（ARCH-002）：仪表盘聚合 + `/api/v1/dashboard/azure`、`/api/v1/gpu/.../history` 端点（被 `panel.js` 轮询）。
- `api/ai_usage.py`（ARCH-004）：AI 用量聚合 + `/api/ai-usage`。
- `api/tailscale/routes.py`（ARCH-003）：`/api/tailscale/nodes`（被 `panel.js` 轮询）。
- `db/repository.py`、`db/gpu_repository.py`（ARCH-001/002）：数据读取与注册 CRUD。
- `domain/models.py`：所有消费的 Pydantic 模型契约。
- `config/settings.py`：`stale_threshold_seconds`、`tailscale_stale_threshold_seconds`（经 `app.state.settings` 读取）。
- `main.py`：装配（挂载 router + StaticFiles，注入 `app.state.repo/gpu_repo/settings`）。

**下游（依赖 web 的）**：仅终端浏览器/Kindle 与 `panel.js`。其他后端模块**不**依赖 web。

**模块边界**：web 是纯展示层。新增一个采集器**不需要改 web 路由**——只需在 `index.html` 加一行 `include` 引入新 partial（见 §6.1）。

---

## 6. 扩展点

### 6.1 新增一张前端模块卡（最常见）

约定见 `index.html:16–24`：

1. 新建 `partials/_<module>.html`，根元素必须是 `<section class="card" data-module="<name>">`。
2. 三重状态编码（色 + 形符 + 文字），遵守 e-ink 硬约束（§9）。
3. 在 `index.html` 的 `{% block content %}` 内加一行：`{% include "partials/_<module>.html" %}`。
4. 若卡需要新数据：在 `index()`（`routes.py:239`）里查好数据，加进 `TemplateResponse` 的 `context={...}`。优先**复用现成聚合函数**并在 `try/except` 里调用（保持优雅降级）。
5. 若状态需要颜色类/形符：写成纯函数并在 `routes.py` 用 `templates.env.globals[...] = fn` 注册（参考 `_vm_status_class` 等）。

### 6.2 新增前端轮询/局部更新（可选 JS）

在 `panel.js` 末尾**新开一个 IIFE**（不要往现有 IIFE 里塞），遵循现有模板：

1. 用独立前缀命名所有变量/函数（如 `myMod*`）。
2. `fetch` 带 `AbortSignal.timeout(10000)`；`.catch` 里**什么都不做**（保留旧 DOM）。
3. 接入 Page Visibility：`document.hidden` 时 `clearInterval`，可见时立即刷新 + 重启计时器。
4. bootstrap：`if (!document.hidden) start...()`。
5. **局部更新优先**（改 class/textContent/属性），避免整块重建导致闪烁（参考 Tailscale IIFE）。
6. 该 JS 仅对非 e-ink 生效（e-ink 不下发 `<script>`）——所以信息必须已在 SSR HTML 里完整呈现，JS 只做「更新」不做「首次填充」。

### 6.3 新增一个 SSR 路由 / 注册页字段

- 新路由：在 `routes.py` 加 `@router.get/post(..., include_in_schema=False)`；用 `getattr(request.app.state, "repo", None)` 取依赖并对 `None` 做降级。
- 重定向类 POST：用 PRG 模式（`RedirectResponse(..., status_code=303)`），消息走 `_FLASH` 码而非在 URL 里塞中文。
- 注册页新字段：在 `servers.html` 表单加 `<label.reg-field>`，在 `servers_create` 加对应 `Form(...)` 参数并塞进 `ServerIn`（需先在 `domain.models.ServerIn` 加字段）。

### 6.4 新增 Jinja2 filter/global

在 `routes.py` 写纯函数 → `templates.env.filters["name"] = fn`（filter）或 `templates.env.globals["name"] = fn`（global）。global 在模板里直接当函数调，无需 `request`。

---

## 7. 配置 / 环境变量

web 模块自身**无专属 env**，但 `index()` 在请求时从 `app.state.settings` 读取两个阈值（缺失时用模块内默认值兜底）：

| settings 字段 | 默认（缺失兜底） | 用途 |
|---------------|------------------|------|
| `stale_threshold_seconds` | `180`（`_DEFAULT_STALE_SECONDS`，`routes.py:196`） | 数据源状态条 stale 判定 |
| `tailscale_stale_threshold_seconds` | `90`（`_DEFAULT_TAILSCALE_STALE_SECONDS`，`routes.py:192`） | Tailscale 节点/采集器 stale 判定 |

前端轮询间隔由 `base.html` 的 `data-poll-interval="45"` 控制，`panel.js` clamp 到 10–300s；e-ink 的 `<meta refresh>` 固定 60s（`base.html:5`）。这些是模板/JS 内常量，非 env。

---

## 8. 测试位置与覆盖

| 测试文件 | 覆盖范围 |
|----------|----------|
| `tests/test_web.py` | TASK-004：SSR 壳——`GET /` 渲染、e-ink 检测（UA/`?eink=1`）、`<meta refresh>` 条件下发、数据源状态条、`_compute_display_status` 的 stale 派生、`app.state` 缺失时的优雅降级 |
| `tests/test_frontend_vm_gpu.py` | TASK-015：`_vm_status_class`/`_vm_status_symbol`/阈值类各分支、VmCard/GpuCard SSR 渲染、不可达 GPU、stale 徽标 |
| `tests/web/test_tailscale_render.py` | TASK-022：NodeCard/NodeGrid/StaleWarning 渲染、`x/y 在线` 汇总、never_run / error 横幅、per-node stale 角标 |
| `tests/test_ai_card.py` | TASK-033：`_ai_status_class`/`symbol`/`pct_warn`/`pct_error`、no_data 空卡、stale 横幅、进度条、倒计时属性、`data-pct-*` |
| `tests/test_gpu_trend.py` | TASK-017：`window.gpuTrendDrawMiniChart` 绘制逻辑、空数据/断线、e-ink 单色降级、`<details>` 懒加载 |

> 路由测试通过 `panel.main.create_app()` + `httpx` 起测试客户端；部分用 `AsyncMock`/`MagicMock` 伪造 `app.state.repo`。

---

## 9. 注意事项 / 降级语义 / gotchas

**e-ink 硬约束（最易踩）**：CSS 中**禁止** `box-shadow`、`animation`、`transition`、`@keyframes`、以及把颜色作为唯一状态指示。任何状态必须**色 + 形符（●◐○◌）+ 文字**三重编码。新写卡片务必照办，否则在 Kindle 上信息丢失。阈值变色一律包在 `@media (color)`（AI 卡）或靠形符/线宽区分（GPU 趋势），灰度设备只见深灰。`@media (prefers-reduced-motion)` 是防回归兜底，不代表可以加动画。

**形符约定不完全统一，注意逐处核对**：

- VM/数据源：`● ok` · `◐ warn` · `○ down/error` · `◌ stale`。
- AI 卡：`● ok 和 error(≥90)` · `◐ warn` · `○ stale` · `◌ no_data`（**error 用 ● 而非 ○**——红色在 e-ink 上失效，但「填满 = 到限」语义靠 ● 传达；`routes.py:156` 注释明确说明）。
- Tailscale：`● ONLINE` · `◐ OFFLINE` · `○ LONG_OFFLINE` · `◌ stale`。

`_vm_status_class`：**`is_stale` 优先于 `power_state`**（`routes.py:51`）——即使 Running，只要数据陈旧就显示 stale。前端 `panel.js` 的 `vmStatusClass`（`:274`）必须与之保持一致，改一处要同步改另一处（Python ↔ JS 两套实现）。

**`stale` 是读时派生态，不是采集态**：`MetricSample.status` 只有 `ok/unreachable/error`（ARCH-001 第 116 行）。stale 由 `index()` 在请求时用 `collected_at`/`ran_at` 与阈值比较算出（`_compute_display_status`、per-node 循环 `routes.py:321`）。

**`TailscaleNodeRow` 是 `slots=True` dataclass，不能 `setattr`**：所以 `index()` 用 `SimpleNamespace` 包装（`routes.py:337`）补 `is_stale` 并把 `last_seen_at` 改名 `last_seen`。新增节点字段时要在这个 wrapper 里同步加，否则模板取不到。

**优雅降级全程兜底**：`index()`/`servers_page` 全部用 `getattr(app.state, "...", None)` 取依赖，每个数据加载段落各自 `try/except` 并 `logger.exception` 后继续——任一上游挂掉只丢对应卡片，不 500。`panel.js` 所有 `fetch` 失败静默吞掉，保留（可能陈旧的）旧 DOM。空状态：无 collector 且无 azure_dashboard 时渲染占位卡（`index.html:27`）。

**SSR ↔ JS 数据形状差异**：SSR 消费 Pydantic 模型（属性访问，用 `gpu.util_pct is not none`），JS 消费 JSON（`gpu.util_pct !== null && !== undefined`）。两边阈值/形符/类名逻辑是**重复实现**，改阈值需两处都改（`routes.py` 与 `panel.js`）。

**e-ink 不下发 JS**：`base.html:26` `{% if not is_eink %}` 才插 `<script>`。因此 e-ink 上无实时时钟、无轮询、无趋势图懒加载，靠 60s `<meta refresh>` 整页刷新。**任何只在 JS 里出现的信息在 e-ink 上不可见**——信息必须先在 SSR HTML 完整呈现。

**表单安全/编码**：`POST /servers` 用 PRG（303）+ `_FLASH` 码，避免在 `Location` 头放非 ASCII（`routes.py:394`）。删除走 `<form onsubmit="return confirm(...)">`（`servers.html:79`），非 GET。`ssh_key_path` 只存路径、不回前端（白名单 `ServerOut`）。`panel.js` 构建 Azure HTML 时对所有动态字符串走 `escHtml`（`:312`），避免注入。

**GPU 趋势懒加载语义**：`<details>` 默认折叠，仅在用户首次展开（`toggle` 且 `open`）才 fetch（`panel.js:751`）；加载成功标 `data-gpu-trend-loaded="1"` 不再重复；失败则清空该标记允许下次重试。绑定幂等（`data-gpu-trend-bound`），整页轮询替换 DOM 后**不会**自动重新绑定——若依赖轮询后趋势仍可展开，需注意 `gpuTrendBindAll()` 仅在脚本初次执行时跑一次。

---

## 10. 关联 REQ / ARCH / TASK

| 编号 | 标题 | 与 web 的关系 |
|------|------|----------------|
| REQ-001 | 非功能约束（轻量/多终端/无访问 CPU≈0/无认证） | 整个前端壳的设计动机；Page Visibility 暂停轮询直接服务此需求 |
| REQ-002 | 服务器注册机制 | `/servers` 注册管理页是其 Web 入口 |
| ARCH-001 | 总体架构与基础设施 | 前端壳、partial 约定（`<section data-module>`）、三重状态编码、轮询/meta-refresh 降级、SSR 数据流均在此落定 |
| ARCH-002 | Azure VM + GPU 监控 | `_vm_card.html` + GPU 趋势图消费其聚合/历史端点 |
| ARCH-003 | Tailscale 网络监控 | `_node_grid.html`/`_node_card.html` 消费其节点 API |
| ARCH-004 | AI 使用额度监控 | `_ai_card.html` 消费其用量聚合 |
| TASK-004 | SSR 前端壳：base 布局 + 响应式/e-ink CSS + 轮询/meta-refresh 降级 | `base.html` / `index.html` / `panel.css` / `panel.js` 主轮询 + `_datasource_status.html` |
| TASK-015 | 前端 VmCard + GpuCard + 状态徽标（e-ink 适配） | `_vm_card.html` + VM/GPU Jinja2 globals + Azure JS IIFE |
| TASK-017 | 前端 GPU 趋势迷你图（默认折叠） | `_vm_card.html` 内 `<details.gpu-trend>` + GPU 趋势 JS IIFE + Canvas 绘制 |
| TASK-022 | 前端 NodeCard/NodeGrid/StaleWarning（e-ink 适配） | `_node_grid.html`/`_node_card.html` + Tailscale JS IIFE + `datetimeformat` filter |
| TASK-024 | 服务器注册管理 Web 表单（REQ-002 注册入口） | `servers.html` + `/servers` 三个路由 |
| TASK-033 | 前端 AI 额度卡片（泛化渲染、stale 标记、手动降级） | `_ai_card.html` + AI Jinja2 globals + AI 倒计时 JS IIFE |
