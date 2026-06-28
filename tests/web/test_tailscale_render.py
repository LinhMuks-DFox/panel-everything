"""TASK-022: Tailscale NodeCard / NodeGrid / StaleWarning SSR rendering tests.

Coverage:
  1.  GET / contains #tailscale-section (data-module="tailscale")
  2.  ONLINE node → status-ok + ● symbol present in HTML
  3.  OFFLINE node → status-warn + ◐ symbol present in HTML
  4.  LONG_OFFLINE node → status-error + ○ symbol present in HTML
  5.  StaleWarning banner present when is_stale=True
  6.  StaleWarning banner absent when is_stale=False
  7.  Collector error banner rendered when collector_status="error"
  8.  Collector error banner absent when collector_status="up"
  9.  Empty node list: page returns 200 and node-grid container exists
  10. node-summary "X/Y 在线" counter matches injected node states
  11. index.html includes _node_grid.html partial (template file check)
  12. _node_grid.html partial file exists
  13. _node_card.html partial file exists
  14. ARCH-003 CSS section exists in panel.css
  15. ARCH-003 CSS section has no box-shadow (e-ink constraint)
  16. ARCH-003 CSS section has no animation / @keyframes (e-ink constraint)
  17. datetimeformat filter: None → "—", datetime → formatted UTC string
  18. datetimeformat filter: naive datetime treated as UTC
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from panel.main import create_app

# ---------------------------------------------------------------------------
# Paths to static assets (used in file-based tests)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent.parent.parent / "src" / "panel" / "web" / "static"
_CSS_PATH = _STATIC_DIR / "css" / "panel.css"
_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent / "src" / "panel" / "web" / "templates"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _make_node(
    *,
    node_id: int = 1,
    hostname: str = "muxrpi",
    tailscale_ips: list[str] | None = None,
    os: str | None = "linux",
    online_state: str = "ONLINE",
    is_exit_node: bool = False,
    last_seen: datetime | None = None,
    is_stale: bool = False,
) -> SimpleNamespace:
    """Build a node view-model exactly as routes.py produces for the template."""
    return SimpleNamespace(
        id=node_id,
        hostname=hostname,
        dns_name=None,
        tailscale_ips=tailscale_ips or ["100.64.0.1"],
        os=os,
        online_state=online_state,
        is_exit_node=is_exit_node,
        last_seen=last_seen,
        is_stale=is_stale,
        updated_at=_NOW,
    )


def _three_state_nodes() -> list[SimpleNamespace]:
    """One node per online_state for three-state tests."""
    return [
        _make_node(node_id=1, hostname="pi-online", online_state="ONLINE"),
        _make_node(node_id=2, hostname="ipad-offline", online_state="OFFLINE",
                   last_seen=_NOW - timedelta(hours=1)),
        _make_node(node_id=3, hostname="old-laptop", online_state="LONG_OFFLINE",
                   last_seen=_NOW - timedelta(days=30)),
    ]


def _make_app(
    nodes: list[SimpleNamespace] | None = None,
    *,
    collector_status: str = "up",
    collector_error: str | None = None,
    is_stale: bool = False,
    stale_seconds: int = 90,
) -> object:
    """Create a test app whose index route receives mocked Tailscale context.

    We patch the repo so that get_all_nodes / get_last_run return our data,
    and also stub out the Azure build to avoid a second layer of mocking.
    """
    app = create_app()

    _nodes = nodes if nodes is not None else []

    # Build a mock CollectorRunRow-like object
    if collector_status != "never_run":
        from dataclasses import dataclass

        @dataclass
        class _FakeRun:
            collector: str = "tailscale"
            status: str = collector_status
            sample_count: int = len(_nodes)
            duration_ms: int = 10
            error: str | None = collector_error
            ran_at: str = (_NOW - timedelta(seconds=10)).isoformat()

        last_run = _FakeRun()
    else:
        last_run = None

    # Build a FakeNodeRow that matches TailscaleNodeRow field layout
    # routes.py reads: id, hostname, dns_name, tailscale_ips, os, online_state,
    # is_exit_node, last_seen_at (→ renamed last_seen), collected_at, updated_at.
    # Provide them all so attribute access doesn't raise.
    fake_node_rows = []
    for n in _nodes:
        row = SimpleNamespace(
            id=n.id,
            node_key="key-secret",
            hostname=n.hostname,
            dns_name=n.dns_name,
            tailscale_ips=n.tailscale_ips,
            os=n.os,
            online_state=n.online_state,
            is_exit_node=n.is_exit_node,
            last_seen_at=n.last_seen,
            # collected_at: recent enough to not be stale (unless overridden)
            collected_at=_NOW - timedelta(seconds=30),
            updated_at=n.updated_at,
        )
        if is_stale:
            # Make collected_at old enough to exceed stale_seconds threshold
            row.collected_at = _NOW - timedelta(seconds=stale_seconds + 30)
        fake_node_rows.append(row)

    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    mock_repo.get_all_nodes = AsyncMock(return_value=fake_node_rows)
    mock_repo.get_last_run = AsyncMock(return_value=last_run)

    app.state.repo = mock_repo
    # gpu_repo must be None so build_azure_dashboard is skipped (no AzureDashboard)
    # avoiding an extra mock layer; azure section simply won't render.
    return app


async def _get(app: object, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ---------------------------------------------------------------------------
# 1. Section presence
# ---------------------------------------------------------------------------

async def test_index_contains_tailscale_section() -> None:
    """GET / HTML must contain data-module="tailscale" section."""
    app = _make_app()
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert 'data-module="tailscale"' in resp.text
    assert 'id="tailscale-section"' in resp.text


# ---------------------------------------------------------------------------
# 2–4. Three-state rendering
# ---------------------------------------------------------------------------

async def test_online_node_renders_status_ok_and_symbol() -> None:
    """ONLINE node produces status-ok class and ● symbol in HTML."""
    nodes = [_make_node(online_state="ONLINE")]
    app = _make_app(nodes=nodes)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    body = resp.text
    assert "status-ok" in body
    assert "●" in body


async def test_offline_node_renders_status_warn_and_symbol() -> None:
    """OFFLINE node produces status-warn class and ◐ symbol in HTML."""
    nodes = [_make_node(online_state="OFFLINE", last_seen=_NOW - timedelta(hours=1))]
    app = _make_app(nodes=nodes)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    body = resp.text
    assert "status-warn" in body
    assert "◐" in body


async def test_long_offline_node_renders_status_error_and_symbol() -> None:
    """LONG_OFFLINE node produces status-error class and ○ symbol in HTML."""
    nodes = [_make_node(online_state="LONG_OFFLINE",
                        last_seen=_NOW - timedelta(days=30))]
    app = _make_app(nodes=nodes)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    body = resp.text
    assert "status-error" in body
    assert "○" in body


async def test_three_state_nodes_all_symbols_present() -> None:
    """All three symbols ●◐○ appear when one node of each state is injected."""
    app = _make_app(nodes=_three_state_nodes())
    resp = await _get(app, "/")
    assert resp.status_code == 200
    body = resp.text
    assert "●" in body
    assert "◐" in body
    assert "○" in body
    # Each class
    assert "status-ok" in body
    assert "status-warn" in body
    assert "status-error" in body


# ---------------------------------------------------------------------------
# 5–6. StaleWarning banner
# ---------------------------------------------------------------------------

async def test_stale_banner_present_when_is_stale_true() -> None:
    """StaleWarning banner appears when is_stale=True (collector data too old)."""
    app = _make_app(nodes=[], is_stale=True, stale_seconds=90)
    # Patch the index route to force is_stale=True in the template context.
    # We set collector_status="up" but give a very old ran_at so stale triggers.
    from dataclasses import dataclass

    @dataclass
    class _OldRun:
        collector: str = "tailscale"
        status: str = "up"
        sample_count: int = 0
        duration_ms: int = 0
        error: str | None = None
        # 200 s ago > 90 s threshold → stale
        ran_at: str = (_NOW - timedelta(seconds=200)).isoformat()

    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    mock_repo.get_all_nodes = AsyncMock(return_value=[])
    mock_repo.get_last_run = AsyncMock(return_value=_OldRun())
    app = create_app()
    app.state.repo = mock_repo

    resp = await _get(app, "/")
    assert resp.status_code == 200
    body = resp.text
    assert "datasource-banner" in body
    # Stale text from _node_grid.html
    assert "未更新" in body or "数据已超过" in body


async def test_stale_banner_absent_when_is_stale_false() -> None:
    """StaleWarning banner is absent for a freshly-collected dataset."""
    nodes = _three_state_nodes()
    app = _make_app(nodes=nodes, is_stale=False)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    # The _node_grid.html stale banner block contains "数据已超过"; it should be absent.
    assert "数据已超过" not in resp.text


# ---------------------------------------------------------------------------
# 7–8. Collector error banner
# ---------------------------------------------------------------------------

async def test_collector_error_banner_when_status_error() -> None:
    """Collector error banner is shown when collector_status='error'."""
    app = _make_app(nodes=[], collector_status="error",
                    collector_error="connection refused")
    resp = await _get(app, "/")
    assert resp.status_code == 200
    body = resp.text
    assert "datasource-banner--issues" in body
    assert "Tailscale 数据源异常" in body


async def test_collector_error_banner_absent_when_status_up() -> None:
    """Collector error banner is absent when collector_status='up'."""
    app = _make_app(nodes=_three_state_nodes(), collector_status="up")
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert "Tailscale 数据源异常" not in resp.text


# ---------------------------------------------------------------------------
# 9. Empty node list
# ---------------------------------------------------------------------------

async def test_empty_node_list_returns_200_and_grid_exists() -> None:
    """nodes=[] produces HTTP 200; #node-grid container exists but is empty."""
    app = _make_app(nodes=[], collector_status="never_run")
    resp = await _get(app, "/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="node-grid"' in body
    # No node-card elements expected
    assert "node-card" not in body


# ---------------------------------------------------------------------------
# 10. Node summary counter
# ---------------------------------------------------------------------------

async def test_node_summary_counter_correct() -> None:
    """node-summary shows correct online/total count."""
    nodes = [
        _make_node(node_id=1, online_state="ONLINE"),
        _make_node(node_id=2, online_state="ONLINE"),
        _make_node(node_id=3, online_state="OFFLINE"),
    ]
    app = _make_app(nodes=nodes)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    # Expect "2/3 在线"
    assert "2/3" in resp.text
    assert "在线" in resp.text


# ---------------------------------------------------------------------------
# 11–13. Template file checks
# ---------------------------------------------------------------------------

def test_index_html_includes_node_grid_partial() -> None:
    """index.html must include partials/_node_grid.html."""
    content = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    assert "_node_grid.html" in content


def test_node_grid_partial_exists() -> None:
    """partials/_node_grid.html must exist."""
    assert (_TEMPLATES_DIR / "partials" / "_node_grid.html").exists()


def test_node_card_partial_exists() -> None:
    """partials/_node_card.html must exist."""
    assert (_TEMPLATES_DIR / "partials" / "_node_card.html").exists()


# ---------------------------------------------------------------------------
# 14–16. CSS ARCH-003 section checks
# ---------------------------------------------------------------------------

def _read_arch003_css_section() -> str:
    """Return the ARCH-003 CSS section from panel.css."""
    css = _CSS_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"ARCH-003:.*?Tailscale node grid.*?End ARCH-003",
        css,
        re.DOTALL,
    )
    return m.group(0) if m else ""


