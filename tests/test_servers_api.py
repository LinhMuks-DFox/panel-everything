"""TASK-011: /api/v1/servers CRUD API 集成测试.

覆盖:
- POST /api/v1/servers — 注册成功、响应 201、响应体不含 ssh_key_path
- POST 重复 name — 409 Conflict
- GET  /api/v1/servers — 返回已注册列表，列表项不含 ssh_key_path
- DELETE /api/v1/servers/{id} — 已存在 id 返回 204
- DELETE 不存在 id — 404
- DELETE 后 GET 列表中该服务器消失
- DB 中 ssh_key_path 字段确实存储了提交的值（直接 SQL 查询验证）
- 非法输入（缺少必填字段 name）返回 422
- GET 空列表时返回空数组
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from panel.collectors import registry
from panel.config.settings import Settings
from panel.main import create_app

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后清空进程级 collector 注册表。

    client fixture 每用例都进入一次 app lifespan,而 lifespan 内
    register_collectors 会注册始终启用的 collector(如 gpu)。注册表是进程级
    全局字典,不在用例间复位会触发「collector already registered」。沿用
    test_collectors / test_azure_collector 的清理约定。
    """
    registry.clear()
    yield
    registry.clear()

_SERVER_PAYLOAD = {
    "name": "gpu-vm-01",
    "azure_resource_group": "lab-rg",
    "azure_vm_name": "gpu-vm-01",
    "ssh_host": "100.64.0.1",
    "ssh_port": 22,
    "ssh_user": "azureuser",
    "ssh_key_path": "/run/secrets/ssh_key_gpu01",
    "has_gpu": True,
    "notes": "4x A100 主力机",
}

_SERVER_OUT_KEYS = {
    "id",
    "name",
    "azure_resource_group",
    "azure_vm_name",
    "ssh_host",
    "ssh_port",
    "ssh_user",
    "has_gpu",
    "notes",
    "created_at",
    "updated_at",
}


@pytest.fixture
async def client(tmp_path: Path):
    """带独立临时 DB 的 ASGI 测试客户端（每测试用例隔离）。

    Uses app.router.lifespan_context to trigger the ASGI lifespan so that
    app.state.gpu_repo (and other state) is initialised before requests.
    """
    settings = Settings(db_path=str(tmp_path / "test.db"))
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# --------------------------------------------------------------------------- #
# POST /api/v1/servers
# --------------------------------------------------------------------------- #


async def test_create_server_returns_201(client: httpx.AsyncClient) -> None:
    """POST 成功注册返回 HTTP 201。"""
    resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    assert resp.status_code == 201


async def test_create_server_response_schema(client: httpx.AsyncClient) -> None:
    """POST 响应体字段集合与 ServerOut 一致（不含 ssh_key_path）。"""
    resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body.keys()) == _SERVER_OUT_KEYS


async def test_create_server_no_ssh_key_path_in_response(client: httpx.AsyncClient) -> None:
    """POST 响应体中绝对不能出现 ssh_key_path 字段。"""
    resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    assert "ssh_key_path" not in resp.json()


async def test_create_server_response_values(client: httpx.AsyncClient) -> None:
    """POST 响应体中各字段值与请求体一致。"""
    resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    body = resp.json()
    assert body["name"] == "gpu-vm-01"
    assert body["azure_resource_group"] == "lab-rg"
    assert body["ssh_host"] == "100.64.0.1"
    assert body["ssh_port"] == 22
    assert body["ssh_user"] == "azureuser"
    assert body["has_gpu"] is True
    assert body["notes"] == "4x A100 主力机"
    assert body["id"] >= 1
    assert body["created_at"] is not None
    assert body["updated_at"] is not None


async def test_create_server_duplicate_name_returns_409(client: httpx.AsyncClient) -> None:
    """重复 name 第二次 POST 应返回 409。"""
    await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


async def test_create_server_duplicate_response_no_ssh_key(client: httpx.AsyncClient) -> None:
    """409 错误响应中也不应含 ssh_key_path。"""
    await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    assert "ssh_key_path" not in resp.json()


async def test_create_server_missing_name_returns_422(client: httpx.AsyncClient) -> None:
    """缺少必填字段 name 应返回 422 Unprocessable Entity。"""
    payload = {k: v for k, v in _SERVER_PAYLOAD.items() if k != "name"}
    resp = await client.post("/api/v1/servers", json=payload)
    assert resp.status_code == 422


