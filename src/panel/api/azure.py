"""Azure / GPU API routes.

Exposes:
    POST   /api/v1/servers         — register a server (credentials NOT returned)
    GET    /api/v1/servers         — list all registered servers
    DELETE /api/v1/servers/{id}    — delete a server by id

Response model ServerOut is a Pydantic white-list that intentionally omits
ssh_key_path, so the field can never appear in any JSON response even if the
underlying ServerRow dataclass carries it.
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status

from panel.api.deps import get_gpu_repo
from panel.db.gpu_repository import GpuRepository
from panel.domain.models import ServerIn, ServerOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["servers"])


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
