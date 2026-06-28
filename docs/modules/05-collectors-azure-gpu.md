# 模块参考：collectors-azure-gpu（Azure VM + GPU 采集）

> 适用范围：`src/panel/collectors/azure/`、`src/panel/collectors/gpu/`。
> 关联架构：ARCH-001（采集框架/降级语义）、ARCH-002（Azure VM + GPU 监控）。
> 关联任务：TASK-012（Azure 采集器）、TASK-013（GPU 采集器）、TASK-016（降采样 job）、TASK-018（动态公网 IP + 只读 SP 对齐）、TASK-019（SP 落配/部署指引）。
> 本文档面向本模块的维护者/扩展者，读完即可在不通读全部源码的前提下理解并安全扩展本模块。

---

## 1. 模块概述与职责

本模块负责**实验室 Azure 云 VM（A100）的运行状态采集**与**所有 GPU 机的多卡指标采集**，是 REQ-002 的核心实现。它由两个相互独立又通过数据库快照单向耦合的采集器，外加一个离线降采样 job 组成：

- **`AzureVmCollector`（collector 名 `azure_vm`，间隔 300s）**：用 Azure SDK 一次性拉取订阅下所有 VM 的电源态，映射为统一展示枚举，写 `azure_vm_status` 专用表；并（在注入 NetworkManagementClient 时）解析每台已注册 VM 当前关联的公网 IP，作为 `metric="public_ip"` 写入通用 `latest_snapshot`。
- **`GpuCollector`（collector 名 `gpu`，间隔 60s）**：通过 asyncssh 并发 SSH 到 `has_gpu=True` 的机器执行 `nvidia-smi`，解析多卡 CSV，写 `gpu_metrics` 专用表，并为每台机产出一条汇总 `MetricSample`。它在 SSH 之前会读取 Azure 采集器写的快照，对绑定 Azure VM 的机器**按电源态跳采**、**用动态公网 IP 覆盖静态 ssh_host**。
- **`downsampler`（两个 APScheduler job）**：把 `gpu_metrics` 原始时序聚合为 5min / 1h 桶，并维护各表的数据保留窗口。

它解决的核心痛点有三：(a) A100 用动态公网 IP，重启后 IP 会变，静态 `ssh_host` 失效——通过「Azure 采集器解析 IP → GPU 采集器消费」的数据流动态修正；(b) VM 非 running 时反复 SSH 超时堆积——通过电源态快照跳采；(c) GPU 秒级时序数据量大、树莓派资源受限——通过降采样 + 保留清理控制磁盘与查询成本。

---

## 2. 文件与关键符号清单

### `src/panel/collectors/azure/__init__.py` — Azure 采集器工厂

| 符号 | 位置 | 职责 |
|------|------|------|
| `register(settings, repo, gpu_repo)` | `__init__.py:31` | 工厂入口。由 `register_collectors` 集中调用。**凭证校验门控**：`settings.azure_configured` 为假即记 warning 跳过（collector disabled）；secret 文件读不到也跳过（不暴露路径）。就绪则**延迟导入** azure SDK，用 `ClientSecretCredential` 构造 `ComputeManagementClient` + `NetworkManagementClient`（同一 credential），注入 `AzureVmCollector` 并注册到全局 registry。 |

### `src/panel/collectors/azure/collector.py` — Azure VM 电源态 + 公网 IP 采集

