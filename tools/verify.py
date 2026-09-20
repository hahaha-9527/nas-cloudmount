#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CloudMount 安装 / 重启后自检

在 **NAS 宿主**上跑（要看 docker 与 /proc/mounts，所以需要 root）：

    sudo python3 tools/verify.py
    sudo python3 tools/verify.py --mount-dir /volume1/data/personal/<你的用户ID>/网盘
    sudo python3 tools/verify.py --panel http://127.0.0.1:8791

不传 --mount-dir 时从容器环境变量里读实际生效的挂载点。

检查项:
    1. 容器 cloudmount-app 在运行
    2. 挂载点在 /proc/mounts 里，且类型为 FUSE
    3. 挂载点能列出目录（死挂载点会永久卡住，所以带超时）
    4. 宿主挂载表里存储池的设备**没有重复挂载点** —— 专门防「缓存目录自绑定」那个坑：
       它会泄露出一个与存储池同设备的重复挂载点，使铁牛把存储池的 MountPoint 认错，
       文件管理器「我的文件」根列表随之变空（看起来像文件夹全丢了，数据其实没少）
    5. 挂载文件的属主（跟容器实际参数对一下，SMB 只读问题就出在这）
    6. 配置页存活 + 闸门状态（待设置口令 / 待登录 / 已进入）

退出码 0 = 全部正常，1 = 有异常（可直接串进部署脚本）。
"""
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

bad = 0


def ck(name, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print("  [%s] %-26s %s" % ("OK" if ok else "NG", name, detail))
    return ok


def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def unesc(s):
    """还原 /proc/mounts 里的八进制转义（空格 \\040、中文也可能是转义形式）"""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_args():
    a = {"--mount-dir": "", "--container": "cloudmount-app",
         "--cache-host": "/volume1/data/cloudmount", "--panel": "http://127.0.0.1:8791"}
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] in a and i + 1 < len(argv):
            a[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return a


def env_of(container):
    rc, out, _ = sh("docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' %s" % container)
    if rc != 0 or not out:
        return {}
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def mounts():
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            return [l.split() for l in f if l.strip()]
    except Exception:
        return []


def http_get(url, timeout=8):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)


def main():
    A = parse_args()
    container = A["--container"]

    print("== 1. 容器 ==")
    rc, status, err = sh("docker inspect -f '{{.State.Status}}' %s" % container)
    if rc != 0:
        ck("容器存在", False, err[:100] or "docker inspect 失败（需要 root？）")
    else:
        ck("容器存在", True, container)
        ck("容器在运行", status == "running", "State.Status = %s" % status)
        rc2, hp, _ = sh("docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' %s" % container)
        if hp:
            ck("健康检查", hp == "healthy",
               "%s（判据是 /proc/mounts 里有没有 fuse.rclone）" % hp)

    env = env_of(container)
    mount_dir = A["--mount-dir"] or env.get("MOUNT_DIR") or "/volume1/data/网盘"
    uid, gid = env.get("MOUNT_UID", "0") or "0", env.get("MOUNT_GID", "0") or "0"

    print()
    print("== 2. 挂载点 ==")
    print("  挂载点 = %s" % mount_dir)
    M = mounts()
    entry = [f for f in M if len(f) >= 3 and unesc(f[1]) == mount_dir]
    if not ck("在 /proc/mounts 里", bool(entry),
              "没找到（容器没挂上，或路径填错）"):
        pass
    else:
        src, fstype = unesc(entry[0][0]), entry[0][2]
        ck("类型是 FUSE", "fuse" in fstype.lower(), "%s <- %s" % (fstype, src))

    print()
    print("== 3. 挂载点可用性 ==")
    rc, out, err = sh("ls -1 -- %s | head -20" % shlex.quote(mount_dir), timeout=8)
    if rc == 124:
        ck("能列出目录", False, "超时 —— 典型的死挂载点（Transport endpoint is not connected）")
    elif rc != 0:
        ck("能列出目录", False, (err or "ls 失败")[:120])
    else:
        n = len([x for x in out.splitlines() if x.strip()])
        ck("能列出目录", True, "顶层 %d 项%s" % (n, " —— 空目录？先确认 AList 里能列出文件" if n == 0 else ""))

    print()
    print("== 4. 宿主挂载表是否被污染（防「文件管理器文件夹全没了」）==")
    pool = None
    for f in M:
        if len(f) >= 3 and unesc(f[1]) in ("/volume1", "/"):
            pool = unesc(f[0])
            break
    if not pool or not pool.startswith("/dev/"):
        print("  [--] %-26s 跳过（没识别出存储池设备）" % "存储池挂载点唯一")
    else:
        cnt = sum(1 for f in M if len(f) >= 1 and unesc(f[0]) == pool)
        ck("存储池挂载点唯一", cnt == 1,
           "%s 在 /proc/mounts 里出现 %d 次%s" % (pool, cnt,
                                                  "" if cnt == 1 else " ← 有重复挂载点泄漏，文件管理器根列表可能变空"))

    print()
    print("== 5. 缓存与属主 ==")
    cache = A["--cache-host"]
    ck("缓存目录存在", os.path.isdir(cache), cache)
    if os.path.isdir(cache):
        ck("缓存目录可写", os.access(cache, os.W_OK), cache)
        ck("缓存不是自绑定挂载点",
           not any(len(f) >= 3 and unesc(f[1]) == cache and unesc(f[0]) == cache for f in M),
           "源 == 目标 会污染宿主挂载表")
    print("  文件属主 = %s:%s %s" % (uid, gid, "(root —— 开 SMB 后 Windows 侧会当只读)" if uid == "0" else ""))

    print()
    print("== 6. 配置页 ==")
    base = A["--panel"].rstrip("/")
    code, body = http_get(base + "/healthz")
    if code != 200:
        ck("配置页存活", False, "GET %s/healthz -> %s（没装 python3 属正常，挂载不受影响）" % (base, code or body[:60]))
    else:
        ck("配置页存活", True, base)
        code, body = http_get(base + "/api/state")
        try:
            d = json.loads(body)
        except Exception:
            d = {}
        if d.get("needSetup"):
            ck("闸门状态", True, "待设置口令 —— 打开页面自己设一个")
        elif d.get("needLogin"):
            ck("闸门状态", True, "待登录")
        elif d.get("success"):
            ck("闸门状态", True, "已进入（已登录）")
        else:
            ck("闸门状态", False, "无法判断：%s" % body[:80])

    print()
    print("RESULT:", "ALL PASS" if not bad else "FAILED %d" % bad)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
