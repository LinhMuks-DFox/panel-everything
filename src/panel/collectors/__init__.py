"""采集层：Collector 协议、注册表、APScheduler 调度 + 框架级降级。

核心契约在 base.py(数据类型 / Collector 协议)、registry.py(全局注册表)、
scheduler.py(调度装配 + run_collector 框架级降级)。模块采集器(azure/gpu/
tailscale)由 ARCH-002/003 在本目录下新增,只实现 collect() 并在自身工厂内调用
registry.register(...)。

`register_collectors` 是集中注册入口,由 main.lifespan 调用(见 ARCH-001 装配契约)。
本卡(TASK-003)提供空实现 + 接入约定;模块卡在此追加对各自工厂的条件调用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from panel.config.settings import Settings
    from panel.db.gpu_repository import GpuRepository
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)


def register_collectors(
    settings: Settings, repo: Repository, gpu_repo: GpuRepository
) -> None:
    """集中注册所有模块采集器(由 main.lifespan 在 build_scheduler 之前调用)。

    每个模块工厂内部判定自身配置:就绪则构造 Collector 并调 registry.register(...),
    缺失则 logger.warning 并跳过(不阻断启动)。随后 build_scheduler 读 registry
    即可拿到全部 collector。

    Args:
        settings: 应用配置,用于按需启用各模块采集器。
        repo: 通用数据访问层(透传给模块工厂)。
        gpu_repo: ARCH-002 专用表访问层(azure/gpu 采集器写 azure_vm_status /
            gpu_metrics 需要)。
    """
    # --- ARCH-002 / TASK-012: Azure VM 采集器(凭证缺失则工厂内跳过)---
    from panel.collectors.azure import register as register_azure

    register_azure(settings, repo, gpu_repo)

    # --- ARCH-002 / TASK-013: GPU 采集器(始终注册;无 GPU 机时 collect() 返回空)---
    from panel.collectors.gpu import register as register_gpu

    register_gpu(settings, repo, gpu_repo)

    # --- ARCH-003 / TASK-020: Tailscale 采集器(socket 不存在则跳过)---
    from panel.collectors.tailscale import register as register_tailscale

    register_tailscale(settings, repo)
