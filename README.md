# 🍊 HGXZ - 黄果下载

自动下载黄果短剧视频到指定目录，带 WebUI 管理界面。

支持增量同步、失败重试、已有视频扫描识别，SQLite 记录下载状态。生成 Emby / Jellyfin 兼容的目录结构与 NFO 元数据。

---

## 功能特点

- **自动发现与下载** — 遍历分类页面，自动发现新专辑并下载全部剧集
- **HLS 逐段下载** — 独立短连接分段下载+自动重试，避免 CDN 长连接断流；支持 AES-128 流解密
- **浏览器级指纹伪装** — 请求走真实 Chrome 的 TLS 指纹 + HTTP/2 + 完整浏览器请求头 + 全站 Cookie 会话，不再被 CDN 按“非浏览器流量”拦截（详见下方「请求伪装说明」）
- **增量同步** — 已下载完成的自动跳过，失败集进入冷却期后定时重试
- **Emby 兼容** — 自动创建 `tvshow.nfo`、`episodedetails.nfo` 和 `poster.jpg`，智能跳过无变化重写
- **多域名故障切换** — 主站异常时自动无缝切换备用域名，恢复后自动切回
- **WebUI 管理** — 仪表盘统计、分类进度、专辑列表（含封面）、剧集明细、实时日志（WebSocket 流式推送）、运行参数查看
- **Unraid 专属支持** — 提供现成的 Unraid Docker 图形化安装模板

---

## 请求伪装说明（本修改版新增）

原版抓分片时用的是 `urllib` + 两个头（`User-Agent: Mozilla/5.0`、`Referer`），在 CDN/WAF 看来处处是破绽，常见表现是分片请求 403、下到一半断流、播放地址返回空。

现在全部 HTTP 请求（分类页、详情页、接口、m3u8、TS 分片、封面）统一走 `HGXZ/browser.py` 的浏览器会话：

| 层次 | 原版 | 现在 |
|------|------|------|
| TLS 指纹（JA3/JA4） | Python OpenSSL 默认指纹 | 真实 Chrome 指纹（curl_cffi impersonate） |
| HTTP 协议 | HTTP/1.1 | HTTP/2 |
| User-Agent | `Mozilla/5.0`（爬虫经典签名） | 完整 Chrome UA，与 `sec-ch-ua` 版本严格一致 |
| 请求头 | 仅 UA + Referer | `Accept` / `Accept-Language` / `sec-ch-ua*` / `sec-fetch-*` / `Accept-Encoding` 全套，按 Chrome 实际顺序发送 |
| 分片请求的 `Referer` | 站点根路径 | 播放页地址（和 hls.js 行为一致） |
| Cookie | 每个请求独立、无状态 | 全站共享 Cookie 罐，站点与 CDN 连续会话 |
| 被拦处理 | 直接失败 | 丢弃会话 → 重开首页/播放页取 Cookie → 重试 |

有个反直觉的点：**分片请求不能带 `Sec-Fetch-User: ?1` / `Upgrade-Insecure-Requests` 这类只有导航请求才有的头**——多带比少带更容易被识别，所以底层默认头被关掉、改由程序完整组装。

`curl_cffi` 缺失时会自动降级为“请求头完备 + 带 Cookie 罐”的 urllib 模式（能过只看请求头的风控，过不了 TLS 指纹检测），日志里有明确提示。

验证指纹（本地执行即可）：

```bash
python -c "
from HGXZ.browser import BrowserSession
import json
s = BrowserSession()
d = json.loads(s.fetch('https://tls.peet.ws/api/all', referer='https://tls.peet.ws/', kind='document'))
print(d['http_version'], d['tls']['ja4'])"
```

输出形如 `h2 t13d1516h2_8daaf6152771_d8a2da3f94cd`，即 Chrome 的 JA4 指纹。

---

## 目录与路径规划

| 用途 | 容器内路径 | Unraid 宿主机路径 | 说明 |
|------|------------|-------------------|------|
| **配置文件** | `/config` | `/mnt/user/appdata/HGXZ/config` | 存放 `config.json` |
| **状态库** | `/data` | `/mnt/user/appdata/HGXZ/data` | 存放 SQLite 状态数据库 `state.sqlite3` |
| **运行日志** | `/logs` | `/mnt/user/appdata/HGXZ/logs` | 存放每日轮转日志 `hgxz.log` |
| **视频媒体库** | `/media` | `/mnt/user/QTZL/黄果` | 下载好的短剧视频与 NFO 元数据 |

---

## 一、Docker 镜像信息

