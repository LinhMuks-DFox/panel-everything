"""TASK-002: 持久化层测试。

覆盖:WAL 生效、migrate 幂等、snapshot upsert、history append + 区间查询、
collector_run + get_last_success / get_all_last_runs、/healthz 反映真实 DB。

用临时文件 DB(WAL 需文件,不用 :memory:)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from panel.collectors.base import CollectorResult, MetricSample
from panel.config.settings import Settings, get_settings
from panel.db import connection, migrate
from panel.db.repository import Repository
from panel.main import create_app


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


def _sample(target_id=1, metric="online", value_num=1.0, value_text=None,
            status="ok", collected_at=None):
    return MetricSample(
        target_id=target_id,
        metric=metric,
        value_num=value_num,
        value_text=value_text,
        status=status,
        collected_at=collected_at or datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# 连接 / WAL / migrate
# --------------------------------------------------------------------------- #


async def test_journal_mode_is_wal(conn):
    async with conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row[0].lower() == "wal"


async def test_foreign_keys_on(conn):
    async with conn.execute("PRAGMA foreign_keys") as cur:
        row = await cur.fetchone()
    assert row[0] == 1


async def test_migrate_idempotent_and_tables_exist(conn):
    # 再跑一次 migrate 不应报错
    await migrate.run(conn)
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        names = {r[0] async for r in cur}
    assert {"latest_snapshot", "metric_history", "collector_run"} <= names


# --------------------------------------------------------------------------- #
# upsert_snapshot
# --------------------------------------------------------------------------- #


async def test_upsert_snapshot_inserts_then_updates(repo, conn):
    s1 = _sample(value_num=1.0, status="ok")
    await repo.upsert_snapshot("gpu", [s1])

    async with conn.execute("SELECT COUNT(*) FROM latest_snapshot") as cur:
        count1 = (await cur.fetchone())[0]
    assert count1 == 1

    # 同 (collector,target_id,metric) 再写 -> 更新,不新增行
    s2 = _sample(value_num=0.0, status="unreachable")
    await repo.upsert_snapshot("gpu", [s2])

    async with conn.execute("SELECT COUNT(*) FROM latest_snapshot") as cur:
        count2 = (await cur.fetchone())[0]
    assert count2 == 1

    row = await repo.get_snapshot_metric("gpu", 1, "online")
    assert row is not None
    assert row.value_num == 0.0
    assert row.status == "unreachable"


async def test_upsert_snapshot_empty_noop(repo, conn):
    await repo.upsert_snapshot("gpu", [])
    async with conn.execute("SELECT COUNT(*) FROM latest_snapshot") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_get_snapshot_returns_all_for_collector(repo):
    await repo.upsert_snapshot(
        "gpu",
        [_sample(target_id=1, metric="online"), _sample(target_id=2, metric="util")],
    )
    await repo.upsert_snapshot("azure", [_sample(target_id=1, metric="power")])

    gpu_rows = await repo.get_snapshot("gpu")
    assert len(gpu_rows) == 2
    assert {r.metric for r in gpu_rows} == {"online", "util"}

    azure_rows = await repo.get_snapshot("azure")
    assert len(azure_rows) == 1


async def test_get_snapshot_metric_missing_returns_none(repo):
    assert await repo.get_snapshot_metric("gpu", 99, "nope") is None


# --------------------------------------------------------------------------- #
# append_history + get_history
# --------------------------------------------------------------------------- #


async def test_append_history_adds_row_each_time(repo, conn):
    await repo.append_history("gpu", [_sample()])
    await repo.append_history("gpu", [_sample()])
    async with conn.execute("SELECT COUNT(*) FROM metric_history") as cur:
        assert (await cur.fetchone())[0] == 2


async def test_get_history_range_limit_and_order(repo):
    base = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
    # 5 个点,间隔 1 分钟
    for i in range(5):
        await repo.append_history(
            "gpu",
            [_sample(value_num=float(i), collected_at=base + timedelta(minutes=i))],
        )

    # 区间 [t1, t3] 应得到 i=1,2,3 三点,升序
    rows = await repo.get_history(
        "gpu", 1, "online",
        since=base + timedelta(minutes=1),
        until=base + timedelta(minutes=3),
    )
    assert [r.value_num for r in rows] == [1.0, 2.0, 3.0]

    # limit 截断:取最近 2 条但仍升序 -> i=3,4
    rows2 = await repo.get_history("gpu", 1, "online", since=base, limit=2)
    assert [r.value_num for r in rows2] == [3.0, 4.0]


async def test_get_history_filters_by_target_and_metric(repo):
    base = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
    await repo.append_history("gpu", [_sample(target_id=1, metric="online",
                                              collected_at=base)])
    await repo.append_history("gpu", [_sample(target_id=2, metric="online",
                                              collected_at=base)])
    rows = await repo.get_history("gpu", 1, "online", since=base)
    assert len(rows) == 1
    assert rows[0].target_id == 1


# --------------------------------------------------------------------------- #
# collector_run / get_last_success / get_all_last_runs
# --------------------------------------------------------------------------- #


async def test_get_last_success_none_when_no_up(repo):
    await repo.record_collector_run(
        CollectorResult(name="gpu", status="error", sample_count=0,
                        duration_ms=10, error="boom",
                        ran_at=datetime(2026, 6, 28, 10, 0, tzinfo=UTC))
    )
    assert await repo.get_last_success("gpu") is None


async def test_get_last_success_returns_latest_up(repo):
    t1 = datetime(2026, 6, 28, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 28, 11, 0, tzinfo=UTC)
    await repo.record_collector_run(
        CollectorResult("gpu", "up", 3, 5, None, ran_at=t1)
    )
    await repo.record_collector_run(
        CollectorResult("gpu", "down", 0, 5, "timeout", ran_at=t2)
    )
    last = await repo.get_last_success("gpu")
    assert last == t1
    assert last.tzinfo is not None


async def test_get_all_last_runs_one_per_collector(repo):
    t1 = datetime(2026, 6, 28, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 28, 11, 0, tzinfo=UTC)
    await repo.record_collector_run(CollectorResult("gpu", "up", 1, 5, None, ran_at=t1))
    await repo.record_collector_run(CollectorResult("gpu", "down", 0, 5, "x", ran_at=t2))
    await repo.record_collector_run(CollectorResult("azure", "up", 2, 8, None, ran_at=t1))

    runs = await repo.get_all_last_runs()
    by_name = {r.collector: r for r in runs}
    assert set(by_name) == {"gpu", "azure"}
    # gpu 最近一行是 down(t2)
    assert by_name["gpu"].status == "down"
    assert by_name["azure"].status == "up"


# --------------------------------------------------------------------------- #
# /healthz 反映真实 DB(lifespan 起 DB)
# --------------------------------------------------------------------------- #


async def test_healthz_db_ok_with_lifespan(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PANEL_DB_PATH", str(tmp_path / "panel.db"))
    get_settings.cache_clear()
    settings = Settings()
    app = create_app(settings)

    transport = httpx.ASGITransport(app=app)
    async with LifespanRunner(app):
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            resp = await client.get("/healthz")
    get_settings.cache_clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"


class LifespanRunner:
    """手动驱动 FastAPI lifespan(ASGITransport 不自动触发 startup/shutdown)。"""

    def __init__(self, app):
        self._cm = app.router.lifespan_context(app)

    async def __aenter__(self):
        await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._cm.__aexit__(*exc)
