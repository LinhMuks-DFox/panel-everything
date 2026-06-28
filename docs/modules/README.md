# Panel Everything · 模块文档总览

> 这是 `docs/modules/` 的门户索引。Panel Everything 是一个**单进程 FastAPI 应用**：在树莓派上承载 Web + REST API + 定时采集，把实验室所有计算设备（Azure VM、GPU 机、Tailscale 节点、AI 用量额度）的状态汇聚到一个响应式单屏面板，从 Kindle e-ink 到 iPad 多终端可看。本文给出整体模块地图、模块一览、端到端数据流、全局约定与新维护者上手路径。
>
> 权威架构基线见 `docs/architecture/`：ARCH-001（总体/通用三表/Collector 契约/装配/凭证/降级）、ARCH-002（Azure VM + GPU）、ARCH-003（Tailscale）、ARCH-004（AI 额度反向推送）。各模块文档是这些架构契约的**落地说明书**。

---

## 1. 项目模块地图

一句话：**采集器周期性把设备状态写进 SQLite，API/SSR 从同一个库读出渲染单屏；AI 额度由工作站 Reporter 反向推送进来**。

分层架构（依赖自下而上，箭头表示「依赖 / 调用」方向）：

```
                      ┌──────────────────────────────────────────────┐
   外部工作站          │              浏览器 / Kindle e-ink             │
   (tools-reporter)   └───────────────┬──────────────────────────────┘
        │ POST /api/ingest             │ GET /  ·  fetch 轮询 JSON
        │                              │
        ▼                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  web (SSR + Jinja2 + panel.js)      api (REST: health/azure/      │  ← 表现层
   │  GET / · /servers                    tailscale/ingest/ai_usage)   │
   │        └──── 直接 await 复用 build_azure_dashboard / get_ai_usage_data
   └───────────────┬───────────────────────────────┬──────────────────┘
                   │ response_model / 请求体校验      │ Depends(get_repo/gpu_repo)
                   ▼                                 ▼
   ┌─────────────────────────┐         ┌─────────────────────────────┐
   │  domain (PublicModel     │         │  collectors-framework        │
   │  白名单 + DTO 模型)       │         │  Collector 协议 / registry /  │  ← 领域 + 采集层
   └─────────────────────────┘         │  scheduler / retention       │
                                       │   ├─ collectors-azure-gpu    │
                                       │   └─ collectors-tailscale    │
                                       └───────────────┬──────────────┘
                   ┌───────────────────────────────────┘ 写 MetricSample / 专用表
                   ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  db (aiosqlite WAL · Repository + GpuRepository · schema/migrate) │  ← 持久化层
   │      通用三表 + Azure/GPU 专用表 + Tailscale + ai_provider          │
   └───────────────────────────────┬─────────────────────────────────┘
                                   │ Settings / read_secret / scrub
                                   ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  config (pydantic-settings · 凭证按路径 · 日志脱敏)                 │  ← 配置/凭证基线
   └─────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────────────────────────────────────────────┐
   │  main 装配 (create_app + lifespan)                                 │  ← 把以上全部
   │  连接 → migrate → repo/gpu_repo → register_collectors →           │     按序组装成
   │  build_scheduler + 3 个维护 job → start → include_router × 6       │     一个进程
   └─────────────────────────────────────────────────────────────────┘
```

> `main 装配` 横跨所有层：它在启动期按固定顺序实例化各层对象、挂到 `app.state`，并集中 `include_router`。`tools-reporter` 是唯一不在 `src/panel/` 内、不打包进镜像的模块，跑在工作站上，仅通过 JSON 契约与面板耦合。

---

## 2. 模块一览表