- **镜像地址**：`ghcr.io/huamei-du/hgxz:latest`
- **平台架构**：`linux/amd64`
- **镜像可见性**：**公开（public）**，无需登录即可直接拉取：

  ```bash
  docker pull ghcr.io/huamei-du/hgxz:latest
  ```

---

## 二、Unraid 图形化配置部署指南

### 第一步：准备配置文件目录

在 Unraid 终端中执行以下命令，快速创建目录并将模板配置文件就位：

```bash
mkdir -p /mnt/user/appdata/HGXZ/config /mnt/user/appdata/HGXZ/data /mnt/user/appdata/HGXZ/logs /mnt/user/QTZL/黄果
```

将项目中的 `config.example.json` 复制到 `/mnt/user/appdata/HGXZ/config/config.json`：

```bash
curl -sSL https://raw.githubusercontent.com/Huamei-Du/HGXZ/main/config.example.json -o /mnt/user/appdata/HGXZ/config/config.json
```

> 也可以通过 SMB 共享或 Unraid 文件管理器直接在 `/mnt/user/appdata/HGXZ/config/` 下创建并编辑 `config.json`。

---

### 第二步：添加 Unraid 容器模板

1. 在 Unraid 终端中，将本项目的模板文件下载到 Unraid 用户模板目录：
   ```bash
   curl -sSL https://raw.githubusercontent.com/Huamei-Du/HGXZ/main/unraid/hgxz.xml -o /boot/config/plugins/dockerMan/templates-user/my-hgxz.xml
   ```
2. 进入 Unraid 网页端 **【Docker】** → 点击底部的 **【添加容器】（Add Container）**。
3. 在 **【模板】（Template）** 下拉菜单中选择 **`HGXZ`**（系统会自动填充所有配置项）。
4. 确认各项参数设置无误：

| 设置项 | 字段名 | 填写内容 | 说明 |
|--------|--------|----------|------|
| **名称** | Name | `HGXZ` | 容器名称 |
| **存储库** | Repository | `ghcr.io/huamei-du/hgxz:latest` | 镜像地址（公开，无需登录） |
| **WebUI 端口** | Port: 8099 | `8099` | Web 界面访问端口，可按需修改 |
| **配置文件目录** | Path: /config | `/mnt/user/appdata/HGXZ/config` | 存放 config.json |
| **状态库目录** | Path: /data | `/mnt/user/appdata/HGXZ/data` | SQLite 数据库文件 |
| **日志目录** | Path: /logs | `/mnt/user/appdata/HGXZ/logs` | 运行日志 |
| **媒体目录** | Path: /media | `/mnt/user/QTZL/黄果` | 视频下载保存目录 |
| **时区** | Variable: TZ | `Asia/Shanghai` | 确保定时时间准确 |
| **每日同步时间** | Variable: SCHEDULE | `03:30` | 每天自动同步时间（HH:MM 格式，默认凌晨 03:30） |
| **文件权限掩码** | Variable: UMASK | `000` | 赋予生成文件完全读写权限，避免 Unraid SMB 权限问题 |
| **运行参数** | Post Arguments | `--daemon` | 常驻后台运行 |

> 🌐 **关于网络代理配置（必读）：**
> 国内网络环境下 `huangguoai.com` 受限，**必须通过代理访问**，点击界面下方的 **【显示更多设置...】（Show more settings...）**：
> - `HTTP_PROXY` / `HTTPS_PROXY`：填入你的局域网 HTTP 代理地址（例如 `http://192.168.2.6:10086`，注意填真实局域网 IP，不可填 `127.0.0.1`）。
> - `NO_PROXY`：保持默认的 `127.0.0.1,localhost`。**切勿**将 `huangguoai.com` 加入直连白名单，否则会导致主站超时报错。

5. 确认无误后，点击最下方的 **【应用】（Apply）** 按钮。Unraid 将自动拉取镜像并启动容器。

---

### 第三步：访问与验证

1. 启动完成后，在 **【Docker】** 页面找到 `HGXZ` 容器，点击图标选择 **【查看日志】（Logs）**，确认看到 WebUI 启动日志。
2. 点击容器图标选择 **【WebUI】**，或直接在浏览器访问：
   ```
   http://[你的Unraid主机IP]:8099
   ```
3. 进入界面后即可查看仪表盘、专辑列表与实时同步日志。

---

## 三、Docker CLI 命令行运行（备用）

如果不使用 Unraid 模板，也可以直接在终端运行以下命令：

