---
id: TASK-020
title: "Tailscale 采集器 + 数据库表 + 在线判定逻辑"
status: review
priority: P1
architecture: ARCH-003
dependencies: [TASK-003]
estimated_effort: M
executed_by: claude-sonnet-4-6
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 `TailscaleCollector`，通过宿主机 Unix socket（`/var/run/tailscale/tailscaled.sock`）调用
`/localapi/v0/status`，将全部 tailnet 节点的在线状态与基本属性持久化到 SQLite。
同时定义 `tailscale_nodes` 与 `tailscale_node_events` 两张专用表，并实现
ONLINE / OFFLINE / LONG_OFFLINE 三态在线判定。

## 技术规格

### 文件路径

- `src/panel/collectors/tailscale/__init__.py` — 模块注册入口 `register()`
- `src/panel/collectors/tailscale/collector.py` — `TailscaleCollector` 实现
- `src/panel/db/migrations/003_tailscale.sql` — 专用表 DDL
- `tests/collectors/tailscale/test_collector.py` — 采集器单测
- `tests/collectors/tailscale/test_online_state.py` — 在线判定单测
- `tests/collectors/tailscale/fixtures/localapi_status.json` — 真实 socket 录制 fixture

### localapi 调用

```python
import aiohttp

SOCKET_PATH_DEFAULT = "/var/run/tailscale/tailscaled.sock"
LOCALAPI_BASE = "http://local-tailscaled"   # Host 仅满足 HTTP 格式要求

connector = aiohttp.UnixConnector(path=socket_path)
async with aiohttp.ClientSession(connector=connector) as session:
    resp = await session.get(
        f"{LOCALAPI_BASE}/localapi/v0/status",
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
    )
    data = await resp.json()
```

响应结构：
```json
{
  "Self": {
    "HostName": "muxrpi",
    "DNSName": "muxrpi.tail-xxx.ts.net.",
    "TailscaleIPs": ["100.x.x.x", "fd7a:..."],
    "OS": "linux",
    "Online": true,
    "LastSeen": null,
    "ExitNodeOption": true,
    "PublicKey": "nodekey:..."
  },
  "Peer": {
    "nodekey:...": {
      "HostName": "muxdesktop-wsl-ubuntu",
      ...
    }
  }
}
```

### 在线判定逻辑

```python
LONG_OFFLINE_THRESHOLD = timedelta(hours=24)  # 可由 settings 覆盖

def determine_online_state(
    online: bool,
    last_seen: datetime | None,
    now: datetime,
) -> Literal["ONLINE", "OFFLINE", "LONG_OFFLINE"]:
    if online:
        return "ONLINE"
    if last_seen is None or (now - last_seen) <= LONG_OFFLINE_THRESHOLD:
        return "OFFLINE"
    return "LONG_OFFLINE"
```

### DDL（migrations/003_tailscale.sql）

```sql
CREATE TABLE IF NOT EXISTS tailscale_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT    NOT NULL UNIQUE,
    hostname        TEXT    NOT NULL,
    dns_name        TEXT,
    tailscale_ips   TEXT    NOT NULL DEFAULT '[]',
    os              TEXT,
    online_state    TEXT    NOT NULL DEFAULT 'OFFLINE',
    is_exit_node    INTEGER NOT NULL DEFAULT 0,
    last_seen_at    TEXT,
    collected_at    TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_online_state
    ON tailscale_nodes(online_state);

CREATE TABLE IF NOT EXISTS tailscale_node_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT    NOT NULL,
    from_state      TEXT,
    to_state        TEXT    NOT NULL,
    occurred_at     TEXT    NOT NULL,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_node_events_key_time
    ON tailscale_node_events(node_key, occurred_at DESC);
```

### Repository 签名（db/repository.py 追加）

```python
async def upsert_tailscale_node(
    self,
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
    """
    UPSERT ON CONFLICT(node_key)。
    若 online_state 与现有值不同，额外 INSERT tailscale_node_events。
    返回 tailscale_nodes.id（供 latest_snapshot 的 target_id 使用）。
    """

async def get_all_nodes(self) -> list[TailscaleNodeRow]: ...
async def get_node_by_id(self, node_id: int) -> TailscaleNodeRow | None: ...
async def get_node_events(self, node_key: str, limit: int = 100) -> list[TailscaleNodeEventRow]: ...
```

行类型（在 `db/repository.py` 头部定义，`@dataclass(slots=True)`）：

```python
@dataclass(slots=True)
class TailscaleNodeRow:
    id: int; node_key: str; hostname: str; dns_name: str | None
    tailscale_ips: list[str]   # json.loads 后的列表
    os: str | None; online_state: str; is_exit_node: bool
    last_seen_at: datetime | None; collected_at: datetime; updated_at: datetime

@dataclass(slots=True)
class TailscaleNodeEventRow:
    id: int; node_key: str; from_state: str | None
    to_state: str; occurred_at: datetime; note: str | None
```

### MetricSample 输出规范

每个节点产出一条 `MetricSample`：
```
MetricSample(
    target_id = tailscale_nodes.id,
    metric    = "online_state",
    value_text = "ONLINE" | "OFFLINE" | "LONG_OFFLINE",
    status    = "ok" | "unreachable",
    collected_at = now(UTC),
)
```
socket 整体不可达时抛出异常，由调度框架捕获并写 `collector_run.status="down"`。

