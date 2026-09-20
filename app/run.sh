#!/bin/sh
# ---------------------------------------------------------------------------
# 网盘挂载（CloudMount）容器入口
#
# 把 AList（或任意 WebDAV）挂成宿主机上的真实目录：
#   AList 网盘 -> rclone WebDAV -> FUSE -> compose 的 :rshared 传播回宿主机
#   -> ${MOUNT_DIR}
#
# 凭据只在本容器里用一次（转成 rclone 的 obscured 形式做连接串），
# 真正的网盘账号密码始终只存在 AList 里。
#
# 本事：本脚本是 PID 1，必须 exec 起 rclone，否则 docker stop 要等满
# stop_grace_period 才被 SIGKILL，宿主机上可能留下僵尸挂载点。
#
# 镜像内没有 bash，本脚本必须保持 POSIX sh 语法。
# ---------------------------------------------------------------------------
set -u

# 应用中心对不同 paramType 的传参形式不一样（字符串 / 切片字面量），
# 这里两种都吃：两端同时带方括号时按切片字面量剥掉。
strip_list() {
    v="$1"
    case "$v" in
        \[*\]*) v="${v#\[}"; v="${v%\]}" ;;
    esac
    printf '%s' "$v"
}

# ---------------------------------------------------------------------------
# 持久配置：由宿主机上的「配置面板」写入（应用中心详情页 →「打开」）。
#
# 为什么要有这一层：应用中心对已安装的应用只给 打开/停用/卸载，没有「设置」，
# 改安装参数得卸载重装；而重装又会把参数重置成出厂默认值（网盘目录因此消失过）。
# 面板把所有可调项写在这个文件里，这里 source 一次 —— **它覆盖 compose 的
# 同名环境变量**。compose 只是渲染产物，应用中心升级/覆盖安装时会被重新渲染，
# 但动不了这个文件，所以面板里设的值不会被冲掉。
# ⚠️ 排查时记得：容器里的实际取值以本文件（若存在）为准，不是 compose。
# ---------------------------------------------------------------------------
CONF="/cmcache/cloudmount.conf"
if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF"
    echo "[cloudmount] 已加载持久配置 $CONF"
fi

