# 手把手：创建 Azure 只读 SP 并点亮 Azure / GPU 监控

> 这是一份**面向操作**的逐步清单,贴合你本机当前状态(已 `az login`、SSH 私钥已就位)。
> 背景原理见 `DEPLOY.md §1/§3`,这里只讲"现在该敲什么"。

## 现状(脚本已替你做好的部分)

- ✅ `secrets/id_ed25519` —— SSH 私钥已从 `~/.ssh/id_ed25519` 拷入(连 A100 跑 nvidia-smi)。
- ✅ `.env` 已填:`PANEL_AZURE_TENANT_ID`、`PANEL_AZURE_SUBSCRIPTION_ID`、`PANEL_AZURE_CLIENT_SECRET_FILE`、`PANEL_SSH_KEY_PATH`。
- ✅ A100(`mux-a100`)已注册到面板(`scripts/seed_a100.sh`)。
- ⬜ **只差两样**(必须你本人授权 Azure 才能拿到):
  - `PANEL_AZURE_CLIENT_ID`(SP 的 appId)
  - `secrets/azure_client_secret`(SP 的 password)

> 为什么脚本不能自动做这步:创建 Service Principal + 分配 RBAC 角色属于**身份/权限变更**,安全策略要求你亲手执行。

---

## 什么是 SP,为什么要它

面板要读 A100 的电源态和公网 IP,得有一个能访问你 Azure 订阅的"机器账号"。直接用你的 `az login` 个人登录态不行(那是交互式、会过期、权限过大)。所以创建一个**只读**的 **Service Principal(SP)**:
- 角色 `Reader`(只能看,**不能**启停/改 VM —— 符合面板"只读监控"定位)。
- 范围收敛到资源组 `rg-mux-a100`(最小权限,不给整个订阅)。

它会给你三样东西:`appId`(=client_id)、`password`(=client_secret)、`tenant`。

---

## Step 1 — 确认 az 已登录到正确订阅

```bash
az account show --query '{user:user.name, sub:id}' -o tsv
```
应显示 `mux.xliu@keio.jp` 和 `d071b64b-e5d3-4b61-9cc8-032d37c7ccb9`。
若不是,先 `az login`。

## Step 2 — 创建只读 SP(一条命令)

```bash
az ad sp create-for-rbac \
  --name panel-everything-reader \
  --role Reader \
  --scopes /subscriptions/d071b64b-e5d3-4b61-9cc8-032d37c7ccb9/resourceGroups/rg-mux-a100
```

输出形如(**password 只显示这一次,务必当场记下**):
```json
{
  "appId":    "11111111-2222-3333-4444-555555555555",
  "password": "abc~secret~xyz",
  "tenant":   "789acfad-6fe0-4cdb-975a-04ab117882ae"
}
```

## Step 3 — 把 password 写入 secret 文件(不带换行)

在仓库根目录 `panel-everything/`：
```bash
printf '%s' '上一步输出的 password 原样粘进这对引号' > secrets/azure_client_secret
chmod 600 secrets/azure_client_secret
```
> 用单引号包住,避免 password 里的特殊字符被 shell 解释。

## Step 4 — 把 appId 填进 .env

编辑 `panel-everything/.env`,找到这行：
```dotenv
# PANEL_AZURE_CLIENT_ID=<待补>  ← 创建 SP 后填 appId(见下方说明)
```
改成(去掉 `#`,填上 Step 2 的 appId)：
```dotenv
PANEL_AZURE_CLIENT_ID=11111111-2222-3333-4444-555555555555
```
> `tenant`/`subscription`/`secret_file 路径` 已经替你填好,不用动。

## Step 5 — 重启面板让配置生效

```bash
./start.sh           # 幂等;会重新读取 .env 与 secrets
```

## Step 6 — 验证 Azure 采集器已启用

```bash
make logs            # 或 docker compose logs panel | grep -i azure
```
- 之前会看到:`Azure credentials not configured; AzureVmCollector disabled`
- 配好后应**不再有**这条 disabled,而是出现 Azure 采集器周期运行的日志。

然后浏览器打开 **http://localhost:8080** ,`mux-a100` 卡片会在一个采集周期内(≤5 分钟)显示电源态;VM running 时进一步显示公网 IP 和 GPU 利用率。

---

## 常见问题

- **VM 是关机的 → 看不到 GPU**:面板只读,不能启动 VM。要看 GPU 数据,先用你的 azure-vm 脚本
  `~/code_workspace/gnn-based-spatial-vc/azure-vm/start_vm.sh`(或 `az vm start -g rg-mux-a100 -n mux-a100`)把它开起来。
- **想换/撤销 SP**:
  ```bash
  az ad sp list --display-name panel-everything-reader --query "[].appId" -o tsv   # 查
  az ad sp delete --id <appId>                                                      # 删
  ```
- **password 忘了**:不用重建 SP,重置凭证即可拿新 password:
  ```bash
  az ad sp credential reset --id <appId> --query password -o tsv
  ```
  把新值写回 `secrets/azure_client_secret`,再 `./start.sh`。
- **不想配 Azure**:跳过即可。面板照常起,只是 Azure/GPU 卡片显示"未配置/无数据",其余功能不受影响。
