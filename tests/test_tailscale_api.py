"""TASK-021: Tailscale REST API integration tests.

Covers:
- GET /api/tailscale/nodes          — returns all nodes, is_stale field present
- GET /api/tailscale/nodes?stale=true — only stale nodes returned
- GET /api/tailscale/nodes/{id}     — existing node 200; unknown id 404
- GET /api/tailscale/status         — never_run when no records; fields present
- GET /api/tailscale/status         — up/error when collector_run rows exist
- POST /api/tailscale/refresh       — modify_job called; triggered=True
- POST /api/tailscale/refresh       — job not found → triggered=False, no 5xx
- node_key whitelist                — node_key must NOT appear in any response
- empty table                       — GET /nodes returns []
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from panel.collectors import registry
from panel.config.settings import Settings
from panel.main import create_app

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_NOW = datetime.now(UTC)

# Fake node data matching TailscaleNodeRow fields
_NODE_A = {
    "id": 1,
    "node_key": "nodekey-secret-do-not-expose",
    "hostname": "muxrpi",
    "dns_name": "muxrpi.tail-abc.ts.net.",
    "tailscale_ips": ["100.64.0.1", "fd7a::1"],
    "os": "linux",
    "online_state": "ONLINE",
    "is_exit_node": False,
    "last_seen_at": None,
    # collected_at = fresh (30s ago → not stale with default 90s threshold)
    "collected_at": _NOW - timedelta(seconds=30),
    "updated_at": _NOW - timedelta(seconds=30),
}

_NODE_B = {
    "id": 2,
    "node_key": "nodekey-b-secret",
    "hostname": "ipad163",
    "dns_name": None,
    "tailscale_ips": ["100.64.0.2"],
    "os": "iOS",
    "online_state": "LONG_OFFLINE",
    "is_exit_node": False,
    "last_seen_at": _NOW - timedelta(hours=48),
    # collected_at = stale (120s ago > 90s threshold)
    "collected_at": _NOW - timedelta(seconds=120),
    "updated_at": _NOW - timedelta(seconds=120),
}


def _make_node_row(**kwargs):
    """Build a minimal TailscaleNodeRow-like object from a dict."""
    from dataclasses import dataclass

    @dataclass
    class FakeNodeRow:
        id: int
        node_key: str
        hostname: str
        dns_name: str | None
        tailscale_ips: list
        os: str | None
        online_state: str
        is_exit_node: bool
        last_seen_at: datetime | None
        collected_at: datetime
        updated_at: datetime

    return FakeNodeRow(**kwargs)


_ROW_A = _make_node_row(**_NODE_A)
_ROW_B = _make_node_row(**_NODE_B)


def _make_collector_run_row(status: str, error: str | None = None):
    """Return a minimal CollectorRunRow-like object."""
    from dataclasses import dataclass

    @dataclass
    class FakeRunRow:
        collector: str
        status: str
        sample_count: int
        duration_ms: int
        error: str | None
        ran_at: str

    return FakeRunRow(
        collector="tailscale",
        status=status,
        sample_count=5,
        duration_ms=42,
        error=error,
        ran_at=(_NOW - timedelta(seconds=10)).isoformat(),
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the global collector registry around each test."""
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
async def client(tmp_path: Path):
    """ASGI test client with isolated DB and lifespan."""
    settings = Settings(db_path=str(tmp_path / "test.db"))
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# --------------------------------------------------------------------------- #
# GET /api/tailscale/nodes — empty table
# --------------------------------------------------------------------------- #


async def test_list_nodes_empty(client: httpx.AsyncClient) -> None:
    """Empty tailscale_nodes table returns an empty list with HTTP 200."""
    resp = await client.get("/api/tailscale/nodes")
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# GET /api/tailscale/nodes — with mock data
# --------------------------------------------------------------------------- #


async def test_list_nodes_returns_all(client: httpx.AsyncClient) -> None:
    """GET /nodes returns all nodes with HTTP 200."""
    with patch("panel.api.tailscale.routes.get_repo") as mock_dep:
        mock_repo = MagicMock()
        mock_repo.get_all_nodes = MagicMock(return_value=_coro([_ROW_A, _ROW_B]))
        mock_dep.return_value = mock_repo

        # Bypass FastAPI DI by calling the view function directly to inspect results
        from panel.api.tailscale.routes import list_nodes

        mock_request = _make_request(client)
        result = await list_nodes(request=mock_request, stale=False, repo=mock_repo)
        assert len(result) == 2
        assert result[0].hostname == "muxrpi"
        assert result[1].hostname == "ipad163"


