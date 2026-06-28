---
id: ARCH-001
title: "总体架构与基础设施"
status: approved
requirements: [REQ-001]
author: Architect
created: 2026-06-28
updated: 2026-06-28
---

## 概述

ARCH-001 定义 Panel Everything 的总体架构与基础设施基线，覆盖 REQ-001 的全部非功能约束与部署要求，并为后续模块文档(ARCH-002 Azure/GPU、ARCH-003 Tailscale、ARCH-004 AI 额度)提供统一契约。

设计目标:

- **轻量第一**:单用户、低频访问、树莓派(ARM)部署。单 FastAPI 进程承载 Web + API + 定时采集,零前端构建链,无访问时 CPU 趋近于 0。
- **统一抽象**:所有数据采集走同一个 `Collector` 协议 + 框架级降级包装,单 target 失败不塌全局,整 collector 失败只标该数据源异常。模块卡只需实现 `collect()` 并注册。
- **基线先行**:本文档落定目录结构、Collector 契约、SQLite(WAL) 通用 schema、repository 薄层签名、app 工厂装配方式、SSR 前端壳、凭证管理规范、容器化方案。后续模块卡严格遵循,不得改动这些契约。
- **多终端**:响应式 + e-ink(Kindle)硬约束。色+形+文三重状态编码;iPhone/iPad 用 fetch 轮询 30–60s,Kindle 用 `<meta refresh>` 降级。
- **无认证**:依赖 Tailscale 内网隔离,后端无任何鉴权中间件(REQ-001 访问控制)。

> 本文档为权威基线。已裁定决策(SSH=asyncssh、DB=aiosqlite、host key 首期 None、表命名混合策略等)不在此重复论证,直接落地。

## 技术选型

| 层面 | 选择 | 理由 |
|------|------|------|
| 语言运行时 | Python 3.12 | 异步生态成熟,`asyncio.timeout` 等新 API 可用 |
| Web 框架 | FastAPI | 轻量、异步原生、Pydantic 集成、自带 OpenAPI |
| ASGI 服务器 | uvicorn(单 worker)+ uvloop | 单用户无需多 worker;uvloop 在 Pi5/8GB 保留以提升 loop 性能 |
| 定时采集 | APScheduler `AsyncIOScheduler` | 与 Web 同进程同 event loop,`max_instances=1` + `coalesce=True` 防堆积 |
| HTTP 客户端 | httpx `AsyncClient` | 异步;模块卡复用(Azure SDK 之外的 HTTP 调用) |
| Unix socket HTTP | aiohttp `UnixConnector` | Tailscale localapi 走 Unix socket(ARCH-003) |
| SSH | asyncssh | 纯 Python、ARM64 无原生扩展、异步原生(ARCH-002 GPU 采集) |
| 持久化 | SQLite(WAL)+ aiosqlite | 单文件、零运维、全异步;挂宿主卷;WAL 提升并发读 |
| 模板引擎 | Jinja2 | SSR 单屏总览 + partial;零前端构建 |
| 前端 | 原生 CSS + 极简渐进增强 JS | e-ink 硬约束否决 SPA;无构建链 |
| 配置/凭证 | pydantic-settings | 集中加载 env / 只读挂载文件;类型校验 |
| 响应模型 | Pydantic response model | 白名单输出,凭证不外泄 |
| 容器 | docker compose + 多阶段 buildx 多 arch | `python:3.12-slim-bookworm`;linux/arm64 + linux/amd64 |
| HTTPS | Tailscale Serve/Funnel | 本期不在应用内做 TLS |

## 系统架构

### 单进程分层(src/panel/)

```
src/panel/
├── __init__.py
├── main.py                 # 入口:create_app() + uvicorn 启动(uvloop)
├── config/
│   ├── __init__.py
│   └── settings.py         # pydantic-settings:Settings 单例,env/secrets 加载
├── db/
│   ├── __init__.py
│   ├── connection.py       # aiosqlite 连接管理 + WAL PRAGMA + lifespan 钩子
│   ├── schema.sql          # 通用基线 DDL(latest_snapshot/metric_history/collector_run)
│   ├── migrate.py          # 启动时执行 schema.sql(IF NOT EXISTS,幂等)
│   └── repository.py       # 薄 SQL 层:snapshot upsert / history append / 读取
├── collectors/
│   ├── __init__.py
│   ├── base.py             # Collector 协议 + MetricSample + CollectorResult
│   ├── registry.py         # 全局注册表:register() / iter_collectors()
│   └── scheduler.py        # APScheduler 装配 + 框架级 try/timeout 降级 + 落 collector_run
│                           # (模块采集器 azure/gpu/tailscale 由 ARCH-002/003 在本目录下新增)
├── api/
│   ├── __init__.py
│   ├── health.py           # GET /healthz
│   └── deps.py             # 依赖注入:get_db / get_settings
├── web/
│   ├── __init__.py
│   ├── routes.py           # SSR 路由:GET /(单屏总览)
│   ├── templates/
│   │   ├── base.html       # 页面壳:<head>/栅格容器/条件 meta refresh
│   │   ├── index.html      # 单屏总览,include 各模块 partial
│   │   └── partials/
│   │       └── _datasource_status.html  # 数据源状态条(stale/down 提示)
│   └── static/
│       ├── css/panel.css   # 响应式 + e-ink CSS(三断点、三重状态编码)
│       └── js/panel.js     # 渐进增强:Page Visibility 暂停轮询 + fetch 刷新
└── domain/
    ├── __init__.py
    └── models.py           # Pydantic 领域/响应模型(白名单基类 PublicModel)
```

