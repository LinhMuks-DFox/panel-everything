"""TASK-033: AI 额度卡片测试 (ARCH-004).

覆盖两层：

API 聚合 (api/ai_usage.get_ai_usage_data via GET /api/ai-usage):
  - test_get_ai_usage_api_ok        : used_requests 上报 → used_percent + stale=false
  - test_get_ai_usage_stale         : collected_at=now-3h(5h 窗口 50% 超出) → stale=true
  - test_get_ai_usage_no_data       : 未上报 codex → status="no_data"
  - test_get_ai_usage_metric_unit   : used_requests → "requests"；used_tokens → "tokens"

模板渲染 (_ai_card.html SSR via GET /):
  - test_ai_card_render_ok          : used_percent=80 → data-pct-warn + 进度条 + 百分比
  - test_ai_card_manual_badge       : source_type="manual" → .badge-manual + ◌
  - test_ai_card_stale_banner       : stale=true → .datasource-banner(ai-card__stale) + 过旧提示
  - test_ai_card_no_data            : status="no_data" → 「尚未收到数据」提示

模板/CSS 文件检查与 Jinja2 helper 单元测试一并覆盖。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from panel.collectors import registry
from panel.config.settings import Settings
from panel.main import create_app

_NOW = datetime.now(UTC)

_STATIC_DIR = Path(__file__).parent.parent / "src" / "panel" / "web" / "static"
_CSS_PATH = _STATIC_DIR / "css" / "panel.css"
_TEMPLATES_DIR = Path(__file__).parent.parent / "src" / "panel" / "web" / "templates"


# --------------------------------------------------------------------------- #
# Fixtures / helpers — isolated DB + lifespan (mirrors tests/test_ingest.py)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the global collector registry around each test."""
    registry.clear()
    yield
    registry.clear()


def _make_client(tmp_path: Path):
    """Build an ASGI client context manager with isolated DB + lifespan."""
    settings = Settings(db_path=str(tmp_path / "test.db"))
    app = create_app(settings=settings)

    class _Ctx:
        async def __aenter__(self) -> httpx.AsyncClient:
            self._life = app.router.lifespan_context(app)
            await self._life.__aenter__()
            self._app = app
            transport = httpx.ASGITransport(app=app)
            self._client = httpx.AsyncClient(transport=transport, base_url="http://test")
            return self._client

        async def __aexit__(self, *exc) -> None:
            await self._client.aclose()
            await self._life.__aexit__(*exc)

        @property
        def app(self):
            return self._app

    return _Ctx()


def _ai_payload(
    *,
    provider: str = "codex",
    reported_at: datetime | None = None,
    metrics: list[dict] | None = None,
    status: str = "ok",
) -> dict:
    """构造一个合法的 AI usage payload(JSON-able dict)。"""
    return {
        "reporter_version": "1.0.0",
        "reported_at": (reported_at or _NOW).isoformat(),
        "provider": provider,
        "status": status,
        "metrics": metrics
        if metrics is not None
        else [
            {"metric": "used_requests", "value_num": 42.0},
            {"metric": "limit_requests", "value_num": 1000.0},
            {"metric": "used_percent", "value_num": 4.2},
            {"metric": "resets_at", "value_text": "2026-06-28T05:00:00Z"},
            {"metric": "window_seconds", "value_num": 18000.0},
        ],
    }


# --------------------------------------------------------------------------- #
# API 聚合测试
# --------------------------------------------------------------------------- #


async def test_get_ai_usage_api_ok(tmp_path: Path) -> None:
    """上报后 GET /api/ai-usage 返回正确 used_percent + stale=false。"""
    ctx = _make_client(tmp_path)
    c = await ctx.__aenter__()
    try:
        resp = await c.post("/api/ingest/ai-usage", json=_ai_payload())
        assert resp.status_code == 200

        usage = await c.get("/api/ai-usage")
        assert usage.status_code == 200
        body = usage.json()
        codex = next(p for p in body["providers"] if p["provider"] == "codex")
        assert codex["status"] == "ok"
        assert codex["stale"] is False
        assert codex["stale_since"] is None
        assert codex["used_percent"] == pytest.approx(4.2)
        assert codex["used_value"] == pytest.approx(42.0)
        assert codex["limit_value"] == pytest.approx(1000.0)
        assert codex["metric_unit"] == "requests"
        assert codex["resets_at"] == "2026-06-28T05:00:00Z"
        assert body["last_updated"] is not None
    finally:
        await ctx.__aexit__(None, None, None)


