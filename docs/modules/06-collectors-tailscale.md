# 模块参考：collectors-tailscale（Tailscale 采集器）

> 面向开发者 / 维护者的模块级参考文档。读完本文，你无需通读全部源码即可理解、维护并扩展本模块。
>
> - 关联需求：**REQ-003**
> - 关联架构：**ARCH-003**（Tailscale 网络监控）；框架契约见 **ARCH-001**（Collector 协议与降级语义）
> - 关联任务：**TASK-020**（采集器 + 数据库表 + 在线判定）；下游 **TASK-021**（REST API）、**TASK-022**（前端卡片）、**TASK-023**（Azure-Tailscale 关联，本期未实现）
> - 关联缺陷：**BUG-001**（localapi 连通性修复，已 fixed）
> - 源码路径：`src/panel/collectors/tailscale/`

---

## 1. 模块概述与职责

`collectors-tailscale` 是 Panel Everything 采集层中负责 **Tailscale 网络节点在线状态采集** 的模块卡。

树莓派宿主机本身即 tailnet 成员，运行着 `tailscaled` 守护进程，并在宿主上暴露一个 Unix domain socket（默认 `/var/run/tailscale/tailscaled.sock`）。本模块把该 socket 以只读方式挂进 panel 容器，通过 `aiohttp.UnixConnector` 调用 tailscaled 的本地 API（localapi）`GET /localapi/v0/status`，一次性拿到 tailnet 内 **Self + 全部 Peer** 节点的在线状态与基本网络信息（主机名、MagicDNS 域名、Tailscale IP、OS、是否 exit node、LastSeen），按 60 秒周期采集，并持久化到 SQLite。

它解决的问题：**让单屏面板实时显示「我所有设备在线吗？」**，且无需任何 API Key、无需公网出口、延迟 <1ms（详见 ARCH-003 对 localapi socket vs HTTP API 的取舍对比）。

本模块只负责 **采集与入库**。REST 暴露由 `api/tailscale`（TASK-021）承担，前端展示由 `web/.../partials/_node_card.html` 等（TASK-022）承担——本文在第 4、5 节描述这些下游消费者，但其源码不属于本模块。

---

## 2. 文件与关键符号清单

本模块仅含两个 Python 文件，但其运行依赖若干外部协作文件，一并列出（标注是否属于本模块）。

### 本模块文件

| 文件 | 符号 | 职责 |
|------|------|------|
| `src/panel/collectors/tailscale/collector.py` | 模块常量 | localapi 连接参数（见下） |
| | `determine_online_state(...)` `collector.py:46` | 纯函数：把 `online` + `last_seen` + `now` 映射为三态枚举 `ONLINE/OFFLINE/LONG_OFFLINE`。无副作用，便于单测 |
| | `_parse_last_seen(raw)` `collector.py:71` | 把 localapi 的 `LastSeen` 字符串（ISO8601，可带 `Z`）解析为 UTC `datetime`；`None`→`None` |
| | `class TailscaleCollector` `collector.py:81` | 满足 ARCH-001 Collector 协议的采集器实例 |
| | `TailscaleCollector.collect()` `collector.py:100` | 采集一轮：拉 status → 解析 Self+Peer → 并发 upsert → 返回 `list[MetricSample]` |
| | `TailscaleCollector._fetch_status()` `collector.py:128` | 通过 `UnixConnector` 调 localapi，返回解析后的 JSON dict；socket 不可达时**向上抛异常** |
| | `TailscaleCollector._process_node(node, now)` `collector.py:144` | 处理单个节点：判定在线态 → `upsert_tailscale_node` → 产出一条 `MetricSample`；**单节点失败隔离** |
| `src/panel/collectors/tailscale/__init__.py` | `register(settings, repo)` `__init__.py:25` | 工厂：检查 socket 是否存在；存在则构造 `TailscaleCollector` 并注册到全局 registry，不存在则 warning 跳过（不抛异常） |

### 关键常量（`collector.py:37-43`）

