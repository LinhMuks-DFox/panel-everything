"""TASK-014: GET /api/v1/dashboard/azure 集成测试.

覆盖:
- 200 OK，响应结构符合 DashboardAzureOut schema
- servers 表空时，返回 vms=[]
- 有 VM 但无采集记录时，is_stale=True，power_state="Unknown"
- collector_run 无记录时，collector_status["azure_vm"].status="unknown"
- collector_run 有失败记录时，collector_status["azure_vm"].status="down"，error 不含路径/密钥
- GPU stale 逻辑：collected_at 超过 180s 的记录 is_stale=True
- GPU 不超 180s 的记录 is_stale=False
- has_gpu=False 的服务器 gpus=[]
- has_gpu=True 且有 GPU 数据时 gpus 含对应卡
- fetched_at 为 UTC datetime（含 tz 信息）
- collector_status 中 azure_vm 和 gpu 两个 key 均存在
- collector_run 有成功记录时 status="up"、last_ran_at 非空
- VM 采集时间在阈值内时 is_stale=False
- VM 采集时间超阈值时 is_stale=True
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from panel.collectors import registry
from panel.config.settings import Settings
from panel.db import connection, migrate
from panel.db.gpu_repository import GpuRepository
from panel.db.repository import Repository
from panel.domain.models import ServerIn
from panel.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后清空进程级 collector 注册表。"""
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
async def client(tmp_path: Path):
    """带独立临时 DB 的 ASGI 测试客户端（每用例隔离）。"""
    settings = Settings(db_path=str(tmp_path / "test.db"))
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def db_repos(tmp_path: Path):
    """返回 (conn, Repository, GpuRepository) 三元组，供直接写库的用例使用。"""
    db_path = str(tmp_path / "direct.db")
    conn = await connection.connect(db_path)
    await migrate.run(conn)
    repo = Repository(conn)
    gpu_repo = GpuRepository(conn)
    yield conn, repo, gpu_repo
    await conn.close()


@pytest.fixture
async def client_with_db(tmp_path: Path):
    """返回 (client, repo, gpu_repo) 三元组，通过同一 DB 文件共享状态。

    先建库并返回 repo 对象（供测试直接写数据），再启动 app（只读同一 DB 文件）。
    """
    db_path = str(tmp_path / "shared.db")

    # Pre-create DB and expose raw repos for test setup
    conn_setup = await connection.connect(db_path)
    await migrate.run(conn_setup)
    repo_setup = Repository(conn_setup)
    gpu_repo_setup = GpuRepository(conn_setup)

    settings = Settings(db_path=db_path)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, repo_setup, gpu_repo_setup

    await conn_setup.close()


# ---------------------------------------------------------------------------
# Basic structure tests
# ---------------------------------------------------------------------------


async def test_dashboard_returns_200(client: httpx.AsyncClient) -> None:
    """GET /api/v1/dashboard/azure 应返回 HTTP 200。"""
    resp = await client.get("/api/v1/dashboard/azure")
    assert resp.status_code == 200


async def test_dashboard_schema_keys(client: httpx.AsyncClient) -> None:
    """响应体包含 fetched_at / collector_status / vms 三个顶层 key。"""
    resp = await client.get("/api/v1/dashboard/azure")
    body = resp.json()
    assert set(body.keys()) == {"fetched_at", "collector_status", "vms"}


async def test_dashboard_empty_servers_returns_empty_vms(client: httpx.AsyncClient) -> None:
    """servers 表为空时，vms=[]。"""
    resp = await client.get("/api/v1/dashboard/azure")
    assert resp.json()["vms"] == []


async def test_dashboard_collector_status_has_both_keys(client: httpx.AsyncClient) -> None:
    """collector_status 中 azure_vm 和 gpu 两个 key 始终存在。"""
    resp = await client.get("/api/v1/dashboard/azure")
    cs = resp.json()["collector_status"]
    assert "azure_vm" in cs
    assert "gpu" in cs


