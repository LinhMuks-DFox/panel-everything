# Panel Everything — 部署指南

> 适用范围：树莓派（arm64）容器部署，走 Tailscale 内网访问，无认证。
> 关联：REQ-002、ARCH-002（含 Addendum）、TASK-018（采集逻辑）、TASK-019（本部署交付物）。
>
> 真实环境参数（贯穿全文）：
> - subscription：`d071b64b-e5d3-4b61-9cc8-032d37c7ccb9`
> - resource group：`rg-mux-a100`
> - VM：`mux-a100`（region `japaneast`，size `Standard_NC24ads_A100_v4`）
> - VM admin 用户：`azureuser`
> - SSH 私钥（宿主）：`~/.ssh/id_ed25519`

---

## 0. 总览：要准备的三样东西

| # | 内容 | 落地方式 |
|---|------|----------|
| 1 | Azure 只读 Service Principal（Reader） | env 三项 + secrets 文件一项（见 §1） |
| 2 | SSH 私钥（连 A100 跑 `nvidia-smi`） | 只读挂载进容器（见 §3） |
| 3 | A100 服务器注册记录 | `/servers` 表单或 `scripts/seed_a100.sh`（见 §5） |

配置约定（以 `src/panel/config/settings.py` 为准）：

- 所有 env 变量前缀统一为 `PANEL_`。
- **凭证按"路径引用"注入**：env 里存的是文件路径，不存明文。运行时由 `read_secret()` 读取文件内容。
- 默认 secrets 目录 `PANEL_SECRETS_DIR=/secrets`，对应宿主 `./secrets` 只读挂载。

---

## 1. 创建 Azure 只读 Service Principal

面板只需读取 VM 电源态与网络（公网 IP），因此使用 **`Reader` 角色**、scope **收敛到资源组级**（非订阅级，最小权限）。`Reader` 无法启停/修改 VM，满足 REQ-002 只读约束。

```bash
az ad sp create-for-rbac \
  --name panel-everything-reader \
  --role Reader \
  --scopes /subscriptions/d071b64b-e5d3-4b61-9cc8-032d37c7ccb9/resourceGroups/rg-mux-a100
```

命令输出形如：