async def test_list_nodes_contains_is_stale_field(client: httpx.AsyncClient) -> None:
    """Every node in the response contains an is_stale field."""
    from panel.api.tailscale.routes import list_nodes

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_all_nodes = MagicMock(return_value=_coro([_ROW_A, _ROW_B]))
    result = await list_nodes(request=mock_request, stale=False, repo=mock_repo)
    assert len(result) == 2
    for node in result:
        assert hasattr(node, "is_stale")
        # is_stale must be a bool
        assert isinstance(node.is_stale, bool)


async def test_list_nodes_stale_flag_fresh_node_not_stale(client: httpx.AsyncClient) -> None:
    """Node collected 30s ago (threshold 90s) has is_stale=False."""
    from panel.api.tailscale.routes import list_nodes

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_all_nodes = MagicMock(return_value=_coro([_ROW_A]))
    result = await list_nodes(request=mock_request, stale=False, repo=mock_repo)
    assert result[0].is_stale is False


async def test_list_nodes_stale_flag_old_node_is_stale(client: httpx.AsyncClient) -> None:
    """Node collected 120s ago (threshold 90s) has is_stale=True."""
    from panel.api.tailscale.routes import list_nodes

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_all_nodes = MagicMock(return_value=_coro([_ROW_B]))
    result = await list_nodes(request=mock_request, stale=False, repo=mock_repo)
    assert result[0].is_stale is True


async def test_list_nodes_stale_filter_returns_only_stale(client: httpx.AsyncClient) -> None:
    """?stale=true returns only nodes where is_stale=True."""
    from panel.api.tailscale.routes import list_nodes

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_all_nodes = MagicMock(return_value=_coro([_ROW_A, _ROW_B]))
    result = await list_nodes(request=mock_request, stale=True, repo=mock_repo)
    # ROW_A is fresh (not stale), ROW_B is stale → only ROW_B returned
    assert len(result) == 1
    assert result[0].hostname == "ipad163"
    assert result[0].is_stale is True


async def test_list_nodes_no_node_key_in_response(client: httpx.AsyncClient) -> None:
    """node_key must NOT appear in any response from GET /nodes."""
    from panel.api.tailscale.routes import list_nodes

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_all_nodes = MagicMock(return_value=_coro([_ROW_A]))
    result = await list_nodes(request=mock_request, stale=False, repo=mock_repo)
    node_json = result[0].model_dump()
    assert "node_key" not in node_json


# --------------------------------------------------------------------------- #
# GET /api/tailscale/nodes/{node_id}
# --------------------------------------------------------------------------- #


async def test_get_node_returns_correct_node(client: httpx.AsyncClient) -> None:
    """GET /nodes/1 returns the correct node."""
    from panel.api.tailscale.routes import get_node

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_node_by_id = MagicMock(return_value=_coro(_ROW_A))
    result = await get_node(node_id=1, request=mock_request, repo=mock_repo)
    assert result.id == 1
    assert result.hostname == "muxrpi"
    assert result.online_state == "ONLINE"


async def test_get_node_no_node_key_in_response(client: httpx.AsyncClient) -> None:
    """node_key must NOT appear in GET /nodes/{id} response."""
    from panel.api.tailscale.routes import get_node

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_node_by_id = MagicMock(return_value=_coro(_ROW_A))
    result = await get_node(node_id=1, request=mock_request, repo=mock_repo)
    node_json = result.model_dump()
    assert "node_key" not in node_json


async def test_get_node_not_found_raises_404(client: httpx.AsyncClient) -> None:
    """GET /nodes/9999 returns 404."""
    from fastapi import HTTPException

    from panel.api.tailscale.routes import get_node

    mock_request = _make_request(client)
    mock_repo = MagicMock()
    mock_repo.get_node_by_id = MagicMock(return_value=_coro(None))
    with pytest.raises(HTTPException) as exc_info:
        await get_node(node_id=9999, request=mock_request, repo=mock_repo)
    assert exc_info.value.status_code == 404


# --------------------------------------------------------------------------- #
# GET /api/tailscale/status
# --------------------------------------------------------------------------- #


async def test_collector_status_never_run(client: httpx.AsyncClient) -> None:
    """GET /status returns never_run when no collector_run rows exist."""
    from panel.api.tailscale.routes import get_collector_status

    mock_repo = MagicMock()
    mock_repo.get_last_run = MagicMock(return_value=_coro(None))
    result = await get_collector_status(repo=mock_repo)
    assert result.status == "never_run"
    assert result.ran_at is None
    assert result.sample_count == 0
    assert result.duration_ms == 0
    assert result.error is None


async def test_collector_status_up(client: httpx.AsyncClient) -> None:
    """GET /status returns up when the last run was successful."""
    from panel.api.tailscale.routes import get_collector_status

    mock_repo = MagicMock()
    run_row = _make_collector_run_row("up")
    mock_repo.get_last_run = MagicMock(return_value=_coro(run_row))
    result = await get_collector_status(repo=mock_repo)
    assert result.status == "up"
    assert result.ran_at is not None
    assert result.sample_count == 5
    assert result.error is None