| 符号 | 位置 | 职责 |
|------|------|------|
| `POWER_STATE_MAP` | `collector.py:48` | Azure PowerState code（小写）→ (展示串, is_running float 1.0/0.0) 的映射表，覆盖 running/stopped/deallocated/starting/stopping/deallocating 六态。 |
| `_UNKNOWN` | `collector.py:56` | 未识别电源态的兜底 `("Unknown", 0.0)`。 |
| `_parse_power_state(statuses)` | `collector.py:59` | 从 `instanceView.statuses` 取首个 `code` 前缀为 `powerstate/`（忽略大小写）的条目，返回 `(display, raw_code, is_running)`。缺失/未识别返回 `("Unknown", None, 0.0)`。 |
| `_get(obj, key)` | `collector.py:87` | 统一从 SDK 对象（属性）或测试 fixture（dict）取字段——全模块解析都走它，使真实 SDK 对象与 dict fixture 可互换。 |
| `_parse_resource_id(resource_id)` | `collector.py:94` | 从 ARM 资源 id（`/subscriptions/.../resourceGroups/{rg}/.../{name}`）大小写不敏感地提取 `(resource_group, name)`，供 SDK 的 `get(rg, name)` 定位资源。 |
| `AzureVmCollector` | `collector.py:118` | dataclass 采集器，满足 ARCH-001 Collector 协议。字段：`client`/`gpu_repo`/`base_repo`/`network_client`/`name="azure_vm"`/`interval_seconds=300`/`timeout_seconds=60`。 |
| `.collect()` | `collector.py:141` | 一轮采集主流程：读 servers → 按 `azure_vm_name` 建索引（只采注册机）→ `to_thread(list_all)` → 逐 VM `_process_vm`。 |
| `._fetch_vms_sync()` | `collector.py:181` | 同步消费 `virtual_machines.list_all(expand="instanceView")` 分页迭代器，在线程池中执行。 |
| `._process_vm(vm, server, now)` | `collector.py:186` | 单台 VM：解析电源态 → upsert `azure_vm_status` → 产 `power_state` sample；解析异常隔离为 `status="error"` 且不 upsert（保留旧值）。注入了 network_client 时追加 `public_ip` sample。 |
| `._resolve_public_ip(vm)` | `collector.py:251` | VM → NIC → public IP 链路解析；失败返回 `None`（仅 debug 日志，不抛）。同步 SDK 调用走 `to_thread`。 |
| `._first_nic_id(vm)` | `collector.py:277` | 取 `network_profile.network_interfaces[0].id`。 |
| `._fetch_public_ip_sync(nic_id)` | `collector.py:288` | 同步：NIC → `ip_configurations[*].public_ip_address.id` → public IP 资源 → `.ip_address`。任一环节缺失返回 None。 |

### `src/panel/collectors/gpu/__init__.py` — GPU 采集器工厂

| 符号 | 位置 | 职责 |
|------|------|------|
| `register(settings, repo, gpu_repo)` | `__init__.py:25` | 工厂入口。**无凭证门控、始终注册**（SSH 私钥按路径存于 servers 表，由 asyncssh 读取）。构造 `GpuCollector(gpu_repo, base_repo=repo)` 并注册。 |

### `src/panel/collectors/gpu/collector.py` — SSH + nvidia-smi 多卡采集

| 符号 | 位置 | 职责 |
|------|------|------|
| `NVIDIA_SMI_CMD` | `collector.py:49` | 查询命令；字段顺序 `index,name,util,mem.used,mem.total,temp,power.draw`，与解析器严格对应。 |
| `_EXPECTED_FIELDS = 7` | `collector.py:57` | CSV 每行期望字段数。 |
| `_NULL_TOKENS` | `collector.py:59` | nvidia-smi 的「无数值」占位串集合（`[not supported]` / `[n/a]` / 空 / `[unknown error]`）。 |
| `SshResult` | `collector.py:67` | `(stdout, exit_status)` 的归一返回 dataclass。 |
| `SshRunner`（Protocol） | `collector.py:75` | SSH 执行层协议：`run(server, command, timeout_seconds, host=None) -> SshResult`。连接/认证/超时类故障以异常抛出；命令非零退出码通过 `exit_status` 表达（不抛）。 |
| `AsyncSshRunner` | `collector.py:92` | 默认实现，基于 `asyncssh.connect`。`host` 非 None 时连传入 host（动态 IP），否则连 `server.ssh_host`。 |
| `_to_float(token)` | `collector.py:129` | 安全转 float；占位串/非数字 → None。 |
| `_parse_nvidia_smi_csv(server_id, output, now)` | `collector.py:140` | 多行 CSV → `list[GpuSample]`。空行跳过；字段数不符/index 非整 → 该行 `status="error"`；单字段转换失败置 None 但不降级整行。 |
| `_error_sample(...)` / `_status_sample(...)` | `collector.py:194` / `:210` | 构造数值全 None 的占位 GpuSample。`_status_sample` 的 `value_text` 透传到 `gpu_name` 占位字段（用于 `vm_not_running` 标注）。 |
| `GpuCollector` | `collector.py:241` | dataclass 采集器。字段：`gpu_repo`/`base_repo`/`ssh_runner=AsyncSshRunner`/`name="gpu"`/`interval_seconds=60`/`timeout_seconds=30`。 |
| `.collect()` | `collector.py:258` | 过滤 `has_gpu=True` → `asyncio.gather(return_exceptions=True)` 并发各机 → 汇总 `append_gpu_metrics` 一次写库 → 每台一条汇总 MetricSample。 |
| `._collect_one(server)` | `collector.py:304` | 单台采集，含 **TASK-018 动态主机逻辑**（见 §4 数据流）与异常分类（unreachable/error）。 |
| `._summarize(server, samples, now)` | `collector.py:376` | 多卡 GpuSample → 一条 `metric="gpu_any_running"` 的 MetricSample。 |

