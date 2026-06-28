---
id: TASK-013
title: "SSH GPU 采集器（asyncssh + nvidia-smi 多卡解析）"
status: done
priority: P1
architecture: ARCH-002
dependencies: [TASK-003, TASK-010]
estimated_effort: M
executed_by: claude-opus-4-8[1m]
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 `GpuCollector`，遵循 ARCH-001 `Collector` 协议，通过 `asyncssh` 并发 SSH 到各已注册 GPU 服务器，执行 `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`，解析多卡 CSV 输出，将 `GpuSample` 列表写入 `gpu_metrics` 表，并产出汇总 MetricSample 写通用 `latest_snapshot`。各服务器并发采集（`asyncio.gather`），单台失败不影响其他台。

## 技术规格

### 文件路径

| 文件 | 说明 |
|------|------|
| `src/panel/collectors/gpu/__init__.py` | `register(settings, repo)` 工厂 |
| `src/panel/collectors/gpu/collector.py` | `GpuCollector` + `GpuSample` |

### nvidia-smi 查询命令

```bash
nvidia-smi \
  --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw \
  --format=csv,noheader,nounits
```

输出示例（多卡，每卡一行）：
```
0, NVIDIA A100-SXM4-80GB, 87, 65536, 81920, 72, 380
1, NVIDIA A100-SXM4-80GB, 12, 8192, 81920, 45, 150
```

字段顺序：`gpu_index, gpu_name, util_pct, mem_used_mib, mem_total_mib, temp_c, power_w`

### GpuSample 定义

```python
# collectors/gpu/collector.py
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

UTC = timezone.utc

@dataclass(slots=True)
class GpuSample:
    server_id: int
    gpu_index: int
    gpu_name: str | None
    util_pct: float | None
    mem_used_mib: float | None
    mem_total_mib: float | None
    temp_c: float | None
    power_w: float | None
    status: Literal["ok", "unreachable", "error"] = "ok"
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

`mem_pct` 在写库时由 `GpuRepository.append_gpu_metrics()` 计算（`mem_used / mem_total * 100`），不在 `GpuSample` 中重复。

### SSH 连接参数

```python
import asyncssh

conn = await asyncssh.connect(
    host=server.ssh_host,
    port=server.ssh_port,
    username=server.ssh_user,
    client_keys=[server.ssh_key_path],   # 私钥路径
    known_hosts=None,                     # 内网假设（Tailscale 隔离）
    connect_timeout=15,                   # 秒，单台连接超时
)
```

`known_hosts=None` 为内网首期假设，代码中需加注释说明：`# ARCH-001 裁定：内网 Tailscale 隔离下可接受；P3 增强强校验`。

### GpuCollector 类结构

```python
class GpuCollector:
    name: str = "gpu"
    interval_seconds: int = 60
    timeout_seconds: int = 30   # 单台 SSH+命令 超时

    async def collect(self) -> list[MetricSample]:
        """
        1. 调 _gpu_repo.get_all_servers() 过滤 has_gpu=True
        2. asyncio.gather(*[self._collect_one(s) for s in gpu_servers],
                          return_exceptions=True)
        3. 整合 GpuSample，写 _gpu_repo.append_gpu_metrics(samples)
        4. 为每台服务器产出一条汇总 MetricSample 写通用表
        """

    async def _collect_one(self, server: ServerRow) -> list[GpuSample]:
        """
        单台 SSH 采集。返回该机所有卡的 GpuSample 列表。
        异常分类：
          - asyncssh.DisconnectError / OSError / asyncio.TimeoutError
            → 该机所有卡 status='unreachable'，不抛
          - nvidia-smi 命令返回非零 exit_status
            → 该机 status='error'（无 GPU 或驱动异常）
          - CSV 解析失败
            → 该机 status='error'
        """
```

### 异常分类与状态建模

| 异常情形 | GpuSample.status | 说明 |
|----------|-----------------|------|
| SSH 连接失败（网络/认证） | `unreachable` | 服务器不可达 |
| SSH 超时 (`asyncio.TimeoutError`) | `unreachable` | 响应超时 |
| nvidia-smi exit code ≠ 0 | `error` | 无 GPU 驱动或命令失败 |
| CSV 行数为 0 | `error` | 无法识别 GPU |
| CSV 字段解析失败（非数字） | `error` | 解析异常（单行） |
| 正常 | `ok` | - |

当 `status` 非 `ok` 时，所有数值字段（`util_pct` 等）置 `None`。

### 汇总 MetricSample（写通用 latest_snapshot）

```python
MetricSample(
    target_id=server.id,
    metric="gpu_any_running",
    value_num=1.0 if any(s.status == "ok" for s in server_samples) else 0.0,
    value_text=f"{ok_count}/{total_count} gpus ok",
    status="ok" if ok_count > 0 else "unreachable",
    collected_at=now,
)
```

### MS-003 预留接口

在 `_collect_one` 开始处，预留但**本期不实现**：

```python
# TODO(MS-003/TASK-016): 若 VM 处于 deallocated/stopped 状态，跳过 SSH 采集
# if not await self._is_vm_running(server.id):
#     return [GpuSample(server_id=server.id, gpu_index=0, ..., status='unreachable',
#                       value_text='vm_not_running')]
```

