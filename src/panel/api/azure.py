"""Azure / GPU API routes.

Exposes:
    POST   /api/v1/servers         — register a server (credentials NOT returned)
    GET    /api/v1/servers         — list all registered servers
    DELETE /api/v1/servers/{id}    — delete a server by id
    GET    /api/v1/dashboard/azure — aggregated VM + GPU dashboard snapshot

Response model ServerOut is a Pydantic white-list that intentionally omits
ssh_key_path, so the field can never appear in any JSON response even if the
underlying ServerRow dataclass carries it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status

from panel.api.deps import get_gpu_repo, get_repo
from panel.db.gpu_repository import GpuMetricRow, GpuRepository
from panel.db.repository import CollectorRunRow, Repository
from panel.domain.models import (
    CollectorStatusOut,
    DashboardAzureOut,
    DashboardVmOut,
    GpuMetricOut,
    ServerIn,
    ServerOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["servers"])

# ---------------------------------------------------------------------------
# Stale thresholds — 2× / 3× the respective collector intervals.
# ---------------------------------------------------------------------------
VM_STALE_SECONDS: int = 600   # AzureVmCollector interval=300s × 2
GPU_STALE_SECONDS: int = 180  # GpuCollector interval=60s × 3


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_utc(value: str) -> datetime:
    """ISO8601 UTC string → timezone-aware datetime.  Naive strings assumed UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _build_collector_status(
    last_runs: list[CollectorRunRow],
    names: list[str],
) -> dict[str, CollectorStatusOut]:
    """Build a collector-name → CollectorStatusOut mapping for the given names.

    Collectors that have never run are mapped to status="unknown".
    """
    by_name = {r.collector: r for r in last_runs}
    result: dict[str, CollectorStatusOut] = {}
    for name in names:
        run = by_name.get(name)
        if run is None:
            result[name] = CollectorStatusOut(status="unknown", last_ran_at=None, error=None)
        else:
            result[name] = CollectorStatusOut(
                status=run.status,
                last_ran_at=_parse_utc(run.ran_at),
                error=run.error,  # already sanitised by the scheduler layer (ARCH-001)
            )
    return result


def _build_gpu_outs(
    gpu_rows: list[GpuMetricRow],
    now: datetime,
) -> list[GpuMetricOut]:
    """Convert a list of GpuMetricRow to GpuMetricOut, computing is_stale."""
    outs: list[GpuMetricOut] = []
    for row in gpu_rows:
        collected_at = _parse_utc(row.collected_at)
        is_stale = (now - collected_at).total_seconds() > GPU_STALE_SECONDS
        outs.append(
            GpuMetricOut(
                server_id=row.server_id,
                gpu_index=row.gpu_index,
                gpu_name=row.gpu_name,
                util_pct=row.util_pct,
                mem_used_mib=row.mem_used_mib,
                mem_total_mib=row.mem_total_mib,
                mem_pct=row.mem_pct,
                temp_c=row.temp_c,
                power_w=row.power_w,
                collected_at=collected_at,
                is_stale=is_stale,
            )
        )
    return outs


def _row_to_out(row: object) -> ServerOut:
    """Convert a ServerRow dataclass to a ServerOut response model.

    Uses model_validate with from_attributes=True so Pydantic reads the
    dataclass fields directly — ssh_key_path is silently omitted because it
    is not declared in ServerOut.
    """
    from datetime import datetime

    # Parse ISO8601 strings to datetime; ServerRow stores strings.
    return ServerOut(
        id=row.id,  # type: ignore[attr-defined]
        name=row.name,  # type: ignore[attr-defined]
        azure_resource_group=row.azure_resource_group,  # type: ignore[attr-defined]
        azure_vm_name=row.azure_vm_name,  # type: ignore[attr-defined]
        ssh_host=row.ssh_host,  # type: ignore[attr-defined]
        ssh_port=row.ssh_port,  # type: ignore[attr-defined]
        ssh_user=row.ssh_user,  # type: ignore[attr-defined]
        has_gpu=row.has_gpu,  # type: ignore[attr-defined]
        notes=row.notes,  # type: ignore[attr-defined]
        created_at=datetime.fromisoformat(row.created_at),  # type: ignore[attr-defined]
        updated_at=datetime.fromisoformat(row.updated_at),  # type: ignore[attr-defined]
    )


