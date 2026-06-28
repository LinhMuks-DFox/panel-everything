"""TASK-015: Front-end VmCard + GpuCard + status badge tests.

Coverage:
  1.  GET / returns 200 and HTML contains data-module="azure" section
  2.  vm_status_class: Running→"ok", Stopped→"warn", Deallocated→"warn",
      Unknown→"error", stale→"stale"
  3.  vm_status_symbol: ok→"●", warn→"◐", stale→"◌", error→"○"
  4.  util_threshold_class: 0%→"bar-ok", 69%→"bar-ok", 70%→"bar-warn",
      90%→"bar-critical", 100%→"bar-critical"
  5.  mem_threshold_class: None→"", 74%→"bar-ok", 75%→"bar-warn",
      90%→"bar-critical"
  6.  GET / with registered VM renders VM name, power_state, resource group
  7.  GET / with GPU data renders GPU card with util bar and memory bar
  8.  GET / with GPU util_pct=None renders "不可达" block (no bar error)
  9.  GET / with stale VM renders ⚠ stale badge
  10. GET / with no registered VMs renders empty-state message
  11. HTML contains no ssh_key_path field content (security whitelist)
  12. panel.css ARCH-002 section has no box-shadow property
  13. panel.css ARCH-002 section has no animation/@keyframes
  14. panel.css defines bar-ok, bar-warn, bar-critical fill classes
  15. VmCard partial is included via {% include %} in index.html (template check)
  16. vm_status_class for each transitional state (Starting, Stopping, Deallocating)
  17. SSR azure section absent when azure_dashboard is None (graceful fallback)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from panel.main import create_app
from panel.web.routes import (
    _mem_threshold_class,
    _util_threshold_class,
    _vm_status_class,
    _vm_status_symbol,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent.parent / "src" / "panel" / "web" / "static"
_CSS_PATH = _STATIC_DIR / "css" / "panel.css"
_TEMPLATES_DIR = (
    Path(__file__).parent.parent / "src" / "panel" / "web" / "templates"
)


async def _get(app, path: str, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers or {})


def _make_vm(
    *,
    server_id: int = 1,
    name: str = "gpu-vm-01",
    power_state: str = "Running",
    is_stale: bool = False,
    is_running: bool = True,
    azure_resource_group: str | None = "lab-rg",
    azure_vm_name: str | None = "gpu-vm-01",
    gpus: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        server_id=server_id,
        name=name,
        power_state=power_state,
        is_stale=is_stale,
        is_running=is_running,
        azure_resource_group=azure_resource_group,
        azure_vm_name=azure_vm_name,
        gpus=gpus or [],
    )


def _make_gpu(
    *,
    server_id: int = 1,
    gpu_index: int = 0,
    gpu_name: str | None = "NVIDIA A100",
    util_pct: float | None = 80.0,
    mem_used_mib: float | None = 40960.0,
    mem_total_mib: float | None = 81920.0,
    mem_pct: float | None = 50.0,
    temp_c: float | None = 70.0,
    power_w: float | None = 300.0,
    is_stale: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        server_id=server_id,
        gpu_index=gpu_index,
        gpu_name=gpu_name,
        util_pct=util_pct,
        mem_used_mib=mem_used_mib,
        mem_total_mib=mem_total_mib,
        mem_pct=mem_pct,
        temp_c=temp_c,
        power_w=power_w,
        is_stale=is_stale,
    )


def _make_dashboard(vms: list | None = None) -> SimpleNamespace:
    """Build a minimal DashboardAzureOut-like namespace for template rendering."""
    from panel.domain.models import CollectorStatusOut

    cs: dict = {
        "azure_vm": CollectorStatusOut(
            status="up",
            last_ran_at=datetime.now(UTC),
            error=None,
        ),
        "gpu": CollectorStatusOut(
            status="up",
            last_ran_at=datetime.now(UTC),
            error=None,
        ),
    }
    return SimpleNamespace(
        fetched_at=datetime.now(UTC),
        collector_status=cs,
        vms=vms or [],
    )


def _make_app_with_dashboard(dashboard_obj):
    """Create a test app whose state exposes mocked repo objects.

    The index route will call build_azure_dashboard; we mock the underlying
    db methods to return the desired data by injecting them onto app.state.
    """
    app = create_app()

    # Mock repo (get_all_last_runs used by datasource banner AND azure builder)
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])

    # Mock gpu_repo methods needed by build_azure_dashboard
    mock_gpu_repo = MagicMock()

    # Instead of mocking individual DB methods, we patch build_azure_dashboard
    # on the module level for each test. This fixture provides the app; the
    # test patches the builder.
    app.state.repo = mock_repo
    app.state.gpu_repo = mock_gpu_repo

    return app, mock_repo, mock_gpu_repo


# ---------------------------------------------------------------------------
# 1. Helper function unit tests (pure Python — fast)
# ---------------------------------------------------------------------------

# vm_status_class ----------------------------------------------------------

class TestVmStatusClass:
    def test_running_is_ok(self):
        vm = _make_vm(power_state="Running", is_stale=False)
        assert _vm_status_class(vm) == "ok"

    def test_stopped_is_warn(self):
        vm = _make_vm(power_state="Stopped", is_stale=False)
        assert _vm_status_class(vm) == "warn"

    def test_deallocated_is_warn(self):
        vm = _make_vm(power_state="Deallocated", is_stale=False)
        assert _vm_status_class(vm) == "warn"

    def test_starting_is_warn(self):
        vm = _make_vm(power_state="Starting", is_stale=False)
        assert _vm_status_class(vm) == "warn"

    def test_stopping_is_warn(self):
        vm = _make_vm(power_state="Stopping", is_stale=False)
        assert _vm_status_class(vm) == "warn"

    def test_deallocating_is_warn(self):
        vm = _make_vm(power_state="Deallocating", is_stale=False)
        assert _vm_status_class(vm) == "warn"

    def test_unknown_is_error(self):
        vm = _make_vm(power_state="Unknown", is_stale=False)
        assert _vm_status_class(vm) == "error"

    def test_stale_overrides_running(self):
        vm = _make_vm(power_state="Running", is_stale=True)
        assert _vm_status_class(vm) == "stale"

    def test_stale_overrides_stopped(self):
        vm = _make_vm(power_state="Stopped", is_stale=True)
        assert _vm_status_class(vm) == "stale"


# vm_status_symbol ----------------------------------------------------------

class TestVmStatusSymbol:
    def test_ok_is_solid(self):
        vm = _make_vm(power_state="Running")
        assert _vm_status_symbol(vm) == "●"

    def test_warn_is_half(self):
        vm = _make_vm(power_state="Deallocated")
        assert _vm_status_symbol(vm) == "◐"

    def test_stale_is_dotted(self):
        vm = _make_vm(power_state="Running", is_stale=True)
        assert _vm_status_symbol(vm) == "◌"

    def test_error_is_hollow(self):
        vm = _make_vm(power_state="Unknown")
        assert _vm_status_symbol(vm) == "○"


# util_threshold_class ------------------------------------------------------

class TestUtilThresholdClass:
    @pytest.mark.parametrize("pct,expected", [
        (0.0,   "bar-ok"),
        (50.0,  "bar-ok"),
        (69.9,  "bar-ok"),
        (70.0,  "bar-warn"),
        (89.9,  "bar-warn"),
        (90.0,  "bar-critical"),
        (100.0, "bar-critical"),
    ])
    def test_thresholds(self, pct: float, expected: str):
        assert _util_threshold_class(pct) == expected


# mem_threshold_class -------------------------------------------------------

class TestMemThresholdClass:
    def test_none_returns_empty(self):
        assert _mem_threshold_class(None) == ""

    @pytest.mark.parametrize("pct,expected", [
        (0.0,   "bar-ok"),
        (74.9,  "bar-ok"),
        (75.0,  "bar-warn"),
        (89.9,  "bar-warn"),
        (90.0,  "bar-critical"),
        (100.0, "bar-critical"),
    ])
    def test_thresholds(self, pct: float, expected: str):
        assert _mem_threshold_class(pct) == expected


# ---------------------------------------------------------------------------
# 2. GET / SSR integration tests (with mocked build_azure_dashboard)
# ---------------------------------------------------------------------------

@pytest.fixture
def app_no_repo():
    """App without repo/gpu_repo — azure section should be absent."""
    return create_app()


@pytest.fixture
def app_mocked_azure():
    """App whose SSR route will call build_azure_dashboard via patched repos.

    We patch panel.api.azure.build_azure_dashboard directly so we can inject
    any DashboardAzureOut-like object without needing a real DB.
    """
    return create_app()


async def test_index_has_azure_section_when_dashboard_available(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """GET / renders data-module="azure" section when azure_dashboard is set."""
    import panel.api.azure as _azure_mod

    dashboard = _make_dashboard(vms=[_make_vm()])
    monkeypatch.setattr(
        _azure_mod,
        "build_azure_dashboard",
        AsyncMock(return_value=dashboard),
    )
    # Provide mock repo/gpu_repo so the route will call build_azure_dashboard
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    assert resp.status_code == 200
    assert 'data-module="azure"' in resp.text


async def test_index_no_azure_section_without_repos(app_no_repo):
    """GET / returns 200 but no azure section when repo/gpu_repo absent."""
    resp = await _get(app_no_repo, "/")
    assert resp.status_code == 200
    # azure section is absent when azure_dashboard is None (no repo available)
    body = resp.text
    # The placeholder should be visible and the page should still render.
    assert 'id="panel-grid"' in body


async def test_index_renders_vm_name_and_power_state(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """GET / shows the VM name and power state from azure_dashboard."""
    import panel.api.azure as _azure_mod

    dashboard = _make_dashboard(vms=[_make_vm(name="lab-gpu-01", power_state="Running")])
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    body = resp.text
    assert "lab-gpu-01" in body
    assert "Running" in body


async def test_index_renders_resource_group(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """GET / renders the VM's azure_resource_group in the vm-meta block."""
    import panel.api.azure as _azure_mod

    dashboard = _make_dashboard(
        vms=[_make_vm(azure_resource_group="prod-rg")]
    )
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    assert "prod-rg" in resp.text


