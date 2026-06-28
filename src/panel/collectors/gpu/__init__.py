"""GPU collector package — register() factory (ARCH-002 / TASK-013).

`register(settings, repo, gpu_repo)` 由 main.register_collectors 集中调用。

GpuCollector 无额外凭证要求(SSH 私钥按路径存于 servers 表,由 asyncssh 读取),
因此**始终注册**;若 servers 表无 has_gpu=True 记录,collect() 直接返回空列表,
不报错、不连任何主机。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from panel.config.settings import Settings
    from panel.db.gpu_repository import GpuRepository
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)


def register(settings: Settings, repo: Repository, gpu_repo: GpuRepository) -> None:
    """构造并注册 GpuCollector(始终启用,无凭证开关)。

    Args:
        settings: 应用配置(GPU 采集无需额外凭证,保留签名一致)。
        repo:     通用 Repository(透传给 collector 作 base_repo)。
        gpu_repo: GpuRepository(读 servers + 写 gpu_metrics)。
    """
    from panel.collectors.gpu.collector import GpuCollector
    from panel.collectors.registry import register as registry_register

    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=repo)
    registry_register(collector)
    logger.info("GpuCollector registered (interval=%ds)", collector.interval_seconds)
