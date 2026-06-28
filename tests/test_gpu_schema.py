"""TASK-010: Azure/GPU 专用表 schema 测试.

覆盖:
- migrate.run() 后五张表及全部索引均存在
- 多次执行 migrate.run() 不报错(幂等性)
- servers.name UNIQUE 约束生效
- 向 servers 插入后删除,azure_vm_status / gpu_metrics 级联删除
- GpuRepository 基本读写:insert_server / get_all_servers / get_server / delete_server
- GpuRepository 读写 azure_vm_status:upsert_vm_status / get_vm_status / get_vm_status_all
- GpuRepository 读写 gpu_metrics:append_gpu_metrics / get_latest_gpu_metrics / get_gpu_history
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from panel.db import connection, migrate
from panel.db.gpu_repository import GpuRepository, GpuSample
from panel.domain.models import ServerIn

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
async def conn(tmp_path: Path):
    """独立文件 DB,每个测试用例隔离(WAL 需文件,不用 :memory:)."""
    db_path = str(tmp_path / "panel_test.db")
    c = await connection.connect(db_path)
    await migrate.run(c)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def repo(conn: aiosqlite.Connection) -> GpuRepository:
    return GpuRepository(conn)


def _server_in(
    name: str = "test-server",
    has_gpu: bool = False,
    ssh_host: str | None = "100.64.0.1",
) -> ServerIn:
    return ServerIn(
        name=name,
        azure_resource_group="lab-rg",
        azure_vm_name=name,
        ssh_host=ssh_host,
        ssh_port=22,
        ssh_user="azureuser",
        ssh_key_path="/run/secrets/ssh_key",
        has_gpu=has_gpu,
        notes="test note",
    )


def _gpu_sample(
    server_id: int = 1,
    gpu_index: int = 0,
    util_pct: float = 50.0,
    mem_used_mib: float = 10240.0,
    mem_total_mib: float = 81920.0,
    collected_at: datetime | None = None,
    status: str = "ok",
) -> GpuSample:
    return GpuSample(
        server_id=server_id,
        gpu_index=gpu_index,
        gpu_name="NVIDIA A100-SXM4-80GB",
        util_pct=util_pct,
        mem_used_mib=mem_used_mib,
        mem_total_mib=mem_total_mib,
        temp_c=72.0,
        power_w=380.0,
        status=status,
        collected_at=collected_at or datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# schema 存在性 + 幂等性
# --------------------------------------------------------------------------- #


async def test_all_five_tables_exist(conn: aiosqlite.Connection) -> None:
    """migrate.run() 后五张专用表应全部存在。"""
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        names = {r[0] async for r in cur}

    expected = {
        "servers",
        "azure_vm_status",
        "gpu_metrics",
        "gpu_metrics_5m",
        "gpu_metrics_1h",
    }
    assert expected <= names, f"缺少表: {expected - names}"


async def test_all_indexes_exist(conn: aiosqlite.Connection) -> None:
    """migrate.run() 后全部 ARCH-002 索引应存在。"""
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    ) as cur:
        names = {r[0] async for r in cur}

    expected_indexes = {
        "idx_servers_name",
        "idx_gpu_metrics_query",
        "idx_gpu_metrics_server_latest",
        "idx_gpu_5m_bucket",
        "idx_gpu_1h_bucket",
        # MS-005 评审修复 (#7/#8):collected_at 前导列索引,支撑 retention/降采样
        "idx_gpu_metrics_collected",
        "idx_history_collected",
    }
    assert expected_indexes <= names, f"缺少索引: {expected_indexes - names}"


async def test_collected_at_indexes_serve_range_seek(
    conn: aiosqlite.Connection,
) -> None:
    """新增 collected_at 索引让时间范围查询走 SEARCH 而非全表 SCAN(MS-005 #7/#8)。"""
    async def _plan(sql: str) -> str:
        async with conn.execute(sql, ("2026-01-01T00:00:00+00:00",)) as cur:
            rows = [r async for r in cur]
        return " ".join(str(r[-1]) for r in rows)

    # gpu_metrics 的 48h 边界删除应命中 idx_gpu_metrics_collected
    plan = await _plan(
        "EXPLAIN QUERY PLAN DELETE FROM gpu_metrics WHERE collected_at < ?"
    )
    assert "idx_gpu_metrics_collected" in plan

    # metric_history 的每日 retention 删除应命中 idx_history_collected
    plan = await _plan(
        "EXPLAIN QUERY PLAN DELETE FROM metric_history WHERE collected_at < ?"
    )
    assert "idx_history_collected" in plan


async def test_migrate_idempotent(conn: aiosqlite.Connection) -> None:
    """多次执行 migrate.run() 不应报错。"""
    # 已在 fixture 中运行过一次,再运行两次
    await migrate.run(conn)
    await migrate.run(conn)

    async with conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ) as cur:
        count = (await cur.fetchone())[0]

    # 至少 8 张表(3 通用 + 5 专用),幂等不应增加重复表
    assert count >= 8


