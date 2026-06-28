"""Codex 数据源 (ARCH-004 / TASK-031).

Codex 把会话写入本地 rollout jsonl 文件。每个会话文件里若干行的
``payload`` 携带 ``rate_limits`` 字段，本 source 取**最近修改文件中最后一条
含 rate_limits 的行**，构造 AI 用量 payload。

实地探查结论（2026-06，codex CLI 当前 schema）
------------------------------------------------
真实目录结构 **不是** 文档假设的 ``~/.codex/<workspace>/logs/*.jsonl``，
而是按日期分层的会话 rollout 文件::

    ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl

每行形如::

    {"timestamp": "...", "type": "event_msg",
     "payload": {"type": "token_count",
                 "info": {...},
                 "rate_limits": {
                     "primary":   {"used_percent": 46.0, "window_minutes": 300,   "resets_at": 1781686515},
                     "secondary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": 1782106461},
                     ...}}}

注意与 ARCH-004 文档假设的差异（已在本实现中两路兼容）：
  * 字段是 ``primary`` / ``secondary`` 桶，**不是** ``requests`` / ``tokens``。
  * 桶里直接给 ``used_percent``（百分比），**没有** ``used`` / ``limit`` 原始计数。
  * ``resets_at`` 是 **Unix epoch 秒**（整数），**不是** ISO8601 字符串。
  * ``window_minutes`` 给出窗口（primary=300=5h）。

兼容策略
--------
``_extract_metrics`` 同时支持两种 schema：
  1. **真实 schema**（primary/secondary + used_percent + epoch resets_at）— 主路径。
  2. **文档 schema**（requests.used / .limit / .reset_at）— 回退，便于旧版本/mock。
两种都拿不到则记 warning 返回 None。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("reporter.codex")

# 5h 滑动窗口（秒）；codex primary 桶 window_minutes=300 与此一致。
DEFAULT_WINDOW_SECONDS = 18000


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _epoch_to_iso(value: object) -> str | None:
    """把 Unix epoch 秒（int/float/数字字符串）转 ISO8601 UTC 字符串。

    已是字符串且非纯数字 → 视为已是 ISO8601，原样返回。无法解析 → None。
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # 纯数字字符串 → 当 epoch；否则当成已格式化的时间串原样返回。
        try:
            num = float(s)
        except ValueError:
            return s
        value = num
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _round1(x: float) -> float:
    return round(x, 1)


