"""ChatGPT 数据源 (ARCH-004，降级方案).

OpenAI 无公开个人账号额度查询 API，且无本地数据文件，故降级为**手动输入**：
用户在工作站维护 ``~/.panel_reporter/chatgpt.json``，Reporter 读取并上报。
面板前端对此来源显示「手动更新」徽标。

期望文件格式（ARCH-004 数据模型节）::

    {
      "used_messages": 30,
      "limit_messages": 80,
      "resets_at": "2026-06-28T20:00:00Z",
      "window_seconds": 10800,
      "updated_manually_at": "2026-06-28T09:00:00Z"
    }

文件不存在 / 非法 JSON / 字段缺失 → 返回 None（不抛）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("reporter.chatgpt")

DEFAULT_WINDOW_SECONDS = 10800  # ChatGPT 常见 3h 窗口


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


class ChatGptSource:
    """ChatGPT 手动输入读取。"""

    name: str = "chatgpt"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.json_path = _expand(
            config.get("CHATGPT_JSON") or "~/.panel_reporter/chatgpt.json"
        )

    def collect(self) -> dict | None:
        if not os.path.isfile(self.json_path):
            logger.info("chatgpt: 手动输入文件不存在: %s（跳过）", self.json_path)
            return None

        try:
            with open(self.json_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("chatgpt: 读取/解析失败 %s: %s", self.json_path, e)
            return None

        if not isinstance(data, dict):
            logger.warning("chatgpt: 文件内容不是 JSON 对象: %s", self.json_path)
            return None

        used = data.get("used_messages")
        limit = data.get("limit_messages")
        resets_at = data.get("resets_at")
        window_seconds = data.get("window_seconds")
        updated_at = data.get("updated_manually_at")

        metrics: list[dict] = []
        status = "ok"

        if isinstance(used, (int, float)):
            metrics.append({"metric": "used_requests", "value_num": float(used), "value_text": None})
        if isinstance(limit, (int, float)):
            metrics.append({"metric": "limit_requests", "value_num": float(limit), "value_text": None})

        if (
            isinstance(used, (int, float))
            and isinstance(limit, (int, float))
            and limit
        ):
            metrics.append(
                {"metric": "used_percent", "value_num": round(used / limit * 100, 1), "value_text": None}
            )
        else:
            status = "error"

        if isinstance(resets_at, str) and resets_at:
            metrics.append({"metric": "resets_at", "value_num": None, "value_text": resets_at})

        ws = window_seconds if isinstance(window_seconds, (int, float)) and window_seconds else DEFAULT_WINDOW_SECONDS
        metrics.append({"metric": "window_seconds", "value_num": float(ws), "value_text": None})

        extra = {"data_source": "manual"}
        if isinstance(updated_at, str):
            extra["updated_manually_at"] = updated_at
        metrics.append({"metric": "extra", "value_num": None, "value_text": json.dumps(extra)})

        if not any(m["metric"] in ("used_requests", "used_percent") for m in metrics):
            logger.warning("chatgpt: 文件缺少 used_messages 字段，跳过: %s", self.json_path)
            return None

        logger.info(
            "chatgpt: used=%s/%s status=%s (手动输入, updated=%s)",
            used,
            limit,
            status,
            updated_at,
        )
        return {
            "reporter_version": "1.0",
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "provider": self.name,
            "status": status,
            "metrics": metrics,
        }
