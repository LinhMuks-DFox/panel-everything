---
id: ARCH-002
title: "Azure VM + GPU 监控"
status: draft
requirements: [REQ-002]
author: Architect
created: 2026-06-28
updated: 2026-06-28
---

## 概述

本模块实现对实验室 Azure 云 VM 运行状态与 GPU 资源使用情况的全程监控。架构分为三层：

1. **数据采集层**：两个独立 Collector——AzureVmCollector（通过 Azure SDK 拉取 VM 电源态）与 GpuCollector（通过 SSH 执行 `nvidia-smi` 获取多卡指标），均遵循 ARCH-001 定义的 `Collector` 协议，由统一调度器驱动。
2. **存储层**：专用表 `servers`（服务器注册）/ `azure_vm_status`（VM 状态快照）/ `gpu_metrics`（GPU 多卡富结构时序）+ `gpu_metrics_5m`/`gpu_metrics_1h` 降采样表（MS-003 实现）。
3. **表现层**：REST JSON API（CRUD + dashboard 聚合）+ Jinja2 SSR 前端 VmCard/GpuCard，遵循 ARCH-001 响应式/e-ink 规范。

模块以 `collectors/azure/` 和 `collectors/gpu/` 子包组织，对外暴露 `register(settings, repo)` 工厂供 `main.register_collectors` 调用；API 路由以 `api/azure.py` 挂载；前端 partial 以 `web/templates/partials/_vm_card.html` / `_gpu_card.html` 注入主页。

## 技术选型

| 层面 | 选择 | 理由 |
|------|------|------|
| Azure 认证 | `azure-identity.ClientSecretCredential` (Service Principal) | 树莓派非 Azure 托管环境，Managed Identity 需 IMDS endpoint，在非 Azure 主机不可用；SP 仅需三个 env 变量，与 pydantic-settings 集中管理模式一致 |
| Azure SDK | `azure-mgmt-compute.ComputeManagementClient` | 官方 SDK，`virtual_machines.list_all(expand="instanceView")` 单次调用获取电源态，无需二次请求 |
| Azure 权限 | `Reader` 角色（订阅/资源组级别只读） | 满足 REQ-002 只读约束；不需要 `Virtual Machine Contributor` |
| SSH 执行 | `asyncssh`（纯 Python，ARM64 无原生扩展依赖） | 与 ARCH-001 裁定一致；异步原生，与 AsyncIOScheduler 同 loop |
| GPU 采集命令 | `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` | 无需在目标机部署 agent；CSV 输出易解析；多卡自然多行 |
| 数据库 | SQLite WAL + aiosqlite，专用表 | 多卡富结构需专用表；降采样表由 APScheduler 定期 job 维护 |
| HTTP 框架 | FastAPI（与主 app 共享） | 模块路由 `include_router` 挂入主 app，无独立进程 |
| 凭证保护 | pydantic-settings 加载 env；DB 只存 `ssh_key_path` 路径引用；response model 白名单 | 私钥内容不进 DB；API 响应不回传 `ssh_key_path` |

### Service Principal vs Managed Identity 说明

| 方案 | 适用场景 | 本项目可行性 |
|------|----------|-------------|
| Managed Identity | Azure 托管资源（VM/ACI/AKS） | **不可用**：树莓派不是 Azure 托管资源，无法访问 IMDS（169.254.169.254），SDK 会超时 |
| Service Principal (ClientSecretCredential) | 任意主机，凭证通过 env/文件注入 | **选用**：`AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` 三变量，pydantic-settings 集中加载，容器内通过 `.env` 或 secrets 挂载注入 |

## 系统架构

