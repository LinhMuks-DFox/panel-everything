"""TailscaleCollector — Tailscale 节点在线状态采集器 (ARCH-003 / TASK-020).

通过宿主机 Unix socket (/var/run/tailscale/tailscaled.sock) 调用 localapi
/localapi/v0/status,解析 Self + Peer 节点属性,判定在线三态,写入:

  - tailscale_nodes 专用表 (upsert)
  - tailscale_node_events (event-driven, 仅在 online_state 变更时 INSERT)
  - 通用 latest_snapshot (metric='online_state', 由框架经 collect() 返回值写入)

设计要点 (遵循 ARCH-001 Collector 协议与降级语义):

  - socket 不可达: collect() 向上抛异常, 由 run_collector 框架降级为
    collector_run.status='down'; 不静默吞掉.
  - 单节点 upsert 失败: 产出 MetricSample(status='unreachable'), 不影响其它节点.
  - ExitNodeOption 字段部分节点无此键, 默认 False.
  - online=True 时 LastSeen 为 null 是正常值, last_seen_at 存 None.
  - 并发用 asyncio.gather 对全部节点并行 upsert, 降低树莓派单次采集耗时.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import aiohttp

from panel.collectors.base import MetricSample

if TYPE_CHECKING:
    from panel.db.repository import Repository

logger = logging.getLogger(__name__)

SOCKET_PATH_DEFAULT = "/var/run/tailscale/tailscaled.sock"
LOCALAPI_BASE = "http://local-tailscaled"  # Host 仅用于 HTTP 格式合法性


def determine_online_state(
    online: bool,
    last_seen: datetime | None,
    now: datetime,
    long_offline_hours: int = 24,
) -> Literal["ONLINE", "OFFLINE", "LONG_OFFLINE"]:
    """判定节点在线三态。

    Args:
        online:              localapi 返回的 Online 字段。
        last_seen:           UTC datetime; Online=True 时传 None。
        now:                 当前时刻 (UTC), 由调用方传入便于测试。
        long_offline_hours:  超过此小时数标 LONG_OFFLINE (默认 24h)。

    Returns:
        "ONLINE" | "OFFLINE" | "LONG_OFFLINE"
    """
    if online:
        return "ONLINE"
    threshold = timedelta(hours=long_offline_hours)
    if last_seen is None or (now - last_seen) <= threshold:
        return "OFFLINE"
    return "LONG_OFFLINE"


def _parse_last_seen(raw: str | None) -> datetime | None:
    """解析 localapi LastSeen 字段 (ISO8601 UTC string or null) 为 datetime。"""
    if raw is None:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class TailscaleCollector:
    """Tailscale 节点在线状态采集器。满足 ARCH-001 Collector 协议。"""

    name: str = "tailscale"
    interval_seconds: int = 60
    timeout_seconds: int = 10

    def __init__(
        self,
        socket_path: str,
        repo: Repository,
        timeout_seconds: int = 10,
        long_offline_hours: int = 24,
    ) -> None:
        self._socket_path = socket_path
        self._repo = repo
        self.timeout_seconds = timeout_seconds
        self._long_offline_hours = long_offline_hours

    async def collect(self) -> list[MetricSample]:
        """采集一轮。解析 Self + Peer → upsert 专用表 → 返回 MetricSample 列表。

        socket 不可达时抛出异常 (由框架层降级为 collector_run.status='down')。
        单节点 upsert 失败: 产出 status='unreachable' 的 sample, 不影响其它节点。
        """
        data = await self._fetch_status()
        now = datetime.now(UTC)

        # 收集所有节点 (Self + Peers)
        raw_nodes: list[dict[str, Any]] = []
        self_node: dict[str, Any] | None = data.get("Self")
        if self_node:
            raw_nodes.append(self_node)
        peers: dict[str, Any] = data.get("Peer") or {}
        raw_nodes.extend(peers.values())

        # 并发 upsert, 各节点独立隔离
        tasks = [self._process_node(node, now) for node in raw_nodes]
        samples: list[MetricSample] = list(await asyncio.gather(*tasks))

        # 写 latest_snapshot (ARCH-001 通用表, 由 collect() 返回后框架写入)
        # 同时在采集器内部也调用 upsert_snapshot, 保持 tailscale 数据源与全局
        # collector dashboard 一致 (ARCH-003 契约: latest_snapshot 含 online_state)
        await self._repo.upsert_snapshot("tailscale", [s for s in samples if s.status == "ok"])

        return samples

    async def _fetch_status(self) -> dict[str, Any]:
        """通过 UnixConnector 调用 localapi, 返回解析后的 JSON dict。

        socket 不可达时抛出异常 (不捕获, 由框架层处理)。
        """
        connector = aiohttp.UnixConnector(path=self._socket_path)
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            resp = await session.get(f"{LOCALAPI_BASE}/localapi/v0/status")
            resp.raise_for_status()
            text = await resp.text()
            return json.loads(text)  # type: ignore[return-value]

    async def _process_node(
        self,
        node: dict[str, Any],
        now: datetime,
    ) -> MetricSample:
        """处理单个节点: 判定在线态 → upsert 专用表 → 产出 MetricSample。

        单节点 upsert 失败: 返回 status='unreachable' 的 sample, 不抛出异常。
        """
        node_key: str | None = node.get("PublicKey")
        hostname: str = node.get("HostName") or "unknown"

        if not node_key:
            logger.warning("tailscale: node missing PublicKey (hostname=%r), skipping", hostname)
            return MetricSample(
                target_id=0,
                metric="online_state",
                value_text="OFFLINE",
                status="error",
                collected_at=now,
            )

        try:
            dns_name: str | None = node.get("DNSName")
            tailscale_ips: list[str] = node.get("TailscaleIPs") or []
            os_: str | None = node.get("OS")
            online: bool = bool(node.get("Online", False))
            last_seen = _parse_last_seen(node.get("LastSeen"))
            is_exit_node: bool = bool(node.get("ExitNodeOption", False))

            online_state = determine_online_state(
                online=online,
                last_seen=last_seen,
                now=now,
                long_offline_hours=self._long_offline_hours,
            )

            node_id = await self._repo.upsert_tailscale_node(  # type: ignore[attr-defined]
                node_key=node_key,
                hostname=hostname,
                dns_name=dns_name,
                tailscale_ips=tailscale_ips,
                os=os_,
                online_state=online_state,
                is_exit_node=is_exit_node,
                last_seen_at=last_seen,
                collected_at=now,
            )

            return MetricSample(
                target_id=node_id,
                metric="online_state",
                value_text=online_state,
                status="ok",
                collected_at=now,
            )

        except Exception:  # noqa: BLE001 — 单节点失败隔离
            logger.warning(
                "tailscale: failed to process node key=%r hostname=%r",
                node_key,
                hostname,
                exc_info=True,
            )
            return MetricSample(
                target_id=0,
                metric="online_state",
                value_text="OFFLINE",
                status="unreachable",
                collected_at=now,
            )
