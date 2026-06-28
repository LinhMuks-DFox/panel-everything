# 模块文档：app-assembly（应用装配）

> 关联：REQ-001 · ARCH-001（装配契约）· TASK-001 / TASK-002 / TASK-003 / TASK-004 / TASK-005 · TASK-011 · TASK-012 · TASK-016 · TASK-040
> 唯一源文件：`src/panel/main.py`（其余为本模块编排/调用的对象）

---

## 1. 模块概述与职责

**app-assembly 是整个 Panel Everything 进程的"装配车间"**：它把分散在各模块里的零件（DB 连接、迁移、两个 repository、各模块采集器、定时调度、HTTP 路由、静态资源）按 ARCH-001 规定的顺序与契约组装成一个可运行的 FastAPI 应用，并提供进程启动入口。

它解决的核心问题是 **"谁在什么时候、按什么顺序、把什么挂到哪里"**：

- **构建期（同步）**：`create_app()` 工厂创建 `FastAPI` 实例，集中 `include_router(...)` 挂载全部路由、`mount` 静态资源、配置日志。
- **运行期（异步生命周期）**：`lifespan()` 在应用启动时打开 SQLite（WAL）连接 → 幂等建表 → 构造 `Repository` / `GpuRepository` → 注册全部采集器 → 构建并启动 APScheduler（含三个维护型 job）；在应用关闭时按相反顺序先停调度、再关 DB。
- **进程入口**：模块级 `app = create_app()` 供 `uvicorn panel.main:app` 引用；`main()` 提供 `uvicorn + uvloop` 单 worker 的本地/容器启动。

本模块**不实现任何业务逻辑**——它只负责编排。新增采集器、端点、迁移、前端卡片时，绝大多数情况只需在本模块的几个集中挂载点各加一行（见 §6 扩展点）。`create_app` / `lifespan` 的**函数签名是冻结契约**（ARCH-001 明文要求"请勿改动"），后续模块卡只在标注的 TODO 锚点处追加，不改签名。

---

## 2. 文件与关键符号清单

唯一源文件 `src/panel/main.py`（173 行）。关键符号：

| 符号 | 位置 | 职责（一句话） |
|------|------|----------------|
| 顶部 import 块 | `main.py:22-36` | 一次性导入全部待挂载的 router、采集器注册入口、调度构建、三个 job 函数、DB/repo/settings/日志。导入即"装配清单"。 |
| `lifespan(app)` | `main.py:39-112` | `@asynccontextmanager`；应用启停生命周期。启动段挂 DB/repo/gpu_repo/scheduler 到 `app.state`，关闭段反序清理。 |
| `create_app(settings=None)` | `main.py:115-151` | 同步工厂；构造 `FastAPI(lifespan=lifespan)`，配置日志、预存 settings、mount 静态、集中 include_router。返回 app。 |
| `app = create_app()` | `main.py:155` | 模块级单例，供 `uvicorn panel.main:app` 加载。 |
| `main()` | `main.py:158-168` | 进程入口：`uvicorn.run("panel.main:app", host=0.0.0.0, port=settings.port, loop="uvloop", workers=1)`。 |
| `if __name__ == "__main__"` | `main.py:171-172` | 允许 `python -m panel.main` 直接启动。 |

**被本模块编排的外部符号**（非本模块定义，但装配逻辑直接依赖其签名）：