```python
SOCKET_PATH_DEFAULT = "/var/run/tailscale/tailscaled.sock"
LOCALAPI_HOST       = "local-tailscaled.sock"      # 注意 .sock 后缀（BUG-001）
LOCALAPI_BASE       = f"http://{LOCALAPI_HOST}"     # → http://local-tailscaled.sock
LOCALAPI_HEADERS    = {"Sec-Tailscale": "localapi"} # localapi CSRF/嗅探防护（BUG-001）
```

> 注：`SOCKET_PATH_DEFAULT` 仅作文档/默认参考；实际 socket 路径由 `register()` 从 `settings.tailscale_socket` 读取后传入构造函数。

### 协作文件（不属于本模块，但本模块直接依赖或被其消费）

| 文件 | 与本模块的关系 |
|------|----------------|
| `src/panel/collectors/base.py` | 定义 `MetricSample` / `CollectorResult` / `Collector` 协议（ARCH-001 契约） |
| `src/panel/collectors/registry.py` | `register()` 把 collector 写入进程级全局字典；`build_scheduler` 由此读取 |
| `src/panel/collectors/scheduler.py` | `run_collector()` 框架级 try/timeout 包装 + 降级；`build_scheduler()` 装配 APScheduler job |
| `src/panel/collectors/__init__.py` | `register_collectors()` 集中调用本模块 `register()`（`__init__.py:50-53`） |
| `src/panel/db/repository.py` | `upsert_tailscale_node` / `get_all_nodes` / `get_node_by_id` / `get_node_events`（以 setattr 注入到 `Repository`，见第 3 节）；`upsert_snapshot` 写通用表 |
| `src/panel/db/migrations/003_tailscale.sql` | 两张专用表 + 索引的 DDL |
| `src/panel/config/settings.py` | `tailscale_socket` 配置项（`settings.py:74`） |
| `src/panel/main.py` | lifespan 中 `register_collectors(...)` → `build_scheduler(...)`（`main.py:79-80`） |
| `src/panel/api/tailscale/routes.py` | 下游 REST 消费者（TASK-021），读专用表 + 触发 refresh |

---

## 3. 关键数据结构 / 表 / 契约

### 3.1 框架契约：`MetricSample`（`collectors/base.py:22`）

采集器对外的唯一产物。本模块每个节点产出一条：

```python
@dataclass(slots=True)
class MetricSample:
    target_id: int                 # = tailscale_nodes.id（无 target 时用 0）
    metric: str                    # 本模块固定为 "online_state"
    value_num: float | None = None # 本模块不用
    value_text: str | None = None  # "ONLINE" | "OFFLINE" | "LONG_OFFLINE"
    status: SampleStatus = "ok"    # "ok" | "unreachable" | "error"
    collected_at: datetime
```

本模块产出的 sample 语义：

| 场景 | `target_id` | `value_text` | `status` |
|------|-------------|--------------|----------|
| 节点正常 upsert | `tailscale_nodes.id` | 三态枚举 | `ok` |
| 节点缺 `PublicKey`（跳过入库） | `0` | `"OFFLINE"` | `error` |
| 单节点 upsert 抛异常（隔离） | `0` | `"OFFLINE"` | `unreachable` |

### 3.2 在线判定三态契约（`determine_online_state`，`collector.py:46`）

```
online == True                                   → "ONLINE"        （无视 last_seen）
online == False 且 (last_seen is None
                    或 now - last_seen <= 阈值)   → "OFFLINE"
online == False 且 now - last_seen >  阈值        → "LONG_OFFLINE"
```

- 阈值默认 24h（`long_offline_hours=24`），由 `register()` 透传、可配置。
- **边界语义**：`now - last_seen == 阈值`（恰好 24h）判为 `OFFLINE`（用 `<=`，未超过即不算 long）；超过 1 分钟才转 `LONG_OFFLINE`。该边界有专门测试（见第 8 节）。
- `online=True` 时 localapi 的 `LastSeen` 通常为 `null`，这是正常值，`last_seen_at` 入库存 `None`。

### 3.3 专用表 DDL（`migrations/003_tailscale.sql`）

两张专用表，**不进通用 `metric_history`**（节点富结构 + event-driven 历史语义不同）。全部 `IF NOT EXISTS`，幂等。