async def test_arch001_tables_still_exist_after_migration(conn: aiosqlite.Connection) -> None:
    """追加 ARCH-002 DDL 后,ARCH-001 三张通用表不受影响。"""
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        names = {r[0] async for r in cur}

    assert {"latest_snapshot", "metric_history", "collector_run"} <= names


# --------------------------------------------------------------------------- #
# servers CRUD
# --------------------------------------------------------------------------- #


async def test_insert_and_get_server(repo: GpuRepository) -> None:
    """插入服务器后可通过 get_server 读回,字段对齐。"""
    server_id = await repo.insert_server(_server_in("gpu-vm-01", has_gpu=True))
    assert server_id >= 1

    row = await repo.get_server(server_id)
    assert row is not None
    assert row.name == "gpu-vm-01"
    assert row.has_gpu is True
    assert row.ssh_key_path == "/run/secrets/ssh_key"  # 内部保留路径引用
    assert row.ssh_user == "azureuser"
    assert row.created_at != ""
    assert row.updated_at != ""


async def test_get_all_servers_empty(repo: GpuRepository) -> None:
    """空表返回空列表。"""
    rows = await repo.get_all_servers()
    assert rows == []


async def test_get_all_servers_multiple(repo: GpuRepository) -> None:
    """多条记录按 id 升序返回。"""
    await repo.insert_server(_server_in("srv-a"))
    await repo.insert_server(_server_in("srv-b"))
    rows = await repo.get_all_servers()
    assert len(rows) == 2
    assert [r.name for r in rows] == ["srv-a", "srv-b"]


async def test_get_server_not_found(repo: GpuRepository) -> None:
    """不存在的 id 返回 None。"""
    assert await repo.get_server(9999) is None


async def test_insert_server_duplicate_name_raises(repo: GpuRepository) -> None:
    """重复 name 应触发 IntegrityError(UNIQUE 约束)。"""
    await repo.insert_server(_server_in("dup-server"))
    with pytest.raises(aiosqlite.IntegrityError):
        await repo.insert_server(_server_in("dup-server"))


async def test_delete_server_returns_true(repo: GpuRepository) -> None:
    """删除存在的记录返回 True。"""
    server_id = await repo.insert_server(_server_in("to-delete"))
    result = await repo.delete_server(server_id)
    assert result is True


async def test_delete_server_not_found_returns_false(repo: GpuRepository) -> None:
    """删除不存在的 id 返回 False。"""
    result = await repo.delete_server(99999)
    assert result is False


async def test_delete_server_removes_row(repo: GpuRepository) -> None:
    """删除后 get_server 返回 None。"""
    server_id = await repo.insert_server(_server_in("will-go"))
    await repo.delete_server(server_id)
    assert await repo.get_server(server_id) is None


# --------------------------------------------------------------------------- #
# 级联删除
# --------------------------------------------------------------------------- #


