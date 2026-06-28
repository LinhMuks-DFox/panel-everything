---
id: TASK-016
title: "GPU 历史降采样 job（5m/1h）+ 趋势查询 API"
status: todo
priority: P2
architecture: ARCH-002
dependencies: [TASK-013]
estimated_effort: M
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

**(MS-003 增强，本期暂不实现。)** 本卡描述 GPU 历史数据降采样功能：定期将 `gpu_metrics` 原始时序聚合为 `gpu_metrics_5m`（5 分钟桶）和 `gpu_metrics_1h`（1 小时桶），并提供趋势查询 REST API，供 TASK-017 前端迷你图使用。同时在 GpuCollector 中激活 deallocated 状态跳采优化。

## 技术规格

### 文件路径

| 文件 | 说明 |
|------|------|
| `src/panel/collectors/gpu/downsampler.py` | 降采样 APScheduler job 函数 |
| `src/panel/db/gpu_repository.py` | 追加降采样读写方法 |
| `src/panel/api/azure.py` | 追加 `/api/v1/gpu/{server_id}/{gpu_index}/history` |
| `src/panel/collectors/gpu/collector.py` | 激活 `_is_vm_running` 跳采逻辑 |

### 降采样 Job

```python
# collectors/gpu/downsampler.py
async def run_5m_downsample(gpu_repo: GpuRepository) -> None:
    """
    定期（每 5 分钟）计算上一个 5min 桶：
    - bucket_start = 当前时间向下对齐到 5min
    - SELECT avg(util_pct), avg(mem_pct), max(temp_c), max(power_w), count(*)
      FROM gpu_metrics
      WHERE collected_at >= bucket_start AND collected_at < bucket_start + 5min
      GROUP BY server_id, gpu_index
    - INSERT OR REPLACE INTO gpu_metrics_5m
    """

async def run_1h_downsample(gpu_repo: GpuRepository) -> None:
    """同上，桶粒度 1h，源表从 gpu_metrics_5m 聚合（减少扫描量）。"""
```

注册方式：在 `main.py` `lifespan` 的 `build_scheduler` 后追加：
```python
scheduler.add_job(run_5m_downsample, 'interval', minutes=5, args=[gpu_repo])
scheduler.add_job(run_1h_downsample, 'interval', hours=1, args=[gpu_repo])
```

### 趋势查询 API

```python
# GET /api/v1/gpu/{server_id}/{gpu_index}/history
# 参数：?granularity=raw|5m|1h&since=<ISO8601>&until=<ISO8601>&limit=200
# 默认：granularity=5m，since=now-24h，limit=200
```

响应：`list[GpuHistoryPointOut]`：`{bucket_start, avg_util_pct, avg_mem_pct, max_temp_c, max_power_w, sample_count}`

### deallocated 跳采激活（GpuCollector）

激活 TASK-013 中预留的注释代码：

```python
# collectors/gpu/collector.py _collect_one() 开头
vm_status = await self._gpu_repo.get_vm_status(server.id)
if vm_status and vm_status.power_state in ("Deallocated", "Stopped"):
    return [GpuSample(server_id=server.id, gpu_index=0, ...,
                      status="unreachable", value_text="vm_not_running")]
```

### 数据保留策略

- `gpu_metrics` 原始表：保留 48h，超期由降采样 job 清理（`DELETE FROM gpu_metrics WHERE collected_at < now - 48h`）
- `gpu_metrics_5m`：保留 30 天
- `gpu_metrics_1h`：长期保留（无清理）

## 实现指引

1. 实现 `downsampler.py` 中两个 async job 函数，使用 `aiosqlite` 聚合查询。
2. `GpuRepository` 追加 `upsert_5m_bucket`、`upsert_1h_bucket`、`get_gpu_history_5m`、`get_gpu_history_1h`。
3. `api/azure.py` 追加趋势端点，根据 `granularity` 参数选择查询来源表。
4. `GpuCollector._collect_one` 中去掉 TASK-013 预留的注释符，激活跳采逻辑。
5. 数据清理：在 5m job 结尾追加原始表清理语句。

## 测试要求

- [ ] 降采样 job 正确计算桶 avg/max/count
- [ ] 时间桶对齐逻辑（5min/1h 向下取整）经单测验证
- [ ] GET history 端点返回正确粒度数据
- [ ] deallocated 机器跳过 SSH，产出 `vm_not_running` 样本
- [ ] 原始表清理（48h 外记录删除）经测试验证

## 完成标准

- [ ] 两个降采样 job 注册并按间隔触发
- [ ] 趋势 API 支持 raw/5m/1h 三种粒度
- [ ] deallocated 跳采逻辑激活
- [ ] 数据保留策略（清理 job）实现
- [ ] TASK-017 所需的 API 契约完全满足
