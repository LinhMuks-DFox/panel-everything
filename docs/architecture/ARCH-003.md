---
id: ARCH-003
title: "Tailscale 网络监控"
status: draft
requirements: [REQ-003]
author: Architect
created: 2026-06-28
updated: 2026-06-28
---

## 概述

本模块基于树莓派本机的 Tailscale localapi Unix socket，采集 tailnet 内所有节点的在线状态与网络基本信息，以 SSR + 轮询模式呈现于单屏面板。

树莓派自身即 tailnet 成员，因此无需 API Key、无需网络出口、无需在容器内安装 tailscale 二进制 — 只需将宿主的 Unix socket 只读挂载进容器，通过 `aiohttp.UnixConnector` 调用 `http://local-tailscaled/localapi/v0/status` 即可获得完整的 Self + Peer 节点信息。

与 HTTP API（`api.tailscale.com`）相比：

| 维度 | localapi socket（选用） | HTTP API |
|------|------------------------|---------|
| 凭证 | 无需（socket 权限即认证） | 需要 API Key 安全存储与轮换 |
| 网络依赖 | 无（本地进程间） | 需公网/tailnet 出口 |
| 延迟 | <1 ms | 数百 ms 跨境 RTT |
| 树莓派资源 | 极低 | httpx 连接池 + TLS |
| 字段完整性 | 含 ExitNodeOption、LastSeen 等本地字段 | 部分字段后端计算才有 |

## 技术选型

| 层面 | 选择 | 理由 |
|------|------|------|
| 数据源 | Tailscale localapi `/localapi/v0/status` | 无需 API Key，本地 socket，延迟极低 |
| HTTP 客户端 | `aiohttp.UnixConnector` | 项目已引入 aiohttp；原生支持 Unix socket 连接 |
| socket 路径 | `/var/run/tailscale/tailscaled.sock` | tailscaled 默认路径；容器只读挂载 |
| 持久化 | SQLite WAL + aiosqlite | 与全局基础设施一致 |
| 通用表 | `latest_snapshot` / `metric_history` | 标量在线态复用通用表（ARCH-001 契约） |
| 专用表 | `tailscale_nodes` / `tailscale_node_events` | 节点富结构 + event-driven 历史，不进通用表 |
| 调度 | APScheduler AsyncIOScheduler（已有，interval=60s） | 与全局调度器共享，max_instances=1 |
| 在线判定 | ONLINE / OFFLINE / LONG_OFFLINE 三态 | 见「在线判定」节 |
| 前端 | Jinja2 SSR partial `_node_card.html` + JS fetch 轮询 | 与全局前端壳一致 |
| Azure 关联 | `node_azure_mapping` 表（MS-003，本期不实现） | TASK-023 范围 |

## 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│  宿主机 (树莓派)                                               │
│  tailscaled ──────── /var/run/tailscale/tailscaled.sock       │
│                              │ (只读 volume 挂载)              │
│  ┌───────────────────────────▼────────────────────────────┐  │
│  │  panel 容器 (src/panel/)                               │  │
│  │                                                        │  │
│  │  APScheduler (60s)                                     │  │
│  │       │                                                │  │
│  │       ▼                                                │  │
│  │  collectors/tailscale/collector.py                     │  │
│  │    TailscaleCollector.collect()                        │  │
│  │    aiohttp.UnixConnector → GET /localapi/v0/status     │  │
│  │       │                                                │  │
│  │       ▼  list[MetricSample]  (target_id=node rowid)   │  │
│  │  collectors/scheduler.py (框架级 try/timeout 包装)     │  │
│  │       │                                                │  │
│  │       ├─► db/repository.py                             │  │
│  │       │    ├─ upsert tailscale_nodes                   │  │
│  │       │    ├─ append tailscale_node_events (状态变更)  │  │
│  │       │    ├─ upsert latest_snapshot                   │  │
│  │       │    └─ record_collector_run                     │  │
│  │       │                                                │  │
│  │  api/tailscale/routes.py (FastAPI router)              │  │
│  │    GET /api/tailscale/nodes  (+ ?stale=true)           │  │
│  │    GET /api/tailscale/nodes/{id}                       │  │
│  │    GET /api/tailscale/status                           │  │
│  │    POST /api/tailscale/refresh                         │  │
│  │       │                                                │  │
│  │  web/templates/partials/_node_card.html                │  │
│  │  web/templates/partials/_node_grid.html                │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 目录结构（本模块新增文件）

