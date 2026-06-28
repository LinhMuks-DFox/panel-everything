---
id: TASK-018
title: "Azure 动态公网 IP 解析 + 只读 SP 认证对齐"
status: todo
priority: P1
architecture: ARCH-002
dependencies: [TASK-012, TASK-013]
estimated_effort: M
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

对齐用户真实 Azure 环境（A100 `mux-a100`，动态公网 IP），解决两个可用性问题：

1. **动态公网 IP**：A100 使用动态公网 IP，重启后会变，`servers` 表静态注册的 `ssh_host` 会失效。让 `AzureVmCollector` 在采集电源态的同时解析每台已注册 VM 的当前公网 IP，写入通用 `latest_snapshot`；`GpuCollector` 采集前据此动态覆盖 SSH host。
2. **非 running 跳采**：VM 非 running（deallocated/stopped/...）时跳过 SSH 采集，避免连接超时堆积。本卡**合并并取代**了原 TASK-016 中的 deallocated 跳采（见 ARCH-002 Addendum）。

认证侧确认采用只读 Service Principal（`Reader`，资源组级 scope），凭证经 env/secrets 注入——SP 的创建与落配指引在 TASK-019，本卡只确保采集器消费这些凭证并新增 `NetworkManagementClient`。

详见 ARCH-002 `## Addendum（2026-06，REQ-002 真实对齐）`。

---

## 技术规格

### 涉及文件

| 文件 | 改动 |
|------|------|
| `pyproject.toml` | 新增运行时依赖 `azure-mgmt-network` |
| `src/panel/collectors/azure/collector.py` | 构造并使用 `NetworkManagementClient`，新增 `public_ip` 解析与样本产出 |
| `src/panel/collectors/azure/__init__.py` | `register()` 注入 network client（同一 credential） |
| `src/panel/collectors/gpu/collector.py` | `SshRunner.run` 增 `host` 参数；`_collect_one` 据快照跳采 + 动态 IP 覆盖 |

**严格约束（务必遵守）**：

- **不新增 migration**：复用通用 `latest_snapshot` 表，`public_ip` 作为一条标量指标（`value_text`）。
- **不改 `db/gpu_repository.py`**：动态 IP / 电源态从通用 `latest_snapshot` 读，用既有 `Repository.get_snapshot_metric`，不走 GPU 专用表。
- **不改 `main.py`**：collector 注册流程不变（仍由 `register()` 入口）。
- **不改 `domain/models.py`**：`MetricSample` 已有 `value_text`，无需新字段。

### Azure 采集器（collectors/azure/collector.py + __init__.py）

`register()` 在已有 `ComputeManagementClient` 之外，用**同一** `ClientSecretCredential` 额外构造 `NetworkManagementClient(credential, subscription_id)`，注入 `AzureVmCollector`（构造参数或属性）。凭证来源不变（`AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET/SUBSCRIPTION_ID`，任一缺失则跳过注册）。

`collect()` 对每台已注册（有 `azure_resource_group` + `azure_vm_name`）的 VM，在产出 `power_state` 样本之外，解析当前公网 IP：

```
VM (compute.virtual_machines.get(rg, vm_name, expand="instanceView"))
  → network_profile.network_interfaces[primary or [0]]  → NIC id
  → network.network_interfaces.get(rg, nic_name)
  → ip_configurations[*].public_ip_address.id           → public IP 资源 id
  → network.public_ip_addresses.get(rg, pip_name).ip_address
```

产出：

```python
MetricSample(
    target_id=server.id,
    metric="public_ip",
    value_text=ip,            # 解析到的字符串 IP
    value_num=None,
    status="ok",
    collected_at=<now utc>,
)
```

- 与 `power_state` 样本一并 `upsert_snapshot("azure_vm", samples)`（沿用 TASK-012 写法）。
- **解析失败不致命**：单台 VM 解析 public IP 抛异常或无公网 IP 时，记 debug/warning 日志，**该 VM 不产 `public_ip` 样本**，不影响其 `power_state` 样本与其他 VM。collector 整体 status 不因此降级为 error。

### GPU 采集器（collectors/gpu/collector.py）

1. **`SshRunner.run` 增加可选 `host` 参数**：签名加 `host: str | None = None`，为 `None` 时使用 `server.ssh_host`（现状），非 `None` 时覆盖连接目标。其余 SSH 参数（port/user/key_path/known_hosts）不变。

