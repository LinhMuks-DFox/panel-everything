---
id: TASK-031
title: "Reporter MVP：Codex 本地 jsonl 解析 + POST 上报"
status: todo
priority: P2
architecture: ARCH-004
dependencies: [TASK-030]
estimated_effort: M
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

**(MS-004 后期，本期不实现)**

在**工作站**上实现 Reporter MVP 脚本，读取 Codex 本地 jsonl 文件中的 `token_count.rate_limits` 字段，构造 AI 用量 payload，通过 tailnet POST 到面板的 `/api/ingest/ai-usage` 端点。脚本部署为工作站 cron job，每 5 分钟执行一次。

Codex 是优先级最高的数据源（本地文件、字段最纯净），作为整个 Reporter 体系的 MVP。

---

## 技术规格

### 文件位置

```
tools/reporter/
├── reporter.py            # 主入口（cron 调用这个文件）
├── sources/
│   ├── __init__.py
│   └── codex.py           # Codex 数据源
└── reporter.example.env   # 配置示例
```

### reporter.example.env 内容

```ini
# 面板地址（树莓派 tailscale IP 或 hostname）
PANEL_URL=http://100.x.x.x:8000
# 可选：摄取端点 token（与 settings.INGEST_TOKEN 对应，空则不发 header）
INGEST_TOKEN=
# Codex workspace 根目录（默认 ~/.codex）
CODEX_DIR=~/.codex
# 日志级别
LOG_LEVEL=INFO
```

### Codex 数据源规格（`sources/codex.py`）

**发现机制**：

```
~/.codex/
└── <workspace-id>/          # 可能有多个 workspace，取最近修改的
    └── logs/
        └── *.jsonl           # 取最新文件最后一行，或最大 mtime 的文件
```

**目标字段**（来自 jsonl 最新一行中的 `token_count.rate_limits`）：

```python
# 期望从 jsonl 行中解析到的结构（防御性访问，缺字段返回 None）
{
  "token_count": {
    "rate_limits": {
      "requests": {
        "used": 42,
        "limit": 500,
        "reset_at": "2026-06-28T15:00:00Z"
      },
      "tokens": {        # 可选，如存在则一并上报
        "used": 10000,
        "limit": 2000000,
        "reset_at": "2026-06-28T15:00:00Z"
      }
    }
  }
}
```

**构造的 metrics 列表**：

| metric | value_num | value_text | 备注 |
|--------|-----------|-----------|------|
| `used_requests` | `requests.used` | null | |
| `limit_requests` | `requests.limit` | null | |
| `used_percent` | `used/limit*100` | null | 保留 1 位小数 |
| `resets_at` | null | `requests.reset_at` | ISO8601 |
| `window_seconds` | 18000 | null | 固定 5h |
| `extra` | null | `json.dumps({"codex_dir": "<path>", "workspace": "<id>"})` | 调试用 |

若 `limit` 为 0 或 None，`used_percent` 置 `None`，`status` 置 `"error"`。

### reporter.py 主逻辑签名

```python
def load_env() -> dict[str, str]: ...
# 从环境变量或 ~/.config/panel-reporter/env 读配置

def post_payload(panel_url: str, token: str, payload: dict) -> bool: ...
# httpx.post(..., timeout=10)，失败返回 False，打印 warning

def main() -> None:
    config = load_env()
    sources = [CodexSource(config)]     # TASK-031 只含 Codex
    for source in sources:
        try:
            payload = source.collect()
            if payload:
                ok = post_payload(config["PANEL_URL"], config.get("INGEST_TOKEN",""), payload)
                logger.info(f"{source.name}: stored={ok}")
        except Exception as e:
            logger.warning(f"{source.name}: failed: {e}")
```

Reporter 使用**标准库 + httpx**，无其他依赖。httpx 通常工作站已有；若无，注释说明 `pip install httpx`。

### 工作站部署（非容器，手动一次）

