# CloudMount — 把网盘挂成 NAS 上的真实目录（铁牛OS / 铁牛NAS / ZeroNAS）

> AList 里的 115、阿里云盘、百度网盘等，只存在于它自己的虚拟目录里，别的程序看不见。
> CloudMount 用 rclone 把 AList 的 WebDAV 桥接成 FUSE 挂载，网盘内容就以普通文件夹出现在 NAS 上 ——
> 文件管理器、刮削器、播放器、别的容器都能直接读取、播放、改名。
>
> **一句话：AList 负责「能连上」，CloudMount 负责「看得见」。**

## 功能特性

- **网盘变成本地目录** —— 通过 rclone 的 WebDAV 后端 + FUSE 挂载，把 AList（或任意 WebDAV）挂成宿主机上的真实路径。按需读取，不额外占用磁盘。
- **内置配置页** —— 应用中心详情页点「打开」就是配置页：挂载目录、文件属主、WebDAV 地址与口令都能改，**保存即重新挂载，不用卸载重装**。首次打开需自行设置访问口令。
- **配置持久化** —— 写在 `/volume1/data/cloudmount/cloudmount.conf`。应用升级、覆盖安装重新渲染编排文件时都不会冲掉它。
- **断电 / 强杀后能自己恢复** —— 容器 `restart: unless-stopped`；上次没来得及卸载的**死挂载点**会在下次启动时自动清掉。判据走 `/proc/mounts`，因为死挂载点上 `mountpoint`、`[ -e ]`、`stat` 全都会返回「不是挂载点」，普通探测根本发现不了它。
- **最小权限** —— `SHARE_ROOT` 只把「挂载点所在的那棵子树」共享进容器。收窄之后，容器看不到 AList 的凭据库（`data.db` 里存着你的网盘登录凭据）。
- **本身就一层壳** —— 直接用 rclone 官方镜像，仓库里只有入口脚本、生命周期脚本和一个配置面板，没有私有二进制。

## 适用机型与系统

| 平台 | 支持情况 |
|---|---|
| 铁牛OS / ZeroNAS（Debian 12） | ✅ 已在 ainas-host（Intel N100）实机验证 |
| 铁牛应用中心 | ✅ 上传 `.tpk` 直接安装，配置页挂在「打开」按钮后面 |
| 任意 Linux + Docker | ✅ 用仓库里的 `docker-compose.yml`，需要 `/dev/fuse` 与特权容器 |
| 群晖 / 威联通 / fnOS / 飞牛 | ⚠️ 原理通用但未实测，需自行确认内核 FUSE 与特权容器可用 |

> 搜索关键词：网盘挂载、AList 挂载、rclone 挂载、WebDAV 挂载、115 挂载、阿里云盘挂载、FUSE 挂载、铁牛OS、ZeroNAS、铁牛NAS

## 环境要求