**`tailscale_nodes`**（节点主表，每节点一行，每轮采集 upsert）：

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK AUTOINCREMENT | 行 id，用作 `MetricSample.target_id` / `latest_snapshot.target_id` |
| `node_key` | TEXT NOT NULL **UNIQUE** | Self/Peer 的 `PublicKey`，节点永久标识、去重键 |
| `hostname` | TEXT NOT NULL | |
| `dns_name` | TEXT | MagicDNS 域名 |
| `tailscale_ips` | TEXT NOT NULL DEFAULT `'[]'` | **JSON 数组字符串**（IPv4+IPv6） |
| `os` | TEXT | linux/macOS/windows/iOS |
| `online_state` | TEXT NOT NULL DEFAULT `'OFFLINE'` | 三态枚举 |
| `is_exit_node` | INTEGER NOT NULL DEFAULT 0 | 0/1（SQLite bool） |
| `last_seen_at` | TEXT | ISO8601 UTC；online 时为 NULL |
| `collected_at` | TEXT NOT NULL | 最近一次采集时刻 |
| `updated_at` | TEXT NOT NULL | 本行最近更新时刻 |

索引：`idx_nodes_online_state ON (online_state)`。

**`tailscale_node_events`**（event-driven 历史，仅状态变更时 INSERT，避免高频写入压树莓派 IO）：

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK AUTOINCREMENT | |
| `node_key` | TEXT NOT NULL | 关联节点 |
| `from_state` | TEXT | **NULL 表示首次发现** |
| `to_state` | TEXT NOT NULL | 变更后的三态 |
| `occurred_at` | TEXT NOT NULL | = 该轮 `collected_at` |
| `note` | TEXT | 备注，首次发现时为 `"first_seen"`，普通变更为 NULL |

索引：`idx_node_events_key_time ON (node_key, occurred_at DESC)`。

### 3.4 Repository 行类型（`db/repository.py:355`、`db/repository.py:372`）

```python
@dataclass(slots=True)
class TailscaleNodeRow:        # tailscale_nodes 一行
    id; node_key; hostname; dns_name
    tailscale_ips: list[str]   # 已 json.loads 反序列化
    os; online_state; is_exit_node: bool
    last_seen_at: datetime | None; collected_at: datetime; updated_at: datetime

@dataclass(slots=True)
class TailscaleNodeEventRow:   # tailscale_node_events 一行
    id; node_key; from_state: str | None; to_state: str
    occurred_at: datetime; note: str | None
```

### 3.5 与通用表的关系

- `collect()` 主动写一次 `latest_snapshot(collector='tailscale', target_id=node.id, metric='online_state', value_text=...)`（`collector.py:124`），仅写 `status == "ok"` 的 sample。目的：让 `/api/tailscale/status` 与全局 collector dashboard 一致。
- 框架层 `run_collector` 在成功路径**也会**再调一次 `repo.upsert_snapshot(name, samples)` 并 `append_history`（`scheduler.py:73-74`）。因此 `metric_history` 实际由框架写入；本模块内部那次 `upsert_snapshot` 是契约上的「数据源自洽」冗余写，二者对 `latest_snapshot` 等价幂等。
- `collector_run` 每轮由框架写入，本模块无需处理。

---

## 4. 对外接口与调用关系

### 4.1 谁调用本模块

```
main.lifespan (main.py:79)
  └─ collectors.register_collectors(settings, repo, gpu_repo)   (collectors/__init__.py:25)
       └─ tailscale.register(settings, repo)                    (tailscale/__init__.py:25)
            └─ registry.register(TailscaleCollector(...))        # socket 存在时

main.lifespan (main.py:80)
  └─ build_scheduler(repo)                                       (scheduler.py:98)
       └─ 为每个已注册 collector 装配 APScheduler interval job
          (id=name, interval=60s, max_instances=1, coalesce, next_run_time=now)

APScheduler 每 60s 触发
  └─ run_collector(collector, repo)                              (scheduler.py:36)
       ├─ asyncio.timeout(timeout_seconds) 包住 collector.collect()
       ├─ 成功 → upsert_snapshot + append_history + collector_run(up)
       ├─ TimeoutError → collector_run(down, error="timeout")，不写 sample
       └─ 其它异常 → collector_run(error, error=scrub(...))，不写 sample
```