### register 工厂

```python
# collectors/gpu/__init__.py
def register(settings: Settings, repo: Repository, gpu_repo: GpuRepository) -> None:
    """
    GPU Collector 无额外凭证要求（SSH 凭证存 servers 表）。
    始终注册；若 servers 表无 has_gpu=True 记录，collect() 返回空列表。
    """
    collector = GpuCollector(gpu_repo=gpu_repo, base_repo=repo)
    from panel.collectors.registry import register as reg_register
    reg_register(collector)
```

## mock/fixture 策略

**测试不 SSH 到真实服务器**，使用 `nvidia-smi` CSV 输出 fixture + asyncssh mock：

```
tests/fixtures/gpu/
├── nvidia_smi_single.txt    # 单卡正常输出
├── nvidia_smi_multi.txt     # 多卡（4卡）正常输出
├── nvidia_smi_empty.txt     # 空输出（无 GPU）
├── nvidia_smi_partial.txt   # 某些字段为 [Not Supported]（异常值）
└── nvidia_smi_timeout.txt   # 模拟超时（测试代码中不实际用文件，而是用 mock）
```

`nvidia_smi_single.txt` 示例内容：
```
0, NVIDIA A100-SXM4-80GB, 87, 65536, 81920, 72, 380
```

`nvidia_smi_multi.txt` 示例内容（4卡）：
```
0, NVIDIA A100-SXM4-80GB, 87, 65536, 81920, 72, 380
1, NVIDIA A100-SXM4-80GB, 12, 8192, 81920, 45, 150
2, NVIDIA A100-SXM4-80GB, 0, 1024, 81920, 38, 80
3, NVIDIA A100-SXM4-80GB, 99, 79872, 81920, 78, 400
```

测试使用 `unittest.mock.AsyncMock` 替换 `asyncssh.connect` + `conn.run()`，令 `result.stdout` 返回 fixture 文本，`result.exit_status` 返回 0 或非 0。

env 开关：`GPU_INTEGRATION_TEST=1` + `TEST_SSH_HOST`/`TEST_SSH_USER`/`TEST_SSH_KEY` 时跑真实 SSH 集成测试。

## 实现指引

1. 创建 `src/panel/collectors/gpu/collector.py`，定义 `GpuSample`（dataclass，slots=True）和 `GpuCollector`。
2. `GpuCollector.__init__` 接受 `gpu_repo: GpuRepository`、`base_repo: Repository`（依赖注入，测试可替换）。
3. `_parse_nvidia_smi_csv(server_id: int, output: str, now: datetime) -> list[GpuSample]` 私有函数：
   - 按行 split，跳过空行
   - 每行 split(',')，strip 空格
   - 安全转换数值：`float(v.strip())` 失败则该字段置 None，整行 status='error'
   - `[Not Supported]` 视为 None
4. `_collect_one` 内：`async with asyncio.timeout(self.timeout_seconds):` 包裹整个 SSH 操作；捕获 `(asyncssh.Error, OSError, asyncio.TimeoutError)` → `status='unreachable'`；捕获 exit_status ≠ 0 → `status='error'`。
5. `collect()` 用 `asyncio.gather(..., return_exceptions=True)` 并发所有 GPU 服务器；对 `Exception` 类型的结果（gather 捕获的异常）记录 warning 并产出 `status='error'` 汇总 MetricSample。
6. `known_hosts=None` 行旁边加注释（见上）。
7. 写库：调 `await self._gpu_repo.append_gpu_metrics(all_samples)`；`mem_pct` 在 repository 写库时计算。

## 测试要求

- [ ] `nvidia_smi_single.txt` fixture：单卡解析，GpuSample 字段正确（util_pct=87.0, mem_used_mib=65536.0 等）
- [ ] `nvidia_smi_multi.txt` fixture：4 卡解析，产出 4 条 GpuSample，gpu_index 0~3
- [ ] `nvidia_smi_empty.txt` fixture：空输出，产出 `status='error'` GpuSample
- [ ] asyncssh mock 抛 `asyncssh.DisconnectError`：该机 status='unreachable'，其他机不受影响
- [ ] asyncio.TimeoutError mock：该机 status='unreachable'
- [ ] nvidia-smi exit_status=1 mock：该机 status='error'
- [ ] 多台服务器并发：一台失败不影响其他台（通过 gather 结果验证）
- [ ] 无 has_gpu=True 服务器时 collect() 返回空列表，不报错
- [ ] `[Not Supported]` 字段置 None，整体不崩溃

## 完成标准

- [ ] `GpuCollector` 实现 `Collector` 协议
- [ ] 单卡/多卡/无GPU/超时/不可达五种场景均经测试覆盖
- [ ] `asyncio.gather` 并发采集，单台失败隔离
- [ ] 写库逻辑：`GpuRepository.append_gpu_metrics` 被正确调用（可用 mock 验证调用参数）
- [ ] `known_hosts=None` 处有内网假设注释
- [ ] 全部 mock 测试通过，无需真实 SSH 连接
