# 模块 07 · API（REST 接口层）

> 模块路径：`src/panel/api/`
> 关联架构：ARCH-001（基线/凭证白名单/降级语义）、ARCH-002（Azure/GPU）、ARCH-003（Tailscale）、ARCH-004（AI 额度）
> 关联任务：TASK-001、TASK-011、TASK-014、TASK-016/017、TASK-021、TASK-030、TASK-033、TASK-018、TASK-040
> 面向读者：本模块的开发者与维护者。读完本文即可理解全部端点契约、扩展新端点、定位测试，无需通读源码。

---

## 1. 模块概述与职责

API 模块是 Panel Everything 唯一的 **对外 HTTP 契约层**。它把 `db` 层（`Repository` / `GpuRepository`）的原始行、`collectors` 层的运行状态，转换成前端（fetch 轮询）与外部 Reporter（工作站推送）可消费的 JSON，并承担三件横切职责：

1. **响应白名单**：所有出站 JSON 都经 `domain/models.py` 的 `PublicModel` 子类序列化，凭证类字段（`ssh_key_path` / `node_key` / token）永不出现在响应里——即使底层 dataclass 携带它们。
2. **读时派生展示态**：`is_stale`、`status="no_data"`、`power_state="Unknown"` 等都是 API 层在读取时按 `collected_at` 与阈值现算的，不落库（符合 ARCH-001「stale 是读取态而非采集态」）。
3. **聚合复用**：两个核心聚合函数 `build_azure_dashboard()` 与 `get_ai_usage_data()` 既服务于各自的 HTTP 端点，也被 `web/routes.py` 的 SSR 首页 **直接 await 调用**（无内部 HTTP 往返），保证 API 与 SSR 同源一致。

本项目**无任何鉴权中间件**（依赖 Tailscale 内网隔离，ARCH-001），唯一的可选鉴权是摄取端点的 Bearer token。所有端点均同进程、异步、单 worker 运行。

---

## 2. 文件与关键符号清单

模块由 6 个源文件组成（`api/__init__.py` 为空包标记）。所有 `Depends(...)` 依赖见 §3.1。

### `api/health.py` — 容器健康探针
- `router` — 无前缀 `APIRouter`，仅挂 `/healthz`。
- `_probe_db(request)` `health.py:18` — 对 `app.state.db` 跑 `SELECT 1`；连接为 `None` 或任何异常都返回 `"down"`，否则 `"ok"`。**捕获所有异常**（裸 `except`，健康探测语义）。
- `healthz(request)` `health.py:31` — `GET /healthz`，**永远返回 200**，body 三字段 `{status, db, time}`；`db` 反映探测结果，供 Dockerfile `HEALTHCHECK` 与 compose 使用。

### `api/deps.py` — 请求级依赖注入
- `get_repo(request)` `deps.py:14` — 返回共享 `app.state.repo`（`Repository`）。
- `get_gpu_repo(request)` `deps.py:19` — 返回共享 `app.state.gpu_repo`（`GpuRepository`）。
- `get_scheduler(request)` `deps.py:24` — 返回共享 `app.state.scheduler`（APScheduler `AsyncIOScheduler`，延迟类型）。

> 三个依赖都只读 `app.state`，不创建新连接/调度器。`app.state.*` 由 `main.py` 的 `lifespan` 装配（见 §4）。