手动触发：下游 `POST /api/tailscale/refresh`（`api/tailscale/routes.py`）调用 `scheduler.modify_job("tailscale", next_run_time=now)` 立即跑一轮——job id 就是 `collector.name == "tailscale"`。

### 4.2 本模块调用谁（数据流）

```
TailscaleCollector.collect()
  ├─ _fetch_status()
  │    aiohttp.UnixConnector(path=socket_path)
  │    aiohttp.ClientSession(connector, timeout).get(
  │        "http://local-tailscaled.sock/localapi/v0/status",
  │        headers={"Sec-Tailscale": "localapi"})
  │    resp.raise_for_status(); json.loads(await resp.text())
  │
  ├─ 收集 raw_nodes = [Self] + list(Peer.values())
  ├─ asyncio.gather(_process_node(n, now) for n in raw_nodes)   # 并发，单节点隔离
  │    └─ _process_node:
  │         determine_online_state(...)
  │         repo.upsert_tailscale_node(...)  → 返回 node_id（并按需写 events）
  │         → MetricSample(target_id=node_id, "online_state", value_text=state, "ok")
  │
  ├─ repo.upsert_snapshot("tailscale", [ok 的 samples])
  └─ return list[MetricSample]
```

### 4.3 下游 REST API（TASK-021，消费本模块写入的表）

`api/tailscale/routes.py`（`APIRouter(prefix="/api/tailscale")`，在 `main.py:146` 挂载）：

| 端点 | 读取 | 说明 |
|------|------|------|
| `GET /api/tailscale/nodes?stale=` | `repo.get_all_nodes()` | 列全部节点；`stale=true` 仅返回 `now - collected_at > 阈值` 的节点；`is_stale` 字段始终附带。阈值取 `settings.tailscale_stale_threshold_seconds`（默认 90s） |
| `GET /api/tailscale/nodes/{id}` | `repo.get_node_by_id(id)` | 单节点详情，404 若不存在 |
| `GET /api/tailscale/status` | `collector_run` 表 | tailscale collector 最近一次 run（含脱敏 error） |
| `POST /api/tailscale/refresh` | scheduler | `modify_job("tailscale", next_run_time=now)` 立即采集一轮 |

> 注意 **stale ≠ online_state**：`is_stale` 衡量「采集数据有多旧（collected_at 距今）」，是采集健康度；`online_state` 是节点本身在线与否。两者正交。

---

## 5. 与其他模块的依赖（上下游）

**上游（本模块依赖）：**

- `collectors/base.py` — `MetricSample`、`Collector` 协议（结构化类型契约，ARCH-001）。
- `collectors/registry.py` — 注册入口。
- `db/repository.py` — Tailscale 专用读写方法 + `upsert_snapshot`。这些方法以 **setattr 注入**附加到 `Repository` 类（见第 9 节 gotcha）。
- `config/settings.py` — `tailscale_socket` 路径。
- `aiohttp`（项目已引入，复用其 `UnixConnector`，无新增依赖）。
- 运行期外部依赖：宿主机 `tailscaled` 守护进程 + 其 Unix socket（容器内只读挂载）。

**下游（依赖本模块）：**

- `collectors/scheduler.py` — 通过 registry 拿到本 collector 并调度。
- `api/tailscale/routes.py`（TASK-021）— 读本模块写入的两张专用表与 `latest_snapshot`。
- `web/templates/partials/_node_card.html` / `_node_grid.html`（TASK-022）— 渲染节点卡片，消费 REST 响应。
- TASK-023（MS-003，**未实现**）— `node_azure_mapping` 表将关联 tailscale 节点与 Azure VM。

---

## 6. 扩展点（可操作步骤）

### 6.1 采集并持久化一个新字段（如 `Tags` / `RxBytes`）