```bash
docker run -d --name HGXZ \
  --restart unless-stopped \
  -p 8099:8099 \
  -v /mnt/user/appdata/HGXZ/config:/config \
  -v /mnt/user/appdata/HGXZ/data:/data \
  -v /mnt/user/appdata/HGXZ/logs:/logs \
  -v /mnt/user/QTZL/黄果:/media \
  -e TZ=Asia/Shanghai \
  -e SCHEDULE=03:30 \
  -e UMASK=000 \
  -e HTTP_PROXY=http://192.168.2.6:10086 \
  -e HTTPS_PROXY=http://192.168.2.6:10086 \
  -e NO_PROXY=127.0.0.1,localhost \
  ghcr.io/huamei-du/hgxz:latest
```

---

## 三之二、Docker Compose 部署（fnOS / 群晖 / 通用）

飞牛 fnOS、群晖等没有 Unraid 模板的机器，用 Compose 最省事——以后换镜像只要
`docker compose pull && docker compose up -d`，不会踩 `latest` 缓存。

```yaml
services:
  hgxz:
    image: ghcr.io/huamei-du/hgxz:latest
    container_name: HGXZ
    restart: unless-stopped
    init: true                # 让 tini 回收 ffmpeg 子进程，避免僵尸进程堆积
    ports:
      - "8099:8099"
    volumes:
      - /vol1/1000/docker/HGXZ/config:/config    # 必须是宿主机真实目录，里面放 config.json
      - /vol1/1000/docker/HGXZ/data:/data
      - /vol1/1000/docker/HGXZ/logs:/logs
      - /vol2/1000/黄果视频:/media
    environment:
      - TZ=Asia/Shanghai
      - SCHEDULE=03:30        # 每天同步时间
      - UMASK=000
      - HTTP_PROXY=http://192.168.31.128:7890
      - HTTPS_PROXY=http://192.168.31.128:7890
      - NO_PROXY=localhost,127.0.0.1
    command: ["--daemon"]
    logging:
      driver: json-file
      options:
        max-size: "10m"       # 不限制的话 docker logs 会一路吃满系统盘
        max-file: "3"
    healthcheck:
      test: ["CMD", "python", "-c", "import socket;socket.create_connection(('127.0.0.1',8099),3).close()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
```

几个容易踩的点：

- **`command` 必须写成列表**。写成 `command: --daemon   # 注释` 时 YAML 会把
  `--daemon` 和 `#` 后面的内容一起当字符串，多切出一个参数，容器启动即退出。
  YAML 注释必须单独占一行且以 `#` 开头。
- **`/config` 里要真的有 `config.json`**。只挂空目录会启动失败，程序现在会直接
  提示「配置文件不存在」并附上处理方法，不再甩一段 traceback。
- **`NO_PROXY` 别省**。不设的话连 `127.0.0.1` 都会走代理，本地回环请求绕一圈回来
  反而容易出错。
- 环境变量里的 `HTTP_PROXY` / `HTTPS_PROXY` 会由 curl_cffi 自动采用，启动日志会
  打印最终生效的线路（见下），配了没生效一眼就能看出来。

启动后前几行日志长这样，可以用来自查：

```
INFO HGXZ 1.1.0 (build 022ef45 @ 2026-11-13T02:12:19Z)      ← 版本 + 构建 commit
INFO HTTP 传输层: curl_cffi/chrome142（Chrome TLS 指纹 + HTTP/2）
INFO 出站代理: http://192.168.31.128:7890（来自环境变量，直连 localhost,127.0.0.1）
INFO WebUI starting on port 8099
```

`build` 后面的短 SHA 和仓库 `git log` 对得上，就能确定容器跑的是哪一次构建。
若无 `HTTP 传输层` 一行，说明 `curl_cffi` 没装进镜像，伪装不会生效。

---

## 四、WebUI 界面说明

| 页面 | 内容 |
|------|------|
| **仪表盘** | 专辑总数/总集数、已下载集数与占用容量、完整专辑数、失败集数；分类进度条；最近完成的专辑 |
| **专辑列表** | 封面缩略图、标题、分类、完成度进度条（绿色完成 / 红色失败）、状态标签；支持按分类/状态筛选与关键字搜索 |
| **剧集明细** | 点击专辑进入：封面大图、已下载容量、最近下载时间、待下载与失败集数、存储路径；逐集状态列表；单独同步 / 重置失败按钮 |
| **同步日志** | WebSocket 实时推送，支持按级别与关键字过滤、自动滚动开关、清屏 |
| **运行参数** | 只读展示当前生效的配置（主站、备用域名、媒体目录、重试次数、超时等） |

顶部按钮：

- **🔄 立即同步** — 全量遍历所有分类并下载新增剧集
- **📂 扫描已有** — 扫描媒体目录，把已存在的视频注册进状态库（不会重复下载）