### `api/azure.py` — 服务器 CRUD + Azure/GPU dashboard + GPU 趋势
- `router` — `APIRouter(prefix="/api/v1", tags=["servers"])`。
- 常量 `VM_STALE_SECONDS=600`、`GPU_STALE_SECONDS=180` `azure.py:43` — stale 阈值（= 采集间隔的 2×/3×，见 §9）。
- `HISTORY_DEFAULT_WINDOW = 24h` `azure.py:326` — `since` 省略时的默认回看窗。
- `_parse_utc(value)` `azure.py:52` — ISO8601 字符串 → tz-aware UTC datetime；naive 视为 UTC。
- `_build_collector_status(last_runs, names)` `azure.py:60` — 把 `CollectorRunRow` 列表映射成 `{name: CollectorStatusOut}`；从未运行的 collector → `status="unknown"`。
- `_build_gpu_outs(gpu_rows, now)` `azure.py:83` — `GpuMetricRow` → `GpuMetricOut`，逐行算 `is_stale`。
- `_row_to_out(row)` `azure.py:110` — `ServerRow` → `ServerOut`，**逐字段显式映射**（不用 `**dict`），`ssh_key_path` 因 `ServerOut` 未声明而被静默丢弃。
- `create_server(body, repo)` `azure.py:141` — `POST /api/v1/servers`，201；重复 name → 409；其他 DB 异常 → 500（日志只记异常类名，不泄路径）。
- `list_servers(repo)` `azure.py:183` — `GET /api/v1/servers`，按 id 升序。
- `delete_server(server_id, repo)` `azure.py:199` — `DELETE /api/v1/servers/{id}`，204；不存在 → 404；`ON DELETE CASCADE` 自动清理关联的 `azure_vm_status` / `gpu_metrics`。
- **`build_azure_dashboard(repo, gpu_repo)`** `azure.py:224` — 纯异步聚合函数（**SSR 复用入口**，见 §4）：拉全部 server → VM 状态快照 → GPU 最新指标 → collector 运行状态，组装 `DashboardAzureOut`。空表 → `vms=[]`；已注册但无采集记录的 server → 占位 `power_state="Unknown", is_stale=True, gpus=[]`。
- `get_azure_dashboard(repo, gpu_repo)` `azure.py:307` — `GET /api/v1/dashboard/azure`，薄包装上面的聚合，始终 200。
- `_raw_to_history_point` / `_bucket_to_history_point` `azure.py:329/345` — raw 行 / 降采样桶行 → `GpuHistoryPointOut`（raw 粒度下 `sample_count=1`、avg/max 回显单点值）。
- `get_gpu_history(...)` `azure.py:362` — `GET /api/v1/gpu/{sid}/{idx}/history`，按 `granularity` 选源表（raw/5m/1h），`bucket_start` 升序，始终 200。

### `api/ingest.py` — AI 用量摄取（入站）
- `router` — `APIRouter(prefix="/api/ingest", tags=["ingest"])`。
- `_check_auth(settings, authorization)` `ingest.py:25` — 可选 Bearer 鉴权：`settings.ingest_token` 为空跳过；非空则要求 `Authorization: Bearer <token>` **精确相等**，否则 403。
- `ingest_ai_usage(body, request, repo, authorization)` `ingest.py:40` — `POST /api/ingest/ai-usage`：鉴权 → 把 `provider` 名解析为 `ai_provider.id`（未知 → 400 JSON `{ok:false}`）→ 转 `MetricSample` 列表 → `upsert_snapshot` + `append_history`（collector 名固定 `"ai_usage"`，`target_id=provider_id`）→ 返回 `{ok:true, stored:N}`。

### `api/ai_usage.py` — AI 额度展示聚合（出站）
- `router` — `APIRouter(prefix="/api", tags=["ai-usage"])`。
- 常量 `_AI_COLLECTOR="ai_usage"` — 与摄取端点落库用的 collector 名一致。
- `_parse_utc` / `_format_age` / `_window_label` `ai_usage.py:32/40/53` — 时间解析 + 把秒数/窗口渲染成人类标签（`'2h 15m'` / `'5h 窗口'`，中文）。
- `_build_provider_status(...)` `ai_usage.py:64` — 把某 provider 的多条 metric 快照行聚合成一条 `AiProviderStatus`：空行 → `no_data`；按 metric 名索引；`requests` 优先于 `tokens` 决定 `metric_unit`/`used_value`/`limit_value`；provider 上报的 `window_seconds` 覆盖配置默认；stale 判定见 §9。
- **`get_ai_usage_data(repo)`** `ai_usage.py:176` — 聚合所有 enabled provider（**SSR 复用入口**，见 §4）：读 `ai_provider` 元数据 + `latest_snapshot('ai_usage')`，按 `target_id` 分组，逐 provider 调 `_build_provider_status`，并跟踪全局最新 `collected_at` 作为 `last_updated`。
- `get_ai_usage(repo)` `ai_usage.py:220` — `GET /api/ai-usage`，薄包装。