1. **DDL**：新增迁移 `src/panel/db/migrations/00X_*.sql`，`ALTER TABLE tailscale_nodes ADD COLUMN ...`（用 `IF NOT EXISTS` 语义不可用于 ALTER，可在迁移里先 `PRAGMA table_info` 判断或保证迁移只跑一次幂等）。迁移按文件名升序加载。
2. **行类型**：在 `db/repository.py` 的 `TailscaleNodeRow` 加字段，并在 `_to_node_row`（`repository.py:540`）解析。
3. **写方法**：在 `_upsert_tailscale_node`（`repository.py:392`）的 INSERT 与 UPDATE 两处都加列。
4. **采集**：在 `_process_node`（`collector.py:144`）从 `node.get("...")` 解析并传入 `upsert_tailscale_node`。
5. **测试**：更新 `fixtures/localapi_status.json` + `test_collector.py` 断言。

### 6.2 新增一个事件类型（如 `is_exit_node` 变更也记录）

`_upsert_tailscale_node`（`repository.py:452-485`）目前仅在 `online_state` 变更时写 `tailscale_node_events`。要追踪其它字段变更：在 UPDATE 分支比较旧值（先 SELECT 出旧字段），变更时 `INSERT tailscale_node_events`，用 `note` 区分事件类别（如 `"exit_node_toggled"`）。注意保持 event 表的 event-driven 语义（不要定时快照）。

### 6.3 修改在线判定逻辑 / 阈值

- 逻辑改在纯函数 `determine_online_state`（`collector.py:46`），保持无副作用以便单测。
- 阈值目前由 `register()` 经 `getattr(settings, "tailscale_long_offline_hours", 24)` 注入。要让它可配置，需在 `config/settings.py` 显式新增 `tailscale_long_offline_hours: int = 24` 字段（目前 Settings **没有**该字段，靠 getattr 默认值兜底，见第 9 节）。

### 6.4 新增一个全新的 collector（参考本模块作模板）

1. 建 `src/panel/collectors/<name>/{__init__.py, collector.py}`。
2. `collector.py`：实现 `class XxxCollector`，含类属性 `name`/`interval_seconds`/`timeout_seconds` 与 `async def collect() -> list[MetricSample]`。遵守降级语义：单 target 失败用 `status=unreachable/error` 表达、不抛；整体不可用才抛。
3. `__init__.py`：`def register(settings, repo, ...)`，按配置就绪与否决定 `registry.register(...)` 或 warning 跳过。
4. 在 `collectors/__init__.py` 的 `register_collectors`（`__init__.py:40` 起）追加对新工厂的调用。
5. 富结构数据走专用表（新 DDL + Repository 方法）；标量指标可直接复用 `latest_snapshot`/`metric_history`（由框架写）。
6. 测试：`tests/collectors/<name>/`，参考本模块 fixture + mock `aiohttp.ClientSession` 的策略。

### 6.5 新增一个 REST 端点 / 前端卡

属于下游 TASK-021 / TASK-022 范畴：在 `api/tailscale/routes.py` 加路由读专用表；在 `web/templates/partials/` 加 partial 并在前端轮询 fetch。本模块无需改动。

---

## 7. 配置 / 环境变量

| 配置（`config/settings.py`） | 环境变量 | 默认值 | 用途 |
|------------------------------|----------|--------|------|
| `tailscale_socket` (`settings.py:74`) | `PANEL_TAILSCALE_SOCKET` | `/var/run/tailscale/tailscaled.sock` | localapi Unix socket 路径（容器内只读挂载点） |

`register()` 还会读以下两项，但 **目前 Settings 类未声明它们**，靠 `getattr(..., 默认值)` 兜底（`__init__.py:40-41`）：

| 读取名 | 默认 | 说明 |
|--------|------|------|
| `tailscale_timeout_seconds` | 10 | 单次 collect 的 HTTP 超时（同时影响框架级 `asyncio.timeout`） |
| `tailscale_long_offline_hours` | 24 | LONG_OFFLINE 阈值小时数 |

下游 API 另读 `tailscale_stale_threshold_seconds`（默认 90s），同样未在 Settings 声明、靠 getattr 兜底。

**部署相关**（ARCH-003 / 默认注释掉，需运维手动启用）：

