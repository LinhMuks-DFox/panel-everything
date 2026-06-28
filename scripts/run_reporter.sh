#!/usr/bin/env bash
#
# run_reporter.sh — 在本机跑一次 AI 用量上报(Codex / Claude Code / ChatGPT)。
#
# 单机场景(面板 + Codex/Claude Code 都在这台 Mac):reporter 直接打 localhost,
# 读 ~/.codex / ~/.claude 本地数据,POST 到面板 /api/ingest/ai-usage。
#
# 用法:
#   scripts/run_reporter.sh                 # 上报一次(用项目 .venv 的 python+httpx)
#   PANEL_URL=http://100.x.x.x:8080 scripts/run_reporter.sh   # 指定远程面板
#
# 自动定时(每 5 分钟)见 scripts/install_reporter_cron.sh。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

# 面板地址:默认本机 8080(与 PANEL_PORT 一致)。
PANEL_URL="${PANEL_URL:-http://localhost:8080}"

# 选 python:优先项目 .venv(已带 httpx),否则系统 python3(需自行装 httpx)。
if [ -x "${REPO_DIR}/.venv/bin/python" ]; then
  PY="${REPO_DIR}/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi
[ -n "${PY}" ] || { echo "找不到 python3" >&2; exit 2; }

export PANEL_URL
exec "${PY}" "${REPO_DIR}/tools/reporter/reporter.py"
