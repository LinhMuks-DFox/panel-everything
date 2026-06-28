"""Claude Code 数据源 (ARCH-004 / TASK-032).

主路径：读工作站本地 ``~/.claude/projects/<hash>/*.jsonl``，用 5h 滑动窗口
累计 token（社区 ccusage 思路）。可选回退：尝试 Claude OAuth usage 端点
（未官方文档化，失败静默降级）。

实地探查结论（2026-06，claude code 当前 schema）
------------------------------------------------
``~/.claude/projects/`` 已确认存在。每行一个事件，**usage 字段嵌在
``message.usage`` 下**（不是文档假设的顶层 ``usage``）::

    {"type": "assistant",
     "timestamp": "2026-06-28T11:50:34.204Z",
     "message": {"usage": {
         "input_tokens": 5588,
         "output_tokens": 628,
         "cache_creation_input_tokens": 22580,
         "cache_read_input_tokens": 0,
         ...}}}

本实现两路兼容：先看 ``message.usage``，再看顶层 ``usage``。
按 ccusage 思路，5h 窗口内累计 ``input_tokens + output_tokens +
cache_creation_input_tokens``（cache_read 通常不计费窗口，故不计入）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("reporter.claude_code")

WINDOW_SECONDS = 18000  # 5h
# 多读 1h 缓冲，避免文件 mtime 边界遗漏。
FILE_MTIME_BUFFER_SECONDS = WINDOW_SECONDS + 3600

# OAuth usage 端点（社区发现，实验性，可能随时变更/下线）。
_OAUTH_USAGE_URL = "https://claude.ai/api/organizations/{org_id}/usage"


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _parse_ts(ts: object) -> datetime | None:
    """解析 ISO8601 时间串（容忍 ``Z`` 后缀），返回 aware UTC datetime。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _mask_token(t: str) -> str:
    """凭证脱敏：仅保留前 8 位明文。"""
    if not t:
        return "****"
    return t[:8] + "****" if len(t) > 8 else "****"


def _to_int(v: object) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


