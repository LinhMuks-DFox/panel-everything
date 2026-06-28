#!/usr/bin/env bash
#
# start.sh — Panel Everything 傻瓜式一键启动。
#
# 一条命令把面板在树莓派(arm64)或本机跑起来:前置检查 → 首次自举(.env/secrets)
# → docker compose 构建启动 → 轮询 /healthz 直到就绪 → (可选)预置 A100 → 打印访问地址。
#
# 对缺失配置优雅降级:不配 Azure / Tailscale / SSH 私钥也能起,相关监控自动禁用。
# 可重复执行(幂等):compose up -d 幂等;seed 已存在返回 409 视为 OK。
#
# 用法:
#   ./start.sh              # 构建并启动,等待就绪
#   ./start.sh --seed       # 启动后预置注册 A100 (mux-a100)
#   ./start.sh --no-build   # 跳过镜像重建(已构建过时更快)
#   ./start.sh --timeout 120  # 自定义健康检查超时秒数(默认 90)
#   ./start.sh --help       # 显示帮助
#
# 退出码: 0 成功;非 0 表示前置检查失败 / 构建失败 / 就绪超时。

set -euo pipefail

# ───────────────────────── 路径 & 常量 ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
SECRETS_DIR="${SCRIPT_DIR}/secrets"
DATA_DIR="${SCRIPT_DIR}/data"
SEED_SCRIPT="${SCRIPT_DIR}/scripts/seed_a100.sh"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

DEFAULT_PORT=8080
HEALTH_TIMEOUT=90      # 健康检查总超时(秒)
DO_SEED=0
DO_BUILD=1

# Apple Silicon + colima 默认的 macOS Virtualization.Framework(VZ)后端,在 import
# asyncssh(其 cryptography/OpenSSL 原生初始化)时会触发 SIGILL(退出码 132),导致
# 容器反复重启、面板起不来。改用 QEMU 后端可规避。脚本会在该场景下自动启用一个
# 专用 colima profile(QEMU),不动用户已有的默认 colima。树莓派/Linux 原生 arm64
# 无此问题,完全不受影响。
COLIMA_QEMU_PROFILE="panel-qemu"

# ───────────────────────── 日志 ─────────────────────────
# 仅在连接到 TTY 时着色,管道/重定向时退化为纯文本(对 e-ink/CI 友好)。
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'
else
  C_RESET=''; C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_BOLD=''
fi

log()  { printf '%s[panel]%s %s\n'        "${C_BLUE}"   "${C_RESET}" "$*"; }
ok()   { printf '%s[panel] ✓%s %s\n'      "${C_GREEN}"  "${C_RESET}" "$*"; }
warn() { printf '%s[panel] ⚠%s %s\n'      "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
err()  { printf '%s[panel] ✗%s %s\n'      "${C_RED}"    "${C_RESET}" "$*" >&2; }
step() { printf '\n%s[panel] ▶ %s%s\n'    "${C_BOLD}"   "$*" "${C_RESET}"; }

# ───────────────────────── 帮助 ─────────────────────────
usage() {
  cat <<EOF
${C_BOLD}Panel Everything — 一键启动${C_RESET}

用法:
  ./start.sh [选项]

选项:
  --seed              就绪后预置注册 A100 (调用 scripts/seed_a100.sh)
  --no-build          跳过镜像重建(用已有镜像启动,更快)
  --timeout <秒>      健康检查就绪超时,默认 ${HEALTH_TIMEOUT}
  -h, --help          显示本帮助

行为概述:
  1. 检查 docker 与 docker compose(v2) 是否可用
  2. 首次运行自举:无 .env 则从 .env.example 复制;无 secrets/ 则创建(700)
  3. docker compose up -d --build 构建并启动
  4. 轮询 http://localhost:<PANEL_PORT>/healthz 直到 200 或超时
  5. (--seed) 注册 A100;最后打印本机 / Tailscale 访问地址与后续命令

不配 Azure / Tailscale / SSH 私钥也能起 —— 相关监控会自动禁用(优雅降级)。
重复执行安全(幂等)。详见 docs/deployment/DEPLOY.md。
EOF
}

# ───────────────────────── 参数解析 ─────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --seed)      DO_SEED=1; shift ;;
    --no-build)  DO_BUILD=0; shift ;;
    --timeout)
      if [ $# -lt 2 ] || ! printf '%s' "${2:-}" | grep -qE '^[0-9]+$'; then
        err "--timeout 需要一个正整数(秒)。"; exit 2
      fi
      HEALTH_TIMEOUT="$2"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)
      err "未知参数: $1"; echo; usage; exit 2 ;;
  esac