| # | 模块 | 一句话职责 | 文档 | 关联 ARCH / REQ |
|---|------|-----------|------|------------------|
| 01 | **app-assembly**（装配） | 进程「装配车间」：`create_app` + `lifespan` 按序组装 DB/repo/采集器/调度/路由，提供 ASGI 入口 | [01-app-assembly.md](./01-app-assembly.md) | ARCH-001 / REQ-001 |
| 02 | **config**（配置/凭证） | 集中加载 `PANEL_*` 配置；凭证只存路径、运行时 `read_secret` 懒读；日志 `scrub` 脱敏 | [02-config.md](./02-config.md) | ARCH-001 / ARCH-004 / REQ-001 |
| 03 | **db**（持久化） | 与 SQLite 唯一的 SQL 边界：WAL 连接 + 幂等迁移 + `Repository` / `GpuRepository` 两个薄层 | [03-db.md](./03-db.md) | ARCH-001/002/003/004 |
| 04 | **collectors-framework**（采集框架） | 数据采集统一抽象：`Collector` 协议 + 注册表 + APScheduler 调度 + 框架级超时/异常降级 + retention | [04-collectors-framework.md](./04-collectors-framework.md) | ARCH-001 / REQ-001 |
| 05 | **collectors-azure-gpu** | Azure VM 电源态 + 动态公网 IP 采集；SSH `nvidia-smi` 多卡 GPU 采集；GPU 降采样 job | [05-collectors-azure-gpu.md](./05-collectors-azure-gpu.md) | ARCH-002 / REQ-002 |
| 06 | **collectors-tailscale** | 经 localapi Unix socket 采集 tailnet 节点在线三态，事件驱动落库 | [06-collectors-tailscale.md](./06-collectors-tailscale.md) | ARCH-003 / REQ-003 |
| 07 | **api**（REST 层） | 唯一对外 HTTP 契约层：servers CRUD / dashboard / GPU 趋势 / Tailscale / AI 摄取与展示；响应白名单 | [07-api.md](./07-api.md) | ARCH-001/002/003/004 |
| 08 | **web**（SSR 前端） | 服务端渲染单屏总览 + 注册页；Jinja2 partial + e-ink 三重状态编码 + `panel.js` 渐进增强 | [08-web.md](./08-web.md) | ARCH-001/002 / REQ-001/002 |
| 09 | **domain**（领域/响应模型） | `PublicModel` 白名单基类 + 全部出站/入站 Pydantic DTO；凭证三层防御的「响应层」 | [09-domain.md](./09-domain.md) | ARCH-001/002/003/004 |
| 10 | **reporter**（工作站上报器） | 部署在工作站、cron 5min 触发的单文件脚本集：读本地 AI 用量 → POST 到面板摄取端点 | [10-reporter.md](./10-reporter.md) | ARCH-004 / REQ-004 |

> 模块 01–09 的源码在 `src/panel/` 下；模块 10（reporter）在 `tools/reporter/` 下，独立于面板镜像。

---

## 3. 端到端数据流

### 3.1 正向链路：采集 → 落库 → SSR/API → 前端

```
[启动期] main.lifespan
  connection.connect(db_path)  →  migrate.run  →  Repository / GpuRepository
  → register_collectors(settings, repo, gpu_repo)     # azure/gpu/tailscale 各工厂条件注册
  → build_scheduler(repo) + add_job × 3（GPU 5m/1h 降采样 + metric_history retention）
  → scheduler.start()（next_run_time=now，启动即首采）

[采集期] APScheduler 周期触发
  run_collector(collector, repo)                       # 框架级 asyncio.timeout 包装
    ├─ collect() 成功 → upsert_snapshot + append_history + collector_run(up)
    ├─ 超时 → collector_run(down, error="timeout")，不写 sample
    └─ 异常 → collector_run(error, scrub 后摘要)，不写 sample
  · Azure / GPU 另写专用表（azure_vm_status / gpu_metrics）
  · Tailscale 仅状态变更时写 tailscale_node_events（省 IO）

[读取期] 浏览器 / Kindle
  SSR GET /  →  index() 直接 await build_azure_dashboard() / get_ai_usage_data()
              （不走内部 HTTP）+ get_all_last_runs / get_all_nodes
              →  Jinja2 渲染单屏（色+形+文三重编码，stale 读时派生）
  fetch 轮询 →  GET /api/v1/dashboard/azure · /api/tailscale/nodes · /api/ai-usage
              · /api/v1/gpu/{sid}/{idx}/history（趋势懒加载）
  e-ink     →  无 JS，靠 <meta refresh> 60s 整页刷新
```