@router.post(
    "/servers",
    response_model=ServerOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a server",
)
async def create_server(
    body: ServerIn,
    repo: GpuRepository = Depends(get_gpu_repo),  # noqa: B008
) -> ServerOut:
    """Register a new monitored server.

    ssh_key_path is written to the database but will never appear in the
    response (ServerOut intentionally omits it).

    Raises:
        409 if a server with the same name already exists.
        500 on unexpected database errors.
    """
    try:
        server_id = await repo.insert_server(body)
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Server name already exists",
        ) from exc
    except Exception as exc:
        # Log without revealing internal paths
        logger.error("create_server: unexpected DB error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from exc

    row = await repo.get_server(server_id)
    if row is None:  # pragma: no cover  — should never happen right after INSERT
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
    return _row_to_out(row)


@router.get(
    "/servers",
    response_model=list[ServerOut],
    summary="List registered servers",
)
async def list_servers(
    repo: GpuRepository = Depends(get_gpu_repo),  # noqa: B008
) -> list[ServerOut]:
    """Return all registered servers ordered by id ascending.

    ssh_key_path is never included in any item of the returned list.
    """
    rows = await repo.get_all_servers()
    return [_row_to_out(r) for r in rows]


@router.delete(
    "/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a server",
)
async def delete_server(
    server_id: int,
    repo: GpuRepository = Depends(get_gpu_repo),  # noqa: B008
) -> None:
    """Delete a registered server by id.

    ON DELETE CASCADE in the DB schema automatically removes associated
    azure_vm_status and gpu_metrics rows.

    Raises:
        404 if no server with the given id exists.
    """
    deleted = await repo.delete_server(server_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found",
        )


# ---------------------------------------------------------------------------
# Dashboard aggregation — pure async helper (reusable by SSR route)
# ---------------------------------------------------------------------------


async def build_azure_dashboard(
    repo: Repository,
    gpu_repo: GpuRepository,
) -> DashboardAzureOut:
    """Aggregate all VM states and latest GPU metrics into a DashboardAzureOut.

    Extracted from the FastAPI handler so that the SSR GET / route (TASK-015)
    can call it directly without an internal HTTP round-trip.

    Always returns a valid model; an empty ``servers`` table produces
    ``vms=[]``.
    """
    now = datetime.now(UTC)

    # 1. All registered servers (ordered by id)
    servers = await gpu_repo.get_all_servers()

    # 2. VM status snapshot indexed by server_id
    vm_status_map = {row.server_id: row for row in await gpu_repo.get_vm_status_all()}

    # 3. Latest GPU metrics indexed by server_id (only for GPU-enabled servers)
    gpu_latest: dict[int, list[GpuMetricRow]] = {}
    for server in servers:
        if server.has_gpu:
            gpu_latest[server.id] = await gpu_repo.get_latest_gpu_metrics(server.id)

    # 4. Collector run status
    last_runs = await repo.get_all_last_runs()
    collector_status = _build_collector_status(last_runs, ["azure_vm", "gpu"])

    # 5. Assemble DashboardVmOut list
    vms: list[DashboardVmOut] = []
    for server in servers:
        vm_row = vm_status_map.get(server.id)
        if vm_row is None:
            # Server registered but never collected: mark as stale placeholder.
            vm_out = DashboardVmOut(
                server_id=server.id,
                name=server.name,
                azure_vm_name=server.azure_vm_name,
                azure_resource_group=server.azure_resource_group,
                power_state="Unknown",
                power_state_raw=None,
                is_running=False,
                collected_at=now,
                is_stale=True,
                gpus=[],
            )
        else:
            collected_at = _parse_utc(vm_row.collected_at)
            is_stale = (now - collected_at).total_seconds() > VM_STALE_SECONDS
            gpu_outs = _build_gpu_outs(gpu_latest.get(server.id, []), now)
            vm_out = DashboardVmOut(
                server_id=server.id,
                name=server.name,
                azure_vm_name=server.azure_vm_name,
                azure_resource_group=server.azure_resource_group,
                power_state=vm_row.power_state,
                power_state_raw=vm_row.power_state_raw,
                is_running=vm_row.is_running,
                collected_at=collected_at,
                is_stale=is_stale,
                gpus=gpu_outs,
            )
        vms.append(vm_out)

    return DashboardAzureOut(
        fetched_at=now,
        collector_status=collector_status,
        vms=vms,
    )


# ---------------------------------------------------------------------------
# Dashboard aggregation endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/dashboard/azure",
    response_model=DashboardAzureOut,
    summary="Azure + GPU dashboard snapshot",
)
async def get_azure_dashboard(
    repo: Repository = Depends(get_repo),          # noqa: B008
    gpu_repo: GpuRepository = Depends(get_gpu_repo),  # noqa: B008
) -> DashboardAzureOut:
    """Return a single-call aggregate of all registered VM states and their
    latest GPU metrics, plus health status for both collectors.

    Designed for front-end VmCard / GpuCard rendering — eliminates multiple
    round-trips.  Always returns HTTP 200; an empty servers table produces
    ``vms=[]``.
    """
    return await build_azure_dashboard(repo=repo, gpu_repo=gpu_repo)