```
src/panel/
├── collectors/
│   └── tailscale/
│       ├── __init__.py          # register(settings, repo) -> None
│       └── collector.py         # TailscaleCollector
├── api/
│   └── tailscale/
│       ├── __init__.py
│       └── routes.py            # APIRouter, prefix=/api/tailscale
└── web/
    └── templates/
        └── partials/
            ├── _node_card.html
            └── _node_grid.html

src/panel/db/
└── migrations/
    └── 003_tailscale.sql        # tailscale_nodes + tailscale_node_events DDL

tests/
└── collectors/
    └── tailscale/
        ├── test_collector.py
        ├── test_online_state.py
        └── fixtures/
            └── localapi_status.json   # 录制的真实 socket 响应
```

## 接口定义

### TailscaleCollector（collectors/tailscale/collector.py）

```python
SOCKET_PATH_DEFAULT = "/var/run/tailscale/tailscaled.sock"
LOCALAPI_BASE = "http://local-tailscaled"   # Host 仅用于 HTTP 格式合法性

class TailscaleCollector:
    name: str = "tailscale"
    interval_seconds: int = 60
    timeout_seconds: int = 10

    def __init__(self, socket_path: str, repo: Repository) -> None: ...
    async def collect(self) -> list[MetricSample]: ...
        # 内部: aiohttp.UnixConnector(path=socket_path)
        #       GET {LOCALAPI_BASE}/localapi/v0/status
        #       解析 Self + Peers → upsert tailscale_nodes → emit MetricSample
        # 单节点失败: MetricSample(status="unreachable") 不抛
        # socket 不可达: MetricSample(status="error") 列表长度=0 或抛给框架

    @staticmethod
    def determine_online_state(
        online: bool,
        last_seen: datetime | None,
        now: datetime,
    ) -> Literal["ONLINE", "OFFLINE", "LONG_OFFLINE"]: ...
        # online=True → ONLINE
        # online=False AND (last_seen is None OR now - last_seen <= 24h) → OFFLINE
        # online=False AND now - last_seen > 24h → LONG_OFFLINE
```

### 模块注册（collectors/tailscale/\_\_init\_\_.py）

```python
def register(settings: Settings, repo: Repository) -> None:
    """
    从 settings.TAILSCALE_SOCKET_PATH (默认 /var/run/tailscale/tailscaled.sock)
    构造 TailscaleCollector 并调 collectors.registry.register(collector)。
    socket 路径不存在时记 warning 跳过，不抛异常。
    """
```

### REST API（api/tailscale/routes.py）

```python
router = APIRouter(prefix="/api/tailscale", tags=["tailscale"])

@router.get("/nodes")
async def list_nodes(
    stale: bool = Query(False),
    repo: Repository = Depends(get_repo),
) -> list[NodeResponse]:
    """
    返回所有节点。stale=true 时包含超过 stale_threshold(默认 90s)未更新的节点，
    并在响应中附 is_stale=True 字段。
    stale=false(默认) 不过滤，始终返回全量，但 is_stale 字段反映 stale 状态。
    """

@router.get("/nodes/{node_id}")
async def get_node(
    node_id: int,
    repo: Repository = Depends(get_repo),
) -> NodeResponse:
    """返回单节点详情，含 LastSeen、is_stale。404 若不存在。"""

@router.get("/status")
async def get_collector_status(
    repo: Repository = Depends(get_repo),
) -> CollectorStatusResponse:
    """
    返回 tailscale collector 最近一次 run 记录（collector_run 表），
    含 status/ran_at/sample_count/duration_ms/error(脱敏)。
    """

@router.post("/refresh")
async def manual_refresh(
    scheduler: AsyncIOScheduler = Depends(get_scheduler),
) -> RefreshResponse:
    """
    触发立即执行一次 tailscale collector job（scheduler.modify_job next_run_time=now）。
    返回 {"triggered": true, "message": "..."}。
    """
```

