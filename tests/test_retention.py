"""TASK-040: 通用 metric_history retention job 测试。

覆盖:
- prune_metric_history 删除旧行、保留新行,返回删除条数与实际一致。
- 边界:恰好等于截止时间(now - retention_days)的行不删(严格小于 before)。
- 空表 / 全为新行时返回 0,不报错。
- Repository.prune_history 直接以 before 参数工作(时间归一化)。

复用 test_db.py 风格:临时文件 DB(WAL 需文件)+ conn/repo fixture。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from panel.collectors.base import MetricSample
from panel.collectors.retention import prune_metric_history
from panel.db import connection, migrate
from panel.db.repository import Repository


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


async def _count(conn) -> int:
    async with conn.execute("SELECT COUNT(*) FROM metric_history") as cur:
        return (await cur.fetchone())[0]


# --------------------------------------------------------------------------- #
# prune_metric_history (collectors/retention.py)
# --------------------------------------------------------------------------- #


async def test_prune_deletes_old_keeps_new(repo, conn):
    now = datetime.now(UTC)
    # 3 条新行(now)、2 条旧行(now - 40 天),retention=30 天
    for _ in range(3):
        await repo.append_history("gpu", [_sample(collected_at=now)])
    for _ in range(2):
        await repo.append_history(
            "gpu", [_sample(collected_at=now - timedelta(days=40))]
        )
    assert await _count(conn) == 5

    deleted = await prune_metric_history(repo, 30)

    assert deleted == 2  # 仅两条旧行被删
    assert await _count(conn) == 3  # 新行保留


async def test_prune_returns_zero_on_empty_table(repo, conn):
    assert await _count(conn) == 0
    deleted = await prune_metric_history(repo, 30)
    assert deleted == 0
    assert await _count(conn) == 0


async def test_prune_returns_zero_when_all_new(repo, conn):
    now = datetime.now(UTC)
    for _ in range(4):
        await repo.append_history("gpu", [_sample(collected_at=now)])
    deleted = await prune_metric_history(repo, 30)
    assert deleted == 0
    assert await _count(conn) == 4


async def test_prune_respects_retention_days_boundary(repo, conn):
    now = datetime.now(UTC)
    # 一条在 retention 窗口内(25 天前),一条在窗口外(35 天前),retention=30
    await repo.append_history("gpu", [_sample(collected_at=now - timedelta(days=25))])
    await repo.append_history("gpu", [_sample(collected_at=now - timedelta(days=35))])

    deleted = await prune_metric_history(repo, 30)

    assert deleted == 1  # 仅窗口外那条被删
    assert await _count(conn) == 1


# --------------------------------------------------------------------------- #
# Repository.prune_history(before) — 直接调用 + 边界(严格小于 before)
# --------------------------------------------------------------------------- #


async def test_prune_history_strictly_less_than_before(repo, conn):
    cutoff = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    # 恰好等于 cutoff 的行:不删(严格 <)
    await repo.append_history("gpu", [_sample(collected_at=cutoff)])
    # 早于 cutoff 一秒:删
    await repo.append_history(
        "gpu", [_sample(collected_at=cutoff - timedelta(seconds=1))]
    )
    # 晚于 cutoff 一秒:保留
    await repo.append_history(
        "gpu", [_sample(collected_at=cutoff + timedelta(seconds=1))]
    )

    deleted = await repo.prune_history(cutoff)

    assert deleted == 1  # 仅早于 cutoff 的那条
    assert await _count(conn) == 2  # 等于 + 晚于 保留


async def test_prune_history_normalizes_naive_datetime(repo, conn):
    """before 为 naive datetime 时按 UTC 归一化(_iso),与库内 ISO8601 UTC 比较。"""
    base = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    await repo.append_history(
        "gpu", [_sample(collected_at=base - timedelta(days=1))]
    )
    await repo.append_history(
        "gpu", [_sample(collected_at=base + timedelta(days=1))]
    )

    # naive datetime(等同 UTC base)
    naive_cutoff = datetime(2026, 6, 1, 0, 0, 0)  # noqa: DTZ001 (故意 naive 测归一化)
    deleted = await repo.prune_history(naive_cutoff)

    assert deleted == 1
    assert await _count(conn) == 1
