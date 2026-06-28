---
id: ARCH-004
title: "AI 使用额度监控"
status: approved
requirements: [REQ-004]
author: Architect
created: 2026-06-28
updated: 2026-06-28
---

> **[MS-004，已批准进入实现]** 设计与三数据源可行性调研已完成，MS-002 已稳定交付，本架构进入实现阶段。

---

## 概述

AI 使用额度监控模块，允许用户在 Panel Everything 面板上集中查看 Codex、Claude/Claude Code、ChatGPT 三类 AI 服务的当前用量和滑动窗口剩余额度，以便合理安排使用节奏，避免在关键时刻触及限额。

### 核心设计决策：推送拓扑（Reporter → 面板）

AI 用量数据存储在**工作站本地**（`~/.codex`、`~/.claude` 等目录），树莓派（面板服务器）不在工作站本地，无法直接读取。因此采用**反向推送架构**：

```
工作站（Reporter 脚本）── POST /api/ingest/ai-usage ──▶ 树莓派（面板摄取端点）
        ↑                                                        ↓
  本地读 ~/.codex/                                        latest_snapshot /
        ~/.claude/                                        metric_history
  cron 定时触发                                           (通用表)
```

工作站与树莓派在同一 Tailscale 网络（tailnet）内，Reporter 通过 tailnet 直接 POST 到面板。面板侧只负责接收、落库、渲染，不主动拉取。

### 三数据源分级策略

| 优先级 | 数据源 | 读取方式 | 可靠性 | 备注 |
|--------|--------|----------|--------|------|
| MVP | **Codex** | 读本地 `~/.codex/.../token_count.rate_limits` jsonl | 高，纯本地文件 | 字段最纯净，做 MVP |
| P2 | **Claude / Claude Code** | 优先读 `~/.claude/projects/` 下 jsonl（社区 ccusage 方案）；回退：尝试 OAuth usage 端点（未官方文档化） | 中，字段需解析 | Anthropic 无公开个人订阅额度 API |
| P3（降级） | **ChatGPT** | 无官方额度 API；降级为手动输入 | 低（手动） | 面板渲染「手动更新」徽标 |

---

## 技术选型

| 层面 | 选择 | 理由 |
|------|------|------|
| 摄取端点 | FastAPI `POST /api/ingest/ai-usage` | 与现有 API 层一致，无额外依赖 |
| Reporter 脚本 | 单文件 Python 脚本（stdlib + httpx），工作站本地 | 轻量，无需安装，工作站不运行 FastAPI 服务 |
| Reporter 调度 | 工作站 cron（`crontab -e`），默认 5min | 与 Codex 5 小时滑动窗口粒度匹配，cron 无守护进程依赖 |
| 持久化 | 复用 ARCH-001 通用表（`latest_snapshot` + `metric_history`） | 无需新建通用表；provider 做 `target_id`，`used_percent` 做 `value_num`，扩展字段走 JSON text 列 |
| AI 额度专用表 | `ai_usage_meta`（provider 元数据）| 存 provider 显示名、数据来源类型、是否手动输入等静态配置 |
| 前端渲染 | Jinja2 SSR partial `_ai_card.html`，泛化渲染 `used_percent` | 与其他模块统一，5 小时窗口泛化为进度条 + 剩余时间文字 |
| 凭证 | Reporter 读本地文件，无需持久化凭证到面板 | 面板侧 ingest 端点在 tailnet 内，不做额外鉴权（单用户系统） |

---

## 系统架构

### 完整数据流

