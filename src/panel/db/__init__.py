"""持久化层:aiosqlite(WAL) 连接、通用 schema、repository 薄层 (TASK-002)。

公开接口:
    from panel.db import connection, migrate
    from panel.db.repository import Repository, SnapshotRow, HistoryRow, CollectorRunRow
"""

from __future__ import annotations

from panel.db import connection, migrate
from panel.db.repository import (
    CollectorRunRow,
    HistoryRow,
    Repository,
    SnapshotRow,
)

__all__ = [
    "CollectorRunRow",
    "HistoryRow",
    "Repository",
    "SnapshotRow",
    "connection",
    "migrate",
]
