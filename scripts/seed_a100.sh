#!/usr/bin/env bash
#
# seed_a100.sh — 预置注册 A100 (mux-a100) 到 Panel Everything (TASK-019)。
#
# 通过 POST {PANEL_URL}/api/v1/servers 注册一台被监控服务器。字段与 ServerIn
# (src/panel/domain/models.py) 一致。name 在 DB 上 UNIQUE：重复执行时 API 返回
# 409 Conflict，本脚本识别为"已注册"并以 0 退出（幂等友好），不视为错误。
#
# 用法:
#   scripts/seed_a100.sh
#   PANEL_URL=http://raspberrypi:8080 scripts/seed_a100.sh
#
# 环境变量:
#   PANEL_URL       面板基础地址 (默认 http://localhost:8080)
#   SSH_KEY_PATH    容器内 SSH 私钥路径 (默认 /secrets/id_ed25519)
#
# 真实环境: subscription d071b64b-e5d3-4b61-9cc8-032d37c7ccb9 / rg-mux-a100 /
#           VM mux-a100 / japaneast / Standard_NC24ads_A100_v4 / admin azureuser

set -euo pipefail

PANEL_URL="${PANEL_URL:-http://localhost:8080}"
SSH_KEY_PATH="${SSH_KEY_PATH:-/secrets/id_ed25519}"
ENDPOINT="${PANEL_URL%/}/api/v1/servers"

# ServerIn 请求体。ssh_host 留空：running 时由 AzureVmCollector 解析的动态公网 IP
# 覆盖 (因设置了 azure_vm_name)。ssh_key_path 仅写入 DB，不会在响应中回传。
read -r -d '' PAYLOAD <<JSON || true
{
  "name": "mux-a100",
  "azure_resource_group": "rg-mux-a100",
  "azure_vm_name": "mux-a100",
  "ssh_user": "azureuser",
  "ssh_key_path": "${SSH_KEY_PATH}",
  "ssh_port": 22,
  "has_gpu": true,
  "notes": "A100 80GB (Standard_NC24ads_A100_v4, japaneast). 动态公网 IP, running 时由 azure_vm 采集器覆盖 ssh_host."
}
JSON

echo "POST ${ENDPOINT}"
echo "  name=mux-a100  azure_vm_name=mux-a100  has_gpu=true  ssh_key_path=${SSH_KEY_PATH}"

# 把响应体与 HTTP 状态码一并取回 (状态码追加在最后一行)。
HTTP_BODY="$(mktemp)"
trap 'rm -f "${HTTP_BODY}"' EXIT

STATUS_CODE="$(
  curl -sS -o "${HTTP_BODY}" -w '%{http_code}' \
    -X POST "${ENDPOINT}" \
    -H 'Content-Type: application/json' \
    -d "${PAYLOAD}"
)"

BODY="$(cat "${HTTP_BODY}")"

case "${STATUS_CODE}" in
  201)
    echo "OK 201 Created — mux-a100 已注册。"
    echo "${BODY}"
    ;;
  409)
    echo "OK 409 Conflict — mux-a100 已存在 (UNIQUE name)，无需重复注册。幂等跳过。"
    exit 0
    ;;
  000)
    echo "ERROR 无法连接 ${ENDPOINT}。确认面板已启动且 PANEL_URL 正确 (当前: ${PANEL_URL})。" >&2
    exit 1
    ;;
  *)
    echo "ERROR HTTP ${STATUS_CODE} — 注册失败。" >&2
    echo "${BODY}" >&2
    exit 1
    ;;
esac