### `src/panel/collectors/gpu/downsampler.py` — 降采样 + 保留清理 job

| 符号 | 位置 | 职责 |
|------|------|------|
| `FIVE_MIN` / `ONE_HOUR` | `downsampler.py:31` | 桶粒度常量。 |
| `RAW_RETENTION = 48h` / `FIVE_MIN_RETENTION = 30d` | `downsampler.py:34` | 保留窗口常量。 |
| `floor_bucket(dt, bucket)` | `downsampler.py:43` | 纯函数：把 dt 以 Unix epoch 为基准向下对齐到桶粒度整数倍；naive 视为 UTC，返回 tz-aware UTC。 |
| `_iso(dt)` | `downsampler.py:61` | tz-aware UTC → ISO8601 字符串。 |
| `run_5m_downsample(gpu_repo, now=None)` | `downsampler.py:71` | 聚合**上一个完整** 5min 桶写 `gpu_metrics_5m`，随后清理 raw（48h）与 5m（30d）。 |
| `run_1h_downsample(gpu_repo, now=None)` | `downsampler.py:120` | 从 `gpu_metrics_5m` 聚合**上一个完整** 1h 桶写 `gpu_metrics_1h`，无清理（长期保留）。 |
| `_round(value)` | `downsampler.py:159` | 聚合数值统一保留两位小数；None 透传。 |

---

## 3. 关键数据结构 / 表 / 契约

### 框架级数据契约（来自 `collectors/base.py`，ARCH-001）

```python
@dataclass(slots=True)
class MetricSample:
    target_id: int            # 关联 server/node 的 id；无 target 维度时 0
    metric: str               # "power_state" / "public_ip" / "gpu_any_running"
    value_num: float | None   # 数值型指标
    value_text: str | None    # 文本型指标
    status: Literal["ok", "unreachable", "error"] = "ok"
    collected_at: datetime
```

两个采集器的 `collect()` 返回 `list[MetricSample]`，由调度框架（`scheduler.run_collector`）统一写入通用 `latest_snapshot` / `metric_history`，并记 `collector_run`。**采集器本身不写通用表**——只返回样本。

### 采集器内部传输对象（来自 `db/gpu_repository.py`）

```python
@dataclass(slots=True)
class GpuSample:               # GpuCollector 产出，写 gpu_metrics
    server_id: int
    gpu_index: int
    gpu_name: str | None       # 占位用途：vm_not_running 标注也写这里
    util_pct / mem_used_mib / mem_total_mib / temp_c / power_w: float | None
    status: Literal["ok", "unreachable", "error"]
    collected_at: datetime
```

> 注意：`GpuSample` **没有** `mem_pct` 字段——`mem_pct` 由 `append_gpu_metrics` 在写库时按 `mem_used/mem_total*100` 计算（`gpu_repository.py:292`）。

`GpuBucketRow`（`gpu_metrics_5m` / `gpu_metrics_1h` 共用）：`server_id, gpu_index, avg_util_pct, avg_mem_pct, max_temp_c, max_power_w, sample_count, bucket_start(ISO8601)`。

