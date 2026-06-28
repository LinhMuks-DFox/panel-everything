"""TASK-016: GPU 历史降采样 job + 趋势查询 API 测试.

覆盖:
- floor_bucket 纯函数 5min/1h 向下对齐
- run_5m_downsample: 桶 avg/max/count 正确;只统计 status='ok'
- run_5m_downsample 结尾清理 gpu_metrics 超 48h 行 + gpu_metrics_5m 超 30 天桶
- run_1h_downsample: 从 5m 桶聚合(avg-of-avg / max-of-max / sum count)
- upsert 桶幂等(同 bucket_start 第二次写覆盖)
- GET /gpu/{id}/{idx}/history 三种粒度 raw/5m/1h
- history 默认 granularity=5m / since=now-24h / limit
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from panel.collectors import registry
from panel.collectors.gpu.downsampler import (
    FIVE_MIN,
    ONE_HOUR,
    floor_bucket,
    run_1h_downsample,
    run_5m_downsample,
)
from panel.config.settings import Settings
from panel.db import connection, migrate
from panel.db.gpu_repository import GpuBucketRow, GpuRepository, GpuSample
from panel.domain.models import ServerIn
from panel.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
async def db_repos(tmp_path: Path):
    """返回 (conn, GpuRepository),供直接写库的 job/repo 用例使用。"""
    db_path = str(tmp_path / "ds.db")
    conn = await connection.connect(db_path)
    await migrate.run(conn)
    gpu_repo = GpuRepository(conn)
    yield conn, gpu_repo
    await conn.close()


@pytest.fixture
async def client_with_db(tmp_path: Path):
    """返回 (client, gpu_repo),通过同一 DB 文件共享状态(写后读)。"""
    db_path = str(tmp_path / "shared.db")
    conn_setup = await connection.connect(db_path)
    await migrate.run(conn_setup)
    gpu_repo_setup = GpuRepository(conn_setup)

    settings = Settings(db_path=db_path)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, gpu_repo_setup

    await conn_setup.close()


def _sample(
    server_id: int,
    gpu_index: int,
    *,
    util: float | None,
    mem_used: float | None,
    mem_total: float | None,
    temp: float | None,
    power: float | None,
    at: datetime,
    status: str = "ok",
) -> GpuSample:
    return GpuSample(
        server_id=server_id,
        gpu_index=gpu_index,
        gpu_name="NVIDIA A100",
        util_pct=util,
        mem_used_mib=mem_used,
        mem_total_mib=mem_total,
        temp_c=temp,
        power_w=power,
        status=status,  # type: ignore[arg-type]
        collected_at=at,
    )


# ---------------------------------------------------------------------------
# floor_bucket 纯函数
# ---------------------------------------------------------------------------


def test_floor_bucket_5min_aligns_down():
    dt = datetime(2026, 6, 28, 12, 7, 33, tzinfo=UTC)
    assert floor_bucket(dt, FIVE_MIN) == datetime(2026, 6, 28, 12, 5, 0, tzinfo=UTC)


def test_floor_bucket_5min_on_boundary_is_identity():
    dt = datetime(2026, 6, 28, 12, 5, 0, tzinfo=UTC)
    assert floor_bucket(dt, FIVE_MIN) == dt


def test_floor_bucket_1h_aligns_down():
    dt = datetime(2026, 6, 28, 12, 59, 59, tzinfo=UTC)
    assert floor_bucket(dt, ONE_HOUR) == datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


def test_floor_bucket_naive_treated_as_utc():
    dt = datetime(2026, 6, 28, 12, 7, 0)  # naive
    out = floor_bucket(dt, FIVE_MIN)
    assert out.tzinfo is not None
    assert out == datetime(2026, 6, 28, 12, 5, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# run_5m_downsample 聚合正确性
# ---------------------------------------------------------------------------


async def test_5m_downsample_avg_max_count(db_repos):
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g1", has_gpu=True))

    # now -> 上一个完整 5m 桶 = [12:00, 12:05); 把 now 设在 12:07
    now = datetime(2026, 6, 28, 12, 7, 0, tzinfo=UTC)
    bucket = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

    await gpu_repo.append_gpu_metrics([
        _sample(srv_id, 0, util=10.0, mem_used=2000.0, mem_total=10000.0,
                temp=50.0, power=100.0, at=bucket + timedelta(seconds=10)),
        _sample(srv_id, 0, util=30.0, mem_used=4000.0, mem_total=10000.0,
                temp=70.0, power=200.0, at=bucket + timedelta(seconds=120)),
    ])

    await run_5m_downsample(gpu_repo, now=now)

    rows = await gpu_repo.get_gpu_history_5m(srv_id, 0, since=bucket - timedelta(hours=1))
    assert len(rows) == 1
    r = rows[0]
    assert r.avg_util_pct == pytest.approx(20.0)        # (10+30)/2
    assert r.avg_mem_pct == pytest.approx(30.0)         # (20+40)/2
    assert r.max_temp_c == pytest.approx(70.0)          # max(50,70)
    assert r.max_power_w == pytest.approx(200.0)        # max(100,200)
    assert r.sample_count == 2


async def test_5m_downsample_ignores_non_ok_rows(db_repos):
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g2", has_gpu=True))
    now = datetime(2026, 6, 28, 12, 7, 0, tzinfo=UTC)
    bucket = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

    await gpu_repo.append_gpu_metrics([
        _sample(srv_id, 0, util=80.0, mem_used=5000.0, mem_total=10000.0,
                temp=60.0, power=150.0, at=bucket + timedelta(seconds=10)),
        # unreachable 行(数值列实际为 None),不应计入均值/计数
        _sample(srv_id, 0, util=None, mem_used=None, mem_total=None,
                temp=None, power=None, at=bucket + timedelta(seconds=20),
                status="unreachable"),
    ])

    await run_5m_downsample(gpu_repo, now=now)
    rows = await gpu_repo.get_gpu_history_5m(srv_id, 0, since=bucket - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].sample_count == 1            # 仅 ok 行
    assert rows[0].avg_util_pct == pytest.approx(80.0)


async def test_5m_downsample_only_previous_bucket(db_repos):
    """当前正在累积的桶不应被聚合。"""
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g3", has_gpu=True))
    now = datetime(2026, 6, 28, 12, 7, 0, tzinfo=UTC)
    prev_bucket = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
    cur_bucket = datetime(2026, 6, 28, 12, 5, 0, tzinfo=UTC)

    await gpu_repo.append_gpu_metrics([
        _sample(srv_id, 0, util=10.0, mem_used=1000.0, mem_total=10000.0,
                temp=40.0, power=90.0, at=prev_bucket + timedelta(seconds=30)),
        # 当前桶 [12:05,12:10) 的样本 —— 不应进入本轮
        _sample(srv_id, 0, util=99.0, mem_used=9000.0, mem_total=10000.0,
                temp=90.0, power=300.0, at=cur_bucket + timedelta(seconds=30)),
    ])

    await run_5m_downsample(gpu_repo, now=now)
    rows = await gpu_repo.get_gpu_history_5m(srv_id, 0, since=prev_bucket - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].bucket_start == prev_bucket.isoformat()
    assert rows[0].avg_util_pct == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 保留清理
# ---------------------------------------------------------------------------


async def test_5m_downsample_prunes_raw_older_than_48h(db_repos):
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g4", has_gpu=True))
    now = datetime(2026, 6, 28, 12, 7, 0, tzinfo=UTC)

    old_at = now - timedelta(hours=49)    # 超 48h,应删
    fresh_at = now - timedelta(hours=1)   # 保留
    await gpu_repo.append_gpu_metrics([
        _sample(srv_id, 0, util=5.0, mem_used=1000.0, mem_total=10000.0,
                temp=40.0, power=80.0, at=old_at),
        _sample(srv_id, 0, util=6.0, mem_used=1000.0, mem_total=10000.0,
                temp=40.0, power=80.0, at=fresh_at),
    ])

    await run_5m_downsample(gpu_repo, now=now)

    remaining = await gpu_repo.get_gpu_history(
        srv_id, 0, since=now - timedelta(days=10), limit=100
    )
    times = [r.collected_at for r in remaining]
    assert old_at.isoformat() not in times
    assert fresh_at.isoformat() in times


async def test_5m_downsample_prunes_5m_older_than_30d(db_repos):
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g5", has_gpu=True))
    now = datetime(2026, 6, 28, 12, 7, 0, tzinfo=UTC)

    old_bucket = floor_bucket(now - timedelta(days=31), FIVE_MIN)
    keep_bucket = floor_bucket(now - timedelta(days=5), FIVE_MIN)
    for b in (old_bucket, keep_bucket):
        await gpu_repo.upsert_5m_bucket(GpuBucketRow(
            server_id=srv_id, gpu_index=0, avg_util_pct=1.0, avg_mem_pct=1.0,
            max_temp_c=1.0, max_power_w=1.0, sample_count=1,
            bucket_start=b.isoformat(),
        ))

    await run_5m_downsample(gpu_repo, now=now)

    rows = await gpu_repo.get_gpu_history_5m(
        srv_id, 0, since=now - timedelta(days=60), limit=100
    )
    starts = [r.bucket_start for r in rows]
    assert old_bucket.isoformat() not in starts
    assert keep_bucket.isoformat() in starts


# ---------------------------------------------------------------------------
# run_1h_downsample 从 5m 桶聚合
# ---------------------------------------------------------------------------


async def test_1h_downsample_from_5m_buckets(db_repos):
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g6", has_gpu=True))
    now = datetime(2026, 6, 28, 13, 5, 0, tzinfo=UTC)
    hour = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)  # 上一个完整 1h 桶

    # 该小时内的两个 5m 桶
    await gpu_repo.upsert_5m_bucket(GpuBucketRow(
        server_id=srv_id, gpu_index=0, avg_util_pct=20.0, avg_mem_pct=10.0,
        max_temp_c=50.0, max_power_w=100.0, sample_count=5,
        bucket_start=(hour + timedelta(minutes=0)).isoformat(),
    ))
    await gpu_repo.upsert_5m_bucket(GpuBucketRow(
        server_id=srv_id, gpu_index=0, avg_util_pct=40.0, avg_mem_pct=30.0,
        max_temp_c=70.0, max_power_w=200.0, sample_count=5,
        bucket_start=(hour + timedelta(minutes=5)).isoformat(),
    ))

    await run_1h_downsample(gpu_repo, now=now)

    rows = await gpu_repo.get_gpu_history_1h(srv_id, 0, since=hour - timedelta(hours=2))
    assert len(rows) == 1
    r = rows[0]
    assert r.avg_util_pct == pytest.approx(30.0)    # avg(20,40)
    assert r.avg_mem_pct == pytest.approx(20.0)     # avg(10,30)
    assert r.max_temp_c == pytest.approx(70.0)      # max(50,70)
    assert r.max_power_w == pytest.approx(200.0)
    assert r.sample_count == 10                     # sum(5,5)


# ---------------------------------------------------------------------------
# upsert 幂等
# ---------------------------------------------------------------------------


async def test_5m_downsample_multi_card_batch_persists_all(db_repos):
    """一轮多卡降采样:批量 upsert 后两张卡的桶都正确落库(MS-005 #10 批量写)。"""
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g-multi", has_gpu=True))
    now = datetime(2026, 6, 28, 12, 7, 0, tzinfo=UTC)
    bucket = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

    await gpu_repo.append_gpu_metrics([
        _sample(srv_id, 0, util=20.0, mem_used=2000.0, mem_total=10000.0,
                temp=50.0, power=100.0, at=bucket + timedelta(seconds=10)),
        _sample(srv_id, 1, util=80.0, mem_used=8000.0, mem_total=10000.0,
                temp=75.0, power=250.0, at=bucket + timedelta(seconds=10)),
    ])

    await run_5m_downsample(gpu_repo, now=now)

    r0 = await gpu_repo.get_gpu_history_5m(srv_id, 0, since=bucket - timedelta(hours=1))
    r1 = await gpu_repo.get_gpu_history_5m(srv_id, 1, since=bucket - timedelta(hours=1))
    assert len(r0) == 1 and r0[0].avg_util_pct == pytest.approx(20.0)
    assert len(r1) == 1 and r1[0].avg_util_pct == pytest.approx(80.0)


async def test_upsert_5m_buckets_batch(db_repos):
    """批量 upsert_5m_buckets:多桶一次写入,空列表无副作用。"""
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g-batch", has_gpu=True))
    b = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC).isoformat()

    # 空列表:不抛、不写
    await gpu_repo.upsert_5m_buckets([])

    await gpu_repo.upsert_5m_buckets([
        GpuBucketRow(server_id=srv_id, gpu_index=0, avg_util_pct=11.0, avg_mem_pct=11.0,
                     max_temp_c=11.0, max_power_w=11.0, sample_count=1, bucket_start=b),
        GpuBucketRow(server_id=srv_id, gpu_index=1, avg_util_pct=22.0, avg_mem_pct=22.0,
                     max_temp_c=22.0, max_power_w=22.0, sample_count=2, bucket_start=b),
    ])

    since = datetime(2026, 6, 28, tzinfo=UTC)
    assert (await gpu_repo.get_gpu_history_5m(srv_id, 0, since=since))[0].sample_count == 1
    assert (await gpu_repo.get_gpu_history_5m(srv_id, 1, since=since))[0].sample_count == 2