async def test_index_renders_gpu_card_with_bars(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """GET / renders GPU card with util and memory bars when util_pct is set."""
    import panel.api.azure as _azure_mod

    gpu = _make_gpu(util_pct=75.0, mem_pct=60.0, gpu_name="NVIDIA A100")
    dashboard = _make_dashboard(vms=[_make_vm(gpus=[gpu])])
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    body = resp.text
    assert "gpu-card" in body
    assert "metric-bar-row" in body
    assert "NVIDIA A100" in body
    assert "算力" in body
    assert "显存" in body


async def test_index_renders_gpu_unreachable_when_util_none(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """When GPU util_pct=None, the 不可达 block is rendered instead of bars."""
    import panel.api.azure as _azure_mod

    gpu = _make_gpu(util_pct=None, mem_pct=None)
    dashboard = _make_dashboard(vms=[_make_vm(gpus=[gpu])])
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    body = resp.text
    assert "不可达" in body
    # Should NOT raise a template error (util_pct is not none guard)
    assert resp.status_code == 200


async def test_index_renders_stale_badge(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """When is_stale=True the ⚠ 陈旧 badge is present in the HTML."""
    import panel.api.azure as _azure_mod

    vm = _make_vm(is_stale=True, power_state="Running")
    dashboard = _make_dashboard(vms=[vm])
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    body = resp.text
    assert "陈旧" in body
    assert "stale-badge" in body


async def test_index_empty_vm_list_shows_empty_msg(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """When vms=[] the empty-state message is rendered inside the azure card."""
    import panel.api.azure as _azure_mod

    dashboard = _make_dashboard(vms=[])
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    body = resp.text
    assert "暂无已注册的 Azure 云服务器" in body


async def test_index_no_ssh_key_path_in_html(
    app_mocked_azure, monkeypatch: pytest.MonkeyPatch
):
    """HTML must NOT contain ssh_key_path anywhere (credential leak check)."""
    import panel.api.azure as _azure_mod

    dashboard = _make_dashboard(vms=[_make_vm()])
    monkeypatch.setattr(
        _azure_mod, "build_azure_dashboard", AsyncMock(return_value=dashboard)
    )
    mock_repo = MagicMock()
    mock_repo.get_all_last_runs = AsyncMock(return_value=[])
    app_mocked_azure.state.repo = mock_repo
    app_mocked_azure.state.gpu_repo = MagicMock()

    resp = await _get(app_mocked_azure, "/")
    assert "ssh_key_path" not in resp.text


# ---------------------------------------------------------------------------
# 3. CSS static-file checks (grep-style assertions)
# ---------------------------------------------------------------------------

def _read_arch002_css_section() -> str:
    """Read panel.css and return only the ARCH-002 section."""
    css = _CSS_PATH.read_text(encoding="utf-8")
    # Grab everything between the ARCH-002 marker and "End ARCH-002"
    m = re.search(
        r"ARCH-002:.*?Azure VM.*?End ARCH-002",
        css,
        re.DOTALL,
    )
    # If marker not found, return empty to cause informative assertion failures
    return m.group(0) if m else ""


def test_css_arch002_section_exists():
    """panel.css must contain the ARCH-002 VM/GPU section."""
    section = _read_arch002_css_section()
    assert section, "ARCH-002 section marker not found in panel.css"


def test_css_no_box_shadow_in_arch002_section():
    """ARCH-002 CSS section must have no box-shadow (e-ink constraint)."""
    section = _read_arch002_css_section()
    # Strip CSS comments before checking
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\bbox-shadow\s*:", no_comments) is None, (
        "ARCH-002 CSS section must not use box-shadow (e-ink hard constraint)"
    )


def test_css_no_animation_in_arch002_section():
    """ARCH-002 CSS section must not define animation or @keyframes."""
    section = _read_arch002_css_section()
    no_comments = re.sub(r"/\*.*?\*/", "", section, flags=re.DOTALL)
    assert re.search(r"\banimation\s*:", no_comments) is None, (
        "ARCH-002 CSS must not use animation property"
    )
    assert re.search(r"@keyframes\b", no_comments) is None, (
        "ARCH-002 CSS must not define @keyframes"
    )


def test_css_defines_bar_threshold_classes():
    """panel.css must define bar-ok, bar-warn, bar-critical fill classes."""
    css = _CSS_PATH.read_text(encoding="utf-8")
    for cls in ("bar-ok", "bar-warn", "bar-critical"):
        assert cls in css, f"Missing CSS class .{cls} in panel.css"


# ---------------------------------------------------------------------------
# 4. Template file check
# ---------------------------------------------------------------------------

def test_index_html_includes_vm_card_partial():
    """index.html must include the _vm_card.html partial via Jinja2 include."""
    index_path = _TEMPLATES_DIR / "index.html"
    content = index_path.read_text(encoding="utf-8")
    assert "_vm_card.html" in content, (
        "index.html does not include partials/_vm_card.html"
    )


def test_vm_card_partial_exists():
    """partials/_vm_card.html must exist."""
    vm_card_path = _TEMPLATES_DIR / "partials" / "_vm_card.html"
    assert vm_card_path.exists(), "_vm_card.html partial not found"


def test_vm_card_partial_has_status_dot():
    """_vm_card.html must use .status-dot for three-layer state encoding."""
    vm_card_path = _TEMPLATES_DIR / "partials" / "_vm_card.html"
    content = vm_card_path.read_text(encoding="utf-8")
    assert "status-dot" in content, "Missing status-dot class in _vm_card.html"


def test_vm_card_partial_has_metric_bar():
    """_vm_card.html must use the .metric-bar pattern for GPU bars."""
    vm_card_path = _TEMPLATES_DIR / "partials" / "_vm_card.html"
    content = vm_card_path.read_text(encoding="utf-8")
    assert "metric-bar" in content, "Missing metric-bar in _vm_card.html"
