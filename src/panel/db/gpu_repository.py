"""GPU / Azure 专用表读写层 (ARCH-002 / TASK-010).

封装五张 ARCH-002 专用表的读写:
    servers          — 服务器注册 CRUD
    azure_vm_status  — VM 电源态快照(upsert)
    gpu_metrics      — GPU 时序追加 + 区间查询
    gpu_metrics_5m   — 5 分钟降采样(TASK-016 填充,本期只提供 schema)
    gpu_metrics_1h   — 1 小时降采样(同上)

所有时间列存 ISO8601 UTC 字符串,与通用 repository 约定一致。
凭证字段(ssh_key_path)仅在写路径存入,读路径通过 ServerRow 保留(供
采集器读取路径),但 API 层的 Pydantic response model(ServerOut)将其
排除在外,不回传前端。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import aiosqlite

from panel.domain.models import ServerIn

# --------------------------------------------------------------------------- #
# 行类型(轻量 dataclass,slots=True)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ServerRow:
    id: int
    name: str
    azure_resource_group: str | None
    azure_vm_name: str | None
    ssh_host: str | None
    ssh_port: int
    ssh_user: str
    ssh_key_path: str | None  # 内部使用;API 层不回传
    has_gpu: bool
    notes: str | None
    created_at: str  # ISO8601 UTC
    updated_at: str  # ISO8601 UTC


@dataclass(slots=True)
class VmStatusRow:
    server_id: int
    power_state: str
    power_state_raw: str | None
    is_running: bool
    collected_at: str  # ISO8601 UTC
    updated_at: str  # ISO8601 UTC


@dataclass(slots=True)
class GpuMetricRow:
    id: int
    server_id: int
    gpu_index: int
    gpu_name: str | None
    util_pct: float | None
    mem_used_mib: float | None
    mem_total_mib: float | None
    mem_pct: float | None
    temp_c: float | None
    power_w: float | None
    status: str
    collected_at: str  # ISO8601 UTC


@dataclass(slots=True)
class GpuBucketRow:
    """gpu_metrics_5m / gpu_metrics_1h 的降采样桶行 (TASK-016).

    两张降采样表列结构相同,共用同一行类型。
    """

    server_id: int
    gpu_index: int
    avg_util_pct: float | None
    avg_mem_pct: float | None
    max_temp_c: float | None
    max_power_w: float | None
    sample_count: int
    bucket_start: str  # ISO8601 UTC,粒度对齐


# --------------------------------------------------------------------------- #
# GpuSample — 采集器内部传输对象(ARCH-002 / collectors/gpu/collector.py 使用)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GpuSample:
    """GPU 单卡单次采集结果,由 GpuCollector 产出并写入 gpu_metrics 表。"""

    server_id: int
    gpu_index: int
    gpu_name: str | None
    util_pct: float | None
    mem_used_mib: float | None
    mem_total_mib: float | None
    temp_c: float | None
    power_w: float | None
    status: Literal["ok", "unreachable", "error"]
    collected_at: datetime


# --------------------------------------------------------------------------- #
# 时间归一化(与 repository.py 同策略)
# --------------------------------------------------------------------------- #


def _iso(dt: datetime) -> str:
    """datetime -> ISO8601 UTC 字符串。naive 视为 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# GpuRepository
# --------------------------------------------------------------------------- #


