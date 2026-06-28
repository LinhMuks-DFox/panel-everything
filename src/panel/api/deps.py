"""Dependency injection helpers for FastAPI route handlers.

ARCH-001 contract: all request-scoped dependencies that read from app.state live here.
"""

from __future__ import annotations

from fastapi import Request

from panel.db.gpu_repository import GpuRepository
from panel.db.repository import Repository


async def get_repo(request: Request) -> Repository:
    """Return the shared Repository instance stored in app.state."""
    return request.app.state.repo


async def get_gpu_repo(request: Request) -> GpuRepository:
    """Return the shared GpuRepository instance stored in app.state.gpu_repo."""
    return request.app.state.gpu_repo
