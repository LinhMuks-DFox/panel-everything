# 模块 10 · Reporter（工作站 AI 用量上报器）

> 模块路径：`tools/reporter/`
> 关联需求：REQ-004（AI 使用额度监控）
> 关联架构：ARCH-004（AI 使用额度监控，反向推送拓扑）
> 关联任务：TASK-031（Reporter MVP / Codex）、TASK-032（Claude Code + OAuth 回退）；契约对齐 TASK-030（面板摄取端点）
> 面向读者：本模块的开发者与维护者。读完本文即可理解整套反向推送架构、三数据源解析逻辑、payload 契约、部署方式，以及如何新增一个数据源，无需通读全部源码。

---

## 1. 模块概述与职责

Reporter 是一个**无状态的单文件 Python 脚本集**，部署在**工作站**（即实际运行 Codex / Claude Code / ChatGPT 的开发机），由工作站 **cron 每 5 分钟触发一次**：读取本地 AI 用量数据源 → 构造符合面板契约的 payload → 通过 tailnet `POST` 到树莓派面板的摄取端点 `POST /api/ingest/ai-usage`，然后退出。

它解决的核心问题是**数据位置与渲染位置分离**：AI 用量数据（额度、token 计数）只存在于工作站本地目录（`~/.codex`、`~/.claude`），而面板服务跑在树莓派上，**读不到工作站的本地文件**。ARCH-004 据此选择**反向推送架构**（Reporter 主动 push，面板被动 ingest），而非面板主动 pull：

```
工作站（reporter.py，cron 5m）                          树莓派（面板）
┌────────────────────────────────┐                    ┌──────────────────────────────┐
│ 读 ~/.codex/sessions/*.jsonl    │                    │ POST /api/ingest/ai-usage     │
│ 读 ~/.claude/projects/*.jsonl   │ ── tailnet POST ──▶│  → latest_snapshot            │
│ 读 ~/.panel_reporter/chatgpt.json│   {provider,       │  → metric_history             │
│ (可选) OAuth usage 端点          │    metrics[]}      │ (collector='ai_usage')        │
└────────────────────────────────┘                    └──────────────────────────────┘
```

设计要点：
- **无守护进程、无长连接、无状态**：每次 cron 运行都是一次独立的「读→POST→退出」，崩溃/网络抖动靠下一次 cron 自愈，面板侧用 stale 机制兜底。
- **零项目依赖**：仅用标准库 + `httpx`（上报用，工作站通常已装）。**不依赖安装本项目包**，可直接 `python3 reporter.py`。
- **防御性优先**：单个 source 解析失败/数据缺失只返回 `None` 并记日志，绝不抛出让脚本崩溃；多 source 之间互不影响。
- **三数据源分级**（ARCH-004）：Codex（MVP，最稳，纯本地文件）> Claude Code（本地 jsonl 滑窗 + 可选 OAuth 回退）> ChatGPT（降级为手动输入）。

---

## 2. 文件与关键符号清单

```
tools/reporter/
├── reporter.py            # 主入口（cron 调用）：load_env / build_sources / post_payload / main
├── sources/
│   ├── __init__.py        # 导出三个 source 类，定义 source 协议契约（docstring）
│   ├── codex.py           # CodexSource：本地 rollout jsonl → rate_limits
│   ├── claude_code.py     # ClaudeCodeSource：projects jsonl 5h 滑窗 + 可选 OAuth 回退
│   └── chatgpt.py         # ChatGptSource：手动维护的 json 降级读取
├── reporter.example.env   # 配置示例（复制到 ~/.config/panel-reporter/env）
├── README.md              # 工作站部署/cron 注册说明（中文）
└── test_reporter.py       # 纯标准库 unittest（也可 pytest 收集）
```

### `reporter.py` — 主入口与上报逻辑

