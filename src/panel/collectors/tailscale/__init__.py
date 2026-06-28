"""Tailscale collector package — register() factory (ARCH-003 / TASK-020).

`register(settings, repo)` 由 main.register_collectors 集中调用:

  - TAILSCALE_SOCKET_PATH (默认 /var/run/tailscale/tailscaled.sock) 不存在
    → 记 warning 并跳过 (collector disabled), 不抛异常, 面板照常运行.
  - socket 存在 → 构造 TailscaleCollector 并注册到全局 registry.

凭证:本模块无需任何 API Key, 只需 socket 文件可读 (权限由 OS 保证).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from panel.config.settings import Settings
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)


def register(settings: Settings, repo: Repository) -> None:
    """构造并注册 TailscaleCollector; socket 不存在则优雅跳过。

    Args:
        settings: 应用配置 (读 tailscale_socket / tailscale_* 参数)。
        repo:     通用 Repository (Tailscale 专用表方法已注入其上)。
    """
    socket_path: str = getattr(settings, "tailscale_socket", "/var/run/tailscale/tailscaled.sock")

    if not os.path.exists(socket_path):
        logger.warning(
            "Tailscale socket not found at %s; TailscaleCollector disabled", socket_path
        )
        return

    timeout_seconds: int = getattr(settings, "tailscale_timeout_seconds", 10)
    long_offline_hours: int = getattr(settings, "tailscale_long_offline_hours", 24)

    from panel.collectors.registry import register as registry_register
    from panel.collectors.tailscale.collector import TailscaleCollector

    collector = TailscaleCollector(
        socket_path=socket_path,
        repo=repo,
        timeout_seconds=timeout_seconds,
        long_offline_hours=long_offline_hours,
    )
    registry_register(collector)
    logger.info(
        "TailscaleCollector registered (socket=%s, interval=%ds)",
        socket_path,
        collector.interval_seconds,
    )