async def test_get_ai_usage_stale(tmp_path: Path) -> None:
    """collected_at=now-3h(5h 窗口 50%=2.5h 阈值超出) → stale=true + stale_since。"""
    ctx = _make_client(tmp_path)
    c = await ctx.__aenter__()
    try:
        old = _NOW - timedelta(hours=3)
        resp = await c.post(
            "/api/ingest/ai-usage", json=_ai_payload(reported_at=old)
        )
        assert resp.status_code == 200

        usage = await c.get("/api/ai-usage")
        codex = next(p for p in usage.json()["providers"] if p["provider"] == "codex")
        assert codex["stale"] is True
        assert codex["stale_since"] is not None
        assert codex["stale_age_label"] is not None
    finally:
        await ctx.__aexit__(None, None, None)


async def test_get_ai_usage_error_status_is_stale(tmp_path: Path) -> None:
    """上报 status='error' → stale=true 且 status='error'(即使数据很新)。"""
    ctx = _make_client(tmp_path)
    c = await ctx.__aenter__()
    try:
        resp = await c.post(
            "/api/ingest/ai-usage", json=_ai_payload(status="error")
        )
        assert resp.status_code == 200

        usage = await c.get("/api/ai-usage")
        codex = next(p for p in usage.json()["providers"] if p["provider"] == "codex")
        assert codex["stale"] is True
        assert codex["status"] == "error"
    finally:
        await ctx.__aexit__(None, None, None)


async def test_get_ai_usage_no_data(tmp_path: Path) -> None:
    """未上报 codex 时 codex provider status='no_data'(空卡)。"""
    ctx = _make_client(tmp_path)
    c = await ctx.__aenter__()
    try:
        # 只上报 chatgpt，codex / claude_code 应为 no_data。
        await c.post(
            "/api/ingest/ai-usage",
            json=_ai_payload(
                provider="chatgpt",
                metrics=[{"metric": "used_percent", "value_num": 50.0}],
            ),
        )
        usage = await c.get("/api/ai-usage")
        providers = {p["provider"]: p for p in usage.json()["providers"]}
        assert providers["codex"]["status"] == "no_data"
        assert providers["codex"]["used_percent"] is None
        assert providers["codex"]["collected_at"] is None
        assert providers["claude_code"]["status"] == "no_data"
        # 上报过的 chatgpt 不是 no_data
        assert providers["chatgpt"]["status"] == "ok"
    finally:
        await ctx.__aexit__(None, None, None)


async def test_get_ai_usage_metric_unit(tmp_path: Path) -> None:
    """used_requests → 'requests'；used_tokens → 'tokens'。"""
    ctx = _make_client(tmp_path)
    c = await ctx.__aenter__()
    try:
        # codex 用 requests
        await c.post(
            "/api/ingest/ai-usage",
            json=_ai_payload(
                provider="codex",
                metrics=[
                    {"metric": "used_requests", "value_num": 10.0},
                    {"metric": "limit_requests", "value_num": 100.0},
                ],
            ),
        )
        # claude_code 用 tokens
        await c.post(
            "/api/ingest/ai-usage",
            json=_ai_payload(
                provider="claude_code",
                metrics=[
                    {"metric": "used_tokens", "value_num": 5000.0},
                    {"metric": "limit_tokens", "value_num": 100000.0},
                ],
            ),
        )
        usage = await c.get("/api/ai-usage")
        providers = {p["provider"]: p for p in usage.json()["providers"]}
        assert providers["codex"]["metric_unit"] == "requests"
        assert providers["codex"]["used_value"] == pytest.approx(10.0)
        assert providers["claude_code"]["metric_unit"] == "tokens"
        assert providers["claude_code"]["used_value"] == pytest.approx(5000.0)
        assert providers["claude_code"]["limit_value"] == pytest.approx(100000.0)
    finally:
        await ctx.__aexit__(None, None, None)


# --------------------------------------------------------------------------- #
# 模板渲染测试 (SSR via GET / 注入 ai_providers)
# --------------------------------------------------------------------------- #


