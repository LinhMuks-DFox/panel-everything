---
id: TASK-030
title: "面板摄取端点 POST /api/ingest/ai-usage + ai_provider 表"
status: todo
priority: P2
architecture: ARCH-004
dependencies: [TASK-002]
estimated_effort: S
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

**(MS-004 后期，本期不实现)**

在面板服务（树莓派）侧实现 AI 用量数据的摄取能力：新建 `ai_provider` 静态配置表，实现 `POST /api/ingest/ai-usage` 端点接收工作站 Reporter 推送的数据，并将其落入通用 `latest_snapshot` / `metric_history` 表。这是 ARCH-004 所有其他任务的基础。

---

## 技术规格

### 新增数据库表

**`ai_provider`** 表（静态配置，记录已知 provider 元数据）：

```sql
CREATE TABLE IF NOT EXISTS ai_provider (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT    NOT NULL UNIQUE,
    display_name TEXT    NOT NULL,
    source_type  TEXT    NOT NULL,          -- 'local_jsonl' | 'oauth_api' | 'manual'
    window_seconds INTEGER NOT NULL DEFAULT 18000,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
```

迁移时插入三条初始行（IF NOT EXISTS）：

| provider | display_name | source_type | window_seconds |
|----------|-------------|-------------|----------------|
| `codex` | `Codex` | `local_jsonl` | 18000 |
| `claude_code` | `Claude Code` | `local_jsonl` | 18000 |
| `chatgpt` | `ChatGPT` | `manual` | 10800 |

### 摄取端点规格

文件路径：`src/panel/api/ingest.py`

```python
router = APIRouter(prefix="/api/ingest", tags=["ingest"])

@router.post("/ai-usage")
async def ingest_ai_usage(
    body: AiUsagePayload,
    repo: Repository = Depends(get_repo),
) -> dict: ...
```

**请求 schema**（Pydantic，`domain/models.py`）：

```python
class AiMetricItem(BaseModel):
    metric: str                    # 'used_requests' | 'limit_requests' | 'used_percent' | 'resets_at' | 'window_seconds' | 'extra'
    value_num: float | None = None
    value_text: str | None = None

class AiUsagePayload(BaseModel):
    reporter_version: str
    reported_at: datetime
    provider: Literal["codex", "claude_code", "chatgpt"]
    metrics: list[AiMetricItem]
    status: Literal["ok", "error"] = "ok"
```

**处理逻辑**：

1. 从 `ai_provider` 表查 `provider` 对应的 `id`（`target_id`）
2. 将 `body.metrics` 逐项转为 `MetricSample`：`target_id=provider_id`，`metric=item.metric`，`value_num/value_text` 原样映射，`status` 取 `body.status`，`collected_at` 取 `body.reported_at`
3. 调用 `repo.upsert_snapshot("ai_usage", samples)`
4. 调用 `repo.append_history("ai_usage", samples)`
5. 返回 `{"ok": True, "stored": len(samples)}`

**可选鉴权**（配置驱动）：读 `settings.INGEST_TOKEN`，若非空则校验 `Authorization: Bearer <token>`，不匹配返回 403。Token 为空时跳过校验。

### 注册路由

在 `src/panel/main.py` 的 `create_app()` 中：

```python
from panel.api.ingest import router as ingest_router
app.include_router(ingest_router)
```

---

## 实现指引

1. **`migrate.run()` 扩展**（`db/migrate.py`）
   - 追加 `ai_provider` 表的 `CREATE TABLE IF NOT EXISTS` DDL
   - 紧接 DDL 后用 `INSERT OR IGNORE INTO ai_provider ...` 插入三条初始 provider 行；`created_at/updated_at` 填入当前 UTC ISO8601 字符串

2. **`db/repository.py` 扩展**
   - 新增 `get_ai_provider_id(provider: str) -> int | None`：执行 `SELECT id FROM ai_provider WHERE provider=?`，未找到返回 `None`

3. **`api/ingest.py` 实现**
   - 若 `get_ai_provider_id` 返回 `None`，返回 HTTP 400 `{"ok": false, "error": "unknown provider: <x>"}`
   - 批量构造 `MetricSample` 列表，调 `repo.upsert_snapshot` 和 `repo.append_history`（这两个方法由 TASK-002 已实现）

4. **settings 扩展**（`config/settings.py`）
   - 新增 `INGEST_TOKEN: str = ""`（空字符串表示不鉴权）

5. **无需新建文件**：所有逻辑进现有文件，只增量扩展不破坏已有结构

---

## 测试要求

- [ ] `test_ingest_ai_usage_ok`：POST 合法 codex payload → 200，`stored=6`，`latest_snapshot` 有记录，`metric_history` 有追加
- [ ] `test_ingest_ai_usage_unknown_provider`：POST `provider="foobar"` → 400，`error` 含 provider 名
- [ ] `test_ingest_ai_usage_token_auth`：`INGEST_TOKEN` 非空时，无 token 请求 → 403，正确 token → 200
- [ ] `test_ingest_ai_usage_token_skip`：`INGEST_TOKEN` 为空时，无 token 请求 → 200（不鉴权）
- [ ] `test_ai_provider_init`：migrate 后 `ai_provider` 表含 3 行（codex / claude_code / chatgpt）
- [ ] `test_ingest_idempotent`：同一 provider 多次 POST → `latest_snapshot` 只有最新一条，`metric_history` 追加多条

---

## 完成标准

- [ ] `src/panel/api/ingest.py` 实现完成，路由注册到 `create_app()`
- [ ] `ai_provider` 表由 `migrate.run()` 自动创建并初始化 3 行
- [ ] `settings.INGEST_TOKEN` 可选鉴权逻辑工作正常
- [ ] `POST /api/ingest/ai-usage` 接受合法 payload 并落入 `latest_snapshot` 和 `metric_history`
- [ ] 所有测试通过，覆盖率 ≥ 80%（当前文件）
- [ ] 无破坏性修改：TASK-002 已有的 repository 方法签名不变
