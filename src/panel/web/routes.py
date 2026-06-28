"""SSR routes for the web UI (ARCH-001 / TASK-004 / TASK-015 / TASK-022).

GET /  renders index.html with:
  - data-source status from the repository (TASK-004)
  - Azure dashboard (DashboardAzureOut) injected as azure_dashboard (TASK-015)
  - Tailscale node grid context injected as nodes/nodes_online/... (TASK-022)

If app.state.repo / app.state.gpu_repo is not yet available (e.g. during
tests with a bare app), the route falls back gracefully.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from panel.db.repository import CollectorRunRow
from panel.domain.models import ServerIn

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# TASK-015: Jinja2 global helper functions for VM / GPU status encoding
# ---------------------------------------------------------------------------

def _vm_status_class(vm: Any) -> str:  # noqa: ANN401 — vm is DashboardVmOut at runtime
    """Return CSS class suffix for a VM's current state.

    Three-layer encoding:
        ok    — Running
        warn  — Stopped / Deallocated / transitional states
        error — Unknown / unparseable
        stale — Data older than the stale threshold

    e-ink: CSS colour alone is never the sole indicator; shape symbol and
    text label always accompany it.
    """
    if getattr(vm, "is_stale", False):
        return "stale"
    state = getattr(vm, "power_state", "Unknown")
    match state:
        case "Running":
            return "ok"
        case "Starting" | "Stopping" | "Deallocating" | "Stopped" | "Deallocated":
            return "warn"
        case _:
            return "error"


def _vm_status_symbol(vm: Any) -> str:  # noqa: ANN401
    """Return Unicode shape symbol for three-layer state encoding.

    ●  solid — ok/running
    ◐  half  — warn/stopped/transitional
    ○  hollow — error/unknown
    ◌  dotted — stale (data too old)
    """
    match _vm_status_class(vm):
        case "ok":
            return "●"
        case "warn":
            return "◐"
        case "stale":
            return "◌"
        case _:
            return "○"


def _util_threshold_class(pct: float) -> str:
    """Map GPU utilisation % to a metric-bar fill CSS class."""
    if pct >= 90:
        return "bar-critical"
    if pct >= 70:
        return "bar-warn"
    return "bar-ok"


def _mem_threshold_class(pct: float | None) -> str:
    """Map GPU memory % to a metric-bar fill CSS class.  None → empty string."""
    if pct is None:
        return ""
    if pct >= 90:
        return "bar-critical"
    if pct >= 75:
        return "bar-warn"
    return "bar-ok"


# Register helpers as Jinja2 globals so templates call them without "request".
templates.env.globals["vm_status_class"] = _vm_status_class
templates.env.globals["vm_status_symbol"] = _vm_status_symbol
templates.env.globals["util_threshold_class"] = _util_threshold_class
templates.env.globals["mem_threshold_class"] = _mem_threshold_class


# ---------------------------------------------------------------------------
# TASK-022: Jinja2 custom filter for datetime formatting (Tailscale last_seen)
# ---------------------------------------------------------------------------

def _datetimeformat(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    """Format a datetime as a UTC string.  None → '—'.

    Handles both naive (assumed UTC) and tz-aware datetimes.
    Registered as the ``datetimeformat`` Jinja2 filter.
    """
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(fmt)


templates.env.filters["datetimeformat"] = _datetimeformat

# Stale threshold for Tailscale nodes when no settings are available.
_DEFAULT_TAILSCALE_STALE_SECONDS = 90

# Seconds after which a collector run is considered stale even if status=up.
# Pulled from settings at request time when repo is available.
_DEFAULT_STALE_SECONDS = 180


def _is_eink(request: Request) -> bool:
    """Return True when the client is an e-ink device or ?eink=1 is set.

    Detection heuristics:
    - Query param  ?eink=1   (explicit override, highest priority)
    - User-Agent contains "Kindle" or "Silk" (Amazon Silk browser on Kindle)
    """
    if request.query_params.get("eink") == "1":
        return True
    ua: str = request.headers.get("user-agent", "")
    return "Kindle" in ua or "Silk" in ua


def _compute_display_status(
    run: CollectorRunRow,
    stale_threshold_seconds: int,
) -> str:
    """Map a CollectorRunRow to a display status string: up/down/error/stale.

    'stale' is a read-time derived state: even if the last run was 'up',
    if it happened more than stale_threshold_seconds ago, we surface it as stale.
    """
    if run.status in ("down", "error"):
        return run.status

    # status == "up" — check staleness
    try:
        ran_at = datetime.fromisoformat(run.ran_at)
        if ran_at.tzinfo is None:
            ran_at = ran_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - ran_at).total_seconds()
        if age_seconds > stale_threshold_seconds:
            return "stale"
    except (ValueError, TypeError):
        logger.warning("Could not parse ran_at for collector %s: %r", run.collector, run.ran_at)
        return "error"

    return "up"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    """Render the single-screen overview dashboard."""
    is_eink = _is_eink(request)

    # --- Load data-source status rows (graceful fallback when repo unavailable) ---
    collector_statuses: list[dict[str, Any]] = []
    stale_threshold = _DEFAULT_STALE_SECONDS

    repo = getattr(request.app.state, "repo", None)
    gpu_repo = getattr(request.app.state, "gpu_repo", None)
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        stale_threshold = getattr(settings, "stale_threshold_seconds", _DEFAULT_STALE_SECONDS)

    if repo is not None:
        try:
            runs = await repo.get_all_last_runs()
            for run in runs:
                display_status = _compute_display_status(run, stale_threshold)
                collector_statuses.append(
                    {
                        "name": run.collector,
                        "status": display_status,
                        "ran_at": run.ran_at,
                        "error": run.error,
                    }
                )
        except Exception:
            logger.exception("Failed to load collector runs for dashboard")

    any_issues = any(s["status"] != "up" for s in collector_statuses)

    # --- TASK-015: Load Azure dashboard for SSR VmCard / GpuCard rendering ---
    azure_dashboard = None
    if repo is not None and gpu_repo is not None:
        try:
            # Reuse the same aggregation logic as the API endpoint, but called
            # directly from the SSR route to avoid an HTTP round-trip.
            from panel.api.azure import build_azure_dashboard  # noqa: PLC0415

            azure_dashboard = await build_azure_dashboard(repo=repo, gpu_repo=gpu_repo)
        except Exception:
            logger.exception("Failed to load Azure dashboard for SSR")

    # --- TASK-022: Load Tailscale node context for SSR NodeGrid rendering ---
    tailscale_nodes: list[Any] = []
    tailscale_collector_status = "never_run"
    tailscale_collector_error: str | None = None
    tailscale_is_stale = False
    tailscale_stale_seconds = (
        getattr(settings, "tailscale_stale_threshold_seconds", _DEFAULT_TAILSCALE_STALE_SECONDS)
        if settings is not None
        else _DEFAULT_TAILSCALE_STALE_SECONDS
    )

    if repo is not None:
        try:
            tailscale_nodes = await repo.get_all_nodes()
        except Exception:
            logger.exception("Failed to load Tailscale nodes for SSR")

        try:
            last_run = await repo.get_last_run("tailscale")
            if last_run is None:
                tailscale_collector_status = "never_run"
            else:
                tailscale_collector_status = last_run.status
                tailscale_collector_error = last_run.error
                # Compute stale: last successful run older than threshold?
                if last_run.status == "up":
                    try:
                        ran_at = datetime.fromisoformat(last_run.ran_at)
                        if ran_at.tzinfo is None:
                            ran_at = ran_at.replace(tzinfo=UTC)
                        age = (datetime.now(UTC) - ran_at).total_seconds()
                        tailscale_is_stale = age > tailscale_stale_seconds
                    except (ValueError, TypeError):
                        pass
        except Exception:
            logger.exception("Failed to load Tailscale collector status for SSR")

    # Annotate each node with is_stale based on its own collected_at
    # (mirrors TASK-021 route logic: per-node stale flag)
    nodes_with_stale: list[Any] = []
    for n in tailscale_nodes:
        try:
            now = datetime.now(UTC)
            collected = n.collected_at
            if collected.tzinfo is None:
                collected = collected.replace(tzinfo=UTC)
            age_s = (now - collected).total_seconds()
            is_node_stale = age_s > tailscale_stale_seconds
        except Exception:
            is_node_stale = False

        # Build a lightweight wrapper that adds is_stale and renames last_seen_at→last_seen
        # without mutating the dataclass (slots=True prevents attribute assignment).
        from types import SimpleNamespace  # noqa: PLC0415

        node_view = SimpleNamespace(
            id=n.id,
            hostname=n.hostname,
            dns_name=getattr(n, "dns_name", None),
            tailscale_ips=n.tailscale_ips,
            os=getattr(n, "os", None),
            online_state=n.online_state,
            is_exit_node=n.is_exit_node,
            last_seen=getattr(n, "last_seen_at", None),  # template uses node.last_seen
            is_stale=is_node_stale,
            updated_at=n.updated_at,
        )
        nodes_with_stale.append(node_view)

    nodes_online = sum(1 for n in nodes_with_stale if n.online_state == "ONLINE")

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "is_eink": is_eink,
            "collector_statuses": collector_statuses,
            "any_issues": any_issues,
            "now": datetime.now(UTC).isoformat(),
            "azure_dashboard": azure_dashboard,
            # TASK-022: Tailscale node grid
            "nodes": nodes_with_stale,
            "nodes_online": nodes_online,
            "nodes_total": len(nodes_with_stale),
            "collector_status": tailscale_collector_status,
            "collector_error": tailscale_collector_error,
            "is_stale": tailscale_is_stale,
            "stale_seconds": tailscale_stale_seconds,
        },
    )


# ---------------------------------------------------------------------------
# 服务器注册管理页(REQ-002 注册机制的 Web 入口；REST API 在 /api/v1/servers)
# 供人类手动填写监控目标的连接/校验信息(SSH host/user/key、Azure VM 名等)。
# ---------------------------------------------------------------------------

# 表单提示码 → 中文消息(避免在 HTTP Location 头里放非 ASCII)
_FLASH = {
    "created": ("ok", "服务器已注册。GPU 采集器将在下一轮(≤60s)尝试连接。"),
    "deleted": ("ok", "服务器已删除。"),
    "dup": ("err", "注册失败:同名服务器已存在。"),
    "fail": ("err", "注册失败:请检查输入。"),
    "norepo": ("err", "数据层不可用。"),
}


def _clean(v: str | None) -> str | None:
    """空白串归一为 None。"""
    v = (v or "").strip()
    return v or None


@router.get("/servers", response_class=HTMLResponse, include_in_schema=False)
async def servers_page(request: Request, flash: str | None = None) -> HTMLResponse:
    """渲染服务器注册管理页:已注册列表 + 注册表单。"""
    gpu_repo = getattr(request.app.state, "gpu_repo", None)
    servers: list[Any] = []
    if gpu_repo is not None:
        try:
            servers = await gpu_repo.get_all_servers()
        except Exception:
            logger.exception("Failed to load servers for /servers page")

    flash_kind, flash_msg = (None, None)
    if flash in _FLASH:
        flash_kind, flash_msg = _FLASH[flash]

    return templates.TemplateResponse(
        request=request,
        name="servers.html",
        context={
            "is_eink": _is_eink(request),
            "now": datetime.now(UTC).isoformat(),
            "servers": servers,
            "flash_kind": flash_kind,
            "flash_msg": flash_msg,
        },
    )


@router.post("/servers", include_in_schema=False)
async def servers_create(  # noqa: PLR0913 — 表单字段平铺
    request: Request,
    name: str = Form(...),
    ssh_host: str = Form(""),
    ssh_port: int = Form(22),
    ssh_user: str = Form("azureuser"),
    ssh_key_path: str = Form(""),
    azure_resource_group: str = Form(""),
    azure_vm_name: str = Form(""),
    has_gpu: str = Form("off"),
    notes: str = Form(""),
) -> RedirectResponse:
    """处理注册表单提交,写入 servers 表后重定向回 /servers。"""
    gpu_repo = getattr(request.app.state, "gpu_repo", None)
    if gpu_repo is None:
        return RedirectResponse("/servers?flash=norepo", status_code=303)
    try:
        data = ServerIn(
            name=name.strip(),
            azure_resource_group=_clean(azure_resource_group),
            azure_vm_name=_clean(azure_vm_name),
            ssh_host=_clean(ssh_host),
            ssh_port=ssh_port,
            ssh_user=(ssh_user.strip() or "azureuser"),
            ssh_key_path=_clean(ssh_key_path),
            has_gpu=(has_gpu in ("on", "true", "1", "yes")),
            notes=_clean(notes),
        )
        await gpu_repo.insert_server(data)
    except aiosqlite.IntegrityError:
        return RedirectResponse("/servers?flash=dup", status_code=303)
    except Exception:
        logger.exception("Failed to register server via form")
        return RedirectResponse("/servers?flash=fail", status_code=303)
    return RedirectResponse("/servers?flash=created", status_code=303)


@router.post("/servers/{server_id}/delete", include_in_schema=False)
async def servers_delete(request: Request, server_id: int) -> RedirectResponse:
    """删除一台已注册服务器(级联清理其 VM/GPU 数据),重定向回 /servers。"""
    gpu_repo = getattr(request.app.state, "gpu_repo", None)
    if gpu_repo is not None:
        try:
            await gpu_repo.delete_server(server_id)
        except Exception:
            logger.exception("Failed to delete server %s", server_id)
    return RedirectResponse("/servers?flash=deleted", status_code=303)
