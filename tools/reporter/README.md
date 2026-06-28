# Panel Everything — 工作站 Reporter

无状态单文件 Python 脚本，部署在**工作站**（即跑 Codex / Claude Code 的机器），由
cron 每 5 分钟触发：读取本地 AI 用量数据 → 构造 payload → 通过 tailnet POST 到
树莓派面板的摄取端点 `POST /api/ingest/ai-usage`。

面板服务器（树莓派）不在工作站本地、读不到 `~/.codex` / `~/.claude`，故采用
**反向推送架构**（详见 `docs/architecture/ARCH-004.md`）。

```
工作站（reporter.py, cron 5m） ──POST /api/ingest/ai-usage──▶ 树莓派（面板）
   读 ~/.codex / ~/.claude / ~/.panel_reporter
```

## 目录结构

```
tools/reporter/
├── reporter.py            # 主入口（cron 调用）
├── sources/
│   ├── __init__.py
│   ├── codex.py           # Codex 本地 jsonl（MVP，最稳）
│   ├── claude_code.py     # Claude Code jsonl 滑动窗口 + 可选 OAuth 回退
│   └── chatgpt.py         # ChatGPT 手动输入降级
├── reporter.example.env   # 配置示例
├── README.md              # 本文件
└── test_reporter.py       # 单元测试（录制 fixture）
```

## 依赖

- Python 3.10+（标准库）
- `httpx`（上报用）。工作站通常已装；若无：`pip install httpx`

无需安装本项目包，可直接 `python3 reporter.py` 运行。

## 数据源说明

### Codex（MVP，优先级最高）

读取 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，取**最近修改文件中最后一条
含 `rate_limits` 的行**。

> 实地探查（2026-06 codex CLI）发现真实 schema 与早期文档假设不同：
> 数据在 `sessions/` 日期分层目录下，`rate_limits` 用 `primary`（5h，
> `window_minutes=300`）/ `secondary`（周限额）两个桶，桶里直接给
> `used_percent`，`resets_at` 是 **Unix epoch 秒**。脚本两路兼容真实 schema 与
> 文档假设的 `requests.used/limit/reset_at` schema。

上报指标：`used_percent` / `resets_at` / `window_seconds` /
`secondary_used_percent` / `secondary_resets_at` / `extra`。

### Claude Code（本地 jsonl 滑动窗口）

枚举 `~/.claude/projects/<hash>/*.jsonl`（仅扫 mtime 在 6h 内的文件），逐行筛
最近 **5h** 窗口内的事件，累计 `input_tokens + output_tokens +
cache_creation_input_tokens`（社区 ccusage 思路）。

> 实地探查发现 usage 字段嵌在 `message.usage`（非顶层 `usage`），脚本两路兼容。

- `CLAUDE_LIMIT_TOKENS` 未配置时仍上报 `used_tokens`，但 `used_percent` 为空
  （Anthropic 无公开个人订阅额度 API，上限需自行估算填入）。
- `resets_at` 估算为窗口内最早一条记录时间 + 5h。

#### 可选 OAuth 回退（实验性）

仅当配置了 `CLAUDE_SESSION_TOKEN` + `CLAUDE_ORG_ID` 时，额外尝试社区发现的
`GET https://claude.ai/api/organizations/{org_id}/usage` 端点；成功则与本地
jsonl 数据**取 `used_tokens` 最大值**，失败静默降级（debug 日志，不噪音）。

> **凭证安全警告**：`CLAUDE_SESSION_TOKEN` 是浏览器会话凭证（DevTools 获取），
> 有效期有限需定期更新。它**只在工作站本地读取，绝不上报到面板**；日志中已脱敏
> （仅前 8 位明文）。此端点未官方文档化，可能随时变更/下线，仅作可选增强。

### ChatGPT（降级：手动输入）

OpenAI 无个人额度 API 且无本地文件，降级为手动输入。维护
`~/.panel_reporter/chatgpt.json`：

```json
{
  "used_messages": 30,
  "limit_messages": 80,
  "resets_at": "2026-06-28T20:00:00Z",
  "window_seconds": 10800,
  "updated_manually_at": "2026-06-28T09:00:00Z"
}
```

文件不存在则跳过该 source。面板前端对此来源显示「手动更新」徽标。

## 部署（工作站，手动一次）

```bash
# 1. 复制脚本到 PATH 可见位置（或直接用项目内绝对路径）
cp tools/reporter/reporter.py ~/bin/panel-reporter
chmod +x ~/bin/panel-reporter

# 2. 创建配置
mkdir -p ~/.config/panel-reporter
cp tools/reporter/reporter.example.env ~/.config/panel-reporter/env
# 编辑 PANEL_URL=http://<pi-tailscale-ip>:8000

# 3. 日志目录
mkdir -p ~/.local/log
```

> 注意：`reporter.py` 依赖同目录的 `sources/` 包。若用 `cp` 单独复制脚本，
> 请确保 `sources/` 也在同目录下（或直接用项目内绝对路径调用，见下方 cron）。

## cron 注册（每 5 分钟）

```bash
crontab -e
```

加入一行（用项目内绝对路径，保证能找到 `sources/`）：

```cron
*/5 * * * * cd /path/to/panel-everything && /usr/bin/python3 tools/reporter/reporter.py >> ~/.local/log/panel-reporter.log 2>&1
```

脚本通过环境变量或 `~/.config/panel-reporter/env` 读配置；cron 环境变量少，
推荐用配置文件。也可指定其他配置文件：

```cron
*/5 * * * * REPORTER_ENV_FILE=~/.config/panel-reporter/env /usr/bin/python3 /path/to/panel-everything/tools/reporter/reporter.py >> ~/.local/log/panel-reporter.log 2>&1
```

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 至少一个 source 成功上报 |
| 1 | 所有 source 均失败（cron 会发邮件告警） |
| 2 | `PANEL_URL` 未配置 |

## 测试

```bash
# 纯标准库 unittest，无需项目依赖：
python3 -m unittest tools.reporter.test_reporter -v
# 或在项目根用 pytest：
.venv/bin/pytest tools/reporter/test_reporter.py -q
```
