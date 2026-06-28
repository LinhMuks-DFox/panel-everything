# 模块 04：采集框架（collectors-framework）

> 维护者参考文档。读完本文，你无需通读全部源码即可理解采集框架的契约、降级语义与扩展方式。
>
> 关联文档：`docs/architecture/ARCH-001.md`（权威基线）、`docs/tasks/TASK-003.md`（框架本体）、`docs/tasks/TASK-040.md`（retention job）。

---

## 1. 模块概述与职责

采集框架是 Panel Everything 的**数据采集统一抽象层**，位于 `src/panel/collectors/`（不含各业务采集器子包）。它定义了「一个数据源如何被采集、调度、降级与持久化」的全部通用契约，使各业务模块（azure / gpu / tailscale）**只需实现一个 `collect()` 方法并注册**，完全不必关心调度、超时、异常兜底、脱敏、落库等横切逻辑。

它解决三个核心问题：

1. **统一数据契约**：所有采集结果都表达为 `MetricSample`（一个 target 的一个标量指标），框架级运行结果统一为 `CollectorResult`，落入通用三张表（`latest_snapshot` / `metric_history` / `collector_run`）。
2. **故障隔离与降级**：单个采集器抛异常或超时，只会把该数据源标记为 `error` / `down`，**绝不外泄异常**、不污染同批其它采集器、不拖垮 event loop。降级语义有一张权威的状态表（见 §3.3 / §9）。
3. **资源有界**：`metric_history` 为 append-only 表，框架提供通用 retention job（`prune_metric_history`）每日清理超过保留窗口的历史行，保证树莓派磁盘占用有界。

设计前提：**单进程、单 event loop 部署**（FastAPI + APScheduler `AsyncIOScheduler` 同进程同 loop），因此注册表是进程级全局字典，无锁。

---

## 2. 文件与关键符号清单

模块根目录 `src/panel/collectors/`（业务采集器子包 azure/ gpu/ tailscale/ 不属于本模块，但通过 `register_collectors` 接入）：

| 文件 | 符号 | 职责 |
|------|------|------|
| `base.py` | `MetricSample` (`base.py:22`) | dataclass，单 target 单指标采样结果 |
| `base.py` | `CollectorResult` (`base.py:34`) | dataclass，一次采集运行的框架级结果（落 `collector_run`） |
| `base.py` | `Collector` (`base.py:46`) | `@runtime_checkable` Protocol，采集器协议（`name`/`interval_seconds`/`timeout_seconds`/`async collect()`） |
| `base.py` | `SampleStatus` / `RunStatus` (`base.py:18-19`) | `Literal` 类型别名：sample 态 `ok/unreachable/error`；运行态 `up/down/error` |
| `registry.py` | `_REGISTRY` (`registry.py:16`) | 进程级全局字典 `name -> Collector` |
| `registry.py` | `register(collector)` (`registry.py:19`) | 按 `name` 注册；重复 name 抛 `ValueError` |
| `registry.py` | `get(name)` (`registry.py:35`) | 取已注册采集器；无则抛 `KeyError` |
| `registry.py` | `iter_collectors()` (`registry.py:44`) | 按注册顺序返回全部采集器（list） |
| `registry.py` | `clear()` (`registry.py:49`) | 清空注册表，**仅供测试**用例间复位 |
| `scheduler.py` | `run_collector(collector, repo)` (`scheduler.py:36`) | 框架级核心：超时/异常降级 + 落库，**永不外泄异常** |
| `scheduler.py` | `build_scheduler(repo)` (`scheduler.py:98`) | 读 registry，为每个采集器装配一个 interval job；返回未 start 的 `AsyncIOScheduler` |
| `scheduler.py` | `_record(repo, result)` (`scheduler.py:86`) | 落 `collector_run`，失败仅记日志不抛 |
| `scheduler.py` | `_elapsed_ms(start)` (`scheduler.py:94`) | `time.monotonic()` 差值换算毫秒 |
| `scheduler.py` | `_utcnow()` (`scheduler.py:125`) | 返回 `datetime.now(tz=UTC)`，给 job 设首采时刻 |
| `__init__.py` | `register_collectors(settings, repo, gpu_repo)` (`__init__.py:25`) | 集中注册入口，由 `main.lifespan` 调用，逐个调用各模块工厂 |
| `retention.py` | `prune_metric_history(repo, retention_days)` (`retention.py:22`) | 通用 retention job，删除 `metric_history` 中超过保留窗口的旧行 |

