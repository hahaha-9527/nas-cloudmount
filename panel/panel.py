#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网盘挂载（CloudMount）—— 配置面板

为什么要单独有这个东西
----------------------
铁牛应用中心的详情页对「已安装」的应用只给 打开 / 停用 / 卸载 三项，
**没有「设置」入口**（那个是官方应用独有的，字段 isShowSettings 由云端元数据算出来，
自装应用恒为 false）。而本应用的挂载目录、文件属主、WebDAV 口令全都来自安装参数 ——
想改就得「卸载 → 再带参数安装」，对普通用户等于不可用。

正解是用**同一个按钮组里那个能用的一项：「打开」**。
tpk 的 config.json 里声明 accessCtrl.urlAppAccesses 之后，应用中心会自动根据它
拼出访问地址，点「打开」就跳到这里。

本面板跑在**宿主机**上（不是容器里，rclone 镜像里没有 python），由
cmd/install.sh 与 cmd/start.sh 通过 systemd 拉起，stop / uninstall 时停掉。

配置的权威位置
--------------
    /volume1/data/cloudmount/cloudmount.conf

容器入口 app/run.sh 启动时会 source 这个文件，**它覆盖 compose 里的同名环境变量**。
这样做的好处：应用中心重新渲染 docker-compose.yaml（升级、覆盖安装）时
只改得到 env，改不动这个文件，所以面板里设的配置不会被冲掉。

安全
----
本页面的「保存」会以 root 在 NAS 上执行 docker 命令、改写挂载配置，
所以必须有访问闸门：PBKDF2-HMAC-SHA256 口令 + 无状态签名 Cookie，
口令优先级 = 环境变量 PANEL_PASSWORD > 已有记录 > 「首次打开由使用者自己设」。
记录文件跟着数据目录走（升级不重置）。忘了口令的兜底：
    rm /volume1/data/cloudmount/.panel-auth.json && systemctl restart tie-niu-cloudmount-panel
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------------- 常量

APP_DIR = os.environ.get("CM_APP_DIR") or "/volume1/@appstore/com.centerm.docker.cloudmount"
DATA_DIR = os.environ.get("CM_DATA_DIR") or "/volume1/data/cloudmount"
CONF_FILE = os.path.join(DATA_DIR, "cloudmount.conf")
AUTH_FILE = os.path.join(DATA_DIR, ".panel-auth.json")
LOG_FILE = os.environ.get("CM_PANEL_LOG") or "/var/log/cloudmount_panel.log"
CONTAINER = os.environ.get("CM_CONTAINER") or "cloudmount-app"
DEFAULT_MOUNT = "/volume1/data/网盘"
DEFAULT_DAV = "http://host.docker.internal:5244/dav"

PORT = int((os.environ.get("CM_PANEL_PORT") or "").strip() or "8791")
BIND = (os.environ.get("CM_PANEL_BIND") or "0.0.0.0").strip() or "0.0.0.0"

COOKIE = "cm_panel_session"
SESSION_TTL = 7 * 24 * 3600
PBKDF2_ROUNDS = 200000

# 面板版本号（与应用包版本保持一致，页脚会上屏）
VERSION = "1.1.5"

# 面板能改的键（顺序即页面上的顺序）。CACHE_DIR 不在其中：它是容器内路径，
# 由 compose 写死，跟着缓存绑定走，不让用户在这里改。
CONF_KEYS = ["MOUNT_DIR", "MOUNT_UID", "MOUNT_GID", "WEBDAV_URL", "WEBDAV_USER", "WEBDAV_PASSWORD"]

_lock = threading.Lock()
AUTH = None          # None = 还没设置口令（「待设置」状态）
SECRET = b""
_fail_count = 0


# --------------------------------------------------------------------------- 工具

def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write("[log rotated]\n")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    sys.stdout.write(line)
    sys.stdout.flush()


def sh(cmd, timeout=30):
    """跑一条宿主命令，返回 (rc, stdout, stderr)。"""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "命令超时：%s" % cmd
    except Exception as e:                                    # noqa: BLE001
        return -1, "", str(e)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def write_text(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def read_mounts():
    return read_text("/proc/mounts")


def is_mounted(path, mounts=None):
    """判据必须走 /proc/mounts —— 死挂载点上 os.path.ismount / stat 全都会返回 False。"""
    if not path:
        return False
    mounts = mounts if mounts is not None else read_mounts()
    target = path.replace(" ", "\\040")
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == target:
            return True
    return False


# --------------------------------------------------------------------------- 认证

def _hash_pw(password, salt, rounds=PBKDF2_ROUNDS):
    return base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)).decode()


def _new_record(password, source):
    salt = os.urandom(16)
    rec = {
        "salt": base64.b64encode(salt).decode(),
        "hash": _hash_pw(password, salt),
        "rounds": PBKDF2_ROUNDS,
        "secret": os.urandom(32).hex(),
        "source": source,
        "updated": int(time.time()),
    }
    return rec


def _save_auth(rec):
    global AUTH, SECRET
    AUTH = rec
    SECRET = bytes.fromhex(rec["secret"])
    write_text(AUTH_FILE, json.dumps(rec, ensure_ascii=False, indent=2))


