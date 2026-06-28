---
id: TASK-002
title: "SQLite(WAL) 连接 + 通用 schema 基线 + repository 薄层"
status: done
priority: P0
architecture: ARCH-001
dependencies: [TASK-001]
estimated_effort: M
executed_by: claude-opus-4-8[1m]
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现持久化基线:aiosqlite(WAL)连接管理、幂等建表(通用三表)、以及 repository 薄 SQL 层。把 health 端点的 `db` 探测接成真实查询。所有后续模块的数据读写都走这一层契约。

## 技术规格

### 连接管理(db/connection.py)

```python
async def connect(db_path: str) -> aiosqlite.Connection:
    """打开连接并设置:
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    PRAGMA foreign_keys=ON;
    PRAGMA busy_timeout=5000;
    conn.row_factory = aiosqlite.Row
    确保 db_path 所在目录存在。"""
```

单连接长生命周期(随 app lifespan),由 `app.state.db` 持有。

### 幂等建表(db/schema.sql + db/migrate.py)

`schema.sql` 含以下三张通用表的完整 DDL(逐字采用,均 `IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS latest_snapshot (
    collector     TEXT    NOT NULL,
    target_id     INTEGER NOT NULL,
    metric        TEXT    NOT NULL,
    value_num     REAL,
    value_text    TEXT,
    status        TEXT    NOT NULL,           -- ok | unreachable | error
    collected_at  TEXT    NOT NULL,           -- ISO8601 UTC
    updated_at    TEXT    NOT NULL,           -- ISO8601 UTC
    PRIMARY KEY (collector, target_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_latest_collector ON latest_snapshot (collector);

CREATE TABLE IF NOT EXISTS metric_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collector     TEXT    NOT NULL,
    target_id     INTEGER NOT NULL,
    metric        TEXT    NOT NULL,
    value_num     REAL,
    value_text    TEXT,
    status        TEXT    NOT NULL,
    collected_at  TEXT    NOT NULL            -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_history_query
    ON metric_history (collector, target_id, metric, collected_at);

CREATE TABLE IF NOT EXISTS collector_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collector     TEXT    NOT NULL,
    status        TEXT    NOT NULL,           -- up | down | error
    sample_count  INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    ran_at        TEXT    NOT NULL            -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_run_latest ON collector_run (collector, ran_at DESC);
```

`migrate.run(conn)`:`executescript(schema.sql)` 后 `commit()`,幂等可重复执行。在 lifespan 中 connect 后立即调用。

> GPU 专用表等富结构表不在本卡,由 ARCH-002 / TASK-010 通过同一 `schema.sql` 追加(或独立 schema 片段),本卡只负责通用三表与 migrate 机制可扩展。

### repository 薄层(db/repository.py)

行类型(轻量 dataclass,`slots=True`):

```python
@dataclass(slots=True)
class SnapshotRow:
    collector: str; target_id: int; metric: str
    value_num: float | None; value_text: str | None
    status: str; collected_at: str; updated_at: str

@dataclass(slots=True)
class HistoryRow:
    collector: str; target_id: int; metric: str
    value_num: float | None; value_text: str | None
    status: str; collected_at: str

@dataclass(slots=True)
class CollectorRunRow:
    collector: str; status: str; sample_count: int
    duration_ms: int; error: str | None; ran_at: str
```

`Repository` 方法(签名为权威契约,不得更改):

```python
class Repository:
    def __init__(self, conn: aiosqlite.Connection) -> None: ...

    async def upsert_snapshot(self, collector: str, samples: list[MetricSample]) -> None:
        # INSERT ... ON CONFLICT(collector,target_id,metric) DO UPDATE
        #   SET value_num, value_text, status, collected_at, updated_at=now
    async def append_history(self, collector: str, samples: list[MetricSample]) -> None:
        # executemany INSERT INTO metric_history
    async def record_collector_run(self, result: CollectorResult) -> None:
        # INSERT INTO collector_run(error 已脱敏)

    async def get_snapshot(self, collector: str) -> list[SnapshotRow]: ...
    async def get_snapshot_metric(self, collector: str, target_id: int, metric: str) -> SnapshotRow | None: ...
    async def get_history(self, collector: str, target_id: int, metric: str,
                          since: datetime, until: datetime | None = None,
                          limit: int = 1000) -> list[HistoryRow]: ...
    async def get_last_success(self, collector: str) -> datetime | None: ...
    async def get_all_last_runs(self) -> list[CollectorRunRow]: ...
```

- `upsert_snapshot` / `append_history` 内部统一一次 `commit()`。
- `MetricSample.collected_at`(datetime)写库前转 ISO8601 UTC 字符串。
- `get_last_success`:`SELECT ran_at FROM collector_run WHERE collector=? AND status='up' ORDER BY ran_at DESC LIMIT 1`,解析回 datetime(UTC)。
- `get_all_last_runs`:每 collector 最近一行(任意 status),供数据源状态条。

### health 端点接真实 DB

`api/health.py` 的 `db` 字段改为执行 `SELECT 1`,成功 `"ok"` 否则 `"down"`(并使整体仍返回 200,内容反映 db 状态)。从 `app.state.db` 取连接。

## 实现指引

1. `connection.py` 实现 `connect()`,设置全部 PRAGMA,`row_factory=aiosqlite.Row`。
2. `schema.sql` 落三表 DDL;`migrate.py` 读同目录 `schema.sql`(用 `importlib.resources` 或 `Path(__file__).parent`)。
3. `repository.py`:实现行 dataclass + Repository 全部方法;UPSERT 用 SQLite `ON CONFLICT` 语法。
4. 在 `main.lifespan` 中:`connect → migrate.run → Repository → app.state.db/app.state.repo`。
5. `health.py` 用 `app.state.db` 探测。
6. 测试用临时文件 DB(或 `:memory:` 不适用 WAL,优先 tmp 文件)。

## 测试要求

- [ ] `connect()` 后 `PRAGMA journal_mode` 返回 `wal`
- [ ] `migrate.run()` 幂等:连续执行两次无错,三表存在
- [ ] `upsert_snapshot` 对同 (collector,target_id,metric) 二次写为更新而非新增(行数不增,值更新)
- [ ] `append_history` 每次新增一行
- [ ] `record_collector_run` 写入后 `get_last_success` 在有 `up` 行时返回正确时间、无 `up` 行时返回 None
- [ ] `get_history` 按时间范围与 limit 正确过滤、按 collected_at 升序
- [ ] `/healthz` 在 DB 可用时 `db="ok"`

## 完成标准

- [ ] 三张通用表 DDL 与 ARCH-001 完全一致(列名/类型/约束/索引)
- [ ] `Repository` 全部方法按契约签名实现并通过测试
- [ ] WAL 连接与幂等 migrate 接入 lifespan
- [ ] `/healthz` 反映真实 DB 状态
- [ ] ruff + pytest 全绿
