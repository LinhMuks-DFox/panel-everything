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
