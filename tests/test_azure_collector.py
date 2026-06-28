"""TASK-012: AzureVmCollector 单元测试(不连真实 Azure)。

覆盖:
  - 电源态映射:running / stopped / deallocated / starting / stopping /
    deallocating / unknown(缺 PowerState)
  - fixture 驱动:running / mixed / empty / no_powerstate
  - 只采集已注册(azure_vm_name 匹配)的 VM,未注册的跳过
  - 单台 VM 解析失败隔离:产出 status='error',不污染其它台,旧值保留
  - 整体 SDK 调用失败:collect() 向上抛异常(供框架层降级)
  - azure_vm_status 专用表 upsert 正确
  - register() 工厂:凭证缺失优雅跳过,不抛异常、不注册
  - register() 工厂:凭证齐全时注册成功(SDK client 构造打桩)
  - 同时验证 SDK 风格属性对象与 dict fixture 均可解析
  - ssh_key_path / secret 不出现在任何采集产出中

mock 策略:FakeClient.virtual_machines.list_all 返回 fixture 反序列化后的对象,
不发起任何网络请求。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from panel.collectors import registry
from panel.collectors.azure import register as register_azure
from panel.collectors.azure.collector import (
    POWER_STATE_MAP,
    AzureVmCollector,
    _parse_power_state,
)
from panel.collectors.base import Collector
from panel.config.settings import Settings
from panel.db import connection, migrate
from panel.db.gpu_repository import GpuRepository
from panel.db.repository import Repository
from panel.domain.models import ServerIn

_FIXTURES = Path(__file__).parent / "fixtures" / "azure"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _load_fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _to_sdk_object(d: Any) -> Any:
    """递归把 dict fixture 转成属性访问的 SDK 风格对象(SimpleNamespace)。

    用于验证 collector 对真实 SDK 属性对象与 dict 两种形态都能解析。
    """
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _to_sdk_object(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_to_sdk_object(x) for x in d]
    return d


class _FakeVms:
    def __init__(self, vms: list[Any], raise_exc: Exception | None = None) -> None:
        self._vms = vms
        self._raise = raise_exc

    def list_all(self, expand: str | None = None) -> list[Any]:  # noqa: ARG002
        if self._raise is not None:
            raise self._raise
        return list(self._vms)


class FakeClient:
    """打桩的 ComputeManagementClient:只暴露 virtual_machines.list_all。"""

    def __init__(self, vms: list[Any], raise_exc: Exception | None = None) -> None:
        self.virtual_machines = _FakeVms(vms, raise_exc)


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


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


async def _register_server(repo: GpuRepository, name: str, azure_vm_name: str) -> int:
    return await repo.insert_server(
        ServerIn(name=name, azure_vm_name=azure_vm_name, ssh_key_path="/secrets/key")
    )


def _make_collector(
    gpu_repo: GpuRepository,
    base_repo: Repository,
    vms: list[Any],
    raise_exc: Exception | None = None,
) -> AzureVmCollector:
    return AzureVmCollector(
        client=FakeClient(vms, raise_exc),
        gpu_repo=gpu_repo,
        base_repo=base_repo,
    )


# --------------------------------------------------------------------------- #
# _parse_power_state — 映射单元测试
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "expected_display", "expected_running"),
    [
        ("PowerState/running", "Running", 1.0),
        ("PowerState/stopped", "Stopped", 0.0),
        ("PowerState/deallocated", "Deallocated", 0.0),
        ("PowerState/starting", "Starting", 0.0),
        ("PowerState/stopping", "Stopping", 0.0),
        ("PowerState/deallocating", "Deallocating", 0.0),
        ("POWERSTATE/RUNNING", "Running", 1.0),  # 大小写不敏感
    ],
)
def test_parse_power_state_mapping(code, expected_display, expected_running):
    statuses = [
        {"code": "ProvisioningState/succeeded"},
        {"code": code},
    ]
    display, raw, is_running = _parse_power_state(statuses)
    assert display == expected_display
    assert raw == code
    assert is_running == expected_running


def test_parse_power_state_unknown_code():
    display, raw, is_running = _parse_power_state([{"code": "PowerState/bogus"}])
    assert display == "Unknown"
    assert raw == "PowerState/bogus"
    assert is_running == 0.0


def test_parse_power_state_missing_powerstate():
    display, raw, is_running = _parse_power_state([{"code": "ProvisioningState/succeeded"}])
    assert display == "Unknown"
    assert raw is None
    assert is_running == 0.0


def test_parse_power_state_empty_or_none():
    for statuses in ([], None):
        display, raw, is_running = _parse_power_state(statuses)
        assert display == "Unknown"
        assert raw is None
        assert is_running == 0.0


def test_power_state_map_covers_six_states():
    assert set(POWER_STATE_MAP) == {
        "powerstate/running",
        "powerstate/stopped",
        "powerstate/deallocated",
        "powerstate/starting",
        "powerstate/stopping",
        "powerstate/deallocating",
    }


# --------------------------------------------------------------------------- #
# collect() — fixture 驱动(dict 形态)
# --------------------------------------------------------------------------- #


async def test_collect_running_fixture(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    vms = _load_fixture("list_vms_running.json")
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()

    assert len(samples) == 1
    s = samples[0]
    assert s.metric == "power_state"
    assert s.value_text == "Running"
    assert s.value_num == 1.0
    assert s.status == "ok"

    # 专用表 upsert 正确
    row = await gpu_repo.get_vm_status(s.target_id)
    assert row is not None
    assert row.power_state == "Running"
    assert row.power_state_raw == "PowerState/running"
    assert row.is_running is True


async def test_collect_mixed_fixture(gpu_repo, base_repo):
    id1 = await _register_server(gpu_repo, "vm1", "gpu-vm-01")
    id2 = await _register_server(gpu_repo, "vm2", "gpu-vm-02")
    vms = _load_fixture("list_vms_mixed.json")
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()

    # unregistered-vm 应被跳过 → 只 2 条
    assert len(samples) == 2
    by_id = {s.target_id: s for s in samples}
    assert by_id[id1].value_text == "Running"
    assert by_id[id1].value_num == 1.0
    assert by_id[id2].value_text == "Deallocated"
    assert by_id[id2].value_num == 0.0


async def test_collect_empty_fixture(gpu_repo, base_repo):
    await _register_server(gpu_repo, "vm1", "gpu-vm-01")
    vms = _load_fixture("list_vms_empty.json")
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()
    assert samples == []


async def test_collect_no_powerstate_fixture(gpu_repo, base_repo):
    sid = await _register_server(gpu_repo, "vm1", "gpu-vm-01")
    vms = _load_fixture("list_vms_no_powerstate.json")
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()
    assert len(samples) == 1
    assert samples[0].value_text == "Unknown"
    assert samples[0].status == "ok"  # 缺 PowerState 仍是成功采集,只是 Unknown

    row = await gpu_repo.get_vm_status(sid)
    assert row.power_state == "Unknown"
    assert row.power_state_raw is None


# --------------------------------------------------------------------------- #
# collect() — SDK 属性对象形态
# --------------------------------------------------------------------------- #


async def test_collect_sdk_attribute_objects(gpu_repo, base_repo):
    await _register_server(gpu_repo, "gpu-vm-01", "gpu-vm-01")
    vms = [_to_sdk_object(v) for v in _load_fixture("list_vms_running.json")]
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()
    assert len(samples) == 1
    assert samples[0].value_text == "Running"


# --------------------------------------------------------------------------- #
# collect() — 只采集已注册的 VM
# --------------------------------------------------------------------------- #


async def test_collect_skips_unregistered_vms(gpu_repo, base_repo):
    # 注册一台 azure_vm_name 不在 fixture 中的机器
    await _register_server(gpu_repo, "other", "not-in-cloud")
    vms = _load_fixture("list_vms_running.json")
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()
    assert samples == []


async def test_collect_no_registered_servers_returns_empty(gpu_repo, base_repo):
    vms = _load_fixture("list_vms_running.json")
    collector = _make_collector(gpu_repo, base_repo, vms)
    samples = await collector.collect()
    assert samples == []


async def test_collect_ignores_servers_without_azure_vm_name(gpu_repo, base_repo):
    # azure_vm_name 为 None 的注册机不参与匹配
    await gpu_repo.insert_server(ServerIn(name="ssh-only", azure_vm_name=None))
    vms = _load_fixture("list_vms_running.json")
    collector = _make_collector(gpu_repo, base_repo, vms)
    samples = await collector.collect()
    assert samples == []


# --------------------------------------------------------------------------- #
# collect() — 单台失败隔离 / 整体异常
# --------------------------------------------------------------------------- #


async def test_single_vm_parse_failure_isolated(gpu_repo, base_repo):
    """一台 VM 的 instance_view 解析触发异常,不影响另一台,且产出 error sample。"""
    id_ok = await _register_server(gpu_repo, "ok", "gpu-vm-01")
    id_bad = await _register_server(gpu_repo, "bad", "gpu-vm-02")

    # 给 bad VM 一个会让 _process_vm 内 upsert 抛错的对象:statuses 是非可迭代的
    # 这里用一个 instance_view.statuses 抛异常的对象触发 except 分支。
    class _Boom:
        @property
        def statuses(self) -> Any:
            raise RuntimeError("boom parsing")

    vms = [
        {
            "name": "gpu-vm-01",
            "instance_view": {"statuses": [{"code": "PowerState/running"}]},
        },
        SimpleNamespace(name="gpu-vm-02", instance_view=_Boom()),
    ]
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()
    by_id = {s.target_id: s for s in samples}
    assert by_id[id_ok].status == "ok"
    assert by_id[id_ok].value_text == "Running"
    assert by_id[id_bad].status == "error"
    assert by_id[id_bad].value_text == "Unknown"


async def test_failed_vm_does_not_overwrite_old_status(gpu_repo, base_repo):
    """单台失败时不 upsert,azure_vm_status 旧值保留。"""
    sid = await _register_server(gpu_repo, "vm1", "gpu-vm-01")
    # 先写入一个旧的 Running 状态
    await gpu_repo.upsert_vm_status(sid, "Running", "PowerState/running", True, datetime.now(UTC))

    class _Boom:
        @property
        def statuses(self) -> Any:
            raise RuntimeError("boom")

    vms = [SimpleNamespace(name="gpu-vm-01", instance_view=_Boom())]
    collector = _make_collector(gpu_repo, base_repo, vms)

    samples = await collector.collect()
    assert samples[0].status == "error"
    # 旧值仍为 Running(未被覆盖)
    row = await gpu_repo.get_vm_status(sid)
    assert row.power_state == "Running"
    assert row.is_running is True


async def test_whole_sdk_failure_propagates(gpu_repo, base_repo):
    """整体 list_all 抛异常 → collect() 向上传播(供 run_collector 框架降级)。"""
    await _register_server(gpu_repo, "vm1", "gpu-vm-01")
    collector = _make_collector(gpu_repo, base_repo, [], raise_exc=RuntimeError("auth failed"))

    with pytest.raises(RuntimeError, match="auth failed"):
        await collector.collect()


async def test_whole_failure_degrades_via_framework(gpu_repo, base_repo):
    """经 run_collector 包装,整体失败降级为 collector_run.status='error',不外泄。"""
    from panel.collectors.scheduler import run_collector

    await _register_server(gpu_repo, "vm1", "gpu-vm-01")
    collector = _make_collector(gpu_repo, base_repo, [], raise_exc=RuntimeError("network down"))

    result = await run_collector(collector, base_repo)
    assert result.status == "error"
    assert result.name == "azure_vm"


# --------------------------------------------------------------------------- #
# Collector 协议 / 默认属性
# --------------------------------------------------------------------------- #


def test_collector_implements_protocol(gpu_repo, base_repo):
    collector = _make_collector(gpu_repo, base_repo, [])
    assert isinstance(collector, Collector)
    assert collector.name == "azure_vm"
    assert collector.interval_seconds == 300
    assert collector.timeout_seconds == 60


# --------------------------------------------------------------------------- #
# register() 工厂
# --------------------------------------------------------------------------- #


def test_register_skips_when_unconfigured(gpu_repo, base_repo, caplog):
    settings = Settings()  # 所有 Azure 字段空
    with caplog.at_level(logging.WARNING):
        register_azure(settings, base_repo, gpu_repo)
    assert registry.iter_collectors() == []
    assert any("disabled" in r.message.lower() for r in caplog.records)


def test_register_skips_when_partial_config(gpu_repo, base_repo):
    settings = Settings(
        azure_tenant_id="t",
        azure_client_id="c",
        # secret_file / subscription 缺失
    )
    register_azure(settings, base_repo, gpu_repo)
    assert registry.iter_collectors() == []


def test_register_skips_when_secret_file_missing(gpu_repo, base_repo, tmp_path):
    settings = Settings(
        azure_tenant_id="t",
        azure_client_id="c",
        azure_client_secret_file=str(tmp_path / "nonexistent_secret"),
        azure_subscription_id="sub",
    )
    register_azure(settings, base_repo, gpu_repo)
    # 文件不存在 → 跳过,不抛异常
    assert registry.iter_collectors() == []


def test_register_succeeds_when_configured(gpu_repo, base_repo, tmp_path, monkeypatch):
    secret_file = tmp_path / "azure_secret"
    secret_file.write_text("super-secret-value\n", encoding="utf-8")
    settings = Settings(
        azure_tenant_id="tenant",
        azure_client_id="client",
        azure_client_secret_file=str(secret_file),
        azure_subscription_id="subscription",
    )

    # 打桩 SDK 类,避免真实网络/凭证依赖。
    import azure.identity
    import azure.mgmt.compute

    monkeypatch.setattr(
        azure.identity, "ClientSecretCredential", lambda **kw: SimpleNamespace(**kw)
    )
    monkeypatch.setattr(
        azure.mgmt.compute,
        "ComputeManagementClient",
        lambda cred, sub: SimpleNamespace(credential=cred, subscription=sub),
    )

    register_azure(settings, base_repo, gpu_repo)

    collectors = registry.iter_collectors()
    assert len(collectors) == 1
    assert collectors[0].name == "azure_vm"


def test_register_does_not_log_secret(gpu_repo, base_repo, tmp_path, monkeypatch, caplog):
    """注册过程不得在日志中泄露 secret 内容。"""
    secret_value = "TOPSECRET-deadbeefcafebabe"  # noqa: S105 — 测试构造的假 secret
    secret_file = tmp_path / "azure_secret"
    secret_file.write_text(secret_value, encoding="utf-8")
    settings = Settings(
        azure_tenant_id="tenant",
        azure_client_id="client",
        azure_client_secret_file=str(secret_file),
        azure_subscription_id="subscription",
    )

    import azure.identity
    import azure.mgmt.compute

    monkeypatch.setattr(
        azure.identity, "ClientSecretCredential", lambda **kw: SimpleNamespace(**kw)
    )
    monkeypatch.setattr(
        azure.mgmt.compute,
        "ComputeManagementClient",
        lambda cred, sub: SimpleNamespace(),
    )

    with caplog.at_level(logging.DEBUG):
        register_azure(settings, base_repo, gpu_repo)

    for record in caplog.records:
        assert secret_value not in record.getMessage()