- `docker-compose.yml`：挂载 `/var/run/tailscale:/var/run/tailscale:ro`（当前为注释行）。
- 树莓派权限：`tailscaled.sock` 通常属 `root:root` 或 `root:tailscale`；容器非 root 运行时需 `group_add: [tailscale]`（gid 因发行版而异，需手动确认）。
- 本模块**无需任何 API Key**，认证由 socket 文件权限保证（OS 层）。

---

## 8. 测试位置与覆盖

| 测试文件 | 覆盖范围 |
|----------|----------|
| `tests/collectors/tailscale/test_collector.py` | 采集器主流程（不连真实 socket） |
| `tests/collectors/tailscale/test_online_state.py` | `determine_online_state` 三态边界（纯函数） |
| `tests/collectors/tailscale/fixtures/localapi_status.json` | 录制的真实 localapi 响应，含 9 个节点（5 online、4 offline，覆盖 linux/macOS/windows/iOS、exit node、LONG_OFFLINE） |
| `tests/test_tailscale_api.py` | 下游 REST API（TASK-021，非本模块） |
| `tests/web/test_tailscale_render.py` | 下游前端渲染（TASK-022，非本模块） |

**`test_collector.py` 覆盖点：**

- `_parse_last_seen`：`None`、带 `Z`、带 `+00:00` offset。
- fixture 驱动 `collect()`：返回 9 条 sample，全部 `ok`；`muxrpi=ONLINE`、`ipad163=LONG_OFFLINE`、`iphone-13=OFFLINE`、5 个在线节点 = ONLINE。
- `upsert_tailscale_node`：首次插入写 `first_seen` 事件（`from_state=None`）；状态不变不写事件；`ONLINE→OFFLINE` 写一行变更事件（`from_state="ONLINE"`，`note=None`）。
- socket 不可达：`collect()` 向上抛 `aiohttp.ClientConnectorError`（不静默吞）；经 `run_collector` 包装降级为 `collector_run.status` 非 up（测试断言为 `"error"`）。
- `register()` 工厂：socket 不存在 → warning + 不注册；socket 存在（`touch` 模拟）→ 注册成功。
- Collector 协议：`isinstance(collector, Collector)`，`name=="tailscale"`，`interval_seconds==60`。
- `@pytest.mark.integration` 的 `test_live_smoke`：真实 socket 活体验证，默认 CI `pytest -m "not integration"` 跳过。

**`test_online_state.py` 覆盖点：** ONLINE（含 last_seen 久远仍 ONLINE）、OFFLINE（无 last_seen / 23h55m / 恰好 24h 边界）、LONG_OFFLINE（24h01m / 48h / 72h）、自定义阈值 12h 内外、参数化矩阵。

> 测试 mock 策略：patch `aiohttp.ClientSession`，用 `MagicMock` 提供 `__aenter__/__aexit__` 与 `get().text()`，不发起任何真实网络/socket 请求。**该 mock 不校验 Host / headers**，正是它当年掩盖了 BUG-001（见第 9 节）。

---

## 9. 注意事项 / 降级语义 / Gotchas

1. **BUG-001：localapi 必须满足两道防护，否则 403 / `invalid localapi request`。** 现代 `tailscaled`（1.50+）要求：
   - Host 必须是 `local-tailscaled.sock`（**带 `.sock` 后缀**）。代码用 `LOCALAPI_BASE = "http://local-tailscaled.sock"`，aiohttp 从 URL authority 推导 Host 自动带上后缀。**不要**改回旧的 `http://local-tailscaled`（无 `.sock`）。
   - 必须带请求头 `Sec-Tailscale: localapi`（CSRF/跨源嗅探防护）。即 `LOCALAPI_HEADERS`，由 `_fetch_status()` 传入 `session.get(..., headers=...)`。**两者缺一即被拒。**
   - 单元测试 mock 太宽松不校验这两点，无法捕获此类协议层回归；BUG-001 当年只被 `@integration` 的 `test_live_smoke` 暴露。改动 `_fetch_status` 时务必跑活体测试，或按 BUG-001「后续建议」补 mock 断言。