```
┌────────────────────────────────────────────────────────────┐
│  APScheduler (ARCH-001 scheduler)                          │
│    ├─ AzureVmCollector  interval=300s (5min)               │
│    └─ GpuCollector      interval=60s  (1min)               │
└──────────────┬─────────────────────┬──────────────────────┘
               │                     │
    ┌──────────▼──────┐   ┌──────────▼──────────────────────┐
    │ AzureVmCollector│   │ GpuCollector                    │
    │ azure-mgmt-     │   │ asyncio.gather(*[               │
    │ compute SDK     │   │   ssh_query(host) for host      │
    │ list_all(expand │   │   in enabled_servers            │
    │ =instanceView)  │   │ ])                              │
    └──────────┬──────┘   └──────────┬──────────────────────┘
               │                     │
    MetricSample(status=ok/          GpuSample (写专用表)
    unreachable/error)               + MetricSample(agg写通用表)
               │                     │
    ┌──────────▼─────────────────────▼──────────────────────┐
    │  Repository (ARCH-001 db/repository.py)                │
    │  + GpuRepository (db/gpu_repository.py)               │
    │  aiosqlite WAL                                        │
    │  ┌──────────┐ ┌─────────────────┐ ┌───────────────┐  │
    │  │ servers  │ │ azure_vm_status  │ │  gpu_metrics  │  │
    │  └──────────┘ └─────────────────┘ └───────────────┘  │
    │               ┌─────────────────┐ ┌───────────────┐  │
    │               │ latest_snapshot │ │gpu_metrics_5m │  │
    │               │ (ARCH-001通用)  │ │gpu_metrics_1h │  │
    │               └─────────────────┘ └───────────────┘  │
    └──────────────────────────────┬────────────────────────┘
                                   │
    ┌──────────────────────────────▼────────────────────────┐
    │  FastAPI Routers                                       │
    │  api/azure.py                                         │
    │  POST/GET/DELETE /api/v1/servers      (CRUD)          │
    │  GET             /api/v1/dashboard/azure              │
    └──────────────────────────────┬────────────────────────┘
                                   │
    ┌──────────────────────────────▼────────────────────────┐
    │  Jinja2 SSR  web/templates/partials/                  │
    │  _vm_card.html   _gpu_card.html                       │
    │  VmCard(电源态·颜色·形符)  GpuCard(利用率条·显存条)  │
    └────────────────────────────────────────────────────────┘
```

### 模块目录结构

```
src/panel/
├── collectors/
│   ├── azure/
│   │   ├── __init__.py       # register(settings, repo) -> None
│   │   └── collector.py      # AzureVmCollector
│   └── gpu/
│       ├── __init__.py       # register(settings, repo) -> None
│       └── collector.py      # GpuCollector
├── api/
│   └── azure.py              # APIRouter; /api/v1/servers + /api/v1/dashboard/azure
├── db/
│   └── gpu_repository.py     # GPU 专用表读写
├── domain/
│   └── models.py             # ServerIn/ServerOut/VmStatusOut/GpuMetricOut/DashboardOut (Pydantic)
└── web/templates/partials/
    ├── _vm_card.html
    └── _gpu_card.html
```

### VM 电源态映射

Azure `instanceView.statuses` 列表中取 `code` 前缀为 `PowerState/` 的条目：

| Azure PowerState 原值 | 映射展示状态 | MetricSample.status | 说明 |
|----------------------|-------------|---------------------|------|
| `PowerState/running` | Running | `ok` | 正常运行 |
| `PowerState/stopped` | Stopped (OS关机) | `ok` | 计费停止但资源保留 |
| `PowerState/deallocated` | Deallocated | `ok` | 资源释放，不计费 |
| `PowerState/starting` | Starting | `ok` | 启动中 |
| `PowerState/stopping` | Stopping | `ok` | 停止中 |
| `PowerState/deallocating` | Deallocating | `ok` | 释放中 |
| 未找到 / SDK 异常 | Unknown | `error` | 解析失败 |
| 网络/认证错误 | Unreachable | `unreachable` | Collector 整体失败时所有 VM 置此态 |

`value_text` 存储映射后的展示字符串；`value_num` 存 1(running)/0(其他) 供趋势计算。

### deallocated 跳采优化（MS-003 预留）

TASK-016 实现时：GpuCollector 在 `collect()` 前读取 `azure_vm_status` 表，对 `power_state IN ('deallocated','stopped')` 的服务器跳过 SSH 采集，直接产出 `MetricSample(status='unreachable', value_text='vm_not_running')`，避免 SSH 连接超时堆积。本期 TASK-013 不实现此优化，但代码结构预留 `_is_vm_running(server_id)` 接口位置。

## 接口定义

### 服务器注册 CRUD — `/api/v1/servers`

#### POST `/api/v1/servers` — 注册服务器