MOUNT="$(strip_list "${MOUNT_DIR:-/volume1/data/网盘}")"
URL="$(strip_list "${WEBDAV_URL:-http://host.docker.internal:5244/dav}")"
VUSER="$(strip_list "${WEBDAV_USER:-admin}")"
VPASS="$(strip_list "${WEBDAV_PASSWORD:-admin}")"
OUSER="$(strip_list "${MOUNT_UID:-0}")"
OGROUP="$(strip_list "${MOUNT_GID:-0}")"
[ -n "$OUSER" ] || OUSER=0
[ -n "$OGROUP" ] || OGROUP=0
CACHE="${CACHE_DIR:-/volume1/data/cloudmount/cache}"
CACHE_MAX="${VFS_CACHE_MAX_SIZE:-20G}"
CACHE_MODE="${VFS_CACHE_MODE:-full}"

# ---------------------------------------------------------------------------
# 残留挂载清理。容器被强杀/断电时 rclone 来不及卸载，宿主机上会留下一个
# 死挂载点，访问它会永久卡在 "Transport endpoint is not connected"。
#   判据必须走 /proc/mounts —— 死挂载点上 mountpoint / [ -e ] / stat 全部
#   返回失败，看起来「根本不是挂载点」，于是兜底卸载不会触发。
# 容器与宿主机共享 /volume1（:rshared），所以这里看到的 $MOUNT 就是宿主机
# 上那一个；在这里 umount 会一并传播回去。
# ---------------------------------------------------------------------------
# 挂载点被改过（在配置面板里换了目录）：把上一处挂载点卸掉。
# 不卸的话它会变成死挂载 —— 用户在文件管理器里还能看到一个点进去就卡住的老目录，
# 而新内容在另一个位置，看起来像"挂载坏了"。
LASTF="/cmcache/.last_mount"
if [ -f "$LASTF" ]; then
    OLD="$(cat "$LASTF" 2>/dev/null || true)"
    if [ -n "$OLD" ] && [ "$OLD" != "$MOUNT" ] && grep -qs " $OLD " /proc/mounts; then
        echo "[cloudmount] 挂载点已变更，卸载旧挂载点：$OLD"
        umount -l "$OLD" 2>/dev/null || fusermount3 -uz "$OLD" 2>/dev/null || true
    fi
fi
printf '%s' "$MOUNT" > "$LASTF" 2>/dev/null || true

if grep -qs " $MOUNT " /proc/mounts; then
    echo "[cloudmount] 发现 $MOUNT 上有残留挂载，先卸载"
    umount -l "$MOUNT" 2>/dev/null || fusermount3 -uz "$MOUNT" 2>/dev/null || true
    n=0
    while grep -qs " $MOUNT " /proc/mounts && [ "$n" -lt 5 ]; do
        n=$((n + 1))
        sleep 1
    done
fi

mkdir -p "$MOUNT" "$CACHE" 2>/dev/null || true

# 缓存目录降级：若 SHARE_ROOT 收窄到看不见 /volume1/data，mkdir 会在容器自己的
# 可写层里建出一个同名目录，缓存就落进了容器层（重启即丢、还可能撑大 docker root）。
# 这里探一下可写性，不可写就退回容器内 /tmp，保证挂载本身不受影响。
if ! ( : > "$CACHE/.wtest" ) 2>/dev/null; then
    echo "[cloudmount] 警告：缓存目录 $CACHE 不可写（检查 SHARE_ROOT 是否覆盖了它）"
    echo "[cloudmount] 临时改用 /tmp/rclone-cache，重启后缓存丢失"
    CACHE=/tmp/rclone-cache
    mkdir -p "$CACHE" 2>/dev/null || true
else
    rm -f "$CACHE/.wtest" 2>/dev/null || true
fi

# 口令转 obscured 形式（obscure 输出只含 [A-Za-z0-9_-]，可安全放进连接串）
# ⚠️ 连接串末尾那个冒号是**语法必需**的终止符，漏掉会报
#    "unquoted config value must end with `,` or `:`" 而挂不上。
REMOTE=":webdav,url='${URL}',vendor=other,user=${VUSER},pass=$(rclone obscure "$VPASS"):"

echo "[cloudmount] WebDAV : $URL"
echo "[cloudmount] 账号    : $VUSER"
echo "[cloudmount] 挂载点  : $MOUNT"
echo "[cloudmount] 文件属主: ${OUSER}:${OGROUP}"
echo "[cloudmount] 读缓存  : $CACHE (上限 $CACHE_MAX，模式 $CACHE_MODE)"

# 等上游就绪，最多 40 秒。这只是给「AList 还没起来」留个缓冲，不是硬门槛：
#   ⚠️ 别用 `rclone lsd` 的退出码当判据 —— AList 里只要有任意一条存储是坏的，
#      根目录列举就会带上错误、返回非 0，于是永远等不到「就绪」而根本不挂载。
#   这里探测 HTTP 接口，与存储状态无关；非 AList 的 WebDAV 探不到也照挂。
API="${URL%/dav}/api/public/settings"
i=0
while [ "$i" -lt 20 ]; do
    if wget -q -O /dev/null --timeout=3 "$API" 2>/dev/null; then
        echo "[cloudmount] 上游已就绪（等待 $((i * 2)) 秒）"
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ "$i" -ge 20 ]; then
    echo "[cloudmount] 提示：未探到 $API（非 AList 的 WebDAV 属正常），继续挂载"
fi

echo "[cloudmount] 启动 rclone mount ..."
exec rclone mount "$REMOTE" "$MOUNT" \
    --allow-other --allow-non-empty \
    --uid "$OUSER" --gid "$OGROUP" --umask 022 \
    --vfs-cache-mode "$CACHE_MODE" \
    --vfs-cache-max-size "$CACHE_MAX" \
    --vfs-cache-max-age 24h \
    --cache-dir "$CACHE" \
    --dir-cache-time 1m --poll-interval 0 \
    --buffer-size 32M --transfers 4 --checkers 8 \
    --low-level-retries 3 --log-level INFO