- 模块顶部 `reporter.py:27-31` — 把脚本所在目录 `_THIS_DIR` 插入 `sys.path`，使 `from sources import ...` 在任意 cwd 下都可导入（cron 工作目录不固定时的关键）。
- 常量 `reporter.py:37-39` — `DEFAULT_ENV_FILE="~/.config/panel-reporter/env"`、`INGEST_PATH="/api/ingest/ai-usage"`、`POST_TIMEOUT=10`。
- `_parse_env_file(path) -> dict[str, str]` `reporter.py:45` — 解析 `KEY=VALUE` 配置文件：忽略空行与 `#` 注释，剥离 `export ` 前缀，去除值两端的单/双引号；文件不存在或读失败时返回空 dict（不抛）。
- `load_env() -> dict[str, str]` `reporter.py:71` — 先读配置文件，再用**进程环境变量覆盖**同名键（环境变量优先级更高）。`known_keys`（`reporter.py:75-86`）列出所有可被环境覆盖的配置键。
- `_resolve_token(config) -> str` `reporter.py:93` — Token 解析：`INGEST_TOKEN` 为主键，`REPORTER_TOKEN` 为兼容别名。
- `post_payload(panel_url, token, payload) -> bool` `reporter.py:101` — POST payload 到面板。延迟 `import httpx`（缺失时记 warning 返回 False）；拼接 `panel_url.rstrip("/") + INGEST_PATH`；token 非空时加 `Authorization: Bearer <token>` 头。**任何网络异常或非 200 都返回 False（不抛）**，并记 warning（非 200 时截前 200 字符响应体）。
- `build_sources(config) -> list` `reporter.py:130` — 构造启用的 source 列表，固定顺序 `[CodexSource, ClaudeCodeSource, ChatGptSource]`。**新增 source 的挂载点**（见 §6）。
- `main() -> int` `reporter.py:139` — 主流程：`load_env` → 配置 logging → 校验 `PANEL_URL`（缺失返回 2）→ 解析 token → `build_sources` → 逐 source `collect()` 并 `post_payload`，每个 source 用 try/except 包裹（单 source 异常不影响其他）。返回退出码（见 §3.3）。

### `sources/__init__.py` — source 协议契约

- 仅 re-export 三个 source 类（`reporter.py` 从此包导入）。
- **docstring 即是 source 协议的规范文档**：每个 source 类须有类属性 `name`（= 面板 provider 名）、`__init__(self, config: dict)`（不抛异常）、`collect(self) -> dict | None`（数据缺失/解析失败返回 `None`，不抛）。

### `sources/codex.py` — Codex 本地 rollout jsonl 解析（MVP）

- 常量 `DEFAULT_WINDOW_SECONDS=18000` `codex.py:48` — 5h 滑动窗口（秒），与 codex `primary` 桶的 `window_minutes=300` 一致。
- `_epoch_to_iso(value) -> str | None` `codex.py:55` — 把 Unix epoch 秒（int/float/纯数字字符串）转 ISO8601 UTC 字符串；非数字字符串视为已格式化时间原样返回；无法解析返回 `None`。**Codex 的 `resets_at` 是 epoch 秒，这是与文档假设的关键差异之一。**
- `_round1(x)` `codex.py:80` — 保留 1 位小数。
- `class CodexSource` `codex.py:84`，`name="codex"`：
  - `__init__` `codex.py:89` — 解析 `CODEX_DIR`（默认 `~/.codex`）；优先用 `sessions/` 子目录，保留 `codex_dir` 作回退根。
  - `_candidate_files() -> list[str]` `codex.py:98` — 收集候选 jsonl，按 mtime **降序**。优先递归扫 `sessions/` 下的 `*.jsonl`（真实布局）；`sessions/` 不存在时回退扫 `<codex_dir>/<workspace>/logs/*.jsonl`（文档假设的旧布局）。
  - `_find_rate_limits(obj) -> dict | None` `codex.py:131`（静态）— 在任意嵌套结构里**深度优先**找第一个 `rate_limits` 字典。
  - `_latest_rate_limits() -> tuple[dict, str] | None` `codex.py:149` — 从最近修改的文件开始，逐文件**从末尾向前**扫，找到第一个含 `rate_limits` 的合法行即返回 `(rate_limits, 文件路径)`（即最新一条）。
  - `_extract_metrics(rl) -> tuple[list[dict], str]` `codex.py:178` — 双 schema 兼容（见 §3.1）：优先真实 schema（`primary`/`secondary` 桶 + `used_percent` + epoch `resets_at`），回退文档 schema（`requests.used`/`.limit`/`.reset_at`）。返回 `(metrics, status)`。
  - `collect()` `codex.py:287` — 目录不存在/无 rate_limits/无法识别 → 记日志返回 `None`；否则附加 `extra`（含来源文件相对路径）与结构化日志，返回完整 payload。

### `sources/claude_code.py` — Claude Code 本地 jsonl 滑窗 + 可选 OAuth 回退

