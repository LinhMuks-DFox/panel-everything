"""TASK-001: /healthz 端点测试。

用 httpx.ASGITransport + AsyncClient 直接打 ASGI app(不起真实端口)。
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from panel.main import create_app


@pytest.fixture
def app():
    return create_app()


async def test_healthz_returns_200_and_schema(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()

    # schema: status / db / time 三字段齐全
    assert set(body) == {"status", "db", "time"}
    assert body["status"] == "ok"
    assert body["db"] in {"ok", "down"}

    # time 必须是可解析的 ISO8601(带 tz)
    parsed = datetime.fromisoformat(body["time"])
    assert parsed.tzinfo is not None


def test_create_app_is_factory():
    """create_app 每次返回独立实例,签名稳定供后续 Coder 复用。"""
    a = create_app()
    b = create_app()
    assert a is not b
