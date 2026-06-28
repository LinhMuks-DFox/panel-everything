"""Repository 薄 SQL 层 (ARCH-001 / TASK-002).

封装通用三表(latest_snapshot / metric_history / collector_run)的读写。所有后续
模块的数据读写都走这一层契约。方法签名为权威契约,不得更改。

时间约定:库内统一存 ISO8601 UTC 字符串。MetricSample/CollectorResult 的 datetime
写库前经 _iso() 归一化为 UTC,读出的时间经 _parse_utc() 还原为带 tz 的 datetime。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from panel.collectors.base import CollectorResult, MetricSample

# --------------------------------------------------------------------------- #
# 行类型(轻量 dataclass,slots=True)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SnapshotRow:
    collector: str
    target_id: int
    metric: str
    value_num: float | None
    value_text: str | None
    status: str
    collected_at: str
    updated_at: str


@dataclass(slots=True)
class HistoryRow:
    collector: str
    target_id: int
    metric: str
    value_num: float | None
    value_text: str | None
    status: str
    collected_at: str


@dataclass(slots=True)
class CollectorRunRow:
    collector: str
    status: str
    sample_count: int
    duration_ms: int
    error: str | None
    ran_at: str


# --------------------------------------------------------------------------- #
# 时间归一化
# --------------------------------------------------------------------------- #


def _iso(dt: datetime) -> str:
    """datetime -> ISO8601 UTC 字符串。

    naive datetime 视为 UTC;带 tz 的转换到 UTC 后输出。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _now_iso() -> str:
    """当前时刻的 ISO8601 UTC 字符串(用于 updated_at)。"""
    return datetime.now(UTC).isoformat()


