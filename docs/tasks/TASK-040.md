---
id: TASK-040
title: "通用 metric_history retention job"
status: todo
priority: P2
architecture: ARCH-001
dependencies: [TASK-002, TASK-003]
estimated_effort: S
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

通用 `metric_history` 表为 append-only，无清理会在树莓派上无限增长（ARCH-001 第 253 行原已标记本期不实现）。新增一个周期性 retention job（APScheduler，默认每日一次），按 `collected_at` 删除超过保留窗口（默认 30 天）的历史行。GPU 专用表清理由 TASK-016 负责，两者互补、互不重叠。

详见 ARCH-001 `## Addendum（2026-06，历史数据 retention）`。

---

## 技术规格

### 涉及文件

| 文件 | 改动 |
|------|------|
| `src/panel/collectors/retention.py` | **新建**：async job `prune_metric_history` |
| `src/panel/db/repository.py` | 末尾 `setattr` 注入 `prune_history(before)` |
| `src/panel/config/settings.py` | 新增 `history_retention_days: int = 30` |
| `src/panel/main.py` | `build_scheduler` 后 `add_job`（每日，集成者接线） |

### retention job（collectors/retention.py，新建）

```python
async def prune_metric_history(repo: Repository, retention_days: int) -> int:
    """
    删除 metric_history 中 collected_at 早于 (now_utc - retention_days) 的行。
    返回删除行数；记 info 日志（删除条数 + 截止时间）。
    """
    before = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = await repo.prune_history(before)
    logger.info("metric_history retention: deleted %d rows older than %s",
                deleted, before.isoformat())
    return deleted
```

### Repository 注入（db/repository.py 末尾 setattr）

沿用项目既有的「末尾 `setattr` 注入只读/维护方法、不改类签名」模式，新增：

```python
async def prune_history(self, before: datetime) -> int:
    """DELETE FROM metric_history WHERE collected_at < ?; 返回删除行数。"""
    cur = await self._conn.execute(
        "DELETE FROM metric_history WHERE collected_at < ?",
        (before.isoformat(),),
    )
    await self._conn.commit()
    return cur.rowcount
```

> `collected_at` 以 ISO8601 UTC 字符串存储（与 ARCH-001 一致），字典序与时间序一致，可直接用字符串比较。

### settings 扩展（config/settings.py）

```python
history_retention_days: int = 30   # env: PANEL_HISTORY_RETENTION_DAYS
```

env 前缀沿用项目既有 `PANEL_` 约定，最终变量名 `PANEL_HISTORY_RETENTION_DAYS`。

### 调度接线（main.py，由集成者接线）

本卡产出 job 函数与 repo 方法，**调度注册由集成者**在 `build_scheduler` 之后加入。所需片段：

```python
scheduler.add_job(
    prune_metric_history,
    "interval",
    days=1,
    args=[repo, settings.history_retention_days],
    id="metric_history_retention",
)
```

---

## 实现指引

1. 新建 `collectors/retention.py`，实现 `prune_metric_history(repo, retention_days) -> int`。
2. 在 `db/repository.py` 末尾按既有 setattr 注入风格加 `prune_history(before) -> int`，不改 `Repository` 既有方法签名。
3. `config/settings.py` 加 `history_retention_days: int = 30`。
4. `main.py` 在 `build_scheduler` 后 `add_job`（每日一次），按上文片段接线。

---

## 测试要求

- [ ] 向 `metric_history` 插入若干"旧行"（`collected_at` 早于截止）与"新行"（晚于截止）。
- [ ] 调 `prune_metric_history(repo, retention_days)` → 旧行被删除、新行保留。
- [ ] 返回的删除行数与实际删除条数一致。
- [ ] 边界：恰好等于截止时间的行**不删**（严格小于 `before`）。
- [ ] 空表或全为新行时返回 0，不报错。

---

## 完成标准

- [ ] `collectors/retention.py` 实现 `prune_metric_history`。
- [ ] `Repository.prune_history(before)` 注入并工作正常，返回删除行数。
- [ ] `settings.history_retention_days` 可由 `PANEL_HISTORY_RETENTION_DAYS` 覆盖，默认 30。
- [ ] `main.py` 接线片段已注明，集成者可直接 `add_job`。
- [ ] 测试全绿；`ruff check` 零 error。