```bash
# 1. 进入项目目录
# 2. 复制到 PATH 可见位置（或直接绝对路径调用）
cp tools/reporter/reporter.py ~/bin/panel-reporter
chmod +x ~/bin/panel-reporter

# 3. 创建配置
mkdir -p ~/.config/panel-reporter
cp tools/reporter/reporter.example.env ~/.config/panel-reporter/env
# 编辑 PANEL_URL

# 4. 注册 cron（每 5 分钟）
# */5 * * * * cd /path/to/panel_everything && python3 tools/reporter/reporter.py >> ~/.local/log/panel-reporter.log 2>&1
```

---

## 实现指引

1. **`sources/codex.py` 实现 `CodexSource` 类**
   - `__init__(self, config: dict)`: 解析 `CODEX_DIR`（默认 `~/.codex`），展开 `~`
   - `collect(self) -> dict | None`: 发现所有 workspace（`os.listdir`），取 `mtime` 最大的 workspace；在其 `logs/` 下取 `mtime` 最大的 `*.jsonl` 文件；读最后一行（`file.readlines()[-1]`）；`json.loads`；防御性提取 `token_count.rate_limits`；构造并返回 `AiUsagePayload` 字典
   - 若目录不存在、文件为空、字段缺失，记录 `warning` 并返回 `None`（不抛）

2. **防御性解析规范**
   - 全程用 `data.get("key")` 而非 `data["key"]`
   - 每次访问嵌套字段前检查上层是否为 `None`
   - `limit` 为 0 时视为未知，`used_percent = None`, `status = "error"`

3. **日志格式**（结构化，便于 grep）
   ```
   2026-06-28 10:00:00 INFO  codex: used=42/500 (8.4%) resets_at=2026-06-28T15:00:00Z
   2026-06-28 10:05:00 WARN  codex: rate_limits field not found in latest jsonl line
   ```

4. **reporter.py 设计要求**
   - 脚本可 `python3 reporter.py` 直接运行，无需 `pip install` 安装本项目包
   - `load_env` 优先读环境变量，回退读 `~/.config/panel-reporter/env`（key=value 格式）
   - 退出码：成功至少一个 source 上报成功 → 0；所有 source 失败 → 1（便于 cron 邮件告警）

5. **工作站不安装 Reporter 为容器**：`tools/reporter/` 是独立单目录，不在 `src/panel/` 内，不被 `pyproject.toml` 打包进面板镜像

---

## 测试要求

- [ ] `test_codex_source_ok`：给定 mock `~/.codex` fixture（含合法 jsonl），`CodexSource.collect()` 返回正确 payload，`used_percent` 精度 1 位小数
- [ ] `test_codex_source_missing_dir`：`CODEX_DIR` 不存在 → `collect()` 返回 `None`，不抛异常
- [ ] `test_codex_source_malformed_json`：jsonl 最后一行非法 JSON → 返回 `None`，日志含 `warning`
- [ ] `test_codex_source_zero_limit`：`limit=0` → `used_percent=None`，`status="error"`
- [ ] `test_post_payload_ok`：httpx mock 200 → 返回 `True`
- [ ] `test_post_payload_fail`：httpx mock 连接错误 → 返回 `False`，不抛
- [ ] `test_reporter_end_to_end`：mock CodexSource.collect + mock post_payload → main() 退出码 0

测试路径：`tests/reporter/test_codex.py`、`tests/reporter/test_reporter.py`

---

## 完成标准

- [ ] `tools/reporter/reporter.py` 可在工作站 `python3 reporter.py` 直接运行
- [ ] `tools/reporter/sources/codex.py` 防御性解析通过所有 mock fixture 测试
- [ ] `reporter.example.env` 包含完整配置说明注释
- [ ] cron 部署说明写入 `tools/reporter/README.md`（中文）
- [ ] 所有测试通过
- [ ] Reporter 脚本不向 `pyproject.toml` 运行时依赖追加任何包（仅 httpx，工作站预装）