async def test_dashboard_fetched_at_is_utc(client: httpx.AsyncClient) -> None:
    """fetched_at 为带 tz（UTC）的 ISO8601 字符串，可解析且包含 +00:00。"""
    resp = await client.get("/api/v1/dashboard/azure")
    fetched_at_str = resp.json()["fetched_at"]
    dt = datetime.fromisoformat(fetched_at_str)
    # Pydantic serialises UTC as +00:00
    assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# collector_status logic
# ---------------------------------------------------------------------------


async def test_collector_status_unknown_when_no_run_records(client: httpx.AsyncClient) -> None:
    """collector_run 中 azure_vm 从未运行时，azure_vm 为 unknown。

    注意：gpu collector 始终注册且调度器 next_run_time=now，启动后立即执行
    首轮采集并落 collector_run，所以 gpu 可能已经有 up 记录。只断言 azure_vm
    因凭证缺失而始终 unknown，gpu 的状态由 scheduler 决定，不在此约束。
    """
    resp = await client.get("/api/v1/dashboard/azure")
    cs = resp.json()["collector_status"]
    assert cs["azure_vm"]["status"] == "unknown"
    assert cs["azure_vm"]["last_ran_at"] is None
    assert cs["azure_vm"]["error"] is None
    # gpu key must still be present
    assert "gpu" in cs


async def test_collector_status_up_after_successful_run(
    client_with_db: tuple,
) -> None:
    """collector_run 有成功记录时，status="up" 且 last_ran_at 非空。"""
    c, repo, _ = client_with_db
    from panel.collectors.base import CollectorResult

    result = CollectorResult(
        name="azure_vm",
        status="up",
        sample_count=3,
        duration_ms=500,
        error=None,
        ran_at=datetime.now(UTC),
    )
    await repo.record_collector_run(result)

    resp = await c.get("/api/v1/dashboard/azure")
    cs = resp.json()["collector_status"]
    assert cs["azure_vm"]["status"] == "up"
    assert cs["azure_vm"]["last_ran_at"] is not None
    assert cs["azure_vm"]["error"] is None


async def test_collector_status_down_after_failed_run(
    client_with_db: tuple,
) -> None:
    """collector_run 有 down 记录时，status="down"，error 不含明文密钥。"""
    c, repo, _ = client_with_db
    from panel.collectors.base import CollectorResult

    result = CollectorResult(
        name="azure_vm",
        status="down",
        sample_count=0,
        duration_ms=60000,
        error="timeout after 60 s",
        ran_at=datetime.now(UTC),
    )
    await repo.record_collector_run(result)

    resp = await c.get("/api/v1/dashboard/azure")
    cs = resp.json()["collector_status"]
    assert cs["azure_vm"]["status"] == "down"
    # Verify error does not contain absolute paths or raw credentials
    err = cs["azure_vm"]["error"] or ""
    assert "/secrets" not in err
    assert "password" not in err.lower()


async def test_collector_status_uses_latest_run(
    client_with_db: tuple,
) -> None:
    """多条 collector_run 时，返回最新一条（按 id 最大值）。"""
    c, repo, _ = client_with_db
    from panel.collectors.base import CollectorResult

    base_time = datetime.now(UTC)
    for i, s in enumerate(["down", "error", "up"]):
        r = CollectorResult(
            name="gpu",
            status=s,
            sample_count=i,
            duration_ms=100 * i,
            error=None,
            ran_at=base_time + timedelta(seconds=i),
        )
        await repo.record_collector_run(r)

    resp = await c.get("/api/v1/dashboard/azure")
    cs = resp.json()["collector_status"]
    assert cs["gpu"]["status"] == "up"


# ---------------------------------------------------------------------------
# VM status / stale logic
# ---------------------------------------------------------------------------


async def test_vm_never_collected_is_stale_unknown(
    client_with_db: tuple,
) -> None:
    """服务器已注册但无采集记录时，power_state="Unknown"，is_stale=True。"""
    c, _, gpu_repo = client_with_db
    srv = ServerIn(name="vm-no-data", azure_vm_name="vm-no-data", has_gpu=False)
    await gpu_repo.insert_server(srv)

    resp = await c.get("/api/v1/dashboard/azure")
    vms = resp.json()["vms"]
    assert len(vms) == 1
    vm = vms[0]
    assert vm["power_state"] == "Unknown"
    assert vm["is_stale"] is True
    assert vm["is_running"] is False
    assert vm["gpus"] == []


