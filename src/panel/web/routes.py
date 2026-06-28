"""SSR routes for the web UI (ARCH-001 / TASK-004).

GET /  renders index.html with data-source status from the repository.
If app.state.repo is not yet available (e.g. during tests with a bare app),
the route falls back gracefully to an empty collector list.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from panel.db.repository import CollectorRunRow

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

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

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "is_eink": is_eink,
            "collector_statuses": collector_statuses,
            "any_issues": any_issues,
            "now": datetime.now(UTC).isoformat(),
        },
    )
