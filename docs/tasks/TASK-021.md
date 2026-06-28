---
id: TASK-021
title: "Tailscale REST API"
status: done
priority: P1
architecture: ARCH-003
dependencies: [TASK-020]
estimated_effort: S
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 Tailscale 模块的 REST API，供前端轮询和手动刷新使用。
所有端点返回 JSON，由 Pydantic 响应模型白名单输出（不暴露 `node_key` 等内部字段）。

## 技术规格

### 文件路径

- `src/panel/api/tailscale/__init__.py`
- `src/panel/api/tailscale/routes.py` — `APIRouter(prefix="/api/tailscale")`
- `src/panel/domain/models.py` — 追加 `NodeResponse`、`CollectorStatusResponse`、`RefreshResponse`
- `tests/api/test_tailscale_api.py`

### 端点一览

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/tailscale/nodes` | 返回全部节点列表，可附 `?stale=true` 过滤 |
| GET | `/api/tailscale/nodes/{node_id}` | 单节点详情 |
| GET | `/api/tailscale/status` | 采集器最近运行状态 |
| POST | `/api/tailscale/refresh` | 触发立即采集一次 |

### Pydantic 响应模型（domain/models.py 追加）

```python
from __future__ import annotations
from pydantic import BaseModel
from datetime import datetime
from typing import Literal

class NodeResponse(BaseModel):
    id: int
    hostname: str
    dns_name: str | None
    tailscale_ips: list[str]
    os: str | None
    online_state: Literal["ONLINE", "OFFLINE", "LONG_OFFLINE"]
    is_exit_node: bool
    last_seen: datetime | None      # UTC，前端可格式化
    is_stale: bool
    updated_at: datetime

class CollectorStatusResponse(BaseModel):
    status: Literal["up", "down", "error", "never_run"]
    ran_at: datetime | None
    sample_count: int
    duration_ms: int
    error: str | None               # 脱敏后，None 表无错误

class RefreshResponse(BaseModel):
    triggered: bool
    message: str
```

### 路由实现要点（routes.py）

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timezone
from panel.api.deps import get_repo, get_scheduler
from panel.domain.models import NodeResponse, CollectorStatusResponse, RefreshResponse
from panel.db.repository import Repository

router = APIRouter(prefix="/api/tailscale", tags=["tailscale"])

STALE_THRESHOLD_SECONDS = 90   # 从 settings 注入，此处为默认值

@router.get("/nodes", response_model=list[NodeResponse])
async def list_nodes(
    stale: bool = Query(False, description="仅返回 stale 节点时传 true"),
    repo: Repository = Depends(get_repo),
) -> list[NodeResponse]:
    rows = await repo.get_all_nodes()
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        is_stale = (now - row.collected_at).total_seconds() > STALE_THRESHOLD_SECONDS
        if stale and not is_stale:
            continue
        result.append(NodeResponse(
            id=row.id,
            hostname=row.hostname,
            dns_name=row.dns_name,
            tailscale_ips=row.tailscale_ips,
            os=row.os,
            online_state=row.online_state,
            is_exit_node=row.is_exit_node,
            last_seen=row.last_seen_at,
            is_stale=is_stale,
            updated_at=row.updated_at,
        ))
    return result

@router.get("/nodes/{node_id}", response_model=NodeResponse)
async def get_node(
    node_id: int,
    repo: Repository = Depends(get_repo),
) -> NodeResponse:
    row = await repo.get_node_by_id(node_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Node not found")
    now = datetime.now(timezone.utc)
    is_stale = (now - row.collected_at).total_seconds() > STALE_THRESHOLD_SECONDS
    return NodeResponse(...)  # 同上映射

@router.get("/status", response_model=CollectorStatusResponse)
async def get_collector_status(repo: Repository = Depends(get_repo)) -> CollectorStatusResponse:
    run = await repo.get_last_run("tailscale")   # get_all_last_runs 筛选
    if run is None:
        return CollectorStatusResponse(status="never_run", ran_at=None,
                                       sample_count=0, duration_ms=0, error=None)
    return CollectorStatusResponse(
        status=run.status, ran_at=run.ran_at,
        sample_count=run.sample_count, duration_ms=run.duration_ms,
        error=run.error,   # 已在 record_collector_run 时脱敏
    )

@router.post("/refresh", response_model=RefreshResponse)
async def manual_refresh(
    scheduler: AsyncIOScheduler = Depends(get_scheduler),
) -> RefreshResponse:
    from apscheduler.jobstores.base import JobLookupError
    from datetime import datetime, timezone
    try:
        scheduler.modify_job("tailscale", next_run_time=datetime.now(timezone.utc))
        return RefreshResponse(triggered=True, message="Tailscale collector scheduled for immediate run")
    except JobLookupError:
        return RefreshResponse(triggered=False, message="Tailscale collector job not found")
```