### `api/tailscale/routes.py` — Tailscale 节点 REST
- `router` — `APIRouter(prefix="/api/tailscale", tags=["tailscale"])`。
- 常量 `_DEFAULT_STALE_THRESHOLD_SECONDS=90`。
- `_stale_threshold(request)` `routes.py:32` — 从 `app.state.settings.tailscale_stale_threshold_seconds` 读阈值，缺省回退 90（见 §9 gotcha）。
- `list_nodes(request, stale, repo)` `routes.py:48` — `GET /api/tailscale/nodes`；`?stale=true` 只回 stale 节点；`is_stale` 始终包含；`node_key` 永不回传。
- `get_node(node_id, request, repo)` `routes.py:89` — `GET /api/tailscale/nodes/{id}`，不存在 → 404。
- `get_collector_status(repo)` `routes.py:124` — `GET /api/tailscale/status`，无运行记录 → `status="never_run"`；否则回最近一次运行（内部局部 import `_parse_utc` 避循环）。
- `manual_refresh(scheduler)` `routes.py:158` — `POST /api/tailscale/refresh`，`scheduler.modify_job("tailscale", next_run_time=now)`；job 不存在（如未配 socket）→ **优雅 200** `triggered=false`，绝不 500。

---

## 3. 关键数据结构 / 契约

### 3.1 端点总表

| 方法 | 路径 | 响应模型 | 成功码 | 说明 |
|------|------|----------|--------|------|
| GET | `/healthz` | `dict[str,str]` | 200 | `{status, db, time}`；恒 200 |
| POST | `/api/v1/servers` | `ServerOut` | 201 | 重复 name 409；DB 错 500 |
| GET | `/api/v1/servers` | `list[ServerOut]` | 200 | id 升序 |
| DELETE | `/api/v1/servers/{server_id}` | —（无 body） | 204 | 不存在 404；级联清理 |
| GET | `/api/v1/dashboard/azure` | `DashboardAzureOut` | 200 | 空表 → `vms=[]` |
| GET | `/api/v1/gpu/{server_id}/{gpu_index}/history` | `list[GpuHistoryPointOut]` | 200 | 见查询参数表 |
| POST | `/api/ingest/ai-usage` | `dict` / `JSONResponse` | 200 | 未知 provider 400；token 不符 403 |
| GET | `/api/ai-usage` | `AiUsageResponse` | 200 | 无数据 provider 仍以空卡返回 |
| GET | `/api/tailscale/nodes` | `list[NodeResponse]` | 200 | `?stale=true` 过滤 |
| GET | `/api/tailscale/nodes/{node_id}` | `NodeResponse` | 200 | 不存在 404 |
| GET | `/api/tailscale/status` | `CollectorStatusResponse` | 200 | 无记录 `never_run` |
| POST | `/api/tailscale/refresh` | `RefreshResponse` | 200 | job 缺失 `triggered=false`（不 500） |

### 3.2 `GET /api/v1/gpu/{sid}/{idx}/history` 查询参数

| 参数 | 类型 | 默认 | 约束 | 含义 |
|------|------|------|------|------|
| `granularity` | `Literal["raw","5m","1h"]` | `5m` | — | 选源表：`gpu_metrics` / `gpu_metrics_5m` / `gpu_metrics_1h` |
| `since` | `datetime?` | `now-24h` | ISO8601，naive 视 UTC | 起始时间 |
| `until` | `datetime?` | `None`（开区间到现在） | 同上 | 结束时间 |
| `limit` | `int` | 200 | `1 ≤ x ≤ 2000`（越界 422） | 最大点数 |

未知 GPU（sid/idx 不存在）不报错，返回 `[]`。

### 3.3 响应模型（`domain/models.py`，均继承 `PublicModel`，`extra="forbid"`）

