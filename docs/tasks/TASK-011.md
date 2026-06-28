---
id: TASK-011
title: "服务器注册 CRUD API（凭证不回传）"
status: review
priority: P1
architecture: ARCH-002
dependencies: [TASK-010, TASK-005]
estimated_effort: M
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 `/api/v1/servers` 的 POST / GET / DELETE 三个端点，允许用户通过 API 注册、查询、删除监控的 Azure 服务器。`ssh_key_path` 字段写入 DB，但通过 Pydantic response model 白名单机制不出现在任何 API 响应中。同时实现 `GpuRepository` 中的服务器 CRUD 方法，并将 `api/azure.py` 路由挂入主 app。

## 技术规格

### 文件路径

| 文件 | 说明 |
|------|------|
| `src/panel/api/azure.py` | APIRouter 定义，挂 `/api/v1` prefix |
| `src/panel/db/gpu_repository.py` | 服务器 CRUD + VM/GPU 读写（本卡实现 server 部分） |
| `src/panel/domain/models.py` | `ServerIn` / `ServerOut` Pydantic 模型 |
| `src/panel/main.py` | `include_router(azure_router)` |

### Pydantic 模型

```python
# domain/models.py
from pydantic import BaseModel, ConfigDict
from datetime import datetime

class ServerIn(BaseModel):
    name: str
    azure_resource_group: str | None = None
    azure_vm_name: str | None = None
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str = "azureuser"
    ssh_key_path: str | None = None   # 写 DB，不出现在 ServerOut
    has_gpu: bool = False
    notes: str | None = None

class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    azure_resource_group: str | None
    azure_vm_name: str | None
    ssh_host: str | None
    ssh_port: int
    ssh_user: str
    # ssh_key_path 字段故意缺失
    has_gpu: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime
```

### GpuRepository 服务器 CRUD 方法

```python
# db/gpu_repository.py
class GpuRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None: ...

    async def insert_server(self, data: ServerIn) -> int:
        """INSERT INTO servers ...; 返回新行 id。name 重复抛 aiosqlite.IntegrityError。"""

    async def get_all_servers(self) -> list[ServerRow]:
        """SELECT * FROM servers ORDER BY id;"""

    async def get_server(self, server_id: int) -> ServerRow | None:
        """SELECT * FROM servers WHERE id=?;"""

    async def delete_server(self, server_id: int) -> bool:
        """DELETE FROM servers WHERE id=?; 返回是否实际删除了记录。"""
```

`ServerRow` 是 `@dataclass(slots=True)`，字段与 servers 表列对应（含 `ssh_key_path`）；转换为 `ServerOut` 时 Pydantic 自动过滤。

### API 端点

```python
# api/azure.py
from fastapi import APIRouter, Depends, HTTPException, status
router = APIRouter(prefix="/api/v1", tags=["servers"])

@router.post("/servers", response_model=ServerOut, status_code=201)
async def create_server(body: ServerIn, repo: GpuRepository = Depends(get_gpu_repo)): ...

@router.get("/servers", response_model=list[ServerOut])
async def list_servers(repo: GpuRepository = Depends(get_gpu_repo)): ...

@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: int, repo: GpuRepository = Depends(get_gpu_repo)): ...
```

`get_gpu_repo` 依赖从 `request.app.state.gpu_repo` 取出（与 ARCH-001 `deps.py` 模式对齐）。

### 挂载到主 app

```python
# main.py create_app() 中追加
from panel.api.azure import router as azure_router
app.include_router(azure_router)
```

`lifespan` 中在 `connect(db_path)` + `migrate.run()` 后构造 `GpuRepository(conn)` 存入 `app.state.gpu_repo`。

### 错误处理

| 情况 | HTTP 状态 | 细节 |
|------|-----------|------|
| name 重复 | 409 Conflict | `{"detail": "Server name already exists"}` |
| DELETE id 不存在 | 404 Not Found | `{"detail": "Server not found"}` |
| 数据库错误 | 500 | 日志记录，响应不含内部路径（脱敏） |

## 实现指引

1. 在 `domain/models.py` 创建 `ServerIn` / `ServerOut`（若文件已存在则追加）。
2. 创建 `db/gpu_repository.py`，实现 `GpuRepository` 类。注意 `aiosqlite` 异步上下文：`async with self.conn.execute(...) as cur:`。
3. 创建 `api/azure.py`，定义 `router`，实现三个端点。`create_server` 捕获 `aiosqlite.IntegrityError` 转 409。`delete_server` 根据 `delete_server()` 返回值决定返回 204 或 404。
4. 在 `api/deps.py`（ARCH-001 已有）中增加 `get_gpu_repo` 依赖函数：
   ```python
   async def get_gpu_repo(request: Request) -> GpuRepository:
       return request.app.state.gpu_repo
   ```
5. 修改 `main.py`：`lifespan` 中追加 `GpuRepository` 初始化；`create_app` 中 `include_router`。
6. `ssh_key_path` 的值经 ARCH-001 的日志脱敏规则处理（不记录路径内容，只记录是否存在）。
7. 响应序列化时 FastAPI 使用 `response_model=ServerOut`，`ssh_key_path` 字段因不在 `ServerOut` 中而被自动过滤。无需额外代码。

## 测试要求

- [ ] POST `/api/v1/servers` 成功注册，响应 201，响应体不含 `ssh_key_path` 字段
- [ ] POST 重复 name 返回 409
- [ ] GET `/api/v1/servers` 返回已注册列表，列表中所有对象不含 `ssh_key_path`
- [ ] DELETE `/api/v1/servers/{id}` 已存在 id 返回 204
- [ ] DELETE 不存在 id 返回 404
- [ ] DELETE 后 GET 列表中该服务器消失
- [ ] 数据库中 `ssh_key_path` 字段确实存储了提交的值（直接 SQL 查询验证）
- [ ] 使用 `TestClient` 或 `httpx.AsyncClient` 编写集成测试；不需要真实 Azure 凭证

## 完成标准

- [ ] 三个端点按规格实现并通过全部测试
- [ ] API 响应（含错误响应）中不出现 `ssh_key_path`
- [ ] `GpuRepository` CRUD 方法覆盖率 ≥ 80%
- [ ] `main.py` 正确挂载路由，`/api/v1/servers` 可通过 `/healthz` 同进程访问
- [ ] 无遗留 TODO/占位符
