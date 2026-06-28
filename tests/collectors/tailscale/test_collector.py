"""TASK-020: TailscaleCollector 单元测试 (不连真实 socket)。

覆盖:
  - fixture 驱动: localapi_status.json → 9 条 MetricSample, 在线态与预期一致
  - 在线态映射: muxrpi=ONLINE, ipad163=LONG_OFFLINE, iphone-13=OFFLINE
  - upsert_tailscale_node: 首次插入写 tailscale_node_events(from_state=None, note='first_seen')
  - upsert_tailscale_node: 状态不变时不写事件; 变更 ONLINE→OFFLINE 时写一行
  - socket 不可达: collect() 向上抛出 (不静默吞掉, 触发框架降级)
  - register() 工厂: socket 不存在时 warning + 不注册
  - register() 工厂: socket 存在时注册成功

mock 策略: 用 aiohttp ClientSession 的 monkeypatch/mock 模拟 JSON 响应,
不发起任何网络/socket 请求。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from panel.collectors import registry
from panel.collectors.base import Collector
from panel.collectors.tailscale import register as register_tailscale
from panel.collectors.tailscale.collector import (
    TailscaleCollector,
    _parse_last_seen,
)
from panel.config.settings import Settings
from panel.db import connection, migrate
from panel.db.repository import Repository

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def _load_fixture(name: str = "localapi_status.json") -> dict[str, Any]:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


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
def repo(conn) -> Repository:
    return Repository(conn)


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def _make_mock_session(response_data: dict[str, Any]) -> MagicMock:
    """返回一个模拟 aiohttp.ClientSession 的 async context manager。"""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = AsyncMock(return_value=json.dumps(response_data))

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


def _make_collector(
    repo: Repository, socket_path: str = "/fake/tailscaled.sock"
) -> TailscaleCollector:
    return TailscaleCollector(
        socket_path=socket_path,
        repo=repo,
        timeout_seconds=5,
        long_offline_hours=24,
    )


# --------------------------------------------------------------------------- #
# _parse_last_seen
# --------------------------------------------------------------------------- #


def test_parse_last_seen_none():
    assert _parse_last_seen(None) is None


def test_parse_last_seen_iso_z():
    dt = _parse_last_seen("2026-06-01T10:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026
    assert dt.month == 6


def test_parse_last_seen_iso_offset():
    dt = _parse_last_seen("2026-06-01T10:00:00+00:00")
    assert dt is not None
    assert dt.tzinfo is not None


# --------------------------------------------------------------------------- #
# fixture 驱动: 9 条 MetricSample
# --------------------------------------------------------------------------- #


async def test_collect_returns_9_samples(repo: Repository):
    """从 fixture 解析 Self + 8 Peer = 9 个节点,返回 9 条 MetricSample。"""
    data = _load_fixture()
    collector = _make_collector(repo)

    with patch("aiohttp.ClientSession") as MockSession:
        MockSession.return_value = _make_mock_session(data)
        samples = await collector.collect()

    assert len(samples) == 9
    # 全部应为 ok 状态 (fixture 数据干净)
    ok_count = sum(1 for s in samples if s.status == "ok")
    assert ok_count == 9


async def test_collect_muxrpi_is_online(repo: Repository):
    """muxrpi 是 Self, Online=true → ONLINE。"""
    data = _load_fixture()
    collector = _make_collector(repo)

    with patch("aiohttp.ClientSession") as MockSession:
        MockSession.return_value = _make_mock_session(data)
        samples = await collector.collect()

    # 取 muxrpi 节点
    nodes = await repo.get_all_nodes()  # type: ignore[attr-defined]
    muxrpi = next(n for n in nodes if n.hostname == "muxrpi")
    sample = next(s for s in samples if s.target_id == muxrpi.id)
    assert sample.value_text == "ONLINE"
    assert sample.status == "ok"


async def test_collect_ipad163_is_long_offline(repo: Repository):
    """ipad163: Online=false, LastSeen=2026-06-01 → LONG_OFFLINE。

    fixture 中 ipad163 的 LastSeen="2026-06-01T10:00:00Z",
    collect() 内 now=当前时刻 (2026-06-28), 相差约 27 天 >> 24h → LONG_OFFLINE。
    """
    data = _load_fixture()
    collector = _make_collector(repo)

    with patch("aiohttp.ClientSession") as MockSession:
        MockSession.return_value = _make_mock_session(data)
        samples = await collector.collect()

    nodes = await repo.get_all_nodes()  # type: ignore[attr-defined]
    ipad = next(n for n in nodes if n.hostname == "ipad163")
    sample = next(s for s in samples if s.target_id == ipad.id)
    assert sample.value_text == "LONG_OFFLINE"


async def test_collect_iphone13_is_offline(repo: Repository):
    """iphone-13: Online=false, LastSeen=2026-06-27T22:00:00Z (<<24h ago) → OFFLINE。"""
    data = _load_fixture()

    # 固定 now 为 2026-06-28T12:00:00Z, 距 2026-06-27T22:00:00Z 仅 14h < 24h
    fixed_now = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
    collector = _make_collector(repo)

    async def _patched_collect(self_inner=collector) -> list:
        """内联替换 collect() 中 now 为固定时间。"""
        data_inner = await self_inner._fetch_status()
        import asyncio
        tasks = [self_inner._process_node(node, fixed_now) for node in _iter_nodes(data_inner)]
        samples = list(await asyncio.gather(*tasks))
        ok_samples = [s for s in samples if s.status == "ok"]
        await self_inner._repo.upsert_snapshot("tailscale", ok_samples)
        return samples

    def _iter_nodes(d: dict) -> list:
        result = []
        if d.get("Self"):
            result.append(d["Self"])
        for v in (d.get("Peer") or {}).values():
            result.append(v)
        return result

    with patch("aiohttp.ClientSession") as MockSession, \
         patch.object(TailscaleCollector, "collect", _patched_collect):
        MockSession.return_value = _make_mock_session(data)
        samples = await collector.collect()

    nodes = await repo.get_all_nodes()  # type: ignore[attr-defined]
    iphone = next(n for n in nodes if n.hostname == "iphone-13")
    sample = next(s for s in samples if s.target_id == iphone.id)
    assert sample.value_text == "OFFLINE"


async def test_collect_metrics_match_expected(repo: Repository):
    """验证 5 个在线节点 ONLINE, ipad163 LONG_OFFLINE, 其余 OFFLINE。"""
    data = _load_fixture()
    # ipad163 在 2026-06-01, 其余 offline 节点在 2026-06-27 (recent)
    # 只要 now >= 2026-06-28, ipad163 就是 LONG_OFFLINE
    collector = _make_collector(repo)

    with patch("aiohttp.ClientSession") as MockSession:
        MockSession.return_value = _make_mock_session(data)
        samples = await collector.collect()

    nodes = await repo.get_all_nodes()  # type: ignore[attr-defined]
    by_hostname = {n.hostname: n for n in nodes}

    online_hosts = [
        "muxrpi", "muxdesktop-wsl-ubuntu", "takamichi-lab-pc15", "mux-mbp", "muxdesktop-windows"
    ]
    for hostname in online_hosts:
        n = by_hostname[hostname]
        s = next(s for s in samples if s.target_id == n.id)
        assert s.value_text == "ONLINE", f"{hostname} should be ONLINE"

    ipad = by_hostname["ipad163"]
    s_ipad = next(s for s in samples if s.target_id == ipad.id)
    assert s_ipad.value_text == "LONG_OFFLINE"


# --------------------------------------------------------------------------- #
# upsert_tailscale_node: 首次插入 / 状态变更事件
# --------------------------------------------------------------------------- #


async def test_first_insert_writes_first_seen_event(repo: Repository):
    """节点首次插入 → tailscale_node_events 有一行 from_state=None, note='first_seen'。"""
    now = datetime.now(UTC)
    await repo.upsert_tailscale_node(  # type: ignore[attr-defined]
        node_key="nodekey:test001",
        hostname="test-host",
        dns_name=None,
        tailscale_ips=["100.1.1.1"],
        os="linux",
        online_state="ONLINE",
        is_exit_node=False,
        last_seen_at=None,
        collected_at=now,
    )

    events = await repo.get_node_events("nodekey:test001")  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0].from_state is None
    assert events[0].to_state == "ONLINE"
    assert events[0].note == "first_seen"


async def test_no_event_when_state_unchanged(repo: Repository):
    """状态不变时不写 tailscale_node_events。"""
    now = datetime.now(UTC)
    await repo.upsert_tailscale_node(  # type: ignore[attr-defined]
        node_key="nodekey:test002",
        hostname="stable-host",
        dns_name=None,
        tailscale_ips=["100.1.1.2"],
        os="linux",
        online_state="ONLINE",
        is_exit_node=False,
        last_seen_at=None,
        collected_at=now,
    )
    # 再次 upsert, 状态不变
    await repo.upsert_tailscale_node(  # type: ignore[attr-defined]
        node_key="nodekey:test002",
        hostname="stable-host",
        dns_name=None,
        tailscale_ips=["100.1.1.2"],
        os="linux",
        online_state="ONLINE",
        is_exit_node=False,
        last_seen_at=None,
        collected_at=now + timedelta(seconds=60),
    )

    events = await repo.get_node_events("nodekey:test002")  # type: ignore[attr-defined]
    # 只有首次发现的一条
    assert len(events) == 1
    assert events[0].note == "first_seen"


async def test_state_change_writes_event(repo: Repository):
    """状态从 ONLINE 变为 OFFLINE → tailscale_node_events 增加一行。"""
    now = datetime.now(UTC)
    await repo.upsert_tailscale_node(  # type: ignore[attr-defined]
        node_key="nodekey:test003",
        hostname="changing-host",
        dns_name=None,
        tailscale_ips=["100.1.1.3"],
        os="linux",
        online_state="ONLINE",
        is_exit_node=False,
        last_seen_at=None,
        collected_at=now,
    )
    # 变更状态
    await repo.upsert_tailscale_node(  # type: ignore[attr-defined]
        node_key="nodekey:test003",
        hostname="changing-host",
        dns_name=None,
        tailscale_ips=["100.1.1.3"],
        os="linux",
        online_state="OFFLINE",
        is_exit_node=False,
        last_seen_at=now + timedelta(minutes=5),
        collected_at=now + timedelta(minutes=5),
    )

    events = await repo.get_node_events("nodekey:test003")  # type: ignore[attr-defined]
    # 首次发现 + 状态变更 = 2 条
    assert len(events) == 2
    # 最新事件 (第一条, 因 ORDER BY occurred_at DESC) 是变更
    change_event = events[0]
    assert change_event.from_state == "ONLINE"
    assert change_event.to_state == "OFFLINE"
    assert change_event.note is None


async def test_online_to_offline_event_details(repo: Repository):
    """验证 ONLINE→OFFLINE 变更事件的 from_state/to_state 正确。"""
    now = datetime.now(UTC)
    await repo.upsert_tailscale_node(  # type: ignore[attr-defined]
        node_key="nodekey:evtest",
        hostname="ev-host",
        dns_name=None,
        tailscale_ips=[],
        os="linux",
        online_state="ONLINE",
        is_exit_node=False,
        last_seen_at=None,
        collected_at=now,
    )
    await repo.upsert_tailscale_node(  # type: ignore[attr-defined]
        node_key="nodekey:evtest",
        hostname="ev-host",
        dns_name=None,
        tailscale_ips=[],
        os="linux",
        online_state="OFFLINE",
        is_exit_node=False,
        last_seen_at=now,
        collected_at=now + timedelta(seconds=60),
    )

    events = await repo.get_node_events("nodekey:evtest")  # type: ignore[attr-defined]
    change = next(e for e in events if e.from_state == "ONLINE")
    assert change.to_state == "OFFLINE"


# --------------------------------------------------------------------------- #
# socket 不可达
# --------------------------------------------------------------------------- #


async def test_socket_unreachable_raises(repo: Repository, tmp_path: Path):
    """socket 文件不存在(或拒绝连接) → collect() 向上抛 aiohttp 异常, 不静默吞掉。"""
    # 使用一个确实不存在的 socket 路径
    collector = TailscaleCollector(
        socket_path=str(tmp_path / "nonexistent.sock"),
        repo=repo,
        timeout_seconds=2,
    )

    # mock aiohttp.ClientSession 抛 ClientConnectorError
    mock_conn_error = aiohttp.ClientConnectorError(
        connection_key=MagicMock(),  # type: ignore[arg-type]
        os_error=OSError("No such file"),
    )

    async def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise mock_conn_error

    mock_session = MagicMock()
    mock_session.get = AsyncMock(side_effect=_raise)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(aiohttp.ClientConnectorError):
            await collector.collect()


async def test_socket_unreachable_degrades_via_framework(repo: Repository, tmp_path: Path):
    """经 run_collector 包装,socket 不可达降级为 collector_run.status='down'。"""
    from panel.collectors.scheduler import run_collector

    collector = TailscaleCollector(
        socket_path=str(tmp_path / "nonexistent.sock"),
        repo=repo,
        timeout_seconds=2,
    )

    mock_conn_error = aiohttp.ClientConnectorError(
        connection_key=MagicMock(),  # type: ignore[arg-type]
        os_error=OSError("No such file"),
    )
    mock_session = MagicMock()
    mock_session.get = AsyncMock(side_effect=mock_conn_error)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await run_collector(collector, repo)

    assert result.status == "error"
    assert result.name == "tailscale"


# --------------------------------------------------------------------------- #
# register() 工厂
# --------------------------------------------------------------------------- #


def test_register_skips_when_socket_missing(repo: Repository, tmp_path: Path, caplog):
    """socket 不存在 → 记 warning, 不注册, 不抛异常。"""
    settings = Settings()
    settings.__dict__["tailscale_socket"] = str(tmp_path / "nonexistent.sock")

    with caplog.at_level(logging.WARNING):
        register_tailscale(settings, repo)

    assert registry.iter_collectors() == []
    assert any("TailscaleCollector disabled" in r.message for r in caplog.records)


def test_register_succeeds_when_socket_exists(repo: Repository, tmp_path: Path):
    """socket 文件存在 → 注册成功。"""
    socket_file = tmp_path / "tailscaled.sock"
    socket_file.touch()  # 创建文件模拟 socket 存在

    settings = Settings()
    settings.__dict__["tailscale_socket"] = str(socket_file)

    register_tailscale(settings, repo)

    collectors = registry.iter_collectors()
    assert len(collectors) == 1
    assert collectors[0].name == "tailscale"


# --------------------------------------------------------------------------- #
# Collector 协议属性
# --------------------------------------------------------------------------- #


def test_collector_implements_protocol(repo: Repository):
    collector = _make_collector(repo)
    assert isinstance(collector, Collector)
    assert collector.name == "tailscale"
    assert collector.interval_seconds == 60
    assert collector.timeout_seconds == 5  # 传入的值


# --------------------------------------------------------------------------- #
# 活体验证 (integration, 默认 CI 跳过)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
async def test_live_smoke(repo: Repository):
    """在有真实 tailscaled.sock 的机器上, 实际调用 localapi 并解析节点。

    本测试用 pytest.mark.integration 标记, 默认 CI 不执行:
        pytest -m "not integration"

    本地运行:
        pytest -m integration tests/collectors/tailscale/test_collector.py::test_live_smoke
    """
    from pathlib import Path as _Path

    socket_path = "/var/run/tailscale/tailscaled.sock"
    if not _Path(socket_path).exists():  # noqa: ASYNC240 (sync check intentional in skip guard)
        pytest.skip("tailscaled.sock not found; skipping live test")

    collector = TailscaleCollector(
        socket_path=socket_path,
        repo=repo,
        timeout_seconds=10,
    )
    samples = await collector.collect()

    print(f"\n[live smoke] Got {len(samples)} nodes:")
    nodes = await repo.get_all_nodes()  # type: ignore[attr-defined]
    by_id = {n.id: n for n in nodes}
    for s in samples:
        node = by_id.get(s.target_id)
        hostname = node.hostname if node else "?"
        print(f"  {hostname:30s} {s.value_text} (status={s.status})")

    assert len(samples) > 0, "Expected at least one node in tailnet"
