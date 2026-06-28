"""APScheduler 装配 + 框架级 try/timeout 降级 (ARCH-001 / TASK-003).

`run_collector` 是框架级包装:它用 `asyncio.timeout` 包住模块的 `collect()`,把
三种结果(成功 / 超时 / 异常)归一为 CollectorResult 并落 collector_run,**异常永
不外泄**——这样单个 collector 的失败不会污染同批其它 collector 或拖垮 event loop。

降级语义(与 ARCH-001 框架级降级表一致):

| 场景                         | 写 snapshot+history | collector_run.status |
|------------------------------|---------------------|----------------------|
| collect() 正常返回           | 是                  | up                   |
| collect() 超时(asyncio)     | 否                  | down                 |
| collect() 抛异常             | 否                  | error(error 脱敏)   |

> 单 target 的失败应由 collect() 自身以 status=unreachable/error 的 MetricSample
> 表达,框架照常写库,collector_run 仍记 up。框架不介入 sample 粒度的判定。
"""

from __future__ import annotations

import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from panel.collectors import registry
from panel.collectors.base import Collector, CollectorResult
from panel.config.scrub import scrub
from panel.db.repository import Repository

logger = logging.getLogger(__name__)


async def run_collector(collector: Collector, repo: Repository) -> CollectorResult:
    """运行一次采集,套框架级超时/异常降级,并落 collector_run。

    成功路径:写 latest_snapshot + metric_history,collector_run.status=up。
    超时路径:collector_run.status=down(error="timeout"),不写 sample。
    异常路径:collector_run.status=error(error=脱敏后摘要),不写 sample。

    本函数**不向外抛出任何异常**;record_collector_run 即便失败也仅记日志,
    保证调度循环不被单次采集拖垮。返回 CollectorResult 供日志/测试。
    """
    name = collector.name
    start = time.monotonic()

    try:
        async with asyncio.timeout(collector.timeout_seconds):
            samples = await collector.collect()
    except asyncio.CancelledError:
        # 外部取消(scheduler shutdown / 任务被 cancel):不降级,向上重抛让任务
        # 正常结束。asyncio.timeout 内部超时已转成 TimeoutError,不会落到这里。
        raise
    except TimeoutError:
        # asyncio.timeout 触发(Python 3.12 抛 TimeoutError):降级为 down。
        duration_ms = _elapsed_ms(start)
        result = CollectorResult(name, "down", 0, duration_ms, error="timeout")
        logger.warning("collector %s timed out after %dms", name, duration_ms)
        await _record(repo, result)
        return result
    except Exception as exc:  # noqa: BLE001 — 框架级兜底:任何 collect() 异常都降级
        duration_ms = _elapsed_ms(start)
        result = CollectorResult(name, "error", 0, duration_ms, error=scrub(str(exc)))
        logger.warning("collector %s failed: %s", name, result.error)
        await _record(repo, result)
        return result

    # --- 成功路径 ---
    duration_ms = _elapsed_ms(start)
    try:
        await repo.upsert_snapshot(name, samples)
        await repo.append_history(name, samples)
    except Exception as exc:  # noqa: BLE001 — 写库失败也降级为 error,不外泄
        result = CollectorResult(name, "error", 0, duration_ms, error=scrub(str(exc)))
        logger.warning("collector %s persist failed: %s", name, result.error)
        await _record(repo, result)
        return result

    result = CollectorResult(name, "up", len(samples), duration_ms)
    await _record(repo, result)
    return result


async def _record(repo: Repository, result: CollectorResult) -> None:
    """落 collector_run;即便失败也只记日志,绝不抛出。"""
    try:
        await repo.record_collector_run(result)
    except Exception:  # noqa: BLE001 — 可观测写入失败不应影响调度
        logger.exception("failed to record collector_run for %s", result.name)


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def build_scheduler(repo: Repository) -> AsyncIOScheduler:
    """读 registry,为每个 collector 装配一个 interval job;返回未 start 的 scheduler。

    每个 job:
      - trigger=interval,seconds=collector.interval_seconds
      - id=collector.name(便于按名管理/去重)
      - max_instances=1 + coalesce=True(防慢采集堆积、错过的触发合并为一次)
      - next_run_time=now(启动即跑一次首采,不必等首个 interval)

    由 lifespan 负责 scheduler.start() / shutdown。
    """
    scheduler = AsyncIOScheduler()
    now = _utcnow()
    for collector in registry.iter_collectors():
        scheduler.add_job(
            run_collector,
            trigger=IntervalTrigger(seconds=collector.interval_seconds),
            args=[collector, repo],
            id=collector.name,
            max_instances=1,
            coalesce=True,
            next_run_time=now,
            replace_existing=True,
        )
    return scheduler


def _utcnow():  # noqa: ANN202 — 返回 datetime,内部小工具
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)
