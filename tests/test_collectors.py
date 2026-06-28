"""TASK-003: 采集框架测试。

覆盖:
  - registry 注册/重复抛错/枚举/清空
  - run_collector 正常路径(写 snapshot+history,collector_run.status=up)
  - run_collector 异常路径(collect 抛 → error,无 snapshot,不外泄)
  - run_collector 超时路径(collect 超 timeout → down,不外泄)
  - 单 collector 失败不影响同批其它 collector
  - collector_run.error 已脱敏
  - build_scheduler 为每个注册 collector 生成 id=name 的 job

用临时文件 DB(WAL 需文件)+ Repository,夹具 NullCollector / 抛异常 /
超时的 fake collector。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from panel.collectors import registry
from panel.collectors.base import Collector, MetricSample
from panel.collectors.scheduler import build_scheduler, run_collector
from panel.db import connection, migrate
from panel.db.repository import Repository

# --------------------------------------------------------------------------- #
# 夹具
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
def repo(conn):
    return Repository(conn)


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后清空全局注册表,避免相互污染。"""
    registry.clear()
    yield
    registry.clear()


class NullCollector:
    """验证全链路的最小采集器:返回固定 sample。"""

    name = "null"
    interval_seconds = 60
    timeout_seconds = 5

    def __init__(self, name: str = "null", samples: list[MetricSample] | None = None) -> None:
        self.name = name
        self._samples = samples if samples is not None else [
            MetricSample(target_id=1, metric="online", value_num=1.0, status="ok",
                         collected_at=datetime.now(UTC)),
            MetricSample(target_id=2, metric="online", value_num=0.0, status="unreachable",
                         collected_at=datetime.now(UTC)),
        ]

    async def collect(self) -> list[MetricSample]:
        return list(self._samples)


class RaisingCollector:
    """collect() 抛异常,异常消息含敏感模式以验证脱敏。"""

    name = "boom"
    interval_seconds = 60
    timeout_seconds = 5

    def __init__(self, name: str = "boom", message: str = "auth failed") -> None:
        self.name = name
        self._message = message

    async def collect(self) -> list[MetricSample]:
        raise RuntimeError(self._message)


class SlowCollector:
    """collect() 睡眠超过 timeout_seconds 以触发框架超时。"""

    name = "slow"
    interval_seconds = 60
    timeout_seconds = 1  # 小超时

    def __init__(self, name: str = "slow", sleep: float = 5.0) -> None:
        self.name = name
        self._sleep = sleep

    async def collect(self) -> list[MetricSample]:
        await asyncio.sleep(self._sleep)
        return []


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_register_and_iter():
    c = NullCollector()
    registry.register(c)
    assert registry.iter_collectors() == [c]
    assert registry.get("null") is c


def test_register_duplicate_raises():
    registry.register(NullCollector())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(NullCollector())


def test_clear():
    registry.register(NullCollector())
    registry.clear()
    assert registry.iter_collectors() == []


def test_collector_protocol_runtime_checkable():
    assert isinstance(NullCollector(), Collector)


# --------------------------------------------------------------------------- #
# run_collector — 正常路径
# --------------------------------------------------------------------------- #


