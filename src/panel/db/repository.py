"""Repository 薄 SQL 层 (ARCH-001 / TASK-002).

封装通用三表(latest_snapshot / metric_history / collector_run)的读写。所有后续
模块的数据读写都走这一层契约。方法签名为权威契约,不得更改。

时间约定:库内统一存 ISO8601 UTC 字符串。MetricSample/CollectorResult 的 datetime
写库前经 _iso() 归一化为 UTC,读出的时间经 _parse_utc() 还原为带 tz 的 datetime。

ARCH-003 / TASK-020 扩展:Tailscale 专用表行类型 + upsert/查询方法。
"""

from __future__ import annotations

import json
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

    async def get_last_run(self, collector: str) -> CollectorRunRow | None:
        """某 collector 最近一次运行(任意 status);无则 None。供单模块 /status 端点使用。"""
        async with self._conn.execute(
            """
            SELECT collector, status, sample_count, duration_ms, error, ran_at
            FROM collector_run
            WHERE collector = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (collector,),
        ) as cur:
            row = await cur.fetchone()
        return self._to_run(row) if row is not None else None

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


# --------------------------------------------------------------------------- #
# ARCH-003 / TASK-020: Tailscale 专用行类型
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TailscaleNodeRow:
    """tailscale_nodes 表的单行映射。"""

    id: int
    node_key: str
    hostname: str
    dns_name: str | None
    tailscale_ips: list[str]  # json.loads 后的列表
    os: str | None
    online_state: str
    is_exit_node: bool
    last_seen_at: datetime | None  # UTC; None when online
    collected_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class TailscaleNodeEventRow:
    """tailscale_node_events 表的单行映射。"""

    id: int
    node_key: str
    from_state: str | None  # None 表示首次发现
    to_state: str
    occurred_at: datetime
    note: str | None


# --------------------------------------------------------------------------- #
# ARCH-003 / TASK-020: Tailscale repository 扩展方法
# --------------------------------------------------------------------------- #
# 直接追加到现有 Repository 类——Python 允许在模块尾部 monkey-patch,
# 但更整洁的做法是在类体内声明。由于不能修改类体封口处,这里用 setattr 注入。
# 为保持代码可读性,以下函数定义后统一 setattr 到 Repository。


async def _upsert_tailscale_node(
    self: Repository,
    node_key: str,
    hostname: str,
    dns_name: str | None,
    tailscale_ips: list[str],
    os: str | None,
    online_state: str,
    is_exit_node: bool,
    last_seen_at: datetime | None,
    collected_at: datetime,
) -> int:
    """UPSERT ON CONFLICT(node_key); 若 online_state 变更则写 tailscale_node_events。

    Returns:
        tailscale_nodes.id (新插入或已存在行的主键)。
    """
    now = _now_iso()
    collected_iso = _iso(collected_at)
    last_seen_iso = _iso(last_seen_at) if last_seen_at is not None else None
    ips_json = json.dumps(tailscale_ips)

    # 先查旧状态,以便判断是否要写 event
    async with self._conn.execute(
        "SELECT id, online_state FROM tailscale_nodes WHERE node_key = ?",
        (node_key,),
    ) as cur:
        existing = await cur.fetchone()

    is_exit_int = 1 if is_exit_node else 0

    if existing is None:
        # 首次插入
        await self._conn.execute(
            """
            INSERT INTO tailscale_nodes
                (node_key, hostname, dns_name, tailscale_ips, os,
                 online_state, is_exit_node, last_seen_at, collected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_key, hostname, dns_name, ips_json, os,
                online_state, is_exit_int, last_seen_iso, collected_iso, now,
            ),
        )
        # 取新行 id
        async with self._conn.execute(
            "SELECT id FROM tailscale_nodes WHERE node_key = ?", (node_key,)
        ) as cur2:
            row2 = await cur2.fetchone()
        node_id: int = row2["id"]
        # 写首次发现事件
        await self._conn.execute(
            """
            INSERT INTO tailscale_node_events
                (node_key, from_state, to_state, occurred_at, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (node_key, None, online_state, collected_iso, "first_seen"),
        )
    else:
        node_id = existing["id"]
        old_state: str = existing["online_state"]
        # UPSERT: 更新全部字段
        await self._conn.execute(
            """
            UPDATE tailscale_nodes SET
                hostname       = ?,
                dns_name       = ?,
                tailscale_ips  = ?,
                os             = ?,
                online_state   = ?,
                is_exit_node   = ?,
                last_seen_at   = ?,
                collected_at   = ?,
                updated_at     = ?
            WHERE node_key = ?
            """,
            (
                hostname, dns_name, ips_json, os,
                online_state, is_exit_int, last_seen_iso, collected_iso, now,
                node_key,
            ),
        )
        # 仅在状态变更时写事件
        if old_state != online_state:
            await self._conn.execute(
                """
                INSERT INTO tailscale_node_events
                    (node_key, from_state, to_state, occurred_at, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (node_key, old_state, online_state, collected_iso, None),
            )

    await self._conn.commit()
    return node_id


async def _get_all_nodes(self: Repository) -> list[TailscaleNodeRow]:
    """返回所有 tailscale_nodes 行,按 hostname 升序。"""
    async with self._conn.execute(
        """
        SELECT id, node_key, hostname, dns_name, tailscale_ips, os,
               online_state, is_exit_node, last_seen_at, collected_at, updated_at
        FROM tailscale_nodes
        ORDER BY hostname
        """
    ) as cur:
        return [_to_node_row(r) async for r in cur]


async def _get_node_by_id(self: Repository, node_id: int) -> TailscaleNodeRow | None:
    """按 id 返回单节点;不存在则 None。"""
    async with self._conn.execute(
        """
        SELECT id, node_key, hostname, dns_name, tailscale_ips, os,
               online_state, is_exit_node, last_seen_at, collected_at, updated_at
        FROM tailscale_nodes WHERE id = ?
        """,
        (node_id,),
    ) as cur:
        row = await cur.fetchone()
    return _to_node_row(row) if row is not None else None


async def _get_node_events(
    self: Repository,
    node_key: str,
    limit: int = 100,
) -> list[TailscaleNodeEventRow]:
    """返回指定节点最近 limit 条事件,按 occurred_at 降序。"""
    async with self._conn.execute(
        """
        SELECT id, node_key, from_state, to_state, occurred_at, note
        FROM tailscale_node_events
        WHERE node_key = ?
        ORDER BY occurred_at DESC
        LIMIT ?
        """,
        (node_key, limit),
    ) as cur:
        return [_to_event_row(r) async for r in cur]


# --- 行映射辅助函数 ---


def _to_node_row(row: aiosqlite.Row) -> TailscaleNodeRow:
    raw_last_seen: str | None = row["last_seen_at"]
    return TailscaleNodeRow(
        id=row["id"],
        node_key=row["node_key"],
        hostname=row["hostname"],
        dns_name=row["dns_name"],
        tailscale_ips=json.loads(row["tailscale_ips"] or "[]"),
        os=row["os"],
        online_state=row["online_state"],
        is_exit_node=bool(row["is_exit_node"]),
        last_seen_at=_parse_utc(raw_last_seen) if raw_last_seen else None,
        collected_at=_parse_utc(row["collected_at"]),
        updated_at=_parse_utc(row["updated_at"]),
    )


def _to_event_row(row: aiosqlite.Row) -> TailscaleNodeEventRow:
    return TailscaleNodeEventRow(
        id=row["id"],
        node_key=row["node_key"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        occurred_at=_parse_utc(row["occurred_at"]),
        note=row["note"],
    )


# 注入到 Repository 类
Repository.upsert_tailscale_node = _upsert_tailscale_node  # type: ignore[attr-defined]
Repository.get_all_nodes = _get_all_nodes  # type: ignore[attr-defined]
Repository.get_node_by_id = _get_node_by_id  # type: ignore[attr-defined]
Repository.get_node_events = _get_node_events  # type: ignore[attr-defined]