### 涉及的表（DDL 见 ARCH-002 §数据模型）

| 表 | 写者 | 内容 | 保留 |
|----|------|------|------|
| `servers` | 注册 API（读：两个采集器） | 服务器注册（含 `azure_vm_name`/`ssh_host`/`ssh_key_path`/`has_gpu`） | 持久 |
| `azure_vm_status` | AzureVmCollector（upsert by `server_id`） | VM 电源态快照（每台一行） | 覆盖式 |
| `gpu_metrics` | GpuCollector（append-only） | 多卡富结构时序 | 48h（5m job 清理） |
| `gpu_metrics_5m` | `run_5m_downsample`（INSERT OR REPLACE by 唯一索引） | 5min 桶 | 30 天（5m job 清理） |
| `gpu_metrics_1h` | `run_1h_downsample` | 1h 桶 | 长期不清理 |
| `latest_snapshot`（通用表） | 框架（经 `collect()` 返回值） | `azure_vm/power_state`、`azure_vm/public_ip`、`gpu/gpu_any_running` | 覆盖式 |

### 电源态映射契约

`value_text` 存展示串（Running/Stopped/...）；`value_num` 存 `1.0`（running）/ `0.0`（其它），**GPU 采集器据 `value_num != 1.0` 判定非 running**——这是跳采的唯一判据，新增电源态时务必保证 `POWER_STATE_MAP` 中非 running 态 `is_running=0.0`。

### Collector 协议

满足 ARCH-001 的 `Collector` Protocol：实例需有 `name: str`、`interval_seconds: int`、`timeout_seconds: int`、`async def collect() -> list[MetricSample]`。两个采集器均以 dataclass 字段提供这三个属性。

---

## 4. 对外接口与调用关系

### 启动期装配链

```
main.lifespan
  └─ register_collectors(settings, repo, gpu_repo)        # collectors/__init__.py
       ├─ azure.register(...)   → ClientSecretCredential → Compute/Network client → registry.register(AzureVmCollector)
       ├─ gpu.register(...)     → registry.register(GpuCollector)
       └─ tailscale.register(...)（本模块外）
  └─ build_scheduler(repo)      # 读 registry 为每个 collector 装配 interval job + run_collector 降级包装
  └─ scheduler.add_job(run_5m_downsample, interval 5min, args=[gpu_repo], id="gpu_downsample_5m")   # main.py:82
  └─ scheduler.add_job(run_1h_downsample, interval 1h,  args=[gpu_repo], id="gpu_downsample_1h")    # main.py:89
```

采集器**不感知调度/降级**：框架（`run_collector`）用 `asyncio.timeout(timeout_seconds)` 包 `collect()`，并把整体异常转成 `collector_run.status="error"`（error 已脱敏）。降采样 job 由 main.py 直接注册到 scheduler，不走 registry。

### 运行期数据流（动态 IP —— 本模块核心）

这是两个采集器之间**唯一的耦合点**，且是**经数据库的单向解耦**（无直接调用）：

```
┌──────────────────┐                                  ┌──────────────────────────────────┐
│ AzureVmCollector │  每 300s collect():               │ GpuCollector  每 60s _collect_one():│
│  · 解析电源态     │  写 latest_snapshot               │  读 base_repo.get_snapshot_metric  │
│  · 解析公网 IP    │ ───(框架写)──▶ azure_vm/power_state │   ("azure_vm", server.id, ...)     │
└──────────────────┘                azure_vm/public_ip │                                    │
                                                        │  仅当 server.azure_vm_name 非空:    │
                                                        │   1) power_state 快照 value_num≠1.0 │
                                                        │      → 跳过 SSH，产 unreachable     │
                                                        │        (value_text="vm_not_running")│
                                                        │   2) ==1.0 → 读 public_ip.value_text│
                                                        │      作连接 host，覆盖 ssh_host      │
                                                        │   3) 无快照(ps is None)/无 public_ip │
                                                        │      → host=None，回退静态 ssh_host  │
                                                        └──────────────────────────────────┘
```

