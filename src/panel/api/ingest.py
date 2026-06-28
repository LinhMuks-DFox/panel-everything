"""AI 用量摄取端点 (ARCH-004 / TASK-030).

工作站 Reporter 通过 POST /api/ingest/ai-usage 推送各 AI provider 的用量指标，
本端点将其落入通用 latest_snapshot / metric_history 表（collector='ai_usage'，
target_id = ai_provider.id）。

可选 Bearer 鉴权：settings.ingest_token 非空时校验 Authorization 头，不匹配 403；
为空则跳过鉴权（开发/内网默认）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from panel.api.deps import get_repo
from panel.collectors.base import MetricSample
from panel.config.settings import Settings
from panel.db.repository import Repository
from panel.domain.models import AiUsagePayload

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


def _check_auth(settings: Settings, authorization: str | None) -> None:
    """可选 Bearer 鉴权。

    settings.ingest_token 为空 → 跳过校验。
    非空 → 要求 `Authorization: Bearer <token>` 精确匹配，否则 403。
    """
    token = settings.ingest_token
    if not token:
        return
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=403, detail="invalid or missing ingest token")


@router.post("/ai-usage")
async def ingest_ai_usage(
    body: AiUsagePayload,
    request: Request,
    repo: Repository = Depends(get_repo),  # noqa: B008
    authorization: str | None = Header(default=None),  # noqa: B008
) -> dict:
    """接收一批 AI 用量指标并落库。

    返回 {"ok": True, "stored": <样本数>}。未知 provider → 400
    {"ok": False, "error": "unknown provider: <x>"}。
    """
    settings: Settings = request.app.state.settings
    _check_auth(settings, authorization)

    provider_id = await repo.get_ai_provider_id(body.provider)
    if provider_id is None:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"unknown provider: {body.provider}"},
        )

    samples = [
        MetricSample(
            target_id=provider_id,
            metric=item.metric,
            value_num=item.value_num,
            value_text=item.value_text,
            status=body.status,
            collected_at=body.reported_at,
        )
        for item in body.metrics
    ]

    await repo.upsert_snapshot("ai_usage", samples)
    await repo.append_history("ai_usage", samples)

    return {"ok": True, "stored": len(samples)}