def load_auth():
    """优先级：已有记录 > 环境变量 > 待设置。

    与"环境变量每一次都以它为准"的常见做法不同，这里**记录优先**：本面板允许
    使用者在页面上自己改口令，如果每次重启都被安装参数里的 PANEL_PASSWORD 顶回去，
    改了等于没改。安装时给的口令因此只在"还没有记录"时作为初始口令生效。
    忘了口令的兜底（也是 root 侧的重置通道）：
        rm /volume1/data/cloudmount/.panel-auth.json && systemctl restart tie-niu-cloudmount-panel
    删掉记录后重启，若安装参数里带了口令就恢复成它，否则回到「首次打开由访问者设定」。
    """
    global AUTH, SECRET
    env_pw = (os.environ.get("PANEL_PASSWORD") or "").strip()
    rec = None
    try:
        rec = json.loads(read_text(AUTH_FILE))
    except Exception:
        rec = None

    if rec and isinstance(rec, dict) and rec.get("hash") and rec.get("secret"):
        AUTH = rec
        SECRET = bytes.fromhex(rec["secret"])
        return

    if env_pw:
        log("按安装参数 PANEL_PASSWORD 建立初始访问口令")
        _save_auth(_new_record(env_pw, "env"))
        return

    AUTH = None
    SECRET = b""
    log("尚未设置访问口令：打开页面后由第一个访问者自行设置")


def check_password(password):
    global _fail_count
    if not AUTH:
        return False
    salt = base64.b64decode(AUTH.get("salt") or "")
    ok = hmac.compare_digest(AUTH.get("hash", ""), _hash_pw(password, salt, AUTH.get("rounds", PBKDF2_ROUNDS)))
    if ok:
        _fail_count = 0
        return True
    _fail_count += 1
    time.sleep(min(4.0, 0.4 * (2 ** min(_fail_count, 4))))
    return False


def _sign(payload):
    return hmac.new(SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session():
    payload = "v1.%d" % (int(time.time()) + SESSION_TTL)
    return "%s.%s" % (payload, _sign(payload))


def verify_session(value):
    if not value or not SECRET:
        return False
    try:
        payload, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return False
        return int(payload.split(".")[1]) > time.time()
    except Exception:
        return False


def set_password(password):
    pw = (password or "").strip()
    if len(pw) < 6:
        return False, "口令至少 6 位"
    _save_auth(_new_record(pw, "page"))
    log("访问口令已设置/更新（%d 位）" % len(pw))
    return True, ""


# --------------------------------------------------------------------------- 配置读写

def parse_conf():
    """读持久配置（run.sh 用的就是同一份）。返回 dict。"""
    cfg = {}
    text = read_text(CONF_FILE)
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z_]+)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key not in CONF_KEYS:
            continue
        if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
            val = val[1:-1].replace("'\\''", "'")
        cfg[key] = val
    return cfg


def compose_env():
    """compose 里的环境变量（面板没设过的键，值就来自这里）。"""
    env = {}
    text = read_text(os.path.join(APP_DIR, "docker-compose.yaml"))
    for line in text.splitlines():
        m = re.match(r'^\s*-\s*"?([A-Z_]+)=([^"]*)"?\s*$', line)
        if m and m.group(1) in CONF_KEYS:
            env[m.group(1)] = m.group(2).strip()
    return env


def effective_conf():
    """实际生效值：conf 覆盖 compose env，最后落默认值。"""
    eff = dict(compose_env())
    eff.update(parse_conf())
    eff.setdefault("MOUNT_DIR", "")
    if not eff.get("MOUNT_DIR"):
        eff["MOUNT_DIR"] = DEFAULT_MOUNT
    if not (eff.get("WEBDAV_URL") or "").strip():
        eff["WEBDAV_URL"] = DEFAULT_DAV
    if not (eff.get("WEBDAV_USER") or "").strip():
        eff["WEBDAV_USER"] = "admin"
    return eff


