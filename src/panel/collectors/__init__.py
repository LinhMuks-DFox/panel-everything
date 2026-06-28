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
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)


def register_collectors(settings: Settings, repo: Repository) -> None:
    """集中注册所有模块采集器(由 main.lifespan 在 build_scheduler 之前调用)。

    本卡为空实现:框架就绪但尚无生产采集器。模块卡(ARCH-002 azure/gpu、
    ARCH-003 tailscale)按以下契约接入——在本函数体内追加对各模块工厂的**条件**
    调用,对应配置存在则注册,缺失则 logger.warning 并跳过(不阻断启动):

        # 示例(模块卡填充):
        # if settings.azure_tenant_id and settings.azure_client_id:
        #     from panel.collectors.azure import register as register_azure
        #     register_azure(settings, repo)
        # else:
        #     logger.warning("azure collector skipped: config missing")

    模块工厂内部构造自身 Collector 实例并调用 registry.register(...);随后
    build_scheduler 读 registry 即可拿到全部 collector。repo 透传给需要在
    collect() 内读历史/快照的采集器(多数采集器只需写,故签名保留 repo 以备用)。

    Args:
        settings: 应用配置,用于按需启用各模块采集器。
        repo: 数据访问层(透传给模块工厂)。
    """
    # 本卡无生产采集器;占位入口就绪供模块卡填充。
    logger.debug("register_collectors: no production collectors registered (TASK-003 baseline)")