```json
{
  "appId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "displayName": "panel-everything-reader",
  "password": "~secret~value~",
  "tenant": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

### 输出字段 → 面板配置映射

| 命令输出字段 | 面板配置项 | 落配方式 |
|--------------|-----------|----------|
| `tenant`   | `PANEL_AZURE_TENANT_ID`         | env（`.env`） |
| `appId`    | `PANEL_AZURE_CLIENT_ID`         | env（`.env`） |
| `password` | `PANEL_AZURE_CLIENT_SECRET_FILE` | **secrets 文件**：把 `password` 写入 `./secrets/azure_client_secret`，env 里填该文件路径 `/secrets/azure_client_secret`（**不写明文 env**） |
| （固定）   | `PANEL_AZURE_SUBSCRIPTION_ID` = `d071b64b-e5d3-4b61-9cc8-032d37c7ccb9` | env（`.env`） |

> 注意字段命名：env 是 `PANEL_AZURE_CLIENT_SECRET_FILE`（**File 结尾，存路径**），不是 `PANEL_AZURE_CLIENT_SECRET`。
> 四项缺任一，`Settings.azure_configured` 为 False，`AzureVmCollector` 会被禁用（register 跳过并打 warning）。

把 `password` 落入 secrets 文件（注意权限与不带换行）：

```bash
mkdir -p ./secrets
# 将上面 JSON 的 password 值写入文件（用引号包住，避免特殊字符被 shell 解释）
printf '%s' '~secret~value~' > ./secrets/azure_client_secret
chmod 600 ./secrets/azure_client_secret
```

对应写入 `.env`（详见 §2）：

```dotenv
PANEL_AZURE_TENANT_ID=<tenant>
PANEL_AZURE_CLIENT_ID=<appId>
PANEL_AZURE_CLIENT_SECRET_FILE=/secrets/azure_client_secret
PANEL_AZURE_SUBSCRIPTION_ID=d071b64b-e5d3-4b61-9cc8-032d37c7ccb9
```

### 撤销 / 轮换

```bash
# 列出
az ad sp list --display-name panel-everything-reader --query "[].appId" -o tsv
# 删除（轮换前先建新的再删旧的）
az ad sp delete --id <appId>
```

---

## 2. 配置 `.env`

复制示例并填值：

```bash
cp .env.example .env
$EDITOR .env
```

至少需要确认/填写：

- `PANEL_AZURE_*` 四项（见 §1）。
- `PANEL_SSH_KEY_PATH=/secrets/id_ed25519`（容器内私钥路径，见 §3）。
- `PANEL_INGEST_TOKEN`（可选，工作站 Reporter 上报 AI 用量用；留空则 ingest 端点不鉴权）。
- `PANEL_HISTORY_RETENTION_DAYS`（可选，默认 30）。

各项含义见 `.env.example` 注释。

---

## 3. SSH 私钥挂载

GPU 采集器（`GpuCollector`）用 `asyncssh` 连到 A100 跑 `nvidia-smi`。私钥**只挂路径、不进 DB、不进 API 响应**：

1. 把宿主 `~/.ssh/id_ed25519` 复制进 `./secrets/`（保持只读、最小权限）：

   ```bash
   cp ~/.ssh/id_ed25519 ./secrets/id_ed25519
   chmod 600 ./secrets/id_ed25519
   ```

2. `docker-compose.yml` 把 `./secrets` 以**只读**方式挂入容器 `/secrets`（见 §4 的 compose）。
3. 容器内私钥路径即 `/secrets/id_ed25519`，对应：
   - 全局默认：`PANEL_SSH_KEY_PATH=/secrets/id_ed25519`（`.env`）。
   - 每台服务器注册时的 `ssh_key_path` 字段（见 §5），A100 用同一路径。

> 主机指纹校验：当前 `GpuCollector` 使用 `known_hosts=None`（等价 `StrictHostKeyChecking=no`）。这是 ARCH-001 裁定的首期取舍——内网 Tailscale 隔离下可接受，P3 再增强为加载 `known_hosts`。

> 路径约定说明：本项目实际 `PANEL_SECRETS_DIR` 默认 `/secrets`，故全部 secrets（Azure secret 文件 + SSH 私钥）统一放在容器内 `/secrets/` 下。ARCH-002 早期示例写的 `/run/secrets/...` 是等价的另一种挂载点命名，本部署以 `/secrets` 为准，保持与 `settings.py` 默认一致。

---

## 4. docker compose 部署

`docker-compose.yml` 已包含 panel 服务、`./data` SQLite 卷、`./secrets` 只读挂载与健康检查。确认 secrets 目录就绪后：

```bash
# 1. 准备目录与凭证（见 §1、§3）
mkdir -p ./secrets ./data
# ./secrets/azure_client_secret  （Azure SP password）
# ./secrets/id_ed25519           （SSH 私钥）

# 2. 构建并启动
docker compose up -d --build

# 3. 查看健康状态与日志
docker compose ps
docker compose logs -f panel
```

健康检查命中 `http://localhost:8080/healthz`。启动后面板监听容器 `8080`，映射到宿主 `8080`。

---

## 5. 预置注册 A100（`mux-a100`）

注册一台被监控服务器有两条路径，二选一。

### 5a. Web 表单（推荐手动）

浏览器打开 `http://<树莓派 Tailscale 名或 IP>:8080/servers`，按下表填写后提交：

