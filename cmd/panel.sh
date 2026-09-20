#!/bin/bash
# ---------------------------------------------------------------------------
# 配置面板的生命周期（install.sh / start.sh / stop.sh / uninstall.sh 共用）
#
#   用法：panel.sh start | stop | remove
#
# 面板是一个跑在**宿主机**上的小 Web 服务（panel/panel.py），不是容器 ——
# 容器用的 rclone 镜像里没有 python。它由 systemd 托管，这样 NAS 重启、
# 进程崩掉都能自己回来。
#
# 它存在的理由：应用中心对已安装的应用只给 打开 / 停用 / 卸载，**没有「设置」**
# （那个入口属于官方应用，自装应用的 isShowSettings 恒为 false）。于是把配置
# 放到「打开」按钮后面 —— tpk 的 config.json 声明 accessCtrl.urlAppAccesses
# 之后，应用中心就会自动拼出访问地址。
# ---------------------------------------------------------------------------
set -u

UNIT=/etc/systemd/system/tie-niu-cloudmount-panel.service
SVC=tie-niu-cloudmount-panel.service
UNIT_NAME=tie-niu-cloudmount-panel
DATA_DIR=/volume1/data/cloudmount
LOG=/var/log/cloudmount_panel.log

APPDIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || PY=/usr/bin/python3

write_unit() {
    if [ ! -x "$PY" ]; then
        echo "  WARNING: 找不到 python3，配置面板无法启动（网盘挂载本身不受影响）"
        return 1
    fi
    cat > "$UNIT" <<EOF
[Unit]
Description=TieNiu CloudMount Configuration Panel
After=network.target docker.service

[Service]
Type=simple
ExecStart=$PY $APPDIR/panel/panel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload >/dev/null 2>&1 || true
    return 0
}

case "${1:-start}" in
start)
    # unit 不在（首次安装、被人删过、从老版本升上来）就重建一次，保证幂等
    if [ ! -f "$UNIT" ]; then
        write_unit || exit 0
    fi
    systemctl enable "$SVC" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$SVC"; then
        systemctl restart "$SVC" >/dev/null 2>&1 || true
    else
        systemctl start "$SVC" >/dev/null 2>&1 || true
    fi
    sleep 1
    if systemctl is-active --quiet "$SVC"; then
        echo "  配置面板已启动：http://<NAS 地址>:8791/  （应用中心详情页点「打开」）"
    else
        echo "  WARNING: 配置面板没能启动，看日志：journalctl -u $UNIT_NAME -n 30 或 $LOG"
    fi
    exit 0
    ;;
stop)
    systemctl stop "$SVC" >/dev/null 2>&1 || true
    echo "  配置面板已停止"
    exit 0
    ;;
remove)
    systemctl disable --now "$SVC" >/dev/null 2>&1 || true
    rm -f "$UNIT" 2>/dev/null || true
    systemctl daemon-reload >/dev/null 2>&1 || true
    echo "  配置面板已移除"
    exit 0
    ;;
*)
    echo "用法：$0 start|stop|remove"
    exit 1
    ;;
esac