async def test_run_collector_success_writes_all(repo: Repository):
    c = NullCollector()
    result = await run_collector(c, repo)

    assert result.status == "up"
    assert result.sample_count == 2
    assert result.error is None
    assert result.duration_ms >= 0

    # snapshot 写入
    snap = await repo.get_snapshot("null")
    assert len(snap) == 2

    # history 写入
    hist = await repo.get_history(
        "null", target_id=1, metric="online",
        since=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert len(hist) == 1

    # collector_run 记录 up
    runs = await repo.get_all_last_runs()
    assert len(runs) == 1
    assert runs[0].collector == "null"
    assert runs[0].status == "up"
    assert runs[0].sample_count == 2

    # get_last_success 可用于 stale 判定
    assert await repo.get_last_success("null") is not None


# --------------------------------------------------------------------------- #
# run_collector — 异常路径
# --------------------------------------------------------------------------- #


async def test_run_collector_exception_degrades(repo: Repository):
    c = RaisingCollector(message="plain failure")
    # 不应抛出
    result = await run_collector(c, repo)

    assert result.status == "error"
    assert result.sample_count == 0
    assert result.error is not None

    # 无 snapshot / history 写入
    assert await repo.get_snapshot("boom") == []
    hist = await repo.get_history(
        "boom", target_id=0, metric="x",
        since=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert hist == []

    # collector_run 记 error,get_last_success 为 None
    runs = await repo.get_all_last_runs()
    assert runs[0].status == "error"
    assert await repo.get_last_success("boom") is None


async def test_run_collector_error_is_scrubbed(repo: Repository):
    # 异常消息含敏感模式 token=... — 写库前必须脱敏。
    secret = "token=abcdef1234567890"  # noqa: S105 — 测试构造的假敏感串,验证脱敏
    c = RaisingCollector(message=f"login failed {secret}")
    result = await run_collector(c, repo)

    assert result.status == "error"
    assert "abcdef1234567890" not in (result.error or "")
    assert "***" in (result.error or "")

    runs = await repo.get_all_last_runs()
    assert "abcdef1234567890" not in (runs[0].error or "")


# --------------------------------------------------------------------------- #
# run_collector — 超时路径
# --------------------------------------------------------------------------- #


async def test_run_collector_timeout_degrades(repo: Repository):
    c = SlowCollector(sleep=5.0)  # timeout_seconds=1
    result = await run_collector(c, repo)

    assert result.status == "down"
    assert result.error == "timeout"
    assert result.sample_count == 0

    assert await repo.get_snapshot("slow") == []
    runs = await repo.get_all_last_runs()
    assert runs[0].status == "down"


# --------------------------------------------------------------------------- #
# 隔离:一失败不影响其它
# --------------------------------------------------------------------------- #


async def test_failure_isolation(repo: Repository):
    good = NullCollector(name="good")
    bad = RaisingCollector(name="bad")

    # 并发运行;均不应抛出
    results = await asyncio.gather(
        run_collector(good, repo),
        run_collector(bad, repo),
    )
    by_name = {r.name: r for r in results}

    assert by_name["good"].status == "up"
    assert by_name["bad"].status == "error"

    # good 的数据完整落库,不受 bad 影响
    assert len(await repo.get_snapshot("good")) == 2
    assert await repo.get_last_success("good") is not None
    assert await repo.get_last_success("bad") is None


# --------------------------------------------------------------------------- #
# build_scheduler
# --------------------------------------------------------------------------- #


def test_build_scheduler_one_job_per_collector(repo: Repository):
    registry.register(NullCollector(name="a"))
    registry.register(NullCollector(name="b"))

    sched = build_scheduler(repo)
    try:
        jobs = sched.get_jobs()
        ids = {j.id for j in jobs}
        assert ids == {"a", "b"}
        # job id 等于 collector.name
        for j in jobs:
            assert j.id in {"a", "b"}
    finally:
        # 未 start,无需 shutdown;但保险起见若已运行则关闭。
        if sched.running:
            sched.shutdown(wait=False)


def test_build_scheduler_not_started(repo: Repository):
    registry.register(NullCollector(name="a"))
    sched = build_scheduler(repo)
    assert sched.running is False


# --------------------------------------------------------------------------- #
# lifespan 集成:scheduler 经 app.lifespan 启动→首采→停机
# --------------------------------------------------------------------------- #


async def test_lifespan_starts_scheduler_and_runs_first_collect(tmp_path, monkeypatch):
    """注册一个 collector,经 app lifespan 启动后应跑首采并落库,关闭后 scheduler 停。"""
    from panel.config.settings import Settings, get_settings
    from panel.main import create_app

    monkeypatch.setenv("PANEL_DB_PATH", str(tmp_path / "panel.db"))
    get_settings.cache_clear()

    registry.register(NullCollector(name="lifespan_demo"))

    settings = Settings()
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            scheduler = app.state.scheduler
            assert scheduler.running is True
            # 首采 next_run_time=now;给调度循环一点时间执行一次。
            for _ in range(50):
                runs = await app.state.repo.get_all_last_runs()
                if runs:
                    break
                await asyncio.sleep(0.05)
            runs = await app.state.repo.get_all_last_runs()
            assert any(r.collector == "lifespan_demo" and r.status == "up" for r in runs)
        # 退出 lifespan 后 scheduler 已 shutdown
        assert scheduler.running is False
    finally:
        get_settings.cache_clear()
