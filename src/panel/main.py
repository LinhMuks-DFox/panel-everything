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
from panel.config.scrub import setup_logging
from panel.config.settings import Settings, get_settings


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
    # TASK-002: 在此初始化 DB 连接并挂到 app.state.db / app.state.repo。
    # TASK-003: 在此 register_collectors(...) + build_scheduler(...).start()。
    try:
        yield
    finally:
        # TASK-003: scheduler.shutdown(wait=False)
        # TASK-002: await app.state.db.close()
        pass


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

    # --- TASK-004: 静态资源挂载点 ---
    # from fastapi.staticfiles import StaticFiles
    # from pathlib import Path
    # static_dir = Path(__file__).parent / "web" / "static"
    # app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # --- 路由集中挂载 ---
    app.include_router(health.router)
    # TASK-004: app.include_router(web.routes.router)   # SSR GET /
    # ARCH-002/003: 各模块在此集中 include_router(...)。

    return app


# 模块级实例供 `uvicorn panel.main:app` 引用。
app = create_app()


def main() -> None:
    """本地/容器启动入口:uvicorn + uvloop,单 worker。"""
    import uvicorn

    uvicorn.run(
        "panel.main:app",
        host="0.0.0.0",  # noqa: S104 (容器内监听;外网由 Tailscale 隔离)
        port=8080,
        loop="uvloop",
        workers=1,
    )


if __name__ == "__main__":
    main()