- 常量 `WINDOW_SECONDS=18000`（5h）、`FILE_MTIME_BUFFER_SECONDS=WINDOW_SECONDS+3600`（多读 1h 缓冲避免 mtime 边界遗漏）`claude_code.py:35-37`。
- 常量 `_OAUTH_USAGE_URL` `claude_code.py:40` — `https://claude.ai/api/organizations/{org_id}/usage`（社区发现，实验性，可能随时变更/下线）。
- `_parse_ts(ts) -> datetime | None` `claude_code.py:47` — 解析 ISO8601（容忍 `Z` 后缀），返回 aware UTC datetime；无 tzinfo 时补 UTC。
- `_mask_token(t) -> str` `claude_code.py:60` — **凭证脱敏**：仅保留前 8 位明文，其余 `****`；长度 ≤ 8 全掩码。
- `_to_int(v) -> int` `claude_code.py:67` — 仅 int/float 转 int，否则 0（防御性累加）。
- `class ClaudeCodeSource` `claude_code.py:71`，`name="claude_code"`：
  - `__init__` `claude_code.py:76` — 解析 `CLAUDE_PROJECTS_DIR`（默认 `~/.claude/projects`）、`CLAUDE_LIMIT_TOKENS`（非整数则忽略并 warning）、`CLAUDE_SESSION_TOKEN`、`CLAUDE_ORG_ID`。
  - `_recent_files(now) -> list[str]` `claude_code.py:97` — 枚举 `*.jsonl`，仅保留 mtime 在 6h 缓冲窗口内的（性能优化，避免扫海量历史文件）。
  - `_extract_usage(d) -> dict | None` `claude_code.py:113`（静态）— 取 usage：**先 `message.usage`，回退顶层 `usage`**（真实 schema 嵌在 `message.usage` 下）。
  - `_collect_jsonl() -> dict | None` `claude_code.py:123` — 主路径：逐文件从末尾向前扫，累计 5h 窗口内的 `input_tokens + output_tokens + cache_creation_input_tokens`（**`cache_read_input_tokens` 不计入**，不计费窗口）；遇早于窗口的有时间戳行即 `break`；`resets_at` = 最早记录 + 5h（无则 now + 5h）；配置了 limit 才算 `used_percent`。窗口内无记录 → `None`。
  - `_collect_oauth() -> dict | None` `claude_code.py:228` — 可选回退：未配 token/org_id 或未装 httpx → `None`（debug 日志）；`httpx.get(timeout=5)` 任何异常/非 200/非 JSON → 静默 `None`（debug，不噪音）；保守嗅探 `used_tokens`/`used`/`tokens_used` 字段，命中返回 `{"used_tokens": int}`。
  - `collect()` `claude_code.py:285` — 组合逻辑（见 §3.3 合并规则）。

### `sources/chatgpt.py` — ChatGPT 手动输入降级

- 常量 `DEFAULT_WINDOW_SECONDS=10800` `chatgpt.py:29` — ChatGPT 常见 3h 窗口（注意与 Codex/Claude 的 5h 不同）。
- `class ChatGptSource` `chatgpt.py:36`，`name="chatgpt"`：
  - `__init__` `chatgpt.py:41` — 解析 `CHATGPT_JSON`（默认 `~/.panel_reporter/chatgpt.json`）。
  - `collect()` `chatgpt.py:47` — 读手动维护的 json：文件不存在（info 日志，跳过）/ 读失败 / 非法 JSON / 非 dict / 缺 `used_messages` → `None`；`limit_messages` 为 0/缺失 → `used_percent` 不算，`status="error"`；`extra` 含 `data_source="manual"` 与 `updated_manually_at`。

### `test_reporter.py` — 单元测试

见 §8。

---

## 3. 关键数据结构 / 契约

### 3.1 上报 payload 契约（对齐 TASK-030 / ARCH-004）

所有 source 的 `collect()` 返回**同一形状的 dict**，即面板 `POST /api/ingest/ai-usage` 的请求体。该形状由面板侧 Pydantic 模型 `AiUsagePayload`（`src/panel/domain/models.py:227`）校验：