async def test_vm_fresh_collected_not_stale(
    client_with_db: tuple,
) -> None:
    """VM 采集时间在 600s 内时，is_stale=False。"""
    c, _, gpu_repo = client_with_db
    srv = ServerIn(name="vm-fresh", azure_vm_name="vm-fresh", has_gpu=False)
    srv_id = await gpu_repo.insert_server(srv)
    await gpu_repo.upsert_vm_status(
        server_id=srv_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=datetime.now(UTC) - timedelta(seconds=100),
    )

    resp = await c.get("/api/v1/dashboard/azure")
    vm = resp.json()["vms"][0]
    assert vm["is_stale"] is False
    assert vm["power_state"] == "Running"
    assert vm["is_running"] is True


async def test_vm_stale_after_threshold(
    client_with_db: tuple,
) -> None:
    """VM 采集时间超过 600s 时，is_stale=True。"""
    c, _, gpu_repo = client_with_db
    srv = ServerIn(name="vm-old", azure_vm_name="vm-old", has_gpu=False)
    srv_id = await gpu_repo.insert_server(srv)
    await gpu_repo.upsert_vm_status(
        server_id=srv_id,
        power_state="Deallocated",
        power_state_raw="PowerState/deallocated",
        is_running=False,
        collected_at=datetime.now(UTC) - timedelta(seconds=700),
    )

    resp = await c.get("/api/v1/dashboard/azure")
    vm = resp.json()["vms"][0]
    assert vm["is_stale"] is True
    assert vm["power_state"] == "Deallocated"


# ---------------------------------------------------------------------------
# GPU metrics / stale logic
# ---------------------------------------------------------------------------


async def test_no_gpu_server_has_empty_gpus(
    client_with_db: tuple,
) -> None:
    """has_gpu=False 的服务器 gpus=[]，即使 gpu_metrics 表中有残留数据也不返回。"""
    c, _, gpu_repo = client_with_db
    srv = ServerIn(name="cpu-only", azure_vm_name="cpu-vm", has_gpu=False)
    srv_id = await gpu_repo.insert_server(srv)
    await gpu_repo.upsert_vm_status(
        server_id=srv_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=datetime.now(UTC),
    )

    resp = await c.get("/api/v1/dashboard/azure")
    vm = resp.json()["vms"][0]
    assert vm["gpus"] == []


async def test_gpu_server_fresh_metrics_not_stale(
    client_with_db: tuple,
) -> None:
    """has_gpu=True 且 GPU 采集时间在 180s 内时，gpus 非空且 is_stale=False。"""
    c, _, gpu_repo = client_with_db
    from panel.db.gpu_repository import GpuSample

    srv = ServerIn(name="gpu-vm", azure_vm_name="gpu-vm", has_gpu=True)
    srv_id = await gpu_repo.insert_server(srv)
    await gpu_repo.upsert_vm_status(
        server_id=srv_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=datetime.now(UTC),
    )
    await gpu_repo.append_gpu_metrics([
        GpuSample(
            server_id=srv_id,
            gpu_index=0,
            gpu_name="NVIDIA A100-SXM4-80GB",
            util_pct=87.5,
            mem_used_mib=65536.0,
            mem_total_mib=81920.0,
            temp_c=72.0,
            power_w=380.0,
            status="ok",
            collected_at=datetime.now(UTC) - timedelta(seconds=30),
        )
    ])

    resp = await c.get("/api/v1/dashboard/azure")
    vm = resp.json()["vms"][0]
    assert len(vm["gpus"]) == 1
    gpu = vm["gpus"][0]
    assert gpu["gpu_index"] == 0
    assert gpu["gpu_name"] == "NVIDIA A100-SXM4-80GB"
    assert gpu["util_pct"] == pytest.approx(87.5)
    assert gpu["is_stale"] is False


