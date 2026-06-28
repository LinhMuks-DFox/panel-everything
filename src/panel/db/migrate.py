"""幂等建表 / 迁移 (ARCH-001 / TASK-002).

读取同目录 schema.sql 并执行。schema.sql 全部使用 IF NOT EXISTS,因此 run() 可
重复调用而无副作用。在 lifespan 中 connect 后立即调用一次。

扩展机制:模块专用表 DDL 放入 `migrations/` 子目录,文件名按升序执行。
每个迁移文件须全部使用 IF NOT EXISTS,保证幂等。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _read_schema() -> str:
    """读取基线 schema.sql 文本。"""
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def _read_migrations() -> list[str]:
    """按文件名升序读取 migrations/ 目录下所有 .sql 文件。

    目录不存在时返回空列表(向后兼容)。
    """
    if not _MIGRATIONS_DIR.is_dir():
        return []
    sql_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    return [f.read_text(encoding="utf-8") for f in sql_files]


async def run(conn: aiosqlite.Connection) -> None:
    """执行基线 schema + migrations/ 下所有迁移文件(幂等)。

    执行顺序:schema.sql 先行,随后按文件名升序执行 migrations/*.sql。
    使用 executescript 一次性建表 + 索引,随后 commit。可重复调用。
    """
    await conn.executescript(_read_schema())
    for migration_sql in _read_migrations():
        await conn.executescript(migration_sql)
    await conn.commit()