async def test_collector_status_error(client: httpx.AsyncClient) -> None:
    """GET /status returns error when the last run failed."""
    from panel.api.tailscale.routes import get_collector_status

    mock_repo = MagicMock()
    run_row = _make_collector_run_row("error", error="connection refused")
    mock_repo.get_last_run = MagicMock(return_value=_coro(run_row))
    result = await get_collector_status(repo=mock_repo)
    assert result.status == "error"
    assert result.error == "connection refused"


async def test_collector_status_fields_complete(client: httpx.AsyncClient) -> None:
    """GET /status response contains all required fields."""
    from panel.api.tailscale.routes import get_collector_status

    mock_repo = MagicMock()
    run_row = _make_collector_run_row("up")
    mock_repo.get_last_run = MagicMock(return_value=_coro(run_row))
    result = await get_collector_status(repo=mock_repo)
    fields = result.model_dump()
    assert set(fields) >= {"status", "ran_at", "sample_count", "duration_ms", "error"}


# --------------------------------------------------------------------------- #
# POST /api/tailscale/refresh
# --------------------------------------------------------------------------- #


async def test_refresh_triggers_when_job_exists(client: httpx.AsyncClient) -> None:
    """POST /refresh returns triggered=True when scheduler job exists."""
    from panel.api.tailscale.routes import manual_refresh

    mock_scheduler = MagicMock()
    mock_scheduler.modify_job = MagicMock()  # does not raise

    result = await manual_refresh(scheduler=mock_scheduler)
    assert result.triggered is True
    assert "tailscale" in result.message.lower() or "scheduled" in result.message.lower()
    mock_scheduler.modify_job.assert_called_once()


async def test_refresh_not_triggered_when_job_missing(client: httpx.AsyncClient) -> None:
    """POST /refresh returns triggered=False (not 500) when job is not found."""
    from apscheduler.jobstores.base import JobLookupError

    from panel.api.tailscale.routes import manual_refresh

    mock_scheduler = MagicMock()
    mock_scheduler.modify_job.side_effect = JobLookupError("tailscale")

    result = await manual_refresh(scheduler=mock_scheduler)
    assert result.triggered is False
    assert isinstance(result.message, str)


async def test_refresh_not_triggered_no_500(client: httpx.AsyncClient) -> None:
    """POST /refresh must return HTTP 200 even when job is missing (no 5xx)."""
    resp = await client.post("/api/tailscale/refresh")
    # Job is unlikely to be registered in test env; but endpoint must not 5xx
    assert resp.status_code == 200
    body = resp.json()
    assert "triggered" in body
    assert "message" in body


# --------------------------------------------------------------------------- #
# Integration-style: full ASGI round-trip for real endpoints
# --------------------------------------------------------------------------- #


async def test_nodes_endpoint_http200(client: httpx.AsyncClient) -> None:
    """Real ASGI round-trip: GET /api/tailscale/nodes returns 200 and a JSON array."""
    resp = await client.get("/api/tailscale/nodes")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_nodes_stale_filter_http200(client: httpx.AsyncClient) -> None:
    """Real ASGI round-trip: ?stale=true still returns 200."""
    resp = await client.get("/api/tailscale/nodes?stale=true")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_node_not_found_http404(client: httpx.AsyncClient) -> None:
    """Real ASGI round-trip: unknown id → 404."""
    resp = await client.get("/api/tailscale/nodes/9999")
    assert resp.status_code == 404


async def test_status_endpoint_http200(client: httpx.AsyncClient) -> None:
    """Real ASGI round-trip: GET /api/tailscale/status returns 200."""
    resp = await client.get("/api/tailscale/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "never_run"
    assert body["ran_at"] is None


async def test_refresh_endpoint_no_5xx(client: httpx.AsyncClient) -> None:
    """Real ASGI round-trip: POST /api/tailscale/refresh never returns 5xx."""
    resp = await client.post("/api/tailscale/refresh")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Private test helpers
# --------------------------------------------------------------------------- #


def _coro(value):
    """Return an awaitable that resolves to *value*."""

    async def _inner():
        return value

    return _inner()


def _make_request(client: httpx.AsyncClient):
    """Build a minimal mock Request with app.state.settings populated."""
    from unittest.mock import MagicMock

    mock_request = MagicMock()
    mock_request.app.state.settings = Settings(db_path=":memory:")
    return mock_request


async def _get_app_repo(client: httpx.AsyncClient):
    """Return the shared Repository from a running test app (not used directly)."""
    # Not actually called; kept for future use.
    return None