class GpuRepository:
    """ARCH-002 专用表的薄 SQL 访问层。

    单连接(随 app lifespan)注入,与通用 Repository 共享同一连接。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ---------------------------------------------------------------- servers #

    async def get_all_servers(self) -> list[ServerRow]:
        """返回全部已注册服务器(按 id 升序)。"""
        async with self._conn.execute(
            """
            SELECT id, name, azure_resource_group, azure_vm_name,
                   ssh_host, ssh_port, ssh_user, ssh_key_path,
                   has_gpu, notes, created_at, updated_at
            FROM servers
            ORDER BY id
            """
        ) as cur:
            return [self._to_server(r) async for r in cur]

    async def get_server(self, server_id: int) -> ServerRow | None:
        """按 id 查单台服务器;不存在返回 None。"""
        async with self._conn.execute(
            """
            SELECT id, name, azure_resource_group, azure_vm_name,
                   ssh_host, ssh_port, ssh_user, ssh_key_path,
                   has_gpu, notes, created_at, updated_at
            FROM servers
            WHERE id = ?
            """,
            (server_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._to_server(row) if row is not None else None

    async def insert_server(self, data: ServerIn) -> int:
        """插入一条服务器记录,返回新生成的 id。

        重复 name 会引发 aiosqlite.IntegrityError(UNIQUE 约束)。
        """
        now = _now_iso()
        cur = await self._conn.execute(
            """
            INSERT INTO servers
                (name, azure_resource_group, azure_vm_name,
                 ssh_host, ssh_port, ssh_user, ssh_key_path,
                 has_gpu, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.name,
                data.azure_resource_group,
                data.azure_vm_name,
                data.ssh_host,
                data.ssh_port,
                data.ssh_user,
                data.ssh_key_path,
                1 if data.has_gpu else 0,
                data.notes,
                now,
                now,
            ),
        )
        await self._conn.commit()
        if cur.lastrowid is None:  # pragma: no cover
            raise RuntimeError("insert_server: lastrowid is None after INSERT")
        return cur.lastrowid

    async def delete_server(self, server_id: int) -> bool:
        """删除服务器记录。存在并删除返回 True;不存在返回 False。

        ON DELETE CASCADE 会自动删除 azure_vm_status 和 gpu_metrics 中的关联行。
        """
        cur = await self._conn.execute(
            "DELETE FROM servers WHERE id = ?",
            (server_id,),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -------------------------------------------------------- azure_vm_status #

    async def upsert_vm_status(
        self,
        server_id: int,
        power_state: str,
        power_state_raw: str | None,
        is_running: bool,
        collected_at: datetime,
    ) -> None:
        """按 server_id UPSERT VM 状态快照。"""
        now = _now_iso()
        await self._conn.execute(
            """
            INSERT INTO azure_vm_status
                (server_id, power_state, power_state_raw, is_running,
                 collected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
                power_state     = excluded.power_state,
                power_state_raw = excluded.power_state_raw,
                is_running      = excluded.is_running,
                collected_at    = excluded.collected_at,
                updated_at      = excluded.updated_at
            """,
            (
                server_id,
                power_state,
                power_state_raw,
                1 if is_running else 0,
                _iso(collected_at),
                now,
            ),
        )
        await self._conn.commit()

    async def get_vm_status_all(self) -> list[VmStatusRow]:
        """返回全部 VM 状态快照(按 server_id 升序)。"""
        async with self._conn.execute(
            """
            SELECT server_id, power_state, power_state_raw,
                   is_running, collected_at, updated_at
            FROM azure_vm_status
            ORDER BY server_id
            """
        ) as cur:
            return [self._to_vm_status(r) async for r in cur]

    async def get_vm_status(self, server_id: int) -> VmStatusRow | None:
        """返回单台 VM 的状态快照;不存在返回 None。"""
        async with self._conn.execute(
            """
            SELECT server_id, power_state, power_state_raw,
                   is_running, collected_at, updated_at
            FROM azure_vm_status
            WHERE server_id = ?
            """,
            (server_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._to_vm_status(row) if row is not None else None

    # ------------------------------------------------------------ gpu_metrics #

    async def append_gpu_metrics(self, samples: list[GpuSample]) -> None:
        """批量追加 GPU 指标行(append-only)。"""
        if not samples:
            return
        rows = [
            (
                s.server_id,
                s.gpu_index,
                s.gpu_name,
                s.util_pct,
                s.mem_used_mib,
                s.mem_total_mib,
                # mem_pct 由调用方计算好或留 None
                (
                    round(s.mem_used_mib / s.mem_total_mib * 100, 2)
                    if s.mem_used_mib is not None and s.mem_total_mib
                    else None
                ),
                s.temp_c,
                s.power_w,
                s.status,
                _iso(s.collected_at),
            )
            for s in samples
        ]
        await self._conn.executemany(
            """
            INSERT INTO gpu_metrics
                (server_id, gpu_index, gpu_name,
                 util_pct, mem_used_mib, mem_total_mib, mem_pct,
                 temp_c, power_w, status, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self._conn.commit()

    async def get_latest_gpu_metrics(self, server_id: int) -> list[GpuMetricRow]:
        """返回指定服务器每张 GPU 卡的最新一条记录(按 gpu_index 升序)。

        用子查询取每 (server_id, gpu_index) 最大 id,再 JOIN 取完整行。
        """
        async with self._conn.execute(
            """
            SELECT g.id, g.server_id, g.gpu_index, g.gpu_name,
                   g.util_pct, g.mem_used_mib, g.mem_total_mib, g.mem_pct,
                   g.temp_c, g.power_w, g.status, g.collected_at
            FROM gpu_metrics AS g
            JOIN (
                SELECT gpu_index, MAX(id) AS max_id
                FROM gpu_metrics
                WHERE server_id = ?
                GROUP BY gpu_index
            ) AS latest
              ON g.gpu_index = latest.gpu_index
             AND g.id = latest.max_id
            WHERE g.server_id = ?
            ORDER BY g.gpu_index
            """,
            (server_id, server_id),
        ) as cur:
            return [self._to_gpu_metric(r) async for r in cur]

    async def get_gpu_history(
        self,
        server_id: int,
        gpu_index: int,
        since: datetime,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[GpuMetricRow]:
        """按时间范围查某卡历史时序(collected_at 升序)。

        内层 DESC+LIMIT 取最近 limit 条,外层 ASC 呈现,与通用 repository 策略一致。
        """
        params: list[object] = [server_id, gpu_index, _iso(since)]
        upper = ""
        if until is not None:
            upper = " AND collected_at <= ?"
            params.append(_iso(until))
        params.append(limit)
        async with self._conn.execute(
            f"""
            SELECT id, server_id, gpu_index, gpu_name,
                   util_pct, mem_used_mib, mem_total_mib, mem_pct,
                   temp_c, power_w, status, collected_at
            FROM (
                SELECT id, server_id, gpu_index, gpu_name,
                       util_pct, mem_used_mib, mem_total_mib, mem_pct,
                       temp_c, power_w, status, collected_at
                FROM gpu_metrics
                WHERE server_id = ? AND gpu_index = ?
                      AND collected_at >= ?{upper}
                ORDER BY collected_at DESC
                LIMIT ?
            )
            ORDER BY collected_at ASC
            """,  # noqa: S608
            params,
        ) as cur:
            return [self._to_gpu_metric(r) async for r in cur]

    # ----------------------------------------------- gpu_metrics_5m / _1h #
    # TASK-016 降采样读写。两张表列结构一致,upsert 走 UNIQUE
    # (server_id, gpu_index, bucket_start) 索引的 INSERT OR REPLACE。

    async def upsert_5m_bucket(self, row: GpuBucketRow) -> None:
        """按 (server_id, gpu_index, bucket_start) UPSERT 一个 5min 降采样桶。"""
        await self._upsert_bucket("gpu_metrics_5m", row)

    async def upsert_1h_bucket(self, row: GpuBucketRow) -> None:
        """按 (server_id, gpu_index, bucket_start) UPSERT 一个 1h 降采样桶。"""
        await self._upsert_bucket("gpu_metrics_1h", row)

    async def upsert_5m_buckets(self, rows: list[GpuBucketRow]) -> None:
        """批量 UPSERT 一轮 5min 降采样桶,单次 executemany + 单次 commit。"""
        await self._upsert_buckets("gpu_metrics_5m", rows)

    async def upsert_1h_buckets(self, rows: list[GpuBucketRow]) -> None:
        """批量 UPSERT 一轮 1h 降采样桶,单次 executemany + 单次 commit。"""
        await self._upsert_buckets("gpu_metrics_1h", rows)

    async def _upsert_buckets(self, table: str, rows: list[GpuBucketRow]) -> None:
        """批量 INSERT OR REPLACE,与 append_gpu_metrics 的批量写策略对齐。

        把一轮降采样的多桶写合并为一次 executemany + 一次 commit,避免逐桶提交
        在共享连接上产生写放大(评审 async-perf 修复)。table 仅取自模块内常量,
        无注入面。空列表直接返回(不开事务)。
        """
        if not rows:
            return
        await self._conn.executemany(
            f"""
            INSERT OR REPLACE INTO {table}
                (server_id, gpu_index, avg_util_pct, avg_mem_pct,
                 max_temp_c, max_power_w, sample_count, bucket_start)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,  # noqa: S608
            [
                (
                    r.server_id,
                    r.gpu_index,
                    r.avg_util_pct,
                    r.avg_mem_pct,
                    r.max_temp_c,
                    r.max_power_w,
                    r.sample_count,
                    r.bucket_start,
                )
                for r in rows
            ],
        )
        await self._conn.commit()

    async def _upsert_bucket(self, table: str, row: GpuBucketRow) -> None:
        """两张降采样表共用的 INSERT OR REPLACE 实现。

        table 仅取自模块内常量字符串,不来自外部输入,无注入面。
        REPLACE 会换新 id(AUTOINCREMENT),但桶以唯一索引去重,id 非业务键。
        """
        await self._conn.execute(
            f"""
            INSERT OR REPLACE INTO {table}
                (server_id, gpu_index, avg_util_pct, avg_mem_pct,
                 max_temp_c, max_power_w, sample_count, bucket_start)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,  # noqa: S608
            (
                row.server_id,
                row.gpu_index,
                row.avg_util_pct,
                row.avg_mem_pct,
                row.max_temp_c,
                row.max_power_w,
                row.sample_count,
                row.bucket_start,
            ),
        )
        await self._conn.commit()

    async def get_gpu_history_5m(
        self,
        server_id: int,
        gpu_index: int,
        since: datetime,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[GpuBucketRow]:
        """按时间范围查某卡 5min 降采样桶(bucket_start 升序)。"""
        return await self._get_history_bucket(
            "gpu_metrics_5m", server_id, gpu_index, since, until, limit
        )

    async def get_gpu_history_1h(
        self,
        server_id: int,
        gpu_index: int,
        since: datetime,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[GpuBucketRow]:
        """按时间范围查某卡 1h 降采样桶(bucket_start 升序)。"""
        return await self._get_history_bucket(
            "gpu_metrics_1h", server_id, gpu_index, since, until, limit
        )

    async def _get_history_bucket(
        self,
        table: str,
        server_id: int,
        gpu_index: int,
        since: datetime,
        until: datetime | None,
        limit: int,
    ) -> list[GpuBucketRow]:
        """两张降采样表共用的区间查询。

        内层 DESC+LIMIT 取最近 limit 桶,外层 ASC 呈现,与 get_gpu_history 一致。
        """
        params: list[object] = [server_id, gpu_index, _iso(since)]
        upper = ""
        if until is not None:
            upper = " AND bucket_start <= ?"
            params.append(_iso(until))
        params.append(limit)
        async with self._conn.execute(
            f"""
            SELECT server_id, gpu_index, avg_util_pct, avg_mem_pct,
                   max_temp_c, max_power_w, sample_count, bucket_start
            FROM (
                SELECT server_id, gpu_index, avg_util_pct, avg_mem_pct,
                       max_temp_c, max_power_w, sample_count, bucket_start
                FROM {table}
                WHERE server_id = ? AND gpu_index = ?
                      AND bucket_start >= ?{upper}
                ORDER BY bucket_start DESC
                LIMIT ?
            )
            ORDER BY bucket_start ASC
            """,  # noqa: S608
            params,
        ) as cur:
            return [self._to_bucket(r) async for r in cur]

    # -------------------------------------------------- 降采样聚合源查询 #
    # TASK-016 job 用:对一个时间窗内的源数据按 (server_id, gpu_index) 聚合。

    async def aggregate_raw_buckets(
        self, since: datetime, until: datetime
    ) -> list[tuple[int, int, float | None, float | None, float | None, float | None, int]]:
        """从 gpu_metrics 聚合 [since, until) 窗口,按卡分组。

        返回 (server_id, gpu_index, avg_util, avg_mem, max_temp, max_power, count)。
        仅统计 status='ok' 的行(unreachable/error 行的数值列为 NULL,不应污染均值)。
        """
        return await self._aggregate(
            "gpu_metrics", "util_pct", "mem_pct", since, until
        )

    async def aggregate_5m_buckets(
        self, since: datetime, until: datetime
    ) -> list[tuple[int, int, float | None, float | None, float | None, float | None, int]]:
        """从 gpu_metrics_5m 聚合 [since, until) 窗口为 1h 桶,按卡分组。

        sample_count 用 SUM(原桶 sample_count),反映底层原始样本总数。
        """
        async with self._conn.execute(
            """
            SELECT server_id, gpu_index,
                   AVG(avg_util_pct), AVG(avg_mem_pct),
                   MAX(max_temp_c), MAX(max_power_w),
                   COALESCE(SUM(sample_count), 0)
            FROM gpu_metrics_5m
            WHERE bucket_start >= ? AND bucket_start < ?
            GROUP BY server_id, gpu_index
            """,
            (_iso(since), _iso(until)),
        ) as cur:
            return [tuple(r) async for r in cur]  # type: ignore[misc]

    async def _aggregate(
        self,
        table: str,
        util_col: str,
        mem_col: str,
        since: datetime,
        until: datetime,
    ) -> list[tuple[int, int, float | None, float | None, float | None, float | None, int]]:
        """gpu_metrics 原始表聚合实现(只计 status='ok')。"""
        async with self._conn.execute(
            f"""
            SELECT server_id, gpu_index,
                   AVG({util_col}), AVG({mem_col}),
                   MAX(temp_c), MAX(power_w),
                   COUNT(*)
            FROM {table}
            WHERE collected_at >= ? AND collected_at < ?
                  AND status = 'ok'
            GROUP BY server_id, gpu_index
            """,  # noqa: S608
            (_iso(since), _iso(until)),
        ) as cur:
            return [tuple(r) async for r in cur]  # type: ignore[misc]

    # ------------------------------------------------------ retention 清理 #

    async def delete_raw_metrics_before(self, cutoff: datetime) -> int:
        """删除 gpu_metrics 中 collected_at < cutoff 的行,返回删除行数。"""
        cur = await self._conn.execute(
            "DELETE FROM gpu_metrics WHERE collected_at < ?",
            (_iso(cutoff),),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def delete_5m_buckets_before(self, cutoff: datetime) -> int:
        """删除 gpu_metrics_5m 中 bucket_start < cutoff 的桶,返回删除行数。"""
        cur = await self._conn.execute(
            "DELETE FROM gpu_metrics_5m WHERE bucket_start < ?",
            (_iso(cutoff),),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    # ---------------------------------------------------------------- mappers #

    @staticmethod
    def _to_server(row: aiosqlite.Row) -> ServerRow:
        return ServerRow(
            id=row["id"],
            name=row["name"],
            azure_resource_group=row["azure_resource_group"],
            azure_vm_name=row["azure_vm_name"],
            ssh_host=row["ssh_host"],
            ssh_port=row["ssh_port"],
            ssh_user=row["ssh_user"],
            ssh_key_path=row["ssh_key_path"],
            has_gpu=bool(row["has_gpu"]),
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_vm_status(row: aiosqlite.Row) -> VmStatusRow:
        return VmStatusRow(
            server_id=row["server_id"],
            power_state=row["power_state"],
            power_state_raw=row["power_state_raw"],
            is_running=bool(row["is_running"]),
            collected_at=row["collected_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_gpu_metric(row: aiosqlite.Row) -> GpuMetricRow:
        return GpuMetricRow(
            id=row["id"],
            server_id=row["server_id"],
            gpu_index=row["gpu_index"],
            gpu_name=row["gpu_name"],
            util_pct=row["util_pct"],
            mem_used_mib=row["mem_used_mib"],
            mem_total_mib=row["mem_total_mib"],
            mem_pct=row["mem_pct"],
            temp_c=row["temp_c"],
            power_w=row["power_w"],
            status=row["status"],
            collected_at=row["collected_at"],
        )

    @staticmethod
    def _to_bucket(row: aiosqlite.Row) -> GpuBucketRow:
        return GpuBucketRow(
            server_id=row["server_id"],
            gpu_index=row["gpu_index"],
            avg_util_pct=row["avg_util_pct"],
            avg_mem_pct=row["avg_mem_pct"],
            max_temp_c=row["max_temp_c"],
            max_power_w=row["max_power_w"],
            sample_count=row["sample_count"],
            bucket_start=row["bucket_start"],
        )