```jsonc
{
  "reporter_version": "1.0",                       // 固定字符串
  "reported_at": "2026-06-28T10:00:00+00:00",      // ISO8601 UTC（datetime.now(utc).isoformat()）
  "provider": "codex",                             // = source.name（codex/claude_code/chatgpt）
  "status": "ok",                                  // "ok" | "error"
  "metrics": [                                     // list[AiMetricItem]
    {"metric": "used_percent",    "value_num": 46.0,  "value_text": null},
    {"metric": "resets_at",       "value_num": null,  "value_text": "2026-06-17T..."},
    {"metric": "window_seconds",  "value_num": 18000.0,"value_text": null},
    {"metric": "extra",           "value_num": null,  "value_text": "{\"data_source\": \"local_jsonl\"}"}
  ]
}
```

每条 metric 是 `AiMetricItem`（`models.py:214`）：`{metric: str, value_num: float|None, value_text: str|None}`。**数值型走 `value_num`，文本/JSON 型走 `value_text`**。面板端点把每条 metric 转成一个 `MetricSample`（`collectors/base.py:23`，`target_id=ai_provider.id`、`collected_at=reported_at`、`status=body.status`），写入 `latest_snapshot`（upsert）与 `metric_history`（append），`collector="ai_usage"`。

> **契约松绑点**：`AiUsagePayload.provider` 用 `str` 而非 `Literal`，未知 provider 不在 Pydantic 层 422 拒绝，而是由端点查 `ai_provider` 表，未命中返回 400 `{"ok": false, "error": "unknown provider: <x>"}`（`api/ingest.py:54`）。`status` 默认 `"ok"`。

### 3.2 各 source 产出的 metric 名清单

测试 `test_reporter.py:37` 维护了**已知 metric 名白名单** `KNOWN_METRICS`，新增 metric 必须同步加入：

| metric | 类型 | Codex | Claude Code | ChatGPT | 说明 |
|--------|------|:----:|:----:|:----:|------|
| `used_percent` | num | ✓ | ✓(有 limit 时) | ✓ | 0–100，1 位小数 |
| `used_tokens` | num |  | ✓ |  | 5h 窗口累计 token |
| `limit_tokens` | num |  | ✓(可空) |  | 来自 `CLAUDE_LIMIT_TOKENS` |
| `used_requests` | num |  |  | ✓ | = `used_messages` |
| `limit_requests` | num | (回退 schema) |  | ✓ | = `limit_messages` |
| `resets_at` | text | ✓ | ✓ | ✓ | ISO8601 UTC |
| `window_seconds` | num | ✓ | ✓ | ✓ | Codex/Claude=18000, ChatGPT=10800 |
| `secondary_used_percent` | num | ✓(有 secondary 桶) |  |  | 周限额桶 |
| `secondary_resets_at` | text | ✓(有 secondary 桶) |  |  | 周限额重置 |
| `extra` | text(JSON) | ✓ | ✓ | ✓ | 调试元数据，含 `data_source` |

`extra.data_source` 取值：`local_jsonl`（Codex/Claude 本地）、`oauth_api`（Claude OAuth-only 路径）、`manual`（ChatGPT）。

### 3.3 退出码

| 码 | 含义 |
|----|------|
| 0 | 至少一个 source 成功上报（`post_payload` 返回 True） |
| 1 | 所有 source 均失败/无数据（便于 cron 邮件告警） |
| 2 | 配置缺失（`PANEL_URL` 未设置） |

### 3.4 Codex 双 schema 兼容（`_extract_metrics`）

实地探查（2026-06）发现 codex CLI 真实 schema 与 ARCH-004 早期文档假设不同。`_extract_metrics` 两路兼容：

- **真实 schema（主路径）**：`rate_limits.primary` / `secondary` 桶，桶里直接给 `used_percent`（百分比），`resets_at` 是 **Unix epoch 秒**，`window_minutes` 给窗口（primary=300=5h）。无 `primary.used_percent` → `status="error"`。
- **文档 schema（回退）**：`rate_limits.requests.used` / `.limit` / `.reset_at`，`used_percent = used/limit*100`。`limit` 为 0/None → `used_percent` 缺失、`status="error"`。可选 `tokens` 桶。

### 3.5 Claude Code 本地+OAuth 合并规则（`collect`）

```
1. payload = _collect_jsonl()                  # 主路径
2. 未配 session_token → 直接返回 payload        # 不触碰 OAuth
3. oauth = _collect_oauth()；失败/无 used → 返回 payload
4. payload 为 None 但 OAuth 有数据 → 用 OAuth 单独构造最小 payload（data_source=oauth_api）
5. 二者都有 → used_tokens 取最大（OAuth 可能更准），有 limit 时重算 used_percent
```

