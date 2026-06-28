"""Collector protocol + framework data types (ARCH-001).

定义采集层的核心数据契约:MetricSample(单指标采样)、CollectorResult(框架级
运行结果)与 Collector 协议。逐字采用 ARCH-001 的契约。

> 注:协议/注册表/调度器(registry.py / scheduler.py)的实现由 TASK-003 完成;
> 本文件的数据类型先行落地,因为 repository 薄层(TASK-002)的写方法签名直接
> 依赖 MetricSample / CollectorResult。TASK-003 在此基础上构建即可,
> 勿改动下列类型的字段定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

SampleStatus = Literal["ok", "unreachable", "error"]
RunStatus = Literal["up", "down", "error"]


@dataclass(slots=True)
class MetricSample:
    """单个 target 的单个指标采样结果。"""

    target_id: int  # 关联 target(server/node/provider)的 id;无 target 维度时用 0
    metric: str  # 指标名,如 "power_state" / "online" / "gpu_util"
    value_num: float | None = None  # 数值型指标
    value_text: str | None = None  # 文本型指标(枚举/字符串)
    status: SampleStatus = "ok"  # 单 target 该指标的采集结果
    collected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(slots=True)
class CollectorResult:
    """框架级包装的一次采集运行结果(落 collector_run 表)。"""

    name: str
    status: RunStatus  # up / down(超时)/ error(异常)
    sample_count: int
    duration_ms: int
    error: str | None = None  # 异常摘要(已脱敏)
    ran_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class Collector(Protocol):
    """采集器协议。模块卡只需实现 collect() 并注册(见 TASK-003)。"""

    name: str  # 唯一标识:'azure' | 'gpu' | 'tailscale' | ...
    interval_seconds: int  # 调度间隔
    timeout_seconds: int  # 单次 collect() 超时上限(框架用 asyncio.timeout 包)

    async def collect(self) -> list[MetricSample]:
        """采集一轮。约定:

        - 单 target 失败应捕获并以 status=unreachable/error 的 MetricSample 表达,不抛异常。
        - 仅当采集器整体不可用(配置缺失/数据源全挂)时才允许抛异常,
          由框架转 collector_run.error。
        """
        ...
