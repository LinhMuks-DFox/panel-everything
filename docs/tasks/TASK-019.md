---
id: TASK-019
title: "只读 SP 创建指引 + A100 预置注册 + 部署文档"
status: todo
priority: P1
architecture: ARCH-002
dependencies: [TASK-011, TASK-018]
estimated_effort: S
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

把 ARCH-002 / TASK-018 的设计落到用户真实 A100 环境，使面板开箱可监控 `mux-a100`。本卡是**部署/运维交付物**（操作指引 + 预置注册 + 部署文档），不写采集逻辑代码（采集逻辑见 TASK-018）。

包含三件事：① 只读 Service Principal 创建指引；② 预置 A100 服务器注册；③ 部署文档补充 secrets 卷挂载 + SP 说明 + SSH 私钥挂载。

真实环境参数：subscription `d071b64b-e5d3-4b61-9cc8-032d37c7ccb9`、resource group `rg-mux-a100`、VM `mux-a100`、region `japaneast`、size `Standard_NC24ads_A100_v4`、admin `azureuser`、SSH key `~/.ssh/id_ed25519`、SSH 选项 `StrictHostKeyChecking=no`。

---

## 技术规格

### ① 只读 Service Principal 创建指引

文档化以下步骤（写入部署文档，见 ③）：

```bash
# 创建资源组级只读 SP（最小权限）
az ad sp create-for-rbac \
  --name panel-everything-reader \
  --role Reader \
  --scopes /subscriptions/d071b64b-e5d3-4b61-9cc8-032d37c7ccb9/resourceGroups/rg-mux-a100
```

输出包含 `tenant`、`appId`、`password`，分别落配：

| 命令输出字段 | 配置项 | 落配方式 |
|--------------|--------|----------|
| `tenant`   | `PANEL_AZURE_TENANT_ID`   | env |
| `appId`    | `PANEL_AZURE_CLIENT_ID`   | env |
| `password` | `PANEL_AZURE_CLIENT_SECRET` | **secrets 文件**（不进明文 env） |
| （固定）   | `PANEL_AZURE_SUBSCRIPTION_ID` = `d071b64b-e5d3-4b61-9cc8-032d37c7ccb9` | env |

强调：`Reader` 角色只读，无法启停/修改 VM，满足 REQ-002 约束。scope 收敛到资源组级而非订阅级。

### ② 预置 A100 服务器注册

通过 `/servers` Web 表单（TASK-024）或 seed 脚本预置一台服务器，字段：

| 字段 | 值 |
|------|----|
| `name` | `mux-a100` |
| `azure_resource_group` | `rg-mux-a100` |
| `azure_vm_name` | `mux-a100` |
| `ssh_user` | `azureuser` |
| `ssh_key_path` | 指向容器内挂载的私钥（如 `/run/secrets/ssh_keys/id_ed25519`） |
| `ssh_port` | `22` |
| `has_gpu` | `true` |
| `ssh_host` | 可留空 / 占位——running 时由 TASK-018 动态 `public_ip` 覆盖；作为无快照时的回退值 |

> 设置了 `azure_vm_name` 后，TASK-018 会用 Azure 解析的动态公网 IP 覆盖 SSH host，因此 `ssh_host` 无需手填真实 IP。

### ③ 部署文档补充

在部署文档（README / docs 部署章节 / compose 注释）补：

- **SP 说明**：上文 ① 的创建命令与四个 env/secret 的对应关系。
- **secrets 卷挂载**：`PANEL_AZURE_CLIENT_SECRET` 走 secrets 文件挂载（如 `./secrets/azure_client_secret:/run/secrets/azure_client_secret:ro`），不写明文 compose env。
- **SSH 私钥挂载**：`~/.ssh/id_ed25519` 只读挂载进容器（如 `./secrets/ssh_keys:/run/secrets/ssh_keys:ro`），`ssh_key_path` 指向挂载路径；SSH 选项 `StrictHostKeyChecking=no`（与现状 `known_hosts=None` 一致，延续 P3 遗留）。

---

## 实现指引

1. 在部署文档中新增「Azure 只读 SP 配置」小节，照搬 ① 的命令与落配表。
2. 提供 seed/预置 A100 注册的具体操作：优先描述 `/servers` 表单填写，附 seed 脚本/请求体（按 TASK-011 的 `ServerIn` 字段）作为可选自动化路径。
3. 在 compose 片段补 secrets + SSH key 两处只读挂载与对应 env，与 ARCH-002 部署方案保持一致（注意 env 前缀已统一为 `PANEL_`）。
4. 不写采集器/路由代码——本卡为文档与配置交付物。

---

## 测试要求

- [ ] 文档 review：SP 命令 scope、四个配置项映射、secrets/SSH 挂载路径自洽，与 ARCH-002 / TASK-018 一致。
- [ ] 预置注册体能被 `/servers` 表单 / API 接受（字段名与 `ServerIn` 一致），`ssh_key_path` 不回传（沿用 TASK-011 断言）。
- [ ] （可选）seed 脚本幂等：重复执行不重复插入 `mux-a100`（`name` UNIQUE）。

---

## 完成标准

- [ ] 部署文档含只读 SP 创建命令（资源组级 scope）+ 四个配置项落配说明。
- [ ] A100（`mux-a100`）预置注册路径明确（表单或 seed），`azure_vm_name=mux-a100`、`has_gpu=true`。
- [ ] 部署文档补全 secrets 卷挂载 + SSH 私钥挂载说明。
- [ ] 文档与 ARCH-002 Addendum / TASK-018 无矛盾。
