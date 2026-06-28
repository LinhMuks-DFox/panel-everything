"""AzureVmCollector — Azure VM 电源态采集器 (ARCH-002 / TASK-012).

通过 azure-mgmt-compute SDK 的 `virtual_machines.list_all(expand="instanceView")`
单次调用拉取订阅下所有 VM 的电源态,映射为统一枚举,写入:

  - azure_vm_status 专用表(每台已注册 VM 一行,upsert)
  - 通用 latest_snapshot / metric_history(框架经 run_collector 自动写入,
    本采集器只返回 MetricSample 列表)

TASK-018 扩展:若注入了 network_client(NetworkManagementClient),额外解析每台
已注册 VM 当前关联的公网 IP(VM→NIC→public IP 资源链路),产出
metric='public_ip' 的 MetricSample。A100 等 VM 重启后公网 IP 会变,GpuCollector
据此快照动态选择 SSH 目标主机,而非静态 ssh_host。IP 解析失败完全隔离,不影响
power_state sample,也不抛异常。

设计要点(遵循 ARCH-001 Collector 协议与降级语义):

  - SDK 是同步的;`list_all()` 在 asyncio.to_thread 中执行,避免阻塞 event loop。
  - 只监控 servers 表中显式注册(按 azure_vm_name 匹配)的 VM,未注册的 Azure VM
    跳过,避免越权采集。
  - 单台 VM 解析失败:产出 status='error' 的 MetricSample,不影响其它台;旧的
    azure_vm_status 行保留(不 upsert),前端凭 is_stale 提示陈旧。
  - 整体调用失败(认证/网络/SDK 异常):collect() 向上抛异常,由 run_collector
    框架层捕获并记 collector_run.status='error'(error 已脱敏)。

凭证保护:client 由 register() 工厂用 ClientSecretCredential 构造后注入;本类不
持有也不记录任何 secret。日志只写 VM 名/状态,绝不写凭证。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from panel.collectors.base import MetricSample

if TYPE_CHECKING:
    from panel.db.gpu_repository import GpuRepository
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)

# Azure PowerState code (lowercased) -> (display state, is_running float).
# value_text 存展示串;value_num 存 1.0(running)/0.0(其它),供趋势计算。
POWER_STATE_MAP: dict[str, tuple[str, float]] = {
    "powerstate/running": ("Running", 1.0),
    "powerstate/stopped": ("Stopped", 0.0),
    "powerstate/deallocated": ("Deallocated", 0.0),
    "powerstate/starting": ("Starting", 0.0),
    "powerstate/stopping": ("Stopping", 0.0),
    "powerstate/deallocating": ("Deallocating", 0.0),
}
_UNKNOWN: tuple[str, float] = ("Unknown", 0.0)


def _parse_power_state(statuses: list[Any] | None) -> tuple[str, str | None, float]:
    """从 instanceView.statuses 列表解析电源态。

    取 code 前缀为 ``PowerState/`` 的条目(忽略大小写),映射为展示状态。
    缺失或未识别返回 ("Unknown", None, 0.0)。

    Args:
        statuses: VM instanceView 的 statuses 列表;每项需有 ``code`` 属性/键。

    Returns:
        (display_state, raw_code, is_running_float)。raw_code 为原始 Azure code
        (如 "PowerState/running"),未找到则 None。
    """
    if not statuses:
        return _UNKNOWN[0], None, _UNKNOWN[1]

    for status in statuses:
        code = _get(status, "code")
        if not code:
            continue
        code_str = str(code)
        if code_str.lower().startswith("powerstate/"):
            display, is_running = POWER_STATE_MAP.get(code_str.lower(), _UNKNOWN)
            return display, code_str, is_running

    return _UNKNOWN[0], None, _UNKNOWN[1]


def _get(obj: Any, key: str) -> Any:
    """统一从 SDK 对象(属性)或测试 fixture(dict)取字段。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _parse_resource_id(resource_id: str | None) -> tuple[str | None, str | None]:
    """从 Azure ARM 资源 id 提取 (resource_group, resource_name)。

    资源 id 形如::

        /subscriptions/{sub}/resourceGroups/{rg}/providers/{ns}/{type}/{name}

    大小写不敏感地匹配 ``resourceGroups`` 段;取末段为资源名。任一缺失返回
    (None, None)。SDK 的 get(rg, name) 据此定位资源。
    """
    if not resource_id:
        return None, None
    parts = [p for p in resource_id.split("/") if p]
    rg: str | None = None
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            rg = parts[i + 1]
            break
    name = parts[-1] if parts else None
    if rg is None or name is None:
        return None, None
    return rg, name