| 符号 | 来源 | 装配中的角色 |
|------|------|--------------|
| `connection.connect(db_path)` | `db/connection.py:14` | 开 aiosqlite 连接 + WAL 等 PRAGMA + `row_factory=Row`。`lifespan` 第一步。 |
| `migrate.run(conn)` | `db/migrate.py:36` | 执行 `schema.sql` + `migrations/*.sql`（全 `IF NOT EXISTS`，幂等）。 |
| `Repository(conn)` | `db/repository.py:96` | 通用三表薄 SQL 层。挂 `app.state.repo`。 |
| `GpuRepository(conn)` | `db/gpu_repository.py:132` | ARCH-002 GPU/Azure 专用表薄层。挂 `app.state.gpu_repo`。 |
| `register_collectors(settings, repo, gpu_repo)` | `collectors/__init__.py:25` | 集中调用 azure/gpu/tailscale 三个模块工厂的 `register(...)`，各工厂按配置自决启停并写进程级 registry。 |
| `build_scheduler(repo)` | `collectors/scheduler.py:98` | 读 registry，为每个 collector 装一个 interval job（`max_instances=1, coalesce=True, id=name, next_run_time=now`）。返回**未 start** 的 `AsyncIOScheduler`。 |
| `run_5m_downsample` / `run_1h_downsample` | `collectors/gpu/downsampler.py:71 / :120` | GPU 降采样 + 专用表清理 job（TASK-016）。 |
| `prune_metric_history` | `collectors/retention.py:22` | 通用 `metric_history` 每日清理 job（TASK-040）。 |
| `setup_logging(level)` | `config/scrub.py:119` | 装根 logger + 脱敏 filter（`_ScrubFilter`），压低 `uvicorn.access` / `apscheduler` 噪声。 |
| `get_settings()` | `config/settings.py:115` | `@lru_cache` 的 `Settings` 单例。 |
| 各 `router` | 见 §4 | 五个 router + web SSR router，集中挂载。 |

---

## 3. 关键数据结构 / 契约

本模块自身不定义数据类，但它**依赖并填充**以下契约对象。

### 3.1 `app.state` —— 进程级共享状态（本模块写入，下游读取）

`lifespan` 在启动段把以下属性挂到 `app.state`，是本模块对下游（路由/SSR）暴露的"运行时句柄表"：

| 属性 | 类型 | 写入位置 | 谁读取 |
|------|------|----------|--------|
| `app.state.settings` | `Settings` | `create_app` 预存 `main.py:132`；`lifespan` 再确认 `:67` | SSR 路由（读 `stale_threshold_seconds` 等）、health |
| `app.state.db` | `aiosqlite.Connection` | `lifespan:72` | `health._probe_db`（`SELECT 1`） |
| `app.state.repo` | `Repository` | `lifespan:73` | `web/routes.py`、`api/azure`、`api/ai_usage`、`api/tailscale` 经 deps 取 |
| `app.state.gpu_repo` | `GpuRepository` | `lifespan:75` | SSR Azure 仪表盘、`/servers` 表单、GPU 趋势 API |
| `app.state.scheduler` | `AsyncIOScheduler` | `lifespan:105` | 关闭段 `shutdown`；`api/tailscale` 的 `/refresh` 可触发即时采集 |

> **关键模式（settings 注入）**：`create_app(settings=...)` 把显式传入的 settings 预存到 `app.state.settings`（`main.py:130-132`）；`lifespan` 再通过 `getattr(app.state, "settings", None) or get_settings()`（`main.py:64`）读取。这让测试能注入临时 `db_path` 等配置而**无需改 `lifespan` 签名**——这是 ARCH-001 "签名稳定"约束下的标准绕道手法。

### 3.2 `Settings`（pydantic-settings，`config/settings.py:23`）

本模块直接读取的字段（`env_prefix="PANEL_"`，从 `.env` / 环境变量加载）：

| 字段 | 默认 | 本模块用途 |
|------|------|------------|
| `db_path` | `/data/panel.db` | `connection.connect()` 入参 |
| `port` | `8080` | `main()` 中 `uvicorn.run(port=...)` |
| `log_level` | `info` | `setup_logging()` 入参 |
| `history_retention_days` | `30` | retention job 的保留窗口（TASK-040） |
| `stale_threshold_seconds` | `180` | （透传给 SSR，本模块不直接用） |

### 3.3 三个 APScheduler 维护 job 的契约

`lifespan` 在 `build_scheduler` 返回的 scheduler 上**额外**注册三个非采集型 job（业务采集 job 由 `build_scheduler` 内部已加好）：