done

# ───────────────────────── 1. 前置检查 ─────────────────────────
step "1/5 前置检查"

if ! command -v docker >/dev/null 2>&1; then
  err "未找到 docker。请先安装 Docker Engine / Docker Desktop:"
  err "  - 树莓派(Debian/Raspberry Pi OS): curl -fsSL https://get.docker.com | sh"
  err "  - macOS / Windows: https://www.docker.com/products/docker-desktop/"
  exit 1
fi

# docker compose v2 子命令(注意:不是旧的 docker-compose 独立二进制)。
if ! docker compose version >/dev/null 2>&1; then
  err "未找到 'docker compose' (v2) 子命令。"
  err "  请升级到 Docker Engine 20.10.13+ / Docker Desktop,或安装 compose v2 插件:"
  err "  https://docs.docker.com/compose/install/"
  err "  (旧的独立 'docker-compose' 二进制不被本脚本使用)"
  exit 1
fi

# daemon 必须在跑,否则后续 build/up 会以晦涩错误失败。
# macOS 没有系统级 docker daemon,需要一个后端 VM(colima / Docker Desktop / OrbStack)。
# 这里在守护进程不可用时,尽量"自动拉起"后端再重试,真起不来才退出并给针对性提示。
# 是否为 Apple Silicon(arm64 Mac)。该平台 colima 默认 VZ 后端有 asyncssh SIGILL 问题。
is_apple_silicon() {
  [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]
}

# Apple Silicon 上:启动专用 QEMU profile 并把 docker context 切过去。
# 已存在则复用,未存在则创建(需要 qemu-img;缺失时返回 1 让调用方提示)。
start_colima_qemu() {
  if ! command -v qemu-img >/dev/null 2>&1; then
    return 1   # 缺 QEMU,交调用方提示 'brew install qemu'。
  fi
  if colima status --profile "${COLIMA_QEMU_PROFILE}" >/dev/null 2>&1; then
    log "复用已有 QEMU 后端(profile ${COLIMA_QEMU_PROFILE})。"
  else
    log "Apple Silicon 检测:用 QEMU 后端启动 colima(规避 VZ 的 asyncssh SIGILL)…"
    log "  首次会创建 VM,需要几分钟,请耐心。"
    colima start --profile "${COLIMA_QEMU_PROFILE}" \
      --arch aarch64 --vm-type qemu --cpu 2 --memory 4 || return 1
  fi
  # 把 docker context 切到该 profile(colima 命名为 colima-<profile>)。
  docker context use "colima-${COLIMA_QEMU_PROFILE}" >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1
}