### 路由注册（main.py）

```python
from panel.api.tailscale.routes import router as tailscale_router
app.include_router(tailscale_router)
```

### Deps（api/deps.py 追加）

```python
def get_scheduler(request: Request) -> AsyncIOScheduler:
    return request.app.state.scheduler
```

## 实现指引

1. 在 `domain/models.py` 中添加三个响应模型，遵循白名单原则（不含 `node_key`）。
2. 创建 `api/tailscale/__init__.py`（空文件）和 `routes.py`，按上述规格实现四个端点。
3. `STALE_THRESHOLD_SECONDS` 从 `settings.tailscale_stale_threshold_seconds` 读取；为避免循环依赖，在 `routes.py` 通过 `Depends(get_settings)` 或模块初始化时从 `app.state` 读取。
4. `get_collector_status` 端点调用 `repo.get_all_last_runs()` 后过滤 `collector=="tailscale"`，或为 repository 新增 `get_last_run(collector: str) -> CollectorRunRow | None` 辅助方法。
5. `/refresh` 端点使用 `scheduler.modify_job(job_id="tailscale", next_run_time=now)` 触发立即执行；job_id 与 `TailscaleCollector.name` 一致（均为 `"tailscale"`）。
6. 在 `main.py` 中 `include_router(tailscale_router)` — 与其他模块路由并列。
7. `stale=true` 查询参数的语义：不是过滤掉 stale 节点，而是**只返回** stale 节点（供 StaleWarning 组件判断是否显示横幅）。`is_stale` 字段在所有响应中均存在，无论 `stale` 参数值。

## 测试要求

- [ ] `GET /api/tailscale/nodes` 返回所有节点，HTTP 200，响应中每条含 `is_stale` 字段。
- [ ] `GET /api/tailscale/nodes?stale=true` 仅返回 `is_stale=True` 的节点（mock collected_at 为过去 120s）。
- [ ] `GET /api/tailscale/nodes/1` 返回正确节点；`GET /api/tailscale/nodes/9999` 返回 404。
- [ ] `GET /api/tailscale/status` 在无采集记录时返回 `{"status": "never_run", ...}`。
- [ ] `POST /api/tailscale/refresh` 调用 `scheduler.modify_job`，返回 `{"triggered": true, ...}`；job 不存在时返回 `{"triggered": false, ...}`（不抛 500）。
- [ ] 响应体不含 `node_key` 字段（白名单检查）。

## 完成标准

- [ ] 四个端点均可通过 `httpx.AsyncClient(app=app)` 集成测试，无 5xx。
- [ ] `NodeResponse` 不暴露 `node_key`（Pydantic 白名单验证）。
- [ ] `/refresh` 端点在 job 不存在时返回 200 + `triggered=false`，不返回 500。
- [ ] 路由已在 `main.py` 中注册，`GET /api/tailscale/nodes` 在启动后可访问。
- [ ] `ruff check` 零 error，相关测试全绿。