| job id | 触发器 | args | 函数契约 |
|--------|--------|------|----------|
| `gpu_downsample_5m` | `interval, minutes=5` | `[gpu_repo]` | 聚合上一个完整 5min 桶写 `gpu_metrics_5m`；结尾清理 `gpu_metrics`(48h) + `gpu_metrics_5m`(30天) |
| `gpu_downsample_1h` | `interval, hours=1` | `[gpu_repo]` | 从 5m 桶聚合上一个 1h 桶写 `gpu_metrics_1h`（avg-of-avg / max-of-max / sum-count）；长期保留不清理 |
| `metric_history_retention` | `interval, days=1` | `[repo, settings.history_retention_days]` | `DELETE FROM metric_history WHERE collected_at < now-retention_days`；返回删除行数并 info 日志 |

> 注意 `args` 的差异：前两个传 `gpu_repo`，第三个传 `repo` + 一个整数配置。三者覆盖**不同物理表**，互补不重叠（见 ARCH-001 Addendum 的清理责任表）。

---

## 4. 对外接口与调用关系

### 4.1 `create_app()` 集中挂载清单（`main.py:142-149`）

按以下顺序 `include_router`（顺序不影响路由匹配，但保持与 ARCH-004 约定一致）：

| 挂载语句 | router 来源 | 前缀 / 路径 | TASK |
|----------|-------------|-------------|------|
| `app.include_router(health.router)` | `api/health.py:15`（无前缀） | `GET /healthz` | TASK-001 |
| `app.include_router(web_routes.router)` | `web/routes.py:29`（无前缀） | `GET /`、`GET/POST /servers`、`POST /servers/{id}/delete` | TASK-004/015/022/024/033 |
| `app.include_router(azure_router)` | `api/azure.py:38` | `prefix=/api/v1`, tags=`servers`（含 `/api/v1/servers`、GPU 趋势 `/api/v1/gpu/...`） | TASK-011/016 |
| `app.include_router(tailscale_router)` | `api/tailscale/routes.py:26` | `prefix=/api/tailscale`（`/nodes`、`/status`、`/refresh`） | TASK-021 |
| `app.include_router(ingest_router)` | `api/ingest.py:22` | `prefix=/api/ingest`（`POST /ai-usage`） | TASK-030 |
| `app.include_router(ai_usage_router)` | `api/ai_usage.py:26` | `prefix=/api`（`GET /api/ai-usage`） | TASK-033 |

静态资源：`app.mount("/static", StaticFiles(directory=src/panel/web/static), name="static")`（`main.py:139-140`，目录用 `Path(__file__).parent / "web" / "static"` 推导，与 cwd 无关）。

### 4.2 调用关系图

```
进程启动
  uvicorn panel.main:app  ──加载──▶  app = create_app()   (构建期，同步)
                                          │  setup_logging(log_level)
                                          │  FastAPI(lifespan=lifespan)
                                          │  app.state.settings = resolved
                                          │  mount /static
                                          └─ include_router × 6

  uvicorn 启动 ASGI ──触发──▶  lifespan(app)  (运行期，异步)
      ┌─ 启动段（按序）────────────────────────────────────┐
      │ 1. settings = app.state.settings or get_settings() │
      │ 2. conn = await connection.connect(db_path)  # WAL  │
      │ 3. await migrate.run(conn)                  # 幂等   │
      │ 4. app.state.repo     = Repository(conn)            │
      │ 5. app.state.gpu_repo = GpuRepository(conn)         │
      │ 6. register_collectors(settings, repo, gpu_repo)    │ ──▶ azure/gpu/tailscale 工厂 → registry.register
      │ 7. scheduler = build_scheduler(repo)                │ ──▶ 读 registry，每 collector 一个 job
      │ 8. scheduler.add_job × 3  (5m / 1h / retention)     │
      │ 9. scheduler.start();  app.state.scheduler = ...    │
      └─────────────────────────────────────────────────────┘
                         │ yield  (应用对外提供服务)
      ┌─ 关闭段（finally，反序）───────────────────────────┐
      │ a. scheduler.shutdown(wait=False)  # 先停调度       │
      │ b. await conn.close()              # 再关 DB        │
      └─────────────────────────────────────────────────────┘
```