| 字段 | 值 |
|------|----|
| name | `mux-a100` |
| azure_resource_group | `rg-mux-a100` |
| azure_vm_name | `mux-a100` |
| ssh_user | `azureuser` |
| ssh_key_path | `/secrets/id_ed25519` |
| ssh_port | `22` |
| has_gpu | ✅ true |
| ssh_host | 留空（running 时由 Azure 解析的动态公网 IP 覆盖） |
| notes | `A100 80GB (Standard_NC24ads_A100_v4, japaneast)` |

> 因设置了 `azure_vm_name`，`AzureVmCollector` 会把 VM 当前公网 IP 写入 `latest_snapshot(metric="public_ip")`，`GpuCollector` running 时用它作连接 host 覆盖 `ssh_host`，所以 `ssh_host` 无需手填真实 IP。

### 5b. seed 脚本（自动化 / 可重复）

```bash
# 默认 PANEL_URL=http://localhost:8080
scripts/seed_a100.sh

# 或显式指定面板地址（在树莓派以外的机器上跑时）
PANEL_URL=http://raspberrypi:8080 scripts/seed_a100.sh
```

脚本对 `POST /api/v1/servers` 提交与上表一致的请求体。`name` 在 DB 上是 UNIQUE，重复执行时 API 返回 `409 Conflict`，脚本会识别并提示"已注册"而非报错（幂等友好）。

---

## 6. Tailscale 访问

面板**不做认证**，仅依赖 Tailscale 内网做边界。

- 树莓派需已加入 tailnet（`tailscale up`）。
- 各终端（Kindle / iPhone / iPad / 笔记本）安装 Tailscale 客户端并登入同一 tailnet。
- 访问 `http://<树莓派 MagicDNS 名，如 raspberrypi>:8080/` 或 `http://<树莓派 100.x.y.z>:8080/`。
- **不要把 8080 暴露到公网**（无认证）。如需公网，请在 Tailscale Serve / 反向代理层加认证，超出本期范围。

---

## 7. 真机验收 checklist

按顺序逐项打勾，覆盖"创建 SP → 配 env → compose up → seed → 启动 A100 → 面板显示 Running + IP + GPU"：

- [ ] **创建 SP**：执行 §1 的 `az ad sp create-for-rbac`，记录 `tenant`/`appId`/`password`。
- [ ] **落 secret 文件**：`password` 写入 `./secrets/azure_client_secret`（`chmod 600`）。
- [ ] **挂 SSH 私钥**：`~/.ssh/id_ed25519` 复制到 `./secrets/id_ed25519`（`chmod 600`）。
- [ ] **配 env**：`.env` 填好四项 Azure（含 `PANEL_AZURE_CLIENT_SECRET_FILE=/secrets/azure_client_secret`）+ `PANEL_SSH_KEY_PATH=/secrets/id_ed25519`。
- [ ] **compose up**：`docker compose up -d --build`，`docker compose ps` 显示 panel `healthy`。
- [ ] **collector 启用**：`docker compose logs panel | grep -i azure` 无 "disabled" warning（说明四项 Azure 齐全）。
- [ ] **seed**：`scripts/seed_a100.sh` 返回 201（首次）或 409 提示已注册（重复）。
- [ ] **启动 A100**：在 Azure 侧 `az vm start -g rg-mux-a100 -n mux-a100`（面板自身只读，不能启停）。
- [ ] **面板显示 Running**：等一个 `AzureVmCollector` 周期（≤5min），`/` 页 `mux-a100` 卡片电源态 = Running。
- [ ] **面板显示 IP**：VM running 后 `latest_snapshot` 出现 `public_ip`（可经 `/api/v1/dashboard/azure` 间接验证 GPU 已连上）。
- [ ] **面板显示 GPU**：等一个 `GpuCollector` 周期（≤1min），卡片出现 GPU 利用率 / 显存 / 温度（说明 SSH + nvidia-smi 链路通）。
- [ ] **凭证不泄露**：`GET /api/v1/servers` 响应中**不含** `ssh_key_path` 字段。
