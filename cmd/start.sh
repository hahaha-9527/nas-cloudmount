#!/bin/bash
set -e
echo "Starting application..."

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

# 容器没在跑却还挂着 = 上次异常退出留下的僵尸挂载，先清掉，
# 否则新容器会把挂载建在死挂载之上，一起卡住
if is_mounted && ! docker inspect cloudmount-app 2>/dev/null | grep -q '"Running": true'; then
    force_umount
fi
purge_stray_cache_mount
ensure_mount_dir
mkdir -p /volume1/data/cloudmount/cache

docker compose -f docker-compose.yaml -p {{service}} up -d

# 等 FUSE 挂上去，把结果直接写进日志（判据看这里，不要只看「容器 Up」）
i=0
while [ "$i" -lt 30 ]; do
    if is_mounted; then
        echo "  mounted: ${MOUNTPATH}"
        break
    fi
    i=$((i + 1))
    sleep 1
done
is_mounted || echo "  WARNING: ${MOUNTPATH} not mounted yet - check: docker logs cloudmount-app"

# 配置面板拉起来（unit 由 cmd/install.sh 写过；不在就由 cmd/panel.sh 重建）
bash ./cmd/panel.sh start || true

exit 0