- `ServerOut` `models.py:76` — `from_attributes=True`；**故意不含 `ssh_key_path`**，凭证白名单（ARCH-001）。
- `DashboardAzureOut` `models.py:161` — `{fetched_at, collector_status: dict[str,CollectorStatusOut], vms: list[DashboardVmOut]}`。
- `DashboardVmOut(VmStatusOut)` `models.py:155` — VM 电源态 + 内嵌 `gpus: list[GpuMetricOut]`。
- `GpuMetricOut` `models.py:112` / `GpuHistoryPointOut` `models.py:139` / `CollectorStatusOut` `models.py:128`。
- `NodeResponse` `models.py:174` — **故意不含 `node_key`**；`online_state: Literal["ONLINE","OFFLINE","LONG_OFFLINE"]`。
- `CollectorStatusResponse` `models.py:192` / `RefreshResponse` `models.py:202`。
- `AiProviderStatus` `models.py:249` / `AiUsageResponse` `models.py:274` — `used_value`+`metric_unit` 由 API 层统一，模板只消费算好的字段。

### 3.4 入站请求模型（**不继承 `PublicModel`**，允许灵活字段）

- `ServerIn` `models.py:59` — 注册请求，含 `ssh_key_path`（仅写库）；默认 `ssh_port=22, ssh_user="azureuser", has_gpu=False`。
- `AiUsagePayload` `models.py:227` — `{reporter_version, reported_at, provider, metrics:[AiMetricItem], status}`；`provider` 用 `str` 而非 `Literal`，未知 provider 不在 Pydantic 层 422，而是交端点查表后返 400（与 TASK-030 测试一致）。
- `AiMetricItem` `models.py:214` — `{metric, value_num?, value_text?}`，metric 示例：`used_requests/limit_requests/used_percent/resets_at/window_seconds`。

### 3.5 底层行/表（供映射，定义在 `db/` 层）
- `ServerRow`（含 `ssh_key_path`）、`VmStatusRow`、`GpuMetricRow`、`GpuBucketRow`、`TailscaleNodeRow`（含 `node_key`）、`SnapshotRow`、`CollectorRunRow`、`AiProviderRow`。
- 通用三表 `latest_snapshot` / `metric_history` / `collector_run`（ARCH-001）；GPU 专用表 `gpu_metrics` / `gpu_metrics_5m` / `gpu_metrics_1h`（ARCH-002）；`ai_provider` 静态配置表（ARCH-004，含 codex/claude_code/chatgpt 三行）。

---

## 4. 对外接口与调用关系（数据流）

```
                         ┌───────────── app.state（lifespan 装配）────────────┐
                         │ db / repo / gpu_repo / scheduler / settings        │
                         └────────────────────────────────────────────────────┘
                                   ▲ get_repo / get_gpu_repo / get_scheduler (deps.py)
                                   │
前端 fetch 轮询 ──► API router ──► Repository / GpuRepository ──► SQLite(WAL)
外部 Reporter  ──► /api/ingest ──► upsert_snapshot + append_history
                                   │
SSR GET / (web/routes.py) ─────────┴─► 直接 await build_azure_dashboard() / get_ai_usage_data()
                                       （不走 HTTP，复用同一聚合逻辑）
```

**路由挂载**（`main.py:143-148`，集中 `include_router`）：`health.router`、`web_routes.router`、`azure_router`、`tailscale_router`、`ingest_router`、`ai_usage_router`。

**SSR 复用**（关键，避免内部 HTTP 往返）：
- `web/routes.py:278-280` `from panel.api.azure import build_azure_dashboard` → 注入 `azure_dashboard` 给 `_vm_card.html` / `_gpu_card.html`。
- `web/routes.py:359-361` `from panel.api.ai_usage import get_ai_usage_data` → 取 `.providers` 注入 `_ai_card.html`。
- 这两个函数因此必须是**无副作用、可独立 await 的纯聚合**，签名为 `(repo[, gpu_repo])`；改它们的签名会同时波及 SSR。

**摄取数据流**：`POST /api/ingest/ai-usage` 落到通用表（collector=`ai_usage`），随后 `GET /api/ai-usage` 与 SSR 从同一张 `latest_snapshot` 读回——摄取与展示通过 `ai_provider.id`（target_id）联系。