> 同步进行中时按钮自动禁用，定时任务与手动同步互斥，不会并发下载同一集。

---

## 五、命令行用法（容器内 / 本地）

daemon 模式由 `--daemon` 显式开启，其余子命令直接运行：

```bash
# 常驻模式（Docker 默认）：立即同步一次，之后每天定时同步，同时开启 WebUI
python -m HGXZ.cli --config /config/config.json --daemon

# 单次同步
python -m HGXZ.cli --config /config/config.json

# 只同步指定专辑（忽略冷却期，强制重试）
python -m HGXZ.cli --config /config/config.json --only-id 12345

# 只写元数据（NFO/封面），不下载视频
python -m HGXZ.cli --config /config/config.json --metadata-only

# 扫描媒体目录，把已有视频注册进状态库
python -m HGXZ.cli --config /config/config.json --scan-existing

# 重建缺失或损坏的封面
python -m HGXZ.cli --config /config/config.json --rebuild-covers

# 限制范围，便于调试
python -m HGXZ.cli --config /config/config.json --max-albums 3 --max-episodes 2
```

在已运行的容器内执行子命令（镜像是常驻模式，需要覆盖入口参数）：

```bash
docker exec HGXZ python -m HGXZ.cli --config /config/config.json --scan-existing
```

---

## 六、配置文件详细说明

配置文件路径：`/mnt/user/appdata/HGXZ/config/config.json`（完整模板见仓库根目录 `config.example.json`）

```json
{
  "site": {
    "base_url": "https://huangguoai.com",
    "backup_urls": [
      "https://l5f9m.rxzfszht.cc",
      "https://blolhh.rxzfszht.cc",
      "https://mxz58.rxzfszht.cc",
      "https://owfv.rxzfszht.cc",
      "https://zd43td.rxzfszht.cc",
      "https://qp0h.rxzfszht.cc",
      "https://olci.rxzfszht.cc",
      "https://j5rs.rxzfszht.cc",
      "https://lhsb.rxzfszht.cc"
    ],
    "request_interval": 2,
    "timeout": 30,
    "browser": { "profile": "chrome" },
    "categories": [
      { "name": "AI成人短剧", "path": "/ai-duanju/", "library": "AI成人短剧" },
      { "name": "AI成人漫剧", "path": "/ai-manju/", "library": "AI成人漫剧" }
    ]
  },
  "download": {
    "root": "/media",
    "retries": 3,
    "minimum_duration": 3,
    "recheck_days": 7,
    "failure_cooldown_hours": 24,
    "episode_timeout": 1800
  },
  "state": { "database": "/data/state.sqlite3" },
  "log": { "directory": "/logs" }
}
```

| 参数项 | 说明 | 默认推荐值 |
|--------|------|------------|
| `site.base_url` | 主站根域名 | `https://huangguoai.com` |
| `site.backup_urls` | 备用域名列表，主站故障时自动切换（建议保留全部 9 个） | 见模板 |
| `site.request_interval` | 请求间隔延迟（秒），防止被站点风控 | `2` |
| `site.timeout` | 单次请求超时（秒） | `30` |
| `site.browser.profile` | 伪装用的浏览器指纹：`chrome` / `chrome142` / `chrome136` / `chrome131`（与站点过不去时可换一个试试） | `chrome` |
| `site.browser.proxy` | 可选，单独给抓取会话指定代理（如 `http://192.168.1.2:7890`）；留空则沿用 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量 | 空 |
| `site.categories` | 抓取的短剧分类与对应生成的 Emby 媒体库目录名 | — |
| `site.category_check` | 详情页分类标签与配置比对的方式：`compat`（默认，包含/缩写都算同一分类，如「短剧」≈「AI成人短剧」）、`off`（完全信任列表页分类，永不判冲突） | `compat` |
| `download.root` | 视频下载根目录（对应容器内 `/media`） | `/media` |
| `download.retries` | 单集重试次数 | `3` |
| `download.minimum_duration` | 视频最短有效时长（秒），过滤无效视频（建议 3 秒以兼容短花絮） | `3` |
| `download.recheck_days` | 已完结专辑再次核查的间隔天数 | `7` |
| `download.failure_cooldown_hours` | 失败集的冷却等待时间（小时），避免重复无效请求 | `24` |
| `download.episode_timeout` | 单集下载总超时时间（秒） | `1800` |
| `state.database` | SQLite 数据库文件路径 | `/data/state.sqlite3` |
| `log.directory` | 日志存放路径（对应容器内 `/logs`） | `/logs` |