def _quote(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def save_conf(cfg):
    """写持久配置。写前备份，写后 0600。"""
    lines = [
        "# 网盘挂载（CloudMount）持久配置 —— 由配置面板写入，app/run.sh 启动时 source。",
        "# 这里的值覆盖 docker-compose.yaml 里的同名环境变量，",
        "# 所以应用中心升级/覆盖安装（只会重渲染 compose）不会把它冲掉。",
        "# 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
    ]
    for k in CONF_KEYS:
        if k in cfg:
            lines.append("%s=%s" % (k, _quote(cfg[k])))
    text = "\n".join(lines) + "\n"

    old = read_text(CONF_FILE)
    if old:
        os.makedirs(os.path.join(DATA_DIR, "_bak"), exist_ok=True)
        try:
            with open(os.path.join(DATA_DIR, "_bak", "cloudmount.conf.%d" % int(time.time())), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(old)
        except Exception:
            pass
    write_text(CONF_FILE, text, 0o600)
    return text


# --------------------------------------------------------------------------- 状态采集

def _inspect(fmt):
    rc, out, _ = sh("docker inspect -f %s %s" % (shlex.quote(fmt), shlex.quote(CONTAINER)), timeout=15)
    return out if rc == 0 else ""


def container_status():
    if not _inspect("{{.Id}}"):
        return {"exists": False, "state": "absent", "health": ""}
    state = _inspect("{{.State.Status}}") or "unknown"
    health = _inspect("{{if .State.Health}}{{.State.Health.Status}}{{end}}")
    started = _inspect("{{.State.StartedAt}}")
    return {"exists": True, "state": state, "health": health, "startedAt": started}


def count_entries(path, limit=200):
    try:
        n = 0
        names = []
        with os.scandir(path) as it:
            for e in it:
                n += 1
                if len(names) < limit:
                    names.append(e.name)
        return n, sorted(names)
    except Exception as e:                                    # noqa: BLE001
        return -1, [str(e)]


def dir_size(path, cap=20 * 1024 ** 3):
    """粗略统计目录大小（读到 cap 就停，避免大缓存拖死面板）。"""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except Exception:
                    pass
                if total > cap:
                    return total, True
    except Exception:
        pass
    return total, False


def probe_upstream(url, timeout=4):
    """探上游是否活着。⚠️ 别用 rclone lsd 的退出码：AList 里有一条坏存储就永远非 0。"""
    import urllib.request
    api = url.rstrip("/")
    if api.endswith("/dav"):
        api = api[:-4]
    # 面板跑在**宿主机**上，而 WEBDAV_URL 是给**容器**用的（容器里 host.docker.internal
    # 才解析得到宿主机）。不换这一下，页面上永远显示「AList 连不上」—— 实测踩到过。
    api = api.replace("//host.docker.internal:", "//127.0.0.1:")
    api = api + "/api/public/settings"
    t0 = time.time()
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "cloudmount-panel"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, int(r.status), int((time.time() - t0) * 1000), api
    except Exception as e:                                    # noqa: BLE001
        return False, 0, int((time.time() - t0) * 1000), "%s（%s）" % (api, e)


def space_candidates():
    """给出「文件管理器里看得见」的挂载位置候选 —— 用户不必知道自己的 UID。"""
    out = []
    for base, label in (("/volume1/data/personal", "个人空间（我的文件）"),
                        ("/volume1/data/share", "共享空间")):
        try:
            uids = sorted([d for d in os.listdir(base) if d.isdigit()],
                          key=lambda x: int(x))
        except Exception:
            continue
        for uid in uids:
            out.append({
                "path": os.path.join(base, uid, "网盘"),
                "label": "%s · 用户 %s" % (label, uid),
                "uid": uid,
            })
    out.append({"path": DEFAULT_MOUNT, "label": "默认位置（不属于任何空间，文件管理器里看不到）", "uid": ""})
    return out


def fm_hint(mount_dir):
    """给一条「文件管理器能不能看到」的提示。"""
    m = re.match(r"^/volume1/data/(personal|share)/(\d+)(/|$)", mount_dir or "")
    if m:
        return True, "在「%s」空间内，文件管理器能看到" % ("我的文件" if m.group(1) == "personal" else "共享空间")
    if (mount_dir or "").startswith("/volume1/data/"):
        return False, "挂在 /volume1/data 直下 —— 不属于任何空间，文件管理器里看不到。建议改到 personal/<UID>/ 下"
    return False, "不在 /volume1/data 之内，容器可能访问不到（共享范围只覆盖 /volume1/data）"


def collect_state():
    eff = effective_conf()
    mount = eff.get("MOUNT_DIR") or DEFAULT_MOUNT
    mounts = read_mounts()
    mounted = is_mounted(mount, mounts)
    n, names = count_entries(mount) if os.path.isdir(mount) else (-1, [])
    visible, hint = fm_hint(mount)
    size, capped = dir_size(os.path.join(DATA_DIR, "cache"))
    up_ok, up_code, up_ms, up_where = probe_upstream(eff.get("WEBDAV_URL") or DEFAULT_DAV)

    stray = []
    for line in mounts.splitlines():
        p = line.split()
        if len(p) >= 2 and "fuse" not in p[2] and p[1].startswith("/volume1/data"):
            stray.append(p[1])

    rc, du, _ = sh("df -h %s 2>/dev/null | tail -n1" % shlex.quote("/volume1"), timeout=10)

    return {
        "container": container_status(),
        "conf": eff,
        "confFile": CONF_FILE,
        "confOverrides": sorted(set(parse_conf().keys())),
        "mount": {
            "path": mount,
            "mounted": mounted,
            "exists": os.path.isdir(mount),
            "entries": n,
            "names": names[:40],
            "visibleInFileManager": visible,
            "hint": hint,
            "owner": "%s:%s" % (eff.get("MOUNT_UID") or "0", eff.get("MOUNT_GID") or "0"),
        },
        "upstream": {
            "url": eff.get("WEBDAV_URL") or DEFAULT_DAV,
            "ok": up_ok,
            "code": up_code,
            "ms": up_ms,
            "where": up_where,
        },
        "cache": {
            "dir": os.path.join(DATA_DIR, "cache"),
            "bytes": size,
            "capped": capped,
            "df": du,
        },
        "strayMounts": stray,
        "candidates": space_candidates(),
        "serverTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "needSetup": AUTH is None,
        "authSource": (AUTH or {}).get("source", ""),
    }


# --------------------------------------------------------------------------- 动作

def do_save(new_cfg):
    """保存配置：备份 → 写 conf → 卸载旧挂载点 → 重启容器 → 等挂载落地。"""
    old = effective_conf()
    old_mount = old.get("MOUNT_DIR") or DEFAULT_MOUNT
    new_mount = (new_cfg.get("MOUNT_DIR") or "").strip() or DEFAULT_MOUNT
    if not new_mount.startswith("/"):
        return {"ok": False, "error": "挂载目录必须是绝对路径"}
    norm = new_mount.rstrip("/") or "/"
    if norm in ("/", "/volume1", "/volume1/data", "/volume1/data/personal", "/volume1/data/share"):
        return {"ok": False, "error": "挂载目录不能是 %s 本身，请指向它下面的一个子目录" % norm}
    new_cfg["MOUNT_DIR"] = new_mount

    save_conf(new_cfg)
    log("配置已保存：MOUNT_DIR=%s UID:GID=%s:%s DAV=%s" % (
        new_mount, new_cfg.get("MOUNT_UID") or "0", new_cfg.get("MOUNT_GID") or "0",
        new_cfg.get("WEBDAV_URL") or DEFAULT_DAV))

    notes = []
    # 改了挂载目录：先把老挂载点卸掉，否则它变死挂载，用户在文件管理器里
    # 会看到一个点进去卡住的老「网盘」。
    if old_mount != new_mount and is_mounted(old_mount):
        rc, _, err = sh("umount -l %s" % shlex.quote(old_mount), timeout=20)
        notes.append("已卸载旧挂载点 %s%s" % (old_mount, "" if rc == 0 else "（%s）" % err))
        log("卸载旧挂载点 %s rc=%s %s" % (old_mount, rc, err))
        # 老位置留着的空目录会在文件管理器里变成一个点进去没内容的「网盘」，
        # 容易让人以为挂载坏了。只在确实为空时删掉（rmdir 非递归，不冒风险）。
        try:
            if os.path.isdir(old_mount) and not os.listdir(old_mount):
                os.rmdir(old_mount)
                notes.append("已删除旧位置的空目录 %s" % old_mount)
        except Exception as e:                                # noqa: BLE001
            notes.append("旧目录 %s 保留（%s），不需要可以自己在文件管理器里删" % (old_mount, e))

    try:
        os.makedirs(new_mount, exist_ok=True)
        uid = (new_cfg.get("MOUNT_UID") or "").strip()
        gid = (new_cfg.get("MOUNT_GID") or "").strip()
        if uid.isdigit() and gid.isdigit():
            os.chown(new_mount, int(uid), int(gid))
    except Exception as e:                                    # noqa: BLE001
        notes.append("建目录/改属主时出错：%s" % e)

    rc, out, err = sh("docker restart %s" % shlex.quote(CONTAINER), timeout=120)
    if rc != 0:
        return {"ok": False, "error": "重启容器失败：%s" % (err or out), "notes": notes}

    # 等挂载落地（run.sh 要先等上游就绪，最多 40 秒，这里给 75 秒）
    ok = False
    for _ in range(75):
        if is_mounted(new_mount):
            ok = True
            break
        time.sleep(1)
    if ok:
        notes.append("挂载已就绪：%s" % new_mount)
    else:
        rc2, tail, _ = sh("docker logs --tail 25 %s 2>&1" % shlex.quote(CONTAINER), timeout=20)
        notes.append("75 秒内没等到挂载点，容器日志末尾：\n%s" % tail)
    log("保存流程结束，mounted=%s" % ok)
    return {"ok": True, "mounted": ok, "notes": notes}


def do_remount():
    if not _inspect("{{.Id}}"):
        return {"ok": False, "error": "容器不存在，请到应用中心重新安装"}
    rc, out, err = sh("docker restart %s" % shlex.quote(CONTAINER), timeout=120)
    if rc != 0:
        return {"ok": False, "error": "重启失败：%s" % (err or out)}
    mount = effective_conf().get("MOUNT_DIR") or DEFAULT_MOUNT
    ok = False
    for _ in range(75):
        if is_mounted(mount):
            ok = True
            break
        time.sleep(1)
    return {"ok": True, "mounted": ok, "notes": ["挂载点 %s %s" % (mount, "已就绪" if ok else "仍未出现，看容器日志")]}


# --------------------------------------------------------------------------- HTTP

# 页头 / 桌面快捷方式用的图标（与应用中心里那张 icon.png 同源；png 取不到时回落到这个）。
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
    '<defs>'
    '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#1b3d4d"/><stop offset="1" stop-color="#091e28"/>'
    '</linearGradient>'
    '<linearGradient id="bl" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#7ceaff"/><stop offset="1" stop-color="#1f9db2"/>'
    '</linearGradient>'
    '</defs>'
    '<rect width="128" height="128" rx="30" fill="url(#bg)"/>'
    '<rect x="3.5" y="3.5" width="121" height="121" rx="27" fill="none"'
    ' stroke="#35c3d6" stroke-opacity=".35" stroke-width="3"/>'
    '<path d="M40 84h48a17 17 0 0 0 1.6-33.9A24 24 0 0 0 42.6 47 17 17 0 0 0 40 84z"'
    ' fill="none" stroke="url(#bl)" stroke-width="7" stroke-linejoin="round"/>'
    '<path d="M64 58v26M53 74l11 11 11-11" fill="none" stroke="#eafcff"'
    ' stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>'
    '</svg>'
)

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网盘挂载 · 配置</title>
<link rel="icon" href="/icon.png">
<script>/* 主题预置：默认深色，用户手动切过则用其选择，避免首屏闪白 */
try{ if((localStorage.getItem('cloudmount-theme')||'dark')==='light')
  document.documentElement.classList.add('light'); }catch(e){}</script>
<style>
:root{
  --bg:#000000; --panel:#0d0d0d; --panel2:#181818; --line:#000000;
  --txt:#f3ece1; --dim:#9c9c9c; --accent:#35c3d6; --warn:#f0a020; --ok:#4cc38a; --bad:#e5484d;
}
/* 浅色主题：白底黑字，辅助文字深灰（与风扇调速 / 灯控中心同一套取值） */
html.light{
  --bg:#ffffff; --panel:#ffffff; --panel2:#f3f3f3; --line:#dcdcdc;
  --txt:#111111; --dim:#555555; --accent:#0b8fa5; --warn:#b96e00;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;padding:24px;zoom:1.5}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:20px;font-weight:600;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--panel2);color:var(--accent);border:1px solid var(--line);font-weight:400}
.hdr{margin-left:auto;display:flex;gap:8px;align-items:center}
.sub{color:var(--dim);font-size:12.5px;margin:6px 0 18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
/* 深色模式：边框全黑，卡片靠阴影区分；标题类文字纯白 */
html:not(.light) .card{box-shadow:0 0 0 1px rgba(255,255,255,.05),0 6px 22px rgba(0,0,0,.55)}
html:not(.light) h1{color:#ffffff}
.card h2{font-size:13px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;display:flex;align-items:center;gap:8px}
html:not(.light) .card h2{color:#ffffff}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:0 0 auto}
.g{background:var(--ok)}.r{background:var(--bad)}.y{background:var(--warn)}.n{background:var(--dim)}
.rows{display:grid;grid-template-columns:124px 1fr;gap:6px 12px;font-size:13px}
.k{color:var(--dim)}
.v{word-break:break-all}
label{display:block;font-size:12.5px;margin:12px 0 4px;color:var(--dim)}
label .tip{color:var(--dim);font-size:11.5px;font-weight:400;margin-left:6px;opacity:.82}
input,select{width:100%;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);color:var(--txt);font:inherit;font-size:13px}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
button{background:var(--accent);color:#04222a;border:0;border-radius:8px;padding:8px 18px;font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:var(--panel2);color:var(--txt);border:1px solid var(--line);font-weight:400}
button:hover{filter:brightness(1.1)}
button:disabled{opacity:.5;cursor:not-allowed}
.bar{display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap}
.msg{margin-top:12px;padding:9px 12px;border-radius:8px;font-size:12.5px;white-space:pre-wrap;display:none;background:var(--panel2)}
.msg.ok{border-left:3px solid var(--ok);color:var(--ok);display:block}
.msg.err{border-left:3px solid var(--bad);color:var(--bad);display:block}
.msg.info{border-left:3px solid var(--accent);color:var(--accent);display:block}
.names{color:var(--dim);font-size:12px;word-break:break-all;margin-top:6px}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.chip{border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:12px;cursor:pointer;background:var(--panel2);color:var(--txt);font-family:inherit;font-weight:400}
.chip:hover{border-color:var(--accent);color:var(--accent)}
#gate{max-width:420px;margin:10vh auto}
.hide{display:none!important}
.foot{text-align:center;color:var(--dim);font-size:12px;margin:20px 0 26px;letter-spacing:.4px}
.foot b{color:var(--accent);font-weight:600}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--ok);color:#03230f;padding:8px 20px;border-radius:20px;font-weight:600;font-size:13px;opacity:0;transition:.3s;pointer-events:none;z-index:9}
#toast.err{background:var(--bad);color:#fff}
#toast.show{opacity:1}
/* 手机端：取消 150% 缩放，单列布局，间距收紧 */
@media(max-width:700px){
  body{zoom:1;padding:12px}
  h1{font-size:17px}
  .hdr{margin-left:0;width:100%}
  .row2{grid-template-columns:1fr}
  .rows{grid-template-columns:104px 1fr}
  .card{padding:13px}
  #themeBtn .theme-txt{display:none}
}
</style>
</head>
<body>
<div class="wrap">

<div id="gate" class="card hide">
  <h2><span class="dot y"></span><span id="gateTitle">设置访问口令</span></h2>
  <p class="sub" id="gateDesc"></p>
  <label>口令<span class="tip">至少 6 位</span></label>
  <input id="pw1" type="password" autocomplete="new-password">
  <div id="pw2wrap"><label>再输一次</label><input id="pw2" type="password" autocomplete="new-password"></div>
  <div class="bar"><button id="gateBtn">确定</button></div>
  <div id="gateMsg" class="msg"></div>
</div>

<div id="main" class="hide">
  <h1><img src="/icon.png" alt="" style="width:30px;height:30px" onerror="this.src='/icon.svg';this.onerror=null">网盘挂载 <span class="badge">配置</span>
    <span class="hdr"><button class="ghost" id="themeBtn" style="padding:4px 12px;font-size:13px"></button></span>
  </h1>
  <div class="sub">把 AList 里的网盘变成 NAS 上的真实目录。这里改完即时生效，不需要卸载重装。</div>

  <div class="card">
    <h2><span id="sDot" class="dot n"></span>运行状态</h2>
    <div class="rows" id="status"></div>
    <div class="names" id="names"></div>
  </div>

  <div class="card">
    <h2>挂载设置</h2>
    <label>挂载目录<span class="tip">文件管理器要看得见，就选它下面列出的位置</span></label>
    <input id="MOUNT_DIR" placeholder="/volume1/data/personal/1000/网盘">
    <div class="chips" id="cands"></div>
    <div class="row2">
      <div><label>文件属主 UID<span class="tip">留空 = 0 (root)</span></label><input id="MOUNT_UID" placeholder="1000"></div>
      <div><label>文件属主 GID<span class="tip">留空 = 0 (root)</span></label><input id="MOUNT_GID" placeholder="1001"></div>
    </div>
    <label>AList 地址<span class="tip">留空 = 本机 AList</span></label>
    <input id="WEBDAV_URL" placeholder="http://host.docker.internal:5244/dav">
    <div class="row2">
      <div><label>AList 账号<span class="tip">留空 = admin</span></label><input id="WEBDAV_USER" placeholder="admin"></div>
      <div><label>AList 口令<span class="tip">改过 AList 口令就必须填这里</span></label><input id="WEBDAV_PASSWORD" type="password"></div>
    </div>
    <div class="bar">
      <button id="saveBtn">保存并重新挂载</button>
      <button id="remountBtn" class="ghost">只重新挂载</button>
      <button id="refreshBtn" class="ghost">刷新状态</button>
      <button id="pwBtn" class="ghost">改访问口令</button>
    </div>
    <div id="msg" class="msg"></div>
    <div class="names" id="confInfo"></div>
  </div>
</div>

<div class="foot">© 2026 网盘挂载 <b>__VERSION__</b> · Crafted by 西了个瓜</div>
</div>
<div id="toast"></div>
<script>
var S = null;
function q(id){return document.getElementById(id)}
function show(el){el.classList.remove('hide')}
function hide(el){el.classList.add('hide')}
function msg(text, cls){var m=q('msg');m.textContent=text;m.className='msg '+(cls||'info')}

async function api(path, body){
  var opt = {headers:{'Content-Type':'application/json'}};
  if(body){opt.method='POST';opt.body=JSON.stringify(body)}
  var r = await fetch(path, opt);
  var d = null;
  try{ d = await r.json() }catch(e){ d = {ok:false, error:'返回不是 JSON（HTTP '+r.status+'）'} }
  return d;
}

function gate(mode, text){
  hide(q('main')); show(q('gate'));
  q('gateTitle').textContent = (mode==='setup') ? '设置访问口令' : '需要登录';
  q('gateDesc').textContent = text || '';
  q('pw2wrap').style.display = (mode==='setup') ? '' : 'none';
  q('gateBtn').textContent = (mode==='setup') ? '设置并进入' : '登录';
  q('gateBtn').dataset.mode = mode;
}

function fmtSize(n){ if(n<0) return '未知'; var u=['B','KB','MB','GB','TB']; var i=0; while(n>=1024&&i<u.length-1){n/=1024;i++} return n.toFixed(i?1:0)+' '+u[i] }

async function load(){
  var d = await api('/api/state');
  if(d.needSetup){ gate('setup','这个页面能改挂载配置，所以要先设一个访问口令（不需要 root）。'); return }
  if(d.needLogin){ gate('login','请输入访问口令。'); return }
  if(!d.ok){ msg(d.error||'读取失败','err'); return }
  S = d;
  hide(q('gate')); show(q('main'));
  render(d);
}

function render(d){
  var c = d.container, m = d.mount, u = d.upstream;
  var cls = c.state==='running' && m.mounted ? 'g' : (c.state==='running' ? 'y' : 'r');
  q('sDot').className = 'dot '+cls;
  var rows = [
    ['容器', c.exists ? (c.state + (c.health ? ' · ' + c.health : '')) : '不存在'],
    ['挂载点', (m.mounted ? '已挂载' : '未挂载') + ' · ' + m.path],
    ['文件管理器', m.visibleInFileManager ? '可见' : '看不到'],
    ['目录内容', m.entries<0 ? '打不开' : (m.entries + ' 个条目')],
    ['文件属主', m.owner],
    ['AList', (u.ok ? '正常（HTTP '+u.code+'，'+u.ms+'ms）' : '连不上') + ' · ' + u.where],
    ['读缓存', fmtSize(d.cache.bytes) + (d.cache.capped?' (已截断统计)':'') + (d.cache.df ? ' · ' + d.cache.df : '')],
  ];
  q('status').innerHTML = rows.map(function(r){
    return '<div class="k">'+r[0]+'</div><div class="v">'+esc(r[1])+'</div>';
  }).join('');
  q('names').textContent = m.entries>0 ? ('内容预览：' + m.names.join('、')) : (m.hint||'');
  if(!m.visibleInFileManager && m.hint){ q('names').textContent = m.hint + (m.entries>0 ? ' ｜ 内容预览：'+m.names.join('、') : '') }

  q('MOUNT_DIR').value = d.conf.MOUNT_DIR || '';
  q('MOUNT_UID').value = d.conf.MOUNT_UID || '';
  q('MOUNT_GID').value = d.conf.MOUNT_GID || '';
  q('WEBDAV_URL').value = d.conf.WEBDAV_URL || '';
  q('WEBDAV_USER').value = d.conf.WEBDAV_USER || '';
  q('WEBDAV_PASSWORD').value = d.conf.WEBDAV_PASSWORD || '';

  q('cands').innerHTML = '';
  (d.candidates||[]).forEach(function(c){
    var b = document.createElement('button');
    b.className='chip'; b.textContent=c.label; b.title=c.path;
    b.onclick = function(){
      q('MOUNT_DIR').value = c.path;
      if(c.uid){ q('MOUNT_UID').value = c.uid; if(!q('MOUNT_GID').value) q('MOUNT_GID').value='1001' }
    };
    q('cands').appendChild(b);
  });

  q('confInfo').textContent = '配置文件：' + d.confFile + (d.confOverrides && d.confOverrides.length ? '（面板已覆盖：'+d.confOverrides.join('、')+'）' : '（尚未由面板改过，当前值来自安装参数）');
}

var _toastT = null;
function toast(text, bad){
  var t = q('toast'); t.textContent = text; t.className = bad ? 'err show' : 'show';
  clearTimeout(_toastT); _toastT = setTimeout(function(){ t.className = bad ? 'err' : '' }, 2600);
}

var ICON_SUN='<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>';
var ICON_MOON='<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
function applyTheme(t){
  document.documentElement.classList.toggle('light', t==='light');
  q('themeBtn').innerHTML = (t==='light' ? ICON_MOON : ICON_SUN) +
    '<span class="theme-txt"> ' + (t==='light' ? '深色模式' : '浅色模式') + '</span>';
  try{ localStorage.setItem('cloudmount-theme', t) }catch(e){}
}
q('themeBtn').onclick = function(){ applyTheme(document.documentElement.classList.contains('light') ? 'dark' : 'light') };
applyTheme(document.documentElement.classList.contains('light') ? 'light' : 'dark');

function esc(s){ s = (s===null||s===undefined) ? '' : String(s);
  return s.replace(/[&<>"]/g, function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]}) }

q('gateBtn').onclick = async function(){
  var mode = this.dataset.mode;
  var p1 = q('pw1').value, p2 = q('pw2').value;
  if(mode==='setup'){
    if(p1 !== p2){ q('gateMsg').className='msg err'; q('gateMsg').textContent='两次输入不一致'; return }
    this.disabled = true;
    var d = await api('/api/setup', {password:p1});
    this.disabled = false;
    if(d.ok){ q('gateMsg').className='msg ok'; q('gateMsg').textContent='已设置'; load() }
    else { q('gateMsg').className='msg err'; q('gateMsg').textContent=d.error||'设置失败' }
  } else {
    this.disabled = true;
    var d2 = await api('/api/login', {password:p1});
    this.disabled = false;
    if(d2.ok){ q('gateMsg').className='msg ok'; q('gateMsg').textContent='登录成功'; load() }
    else { q('gateMsg').className='msg err'; q('gateMsg').textContent=d2.error||'口令错误' }
  }
};

q('saveBtn').onclick = async function(){
  if(!confirm('保存会重启挂载容器，网盘会短暂断开几秒，确定继续？')) return;
  var body = {
    MOUNT_DIR:q('MOUNT_DIR').value.trim(), MOUNT_UID:q('MOUNT_UID').value.trim(),
    MOUNT_GID:q('MOUNT_GID').value.trim(), WEBDAV_URL:q('WEBDAV_URL').value.trim(),
    WEBDAV_USER:q('WEBDAV_USER').value.trim(), WEBDAV_PASSWORD:q('WEBDAV_PASSWORD').value
  };
  this.disabled = true; msg('正在保存并重新挂载，最多等 75 秒…','info');
  var d = await api('/api/save', body);
  this.disabled = false;
  if(!d.ok){ msg('保存失败：'+(d.error||''),'err'); return }
  msg((d.mounted?'✅ 已重新挂载成功':'⚠️ 操作已完成但没等到挂载点') + (d.notes&&d.notes.length?('\n'+d.notes.join('\n')):''), d.mounted?'ok':'err');
  load();
};

q('remountBtn').onclick = async function(){
  this.disabled = true; msg('正在重新挂载…','info');
  var d = await api('/api/remount', {});
  this.disabled = false;
  msg((d.ok?(d.mounted?'✅ 挂载已就绪':'⚠️ 仍未挂上'):'失败') + (d.notes?('\n'+d.notes.join('\n')):''), d.ok&&d.mounted?'ok':'err');
  load();
};

q('refreshBtn').onclick = function(){ load() };

q('pwBtn').onclick = async function(){
  var oldP = prompt('当前口令：'); if(oldP===null) return;
  var newP = prompt('新口令（至少 6 位）：'); if(newP===null) return;
  var d = await api('/api/passwd', {old:oldP, password:newP});
  if(d.ok){ toast('口令已修改，其它设备需要重新登录'); }
  else { toast('修改失败：'+(d.error||''), true) }
};

load();
</script>
</body>
</html>
""".replace("__VERSION__", VERSION)


class Handler(BaseHTTPRequestHandler):
    server_version = "CloudMountPanel/1.1.5"

    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------------- 基础输出

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, ok, session=None, **kw):
        kw["ok"] = ok
        extra = {}
        if session:
            extra["Set-Cookie"] = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
                COOKIE, session, SESSION_TTL)
        self._send(200, json.dumps(kw, ensure_ascii=False), extra=extra)

    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == name:
                    return unquote(v.strip())
        return ""

    def _authed(self):
        return verify_session(self._cookie(COOKIE))

    def _need_login(self):
        """保护所有 /api/*（读接口也算：状态里含路径与口令字段）。"""
        if AUTH is None:
            self._json(False, needSetup=True, error="尚未设置访问口令")
            return True
        if self._authed():
            return False
        self._json(False, needLogin=True, error="请先登录")
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------------------------------------------------------------- 路由

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path in ("/icon.png", "/favicon.ico", "/icon.svg"):
            # 应用图标：优先用包内那张 icon.png（跟应用中心里显示的是同一张），
            # 取不到就回落到内嵌 SVG —— 页头不留裂图。
            blob = None
            if path != "/icon.svg":
                try:
                    with open(os.path.join(APP_DIR, "icon.png"), "rb") as f:
                        blob = (f.read(), "image/png")
                except Exception:
                    blob = None
            if blob is None:
                blob = (ICON_SVG.encode("utf-8"), "image/svg+xml; charset=utf-8")
            return self._send(200, blob[0], blob[1])
        if path == "/api/state":
            if self._need_login():
                return
            st = collect_state()
            st["needSetup"] = False
            return self._json(True, **st)
        if path == "/healthz":
            return self._send(200, "ok", "text/plain; charset=utf-8")
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/setup":
            if AUTH is not None:
                return self._json(False, error="口令已设置过，请直接登录")
            ok, why = set_password(body.get("password") or "")
            if not ok:
                return self._json(False, error=why)
            return self._json(True, session=issue_session())

        if path == "/api/login":
            if AUTH is None:
                return self._json(False, needSetup=True, error="尚未设置访问口令")
            if not check_password(body.get("password") or ""):
                return self._json(False, error="口令错误")
            return self._json(True, session=issue_session())

        if path == "/api/passwd":
            if self._need_login():
                return
            if not check_password(body.get("old") or ""):
                return self._json(False, error="当前口令错误")
            ok, why = set_password(body.get("password") or "")
            if not ok:
                return self._json(False, error=why)
            # 改口令顺带换了会话密钥 → 其它设备立刻掉线
            return self._json(True, session=issue_session())

        if path == "/api/save":
            if self._need_login():
                return
            cfg = {}
            for k in CONF_KEYS:
                if k in body:
                    cfg[k] = body[k]
            with _lock:
                res = do_save(cfg)
            return self._json(res.get("ok", False), **{k: v for k, v in res.items() if k != "ok"})

        if path == "/api/remount":
            if self._need_login():
                return
            with _lock:
                res = do_remount()
            return self._json(res.get("ok", False), **{k: v for k, v in res.items() if k != "ok"})

        return self._send(404, "not found", "text/plain; charset=utf-8")

    def _cookie_header(self):
        val = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (COOKIE, self.__last_session, SESSION_TTL)
        return {"Set-Cookie": val}

    @property
    def __last_session(self):
        return getattr(self, "_session_val", "")

    def _json(self, ok, session=None, **kw):        # noqa: F811  (覆盖上面的简版)
        kw["ok"] = ok
        extra = {}
        if session:
            self._session_val = session
            extra["Set-Cookie"] = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
                COOKIE, session, SESSION_TTL)
        self._send(200, json.dumps(kw, ensure_ascii=False), extra=extra)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        os.makedirs(os.path.join(DATA_DIR, "cache"), exist_ok=True)
    except Exception:
        pass
    load_auth()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log("配置面板已启动：http://%s:%d/  （口令来源：%s）" % (BIND, PORT, (AUTH or {}).get("source", "待设置")))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
