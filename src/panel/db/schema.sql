-- Panel Everything — 通用基线 schema (ARCH-001 / TASK-002)
--
-- 三张通用表承载"一个 target 一个标量指标"的泛化数据(VM 电源态、节点在线、
-- AI 额度)。全部 IF NOT EXISTS,启动时由 migrate.run() 幂等执行。
-- GPU 多卡时序等富结构表不在此(由 ARCH-002 / TASK-010 追加)。

-- latest_snapshot — 最新快照(每 target×metric 一行,upsert)
CREATE TABLE IF NOT EXISTS latest_snapshot (
    collector     TEXT    NOT NULL,           -- collector.name
    target_id     INTEGER NOT NULL,           -- target 维度;无维度用 0
    metric        TEXT    NOT NULL,           -- 指标名
    value_num     REAL,                       -- 数值型(可空)
    value_text    TEXT,                       -- 文本型(可空)
    status        TEXT    NOT NULL,           -- ok | unreachable | error
    collected_at  TEXT    NOT NULL,           -- ISO8601 UTC
    updated_at    TEXT    NOT NULL,           -- 写库时刻 ISO8601 UTC
    PRIMARY KEY (collector, target_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_latest_collector ON latest_snapshot (collector);

-- metric_history — 历史时序(append-only)
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

-- collector_run — 采集运行可观测(每次运行一行,append)
CREATE TABLE IF NOT EXISTS collector_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collector     TEXT    NOT NULL,
    status        TEXT    NOT NULL,           -- up | down | error
    sample_count  INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,                       -- 脱敏后的异常摘要(可空)
    ran_at        TEXT    NOT NULL            -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_run_latest ON collector_run (collector, ran_at DESC);