### 数据流

```
APScheduler(分钟级触发)
   └─ scheduler.run_collector(c)        # 框架级包装
        ├─ async with asyncio.timeout(c.timeout_seconds):
        │     samples = await c.collect()        # 模块只实现这一步
        ├─ 成功 → repository.upsert_snapshot(samples) + repository.append_history(samples)
        │         + repository.record_collector_run(name, status="up", ...)
        └─ 异常/超时 → repository.record_collector_run(name, status="down"/"error", error=...)
                       (不抛出,不影响其它 collector)

SSR GET /  ── 一次小查询 latest_snapshot(+ collector_run 求 stale/down) ── Jinja2 渲染单屏
趋势(后期) ── 按需查 metric_history / 专用降采样表
前端刷新   ── iPhone/iPad: panel.js fetch 轮询 30–60s;Kindle: <meta refresh>
无访问     ── 零前端请求,后端仅后台采集,CPU≈0
```

### 框架级降级语义

| 场景 | 处理 | collector_run.status | 前端表现 |
|------|------|----------------------|----------|
| collect() 正常返回 | 写 snapshot+history | `up` | 正常渲染 |
| 单 target 不可达(采集器内部已判定) | 该 sample.status=`unreachable`,仍写库 | `up`(采集器本身正常) | 该卡片标"不可达" |
| 单 target 采集出错 | 该 sample.status=`error`,仍写库 | `up` | 该卡片标"错误" |
| 整个 collect() 抛异常 | 捕获,不写 sample | `error` | 该数据源标"数据源异常",其余模块照常 |
| collect() 超时 | `asyncio.timeout` 触发,取消任务 | `down` | 同上,数据源异常 |
| 数据陈旧(last success 距今 > stale 阈值) | 读时计算 | (按 last run 判定) | 该数据源/卡片标 `stale` |

`MetricSample.status` 的取值固定为 `ok` / `unreachable` / `error`(`stale` 不是采集态,而是读取时按 `collected_at` 与阈值比较计算得出的展示态)。

## 接口定义

### Collector 协议(collectors/base.py)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

SampleStatus = Literal["ok", "unreachable", "error"]
RunStatus = Literal["up", "down", "error"]


@dataclass(slots=True)
class MetricSample:
    target_id: int                 # 关联 target(server/node/provider)的 id;无 target 维度时用 0
    metric: str                    # 指标名,如 "power_state" / "online" / "gpu_util"
    value_num: float | None = None # 数值型指标
    value_text: str | None = None  # 文本型指标(枚举/字符串)
    status: SampleStatus = "ok"    # 单 target 该指标的采集结果
    collected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class Collector(Protocol):
    name: str                      # 唯一标识:'azure' | 'gpu' | 'tailscale' | ...
    interval_seconds: int          # 调度间隔
    timeout_seconds: int           # 单次 collect() 超时上限(框架用 asyncio.timeout 包)

    async def collect(self) -> list[MetricSample]:
        """采集一轮。约定:
        - 单 target 失败应捕获并以 status=unreachable/error 的 MetricSample 表达,不抛异常。
        - 仅当采集器整体不可用(配置缺失/数据源全挂)时才允许抛异常,由框架转 collector_run.error。
        """
        ...
```

> `UTC` 取自 `datetime.timezone.utc`。所有 `collected_at` 一律存 UTC,前端渲染时转本地。

### 框架级包装结果(collectors/base.py)

```python
@dataclass(slots=True)
class CollectorResult:
    name: str
    status: RunStatus              # up / down(超时)/ error(异常)
    sample_count: int
    duration_ms: int
    error: str | None = None       # 异常摘要(已脱敏)
    ran_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
```

### 注册表(collectors/registry.py)

```python
_REGISTRY: dict[str, Collector] = {}

def register(collector: Collector) -> None:
    """按 name 注册;重复 name 抛错。模块在自身模块的工厂函数里调用。"""