### 3.6 ChatGPT 手动文件格式（`~/.panel_reporter/chatgpt.json`）

```json
{
  "used_messages": 30,
  "limit_messages": 80,
  "resets_at": "2026-06-28T20:00:00Z",
  "window_seconds": 10800,
  "updated_manually_at": "2026-06-28T09:00:00Z"
}
```

---

## 4. 对外接口与调用关系

**被谁调用**：工作站 cron（`*/5 * * * *`）调用 `python3 tools/reporter/reporter.py`，入口 `main()`（`reporter.py:173` 的 `if __name__ == "__main__"`）。

**调用谁 / 数据流**：

```
cron → main()
        ├─ load_env()  ← 读 ~/.config/panel-reporter/env + 环境变量
        ├─ build_sources(config) → [CodexSource, ClaudeCodeSource, ChatGptSource]
        └─ for source in sources:
             source.collect()  ← 读本地文件 / (Claude 可选) httpx.get OAuth
             post_payload(panel_url, token, payload)  → httpx.post → 面板 POST /api/ingest/ai-usage
```

**面板侧消费链**（下游，本模块不实现，仅契约对接）：
`api/ingest.py` 端点 → `repo.get_ai_provider_id(provider)` → `repo.upsert_snapshot("ai_usage", samples)` + `repo.append_history("ai_usage", samples)` → 前端 `GET /api/ai-usage`（`api/ai_usage.py`）聚合 → SSR `_ai_card.html` 渲染。

---

## 5. 与其他模块的依赖

**上游（本模块依赖的外部数据）**：
- 工作站本地文件：`~/.codex/sessions/`、`~/.claude/projects/`、`~/.panel_reporter/chatgpt.json`（均为第三方工具产生，schema 不受本项目控制 → 防御性解析）。
- 配置文件 `~/.config/panel-reporter/env` 与进程环境变量。
- 第三方库 `httpx`（仅上报与 OAuth 用；缺失时优雅降级）。

**下游（消费本模块产出的面板侧模块，本仓库内）**：
- `src/panel/api/ingest.py`（模块 07）— 摄取端点，契约见 §3.1，鉴权见 §7。
- `src/panel/domain/models.py` — `AiUsagePayload` / `AiMetricItem` 校验请求体。
- `src/panel/db/repository.py` — `get_ai_provider_id` / `upsert_snapshot` / `append_history`（模块 03）。
- `src/panel/db/migrations/004_ai_provider.sql` — `ai_provider` 表（`codex`/`claude_code`/`chatgpt` 三行），provider 名必须与本模块各 source 的 `name` 一致。
- 前端 `api/ai_usage.py` + `web/templates/partials/_ai_card.html`（模块 07/08，TASK-033）。

> **重要解耦**：Reporter 不在 `src/panel/` 内，不被 `pyproject.toml` 打包进面板镜像（TASK-031 完成标准）。两侧唯一的耦合是 §3.1 的 JSON 契约与 provider 名集合。

---

## 6. 扩展点

### 6.1 新增一个数据源（最常见）

例如新增 `gemini` 数据源：

1. **建文件** `tools/reporter/sources/gemini.py`，实现一个类，遵守 `sources/__init__.py` docstring 定义的协议：
   - 类属性 `name = "gemini"`（必须与面板 `ai_provider` 表的 provider 名完全一致）。
   - `__init__(self, config: dict)`：从 config 取所需路径/开关，**不抛异常**。
   - `collect(self) -> dict | None`：返回 §3.1 形状的 payload；任何缺失/解析失败返回 `None` 并记日志（不抛）。`reporter_version="1.0"`、`reported_at=datetime.now(timezone.utc).isoformat()`、`provider=self.name`。
2. **导出** 在 `sources/__init__.py` 加 `from .gemini import GeminiSource` 并加入 `__all__`。
3. **挂载** 在 `reporter.py` `build_sources()`（`reporter.py:130`）的列表里 append `GeminiSource(config)`，并在 `load_env()` 的 `known_keys`（`reporter.py:75`）补上新配置键（否则环境变量无法覆盖）。
4. **面板侧同步**（下游，必须）：
   - 在 `004_ai_provider.sql`（或新迁移）`INSERT OR IGNORE` 一行 `gemini` provider，否则端点返回 400 unknown provider。
   - 若引入新 metric 名，把它加进面板聚合逻辑（`api/ai_usage.py`）与本模块测试的 `KNOWN_METRICS`（`test_reporter.py:37`）。