Request body (`ServerIn`):
```json
{
  "name": "gpu-vm-01",
  "azure_resource_group": "lab-rg",
  "azure_vm_name": "gpu-vm-01",
  "ssh_host": "100.64.x.x",
  "ssh_port": 22,
  "ssh_user": "azureuser",
  "ssh_key_path": "/run/secrets/ssh_key_gpu01",
  "has_gpu": true,
  "notes": "4x A100 主力机"
}
```

Response 201 (`ServerOut`)：**`ssh_key_path` 字段不出现在响应中**（Pydantic response model 白名单）：
```json
{
  "id": 1,
  "name": "gpu-vm-01",
  "azure_resource_group": "lab-rg",
  "azure_vm_name": "gpu-vm-01",
  "ssh_host": "100.64.x.x",
  "ssh_port": 22,
  "ssh_user": "azureuser",
  "has_gpu": true,
  "notes": "4x A100 主力机",
  "created_at": "2026-06-28T00:00:00Z",
  "updated_at": "2026-06-28T00:00:00Z"
}
```

#### GET `/api/v1/servers` — 查询已注册列表

Response 200: `list[ServerOut]`（同上，不含 `ssh_key_path`）

#### DELETE `/api/v1/servers/{id}` — 删除服务器

Response 204 No Content；若 id 不存在返回 404。

---

### Dashboard 聚合 API — `GET /api/v1/dashboard/azure`

一次调用返回所有 VM 状态 + 各机最新 GPU 指标，供前端渲染用。

Response 200 (`DashboardAzureOut`):
```json
{
  "fetched_at": "2026-06-28T12:00:00Z",
  "collector_status": {
    "azure_vm": {"status": "up", "last_ran_at": "2026-06-28T11:59:00Z", "error": null},
    "gpu":      {"status": "up", "last_ran_at": "2026-06-28T11:59:30Z", "error": null}
  },
  "vms": [
    {
      "server_id": 1,
      "name": "gpu-vm-01",
      "azure_vm_name": "gpu-vm-01",
      "azure_resource_group": "lab-rg",
      "power_state": "Running",
      "power_state_raw": "PowerState/running",
      "is_running": true,
      "collected_at": "2026-06-28T11:59:00Z",
      "is_stale": false,
      "gpus": [
        {
          "server_id": 1,
          "gpu_index": 0,
          "gpu_name": "NVIDIA A100-SXM4-80GB",
          "util_pct": 87.5,
          "mem_used_mib": 65536,
          "mem_total_mib": 81920,
          "mem_pct": 80.0,
          "temp_c": 72,
          "power_w": 380,
          "collected_at": "2026-06-28T11:59:30Z",
          "is_stale": false
        }
      ]
    }
  ]
}
```

`is_stale` 规则：`now - collected_at > stale_threshold_seconds`（默认 VM=600s, GPU=180s）。

---

### Collector 注册接口（collectors/azure/__init__.py）

```python
def register(settings: Settings, repo: Repository) -> None:
    """
    从 settings 读取 Azure 凭证。若 AZURE_TENANT_ID / AZURE_CLIENT_ID /
    AZURE_CLIENT_SECRET / AZURE_SUBSCRIPTION_ID 任一缺失，记录 warning 后跳过，
    不抛异常。否则构造 AzureVmCollector 并调 collectors.registry.register(...)。
    """
```

```python
def register(settings: Settings, repo: Repository) -> None:  # collectors/gpu/__init__.py
    """
    GPU Collector 注册。依赖 servers 表中 has_gpu=True 的记录。
    settings 无需额外凭证（SSH 凭证在 servers 表中按路径引用）。
    若 servers 表为空或无 GPU 机，collector 注册但 collect() 直接返回空列表。
    """
```

## 数据模型

### DDL — 专用表