**数据流**：请求进来 → router handler 通过 `request.app.state.repo`（或 `Depends(get_db/get_settings)`）拿到运行时句柄 → 查 SQLite → 返回。后台 APScheduler 在同一 event loop 周期性调 `run_collector`（采集 job）与三个维护 job，写库。两条流共用 `app.state` 上同一个 aiosqlite 连接。

---

## 5. 与其他模块的依赖

**上游（本模块导入/调用，是它们的消费者）**：

- `panel.config.settings`（Settings 单例）、`panel.config.scrub`（日志脱敏）
- `panel.db.connection` / `panel.db.migrate` / `panel.db.repository` / `panel.db.gpu_repository`
- `panel.collectors`（`register_collectors`）、`panel.collectors.scheduler`（`build_scheduler`）、`panel.collectors.gpu.downsampler`、`panel.collectors.retention`
- `panel.api.health` / `panel.api.azure` / `panel.api.tailscale.routes` / `panel.api.ingest` / `panel.api.ai_usage`
- `panel.web.routes`（SSR）

**下游（依赖本模块产出的，是它的消费者）**：

- 所有路由 handler —— 依赖 `app.state.repo / gpu_repo / settings / db` 已被 `lifespan` 填充。
- 容器编排（Dockerfile / docker-compose）—— 依赖 `panel.main:app` 这一 ASGI 入口与 `main()` 的 uvloop 启动约定。
- 测试 —— 依赖 `create_app(settings=...)` 工厂签名与"每次返回独立实例"语义。

**注册契约（模块卡必须遵循）**：每个采集器模块提供 `register(settings, repo[, gpu_repo]) -> None` 工厂，内部按配置自决"就绪则 `registry.register(...)`、缺失则 warning 跳过"。本模块只负责在 `register_collectors` 里集中调用它们，**不感知任何具体采集器**。这是上下游解耦的关键边界。

---

## 6. 扩展点（可操作步骤）

### 6.1 新增一个采集器（collector）

1. 在 `src/panel/collectors/<name>/` 下实现满足 `Collector` 协议的类（`name` / `interval_seconds` / `timeout_seconds` / `async collect()`），并写一个 `register(settings, repo[, gpu_repo])` 工厂在其中 `registry.register(实例)`（配置缺失则 `logger.warning` 跳过）。
2. 在 `collectors/__init__.py` 的 `register_collectors` 内追加一行 `from ... import register as register_xxx; register_xxx(settings, repo, ...)`。
3. **本模块（main.py）通常无需改动**——`build_scheduler` 会自动从 registry 给新 collector 生成 interval job。

### 6.2 新增一个 HTTP 端点 / router

1. 在 `src/panel/api/...` 写好 `router = APIRouter(prefix=..., tags=...)`。
2. 在 `main.py` 顶部 import 该 router（`main.py:22-26` 风格）。
3. 在 `create_app` 的集中挂载区（`main.py:143-148`）加一行 `app.include_router(xxx_router)`。这是唯一改动点。

### 6.3 新增一个定时维护 job（非采集型）

1. 实现一个 `async def job(...)` 函数（参考 `retention.py` / `downsampler.py`，幂等、自带日志、不向外抛）。
2. 在 `lifespan` 的 scheduler 装配区（`main.py:81-103`）`scheduler.add_job(job, "interval", <粒度>, args=[...], id="<唯一id>")`。`id` 必须全局唯一，`args` 注意传 `repo` 还是 `gpu_repo`。

### 6.4 新增一张表 / 迁移

1. 把 DDL（全部 `IF NOT EXISTS`）放进 `src/panel/db/migrations/NNN_*.sql`，文件名升序即执行序。
2. **本模块无需改动**——`migrate.run(conn)` 在 `lifespan:71` 已会自动按序执行 `schema.sql` + `migrations/*.sql`（见 `migrate.py:25-45`）。新表的读写方法加到 `Repository` / `GpuRepository`。

### 6.5 新增一个前端卡片（SSR partial）

属于 `web` 模块范畴，本模块只在 `create_app` 已挂好 `web_routes.router`，新卡片通常无需触碰 main.py（数据由 SSR 路由从 `app.state.repo/gpu_repo` 自取）。仅当卡片需要新的运行时句柄时才回到本模块往 `app.state` 加属性。