5. **配置示例** 在 `reporter.example.env` 补注释与默认值。
6. **测试** 在 `test_reporter.py` 加一个 `TestGeminiSource`，覆盖正常解析 + 至少缺失目录/非法 JSON 两个降级分支，并对 payload 调 `_assert_contract`。

### 6.2 新增一个 metric 字段（不新增 source）

在某 source 的 `collect()`/`_extract_metrics` 里 `metrics.append({"metric": "<新名>", "value_num"/"value_text": ...})`。**同步把新名加入** `test_reporter.py` 的 `KNOWN_METRICS`，并确认面板聚合层会消费它（否则会被存进 `metric_history` 但前端不展示）。

### 6.3 新增一个配置键

加到 `reporter.example.env`（带中文注释）+ `reporter.py` `load_env()` 的 `known_keys` 元组。两处缺一会导致环境变量覆盖失效或文件配置不被识别。

---

## 7. 配置 / 环境变量

配置来源优先级：**进程环境变量 > 配置文件**。默认配置文件 `~/.config/panel-reporter/env`，可用 `REPORTER_ENV_FILE` 改路径。格式 `KEY=VALUE`，`#` 注释，支持 `export ` 前缀与值两端引号。完整示例见 `tools/reporter/reporter.example.env`。

| 键 | 默认 | 用途 |
|----|------|------|
| `PANEL_URL` | （无，**必填**） | 面板地址（树莓派 tailscale IP/hostname + 端口），缺失退出码 2 |
| `INGEST_TOKEN` | `""` | 摄取端点 Bearer token；空则不发 `Authorization` 头 |
| `REPORTER_TOKEN` | — | `INGEST_TOKEN` 的兼容别名（`_resolve_token` 回退） |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `CODEX_DIR` | `~/.codex` | Codex 根目录（扫 `sessions/` 下 rollout） |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Claude Code 数据目录 |
| `CLAUDE_LIMIT_TOKENS` | （空） | Claude 5h 窗口 token 上限（自行估算）；不填则 `used_percent` 为空 |
| `CLAUDE_SESSION_TOKEN` | （空） | 可选 OAuth 会话凭证（浏览器 DevTools 获取），**只本地读，绝不上报** |
| `CLAUDE_ORG_ID` | （空） | 可选 OAuth org id；与 session token 都非空才尝试 OAuth |
| `CHATGPT_JSON` | `~/.panel_reporter/chatgpt.json` | ChatGPT 手动维护文件 |

> 面板侧对应 `settings.ingest_token`（`src/panel/config/settings.py:83`，环境变量 `PANEL_INGEST_TOKEN`）。两端 token 必须一致；面板侧为空则跳过鉴权（内网默认）。

---

## 8. 测试位置与覆盖

测试文件：`tools/reporter/test_reporter.py`（纯标准库 `unittest`，也可 pytest 收集；**注意不在仓库顶层 `tests/` 下，而与脚本同目录**——TASK-031 卡曾写 `tests/reporter/`，实现落在 `tools/reporter/test_reporter.py`）。

运行：
```bash
python3 -m unittest tools.reporter.test_reporter -v
# 或
.venv/bin/pytest tools/reporter/test_reporter.py -q
```

覆盖矩阵：

| 测试类 | 覆盖点 |
|--------|--------|
| `TestCodexSource` | 真实 primary/secondary schema、取最后一条（最新）、文档 requests 回退、limit=0→error、缺目录→None、无 rate_limits→None、非法 JSON 行被跳过 |
| `TestClaudeCodeSource` | 5h 窗口排除旧记录、`message.usage` 嵌套、无 limit→percent None、有 limit 算 percent、缺目录→None、无 token 跳过 OAuth、OAuth 失败仍回 jsonl、OAuth 合并取最大、`_mask_token` 脱敏 |
| `TestChatGptSource` | 手动文件正常、缺文件→None、非法 JSON→None、limit=0→error |
| `TestPostPayload` | URL 拼接 + 尾斜杠、Bearer 头有/无、非 200→False、网络异常→False、body 符合 TASK-030 契约（用 `_FakeHttpx` 替身，不发真网络） |
| `TestMainEndToEnd` | 缺 PANEL_URL→2、成功→0、全失败→1、两 source 各 POST 一次 |