```sql
-- 服务器注册表
CREATE TABLE IF NOT EXISTS servers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    azure_resource_group TEXT,
    azure_vm_name    TEXT,
    ssh_host         TEXT,
    ssh_port         INTEGER NOT NULL DEFAULT 22,
    ssh_user         TEXT    NOT NULL DEFAULT 'azureuser',
    ssh_key_path     TEXT,                          -- 存路径引用，不存私钥内容
    has_gpu          INTEGER NOT NULL DEFAULT 0,    -- 0/1
    notes            TEXT,
    created_at       TEXT    NOT NULL,              -- ISO8601 UTC
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_servers_name ON servers(name);

-- Azure VM 状态快照（每台 VM 一行，upsert on conflict server_id）
CREATE TABLE IF NOT EXISTS azure_vm_status (
    server_id        INTEGER PRIMARY KEY,
    power_state      TEXT    NOT NULL,              -- 映射后展示值: Running/Stopped/Deallocated/...
    power_state_raw  TEXT,                          -- Azure 原始值: PowerState/running
    is_running       INTEGER NOT NULL DEFAULT 0,    -- 1=running, 0=其他
    collected_at     TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);

-- GPU 指标时序表（多卡，每张卡每次采集一行）
CREATE TABLE IF NOT EXISTS gpu_metrics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id        INTEGER NOT NULL,
    gpu_index        INTEGER NOT NULL,              -- 0-based，对应 nvidia-smi 行序
    gpu_name         TEXT,                          -- e.g. "NVIDIA A100-SXM4-80GB"
    util_pct         REAL,                          -- GPU utilization %
    mem_used_mib     REAL,
    mem_total_mib    REAL,
    mem_pct          REAL,                          -- mem_used/mem_total * 100
    temp_c           REAL,                          -- 温度 °C
    power_w          REAL,                          -- 功耗 W
    status           TEXT    NOT NULL DEFAULT 'ok', -- ok/unreachable/error
    collected_at     TEXT    NOT NULL,
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_query
    ON gpu_metrics(server_id, gpu_index, collected_at);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_server_latest
    ON gpu_metrics(server_id, collected_at DESC);

-- GPU 5 分钟降采样（MS-003 实现，表在 TASK-010 提前建好供后续使用）
CREATE TABLE IF NOT EXISTS gpu_metrics_5m (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id        INTEGER NOT NULL,
    gpu_index        INTEGER NOT NULL,
    avg_util_pct     REAL,
    avg_mem_pct      REAL,
    max_temp_c       REAL,
    max_power_w      REAL,
    sample_count     INTEGER NOT NULL DEFAULT 0,
    bucket_start     TEXT    NOT NULL,              -- ISO8601 UTC，5min 对齐
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_5m_bucket
    ON gpu_metrics_5m(server_id, gpu_index, bucket_start);

-- GPU 1 小时降采样（MS-003 实现）
CREATE TABLE IF NOT EXISTS gpu_metrics_1h (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id        INTEGER NOT NULL,
    gpu_index        INTEGER NOT NULL,
    avg_util_pct     REAL,
    avg_mem_pct      REAL,
    max_temp_c       REAL,
    max_power_w      REAL,
    sample_count     INTEGER NOT NULL DEFAULT 0,
    bucket_start     TEXT    NOT NULL,              -- ISO8601 UTC，1h 对齐
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_1h_bucket
    ON gpu_metrics_1h(server_id, gpu_index, bucket_start);
```

### Pydantic 领域模型（domain/models.py）

```python
class ServerIn(BaseModel):
    name: str
    azure_resource_group: str | None = None
    azure_vm_name: str | None = None
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str = "azureuser"
    ssh_key_path: str | None = None   # 仅写入 DB，不出现在响应中
    has_gpu: bool = False
    notes: str | None = None

class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    azure_resource_group: str | None
    azure_vm_name: str | None
    ssh_host: str | None
    ssh_port: int
    ssh_user: str
    # ssh_key_path 故意缺失 — 白名单不回传
    has_gpu: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

class VmStatusOut(BaseModel):
    server_id: int
    name: str
    azure_vm_name: str | None
    azure_resource_group: str | None
    power_state: str
    power_state_raw: str | None
    is_running: bool
    collected_at: datetime
    is_stale: bool

class GpuMetricOut(BaseModel):
    server_id: int
    gpu_index: int
    gpu_name: str | None
    util_pct: float | None
    mem_used_mib: float | None
    mem_total_mib: float | None
    mem_pct: float | None
    temp_c: float | None
    power_w: float | None
    collected_at: datetime
    is_stale: bool

class CollectorStatusOut(BaseModel):
    status: str        # "up"/"down"/"error"/"unknown"
    last_ran_at: datetime | None
    error: str | None

class DashboardVmOut(VmStatusOut):
    gpus: list[GpuMetricOut] = []

class DashboardAzureOut(BaseModel):
    fetched_at: datetime
    collector_status: dict[str, CollectorStatusOut]
    vms: list[DashboardVmOut]
```

### GpuRepository（db/gpu_repository.py）