def get(name: str) -> Collector: ...
def iter_collectors() -> list[Collector]: ...
def clear() -> None:           # 测试用
    ...
```

### 调度器(collectors/scheduler.py)

```python
async def run_collector(collector: Collector, repo: Repository) -> CollectorResult:
    """框架级:asyncio.timeout 包 collect();成功写 snapshot+history+collector_run;
    异常/超时仅落 collector_run,不抛出。返回 CollectorResult 供日志/测试。"""

def build_scheduler(repo: Repository) -> AsyncIOScheduler:
    """读 registry,对每个 collector 注册 interval job
    (max_instances=1, coalesce=True, id=collector.name)。返回未 start 的 scheduler。"""
```

### 健康检查(api/health.py)

```
GET /healthz  → 200 {"status": "ok", "db": "ok"|"down", "time": "<iso8601>"}
```

容器 healthcheck 直接 curl 此端点。DB 探测执行 `SELECT 1`。

### SSR(web/routes.py)

```
GET /  → text/html,渲染 index.html(单屏总览)。
         本期 index 仅含数据源状态条 + 空占位;模块卡通过新增 partial 填充。
```

## 数据模型

通用基线三张表,落在 `db/schema.sql`,启动幂等执行。承载"一个 target 一个标量指标"的泛化数据(VM 电源态、节点在线、AI 额度)。GPU 多卡时序等富结构走专用表(由 ARCH-002 定义),不放在通用表。

### latest_snapshot — 最新快照(每 target×metric 一行,upsert)

```sql
CREATE TABLE IF NOT EXISTS latest_snapshot (
    collector     TEXT    NOT NULL,           -- collector.name
    target_id     INTEGER NOT NULL,           -- target 维度;无维度用 0
    metric        TEXT    NOT NULL,           -- 指标名
    value_num     REAL,                       -- 数值型(可空)
    value_text    TEXT,                       -- 文本型(可空)
    status        TEXT    NOT NULL,           -- ok | unreachable | error
    collected_at  TEXT    NOT NULL,           -- ISO8601 UTC
    updated_at    TEXT    NOT NULL,           -- 写库时刻 ISO8601 UTC
    PRIMARY KEY (collector, target_id, metric)
);

CREATE INDEX IF NOT EXISTS idx_latest_collector
    ON latest_snapshot (collector);
```

### metric_history — 历史时序(append-only)

```sql
CREATE TABLE IF NOT EXISTS metric_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collector     TEXT    NOT NULL,
    target_id     INTEGER NOT NULL,
    metric        TEXT    NOT NULL,
    value_num     REAL,
    value_text    TEXT,
    status        TEXT    NOT NULL,
    collected_at  TEXT    NOT NULL            -- ISO8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_history_query
    ON metric_history (collector, target_id, metric, collected_at);
```

> 历史保留策略本期不实现自动清理;后续可加 retention job 按 `collected_at` 删旧。

### collector_run — 采集运行可观测(每次运行一行,append)

```sql
CREATE TABLE IF NOT EXISTS collector_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collector     TEXT    NOT NULL,
    status        TEXT    NOT NULL,           -- up | down | error
    sample_count  INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,                       -- 脱敏后的异常摘要(可空)
    ran_at        TEXT    NOT NULL            -- ISO8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_run_latest
    ON collector_run (collector, ran_at DESC);
```

读取"某 collector 最近一次成功运行时间"以判定 stale:取 `collector` 最新 `status='up'` 行的 `ran_at`,与 now 比较超阈值即 stale。

### repository 薄层签名(db/repository.py)

```python
class Repository:
    def __init__(self, conn: aiosqlite.Connection) -> None: ...

    # —— 写 ——
    async def upsert_snapshot(self, collector: str, samples: list[MetricSample]) -> None:
        """对每个 sample 按 (collector,target_id,metric) UPSERT;更新 value/status/collected_at/updated_at。"""

    async def append_history(self, collector: str, samples: list[MetricSample]) -> None:
        """对每个 sample 追加一行 metric_history。"""

    async def record_collector_run(self, result: CollectorResult) -> None:
        """追加一行 collector_run(error 字段须已脱敏)。"""

    # —— 读 ——
    async def get_snapshot(self, collector: str) -> list[SnapshotRow]:
        """返回某 collector 的全部最新快照行。"""

    async def get_snapshot_metric(self, collector: str, target_id: int, metric: str) -> SnapshotRow | None: ...

    async def get_history(
        self, collector: str, target_id: int, metric: str,
        since: datetime, until: datetime | None = None, limit: int = 1000,
    ) -> list[HistoryRow]:
        """按时间范围查历史时序,collected_at 升序。"""

    async def get_last_success(self, collector: str) -> datetime | None:
        """某 collector 最近一次 status='up' 的 ran_at;无则 None。用于 stale 判定。"""

    async def get_all_last_runs(self) -> list[CollectorRunRow]:
        """每个 collector 的最近一次运行(任意 status),供数据源状态条渲染。"""