> 注意符号命名细节：`registry.py` 内部函数名是 `iter_collectors()`，**不是** `iter()`（避免遮蔽内建）。文档/任务卡里偶尔简写为 `iter`，实际调用务必用 `iter_collectors()`。

---

## 3. 关键数据结构 / 表 / 契约

### 3.1 `MetricSample`（`base.py:22`）

`@dataclass(slots=True)`，由 `collect()` 返回的最小单元。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `target_id` | `int` | 必填 | 关联 target（server/node/provider）的 id；**无 target 维度时用 `0`** |
| `metric` | `str` | 必填 | 指标名，如 `power_state` / `online` / `gpu_util` |
| `value_num` | `float \| None` | `None` | 数值型指标 |
| `value_text` | `str \| None` | `None` | 文本型指标（枚举/字符串） |
| `status` | `SampleStatus` | `"ok"` | 单 target 该指标的采集态：`ok` / `unreachable` / `error` |
| `collected_at` | `datetime` | `now(UTC)` | 采集时刻，**必须 UTC**；落库存 ISO8601 字符串 |

约定：单 target 失败由 `collect()` 自己以 `status=unreachable/error` 的 sample 表达，**不抛异常**；框架照常写库，`collector_run` 仍记 `up`。`stale` **不是**采集态——它是读取时按 `collected_at`/`get_last_success` 与阈值比较计算出来的展示态。

### 3.2 `CollectorResult`（`base.py:34`）

`@dataclass(slots=True)`，框架对一次运行的包装结果，由 `run_collector` 构造并落 `collector_run`。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `name` | `str` | 必填 | collector 名 |
| `status` | `RunStatus` | 必填 | `up` / `down`（超时）/ `error`（异常） |
| `sample_count` | `int` | 必填 | 成功路径为 `len(samples)`，降级路径为 `0` |
| `duration_ms` | `int` | 必填 | `time.monotonic()` 差值，毫秒 |
| `error` | `str \| None` | `None` | 异常摘要，**写库前已脱敏**；超时固定为 `"timeout"` |
| `ran_at` | `datetime` | `now(UTC)` | 运行时刻 |

### 3.3 `Collector` 协议（`base.py:46`）

`@runtime_checkable Protocol`。任何具备以下结构属性的对象都满足协议（鸭子类型，无需继承）：

```python
class Collector(Protocol):
    name: str               # 唯一标识：'azure' | 'gpu' | 'tailscale' | ...
    interval_seconds: int   # 调度间隔
    timeout_seconds: int    # 单次 collect() 超时上限（框架用 asyncio.timeout 包）
    async def collect(self) -> list[MetricSample]: ...
```

`collect()` 契约（载于 `base.py:54` docstring）：

- **单 target 失败**：捕获并以 `status=unreachable/error` 的 `MetricSample` 表达，**不抛异常**。
- **整体不可用**（配置缺失 / 数据源全挂）：才允许抛异常，由框架转 `collector_run.error`。

> `@runtime_checkable` 仅检查方法/属性**是否存在**，不检查类型签名。`isinstance(obj, Collector)` 在测试里用于断言夹具结构正确（见 `test_collectors.py:134`）。

### 3.4 通用三张表（DDL 在 `db/schema.sql`，详见 ARCH-001 §数据模型）

本框架直接读写其中三张：

- **`latest_snapshot`**：每 `(collector, target_id, metric)` 一行，`run_collector` 成功路径通过 `repo.upsert_snapshot` UPSERT。
- **`metric_history`**：append-only 时序，成功路径通过 `repo.append_history` 追加；由 `prune_metric_history` 按 `collected_at` 清理。
- **`collector_run`**：每次运行一行，所有路径（up/down/error）都通过 `repo.record_collector_run` 追加。`get_last_success(name)` 取最近 `status='up'` 的 `ran_at` 用于 stale 判定。

`collected_at` / `ran_at` 一律存 **ISO8601 UTC 字符串**，字典序与时间序一致，可直接用字符串比较（retention job 据此可直接 `WHERE collected_at < ?`）。

### 3.5 降级语义表（与 ARCH-001 框架级降级表一致，`scheduler.py:9`）

