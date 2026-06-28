"""Application entrypoint: create_app() factory + lifespan + uvicorn (uvloop).

ARCH-001 装配契约。本卡(TASK-001)提供最小可用骨架:
- create_app() 工厂,挂载 /healthz。
- lifespan 留好 DB 初始化与 scheduler 启停的挂载点(标注 TODO),
  由 TASK-002(DB)/TASK-003(scheduler)/TASK-004(SSR+static)接入。
- 模块级 `app = create_app()` 供 `uvicorn panel.main:app` 加载。

后续 Coder 接入点(签名稳定,请勿改动 create_app/lifespan 函数签名):
    create_app(settings: Settings | None = None) -> FastAPI
    lifespan(app: FastAPI)  # asynccontextmanager
在 lifespan 内按标注的 TODO 顺序挂载 db/repo/scheduler 到 app.state。
在 create_app 内按标注的 TODO 集中 include_router / mount StaticFiles。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from panel.api import health
from panel.api.ai_usage import router as ai_usage_router
from panel.api.azure import router as azure_router
from panel.api.ingest import router as ingest_router
from panel.api.tailscale.routes import router as tailscale_router
from panel.collectors import register_collectors
from panel.collectors.gpu.downsampler import run_1h_downsample, run_5m_downsample
from panel.collectors.retention import prune_metric_history
from panel.collectors.scheduler import build_scheduler
from panel.config.scrub import setup_logging
from panel.config.settings import Settings, get_settings
from panel.db import connection, migrate
from panel.db.gpu_repository import GpuRepository
from panel.db.repository import Repository
from panel.web import routes as web_routes


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201 (asynccontextmanager 推断返回类型)
    """Application lifespan: 启动时初始化资源,关闭时清理。

    本卡为最小占位。后续 Coder 在标注处按 ARCH-001 装配:

    # --- TASK-002: DB 初始化挂载点 ---
    # settings = get_settings()
    # conn = await db.connection.connect(settings.db_path)   # 开 WAL
    # await db.migrate.run(conn)                              # 幂等建表
    # repo = Repository(conn)
    # app.state.db = conn
    # app.state.repo = repo

    # --- TASK-003: 采集器注册 + scheduler 启停挂载点 ---
    # register_collectors(settings, repo)   # 各模块工厂集中注册
    # scheduler = build_scheduler(repo)
    # scheduler.start()
    # app.state.scheduler = scheduler

    关闭阶段(finally)对应清理:
    # scheduler.shutdown(wait=False)
    # await conn.close()
    """
    # Use pre-injected settings if create_app stored them (e.g. in tests), else fall back.
    settings = getattr(app.state, "settings", None) or get_settings()
    # Expose settings on app.state so request handlers (e.g. SSR routes reading
    # stale_threshold_seconds) can pick up config rather than falling back to defaults.
    app.state.settings = settings

    # --- TASK-002: DB 初始化(WAL 连接 + 幂等建表),挂到 app.state ---
    conn = await connection.connect(settings.db_path)  # 开 WAL
    await migrate.run(conn)  # 幂等建表
    app.state.db = conn
    app.state.repo = Repository(conn)
    # --- TASK-011: GpuRepository 初始化(ARCH-002 专用表) ---
    app.state.gpu_repo = GpuRepository(conn)

    # --- TASK-003 / TASK-012: 采集器注册 + scheduler 启停 ---
    # 先集中注册各模块采集器(azure/gpu/...,各工厂按配置自行启停),再读 registry 装配调度。
    register_collectors(settings, app.state.repo, app.state.gpu_repo)
    scheduler = build_scheduler(app.state.repo)
    # --- TASK-016: GPU 历史降采样 job(5min / 1h) ---
    scheduler.add_job(
        run_5m_downsample,
        "interval",
        minutes=5,
        args=[app.state.gpu_repo],
        id="gpu_downsample_5m",
    )
    scheduler.add_job(
        run_1h_downsample,
        "interval",
        hours=1,
        args=[app.state.gpu_repo],
        id="gpu_downsample_1h",
    )
    # --- TASK-040: 通用 metric_history retention job(每日) ---
    scheduler.add_job(
        prune_metric_history,
        "interval",
        days=1,
        args=[app.state.repo, settings.history_retention_days],
        id="metric_history_retention",
    )
    scheduler.start()
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        # 先停调度(不等待在跑的任务),再关 DB 连接,顺序见 ARCH-001。
        scheduler.shutdown(wait=False)
        await conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        settings: 可选,显式注入 Settings(测试用)。
            由 TASK-005 接入:传入的 settings 优先,否则调 get_settings()。
    """
    resolved = settings or get_settings()
    setup_logging(resolved.log_level)

    app = FastAPI(
        title="Panel Everything",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Pre-store settings on app.state so lifespan can pick up injected settings
    # (e.g. test-specific db_path) without changing the lifespan signature.
    app.state.settings = resolved

    # --- TASK-004: 静态资源挂载 ---
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- 路由集中挂载 ---
    app.include_router(health.router)
    app.include_router(web_routes.router)          # TASK-004: SSR GET /
    app.include_router(azure_router)               # TASK-011: /api/v1/servers
    app.include_router(tailscale_router)           # TASK-021: /api/tailscale
    app.include_router(ingest_router)              # TASK-030: /api/ingest/ai-usage
    app.include_router(ai_usage_router)            # TASK-033: GET /api/ai-usage
    # ARCH-004: 各模块在此集中 include_router(...)。

    return app


# 模块级实例供 `uvicorn panel.main:app` 引用。
app = create_app()


def main() -> None:
    """本地/容器启动入口:uvicorn + uvloop,单 worker。"""
    import uvicorn

    uvicorn.run(
        "panel.main:app",
        host="0.0.0.0",  # noqa: S104 (容器内监听;外网由 Tailscale 隔离)
        port=get_settings().port,
        loop="uvloop",
        workers=1,
    )


if __name__ == "__main__":
    main()