关键代码：`GpuCollector._collect_one`（`collector.py:321-341`）。读取走 ARCH-001 通用 `Repository.get_snapshot_metric(collector, target_id, metric)`（`repository.py:207`），**不读 azure_vm_status 专用表**——TASK-018 明确改为读通用快照，与 ARCH-001 语义对齐。

时序耦合的两个边界条件务必记住：
- Azure 采集器**未启用**（凭证缺失）或**首轮尚未跑** → 无 `azure_vm` 快照 → `ps is None` → **不跳采**，按静态 `ssh_host` 走（向后兼容）。
- Azure 采集器间隔 300s 远大于 GPU 的 60s，故 GPU 采集器读到的电源态/IP 最长可陈旧约 5 分钟——这是可接受的设计折中（VM 状态/IP 变化不频繁）。

### 被谁调用 / 调用谁

- `AzureVmCollector` 调用：`gpu_repo.get_all_servers`、`gpu_repo.upsert_vm_status`；Azure SDK `virtual_machines.list_all` / `network_interfaces.get` / `public_ip_addresses.get`。
- `GpuCollector` 调用：`gpu_repo.get_all_servers`、`gpu_repo.append_gpu_metrics`、`base_repo.get_snapshot_metric`；`SshRunner.run`。
- `downsampler` 调用：`gpu_repo.aggregate_raw_buckets` / `aggregate_5m_buckets` / `upsert_5m_bucket` / `upsert_1h_bucket` / `delete_raw_metrics_before` / `delete_5m_buckets_before`。

下游消费者（本模块产出的读取方）：`api/azure.py` 的 dashboard 聚合与 GPU 趋势查询端点、前端 `_vm_card.html` / `_gpu_card.html`。

---

## 5. 与其他模块的依赖

**上游（本模块依赖）**：

- `panel.collectors.base`：`MetricSample`、`Collector` 协议（契约源头，不可改字段）。
- `panel.db.gpu_repository`：`GpuRepository`、`GpuSample`、`GpuBucketRow`、`ServerRow`（专用表读写）。
- `panel.db.repository`：`Repository.get_snapshot_metric`（GPU 采集器读 Azure 快照）。
- `panel.config.settings`：`Settings.azure_configured`、`read_secret`（Azure 工厂凭证门控）。
- `panel.collectors.registry`：`register`（注册到全局表）。
- 第三方：`azure-identity`、`azure-mgmt-compute`、`azure-mgmt-network`（延迟导入，仅 Azure 启用时需要）、`asyncssh`、`aiosqlite`。

**下游（依赖本模块产出）**：

- `panel.main`：`register_collectors` 调本模块两个工厂；`run_5m_downsample` / `run_1h_downsample` 由 lifespan 注册。
- `panel.api.azure`：读 `azure_vm_status` / `gpu_metrics` / 降采样表 + `latest_snapshot` 渲染 dashboard 与趋势。
- 前端 partial 卡片。

**横向（无直接调用，仅数据耦合）**：Azure 采集器与 GPU 采集器通过 `latest_snapshot` 单向耦合（§4）。

---

## 6. 扩展点（可操作步骤）

### 6.1 新增一个采集器（同模块风格）

1. 新建 `src/panel/collectors/<name>/collector.py`，实现一个满足 Collector 协议的类（dataclass，含 `name`/`interval_seconds`/`timeout_seconds` 字段 + `async collect() -> list[MetricSample]`）。
2. **降级语义**：单 target 失败必须捕获并以 `status=unreachable/error` 的 MetricSample 表达，**不抛**；仅整体不可用（配置缺失/数据源全挂）才抛异常交框架降级。
3. 新建 `<name>/__init__.py`，写 `register(settings, repo, gpu_repo)` 工厂：需凭证的在工厂内做门控（缺失记 warning 跳过，参考 `azure/__init__.py:39`），延迟导入重依赖。
4. 在 `collectors/__init__.py` 的 `register_collectors` 末尾追加对该工厂的调用。
5. 同步加测试（见 §8 风格）。

### 6.2 给 Azure 采集器新增一个 VM 维度指标

例如新增 `private_ip` / `vm_size`：