try_start_daemon() {
  # colima:命令行后端,可由脚本直接拉起(无需手动开 App)。
  if command -v colima >/dev/null 2>&1; then
    # Apple Silicon:优先走 QEMU profile 规避 VZ 的 SIGILL。
    if is_apple_silicon; then
      if start_colima_qemu; then
        return 0
      fi
      # QEMU 路线没成(多半缺 qemu),提示后退回默认 colima(可能仍会 SIGILL,但至少能起 daemon)。
      warn "未能启用 QEMU 后端(可能未安装 QEMU)。Apple Silicon 上建议: brew install qemu 后重跑 ./start.sh"
      warn "  否则面板容器可能因 VZ 后端的 asyncssh SIGILL 反复重启。"
    fi
    if colima status >/dev/null 2>&1; then
      return 0   # 已在跑,docker info 仍失败多半是沙箱/权限,交给调用方判断。
    fi
    log "检测到 colima(未运行),尝试自动启动: colima start …(首次会创建 VM,稍候)"
    if colima start; then
      return 0
    fi
    return 1
  fi
  # Docker Desktop(macOS):无 CLI 启动方式,用 open 拉起 App 后轮询等待。
  if [ "$(uname -s)" = "Darwin" ] && [ -d "/Applications/Docker.app" ]; then
    log "检测到 Docker Desktop(未运行),尝试自动启动并等待 …"
    open -a Docker || true
    local i=0
    while [ "${i}" -lt 60 ]; do
      docker info >/dev/null 2>&1 && return 0
      sleep 2; i=$((i + 2))
    done
    return 1
  fi
  # OrbStack(macOS):同理用 open 拉起。
  if [ "$(uname -s)" = "Darwin" ] && [ -d "/Applications/OrbStack.app" ]; then
    log "检测到 OrbStack(未运行),尝试自动启动并等待 …"
    open -a OrbStack || true
    local i=0
    while [ "${i}" -lt 60 ]; do
      docker info >/dev/null 2>&1 && return 0
      sleep 2; i=$((i + 2))
    done
    return 1
  fi
  # Linux:尝试 systemd 启动 docker 服务(可能需要 sudo)。
  if [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
    log "尝试启动 docker 服务: systemctl start docker(可能需要 sudo)…"
    systemctl start docker 2>/dev/null || sudo systemctl start docker 2>/dev/null || true
    docker info >/dev/null 2>&1 && return 0
    return 1
  fi
  return 1
}

if ! docker info >/dev/null 2>&1; then
  warn "Docker 守护进程当前不可用,尝试自动拉起后端 …"
  try_start_daemon || true
  if ! docker info >/dev/null 2>&1; then
    err "Docker 已安装但守护进程未运行 / 当前用户无权限访问 / 当前 shell 被沙箱限制。"
    err "  当前 docker context: $(docker context show 2>/dev/null || echo '?')"
    err "  - colima 用户(macOS 命令行后端): 运行 'colima start' 后重试 ./start.sh"
    err "  - Docker Desktop / OrbStack(macOS): 打开对应 App,等右上角图标变绿后重试"
    err "  - Linux: sudo systemctl start docker;权限问题 'sudo usermod -aG docker \$USER' 后重新登录"
    err "  (若 'colima status' 显示 running 但这里仍失败,多半是当前 shell 沙箱挡住了 docker.sock,换非沙箱终端重试)"
    exit 1
  fi
fi

# daemon 已就绪。但 Apple Silicon + colima 默认 VZ 后端会让容器因 asyncssh SIGILL
# 反复重启。若当前正用着 colima 的 VZ 后端,主动切到/启动 QEMU profile 规避。
if is_apple_silicon && command -v colima >/dev/null 2>&1; then
  CUR_CTX="$(docker context show 2>/dev/null || true)"
  # 当前 context 不是我们的 QEMU profile,且看起来是 colima(默认/其它 VZ profile)时才介入。
  if [ "${CUR_CTX}" != "colima-${COLIMA_QEMU_PROFILE}" ] && printf '%s' "${CUR_CTX}" | grep -qi 'colima'; then
    # 仅当默认 colima 确实是 VZ(vz/vmType: vz)时才切;QEMU 单 profile 用户无需打扰。
    BACKEND="$(colima status 2>&1 | grep -ioE 'Virtualization.Framework|qemu|vz' | head -1 || true)"
    if printf '%s' "${BACKEND}" | grep -qiE 'Virtualization.Framework|vz'; then
      warn "检测到 Apple Silicon 上 colima 正用 VZ 后端 —— 已知会让面板容器因 asyncssh SIGILL 反复重启。"
      if start_colima_qemu; then
        ok "已切换到 QEMU 后端(profile ${COLIMA_QEMU_PROFILE}),规避该问题。"
      else
        warn "未能启用 QEMU 后端。请 'brew install qemu' 后重跑 ./start.sh;否则容器可能反复重启。"
      fi
    fi
  fi
fi

if [ ! -f "${COMPOSE_FILE}" ]; then
  err "未找到 ${COMPOSE_FILE}。请在仓库根目录运行 ./start.sh。"
  exit 1
fi
ok "docker / docker compose(v2) / daemon 就绪"

# ───────────────────────── 2. 首次运行自举 ─────────────────────────
step "2/5 首次运行自举(.env / secrets / data)"

# 2a. .env —— 无则从示例复制。
if [ -f "${ENV_FILE}" ]; then
  ok ".env 已存在,沿用现有配置"
else
  if [ ! -f "${ENV_EXAMPLE}" ]; then
    err "缺少 .env 且找不到 .env.example,无法自举配置。"
    exit 1
  fi
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  ok "已从 .env.example 生成 .env"
  log "  可按需编辑 .env;不配 Azure / Tailscale 也能起,相关监控会自动禁用。"
fi

# 2b. data/ —— SQLite 卷挂载点(compose: ./data:/data)。
if [ ! -d "${DATA_DIR}" ]; then
  mkdir -p "${DATA_DIR}"
  ok "已创建 data/ (SQLite 数据卷)"
fi

# 2c. secrets/ —— 凭证只读挂载点(compose: ./secrets:/secrets:ro),权限 700。
if [ ! -d "${SECRETS_DIR}" ]; then
  mkdir -p "${SECRETS_DIR}"
  chmod 700 "${SECRETS_DIR}"
  ok "已创建 secrets/ (权限 700,凭证只读挂载源)"
else
  chmod 700 "${SECRETS_DIR}" 2>/dev/null || true
  ok "secrets/ 已存在"
fi

# 2d. 凭证存在性检查 —— 缺失仅 warning,绝不阻断启动(优雅降级)。
AZURE_SECRET="${SECRETS_DIR}/azure_client_secret"
SSH_KEY="${SECRETS_DIR}/id_ed25519"
DEGRADED=0

if [ ! -f "${AZURE_SECRET}" ]; then
  warn "未发现 secrets/azure_client_secret —— Azure VM 监控将不可用(采集器会跳过并 warning)。"
  warn "    如需启用,见 docs/deployment/DEPLOY.md §1(创建只读 Service Principal)。"
  DEGRADED=1
else
  ok "secrets/azure_client_secret 已就位"
fi

if [ ! -f "${SSH_KEY}" ]; then
  warn "未发现 secrets/id_ed25519 —— GPU(nvidia-smi over SSH)采集将不可用。"
  warn "    如需启用,见 docs/deployment/DEPLOY.md §3(挂载 SSH 私钥)。"
  DEGRADED=1
else
  ok "secrets/id_ed25519 已就位"
fi

if [ "${DEGRADED}" -eq 1 ]; then
  log "面板仍会正常启动,上述监控模块降级禁用即可,不影响其余功能。"
fi

# ───────────────────────── 读取端口(从 .env 的 PANEL_PORT) ─────────────────────────
# 容器端口由 PANEL_PORT 决定;compose 的宿主映射当前固定 8080:8080,
# 若用户改了 PANEL_PORT 但未同步 compose ports,健康检查仍打宿主 8080——
# 这里取 .env 值作首选,以应对 compose 一并改端口的情形。
read_env_port() {
  # 取最后一个非注释的 PANEL_PORT 赋值,容忍空格。
  local line
  line="$(grep -E '^[[:space:]]*PANEL_PORT[[:space:]]*=' "${ENV_FILE}" 2>/dev/null | grep -vE '^\s*#' | tail -1 || true)"
  if [ -n "${line}" ]; then
    printf '%s' "${line}" | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//' \
      | grep -E '^[0-9]+$' || printf '%s' "${DEFAULT_PORT}"
  else
    printf '%s' "${DEFAULT_PORT}"
  fi
}
PANEL_PORT="$(read_env_port)"
[ -n "${PANEL_PORT}" ] || PANEL_PORT="${DEFAULT_PORT}"
HEALTH_URL="http://localhost:${PANEL_PORT}/healthz"

# ───────────────────────── 3. 构建并启动 ─────────────────────────
step "3/5 构建并启动容器"

if [ "${DO_BUILD}" -eq 1 ]; then
  log "docker compose up -d --build  (首次或代码变更后会构建,Pi 上较慢,请耐心)"
  if ! docker compose up -d --build; then
    err "docker compose 启动失败。请查看上方错误输出。"
    exit 1
  fi
else
  log "docker compose up -d  (--no-build:跳过镜像重建)"
  if ! docker compose up -d; then
    err "docker compose 启动失败。请查看上方错误输出。"
    exit 1
  fi
fi
ok "容器已启动(后台 -d)"

# ───────────────────────── 4. 等待就绪(轮询 /healthz) ─────────────────────────
step "4/5 等待面板就绪(${HEALTH_URL},超时 ${HEALTH_TIMEOUT}s)"

# 探测器:优先 curl,退化 wget,再退化 python(镜像内一定有,但宿主未必)。
probe() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --max-time 3 "${HEALTH_URL}" 2>/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null -T 3 "${HEALTH_URL}" 2>/dev/null
  else
    python3 - "${HEALTH_URL}" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    sys.exit(0 if urllib.request.urlopen(sys.argv[1], timeout=3).status == 200 else 1)
except Exception:
    sys.exit(1)
PY
  fi
}