```
工作站本地
┌─────────────────────────────────────────────────────────────┐
│  ~/.codex/<workspace>/*.jsonl  ──┐                          │
│  ~/.claude/projects/.../*.jsonl  ├── reporter.py (cron 5m) ─┼─▶ POST /api/ingest/ai-usage
│  手动输入 (ChatGPT)  ────────────┘                          │       { provider, metrics[] }
└─────────────────────────────────────────────────────────────┘
                                                                        │
树莓派（面板）                                                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│  api/ingest.py                                                         │
│    POST /api/ingest/ai-usage                                           │
│      ▼                                                                 │
│  repository.upsert_snapshot("ai_usage", samples)                       │
│  repository.append_history("ai_usage", samples)                        │
│  latest_snapshot (collector="ai_usage", target_id=<provider_id>)       │
│  metric_history  (同上)                                                 │
│                                                                        │
│  web/routes.py  GET /                                                  │
│    query latest_snapshot WHERE collector="ai_usage"                    │
│    ──▶ Jinja2 render _ai_card.html                                     │
└────────────────────────────────────────────────────────────────────────┘
```

### 模块文件布局（在 ARCH-001 src/panel/ 基础上新增）

```
src/panel/
└── api/
    └── ingest.py              # POST /api/ingest/ai-usage 摄取路由
tools/
└── reporter/
    ├── reporter.py            # 工作站 Reporter 单文件脚本（cron 调用）
    ├── sources/
    │   ├── __init__.py
    │   ├── codex.py           # Codex 本地 jsonl 解析
    │   ├── claude_code.py     # Claude Code ~/.claude/projects jsonl 解析
    │   └── chatgpt.py         # ChatGPT 手动输入读取（读本地 yaml/json）
    ├── reporter.example.env   # 示例配置（PANEL_URL、REPORTER_TOKEN 等）
    └── README.md              # 工作站部署说明
```

---

## 接口定义

### 摄取端点（面板侧）

**`POST /api/ingest/ai-usage`**

请求 body（JSON）：

```json
{
  "reporter_version": "1.0",
  "reported_at": "2026-06-28T10:00:00Z",
  "provider": "codex",
  "metrics": [
    {
      "metric": "used_requests",
      "value_num": 42.0,
      "value_text": null
    },
    {
      "metric": "limit_requests",
      "value_num": 500.0,
      "value_text": null
    },
    {
      "metric": "used_percent",
      "value_num": 8.4,
      "value_text": null
    },
    {
      "metric": "resets_at",
      "value_num": null,
      "value_text": "2026-06-28T15:00:00Z"
    },
    {
      "metric": "window_seconds",
      "value_num": 18000.0,
      "value_text": null
    },
    {
      "metric": "extra",
      "value_num": null,
      "value_text": "{\"data_source\": \"local_jsonl\", \"model\": \"gpt-4o\"}"
    }
  ]
}
```

响应：

```json
// 200 OK
{ "ok": true, "stored": 6 }

// 400 Bad Request
{ "ok": false, "error": "unknown provider: foobar" }
```

**provider 枚举**（已知集合，可扩展）：`"codex"` / `"claude_code"` / `"chatgpt"`

**鉴权**：tailnet 内无需鉴权（与其他 API 一致，单用户系统）。如需防误报，可在 `settings.INGEST_TOKEN` 配置可选 Bearer token，Reporter 发 `Authorization: Bearer <token>`，端点做可选校验（token 为空则跳过）。

---

### 面板读取 API（供前端调用）

**`GET /api/ai-usage`** — 返回所有 provider 最新快照 + stale 判断

```json
{
  "providers": [
    {
      "provider": "codex",
      "display_name": "Codex",
      "source_type": "local_jsonl",
      "used_percent": 8.4,
      "used_requests": 42,
      "limit_requests": 500,
      "resets_at": "2026-06-28T15:00:00Z",
      "window_label": "5h rolling",
      "stale": false,
      "stale_since": null,
      "collected_at": "2026-06-28T10:00:00Z",
      "status": "ok"
    }
  ]
}
```

**stale 判断规则**：`collected_at` 距当前超过 `window_seconds * 0.5`（如 5h 窗口 → 2.5h 无更新则标 stale），或 Reporter 上报 `status = "error"`。

