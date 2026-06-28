"""通用 metric_history retention job (ARCH-001 Addendum / TASK-040).

`metric_history` 表为 append-only,无清理会在树莓派上无限增长。本模块提供一个
周期性 retention job(由 main.py 经 APScheduler 每日触发),按 `collected_at`
删除超过保留窗口(默认 30 天,见 Settings.history_retention_days)的历史行。

GPU 专用表(gpu_metrics 等)的清理由 TASK-016 负责,两者互补、互不重叠。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)


async def prune_metric_history(repo: Repository, retention_days: int) -> int:
    """删除 metric_history 中 collected_at 早于 (now_utc - retention_days) 的行。

    Args:
        repo: Repository 实例(提供 prune_history)。
        retention_days: 保留窗口天数;早于此窗口的行被删除。

    Returns:
        删除的行数;同时记 info 日志(删除条数 + 截止时间)。
    """
    before = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = await repo.prune_history(before)
    logger.info(
        "metric_history retention: deleted %d rows older than %s",
        deleted,
        before.isoformat(),
    )
    return deleted