### Pydantic 响应模型（domain/models.py 追加）

```python
class NodeResponse(BaseModel):
    id: int
    hostname: str
    dns_name: str | None
    tailscale_ips: list[str]          # 通常一个 IPv4 + 一个 IPv6
    os: str | None
    online_state: Literal["ONLINE", "OFFLINE", "LONG_OFFLINE"]
    is_exit_node: bool
    last_seen: datetime | None        # UTC
    is_stale: bool                    # collected_at 距今超阈值
    updated_at: datetime              # 本行最近更新时间

class CollectorStatusResponse(BaseModel):
    status: Literal["up", "down", "error", "never_run"]
    ran_at: datetime | None
    sample_count: int
    duration_ms: int
    error: str | None                 # 脱敏后；None 表示无错误
```

## 数据模型

### 采集字段（来自 localapi /status）

| localapi 字段 | 本地列名 | 说明 |
|--------------|---------|------|
| `HostName` | `hostname` | 节点主机名 |
| `DNSName` | `dns_name` | MagicDNS 域名（如 `muxrpi.tail-xxx.ts.net.`） |
| `TailscaleIPs` | `tailscale_ips` | JSON 数组存储，IPv4+IPv6 |
| `OS` | `os` | 操作系统（linux/macOS/windows/iOS） |
| `Online` | `online_state` | 映射为三态枚举 |
| `LastSeen` | `last_seen_at` | ISO8601 UTC，Online=true 时为 null |
| `ExitNodeOption` | `is_exit_node` | 节点是否可作 exit node |
| Self.PublicKey | `node_key` | 节点唯一标识（也用于去重） |

### DDL（migrations/003_tailscale.sql）

```sql
-- 节点主表：每个 tailnet 节点一行，每次采集 upsert
CREATE TABLE IF NOT EXISTS tailscale_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT    NOT NULL UNIQUE,   -- Self/Peer 的 PublicKey，节点永久标识
    hostname        TEXT    NOT NULL,
    dns_name        TEXT,
    tailscale_ips   TEXT    NOT NULL DEFAULT '[]',  -- JSON array
    os              TEXT,
    online_state    TEXT    NOT NULL DEFAULT 'OFFLINE',  -- ONLINE|OFFLINE|LONG_OFFLINE
    is_exit_node    INTEGER NOT NULL DEFAULT 0,
    last_seen_at    TEXT,                       -- ISO8601 UTC; NULL when online
    collected_at    TEXT    NOT NULL,           -- 最近一次采集时刻 ISO8601 UTC
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_online_state
    ON tailscale_nodes(online_state);

-- 事件历史表：event-driven，仅在 online_state 发生变更时 INSERT
-- 不做定时快照，避免高频写入占用树莓派 IO
CREATE TABLE IF NOT EXISTS tailscale_node_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT    NOT NULL,
    from_state      TEXT,                       -- NULL 表示首次发现
    to_state        TEXT    NOT NULL,           -- ONLINE|OFFLINE|LONG_OFFLINE
    occurred_at     TEXT    NOT NULL,           -- ISO8601 UTC (= collected_at)
    note            TEXT                        -- 备注（如 "first_seen"）
);

CREATE INDEX IF NOT EXISTS idx_node_events_key_time
    ON tailscale_node_events(node_key, occurred_at DESC);
```

