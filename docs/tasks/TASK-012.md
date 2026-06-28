---
id: TASK-012
title: "Azure VM 采集器（ClientSecretCredential, Reader）"
status: done
priority: P1
architecture: ARCH-002
dependencies: [TASK-003, TASK-010]
estimated_effort: M
executed_by: claude-opus-4-8[1m]
created: 2026-06-28
updated: 2026-06-28
---

## 目标

实现 `AzureVmCollector`，遵循 ARCH-001 `Collector` 协议，通过 Azure SDK（`azure-mgmt-compute` + `azure-identity`）定期拉取所有已注册 Azure VM 的电源状态，映射为统一枚举，写入 `azure_vm_status` 表和通用 `latest_snapshot` 表，并暴露 `register(settings, repo)` 工厂供主 app 注册。

## 技术规格

### 文件路径

| 文件 | 说明 |
|------|------|
| `src/panel/collectors/azure/__init__.py` | `register(settings, repo)` 工厂 |
| `src/panel/collectors/azure/collector.py` | `AzureVmCollector` 实现 |

### 认证方案：Service Principal (ClientSecretCredential)

```python
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient

credential = ClientSecretCredential(
    tenant_id=settings.azure_tenant_id,
    client_id=settings.azure_client_id,
    client_secret=settings.azure_client_secret,
)
client = ComputeManagementClient(credential, settings.azure_subscription_id)
```

- **为何不用 Managed Identity**：树莓派非 Azure 托管主机，无 IMDS endpoint（169.254.169.254），`ManagedIdentityCredential` 会超时失败。
- 客户端单例：在 `__init__` 中构造一次，跨调用复用（`ComputeManagementClient` 内部管理 token 刷新）。
- 所需 Azure 权限：在订阅或资源组级别赋予 `Reader` 角色（只读，不执行任何操作性 API）。

### Settings 新增字段（src/panel/config/settings.py）

```python
azure_tenant_id: str | None = None
azure_client_id: str | None = None
azure_client_secret: SecretStr | None = None   # pydantic SecretStr，日志中自动遮码
azure_subscription_id: str | None = None
```

若任一为 None，`register()` 记录 warning 并跳过注册（collector disabled）。

### AzureVmCollector 类签名

```python
@dataclass
class AzureVmCollector:
    name: str = "azure_vm"
    interval_seconds: int = 300
    timeout_seconds: int = 60

    _client: ComputeManagementClient  # 构造时注入
    _gpu_repo: GpuRepository          # 写 azure_vm_status 用
    _base_repo: Repository            # 写通用 latest_snapshot 用

    async def collect(self) -> list[MetricSample]:
        """
        1. 调 _client.virtual_machines.list_all(expand="instanceView")
           （SDK 返回同步 Iterator，需 asyncio.to_thread 或按页异步消费，
            或在 asyncio.get_event_loop().run_in_executor 中包装）
        2. 对每个 VM 提取 PowerState，调 _parse_power_state()
        3. 写 azure_vm_status（upsert）
        4. 产出 MetricSample(target_id=server_id, metric='power_state',
                            value_text=power_state, value_num=is_running_float)
        单台 VM 失败：MetricSample(status='error')，不影响其他台
        Azure API 整体失败：抛异常，框架层捕获记录 collector_run.status='down'
        """
```

### VM 电源态映射表

```python
POWER_STATE_MAP: dict[str, tuple[str, float]] = {
    "powerstate/running":      ("Running",      1.0),
    "powerstate/stopped":      ("Stopped",      0.0),
    "powerstate/deallocated":  ("Deallocated",  0.0),
    "powerstate/starting":     ("Starting",     0.0),
    "powerstate/stopping":     ("Stopping",     0.0),
    "powerstate/deallocating": ("Deallocating", 0.0),
}
# 匹配：vm.instance_view.statuses 中 code.lower() 在 POWER_STATE_MAP 的 key
# 未匹配：("Unknown", 0.0)
```

### server_id 匹配逻辑

`collect()` 开始前调 `_gpu_repo.get_all_servers()` 获取 DB 中已注册服务器，按 `azure_vm_name` 做 key 建索引。Azure API 返回的 VM `vm.name` 与之匹配；未匹配（DB 未注册）的 VM 跳过（不产出 MetricSample）。这样做的好处：只监控用户显式注册的机器，避免越权采集。

### asyncio 包装注意

`azure-mgmt-compute` 的 `list_all()` 是同步迭代器（SDK 非 async）。推荐做法：