elapsed=0
interval=3
READY=0
while [ "${elapsed}" -lt "${HEALTH_TIMEOUT}" ]; do
  if probe; then
    READY=1
    break
  fi
  printf '%s[panel]%s 等待中… (%ss/%ss)\r' "${C_BLUE}" "${C_RESET}" "${elapsed}" "${HEALTH_TIMEOUT}"
  sleep "${interval}"
  elapsed=$((elapsed + interval))
done
printf '\n'

if [ "${READY}" -ne 1 ]; then
  err "面板在 ${HEALTH_TIMEOUT}s 内未就绪(${HEALTH_URL} 未返回 200)。"
  err "最近 50 行日志:"
  docker compose logs --tail=50 || true
  err "排查建议: 'docker compose ps' 看状态;'docker compose logs -f' 看实时日志。"
  exit 1
fi
ok "面板已就绪 — ${HEALTH_URL} 返回 200"

# ───────────────────────── 5. 可选预置 A100 ─────────────────────────
step "5/5 预置注册 (可选)"

if [ "${DO_SEED}" -eq 1 ]; then
  if [ -x "${SEED_SCRIPT}" ] || [ -f "${SEED_SCRIPT}" ]; then
    log "注册 A100 (mux-a100) — 已存在返回 409 视为 OK(幂等)。"
    # seed 脚本读 PANEL_URL;用本机端口对齐。已注册(409)脚本自身以 0 退出。
    if PANEL_URL="http://localhost:${PANEL_PORT}" bash "${SEED_SCRIPT}"; then
      ok "A100 预置完成(新注册或已存在)。"
    else
      warn "seed 脚本返回非 0;面板已在运行,可稍后手动重试: make seed"
    fi
  else
    warn "未找到 ${SEED_SCRIPT},跳过预置。可用 /servers 表单手动注册。"
  fi