async def test_create_server_minimal_payload(client: httpx.AsyncClient) -> None:
    """只提供 name（其余可选字段省略）也能成功注册。"""
    resp = await client.post("/api/v1/servers", json={"name": "minimal-server"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "minimal-server"
    assert body["ssh_host"] is None
    assert body["has_gpu"] is False
    assert "ssh_key_path" not in body


# --------------------------------------------------------------------------- #
# GET /api/v1/servers
# --------------------------------------------------------------------------- #


async def test_list_servers_empty(client: httpx.AsyncClient) -> None:
    """空库时 GET 返回空数组。"""
    resp = await client.get("/api/v1/servers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_servers_returns_registered(client: httpx.AsyncClient) -> None:
    """POST 后 GET 列表中出现该服务器。"""
    await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    resp = await client.get("/api/v1/servers")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["name"] == "gpu-vm-01"


async def test_list_servers_no_ssh_key_path(client: httpx.AsyncClient) -> None:
    """GET 列表中每个元素均不含 ssh_key_path。"""
    await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    resp = await client.get("/api/v1/servers")
    for item in resp.json():
        assert "ssh_key_path" not in item


async def test_list_servers_multiple(client: httpx.AsyncClient) -> None:
    """注册多台服务器后列表按 id 升序返回所有记录。"""
    names = ["srv-alpha", "srv-beta", "srv-gamma"]
    for name in names:
        payload = {**_SERVER_PAYLOAD, "name": name}
        await client.post("/api/v1/servers", json=payload)

    resp = await client.get("/api/v1/servers")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 3
    assert [item["name"] for item in items] == names


async def test_list_servers_items_schema(client: httpx.AsyncClient) -> None:
    """GET 列表中每个元素的字段集合与 ServerOut 一致。"""
    await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    resp = await client.get("/api/v1/servers")
    for item in resp.json():
        assert set(item.keys()) == _SERVER_OUT_KEYS


# --------------------------------------------------------------------------- #
# DELETE /api/v1/servers/{id}
# --------------------------------------------------------------------------- #


async def test_delete_existing_server_returns_204(client: httpx.AsyncClient) -> None:
    """DELETE 已存在的 id 返回 204 No Content。"""
    create_resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    server_id = create_resp.json()["id"]
    resp = await client.delete(f"/api/v1/servers/{server_id}")
    assert resp.status_code == 204


async def test_delete_nonexistent_server_returns_404(client: httpx.AsyncClient) -> None:
    """DELETE 不存在的 id 返回 404。"""
    resp = await client.delete("/api/v1/servers/99999")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


async def test_delete_removes_from_list(client: httpx.AsyncClient) -> None:
    """DELETE 后 GET 列表中该服务器消失。"""
    create_resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    server_id = create_resp.json()["id"]

    await client.delete(f"/api/v1/servers/{server_id}")

    resp = await client.get("/api/v1/servers")
    names = [item["name"] for item in resp.json()]
    assert "gpu-vm-01" not in names


async def test_delete_only_removes_target(client: httpx.AsyncClient) -> None:
    """DELETE 一台不影响其他已注册服务器。"""
    await client.post("/api/v1/servers", json={**_SERVER_PAYLOAD, "name": "keep-me"})
    r2 = await client.post("/api/v1/servers", json={**_SERVER_PAYLOAD, "name": "delete-me"})
    id2 = r2.json()["id"]

    await client.delete(f"/api/v1/servers/{id2}")

    resp = await client.get("/api/v1/servers")
    names = [item["name"] for item in resp.json()]
    assert "keep-me" in names
    assert "delete-me" not in names


async def test_delete_twice_second_is_404(client: httpx.AsyncClient) -> None:
    """同一 id DELETE 两次,第二次返回 404。"""
    create_resp = await client.post("/api/v1/servers", json=_SERVER_PAYLOAD)
    server_id = create_resp.json()["id"]

    await client.delete(f"/api/v1/servers/{server_id}")
    resp = await client.delete(f"/api/v1/servers/{server_id}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# DB 凭证存储验证（直接 SQL）
# --------------------------------------------------------------------------- #


async def test_ssh_key_path_stored_in_db(tmp_path: Path) -> None:
    """ssh_key_path 在 DB servers 表中确实存储了提交的值（直接查询验证）。"""
    from panel.db import connection, migrate
    from panel.db.gpu_repository import GpuRepository
    from panel.domain.models import ServerIn

    db_path = str(tmp_path / "db_verify.db")
    conn = await connection.connect(db_path)
    await migrate.run(conn)
    repo = GpuRepository(conn)

    data = ServerIn(
        name="db-verify-server",
        ssh_key_path="/run/secrets/my_private_key",
    )
    await repo.insert_server(data)

    async with conn.execute(
        "SELECT ssh_key_path FROM servers WHERE name = ?", ("db-verify-server",)
    ) as cur:
        row = await cur.fetchone()

    await conn.close()

    assert row is not None
    assert row[0] == "/run/secrets/my_private_key"


# --------------------------------------------------------------------------- #
# /healthz 可在同进程访问（路由正确挂载验证）
# --------------------------------------------------------------------------- #


async def test_healthz_accessible_alongside_servers_api(client: httpx.AsyncClient) -> None:
    """/healthz 和 /api/v1/servers 均由同一 app 提供服务。"""
    resp_health = await client.get("/healthz")
    resp_servers = await client.get("/api/v1/servers")
    assert resp_health.status_code == 200
    assert resp_servers.status_code == 200