2. **降级语义分两层，不要混淆：**
   - **整体不可达**（socket 文件不存在 / 连接被拒 / HTTP 非 2xx）：`_fetch_status()` 不捕获，异常穿过 `collect()` 抛给框架；`run_collector` 降级为 `collector_run.status='down'`（超时）或 `'error'`（其它异常），**不写任何 sample**。绝不静默吞掉。
   - **单节点失败**（某 peer upsert 抛错）：`_process_node` 内 `try/except` 隔离，产出 `status='unreachable'`、`target_id=0` 的 sample，**不影响其它节点**。`asyncio.gather` 并发跑所有节点。

3. **缺 `PublicKey` 的节点被跳过**（`collector.py:156`）：产出 `status='error'`、`target_id=0`、`value_text='OFFLINE'` 的 sample，不入库（因为 `node_key` 是 UNIQUE 主标识，没它无法 upsert）。

4. **`LastSeen: null` 是正常值**：`online=True` 时 last_seen 为 null，`_parse_last_seen` 返回 `None`，`last_seen_at` 入库存 NULL，**不是错误**。

5. **`ExitNodeOption` 字段部分节点没有**：`bool(node.get("ExitNodeOption", False))` 默认 False，不报错。

6. **Repository 方法是 setattr 注入的，不在类体内**（`repository.py:568-572`）：`upsert_tailscale_node` / `get_all_nodes` / `get_node_by_id` / `get_node_events` 在模块尾部用 `Repository.xxx = _fn` 附加。后果：
   - 静态类型检查看不到这些方法，调用处普遍带 `# type: ignore[attr-defined]`。
   - 这是项目「不改类体封口处」的约定，新增 tailscale repo 方法要沿用同一模式（定义 `_fn(self, ...)` 再 `Repository.x = _fn`）。AI provider 等扩展也用同样手法（`repository.py:578`、`:640`）。

7. **`latest_snapshot` 被写两次**（本模块一次 + 框架一次，见 3.5）：幂等无害，但改动写入逻辑时知道这点，别误以为重复是 bug。`metric_history` 仅框架写；本模块**不**额外写 history（`tailscale_node_events` 已承担 event-driven 历史，避免重复写压 IO）。

8. **`tailscale_ips` 在表里是 JSON 字符串**：写入 `json.dumps(list)`，读取 `json.loads(... or "[]")`。直接对该列做 SQL 比较/查询会失败，需在应用层反序列化。

9. **e-ink / 资源受限约束（ARCH-003）**：
   - 事件表 event-driven、不定时快照，正是为压低树莓派写 IO；扩展事件类型时勿引入高频写。
   - 前端（下游 TASK-022）需适配 Kindle e-ink 到 iPad 的各种屏幕，轮询而非长连接。

10. **`now` 由调用方传入**：`determine_online_state` 与 `_process_node` 把 `now` 作为参数（`collector.py:107` 取一次 `datetime.now(UTC)` 贯穿整轮），便于测试注入固定时间（见 `test_collect_iphone13_is_offline` 的 `_patched_collect`）。

11. **job id == collector name == `"tailscale"`**：`POST /api/tailscale/refresh` 与 scheduler 去重都依赖这个名字唯一。registry 注册同名会 `ValueError`（`registry.py:31`），测试间需 `registry.clear()`。

---

## 10. 关联 REQ / ARCH / TASK / BUG 编号

| 类型 | 编号 | 标题 / 角色 |
|------|------|-------------|
| 需求 | REQ-003 | Tailscale 网络监控需求 |
| 架构 | ARCH-003 | Tailscale 网络监控（本模块设计来源） |
| 架构 | ARCH-001 | Collector 协议 + 框架级降级语义（上游契约） |
| 任务 | TASK-020 | Tailscale 采集器 + 数据库表 + 在线判定逻辑（本模块实现，status: done） |
| 任务 | TASK-021 | Tailscale REST API（下游消费者） |
| 任务 | TASK-022 | 前端 NodeCard/NodeGrid/StaleWarning（e-ink 适配，下游） |
| 任务 | TASK-023 | Azure-Tailscale 节点关联（MS-003，**本期未实现**） |
| 任务 | TASK-003 | Collector 协议/注册表/调度器（本模块依赖的框架卡） |
| 缺陷 | BUG-001 | localapi 连通性修复（Host `.sock` 后缀 + `Sec-Tailscale` 头，severity major，status fixed） |