def test_css_arch003_section_exists() -> None:
    """panel.css must contain the ARCH-003 Tailscale node grid section."""
    assert _read_arch003_css_section(), "ARCH-003 section marker not found in panel.css"


def test_css_no_box_shadow_in_arch003_section() -> None:
    """ARCH-003 CSS section must have no box-shadow (e-ink constraint)."""
    section = _read_arch003_css_section()
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\bbox-shadow\s*:", no_comments) is None, (
        "ARCH-003 CSS must not use box-shadow (e-ink hard constraint)"
    )


def test_css_no_animation_in_arch003_section() -> None:
    """ARCH-003 CSS section must not define animation or @keyframes."""
    section = _read_arch003_css_section()
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\banimation\s*:", no_comments) is None, (
        "ARCH-003 CSS must not use animation property"
    )
    assert re.search(r"@keyframes\b", no_comments) is None, (
        "ARCH-003 CSS must not define @keyframes"
    )


# ---------------------------------------------------------------------------
# 17–18. datetimeformat filter unit tests
# ---------------------------------------------------------------------------

def test_datetimeformat_none_returns_dash() -> None:
    """datetimeformat(None) returns '—'."""
    from panel.web.routes import _datetimeformat

    assert _datetimeformat(None) == "—"


def test_datetimeformat_aware_datetime() -> None:
    """datetimeformat formats a tz-aware datetime as 'YYYY-MM-DD HH:MM UTC'."""
    from panel.web.routes import _datetimeformat

    dt = datetime(2026, 6, 28, 12, 34, 56, tzinfo=UTC)
    result = _datetimeformat(dt)
    assert "2026-06-28" in result
    assert "12:34" in result
    assert "UTC" in result


def test_datetimeformat_naive_datetime_treated_as_utc() -> None:
    """datetimeformat treats naive datetime as UTC."""
    from panel.web.routes import _datetimeformat

    dt_naive = datetime(2026, 6, 28, 9, 0, 0)  # no tzinfo
    result = _datetimeformat(dt_naive)
    assert "2026-06-28" in result
    assert "09:00" in result
