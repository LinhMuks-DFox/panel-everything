---
id: TASK-032
title: "Reporter 扩展：Claude Code jsonl 解析 + OAuth usage 回退"
status: todo
priority: P3
architecture: ARCH-004
dependencies: [TASK-031]
estimated_effort: M
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

**(MS-004 后期，本期不实现)**

在 TASK-031 Reporter 骨架上，新增 Claude Code 数据源（`sources/claude_code.py`）：主路径读取工作站本地 `~/.claude/projects/` 下的 jsonl 文件，用滑动窗口计算最近 5 小时 token 用量（参考社区 ccusage 解析方案）；可选回退路径尝试 Claude OAuth usage 端点（未官方文档化，失败静默降级）。同步扩展 `reporter.py` 主流程加入 Claude Code source。

---

## 技术规格

### 文件位置

```
tools/reporter/
└── sources/
    └── claude_code.py     # 本卡新增
```

### Claude Code 本地 jsonl 解析（主路径）

**数据目录结构**（已实地确认存在）：

```
~/.claude/
└── projects/
    └── <project-hash>/
        └── *.jsonl        # 每行一个事件，含 usage 字段
```

**目标字段**（每行中的 usage 字段，参考 ccusage 解析）：

```json
{
  "type": "assistant",
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 567,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  },
  "timestamp": "2026-06-28T09:30:00Z"
}
```

**滑动窗口计算逻辑**：

1. 枚举 `~/.claude/projects/` 下所有 `*.jsonl` 文件
2. 逐行解析，筛选 `timestamp` 在 `[now - 5h, now]` 范围内的行
3. 累计 `input_tokens + output_tokens + cache_creation_input_tokens`
4. `resets_at` 估算：最早一条记录的 `timestamp + 5h`（若无历史记录，`resets_at = now + 5h`）
5. `limit_tokens`：无官方 API，读 `CLAUDE_LIMIT_TOKENS` 配置（默认 `None`）；若未配置，`used_percent = None`，但仍上报 `used_tokens`

**构造的 metrics 列表**：

| metric | value_num | value_text | 备注 |
|--------|-----------|-----------|------|
| `used_tokens` | 累计值 | null | 5h 窗口内 |
| `limit_tokens` | 配置值或 null | null | 无配置则 null |
| `used_percent` | 计算值或 null | null | limit 未知时 null |
| `resets_at` | null | 估算 ISO8601 | |
| `window_seconds` | 18000 | null | 5h |
| `extra` | null | `json.dumps({"data_source": "local_jsonl", "project_count": N, "line_count": M})` | |

**reporter.example.env 新增字段**：

```ini
# Claude Code token 窗口上限（选填，如不填则 used_percent 为空）
CLAUDE_LIMIT_TOKENS=
# Claude Code 数据目录（默认 ~/.claude/projects）
CLAUDE_PROJECTS_DIR=~/.claude/projects
```

### OAuth usage 端点（可选回退路径）

> **注意**：此端点未官方文档化，可能随时下线或变更。本路径为可选增强，主路径（jsonl）失败时才启用，且本身失败也静默降级。

**已知端点**（社区发现，实验性）：

```
GET https://claude.ai/api/organizations/{org_id}/usage
Authorization: sessionKey <token>
```

**配置项**（reporter.example.env）：

```ini
# Claude OAuth session token（从浏览器 DevTools 获取，选填）
# 警告：此 token 为会话凭证，有效期有限，需定期更新
CLAUDE_SESSION_TOKEN=
CLAUDE_ORG_ID=
```

**实现要求**：

- 仅当 `CLAUDE_SESSION_TOKEN` 非空时尝试
- `httpx.get(..., timeout=5)`，HTTP 非 200 或网络错误 → 静默返回 `None`，记录 `debug` 级日志（不用 warning，避免噪音）
- 若 OAuth 返回有效数据，与 jsonl 本地数据**合并取最大值**（OAuth 数据可能更准）；若 OAuth 失败，纯用 jsonl 数据
- **凭证安全**：`CLAUDE_SESSION_TOKEN` 只读不存到面板；若 Reporter 打印调试信息，token 前 8 位后掩码

