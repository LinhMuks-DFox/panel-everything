---
id: TASK-001
title: "项目骨架 + Dockerfile + compose + /healthz(声明全量依赖)"
status: review
priority: P0
architecture: ARCH-001
dependencies: []
estimated_effort: M
executed_by: claude-opus-4-8[1m]
created: 2026-06-28
updated: 2026-06-28
---

## 目标

搭建 Panel Everything 的工程骨架,使 `docker compose up` 能在 arm64/amd64 起容器,`GET /healthz` 返回 200,并**一次性声明项目全量依赖**(后续模块卡不再改 `pyproject.toml`,避免合并冲突)。这是所有其它任务的地基。

## 技术规格

### 项目结构(按 ARCH-001 分层创建空骨架)

```
panel_everything/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .dockerignore
├── src/panel/
│   ├── __init__.py
│   ├── main.py                 # create_app() + lifespan + uvloop 启动
│   ├── config/__init__.py
│   ├── db/__init__.py
│   ├── collectors/__init__.py
│   ├── api/__init__.py
│   │   └── health.py           # GET /healthz
│   ├── web/__init__.py
│   └── domain/__init__.py
└── tests/
    └── test_health.py
```

> 本卡只建骨架与 health 端点;`db/`、`collectors/`、`web/`、`config/`、`domain/` 的实质内容由 TASK-002~005 填充。`lifespan` 本卡可先放最小可用版本(只做占位),TASK-002/003 再接入 DB 与 scheduler。

### pyproject.toml 全量依赖(权威清单,一次写全)

运行时依赖:

```
fastapi
uvicorn[standard]
uvloop
jinja2
aiosqlite
apscheduler
httpx
aiohttp
asyncssh
azure-identity
azure-mgmt-compute
pydantic
pydantic-settings
python-multipart
```

开发依赖(`[project.optional-dependencies].dev` 或 `[dependency-groups]`):

```
pytest
pytest-asyncio
pytest-cov
ruff
```

- Python 要求:`requires-python = ">=3.12"`。
- 包名 `panel`,`src/` 布局(`[tool.hatchling]` 或等价 build backend 指向 `src/panel`)。
- 配置 `ruff`(line-length、目标版本 py312)与 `pytest`(`asyncio_mode = "auto"`)。

### /healthz 端点(api/health.py)

```
GET /healthz → 200 {"status": "ok", "db": "ok"|"down", "time": "<iso8601 UTC>"}
```

- 本卡 DB 尚未接入,`db` 字段可先固定 `"ok"`(占位);TASK-002 接入后改为真实 `SELECT 1` 探测。保持响应 schema 不变。

### Dockerfile(多阶段、多 arch、非 root)

- base:`python:3.12-slim-bookworm`。
- builder 阶段:装运行时依赖。
- runtime 阶段:拷贝已装依赖 + `src/`;创建非特权用户 `app` 并 `USER app`。
- 暴露 `8080`;启动 `uvicorn panel.main:app --host 0.0.0.0 --port 8080`(uvloop 由 main 选用)。
- `HEALTHCHECK` 调 `/healthz`。
- 构建命令文档化:`docker buildx build --platform linux/arm64,linux/amd64 ...`。

### docker-compose.yml

按 ARCH-001 部署方案:`restart: unless-stopped`、`ports 8080:8080`、`env_file: .env`、`volumes ./data:/data`、`mem_limit`(Pi5 384–512M,选 compose 上实际生效的写法)、`healthcheck` 调 /healthz。Tailscale socket 卷与 secrets 卷先在文件里以注释形式预留(模块卡按需启用)。

### .env.example

至少含:`PANEL_DB_PATH=/data/panel.db`、`PANEL_LOG_LEVEL=info`、`PANEL_PORT=8080`。其余模块配置项由 TASK-005 与各模块卡补充(以注释占位)。

## 实现指引

1. 先写 `pyproject.toml`(全量依赖 + build backend + ruff/pytest 配置),本地 `pip install -e .[dev]` 验证可装。
2. `src/panel/main.py`:实现 `create_app()` 返回 `FastAPI(lifespan=lifespan)`,`include_router(health.router)`;`lifespan` 暂为最小占位(`yield`)。提供 `app = create_app()` 模块级实例供 uvicorn 引用。uvloop 在 `if __name__ == "__main__"` 或入口处 `uvicorn.run(..., loop="uvloop")`。
3. `api/health.py`:`APIRouter`,`@router.get("/healthz")`,返回上述 JSON,`time` 用 `datetime.now(UTC).isoformat()`。
4. Dockerfile 多阶段;`.dockerignore` 排除 `.git/`、`tests/`、`data/`、`__pycache__`、`*.md`(保留必要文件)。
5. compose 落 `mem_limit` 时实测 Pi(或 amd64 模拟)生效;若用 compose v3 需用兼容写法,记录在 .env.example 注释或 README 片段(本卡不写 README 正文)。
6. `tests/test_health.py`:用 `httpx.ASGITransport` + `AsyncClient` 调 `/healthz` 断言 200 与 schema。

## 测试要求

- [ ] `pip install -e .[dev]` 成功安装全部运行时与开发依赖
- [ ] `ruff check src tests` 无错误
- [ ] `pytest` 通过,`test_health.py` 断言 `/healthz` 返回 200 且含 `status/db/time` 字段
- [ ] `docker buildx build --platform linux/arm64,linux/amd64 .` 构建成功(至少 arm64 成功)
- [ ] `docker compose up` 起容器,容器内 `GET /healthz` 返回 200,容器 healthcheck 变 healthy
- [ ] 容器以非 root 用户运行(`docker exec ... whoami` 非 root)

## 完成标准

- [ ] 目录结构与 ARCH-001 分层一致,`pyproject.toml` 含全量依赖清单(运行时 14 + 开发 4)
- [ ] `create_app()` 工厂存在,`app` 模块级实例可被 uvicorn 加载
- [ ] `/healthz` 端点按规格返回
- [ ] Dockerfile 多阶段、多 arch、非 root、含 HEALTHCHECK
- [ ] docker-compose.yml 含 restart/mem_limit/volumes/healthcheck
- [ ] `.env.example` 提供基础配置项
- [ ] 全部测试与 ruff 通过