---

## 7. 配置 / 环境变量

本模块直接消费的环境变量（前缀 `PANEL_`，由 pydantic-settings 加载，见 `.env.example`）：

| 环境变量 | Settings 字段 | 默认 | 用途 |
|----------|---------------|------|------|
| `PANEL_DB_PATH` | `db_path` | `/data/panel.db` | SQLite 文件路径，传 `connect()` |
| `PANEL_PORT` | `port` | `8080` | uvicorn 监听端口 |
| `PANEL_LOG_LEVEL` | `log_level` | `info` | `setup_logging` 级别 |
| `PANEL_HISTORY_RETENTION_DAYS` | `history_retention_days` | `30` | retention job 保留天数 |

容器内由 Dockerfile 预置 `PANEL_DB_PATH=/data/panel.db`、`PANEL_PORT=8080`、`PYTHONPATH=/app/src`（`Dockerfile:45-47`）。其余采集器相关变量（Azure SP、SSH key、Tailscale socket、ingest token）由各模块消费，本装配模块不直接读，但它们的"缺失即跳过"行为通过 `register_collectors` 链路生效。

---

## 8. 容器运行

- **入口**：Dockerfile `CMD`（`Dockerfile:57`）= `uvicorn panel.main:app --host 0.0.0.0 --port 8080 --loop uvloop --workers 1`。即直接用 uvicorn CLI 加载本模块的 `app` 单例，**不走 `main()` 函数**（`main()` 主要供 `python -m panel.main` 的本地直跑）。两条路径都保证 uvloop + 单 worker。
- **多阶段 / 多 arch / 非 root**：builder 装依赖到 `/install`，runtime 拷贝；`USER app`（uid 10001）；目标 `linux/arm64`（Pi 为主）+ `linux/amd64`。
- **健康检查**：`HEALTHCHECK` 与 compose 的 `healthcheck` 都命中 `GET /healthz`（`_probe_db` 对 `app.state.db` 跑 `SELECT 1`，整体恒 200，内容反映 db 态）。`start_period=10s` 给 `lifespan` 建表留窗口。
- **卷**：`./data:/data`（SQLite 单文件持久化）；`./secrets:/secrets:ro`（凭证只读，路径引用）；Tailscale socket 卷在 compose 中以注释预留。
- **资源**：`mem_limit: 512m`（compose v2 直接生效，Pi5/8GB；Swarm 需改 `deploy.resources`）。

---

## 9. 测试位置与覆盖

| 测试文件 | 覆盖的装配关注点 |
|----------|------------------|
| `tests/test_health.py` | `create_app()` 工厂："每次返回独立实例"（`test_create_app_is_factory`）；`GET /healthz` 200 + `{status,db,time}` schema（用 `httpx.ASGITransport` 直打 ASGI，不起真实端口）。 |
| `tests/test_web.py` | SSR `GET /` 在 `app.state.repo` 缺失时**优雅降级**仍返回 200（验证 `lifespan` 未跑时路由不崩）；用 `create_app()` + 手工 `_app.state.repo = MagicMock()` 模拟运行时句柄注入。 |
| `tests/test_retention.py` | `prune_metric_history(repo, days)` job 行为（删旧留新、边界严格小于、空表返回 0），以及 `Repository.prune_history(before)` 注入方法。临时文件 DB + `connection.connect` + `migrate.run` 复刻 lifespan 前两步。 |
| `tests/test_gpu_downsample.py` | `floor_bucket` 桶对齐纯函数、`run_5m_downsample` / `run_1h_downsample` job 逻辑与专用表清理（与本模块注册的两个 GPU job 对应）。 |
| `tests/test_collectors.py` | `build_scheduler` 为每个注册 collector 生成 id=name 的 job、`run_collector` 三态降级——本模块 `lifespan` 依赖这些行为正确。 |

> 测试基本不直接驱动 `lifespan`（多数 fixture 用 `create_app()` 后手工往 `app.state` 塞 mock，绕过真实 DB/scheduler 启停），因此 `lifespan` 的启停顺序主要靠代码审查 + 容器 healthcheck 端到端验证。