| 场景 | 写 snapshot+history | `collector_run.status` | `error` 字段 | 前端表现 |
|------|---------------------|------------------------|--------------|----------|
| `collect()` 正常返回 | 是 | `up` | `None` | 正常渲染；`sample_count=len(samples)` |
| `collect()` 超时（`asyncio.timeout`） | 否 | `down` | `"timeout"` | 数据源异常 |
| `collect()` 抛异常 | 否 | `error` | 脱敏后摘要 | 数据源异常，其余模块照常 |
| 写库失败（upsert/append 抛） | 否（已部分写则视实现） | `error` | 脱敏后摘要 | 同上 |
| 单 target 不可达/出错（采集器内部判定） | 是（sample.status=unreachable/error） | `up` | `None` | 该卡片标「不可达/错误」，采集器本身正常 |
| 数据陈旧（last success 距今 > 阈值） | —（读时计算） | 按 last run 判定 | — | 该数据源/卡片标 `stale` |

---

## 4. 对外接口与调用关系

### 4.1 `run_collector(collector, repo)`（`scheduler.py:36`）— 框架级核心

执行流程（**保证不向外抛任何异常**）：

```
start = time.monotonic()
try:
    async with asyncio.timeout(collector.timeout_seconds):
        samples = await collector.collect()
except asyncio.CancelledError:      # 外部取消(shutdown) → 重抛，不降级
    raise
except TimeoutError:                # Python 3.12 asyncio.timeout 触发 → down，落库，返回
    ...
except Exception:                   # 任何 collect() 异常 → error(脱敏)，落库，返回
    ...
# 成功路径：
try:
    await repo.upsert_snapshot(name, samples)
    await repo.append_history(name, samples)
except Exception:                   # 写库失败也降级为 error，不外泄
    ...
result = CollectorResult(name, "up", len(samples), duration_ms)
await _record(repo, result)         # 落 collector_run
return result
```

关键点：

- **被谁调用**：作为 APScheduler job 的目标函数被调度循环周期性调用（`build_scheduler` 用 `args=[collector, repo]` 注册）。测试也直接 `await run_collector(...)`。
- **调用谁**：`collector.collect()`、`repo.upsert_snapshot` / `repo.append_history` / `repo.record_collector_run`（经 `_record`）、`scrub()`（脱敏）。
- **`CancelledError` 特例**（`scheduler.py:52`）：scheduler shutdown 或任务被 cancel 时重抛，让任务正常结束，**不降级**。注意 `asyncio.timeout` 内部超时在 Python 3.12 转成 `TimeoutError`，不会落到 `CancelledError` 分支。
- **`_record` 兜底**（`scheduler.py:86`）：`record_collector_run` 即便失败也只 `logger.exception` 不抛，保证调度循环不被可观测写入拖垮。

### 4.2 `build_scheduler(repo)`（`scheduler.py:98`）

读 `registry.iter_collectors()`，对每个 collector `add_job`：

| job 参数 | 值 | 作用 |
|----------|----|------|
| 目标函数 | `run_collector` | 框架级包装 |
| `trigger` | `IntervalTrigger(seconds=collector.interval_seconds)` | 周期触发 |
| `args` | `[collector, repo]` | 传入采集器与 repo |
| `id` | `collector.name` | 按名管理/去重 |
| `max_instances` | `1` | 防慢采集堆积（同一 collector 同时只跑一个） |
| `coalesce` | `True` | 错过的多次触发合并为一次 |
| `next_run_time` | `now` | 启动即跑首采，不等首个 interval |
| `replace_existing` | `True` | 同 id 覆盖 |

返回的 scheduler **未 start**。由 `main.lifespan` 负责 `scheduler.start()` 与 `scheduler.shutdown(wait=False)`。

### 4.3 数据流（端到端）

```
main.lifespan 启动
  └─ register_collectors(settings, repo, gpu_repo)   # 各模块工厂条件注册
  └─ scheduler = build_scheduler(repo)               # 读 registry 装配 job
  └─ scheduler.add_job(prune_metric_history, days=1) # retention（main 接线）
  └─ scheduler.add_job(run_5m/1h_downsample, ...)    # GPU 降采样（main 接线，TASK-016）
  └─ scheduler.start()                               # 首采立即触发

APScheduler 周期触发
  └─ run_collector(collector, repo)
       ├─ asyncio.timeout(timeout_seconds) 包 collect()
       ├─ 成功 → upsert_snapshot + append_history + record_collector_run(up)
       └─ 超时/异常 → record_collector_run(down/error)   # 不写 sample，不外泄

SSR GET / 读取
  └─ repo.get_snapshot(collector) + repo.get_all_last_runs() → 渲染卡片 + 数据源状态条
  └─ repo.get_last_success(collector) → stale 判定

每日触发
  └─ prune_metric_history(repo, retention_days) → DELETE FROM metric_history WHERE collected_at < before
```