- **Docker**（铁牛自带；其它平台自行安装）
- **宿主内核支持 FUSE** —— `ls -l /dev/fuse` 能看到设备即可，铁牛默认支持
- **宿主有 `python3`** —— **只有配置页需要**，网盘挂载本身不依赖它。没有 python3 时安装脚本会跳过配置页并给出提示，挂载不受影响
- **一个 WebDAV 上游** —— 推荐 [AList](https://github.com/AlistGo/alist)，也可以填任何标准 WebDAV 服务器（另一台 NAS、网盘自带的 WebDAV 都行）

## 包内容

| 文件 | 说明 |
|---|---|
| `config.json` | 应用中心元数据：中英文描述 + 8 个安装参数 |
| `docker-compose.tmpl` | 应用中心使用的编排模板，`{{ .参数 }}` 由安装参数填入 |
| `docker-compose.yml` | Docker 渠道使用的独立编排，读 `.env`（不配也能跑，全部走默认值） |
| `.env.example` | Docker 渠道的环境变量样例 |
| `app/run.sh` | 容器入口（**POSIX sh**，rclone 镜像里没有 bash）：加载持久配置 → 清理残留挂载 → `exec` 起 rclone |
| `panel/panel.py` | 配置页，跑在**宿主机**上（不是容器里），带口令闸门 |
| `cmd/install.sh` | 安装：建挂载点与缓存目录、清死挂载、检查 `/dev/fuse`、拉起配置页 |
| `cmd/start.sh` | 启动：清残留挂载后启动容器，并确保配置页在跑 |
| `cmd/stop.sh` | 停止：停容器、卸挂载、停配置页 |
| `cmd/update.sh` | 升级：保留配置与挂载点，重启到新版本 |
| `cmd/uninstall.sh` | 卸载：清容器、卸挂载、移除配置页 |
| `cmd/panel.sh` | 配置页的 systemd 生命周期（`start` / `stop` / `remove`） |
| `tools/verify.py` | 安装后自检，见下文「验证安装」 |
| `icon.png` | 应用图标 |

## 安装步骤（铁牛OS / ZeroNAS）

### 1. 先在 AList 里加好网盘

打开 `http://<NAS地址>:5244/@manage`（默认 `admin / admin`，**首次登录请改掉**），添加你的网盘存储，并**确认在 AList 里能正常列出文件**。115 网盘这类只能扫码登录的，请先在 AList 里完成设备端授权。

> ⚠️ 这一步最容易被跳过，也是「挂上了但目录是空的」的头号原因。
> CloudMount 对上游**探不到也照挂** —— 存储是坏的，它照样显示 Up，但目录是空的。

### 2. 上传安装包

应用中心 → 本地安装 → 选择 `cloudmount_1.1.5.tpk`。

### 3. 填安装参数

**这里填错的话，挂载点、属主、口令都会跟着错** —— 尤其是 WebDAV 口令，留空等于用默认值 `admin`。

| 参数 | 留空时 | 怎么填 |
|---|---|---|
| 挂载目录 | `/volume1/data/网盘` | 网盘在 NAS 上出现的位置。想让文件管理器「我的文件」里看到，填 `/volume1/data/personal/<你的用户ID>/网盘`。**必须是专用空目录**，会被本应用接管为挂载点 |
| 共享给容器的宿主目录 | `/volume1/data` | **挂载目录的上一级**。填 `/volume1/data` 会把 AList 的凭据库也一起共享进去，建议收窄到你的个人空间，例如 `/volume1/data/personal/<你的用户ID>` |
| 挂载文件属主 UID | `0`（root） | 填你自己的 UID（`id -u <用户名>` 可查）。留空是 root，开了 SMB 共享后 Windows 侧会把这些文件当只读，改名编辑都会失败 |
| 挂载文件属主 GID | `0`（root） | 同上，填你的 GID（`id -g <用户名>` 可查） |
| WebDAV 地址 | `http://host.docker.internal:5244/dav` | 默认指向本机 AList。也可以填别的 WebDAV 服务器 |
| WebDAV 账号 | `admin` | AList 的管理员账号名 |
| WebDAV 口令 | `admin` | **如果你改过 AList 管理员口令，这里必须填新的**，否则挂载会一直空着。口令里不要有英文双引号 |
| 配置页访问口令 | 首次打开页面时自己设 | 至少 6 位。配置页能改挂载目录、重启容器，所以必须设防。忘了见「常见问题」 |

### 4. 首次打开配置页

安装完成后，在应用中心详情页点**「打开」** —— 进入配置页，第一次会让你设一个访问口令。之后挂载目录、属主、AList 地址与口令都可以在这里改，**保存后自动重新挂载**。

> 为什么配置挂在「打开」上：应用中心对已安装的应用只给「打开 / 停用 / 卸载」，没有「设置」入口（`isShowSettings` 由云端元数据计算，自装应用恒为 false）。而安装参数一旦装完就改不了 —— 想改就只能卸载重装，而重装会把参数重置回出厂默认值。配置页绕开了这一整套麻烦。

### 5. 验证

```bash
python3 tools/verify.py
```

## 用 Docker Compose 部署（不用应用中心）

### 1. 下载

取 Release 里的 `nas-cloudmount-v1.1.5.zip`，解压到任意目录。

### 2. 配置

```bash
cp .env.example .env
vi .env          # 至少改 MOUNT_DIR / SHARE_ROOT / WEBDAV_PASSWORD
```

> 国内直连 Docker Hub 多数会超时。如果 `docker compose up -d` 卡在拉镜像，把 `docker-compose.yml`
> 里的 `image:` 改成加速地址，例如 `docker.1ms.run/rclone/rclone:1.75.1`。

### 3. 启动

```bash
# 关键：共享挂载必须先做成 shared，FUSE 挂载才能传播回宿主机
sudo mount --make-rshared /

docker compose up -d
docker compose logs -f app        # 看到 [cloudmount] 启动 rclone mount ... 即成功
```

### 4. 验证

```bash
sudo python3 tools/verify.py
```

### 5. （可选）起配置页

Docker 渠道不带 systemd 托管，但配置页可以直接跑（需要宿主有 python3）：

```bash
CM_APP_DIR=$(pwd) CM_DATA_DIR=/volume1/data/cloudmount sudo -E python3 panel/panel.py
# 浏览器打开 http://<NAS地址>:8791/
```

不想用配置页也行 —— 直接改 `.env` 然后 `docker compose up -d` 重建容器，效果一样。

## 环境变量

`app/run.sh` 认这些变量（`docker-compose.yml` 与 `.env.example` 里都有）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MOUNT_DIR` | `/volume1/data/网盘` | 网盘在 NAS 上出现的位置 |
| `WEBDAV_URL` | `http://host.docker.internal:5244/dav` | WebDAV 端点地址 |
| `WEBDAV_USER` | `admin` | WebDAV 账号 |
| `WEBDAV_PASSWORD` | `admin` | WebDAV 口令（会先转成 rclone 的 obscured 形式再进连接串） |
| `MOUNT_UID` | `0` | 挂载出来的文件属主 UID |
| `MOUNT_GID` | `0` | 挂载出来的文件属主 GID |
| `SHARE_ROOT` | `/volume1/data` | **编排层使用**：共享进容器的宿主目录，即容器的可见边界 |
| `CACHE_DIR` | `/volume1/data/cloudmount/cache` | 读缓存目录（容器内路径是 `/cmcache/cache`） |
| `VFS_CACHE_MAX_SIZE` | `20G` | 读缓存上限 |
| `VFS_CACHE_MODE` | `full` | rclone VFS 缓存模式。WebDAV 不支持流式读写，**必须开缓存** |

配置页可改前 6 项；后 3 项请改 `.env` / 编排文件。

> **配置的权威位置**：`/volume1/data/cloudmount/cloudmount.conf`。
> `run.sh` 启动时会 source 它，**它覆盖编排文件里的同名环境变量**。
> 排查问题时，容器里的实际取值以这个文件为准（存在的话），不是 compose。

## 挂载点该填哪里

| 填法 | 结果 |
|---|---|
| `/volume1/data/网盘`（默认） | 在 NAS 上可见、SMB 可共享，但**不属于任何「空间」** → 铁牛文件管理器里看不到 |
| `/volume1/data/personal/<你的用户ID>/网盘` | 出现在文件管理器「我的文件」里 ✅ 推荐 |
| 指向一个已有数据的目录 | ❌ 不要这样做 —— 该目录会被接管为挂载点 |

文件管理器**不认符号链接**，必须是真挂载点。另外注意：`.tpk` 的安装参数在装完之后改不了，所以**第一次装就填对**，要改请用配置页。

## 权限与安全

这个应用需要的能力，全是 FUSE 挂载的硬性要求，不是「想要 root」：

| 配置 | 为什么必须有 |
|---|---|
| `devices: /dev/fuse` | 容器里要调用内核 FUSE 建挂载 |
| `cap_add: SYS_ADMIN` | `mount()` 系统调用需要这个 capability |
| `security_opt: apparmor:unconfined` | 不解除 AppArmor 限制，`mount` 会直接报 `Permission denied` |
| 挂载点 `:rshared` | 容器里的 FUSE 挂载要**传播回宿主机**，才能变成宿主上的真实目录 |

因此 `SHARE_ROOT` 是唯一能收窄的边界，**建议填成挂载目录的上一级**。默认值 `/volume1/data` 会把这棵子树里的一切（包括别的应用数据）都共享进容器。

其它几点：

- **网盘账号密码不经过本应用** —— 真正的登录凭据只存在 AList 里，CloudMount 拿到的只是 AList 的 WebDAV 账号。凭据在容器里只用于拼一次 rclone 连接串（口令转 obscured 形式）。
- **配置页必须设口令** —— 它能改挂载配置、以 root 执行 `docker restart`，所以不能裸奔。口令用 PBKDF2-HMAC-SHA256（20 万轮）存哈希 + 无状态签名 Cookie，7 天过期。
- **AList 的 WebDAV 默认只监听本机** —— 请勿把 5244 端口直接暴露到公网。

## 常见问题

**挂上了，但目录是空的？** → 九成是上游问题。先去 AList 后台确认能列出文件；再确认 WebDAV 口令填对了（在 AList 改过口令就必须在安装参数或配置页里填新的）。最后看容器日志 `docker logs cloudmount-app`，`[cloudmount] WebDAV :` 那行会打印实际用的地址与账号。

**文件管理器里看不到？** → 挂载点必须落在 `/volume1/data/personal/<你的用户ID>/` 下面，且必须是**真挂载点**（不认符号链接）。挂在 `/volume1/data/` 直下不属任何空间，界面不显示。

**重启后目录点不进去，一直转圈？** → 典型的**死挂载点**（`Transport endpoint is not connected`）。通常是容器被强杀 / 断电导致 rclone 没来得及卸载。正常情况下本应用的启动脚本会自动清掉；如果没清干净，手工 `sudo umount -l <挂载目录>` 再重启应用。

**SMB 共享里这些文件是只读的？** → 属主是 root。把「挂载文件属主 UID/GID」改成你自己的（`id -u` / `id -g` 查），在配置页里改完保存即可。

**在配置页改了挂载目录，老目录还在？** → 启动脚本会把上一个挂载点卸掉（记录在 `/cmcache/.last_mount`）。如果老目录变成一个点进去就卡住的空壳，手工 `sudo umount -l <老目录>`。

**忘了配置页口令？** →
```bash
rm /volume1/data/cloudmount/.panel-auth.json
systemctl restart tie-niu-cloudmount-panel     # 或重启应用
```

**没有 python3 能装吗？** → 能。安装脚本会跳过配置页并给出提示，网盘挂载不受影响，只是没有图形配置入口（改配置要卸载重装）。

**能不用 AList 吗？** → 能。任何标准 WebDAV 都行，把 WebDAV 地址指向它即可。AList 只是最常用的上游。

**容器健康状态显示 unhealthy？** → 健康检查的判据是「`/proc/mounts` 里有没有 `fuse.rclone`」。unhealthy 意味着挂载真的掉了（不是误报），看日志排查上游。

## 验证安装

`tools/verify.py` 在**宿主机**上跑，逐项检查（退出码 0 = 全部正常）：

```bash
sudo python3 tools/verify.py
sudo python3 tools/verify.py --mount-dir /volume1/data/personal/<你的用户ID>/网盘
```

检查内容：

1. 容器 `cloudmount-app` 在运行
2. 挂载点在 `/proc/mounts` 里，且类型为 FUSE
3. 挂载点能被 `ls` 列出（不是可读但卡住的死挂载）
4. **宿主挂载表里没有重复的存储池挂载点** —— 这条专门防「缓存目录自绑定」那个坑：它会泄露出一个与存储池同设备的重复挂载点，导致铁牛把存储池的挂载点认错，文件管理器「我的文件」根列表变空（看起来像文件夹全丢了，数据其实没少）
5. 缓存目录可写且不在容器层
6. 配置页在监听（默认 8791），并区分「待设置口令 / 待登录 / 正常」三种状态

## 相关项目

同系列工具，都在 Centerm Zero 1 Pro（铁牛OS）上实机跑通 —— 纯 Python 标准库、单容器、MIT：

| 项目 | 用途 |
| --- | --- |
| [nas-appinstall](https://github.com/hahaha-9527/nas-appinstall) | 网页版 `.tpk` 上传口子：把本地应用包注册进应用中心并完成安装 / 升级 |
| [nas-tieniuled](https://github.com/hahaha-9527/nas-tieniuled) | 机箱电源灯 / 硬盘灯的可视化控制台 |
| [nas-fanctl](https://github.com/hahaha-9527/nas-fanctl) | 风扇温度调速：按 CPU 与硬盘温度自动调节转速 |

## 版本记录

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE) © 2026 西了个瓜

本应用基于 [rclone](https://github.com/rclone/rclone)（MIT）的**官方镜像**构建，通过编排文件引用，未修改也未再分发其源码。
推荐搭配 [AList](https://github.com/AlistGo/alist) 使用，两者通过标准 WebDAV 协议通信，代码上互相独立。

> ⚠️ FUSE 挂载依赖 FUSE 与特权容器，**网盘挂载目录请务必填一个专用的空目录**。
> 本应用对挂载点具有接管权，误指向已有数据的目录会造成困扰（数据不会丢，但会被挂载遮挡）。
> 使用前请自行评估网盘服务商的条款与账号安全，因账号风控、服务条款变更导致的后果由使用者自行承担。