---

## 数据模型

### 复用通用表（ARCH-001 定义，不新建）

```sql
-- latest_snapshot（upsert）
-- collector = 'ai_usage', target_id = ai_provider.id, metric = 'used_percent' / 'resets_at' / ...
-- value_num 存数值型指标，value_text 存字符串/JSON

-- metric_history（append）
-- 同上，每次上报追加一行，保留趋势历史
```

### 新增专用表（`ai_usage_meta`）

```sql
CREATE TABLE IF NOT EXISTS ai_provider (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT    NOT NULL UNIQUE,          -- 'codex' | 'claude_code' | 'chatgpt'
    display_name TEXT   NOT NULL,                  -- 'Codex' | 'Claude Code' | 'ChatGPT'
    source_type TEXT    NOT NULL,                  -- 'local_jsonl' | 'oauth_api' | 'manual'
    window_seconds INTEGER NOT NULL DEFAULT 18000, -- 默认 5h = 18000s
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
-- 初始化 3 行数据由 migrate.run() 在 TASK-030 中插入
```

### Reporter 本地数据源字段映射

**Codex**（`~/.codex/.../token_count.rate_limits` jsonl 最新行）：

| Reporter 字段 | 来源 jsonl 字段 | 说明 |
|---------------|-----------------|------|
| `used_requests` | `rate_limits.requests.used` | 当前已用请求数 |
| `limit_requests` | `rate_limits.requests.limit` | 总限额 |
| `used_percent` | 计算 `used/limit*100` | |
| `resets_at` | `rate_limits.requests.reset_at` | ISO8601 UTC |
| `window_seconds` | 固定 18000（5h） | |

**Claude Code**（`~/.claude/projects/.../*.jsonl`，社区 ccusage 解析方案）：

| Reporter 字段 | 来源 | 说明 |
|---------------|------|------|
| `used_tokens` | 最近 5h 内 `usage.input_tokens + output_tokens` 累计 | 滑动窗口计算 |
| `limit_tokens` | Pro: 约 8000 msgs/5h tokens；实际值需用户配置 | 无官方 API |
| `used_percent` | 计算值 | |
| `resets_at` | 最早一条记录时间 + 5h | 估算 |
| `data_source` | `"local_jsonl"` | extra 字段 |

**ChatGPT**（手动输入）：

Reporter 读取工作站本地 `~/.panel_reporter/chatgpt.json`，由用户手动维护：

```json
{
  "used_messages": 30,
  "limit_messages": 80,
  "resets_at": "2026-06-28T20:00:00Z",
  "window_seconds": 10800,
  "updated_manually_at": "2026-06-28T09:00:00Z"
}
```

面板前端对手动数据来源显示「手动更新」降级卡，并用 `◌` 虚线状态符表示数据非实时。

---

## 部署方案

### 面板侧（树莓派，随 ARCH-001 容器一同部署）

- `api/ingest.py` 路由在 `create_app()` 中 `include_router`，无额外依赖
- `ai_provider` 表在 `migrate.run()` 时自动初始化（`IF NOT EXISTS`）
- 容器无需任何额外配置即可接收 Reporter 推送

### Reporter 侧（工作站，手动部署一次）

```bash
# 1. 复制脚本
cp tools/reporter/reporter.py ~/bin/panel-reporter

# 2. 配置
cp tools/reporter/reporter.example.env ~/.config/panel-reporter/env
# 编辑 PANEL_URL=http://<pi-tailscale-ip>:8000

# 3. cron（每 5 分钟）
crontab -e
# */5 * * * * source ~/.config/panel-reporter/env && python3 ~/bin/panel-reporter >> ~/.local/log/panel-reporter.log 2>&1
```

Reporter 是**无状态单文件脚本**，cron 触发 → 读本地文件 → POST → 退出。无守护进程，无长期网络连接。

---

## 前端渲染规范

