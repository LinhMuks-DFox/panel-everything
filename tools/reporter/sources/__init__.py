"""Reporter 数据源解析器集合 (ARCH-004).

每个 source 暴露一个类，具备：
  * 类属性 ``name``：provider 名（与面板 ai_provider 表一致：
    ``codex`` / ``claude_code`` / ``chatgpt``）。
  * ``__init__(self, config: dict)``：从配置字典构造（不抛异常）。
  * ``collect(self) -> dict | None``：读取本地数据源，返回符合
    ``POST /api/ingest/ai-usage`` 契约的 payload 字典；数据缺失/解析失败时
    返回 ``None``（不抛异常，仅记录日志）。

各 source 防御性解析：字段缺失或 schema 变化不应导致崩溃。
"""

from __future__ import annotations

from .chatgpt import ChatGptSource
from .claude_code import ClaudeCodeSource
from .codex import CodexSource

__all__ = ["ChatGptSource", "ClaudeCodeSource", "CodexSource"]