### ClaudeCodeSource 类签名

```python
class ClaudeCodeSource:
    name: str = "claude_code"

    def __init__(self, config: dict): ...
    # 解析 CLAUDE_PROJECTS_DIR, CLAUDE_LIMIT_TOKENS, CLAUDE_SESSION_TOKEN, CLAUDE_ORG_ID

    def _collect_jsonl(self) -> dict | None: ...
    # 主路径：读本地文件，滑动窗口计算

    def _collect_oauth(self) -> dict | None: ...
    # 可选回退路径：OAuth 端点，失败返回 None

    def collect(self) -> dict | None: ...
    # 调 _collect_jsonl，若 CLAUDE_SESSION_TOKEN 有配置则额外调 _collect_oauth 合并
```

---

## 实现指引

1. **`sources/claude_code.py` 开发顺序**
   - 先实现 `_collect_jsonl`（本地，可单元测试，不依赖网络）
   - 再实现 `_collect_oauth`（网络，用 httpx mock 测试）
   - `collect()` 最后组合

2. **滑动窗口时间处理**
   - 所有时间比较用 UTC（`datetime.now(timezone.utc)`）
   - jsonl 中的 `timestamp` 字段格式可能有 `Z` 后缀，用 `datetime.fromisoformat(ts.replace("Z", "+00:00"))` 解析

3. **大量 jsonl 文件性能考虑**
   - `~/.claude/projects/` 可能有大量历史文件
   - 只读 `mtime > now - 6h` 的文件（多读 1h 缓冲，避免边界遗漏）
   - 单文件行数可能很大：从文件末尾向前扫（`seek`），一旦遇到 `timestamp < now - 5h` 的行即停止

4. **reporter.py 更新**
   - `sources = [CodexSource(config), ClaudeCodeSource(config)]`
   - 若 `CLAUDE_PROJECTS_DIR` 不存在，`ClaudeCodeSource.__init__` 打印 `info` 提示，`collect()` 返回 `None`（不报错）

5. **OAuth token 脱敏**
   ```python
   def _mask_token(t: str) -> str:
       return t[:8] + "****" if len(t) > 8 else "****"
   ```

---

## 测试要求

- [ ] `test_claude_jsonl_window_calc`：给定 mock 文件，含 `now-6h` 和 `now-2h` 两条记录，只累计后者
- [ ] `test_claude_jsonl_missing_dir`：`CLAUDE_PROJECTS_DIR` 不存在 → `collect()` 返回 `None`
- [ ] `test_claude_jsonl_no_limit`：`CLAUDE_LIMIT_TOKENS` 未配置 → payload 有 `used_tokens`，`used_percent = None`
- [ ] `test_claude_jsonl_with_limit`：配置 `CLAUDE_LIMIT_TOKENS=100000` → `used_percent` 计算正确
- [ ] `test_claude_oauth_skip`：`CLAUDE_SESSION_TOKEN` 空 → `_collect_oauth` 不被调用
- [ ] `test_claude_oauth_fail_silent`：httpx mock 网络错误 → `collect()` 仍返回 jsonl 数据（不 None），不抛异常
- [ ] `test_claude_oauth_merge`：OAuth 返回更大值 → 合并后取最大
- [ ] `test_token_masking`：日志输出中 token 被掩码（仅前 8 位明文）
- [ ] `test_reporter_two_sources`：reporter.main() with CodexSource+ClaudeCodeSource mock → 两次 POST 均调用

测试路径：`tests/reporter/test_claude_code.py`

---

## 完成标准

- [ ] `tools/reporter/sources/claude_code.py` 实现完整，主路径（jsonl）工作正常
- [ ] OAuth 回退路径：配置有 token 时尝试，失败静默降级，无 unhandled exception
- [ ] `reporter.example.env` 增加 Claude Code 相关配置项及说明注释
- [ ] `tools/reporter/README.md` 新增 Claude Code 数据源说明（含 token 安全警告）
- [ ] 所有测试通过
- [ ] OAuth token 在日志中已脱敏，不明文打印