1. 在 `_process_vm`（`collector.py:186`）中解析出值，`samples.append(MetricSample(target_id=server.id, metric="<新指标>", value_text=..., status="ok", collected_at=now))`。
2. 失败务必隔离：仿照 `_resolve_public_ip` 用 try/except 包住，失败返回 None 且不追加样本——**绝不让新指标解析失败影响 power_state**。
3. 同步 SDK 调用一律放 `asyncio.to_thread`。
4. **无需 migration**：通用 `latest_snapshot` 表「一 target 一标量指标」语义即可承载（TASK-018 同款做法）。下游若要读，用 `Repository.get_snapshot_metric("azure_vm", server_id, "<新指标>")`。

### 6.3 新增 nvidia-smi 采集字段

1. 在 `NVIDIA_SMI_CMD`（`collector.py:49`）的 `--query-gpu=` 末尾追加字段名，并把 `_EXPECTED_FIELDS` 加 1。
2. 在 `_parse_nvidia_smi_csv`（`collector.py:140`）按新列位置取 `parts[i]` 并 `_to_float`。
3. 若要持久化：给 `GpuSample` 加字段、给 `gpu_metrics` 表加列（**需 migration**）、在 `append_gpu_metrics` 的写 SQL 加占位。注意字段顺序必须三处（命令/解析/写库）一致。

### 6.4 新增/调整降采样桶或保留策略

- 改保留窗口：调 `RAW_RETENTION` / `FIVE_MIN_RETENTION` 常量（`downsampler.py:34`）。
- 新增更粗粒度桶（如 1d）：仿 `run_1h_downsample` 写新 job 函数 + `GpuRepository` 加 `aggregate_*` / `upsert_*` / `get_gpu_history_*` 方法（复用 `_upsert_bucket` / `_get_history_bucket` / `_aggregate` 私有实现）+ 在 main.py lifespan 注册新 `add_job`。

### 6.5 替换 SSH 执行层（测试或加固）

`GpuCollector.ssh_runner` 是注入点。实现 `SshRunner` 协议（`run(server, command, timeout_seconds, host=None)`）的新类，构造时传入即可。生产加固例：把 `AsyncSshRunner.run` 的 `known_hosts=None` 改为加载 `known_hosts` 做指纹校验（ARCH-001 标注的 P3 增强）。

---

## 7. 配置 / 环境变量

所有变量走 `PANEL_` 前缀（pydantic-settings，`config/settings.py`）。

| 变量 | 字段 | 作用 |
|------|------|------|
| `PANEL_AZURE_TENANT_ID` | `azure_tenant_id` | SP 租户 id |
| `PANEL_AZURE_CLIENT_ID` | `azure_client_id` | SP 客户端 id |
| `PANEL_AZURE_CLIENT_SECRET_FILE` | `azure_client_secret_file` | **secret 文件路径**（非明文），如 `/secrets/azure_client_secret` |
| `PANEL_AZURE_SUBSCRIPTION_ID` | `azure_subscription_id` | 订阅 id |
| `PANEL_SECRETS_DIR` | `secrets_dir` | secret 目录（默认 `/secrets`），`read_secret` 解析裸文件名时的基目录 |
| `PANEL_STALE_THRESHOLD_SECONDS` | `stale_threshold_seconds` | 陈旧判定阈值（API/前端用，采集器本身不读） |

**门控规则**：`Settings.azure_configured`（`settings.py:60`）要求上述四个 Azure 字段**全部非空**才返回 True；否则 `azure.register` 记 warning 跳过，AzureVmCollector disabled，面板照常运行（前端标「未配置」），GPU 采集对绑定 VM 的机器回退静态 ssh_host。

**凭证安全**：client_secret 经 `read_secret(path)` 从挂载文件读取（按路径约定，secret 内容**不入 env 明文、不入 DB、不进日志**）。SSH 私钥以 `server.ssh_key_path` 路径形式存 servers 表，由 asyncssh 读取，内容不进 DB/日志。SP 的创建命令、`Reader` 角色 + 资源组级 scope 落配、A100 预置注册见 TASK-019。

采集间隔（硬编码在 dataclass 默认值，非 env）：Azure 300s、GPU 60s、5m job 每 5min、1h job 每 1h。