class CodexSource:
    """Codex 本地 rollout jsonl 解析。"""

    name: str = "codex"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.codex_dir = _expand(config.get("CODEX_DIR") or "~/.codex")
        # 会话文件根目录。新版 codex 用 sessions/ 子目录；保留 codex_dir 作回退根。
        self.sessions_dir = os.path.join(self.codex_dir, "sessions")

    # ------------------------------------------------------------------ #
    # 文件发现
    # ------------------------------------------------------------------ #
    def _candidate_files(self) -> list[str]:
        """收集候选 jsonl 文件，按 mtime 降序。

        优先扫 ``sessions/`` 下递归的 ``rollout-*.jsonl``；
        若 sessions 不存在，回退扫 codex_dir 下直接子目录的 ``logs/*.jsonl``
        （文档假设的旧布局）。
        """
        files: list[str] = []
        root = self.sessions_dir if os.path.isdir(self.sessions_dir) else None
        if root:
            for dirpath, _dirs, names in os.walk(root):
                for n in names:
                    if n.endswith(".jsonl"):
                        files.append(os.path.join(dirpath, n))
        else:
            # 文档假设的旧布局：~/.codex/<workspace>/logs/*.jsonl
            if os.path.isdir(self.codex_dir):
                for entry in os.listdir(self.codex_dir):
                    logs = os.path.join(self.codex_dir, entry, "logs")
                    if os.path.isdir(logs):
                        for n in os.listdir(logs):
                            if n.endswith(".jsonl"):
                                files.append(os.path.join(logs, n))

        def _mtime(p: str) -> float:
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0.0

        files.sort(key=_mtime, reverse=True)
        return files

    @staticmethod
    def _find_rate_limits(obj: object) -> dict | None:
        """在任意嵌套结构里深度优先找第一个 ``rate_limits`` 字典。"""
        if isinstance(obj, dict):
            rl = obj.get("rate_limits")
            if isinstance(rl, dict):
                return rl
            for v in obj.values():
                found = CodexSource._find_rate_limits(v)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = CodexSource._find_rate_limits(v)
                if found is not None:
                    return found
        return None

    def _latest_rate_limits(self) -> tuple[dict, str] | None:
        """遍历候选文件，返回 (rate_limits 字典, 文件路径)。

        从最近修改的文件开始，逐文件从**末尾向前**扫，找到第一个含
        rate_limits 的行即返回（最新一条）。
        """
        for path in self._candidate_files():
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError as e:
                logger.debug("codex: 无法读取 %s: %s", path, e)
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line or "rate_limits" not in line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rl = self._find_rate_limits(data)
                if rl:
                    return rl, path
        return None

    # ------------------------------------------------------------------ #
    # 指标提取
    # ------------------------------------------------------------------ #
    def _extract_metrics(self, rl: dict) -> tuple[list[dict], str]:
        """从 rate_limits 字典构造 metrics 列表，返回 (metrics, status)。

        优先真实 schema（primary/secondary 桶）；回退文档 schema（requests）。
        """
        metrics: list[dict] = []
        status = "ok"

        primary = rl.get("primary") if isinstance(rl.get("primary"), dict) else None
        secondary = (
            rl.get("secondary") if isinstance(rl.get("secondary"), dict) else None
        )

        if primary is not None:
            # ---- 真实 schema：直接拿百分比 ----
            used_percent = primary.get("used_percent")
            window_minutes = primary.get("window_minutes")
            resets_at = _epoch_to_iso(primary.get("resets_at"))

            if isinstance(used_percent, (int, float)):
                metrics.append(
                    {"metric": "used_percent", "value_num": _round1(float(used_percent)), "value_text": None}
                )
            else:
                status = "error"

            if resets_at:
                metrics.append({"metric": "resets_at", "value_num": None, "value_text": resets_at})

            window_seconds = (
                int(window_minutes) * 60
                if isinstance(window_minutes, (int, float)) and window_minutes
                else DEFAULT_WINDOW_SECONDS
            )
            metrics.append(
                {"metric": "window_seconds", "value_num": float(window_seconds), "value_text": None}
            )

            # 次级窗口（周限额）作为附加文本指标，便于面板侧可选展示。
            if secondary is not None:
                sec_pct = secondary.get("used_percent")
                if isinstance(sec_pct, (int, float)):
                    metrics.append(
                        {
                            "metric": "secondary_used_percent",
                            "value_num": _round1(float(sec_pct)),
                            "value_text": None,
                        }
                    )
                sec_reset = _epoch_to_iso(secondary.get("resets_at"))
                if sec_reset:
                    metrics.append(
                        {"metric": "secondary_resets_at", "value_num": None, "value_text": sec_reset}
                    )
            return metrics, status

        # ---- 文档 schema 回退：requests.used / .limit / .reset_at ----
        requests = rl.get("requests") if isinstance(rl.get("requests"), dict) else None
        if requests is not None:
            used = requests.get("used")
            limit = requests.get("limit")
            reset_at = requests.get("reset_at")

            if isinstance(used, (int, float)):
                metrics.append({"metric": "used_requests", "value_num": float(used), "value_text": None})
            if isinstance(limit, (int, float)):
                metrics.append({"metric": "limit_requests", "value_num": float(limit), "value_text": None})

            # limit 为 0 / None → used_percent 不可计算，status=error
            if (
                isinstance(used, (int, float))
                and isinstance(limit, (int, float))
                and limit
            ):
                metrics.append(
                    {"metric": "used_percent", "value_num": _round1(used / limit * 100), "value_text": None}
                )
            else:
                status = "error"

            reset_iso = _epoch_to_iso(reset_at)
            if reset_iso:
                metrics.append({"metric": "resets_at", "value_num": None, "value_text": reset_iso})

            # tokens 桶可选
            tokens = rl.get("tokens") if isinstance(rl.get("tokens"), dict) else None
            if tokens is not None:
                t_used = tokens.get("used")
                t_limit = tokens.get("limit")
                if isinstance(t_used, (int, float)):
                    metrics.append(
                        {"metric": "used_tokens", "value_num": float(t_used), "value_text": None}
                    )
                if isinstance(t_limit, (int, float)):
                    metrics.append(
                        {"metric": "limit_tokens", "value_num": float(t_limit), "value_text": None}
                    )

            metrics.append(
                {"metric": "window_seconds", "value_num": float(DEFAULT_WINDOW_SECONDS), "value_text": None}
            )
            return metrics, status

        # 两种 schema 都没命中
        return [], "error"

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #
    def collect(self) -> dict | None:
        if not os.path.isdir(self.codex_dir):
            logger.warning("codex: 目录不存在: %s", self.codex_dir)
            return None

        found = self._latest_rate_limits()
        if found is None:
            logger.warning("codex: 未在任何 jsonl 中找到 rate_limits 字段 (dir=%s)", self.codex_dir)
            return None
        rl, path = found
        logger.debug("codex: 检测到 rate_limits 字段: %s (来自 %s)", sorted(rl.keys()), path)

        metrics, status = self._extract_metrics(rl)
        if not metrics:
            logger.warning("codex: rate_limits 结构无法识别，字段=%s", sorted(rl.keys()))
            return None

        # extra：调试用，记录数据来源
        extra = {
            "codex_dir": self.codex_dir,
            "source_file": os.path.relpath(path, self.codex_dir),
            "data_source": "local_jsonl",
        }
        metrics.append({"metric": "extra", "value_num": None, "value_text": json.dumps(extra)})

        # 结构化日志：尽量打印 used_percent / resets_at 摘要
        pct = next((m["value_num"] for m in metrics if m["metric"] == "used_percent"), None)
        resets = next((m["value_text"] for m in metrics if m["metric"] == "resets_at"), None)
        logger.info("codex: used_percent=%s resets_at=%s status=%s", pct, resets, status)

        return {
            "reporter_version": "1.0",
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "provider": self.name,
            "status": status,
            "metrics": metrics,
        }