```

`SnapshotRow` / `HistoryRow` / `CollectorRunRow` 为轻量 dataclass(或 `aiosqlite.Row` 直返,由 TASK-002 定);`domain/models.py` 提供对外的白名单 Pydantic 响应模型(基类 `PublicModel`,禁止序列化任何凭证字段)。

### app 工厂与装配(main.py)

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=...), name="static")
    app.include_router(health.router)
    app.include_router(web.routes.router)
    # 模块路由由 ARCH-002/003 各自 include_router 接入(在此处集中挂载)
    return app

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    conn = await db.connection.connect(settings.db_path)   # 开 WAL
    await db.migrate.run(conn)                              # 幂等建表
    repo = Repository(conn)
    app.state.db = conn
    app.state.repo = repo
    register_collectors(settings, repo)                    # 见下,模块注册入口
    scheduler = build_scheduler(repo)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await conn.close()
```

**模块采集器注册契约**(模块卡必须遵循):每个模块(azure/gpu/tailscale)提供一个 `register(settings, repo) -> None` 风格的工厂函数,内部构造自身 Collector 实例并调用 `collectors.registry.register(...)`。`main.register_collectors(settings, repo)` 集中调用各模块工厂(条件:对应配置存在则注册,缺失则跳过并 warning)。这样 `build_scheduler` 读 registry 即可拿到全部 collector,模块无需触碰 scheduler。

## 部署方案

### 容器化

- **基础镜像**:`python:3.12-slim-bookworm`。
- **多阶段**:builder 阶段装依赖(可用 wheel/pip cache),runtime 阶段仅拷代码 + 已装环境,减小镜像体积。
- **多 arch**:`docker buildx build --platform linux/arm64,linux/amd64`。Pi(arm64)为主目标,amd64 供本地开发。
- **非 root 运行**:runtime 阶段 `USER app`(创建非特权用户)。
- **启动**:`uvicorn panel.main:app`(uvloop 由 main 选用),单 worker。
- **健康检查**:Dockerfile `HEALTHCHECK` 调 `GET /healthz`。

### docker-compose.yml(要点)

```yaml
services:
  panel:
    build: { context: ., dockerfile: Dockerfile }
    image: panel-everything:latest
    restart: unless-stopped
    ports: ["8080:8080"]
    env_file: [.env]
    volumes:
      - ./data:/data                                  # SQLite 单文件挂宿主卷
      - /var/run/tailscale:/var/run/tailscale:ro      # Tailscale localapi socket(只读,ARCH-003)
      - ./secrets:/secrets:ro                         # 凭证只读挂载(可选)
    mem_limit: 512m          # Pi5/8GB 放宽到 384–512M
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz').status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
```

> `mem_limit` 在 compose v3+ 需 `deploy.resources` 或保留 v2 风格;TASK-001 选定可用写法并保证 Pi 上生效。HTTPS 交 Tailscale Serve/Funnel,本期不在应用内做。

### 凭证管理规范

- 所有凭证来源:环境变量 或 只读挂载文件(`/secrets/*`、`ssh_key_path`)。代码只读路径引用。
- **DB 不存明文凭证**:`servers` 等表只存路径/引用(ARCH-002),绝不存私钥/secret 明文。
- **响应白名单**:所有对外 JSON 经 Pydantic response model 输出,基类 `PublicModel` 显式列字段;凭证类字段一律不进模型。
- **日志脱敏**:统一日志格式化器对 token/key/secret/password 模式打码;`collector_run.error` 写库前脱敏。
- 详见 TASK-005。

## 任务分解

| TASK ID | 标题 | 优先级 | 依赖 | 预估工作量 |
|---------|------|--------|------|-----------|
| TASK-001 | 项目骨架 + Dockerfile(多阶段多 arch)+ compose + /healthz(声明全量依赖) | P0 | — | M |
| TASK-002 | SQLite(WAL) 连接 + 通用 schema 基线 + repository 薄层 | P0 | TASK-001 | M |
| TASK-003 | Collector 框架:协议 + 注册表 + APScheduler 调度 + 框架级降级 | P0 | TASK-002 | M |
| TASK-004 | SSR 前端壳:base 布局 + 响应式/e-ink CSS + 轮询/meta-refresh 降级 | P0 | TASK-001 | M |
| TASK-005 | 配置与凭证管理 + response model 白名单 + 日志脱敏 | P0 | TASK-001 | S |