---

## 5. 与其他模块的依赖

**上游（API 依赖）**：
- `db/repository.py`（`Repository` + setattr 注入的 tailscale / ai_provider / 历史方法）、`db/gpu_repository.py`（`GpuRepository`）。
- `domain/models.py`（全部响应/请求模型）。
- `config/settings.py`（`Settings`：`ingest_token`、`tailscale_stale_threshold_seconds`）。
- `collectors/base.py`（`MetricSample`，摄取端点构造）。
- 间接依赖 `collectors/scheduler.py`（`manual_refresh` 通过 `app.state.scheduler.modify_job` 操作 job id）。

**下游（谁依赖 API）**：
- `main.py` — `create_app` 集中挂载全部 router；`lifespan` 装配 `app.state.*`。
- `web/routes.py` — SSR 复用 `build_azure_dashboard` / `get_ai_usage_data`。
- 前端 `static/js/panel.js` — fetch `/api/v1/gpu/.../history`、`/api/tailscale/*`、`/api/ai-usage` 等。
- 外部工作站 Reporter（`tools/` 脚本，TASK-031/032）— POST `/api/ingest/ai-usage`。

---

## 6. 扩展点（可操作步骤）

### 6.1 新增一个 REST 端点（到现有 router）
1. 选对应文件（如 servers/dashboard 入 `api/azure.py`，AI 入 `api/ai_usage.py`）。
2. 在 `domain/models.py` 新增**继承 `PublicModel`** 的响应模型；**严禁**声明 `*secret*/*token*/*key*/*password*/*private_*/ssh_key_path/node_key` 命名字段（白名单禁忌，`models.py:1-24`）。需暴露路径名时改用安全别名（如 `azure_secret_configured: bool`）。
3. 写 handler：用 `Depends(get_repo)` / `Depends(get_gpu_repo)` 注入仓库；DB 行 → 响应模型必须**逐字段显式映射**，禁止 `**row_dict`（ARCH-001）。
4. 异常映射：业务态用 `HTTPException`（404/409/400）；意外 DB 错记日志（只记 `type(exc).__name__`，不泄路径）后 500。
5. 在 `tests/` 加集成测试（ASGITransport + AsyncClient）。

### 6.2 新增一个 router（全新前缀）
1. 新建 `api/<name>.py` 或 `api/<name>/routes.py`，`router = APIRouter(prefix="/api/...", tags=[...])`。
2. 在 `main.py` 的集中段（约 `main.py:143-148`）`app.include_router(<name>_router)`，并在顶部 import 时改别名（参考现有 `azure_router` 等）。
3. 若 handler 需新依赖（如某子系统的 repo），在 `deps.py` 增 `get_xxx`，并确保 `lifespan` 已把对象放进 `app.state`。

### 6.3 新增一个 Reporter 摄取指标 / provider
- 新 metric：Reporter 在 payload 的 `metrics` 里多带 `AiMetricItem`，**端点无需改**——`_build_provider_status` 已按 metric 名取值；若是新的「单位」维度（非 requests/tokens），需在 `ai_usage.py:107-120` 扩展 `metric_unit` 推断。
- 新 provider：在 `ai_provider` 表插一行（migrate/seed），`provider` 名要与 Reporter 上报一致；端点与展示自动覆盖。

### 6.4 新增一个采集器并暴露其状态
1. 采集器实现见 `collectors/` 模块文档；其运行状态自动落 `collector_run`。
2. dashboard 想纳入它：在 `build_azure_dashboard` 的 `_build_collector_status(last_runs, [...])` name 列表里加该 collector 名（`azure.py:252`）。

### 6.5 新增 stale 阈值 / 配置
- 在 `config/settings.py` 加 `PANEL_*` 字段，handler 经 `app.state.settings` 或 `Depends` 读取。Tailscale 阈值已走 `_stale_threshold(request)` 模式，可仿照。

---

## 7. 配置 / 环境变量