---

## 5. 与其他模块的依赖

**上游（本框架依赖）：**

- `panel.db.repository.Repository`（`scheduler.py:31`、`retention.py`）— 落库与读取薄层。框架只用其方法签名，不关心 SQL。
- `panel.config.scrub.scrub`（`scheduler.py:30`）— 异常摘要脱敏。
- `apscheduler.schedulers.asyncio.AsyncIOScheduler` + `IntervalTrigger`（`scheduler.py:25-26`）— 调度。
- `panel.collectors.registry`（`scheduler.py:28`）— `build_scheduler` 读注册表。
- `panel.config.settings.Settings` —（`__init__.py` / retention 经 main）提供 `history_retention_days` 等配置（仅类型引用，`TYPE_CHECKING`）。

**下游（依赖本框架）：**

- 各业务采集器子包 `collectors/azure`、`collectors/gpu`、`collectors/tailscale` — 实现 `Collector` 协议，提供 `register(...)` 工厂，调用 `registry.register(...)`。
- `panel.main`（`main.py:79` 起）— 在 `lifespan` 内调用 `register_collectors` / `build_scheduler` / `scheduler.start()` / `shutdown`，并 `add_job` 接线 retention 与 GPU 降采样。
- `panel.db.repository.prune_history`（`repository.py:643`，setattr 注入）— `prune_metric_history` 调用它。
- SSR / API 层 — 读 `collector_run` / `latest_snapshot` 渲染数据源状态与卡片。

依赖方向严格单向：业务采集器与 main 依赖框架，框架只依赖 db/config，不反向依赖任何业务采集器（`register_collectors` 内对各模块的 `import` 是**函数体内延迟导入**，避免循环依赖与启动期强耦合，见 `__init__.py:41`）。

---

## 6. 扩展点

### 6.1 新增一个采集器（最常见）

1. **建子包** `src/panel/collectors/<name>/`，内含 `collector.py`（实现类）+ `__init__.py`（`register` 工厂）。
2. **实现 Collector 协议**：定义类属性 `name`（全局唯一）、`interval_seconds`、`timeout_seconds`，以及 `async def collect(self) -> list[MetricSample]`。
   - `collect()` 内**自行捕获单 target 失败**，以 `status=unreachable/error` 的 sample 表达；只有整体不可用才抛异常。
   - 每个 sample 的 `target_id` 关联业务实体 id（无维度用 `0`），`metric` 命名稳定，`collected_at=now(UTC)`。
3. **写 `register(settings, repo[, gpu_repo])` 工厂**：在内部判定自身配置——
   - 配置/前置条件就绪 → 构造 Collector 实例并 `from panel.collectors.registry import register as registry_register; registry_register(collector)`，记 `logger.info`。
   - 配置缺失 → `logger.warning(...)` 并 `return`（**不抛异常、不阻断启动**）。参考 `collectors/tailscale/__init__.py` 的 socket 存在性判定。
4. **接入集中入口**：在 `collectors/__init__.py` 的 `register_collectors` 末尾，**函数体内**追加对你工厂的条件调用（沿用 `from panel.collectors.<name> import register as register_xxx` 的延迟导入风格，`__init__.py:40-53`）。
5. **写测试**：参考 `tests/test_collectors.py` 的夹具风格（`NullCollector` / `RaisingCollector` / `SlowCollector`）+ tmp DB。

> 你**不需要**碰 `scheduler.py`：`build_scheduler` 自动为新注册的 collector 装配 job。`interval_seconds` / `timeout_seconds` 即调度参数。

### 6.2 新增一个周期性维护 job（如 retention / 降采样）

1. 在合适模块写 `async def my_job(repo, ...) -> ...`（参考 `retention.py:prune_metric_history`）。
2. 若需新 SQL，在 `db/repository.py` **末尾用 setattr 注入**新方法（沿用 `_prune_history` 模式，`repository.py:643-660`），不改 `Repository` 类体封口处。
3. 在 `main.lifespan` 的 `build_scheduler(repo)` **之后** `scheduler.add_job(my_job, "interval", days=.../hours=..., args=[...], id="<unique-id>")`（参考 `main.py:96-103`）。`id` 必须与采集器名及其它 job id 不冲突。
4. 若需配置项，在 `config/settings.py` 加 `xxx: int = <default>`（自动接受 `PANEL_XXX` env），并以 `args=[..., settings.xxx]` 传入。