def _parse_utc(value: str) -> datetime:
    """ISO8601 字符串 -> 带 tz 的 datetime(UTC)。

    无 tz 信息的字符串视为 UTC。
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


class Repository:
    """通用三表的薄 SQL 访问层。

    单连接(随 app lifespan)注入;写方法各自一次 commit。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ---------------------------------------------------------------- 写 ---- #

    async def upsert_snapshot(self, collector: str, samples: list[MetricSample]) -> None:
        """对每个 sample 按 (collector,target_id,metric) UPSERT 最新快照。

        更新 value_num/value_text/status/collected_at/updated_at(updated_at=now)。
        """
        if not samples:
            return
        now = _now_iso()
        rows = [
            (
                collector,
                s.target_id,
                s.metric,
                s.value_num,
                s.value_text,
                s.status,
                _iso(s.collected_at),
                now,
            )
            for s in samples
        ]
        await self._conn.executemany(
            """
            INSERT INTO latest_snapshot
                (collector, target_id, metric, value_num, value_text,
                 status, collected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(collector, target_id, metric) DO UPDATE SET
                value_num    = excluded.value_num,
                value_text   = excluded.value_text,
                status       = excluded.status,
                collected_at = excluded.collected_at,
                updated_at   = excluded.updated_at
            """,
            rows,
        )
        await self._conn.commit()

    async def append_history(self, collector: str, samples: list[MetricSample]) -> None:
        """对每个 sample 追加一行 metric_history(append-only)。"""
        if not samples:
            return
        rows = [
            (
                collector,
                s.target_id,
                s.metric,
                s.value_num,
                s.value_text,
                s.status,
                _iso(s.collected_at),
            )
            for s in samples
        ]
        await self._conn.executemany(
            """
            INSERT INTO metric_history
                (collector, target_id, metric, value_num, value_text,
                 status, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self._conn.commit()

    async def record_collector_run(self, result: CollectorResult) -> None:
        """追加一行 collector_run。result.error 须已脱敏(由调用方负责)。"""
        await self._conn.execute(
            """
            INSERT INTO collector_run
                (collector, status, sample_count, duration_ms, error, ran_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                result.name,
                result.status,
                result.sample_count,
                result.duration_ms,
                result.error,
                _iso(result.ran_at),
            ),
        )
        await self._conn.commit()

    # ---------------------------------------------------------------- 读 ---- #

    async def get_snapshot(self, collector: str) -> list[SnapshotRow]:
        """返回某 collector 的全部最新快照行。"""
        async with self._conn.execute(
            """
            SELECT collector, target_id, metric, value_num, value_text,
                   status, collected_at, updated_at
            FROM latest_snapshot
            WHERE collector = ?
            ORDER BY target_id, metric
            """,
            (collector,),
        ) as cur:
            return [self._to_snapshot(r) async for r in cur]

    async def get_snapshot_metric(
        self, collector: str, target_id: int, metric: str
    ) -> SnapshotRow | None:
        """返回某 (collector,target_id,metric) 的最新快照行,无则 None。"""
        async with self._conn.execute(
            """
            SELECT collector, target_id, metric, value_num, value_text,
                   status, collected_at, updated_at
            FROM latest_snapshot
            WHERE collector = ? AND target_id = ? AND metric = ?
            """,
            (collector, target_id, metric),
        ) as cur:
            row = await cur.fetchone()
        return self._to_snapshot(row) if row is not None else None

    async def get_history(
        self,
        collector: str,
        target_id: int,
        metric: str,
        since: datetime,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[HistoryRow]:
        """按时间范围查历史时序,collected_at 升序。

        范围为 [since, until](until 为 None 时不设上界)。limit 限制返回行数;
        取最近 limit 条但仍按时间升序呈现。
        """
        params: list[object] = [collector, target_id, metric, _iso(since)]
        upper = ""
        if until is not None:
            upper = " AND collected_at <= ?"
            params.append(_iso(until))
        params.append(limit)
        # 先按时间降序取最近 limit 条,再外层升序排列,保证范围内裁剪到最新数据。
        async with self._conn.execute(
            f"""
            SELECT collector, target_id, metric, value_num, value_text,
                   status, collected_at
            FROM (
                SELECT collector, target_id, metric, value_num, value_text,
                       status, collected_at
                FROM metric_history
                WHERE collector = ? AND target_id = ? AND metric = ?
                      AND collected_at >= ?{upper}
                ORDER BY collected_at DESC
                LIMIT ?
            )
            ORDER BY collected_at ASC
            """,  # noqa: S608 (upper 仅为静态片段,无外部输入拼接)
            params,
        ) as cur:
            return [self._to_history(r) async for r in cur]

    async def get_last_success(self, collector: str) -> datetime | None:
        """某 collector 最近一次 status='up' 的 ran_at(UTC datetime);无则 None。"""
        async with self._conn.execute(
            """
            SELECT ran_at
            FROM collector_run
            WHERE collector = ? AND status = 'up'
            ORDER BY ran_at DESC
            LIMIT 1
            """,
            (collector,),
        ) as cur:
            row = await cur.fetchone()
        return _parse_utc(row["ran_at"]) if row is not None else None

    async def get_all_last_runs(self) -> list[CollectorRunRow]:
        """每个 collector 的最近一次运行(任意 status),供数据源状态条渲染。"""
        async with self._conn.execute(
            """
            SELECT cr.collector, cr.status, cr.sample_count, cr.duration_ms,
                   cr.error, cr.ran_at
            FROM collector_run AS cr
            JOIN (
                SELECT collector, MAX(id) AS max_id
                FROM collector_run
                GROUP BY collector
            ) AS latest
              ON cr.id = latest.max_id
            ORDER BY cr.collector
            """,
        ) as cur:
            return [self._to_run(r) async for r in cur]

    # ----------------------------------------------------------- mappers ---- #

    @staticmethod
    def _to_snapshot(row: aiosqlite.Row) -> SnapshotRow:
        return SnapshotRow(
            collector=row["collector"],
            target_id=row["target_id"],
            metric=row["metric"],
            value_num=row["value_num"],
            value_text=row["value_text"],
            status=row["status"],
            collected_at=row["collected_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_history(row: aiosqlite.Row) -> HistoryRow:
        return HistoryRow(
            collector=row["collector"],
            target_id=row["target_id"],
            metric=row["metric"],
            value_num=row["value_num"],
            value_text=row["value_text"],
            status=row["status"],
            collected_at=row["collected_at"],
        )

    @staticmethod
    def _to_run(row: aiosqlite.Row) -> CollectorRunRow:
        return CollectorRunRow(
            collector=row["collector"],
            status=row["status"],
            sample_count=row["sample_count"],
            duration_ms=row["duration_ms"],
            error=row["error"],
            ran_at=row["ran_at"],
        )