关键：API 的两个聚合函数 `build_azure_dashboard(repo, gpu_repo)` 与 `get_ai_usage_data(repo)` **既是 HTTP 端点的实现，又被 SSR 路由直接 import 调用**，保证首屏与轮询刷新同源一致，无进程内 HTTP 往返。

### 3.2 反向链路：AI 额度推送

AI 用量数据只存在于工作站本地（`~/.codex`、`~/.claude`），面板（树莓派）读不到，故采用**反向推送**：

```
工作站 reporter.py（cron 5min）
  读本地 jsonl / 手动 json  →  构造 payload  →  POST /api/ingest/ai-usage（tailnet，可选 Bearer）
                                                       │
面板 api/ingest.py                                      ▼
  鉴权 → provider 名查 ai_provider.id → 转 MetricSample
  → upsert_snapshot("ai_usage", …) + append_history("ai_usage", …)   # 走通用三表
                                                       │
展示                                                    ▼
  GET /api/ai-usage（get_ai_usage_data）+ SSR _ai_card.html 从同一张 latest_snapshot 读回
```

摄取与展示通过 `ai_provider.id`（= `latest_snapshot.target_id`）联系；provider 名（`codex`/`claude_code`/`chatgpt`）是 Reporter 与面板之间的硬契约。

---

## 4. 关键全局约定汇总

跨模块、必须共同遵守的契约。改动任一项前请回到对应模块文档与 ARCH 基线核对。

| 约定 | 内容 | 主责模块 |
|------|------|----------|
| **凭证只存路径** | Azure secret / SSH 私钥等只在配置/DB 里存**文件路径**（`*_file` / `ssh_key_path`），运行时 `read_secret()` 从只读挂载 `/secrets` 懒读、短暂持有；明文绝不入 env/DB/响应/日志。 | config · db |
| **响应白名单 PublicModel** | 所有出站 JSON 继承 `PublicModel(extra="forbid")`；禁声明 `*secret*/*token*/*key*/*password*/private_*/ssh_key_path/node_key` 命名字段。靠「不声明 + 显式逐字段映射（禁用 `**row_dict`）+ 测试断言」三重保险（命名禁忌**非运行时强制**）。 | domain · api |
| **日志脱敏** | `setup_logging` 给 root logger 装 `_ScrubFilter`，对 token/secret/key/password/Bearer/Basic/PEM/长 hex/base64 自动打码；`collector_run.error` 写库前另显式 `scrub()`。新日志勿绕过 logging 直接 print 敏感值。 | config · collectors-framework |
| **Collector 协议 + 框架级降级** | 采集器只需实现 `name/interval_seconds/timeout_seconds/async collect()`。`run_collector` 用 `asyncio.timeout` 包裹：超时→`down`、异常→`error`（脱敏），**永不外泄异常**；单 target 失败由 sample 的 `status=unreachable/error` 表达而非抛异常。`CancelledError` 重抛不降级。 | collectors-framework |
| **Repository setattr 注入扩展** | Tailscale / ai_provider / retention 等方法在 `repository.py` 类体之后用 `Repository.xxx = _fn` 注入（不改已封口的类体）。调用处与注入处均带 `# type: ignore[attr-defined]`。`GpuRepository` 则全部写在类体内，可作正例。 | db |
| **migration 升序编号** | `migrations/NNN_*.sql` 按文件名升序执行，全 `IF NOT EXISTS` / `INSERT OR IGNORE` 幂等；种子数据时间戳用**字面量常量**保证可重复执行。ARCH-001/002 表在 `schema.sql`，ARCH-003/004 才走迁移文件；新表一律走迁移。 | db |
| **e-ink 硬约束** | CSS 禁用 `box-shadow/animation/transition/@keyframes`；状态必须**色 + 形符（●◐○◌）+ 文字**三重编码，颜色绝不作为唯一指示；e-ink 不下发 JS，信息须先在 SSR HTML 完整呈现，靠 `<meta refresh>` 降级。 | web |
| **stale 是读时派生态** | `MetricSample.status` 仅 `ok/unreachable/error`；`stale` 不是采集态，而是 API/SSR 读取时用 `collected_at`/`get_last_success` 与阈值比较现算（VM 600s / GPU 180s / AI `window×0.5` / Tailscale 90s），不落库。 | api · web · collectors-framework |
| **单进程同 loop 调度** | FastAPI + APScheduler `AsyncIOScheduler` 同进程同 event loop、单 worker、uvloop。注册表是进程级全局字典（无锁，仅适用单 worker）；`Repository` 与 `GpuRepository` 共享同一 aiosqlite 连接（WAL 提供并发读）。 | app-assembly · collectors-framework · db |