else
  log "未带 --seed,跳过 A100 预置。"
  log "  需要时: ./start.sh --seed  或  make seed  或浏览器打开 /servers 表单。"
fi

# ───────────────────────── 收尾输出 ─────────────────────────
printf '\n'
ok "Panel Everything 已启动 🎉"
printf '\n'
printf '  %s本机访问:%s   http://localhost:%s/\n' "${C_BOLD}" "${C_RESET}" "${PANEL_PORT}"

# Tailscale 访问地址(若装了 tailscale 且已登入)。
if command -v tailscale >/dev/null 2>&1; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  if [ -n "${TS_IP}" ]; then
    printf '  %sTailnet 访问:%s http://%s:%s/   (各终端登入同一 tailnet 即可访问)\n' \
      "${C_BOLD}" "${C_RESET}" "${TS_IP}" "${PANEL_PORT}"
  else
    printf '  Tailnet 访问: 运行 %stailscale up%s 后用 %stailscale ip -4%s 得到地址 http://<tailscale-ip>:%s/\n' \
      "${C_BOLD}" "${C_RESET}" "${C_BOLD}" "${C_RESET}" "${PANEL_PORT}"
  fi
else
  printf '  Tailnet 访问: 装好 Tailscale 并 %stailscale up%s 后,用 %stailscale ip -4%s 得到 http://<tailscale-ip>:%s/\n' \
    "${C_BOLD}" "${C_RESET}" "${C_BOLD}" "${C_RESET}" "${PANEL_PORT}"
fi

printf '\n  后续命令:\n'
printf '    make status    # 容器状态 + 健康检查\n'
printf '    make logs      # 实时日志\n'
printf '    make down      # 停止并移除容器\n'
printf '    make seed      # (单独)注册 A100\n'
printf '\n  注意: 不要把 %s 端口暴露到公网(面板无认证,仅依赖 Tailscale 内网边界)。\n' "${PANEL_PORT}"
printf '\n'