```python
# 写
async def upsert_vm_status(server_id: int, power_state: str, power_state_raw: str,
                           is_running: bool, collected_at: datetime) -> None: ...
async def append_gpu_metrics(samples: list[GpuSample]) -> None: ...

# 读
async def get_vm_status_all() -> list[VmStatusRow]: ...
async def get_vm_status(server_id: int) -> VmStatusRow | None: ...
async def get_latest_gpu_metrics(server_id: int) -> list[GpuMetricRow]: ...
async def get_gpu_history(server_id: int, gpu_index: int,
                          since: datetime, until: datetime | None = None,
                          limit: int = 1000) -> list[GpuMetricRow]: ...
async def get_all_servers() -> list[ServerRow]: ...
async def insert_server(data: ServerIn) -> int: ...          # 返回新 id
async def delete_server(server_id: int) -> bool: ...         # 返回是否存在

# 行类型（slots dataclass）
@dataclass(slots=True) class VmStatusRow: ...
@dataclass(slots=True) class GpuMetricRow: ...
@dataclass(slots=True) class ServerRow: ...
```

### GpuSample（内部传输对象，collectors/gpu/collector.py）

```python
@dataclass(slots=True)
class GpuSample:
    server_id: int
    gpu_index: int
    gpu_name: str | None
    util_pct: float | None
    mem_used_mib: float | None
    mem_total_mib: float | None
    temp_c: float | None
    power_w: float | None
    status: Literal["ok", "unreachable", "error"]
    collected_at: datetime
```

GpuCollector.collect() 同时：
- 将 GpuSample 列表写 `gpu_metrics` 专用表（通过 GpuRepository）
- 为每台服务器产出一条汇总 MetricSample（metric="gpu_any_running"，value_num=1.0 或 0.0）写通用 `latest_snapshot` 表，供 ARCH-001 的 collector_run 可观测机制统计 sample_count

## 部署方案

### 环境变量（`.env` 或 secrets 挂载）

```bash
# Azure Service Principal（必须，缺任一则 AzureVmCollector disabled）
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=<secret>
AZURE_SUBSCRIPTION_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# SSH 凭证以路径形式挂载（在 servers 表注册时写路径）
# docker compose volumes 挂载：
#   - ./secrets/ssh_keys:/run/secrets/ssh_keys:ro
```

### docker-compose.yml 片段（追加至 ARCH-001 compose）

```yaml
services:
  panel:
    environment:
      - AZURE_TENANT_ID=${AZURE_TENANT_ID}
      - AZURE_CLIENT_ID=${AZURE_CLIENT_ID}
      - AZURE_CLIENT_SECRET=${AZURE_CLIENT_SECRET}
      - AZURE_SUBSCRIPTION_ID=${AZURE_SUBSCRIPTION_ID}
    volumes:
      - ./secrets/ssh_keys:/run/secrets/ssh_keys:ro
```

### 采集间隔

| Collector | 间隔 | 理由 |
|-----------|------|------|
| AzureVmCollector | 300s (5min) | Azure API 频控友好；VM 状态变化不频繁 |
| GpuCollector | 60s (1min) | GPU 利用率秒级变化，1min 是观测周期下限 |

## 任务分解

| TASK ID | 标题 | 优先级 | 依赖 | 预估工作量 |
|---------|------|--------|------|-----------|
| TASK-010 | Azure/GPU 专用表 schema | P1 | TASK-002 | S |
| TASK-011 | 服务器注册 CRUD API（凭证不回传） | P1 | TASK-010, TASK-005 | M |
| TASK-012 | Azure VM 采集器（ClientSecretCredential, Reader） | P1 | TASK-003, TASK-010 | M |
| TASK-013 | SSH GPU 采集器（asyncssh + nvidia-smi 多卡解析） | P1 | TASK-003, TASK-010 | M |
| TASK-014 | Azure + GPU dashboard 聚合 API | P1 | TASK-012, TASK-013 | S |
| TASK-015 | 前端 VmCard + GpuCard + 状态徽标（e-ink 适配） | P1 | TASK-004, TASK-014 | M |
| TASK-016 | GPU 历史降采样 job（5m/1h）+ 趋势查询 API | P2 | TASK-013 | M |
| TASK-017 | 前端 GPU 趋势迷你图（默认折叠） | P2 | TASK-016, TASK-015 | M |