> 维护 job **不经过** `run_collector`，因此**没有框架级超时/降级兜底**——务必在 job 内部自行 try/except 或确保幂等，异常会被 APScheduler 记录但不会重抛到 loop。

### 6.3 复用 `MetricSample` 写通用表（不走采集器）

如某端点要直接写快照/历史（如 AI 用量摄取），构造 `list[MetricSample]` 后调 `repo.upsert_snapshot(name, samples)` / `repo.append_history(name, samples)` 即可——与采集器走同一落库路径，自然受 retention 管辖。

---

## 7. 配置 / 环境变量

| 配置项 (`Settings`) | env 变量 | 默认 | 用途 | 出处 |
|---------------------|----------|------|------|------|
| `history_retention_days` | `PANEL_HISTORY_RETENTION_DAYS` | `30` | `metric_history` 保留窗口天数；retention job 删早于 `now-该天数` 的行 | `config/settings.py:88` |

本框架自身**无其它专属 env**。采集器的调度参数（`interval_seconds` / `timeout_seconds`）由各采集器类属性决定（通常从各模块自己的 settings 字段读，如 tailscale 的 `tailscale_timeout_seconds`）。retention / 降采样 job 的触发频率（每日 / 5min / 1h）硬编码在 `main.lifespan` 的 `add_job` 里（`main.py:82-103`）。

> 脱敏所覆盖的敏感模式（token/secret/password/api_key/key/Bearer/Basic/PEM/长 hex/长 base64）由 `config/scrub.py` 定义，不通过 env 配置。

---

## 8. 测试位置与覆盖

### `tests/test_collectors.py`（TASK-003，框架本体）

夹具：`conn`（tmp 文件 DB + migrate，WAL 需文件而非 `:memory:`）、`repo`、`_clean_registry`（autouse，每用例前后 `registry.clear()`）。fake 采集器：`NullCollector`（返回固定 sample）、`RaisingCollector`（抛含敏感串的异常）、`SlowCollector`（睡眠超 `timeout_seconds`）。

覆盖项：

- registry：注册/枚举（`test_register_and_iter`）、重复 name 抛 `ValueError`（`test_register_duplicate_raises`）、`clear`（`test_clear`）、协议 `isinstance`（`test_collector_protocol_runtime_checkable`）。
- `run_collector` 成功路径：写 snapshot+history、`collector_run.status=up`、`sample_count` 正确、`get_last_success` 非 None（`test_run_collector_success_writes_all`）。
- 异常路径：`status=error`、无 snapshot/history、不外泄、`get_last_success` 为 None（`test_run_collector_exception_degrades`）。
- 脱敏：`token=...` 不出现在 `error` 与库内、出现 `***`（`test_run_collector_error_is_scrubbed`）。
- 超时路径：`status=down`、`error="timeout"`、无 snapshot（`test_run_collector_timeout_degrades`）。
- 故障隔离：并发跑 good+bad，互不影响（`test_failure_isolation`）。
- `build_scheduler`：每个 collector 一个 job、`job.id == collector.name`（`test_build_scheduler_one_job_per_collector`）、返回未 start（`test_build_scheduler_not_started`）。
- lifespan 集成：经 `app.router.lifespan_context` 启动→首采落库→关闭后 scheduler 停（`test_lifespan_starts_scheduler_and_runs_first_collect`）。

### `tests/test_retention.py`（TASK-040，retention job）

- 删旧保新、返回删除条数与实际一致（`test_prune_deletes_old_keeps_new`）。
- 空表 / 全新行返回 0 不报错（`test_prune_returns_zero_on_empty_table` / `_when_all_new`）。
- 保留窗口边界（25 天保留 / 35 天删，retention=30）（`test_prune_respects_retention_days_boundary`）。
- `Repository.prune_history` 边界：**严格小于 `before`**，恰好等于不删（`test_prune_history_strictly_less_than_before`）。
- naive datetime 经 `_iso` 归一化为 UTC 比较（`test_prune_history_normalizes_naive_datetime`）。

各业务采集器的 `collect()` 单测在 `tests/test_azure_collector.py` / `test_gpu_collector.py` / `test_tailscale_api.py` 等，不属本框架范畴但依赖本框架契约。

---

## 9. 注意事项 / 降级语义 / gotchas

