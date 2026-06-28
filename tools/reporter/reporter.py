#!/usr/bin/env python3
"""Panel Everything — 工作站 AI 用量 Reporter (ARCH-004 / TASK-031 / TASK-032).

无状态单文件脚本，由工作站 cron 每 5 分钟触发：
  读本地 AI 数据源 → 构造 payload → POST 到面板 ``/api/ingest/ai-usage``。

依赖：标准库 + httpx。httpx 工作站通常已装；若无：``pip install httpx``。
无需安装本项目包，可直接 ``python3 reporter.py`` 运行。

配置来源（优先级）：
  1. 进程环境变量
  2. ``~/.config/panel-reporter/env``（``KEY=VALUE`` 每行，支持 ``#`` 注释）

退出码：
  0 — 至少一个 source 成功上报
  1 — 所有 source 均失败（便于 cron 邮件告警）
  2 — 配置缺失（PANEL_URL 未设置）
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

# 允许 `python3 tools/reporter/reporter.py` 直接运行：把本目录加入 sys.path，
# 以便 `from sources import ...` 在任意 cwd 下都能导入。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from sources import ChatGptSource, ClaudeCodeSource, CodexSource  # noqa: E402

logger = logging.getLogger("reporter")

DEFAULT_ENV_FILE = "~/.config/panel-reporter/env"
INGEST_PATH = "/api/ingest/ai-usage"
POST_TIMEOUT = 10


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _parse_env_file(path: str) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 配置文件。忽略空行与 ``#`` 注释，去除值两端引号。"""
    result: dict[str, str] = {}
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        return result
    try:
        with open(expanded, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    result[key] = value
    except OSError as e:
        logger.warning("无法读取配置文件 %s: %s", path, e)
    return result


def load_env() -> dict[str, str]:
    """先读配置文件，再用进程环境变量覆盖（环境变量优先级更高）。"""
    config = _parse_env_file(os.environ.get("REPORTER_ENV_FILE", DEFAULT_ENV_FILE))
    # 已知配置键 + 任何已在环境中的同名键，由环境变量覆盖。
    known_keys = (
        "PANEL_URL",
        "INGEST_TOKEN",
        "REPORTER_TOKEN",  # 兼容别名
        "CODEX_DIR",
        "CLAUDE_PROJECTS_DIR",
        "CLAUDE_LIMIT_TOKENS",
        "CLAUDE_SESSION_TOKEN",
        "CLAUDE_ORG_ID",
        "CHATGPT_JSON",
        "LOG_LEVEL",
    )
    for key in known_keys:
        if key in os.environ:
            config[key] = os.environ[key]
    return config


def _resolve_token(config: dict[str, str]) -> str:
    """INGEST_TOKEN 为主键，REPORTER_TOKEN 作兼容别名。"""
    return (config.get("INGEST_TOKEN") or config.get("REPORTER_TOKEN") or "").strip()


# --------------------------------------------------------------------------- #
# 上报
# --------------------------------------------------------------------------- #
def post_payload(panel_url: str, token: str, payload: dict[str, Any]) -> bool:
    """POST payload 到面板摄取端点。失败返回 False（不抛），打印 warning。"""
    try:
        import httpx
    except ImportError:
        logger.warning("未安装 httpx，无法上报。请 `pip install httpx`")
        return False

    url = panel_url.rstrip("/") + INGEST_PATH
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=POST_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — 网络任何异常都不应让 reporter 崩
        logger.warning("POST %s 失败: %s", url, e)
        return False

    if resp.status_code != 200:
        body = resp.text[:200] if resp.text else ""
        logger.warning("POST %s 返回 %s: %s", url, resp.status_code, body)
        return False
    return True


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_sources(config: dict[str, str]) -> list:
    """构造启用的 source 列表。TASK-031: Codex；TASK-032: + Claude Code；+ ChatGPT 降级。"""
    return [
        CodexSource(config),
        ClaudeCodeSource(config),
        ChatGptSource(config),
    ]


def main() -> int:
    config = load_env()

    logging.basicConfig(
        level=getattr(logging, config.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    panel_url = (config.get("PANEL_URL") or "").strip()
    if not panel_url:
        logger.error("PANEL_URL 未配置（环境变量或 %s）", DEFAULT_ENV_FILE)
        return 2

    token = _resolve_token(config)
    sources = build_sources(config)

    any_success = False
    for source in sources:
        try:
            payload = source.collect()
        except Exception as e:  # noqa: BLE001 — 单 source 失败不影响其他
            logger.warning("%s: collect 异常: %s", source.name, e)
            continue
        if not payload:
            logger.info("%s: 无数据可上报", source.name)
            continue
        ok = post_payload(panel_url, token, payload)
        logger.info("%s: stored=%s", source.name, ok)
        any_success = any_success or ok

    return 0 if any_success else 1


if __name__ == "__main__":
    sys.exit(main())