async def test_upsert_5m_bucket_idempotent(db_repos):
    conn, gpu_repo = db_repos
    srv_id = await gpu_repo.insert_server(ServerIn(name="g7", has_gpu=True))
    bucket = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC).isoformat()

    await gpu_repo.upsert_5m_bucket(GpuBucketRow(
        server_id=srv_id, gpu_index=0, avg_util_pct=10.0, avg_mem_pct=10.0,
        max_temp_c=10.0, max_power_w=10.0, sample_count=1, bucket_start=bucket,
    ))
    # 重算同桶,覆盖
    await gpu_repo.upsert_5m_bucket(GpuBucketRow(
        server_id=srv_id, gpu_index=0, avg_util_pct=99.0, avg_mem_pct=99.0,
        max_temp_c=99.0, max_power_w=99.0, sample_count=7, bucket_start=bucket,
    ))

    rows = await gpu_repo.get_gpu_history_5m(srv_id, 0, since=datetime(2026, 6, 28, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].avg_util_pct == pytest.approx(99.0)
    assert rows[0].sample_count == 7


# ---------------------------------------------------------------------------
# history 端点 — 三种粒度
# ---------------------------------------------------------------------------


async def test_history_endpoint_raw(client_with_db):
    c, gpu_repo = client_with_db
    srv_id = await gpu_repo.insert_server(ServerIn(name="h-raw", has_gpu=True))
    at = datetime.now(UTC) - timedelta(minutes=5)
    await gpu_repo.append_gpu_metrics([
        _sample(srv_id, 0, util=42.0, mem_used=5000.0, mem_total=10000.0,
                temp=55.0, power=120.0, at=at),
    ])

    resp = await c.get(f"/api/v1/gpu/{srv_id}/0/history", params={"granularity": "raw"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    pt = body[0]
    assert pt["avg_util_pct"] == pytest.approx(42.0)
    assert pt["avg_mem_pct"] == pytest.approx(50.0)
    assert pt["max_temp_c"] == pytest.approx(55.0)
    assert pt["max_power_w"] == pytest.approx(120.0)
    assert pt["sample_count"] == 1
    assert set(pt.keys()) == {
        "bucket_start", "avg_util_pct", "avg_mem_pct",
        "max_temp_c", "max_power_w", "sample_count",
    }


async def test_history_endpoint_5m_default(client_with_db):
    c, gpu_repo = client_with_db
    srv_id = await gpu_repo.insert_server(ServerIn(name="h-5m", has_gpu=True))
    bucket = floor_bucket(datetime.now(UTC) - timedelta(hours=1), FIVE_MIN)
    await gpu_repo.upsert_5m_bucket(GpuBucketRow(
        server_id=srv_id, gpu_index=0, avg_util_pct=33.0, avg_mem_pct=22.0,
        max_temp_c=60.0, max_power_w=180.0, sample_count=4,
        bucket_start=bucket.isoformat(),
    ))

    # 不传 granularity -> 默认 5m;不传 since -> 默认 now-24h
    resp = await c.get(f"/api/v1/gpu/{srv_id}/0/history")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["avg_util_pct"] == pytest.approx(33.0)
    assert body[0]["sample_count"] == 4


async def test_history_endpoint_1h(client_with_db):
    c, gpu_repo = client_with_db
    srv_id = await gpu_repo.insert_server(ServerIn(name="h-1h", has_gpu=True))
    bucket = floor_bucket(datetime.now(UTC) - timedelta(hours=3), ONE_HOUR)
    await gpu_repo.upsert_1h_bucket(GpuBucketRow(
        server_id=srv_id, gpu_index=0, avg_util_pct=55.0, avg_mem_pct=44.0,
        max_temp_c=75.0, max_power_w=250.0, sample_count=60,
        bucket_start=bucket.isoformat(),
    ))

    resp = await c.get(
        f"/api/v1/gpu/{srv_id}/0/history",
        params={"granularity": "1h", "since": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["avg_util_pct"] == pytest.approx(55.0)
    assert body[0]["sample_count"] == 60


async def test_history_endpoint_unknown_card_returns_empty(client_with_db):
    c, gpu_repo = client_with_db
    srv_id = await gpu_repo.insert_server(ServerIn(name="h-empty", has_gpu=True))
    resp = await c.get(f"/api/v1/gpu/{srv_id}/9/history", params={"granularity": "5m"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_history_endpoint_invalid_granularity_422(client_with_db):
    c, gpu_repo = client_with_db
    srv_id = await gpu_repo.insert_server(ServerIn(name="h-bad", has_gpu=True))
    resp = await c.get(f"/api/v1/gpu/{srv_id}/0/history", params={"granularity": "10m"})
    assert resp.status_code == 422


async def test_history_endpoint_limit_caps_rows(client_with_db):
    c, gpu_repo = client_with_db
    srv_id = await gpu_repo.insert_server(ServerIn(name="h-limit", has_gpu=True))
    base = floor_bucket(datetime.now(UTC) - timedelta(hours=2), FIVE_MIN)
    for i in range(5):
        await gpu_repo.upsert_5m_bucket(GpuBucketRow(
            server_id=srv_id, gpu_index=0, avg_util_pct=float(i),
            avg_mem_pct=1.0, max_temp_c=1.0, max_power_w=1.0, sample_count=1,
            bucket_start=(base + timedelta(minutes=5 * i)).isoformat(),
        ))

    resp = await c.get(
        f"/api/v1/gpu/{srv_id}/0/history",
        params={"granularity": "5m", "limit": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # 取最近 2 桶,ASC 呈现 -> util 应为 3.0, 4.0
    assert body[0]["avg_util_pct"] == pytest.approx(3.0)
    assert body[1]["avg_util_pct"] == pytest.approx(4.0)