---

## 8. 测试位置与覆盖

| 测试文件 | 覆盖对象 | 要点 |
|----------|----------|------|
| `tests/test_azure_collector.py` | `AzureVmCollector` 电源态 + `register` 工厂 | 电源态映射参数化、六态全覆盖断言、running/mixed/empty/no-powerstate fixture、SDK 属性对象 vs dict fixture 兼容、未注册 VM 跳过、无 azure_vm_name 忽略、单台解析失败隔离且不覆盖旧状态、整体 SDK 失败向上抛、协议合规、register 在 unconfigured/partial/secret 缺失时跳过、register 成功、**register 不记 secret 日志**。 |
| `tests/test_azure_public_ip.py` | `AzureVmCollector` 公网 IP 解析（TASK-018） | `_parse_resource_id` 正常/大小写/缺失；dict 与 SDK 属性两种 NIC 形态；跳过无 public_ip 的 ip_config 取下一个；无 NIC / 无 public_ip ref / public IP 资源无 ip_address 均跳过；**SDK 解析异常隔离**（仍有 power_state、无 public_ip、不降级）；无 network_client 不产 public_ip；power_state error 路径不产 IP；running VM 产两条样本。 |
| `tests/test_gpu_collector.py` | `GpuCollector` 解析 + 采集 + register | CSV 单卡/多卡/空输出/部分 Not Supported/字段数错/非数字 index/空行；写库 + 汇总；非零退出 → error；连接失败/OSError/超时 → unreachable；并发失败隔离；gather 兜底意外异常 → error；无 GPU 机返回空；跳过非 GPU 机；**ssh_key_path 不出现在任何样本**；协议合规；register 始终注册；AsyncSshRunner 连接错误传播 / 成功路径。 |
| `tests/test_gpu_dynamic_host.py` | `GpuCollector` 动态主机（TASK-018） | VM 非 running / stopped 跳采且不调 SSH（mock 验证未调用）；running 用动态 IP 作 host；running 无 public_ip → 回退静态；public_ip 文本为空 → 回退；无 power_state 快照不跳采且不查 public_ip；纯 SSH 机（无 azure_vm_name）逻辑完全不变；running+stopped 混合。 |
| `tests/test_gpu_downsample.py` | `downsampler` + 趋势 API | `floor_bucket` 5min/1h 对齐 + 边界恒等 + naive 视 UTC；5m 桶 avg/max/count；**只计 status='ok' 行**；只聚合上一个完整桶；raw 48h 清理；5m 30d 清理；1h 从 5m 桶聚合；upsert 幂等；history 端点 raw/5m/1h/未知卡空/非法 granularity 422/limit 上限。 |
| `tests/test_retention.py` | 通用 metric_history 保留（TASK-040，相邻模块） | 与本模块 GPU 专用表清理互补。 |

测试中 `gpu_repo` / `base_repo` 为公共 fixture；SSH 与 Azure SDK 均以 mock/dict fixture 注入，单测不连真实机/不调真实 Azure。

---

## 9. 注意事项 / 降级语义 / gotchas

