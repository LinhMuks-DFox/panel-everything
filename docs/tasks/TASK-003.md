---
id: TASK-003
title: "Collector 框架:协议 + 注册表 + APScheduler 调度 + 框架级降级"
status: review
priority: P0
architecture: ARCH-001
dependencies: [TASK-002]
estimated_effort: M
executed_by: claude-opus-4-8[1m]
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现统一采集框架:`Collector` 协议与 `MetricSample` 数据结构、注册表、APScheduler 调度装配、以及框架级 try/timeout 降级包装(成功写 snapshot+history、运行结果落 collector_run)。模块采集器(azure/gpu/tailscale)将只实现 `collect()` 并注册,不触碰调度与降级逻辑。

## 技术规格

### Collector 协议与样本(collectors/base.py)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

UTC = timezone.utc
SampleStatus = Literal["ok", "unreachable", "error"]
RunStatus = Literal["up", "down", "error"]


@dataclass(slots=True)
class MetricSample:
    target_id: int
    metric: str
    value_num: float | None = None
    value_text: str | None = None
    status: SampleStatus = "ok"
    collected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class Collector(Protocol):
    name: str
    interval_seconds: int
    timeout_seconds: int
    async def collect(self) -> list[MetricSample]: ...


@dataclass(slots=True)
class CollectorResult:
    name: str
    status: RunStatus
    sample_count: int
    duration_ms: int
    error: str | None = None
    ran_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
```

### 注册表(collectors/registry.py)

```python
_REGISTRY: dict[str, Collector] = {}

def register(collector: Collector) -> None:
    """按 collector.name 注册;name 重复抛 ValueError。"""

def get(name: str) -> Collector: ...
def iter_collectors() -> list[Collector]: ...
def clear() -> None: ...   # 测试用,清空注册表
```

### 调度器与框架级降级(collectors/scheduler.py)

```python
async def run_collector(collector: Collector, repo: Repository) -> CollectorResult:
    """
    start = monotonic
    try:
        async with asyncio.timeout(collector.timeout_seconds):
            samples = await collector.collect()
        await repo.upsert_snapshot(collector.name, samples)
        await repo.append_history(collector.name, samples)
        result = CollectorResult(name, "up", len(samples), duration_ms)
    except TimeoutError:
        result = CollectorResult(name, "down", 0, duration_ms, error="timeout")
    except Exception as e:
        result = CollectorResult(name, "error", 0, duration_ms, error=scrub(str(e)))
    finally / always:
        await repo.record_collector_run(result)
    return result   # 不向外抛
    """

def build_scheduler(repo: Repository) -> AsyncIOScheduler:
    """
    sched = AsyncIOScheduler()
    for c in registry.iter_collectors():
        sched.add_job(run_collector, "interval",
                      seconds=c.interval_seconds,
                      args=[c, repo],
                      id=c.name,
                      max_instances=1,
                      coalesce=True,
                      next_run_time=now)   # 启动即跑一次首采
    return sched   # 未 start;由 lifespan start()
    """
```

降级语义(与 ARCH-001 一致):

- `collect()` 内部应自行把单 target 失败表达为 `status=unreachable/error` 的 sample(框架照常写库,collector_run=up)。
- `collect()` 整体抛异常 → collector_run=error,不写 sample,不影响其它 collector。
- `collect()` 超时 → collector_run=down。
- 任何情况都 `record_collector_run`;`error` 字段写库前经脱敏(可临时本地 `scrub`,TASK-005 落地统一脱敏后替换为共享实现)。

### lifespan 接入(main.py)

在 TASK-002 的 lifespan 基础上:`register_collectors(settings, repo)`(本卡先提供空实现/占位入口,模块卡填充)→ `scheduler = build_scheduler(repo)` → `scheduler.start()` → 存 `app.state.scheduler`;关闭时 `scheduler.shutdown(wait=False)`。

### 验证用空 collector

提供一个测试夹具用 `NullCollector`(返回固定 sample,`interval_seconds`/`timeout_seconds` 小值)验证全链路。**不在生产注册**。

## 实现指引

1. `base.py` 落协议与 dataclass。
2. `registry.py` 用模块级 dict;`register` 重复 name 抛错;`clear` 供测试。
3. `scheduler.py`:`run_collector` 严格按降级表实现,务必保证异常不外泄、`record_collector_run` 必达(用 try/finally 或显式分支)。`duration_ms` 用 `time.monotonic()` 差值。
4. `build_scheduler` 用 `apscheduler.schedulers.asyncio.AsyncIOScheduler`,job 参数 `max_instances=1, coalesce=True, id=c.name`,设 `next_run_time` 为 now 触发首采。
5. lifespan 接 scheduler;`register_collectors` 占位(本卡空实现 + docstring 说明模块如何接入)。
6. 测试用 `Repository` over tmp DB + `NullCollector` 及一个会抛异常/会超时的 fake collector。

## 测试要求

- [ ] 注册重复 name 抛 ValueError;`iter_collectors` 返回已注册项
- [ ] `run_collector` 正常路径:写入 snapshot+history,collector_run.status=up,sample_count 正确
- [ ] `run_collector` 异常路径:collect() 抛异常 → collector_run.status=error,无 snapshot 写入,函数不抛出
- [ ] `run_collector` 超时路径:collect() 超 timeout → collector_run.status=down,函数不抛出
- [ ] 单 collector 失败不影响同批其它 collector(并发/顺序运行两个,一个失败一个成功)
- [ ] `record_collector_run` 的 error 字段已脱敏(含敏感模式的异常消息被打码)
- [ ] `build_scheduler` 为每个注册 collector 生成一个 job,id 等于 name

## 完成标准

- [ ] `MetricSample` / `Collector` / `CollectorResult` 定义与 ARCH-001 逐字一致
- [ ] registry / scheduler 按契约实现
- [ ] 框架级降级三态(up/down/error)行为正确且异常永不外泄
- [ ] scheduler 接入 lifespan,`register_collectors` 占位入口就绪供模块卡填充
- [ ] ruff + pytest 全绿