2. **`_collect_one` 逻辑**（仅当 `server.azure_vm_name` 非空时启用，否则保持现状直接走静态 `ssh_host`）：

   ```python
   if server.azure_vm_name:
       ps = await base_repo.get_snapshot_metric("azure_vm", server.id, "power_state")
       if ps is not None and ps.value_num != 1.0:
           # 非 running → 跳采，不发起 SSH
           return [GpuSample(server_id=server.id, gpu_index=0, ...,
                             status="unreachable", value_text="vm_not_running",
                             collected_at=now)]
       pip = await base_repo.get_snapshot_metric("azure_vm", server.id, "public_ip")
       host = pip.value_text if (pip and pip.value_text) else None  # None → run() 回退 ssh_host
   else:
       host = None
   samples = await self._ssh_runner.run(server, host=host)
   ```

   - `ps is None`（尚无 Azure 快照，如 Azure collector 未启用或首轮未跑）：**不跳采**，按现状用静态 `ssh_host` 采集（保持向后兼容）。
   - `value_num != 1.0`：覆盖 deallocated/stopped/stopping/未知等所有非运行态（合并原 TASK-016 deallocated 跳采）。
   - 动态 host 仅在 running 且有 `public_ip` 时生效；否则回退 `ssh_host`。

3. **`base_repo`**：GpuCollector 需要持有通用 `Repository`（读 `latest_snapshot`）。若 TASK-013 已注入则复用；其访问通过既有 `Repository.get_snapshot_metric(collector, target_id, metric)`（ARCH-001 通用读方法），不新增 repo 方法。

> `get_snapshot_metric` 由 ARCH-001 通用 Repository 提供。若当前 `Repository` 尚无此读方法，本卡可在 `db/repository.py` 末尾 `setattr` 注入一个只读 `get_snapshot_metric(collector, target_id, metric) -> MetricRow | None`（`SELECT ... FROM latest_snapshot WHERE collector=? AND target_id=? AND metric=?`），不改既有方法签名。

---

## 实现指引

1. `pyproject.toml` 依赖区加 `azure-mgmt-network`（与 `azure-mgmt-compute`、`azure-identity` 同组），锁版本与现有 azure SDK 兼容。
2. `collectors/azure/__init__.py`：构造 credential 后同时实例化 compute + network 两个 client，传入 `AzureVmCollector`。
3. `collectors/azure/collector.py`：抽一个 `_resolve_public_ip(rg, vm) -> str | None` 私有方法（VM→NIC→public IP），用 try/except 包住，失败返回 `None`；`collect()` 拿到非 None 时追加 `public_ip` 样本。
4. `collectors/gpu/collector.py`：先改 `SshRunner.run` 签名加 `host`；再改 `_collect_one`（按上文分支）；确保 `azure_vm_name` 为空的服务器路径完全不变。
5. 通读，确认无 migration、无 `gpu_repository`、无 `main.py`、无 `models.py` 改动。

---

## 测试要求

- [ ] **network mock**：参照 `tests/.../test_azure_collector` 现有 compute mock 风格，mock `NetworkManagementClient`（`network_interfaces.get` / `public_ip_addresses.get`），断言 `collect()` 为有公网 IP 的 VM 产出 `metric="public_ip"` 且 `value_text` 等于 mock IP。
- [ ] **public_ip 解析失败不致命**：mock network 抛异常 → 该 VM 仍有 `power_state` 样本、无 `public_ip` 样本，collector status 非 error。
- [ ] **GPU 非 running 跳采**：fixture 向 `latest_snapshot` 注入 `azure_vm/<id>/power_state` `value_num=0.0` → `_collect_one` 不调用 `SshRunner.run`（mock 验证未调用），产出 `unreachable` + `value_text="vm_not_running"`。
- [ ] **GPU 动态 IP 覆盖**：注入 `power_state value_num=1.0` + `public_ip value_text="20.1.2.3"` → `SshRunner.run` 收到 `host="20.1.2.3"`。
- [ ] **回退路径**：① 无 `public_ip` 样本但 running → `host=None`（用 `ssh_host`）；② 无任何 `azure_vm` 快照（`ps is None`）→ 不跳采，用 `ssh_host`；③ `azure_vm_name` 为空的服务器 → 行为完全不变。
- [ ] **凭证不泄漏**：断言 `ssh_key_path`、`AZURE_CLIENT_SECRET` 等不出现在任何日志/异常消息/样本字段中（沿用 TASK-005 脱敏断言风格）。

---

## 完成标准

- [ ] `azure-mgmt-network` 加入 `pyproject.toml` 运行时依赖。
- [ ] `AzureVmCollector` 为每台已注册 VM 产出 `power_state` 与（可解析时）`public_ip` 两类样本，写通用 `latest_snapshot`（collector `azure_vm`）。
- [ ] `GpuCollector` 对设置了 `azure_vm_name` 的服务器：非 running 跳采（`vm_not_running`）、running 用动态 `public_ip` 覆盖 SSH host、无 IP/无快照回退静态 `ssh_host`；纯静态机行为不变。
- [ ] 无 migration、无 `gpu_repository`、无 `main.py`、无 `models.py` 改动。
- [ ] 所有测试通过；`ruff check` 零 error；凭证脱敏断言通过。