- **异常永不外泄是硬约束**：`run_collector` 用宽 `except Exception`（`scheduler.py:63`，带 `noqa: BLE001`）做框架级兜底。**不要**在 `collect()` 里指望异常能向上传播改变控制流——它一定被降级成 `error`。单 target 失败请用 `status=unreachable/error` 的 sample 表达，而不是抛异常。
- **`CancelledError` 不可降级**：必须重抛（`scheduler.py:52`），否则 shutdown 时任务无法正常取消。已知 Python 3.12 行为：`asyncio.timeout` 内部超时转成 `TimeoutError`，不会与外部 cancel 混淆。
- **超时只取消，不强杀**：`asyncio.timeout` 取消 `collect()` 协程；若 `collect()` 内有不响应取消的阻塞调用（如同步阻塞 IO），超时不会立刻生效。采集器内的 IO 必须是 awaitable 的。
- **写库失败也降级**（`scheduler.py:75`）：upsert/append 抛异常同样转 `error`、不外泄。极端情况下可能 snapshot 已写、history 未写（两次独立 commit），但运行态会标 `error`，可观测可见。
- **脱敏发生在两处，互补**：① `run_collector` 显式 `scrub(str(exc))` 后才放入 `CollectorResult.error`（写库前脱敏，`scheduler.py:65`）；② `setup_logging` 安装的 `_ScrubFilter` 对所有日志记录脱敏。`record_collector_run` 写库**不再**脱敏，依赖调用方已脱敏（`repository.py:173` docstring 明示）。新增直接写 `collector_run` 的路径时务必自行 `scrub`。
- **注册表是全局可变状态**：进程级单例字典，无锁，仅适用于单进程单 loop 部署。多 worker 会各自持有独立注册表（当前部署是单 worker，见 ARCH-001）。测试必须用 `registry.clear()`（已由 autouse fixture 处理）避免用例间污染。
- **重复 name 抛 `ValueError`**：`name` 全局唯一。两个采集器同名（或同采集器被注册两次）会在 `register` 抛错。
- **`next_run_time=now` → 启动即首采**：`build_scheduler` 让每个 job 立即跑一次，不等首个 interval。lifespan 测试据此轮询等待首采落库。
- **`tmp 文件 DB 而非 `:memory:`**：WAL 模式需要真实文件，测试夹具用 `tmp_path / "panel.db"`。
- **retention 仅作用于 `metric_history`**：GPU 专用表（`gpu_metrics` / `gpu_metrics_5m` / `gpu_metrics_1h`）的清理由 TASK-016 的降采样/清理 job 负责，两者互补、互不重叠（见 ARCH-001 Addendum 表）。不要让 `prune_metric_history` 去碰 GPU 表。
- **retention 的「严格小于」边界**：`DELETE ... WHERE collected_at < before`，恰好等于 `before` 的行保留。`before = now(UTC) - retention_days`。
- **`Repository.prune_history` 经 setattr 注入**（`repository.py:660`），不是类体内声明的方法——静态类型检查器看不到它（代码用 `# type: ignore[attr-defined]`）。同模式还有 tailscale / ai_provider 的注入方法。新增 Repository 维护方法请沿用此模式，不改类体封口处。
- **维护 job 无框架降级**：retention / 降采样 job 不经 `run_collector`，无超时/异常兜底，异常仅由 APScheduler 记录。需自行保证幂等与健壮。
- **`iter` vs `iter_collectors`**：实际函数名是 `iter_collectors()`；ARCH/TASK 文档里的 `iter` 是简写。

---

## 10. 关联 REQ / ARCH / TASK 编号

| 类型 | 编号 | 说明 |
|------|------|------|
| 需求 | REQ-001 | 总体需求（非功能约束、部署、访问控制），ARCH-001 的来源 |
| 架构 | ARCH-001 | 总体架构与基础设施基线：Collector 契约、通用三张表、降级语义表、装配契约、retention Addendum（**权威基线**） |
| 任务 | TASK-002 | SQLite(WAL) 连接 + 通用 schema + repository 薄层（本框架的落库依赖，先行落地 `base.py` 数据类型） |
| 任务 | TASK-003 | 本框架本体：`Collector` 协议 + 注册表 + APScheduler 调度 + 框架级降级（`status: done`） |
| 任务 | TASK-005 | 配置与凭证管理 + response model 白名单 + 日志脱敏（`scrub` 来源） |
| 任务 | TASK-040 | 通用 `metric_history` retention job（`prune_metric_history`） |
| 任务 | TASK-012 / TASK-013 / TASK-020 | 接入本框架的业务采集器工厂（azure / gpu / tailscale），由 `register_collectors` 集中调用 |
| 任务 | TASK-016 | GPU 专用表降采样/清理 job（与 retention 互补，同经 main 接线） |