---

## 七、Emby / Jellyfin 刮削结构

下载完成后的媒体库结构直接兼容 Emby 和 Jellyfin，无需额外刮削插件：

```
/mnt/user/QTZL/黄果/
├── AI成人短剧/
│   ├── 剧名A [huangguo-12345]/
│   │   ├── tvshow.nfo           # 剧集元数据（标题/分类/标签/完结状态）
│   │   ├── poster.jpg           # 剧集封面（自动解密）
│   │   └── Season 01/
│   │       ├── 剧名A.S01E01.mp4 # 视频文件
│   │       ├── 剧名A.S01E01.nfo # 单集元数据
│   │       ├── 剧名A.S01E02.mp4
│   │       └── 剧名A.S01E02.nfo
│   └── ...
└── AI成人漫剧/
    └── ...
```

在 Emby 中将 `/mnt/user/QTZL/黄果` 添加为**节目（电视节目）**类型的媒体库即可直接识别展示。

> 目录名使用 `[huangguo-专辑ID]` 后缀作为唯一标识。站点改标题时程序会沿用状态库中记录的目录，不会产生重复目录。

---

## 八、常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 容器**退出码 137** | 137 = 128 + 9，进程被 SIGKILL。多数是「停止容器时 10 秒内没退出被强杀」，也可能是内存不足。先看日志最后一行：出现 `收到 SIGTERM，退出` 属正常停止；没有任何信号日志且发生在下载中，就要查内存（`docker stats HGXZ`）或降低同时下载的规模 |
| 容器**退出码 2** | 命令行参数错了。多半是 `command` 那行写了 `command: --daemon   # 说明` 这种行尾注释，被当成多余参数。改成 `command: ["--daemon"]` |
| 日志提示「配置文件不存在」 | `/config` 挂载的宿主机目录里还没有 `config.json`。把 `config.example.json` 复制过去改名即可 |
| 启动日志没有 `HTTP 传输层` 一行 | `curl_cffi` 没装进镜像，TLS 指纹仍是 Python 的。需要重新 `docker pull`（依赖层变了，只重启容器不生效） |
| 启动日志的 `出站代理` 和自己想的不一样 | 该行会说明代理来自 `config.json` 还是环境变量。若显示「直连」而你以为配了，检查变量名是否为 `HTTP_PROXY`/`HTTPS_PROXY`，以及是否写在了 compose 的 `environment` 里 |
| 容器启动后立即退出并报 JSON 解析错误 | `config.json` 内容不合法或为空文件。检查文件是否为合法 JSON |
| 日志出现 `SSL: WRONG_VERSION_NUMBER` 或 `HTTP 404` | DNS 污染。确认 `HTTP_PROXY`/`HTTPS_PROXY` 已设置为局域网代理，且 `NO_PROXY` **不要**包含 `huangguoai.com` |
| 日志出现 `HTTP 403 Forbidden` | 站点风控。程序现在会伪装真实 Chrome 指纹并在被拦后重建会话重试；若仍持续，可尝试换 `site.browser.profile`（如 `chrome136`）、调大 `site.request_interval`，或经代理出网 |
| 日志出现 `BlockedError` / `请求被拦` | 同上，说明 CDN 识别到了异常流量。连续出现时先确认出网 IP 是否被站点拉黑 |
| 日志出现 `未安装 curl_cffi，退化为 urllib 传输层` | 容器/环境里缺 `curl_cffi`，只能挡请求头级检测。重装依赖：`pip install "curl_cffi>=0.7"` |
| 某集一直失败 | 剧集可能已下架，失败会进入 24 小时冷却。可在 WebUI 点「重置失败」立即重试 |
| 界面看不到封面 | 站点封面接口异常时会自动回退到 og:image；若两者都失败，日志中会有 `cover via ... failed` 记录 |
| 重新构建后行为没变 | 先看日志第一行 `HGXZ <版本> (build <commit> @ <时间>)`，对照仓库最新提交的 SHA。对不上说明容器还在用本地缓存的旧镜像——Unraid 的 Apply 不会自动重新拉取 tag 未变的 `latest`，需要在终端 `docker pull ghcr.io/huamei-du/hgxz:latest` 后再 Apply，或删掉旧镜像重拉。WebUI「运行参数」页也会显示同样的版本串 |
| 日志每行都显示两次 | 已修复：页面加载会先拉一次历史日志，WebSocket 连上后服务端又会重推最近 200 条。前端按「时间戳+级别+内容」去重，旧版本镜像仍会出现该现象 |

---

## 许可证

仅供个人学习与自用。
