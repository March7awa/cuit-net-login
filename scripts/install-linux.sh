#!/usr/bin/env bash
# 把 campus-net-login 装成 systemd 用户服务（Linux），实现开机自启 + 断线重连。
#
#   ./scripts/install-linux.sh              # 安装并启动
#   ./scripts/install-linux.sh --uninstall  # 卸载
#
# 说明：用 systemd --user 是为了让 DPAPI 之外的凭据文件保持在你自己的家目录权限下。
# 若希望开机即启动（无需登录），执行：  sudo loginctl enable-linger "$USER"
set -euo pipefail

SERVICE="campus-net-login"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO/campus_login.py"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$SERVICE.service"

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "[+] 已卸载 $SERVICE"
  exit 0
fi

[[ -f "$SCRIPT" ]] || { echo "找不到 $SCRIPT"; exit 1; }

PYTHON="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON" ]] || { echo "找不到 python3"; exit 1; }

CONFIG="${CAMPUS_LOGIN_CONFIG:-}"
if [[ -z "$CONFIG" ]]; then
  for c in "$REPO/config.json" "$HOME/.config/campus-net-login/config.json"; do
    [[ -f "$c" ]] && CONFIG="$c" && break
  done
fi
[[ -n "$CONFIG" ]] || { echo "还没有配置文件，先运行:  $PYTHON $SCRIPT init"; exit 1; }

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Campus network auto login (campus-net-login)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO
ExecStart=$PYTHON $SCRIPT --config $CONFIG watch
Restart=always
RestartSec=30
# 断线重连本来就是它的工作，这里只做兜底
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE"

echo "[+] 已安装并启动 $SERVICE"
echo "    查看状态: systemctl --user status $SERVICE"
echo "    查看日志: journalctl --user -u $SERVICE -f"
echo "    开机(未登录)也自动跑: sudo loginctl enable-linger $USER"