### 模块注册（collectors/tailscale/\_\_init\_\_.py）

```python
def register(settings: Settings, repo: Repository) -> None:
    import os
    from panel.collectors.registry import register as reg_register
    socket_path = settings.tailscale_socket_path
    if not os.path.exists(socket_path):
        import logging
        logging.getLogger(__name__).warning(
            "Tailscale socket not found at %s; skipping collector", socket_path
        )
        return
    collector = TailscaleCollector(
        socket_path=socket_path,
        repo=repo,
        timeout_seconds=settings.tailscale_timeout_seconds,
        long_offline_hours=settings.tailscale_long_offline_hours,
    )
    reg_register(collector)
```

### Docker Compose volume 追加

```yaml
volumes:
  - /var/run/tailscale/tailscaled.sock:/var/run/tailscale/tailscaled.sock:ro
```

## 实现指引

1. 先写 `migrations/003_tailscale.sql`，在 `db/migrate.py` 中按文件名升序加载所有 `.sql`，确保幂等（`IF NOT EXISTS`）。
2. 在 `db/repository.py` 追加 `TailscaleNodeRow`、`TailscaleNodeEventRow` dataclass，以及上述四个方法。`upsert_tailscale_node` 使用一次 `SELECT online_state FROM tailscale_nodes WHERE node_key=?` 获取旧状态，再执行 `INSERT OR REPLACE` / `UPDATE`，若状态变化则额外 `INSERT tailscale_node_events`。
3. 实现 `collector.py`：
   - `__init__` 接收 `socket_path, repo, timeout_seconds, long_offline_hours`。
   - `collect()` 建立 `UnixConnector` → GET status → 解析 Self + Peer → 并发（`asyncio.gather`）upsert 各节点 → 返回 `list[MetricSample]`。
   - 调用 `repo.upsert_snapshot("tailscale", samples)` 将 online_state 写入 `latest_snapshot`（沿用 ARCH-001 通用表）。
   - 对 `LastSeen: null` 的情况：`online=True` 时 `last_seen_at=None` 是正常值，不需特殊处理。
   - `ExitNodeOption` 字段部分节点无此键，默认 `False`。
4. 写 `collectors/tailscale/__init__.py` 中的 `register()` 函数。
5. 在 `main.register_collectors()` 中调用 `tailscale.register(settings, repo)`。
6. 将 `tests/collectors/tailscale/fixtures/localapi_status.json` 填入以下样本节点（涵盖所有测试场景）：`muxrpi`（linux, online, exit node）、`muxdesktop-wsl-ubuntu`（linux, online）、`takamichi-lab-pc15`（linux, online, exit node）、`mux-mbp`（macOS, online）、`muxdesktop-windows`（windows, online）、`ipad163`（iOS, offline, last_seen>24h）、`iphone-13`（iOS, offline）、`iphone181`（iOS, offline）、`trl-pc`（windows, offline）。

## 测试要求

- [ ] **fixture 单测**：从 `localapi_status.json` 加载响应，mock `aiohttp` 调用，验证 `collect()` 输出 9 条 `MetricSample`，在线态与预期一致（muxrpi=ONLINE, ipad163=LONG_OFFLINE, iphone-13=OFFLINE）。
- [ ] **在线判定单测**（`test_online_state.py`）：覆盖全部三态边界——`online=True`→ONLINE；`online=False,last_seen=None`→OFFLINE；`online=False,last_seen=23h55m`→OFFLINE；`online=False,last_seen=24h01m`→LONG_OFFLINE。
- [ ] **upsert 变更事件测试**：在内存 SQLite 中连续调用 `upsert_tailscale_node` 两次（ONLINE→OFFLINE），验证 `tailscale_node_events` 中有一行且 `from_state="ONLINE",to_state="OFFLINE"`。
- [ ] **首次发现测试**：节点首次插入，`tailscale_node_events` 有一行 `from_state=None,note="first_seen"`。
- [ ] **socket 不可达测试**：mock `aiohttp` 抛 `aiohttp.ClientConnectorError`，验证 `collect()` 向上抛出（触发框架级 `collector_run.status="down"`），不静默吞掉。
- [ ] **活体验证（可选，CI 跳过）**：在开发机本地，若 `/var/run/tailscale/tailscaled.sock` 存在，可运行集成测试直接调真实 socket，打印节点列表。该测试用 `pytest.mark.integration` 标记，默认 CI `pytest -m "not integration"` 不执行。

## 完成标准

- [ ] `migrations/003_tailscale.sql` 可幂等执行，两张表及索引正确创建。
- [ ] `TailscaleCollector.collect()` 从 fixture 正确解析所有节点，返回 `list[MetricSample]`，数量与 Self+Peer 节点数一致。
- [ ] `determine_online_state()` 三态均被测试覆盖，边界（24h）正确。
- [ ] `upsert_tailscale_node()` 在状态不变时不写 `tailscale_node_events`，状态变更时写入一行。
- [ ] `register()` 在 socket 不存在时不抛异常，仅输出 warning，进程正常启动。
- [ ] `ruff check` 零 error，`pytest -m "not integration"` 全绿。
