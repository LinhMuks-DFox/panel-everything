#!/usr/bin/env bash
#
# install_reporter_cron.sh — 把 AI 用量上报装成每 5 分钟自动跑。
#
# macOS 用 launchd(比 cron 稳,登录即生效);Linux 用 crontab。
# 幂等:重复执行会覆盖同名任务。
#
# 用法:
#   scripts/install_reporter_cron.sh            # 安装(每 5 分钟)
#   scripts/install_reporter_cron.sh --uninstall # 卸载
#
# 面板地址默认 http://localhost:8080;远程面板用:
#   PANEL_URL=http://100.x.x.x:8080 scripts/install_reporter_cron.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${REPO_DIR}/scripts/run_reporter.sh"
PANEL_URL="${PANEL_URL:-http://localhost:8080}"
LABEL="com.panel-everything.reporter"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG="${HOME}/.local/log/panel-reporter.log"

chmod +x "${RUNNER}" 2>/dev/null || true
mkdir -p "$(dirname "${LOG}")"

uninstall() {
  if [ "$(uname -s)" = "Darwin" ]; then
    launchctl unload "${PLIST}" 2>/dev/null || true
    rm -f "${PLIST}"
    echo "已卸载 launchd 任务 ${LABEL}。"
  else
    ( crontab -l 2>/dev/null | grep -v "${RUNNER}" ) | crontab - || true
    echo "已从 crontab 移除 reporter。"
  fi
}

if [ "${1:-}" = "--uninstall" ]; then uninstall; exit 0; fi

if [ "$(uname -s)" = "Darwin" ]; then
  mkdir -p "$(dirname "${PLIST}")"
  cat > "${PLIST}" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${RUNNER}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PANEL_URL</key><string>${PANEL_URL}</string></dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
</dict>
</plist>
PLISTEOF
  launchctl unload "${PLIST}" 2>/dev/null || true
  launchctl load "${PLIST}"
  echo "已安装 launchd 任务 ${LABEL}(每 5 分钟,PANEL_URL=${PANEL_URL})。"
  echo "  日志: ${LOG}"
  echo "  卸载: scripts/install_reporter_cron.sh --uninstall"
else
  LINE="*/5 * * * * PANEL_URL=${PANEL_URL} ${RUNNER} >> ${LOG} 2>&1"
  ( crontab -l 2>/dev/null | grep -v "${RUNNER}"; echo "${LINE}" ) | crontab -
  echo "已写入 crontab(每 5 分钟,PANEL_URL=${PANEL_URL})。"
  echo "  日志: ${LOG}"
  echo "  卸载: scripts/install_reporter_cron.sh --uninstall"
fi