1. **`known_hosts=None`（`collector.py:115`）**：默认 SSH 执行层不校验主机指纹——首期假设内网 Tailscale 已做网络隔离（ARCH-001 裁定），P3 才加强校验。不要误以为这是 bug；改动前先读 §6.5。
2. **`_status_sample` 借用 `gpu_name` 当占位字段**：`vm_not_running` 标注塞进 `GpuSample.gpu_name`（`collector.py:222`），`_summarize` 据 `samples[0].gpu_name == "vm_not_running"` 特判（`collector.py:399`）。这是有意复用，新增整机级标注沿此模式；勿把 `gpu_name` 当作可靠 GPU 名读取（error/unreachable 行该字段是占位）。
3. **`ps.value_num != 1.0` 是跳采唯一判据**：浮点等值比较在此安全（写入恒为 `1.0`/`0.0` 字面量）。新增电源态时务必让非 running 态在 `POWER_STATE_MAP` 中 `is_running=0.0`，否则会误判为 running 而反复 SSH 超时。
4. **`ps is None` 不跳采**：Azure 未配置/首轮未跑时按静态 ssh_host 走。这是向后兼容的关键分支，改 `_collect_one` 时勿误删。
5. **`host=None` 三参/四参签名兼容**（`collector.py:347`）：host 为 None 时不传 `host=` 关键字，兼容只接受三参的旧 SshRunner 实现。自定义 runner 实现 4 参签名即可，但勿在 host=None 时强依赖该参。
6. **公网 IP 解析失败完全隔离**：`_resolve_public_ip` 的 try/except 兜底（仅 debug 日志），`network_client=None`（默认）时整条 public_ip 逻辑跳过——向后兼容无 network 依赖的旧构造。**不要把 IP 解析异常往上抛**，否则会污染 power_state 采集。
7. **单台 vs 整体失败的两层降级**：单 VM/单机失败 → 该 target `status=error/unreachable`，其它不受影响；整体（认证/`list_all`/写库）失败 → `collect()` 抛异常，框架记 `collector_run.error`。Azure 单台解析失败时**不 upsert**（保留 azure_vm_status 旧值，前端凭 is_stale 提示陈旧）。
8. **降采样聚合只计 `status='ok'`**（`gpu_repository.py:537`）：unreachable/error 行数值列为 NULL，不污染均值；但 raw 表清理删的是**全部** 48h 外行（含 error 行）。
9. **降采样桶只算「上一个完整桶」**（`downsampler.py:80`/`:130`）：避免对正在累积的当前桶落不全均值。1h 桶从 5m 桶二次聚合（减少扫描），故 1h 数据完整性依赖 5m job 已先跑——两个 job 间隔不同但各自独立，1h 桶的 sample_count 是底层原始样本数之和（`SUM(sample_count)`）。
10. **`gpu_metrics_5m`/`_1h` 用 INSERT OR REPLACE**：REPLACE 换新自增 id，但桶以唯一索引 `(server_id, gpu_index, bucket_start)` 去重，id 非业务键——勿依赖其 id 稳定。
11. **`GpuSample` 无 `mem_pct`**：mem_pct 在写库时由 `mem_used/mem_total` 计算（分母为 0/None 则 None），不要在采集器里自己算。
12. **凭证脱敏是硬约束**：日志只写 VM 名/主机/卡数/状态。新增日志时严禁打印 secret、ssh_key_path、IP 之外的敏感字段；测试 `test_register_does_not_log_secret` / `test_ssh_key_path_not_in_any_sample` 会拦截回归。
13. **azure SDK 延迟导入**：`azure.register` 仅在凭证就绪时才 import azure 包——缺凭证环境（如 CI / 树莓派未配 Azure）零开销且无需安装这些重依赖。

---

## 10. 关联 REQ / ARCH / TASK 编号

| 类型 | 编号 | 关系 |
|------|------|------|
| 需求 | REQ-002 | 设备监控（Azure VM + GPU）总需求 |
| 架构 | ARCH-001 | 采集框架、Collector 协议、降级语义、凭证按路径约定、`known_hosts` 裁定 |
| 架构 | ARCH-002 | 本模块主架构：分层、电源态映射、专用表 DDL、Pydantic 模型、采集间隔；§Addendum（2026-06）确立动态公网 IP 数据流并取代原 deallocated 跳采归属 |
| 任务 | TASK-012 | AzureVmCollector（ClientSecretCredential / Reader / list_all instanceView） |
| 任务 | TASK-013 | GpuCollector（asyncssh + nvidia-smi 多卡解析、汇总 MetricSample） |
| 任务 | TASK-016 | GPU 降采样 job（5m/1h）+ 保留清理 + 趋势查询 API（重切后不再含跳采逻辑） |
| 任务 | TASK-018 | 动态公网 IP 解析 + 非 running 跳采 + 只读 SP 认证对齐（NetworkManagementClient、`public_ip` 样本、`_collect_one` 动态主机、`SshRunner.run(host=)`） |
| 任务 | TASK-019 | 只读 SP 创建/落配指引、A100 预置注册、部署挂载（凭证操作侧，非本模块代码） |
