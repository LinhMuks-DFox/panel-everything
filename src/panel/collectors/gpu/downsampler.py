"""GPU 历史降采样 job (ARCH-002 / TASK-016).

把 ``gpu_metrics`` 原始时序周期性聚合为 5min / 1h 降采样桶,并维护各表的
数据保留策略:

    gpu_metrics      原始表,保留 48h(由 5m job 结尾清理)
    gpu_metrics_5m   5min 桶,保留 30 天(由 5m job 结尾清理)
    gpu_metrics_1h   1h 桶,长期保留(不清理)

两个 job 由 main.py 的 scheduler 注册(见任务卡):

    scheduler.add_job(run_5m_downsample, 'interval', minutes=5, args=[gpu_repo])
    scheduler.add_job(run_1h_downsample, 'interval', hours=1, args=[gpu_repo])

桶对齐用纯函数 ``floor_bucket`` 实现,便于单测且与聚合查询解耦。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from panel.db.gpu_repository import GpuBucketRow, GpuRepository

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量:桶粒度与保留窗口
# --------------------------------------------------------------------------- #

FIVE_MIN = timedelta(minutes=5)
ONE_HOUR = timedelta(hours=1)

RAW_RETENTION = timedelta(hours=48)   # gpu_metrics 保留 48h
FIVE_MIN_RETENTION = timedelta(days=30)  # gpu_metrics_5m 保留 30 天


# --------------------------------------------------------------------------- #
# 纯函数:时间桶向下对齐
# --------------------------------------------------------------------------- #


def floor_bucket(dt: datetime, bucket: timedelta) -> datetime:
    """把 dt 向下对齐到 bucket 粒度的整数倍(以 Unix epoch 为基准)。

    naive datetime 视为 UTC。返回 tz-aware UTC datetime。

    例:floor_bucket(12:07:33, 5min) == 12:05:00
       floor_bucket(12:59:00, 1h)   == 12:00:00
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (dt - epoch).total_seconds()
    step = bucket.total_seconds()
    floored = (elapsed // step) * step
    return epoch + timedelta(seconds=floored)


def _iso(dt: datetime) -> str:
    """tz-aware UTC datetime -> ISO8601 字符串(与 gpu_repository 约定一致)。"""
    return dt.astimezone(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Job: 5min 降采样 + 保留清理
# --------------------------------------------------------------------------- #


async def run_5m_downsample(
    gpu_repo: GpuRepository, now: datetime | None = None
) -> None:
    """计算上一个完整的 5min 桶并写入 gpu_metrics_5m,随后清理过期数据。

    聚合 *上一个* 完整桶(不含当前正在累积的桶),避免在桶尚未收满时落不全的
    均值。``now`` 仅供测试注入。
    """
    now = now or datetime.now(UTC)
    bucket_start = floor_bucket(now, FIVE_MIN) - FIVE_MIN
    bucket_end = bucket_start + FIVE_MIN

    rows = await gpu_repo.aggregate_raw_buckets(bucket_start, bucket_end)
    buckets = [
        GpuBucketRow(
            server_id=server_id,
            gpu_index=gpu_index,
            avg_util_pct=_round(avg_util),
            avg_mem_pct=_round(avg_mem),
            max_temp_c=_round(max_temp),
            max_power_w=_round(max_power),
            sample_count=count,
            bucket_start=_iso(bucket_start),
        )
        for server_id, gpu_index, avg_util, avg_mem, max_temp, max_power, count in rows
        if count
    ]
    # 一轮多桶合并为单次 executemany + 单次 commit(避免逐桶提交写放大)。
    await gpu_repo.upsert_5m_buckets(buckets)
    written = len(buckets)

    # 保留清理:原始表 48h、5m 表 30 天
    raw_deleted = await gpu_repo.delete_raw_metrics_before(now - RAW_RETENTION)
    fivem_deleted = await gpu_repo.delete_5m_buckets_before(now - FIVE_MIN_RETENTION)

    logger.info(
        "run_5m_downsample: bucket=%s wrote=%d raw_pruned=%d 5m_pruned=%d",
        bucket_start.isoformat(),
        written,
        raw_deleted,
        fivem_deleted,
    )


# --------------------------------------------------------------------------- #
# Job: 1h 降采样(源表 gpu_metrics_5m,长期保留不清理)
# --------------------------------------------------------------------------- #


async def run_1h_downsample(
    gpu_repo: GpuRepository, now: datetime | None = None
) -> None:
    """计算上一个完整的 1h 桶并写入 gpu_metrics_1h。

    从 gpu_metrics_5m 聚合(减少扫描量):avg = 各 5m 桶 avg 的均值,
    max = 各 5m 桶 max 的最大值,sample_count = 各 5m 桶 sample_count 之和。
    1h 表长期保留,无清理。``now`` 仅供测试注入。
    """
    now = now or datetime.now(UTC)
    bucket_start = floor_bucket(now, ONE_HOUR) - ONE_HOUR
    bucket_end = bucket_start + ONE_HOUR

    rows = await gpu_repo.aggregate_5m_buckets(bucket_start, bucket_end)
    buckets = [
        GpuBucketRow(
            server_id=server_id,
            gpu_index=gpu_index,
            avg_util_pct=_round(avg_util),
            avg_mem_pct=_round(avg_mem),
            max_temp_c=_round(max_temp),
            max_power_w=_round(max_power),
            sample_count=count,
            bucket_start=_iso(bucket_start),
        )
        for server_id, gpu_index, avg_util, avg_mem, max_temp, max_power, count in rows
        if count
    ]
    await gpu_repo.upsert_1h_buckets(buckets)
    written = len(buckets)

    logger.info(
        "run_1h_downsample: bucket=%s wrote=%d",
        bucket_start.isoformat(),
        written,
    )


def _round(value: float | None) -> float | None:
    """聚合数值统一保留两位小数;None 透传。"""
    return None if value is None else round(value, 2)
