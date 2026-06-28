"""Health check endpoint.

GET /healthz -> 200 {"status": "ok", "db": "ok"|"down", "time": "<iso8601 UTC>"}

TASK-002 接入:`db` 字段对 app.state.db 执行 `SELECT 1`,成功 "ok",异常/无连接 "down"。
整体始终返回 200,内容反映 db 状态(供容器 HEALTHCHECK)。三字段 schema 保持不变。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

router = APIRouter()


async def _probe_db(request: Request) -> str:
    """对 app.state.db 执行 SELECT 1;成功返回 "ok",否则 "down"。"""
    conn = getattr(request.app.state, "db", None)
    if conn is None:
        return "down"
    try:
        async with conn.execute("SELECT 1") as cur:
            await cur.fetchone()
    except Exception:  # noqa: BLE001 (健康探测:任何异常都视为 db down)
        return "down"
    return "ok"


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """Liveness/readiness probe used by the container HEALTHCHECK."""
    return {
        "status": "ok",
        "db": await _probe_db(request),
        "time": datetime.now(UTC).isoformat(),
    }
