-- ARCH-004 / TASK-030: AI 用量 provider 静态配置表 DDL
--
-- ai_provider: 记录已知 AI provider 元数据（静态配置），由摄取端点
--     POST /api/ingest/ai-usage 通过 provider 名查 id 用作 latest_snapshot /
--     metric_history 的 target_id。
--
-- 全部 IF NOT EXISTS / INSERT OR IGNORE，幂等执行（executescript 每次启动都跑）。
-- created_at / updated_at 写字面量时间戳常量，避免每次启动产生新值破坏幂等。

CREATE TABLE IF NOT EXISTS ai_provider (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT    NOT NULL UNIQUE,            -- 'codex' | 'claude_code' | 'chatgpt'
    display_name   TEXT    NOT NULL,
    source_type    TEXT    NOT NULL,                   -- 'local_jsonl' | 'oauth_api' | 'manual'
    window_seconds INTEGER NOT NULL DEFAULT 18000,
    enabled        INTEGER NOT NULL DEFAULT 1,         -- 0/1 (SQLite bool)
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

-- 三条初始 provider 行；INSERT OR IGNORE 保证幂等（UNIQUE(provider) 冲突即跳过）。
INSERT OR IGNORE INTO ai_provider
    (provider, display_name, source_type, window_seconds, enabled, created_at, updated_at)
VALUES
    ('codex',       'Codex',       'local_jsonl', 18000, 1, '2026-06-28T00:00:00Z', '2026-06-28T00:00:00Z'),
    ('claude_code', 'Claude Code', 'local_jsonl', 18000, 1, '2026-06-28T00:00:00Z', '2026-06-28T00:00:00Z'),
    ('chatgpt',     'ChatGPT',     'manual',      10800, 1, '2026-06-28T00:00:00Z', '2026-06-28T00:00:00Z');