```python
import asyncio

def _fetch_vms_sync(client, expand):
    return list(client.virtual_machines.list_all(expand=expand))

vms = await asyncio.get_event_loop().run_in_executor(None, _fetch_vms_sync, self._client, "instanceView")
```

### register 工厂

```python
# collectors/azure/__init__.py
def register(settings: Settings, repo: Repository, gpu_repo: GpuRepository) -> None:
    if not all([settings.azure_tenant_id, settings.azure_client_id,
                settings.azure_client_secret, settings.azure_subscription_id]):
        logger.warning("Azure credentials not configured; AzureVmCollector disabled")
        return
    credential = ClientSecretCredential(...)
    client = ComputeManagementClient(credential, settings.azure_subscription_id)
    collector = AzureVmCollector(client=client, gpu_repo=gpu_repo, base_repo=repo)
    from panel.collectors.registry import register as reg_register
    reg_register(collector)
```

## mock/fixture 策略

**测试不连接真实 Azure API**，使用录制响应 fixture：

```
tests/fixtures/azure/
├── list_vms_running.json    # 1台 running VM 的 SDK response 序列化
├── list_vms_mixed.json      # running + deallocated 混合
├── list_vms_empty.json      # 空列表
└── list_vms_no_powerstate.json  # instanceView.statuses 缺 PowerState 条目
```

Fixture 格式：SDK `VirtualMachine` 对象序列化为 dict list，测试中用 `unittest.mock.patch` 或 `pytest` fixture 替换 `_client.virtual_machines.list_all`。

env 开关：若 `AZURE_INTEGRATION_TEST=1`（且四项凭证均设），则集成测试路径连接真实 Azure API（CI 默认不设，仅本地手动触发）。

### 失败保留旧值

采集失败时（异常或 status='error'/'unreachable'），`azure_vm_status` 表中旧值保留（不 upsert），仅产出带 `status='error'` 的 MetricSample，前端通过 `is_stale` 判断展示陈旧警告。

## 实现指引

1. 在 `src/panel/collectors/azure/` 下创建 `__init__.py` 和 `collector.py`。
2. `collector.py` 实现 `AzureVmCollector`，构造函数接受 `client`、`gpu_repo`、`base_repo`，三者均由 `register()` 工厂注入（便于测试时替换 mock）。
3. `_parse_power_state(statuses: list) -> tuple[str, str | None, float]` 私有方法：返回 `(display_state, raw_code, is_running_float)`。
4. `collect()` 内 `asyncio.to_thread` 包装同步 SDK 调用，设置 `timeout_seconds` 超时（`asyncio.wait_for`）。
5. 每台 VM 的 upsert：`await self._gpu_repo.upsert_vm_status(server_id, power_state, raw, is_running, now)`，单台失败 catch + 产出 `status='error'` sample。
6. 同时产出通用 MetricSample 写 `latest_snapshot`（`metric='power_state'`），供 ARCH-001 collector_run 可观测统计。
7. `register()` 工厂：四项 Azure 配置任一缺失则 warning + return，不抛异常。
8. `settings.py` 追加四个 Azure 字段（若 TASK-005 未涵盖）。

## 测试要求

- [ ] fixture `list_vms_running.json`：1台 running VM，MetricSample 正确产出，value_text="Running"，value_num=1.0
- [ ] fixture `list_vms_mixed.json`：running + deallocated 各一台，各自映射正确
- [ ] fixture `list_vms_empty.json`：返回空列表，collect() 返回空列表，不报错
- [ ] fixture `list_vms_no_powerstate.json`：instanceView 无 PowerState，产出 value_text="Unknown"
- [ ] Azure API 整体抛异常时，collect() 向上传播，框架层可捕获
- [ ] 未配置凭证时，register() 不抛异常，collector 未被注册
- [ ] mock 模式测试不需要真实 Azure 凭证
- [ ] 若设 `AZURE_INTEGRATION_TEST=1` + 真实凭证，可跑真实 API 集成测试（非 CI 强制）

## 完成标准

- [ ] `AzureVmCollector` 实现 `Collector` 协议（name/interval_seconds/timeout_seconds/collect）
- [ ] 五种电源态映射（running/stopped/deallocated/starting/stopping/deallocating/unknown）均经测试验证
- [ ] register() 工厂凭证缺失时优雅跳过
- [ ] 全部 mock 测试通过，无需真实 Azure 凭证
- [ ] `ssh_key_path` 等凭证字段不出现在任何日志输出中