| 字段（`Settings`） | env | 默认 | 被谁用 |
|--------------------|-----|------|--------|
| `ingest_token` | `PANEL_INGEST_TOKEN` | `""`（禁用鉴权） | `ingest._check_auth` |
| `stale_threshold_seconds` | `PANEL_STALE_THRESHOLD_SECONDS` | 180 | 通用 collector stale（非本模块直接使用） |
| `tailscale_stale_threshold_seconds` | `PANEL_TAILSCALE_STALE_THRESHOLD_SECONDS` | （见 §9 gotcha） | `tailscale.routes._stale_threshold` |
| `history_retention_days` | `PANEL_HISTORY_RETENTION_DAYS` | 30 | retention job（间接，非 API） |

API 内**硬编码**的阈值（不可 env 配，改需动代码）：`VM_STALE_SECONDS=600`、`GPU_STALE_SECONDS=180`、`HISTORY_DEFAULT_WINDOW=24h`、tailscale 回退默认 `90`、AI stale 系数 `window_seconds*0.5`。

---

## 8. 测试位置与覆盖

| 端点/函数 | 测试文件 | 关键用例 |
|-----------|----------|----------|
| `/healthz` | `tests/test_health.py` | 恒 200 + 三字段 schema |
| `/api/v1/servers` CRUD | `tests/test_servers_api.py` | 201/409/404/204、响应**不含** `ssh_key_path`、DB 确实存了 key、422 缺字段、空数组 |
| `/api/v1/dashboard/azure` + `build_azure_dashboard` | `tests/test_dashboard_azure.py` | 空表 `vms=[]`、无采集 `Unknown`/`is_stale`、collector status unknown/down/up、GPU 180s stale 边界、`has_gpu` 控制 `gpus` |
| `/api/v1/gpu/.../history` | `tests/test_gpu_trend.py` | raw/5m/1h 粒度、`since/limit`、SSR 趋势块渲染、前端 URL 形态 |
| `/api/ingest/ai-usage` | `tests/test_ingest.py` | 合法 codex stored=6、未知 provider 400、Bearer 鉴权（空跳过/非空 403/正确 200）、ai_provider 三行、多次 upsert+append |
| `/api/ai-usage` + `get_ai_usage_data` | `tests/test_ai_card.py` | used_requests→used_percent/stale=false、3h(5h 窗 50%)→stale、no_data、metric_unit requests/tokens |
| `/api/tailscale/*` | `tests/test_tailscale_api.py` | nodes/`?stale=true`、404、never_run/up/error、refresh triggered true/false 不 5xx、`node_key` 白名单 |

测试统一用 `httpx.ASGITransport(app=app)` + `AsyncClient` 直打 ASGI app，不起真实端口。

---

## 9. 注意事项 / 降级语义 / gotchas

- **凭证白名单是「靠模型未声明」实现的**：`ServerOut` 不含 `ssh_key_path`、`NodeResponse` 不含 `node_key`。`_row_to_out` 逐字段映射（`azure.py:110`），即使底层 `ServerRow`/`TailscaleNodeRow` 带凭证字段也不会泄露。**新增字段务必复查命名禁忌**（`models.py:1-24`，`extra="forbid"` 会拒绝多余字段但不阻止你主动声明一个坏名字字段）。
- **stale 是读时派生，不落库**：
  - VM `>600s`（间隔 300×2）、GPU `>180s`（间隔 60×3）—— `azure.py:43`。
  - AI：`(now - collected_at) > window_seconds*0.5`，**或** 上报 `status='error'` —— `ai_usage.py:140-149`；provider 上报的 `window_seconds` 覆盖配置默认 `ai_usage.py:126-129`。
  - Tailscale：`> _stale_threshold(request)`。
