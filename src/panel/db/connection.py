"""aiosqlite 连接管理 (ARCH-001 / TASK-002).

单连接长生命周期(随 app lifespan),由 app.state.db 持有。connect() 打开连接并
设置 WAL 等 PRAGMA,row_factory 设为 aiosqlite.Row。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite


async def connect(db_path: str) -> aiosqlite.Connection:
    """打开并配置一个 aiosqlite 连接。

    设置:
        PRAGMA journal_mode=WAL;       -- 提升并发读
        PRAGMA synchronous=NORMAL;     -- WAL 下安全且更快
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;      -- 锁等待 5s
        conn.row_factory = aiosqlite.Row

    确保 db_path 所在目录存在(:memory: 等特殊路径除外)。

    Args:
        db_path: SQLite 文件路径(取自 Settings.db_path)。

    Returns:
        已配置好的 aiosqlite.Connection(调用方负责 close)。
    """
    # 文件型路径:确保父目录存在;:memory: / file::memory: 等不创建目录。
    if db_path != ":memory:" and not db_path.startswith("file:"):
        parent = Path(db_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    return conn