class ClaudeCodeSource:
    """Claude Code 本地 jsonl 滑动窗口解析 + 可选 OAuth 回退。"""

    name: str = "claude_code"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.projects_dir = _expand(config.get("CLAUDE_PROJECTS_DIR") or "~/.claude/projects")

        limit_raw = (config.get("CLAUDE_LIMIT_TOKENS") or "").strip()
        self.limit_tokens: int | None = None
        if limit_raw:
            try:
                self.limit_tokens = int(limit_raw)
            except ValueError:
                logger.warning("claude_code: CLAUDE_LIMIT_TOKENS 非整数，忽略: %r", limit_raw)

        self.session_token = (config.get("CLAUDE_SESSION_TOKEN") or "").strip()
        self.org_id = (config.get("CLAUDE_ORG_ID") or "").strip()

        if not os.path.isdir(self.projects_dir):
            logger.info("claude_code: 数据目录不存在: %s（将返回 None）", self.projects_dir)

    # ------------------------------------------------------------------ #
    # 主路径：本地 jsonl 滑动窗口
    # ------------------------------------------------------------------ #
    def _recent_files(self, now: datetime) -> list[str]:
        """枚举 projects_dir 下所有 ``*.jsonl``，仅保留 mtime 在缓冲窗口内的。"""
        files: list[str] = []
        cutoff = now.timestamp() - FILE_MTIME_BUFFER_SECONDS
        for dirpath, _dirs, names in os.walk(self.projects_dir):
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    if os.path.getmtime(p) >= cutoff:
                        files.append(p)
                except OSError:
                    continue
        return files

    @staticmethod
    def _extract_usage(d: dict) -> dict | None:
        """从一行事件里取 usage 字典：先 message.usage，回退顶层 usage。"""
        msg = d.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
            return msg["usage"]
        if isinstance(d.get("usage"), dict):
            return d["usage"]
        return None

    def _collect_jsonl(self) -> dict | None:
        if not os.path.isdir(self.projects_dir):
            logger.warning("claude_code: 数据目录不存在: %s", self.projects_dir)
            return None

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=WINDOW_SECONDS)

        files = self._recent_files(now)
        used_tokens = 0
        line_count = 0
        project_count = 0
        earliest: datetime | None = None

        for path in files:
            counted_in_file = False
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError as e:
                logger.debug("claude_code: 无法读取 %s: %s", path, e)
                continue
            # 单文件从末尾向前扫；遇到早于窗口的有时间戳行即停止（行大致按时间追加）。
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dt = _parse_ts(d.get("timestamp"))
                if dt is None:
                    continue
                if dt < window_start:
                    break
                usage = self._extract_usage(d)
                if not usage:
                    continue
                used_tokens += (
                    _to_int(usage.get("input_tokens"))
                    + _to_int(usage.get("output_tokens"))
                    + _to_int(usage.get("cache_creation_input_tokens"))
                )
                line_count += 1
                counted_in_file = True
                if earliest is None or dt < earliest:
                    earliest = dt
            if counted_in_file:
                project_count += 1

        if line_count == 0:
            logger.warning("claude_code: 5h 窗口内无 usage 记录 (dir=%s)", self.projects_dir)
            return None

        # resets_at：最早一条记录 + 5h；无则 now + 5h。
        reset_base = earliest if earliest is not None else now
        resets_at = (reset_base + timedelta(seconds=WINDOW_SECONDS)).isoformat()

        used_percent: float | None = None
        status = "ok"
        if self.limit_tokens:
            used_percent = round(used_tokens / self.limit_tokens * 100, 1)

        metrics: list[dict] = [
            {"metric": "used_tokens", "value_num": float(used_tokens), "value_text": None},
            {
                "metric": "limit_tokens",
                "value_num": float(self.limit_tokens) if self.limit_tokens else None,
                "value_text": None,
            },
            {"metric": "used_percent", "value_num": used_percent, "value_text": None},
            {"metric": "resets_at", "value_num": None, "value_text": resets_at},
            {"metric": "window_seconds", "value_num": float(WINDOW_SECONDS), "value_text": None},
            {
                "metric": "extra",
                "value_num": None,
                "value_text": json.dumps(
                    {
                        "data_source": "local_jsonl",
                        "project_count": project_count,
                        "line_count": line_count,
                    }
                ),
            },
        ]
        logger.info(
            "claude_code: used_tokens=%d limit=%s (%.1f%%) lines=%d resets_at=%s",
            used_tokens,
            self.limit_tokens,
            used_percent if used_percent is not None else -1.0,
            line_count,
            resets_at,
        )
        return {
            "reporter_version": "1.0",
            "reported_at": now.isoformat(),
            "provider": self.name,
            "status": status,
            "metrics": metrics,
        }

    # ------------------------------------------------------------------ #
    # 可选回退路径：OAuth usage 端点
    # ------------------------------------------------------------------ #
    def _collect_oauth(self) -> dict | None:
        """尝试 OAuth usage 端点。失败/未配置 → None（debug 级日志，不噪音）。

        仅返回一个 ``{"used_tokens": int}`` 形式的轻量结果供 collect() 合并；
        端点 schema 未文档化，这里只做最保守的字段嗅探。
        """
        if not self.session_token:
            logger.debug("claude_code: 未配置 CLAUDE_SESSION_TOKEN，跳过 OAuth")
            return None
        if not self.org_id:
            logger.debug("claude_code: 未配置 CLAUDE_ORG_ID，跳过 OAuth")
            return None

        try:
            import httpx
        except ImportError:
            logger.debug("claude_code: 未安装 httpx，跳过 OAuth")
            return None

        url = _OAUTH_USAGE_URL.format(org_id=self.org_id)
        logger.debug("claude_code: 尝试 OAuth usage 端点 token=%s", _mask_token(self.session_token))
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"sessionKey {self.session_token}"},
                timeout=5,
            )
        except Exception as e:  # noqa: BLE001 — 网络层任何异常都静默降级
            logger.debug("claude_code: OAuth 请求失败，静默降级: %s", e)
            return None

        if resp.status_code != 200:
            logger.debug("claude_code: OAuth 非 200 (%s)，静默降级", resp.status_code)
            return None

        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.debug("claude_code: OAuth 响应非 JSON，静默降级: %s", e)
            return None

        # 端点 schema 未知：保守嗅探 used_tokens / used 字段。
        used = None
        if isinstance(data, dict):
            for key in ("used_tokens", "used", "tokens_used"):
                if isinstance(data.get(key), (int, float)):
                    used = int(data[key])
                    break
        if used is None:
            logger.debug("claude_code: OAuth 响应未含已知 used 字段，忽略")
            return None
        logger.debug("claude_code: OAuth used_tokens=%d", used)
        return {"used_tokens": used}

    # ------------------------------------------------------------------ #
    # 组合
    # ------------------------------------------------------------------ #
    def collect(self) -> dict | None:
        payload = self._collect_jsonl()

        if not self.session_token:
            return payload

        oauth = self._collect_oauth()
        if oauth is None:
            return payload

        oauth_used = oauth.get("used_tokens")
        if not isinstance(oauth_used, (int, float)):
            return payload

        # OAuth 成功但本地无数据 → 单独构造一个最小 payload。
        if payload is None:
            now = datetime.now(timezone.utc)
            used_percent = (
                round(oauth_used / self.limit_tokens * 100, 1) if self.limit_tokens else None
            )
            return {
                "reporter_version": "1.0",
                "reported_at": now.isoformat(),
                "provider": self.name,
                "status": "ok",
                "metrics": [
                    {"metric": "used_tokens", "value_num": float(oauth_used), "value_text": None},
                    {
                        "metric": "limit_tokens",
                        "value_num": float(self.limit_tokens) if self.limit_tokens else None,
                        "value_text": None,
                    },
                    {"metric": "used_percent", "value_num": used_percent, "value_text": None},
                    {
                        "metric": "extra",
                        "value_num": None,
                        "value_text": json.dumps({"data_source": "oauth_api"}),
                    },
                ],
            }

        # 本地 + OAuth 都有 → used_tokens 取最大（OAuth 可能更准），重算 used_percent。
        merged = False
        for m in payload["metrics"]:
            if m["metric"] == "used_tokens":
                if oauth_used > (m["value_num"] or 0):
                    m["value_num"] = float(oauth_used)
                    merged = True
                break
        if merged and self.limit_tokens:
            new_pct = round(oauth_used / self.limit_tokens * 100, 1)
            for m in payload["metrics"]:
                if m["metric"] == "used_percent":
                    m["value_num"] = new_pct
                    break
        return payload
