---
id: TASK-014
title: "Azure + GPU dashboard 聚合 API"
status: done
priority: P1
architecture: ARCH-002
dependencies: [TASK-012, TASK-013]
estimated_effort: S
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 `GET /api/v1/dashboard/azure` 端点，一次调用聚合返回所有已注册 VM 的电源状态、各机最新 GPU 指标、以及两个 collector 的运行健康状态。供前端 VmCard/GpuCard 渲染使用，减少前端多次请求。

## 技术规格

### 文件

- `src/panel/api/azure.py`（在 TASK-011 已创建的 router 中追加此端点）

### 端点签名

```python
@router.get("/dashboard/azure", response_model=DashboardAzureOut)
async def get_azure_dashboard(
    repo: Repository = Depends(get_repo),
    gpu_repo: GpuRepository = Depends(get_gpu_repo),
) -> DashboardAzureOut:
```

### 响应模型（domain/models.py 追加）

```python
class CollectorStatusOut(BaseModel):
    status: str                    # "up"/"down"/"error"/"unknown"
    last_ran_at: datetime | None
    error: str | None

class DashboardVmOut(VmStatusOut):
    gpus: list[GpuMetricOut] = []

class DashboardAzureOut(BaseModel):
    fetched_at: datetime
    collector_status: dict[str, CollectorStatusOut]
    vms: list[DashboardVmOut]
```

`VmStatusOut` / `GpuMetricOut` 已在 TASK-011/TASK-013 定义。

### 聚合逻辑

```python
async def get_azure_dashboard(...):
    now = datetime.now(UTC)

    # 1. 查询所有已注册服务器
    servers = await gpu_repo.get_all_servers()

    # 2. 查询 VM 状态（azure_vm_status 表）
    vm_statuses: dict[int, VmStatusRow] = {
        row.server_id: row
        for row in await gpu_repo.get_vm_status_all()
    }

    # 3. 查询各机最新 GPU（gpu_metrics 按 server_id + collected_at DESC 取最新）
    gpu_latest: dict[int, list[GpuMetricRow]] = {}
    for s in servers:
        if s.has_gpu:
            gpu_latest[s.id] = await gpu_repo.get_latest_gpu_metrics(s.id)

    # 4. 查询 collector 运行状态（ARCH-001 collector_run 表）
    last_runs = await repo.get_all_last_runs()   # list[CollectorRunRow]
    collector_status = _build_collector_status(last_runs, ["azure_vm", "gpu"])

    # 5. 组装 DashboardVmOut 列表
    vms = []
    for server in servers:
        vm_row = vm_statuses.get(server.id)
        if vm_row is None:
            # 从未采集：占位
            vm_out = DashboardVmOut(
                server_id=server.id, name=server.name, ...,
                power_state="Unknown", is_running=False,
                collected_at=now, is_stale=True,
                gpus=[],
            )
        else:
            is_stale = (now - vm_row.collected_at).total_seconds() > VM_STALE_SECONDS
            gpu_outs = _build_gpu_outs(gpu_latest.get(server.id, []), now)
            vm_out = DashboardVmOut(..., is_stale=is_stale, gpus=gpu_outs)
        vms.append(vm_out)

    return DashboardAzureOut(fetched_at=now, collector_status=collector_status, vms=vms)
```

### stale 阈值常量

```python
VM_STALE_SECONDS: int = 600    # 10 分钟（AzureVmCollector interval=300s 的 2倍）
GPU_STALE_SECONDS: int = 180   # 3 分钟（GpuCollector interval=60s 的 3倍）
```

常量放 `api/azure.py` 顶部，或提升到 `config/settings.py`（可由 env 覆盖，推荐后者）。

### collector_status 构建逻辑

```python
def _build_collector_status(
    last_runs: list[CollectorRunRow],
    names: list[str],
) -> dict[str, CollectorStatusOut]:
    by_name = {r.collector: r for r in last_runs}
    result = {}
    for name in names:
        run = by_name.get(name)
        if run is None:
            result[name] = CollectorStatusOut(status="unknown", last_ran_at=None, error=None)
        else:
            result[name] = CollectorStatusOut(
                status=run.status,
                last_ran_at=run.ran_at,
                error=run.error,   # 已脱敏（ARCH-001 repository 写库前脱敏）
            )
    return result
```

### GpuRepository 追加方法（本卡实现）

TASK-013 会已创建 `GpuRepository`，本卡在其中追加：

```python
async def get_vm_status_all(self) -> list[VmStatusRow]:
    """SELECT * FROM azure_vm_status"""

async def get_latest_gpu_metrics(self, server_id: int) -> list[GpuMetricRow]:
    """
    SELECT * FROM gpu_metrics
    WHERE server_id=? AND collected_at = (
        SELECT MAX(collected_at) FROM gpu_metrics WHERE server_id=?
    )
    ORDER BY gpu_index
    """
```

## 实现指引

1. 在 `api/azure.py` 中的已有 `router` 上追加 `get_azure_dashboard` 端点。
2. 在 `domain/models.py` 追加 `CollectorStatusOut` / `DashboardVmOut` / `DashboardAzureOut`（若 TASK-011/013 已定义基础模型则直接继承/组合）。
3. 在 `db/gpu_repository.py` 追加 `get_vm_status_all` 和 `get_latest_gpu_metrics`。
4. stale 判断：`collected_at` 从 DB 读出为 ISO8601 字符串，需先 `datetime.fromisoformat(...)` 转为 `datetime`。确保 tzinfo=UTC（若 DB 存的是 naive UTC 字符串，则 `.replace(tzinfo=UTC)` 补充）。
5. `_build_collector_status` 实现为 module-level 私有函数。
6. 无已知数据时（servers 为空），返回 `DashboardAzureOut(fetched_at=now, collector_status={...}, vms=[])`，HTTP 200。

## 测试要求

- [ ] GET `/api/v1/dashboard/azure` 返回 200，响应结构符合 `DashboardAzureOut` schema
- [ ] servers 表空时，返回 `vms=[]`
- [ ] 有 VM 但无采集记录时，`is_stale=True`，`power_state="Unknown"`
- [ ] collector_run 无记录时，`collector_status["azure_vm"].status="unknown"`
- [ ] collector_run 有失败记录时，`collector_status["azure_vm"].status="down"`，`error` 已脱敏（不含绝对路径/明文密钥）
- [ ] GPU stale 逻辑：注入 `collected_at` 超过 180s 的 GPU 记录，`is_stale=True`
- [ ] 测试使用内存 SQLite（`:memory:`）+ `TestClient`

## 完成标准

- [ ] `GET /api/v1/dashboard/azure` 端点实现，response_model 验证通过
- [ ] `GpuRepository.get_vm_status_all` 和 `get_latest_gpu_metrics` 实现
- [ ] stale 判断逻辑（VM 600s / GPU 180s）经测试验证
- [ ] `DashboardAzureOut` 中 `collector_status` 覆盖 `azure_vm` 和 `gpu` 两个 collector
- [ ] 无遗留 TODO/占位符