---

## 10. 注意事项 / 降级语义 / gotchas

- **启停顺序是硬契约**（ARCH-001 明文）：启动 `connect → migrate → repo/gpu_repo → register_collectors → build_scheduler → add_job×3 → start`；关闭 **先 `scheduler.shutdown(wait=False)` 再 `conn.close()`**（`main.py:111-112`）。顺序颠倒会导致 job 在连接已关后还想写库。`wait=False` 不等待在跑的任务结束——配合采集 job 的"异常不外泄"设计，残留任务被 cancel 是安全的。
- **`create_app` / `lifespan` 签名冻结**：源文件 docstring 与 ARCH-001 都要求不改这两个签名。需要传配置就走 `app.state.settings` 注入（§3.1），不要给函数加参数。
- **`app = create_app()` 在 import 时即执行**（`main.py:155`）：导入 `panel.main` 会立刻构造一个 app（含 `setup_logging` 副作用、静态目录 mount），但 `lifespan` 不会触发——只有 ASGI 服务器真正启动时才跑。测试里 `create_app()` 多次调用各得独立实例。
- **健康检查恒 200**：`/healthz` 即便 DB down 也返回 200，仅 `db` 字段变 `"down"`（`health.py:31-38`）。容器 `HEALTHCHECK` 靠 HTTP 200 判活，DB 异常需看响应体——这是刻意设计（端点本身存活 ≠ DB 健康）。
- **采集器"缺配置即跳过"**：`register_collectors` 里 azure（凭证缺失）、tailscale（socket 不存在）会在工厂内 warning 跳过，gpu 始终注册（无机器时 `collect()` 返回空）。因此 `build_scheduler` 可能只装到部分 job，**这是正常降级**，不是错误。
- **`setattr` 注入式 repo 方法**：`Repository` 的若干方法（如 `prune_history`、Tailscale 相关）是在 `repository.py` 模块尾部用 `Repository.xxx = _fn` 注入的（`repository.py:388-660`），不在类体内。本模块调用 `repo.prune_history(...)` 等时这些方法已存在，但**静态类型检查器看不到**——故源码用 `# type: ignore[attr-defined]`。改 retention/Tailscale 方法签名时记得对齐注入处。
- **凭证白名单 / 路径引用**：Settings 里所有凭证字段都是**路径**（`*_file` / `*_key_path`），明文绝不进 env/DB/响应（ARCH-001 凭证规范）。本模块把 settings 挂上 `app.state`，因此**不要**给响应模型直接序列化 `app.state.settings`；对外 JSON 一律走 `PublicModel` 白名单。
- **日志脱敏全局生效**：`setup_logging` 在 `create_app` 早期调用（`main.py:123`），给根 logger 装 `_ScrubFilter`，对 token/secret/key/PEM/长 hex 等模式打码。`run_collector` 写 `collector_run.error` 前也会 `scrub()`。新加的日志无需手动脱敏，但**不要绕过 logging 直接 `print` 敏感信息**。
- **known_hosts=None / StrictHostKeyChecking=no**：GPU SSH 采集首期不校验 host key（`.env.example:32-34` 注释说明，依赖 Tailscale 内网隔离）。这是已知安全取舍，不属本模块代码但通过装配链路启用——审查时知悉即可。
- **`uvloop` 双重指定**：Dockerfile CMD 用 `--loop uvloop`，`main()` 也写了 `loop="uvloop"`。容器走 CMD 路径，`main()` 仅本地直跑用。两处保持一致即可，别只改一处。
- **三个维护 job 的 args 易混**：`gpu_downsample_*` 传 `app.state.gpu_repo`，`metric_history_retention` 传 `app.state.repo` + `settings.history_retention_days`。复制粘贴新增 job 时极易传错 repo 或漏掉配置参数。
- **e-ink 三重编码约束**（贯穿前端，装配层透传 settings）：颜色永不作为唯一状态指示，必须叠加形状符号 + 文字。本模块只提供 `stale_threshold_seconds` 等配置入口，具体编码在 `web/routes.py` 的 Jinja globals。
