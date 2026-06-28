"""TASK-018: AzureVmCollector 动态公网 IP 解析单元测试(不连真实 Azure)。

覆盖:
  - network_client 注入时,VM→NIC→public IP 链路解析出 IP → 产出
    metric='public_ip' 的 MetricSample(与 power_state sample 并存)。
  - SDK 属性对象形态(SimpleNamespace)同样可解析。
  - 解析失败(无 NIC / 无 public IP 引用 / public IP 资源无 ip_address /
    SDK get 抛异常)→ 不产 public_ip sample,且不影响 power_state sample,
    collect() 不抛异常。
  - network_client=None(向后兼容)→ 仅 power_state,无 public_ip sample。
  - _parse_resource_id 边界:正常 / 大小写 / 残缺。

mock 策略:FakeNetworkClient.network_interfaces.get / public_ip_addresses.get
返回预设对象,不发起任何网络请求。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from panel.collectors.azure.collector import (
    AzureVmCollector,
    _parse_resource_id,
)
from panel.db import connection, migrate
from panel.db.gpu_repository import GpuRepository
from panel.db.repository import Repository
from panel.domain.models import ServerIn

# Azure 资源 id 模板
_SUB = "/subscriptions/sub-123"
_NIC_ID = f"{_SUB}/resourceGroups/rg-gpu/providers/Microsoft.Network/networkInterfaces/nic-01"
# 第二块(primary)NIC 的资源 id,用于多 NIC primary-preference 测试
_NIC_ID_PRIMARY = (
    f"{_SUB}/resourceGroups/rg-gpu/providers/Microsoft.Network/networkInterfaces/nic-primary"
)
_PIP_ID = f"{_SUB}/resourceGroups/rg-gpu/providers/Microsoft.Network/publicIPAddresses/pip-01"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
async def conn(tmp_path: Path):
    db_path = str(tmp_path / "panel.db")
    c = await connection.connect(db_path)
    await migrate.run(c)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def gpu_repo(conn) -> GpuRepository:
    return GpuRepository(conn)


@pytest.fixture
def base_repo(conn) -> Repository:
    return Repository(conn)


async def _register_server(repo: GpuRepository, name: str, azure_vm_name: str) -> int:
    return await repo.insert_server(
        ServerIn(name=name, azure_vm_name=azure_vm_name, ssh_key_path="/secrets/key")
    )


def _vm_running_with_nic(nic_id: str | None = _NIC_ID) -> dict[str, Any]:
    """构造一个 running 且带 network_profile 的 VM(dict 形态)。"""
    nics = [{"id": nic_id}] if nic_id is not None else []
    return {
        "name": "gpu-vm-01",
        "instance_view": {"statuses": [{"code": "PowerState/running"}]},
        "network_profile": {"network_interfaces": nics},
    }


class _FakeGettable:
    """打桩 network_interfaces / public_ip_addresses 子客户端:.get(rg, name)。"""

    def __init__(self, result: Any = None, raise_exc: Exception | None = None) -> None:
        self._result = result
        self._raise = raise_exc
        self.calls: list[tuple[str, str]] = []

    def get(self, resource_group: str, name: str) -> Any:
        self.calls.append((resource_group, name))
        if self._raise is not None:
            raise self._raise
        return self._result


class FakeNetworkClient:
    """打桩 NetworkManagementClient:暴露 network_interfaces / public_ip_addresses。"""

    def __init__(
        self,
        nic: Any = None,
        public_ip: Any = None,
        nic_exc: Exception | None = None,
        pip_exc: Exception | None = None,
    ) -> None:
        self.network_interfaces = _FakeGettable(nic, nic_exc)
        self.public_ip_addresses = _FakeGettable(public_ip, pip_exc)


def _make_collector(
    gpu_repo: GpuRepository,
    base_repo: Repository,
    vms: list[Any],
    network_client: Any = None,
) -> AzureVmCollector:
    class _FakeVms:
        def list_all(self, expand: str | None = None) -> list[Any]:  # noqa: ARG002
            return list(vms)

    client = SimpleNamespace(virtual_machines=_FakeVms())
    return AzureVmCollector(
        client=client,
        gpu_repo=gpu_repo,
        base_repo=base_repo,
        network_client=network_client,
    )


# --------------------------------------------------------------------------- #
# _parse_resource_id
# --------------------------------------------------------------------------- #


def test_parse_resource_id_normal():
    assert _parse_resource_id(_NIC_ID) == ("rg-gpu", "nic-01")


def test_parse_resource_id_case_insensitive_rg():
    rid = "/subscriptions/s/RESOURCEGROUPS/MyRG/providers/x/y/the-name"
    assert _parse_resource_id(rid) == ("MyRG", "the-name")


def test_parse_resource_id_missing_returns_none():
    assert _parse_resource_id(None) == (None, None)
    assert _parse_resource_id("") == (None, None)
    # 无 resourceGroups 段
    assert _parse_resource_id("/subscriptions/s/providers/x/y/z") == (None, None)


# --------------------------------------------------------------------------- #
# 公网 IP 解析:成功路径
# --------------------------------------------------------------------------- #


async def test_resolves_public_ip_dict_form(gpu_repo, base_repo):
    sid = await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    nic = {"ip_configurations": [{"public_ip_address": {"id": _PIP_ID}}]}
    public_ip = {"ip_address": "20.30.40.50"}
    net = FakeNetworkClient(nic=nic, public_ip=public_ip)
    collector = _make_collector(gpu_repo, base_repo, [_vm_running_with_nic()], net)

    samples = await collector.collect()

    by_metric = {s.metric: s for s in samples}
    assert by_metric["power_state"].value_text == "Running"
    assert by_metric["power_state"].status == "ok"

    pip = by_metric["public_ip"]
    assert pip.target_id == sid
    assert pip.value_text == "20.30.40.50"
    assert pip.value_num is None
    assert pip.status == "ok"

    # SDK get 用解析出的 rg/name 调用
    assert net.network_interfaces.calls == [("rg-gpu", "nic-01")]
    assert net.public_ip_addresses.calls == [("rg-gpu", "pip-01")]


async def test_resolves_public_ip_sdk_attribute_form(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    nic = SimpleNamespace(
        ip_configurations=[
            SimpleNamespace(public_ip_address=SimpleNamespace(id=_PIP_ID))
        ]
    )
    public_ip = SimpleNamespace(ip_address="13.14.15.16")
    net = FakeNetworkClient(nic=nic, public_ip=public_ip)
    vm = SimpleNamespace(
        name="gpu-vm-01",
        instance_view=SimpleNamespace(statuses=[SimpleNamespace(code="PowerState/running")]),
        network_profile=SimpleNamespace(network_interfaces=[SimpleNamespace(id=_NIC_ID)]),
    )
    collector = _make_collector(gpu_repo, base_repo, [vm], net)

    samples = await collector.collect()
    by_metric = {s.metric: s for s in samples}
    assert by_metric["public_ip"].value_text == "13.14.15.16"


async def test_prefers_primary_nic_when_not_first(gpu_repo, base_repo):
    """多 NIC 且 primary 不在首位 → 解析 primary NIC(而非 nics[0])的 IP。"""
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    # nics[0] 非 primary(若错误地取首个,会查到 nic-01 而非 nic-primary)。
    vm = {
        "name": "gpu-vm-01",
        "instance_view": {"statuses": [{"code": "PowerState/running"}]},
        "network_profile": {
            "network_interfaces": [
                {"id": _NIC_ID, "primary": False},
                {"id": _NIC_ID_PRIMARY, "primary": True},
            ]
        },
    }
    nic = {"ip_configurations": [{"public_ip_address": {"id": _PIP_ID}}]}
    net = FakeNetworkClient(nic=nic, public_ip={"ip_address": "5.6.7.8"})
    collector = _make_collector(gpu_repo, base_repo, [vm], net)

    samples = await collector.collect()
    by_metric = {s.metric: s for s in samples}
    assert by_metric["public_ip"].value_text == "5.6.7.8"
    # 关键断言:SDK 用 primary NIC 的名字查询,而不是数组首个 NIC。
    assert net.network_interfaces.calls == [("rg-gpu", "nic-primary")]


async def test_falls_back_to_first_nic_when_no_primary_flag(gpu_repo, base_repo):
    """无任何 NIC 标记 primary(单 NIC 常省略该标志)→ 回退到第一个 NIC。"""
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    nic = {"ip_configurations": [{"public_ip_address": {"id": _PIP_ID}}]}
    net = FakeNetworkClient(nic=nic, public_ip={"ip_address": "1.1.1.1"})
    # _vm_running_with_nic 的 NIC 不带 primary 标志 → 走回退分支。
    collector = _make_collector(gpu_repo, base_repo, [_vm_running_with_nic()], net)

    samples = await collector.collect()
    by_metric = {s.metric: s for s in samples}
    assert by_metric["public_ip"].value_text == "1.1.1.1"
    assert net.network_interfaces.calls == [("rg-gpu", "nic-01")]


async def test_skips_ip_config_without_public_ip_then_uses_next(gpu_repo, base_repo):
    """第一个 ip_config 无 public_ip,第二个有 → 取第二个的 IP。"""
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    nic = {
        "ip_configurations": [
            {"public_ip_address": None},
            {"public_ip_address": {"id": _PIP_ID}},
        ]
    }
    net = FakeNetworkClient(nic=nic, public_ip={"ip_address": "1.2.3.4"})
    collector = _make_collector(gpu_repo, base_repo, [_vm_running_with_nic()], net)

    samples = await collector.collect()
    by_metric = {s.metric: s for s in samples}
    assert by_metric["public_ip"].value_text == "1.2.3.4"


# --------------------------------------------------------------------------- #
# 公网 IP 解析:失败隔离(不产 IP sample、不影响 power_state、不抛)
# --------------------------------------------------------------------------- #


async def test_no_nic_skips_public_ip(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    net = FakeNetworkClient()
    vm = _vm_running_with_nic(nic_id=None)  # network_interfaces 为空
    collector = _make_collector(gpu_repo, base_repo, [vm], net)

    samples = await collector.collect()
    metrics = {s.metric for s in samples}
    assert metrics == {"power_state"}  # 无 public_ip
    # 没有 NIC 就不该调用 SDK get
    assert net.network_interfaces.calls == []


async def test_nic_without_public_ip_ref_skips(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    nic = {"ip_configurations": [{"public_ip_address": None}]}
    net = FakeNetworkClient(nic=nic)
    collector = _make_collector(gpu_repo, base_repo, [_vm_running_with_nic()], net)

    samples = await collector.collect()
    assert {s.metric for s in samples} == {"power_state"}
    # 没有 public IP 引用就不查 public_ip_addresses
    assert net.public_ip_addresses.calls == []


async def test_public_ip_resource_without_address_skips(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    nic = {"ip_configurations": [{"public_ip_address": {"id": _PIP_ID}}]}
    # public IP 资源存在但 ip_address 为 None(例如未分配/未关联)
    net = FakeNetworkClient(nic=nic, public_ip={"ip_address": None})
    collector = _make_collector(gpu_repo, base_repo, [_vm_running_with_nic()], net)

    samples = await collector.collect()
    assert {s.metric for s in samples} == {"power_state"}


async def test_sdk_exception_during_ip_resolution_isolated(gpu_repo, base_repo):
    """NIC get 抛异常 → 不产 IP sample,但 power_state 正常,collect 不抛。"""
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    net = FakeNetworkClient(nic_exc=RuntimeError("azure network 500"))
    collector = _make_collector(gpu_repo, base_repo, [_vm_running_with_nic()], net)

    samples = await collector.collect()
    by_metric = {s.metric: s for s in samples}
    assert "public_ip" not in by_metric
    assert by_metric["power_state"].status == "ok"
    assert by_metric["power_state"].value_text == "Running"


# --------------------------------------------------------------------------- #
# 向后兼容:network_client=None
# --------------------------------------------------------------------------- #


async def test_no_network_client_no_public_ip_sample(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    collector = _make_collector(
        gpu_repo, base_repo, [_vm_running_with_nic()], network_client=None
    )

    samples = await collector.collect()
    metrics = [s.metric for s in samples]
    assert metrics == ["power_state"]  # 仅一条,不解析 IP


async def test_power_state_error_path_yields_no_ip(gpu_repo, base_repo):
    """power_state 解析触发异常时只返回 error sample,不进入 IP 解析。"""
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")

    class _Boom:
        @property
        def statuses(self) -> Any:
            raise RuntimeError("boom")

    net = FakeNetworkClient(nic={"ip_configurations": []})
    vm = SimpleNamespace(
        name="gpu-vm-01",
        instance_view=_Boom(),
        network_profile=SimpleNamespace(network_interfaces=[SimpleNamespace(id=_NIC_ID)]),
    )
    collector = _make_collector(gpu_repo, base_repo, [vm], net)

    samples = await collector.collect()
    assert len(samples) == 1
    assert samples[0].metric == "power_state"
    assert samples[0].status == "error"
    # IP 链路未被触发
    assert net.network_interfaces.calls == []


# --------------------------------------------------------------------------- #
# 多台并存:power_state 与 public_ip 计数
# --------------------------------------------------------------------------- #


async def test_running_vm_produces_two_samples(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    nic = {"ip_configurations": [{"public_ip_address": {"id": _PIP_ID}}]}
    net = FakeNetworkClient(nic=nic, public_ip={"ip_address": "9.9.9.9"})
    collector = _make_collector(gpu_repo, base_repo, [_vm_running_with_nic()], net)

    samples = await collector.collect()
    assert len(samples) == 2
    assert {s.metric for s in samples} == {"power_state", "public_ip"}
    now = datetime.now(UTC)
    # collected_at 为带 tz 的 UTC datetime
    for s in samples:
        assert s.collected_at.tzinfo is not None
        assert s.collected_at <= now