@dataclass
class AzureVmCollector:
    """Azure VM 电源态采集器。满足 ARCH-001 Collector 协议。

    构造参数(均由 register() 工厂注入,便于测试替换 mock):
        client:    azure-mgmt-compute ComputeManagementClient(单例,跨调用复用)。
        gpu_repo:  写 azure_vm_status 专用表。
        base_repo: 保留以备读取(本采集器不直接写通用表,由框架经返回值写)。
    """

    client: Any  # ComputeManagementClient — Any 以避免在无凭证环境强依赖类型
    gpu_repo: GpuRepository
    base_repo: Repository
    # NetworkManagementClient — 由 register() 注入;为 None 时不解析公网 IP
    # (向后兼容:无此 client 的旧构造仍只产 power_state sample)。Any 类型避免
    # 在无 azure-mgmt-network 依赖的环境强依赖该类型。
    network_client: Any = None
    name: str = "azure_vm"
    interval_seconds: int = 300
    timeout_seconds: int = 60

    # Collector 协议要求的实例属性已由 dataclass 字段提供。

    async def collect(self) -> list[MetricSample]:
        """采集一轮所有已注册 VM 的电源态。

        流程:
          1. 读 servers 表,按 azure_vm_name 建索引(只监控显式注册的机器)。
          2. asyncio.to_thread 调用同步 SDK list_all(expand=instanceView)。
          3. 对每个匹配到注册记录的 VM:解析电源态 → upsert azure_vm_status →
             产出 MetricSample(metric='power_state')。
          4. 单台失败:产出 status='error' 的 sample,不 upsert(保留旧值)。
          5. 整体 SDK 调用失败:异常向上传播,由 run_collector 框架降级。

        Returns:
            MetricSample 列表,框架据此写 latest_snapshot / metric_history。
        """
        servers = await self.gpu_repo.get_all_servers()
        # 仅监控注册了 azure_vm_name 的机器;按名建索引(同名取首条)。
        by_vm_name: dict[str, Any] = {}
        for s in servers:
            if s.azure_vm_name:
                by_vm_name.setdefault(s.azure_vm_name, s)

        if not by_vm_name:
            logger.debug("azure_vm: no registered servers with azure_vm_name; nothing to collect")
            return []

        # 同步 SDK 调用放线程池;异常向上抛(框架层降级为 collector_run=error)。
        vms = await asyncio.to_thread(self._fetch_vms_sync)

        now = datetime.now(UTC)
        samples: list[MetricSample] = []
        for vm in vms:
            vm_name = _get(vm, "name")
            if vm_name is None or vm_name not in by_vm_name:
                # 未注册的 Azure VM:跳过(避免越权采集)。
                continue
            server = by_vm_name[vm_name]
            samples.extend(await self._process_vm(vm, server, now))

        return samples

    def _fetch_vms_sync(self) -> list[Any]:
        """同步消费 SDK 分页迭代器,返回 VM 列表。在 to_thread 中执行。"""
        instance_view = self.client.virtual_machines.list_all(expand="instanceView")
        return list(instance_view)

    async def _process_vm(
        self, vm: Any, server: Any, now: datetime
    ) -> list[MetricSample]:
        """处理单台 VM:解析电源态(+ 可选公网 IP),upsert 专用表,产出 MetricSample。

        产出:
          - 必有一条 metric='power_state' 的 sample(解析失败时 status='error')。
          - 若注入了 network_client 且解析出公网 IP,额外追加一条
            metric='public_ip'(value_text=IP)的 sample;解析失败则跳过该条
            (记 debug 日志,不影响 power_state、不抛异常)。

        单台 power_state 解析异常不抛出:返回 [status='error'],且不 upsert
        (保留旧值)。
        """
        try:
            instance_view = _get(vm, "instance_view")
            statuses = _get(instance_view, "statuses") if instance_view is not None else None
            display, raw_code, is_running = _parse_power_state(statuses)

            await self.gpu_repo.upsert_vm_status(
                server_id=server.id,
                power_state=display,
                power_state_raw=raw_code,
                is_running=bool(is_running),
                collected_at=now,
            )
            samples = [
                MetricSample(
                    target_id=server.id,
                    metric="power_state",
                    value_num=is_running,
                    value_text=display,
                    status="ok",
                    collected_at=now,
                )
            ]
        except Exception:  # noqa: BLE001 — 单台失败隔离,不污染其它 VM
            logger.warning("azure_vm: failed to process VM %r", _get(vm, "name"))
            return [
                MetricSample(
                    target_id=server.id,
                    metric="power_state",
                    value_num=0.0,
                    value_text="Unknown",
                    status="error",
                    collected_at=now,
                )
            ]

        # 公网 IP 解析(独立于 power_state,失败完全隔离、不影响上面的 sample)。
        if self.network_client is not None:
            ip = await self._resolve_public_ip(vm)
            if ip:
                samples.append(
                    MetricSample(
                        target_id=server.id,
                        metric="public_ip",
                        value_num=None,
                        value_text=ip,
                        status="ok",
                        collected_at=now,
                    )
                )
        return samples

    async def _resolve_public_ip(self, vm: Any) -> str | None:
        """解析 VM 当前关联的公网 IP 地址;失败返回 None(记 debug 日志,不抛)。

        链路:VM.network_profile.network_interfaces[0].id → NIC →
        ip_configurations[*].public_ip_address.id → public IP 资源 → .ip_address。

        同步 SDK 调用统一放进 asyncio.to_thread,避免阻塞 event loop。
        """
        try:
            nic_id = self._first_nic_id(vm)
            if not nic_id:
                logger.debug("azure_vm: no NIC on VM %r; skip public_ip", _get(vm, "name"))
                return None

            ip = await asyncio.to_thread(self._fetch_public_ip_sync, nic_id)
            if not ip:
                logger.debug(
                    "azure_vm: no public IP for VM %r; skip public_ip", _get(vm, "name")
                )
            return ip
        except Exception:  # noqa: BLE001 — IP 解析失败不影响 power_state,不外抛
            logger.debug(
                "azure_vm: public IP resolution failed for VM %r", _get(vm, "name")
            )
            return None

    @staticmethod
    def _first_nic_id(vm: Any) -> str | None:
        """从 VM.network_profile.network_interfaces 取第一个 NIC 的资源 id。"""
        network_profile = _get(vm, "network_profile")
        if network_profile is None:
            return None
        nics = _get(network_profile, "network_interfaces")
        if not nics:
            return None
        return _get(nics[0], "id")

    def _fetch_public_ip_sync(self, nic_id: str) -> str | None:
        """同步:用 network_client 取 NIC → 其 public IP 资源 → ip_address。

        在 asyncio.to_thread 中执行。从资源 id 解析 resource_group / name 后调用
        SDK 的 get();任一环节缺失则返回 None。
        """
        nic_rg, nic_name = _parse_resource_id(nic_id)
        if not nic_rg or not nic_name:
            return None
        nic = self.network_client.network_interfaces.get(nic_rg, nic_name)

        ip_configs = _get(nic, "ip_configurations")
        if not ip_configs:
            return None
        for ip_config in ip_configs:
            public_ip_ref = _get(ip_config, "public_ip_address")
            public_ip_id = _get(public_ip_ref, "id") if public_ip_ref is not None else None
            if not public_ip_id:
                continue
            pip_rg, pip_name = _parse_resource_id(public_ip_id)
            if not pip_rg or not pip_name:
                continue
            public_ip = self.network_client.public_ip_addresses.get(pip_rg, pip_name)
            ip_address = _get(public_ip, "ip_address")
            if ip_address:
                return str(ip_address)
        return None