def _make_provider(
    *,
    provider: str = "codex",
    display_name: str = "Codex",
    source_type: str = "local_jsonl",
    used_percent: float | None = 40.0,
    used_value: float | None = 400.0,
    limit_value: float | None = 1000.0,
    metric_unit: str = "requests",
    resets_at: str | None = "2026-06-28T20:00:00Z",
    window_label: str = "5h 窗口",
    stale: bool = False,
    stale_since: str | None = None,
    stale_age_label: str | None = None,
    collected_at: str | None = "2026-06-28T12:00:00Z",
    status: str = "ok",
) -> SimpleNamespace:
    """AiProviderStatus-like view-model for template rendering (attr access)."""
    return SimpleNamespace(
        provider=provider,
        display_name=display_name,
        source_type=source_type,
        used_percent=used_percent,
        used_value=used_value,
        limit_value=limit_value,
        metric_unit=metric_unit,
        resets_at=resets_at,
        window_label=window_label,
        stale=stale,
        stale_since=stale_since,
        stale_age_label=stale_age_label,
        collected_at=collected_at,
        status=status,
    )


def _render_index(ai_providers: list[SimpleNamespace]) -> str:
    """直接用 routes.templates 渲染 index.html，只关心 _ai_card 部分。

    传入最小上下文(其他 partial 在空数据下优雅降级)。
    """
    from panel.web.routes import templates

    template = templates.get_template("index.html")
    return template.render(
        is_eink=False,
        collector_statuses=[],
        any_issues=False,
        now=_NOW.isoformat(),
        azure_dashboard=None,
        nodes=[],
        nodes_online=0,
        nodes_total=0,
        collector_status="never_run",
        collector_error=None,
        is_stale=False,
        stale_seconds=90,
        ai_providers=ai_providers,
    )


def test_ai_card_render_ok() -> None:
    """used_percent=80 → data-pct-warn + 进度条 + 百分比文本。"""
    html = _render_index([_make_provider(used_percent=80.0)])
    assert 'data-module="ai-usage"' in html
    assert "data-pct-warn" in html
    assert "ai-metric-bar" in html
    assert "80" in html
    # 状态符 ●(ok/warn band) 应存在
    assert "◐" in html  # 70 ≤ 80 < 90 → warn → ◐


def test_ai_card_render_error_band() -> None:
    """used_percent=95 → data-pct-error + ● 符号。"""
    html = _render_index([_make_provider(used_percent=95.0)])
    assert "data-pct-error" in html
    assert "●" in html  # ≥90 → error → ●


def test_ai_card_manual_badge() -> None:
    """source_type='manual' → .badge-manual + ◌。"""
    html = _render_index(
        [
            _make_provider(
                provider="chatgpt",
                display_name="ChatGPT",
                source_type="manual",
            )
        ]
    )
    assert "badge-manual" in html
    assert "手动" in html
    assert "◌" in html


def test_ai_card_stale_banner() -> None:
    """stale=true → .datasource-banner(ai-card__stale) + 过旧提示。"""
    html = _render_index(
        [
            _make_provider(
                stale=True,
                stale_since="2026-06-28T09:00:00Z",
                stale_age_label="3h 0m",
            )
        ]
    )
    assert "ai-card__stale" in html
    assert "datasource-banner" in html
    assert "数据可能过旧" in html
    assert "3h 0m" in html


def test_ai_card_no_data() -> None:
    """status='no_data' → 「尚未收到数据」提示 + ◌。"""
    html = _render_index(
        [
            _make_provider(
                status="no_data",
                used_percent=None,
                used_value=None,
                limit_value=None,
                metric_unit="unknown",
                resets_at=None,
                collected_at=None,
            )
        ]
    )
    assert "尚未收到数据" in html
    assert "◌" in html
    # no_data 卡不应渲染进度条
    assert "ai-metric-bar" not in html


def test_ai_card_unknown_unit_no_count() -> None:
    """used_value 为 None(codex 仅有 used_percent)→ 只显示百分比，无分子/分母括号。"""
    html = _render_index(
        [
            _make_provider(
                used_percent=46.0,
                used_value=None,
                limit_value=None,
                metric_unit="unknown",
            )
        ]
    )
    assert "46" in html
    # 不应出现 "/ ... unknown" 计数文本
    assert "unknown" not in html


