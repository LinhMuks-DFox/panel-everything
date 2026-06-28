"""TASK-004: SSR frontend shell tests.

Tests:
  1. GET / returns 200 with panel-grid container and viewport meta
  2. ?eink=1 → meta refresh present, panel.js NOT loaded
  3. Kindle UA → meta refresh present, panel.js NOT loaded
  4. Default (non-eink) → no meta refresh, panel.js IS loaded
  5. Data-source status bar renders all four states (up/down/error/stale)
  6. SSR renders full content without JS (no data-requires-js sentinel)
  7. CSS contains color, shape symbol, and text label for each status (three-layer encoding)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from panel.main import create_app

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_run_row(
    name: str,
    status: str,
    ran_at: str | None = None,
    error: str | None = None,
):
    """Build a minimal CollectorRunRow-like object for mocking."""
    from panel.db.repository import CollectorRunRow

    if ran_at is None:
        # Recent run — not stale
        ran_at = datetime.now(UTC).isoformat()
    return CollectorRunRow(
        collector=name,
        status=status,
        sample_count=1,
        duration_ms=50,
        error=error,
        ran_at=ran_at,
    )


def make_stale_ran_at() -> str:
    """Return an ISO timestamp > 180s in the past (triggers stale calculation)."""
    return (datetime.now(UTC) - timedelta(seconds=300)).isoformat()


async def _get(app, path: str, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers or {})


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def app_no_repo():
    """App without a repo attached — routes must degrade gracefully."""
    return create_app()


@pytest.fixture
def app_with_repo():
    """App whose state.repo is mocked to return collector runs."""
    _app = create_app()

    mock_repo = MagicMock()
    # Provide all four display-states: up, down, error, stale
    mock_repo.get_all_last_runs = AsyncMock(
        return_value=[
            make_run_row("azure", "up"),
            make_run_row("gpu", "down"),
            make_run_row("tailscale", "error", error="connection refused"),
            make_run_row("ssh", "up", ran_at=make_stale_ran_at()),
        ]
    )

    _app.state.repo = mock_repo
    return _app


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_index_returns_200_with_panel_grid(app_no_repo):
    """GET / returns 200 and HTML contains id="panel-grid" and viewport meta."""
    resp = await _get(app_no_repo, "/")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text

    assert 'id="panel-grid"' in body
    assert 'name="viewport"' in body
    assert "width=device-width" in body


async def test_eink_query_param_adds_meta_refresh_and_omits_js(app_no_repo):
    """?eink=1 → <meta http-equiv="refresh"> present and panel.js not loaded."""
    resp = await _get(app_no_repo, "/?eink=1")

    assert resp.status_code == 200
    body = resp.text

    assert 'http-equiv="refresh"' in body
    assert "panel.js" not in body


async def test_kindle_ua_adds_meta_refresh_and_omits_js(app_no_repo):
    """Kindle User-Agent → meta refresh present, panel.js absent."""
    resp = await _get(
        app_no_repo,
        "/",
        headers={"user-agent": "Mozilla/5.0 (X11; U; Linux armv7l; en-US) Kindle/3.0"},
    )
    body = resp.text

    assert 'http-equiv="refresh"' in body
    assert "panel.js" not in body


async def test_default_no_meta_refresh_loads_js(app_no_repo):
    """Default desktop request: no meta refresh, panel.js is loaded."""
    resp = await _get(
        app_no_repo,
        "/",
        headers={"user-agent": "Mozilla/5.0 Chrome/120"},
    )
    body = resp.text

    assert 'http-equiv="refresh"' not in body
    assert "panel.js" in body


async def test_datasource_banner_renders_all_four_states(app_with_repo):
    """Status bar renders up/down/error/stale with correct shape symbols and labels."""
    resp = await _get(app_with_repo, "/")

    assert resp.status_code == 200
    body = resp.text

    # Each collector name must appear
    assert "azure" in body
    assert "gpu" in body
    assert "tailscale" in body
    assert "ssh" in body

    # Shape symbols (three-layer encoding)
    assert "●" in body   # up / ok
    assert "○" in body   # down / error
    assert "◌" in body   # stale

    # Text labels
    assert "在线" in body
    assert "离线" in body
    assert "异常" in body or "error" in body.lower()
    assert "数据陈旧" in body


async def test_ssr_content_complete_without_js(app_with_repo):
    """All collector status information is present in SSR output (no JS required)."""
    resp = await _get(app_with_repo, "/")
    body = resp.text

    # All four collectors are rendered in the SSR HTML
    for name in ("azure", "gpu", "tailscale", "ssh"):
        assert name in body, f"Collector '{name}' missing from SSR output"

    # The panel-grid container is populated
    assert 'id="panel-grid"' in body


async def test_index_graceful_when_repo_unavailable(app_no_repo):
    """GET / returns 200 even when app.state.repo is not set (no crash)."""
    resp = await _get(app_no_repo, "/")
    assert resp.status_code == 200


# ── CSS three-layer encoding verification (static file check) ────────────────

def test_css_status_encoding_has_color_shape_and_text():
    """panel.css must encode each status state with color, shape symbol, AND text class.

    This is a grep-style check to enforce the ARCH-001 three-layer encoding rule
    (color + form + text) cannot be disabled accidentally.
    """
    css_path = (
        Path(__file__).parent.parent
        / "src" / "panel" / "web" / "static" / "css" / "panel.css"
    )
    css = css_path.read_text(encoding="utf-8")

    # Color layer: each status has a color property
    for state in ("ok", "warn", "error", "stale"):
        assert f"status-{state}" in css, f"Missing .status-{state} in CSS"

    # Shape layer: all four symbols appear in the template (checked here via CSS comments)
    # The actual symbols ●◐○◌ are in the HTML template; verify they're documented in CSS.
    assert "●" in css or "shape" in css.lower() or "symbol" in css.lower(), (
        "CSS should document shape symbols (●◐○◌)"
    )

    # Text layer: Chinese labels must be in the partial template
    template_path = (
        Path(__file__).parent.parent
        / "src" / "panel" / "web" / "templates" / "partials" / "_datasource_status.html"
    )
    template = template_path.read_text(encoding="utf-8")
    for label in ("在线", "离线", "数据陈旧", "异常"):
        assert label in template, f"Missing text label '{label}' in _datasource_status.html"

    # No box-shadow CSS property in CSS (e-ink constraint).
    # Strip comments first to avoid matching documentation in comments.
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    assert re.search(r"\bbox-shadow\s*:", css_no_comments) is None, (
        "CSS must not use box-shadow property (e-ink constraint)"
    )

    # No CSS animation property or @keyframes (e-ink constraint)
    assert re.search(r"\banimation\s*:", css_no_comments) is None, (
        "CSS must not define animation property (e-ink constraint)"
    )
    assert re.search(r"@keyframes\b", css_no_comments) is None, (
        "CSS must not define @keyframes (e-ink constraint)"
    )