async def test_cascade_delete_azure_vm_status(
    repo: GpuRepository,
    conn: aiosqlite.Connection,
) -> None:
    """删除 server 后,azure_vm_status 中关联行被级联删除。"""
    server_id = await repo.insert_server(_server_in("vm-cascade"))
    await repo.upsert_vm_status(
        server_id=server_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=datetime.now(UTC),
    )

    async with conn.execute(
        "SELECT COUNT(*) FROM azure_vm_status WHERE server_id = ?", (server_id,)
    ) as cur:
        before = (await cur.fetchone())[0]
    assert before == 1

    await repo.delete_server(server_id)

    async with conn.execute(
        "SELECT COUNT(*) FROM azure_vm_status WHERE server_id = ?", (server_id,)
    ) as cur:
        after = (await cur.fetchone())[0]
    assert after == 0


async def test_cascade_delete_gpu_metrics(
    repo: GpuRepository,
    conn: aiosqlite.Connection,
) -> None:
    """删除 server 后,gpu_metrics 中关联行被级联删除。"""
    server_id = await repo.insert_server(_server_in("gpu-cascade", has_gpu=True))
    await repo.append_gpu_metrics([_gpu_sample(server_id=server_id)])

    async with conn.execute(
        "SELECT COUNT(*) FROM gpu_metrics WHERE server_id = ?", (server_id,)
    ) as cur:
        before = (await cur.fetchone())[0]
    assert before == 1

    await repo.delete_server(server_id)

    async with conn.execute(
        "SELECT COUNT(*) FROM gpu_metrics WHERE server_id = ?", (server_id,)
    ) as cur:
        after = (await cur.fetchone())[0]
    assert after == 0


# --------------------------------------------------------------------------- #
# azure_vm_status 读写
# --------------------------------------------------------------------------- #


async def test_upsert_vm_status_insert_then_update(repo: GpuRepository) -> None:
    """同一 server_id 第二次 upsert 更新字段,不新增行。"""
    server_id = await repo.insert_server(_server_in("vm-upsert"))
    t = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

    await repo.upsert_vm_status(
        server_id=server_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=t,
    )

    row = await repo.get_vm_status(server_id)
    assert row is not None
    assert row.power_state == "Running"
    assert row.is_running is True

    # 更新为 deallocated
    t2 = t + timedelta(minutes=5)
    await repo.upsert_vm_status(
        server_id=server_id,
        power_state="Deallocated",
        power_state_raw="PowerState/deallocated",
        is_running=False,
        collected_at=t2,
    )

    row2 = await repo.get_vm_status(server_id)
    assert row2 is not None
    assert row2.power_state == "Deallocated"
    assert row2.is_running is False


async def test_get_vm_status_not_found(repo: GpuRepository) -> None:
    """不存在的 server_id 返回 None。"""
    assert await repo.get_vm_status(9999) is None


async def test_get_vm_status_all(repo: GpuRepository) -> None:
    """get_vm_status_all 返回所有 VM 状态。"""
    s1 = await repo.insert_server(_server_in("vm-all-a"))
    s2 = await repo.insert_server(_server_in("vm-all-b"))
    t = datetime.now(UTC)

    await repo.upsert_vm_status(s1, "Running", "PowerState/running", True, t)
    await repo.upsert_vm_status(s2, "Stopped", "PowerState/stopped", False, t)

    rows = await repo.get_vm_status_all()
    assert len(rows) == 2
    server_ids = {r.server_id for r in rows}
    assert server_ids == {s1, s2}


# --------------------------------------------------------------------------- #
# gpu_metrics 读写
# --------------------------------------------------------------------------- #


