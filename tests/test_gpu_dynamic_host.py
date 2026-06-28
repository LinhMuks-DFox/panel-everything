"""TASK-018: GpuCollector Azure 动态主机解析单元测试(不连真实 SSH)。

覆盖 _collect_one 在 SSH 前的 Azure VM 判定逻辑:
  - VM 非 running(power_state 快照 value_num != 1.0)→ 跳过 SSH
    (ssh_runner.run 未被调用),产出 unreachable + 'vm_not_running' 标注。
  - VM running(value_num == 1.0)且有 public_ip 快照 → 用解析出的公网 IP 作
    连接 host(断言 run 收到的 host == 该 IP)。
  - VM running 但无 public_ip 快照 → host=None(回退静态 ssh_host),仍采集。
  - 无 power_state 快照(Azure 未配置)→ host=None,正常走静态 ssh_host。
  - azure_vm_name 为空 → 完全不走动态逻辑,host=None。

mock 策略:RecordingSshRunner 记录每次调用的 (server_id, host),返回预设
SshResult。Azure 快照经 base_repo.upsert_snapshot 直接种入 latest_snapshot。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from panel.collectors.base import MetricSample
from panel.collectors.gpu.collector import GpuCollector, SshResult
from panel.db import connection, migrate
from panel.db.gpu_repository import GpuRepository
from panel.db.repository import Repository
from panel.domain.models import ServerIn

if TYPE_CHECKING:
    from panel.db.gpu_repository import ServerRow

_SMI_SINGLE = (
    "0, NVIDIA A100-SXM4-80GB, 87, 65536, 81920, 72, 380\n"
)


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


class RecordingSshRunner:
    """记录每次 run 调用的 (server_id, host);返回预设 SshResult。

    实现新签名 run(server, command, timeout_seconds, host=None)。
    """

    def __init__(self, result: SshResult) -> None:
        self._result = result
        self.calls: list[tuple[int, str | None]] = []

    async def run(
        self,
        server: ServerRow,
        command: str,  # noqa: ARG002
        timeout_seconds: float,  # noqa: ARG002
        host: str | None = None,
    ) -> SshResult:
        self.calls.append((server.id, host))
        return self._result


async def _add_azure_gpu_server(repo: GpuRepository, name: str, vm_name: str) -> int:
    return await repo.insert_server(
        ServerIn(
            name=name,
            azure_vm_name=vm_name,
            ssh_host="10.0.0.9",  # 静态地址(running 时应被动态 IP 取代)
            ssh_user="azureuser",
            ssh_key_path="/run/secrets/ssh_key",
            has_gpu=True,
        )
    )


async def _add_plain_gpu_server(repo: GpuRepository, name: str) -> int:
    return await repo.insert_server(
        ServerIn(
            name=name,
            azure_vm_name=None,
            ssh_host="100.64.0.1",
            ssh_user="azureuser",
            ssh_key_path="/run/secrets/ssh_key",
            has_gpu=True,
        )
    )


async def _seed_power_state(
    base_repo: Repository, server_id: int, value_num: float, value_text: str
) -> None:
    await base_repo.upsert_snapshot(
        "azure_vm",
        [
            MetricSample(
                target_id=server_id,
                metric="power_state",
                value_num=value_num,
                value_text=value_text,
                status="ok",
                collected_at=datetime.now(UTC),
            )
        ],
    )


async def _seed_public_ip(base_repo: Repository, server_id: int, ip: str) -> None:
    await base_repo.upsert_snapshot(
        "azure_vm",
        [
            MetricSample(
                target_id=server_id,
                metric="public_ip",
                value_num=None,
                value_text=ip,
                status="ok",
                collected_at=datetime.now(UTC),
            )
        ],
    )


# --------------------------------------------------------------------------- #
# VM 非 running → 跳采
# --------------------------------------------------------------------------- #


async def test_vm_not_running_skips_ssh(gpu_repo, base_repo):
    sid = await _add_azure_gpu_server(gpu_repo, "a100", "a100-vm")
    # power_state 快照:Deallocated(value_num=0.0)
    await _seed_power_state(base_repo, sid, 0.0, "Deallocated")
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    samples = await collector.collect()

    # 未发起任何 SSH
    assert runner.calls == []
    # 汇总 sample:unreachable + vm_not_running 标注
    assert len(samples) == 1
    assert samples[0].status == "unreachable"
    assert samples[0].value_text == "vm_not_running"
    assert samples[0].value_num == 0.0

    # 落库的 GpuSample 也带 vm_not_running 标注(存于 gpu_name 占位字段)
    rows = await gpu_repo.get_latest_gpu_metrics(sid)
    assert len(rows) == 1
    assert rows[0].status == "unreachable"
    assert rows[0].gpu_name == "vm_not_running"


async def test_vm_stopped_skips_ssh(gpu_repo, base_repo):
    sid = await _add_azure_gpu_server(gpu_repo, "a100", "a100-vm")
    await _seed_power_state(base_repo, sid, 0.0, "Stopped")
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    samples = await collector.collect()
    assert runner.calls == []
    assert samples[0].status == "unreachable"


# --------------------------------------------------------------------------- #
# VM running → 用动态 IP 作 host
# --------------------------------------------------------------------------- #


async def test_vm_running_uses_dynamic_ip_as_host(gpu_repo, base_repo):
    sid = await _add_azure_gpu_server(gpu_repo, "a100", "a100-vm")
    await _seed_power_state(base_repo, sid, 1.0, "Running")
    await _seed_public_ip(base_repo, sid, "52.140.1.2")
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    samples = await collector.collect()

    # SSH 用动态 IP 作 host(而非静态 ssh_host 10.0.0.9)
    assert runner.calls == [(sid, "52.140.1.2")]
    assert samples[0].status == "ok"
    assert samples[0].value_text == "1/1 gpus ok"


async def test_vm_running_without_public_ip_falls_back_to_static(gpu_repo, base_repo):
    sid = await _add_azure_gpu_server(gpu_repo, "a100", "a100-vm")
    await _seed_power_state(base_repo, sid, 1.0, "Running")
    # 不种 public_ip 快照
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    samples = await collector.collect()

    # host=None → 回退静态 ssh_host(由 runner 表现为 host is None)
    assert runner.calls == [(sid, None)]
    assert samples[0].status == "ok"


async def test_vm_running_with_empty_public_ip_text_falls_back(gpu_repo, base_repo):
    """public_ip 快照存在但 value_text 为空 → 回退静态 host。"""
    sid = await _add_azure_gpu_server(gpu_repo, "a100", "a100-vm")
    await _seed_power_state(base_repo, sid, 1.0, "Running")
    await _seed_public_ip(base_repo, sid, "")  # 空串
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    await collector.collect()
    assert runner.calls == [(sid, None)]


# --------------------------------------------------------------------------- #
# 无 power_state 快照(Azure 未配置)→ 静态 host,不跳采
# --------------------------------------------------------------------------- #


async def test_no_power_state_snapshot_uses_static_host(gpu_repo, base_repo):
    sid = await _add_azure_gpu_server(gpu_repo, "a100", "a100-vm")
    # 不种任何 azure_vm 快照(Azure collector 未跑/未配置)
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    samples = await collector.collect()

    # 有 azure_vm_name 但无快照 → host=None(静态),不跳采
    assert runner.calls == [(sid, None)]
    assert samples[0].status == "ok"


async def test_no_power_state_snapshot_does_not_query_public_ip(gpu_repo, base_repo):
    """无 power_state 快照时,即便误种了 public_ip,也不应使用(因为整段动态逻辑
    只在 power_state 快照存在时才进入)。"""
    sid = await _add_azure_gpu_server(gpu_repo, "a100", "a100-vm")
    await _seed_public_ip(base_repo, sid, "203.0.113.7")  # 仅 public_ip,无 power_state
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    await collector.collect()
    # 无 power_state → host 必须 None(不取 public_ip)
    assert runner.calls == [(sid, None)]


# --------------------------------------------------------------------------- #
# azure_vm_name 为空 → 不走动态逻辑
# --------------------------------------------------------------------------- #


async def test_plain_server_skips_dynamic_logic(gpu_repo, base_repo):
    sid = await _add_plain_gpu_server(gpu_repo, "ws01")
    # 即便误种了 azure 快照,azure_vm_name 为空也不应触发动态逻辑
    await _seed_power_state(base_repo, sid, 0.0, "Deallocated")
    await _seed_public_ip(base_repo, sid, "198.51.100.5")
    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    samples = await collector.collect()

    # 不跳采、host=None(静态 ssh_host)
    assert runner.calls == [(sid, None)]
    assert samples[0].status == "ok"


# --------------------------------------------------------------------------- #
# 混合:running(动态 IP)与 not-running(跳采)并存,互不影响
# --------------------------------------------------------------------------- #


async def test_mixed_running_and_stopped(gpu_repo, base_repo):
    run_id = await _add_azure_gpu_server(gpu_repo, "a100-run", "vm-run")
    stop_id = await _add_azure_gpu_server(gpu_repo, "a100-stop", "vm-stop")
    await _seed_power_state(base_repo, run_id, 1.0, "Running")
    await _seed_public_ip(base_repo, run_id, "40.40.40.40")
    await _seed_power_state(base_repo, stop_id, 0.0, "Deallocated")

    runner = RecordingSshRunner(SshResult(_SMI_SINGLE, 0))
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)

    samples = await collector.collect()
    by_id = {s.target_id: s for s in samples}

    # 只对 running 机发起 SSH,host=动态 IP
    assert runner.calls == [(run_id, "40.40.40.40")]
    assert by_id[run_id].status == "ok"
    assert by_id[stop_id].status == "unreachable"
    assert by_id[stop_id].value_text == "vm_not_running"
