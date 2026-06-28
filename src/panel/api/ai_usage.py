"""AI 额度展示端点 (ARCH-004 / TASK-033).

GET /api/ai-usage 从通用 latest_snapshot(collector='ai_usage')聚合各 AI
provider 的最新用量,与 ai_provider 表的静态元数据(display_name/source_type/
window_seconds)合并,统一成 AiProviderStatus 列表返回。

聚合逻辑集中在 get_ai_usage_data(repo) 中,既供本 HTTP 端点,也供 web/routes.py
的 SSR index() 直接复用(避免 HTTP 往返)。

stale 判断:(now - collected_at) > window_seconds * 0.5,或上报 status='error'。
no_data:provider 配置存在但 latest_snapshot 中无任何该 provider 的行。
metric_unit 推断:有 used_requests → 'requests';有 used_tokens → 'tokens';
两者都没有 → 'unknown'。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from panel.api.deps import get_repo
from panel.db.repository import Repository, SnapshotRow
from panel.domain.models import AiProviderStatus, AiUsageResponse

router = APIRouter(prefix="/api", tags=["ai-usage"])

# latest_snapshot 中 ai_usage collector 使用的指标名(见 TASK-030 / reporter)。
_AI_COLLECTOR = "ai_usage"


def _parse_utc(value: str) -> datetime:
    """ISO8601 字符串 → 带 tz 的 datetime(UTC)。无 tz 视为 UTC。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _format_age(seconds: float) -> str:
    """把秒数格式化为紧凑的 'Xh Ym' / 'Ym' / 'Xs' 标签(向下取整)。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_min = minutes % 60
    return f"{hours}h {rem_min}m"


def _window_label(window_seconds: int) -> str:
    """把窗口秒数渲染成人类可读标签,如 '5h 窗口' / '180m 窗口'。"""
    if window_seconds <= 0:
        return "—"
    if window_seconds % 3600 == 0:
        return f"{window_seconds // 3600}h 窗口"
    if window_seconds % 60 == 0:
        return f"{window_seconds // 60}m 窗口"
    return f"{window_seconds}s 窗口"


def _build_provider_status(
    *,
    provider: str,
    display_name: str,
    source_type: str,
    window_seconds: int,
    rows: list[SnapshotRow],
    now: datetime,
) -> AiProviderStatus:
    """把某 provider 的若干 metric 快照行聚合成一条 AiProviderStatus。

    rows 为同一 target_id 下的全部 metric 行;空列表 → status='no_data'。
    """
    if not rows:
        return AiProviderStatus(
            provider=provider,
            display_name=display_name,
            source_type=source_type,
            used_percent=None,
            used_value=None,
            limit_value=None,
            metric_unit="unknown",
            resets_at=None,
            window_label=_window_label(window_seconds),
            stale=False,
            stale_since=None,
            stale_age_label=None,
            collected_at=None,
            status="no_data",
        )

    # 按 metric 名索引(同一 metric 只会有一行,latest_snapshot UNIQUE 约束)。
    by_metric: dict[str, SnapshotRow] = {r.metric: r for r in rows}

    def num(metric: str) -> float | None:
        row = by_metric.get(metric)
        return row.value_num if row is not None else None

    def text(metric: str) -> str | None:
        row = by_metric.get(metric)
        return row.value_text if row is not None else None

    # metric_unit + used_value/limit_value 统一(requests 优先于 tokens)。
    used_requests = num("used_requests")
    used_tokens = num("used_tokens")
    if used_requests is not None:
        metric_unit = "requests"
        used_value = used_requests
        limit_value = num("limit_requests")
    elif used_tokens is not None:
        metric_unit = "tokens"
        used_value = used_tokens
        limit_value = num("limit_tokens")
    else:
        metric_unit = "unknown"
        used_value = None
        limit_value = None

    used_percent = num("used_percent")
    resets_at = text("resets_at")

    # 窗口秒数:provider 上报覆盖配置默认值。
    reported_window = num("window_seconds")
    effective_window = (
        int(reported_window) if reported_window and reported_window > 0 else window_seconds
    )

    # collected_at / status 取任意一行(同 provider 同批上报共享)。
    sample = rows[0]
    collected_at = sample.collected_at
    upstream_status = sample.status

    # stale 判断:数据超过窗口一半 或 上报状态为 error。
    stale = False
    stale_since: str | None = None
    stale_age_label: str | None = None
    if upstream_status == "error":
        stale = True
    else:
        try:
            collected_dt = _parse_utc(collected_at)
            age = (now - collected_dt).total_seconds()
            if effective_window > 0 and age > effective_window * 0.5:
                stale = True
        except (ValueError, TypeError):
            stale = True

    if stale:
        stale_since = collected_at
        try:
            stale_age_label = _format_age((now - _parse_utc(collected_at)).total_seconds())
        except (ValueError, TypeError):
            stale_age_label = None

    return AiProviderStatus(
        provider=provider,
        display_name=display_name,
        source_type=source_type,
        used_percent=used_percent,
        used_value=used_value,
        limit_value=limit_value,
        metric_unit=metric_unit,
        resets_at=resets_at,
        window_label=_window_label(effective_window),
        stale=stale,
        stale_since=stale_since,
        stale_age_label=stale_age_label,
        collected_at=collected_at,
        status="error" if upstream_status == "error" else "ok",
    )


async def get_ai_usage_data(repo: Repository) -> AiUsageResponse:
    """聚合所有 enabled provider 的最新用量为 AiUsageResponse。

    供 GET /api/ai-usage 与 SSR index() 复用。无数据的 provider 仍以
    status='no_data' 的空卡形式返回(保证空卡提示渲染)。
    """
    now = datetime.now(UTC)

    providers = await repo.get_ai_providers()
    snapshot_rows = await repo.get_snapshot(_AI_COLLECTOR)

    # 按 target_id 分组快照行。
    rows_by_target: dict[int, list[SnapshotRow]] = {}
    for row in snapshot_rows:
        rows_by_target.setdefault(row.target_id, []).append(row)

    statuses: list[AiProviderStatus] = []
    latest: datetime | None = None
    for p in providers:
        rows = rows_by_target.get(p.id, [])
        status = _build_provider_status(
            provider=p.provider,
            display_name=p.display_name,
            source_type=p.source_type,
            window_seconds=p.window_seconds,
            rows=rows,
            now=now,
        )
        statuses.append(status)
        # 跟踪全局最新 collected_at(用于 last_updated)。
        if status.collected_at is not None:
            try:
                dt = _parse_utc(status.collected_at)
                if latest is None or dt > latest:
                    latest = dt
            except (ValueError, TypeError):
                pass

    return AiUsageResponse(
        providers=statuses,
        last_updated=latest.isoformat() if latest is not None else None,
    )


@router.get("/ai-usage")
async def get_ai_usage(
    repo: Repository = Depends(get_repo),  # noqa: B008
) -> AiUsageResponse:
    """返回所有 AI provider 的最新用量聚合 + stale 判断。"""
    return await get_ai_usage_data(repo)