### 与通用表的关系

- `latest_snapshot(collector='tailscale', target_id=tailscale_nodes.id, metric='online_state', value_text='ONLINE|OFFLINE|LONG_OFFLINE')` — 使 `/api/tailscale/status` 与全局 collector dashboard 保持一致。
- `metric_history` 本模块不额外写入（`tailscale_node_events` 已承担 event-driven 历史语义，避免重复写）。
- `collector_run` 每次采集周期由调度框架写入（ARCH-001 契约，无需本模块额外处理）。

### Repository 扩展签名（db/repository.py 追加）

```python
# 写
async def upsert_tailscale_node(
    node_key: str,
    hostname: str,
    dns_name: str | None,
    tailscale_ips: list[str],
    os: str | None,
    online_state: str,
    is_exit_node: bool,
    last_seen_at: datetime | None,
    collected_at: datetime,
) -> int:  # 返回 tailscale_nodes.id
    """UPSERT ON CONFLICT(node_key)；若 online_state 变更则写 tailscale_node_events。"""

# 读
async def get_all_nodes(self) -> list[TailscaleNodeRow]: ...
async def get_node_by_id(self, node_id: int) -> TailscaleNodeRow | None: ...
async def get_node_events(
    self,
    node_key: str,
    limit: int = 100,
) -> list[TailscaleNodeEventRow]: ...
```

行类型（slots dataclass）：

```python
@dataclass(slots=True)
class TailscaleNodeRow:
    id: int
    node_key: str
    hostname: str
    dns_name: str | None
    tailscale_ips: list[str]   # 反序列化自 JSON
    os: str | None
    online_state: str
    is_exit_node: bool
    last_seen_at: datetime | None
    collected_at: datetime
    updated_at: datetime

@dataclass(slots=True)
class TailscaleNodeEventRow:
    id: int
    node_key: str
    from_state: str | None
    to_state: str
    occurred_at: datetime
    note: str | None
```

## 部署方案

### docker-compose.yml 追加挂载（在 TASK-001 基础上）

```yaml
services:
  panel:
    volumes:
      - /var/run/tailscale/tailscaled.sock:/var/run/tailscale/tailscaled.sock:ro
```

### 环境变量（.env.example 追加）

```
# Tailscale localapi socket 路径（容器内，只读挂载）
TAILSCALE_SOCKET_PATH=/var/run/tailscale/tailscaled.sock
```

### 配置项（config/settings.py 追加）

```python
tailscale_socket_path: str = "/var/run/tailscale/tailscaled.sock"
tailscale_interval_seconds: int = 60
tailscale_timeout_seconds: int = 10
tailscale_stale_threshold_seconds: int = 90  # 超过此值标 stale
tailscale_long_offline_hours: int = 24        # 超过此值标 LONG_OFFLINE
```

### 树莓派权限

- 宿主机上 `tailscaled.sock` 通常属 root:root 或 root:tailscale。
- 容器以非 root 用户运行时需将容器用户加入 socket 所属组，或在 compose 中加 `group_add: [tailscale]`（gid 因发行版而异，文档提示运维手动确认）。
- 容器内不安装 tailscale，仅通过 socket 通信，满足最小权限原则。

## 任务分解

| TASK ID | 标题 | 优先级 | 依赖 | 预估工作量 |
|---------|------|--------|------|-----------|
| TASK-020 | Tailscale 采集器 + 数据库表 + 在线判定逻辑 | P1 | TASK-003 | M |
| TASK-021 | Tailscale REST API | P1 | TASK-020 | S |
| TASK-022 | 前端 NodeCard/NodeGrid/StaleWarning（e-ink 适配） | P1 | TASK-004, TASK-021 | M |
| TASK-023 | Azure-Tailscale 节点关联（node_azure_mapping + 徽标）※MS-003 | P2 | TASK-020, TASK-013 | S |
