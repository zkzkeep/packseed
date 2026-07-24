# 观澜 Wavegazer 🌊

> Wavegazer · 观影观澜——站在岸上看自己的海。(原名 PackSeed)

> 一个人的 PT 全家桶：搜索下载 → 自动刮削入库 Emby → qb 转 tr 保种 → 全站辅种，一个文件全自动。
> 起点是专治 [cross-seed](https://github.com/cross-seed/cross-seed) 辅不上的「多季合集包」，后来把 MoviePilot 的核心流程也装了进来。

PackSeed 不靠**文件名解析**来判断种子是否相同，而是靠 **「体积预筛 + 文件列表精确比对」**。因此像 `无耻之徒S01-S11`、`真探 S01-S03` 这种被 cross-seed 以「非标准季集命名」为由拒绝的合集包，PackSeed 照样能辅上。

纯 Python 标准库实现（自写 bencode 解析），无第三方依赖，一个文件跑起来。搭配 [Prowlarr](https://github.com/Prowlarr/Prowlarr) 作为统一的站点搜索后端，配合 [Transmission](https://transmissionbt.com/) 做种。

## 为什么造这个轮子

cross-seed 依赖 release 命名规范来匹配。遇到 PT 站常见的中文合集、多季打包、非标准命名，它会直接跳过。而辅种的本质其实很简单：**同样的文件内容，在另一个站找到对应种子，把 tracker 换过去继续做种**。所以正确的判断依据应该是「文件是否一致」，而不是「名字长得像不像」。PackSeed 就是按这个思路重写的。

## 特性

- 🎯 **按内容比对辅种**：体积预筛（±0.3%）+ 文件列表精确比对，不解析文件名，多季合集包也能辅
- 🔎 **搜索下载**：面板里输入剧名/片名，全站搜索、按做种数排序，一键下载到 qBittorrent
- 📥 **自动刮削入库**：qb 下载完成 → 识别片名 → TMDB 匹配 → 硬链接进 Emby 媒体库标准目录（`中文名 (年份)/`）→ 通知 Emby 刷新。命名对了 Emby 自带刮削补齐海报简介
- 🔁 **qb→tr 自动转种**：下载完成自动把种子转到 Transmission 做种（数据保留、校验后接管），随后辅种自动跟上
- 🎌 **动漫识别**：番组命名（`[字幕组][标题][01-24]`、`标题 - 01`、`第1123话`）专门解析；长寿番（海贼王/柯南/蜡笔小新的千集话数）自动匹配到正确的原版条目；动画优先不误配真人版
- ✅ **置信度兜底**：识别不准的进「待确认」队列，面板里填 TMDB id 或片名一键入库，绝不静默入错
- 🔍 **中英双关键词**：中文名优先搜索，搜不到自动回退英文名
- 🖥️ **Web 面板**：搜索、辅种记录、整理入库记录、日志一览；点进种子看**来源站**和**辅种去向**
- ✅ **防重复下载**：搜索结果按 TMDB id 比对本地库，已有的直接打绿色「✓ 已入库」角标；媒体页还有库内搜索框，下手前先查一遍
- ✋ **手动兜底**：辅不上的可以自定义关键词重新触发搜索
- 🔁 **内容去重**：同一部剧（名字+大小）只处理一次，辅种产生的副本不会被重复处理
- 🌊 **批量保种**：选站拉种子列表(直连站点真分页),按体积/做种数/🆓免费/🏅官种筛选,或填个目标总量全自动拉满;下载完自动转 tr 做种;内建节流和磁盘保护线,不塞爆盘不打爆站
- ⚡ **抢免费守候**：定时盯站,新出的免费种自动抢下做种回吐上传——新手刷考核的救命稻草,不用人肉 F5;可设为**只抢官种**(有些站保种考核只认官方组发布)
- 🧭 **缺种报告**：每个内容在哪些站做种、哪些站搜不到,一张矩阵看清转种机会
- 🚚 **半自动发种资料包**：一键生成主标题/副标题/TMDB 简介 bbcode,再点一下自动出 **MediaInfo(ffprobe)+ 均匀抽帧截图**(自托管走面板公网地址,不依赖墙外图床),复制去目标站发种;**带禁转/独家标记的直接硬拦截,不给确认后门**
- 🔐 **可选登录**：设置账号密码后启用;自带克莱因蓝登录页(海浪视频背景、Cookie 会话 30 天免登),脚本仍可走 HTTP Basic Auth
- 🐳 **一个文件**：纯标准库，`python packseed.py` 即可运行

## 工作流程

```
面板搜索 → 一键推送 qb 下载
        ↓ 下载完成(自动检测)
   ┌────┴─────────────┐
   ↓                  ↓
识别→TMDB匹配        转种到 Transmission 保种
→硬链接进媒体库          ↓ (数据保留,tr校验接管)
   ↓             PackSeed 扫描 tr,全站辅种:
通知 Emby 刷新     Prowlarr 搜索 → 体积预筛 →
(海报简介自动补齐)   文件清单精确比对 → 硬链接注入
```

硬链接三方共享同一份文件——qb 下载的、Emby 媒体库里的、tr 做种的都是同一份，不重复占空间。

## 快速开始

### 你需要准备的邻居们

| 服务 | 必要性 | 作用 |
|---|---|---|
| [qBittorrent](https://www.qbittorrent.org/) | **必装** | 下载器 |
| [Transmission](https://transmissionbt.com/) | **必装** | 保种做种(建议 3.00,PT 全站白名单) |
| [Prowlarr](https://github.com/Prowlarr/Prowlarr) | **必装** | 站点聚合搜索(把你的 PT 站都加进去) |
| TMDB API Key | 推荐 | 识别/海报/简介(themoviedb.org 免费申请,国内需配代理) |
| [Emby](https://emby.media/)/Jellyfin | 推荐 | 播放媒体库 |
| [LrcApi](https://github.com/HisAtri/LrcApi) | 选配 | 音乐歌词 |
| 企业微信自建应用 | 选配 | 通知+微信点播 |

### 起观澜

```yaml
services:
  guanlan:
    image: python:3.11-slim
    container_name: guanlan
    ports:
      - "2470:2470"
    volumes:
      - ./guanlan:/config              # packseed.py、数据库、settings.json 都在这
      - /path/to/your/data:/data       # 与下载器共享的数据目录(硬链接需同一文件系统)
    environment:
      - TZ=Asia/Shanghai
      # 可选:面板登录(都设置才启用)
      - PACKSEED_USER=admin
      - PACKSEED_PASS=your_password
    command: python /config/packseed.py
    restart: unless-stopped
```

把 `packseed.py` 放进 `./guanlan`,`docker compose up -d`,浏览器打开 `http://<host>:2470`。

### 三分钟接线

打开 **⚙️ 设置** 标签页 → 填入各服务的地址和密钥 → 点 **🔌 测试全部连接** 逐项验证 → **💾 保存**(热生效,无需重启)。

> 地址怎么填?同一台机器上的服务用宿主机内网 IP(如 `http://192.168.1.100:8080`);
> 同一个 compose 里的服务可以用服务名(如 `http://qbittorrent:8080`,host 网络的服务除外)。
> 配置保存在 `/config/settings.json`,优先级高于环境变量。

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TR_URL` | `http://transmission:9091` | Transmission RPC 地址 |
| `TR_USER` / `TR_PASS` | `admin` / 空 | Transmission 账号密码 |
| `PROWLARR_URL` | `http://prowlarr:9696` | Prowlarr 地址 |
| `PROWLARR_KEY` | 空 | Prowlarr API Key（设置→通用里找）|
| `DATA_LINK_DIR` | `/data/cross-seed-links` | 硬链接目标目录（容器内路径）|
| `SCAN_INTERVAL` | `1800` | 扫描间隔（秒）|
| `SIZE_TOLERANCE` | `0.003` | 体积预筛容差（0.3%）|
| `SNATCH_DELAY` | `2` | 每次下载 .torrent 之间的间隔（秒），别把站搜爆 |
| `PORT` | `2470` | Web 端口 |
| `DB_PATH` | `/config/packseed.db` | sqlite 数据库路径 |
| `PACKSEED_USER` / `PACKSEED_PASS` | 空 | Web 登录账号密码，**都设置**才启用 |
| `QB_URL` | `http://qbittorrent:8080` | 搜索下载的目标 qBittorrent 地址 |
| `QB_USER` / `QB_PASS` | `admin` / 空 | qb 账号密码；**留空则不登录**，靠 qb 的子网白名单免密（见下）|
| `QB_CATEGORY` | 空 | 强制 qb 分类；留空则按识别类型自动打 电影/电视剧/动漫 |
| `TMDB_KEY` | 空 | TMDB v3 API Key（免费申请），**整理入库的开关前提** |
| `TMDB_PROXY` | 空 | TMDB 走的代理（国内需要），如 `http://192.168.1.1:7890` |
| `ORGANIZE` | `1` | 下载完成自动整理入库+转种流水线 |
| `MEDIA_TV` / `MEDIA_MOVIE` | `/data/media/tv` `/movies` | Emby 剧集/电影库根目录（容器内路径）|
| `MEDIA_ANIME` | 空 | 动漫库根目录，留空则动漫也归到 tv 库 |
| `EMBY_URL` / `EMBY_KEY` | 空 | Emby 地址和 API Key，入库后通知刷新（可选）|
| `TR_SEED_DIR` | 空 | 转种到 tr 的数据目录，留空=用 qb 的保存目录 |
| `KEEP_DIR` | `/data/downloads/keepseed` | 批量保种专用目录（容器内路径）：保种种子全部隔离在此，**不辅种/不入库**，到期删目录即清仓 |
| `KEEP_MIN_FREE_GB` | `200` | 批量保种磁盘保护线：剩余空间低于此值任务自动暂停 |
| `FREE_WATCH_MIN` | `5` | 抢免费守候的巡查间隔（分钟），底线 3 分钟 |
| `FREE_MAX_GB` | `30` | 守候只抢不超过此体积的免费种，0=不限 |
| `FREE_OFFICIAL` | `0` | 守候只抢官种（1=开），应付「只认官种」的站点考核 |

> 发种资料包的 **MediaInfo + 截图** 功能需要容器内有 `ffmpeg/ffprobe`（放在 `/config/bin/` 或装进 PATH 均可），截图走 `PUBLIC_URL` 自托管，请确保面板公网可达。

## 前置条件

- **Prowlarr** 已接好你的各个 PT 站（PackSeed 复用它的站点列表，无需重复配置）
- **Transmission** 做种，且 PackSeed 能通过 RPC 访问
- **qBittorrent**（搜索下载功能需要）：可填账号密码，或在 qb「设置→WebUI→对 IP 子网白名单中的客户端跳过身份验证」里加上 Docker 内网段（如 `172.16.0.0/12`），PackSeed 免密连接更省事
- `DATA_LINK_DIR` 和你的做种文件在**同一文件系统**（硬链接不能跨盘）

## 说明

- 硬链接不占额外空间，辅种的多个副本共享同一份文件。
- 首次运行会全量扫描一遍现有种子，之后按 `SCAN_INTERVAL` 增量。
- 搜索失败的内容有 6 小时冷却，避免反复搜同一个搜不到的种子。

## License

MIT