- **Tailscale 阈值字段名不一致（已知坑）**：`routes._stale_threshold` 读 `settings.tailscale_stale_threshold_seconds`，但 `config/settings.py` 当前**未声明该字段**——因此实际**总是回退到硬编码默认 90s**。`getattr(..., _DEFAULT_STALE_THRESHOLD_SECONDS)` 的兜底掩盖了这一点。要让它可配，需在 `Settings` 显式加该字段（env `PANEL_TAILSCALE_STALE_THRESHOLD_SECONDS`）。
- **`manual_refresh` 永不 500**：job 未注册（如未配 Tailscale socket）时捕获 `JobLookupError` 等，返回 `triggered=false`（200）。前端不应把 false 当错误。
- **摄取未知 provider 返 400 而非 422**：`AiUsagePayload.provider` 故意用 `str`，校验下沉到 `ai_provider` 表查询（`ingest.py:54-59`），与 TASK-030 测试约定一致。
- **dashboard / ai-usage 永不 4xx/5xx**（除依赖崩溃）：空表/无数据走占位（`Unknown`/`no_data`/空卡），保证前端总有可渲染结构。SSR 侧再包一层 `try/except`（`web/routes.py`），聚合抛错只记日志、降级为不渲染该块，不拖垮整页。
- **setattr 注入的 repo 方法**：tailscale（`get_all_nodes`/`get_node_by_id` 等）、AI provider（`get_ai_provider_id`/`get_ai_providers`）、`prune_history` 都是在 `db/repository.py` 模块尾部 `Repository.xxx = _fn` 注入的，**不在类体里**。静态类型检查/IDE 跳转会标 `# type: ignore[attr-defined]`；routes 里也用 `# type: ignore[attr-defined]` 调用它们。新增此类方法照此模式。
- **e-ink / 多终端约束（ARCH-001）**：本模块产出的 JSON 同时服务于 e-ink Kindle 的 SSR 渲染，stale/状态用「色+形+文」三重编码体现在模板侧；API 提供的 `is_stale`/`status`/`stale_age_label` 是这套编码的数据基础，删改字段会波及多终端展示。
- **collector 错误已脱敏**：`CollectorStatusOut.error` / `CollectorStatusResponse.error` 来自 `collector_run.error`，在 scheduler 层入库前已脱敏（ARCH-001），API 直接透传。`create_server` 自己的 500 路径也只 `logger.error("%s", type(exc).__name__)`，不打印路径/堆栈细节。
- **时间一律 UTC**：所有 `_parse_utc` naive 值按 UTC 处理；前端负责转本地。各文件各有一份 `_parse_utc`（azure / ai_usage 私有，repository 提供一份供 tailscale 局部 import）——逻辑等价但未共享，改 timezone 语义需同步多处。
- **host key = None（ARCH-001 首期）**：本模块不直接涉 SSH，但 GPU 数据源 SSH `known_hosts=None`（不校验主机指纹）是上游 collector 的首期约束，dashboard 透出的 GPU 指标默认信任该链路。

---

## 10. 关联 REQ / ARCH / TASK

| 编号 | 关联点 |
|------|--------|
| REQ-001 | 非功能基线：轻量、无认证（Tailscale 隔离）、多终端/e-ink |
| ARCH-001 | `/healthz` 契约、`deps` 注入约定、`PublicModel` 白名单、降级/stale 语义、setattr 扩展模式、TASK-040 retention |
| ARCH-002 | servers CRUD、Azure/GPU dashboard、GPU 趋势表与降采样 |
| ARCH-003 | Tailscale REST、`node_key` 白名单、stale 阈值 |
| ARCH-004 | AI 摄取 + 展示、`ai_provider` 表、Reporter 协议 |
| TASK-001 | `/healthz`（容器健康检查） |
| TASK-011 | `/api/v1/servers` CRUD（凭证不回传） |
| TASK-014 | `/api/v1/dashboard/azure` 聚合 |
| TASK-016 / TASK-017 | GPU 降采样 job + `/gpu/.../history` + 前端迷你图 |
| TASK-018 | Azure 动态公网 IP + 只读 SP 认证对齐（dashboard 数据来源） |
| TASK-021 | Tailscale REST API |
| TASK-030 | `/api/ingest/ai-usage` + `ai_provider` 表 |
| TASK-033 | `/api/ai-usage` 聚合 + AI 额度卡片 |
| TASK-040 | `metric_history` retention（与摄取写入互补） |