async def test_gpu_server_stale_metrics(
    client_with_db: tuple,
) -> None:
    """GPU 采集时间超过 180s 时，gpus[0].is_stale=True。"""
    c, _, gpu_repo = client_with_db
    from panel.db.gpu_repository import GpuSample

    srv = ServerIn(name="gpu-stale", azure_vm_name="gpu-stale", has_gpu=True)
    srv_id = await gpu_repo.insert_server(srv)
    await gpu_repo.upsert_vm_status(
        server_id=srv_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=datetime.now(UTC),
    )
    await gpu_repo.append_gpu_metrics([
        GpuSample(
            server_id=srv_id,
            gpu_index=0,
            gpu_name="NVIDIA A100",
            util_pct=0.0,
            mem_used_mib=0.0,
            mem_total_mib=81920.0,
            temp_c=30.0,
            power_w=50.0,
            status="ok",
            collected_at=datetime.now(UTC) - timedelta(seconds=200),
        )
    ])

    resp = await c.get("/api/v1/dashboard/azure")
    gpu = resp.json()["vms"][0]["gpus"][0]
    assert gpu["is_stale"] is True


async def test_gpu_latest_only_returned(
    client_with_db: tuple,
) -> None:
    """多次采集时，gpus 只返回每卡最新一条（两卡各取最新）。"""
    c, _, gpu_repo = client_with_db
    from panel.db.gpu_repository import GpuSample

    srv = ServerIn(name="gpu-multi", azure_vm_name="gpu-multi", has_gpu=True)
    srv_id = await gpu_repo.insert_server(srv)
    await gpu_repo.upsert_vm_status(
        server_id=srv_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=datetime.now(UTC),
    )
    base = datetime.now(UTC) - timedelta(seconds=10)
    # Two rounds of collection; second round has higher util_pct
    for round_offset, util in [(60, 10.0), (0, 99.0)]:
        await gpu_repo.append_gpu_metrics([
            GpuSample(
                server_id=srv_id,
                gpu_index=i,
                gpu_name=f"GPU-{i}",
                util_pct=util,
                mem_used_mib=1024.0,
                mem_total_mib=81920.0,
                temp_c=40.0,
                power_w=100.0,
                status="ok",
                collected_at=base - timedelta(seconds=round_offset),
            )
            for i in range(2)
        ])

    resp = await c.get("/api/v1/dashboard/azure")
    gpus = resp.json()["vms"][0]["gpus"]
    assert len(gpus) == 2  # one entry per card
    # Both from the latest round (util=99.0)
    for g in gpus:
        assert g["util_pct"] == pytest.approx(99.0)


# ---------------------------------------------------------------------------
# vm_out field coverage
# ---------------------------------------------------------------------------


async def test_vm_out_fields(
    client_with_db: tuple,
) -> None:
    """DashboardVmOut 响应体包含所有规定字段（含 gpus）。"""
    c, _, gpu_repo = client_with_db
    srv = ServerIn(
        name="field-check",
        azure_vm_name="field-vm",
        azure_resource_group="lab-rg",
        has_gpu=False,
    )
    srv_id = await gpu_repo.insert_server(srv)
    await gpu_repo.upsert_vm_status(
        server_id=srv_id,
        power_state="Running",
        power_state_raw="PowerState/running",
        is_running=True,
        collected_at=datetime.now(UTC),
    )

    resp = await c.get("/api/v1/dashboard/azure")
    vm = resp.json()["vms"][0]
    expected_keys = {
        "server_id", "name", "azure_vm_name", "azure_resource_group",
        "power_state", "power_state_raw", "is_running",
        "collected_at", "is_stale", "gpus",
    }
    assert set(vm.keys()) == expected_keys
    assert vm["azure_resource_group"] == "lab-rg"
    assert vm["azure_vm_name"] == "field-vm"


async def test_dashboard_no_ssh_key_path_in_response(
    client_with_db: tuple,
) -> None:
    """DashboardAzureOut 响应体中绝对不能出现 ssh_key_path 字段（凭证白名单）。"""
    c, _, gpu_repo = client_with_db
    srv = ServerIn(name="sec-vm", ssh_key_path="/run/secrets/key", has_gpu=False)
    await gpu_repo.insert_server(srv)

    resp = await c.get("/api/v1/dashboard/azure")
    import json
    body_str = json.dumps(resp.json())
    assert "ssh_key_path" not in body_str