关键测试工具：`_assert_contract`（`test_reporter.py:58`）断言 payload 符合 §3.1 契约；`KNOWN_METRICS`（`test_reporter.py:37`）约束 metric 名白名单；`_FakeHttpx`（`test_reporter.py:402`）经 `mock.patch.dict(sys.modules, {"httpx": fake})` 注入，验证 `post_payload` 行为不触网。

---

## 9. 注意事项 / 降级语义 / gotchas

- **绝不抛异常上抛**：`collect()` 失败返回 `None`，`post_payload` 失败返回 `False`，`main()` 对每个 source try/except。单点失败不影响整体，靠下次 cron 重试 + 面板 stale 兜底。
- **schema 漂移是头号风险**（ARCH-004 风险表）。Codex/Claude 的本地文件 schema 由第三方工具产生、未公开文档化，已发生过两次「文档假设 vs 真实」偏差，均已两路兼容：
  - Codex：`primary/secondary` 桶 + `used_percent` + **epoch `resets_at`**（非 `requests.used/limit` + ISO 字符串）；目录是 `sessions/YYYY/MM/DD/`（非 `<workspace>/logs/`）。
  - Claude：usage 嵌在 **`message.usage`**（非顶层 `usage`）。
  升级第三方工具后若数据停更，优先怀疑 schema 变化，先开 `LOG_LEVEL=DEBUG` 看检测到的字段。
- **凭证安全（白名单/脱敏）**：`CLAUDE_SESSION_TOKEN` 是浏览器会话凭证，有效期有限需定期更新；**只在工作站本地读取，绝不放进任何上报 payload**；日志中经 `_mask_token` 脱敏（仅前 8 位明文）。OAuth 端点未官方文档化，可能随时下线 → 任何失败都静默降级（debug 日志，不刷 warning 噪音）。
- **OAuth 仅作可选增强**：未配 `CLAUDE_SESSION_TOKEN`+`CLAUDE_ORG_ID` 时完全不触碰网络，主路径纯本地 jsonl。
- **`cache_read_input_tokens` 不计入** Claude 用量累计（不计费窗口），只计 `input + output + cache_creation`。
- **窗口差异**：Codex/Claude = 5h（18000s），ChatGPT = 3h（10800s）。stale 判定在面板侧按 `window_seconds*0.5` 现算（非本模块职责）。
- **cron 工作目录不固定**：`reporter.py` 顶部把自身目录注入 `sys.path`，但 `cp` 单独复制脚本会丢 `sources/` 包——README 建议 cron 用项目内**绝对路径**调用，或确保 `sources/` 随脚本同目录。
- **httpx 缺失**：上报路径会记 warning 返回 False（视作上报失败）；OAuth 路径记 debug 跳过。脚本本身不会因此崩溃。
- **`reported_at` 时区**：均为 `datetime.now(timezone.utc).isoformat()`（aware UTC，带 `+00:00`）。Codex epoch、Claude jsonl 的 `Z` 后缀都已在解析侧归一到 UTC。
- **provider 名是硬契约**：source 的 `name` 必须与面板 `ai_provider` 表行完全一致，否则端点 400。
- **性能**：Claude 只扫 mtime 在 6h 内的文件，且单文件从末尾向前扫、遇窗口外行即停；Codex 按 mtime 降序、找到首个 rate_limits 即返回。避免扫海量历史。

---

## 10. 关联 REQ / ARCH / TASK 编号

- **REQ-004** — AI 使用额度监控（需求来源）。
- **ARCH-004** — 反向推送拓扑、三数据源分级策略、payload 契约、部署方案、风险表。
- **TASK-030** — 面板摄取端点 `POST /api/ingest/ai-usage` + `ai_provider` 表（本模块的契约对端，由 `src/panel/api/ingest.py` 实现）。
- **TASK-031** — Reporter MVP：`reporter.py` 骨架（`load_env`/`post_payload`/`main`/`build_sources`）+ Codex 数据源。
- **TASK-032** — Reporter 扩展：Claude Code jsonl 滑窗 + 可选 OAuth 回退；同步加入 ChatGPT 手动降级 source。
- **TASK-033** — 前端 AI 额度卡片（下游消费，不在本模块）。
