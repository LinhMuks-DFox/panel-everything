"""幂等建表 / 迁移 (ARCH-001 / TASK-002).

读取同目录 schema.sql 并执行。schema.sql 全部使用 IF NOT EXISTS,因此 run() 可
重复调用而无副作用。在 lifespan 中 connect 后立即调用一次。

> 后续模块(ARCH-002 GPU 专用表等)通过向 schema.sql 追加 IF NOT EXISTS 片段,
> 或新增独立 schema 片段并在此扩展 run() 来接入。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _read_schema() -> str:
    """读取基线 schema.sql 文本。"""
    return _SCHEMA_PATH.read_text(encoding="utf-8")


async def run(conn: aiosqlite.Connection) -> None:
    """执行基线 schema(幂等)。

    使用 executescript 一次性建表 + 索引,随后 commit。可重复调用。
    """
    await conn.executescript(_read_schema())
    await conn.commit()
