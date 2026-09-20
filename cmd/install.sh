#!/bin/bash
set -e
echo "Installing CloudMount ..."

# --- 同名容器残留清理 -------------------------------------------------------
for cid in $(docker ps -aq -f name=cloudmount-app 2>/dev/null || true); do
    [ "$(docker inspect "$cid" 2>/dev/null | grep -o '"Name": "/[^"]*"' | cut -d'"' -f4)" = "/cloudmount-app" ] || continue
    cproj=$(docker inspect "$cid" 2>/dev/null | grep -m1 -o '"com.docker.compose.project": "[^"]*"' | cut -d'"' -f4 || true)
    if [ "$cproj" = "cloudmount" ]; then
        echo "  cloudmount-app already belongs to project 'cloudmount', keep it"
        continue
    fi
    echo "  removing conflicting container cloudmount-app (belongs to '${cproj:-none}')"
    docker rm -f "$cid" >/dev/null 2>&1 || true
done
# --- FUSE 挂载点工具 ---------------------------------------------------------
resolve_mount_path() {
    _p=""
    # 这里踩过一次坑：docker inspect 输出里的片段两端带引号，grep 若连引号一起
    # 吃进去再 cut，末尾那个引号会残留，于是 is_mounted 拿着带引号的路径去比
    # /proc/mounts 永远比不中 —— 表现是「停止后残留挂载清不掉、启动后误报没挂上」。
    # 所以：匹配模式不吃引号，最后再统一把引号剔掉兜底。
    _p=$(docker inspect cloudmount-app 2>/dev/null | grep -o 'MOUNT_DIR=[^"]*' | head -n1 | cut -d= -f2- || true)
    if [ -z "$_p" ] && [ -f docker-compose.yaml ]; then
        _p=$(grep -o 'MOUNT_DIR=[^"]*' docker-compose.yaml 2>/dev/null | head -n1 | cut -d= -f2- || true)
    fi
    printf '%s' "$_p" | tr -d '"'
}
# 解析不到真实挂载点**只会在首次安装时发生**：应用中心先跑 cmd/install.sh，
# 之后才渲染 docker-compose.yaml、才建容器，所以这两条线索此刻都还是空的。
# ⚠️ 此时绝不能退回默认值去 mkdir —— 真实挂载点由安装参数决定（例如
#    /volume1/data/personal/1000/网盘），而默认值是 /volume1/data/网盘，于是在宿主上
#    凭空多出一个空的「网盘」（实测每次首装必现）。挂载点交给容器入口 run.sh 自己建：
#    它写在共享卷里，mkdir 会直接落到宿主（彩排已验证「预先不建目录也能正常挂上」）。
MOUNT_UNRESOLVED=0
MOUNTPATH="$(resolve_mount_path)"
if [ -n "$MOUNTPATH" ]; then
    echo "  mount point: ${MOUNTPATH}"
else
    MOUNTPATH="/volume1/data/网盘"
    MOUNT_UNRESOLVED=1
    echo "  mount point: 尚未解析到（首次安装属正常），暂用 ${MOUNTPATH} 做兜底判断"
fi

is_mounted() { grep -qs " ${MOUNTPATH} " /proc/mounts 2>/dev/null; }
force_umount() {
    is_mounted || return 0
    echo "  leftover mount on ${MOUNTPATH}, unmounting"
    umount -l "${MOUNTPATH}" 2>/dev/null || fusermount3 -uz "${MOUNTPATH}" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        is_mounted || break
        sleep 1
    done
    if is_mounted; then
        echo "  WARNING: ${MOUNTPATH} still mounted - run manually: umount -l ${MOUNTPATH}"
    fi
    return 0
}
# 正常停容器时 rclone 会自己卸载并传播回宿主机，先等它，再兜底
wait_unmount() {
    for _ in 1 2 3 4 5; do
        is_mounted || return 0
        sleep 1
    done
    force_umount
    return 0
}
# 目标目录不存在时补建（安装/启动/升级时各做一次）
# ⚠️ 但挂载点没解析出来时**必须跳过**：那种情况只出现在首次安装，此刻建的必是
#    默认路径而不是真实路径，等于在宿主上扔一个空目录（v1.1.2 修的就是这个）。
ensure_mount_dir() {
    if [ "${MOUNT_UNRESOLVED:-0}" = "1" ]; then
        echo "  skip mkdir（挂载点尚未解析，交给容器入口 run.sh 创建）"
        return 0
    fi
    mkdir -p "${MOUNTPATH}" 2>/dev/null || true
}
# --- 老版本遗留挂载点自愈 -----------------------------------------------------
# v1.1.1/1.1.2 把读缓存挂成「源 == 目标」的自绑定，在宿主命名空间留下一枚与
# 存储池同设备的重复挂载点。它会让 NAS 把存储池 MountPoint 认成缓存目录，
# 文件管理器根列表变空。容器重建不会去掉它（它已不在任何容器的挂载空间里），
# 必须在宿主机上显式卸载，所以这里做一次检查 + 清理。
# 判据：正常状态下 /volume1 与 /volume1/data/cloudmount 不应同时是挂载点。
purge_stray_cache_mount() {
    grep -qs " /volume1/data/cloudmount " /proc/mounts 2>/dev/null || return 0
    echo "  检测到缓存路径上的重复挂载点（老版本遗留），卸载中"
    umount /volume1/data/cloudmount 2>/dev/null         || umount -l /volume1/data/cloudmount 2>/dev/null || true
    if grep -qs " /volume1/data/cloudmount " /proc/mounts 2>/dev/null; then
        echo "  WARNING: /volume1/data/cloudmount 仍是挂载点，请手动执行 umount -l /volume1/data/cloudmount"
    else
        echo "  已清理"
    fi
    return 0
}

# 先清僵尸挂载再建目录：目录若还是个死挂载点，mkdir 会直接报 ENOTCONN
purge_stray_cache_mount
force_umount
ensure_mount_dir
mkdir -p /volume1/data/cloudmount/cache

# 前提：宿主机得能跑 FUSE
if [ ! -e /dev/fuse ]; then
    echo "  WARNING: /dev/fuse not found - FUSE mount will fail (enable the fuse module)"
fi
if ! grep -q '^nodev[[:space:]]*fuse' /proc/filesystems 2>/dev/null; then
    echo "  WARNING: fuse not listed in /proc/filesystems - FUSE mount will fail"
fi

# 图标落位：本机包库备份 + 对外网页目录（手机 App 与远程访问只认公网图标地址）
APPDIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$APPDIR/icon.png" ]; then
    mkdir -p /userdata/tpk_local/icons
    cp -f "$APPDIR/icon.png" /userdata/tpk_local/icons/cloudmount.png
    if [ -d /usr/local/pc ]; then
        cp -f "$APPDIR/icon.png" /usr/local/pc/cloudmount-icon.png
        echo "  icon published: /usr/local/pc/cloudmount-icon.png"
    fi
fi

# --- 配置面板 ---------------------------------------------------------------
# 应用中心详情页那个「打开」按钮指向的就是它（config.json 的 accessCtrl 声明了端口）。
# 面板本身是宿主上的 systemd 服务，见 cmd/panel.sh。
# 安装参数给了 PANEL_PASSWORD 就先落一份口令记录，用户首次打开即可直接用；
# 没给则留空 —— 页面会显示「设置访问口令」，由第一个访问者自己设（不需要 root）。
PY_BIN="$(command -v python3 || true)"
[ -n "$PY_BIN" ] || PY_BIN=/usr/bin/python3
mkdir -p /volume1/data/cloudmount 2>/dev/null || true
PANEL_PW="$(printf '%s' "${PANEL_PASSWORD:-}" | tr -d '[]')"
if [ -n "$PANEL_PW" ] && [ ! -f /volume1/data/cloudmount/.panel-auth.json ]; then
    "$PY_BIN" - "$PANEL_PW" <<'PYEOF' || true
import base64, hashlib, json, os, sys
pw = sys.argv[1]
salt = os.urandom(16)
rec = {
    "salt": base64.b64encode(salt).decode(),
    "hash": base64.b64encode(hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200000)).decode(),
    "rounds": 200000,
    "secret": os.urandom(32).hex(),
    "source": "install",
    "updated": 0,
}
p = "/volume1/data/cloudmount/.panel-auth.json"
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "w", encoding="utf-8") as f:
    f.write(json.dumps(rec))
os.chmod(p, 0o600)
PYEOF
    echo "  已按安装参数写入配置面板的初始访问口令"
fi
bash "$APPDIR/cmd/panel.sh" start || true

echo "Docker app installation completed..."
exit 0