async def test_append_gpu_metrics_inserts_rows(
    repo: GpuRepository,
    conn: aiosqlite.Connection,
) -> None:
    """append_gpu_metrics 批量写入后行数应正确。"""
    server_id = await repo.insert_server(_server_in("gpu-write", has_gpu=True))
    t = datetime.now(UTC)
    samples = [_gpu_sample(server_id=server_id, gpu_index=i, collected_at=t) for i in range(4)]
    await repo.append_gpu_metrics(samples)

    async with conn.execute(
        "SELECT COUNT(*) FROM gpu_metrics WHERE server_id = ?", (server_id,)
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 4


async def test_append_gpu_metrics_empty_noop(
    repo: GpuRepository,
    conn: aiosqlite.Connection,
) -> None:
    """空列表不写库。"""
    await repo.append_gpu_metrics([])
    async with conn.execute("SELECT COUNT(*) FROM gpu_metrics") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_append_gpu_metrics_calculates_mem_pct(
    repo: GpuRepository,
    conn: aiosqlite.Connection,
) -> None:
    """append_gpu_metrics 自动计算 mem_pct = mem_used/mem_total*100。"""
    server_id = await repo.insert_server(_server_in("gpu-mem-pct", has_gpu=True))
    sample = _gpu_sample(
        server_id=server_id,
        mem_used_mib=40960.0,
        mem_total_mib=81920.0,
    )
    await repo.append_gpu_metrics([sample])

    async with conn.execute(
        "SELECT mem_pct FROM gpu_metrics WHERE server_id = ?", (server_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert abs(row[0] - 50.0) < 0.01


async def test_get_latest_gpu_metrics_one_per_card(repo: GpuRepository) -> None:
    """get_latest_gpu_metrics 每张卡只返回最新一行。"""
    server_id = await repo.insert_server(_server_in("gpu-latest", has_gpu=True))
    t1 = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 28, 11, 0, 0, tzinfo=UTC)

    # 写两张卡,每张写两次
    for t in [t1, t2]:
        for idx in [0, 1]:
            await repo.append_gpu_metrics([
                _gpu_sample(server_id=server_id, gpu_index=idx,
                            util_pct=float(idx * 10 + (1 if t == t2 else 0)),
                            collected_at=t)
            ])

    rows = await repo.get_latest_gpu_metrics(server_id)
    assert len(rows) == 2  # 只返回每张卡最新行
    assert [r.gpu_index for r in rows] == [0, 1]
    # t2 时刻写入的数据应被返回
    assert rows[0].collected_at.startswith("2026-06-28T11") or "11:00" in rows[0].collected_at


async def test_get_latest_gpu_metrics_empty(repo: GpuRepository) -> None:
    """无数据时返回空列表。"""
    server_id = await repo.insert_server(_server_in("gpu-empty", has_gpu=True))
    rows = await repo.get_latest_gpu_metrics(server_id)
    assert rows == []


async def test_get_gpu_history_range(repo: GpuRepository) -> None:
    """get_gpu_history 按时间范围过滤,返回升序结果。"""
    server_id = await repo.insert_server(_server_in("gpu-hist", has_gpu=True))
    base = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

    # 写 5 个点,间隔 1 分钟
    for i in range(5):
        await repo.append_gpu_metrics([
            _gpu_sample(server_id=server_id, gpu_index=0,
                        util_pct=float(i * 10),
                        collected_at=base + timedelta(minutes=i))
        ])

    # 区间 [t1, t3] 取 i=1,2,3
    rows = await repo.get_gpu_history(
        server_id=server_id,
        gpu_index=0,
        since=base + timedelta(minutes=1),
        until=base + timedelta(minutes=3),
    )
    assert len(rows) == 3
    assert [r.util_pct for r in rows] == [10.0, 20.0, 30.0]


async def test_get_gpu_history_limit(repo: GpuRepository) -> None:
    """limit 截取最近 N 条,返回仍升序。"""
    server_id = await repo.insert_server(_server_in("gpu-hist-limit", has_gpu=True))
    base = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

    for i in range(5):
        await repo.append_gpu_metrics([
            _gpu_sample(server_id=server_id, gpu_index=0,
                        util_pct=float(i * 10),
                        collected_at=base + timedelta(minutes=i))
        ])

    # limit=2 取最近 2 条 -> i=3,4 按升序
    rows = await repo.get_gpu_history(server_id=server_id, gpu_index=0,
                                       since=base, limit=2)
    assert len(rows) == 2
    assert [r.util_pct for r in rows] == [30.0, 40.0]


async def test_get_gpu_history_filters_by_gpu_index(repo: GpuRepository) -> None:
    """get_gpu_history 只返回指定 gpu_index 的数据。"""
    server_id = await repo.insert_server(_server_in("gpu-hist-idx", has_gpu=True))
    base = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

    # gpu_index=0 和 gpu_index=1 各写一条
    await repo.append_gpu_metrics([
        _gpu_sample(server_id=server_id, gpu_index=0, util_pct=10.0, collected_at=base),
        _gpu_sample(server_id=server_id, gpu_index=1, util_pct=90.0, collected_at=base),
    ])

    rows = await repo.get_gpu_history(server_id=server_id, gpu_index=0, since=base)
    assert len(rows) == 1
    assert rows[0].gpu_index == 0
    assert rows[0].util_pct == 10.0


# --------------------------------------------------------------------------- #
# 降采样表仅验证建表即可(填充逻辑属 TASK-016)
# --------------------------------------------------------------------------- #


async def test_downsample_tables_accept_insert(
    conn: aiosqlite.Connection,
) -> None:
    """gpu_metrics_5m 和 gpu_metrics_1h 表结构完整,可正常插入数据。"""
    # 需要先插入 server 以满足 FK 约束
    await conn.execute(
        """
        INSERT INTO servers (name, ssh_user, has_gpu, created_at, updated_at)
        VALUES ('ds-test', 'azureuser', 1, '2026-06-28T00:00:00+00:00', '2026-06-28T00:00:00+00:00')
        """
    )
    await conn.commit()

    async with conn.execute("SELECT id FROM servers WHERE name='ds-test'") as cur:
        server_id = (await cur.fetchone())[0]

    # 写入 5m 降采样行
    await conn.execute(
        """
        INSERT INTO gpu_metrics_5m
            (server_id, gpu_index, avg_util_pct, avg_mem_pct, max_temp_c,
             max_power_w, sample_count, bucket_start)
        VALUES (?, 0, 55.5, 62.0, 78.0, 390.0, 5, '2026-06-28T12:00:00+00:00')
        """,
        (server_id,),
    )
    # 写入 1h 降采样行
    await conn.execute(
        """
        INSERT INTO gpu_metrics_1h
            (server_id, gpu_index, avg_util_pct, avg_mem_pct, max_temp_c,
             max_power_w, sample_count, bucket_start)
        VALUES (?, 0, 55.5, 62.0, 78.0, 390.0, 60, '2026-06-28T12:00:00+00:00')
        """,
        (server_id,),
    )
    await conn.commit()

    async with conn.execute("SELECT COUNT(*) FROM gpu_metrics_5m") as cur:
        assert (await cur.fetchone())[0] == 1

    async with conn.execute("SELECT COUNT(*) FROM gpu_metrics_1h") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_downsample_unique_constraint(
    conn: aiosqlite.Connection,
) -> None:
    """降采样表的 UNIQUE INDEX (server_id, gpu_index, bucket_start) 生效。"""
    await conn.execute(
        """
        INSERT INTO servers (name, ssh_user, has_gpu, created_at, updated_at)
        VALUES ('ds-unique', 'azureuser', 1,
                '2026-06-28T00:00:00+00:00', '2026-06-28T00:00:00+00:00')
        """
    )
    await conn.commit()

    async with conn.execute("SELECT id FROM servers WHERE name='ds-unique'") as cur:
        server_id = (await cur.fetchone())[0]

    bucket = "2026-06-28T12:00:00+00:00"
    await conn.execute(
        """
        INSERT INTO gpu_metrics_5m
            (server_id, gpu_index, avg_util_pct, avg_mem_pct, max_temp_c,
             max_power_w, sample_count, bucket_start)
        VALUES (?, 0, 50.0, 50.0, 70.0, 350.0, 5, ?)
        """,
        (server_id, bucket),
    )
    await conn.commit()

    # 相同 (server_id, gpu_index, bucket_start) 再插入应失败
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            """
            INSERT INTO gpu_metrics_5m
                (server_id, gpu_index, avg_util_pct, avg_mem_pct, max_temp_c,
                 max_power_w, sample_count, bucket_start)
            VALUES (?, 0, 60.0, 60.0, 75.0, 360.0, 5, ?)
            """,
            (server_id, bucket),
        )
        await conn.commit()
