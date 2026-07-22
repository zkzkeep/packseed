# PackSeed 🌱

> 一个自建的辅种（cross-seed）服务，专治 [cross-seed](https://github.com/cross-seed/cross-seed) 辅不上的「多季合集包」。

PackSeed 不靠**文件名解析**来判断种子是否相同，而是靠 **「体积预筛 + 文件列表精确比对」**。因此像 `无耻之徒S01-S11`、`真探 S01-S03` 这种被 cross-seed 以「非标准季集命名」为由拒绝的合集包，PackSeed 照样能辅上。

纯 Python 标准库实现（自写 bencode 解析），无第三方依赖，一个文件跑起来。搭配 [Prowlarr](https://github.com/Prowlarr/Prowlarr) 作为统一的站点搜索后端，配合 [Transmission](https://transmissionbt.com/) 做种。

## 为什么造这个轮子

cross-seed 依赖 release 命名规范来匹配。遇到 PT 站常见的中文合集、多季打包、非标准命名，它会直接跳过。而辅种的本质其实很简单：**同样的文件内容，在另一个站找到对应种子，把 tracker 换过去继续做种**。所以正确的判断依据应该是「文件是否一致」，而不是「名字长得像不像」。PackSeed 就是按这个思路重写的。

## 特性

- 🎯 **按内容比对辅种**：体积预筛（±0.3%）+ 文件列表精确比对，不解析文件名，多季合集包也能辅
- 🔎 **搜索下载**：面板里输入剧名/片名，全站搜索、按做种数排序，一键下载到 qBittorrent
- 🔍 **中英双关键词**：中文名优先搜索，搜不到自动回退英文名
- 🖥️ **Web 面板**：辅种记录、统计、日志一览；点进种子看**来源站**和**辅种去向**
- ✋ **手动兜底**：辅不上的可以自定义关键词重新触发搜索
- 🔁 **内容去重**：同一部剧（名字+大小）只处理一次，辅种产生的副本不会被重复处理
- 🔐 **可选登录**：设置账号密码后启用 HTTP Basic Auth
- 🐳 **一个文件**：纯标准库，`python packseed.py` 即可运行

## 工作流程

```
下载器下好种子 → Transmission 做种
        ↓
PackSeed 定时扫描 Transmission 里的种子
        ↓
用中文名(或英文名回退)去 Prowlarr 搜遍所有站
        ↓
候选种子体积接近? → 下载 .torrent → 文件列表逐一比对
        ↓ 一致
硬链接原文件 → 注入 Transmission → 在新站开始做种
```

## 快速开始

### Docker Compose（推荐）

```yaml
services:
  packseed:
    image: python:3.11-slim
    container_name: packseed
    ports:
      - "2470:2470"
    volumes:
      - ./packseed:/config          # packseed.py 和数据库放这
      - /path/to/your/data:/data     # 和下载器共享的数据目录(硬链接需同一文件系统)
    environment:
      - TZ=Asia/Shanghai
      - TR_URL=http://transmission:9091
      - TR_USER=admin
      - TR_PASS=your_tr_password
      - PROWLARR_URL=http://prowlarr:9696
      - PROWLARR_KEY=your_prowlarr_apikey
      - DATA_LINK_DIR=/data/cross-seed-links
      - SCAN_INTERVAL=1800
      # 下面两个都设了才启用登录，留空则免登录
      - PACKSEED_USER=admin
      - PACKSEED_PASS=your_web_password
    command: python /config/packseed.py
    restart: unless-stopped
```

把 `packseed.py` 放进挂载的 `./packseed` 目录，`docker compose up -d`，浏览器打开 `http://<host>:2470`。

### 直接运行

```bash
TR_URL=http://localhost:9091 TR_USER=admin TR_PASS=xxx \
PROWLARR_URL=http://localhost:9696 PROWLARR_KEY=xxx \
python3 packseed.py
```

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
| `QB_CATEGORY` | 空 | 下载时给 qb 打的分类，留空不设 |

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
