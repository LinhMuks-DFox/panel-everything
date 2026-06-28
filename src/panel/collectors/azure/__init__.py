"""Azure VM collector package — register() factory (ARCH-002 / TASK-012).

`register(settings, repo, gpu_repo)` 由 main.register_collectors 集中调用:

  - 四项 Azure Service Principal 配置(tenant_id / client_id / client_secret_file /
    subscription_id)任一缺失 → 记 warning 并跳过(collector disabled),不抛异常,
    面板照常运行,该数据源在前端标「未配置」。
  - 全部就绪 → 用 ClientSecretCredential 构造 ComputeManagementClient 与
    NetworkManagementClient(单例),注入 AzureVmCollector(后者用于动态解析
    VM 当前公网 IP),注册到全局 registry。

凭证安全:client_secret 通过 read_secret() 从挂载文件读取(ARCH-001 凭证按路径
约定),只在构造 credential 时短暂持有,绝不写日志、不入 DB。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from panel.config.settings import read_secret

if TYPE_CHECKING:
    from panel.config.settings import Settings
    from panel.db.gpu_repository import GpuRepository
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)


def register(settings: Settings, repo: Repository, gpu_repo: GpuRepository) -> None:
    """构造并注册 AzureVmCollector;凭证缺失则优雅跳过。

    Args:
        settings: 应用配置(读 Azure SP 凭证)。
        repo:     通用 Repository(透传给 collector 作 base_repo)。
        gpu_repo: GpuRepository(写 azure_vm_status)。
    """
    if not settings.azure_configured:
        logger.warning("Azure credentials not configured; AzureVmCollector disabled")
        return

    try:
        client_secret = read_secret(settings.azure_client_secret_file, settings)
    except (FileNotFoundError, ValueError):
        # 文件路径配了但读不到:不暴露路径细节,跳过注册。
        logger.warning("Azure client secret file unreadable; AzureVmCollector disabled")
        return

    # 延迟导入 azure SDK:仅在确实启用时才需要,缺凭证环境零开销。
    from azure.identity import ClientSecretCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient

    credential = ClientSecretCredential(
        tenant_id=settings.azure_tenant_id,
        client_id=settings.azure_client_id,
        client_secret=client_secret,
    )
    client = ComputeManagementClient(credential, settings.azure_subscription_id)
    # NetworkManagementClient 复用同一 credential,用于把 VM 解析到当前公网 IP
    # (A100 重启后公网 IP 会变,需动态解析而非静态 ssh_host)。
    network_client = NetworkManagementClient(credential, settings.azure_subscription_id)

    from panel.collectors.azure.collector import AzureVmCollector
    from panel.collectors.registry import register as registry_register

    collector = AzureVmCollector(
        client=client,
        gpu_repo=gpu_repo,
        base_repo=repo,
        network_client=network_client,
    )
    registry_register(collector)
    logger.info("AzureVmCollector registered (interval=%ds)", collector.interval_seconds)
