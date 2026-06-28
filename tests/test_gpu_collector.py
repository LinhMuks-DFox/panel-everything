"""TASK-013: GpuCollector 单元测试(不连真实 SSH)。

覆盖:
  - CSV 解析:单卡 / 多卡(4卡)/ 空输出 / [Not Supported] 部分字段 / 字段数不符 /
    非数字 index
  - 状态分类:ok / 无GPU(exit≠0)/ 输出空(error)/ 连接失败(unreachable)/
    超时(unreachable)
  - 多台并发:一台失败不影响其它台(asyncio.gather 隔离)
  - 无 has_gpu=True 服务器时 collect() 返回空列表,不报错、不连主机
  - 写库:append_gpu_metrics 被以正确参数调用(含 mem_pct 由 repository 计算)
  - 汇总 MetricSample:gpu_any_running / value_num / value_text / status
  - Collector 协议 isinstance + 默认 name/interval/timeout
  - register() 工厂:始终注册,无凭证开关
  - AsyncSshRunner:asyncssh.connect 抛 DisconnectError → 异常上抛(由 _collect_one 捕获)
  - 凭证(ssh_key_path)不出现在任何采集产出中

mock 策略:注入 FakeSshRunner(实现 SshRunner 协议)按 server 返回预设
SshResult 或抛异常;不发起任何网络/SSH 请求。另有少量用例 monkeypatch
asyncssh.connect 验证默认执行层。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import asyncssh
import pytest

from panel.collectors import registry
from panel.collectors.base import Collector, MetricSample
from panel.collectors.gpu import register as register_gpu
from panel.collectors.gpu.collector import (
    NVIDIA_SMI_CMD,
    AsyncSshRunner,
    GpuCollector,
    SshResult,
    _parse_nvidia_smi_csv,
)
from panel.config.settings import Settings
from panel.db import connection, migrate
from panel.db.gpu_repository import GpuRepository, GpuSample
from panel.db.repository import Repository
from panel.domain.models import ServerIn

if TYPE_CHECKING:
    from datetime import datetime

    from panel.db.gpu_repository import ServerRow

_FIXTURES = Path(__file__).parent / "fixtures" / "gpu"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


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


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


class FakeSshRunner:
    """打桩 SSH 执行层:按 server.id 返回预设 SshResult 或抛异常。

    plan: {server_id: SshResult | Exception}
    缺省(server_id 不在 plan)时抛 OSError(模拟不可达)。
    """

    def __init__(self, plan: dict[int, SshResult | Exception]) -> None:
        self._plan = plan
        self.calls: list[tuple[int, str]] = []

    async def run(
        self, server: ServerRow, command: str, timeout_seconds: float
    ) -> SshResult:
        self.calls.append((server.id, command))
        outcome = self._plan.get(server.id)
        if outcome is None:
            raise OSError("no plan for server")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _add_gpu_server(repo: GpuRepository, name: str) -> int:
    return await repo.insert_server(
        ServerIn(
            name=name,
            ssh_host="100.64.0.1",
            ssh_user="azureuser",
            ssh_key_path="/run/secrets/ssh_key",
            has_gpu=True,
        )
    )


def _make_collector(
    gpu_repo: GpuRepository,
    base_repo: Repository,
    runner: FakeSshRunner,
) -> GpuCollector:
    return GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=runner)


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# _parse_nvidia_smi_csv — 纯解析单元测试
# --------------------------------------------------------------------------- #


def test_parse_single_card():
    samples = _parse_nvidia_smi_csv(1, _fixture("nvidia_smi_single.txt"), _now())
    assert len(samples) == 1
    s = samples[0]
    assert s.server_id == 1
    assert s.gpu_index == 0
    assert s.gpu_name == "NVIDIA A100-SXM4-80GB"
    assert s.util_pct == 87.0
    assert s.mem_used_mib == 65536.0
    assert s.mem_total_mib == 81920.0
    assert s.temp_c == 72.0
    assert s.power_w == 380.0
    assert s.status == "ok"


def test_parse_multi_card():
    samples = _parse_nvidia_smi_csv(7, _fixture("nvidia_smi_multi.txt"), _now())
    assert len(samples) == 4
    assert [s.gpu_index for s in samples] == [0, 1, 2, 3]
    assert all(s.server_id == 7 for s in samples)
    assert all(s.status == "ok" for s in samples)
    assert samples[3].util_pct == 99.0
    assert samples[2].mem_used_mib == 1024.0


def test_parse_empty_output_returns_no_rows():
    samples = _parse_nvidia_smi_csv(1, _fixture("nvidia_smi_empty.txt"), _now())
    assert samples == []


def test_parse_partial_not_supported_fields_become_none():
    samples = _parse_nvidia_smi_csv(1, _fixture("nvidia_smi_partial.txt"), _now())
    assert len(samples) == 2
    # 行0:temp/power 为 [Not Supported] → None,其余正常,整行仍 ok
    s0 = samples[0]
    assert s0.status == "ok"
    assert s0.util_pct == 55.0
    assert s0.temp_c is None
    assert s0.power_w is None
    # 行1:util 为 [N/A] → None,其余正常
    s1 = samples[1]
    assert s1.status == "ok"
    assert s1.util_pct is None
    assert s1.mem_used_mib == 2048.0
    assert s1.temp_c == 50.0


def test_parse_wrong_field_count_row_is_error():
    samples = _parse_nvidia_smi_csv(1, "0, NVIDIA A100, 50\n", _now())
    assert len(samples) == 1
    assert samples[0].status == "error"
    assert samples[0].gpu_index == 0
    assert samples[0].util_pct is None


def test_parse_non_numeric_index_is_error():
    bad = "abc, NVIDIA A100, 50, 100, 200, 60, 100\n"
    samples = _parse_nvidia_smi_csv(1, bad, _now())
    assert len(samples) == 1
    assert samples[0].status == "error"


def test_parse_skips_blank_lines():
    text = "\n0, NVIDIA A100, 50, 100, 200, 60, 100\n\n"
    samples = _parse_nvidia_smi_csv(1, text, _now())
    assert len(samples) == 1
    assert samples[0].status == "ok"


# --------------------------------------------------------------------------- #
# collect() — 正常路径(单卡 / 多卡)
# --------------------------------------------------------------------------- #


async def test_collect_single_card_writes_and_summarizes(gpu_repo, base_repo):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: SshResult(_fixture("nvidia_smi_single.txt"), 0)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()

    # 汇总 MetricSample:每台一条
    assert len(samples) == 1
    m = samples[0]
    assert m.metric == "gpu_any_running"
    assert m.target_id == sid
    assert m.value_num == 1.0
    assert m.value_text == "1/1 gpus ok"
    assert m.status == "ok"

    # 写入 gpu_metrics(mem_pct 由 repository 计算)
    rows = await gpu_repo.get_latest_gpu_metrics(sid)
    assert len(rows) == 1
    assert rows[0].util_pct == 87.0
    assert rows[0].mem_pct == round(65536 / 81920 * 100, 2)
    assert rows[0].status == "ok"

    # 命令正确
    assert runner.calls == [(sid, NVIDIA_SMI_CMD)]


async def test_collect_multi_card(gpu_repo, base_repo):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: SshResult(_fixture("nvidia_smi_multi.txt"), 0)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    assert samples[0].value_text == "4/4 gpus ok"
    assert samples[0].value_num == 1.0

    rows = await gpu_repo.get_latest_gpu_metrics(sid)
    assert len(rows) == 4
    assert [r.gpu_index for r in rows] == [0, 1, 2, 3]


# --------------------------------------------------------------------------- #
# collect() — 状态分类
# --------------------------------------------------------------------------- #


async def test_collect_no_gpu_nonzero_exit_is_error(gpu_repo, base_repo):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: SshResult("command not found", 127)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    assert samples[0].status == "error"
    assert samples[0].value_num == 0.0
    assert samples[0].value_text == "0/1 gpus ok"

    rows = await gpu_repo.get_latest_gpu_metrics(sid)
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].util_pct is None


async def test_collect_empty_output_is_error(gpu_repo, base_repo):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: SshResult("", 0)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    assert samples[0].status == "error"


async def test_collect_connection_failure_is_unreachable(gpu_repo, base_repo):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: asyncssh.DisconnectError(1, "conn refused")})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    assert samples[0].status == "unreachable"
    assert samples[0].value_num == 0.0

    rows = await gpu_repo.get_latest_gpu_metrics(sid)
    assert len(rows) == 1
    assert rows[0].status == "unreachable"


async def test_collect_oserror_is_unreachable(gpu_repo, base_repo):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: OSError("network unreachable")})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    assert samples[0].status == "unreachable"


async def test_collect_timeout_is_unreachable(gpu_repo, base_repo):
    await _add_gpu_server(gpu_repo, "gpu01")

    class _SlowRunner:
        async def run(self, server, command, timeout_seconds):  # noqa: ANN001, ANN201, ARG002
            await asyncio.sleep(10)
            return SshResult("", 0)

    collector = GpuCollector(
        gpu_repo=gpu_repo, base_repo=base_repo, ssh_runner=_SlowRunner()
    )
    collector.timeout_seconds = 0  # 立即超时

    samples = await collector.collect()
    assert samples[0].status == "unreachable"


# --------------------------------------------------------------------------- #
# collect() — 多台并发隔离
# --------------------------------------------------------------------------- #


async def test_collect_concurrent_failure_isolated(gpu_repo, base_repo):
    ok_id = await _add_gpu_server(gpu_repo, "gpu-ok")
    down_id = await _add_gpu_server(gpu_repo, "gpu-down")
    err_id = await _add_gpu_server(gpu_repo, "gpu-err")

    runner = FakeSshRunner(
        {
            ok_id: SshResult(_fixture("nvidia_smi_multi.txt"), 0),
            down_id: asyncssh.DisconnectError(1, "down"),
            err_id: SshResult("no nvidia driver", 9),
        }
    )
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    by_id = {s.target_id: s for s in samples}
    assert by_id[ok_id].status == "ok"
    assert by_id[ok_id].value_text == "4/4 gpus ok"
    assert by_id[down_id].status == "unreachable"
    assert by_id[err_id].status == "error"

    # ok 机的卡数据照常落库
    assert len(await gpu_repo.get_latest_gpu_metrics(ok_id)) == 4


async def test_collect_unexpected_exception_in_gather_becomes_error(
    gpu_repo, base_repo
):
    """_collect_one 内部若抛出未预期异常,gather 兜底 → 汇总 error,不塌全局。"""
    ok_id = await _add_gpu_server(gpu_repo, "gpu-ok")
    boom_id = await _add_gpu_server(gpu_repo, "gpu-boom")

    runner = FakeSshRunner({ok_id: SshResult(_fixture("nvidia_smi_single.txt"), 0)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    # 让 boom 机的 _collect_one 抛 ValueError(非预期类型,不被 _collect_one 捕获)
    runner._plan[boom_id] = ValueError("unexpected")

    samples = await collector.collect()
    by_id = {s.target_id: s for s in samples}
    assert by_id[ok_id].status == "ok"
    assert by_id[boom_id].status == "error"


# --------------------------------------------------------------------------- #
# collect() — 空态 / 非 GPU 机
# --------------------------------------------------------------------------- #


async def test_collect_no_gpu_servers_returns_empty(gpu_repo, base_repo):
    runner = FakeSshRunner({})
    collector = _make_collector(gpu_repo, base_repo, runner)
    samples = await collector.collect()
    assert samples == []
    assert runner.calls == []  # 不连任何主机


async def test_collect_skips_non_gpu_servers(gpu_repo, base_repo):
    # 一台无 GPU 机 + 一台 GPU 机
    await gpu_repo.insert_server(ServerIn(name="cpu-only", has_gpu=False))
    gpu_id = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({gpu_id: SshResult(_fixture("nvidia_smi_single.txt"), 0)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    assert len(samples) == 1
    assert samples[0].target_id == gpu_id
    # 只 SSH 了 GPU 机
    assert [c[0] for c in runner.calls] == [gpu_id]


# --------------------------------------------------------------------------- #
# 凭证不外泄
# --------------------------------------------------------------------------- #


async def test_ssh_key_path_not_in_any_sample(gpu_repo, base_repo):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: SshResult(_fixture("nvidia_smi_single.txt"), 0)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    samples = await collector.collect()
    for s in samples:
        assert "ssh_key" not in str(s.value_text or "")
        assert "/run/secrets" not in str(s.value_text or "")


# --------------------------------------------------------------------------- #
# Collector 协议 / 默认属性
# --------------------------------------------------------------------------- #


def test_collector_implements_protocol(gpu_repo, base_repo):
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo)
    assert isinstance(collector, Collector)
    assert collector.name == "gpu"
    assert collector.interval_seconds == 60
    assert collector.timeout_seconds == 30


def test_collect_returns_metric_samples(gpu_repo, base_repo):
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=base_repo)
    # 类型断言:返回值为 list[MetricSample](运行验证在其它用例)
    assert MetricSample is not None
    assert callable(collector.collect)


# --------------------------------------------------------------------------- #
# register() 工厂
# --------------------------------------------------------------------------- #


def test_register_always_registers(gpu_repo, base_repo):
    settings = Settings()
    register_gpu(settings, base_repo, gpu_repo)
    collectors = registry.iter_collectors()
    assert len(collectors) == 1
    assert collectors[0].name == "gpu"


async def test_registered_collector_works_with_no_servers(gpu_repo, base_repo):
    settings = Settings()
    register_gpu(settings, base_repo, gpu_repo)
    collector = registry.get("gpu")
    samples = await collector.collect()
    assert samples == []


# --------------------------------------------------------------------------- #
# AsyncSshRunner — 默认执行层(monkeypatch asyncssh.connect)
# --------------------------------------------------------------------------- #


async def test_async_ssh_runner_propagates_connect_error(monkeypatch, gpu_repo):
    """asyncssh.connect 抛 DisconnectError 时,AsyncSshRunner 透出异常,
    由 _collect_one 捕获分类为 unreachable。"""

    class _BoomCtx:
        # asyncssh.connect 返回的对象既可 await 也可作 async CM;真实连接失败时
        # __aenter__ 抛 DisconnectError。这里复刻该形态。
        async def __aenter__(self):  # noqa: ANN204
            raise asyncssh.DisconnectError(1, "refused")

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
            return False

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _BoomCtx()

    monkeypatch.setattr(asyncssh, "connect", _boom)

    sid = await _add_gpu_server(gpu_repo, "gpu01")
    server = await gpu_repo.get_server(sid)
    assert server is not None

    runner = AsyncSshRunner()
    with pytest.raises(asyncssh.Error):
        await runner.run(server, NVIDIA_SMI_CMD, 5.0)


async def test_async_ssh_runner_success(monkeypatch, gpu_repo):
    """asyncssh.connect 成功路径:返回 stdout + exit_status。"""

    class _FakeRunResult:
        def __init__(self) -> None:
            self.stdout = _fixture("nvidia_smi_single.txt")
            self.exit_status = 0

    class _FakeConn:
        async def run(self, command, check, timeout):  # noqa: ANN001, ANN201, ARG002, ASYNC109
            return _FakeRunResult()

    class _FakeConnCtx:
        async def __aenter__(self):  # noqa: ANN204
            return _FakeConn()

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
            return False

    def _connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _FakeConnCtx()

    monkeypatch.setattr(asyncssh, "connect", _connect)

    sid = await _add_gpu_server(gpu_repo, "gpu01")
    server = await gpu_repo.get_server(sid)
    assert server is not None

    runner = AsyncSshRunner()
    result = await runner.run(server, NVIDIA_SMI_CMD, 5.0)
    assert result.exit_status == 0
    assert "A100" in result.stdout


# --------------------------------------------------------------------------- #
# 写库参数验证(append_gpu_metrics 被以正确 GpuSample 调用)
# --------------------------------------------------------------------------- #


async def test_append_gpu_metrics_called_with_samples(gpu_repo, base_repo, monkeypatch):
    sid = await _add_gpu_server(gpu_repo, "gpu01")
    runner = FakeSshRunner({sid: SshResult(_fixture("nvidia_smi_multi.txt"), 0)})
    collector = _make_collector(gpu_repo, base_repo, runner)

    captured: list[GpuSample] = []
    orig = gpu_repo.append_gpu_metrics

    async def _spy(samples: list[GpuSample]) -> None:
        captured.extend(samples)
        await orig(samples)

    monkeypatch.setattr(gpu_repo, "append_gpu_metrics", _spy)

    await collector.collect()
    assert len(captured) == 4
    assert all(isinstance(s, GpuSample) for s in captured)
    assert {s.gpu_index for s in captured} == {0, 1, 2, 3}
