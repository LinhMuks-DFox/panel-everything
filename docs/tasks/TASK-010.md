---
id: TASK-010
title: "Azure/GPU 专用表 schema"
status: done
priority: P1
architecture: ARCH-002
dependencies: [TASK-002]
estimated_effort: S
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

在 ARCH-001 基线 schema（`db/schema.sql`）的基础上，追加 ARCH-002 所需的五张专用表：`servers`、`azure_vm_status`、`gpu_metrics`、`gpu_metrics_5m`、`gpu_metrics_1h`，并在迁移器中注册。降采样表（`gpu_metrics_5m`/`gpu_metrics_1h`）供 MS-003 的 TASK-016 使用，本期提前建好以避免后续迁移。

## 技术规格

### 文件位置

- DDL 追加至：`src/panel/db/schema.sql`
- 迁移器：`src/panel/db/migrate.py`（ARCH-001 已实现，本卡只需追加 DDL，migrate.run() 会自动执行所有 CREATE IF NOT EXISTS）

### 完整 DDL

```sql
-- 服务器注册表
CREATE TABLE IF NOT EXISTS servers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL UNIQUE,
    azure_resource_group TEXT,
    azure_vm_name        TEXT,
    ssh_host             TEXT,
    ssh_port             INTEGER NOT NULL DEFAULT 22,
    ssh_user             TEXT    NOT NULL DEFAULT 'azureuser',
    ssh_key_path         TEXT,
    has_gpu              INTEGER NOT NULL DEFAULT 0,
    notes                TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_servers_name ON servers(name);

-- Azure VM 状态快照
CREATE TABLE IF NOT EXISTS azure_vm_status (
    server_id        INTEGER PRIMARY KEY,
    power_state      TEXT    NOT NULL,
    power_state_raw  TEXT,
    is_running       INTEGER NOT NULL DEFAULT 0,
    collected_at     TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);

-- GPU 指标时序
CREATE TABLE IF NOT EXISTS gpu_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id     INTEGER NOT NULL,
    gpu_index     INTEGER NOT NULL,
    gpu_name      TEXT,
    util_pct      REAL,
    mem_used_mib  REAL,
    mem_total_mib REAL,
    mem_pct       REAL,
    temp_c        REAL,
    power_w       REAL,
    status        TEXT    NOT NULL DEFAULT 'ok',
    collected_at  TEXT    NOT NULL,
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_query
    ON gpu_metrics(server_id, gpu_index, collected_at);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_server_latest
    ON gpu_metrics(server_id, collected_at DESC);

-- GPU 5min 降采样（MS-003）
CREATE TABLE IF NOT EXISTS gpu_metrics_5m (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER NOT NULL,
    gpu_index    INTEGER NOT NULL,
    avg_util_pct REAL,
    avg_mem_pct  REAL,
    max_temp_c   REAL,
    max_power_w  REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    bucket_start TEXT    NOT NULL,
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_5m_bucket
    ON gpu_metrics_5m(server_id, gpu_index, bucket_start);

-- GPU 1h 降采样（MS-003）
CREATE TABLE IF NOT EXISTS gpu_metrics_1h (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER NOT NULL,
    gpu_index    INTEGER NOT NULL,
    avg_util_pct REAL,
    avg_mem_pct  REAL,
    max_temp_c   REAL,
    max_power_w  REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    bucket_start TEXT    NOT NULL,
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_1h_bucket
    ON gpu_metrics_1h(server_id, gpu_index, bucket_start);
```

### 注意事项

- 所有时间列存 ISO8601 UTC 字符串（与 ARCH-001 基线一致）
- `ssh_key_path` 只存路径引用，不存私钥内容
- `has_gpu` 用 `INTEGER 0/1` 而非 BOOLEAN（SQLite 无原生 bool 类型）
- `ON DELETE CASCADE` 确保删除 server 时关联数据同步清除
- 降采样表的 `UNIQUE INDEX` 保证 upsert 语义（`INSERT OR REPLACE`）

## 实现指引

1. 打开 `src/panel/db/schema.sql`，在 ARCH-001 通用表之后追加上述 DDL 块。
2. `migrate.py` 中 `run(conn)` 逐条执行 `schema.sql` 中所有语句，无需修改逻辑（IF NOT EXISTS 幂等）。
3. 建议在文件中用注释分隔：`-- === ARCH-002: Azure/GPU Tables ===`。
4. 不需要创建新 Python 文件；`GpuRepository` 将在 TASK-013 中创建，本卡只关注 DDL。

## 测试要求

- [ ] 单测：对空 DB 运行 migrate.run() 后，`sqlite_master` 包含全部五张表及对应索引
- [ ] 单测：多次执行 migrate.run() 不报错（幂等性）
- [ ] 单测：向 `servers` 插入记录后删除，`azure_vm_status` 和 `gpu_metrics` 中相关行被级联删除
- [ ] 单测：`servers.name` UNIQUE 约束生效（重复 name 报 IntegrityError）
- [ ] 人工验证：`sqlite3 panel.db .schema` 输出结构与 DDL 一致

## 完成标准

- [ ] `src/panel/db/schema.sql` 包含全部五张表 DDL，且所有 CREATE 语句带 `IF NOT EXISTS`
- [ ] `migrate.run()` 执行后五张表和全部索引均存在
- [ ] 级联删除行为经单测验证
- [ ] 无遗留 TODO/占位符
