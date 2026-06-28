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

-- === ARCH-002: Azure/GPU Tables ===
-- 五张专用表:servers / azure_vm_status / gpu_metrics / gpu_metrics_5m / gpu_metrics_1h
-- 所有 CREATE 均带 IF NOT EXISTS,migrate.run() 幂等执行。
-- 降采样表(gpu_metrics_5m/gpu_metrics_1h)供 MS-003 / TASK-016 填充,本期提前建好。

-- servers — 服务器注册表
CREATE TABLE IF NOT EXISTS servers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL UNIQUE,           -- 人可读名称,全局唯一
    azure_resource_group TEXT,                              -- Azure 资源组(可空:非 Azure 机)
    azure_vm_name        TEXT,                              -- Azure VM 名称(可空)
    ssh_host             TEXT,                              -- SSH 连接地址(Tailscale IP 或 hostname)
    ssh_port             INTEGER NOT NULL DEFAULT 22,
    ssh_user             TEXT    NOT NULL DEFAULT 'azureuser',
    ssh_key_path         TEXT,                              -- 存路径引用,不存私钥内容
    has_gpu              INTEGER NOT NULL DEFAULT 0,        -- 0/1(SQLite 无原生 bool)
    notes                TEXT,
    created_at           TEXT    NOT NULL,                  -- ISO8601 UTC
    updated_at           TEXT    NOT NULL                   -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_servers_name ON servers(name);

-- azure_vm_status — VM 状态快照(每台 VM 一行,upsert on conflict server_id)
CREATE TABLE IF NOT EXISTS azure_vm_status (
    server_id        INTEGER PRIMARY KEY,
    power_state      TEXT    NOT NULL,              -- 映射后展示值: Running/Stopped/Deallocated/...
    power_state_raw  TEXT,                          -- Azure 原始值: PowerState/running
    is_running       INTEGER NOT NULL DEFAULT 0,    -- 1=running, 0=其他
    collected_at     TEXT    NOT NULL,              -- ISO8601 UTC
    updated_at       TEXT    NOT NULL,              -- ISO8601 UTC
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);

-- gpu_metrics — GPU 指标时序表(多卡,每张卡每次采集一行,append-only)
CREATE TABLE IF NOT EXISTS gpu_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id     INTEGER NOT NULL,
    gpu_index     INTEGER NOT NULL,                 -- 0-based,对应 nvidia-smi 行序
    gpu_name      TEXT,                             -- e.g. "NVIDIA A100-SXM4-80GB"
    util_pct      REAL,                             -- GPU utilization %
    mem_used_mib  REAL,
    mem_total_mib REAL,
    mem_pct       REAL,                             -- mem_used/mem_total * 100
    temp_c        REAL,                             -- 温度 °C
    power_w       REAL,                             -- 功耗 W
    status        TEXT    NOT NULL DEFAULT 'ok',    -- ok/unreachable/error
    collected_at  TEXT    NOT NULL,                 -- ISO8601 UTC
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_query
    ON gpu_metrics(server_id, gpu_index, collected_at);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_server_latest
    ON gpu_metrics(server_id, collected_at DESC);

-- gpu_metrics_5m — GPU 5 分钟降采样(MS-003 / TASK-016 填充,本期提前建好)
CREATE TABLE IF NOT EXISTS gpu_metrics_5m (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER NOT NULL,
    gpu_index    INTEGER NOT NULL,
    avg_util_pct REAL,
    avg_mem_pct  REAL,
    max_temp_c   REAL,
    max_power_w  REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    bucket_start TEXT    NOT NULL,                  -- ISO8601 UTC,5min 对齐
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_5m_bucket
    ON gpu_metrics_5m(server_id, gpu_index, bucket_start);

-- gpu_metrics_1h — GPU 1 小时降采样(MS-003 / TASK-016 填充,本期提前建好)
CREATE TABLE IF NOT EXISTS gpu_metrics_1h (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER NOT NULL,
    gpu_index    INTEGER NOT NULL,
    avg_util_pct REAL,
    avg_mem_pct  REAL,
    max_temp_c   REAL,
    max_power_w  REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    bucket_start TEXT    NOT NULL,                  -- ISO8601 UTC,1h 对齐
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_1h_bucket
    ON gpu_metrics_1h(server_id, gpu_index, bucket_start);
