#!/usr/bin/env bash
# 把 campus-net-login 装成 macOS launchd 用户代理（登录自启 + 常驻重连）。
#
#   ./scripts/install-macos.sh              # 安装并加载
#   ./scripts/install-macos.sh --uninstall  # 卸载
set -euo pipefail

LABEL="com.github.campus-net-login"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO/campus_login.py"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "[+] 已卸载 $LABEL"
  exit 0
fi

[[ -f "$SCRIPT" ]] || { echo "找不到 $SCRIPT"; exit 1; }
PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || { echo "找不到 python3"; exit 1; }

CONFIG="${CAMPUS_LOGIN_CONFIG:-}"
if [[ -z "$CONFIG" ]]; then
  for c in "$REPO/config.json" "$HOME/Library/Application Support/campus-net-login/config.json"; do
    [[ -f "$c" ]] && CONFIG="$c" && break
  done
fi
[[ -n "$CONFIG" ]] || { echo "还没有配置文件，先运行:  $PYTHON $SCRIPT init"; exit 1; }

mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SCRIPT</string>
    <string>--config</string>
    <string>$CONFIG</string>
    <string>watch</string>
  </array>
  <key>WorkingDirectory</key> <string>$REPO</string>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>ThrottleInterval</key> <integer>30</integer>
  <key>StandardOutPath</key>  <string>$HOME/Library/Logs/campus-net-login.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/campus-net-login.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "[+] 已安装并加载 $LABEL"
echo "    查看状态: launchctl print gui/$(id -u)/$LABEL | head -30"
echo "    查看日志: tail -f ~/Library/Logs/campus-net-login.out.log"