### AI 额度卡片（`_ai_card.html`）

- 每个 provider 渲染一张独立 `<section class="card" data-module="ai-usage">`
- 核心展示：`used_percent` 进度条（`.metric-bar`）+ 分子/分母文字 + `resets_at` 倒计时
- 阈值变色规则：`used_percent < 70` → 正常绿；`70–90` → 警告黄（`.status-warn`）；`> 90` → 危险红（`.status-error`）
- Stale 标记：`stale = true` 时显示 `.datasource-banner`「数据可能过旧（上次更新 Xh 前）」
- ChatGPT 手动降级：`source_type = "manual"` 时显示「手动更新」小徽标 + `◌` 状态符
- e-ink 适配：无 box-shadow、无动画、进度条用 `border` 模拟避免 gradient

### 状态符映射

| 状态 | 符号 | CSS 类 |
|------|------|--------|
| 正常且新鲜 | ● | `.status-ok` |
| 接近上限（70–90%） | ◐ | `.status-warn` |
| 超限（>90%） | ● | `.status-error` |
| Stale | ○ | `.status-stale` |
| 手动输入 | ◌ | `.status-stale` |

---

## 技术可行性调研结论

本节记录 ARCH-004 设计阶段的调研结论，供 MS-004 实现时参考。

| 数据源 | 官方 API | 本地文件方案 | 结论 |
|--------|---------|-------------|------|
| Codex | 无公开个人额度 API | `~/.codex` jsonl 已实地确认存在 `token_count.rate_limits` | **可行，MVP 首选** |
| Claude Code | Anthropic 无公开个人订阅额度 API；OAuth usage 端点存在但未文档化，稳定性不确定 | `~/.claude/projects/` jsonl 已实地确认；社区 ccusage 工具可解析 | **可行，回退方案充分** |
| ChatGPT | OpenAI 无开放个人账号额度查询 API | 无本地文件源；浏览器扩展/代理方案复杂度过高 | **降级为手动输入** |

**结论**：三数据源均有可行方案，MVP 优先实现 Codex，Claude Code 作 P2，ChatGPT 手动降级。风险可控。

---

## 风险与注意事项

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| Codex jsonl 字段 schema 变更（OpenAI 未公开文档） | 中 | Reporter 加防御性解析 + 版本 fallback；Reporter 启动时打印检测到的字段 |
| Claude Code jsonl 字段随版本变化 | 中 | 参考 ccusage 开源解析器，跟进上游；tokens 累计可独立计算，不依赖汇总字段 |
| `~/.claude` OAuth usage 端点不稳定/下线 | 低（已有 JSONL 回退） | 优先 JSONL，OAuth 端点仅作可选增强 |
| Reporter cron 在工作站休眠/关机时不触发 | 中 | 面板 stale 机制已覆盖；前端明显标注数据时效 |
| ChatGPT 手动输入长期不更新 | 高（用户习惯） | stale 窗口设短（如 8h），超时显示 `◌` + 提示手动更新；降低期望 |
| tailnet 内 Reporter 到面板网络抖动 | 低 | POST 失败 Reporter 打印 warning，下次 cron 重试即可，面板侧 stale 覆盖 |

---

## 任务分解

| TASK ID | 标题 | 优先级 | 依赖 | 预估工作量 |
|---------|------|--------|------|-----------|
| TASK-030 | 面板摄取端点 POST /api/ingest/ai-usage + ai_provider 表 | P2 | TASK-002 | S |
| TASK-031 | Reporter MVP：Codex 本地 jsonl 解析 + POST 上报 | P2 | TASK-030 | M |
| TASK-032 | Reporter 扩展：Claude Code jsonl 解析 + OAuth usage 回退 | P3 | TASK-031 | M |
| TASK-033 | 前端 AI 额度卡片（泛化渲染、stale 标记、手动降级） | P2 | TASK-004, TASK-030 | M |
