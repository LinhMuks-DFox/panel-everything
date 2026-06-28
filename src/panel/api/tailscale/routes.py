"""Tailscale REST API routes (ARCH-003 / TASK-021).

Endpoints:
    GET  /api/tailscale/nodes          — list all nodes; ?stale=true filters to stale only
    GET  /api/tailscale/nodes/{id}     — single node detail
    GET  /api/tailscale/status         — collector last-run status
    POST /api/tailscale/refresh        — trigger an immediate collect run

Response models use Pydantic white-list (NodeResponse) — node_key is never returned.
Stale detection: collected_at distance from now > STALE_THRESHOLD_SECONDS.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from panel.api.deps import get_repo, get_scheduler
from panel.db.repository import Repository
from panel.domain.models import CollectorStatusResponse, NodeResponse, RefreshResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tailscale", tags=["tailscale"])

# Default stale threshold — overridden at request time from app.state.settings when available.
_DEFAULT_STALE_THRESHOLD_SECONDS: int = 90


def _stale_threshold(request: Request) -> int:
    """Read tailscale_stale_threshold_seconds from app.state.settings (fallback default)."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return int(
            getattr(settings, "tailscale_stale_threshold_seconds", _DEFAULT_STALE_THRESHOLD_SECONDS)
        )
    return _DEFAULT_STALE_THRESHOLD_SECONDS


# --------------------------------------------------------------------------- #
# GET /api/tailscale/nodes
# --------------------------------------------------------------------------- #


@router.get("/nodes", response_model=list[NodeResponse])
async def list_nodes(
    request: Request,
    stale: bool = Query(False, description="Only return stale nodes when true"),
    repo: Repository = Depends(get_repo),  # noqa: B008
) -> list[NodeResponse]:
    """Return all Tailscale nodes.

    ``?stale=true`` — only nodes whose collected_at is older than the stale threshold.
    ``is_stale`` is always included in each item regardless of the filter.
    """
    rows = await repo.get_all_nodes()  # type: ignore[attr-defined]
    threshold = _stale_threshold(request)
    now = datetime.now(UTC)
    result: list[NodeResponse] = []
    for row in rows:
        is_stale = (now - row.collected_at).total_seconds() > threshold
        if stale and not is_stale:
            continue
        result.append(
            NodeResponse(
                id=row.id,
                hostname=row.hostname,
                dns_name=row.dns_name,
                tailscale_ips=row.tailscale_ips,
                os=row.os,
                online_state=row.online_state,  # type: ignore[arg-type]
                is_exit_node=row.is_exit_node,
                last_seen=row.last_seen_at,
                is_stale=is_stale,
                updated_at=row.updated_at,
            )
        )
    return result


# --------------------------------------------------------------------------- #
# GET /api/tailscale/nodes/{node_id}
# --------------------------------------------------------------------------- #


@router.get("/nodes/{node_id}", response_model=NodeResponse)
async def get_node(
    node_id: int,
    request: Request,
    repo: Repository = Depends(get_repo),  # noqa: B008
) -> NodeResponse:
    """Return a single Tailscale node by its database id.

    Raises 404 when the node does not exist.
    """
    row = await repo.get_node_by_id(node_id)  # type: ignore[attr-defined]
    if row is None:
        raise HTTPException(status_code=404, detail="Node not found")
    threshold = _stale_threshold(request)
    now = datetime.now(UTC)
    is_stale = (now - row.collected_at).total_seconds() > threshold
    return NodeResponse(
        id=row.id,
        hostname=row.hostname,
        dns_name=row.dns_name,
        tailscale_ips=row.tailscale_ips,
        os=row.os,
        online_state=row.online_state,  # type: ignore[arg-type]
        is_exit_node=row.is_exit_node,
        last_seen=row.last_seen_at,
        is_stale=is_stale,
        updated_at=row.updated_at,
    )


# --------------------------------------------------------------------------- #
# GET /api/tailscale/status
# --------------------------------------------------------------------------- #


@router.get("/status", response_model=CollectorStatusResponse)
async def get_collector_status(
    repo: Repository = Depends(get_repo),  # noqa: B008
) -> CollectorStatusResponse:
    """Return the most recent run record for the tailscale collector.

    Returns ``{"status": "never_run", ...}`` when no run has been recorded yet.
    """
    run = await repo.get_last_run("tailscale")  # type: ignore[attr-defined]
    if run is None:
        return CollectorStatusResponse(
            status="never_run",
            ran_at=None,
            sample_count=0,
            duration_ms=0,
            error=None,
        )
    # ran_at is stored as ISO8601 UTC string in CollectorRunRow
    from panel.db.repository import _parse_utc  # noqa: PLC0415 (local import to avoid circular)

    return CollectorStatusResponse(
        status=run.status,  # type: ignore[arg-type]
        ran_at=_parse_utc(run.ran_at),
        sample_count=run.sample_count,
        duration_ms=run.duration_ms,
        error=run.error,
    )


# --------------------------------------------------------------------------- #
# POST /api/tailscale/refresh
# --------------------------------------------------------------------------- #


@router.post("/refresh", response_model=RefreshResponse)
async def manual_refresh(
    scheduler=Depends(get_scheduler),  # noqa: ANN001, B008
) -> RefreshResponse:
    """Trigger an immediate run of the tailscale collector job.

    Uses ``scheduler.modify_job`` to set next_run_time to now.
    Returns ``triggered=false`` (HTTP 200) when the job is not registered
    (e.g. socket not configured) — does NOT return 500.
    """
    try:
        scheduler.modify_job("tailscale", next_run_time=datetime.now(UTC))
        return RefreshResponse(
            triggered=True,
            message="Tailscale collector scheduled for immediate run",
        )
    except Exception as exc:  # noqa: BLE001
        # JobLookupError (job not found) or other scheduler errors → graceful 200
        logger.warning("tailscale refresh: could not modify job: %s", exc)
        return RefreshResponse(
            triggered=False,
            message=f"Tailscale collector job not available: {type(exc).__name__}",
        )
