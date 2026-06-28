-- ARCH-003 / TASK-020: Tailscale 专用表 DDL
--
-- tailscale_nodes: 每个 tailnet 节点一行,每次采集 upsert (ON CONFLICT node_key)。
-- tailscale_node_events: event-driven 历史,仅在 online_state 变更时 INSERT,
--     避免高频写入占用树莓派 IO。
--
-- 全部 IF NOT EXISTS,幂等执行。

-- 节点主表
CREATE TABLE IF NOT EXISTS tailscale_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT    NOT NULL UNIQUE,                  -- Self/Peer PublicKey，节点永久标识
    hostname        TEXT    NOT NULL,
    dns_name        TEXT,
    tailscale_ips   TEXT    NOT NULL DEFAULT '[]',            -- JSON array
    os              TEXT,
    online_state    TEXT    NOT NULL DEFAULT 'OFFLINE',       -- ONLINE | OFFLINE | LONG_OFFLINE
    is_exit_node    INTEGER NOT NULL DEFAULT 0,               -- 0/1 (SQLite bool)
    last_seen_at    TEXT,                                     -- ISO8601 UTC; NULL when online
    collected_at    TEXT    NOT NULL,                         -- 最近一次采集时刻 ISO8601 UTC
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_online_state
    ON tailscale_nodes(online_state);

-- 事件历史表 (event-driven, 仅状态变更时 INSERT)
CREATE TABLE IF NOT EXISTS tailscale_node_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT    NOT NULL,
    from_state      TEXT,                                     -- NULL 表示首次发现
    to_state        TEXT    NOT NULL,                         -- ONLINE | OFFLINE | LONG_OFFLINE
    occurred_at     TEXT    NOT NULL,                         -- ISO8601 UTC (= collected_at)
    note            TEXT                                      -- 备注, e.g. "first_seen"
);

CREATE INDEX IF NOT EXISTS idx_node_events_key_time
    ON tailscale_node_events(node_key, occurred_at DESC);