> 相关已知裁定（非约定但常被一起踩）：GPU SSH 首期 `known_hosts=None`（依赖 Tailscale 内网隔离，P3 增强）；ingest 端点 `ingest_token` 为空即不鉴权（内网默认）；`create_app`/`lifespan` 签名冻结（需传配置走 `app.state.settings` 注入）。

---

## 5. 新维护者上手路径

建议按以下顺序阅读，从「基线契约」到「装配全貌」，再按需深入业务模块：

1. **`docs/architecture/ARCH-001.md`** — 先读权威基线：设计目标、技术选型、通用三表 DDL、Collector 协议、装配契约、凭证规范、降级语义表。本目录所有模块都是它的落地。
2. **[02-config.md](./02-config.md)** — 最底层、无项目内依赖。理解配置加载、凭证按路径、日志脱敏（凭证三层防御之二层）。
3. **[09-domain.md](./09-domain.md)** — 配套理解 `PublicModel` 白名单（凭证防御第三层）与全部 DTO 形状，几乎所有上层模块都消费它。
4. **[03-db.md](./03-db.md)** — 持久化层：通用三表 + 各专用表、两个 Repository 薄层签名、setattr 注入扩展、时间归一化。
5. **[04-collectors-framework.md](./04-collectors-framework.md)** — 采集统一抽象：协议、注册表、调度、框架级降级。读完即懂「数据怎么进库」。
6. **[01-app-assembly.md](./01-app-assembly.md)** — `main.py` 装配全貌：把 2–5 串成一个进程的启停顺序与 `app.state` 句柄表。读到此处即掌握整体骨架。
7. 业务采集器（按需）：**[05-collectors-azure-gpu.md](./05-collectors-azure-gpu.md)**（含动态公网 IP 数据流）、**[06-collectors-tailscale.md](./06-collectors-tailscale.md)**（localapi + BUG-001 防护）。
8. **[07-api.md](./07-api.md)** — 对外 REST 契约、聚合函数、stale 派生。理解「数据怎么出库」。
9. **[08-web.md](./08-web.md)** — SSR 前端壳、partial 注入约定、e-ink 三重编码、`panel.js` 渐进增强。
10. **[10-reporter.md](./10-reporter.md)** — 最后读：独立于面板的工作站上报器，理解 AI 额度反向推送链路与三数据源解析。

> 快速定位技巧：每篇模块文档结构一致——§1 概述/职责 → §2 符号清单 → §3 数据结构/契约 → §4 调用关系 → §5 上下游依赖 → §6 扩展点（可操作步骤）→ §7 配置 → §8 测试 → §9 注意事项/降级语义/gotchas → §10 关联编号。要扩展某模块直接跳 §6；要避坑直接跳 §9。