# --------------------------------------------------------------------------- #
# 模板 / CSS 文件检查
# --------------------------------------------------------------------------- #


def test_index_html_includes_ai_card_partial() -> None:
    """index.html must include partials/_ai_card.html."""
    content = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    assert "_ai_card.html" in content


def test_ai_card_partial_exists() -> None:
    """partials/_ai_card.html must exist."""
    assert (_TEMPLATES_DIR / "partials" / "_ai_card.html").exists()


def _read_ai_css_section() -> str:
    """Return the AI 额度 CSS section from panel.css."""
    css = _CSS_PATH.read_text(encoding="utf-8")
    m = re.search(r"── AI 额度 ──.*?End AI 额度", css, re.DOTALL)
    return m.group(0) if m else ""


def test_css_ai_section_exists() -> None:
    """panel.css must contain the AI 额度 section."""
    assert _read_ai_css_section(), "AI 额度 section marker not found in panel.css"


def test_css_no_box_shadow_in_ai_section() -> None:
    """AI 额度 CSS section must have no box-shadow (e-ink constraint)."""
    section = _read_ai_css_section()
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\bbox-shadow\s*:", no_comments) is None


def test_css_no_animation_in_ai_section() -> None:
    """AI 额度 CSS section must not define animation or @keyframes (e-ink)."""
    section = _read_ai_css_section()
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\banimation\s*:", no_comments) is None
    assert re.search(r"@keyframes\b", no_comments) is None


def test_css_threshold_colour_wrapped_in_media_color() -> None:
    """阈值变色规则必须包裹在 @media (color) 内(灰度设备不变色)。"""
    section = _read_ai_css_section()
    # data-pct-warn/error 的 background 变色必须出现在 @media (color) 块中
    m = re.search(r"@media \(color\)\s*\{(.*?)\n\}", section, re.DOTALL)
    assert m is not None, "AI section must contain a @media (color) block"
    color_block = m.group(1)
    assert "data-pct-warn" in color_block
    assert "data-pct-error" in color_block


# --------------------------------------------------------------------------- #
# Jinja2 helper 单元测试
# --------------------------------------------------------------------------- #


class TestAiStatusClass:
    def test_no_data(self):
        from panel.web.routes import _ai_status_class

        assert _ai_status_class(_make_provider(status="no_data")) == "nodata"

    def test_stale(self):
        from panel.web.routes import _ai_status_class

        assert _ai_status_class(_make_provider(stale=True)) == "stale"

    def test_error_status(self):
        from panel.web.routes import _ai_status_class

        assert _ai_status_class(_make_provider(status="error")) == "error"

    def test_high_pct_is_error(self):
        from panel.web.routes import _ai_status_class

        assert _ai_status_class(_make_provider(used_percent=92.0)) == "error"

    def test_mid_pct_is_warn(self):
        from panel.web.routes import _ai_status_class

        assert _ai_status_class(_make_provider(used_percent=75.0)) == "warn"

    def test_low_pct_is_ok(self):
        from panel.web.routes import _ai_status_class

        assert _ai_status_class(_make_provider(used_percent=10.0)) == "ok"


class TestAiStatusSymbol:
    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({"used_percent": 10.0}, "●"),  # ok
            ({"used_percent": 75.0}, "◐"),  # warn
            ({"used_percent": 95.0}, "●"),  # error band
            ({"stale": True}, "○"),  # stale
            ({"status": "no_data"}, "◌"),  # no_data
        ],
    )
    def test_symbols(self, kwargs: dict, expected: str):
        from panel.web.routes import _ai_status_symbol

        assert _ai_status_symbol(_make_provider(**kwargs)) == expected


class TestAiPctThresholdHelpers:
    @pytest.mark.parametrize(
        "pct,warn,error",
        [
            (None, False, False),
            (69.9, False, False),
            (70.0, True, False),
            (89.9, True, False),
            (90.0, False, True),
            (100.0, False, True),
        ],
    )
    def test_bands(self, pct, warn, error):
        from panel.web.routes import _ai_pct_error, _ai_pct_warn

        p = _make_provider(used_percent=pct)
        assert _ai_pct_warn(p) is warn
        assert _ai_pct_error(p) is error
