#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PackSeed —— 辅种服务 (cross-seed 替代)
按"大小粗筛 + 文件清单精确比对"辅种，绕过名字解析，能辅跨季合集。
纯标准库：无第三方依赖。自带 sqlite 记录 + 网页仪表盘。
"""
import os, re, json, time, base64, shutil, sqlite3, threading, urllib.request, urllib.parse, socket, traceback, hashlib
import unicodedata, difflib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

socket.setdefaulttimeout(25)

CFG = {
    "TR_URL":       os.environ.get("TR_URL", "http://transmission:9091"),
    "TR_USER":      os.environ.get("TR_USER", "admin"),
    "TR_PASS":      os.environ.get("TR_PASS", ""),
    "PROWLARR_URL": os.environ.get("PROWLARR_URL", "http://prowlarr:9696"),
    "PROWLARR_KEY": os.environ.get("PROWLARR_KEY", ""),
    "DATA_LINK_DIR":os.environ.get("DATA_LINK_DIR", "/data/cross-seed-links"),  # 容器内，硬链接目标
    "SCAN_INTERVAL":int(os.environ.get("SCAN_INTERVAL", "1800")),  # 秒
    "SIZE_TOLERANCE":float(os.environ.get("SIZE_TOLERANCE", "0.003")),
    "SNATCH_DELAY": float(os.environ.get("SNATCH_DELAY", "2")),
    "PORT":         int(os.environ.get("PORT", "2470")),
    "DB":           os.environ.get("DB_PATH", "/config/packseed.db"),
    "AUTH_USER":    os.environ.get("PACKSEED_USER", ""),   # 设了才启用登录
    "AUTH_PASS":    os.environ.get("PACKSEED_PASS", ""),
    "QB_URL":       os.environ.get("QB_URL", "http://qbittorrent:8080"),  # 搜索下载的目标下载器
    "QB_USER":      os.environ.get("QB_USER", "admin"),
    "QB_PASS":      os.environ.get("QB_PASS", ""),
    "QB_CATEGORY":  os.environ.get("QB_CATEGORY", ""),     # 兜底分类，留空则按识别的类型(电影/电视剧/动漫)
    "MIN_SEEDERS":  int(os.environ.get("MIN_SEEDERS", "20")),  # 搜索结果做种数门槛;整体都低时保留前20%
    # 单站超时。实测 12s→9s 让整体从 15.3s 降到 9.3s,锚定命中几乎无损(大明王朝 97 条一条没少,
    # 白色巨塔 58→52,丢的是慢站上的重复种)。不敢再压到 7s:主力站 TTG 实测最慢 7.6s,会被误伤。
    "SEARCH_TIMEOUT":  int(os.environ.get("SEARCH_TIMEOUT", "9")),
    "SEARCH_DEADLINE": int(os.environ.get("SEARCH_DEADLINE", "22")),  # 全局硬截止(秒),再慢也不等了
    "SEARCH_QUORUM":   int(os.environ.get("SEARCH_QUORUM", "80")),    # 多少%的站回来就算「大部队到齐」
    "SEARCH_GRACE":  float(os.environ.get("SEARCH_GRACE", "3")),      # 大部队到齐后再给掉队的几秒
    # 手动指定的主力站(逗号分隔,按站名模糊匹配)。这些站慢也要等它回来才收网。
    # 注意:搜索总时长 = 最慢的那个主力站 + 宽限,所以这个名单直接决定搜索能多快。
    # 实测(2026-08-02,各3轮取中位)这批都在 7.6 秒内,天花板是 TTG 的 7.1s。
    # 故意没放 HDFans(19.8s) 和 AGSVPT(21.6s):这俩一进名单,每次搜索都得拖到 22 秒硬截止,
    # 而它们的产出(11~42条)还不如馒头(25~100条),多半是重复资源。要它们就自己往后加。
    "MAJOR_SITES": os.environ.get("MAJOR_SITES",
        "M-Team,OurBits,UBits,HDArea,HDSky,ToTheGlory,SpringSunday,HDHome,CHDBits,PTerClub,Audiences,HHanClub,Keep Friends,U2"),
    # 别名兜底:主查询捞不到几条时,才拿 TMDB 的原名/译名再搜一遍。
    # 别做成「跟主查询同一波并发」—— Prowlarr 对同一个站的请求是排队的,同波双词实测让整批从
    # 4.6s 涨到 8.8s(翻倍),而国内 PT 站本来就同时索引中文名和原名,搜「白色巨塔」照样命中
    # Shiroi Kyotou 的种。实测三部经典:别名多花 0.6~4.4 秒,只多认领 0~5 条,绝大多数时候纯浪费。
    "SEARCH_ALIAS": os.environ.get("SEARCH_ALIAS", "1") == "1",
    "SEARCH_ALIAS_MIN": int(os.environ.get("SEARCH_ALIAS_MIN", "5")),  # 主查询认领少于这个数才触发兜底
    "DOUBAN_PROXY": os.environ.get("DOUBAN_PROXY", ""),               # 豆瓣代理,国内直连留空
    "TR_BAN_SITES": os.environ.get("TR_BAN_SITES", ""),   # ban了tr客户端的站(tr3.00全站通行,默认空)
    # —— 企业微信通知(从 MP 迁移) ——
    "WECOM_CORPID": os.environ.get("WECOM_CORPID", ""),
    "WECOM_SECRET": os.environ.get("WECOM_SECRET", ""),
    "WECOM_AGENTID":os.environ.get("WECOM_AGENTID", ""),
    "WECOM_PROXY":  os.environ.get("WECOM_PROXY", ""),     # 企微API代理,留空直连 qyapi.weixin.qq.com
    "NOTIFY_START": int(os.environ.get("NOTIFY_START", "9")),   # 免打扰:只在此小时段内推送
    "NOTIFY_END":   int(os.environ.get("NOTIFY_END", "22")),
    "WECOM_TOKEN":  os.environ.get("WECOM_TOKEN", ""),     # 企微回调 Token(双向交互)
    "WECOM_AESKEY": os.environ.get("WECOM_AESKEY", ""),    # 企微回调 EncodingAESKey(43位)
    "PUBLIC_URL":   os.environ.get("PUBLIC_URL", ""),      # 面板公网地址(图文通知的海报要外网可达)
    # —— 整理器（识别+刮削入库）——
    "TMDB_KEY":     os.environ.get("TMDB_KEY", ""),
    "TMDB_PROXY":   os.environ.get("TMDB_PROXY", ""),      # TMDB 走代理(国内需要)，如 http://x:7890
    "MEDIA_TV":     os.environ.get("MEDIA_TV", "/data/media/tv"),
    "MEDIA_MOVIE":  os.environ.get("MEDIA_MOVIE", "/data/media/movies"),
    "MEDIA_ANIME":  os.environ.get("MEDIA_ANIME", ""),     # 动漫库根，留空则动漫也归到 tv
    "MEDIA_MUSIC":  os.environ.get("MEDIA_MUSIC", "/data/media/music"),  # 音乐库根(Navidrome 的库)
    "LRCAPI_URL":   os.environ.get("LRCAPI_URL", ""),      # lrcapi 地址,音乐入库时自动落 .lrc 歌词
    "EMBY_URL":     os.environ.get("EMBY_URL", ""),
    "EMBY_KEY":     os.environ.get("EMBY_KEY", ""),
    "ORGANIZE":     os.environ.get("ORGANIZE", "1") == "1",  # 下载完成自动整理入库+转种
    "TR_SEED_DIR":  os.environ.get("TR_SEED_DIR", ""),    # 转种到 tr 时的数据目录(容器内)，留空=用 qb 的保存目录
    "KEEP_MIN_FREE_GB": int(os.environ.get("KEEP_MIN_FREE_GB", "200")),  # 批量保种磁盘保护线:剩余低于此值自动暂停
    "KEEP_DIR": os.environ.get("KEEP_DIR", "/data/downloads/keepseed"),  # 保种专用目录:与正常下载隔离,不辅种不入库,到期整锅清
    "FREE_WATCH_IX": os.environ.get("FREE_WATCH_IX", ""),   # 抢免费守候的站点(Prowlarr索引器id),空=关闭
    "FREE_WATCH_MIN": int(os.environ.get("FREE_WATCH_MIN", "5")),   # 守候刷新间隔(分钟)
    "FREE_MAX_GB": int(os.environ.get("FREE_MAX_GB", "30")),        # 守候单种体积上限(GB),0=不限
    "FREE_OFFICIAL": os.environ.get("FREE_OFFICIAL", "0") == "1",   # 守候只抢官种(有些站保种考核只认官种)
    "BACKUP_KEEP":   int(os.environ.get("BACKUP_KEEP", "7")),       # 自动备份保留份数(每天备份 DB+settings)
    "LOG_KEEP_DAYS": int(os.environ.get("LOG_KEEP_DAYS", "30")),    # 日志保留天数,定期清理防膨胀
    # 辅种每轮预算:一轮最多辅几份内容。首轮全库铺开时这个数直接决定 Prowlarr 的压力 ——
    # 4000 份内容 × 几十个站,不设预算就是自己 DDoS 自己。默认 15,30 分钟一轮 ≈ 每天 720 份,
    # 全库首轮约一周跑完;之后全覆盖的内容自动退出队列,是收敛的,不像老版每轮都全量重来。
    "CROSSSEED_BUDGET": int(os.environ.get("CROSSSEED_BUDGET", "15")),
}

# ============ 设置中心: /config/settings.json 覆盖环境变量,网页可改,热生效 ============
SETTINGS_FILE = os.path.join(os.path.dirname(CFG["DB"]), "settings.json")

def _coerce(k, v):
    cur = CFG.get(k)
    if isinstance(cur, bool): return str(v) in ("1", "true", "True", "on")
    if isinstance(cur, int):
        try: return int(v)
        except Exception: return cur
    if isinstance(cur, float):
        try: return float(v)
        except Exception: return cur
    return str(v)

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as _f:
                saved = json.load(_f)
            for k, v in saved.items():
                if k in CFG and k in SETTABLE:
                    CFG[k] = _coerce(k, v)
    except Exception as e:
        print("settings.json 加载失败:", e, flush=True)

def save_settings(d):
    cur = {}
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as _f:
                cur = json.load(_f)
    except Exception: pass
    for k, v in d.items():
        if k in SETTABLE and k in CFG:
            CFG[k] = _coerce(k, v)
            cur[k] = CFG[k]
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as _f:
        json.dump(cur, _f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_FILE)
    try: os.chmod(SETTINGS_FILE, 0o600)   # 里面有密钥,只许属主读写
    except Exception: pass

# 网页可配置字段: (分组, [(键, 标签, 提示, 是否密文)])
SETTING_GROUPS = [
    ("🔐 面板登录", [
        ("AUTH_USER", "面板用户名", "两项都填才启用登录;清空即关闭。保存立即生效,浏览器会要求重新登录", False),
        ("AUTH_PASS", "面板密码", "", True),
    ]),
    ("⬇️ 下载与保种(必配)", [
        ("QB_URL", "qBittorrent 地址", "如 http://192.168.1.100:8080(容器同网可用 http://qbittorrent:8080)", False),
        ("QB_USER", "qb 用户名", "配了子网白名单免密可留空", False),
        ("QB_PASS", "qb 密码", "", True),
        ("TR_URL", "Transmission 地址", "如 http://192.168.1.100:9091", False),
        ("TR_USER", "tr 用户名", "", False),
        ("TR_PASS", "tr 密码", "", True),
    ]),
    ("🔍 站点搜索(必配)", [
        ("PROWLARR_URL", "Prowlarr 地址", "如 http://192.168.1.100:9696", False),
        ("PROWLARR_KEY", "Prowlarr API Key", "Prowlarr 设置→通用 里复制", True),
        ("SEARCH_TIMEOUT", "单站超时(秒)", "某个站超过这个时间没回就丢下它,默认12。调大=更全但更慢", False),
        ("SEARCH_DEADLINE", "全局截止(秒)", "到点带着已有结果直接收网,默认22。这两项决定搜索最长要等多久", False),
        ("SEARCH_ALIAS", "别名兜底补搜", "填 1 = 主查询没捞着几条时,再用 TMDB 的原名/译名补搜一波(中文名搜不到的冷门外剧靠它)。同站请求 Prowlarr 会排队,所以只在需要时才补;填 0 = 永远只搜你输的词", False),
        ("MAJOR_SITES", "主力站(慢也要等)", "逗号分隔,按站名模糊匹配,如 M-Team,OurBits,UBits。这些站再慢也等它回来才收网;其余小站超时就丢下。留空=全靠历史产出自动判断", False),
        ("SEARCH_QUORUM", "收网门槛(%)", "按各站历史产出加权,达到这个比例就准备收网。调低=更快但结果略少,默认80", False),
        ("SEARCH_GRACE", "收网宽限(秒)", "主力站全回来后,再给掉队的小站几秒,默认3。这三项一起决定搜索快慢", False),
    ]),
    ("🎬 识别与刮削(推荐)", [
        ("TMDB_KEY", "TMDB API Key", "themoviedb.org 免费申请 v3 key,识别/海报/简介全靠它", True),
        ("TMDB_PROXY", "TMDB 代理", "国内必填,如 http://192.168.1.100:7890", False),
        ("DOUBAN_PROXY", "豆瓣代理", "首页豆瓣榜单用。国内直连留空,只有出海机器才需要填", False),
        ("LRCAPI_URL", "LrcApi 地址(歌词)", "选配,如 http://192.168.1.100:28883", False),
    ]),
    ("📚 媒体库", [
        ("MEDIA_TV", "剧集库路径(容器内)", "", False),
        ("MEDIA_MOVIE", "电影库路径(容器内)", "", False),
        ("MEDIA_ANIME", "动漫库路径(容器内)", "留空则动漫归入剧集库", False),
        ("MEDIA_MUSIC", "音乐库路径(容器内)", "", False),
        ("EMBY_URL", "Emby 地址", "选配,入库后通知刷新+钉身份", False),
        ("EMBY_KEY", "Emby API Key", "Emby 控制台→高级→API 密钥", True),
    ]),
    ("📱 企业微信通知(选配)", [
        ("WECOM_CORPID", "企业 ID", "", False),
        ("WECOM_AGENTID", "应用 AgentId", "", False),
        ("WECOM_SECRET", "应用 Secret", "", True),
        ("WECOM_TOKEN", "回调 Token(双向交互)", "", True),
        ("WECOM_AESKEY", "回调 EncodingAESKey", "43位", True),
        ("WECOM_PROXY", "企微 API 代理", "可信IP方案用,留空直连", False),
    ]),
    ("🌊 批量保种(选用)", [
        ("KEEP_DIR", "保种专用目录", "容器内路径。批量保种的种子全部隔离在此:不辅种/不入库/不打扰正常下载,到期删此目录+tr按目录删种即整体清仓", False),
        ("KEEP_MIN_FREE_GB", "磁盘保护线(GB)", "剩余空间低于此值,保种任务自动暂停,防止塞爆盘", False),
        ("FREE_WATCH_MIN", "抢免费守候间隔(分钟)", "盯站频率,别低于3分钟(刷太狠会被站盯上)", False),
        ("FREE_MAX_GB", "抢免费单种上限(GB)", "守候只抢不超过此体积的免费种,0=不限。守候开关在「保种转种」页", False),
        ("FREE_OFFICIAL", "守候只抢官种", "填 1 = 只抢站点官方组发布的种(有些站保种考核只认官种);填 0 = 都要", False),
    ]),
    ("⚙️ 其他", [
        ("PUBLIC_URL", "本面板公网地址", "图文通知海报要用,如 https://seed.example.com", False),
        ("MIN_SEEDERS", "搜索结果做种数门槛", "", False),
        ("SCAN_INTERVAL", "辅种扫描间隔(秒)", "", False),
        ("TR_BAN_SITES", "tr被ban站点黑名单", "逗号分隔,命中站点不注入", False),
        ("BACKUP_KEEP", "自动备份保留份数", "每天自动备份 DB+settings 到 /config/backups,保留最近 N 份", False),
        ("LOG_KEEP_DAYS", "日志保留天数", "超过此天数的活动日志定期清理,防数据库膨胀", False),
    ]),
]
SETTABLE = {k for _, fs in SETTING_GROUPS for k, _, _, _ in fs} | {"FREE_WATCH_IX"}   # 守候站点id由保种页按钮写入,不进表单
load_settings()

# ============ DB ============
def db():
    c = sqlite3.connect(CFG["DB"], timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c

SITE_MAP = {"totheglory":"TTG","ptchdbits":"CHDBits","chdbits":"CHDBits","keepfrds":"朋友FRDS","frds":"朋友FRDS",
    "springsunday":"SSD","pterclub":"PTer","audiences":"观众","13city":"13City","btschool":"BTSchool","carpt":"CarPT",
    "hdsky":"HDSky","hdhome":"HDHome","hddolby":"HDDolby","hdarea":"HDArea","ourbits":"OurBits","hdfans":"HDFans",
    "hhanclub":"憨憨","chdbits":"CHDBits","ptsbao":"烧包","tjupt":"北洋园","ptchina":"PTChina","hddolby":"HDDolby",
    "cinefiles":"CarPT","rainbowisland":"CHDBits","ptchdbits":"CHDBits","hdtime":"HDTime","hdcity":"HDCity",
    "u2":"U2","dmhy":"U2","nexusphp":"?","opencd":"OpenCD","hdchina":"瓷器","chdtv":"CHDBits"}
def tracker_site(announce):
    try:
        host = urllib.parse.urlparse(announce).hostname or ""
    except Exception:
        return ""
    parts = host.split(".")
    sld = parts[-2] if len(parts) >= 2 else host
    return SITE_MAP.get(sld.lower(), sld)

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS torrents(
        info_hash TEXT PRIMARY KEY, name TEXT, size INTEGER, files INTEGER,
        query TEXT, status TEXT, matched INTEGER DEFAULT 0, injected INTEGER DEFAULT 0,
        first_seen INTEGER, last_searched INTEGER)""")
    try: c.execute("ALTER TABLE torrents ADD COLUMN source TEXT")   # 来源站
    except Exception: pass
    c.execute("""CREATE TABLE IF NOT EXISTS matches(
        id INTEGER PRIMARY KEY AUTOINCREMENT, info_hash TEXT, indexer TEXT,
        matched_name TEXT, mode TEXT, result TEXT, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_seed(
        name TEXT PRIMARY KEY, data TEXT, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, msg TEXT)""")
    # 整理入库记录
    c.execute("""CREATE TABLE IF NOT EXISTS media(
        info_hash TEXT PRIMARY KEY, name TEXT, cat TEXT, mtype TEXT,
        tmdbid INTEGER, tmdb_name TEXT, year TEXT, target TEXT,
        conf TEXT, status TEXT, files INTEGER DEFAULT 0, ts INTEGER)""")
    # 这两个 ALTER 必须排在 CREATE TABLE media 之后:全新库里表还不存在时 ALTER 会失败,
    # 而失败被 except 吞掉 → poster 列永远建不出来,首页一开就 "no such column: poster"。
    # 老库当年是先有表后加列才侥幸没事,新装的人一上来就是坏的。
    try: c.execute("ALTER TABLE media ADD COLUMN poster TEXT")      # 首页最近入库海报
    except Exception: pass
    try: c.execute("ALTER TABLE media ADD COLUMN save TEXT")   # 下载内容的磁盘路径(content_path)
    except Exception: pass
    # 批量保种任务队列
    # 各站历史产出:用来给收网门槛加权 —— 大站资源多,它回来了才算数,小站不该拖着大部队
    c.execute("""CREATE TABLE IF NOT EXISTS ixstat(
        name TEXT PRIMARY KEY, n INTEGER DEFAULT 0, res INTEGER DEFAULT 0, ms INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS keepseed(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, size INTEGER, url TEXT,
        indexer TEXT, status TEXT, err TEXT, ts INTEGER)""")
    try: c.execute("ALTER TABLE keepseed ADD COLUMN cid TEXT")   # 下完才知道身份,回填
    except Exception: pass

    # ===== 总账(§4):一份内容的唯一身份是文件清单指纹 cid,不是 info_hash =====
    # 为什么:同一份数据在不同站有不同 info_hash,辅种成立的根据恰恰是「文件清单一模一样」。
    # 用 info_hash 记账,天然表达不了「这是同一份内容」,只能靠裁字符串猜名字(已埋过雷)。
    c.execute("""CREATE TABLE IF NOT EXISTS content(
        cid TEXT PRIMARY KEY, name TEXT, size INTEGER, nfiles INTEGER,
        role TEXT DEFAULT '', place TEXT DEFAULT '',
        first_seen INTEGER, last_seen INTEGER)""")
    # 同一份内容的多个实例:qb 里一个、tr 里一个、别站辅来的又一个,info_hash 各不相同
    c.execute("""CREATE TABLE IF NOT EXISTS instance(
        info_hash TEXT PRIMARY KEY, cid TEXT, client TEXT, site TEXT, path TEXT, ts INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_instance_cid ON instance(cid)")
    # 辅种覆盖矩阵:这才是「辅全了没有」的唯一依据。
    # 关键在于区分 absent(问过确实没有) 和 pending(压根没问过) —— 老的 gap_report 是从
    # matches 表反推的,两者混为一谈,所以注释里只能写「搜不到≠一定没有」。
    c.execute("""CREATE TABLE IF NOT EXISTS coverage(
        cid TEXT, site TEXT, state TEXT, ts INTEGER, note TEXT DEFAULT '',
        PRIMARY KEY(cid, site))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_cov_state ON coverage(state)")
    try: c.execute("ALTER TABLE torrents ADD COLUMN cid TEXT")   # 双写过渡:老表挂上新身份
    except Exception: pass
    try: c.execute("ALTER TABLE media ADD COLUMN cid TEXT")
    except Exception: pass
    try: c.execute("ALTER TABLE content ADD COLUMN xfer_fail INTEGER DEFAULT 0")  # 交棒连败次数
    except Exception: pass
    try: c.execute("ALTER TABLE content ADD COLUMN xfer_err TEXT DEFAULT ''")     # 最后一次失败原因
    except Exception: pass
    c.commit(); c.close()

# ============ §3 身份:一份内容「是什么」 ============
# 观澜里同一份数据有五个身份(tr的种子名/qb的种子名/站点标题/媒体库路径/info_hash),
# 过去靠裁字符串互相猜(见 process_completed 里的 name.rsplit)。真正的身份只有一个:
# **文件清单**。同一份内容在不同站 info_hash 不同、名字不同,但相对路径+大小一模一样 ——
# 这正是辅种能成立的根据,run_match 本来就在算(local_set),算完就扔了。这里把它留下来。

def content_id(manifest):
    """文件清单指纹。manifest: {相对路径: 字节数}。同一份内容在任何地方都算出同一个值。"""
    items = sorted((str(p), int(sz)) for p, sz in dict(manifest).items())
    raw = "\n".join("%s\t%d" % (p, sz) for p, sz in items)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]

def _rel_manifest(pairs, top):
    """[(含顶层目录的路径, 大小)] → {相对路径: 大小}。
       口径必须与 torrent_files() 完全一致(它按 BT 规范返回不含顶层的 path),
       否则同一份内容从 tr / qb / 种子文件 三个来源会算出三个不同的 cid,整套账就废了。"""
    out = {}; pre = (top or "") + "/"
    for p, sz in pairs:
        out[p[len(pre):] if p.startswith(pre) else p] = int(sz)
    return out

def manifest_tr(t):
    """tr 种子 → 文件清单。tr 的 files[].name 含顶层目录。"""
    return _rel_manifest([(f["name"], f["length"]) for f in t.get("files", [])], t.get("name", ""))

def manifest_qb(qbfiles, name):
    """qb 种子 → 文件清单。qb 的 files 接口返回相对 save_path 的路径,同样含顶层目录。"""
    return _rel_manifest([(f["name"], f.get("size", 0)) for f in qbfiles], name)

def manifest_torrent(data):
    """.torrent 字节 → (顶层名, 文件清单)。直接复用既有的 bencode 解析。"""
    return torrent_files(data)

def _under(path, root):
    """path 是否真的在 root 目录之下。

       别用裸 startswith:root="/data/downloads/keepseed" 时,
       "/data/downloads/keepseed-old" 也会 startswith 通过 —— 用在清退护栏上,
       就是把不该删的目录当成保种库存删掉。必须卡住目录边界。"""
    if not root or not path: return False
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")

def _reg_domain(host):
    """注册域:tracker.totheglory.im → totheglory.im。站点主站和 tracker 常不同子域。"""
    parts = (host or "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")

def match_site(raw, sites, urlmap=None):
    """把「站的粗略名字」对齐到 Prowlarr 索引器名。

       为什么非要有这一步:coverage 表的键必须全库统一。tracker 域名推出来的是 'ttg',
       Prowlarr 里叫 'TTG' —— 差一个大小写,来源站就会被判成「从没问过」,每一轮都去问
       一个自己本来就在做种的站。老代码在 gap_report 里用双向子串兜过这个坑,但没根治,
       所以缺种报告的注释只能写「搜不到≠一定没有」。

       ① 精确(忽略大小写) ② 按注册域匹配 Prowlarr 的 indexerUrls ③ 双向子串兜底"""
    r = (raw or "").strip()
    if not r: return ""
    low = {s.lower(): s for s in sites}
    if r.lower() in low: return low[r.lower()]
    if urlmap:
        d = _reg_domain(r.lower())
        if d in urlmap: return urlmap[d]
        for dom, nm in urlmap.items():
            if r.lower() in dom or _reg_domain(dom) == d: return nm
    for s in sites:
        sl = s.lower()
        if sl in r.lower() or r.lower() in sl: return s
    return r          # 认不出来就保留原样,别硬塞给某个站

_SITEURL = {"t": 0, "d": {}, "names": []}
def site_urlmap():
    """{注册域: 索引器名} + 索引器名单,缓存 5 分钟。靠 Prowlarr 自己的配置说话。"""
    if _SITEURL["d"] and time.time() - _SITEURL["t"] < 300:
        return _SITEURL["d"], _SITEURL["names"]
    d = {}; names = []
    try:
        for i in prowlarr_indexers():
            nm = i.get("name") or ""
            if not nm: continue
            names.append(nm)
            for u in (i.get("indexerUrls") or []) + ([i.get("baseUrl")] if i.get("baseUrl") else []):
                try: h = urllib.parse.urlparse(u).hostname or ""
                except Exception: continue
                if h: d[_reg_domain(h.lower())] = nm
    except Exception:
        return _SITEURL["d"], _SITEURL["names"]
    _SITEURL.update(t=time.time(), d=d, names=names)
    return d, names

# ============ §4 总账:一份内容「现在怎么样」 ============
# 三个**正交**的维度,过去被塞进一个 status 字段(torrents/media/keepseed 各一套,互不相干):
#   role  角色 —— library(媒体库资产,要入库刮削) / stock(保种库存,只做种可淘汰)
#   place 位置 —— 在 qb / 在 tr / 两边都有 / 数据没了
#   cov   辅种覆盖 —— 每个站一行,单独记
# 正交的东西塞一个字段,就会出现「保种种子转完 tr 之后 status 该写什么」这种答不上来的问题。

def _led(sql, args=(), many=None, fetch=""):
    """账本层唯一的 SQL 执行器。账本函数一律走这里,账本之外一律不直连这三张表 ——
       这是「唯一 SQL 出口」能被 grep 查出来的前提。"""
    c = db()
    try:
        cur = c.executemany(sql, many) if many is not None else c.execute(sql, args)
        r = cur.fetchall() if fetch == "all" else (cur.fetchone() if fetch == "one" else None)
        c.commit(); return r
    finally:
        c.close()

def led_touch(cid, name="", size=0, nfiles=0):
    """登记/刷新一份内容。名字只在第一次登记时写,后面改名不覆盖(展示名要稳定)。"""
    if not cid: return
    now = int(time.time())
    _led("""INSERT INTO content(cid,name,size,nfiles,first_seen,last_seen) VALUES(?,?,?,?,?,?)
            ON CONFLICT(cid) DO UPDATE SET last_seen=excluded.last_seen,
              size=CASE WHEN content.size=0 THEN excluded.size ELSE content.size END,
              nfiles=CASE WHEN content.nfiles=0 THEN excluded.nfiles ELSE content.nfiles END""",
         (cid, name[:200], int(size or 0), int(nfiles or 0), now, now))

def led_bind(info_hash, cid, client="", site="", path=""):
    """把一个 info_hash 绑到内容上。同一份内容会有多个实例,这是多对一。"""
    if not info_hash or not cid: return
    _led("""INSERT INTO instance(info_hash,cid,client,site,path,ts) VALUES(?,?,?,?,?,?)
            ON CONFLICT(info_hash) DO UPDATE SET cid=excluded.cid, client=excluded.client,
              site=CASE WHEN excluded.site!='' THEN excluded.site ELSE instance.site END,
              path=CASE WHEN excluded.path!='' THEN excluded.path ELSE instance.path END,
              ts=excluded.ts""",
         (info_hash.lower(), cid, client, site, path, int(time.time())))

def led_cid(info_hash):
    r = _led("SELECT cid FROM instance WHERE info_hash=?", ((info_hash or "").lower(),), fetch="one")
    return r[0] if r else ""

def led_role(cid, role):
    """角色只升不降:一份内容一旦是媒体库资产,后来又被保种流程碰到,不能被降级成 stock。"""
    if not cid or not role: return
    cur = _led("SELECT role FROM content WHERE cid=?", (cid,), fetch="one")
    if cur and cur[0] == "library" and role != "library": return
    _led("UPDATE content SET role=? WHERE cid=?", (role, cid))

def led_place(cid, place):
    if not cid or not place: return
    _led("UPDATE content SET place=? WHERE cid=?", (place, cid))

_CONTENT_COLS = ["cid","name","size","nfiles","role","place","first_seen","last_seen","xfer_fail","xfer_err"]
def led_get(cid):
    """⚠️ 列清单必须和这里的 SELECT 同步。曾经漏掉后加的 xfer_fail,
       调用方 g.get("xfer_fail") 永远拿到 None,交棒失败上限那道闸门直接失效 ——
       类型没错、不抛异常、日志也正常,只有真跑一遍才看得出来。"""
    r = _led("SELECT " + ",".join(_CONTENT_COLS) + " FROM content WHERE cid=?", (cid,), fetch="one")
    return dict(zip(_CONTENT_COLS, r)) if r else None

# ---- 覆盖矩阵:辅种「辅全了没有」的唯一依据 ----
# state 取值:
#   source   这份内容本来就是从这个站下的(不是辅上去的)
#   seeding  已经在这个站做种(辅种成功/加了 tracker)
#   absent   问过了,这个站确实没有 —— 带时间戳,过期会自动转回 pending 再问一次
#   error    问的时候出错了(cookie 过期/站点抽风) —— 下一轮必重试,不能跟 absent 混为一谈
#   banned   这个站 ban 了 tr 客户端,注了也是废种,永远别问
def led_cov_set(cid, site, state, note=""):
    if not cid or not site: return
    _led("""INSERT INTO coverage(cid,site,state,ts,note) VALUES(?,?,?,?,?)
            ON CONFLICT(cid,site) DO UPDATE SET state=excluded.state, ts=excluded.ts, note=excluded.note""",
         (cid, site, state, int(time.time()), (note or "")[:80]))

def led_cov_get(cid):
    """{站名: (state, ts, note)}"""
    return {r[0]: (r[1], r[2], r[3]) for r in
            (_led("SELECT site,state,ts,note FROM coverage WHERE cid=?", (cid,), fetch="all") or [])}

COV_STALE_DAYS = 30      # 「问过说没有」的保质期:站点会补种,过了这么久值得再问一次
def led_cov_pending(cid, all_sites, stale_days=COV_STALE_DAYS):
    """这份内容**还该问哪些站**。辅种作业的驱动力就是把这个列表消成空。
       老代码没有这个概念,只能靠「6 小时冷却」拍脑袋整个种子跳过 ——
       结果是:辅到一个站就 break,剩下几十个站永远没问过,而且没人知道漏了谁。"""
    cur = led_cov_get(cid)
    stale = int(time.time()) - stale_days * 86400
    out = []
    for s in all_sites:
        st = cur.get(s)
        if st is None: out.append(s)                              # 从没问过
        elif st[0] == "error": out.append(s)                      # 上次出错,必重试
        elif st[0] == "absent" and st[1] < stale: out.append(s)   # 问过没有,但过期了
    return out

def led_xfer_fail(cid, err):
    """交棒失败记一笔。连败到上限就不再自动重试 —— 一个坏种子不该每分钟重试到天荒地老。"""
    if not cid: return
    _led("UPDATE content SET xfer_fail=IFNULL(xfer_fail,0)+1, xfer_err=? WHERE cid=?", ((err or "")[:80], cid))

def led_xfer_reset(cid=""):
    """人工重置交棒失败计数(面板「重试」按钮)。清 _QB_SETTLED 让下一轮重新尝试。"""
    if cid: _led("UPDATE content SET xfer_fail=0, xfer_err='' WHERE cid=?", (cid,))
    else:   _led("UPDATE content SET xfer_fail=0, xfer_err='' WHERE IFNULL(xfer_fail,0)>0")
    try: _QB_SETTLED.clear()
    except Exception: pass

def led_xfer_ok(cid):
    if not cid: return
    _led("UPDATE content SET xfer_fail=0, xfer_err='', place='tr' WHERE cid=?", (cid,))

def led_xfer_stuck(limit=5):
    """卡住的交棒:下完了但送不进 tr 的。老版失败只写一行日志,面板上根本看不见。"""
    rows = _led("SELECT cid,name,xfer_fail,xfer_err FROM content WHERE IFNULL(xfer_fail,0)>0 "
                "ORDER BY xfer_fail DESC LIMIT 50", fetch="all") or []
    return [{"cid": r[0], "name": r[1], "fail": r[2], "err": r[3], "gaveup": r[2] >= limit} for r in rows]

def led_has_tr(cid):
    """tr 那边接手了没有 —— 交棒该不该做,从事实推导,不靠队列表。
       队列表会和现实脱节:转成功但没来得及更新队列,下一轮就重复转。"""
    r = _led("SELECT 1 FROM instance WHERE cid=? AND client='tr' LIMIT 1", (cid,), fetch="one")
    return bool(r)

def led_recent(limit=120, skip_role=""):
    """最近登记的内容(总账视图)。缺种报告之类的读取一律走这里,别自己拼 SQL。"""
    sql = "SELECT cid,name,size,role,place FROM content"
    args = []
    if skip_role:
        sql += " WHERE IFNULL(role,'') != ?"; args.append(skip_role)
    sql += " ORDER BY last_seen DESC LIMIT ?"; args.append(int(limit))
    return [{"cid": r[0], "name": r[1], "size": r[2], "role": r[3], "place": r[4]}
            for r in (_led(sql, tuple(args), fetch="all") or [])]

def led_any_hash(cid):
    """随便取这份内容的一个 info_hash(界面上要用它调别的接口)。"""
    r = _led("SELECT info_hash FROM instance WHERE cid=? LIMIT 1", (cid,), fetch="one")
    return r[0] if r else ""

def led_cov_stats():
    """全库覆盖概览:多少内容辅全了、还欠多少站没问。"""
    rows = _led("SELECT state, COUNT(*) FROM coverage GROUP BY state", fetch="all") or []
    d = {k: v for k, v in rows}
    ncontent = (_led("SELECT COUNT(*) FROM content", fetch="one") or [0])[0]
    return {"content": ncontent, "seeding": d.get("seeding", 0) + d.get("source", 0),
            "absent": d.get("absent", 0), "error": d.get("error", 0), "banned": d.get("banned", 0)}

_WECOM = {"tok": "", "exp": 0}
_NQUEUE = []   # 未送达通知队列,后台线程重投
def _wecom_opener():
    # 注意:这里刻意复用 TMDB_PROXY(通常就是本机 mihomo/clash)。企微要求「可信IP」白名单,
    # 家宽是动态公网IP会被拒;mihomo 里加规则 DOMAIN-SUFFIX,qyapi.weixin.qq.com,PROXY 把企微流量
    # 路由到固定IP的机场节点出口,才能过白名单。所以这不是"国内服务错走国际代理",是有意为之。
    import ssl
    ctx = ssl.create_default_context()
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2   # 实测该链路 TLS1.2 成功率最高
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": CFG["TMDB_PROXY"], "https": CFG["TMDB_PROXY"]}) if CFG["TMDB_PROXY"] else urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx))

def _wecom_send(payload):
    """发送任意企微消息(4次尝试)。链路(dmit→腾讯跨境)天然丢包,失败由队列重投"""
    base = (CFG["WECOM_PROXY"] or "https://qyapi.weixin.qq.com").rstrip("/")
    op = _wecom_opener()
    payload = dict(payload); payload.update({"touser": "@all", "agentid": int(CFG["WECOM_AGENTID"])})
    for attempt in range(4):
        try:
            if time.time() > _WECOM["exp"] or not _WECOM["tok"]:
                d = json.load(op.open(
                    f"{base}/cgi-bin/gettoken?corpid={CFG['WECOM_CORPID']}&corpsecret={CFG['WECOM_SECRET']}", timeout=12))
                _WECOM["tok"] = d.get("access_token", ""); _WECOM["exp"] = time.time() + 6600
            r = json.load(op.open(urllib.request.Request(
                f"{base}/cgi-bin/message/send?access_token={_WECOM['tok']}", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}), timeout=12))
            if r.get("errcode") == 0:
                return True
            if r.get("errcode") in (40014, 42001, 41001):
                _WECOM["tok"] = ""; _WECOM["exp"] = 0
        except Exception:
            pass
        time.sleep(2)
    return False

def _notify_try(title, text):
    content = (title + ("\n" + text if text else "")).strip()
    return _wecom_send({"msgtype": "text", "text": {"content": content}})

def poster_url(p):
    """TMDB poster path → 公网可达的海报直链(手机上企微能加载)"""
    if not p: return ""
    if p.startswith("http"): return p            # iTunes 等直链
    if CFG["PUBLIC_URL"]:
        return CFG["PUBLIC_URL"].rstrip("/") + "/api/poster?p=" + urllib.parse.quote(p)
    return ""

def notify_news(articles):
    """图文卡片通知(带海报)。articles: [{title,description,url,picurl}] 最多8条"""
    if not (CFG["WECOM_CORPID"] and CFG["WECOM_SECRET"] and CFG["WECOM_AGENTID"]):
        return
    h = time.localtime().tm_hour
    if not (CFG["NOTIFY_START"] <= h < CFG["NOTIFY_END"]):
        return
    arts = articles[:8]
    if not _wecom_send({"msgtype": "news", "news": {"articles": arts}}):
        _NQUEUE.append(("__news__", json.dumps(arts, ensure_ascii=False), time.time()))
        logmsg("WARN", f"图文通知暂未达,入队重投({len(_NQUEUE)}条)")

def notify(title, text=""):
    """企业微信推送。免打扰时段外静默;当场发不出去进队列由后台必达重投"""
    if not (CFG["WECOM_CORPID"] and CFG["WECOM_SECRET"] and CFG["WECOM_AGENTID"]):
        return
    h = time.localtime().tm_hour
    if not (CFG["NOTIFY_START"] <= h < CFG["NOTIFY_END"]):
        return
    if not _notify_try(title, text):
        _NQUEUE.append((title, text, time.time()))
        logmsg("WARN", f"通知暂未达,已入队重投({len(_NQUEUE)}条待发)")

def notify_worker():
    """每2分钟重投未达通知,1小时后放弃"""
    while True:
        time.sleep(120)
        try:
            while _NQUEUE:
                title, text, ts = _NQUEUE[0]
                if time.time() - ts > 3600:
                    _NQUEUE.pop(0); continue
                ok = (_wecom_send({"msgtype": "news", "news": {"articles": json.loads(text)}})
                      if title == "__news__" else _notify_try(title, text))
                if ok:
                    _NQUEUE.pop(0); logmsg("INFO", f"队列通知补投成功,剩{len(_NQUEUE)}条")
                else:
                    break
        except Exception:
            pass


# ============ 纯标准库 AES-256-CBC(企微回调解密;S盒运行时生成,防手抄笔误) ============
def _gmul(a, b):
    r = 0
    for _ in range(8):
        if b & 1: r ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi: a ^= 0x1B
        b >>= 1
    return r

_SBOX = [0] * 256; _ISBOX = [0] * 256
def _init_sbox():
    exp = [0] * 512; log = [0] * 256; x = 1
    for i in range(255):
        exp[i] = x; log[x] = i; x = _gmul(x, 3)
    for i in range(255, 512): exp[i] = exp[i - 255]
    inv = [0] * 256
    for i in range(1, 256): inv[i] = exp[255 - log[i]]
    for i in range(256):
        c = inv[i]; x = c
        for _ in range(4):
            c = ((c << 1) | (c >> 7)) & 0xFF
            x ^= c
        _SBOX[i] = x ^ 0x63
    for i, v in enumerate(_SBOX): _ISBOX[v] = i
_init_sbox()

def _kexp(key):
    w = [list(key[4*i:4*i+4]) for i in range(8)]
    rcon = 1
    for i in range(8, 60):
        t = list(w[i-1])
        if i % 8 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[x] for x in t]
            t[0] ^= rcon
            rcon = _gmul(rcon, 2)
        elif i % 8 == 4:
            t = [_SBOX[x] for x in t]
        w.append([w[i-8][j] ^ t[j] for j in range(4)])
    return w

def _ark(s, w, rnd):
    for c in range(4):
        for r in range(4):
            s[r + 4*c] ^= w[4*rnd + c][r]

def _shift(s):  return [s[r + 4*((c + r) % 4)] for c in range(4) for r in range(4)][0:16] if False else [s[(i % 4) + 4*(((i // 4) + (i % 4)) % 4)] for i in range(16)]
def _ishift(s): return [s[(i % 4) + 4*(((i // 4) - (i % 4)) % 4)] for i in range(16)]

def _mix(s):
    for c in range(4):
        a = s[4*c:4*c+4]
        s[4*c+0] = _gmul(a[0],2) ^ _gmul(a[1],3) ^ a[2] ^ a[3]
        s[4*c+1] = a[0] ^ _gmul(a[1],2) ^ _gmul(a[2],3) ^ a[3]
        s[4*c+2] = a[0] ^ a[1] ^ _gmul(a[2],2) ^ _gmul(a[3],3)
        s[4*c+3] = _gmul(a[0],3) ^ a[1] ^ a[2] ^ _gmul(a[3],2)

def _imix(s):
    for c in range(4):
        a = s[4*c:4*c+4]
        s[4*c+0] = _gmul(a[0],14) ^ _gmul(a[1],11) ^ _gmul(a[2],13) ^ _gmul(a[3],9)
        s[4*c+1] = _gmul(a[0],9) ^ _gmul(a[1],14) ^ _gmul(a[2],11) ^ _gmul(a[3],13)
        s[4*c+2] = _gmul(a[0],13) ^ _gmul(a[1],9) ^ _gmul(a[2],14) ^ _gmul(a[3],11)
        s[4*c+3] = _gmul(a[0],11) ^ _gmul(a[1],13) ^ _gmul(a[2],9) ^ _gmul(a[3],14)

def _eblk(b, w):
    s = list(b)
    _ark(s, w, 0)
    for rnd in range(1, 14):
        s = [_SBOX[x] for x in s]
        s = _shift(s)
        _mix(s)
        _ark(s, w, rnd)
    s = [_SBOX[x] for x in s]
    s = _shift(s)
    _ark(s, w, 14)
    return bytes(s)

def _dblk(b, w):
    s = list(b)
    _ark(s, w, 14)
    for rnd in range(13, 0, -1):
        s = _ishift(s)
        s = [_ISBOX[x] for x in s]
        _ark(s, w, rnd)
        _imix(s)
    s = _ishift(s)
    s = [_ISBOX[x] for x in s]
    _ark(s, w, 0)
    return bytes(s)

def _aes_cbc_dec(data, key, iv):
    w = _kexp(key); out = b""; prev = iv
    for i in range(0, len(data), 16):
        blk = data[i:i+16]
        out += bytes(x ^ y for x, y in zip(_dblk(blk, w), prev))
        prev = blk
    return out

def _aes_cbc_enc(data, key, iv):
    w = _kexp(key); out = b""; prev = iv
    for i in range(0, len(data), 16):
        blk = bytes(x ^ y for x, y in zip(data[i:i+16], prev))
        prev = _eblk(blk, w)
        out += prev
    return out

def aes_selftest():
    key = bytes(range(32)); pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    w = _kexp(key); ct = _eblk(pt, w)
    return ct.hex() == "8ea2b7ca516745bfeafc49904b496089" and _dblk(ct, w) == pt

# ============ 企业微信回调(双向交互:发片名→选序号→自动下载) ============
def _wecom_sig(ts, nonce, enc):
    import hashlib
    return hashlib.sha1("".join(sorted([CFG["WECOM_TOKEN"], ts, nonce, enc])).encode()).hexdigest()

def wecom_decrypt(enc_b64):
    key = base64.b64decode(CFG["WECOM_AESKEY"] + "=")
    plain = _aes_cbc_dec(base64.b64decode(enc_b64), key, key[:16])
    pad = plain[-1]
    if not 1 <= pad <= 32: raise ValueError("bad padding")
    plain = plain[:-pad]
    ln = int.from_bytes(plain[16:20], "big")
    return plain[20:20+ln].decode("utf-8"), plain[20+ln:].decode("utf-8", "ignore")

def wecom_encrypt(msg):
    """仅本地回环自测用(生成合法加密包)"""
    key = base64.b64decode(CFG["WECOM_AESKEY"] + "=")
    raw = os.urandom(16) + len(msg.encode()).to_bytes(4, "big") + msg.encode() + CFG["WECOM_CORPID"].encode()
    pad = 32 - len(raw) % 32
    raw += bytes([pad]) * pad
    return base64.b64encode(_aes_cbc_enc(raw, key, key[:16])).decode()

_CHAT = {"stage": "idle", "groups": [], "gi": 0, "ts": 0, "q": "", "last": None}

def _chat_label(g):
    mt = {"tv": "剧", "movie": "影", "music": "乐", "anime": "漫"}.get(g.get("cat") or g.get("mtype"), "?")
    yr = f"({g['year']})" if g.get("year") else ""
    top = (g.get("results") or [{}])[0]
    return f"{g['name']}{yr} {mt}·{len(g.get('results', []))}种·做种{top.get('seeders', 0)}"

def _chat_send_groups():
    gs = _CHAT["groups"]; q = _CHAT["q"]
    arts = []
    for i, g in enumerate(gs):
        pic = poster_url(g.get("posterurl") or g.get("poster") or "")
        top = (g.get("results") or [{}])[0]
        mt = {"tv": "剧集", "movie": "电影", "music": "音乐", "anime": "动漫"}.get(g.get("cat") or g.get("mtype"), "其他")
        yr = f" ({g['year']})" if g.get("year") else ""
        arts.append({"title": f"{i+1}. {g['name']}{yr}",
                     "description": f"{mt} · {len(g.get('results', []))}个种 · 最高做种{top.get('seeders', 0)}",
                     "url": CFG["PUBLIC_URL"] or "https://seed.leesy.cc", "picurl": pic})
    notify_news(arts)
    notify(f"🔍「{q}」共 {len(gs)} 个", "回复数字选片(15分钟有效),0 取消")
    _CHAT["stage"] = "groups"; _CHAT["ts"] = time.time()

def _chat_search(q):
    try:
        name, year = split_query(q)
        anchor = query_anchor(name, year)
        # 手机上发片名等结果:一次扇出,不像面板那样分两轮。只补一个别名 ——
        # 同站请求 Prowlarr 会排队,词数直接乘在墙上时间上。altqs[0] 已按命名体系挑过。
        alts = (anchor or {}).get("altqs") or []
        qs = [name] + (alts[:1] if (anchor and CFG["SEARCH_ALIAS"]) else [])
        pol = POLICY["find"]
        d = search_group(q, prowlarr_search_fan(qs, per_timeout=pol["timeout"],
                                                deadline=pol["deadline"], workers=pol["workers"]),
                         anchor=anchor)
        gs = list(d.get("groups") or [])[:8]
        if not gs:
            for x in (d.get("other") or [])[:5]:
                gs.append({"name": x["title"][:44], "year": "", "mtype": "?", "cat": x.get("cat",""), "results": [x]})
        if not gs:
            notify(f"「{q}」没搜到资源", "换个关键词试试"); return
        _CHAT["groups"] = gs; _CHAT["q"] = q
        _chat_send_groups()
    except Exception as e:
        notify("❌ 搜索出错", str(e)[:50])

def _chat_pick_group(i):
    gs = _CHAT.get("groups") or []
    if not gs or time.time() - _CHAT.get("ts", 0) > 900:
        notify("❓ 没有待选片单", "先发片名搜索"); return
    if not (1 <= i <= len(gs)):
        notify(f"❓ 请回复 1~{len(gs)}"); return
    _CHAT["gi"] = i - 1; g = gs[i-1]
    rs = (g.get("results") or [])[:8]
    lines = [f"{j+1}. {x.get('site','?')} · {x.get('sizeh','')} · 做种{x.get('seeders',0)}" for j, x in enumerate(rs)]
    notify(f"🎯 已选«{g['name']}»,挑个站的种子:", "\n".join(lines) + "\n\n回复数字下载 · 0 返回片单\n(1 通常就是最优:做种最多)")
    _CHAT["stage"] = "torrents"; _CHAT["ts"] = time.time()

def _chat_pick_torrent(j):
    gs = _CHAT.get("groups") or []
    if _CHAT.get("stage") != "torrents" or not gs or time.time() - _CHAT.get("ts", 0) > 900:
        notify("❓ 没有待选站点列表", "先发片名→选片"); return
    g = gs[_CHAT["gi"]]; rs = (g.get("results") or [])[:8]
    if not (1 <= j <= len(rs)):
        notify(f"❓ 请回复 1~{len(rs)},或 0 返回"); return
    pick = rs[j-1]
    try:
        data = prowlarr_download(pick["url"])
        if data[:1] != b"d":
            notify("❌ 种子拉取失败", pick.get("site", "")); return
        try: cname, _ = torrent_files(data)
        except Exception: cname = g["name"]
        try: ih = torrent_infohash(data)
        except Exception: ih = ""
        catmap = {"music": "音乐", "anime": "动漫", "tv": "电视剧", "movie": "电影"}
        cat = catmap.get(g.get("cat") or g.get("mtype")) or media_category(cname, None)
        qb_conn().add(data, category=cat, tags="packseed")
        mates = [{"url": x["url"], "size": x.get("size", 0), "site": x.get("site", "")}
                 for x in (g.get("results") or []) if x.get("url") != pick.get("url")][:40]
        if cname and mates:
            c = db(); c.execute("INSERT OR REPLACE INTO pending_seed(name,data,ts) VALUES(?,?,?)",
                                (cname, json.dumps(mates, ensure_ascii=False), int(time.time())))
            c.commit(); c.close()
        _CHAT["last"] = {"hash": ih, "name": g["name"], "cname": cname, "ts": time.time()}
        _CHAT["stage"] = "idle"; _CHAT["groups"] = []
        logmsg("INFO", f"微信点播: {g['name']} ← {pick.get('site','')}")
        notify(f"⬇️ 已开始下载 · {g['name']}",
               f"{pick.get('site','')} · {pick.get('sizeh','')} · 做种{pick.get('seeders',0)}\n"
               f"完成后自动入库+转种+辅种\n选错了?回复「撤回」取消并删除")
    except Exception as e:
        notify("❌ 下载失败", str(e)[:50])

def _chat_undo():
    last = _CHAT.get("last")
    if not last or time.time() - last.get("ts", 0) > 1800:
        notify("❓ 没有可撤回的下载", "撤回窗口为下载后30分钟内"); return
    try:
        qb = qb_conn()
        t = next((x for x in qb.torrents() if last.get("hash") and x.get("hash","").lower() == last["hash"].lower()), None)
        if not t and last.get("cname"):
            t = next((x for x in qb.torrents() if x.get("name") == last["cname"]), None)
        if not t:
            notify("⏰ 来不及撤回", f"«{last['name']}»已下载完成并入库转种\n要删的话去面板处理"); return
        qb.delete(t["hash"], delete_files=True)
        c = db(); c.execute("DELETE FROM pending_seed WHERE name=?", (last.get("cname",""),)); c.commit(); c.close()
        logmsg("INFO", f"微信撤回下载: {last['name']}")
        notify(f"↩️ 已撤回 · {last['name']}", "任务已取消,已下载的数据已清理")
        _CHAT["last"] = None
    except Exception as e:
        notify("❌ 撤回失败", str(e)[:50])

def _chat_progress():
    try: dl = [t for t in qb_conn().torrents() if t.get("progress", 0) < 1]
    except Exception as e: notify("❌ 连不上 qb", str(e)[:40]); return
    if not dl: notify("📭 当前没有下载任务", "下载完的已自动入库+转种"); return
    lines = [f"{round(t.get('progress',0)*100)}% · {t['name'][:26]} · {human_size(t.get('dlspeed',0))}/s"
             for t in sorted(dl, key=lambda x: -x.get("progress", 0))[:8]]
    notify(f"⬇️ 下载中 {len(dl)} 个", "\n".join(lines))

def _chat_stats():
    body = []
    try:
        st = tr_conn().call("session-stats", {}).get("arguments", {})
        cur = st.get("current-stats", {}); cum = st.get("cumulative-stats", {})
        body.append(f"做种 {st.get('torrentCount',0)} 个 · 活跃 {st.get('activeTorrentCount',0)}")
        body.append(f"本次上传 {human_size(cur.get('uploadedBytes',0))} · 累计 {human_size(cum.get('uploadedBytes',0))}")
    except Exception: body.append("tr 读取失败")
    try:
        du = shutil.disk_usage("/data")
        body.append(f"磁盘剩余 {human_size(du.free)} / {human_size(du.total)}")
    except Exception: pass
    notify("📊 保种状态", "\n".join(body))

def _chat_holds():
    c = db(); rows = c.execute("SELECT info_hash,name FROM media WHERE status='hold' ORDER BY ts DESC LIMIT 9").fetchall(); c.close()
    if not rows: notify("✅ 没有待确认的条目", "识别不准的才会进这里"); return
    _CHAT["holds"] = [r[0] for r in rows]; _CHAT["holds_ts"] = time.time()
    lines = [f"{i+1}. {r[1][:32]}" for i, r in enumerate(rows)]
    notify(f"⚠️ 待确认 {len(rows)} 个", "\n".join(lines) + "\n\n回复「确认N 内容」入库\n如: 确认1 12345(TMDB id)或 确认1 大明王朝")

def _chat_confirm(text):
    m = re.match(r'^确认\s*(\d+)[\s:：]+(.+)$', text)
    if not m: notify("格式: 确认N 内容", "如 确认1 12345 或 确认1 大明王朝"); return
    idx = int(m.group(1)); q = m.group(2).strip()
    holds = _CHAT.get("holds") or []
    if not (1 <= idx <= len(holds)) or time.time() - _CHAT.get("holds_ts", 0) > 900:
        notify("❓ 序号无效或列表已过期", "先发「待确认」刷新一下"); return
    r = manual_organize(holds[idx-1], q)
    if r.get("ok"): notify(f"✅ 已入库 · {r.get('name','')}", f"{r.get('n',0)} 个文件已硬链接进库")
    else: notify("❌ 入库失败", r.get("err", ""))

def _chat_recent():
    c = db(); rows = c.execute("SELECT tmdb_name,year,cat FROM media WHERE status='done' AND tmdb_name!='' ORDER BY ts DESC LIMIT 6").fetchall(); c.close()
    if not rows: notify("📭 还没有入库记录", ""); return
    cmap = {"电影": "🎬", "电视剧": "📺", "动漫": "🎌", "音乐": "🎵"}
    lines = [f"{cmap.get(r[2],'')} {r[0]}{(' ('+r[1]+')') if r[1] else ''}" for r in rows]
    notify("🎬 最近入库", "\n".join(lines))

def wecom_on_text(text):
    text = (text or "").strip()
    if not text: return
    if text in ("撤回", "取消下载"):
        _chat_undo(); return
    if text in ("进度", "下载", "下载进度"):
        _chat_progress(); return
    if text in ("统计", "上传", "状态", "保种"):
        _chat_stats(); return
    if text in ("待确认", "确认列表", "待入库"):
        _chat_holds(); return
    if text.startswith("确认"):
        _chat_confirm(text); return
    if text in ("最近", "最近入库", "最近入的"):
        _chat_recent(); return
    if text in ("帮助", "help", "?", "？", "菜单"):
        notify("🌊 观澜微信指令",
               "片名 → 搜索点播\n进度 → 看下载进度\n统计 → 保种/上传/磁盘\n待确认 → 列出待入库\n确认N 内容 → 确认入库\n最近 → 最近入库\n撤回 → 撤回上次下载"); return
    if text == "0":
        if _CHAT.get("stage") == "torrents":
            _chat_send_groups()          # 返回片单
        else:
            _CHAT["stage"] = "idle"; _CHAT["groups"] = []
            notify("已取消", "发片名重新搜索")
        return
    if text.isdigit() and len(text) <= 2:
        if _CHAT.get("stage") == "torrents":
            _chat_pick_torrent(int(text))
        else:
            _chat_pick_group(int(text))
        return
    notify(f"🔍 收到「{text}」,全站搜索中…", "约 15~25 秒,结果稍后推送")
    threading.Thread(target=_chat_search, args=(text,), daemon=True).start()

def logmsg(level, msg):
    try:
        c = db(); c.execute("INSERT INTO log(ts,level,msg) VALUES(?,?,?)", (int(time.time()), level, msg[:500])); c.commit(); c.close()
    except: pass
    print(f"[{level}] {msg}", flush=True)

# ============ bencode ============
import hashlib
def bencode(x):
    if isinstance(x, int): return b"i%de" % x
    if isinstance(x, bytes): return b"%d:%s" % (len(x), x)
    if isinstance(x, list): return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict): return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(x.items())) + b"e"
    raise TypeError(type(x))

def torrent_infohash(data):
    return hashlib.sha1(bencode(bdecode(data)[b"info"])).hexdigest()

def torrent_announces(data):
    top = bdecode(data); anns = []
    if b"announce" in top: anns.append(top[b"announce"].decode("utf-8", "ignore"))
    for tier in (top.get(b"announce-list") or []):
        for a in tier: anns.append(a.decode("utf-8", "ignore"))
    return [a for a in dict.fromkeys(anns) if a]

def tr_add_trackers(tr, tid, anns):
    """给现有种子追加 tracker。tr3 用 trackerAdd;tr4 废弃了它,失败则 trackerList 全量覆盖。
    按主机名去重: 同站不同 authkey 不算新 tracker(重复汇报同站有连坐风险)"""
    curt = tr.call("torrent-get", {"ids": [tid], "fields": ["trackers"]})["arguments"]["torrents"][0].get("trackers", [])
    have = [u.get("announce", "") for u in curt]
    def host(a):
        try: return urllib.parse.urlparse(a).hostname or a
        except Exception: return a
    have_hosts = {host(a) for a in have}
    new = [a for a in anns if a and host(a) not in have_hosts]
    if not new: return "duplicate"
    r2 = tr.call("torrent-set", {"ids": [tid], "trackerAdd": new})
    if r2.get("result") != "success":
        r2 = tr.call("torrent-set", {"ids": [tid], "trackerList": "\n\n".join([h for h in have if h] + new)})
    return "tracker" if r2.get("result") == "success" else "duplicate"

def bdecode(data):
    def parse(i):
        c = data[i:i+1]
        if c == b'i':
            j = data.index(b'e', i); return int(data[i+1:j]), j+1
        if c == b'l':
            i += 1; lst = []
            while data[i:i+1] != b'e':
                v, i = parse(i); lst.append(v)
            return lst, i+1
        if c == b'd':
            i += 1; d = {}
            while data[i:i+1] != b'e':
                k, i = parse(i); v, i = parse(i); d[k] = v
            return d, i+1
        j = data.index(b':', i); n = int(data[i:j]); return data[j+1:j+1+n], j+1+n
    return parse(0)[0]

def torrent_files(data):
    info = bdecode(data)[b'info']
    name = info[b'name'].decode('utf-8', 'ignore')
    files = {}
    if b'files' in info:
        for f in info[b'files']:
            files['/'.join(p.decode('utf-8','ignore') for p in f[b'path'])] = f[b'length']
    else:
        files[name] = info[b'length']
    return name, files

# ============ 从种子名提取搜索词 ============
# DROP: 季集/集数标记，丢弃但不结束标题(中英标题可能在其两侧)
DROP = re.compile(r'^(s\d{1,2}(-s\d{1,2})?|s\d{1,2}e\d{1,3}|e\d{1,3}(-e\d{1,3})?|\d+集全?|全\d+集|第[\d一二三四五六七八九十百]+[季集话話期]|季全|全集|sp\d*|complete|proper|repack|internal)$', re.I)
# STOPW: 年份/清晰度/编码/来源/语言等，标题到此为止
STOPW = re.compile(r'^((19|20)\d{2}(-\d{4})?|\d{3,4}[pi]|x26[45]|h\.?26[45]|hevc|avc|bluray|blu-ray|web-?dl|webrip|hdtv|remux|bdrip|dvdrip|dts|ddp|truehd|flac|aac|国语|粤语|中字|字幕|双语|国粤|简繁|repack)', re.I)
# GLUE: 词内粘连的季集，如 真探S01-S03 / 白色巨塔S01E01
GLUE = re.compile(r'^(.+?)(s\d{1,2}(-s\d{1,2})?|s\d{1,2}e\d{1,3}|e\d{1,3}(-e\d{1,3})?)$', re.I)
CJK = re.compile(r'[一-鿿]')
# DESC: "合集"类描述词，标题到此为止(避免拼出 "Better Call Saul The Series" 这种不存在的短语)
DESC = re.compile(r'^(complete|collection|duology|trilogy|quadrilogy|pentalogy|anthology|saga|boxset|box|series)$', re.I)

def _english_prefix(name):
    # 纯英文标题：去中文、砍发布组，从头连续取词，遇 季集/年份/清晰度/Complete 即停，去尾部 The
    s = re.sub(r'[._]+', ' ', re.split(r'[￡@]', name)[0])
    s = re.sub(r'[一-鿿]', ' ', s)
    out = []
    for t in s.split():
        if not re.search(r'[A-Za-z]', t):        # 纯数字/符号
            if out: break
            else: continue
        if DROP.match(t):                        # 季集标记
            if out: break
            else: continue                       # 开头残留(中文去掉后)→跳过
        if STOPW.match(t) or DESC.match(t):      # 年份/清晰度/来源/Complete/Collection → 停
            break
        out.append(t)
    while out and out[-1].lower() == 'the':      # 去尾部 The
        out.pop()
    return ' '.join(out[:6]).strip()

def extract_query(name):
    # 规则一：取第一个点(或下划线)前的内容，砍掉发布组标记(￡/@ 后的)
    first = re.split(r"[._]", name, maxsplit=1)[0]
    first = re.split(r'[￡@]', first)[0].strip()
    # 中文名：第一段就是标题，去掉粘连的季集标记(无耻之徒S01-S11 → 无耻之徒)，只搜纯中文名
    if CJK.search(first) and len(first) >= 2:
        m = GLUE.match(first)
        if m and m.group(1) and CJK.search(m.group(1)):
            first = m.group(1).strip()       # 剥离 S01-S11 / E01-E21 等系列标记
        return first[:40]
    # 纯英文名：连续取词到停止标记
    return (_english_prefix(name) or first or name[:15])[:40]

def extract_english(name):
    # 中文名搜不到时的英文兜底
    return _english_prefix(name)[:40]

def extract_season_query(name):
    """保留英文剧名的季号，供辅种精确检索。

    ``extract_english`` 面向媒体识别，遇到 S01 会主动截断；辅种则恰恰
    需要这个季号，否则搜索会混入整部剧的各季、合集和重编码版本。
    """
    m = re.search(r"(?i)([A-Za-z][A-Za-z0-9._ -]{1,100}?[._ -]S\d{1,2}(?:[._ -]E\d{1,2})?)", name)
    return m.group(1).strip(" ._-")[:80] if m else ""

def cross_seed_queries(name):
    """从精确到宽泛的辅种搜索词，去重并丢弃空值。"""
    out = []
    for q in (extract_season_query(name), extract_query(name), extract_english(name)):
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    return out

# ============ Transmission RPC ============
class TR:
    def __init__(s):
        s.url = CFG["TR_URL"] + "/transmission/rpc"
        s.auth = base64.b64encode(f'{CFG["TR_USER"]}:{CFG["TR_PASS"]}'.encode()).decode()
        s.sid = ""
    def call(s, method, args):
        for _ in range(2):
            req = urllib.request.Request(s.url, data=json.dumps({"method":method,"arguments":args}).encode(),
                headers={"Authorization":"Basic "+s.auth,"X-Transmission-Session-Id":s.sid,"Content-Type":"application/json"})
            try:
                return json.load(urllib.request.urlopen(req, timeout=40))
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    s.sid = e.headers["X-Transmission-Session-Id"]; continue
                # tr 3.00 的暴力破解保护:任何一个不带正确 Authorization 的请求都会让计数+1,
                # 攒够 100 就对所有请求(包括凭据正确的)返回 403,且只有重启守护进程才清零。
                # 裸报 "HTTP Error 403" 会让人误以为是索引器/站点风控,这里把真因说清楚。
                body = ""
                try: body = e.read().decode("utf-8", "ignore")
                except Exception: pass
                if e.code == 403 and "login attempt" in body.lower():
                    raise RuntimeError("Transmission 已被暴力破解保护锁定(累计100次认证失败),"
                                       "需 docker restart transmission;若反复锁定,检查 9091 是否暴露在公网")
                if e.code == 401:
                    raise RuntimeError("Transmission 认证失败,检查 TR_USER/TR_PASS")
                raise
        raise RuntimeError("tr rpc fail")
    TFULL = ["hashString","name","totalSize","files","downloadDir","trackers","percentDone"]
    TLITE = ["hashString","name","totalSize","downloadDir","percentDone"]
    def torrents(s, fields=None):
        """默认拉全字段(含 files)。4000+ 个种子的文件清单是几十 MB 的 JSON,
           只是想看看有哪些种子时务必传 TLITE —— 辅种调度就靠这个把每轮开销压下来。"""
        r = s.call("torrent-get", {"fields": fields or TR.TFULL})
        return r.get("arguments", {}).get("torrents", [])
    def torrent(s, ih):
        """按 info_hash 拉单个种子的全字段(要算文件清单指纹时才拉)。"""
        r = s.call("torrent-get", {"ids": [ih], "fields": TR.TFULL})
        ts = r.get("arguments", {}).get("torrents", [])
        return ts[0] if ts else None
    def add(s, torrent_bytes, download_dir):
        return s.call("torrent-add", {"metainfo":base64.b64encode(torrent_bytes).decode(),"download-dir":download_dir,"paused":False})

# ============ qBittorrent WebUI（搜索下载的目标） ============
_CONN = {"qb": None, "tr": None, "cfg": ""}
def _conn_key():
    return f'{CFG["QB_URL"]}|{CFG["QB_USER"]}|{CFG["QB_PASS"]}|{CFG["TR_URL"]}|{CFG["TR_USER"]}|{CFG["TR_PASS"]}'
def qb_conn():
    """复用同一个 QB 实例(带 cookie),别每个种子都重新登录——高频登录会触发 qb 的
    「认证失败次数过多,IP 已封禁」,批量保种时尤其致命(血泪教训)。改设置后自动重建。"""
    if _CONN["cfg"] != _conn_key(): _CONN.update(qb=None, tr=None, cfg=_conn_key())
    if _CONN["qb"] is None: _CONN["qb"] = QB()
    return _CONN["qb"]
def tr_conn():
    if _CONN["cfg"] != _conn_key(): _CONN.update(qb=None, tr=None, cfg=_conn_key())
    if _CONN["tr"] is None: _CONN["tr"] = TR()
    return _CONN["tr"]

class QB:
    def __init__(s):
        s.url = CFG["QB_URL"].rstrip("/"); s.user = CFG["QB_USER"]; s.pw = CFG["QB_PASS"]; s.cookie = ""
    def login(s):
        data = urllib.parse.urlencode({"username":s.user,"password":s.pw}).encode()
        req = urllib.request.Request(s.url+"/api/v2/auth/login", data=data,
              headers={"Referer":s.url,"Content-Type":"application/x-www-form-urlencoded"})
        r = urllib.request.urlopen(req, timeout=15)
        for h in (r.headers.get_all("Set-Cookie") or []):
            if h.startswith("SID="): s.cookie = h.split(";",1)[0]
        body = r.read().decode("utf-8","ignore").strip()
        if not s.cookie and body != "Ok.":
            raise RuntimeError("qb 登录失败(账号密码?)")
    def _headers(s, extra=None):
        if s.pw and not s.cookie: s.login()
        h = {"Referer": s.url}
        if s.cookie: h["Cookie"] = s.cookie
        if extra: h.update(extra)
        return h
    def _retry(s, fn):
        """cookie 过期(403)时清掉重登一次。配合连接池:平时零登录,过期才补一次"""
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and s.pw:
                s.cookie = ""; return fn()
            raise
    def _post(s, path, params):
        data = urllib.parse.urlencode(params).encode()
        def go():
            req = urllib.request.Request(s.url+path, data=data,
                  headers=s._headers({"Content-Type":"application/x-www-form-urlencoded"}))
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8","ignore")
        return s._retry(go)
    def _get(s, path):
        def go():
            req = urllib.request.Request(s.url+path, headers=s._headers())
            return urllib.request.urlopen(req, timeout=30).read()
        return s._retry(go)
    def add(s, data, category="", tags="", savepath=""):
        b = "----packseed" + str(int(time.time()*1000)); parts = []
        def field(name, val):
            parts.append(("--"+b+"\r\nContent-Disposition: form-data; name=\""+name+"\"\r\n\r\n"+val+"\r\n").encode())
        parts.append(("--"+b+"\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"t.torrent\"\r\n"
                      "Content-Type: application/x-bittorrent\r\n\r\n").encode()); parts.append(data); parts.append(b"\r\n")
        if category: field("category", category)
        if tags: field("tags", tags)
        if savepath: field("savepath", savepath); field("autoTMM", "false")   # 指定目录时禁自动管理,防被分类路径顶掉
        parts.append(("--"+b+"--\r\n").encode())
        req = urllib.request.Request(s.url+"/api/v2/torrents/add", data=b"".join(parts),
              headers=s._headers({"Content-Type":"multipart/form-data; boundary="+b}))
        return urllib.request.urlopen(req, timeout=40).read().decode("utf-8","ignore")
    def torrents(s, **filt):
        q = ("?"+urllib.parse.urlencode(filt)) if filt else ""
        return json.loads(s._get("/api/v2/torrents/info"+q).decode("utf-8","ignore"))
    def files(s, h):
        return json.loads(s._get("/api/v2/torrents/files?hash="+h).decode("utf-8","ignore"))
    def export(s, h):
        return s._get("/api/v2/torrents/export?hash="+h)
    def set_category(s, hashes, cat):
        try: s._post("/api/v2/torrents/createCategory", {"category":cat})
        except Exception: pass
        return s._post("/api/v2/torrents/setCategory", {"hashes":hashes, "category":cat})
    def add_tags(s, hashes, tags):
        return s._post("/api/v2/torrents/addTags", {"hashes":hashes, "tags":tags})
    def delete(s, hashes, delete_files=False):
        return s._post("/api/v2/torrents/delete", {"hashes":hashes, "deleteFiles":"true" if delete_files else "false"})

def human_size(n):
    n = float(n or 0)
    for u in ["B","KB","MB","GB","TB"]:
        if n < 1024: return (f"{int(n)}{u}" if u == "B" else f"{n:.1f}{u}")
        n /= 1024
    return f"{n:.1f}PB"

# ============ 名字解析 + TMDB 识别（整理器） ============
_CN_NUM = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
_STOP = re.compile(r'^(19\d{2}|20\d{2}|\d{3,4}[pi]|2160p|1080p|720p|x26[45]|h\.?26[45]|hevc|avc|'
                   r'bluray|blu-ray|web-?dl|webrip|hdtv|remux|bdrip|dvdrip|uhd|hdr|dv|dts|ddp?\d|'
                   r'truehd|flac|aac|atmos|nf|amzn|complete|proper|repack|internal|us|'
                   r'国语|粤语|中字|中英|双语|国粤|简繁|字幕|内封|内嵌)$', re.I)
_VIDEO_EXT = (".mkv",".mp4",".ts",".avi",".m2ts",".wmv",".mov",".flv",".rmvb",".iso")

def _cn_num(s):
    if s.isdigit(): return int(s)
    if s == '十': return 10
    if len(s)==2 and s[0]=='十': return 10+_CN_NUM.get(s[1],0)
    if len(s)==2 and s[1]=='十': return _CN_NUM.get(s[0],0)*10
    return _CN_NUM.get(s)
def meta_year(n):
    m = re.findall(r'(19\d{2}|20\d{2})', n); return m[0] if m else ""
def meta_season(n):
    m = re.search(r'S(\d{1,2})(?:-S?\d{1,2})?', n, re.I)
    if m: return int(m.group(1))
    m = re.search(r'第\s*([\d一二三四五六七八九十]+)\s*季', n)
    if m: return _cn_num(m.group(1))
    if re.search(r'Season\s?(\d+)', n, re.I): return int(re.search(r'Season\s?(\d+)', n, re.I).group(1))
    if re.search(r'E\d{1,3}|EP\d+|\d+集', n): return 1
    return None
def meta_is_tv(n):
    return bool(re.search(r'(S\d{1,2}(?![0-9])|Season\s?\d|第[\d一二三四五六七八九十]+季|E\d{1,3}|EP\d+|\d+集|集全|全\d+集|complete\s+series)', n, re.I))
def meta_is_anime(n):
    # 启发式：常见番组字幕组/来源标记，或番组括号命名能解析出标题
    return bool(re.search(r'\b(baha|bahamut|ani|vcb-studio|nc-raws|sweetsub|lolihouse|喵萌|animes?|'
                          r'jitaku|ohys|leopard-raws|erai-raws|subsplease|horriblesubs|philosophy-raws|'
                          r'桜都|千夏|澄空|华盟|极影|悠哈|幻樱|白恋|风车|雪飘|星空|黒ネズミたち|北宇治)\b', n, re.I)
                or re.search(r'\[[^\]]+\].*\[\d{2,3}(v\d)?\]', n)          # [组]...[集号]
                or (n[:1] in "[【" and _anime_title(n) != ("", "")))       # 番组命名解析成功
def meta_title_cn(n):
    first = re.split(r'[._]', n, maxsplit=1)[0]; first = re.split(r'[￡@]', first)[0].strip().strip('[]【】()（） ')
    if CJK.search(first):
        t = re.split(r'(S\d{1,2}|第[\d一二三四五六七八九十]+季|Season|E\d{1,3}|\d+集)', first, flags=re.I)[0]
        # 文件夹经常已经被错误刮成「片名 (年份)」。年份不是片名的一部分，带着它去
        # TMDB 搜会把同名的老电影当成唯一候选，剧集即使有 E01-E32 也救不回来。
        # first.strip() 会先去掉末尾右括号，因此这里右括号必须是可选的。
        t = re.sub(r'\s*[（(]\s*(?:19|20)\d{2}\s*[）)]?\s*$', '', t)
        t = re.sub(r'[A-Za-z].*$','',t).strip() if CJK.search(t) else t
        if t.strip(): return t.strip()
    return ""
def meta_title_en(n):
    s = re.sub(r'[一-鿿]',' ', re.sub(r'[._]+',' ', re.split(r'[￡@]', n)[0])); out=[]
    for tok in s.split():
        if not re.search(r'[A-Za-z]',tok):
            if out: break
            continue
        if _STOP.match(tok) or re.match(r'^S\d{1,2}',tok,re.I) or re.match(r'^E\d{1,3}$',tok,re.I):
            if out: break
            continue
        out.append(tok)
    while out and out[-1].lower()=='the': out.pop()
    return ' '.join(out[:6]).strip()

_ANIME_JUNK = re.compile(r'^(\d{1,4}(-\d{1,4})?(v\d)?|(19|20)\d{2}[-.]?\d{0,4}|.*\d{3,4}[pP].*|WEB.?(DL|RIP)?.*|BD(RIP|BOX)?.*|'
                         r'x?26[45].*|HEVC.*|AVC.*|AAC.*|FLAC.*|OPUS.*|MKV|MP4|GB|BIG5|JP(SC|TC)?|SC|TC|CHT|CHS|'
                         r'简繁.*|繁[體体]?.*|简[体日]?.*|招募.*|\d{1,2}月新番.*|★.*|新番.*|合集|全集|完结|字幕.*|'
                         r'Fin|END|S\d{1,2}|Season\s?\d+|OVA\d*|OAD|SP\d*|Movie|剧场版|檢索.*|V\d|'
                         r'国漫|日漫|美漫|港漫|漫画改?|轻改|游戏改)$', re.I)

def _anime_title(n):
    """番组命名：[组] 标题 [01-24] / 【组】【标题】[话数] / [组] Title - 01 (1080p)。
    返回 (中文题, 英文/罗马字题)；识别不了返回 ("","")"""
    segs = [a or b for a, b in re.findall(r'\[([^\[\]]+)\]|【([^【】]+)】', n)]
    if len(segs) < 2 and not re.search(r'\]\s*[^\[\]]+\s*-\s*\d{1,3}', n):
        return "", ""      # 不是典型番组命名(如 [福贵].Fu.Gui 这种走普通解析)
    # 括号外的裸文本(如 "[SubsPlease] Sousou no Frieren - 28 (1080p)" 的中段)
    bare = re.sub(r'\[[^\[\]]*\]|【[^【】]*】|\([^()]*\)', ' ', n).strip(' ★-_')
    cands = ([bare] if bare else []) + segs[1:]   # 第一个括号通常是字幕组，跳过
    for seg in cands:
        seg = seg.replace('_', ' ').strip(' ★-')
        seg = re.sub(r'\s*[-–]\s*\d{1,4}(\.\d)?(v\d)?\s*(END|Fin)?\s*$', '', seg, flags=re.I)  # 去尾部话数 " - 01"/" - 1071"
        seg = re.sub(r'\s*第?\d{1,4}[-~]\d{1,4}[话話集]?(\+.*)?$', '', seg)                      # 去 "01-24话"
        seg = re.sub(r'\s*第\d{1,4}[话話集]$', '', seg)                                          # 去 "第1123话"
        seg = re.sub(r'\s*(第[\d一二三四五六七八九十]+季|\d+(st|nd|rd|th)\s+Season|Season\s?\d+|Part\s?\d+|S\d{1,2})\s*$', '', seg, flags=re.I)  # 去 "第二季"/"Season 2"
        seg = seg.strip(' ★-')
        if not seg or _ANIME_JUNK.match(seg): continue
        # 中英双语段 "夏日重现/Summer Time Rendering" 或 "葬送的芙莉莲 Sousou no Frieren"
        if '/' in seg:
            parts = [p.strip() for p in seg.split('/') if p.strip()]
            cn = next((p for p in parts if CJK.search(p)), "")
            en = next((p for p in parts if p and not CJK.search(p)), "")
            return cn, en
        if CJK.search(seg):
            mixed = re.match(r'^([^\sA-Za-z]*[一-鿿][^A-Za-z]*)\s+([A-Za-z].*)$', seg)
            if mixed: return mixed.group(1).strip(), mixed.group(2).strip()
            return seg, ""
        return "", seg
    return "", ""

def _tmdb_call(path, **params):
    params["api_key"] = CFG["TMDB_KEY"]
    u = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    op = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http":CFG["TMDB_PROXY"],"https":CFG["TMDB_PROXY"]})) if CFG["TMDB_PROXY"] else urllib.request.build_opener()
    return json.load(op.open(u, timeout=20))
def _tmdb_search(q, tv_only=False):
    try:
        if tv_only:   # 确定是剧集时用 tv 专用端点：排序更准，长寿番(如1999海贼王)不会被剧场版淹没
            rs = _tmdb_call("/search/tv", query=q, language="zh-CN").get("results", [])
            for r in rs: r["media_type"] = "tv"
            if rs: return rs
        return [r for r in _tmdb_call("/search/multi", query=q, language="zh-CN", include_adult="false").get("results",[])
                if r.get("media_type") in ("movie","tv")]
    except Exception:
        return []
def _ryear(r): return (r.get("release_date") or r.get("first_air_date") or "")[:4]

_TMDB_CACHE = {}
def tmdb_match(name, force_tv=False):
    # 同一个标题在「普通判断」和「文件已证明是剧集」两种上下文不能共用缓存：
    # 否则先命中过电影的结果会污染随后按文件清单发起的纠正识别。
    cache_key = (name, bool(force_tv))
    hit = _TMDB_CACHE.get(cache_key)
    if hit and time.time() - hit[1] < 21600:
        return hit[0]
    m = _tmdb_match_raw(name, force_tv=force_tv)
    if len(_TMDB_CACHE) > 2000: _TMDB_CACHE.clear()
    _TMDB_CACHE[cache_key] = (m, time.time())
    return m

def _tmdb_match_raw(name, force_tv=False):
    """解析 name → 匹配 TMDB。返回 dict(mtype,id,tmdb_name,year,conf,q) 或 None。结果缓存6小时。"""
    if not CFG["TMDB_KEY"]: return None
    tc, te = _anime_title(name)          # 番组命名优先(动漫)
    if not (tc or te):
        tc, te = meta_title_cn(name), meta_title_en(name)
    year = meta_year(name)
    # 名字里带集数/季标记 → 明确是剧集，别让剧场版/总集篇/舞台剧抢走匹配
    want_tv = force_tv or bool(re.search(r'\[\d{1,4}(-\d{1,4})?(v\d)?\]|第\d{1,4}[话話集]|[-–]\s*\d{1,4}\s*[\[\(]|'
                                        r'S\d{1,2}(?!\d)|E\d{1,3}|\d+集', name) or meta_is_tv(name))
    for q in [x for x in (tc, te) if x]:
        cand = _tmdb_search(q, tv_only=want_tv)
        if not cand: continue
        if want_tv and any(r.get("media_type") == "tv" for r in cand):
            cand = [r for r in cand if r.get("media_type") == "tv"]
        if meta_is_anime(name):                                 # 动漫优先"动画"类型(如 One Piece 别配到真人版)
            ani = [r for r in cand if 16 in (r.get("genre_ids") or [])]
            if ani: cand = ani
        # 话数很大(≥50)=长寿番，同名候选取最早开播的(如 One Piece 1071 → 1999 原版而非重制版)
        epm = (re.search(r'[-–]\s*(\d{2,4})(?:v\d)?\s*(?:[\[\(]|$)', name)
               or re.search(r'第(\d{1,4})[话話集]', name)
               or re.search(r'\[(?!(?:19|20)\d{2}\])(\d{2,3})(?:v\d)?\]', name))
        big_ep = epm and int(epm.group(1)) >= 50
        if big_ep and len(cand) > 1:
            cand.sort(key=lambda r: (_ryear(r) or "9999"))
        else:
            cand.sort(key=lambda r: -(r.get("popularity") or 0))   # 正片热度远高于总集篇/衍生
        # 优先级：名字准+年份准 > 名字像+年份准 > 仅名字准 > 年份对但名字完全不像(不可信,low) > 首位候选(low)
        def _names(r):
            return [x for x in [r.get("name") or r.get("title"),
                                r.get("original_name") or r.get("original_title")] if x]
        def _namefit(r):
            ql = q.lower()
            for nn in _names(r):
                n = nn.lower()
                if CJK.search(q) or CJK.search(n):
                    if ql in n or n in ql: return True
                # 英文按词边界，防 Sakra 误配 Sakrament 这种子串陷阱
                elif re.search(r'(^|\W)' + re.escape(ql) + r'(\W|$)', n) or \
                     re.search(r'(^|\W)' + re.escape(n) + r'(\W|$)', ql):
                    return True
            return False
        def _alt_fit(r):
            # 终审：查该条目的官方别名(各语言译名)。射雕的别名含 The Legend of the Condor Heroes → 放行；
            # Sakrament 的别名不含 Sakra → 拦截。只在歧义分支调用，多一次 API 换准确率。
            try:
                mt = "tv" if r.get("media_type") == "tv" else "movie"
                d = _tmdb_call(f"/{mt}/{r['id']}/alternative_titles")
                alts = [t.get("title","") for t in (d.get("results") or d.get("titles") or [])]
                norm = lambda x: re.sub(r'[^a-z0-9一-鿿]', '', x.lower())
                qn = norm(q)
                return any(qn and (qn == norm(t) or qn in norm(t) or norm(t) in qn) for t in alts if t)
            except Exception:
                return False
        exact = [r for r in cand if q in _names(r)]
        if year:
            ye = [r for r in cand if _ryear(r) == year]
            exact_ye = [r for r in ye if r in exact]
            ye_fit = [r for r in ye if _namefit(r)]
            if exact_ye:  pick = (exact_ye[0], "high")
            elif ye_fit:  pick = (ye_fit[0], "high")
            elif exact:   pick = (exact[0], "high")   # 名字精确但年份解析错了(请回答1988型)
            elif ye and _alt_fit(ye[0]): pick = (ye[0], "high")   # 别名验证通过(英文译名场景)
            else:         pick = ((ye[0], "low") if ye else (cand[0], "low"))
        else:
            if exact: pick = (exact[0], "high")
            elif _namefit(cand[0]) or _alt_fit(cand[0]): pick = (cand[0], "mid")
            else: pick = (cand[0], "low")
        r, conf = pick
        if conf=="low" and q==tc and te and te.lower()!=tc.lower(): continue  # 中文低置信→再试英文
        return {"mtype":"tv" if r.get("media_type")=="tv" else "movie","id":r.get("id"),
                "tmdb_name":(r.get("name") or r.get("title")),"year":_ryear(r),"conf":conf,"q":q,
                "poster":r.get("poster_path") or "","overview":r.get("overview") or "",
                "anime": 16 in (r.get("genre_ids") or [])}
    return None

def tmdb_by_id(tid, mtype_hint=""):
    order = ["movie","tv"] if mtype_hint == "movie" else ["tv","movie"]
    for mt in order:
        try:
            d = _tmdb_call(f"/{mt}/{tid}", language="zh-CN")
            if d.get("id"):
                # 详情接口给的是 genres 对象数组(不是搜索接口的 genre_ids),16=动画。
                # 不带这个字段的话,手动改识别的动漫会漏判成电视剧、进错库。
                anime = any(g.get("id") == 16 for g in (d.get("genres") or []))
                return {"mtype":mt,"id":d["id"],"tmdb_name":(d.get("name") or d.get("title")),
                        "year":(d.get("first_air_date") or d.get("release_date") or "")[:4],
                        "conf":"manual","q":str(tid),"anime":anime,
                        "poster":d.get("poster_path") or "","overview":d.get("overview") or ""}
        except Exception: pass
    return None

# ============ 查询锚定:先把「用户输的这句话」钉到 TMDB 上的唯一一部作品 ============
# 搜不准的两个老根子:
#   ① 年份被原样丢给站点检索。站内种子名里带年份的是少数,一加年份反而搜空 —— 越限定越搜不到。
#   ② 归组只认「种子名解析出来的词」,用户真正要的那部一旦解析偏了,就掉进「未识别/其他」。
# 解法:年份只留给 TMDB 做消歧,站点只搜片名;钉住作品后拿它的全部别名(中/原/各语种译名)去认领种子,
#      再用年份把不是这一版的种子挡在组外。这样「加年份」第一次真正起到限定作用。
# 前后界用零宽断言,不吃掉分隔符 —— 否则「1917 2019」里前一个年份会把空格吞掉,后一个就再也匹配不上
_QYEAR = re.compile(r'(?:^|(?<=[\s\-_.\[\(（【]))((?:19|20)\d{2})(?=$|[\s\-_.\]\)）】])')
_TYEAR = re.compile(r'(?<![0-9])((?:19|20)\d{2})(?![0-9pxi])')

def split_query(q):
    """用户输入 → (片名, 年份)。年份只做消歧,不进站点检索词。
       只认独立成词的年份:《大明王朝1566》《请回答1988》里的数字是片名的一部分,不会被摘走。"""
    q = (q or "").strip()
    m = None
    for mm in _QYEAR.finditer(q): m = mm        # 取最后一个:片名本身是年份时(如「1917 2019」)别摘错
    if not m: return q, ""
    name = (q[:m.start(1)] + " " + q[m.end(1):])
    name = re.sub(r'\s{2,}', ' ', name).strip(" -_.[]()（）【】")
    if not name: return q, ""      # 整句就是个年份(《1917》《2012》)——那是片名,不是限定条件
    return name, m.group(1)

def _norm(s):
    return re.sub(r'[^0-9a-z一-鿿]+', '', (s or "").lower())

def _alias_hit(title, alias):
    """种子标题是否属于这批别名。中文按归一化子串;英文必须按词边界,
       否则 Sakra 会认领 Sakrament、Dark 会认领 Darkest —— 这类子串陷阱是误配大户。"""
    tl = re.sub(r'[._]+', ' ', title or "").lower()
    tn = _norm(title)
    for a in alias:
        if not a: continue
        if CJK.search(a):
            an = _norm(a)
            if len(an) >= 2 and an in tn: return True
        else:
            al = a.lower().strip()
            if len(al) < 4: continue          # 太短的英文别名(如 IT、Us)一放开就满屏误配,宁可漏
            pat = r'[\s._\-]+'.join(re.escape(w) for w in al.split())
            if re.search(r'(^|[^a-z0-9])' + pat + r'([^a-z0-9]|$)', tl): return True
    return False

def _year_ok(title, year, tol=1):
    """种子标题里的年份和目标年份对不对得上。标题里压根没年份 → 放行(不能因为发布组懒得写就丢掉)。"""
    if not (year or "").isdigit(): return True
    ys = [int(y) for y in _TYEAR.findall(title or "")]
    if not ys: return True
    return any(abs(y - int(year)) <= tol for y in ys)

_ANCHOR_CACHE = {}
def query_anchor(name, year="", filt=""):
    """把用户查的片名钉到 TMDB 上的一部作品,连别名一起取回来。缓存 6 小时。
       filt 是前端选的类型:选了电影/电视剧就据此消歧(库里 2003 电影《手机》≠ 2010 电视剧《手机》)。"""
    if not CFG["TMDB_KEY"] or not name: return None
    ck = f"{name}|{year}|{filt}"
    hit = _ANCHOR_CACHE.get(ck)
    if hit and time.time() - hit[1] < 21600: return hit[0]
    a = None
    try: a = _query_anchor_raw(name, year, filt)
    except Exception as e: logmsg("WARN", f"锚定失败[{name}]: {e}")
    if len(_ANCHOR_CACHE) > 500: _ANCHOR_CACHE.clear()
    _ANCHOR_CACHE[ck] = (a, time.time())
    return a

# ---- §7 查询理解:别名扇出该扇哪几个词 ----
def _script_of(s):
    """这个名字用的哪套文字。同一套文字里的多个拼写多半是近似重复,扇出去等于白花一波。

       ⚠️ 判定必须看**字符实际属于哪个区**,不能拿「不含汉字」当「是英文」——
       假名/谚文/西里尔都不含汉字,当成英文名扇出去,就是让几十个站白搜一遍假名。"""
    if re.search(r'[぀-ゟ゠-ヿ]', s or ""): return "kana"      # 日文假名
    if re.search(r'[가-힯]', s or ""): return "hangul"                 # 韩文谚文
    if re.search(r'[一-鿿]', s or ""): return "han"                    # 汉字
    if re.search(r'[Ѐ-ӿ]', s or ""): return "cyrl"
    if re.search(r'[A-Za-z]', s or ""): return "latin"
    return "other"

def _norm_alias(s):
    """归一化:剥变音符、小写、只留字母数字和 CJK。
       Shiroi kyotô 和 Shiroi Kyoto 归一化之后就认得出是同一个词。"""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9一-鿿぀-ヿ가-힯]', '', s.lower())

def alias_plan(alias, qname, k=2):
    """从别名池里挑 k 个**真正不同的召回入口**。

       为什么不能随手挑:TMDB 的 alternative_titles 里绝大多数是近似重复 ——
       《白色巨塔》挑出来的是 Shiroi Kyotou / Shiroi kyotô / Shiroi Kyoto,
       同一种罗马字拼法的三个变体。三个词扇出去实测墙上时间 +28%,
       只多认领 0~5 条,基本是纯浪费(所以「别名扇出」这一项一直没敢做)。

       做法:先按文字分桶(汉字/假名/谚文/拉丁),桶内再按拼写相似度聚类,
       每类只留一个代表,然后**在各桶之间轮转取** —— 保证挑出来的是不同入口,
       而不是同一个入口的三种拼写。与原查询同文字的桶排最后(那多半是重复)。"""
    qn = _norm_alias(qname); qs = _script_of(qname)
    buckets = {}
    for a in alias or []:
        a = (a or "").strip()
        if not (2 <= len(a) <= 40): continue
        na = _norm_alias(a)
        if not na or na == qn: continue                 # 跟原查询等价,搜了也是重复
        buckets.setdefault(_script_of(a), []).append((a, na))
    picks = {}
    for sc, items in buckets.items():
        reps = []
        for a, na in sorted(items, key=lambda x: (len(x[1]), x[0])):   # 短的优先:长的常带副标题
            if any(difflib.SequenceMatcher(None, na, rn).ratio() > 0.8 for _, rn in reps):
                continue                                # 拼写近似 → 同一个入口,不重复扇
            reps.append((a, na))
        picks[sc] = [a for a, _ in reps]
    order = sorted(picks, key=lambda x: (x == qs, x))   # 同文字的排最后
    out = []
    for rnd in range(4):
        for sc in order:
            if len(picks[sc]) > rnd:
                out.append(picks[sc][rnd])
                if len(out) >= k: return out
    return out

def _query_anchor_raw(name, year="", filt=""):
    want = {"movie": "movie", "tv": "tv", "anime": "tv"}.get(filt, "")
    cand = _tmdb_search(name, tv_only=(want == "tv"))
    if not cand: return None
    if want:      # 选了类型就只认这一类,认不出再退回全部(冷门剧 TMDB 有时只收成电影)
        cand = [r for r in cand if (r.get("media_type") == "tv") == (want == "tv")] or cand
    def nms(r): return [x for x in (r.get("name"), r.get("title"),
                                    r.get("original_name"), r.get("original_title")) if x]
    nq = _norm(name)
    if not nq: return None
    exact = [r for r in cand if any(_norm(x) == nq for x in nms(r))]
    fit   = [r for r in cand if any(nq in _norm(x) or _norm(x) in nq for x in nms(r))]
    pool  = exact or fit or cand
    if year:
        yp = [r for r in pool if _ryear(r) == year] or \
             [r for r in pool if _ryear(r).isdigit() and abs(int(_ryear(r)) - int(year)) <= 1]
        if yp: pool = yp
        # 年份一个都对不上但名字对得上 → 仍以名字为准(TMDB 首播年和站里标的年常差一年),
        # 后面挑种子时再按用户给的年份筛,不在这里把作品否掉。
    pool = sorted(pool, key=lambda r: -(r.get("popularity") or 0))
    r = pool[0]
    mt = "tv" if r.get("media_type") == "tv" else "movie"
    alias = {name} | {x.strip() for x in nms(r) if x and x.strip()}
    try:
        d = _tmdb_call(f"/{mt}/{r['id']}/alternative_titles")
        for t in (d.get("results") or d.get("titles") or []):
            tt = (t.get("title") or "").strip()
            if tt: alias.add(tt)
    except Exception: pass
    # 补搜词:用户敲中文就补个拉丁名,敲英文就补个中文名 —— 站里同一部剧两种命名都有,只搜一种必漏。
    # 必须显式判「是不是拉丁字母」,不能拿「没有汉字」当拉丁:CJK 只覆盖汉字区(U+4E00-9FFF),
    # 纯假名的日译名(こいのスケッチ…)不含汉字,会被当成英文名选中,白白让 66 个站搜一遍假名。
    altqs = alias_plan(alias, name, k=2)
    return {"mtype": mt, "id": r.get("id"),
            "name": (r.get("name") or r.get("title") or name), "year": _ryear(r),
            "poster": r.get("poster_path") or "", "overview": r.get("overview") or "",
            "anime": 16 in (r.get("genre_ids") or []),
            "alias": sorted(alias, key=lambda x: -len(x)),
            "altqs": altqs, "altq": altqs[0] if altqs else "",
            "qname": name, "qyear": year}

def meta_is_music(n):
    return bool(re.search(r'\b(FLAC|APE|WAV|DSD|DSF|SACD|MQA|24bit|24-96|24-192|Hi-?Res|无损|MP3|320K)\b', n, re.I)
                and not re.search(r'\b(\d{3,4}[pi]|x26[45]|HEVC|BluRay|WEB-?DL|REMUX)\b', n, re.I))

def media_category(name, m):
    """qb 分类：音乐 > 动漫 > 电视剧 > 电影"""
    if meta_is_music(name): return "音乐"
    if meta_is_anime(name): return "动漫"
    # 光看种子名不够:欧美式发布名(如 Natsume's.Book.of.Friends.S05.2016.1080p.CR.WEB-DL)
    # 没有任何字幕组标记,meta_is_anime 认不出来,夏目友人帐/灌篮高手 就这么全进了剧集库。
    # TMDB 早就知道它是 genre 16(动画),这里直接采信。
    if m and m.get("anime"): return "动漫"
    if m: return "电视剧" if m["mtype"]=="tv" else "电影"
    return "电视剧" if meta_is_tv(name) else "电影"

def walk_files(path):
    """把磁盘路径(文件或目录)展开成 [(绝对路径, 相对名)]"""
    if os.path.isfile(path): return [(path, os.path.basename(path))]
    out = []
    for root, _, fs in os.walk(path):
        for f in fs:
            ap = os.path.join(root, f); out.append((ap, os.path.relpath(ap, path)))
    return out

def files_indicate_tv(files):
    """实际文件清单优先于种子标题：两个以上不同的 E## 视频就是电视剧。

    搜索标题可能是错误的历史目录名，或只写了英文片名，不能据此把整季剧集
    错送进电影库。只接受至少两集，避免花絮/单个试播集把电影误判成电视剧。
    """
    episodes = set()
    for _src, rel in files:
        stem, ext = os.path.splitext(os.path.basename(rel))
        if ext.lower() not in _VIDEO_EXT:
            continue
        m = re.search(r'(?:\bS(\d{1,2})[ ._-]?)?\bE(\d{1,3})\b', stem, re.I)
        if m:
            episodes.add((int(m.group(1) or 1), int(m.group(2))))
    return len(episodes) >= 2

# ============ 整理入库（硬链接） + 转种 + 通知 Emby ============
def _safe(s): return re.sub(r'[\\/:*?"<>|]+',' ',(s or "")).strip()

_SEASON_RE     = re.compile(r'S(\d{1,2})\s?E\d{1,3}', re.I)                      # S05E03
_SEASONPACK_RE = re.compile(r'(?:^|[.\s_\-\[（(])S(\d{1,2})(?![0-9EeXxPp])', re.I)  # 整季包 .S05.
_MULTISEASON_RE= re.compile(r'S\d{1,2}\s*[-~]\s*S?\d{1,2}', re.I)                # S01-S03 跨季包
def _season_of(s):
    """从文件名/目录名/种子名里抠季号,抠不到返回 None（不瞎猜第 1 季）。
       识别三种写法:SxxExx 单集、Sxx 整季包、Season N / 第N季。
       跨季包(S01-S03)返回 None —— 它横跨好几季,只能靠每个文件自己的 SxxExx 定位,
       在这里猜一个季号会把三季文件全塞进 Season 01。"""
    s = s or ""
    mm = _SEASON_RE.search(s)
    if mm: return int(mm.group(1))
    if _MULTISEASON_RE.search(s): return None
    mm = _SEASONPACK_RE.search(s)
    if mm:
        v = int(mm.group(1))
        if 0 < v < 100: return v
    mm = re.search(r'(?:Season|第)\s*([0-9一二三四五六七八九十]{1,3})\s*[季]?', s, re.I)
    if mm:
        v = _cn_num(mm.group(1))
        if v and 0 < v < 100: return v
    return None

# 制作组:PT 命名惯例是缀在最后,「-CMCT」「@FRDS」「-52KHD」「[TLF]」都是。
# 但结尾也可能是分辨率/编码(「-1080p」「-x265」),那些不是组名,得挡掉,
# 否则一部剧会冒出一堆叫「1080p」的假组,推荐统一就成了笑话。
_GRPBAD = re.compile(r"""^(?:\d{3,4}p|x26[45]|h26[45]|hevc|avc|aac|ac3|dts(?:hd|ma)?|"""
                     r"""truehd|flac|ape|wav|web|webrip|webdl|bluray|blu|remux|hdtv|dvd|"""
                     r"""dvdrip|bdrip|repack|proper|internal|10bit|8bit|hdr|hdr10|sdr|uhd|"""
                     r"""4k|2160|1080|720|480|mkv|mp4|iso|cd\d?|dis[ck]\d?|part\d?|"""
                     r"""complete|multi|chs|cht|eng|jpn)$""", re.I)
_GRP_RE = re.compile(r'(?:[-@＠]|\[)\s*([A-Za-z0-9][A-Za-z0-9_.\-]{1,19})\s*\]?\s*$')
def _relgrp(t):
    """从种子名末尾抠制作组,抠不到返回空串(不瞎编)。"""
    t = re.sub(r'\.(?:mkv|mp4|ts|avi|iso)$', '', (t or "").strip(), flags=re.I)
    m = _GRP_RE.search(t)
    if not m: return ""
    g = m.group(1).strip(" .-_")
    if not g or _GRPBAD.match(g): return ""
    if not re.search(r'[A-Za-z]', g): return ""      # 纯数字不是组名
    return g

def _is_pack(t):
    """是不是跨季合集。单季整季包不算 —— 那个归它自己那一季,不然「第1季」会空着。"""
    t = t or ""
    if _MULTISEASON_RE.search(t): return True
    if re.search(r'(?:全|共)\s*[0-9一二三四五六七八九十]{1,3}\s*季', t): return True
    if re.search(r'Complete\s*(?:Series|Collection|Pack)', t, re.I): return True
    if re.search(r'Season\s*\d{1,2}\s*[-~]\s*\d{1,2}', t, re.I): return True
    if re.search(r'[0-9一二三四五六七八九十]{1,3}\s*[-~]\s*[0-9一二三四五六七八九十]{1,3}\s*季', t): return True
    return False


# ==================== 搜索内核 ====================
# 全站**唯一**解析种子名的地方,也是**唯一**给种子打分的地方。
#
# 之前这两件事各有三份实现:交互搜索只按 seeders 排、榜单批量走 _pick_release、
# 音乐走 _score_music。同一个「哪个版本更好」有三个互不相同的答案,
# 加一个需求得改三处、改漏一处就出怪事 —— 这才是「补丁摞补丁」的根,
# 不是某一个具体 bug。合并成「解析 → 打分」两层后,新需求只会落在一处:
# 要么给 parse_release 加一个字段,要么给某个 intent 调一次权重。
_RE_RES   = re.compile(r'\b(2160p|1080p|720p|480p|4k)\b', re.I)
_RE_SRC   = re.compile(r'\b(remux|blu-?ray|bd-?rip|web-?dl|web-?rip|hdtv|dvd-?rip|dvd)\b', re.I)
_RE_CODEC = re.compile(r'\b(x265|h\.?265|hevc|x264|h\.?264|avc)\b', re.I)
_RE_HDR   = re.compile(r'\b(hdr10\+?|hdr|dolby\s*vision|dovi)\b', re.I)

# 中文音轨。**最容易错的地方是把字幕当音轨** ——「中英双语字幕」「中字」「简繁」
# 说的都是字幕,认成国语会让人白高兴一场,点进去还是原声。所以「双语」必须排除后面跟「字幕」的写法。
# 粤语单列:它是中文,但内地用户要的多半是国语,排序时要比国语低一档。
_ZH_GUO  = re.compile(r'国语|國語|国配|國配|普通话|普通話|中文配音|中配|\bMandarin\b', re.I)
_ZH_DUAL = re.compile(r'国粤|國粵|粤国|粵國|国英|國英|国日|國日|国韩|國韓|双音轨|雙音軌'
                      r'|(?:双语|雙語|双語)(?!\s*字幕)', re.I)
_ZH_YUE  = re.compile(r'粤语|粵語|\bCantonese\b', re.I)
# 「4Audio」「3声轨」这类只说明音轨条数,没说是什么语言。但中文站上 3 条以上的碟
# 基本就是 英语+国语+粤语/台配 —— 实测 Zootopia 那个 4Audio 的碟确实带国语。
# 这是**推断不是明写**,所以单开一档「多音轨」提示,不冒充「国语」:
# 规律五说了,判不出就别硬判,但也不能默默扔掉,得让人看见自己去确认。
_ZH_MULTI = re.compile(r'\b([3-9])\s*Audio\b|[三四五六3-9]\s*[声聲]轨|多国语言|多國語言|多语言|多語言', re.I)

def parse_release(it):
    """一条搜索结果 → 补全结构化字段(原地改并返回)。it 至少要有 title。

       只解析**读得出来的**,读不出来一律留 0/空串/None ——
       猜出来的字段比没有更糟:季号猜错会把三季塞进一季,组名猜错会推荐一个叫「1080p」的组。"""
    t = it.get("title") or ""
    it["pack"] = _is_pack(t)                                  # 跨季合集
    it["ss"]   = 0 if it["pack"] else (_season_of(t) or 0)    # 季号,0=判不出
    it["grp"]  = _relgrp(t)                                   # 制作组,""=判不出
    m = _RE_RES.search(t); r = (m.group(1).lower() if m else "")
    it["res"]  = {"2160p": 2160, "4k": 2160, "1080p": 1080, "720p": 720, "480p": 480}.get(r, 0)
    m = _RE_SRC.search(t)
    it["src"]  = re.sub(r'[-\s]', '', m.group(1).lower()) if m else ""
    m = _RE_CODEC.search(t)
    c = re.sub(r'[.\s]', '', m.group(1).lower()) if m else ""
    it["codec"] = "x265" if c in ("x265", "h265", "hevc") else ("x264" if c in ("x264", "h264", "avc") else "")
    it["hdr"]  = bool(_RE_HDR.search(t))
    # zhrank: 3=中外双语(最优:两条音轨都在,想听哪个听哪个)
    #         2=只有国语  1=粤语/只知道是多音轨  0=没有中文音轨(或没标)
    if _ZH_DUAL.search(t):
        it["zhkind"], it["zhrank"] = "双语", 3
    elif _ZH_GUO.search(t):
        it["zhkind"], it["zhrank"] = "国语", 2
    elif _ZH_YUE.search(t):
        it["zhkind"], it["zhrank"] = "粤语", 1
    elif _ZH_MULTI.search(t):
        it["zhkind"], it["zhrank"] = "多音轨?", 1
    else:
        it["zhkind"], it["zhrank"] = "", 0
    it["zhaud"] = it["zhrank"] > 0
    # 音乐维度(剧集/电影用不上,但解析一次比三处各判一次强)
    it["lossless"]  = bool(_LOSS_RE.search(t))
    it["split"]     = True if _SPLIT_RE.search(t) else (False if _WHOLE_RE.search(t) else None)
    it["single"]    = bool(_SINGLE_RE.search(t))
    it["nonstudio"] = bool(_NONSTUDIO.search(t))
    return it


def score_release(r, intent="browse", prefer_4k=False):
    """按**用途**打分。同一个种子在不同用途下该排第几,本来就不是一回事:

       browse  人在屏幕前挑 —— 做种数(公认度)是主序,画质只在 ±3 分内微调。
               不能替人做主:他可能就是想要那个 4K 原盘。
       collect 批量收藏、无人值守 —— 必须自己拿主意:体积落窗口、优先 x265、认分辨率偏好。
       music   收藏音乐 —— 分轨/正传/无损是硬指标,做种数退化成公认度的代理。"""
    sd = float(r.get("seeders") or 0)
    if intent == "music":
        sc = sd
        if r.get("split") is True:    sc += 500   # 分轨:Navidrome 才认得出单曲,硬需求
        elif r.get("split") is False: sc -= 400   # 整轨:一张碟一个大文件+CUE,播放器难用
        if re.search(r'\bFLAC\b', r.get("title") or "", re.I): sc += 120
        if r.get("nonstudio"): sc -= 250          # 精选/Live 往后排,正传优先
        sz = r.get("size") or 0
        if r.get("single") or sz < 120 * 1024**2: sc -= 600
        if sz > 3 * 1024**3: sc -= 300            # 超大合集不好管,也没法单张辅种
        return sc
    if intent == "collect":
        sc = sd
        # 批量下载没人盯着,必须自己拿主意:有国语的直接顶上去。
        # 交互搜索**不**在这里加分 —— 那边人在屏幕前,把 500 做种的原盘挤到国语版后面
        # 是替人做主。国语优先在呈现层做:排前面 + 打标 + 可筛选,看得见也关得掉。
        if CFG.get("PREFER_ZH_AUDIO") and r.get("zhrank"):
            sc += {3: 250, 2: 200}.get(r["zhrank"], 60)
        if r.get("codec") == "x265": sc += 30
        if prefer_4k and r.get("res") == 2160: sc += 200
        elif not prefer_4k and r.get("res") == 1080: sc += 50
        return sc
    sc = sd + {2160: 3, 1080: 2, 720: 1}.get(r.get("res") or 0, 0)
    if r.get("codec") == "x265": sc += 0.5
    if r.get("src") in ("remux", "bluray"): sc += 0.5
    return sc


def dedupe_releases(rs):
    """同一个发布往往好几个站都有,列表里就是连着好几行几乎一样的名字
       (实测绝命毒师第 1 季 11 条里有 3 条都是同一个 HHWEB 2160p)。
       合成一行、其余站收进 alts —— 列表短一半,「哪些版本可选」才看得清。

       **不丢数据**:alts 里保留每个站的完整信息,想换站下载仍然拿得到。
       抠不出制作组的不合并 —— 没有组名的指纹区分度不够,宁可多列几行也不能合错。"""
    rs = sorted(rs, key=lambda x: -score_release(x, "browse"))
    idx, out = {}, []
    for x in rs:
        gp = (x.get("grp") or "").lower()
        if not gp:
            out.append(x); continue
        # 指纹带体积:同组同季但 2160p 原盘和 1080p 重编码是两个东西,体积差得很远
        fp = (gp, x.get("ss", 0), bool(x.get("pack")), x.get("res", 0),
              x.get("src", ""), x.get("codec", ""), round((x.get("size") or 0) / (50 * 1024**2)))
        if fp not in idx:
            idx[fp] = len(out); x["alts"] = []; out.append(x)
        else:
            out[idx[fp]]["alts"].append({"site": x.get("site", ""), "seeders": x.get("seeders", 0),
                                         "url": x.get("url", ""), "info": x.get("info", "")})
    return out

def organize_files(files, m, cat, name_hint=""):
    """files: [(绝对源路径, 相对路径)]；按类型硬链接进媒体库。返回(目标目录, 链接数)
    普通剧/影：只链视频+字幕，拍平到目标目录(Emby 认文件名里的 SxxExx)。
    蓝光/DVD原盘(BDMV/VIDEO_TS)：完整保留目录结构、不过滤文件，Emby 才能当原盘碟识别。"""
    folder = f"{_safe(m['tmdb_name'])} ({m['year']})" if m.get("year") else _safe(m['tmdb_name'])
    if cat == "动漫" and CFG["MEDIA_ANIME"]: root = CFG["MEDIA_ANIME"]
    elif m and m["mtype"] == "movie": root = CFG["MEDIA_MOVIE"]
    else: root = CFG["MEDIA_TV"]
    dest_dir = os.path.join(root, folder)
    disc = any(re.search(r'(^|/)(BDMV|VIDEO_TS|CERTIFICATE)(/|$)', rel) for _, rel in files)
    n = 0
    for src, rel in files:
        if disc:
            # 去掉种子根目录这一层：电影文件夹直接包含 BDMV/，Emby 标准原盘结构
            sub = rel.split("/", 1)[1] if "/" in rel else rel
            dst = os.path.join(dest_dir, sub)
        else:
            if os.path.splitext(rel)[1].lower() not in _VIDEO_EXT and not rel.lower().endswith((".srt",".ass",".sub")):
                continue
            base = os.path.basename(rel)
            # 剧集按季分子目录(Emby 标准结构)。原来是全部拍平在剧集根目录 ——
            # 单季没问题,但多季陆续下载时 7 季一百多个文件混在一起,Emby 认季就容易出岔子,
            # 人也没法一眼看出哪季齐了。季号优先从文件名的 SxxExx 取,取不到再看种子名。
            if m and m.get("mtype") == "tv":
                ss = _season_of(base) or _season_of(rel) or meta_season(name_hint or "") or 1
                dst = os.path.join(dest_dir, f"Season {ss:02d}", base)
            else:
                dst = os.path.join(dest_dir, base)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst): n += 1; continue
        try:
            os.link(src, dst); n += 1
        except OSError as e:
            logmsg("WARN", f"硬链接失败 {os.path.basename(rel)}: {e}")
    # 目录属主对齐媒体库惯例(PUID/PGID)，别让 Emby/其他工具因权限犯嘀咕
    try:
        uid, gid = int(os.environ.get("PUID", "1000")), int(os.environ.get("PGID", "1001"))
        for root_, dirs, _fs in os.walk(dest_dir):
            os.chown(root_, uid, gid)
    except Exception:
        pass
    return dest_dir, n

def fetch_lyrics(files, root):
    """音乐入库后调 lrcapi 给每首歌落一个同名 .lrc —— Navidrome 网页/各类客户端都能显示"""
    if not CFG["LRCAPI_URL"]: return 0
    got = 0
    for _src, rel in files:
        if os.path.splitext(rel)[1].lower() not in (".flac",".mp3",".ape",".wav",".m4a",".ogg",".wv"): continue
        dst = os.path.join(root, os.path.splitext(rel)[0] + ".lrc")
        if os.path.exists(dst): continue
        base = os.path.splitext(os.path.basename(rel))[0]
        title = re.sub(r'^\s*\d{1,3}\s*[.\-_、]?\s*', '', base).strip()
        parts = rel.split("/")
        album = parts[-2] if len(parts) >= 2 else ""
        m = re.match(r'^(.{1,24}?)\s*[-–]\s', parts[0])
        artist = m.group(1).strip() if m else ""
        for params in ({"title": title, "artist": artist, "album": album}, {"title": title}):
            try:
                u = CFG["LRCAPI_URL"].rstrip("/") + "/lyrics?" + urllib.parse.urlencode(params)
                txt = urllib.request.urlopen(u, timeout=12).read().decode("utf-8", "ignore")
                if "[0" in txt:                  # 有时间轴才算真歌词
                    open(dst, "w", encoding="utf-8").write(txt)
                    try: os.chown(dst, int(os.environ.get("PUID","1000")), int(os.environ.get("PGID","1001")))
                    except Exception: pass
                    got += 1; break
            except Exception:
                continue
    return got

def fetch_covers(files, root):
    """给每个专辑文件夹落 cover.jpg(iTunes Search 免key,600x600)。Navidrome 优先读 cover.*"""
    audio_ext = (".flac",".mp3",".ape",".wav",".m4a",".ogg",".wv")
    albums = {}
    for _s, rel in files:
        if os.path.splitext(rel)[1].lower() in audio_ext:
            albums.setdefault(os.path.dirname(rel), rel)
    got = 0
    for d in albums:
        dstdir = os.path.join(root, d) if d else root
        if any(os.path.exists(os.path.join(dstdir, c)) for c in ("cover.jpg","cover.png","folder.jpg","front.jpg","Cover.jpg")):
            continue
        album = os.path.basename(d) if d else ""
        top = (d or "").split("/")[0]
        m = re.match(r'^(.{1,24}?)\s*[-–]\s', top)
        artist = m.group(1).strip() if m else ""
        if not album: continue
        try:
            q = urllib.parse.urlencode({"term": f"{artist} {album}".strip(), "media": "music",
                                        "entity": "album", "limit": 1, "country": "cn"})
            r = json.load(urllib.request.urlopen("https://itunes.apple.com/search?" + q, timeout=15))
            res = r.get("results") or []
            if not res: continue
            art = (res[0].get("artworkUrl100") or "").replace("100x100", "600x600")
            if not art: continue
            img = urllib.request.urlopen(art, timeout=20).read()
            if len(img) > 5000:
                p = os.path.join(dstdir, "cover.jpg")
                open(p, "wb").write(img)
                try: os.chown(p, int(os.environ.get("PUID","1000")), int(os.environ.get("PGID","1001")))
                except Exception: pass
                got += 1
        except Exception:
            continue
    return got

def organize_music(ih, name, files):
    """音乐入库：整个种子目录结构原样硬链接进 Navidrome 音乐库。
    不动文件名不拍平——tag 是 Navidrome 的事，歌词是 lrcapi 的事。"""
    root = CFG["MEDIA_MUSIC"]; n = 0
    for src_, rel in files:
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst): n += 1; continue
        try: os.link(src_, dst); n += 1
        except OSError as e: logmsg("WARN", f"硬链接失败 {os.path.basename(rel)}: {e}")
    try:
        uid, gid = int(os.environ.get("PUID","1000")), int(os.environ.get("PGID","1001"))
        top = os.path.join(root, files[0][1].split("/",1)[0]) if files and "/" in files[0][1] else root
        for r_, ds, _fs in os.walk(top): os.chown(r_, uid, gid)
    except Exception: pass
    c = db()
    c.execute("INSERT OR REPLACE INTO media(info_hash,name,cat,mtype,tmdbid,tmdb_name,year,target,conf,status,files,ts,poster) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (ih, name, "音乐", "music", None, name[:60], "", root, "music", "done", n, int(time.time()), ""))
    c.commit(); c.close()
    lrc = fetch_lyrics(files, root)
    cov = fetch_covers(files, root)
    logmsg("INFO", f"音乐入库 {name[:44]} | {n}个文件 + {lrc}首歌词 + {cov}张封面 → {root}")
    notify("🎵 音乐已入库", f"{name[:56]}\n{n}个文件 · {lrc}首歌词 · {cov}张封面,Navidrome可听")
    return root, n

_AUDIO_EXT = (".flac", ".mp3", ".ape", ".wav", ".m4a", ".ogg", ".wv", ".dsf", ".dff")
_COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "front.jpg", "Cover.jpg")
_ALBUMS = {"ts": 0, "d": []}
def music_albums(force=False):
    """扫音乐库目录 → 专辑卡列表(封面/专辑名/歌手/歌曲数)。
    比按种子列强得多:一张专辑一张卡、有真封面、多版本自动合并。缓存10分钟。"""
    if not force and _ALBUMS["d"] and time.time() - _ALBUMS["ts"] < 600:
        return _ALBUMS["d"]
    root = CFG["MEDIA_MUSIC"]
    out = []
    if os.path.isdir(root):
        for cur, dirs, fs in os.walk(root):
            songs = [f for f in fs if os.path.splitext(f)[1].lower() in _AUDIO_EXT]
            if not songs: continue                       # 只认真正装歌的目录 = 一张专辑
            rel = os.path.relpath(cur, root)
            if rel == ".": continue
            parts = rel.split(os.sep)
            album = parts[-1]
            artist = parts[0] if len(parts) > 1 else ""
            # 目录名常见 "歌手 - 专辑" 形式,拆出来更好看
            m = re.match(r'^(.{1,30}?)\s*[-–]\s*(.+)$', album)
            if m and not artist: artist, album = m.group(1).strip(), m.group(2).strip()
            elif m and artist and artist in m.group(1): album = m.group(2).strip()
            cov = next((c for c in _COVER_NAMES if os.path.exists(os.path.join(cur, c))), "")
            out.append({"album": album, "artist": artist, "n": len(songs),
                        "cover": os.path.join(rel, cov) if cov else "",
                        "mtime": os.path.getmtime(cur)})
    out.sort(key=lambda x: -x["mtime"])
    _ALBUMS.update(ts=time.time(), d=out)
    return out

def _xesc(s): return (str(s or "")).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _tmdb_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": CFG["TMDB_PROXY"], "https": CFG["TMDB_PROXY"]})) if CFG["TMDB_PROXY"] else urllib.request.build_opener()

def tmdb_details(mtype, tid, season=None):
    try:
        if season is not None: return _tmdb_call(f"/tv/{tid}/season/{season}", language="zh-CN")
        return _tmdb_call(f"/{mtype}/{tid}", language="zh-CN")
    except Exception: return {}

def write_nfo(path, tag, fields, genres=(), raw=()):
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', f"<{tag}>"]
    for k, v in fields:
        if v not in (None, ""): xml.append(f"  <{k}>{_xesc(v)}</{k}>")
    for g in genres: xml.append(f"  <genre>{_xesc(g)}</genre>")
    for r in raw: xml.append("  " + r)
    xml.append(f"</{tag}>")
    open(path, "w", encoding="utf-8").write("\n".join(xml))
    try: os.chown(path, int(os.environ.get("PUID","1000")), int(os.environ.get("PGID","1001")))
    except Exception: pass

_SE_RE = re.compile(r'S(\d{1,2})\s?E(\d{1,3})', re.I)
_EP_RE = re.compile(r'(?:EP|E|第)(\d{1,3})(?=[.\s_\-集话話]|$)', re.I)

def _grab_img(op, path, dst, size):
    if not path or os.path.exists(dst): return
    try:
        img = op.open(f"https://image.tmdb.org/t/p/{size}{path}", timeout=25).read()
        if len(img) > 5000:
            open(dst, "wb").write(img)
            try: os.chown(dst, int(os.environ.get("PUID","1000")), int(os.environ.get("PGID","1001")))
            except Exception: pass
    except Exception: pass

def scrape_pack(dest, m):
    """自给自足刮削包：nfo+海报+背景+标准重命名+每集nfo,全落在本地。
    Emby/Jellyfin/Kodi/Infuse 谁来都拿来即用,不依赖播放器自己刮。硬链接改名不影响做种。"""
    mt = m["mtype"]; tid = m["id"]; title = _safe(m["tmdb_name"]); year = m.get("year", "")
    d = tmdb_details(mt, tid); op = _tmdb_opener()
    _grab_img(op, d.get("poster_path") or m.get("poster"), os.path.join(dest, "poster.jpg"), "w780")
    _grab_img(op, d.get("backdrop_path"), os.path.join(dest, "fanart.jpg"), "w1280")
    genres = [g.get("name","") for g in (d.get("genres") or [])][:6]
    plot = d.get("overview") or m.get("overview", "")
    rating = round(d.get("vote_average") or 0, 1) or ""
    uid = f'<uniqueid type="tmdb" default="true">{tid}</uniqueid>'
    if mt == "tv":
        write_nfo(os.path.join(dest, "tvshow.nfo"), "tvshow",
                  [("title", d.get("name") or m["tmdb_name"]), ("plot", plot), ("year", year),
                   ("premiered", d.get("first_air_date","")), ("rating", rating), ("tmdbid", tid)], genres, (uid,))
        season_cache = {}
        # 剧集现在按 Season XX 分子目录存放,所以要递归遍历;
        # 老库里平铺在根目录的文件同样能扫到,两种结构都兼容。
        targets = []
        for dp, _dn, fns in os.walk(dest):
            for f in sorted(fns): targets.append((dp, f))
        for dp, f in targets:
            fp = os.path.join(dp, f)
            if not os.path.isfile(fp): continue
            stem, ext = os.path.splitext(f)
            is_video = ext.lower() in _VIDEO_EXT; is_sub = ext.lower() in (".srt",".ass",".sub")
            if not (is_video or is_sub): continue
            mm = _SE_RE.search(stem)
            if mm: ss, ee = int(mm.group(1)), int(mm.group(2))
            else:
                m2 = _EP_RE.search(stem)
                if not m2: continue
                # 文件名里没写季号:优先信它所在的 Season XX 目录,再退回第 1 季。
                # 直接假定第 1 季是老毛病 —— 番组命名(如「夏目友人帐 [01]」)全会被打成 S01。
                ss = _season_of(os.path.basename(dp)) or 1
                ee = int(m2.group(1))
            newstem = f"{title} - S{ss:02d}E{ee:02d}"
            newf = newstem + ext
            # 落点跟着文件走:文件在 Season 05/ 里,改名和 nfo 就都留在 Season 05/,
            # 不能写回剧集根目录,否则视频和它的 nfo 会分家
            if f != newf:
                np = os.path.join(dp, newf)
                if os.path.exists(np): newstem = stem
                else: os.rename(fp, np)
            if is_video:
                nfop = os.path.join(dp, newstem + ".nfo")
                if os.path.exists(nfop): continue
                if ss not in season_cache: season_cache[ss] = tmdb_details("tv", tid, season=ss)
                eps = {e.get("episode_number"): e for e in (season_cache[ss].get("episodes") or [])}
                e = eps.get(ee) or {}
                write_nfo(nfop, "episodedetails",
                          [("title", e.get("name") or f"第 {ee} 集"), ("season", ss), ("episode", ee),
                           ("plot", e.get("overview","")), ("aired", e.get("air_date",""))])
    else:
        write_nfo(os.path.join(dest, "movie.nfo"), "movie",
                  [("title", d.get("title") or m["tmdb_name"]), ("plot", plot), ("year", year),
                   ("premiered", d.get("release_date","")), ("rating", rating), ("tmdbid", tid)], genres, (uid,))
        if not os.path.isdir(os.path.join(dest, "BDMV")):   # 原盘不动
            vids = [f for f in os.listdir(dest) if os.path.isfile(os.path.join(dest, f))
                    and os.path.splitext(f)[1].lower() in _VIDEO_EXT]
            if len(vids) == 1:
                ext = os.path.splitext(vids[0])[1]
                nn = f"{title} ({year}){ext}" if year else f"{title}{ext}"
                if vids[0] != nn and not os.path.exists(os.path.join(dest, nn)):
                    os.rename(os.path.join(dest, vids[0]), os.path.join(dest, nn))

def emby_pin(dest, tid, name, mtype):
    """入库后把 Emby 条目身份钉死(正确tmdbid+锁名)。
    Emby 的 NFO 读取和自动刮削都不可靠,干脆由流水线直接写入身份——它只负责播放。"""
    if not (CFG["EMBY_URL"] and CFG["EMBY_KEY"] and tid): return
    try:
        E = CFG["EMBY_URL"].rstrip("/") + "/emby"; k = CFG["EMBY_KEY"]
        def _g(p): return json.load(urllib.request.urlopen(E+p+("&" if "?" in p else "?")+"api_key="+k, timeout=20))
        def _p(p, body):
            req = urllib.request.Request(E+p+("&" if "?" in p else "?")+"api_key="+k,
                data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=30)
        it = None
        for _ in range(6):                     # 最多等6分钟让 Emby 扫到新条目
            time.sleep(60)
            t_ = "Series" if mtype == "tv" else "Movie"
            d = _g(f"/Items?IncludeItemTypes={t_}&Recursive=true&Fields=Path,ProviderIds")
            it = next((x for x in d.get("Items", []) if (x.get("Path") or "") == dest
                       or (x.get("Path") or "").startswith(dest + "/")), None)
            if it: break
        if not it:
            logmsg("WARN", f"Emby钉身份: 6分钟没扫到 {name},下次刷新自愈"); return
        if str((it.get("ProviderIds") or {}).get("Tmdb", "")) == str(tid) and it.get("Name") == name:
            return                              # 已正确,不折腾
        uid = _g("/Users?")[0]["Id"]
        dto = _g(f"/Users/{uid}/Items/{it['Id']}?")
        dto["Name"] = name; dto["ForcedSortName"] = name
        dto["ProviderIds"] = {"Tmdb": str(tid)}
        dto["LockedFields"] = ["Name"]
        _p(f"/Items/{it['Id']}", dto)
        # 千万不能 ReplaceAllMetadata=true——那会清掉刚写的身份重新瞎识别(血泪教训)
        _p(f"/Items/{it['Id']}/Refresh?Recursive=true&MetadataRefreshMode=FullRefresh&ImageRefreshMode=Default&ReplaceAllMetadata=false&ReplaceAllImages=false", {})
        logmsg("INFO", f"📌 Emby 身份已钉死: {name} (tmdb {tid})")
    except Exception as e:
        logmsg("WARN", f"Emby钉身份失败 {name[:20]}: {str(e)[:40]}")

def emby_refresh():
    if not (CFG["EMBY_URL"] and CFG["EMBY_KEY"]): return
    try:
        req = urllib.request.Request(CFG["EMBY_URL"].rstrip("/") + "/emby/Library/Refresh?api_key=" + CFG["EMBY_KEY"], method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        logmsg("WARN", f"通知 Emby 刷新失败: {e}")



# ============ 全自动流水线：qb完成 → 识别整理入库 → 转种tr → 通知Emby ============
def do_organize(ih, name, files, m, cat):
    """执行硬链接入库并记录。files: [(绝对源路径, 相对路径)]"""
    c = db()
    c.execute("INSERT OR REPLACE INTO media(info_hash,name,cat,mtype,tmdbid,tmdb_name,year,target,conf,status,files,ts,poster) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (ih, name, cat, m["mtype"], m["id"], m["tmdb_name"], m["year"], "", m["conf"], "processing", len(files), int(time.time()), m.get("poster") or ""))
    c.commit(); c.close()
    dest, n = organize_files(files, m, cat, name_hint=name)
    try:
        scrape_pack(dest, m)      # 自给自足刮削包: nfo+图+标准命名,不依赖播放器刮削
    except Exception as e:
        logmsg("WARN", f"刮削包生成失败 {name[:24]}: {str(e)[:40]}")
    c = db(); c.execute("UPDATE media SET target=?, files=?, status='done' WHERE info_hash=?", (dest, n, ih)); c.commit(); c.close()
    logmsg("INFO", f"入库 {m['tmdb_name']} ({m['year']}) ← {name[:36]} | {n}个文件 → {dest}")
    emby_refresh()
    threading.Thread(target=emby_pin, args=(dest, m["id"], m["tmdb_name"], m["mtype"]), daemon=True).start()
    pic = poster_url(m.get("poster") or "")
    if pic:
        notify_news([{"title": f"📥 已入库 · {m['tmdb_name']} ({m['year']})",
                      "description": f"{cat} · {n}个文件,Emby可看",
                      "url": CFG["PUBLIC_URL"] or "https://seed.leesy.cc", "picurl": pic}])
    else:
        notify(f"📥 已入库 · {m['tmdb_name']} ({m['year']})", f"{cat} · {n}个文件,Emby可看\n{name[:56]}")
    return dest, n

def hold_media(ih, name, cat, reason):
    c = db()
    c.execute("INSERT OR REPLACE INTO media(info_hash,name,cat,mtype,tmdbid,tmdb_name,year,target,conf,status,files,ts,poster) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (ih, name, cat, "", None, "", "", "", "", "hold", 0, int(time.time()), ""))
    c.commit(); c.close()
    logmsg("WARN", f"整理待确认({reason}): {name[:44]}")
    notify("⚠️ 入库待确认", f"{name[:56]}\n{reason},去面板『整理入库』填片名或TMDB id一键入库")

# ⚠️ 名词澄清 —— 观澜里有**三件不同的事**都被叫过「转种」,这是概念混乱最大的一处来源:
#
#   交棒  handoff    qb 下完 → tr 接手长期做种。同一份数据换个客户端,**站点不变、不跨站**。
#                    PT 规矩是一个种子只能在一个客户端做种,所以 tr 接手后必须从 qb 删任务
#                    (留数据)。**不受禁转标记约束** —— 压根没换站,谈不上转。就是本函数。
#   转发种 repost    把 A 站下到的资源发布到 B 站。**这才是 PT 圈说的「转种」**,
#                    受禁转红线硬拦截(在 xfer_pack 里拦),转了要被请喝茶。
#   辅种  crossseed  同一份数据别站已有种子,把那个站的 tracker 挂上去一起做种。
#                    不产生新发布,不算转发。crossseed_one 干的事。
#
# 三者的风险等级完全不同,合在一个词里迟早出事。界面文案也照这个分。
XFER_FAIL_LIMIT = 5
def transfer_to_tr(qb, ih, name, save_path, cid=""):
    """【交棒】tr 指向同一数据目录，校验后从 qb 删任务(数据保留)。
       失败会记账(content.xfer_fail),面板看得见,下一轮自动重试 ——
       老版失败只写一行日志就算了,而且因为 media 表已有记录,下一轮压根不会再试。"""
    err = ""
    try:
        data = qb.export(ih)
        if data[:1] != b'd':
            err = "qb 导出的不是种子文件"
            logmsg("WARN", f"交棒失败(导出非种子) {name[:36]}")
        else:
            tr = tr_conn(); resp = tr.add(data, save_path); args = resp.get("arguments", {})
            added = args.get("torrent-added") or args.get("torrent-duplicate")
            if added:
                if added.get("id"):
                    try: tr.call("torrent-verify", {"ids": [added["id"]]})
                    except Exception: pass
                qb.delete(ih, delete_files=False)
                logmsg("INFO", f"交棒 qb→tr 完成(数据保留): {name[:40]}")
                if cid:
                    led_bind(ih, cid, "tr", "", save_path); led_xfer_ok(cid)
                return True
            err = f"tr 拒绝: {resp.get('result')}"
            logmsg("WARN", f"交棒被 tr 拒绝 {name[:36]}: {resp.get('result')}")
    except Exception as e:
        err = str(e)[:70]
        logmsg("ERROR", f"交棒异常 {name[:30]}: {e}")
    if cid: led_xfer_fail(cid, err)
    return False

def _qb_identify(qb, t, files=None):
    """给一个 qb 种子定身份并登记。先查账,查不到才去拉文件列表(每分钟扫一遍,别浪费请求)。"""
    ih = t["hash"]; name = t["name"]; sp = t.get("save_path", "")
    cid = led_cid(ih)
    if cid and files is None:
        return cid, None
    if files is None:
        files = qb.files(ih)
    man = manifest_qb(files, name)
    cid = content_id(man)
    led_touch(cid, name, t.get("size") or t.get("total_size") or 0, len(man))
    led_bind(ih, cid, "qb", "", sp)
    return cid, files

def organize_step(qb, t, cid, files):
    """【入库】识别 + 硬链接进媒体库 + 刮削。与交棒完全独立 —— 入库失败不该拖累做种。"""
    ih = t["hash"]; name = t["name"]; sp = t["save_path"]
    paths = [(os.path.join(sp, f["name"]), f["name"]) for f in files]
    if t.get("category") == "音乐" or meta_is_music(name):
        try: organize_music(ih, name, paths)
        except Exception as e: logmsg("ERROR", f"音乐入库异常 {name[:30]}: {e}")
        return
    # 下载后的文件名是最可靠的证据。像 E01…E32 这种整季资源，即使种子顶层
    # 文件夹曾被错命名为某部电影，也必须只在 TMDB 的电视剧结果里匹配。
    m = tmdb_match(name, force_tv=files_indicate_tv(paths))
    cat = media_category(name, m)
    if m and m["conf"] in ("high", "mid"):
        try:
            do_organize(ih, name, paths, m, cat)
            c = db(); c.execute("UPDATE media SET cid=? WHERE info_hash=?", (cid, ih)); c.commit(); c.close()
        except Exception as e:
            logmsg("ERROR", f"入库异常 {name[:30]}: {e}")
            c = db(); c.execute("UPDATE media SET status='error' WHERE info_hash=?", (ih,)); c.commit(); c.close()
    else:
        hold_media(ih, name, cat, "识别置信度不足" if m else "TMDB无匹配")

def handoff_step(qb, t, cid):
    """【交棒】qb→tr。做完顺带触发预存辅种。返回是否成功。"""
    ih = t["hash"]; name = t["name"]; sp = t["save_path"]
    if not transfer_to_tr(qb, ih, name, sp, cid=cid):
        return False
    c = db(); row = c.execute("SELECT data FROM pending_seed WHERE name=?", (name,)).fetchone(); c.close()
    if row:
        try:
            threading.Thread(target=_preseed, args=(ih, name, json.loads(row[0])), daemon=True).start()
        except Exception: pass
    return True

_QB_SETTLED = set()      # 本进程内已确认「入库和交棒都无事可做」的 qb 种子,跳过以免每分钟空查
def process_completed(qb, t):
    """qb 一个种子下完之后的处理。老版把入库和交棒串成一条路,任何一步的记录存在就整个跳过 ——
       结果是交棒失败以后**永远不会重试**(media 表已有记录),而且面板上看不出来。
       现在两件事各自判断、各自记账、各自重试。"""
    ih = t["hash"]; name = t["name"]
    if ih in _QB_SETTLED: return
    is_keep = "keepseed" in (t.get("tags") or "")
    try:
        cid, files = _qb_identify(qb, t)
    except Exception as e:
        logmsg("ERROR", f"取qb文件列表失败 {name[:30]}: {e}"); return
    led_role(cid, "stock" if is_keep else "library")
    # ① 入库:只有媒体库资产才做,保种库存不刮削不入库
    need_org = False
    if not is_keep and CFG["ORGANIZE"] and CFG["TMDB_KEY"]:
        c = db(); row = c.execute("SELECT status FROM media WHERE info_hash=?", (ih,)).fetchone(); c.close()
        need_org = not row                     # hold/error/done 都不重复自动入库
    if need_org:
        if files is None:
            try: files = qb.files(ih)
            except Exception as e:
                logmsg("ERROR", f"取qb文件列表失败 {name[:30]}: {e}"); files = []
        if files:
            logmsg("INFO", f"qb 下载完成，整理入库: {name[:44]}")
            organize_step(qb, t, cid, files)
    # ② 交棒:所有种子都要做。失败留在账上,下一轮自己重试,不用人管
    if not led_has_tr(cid):
        g = led_get(cid) or {}
        if (g.get("xfer_fail") or 0) >= XFER_FAIL_LIMIT:
            _QB_SETTLED.add(ih)                # 连败到上限,等人处理,别每分钟重试
            return
        if handoff_step(qb, t, cid) and is_keep:
            base = name.rsplit(".", 1)[0] if "." in name[-6:] else name
            c = db()
            c.execute("UPDATE keepseed SET status='done', cid=? WHERE status='pushed' AND name IN (?,?)",
                      (cid, name, base))
            c.commit(); c.close()
            logmsg("INFO", f"保种完成→tr: {name[:44]}")
    elif not need_org:
        _QB_SETTLED.add(ih)                    # 入库和交棒都无事可做,这个种子不用再看了

def manual_organize(ih, query):
    """待确认条目：用户给 TMDB id 或片名，重新匹配并入库。数据可能已转到 tr。"""
    name = sp = None; files = []
    try:
        tr = tr_conn()
        t = next((x for x in tr.torrents() if x["hashString"].lower() == ih.lower()), None)
        if t:
            name = t["name"]; sp = t["downloadDir"]
            files = [(os.path.join(sp, f["name"]), f["name"]) for f in t.get("files", [])]
    except Exception: pass
    if not files:
        try:
            qb = qb_conn()
            t = next((x for x in qb.torrents() if x["hash"].lower() == ih.lower()), None)
            if t:
                name = t["name"]; sp = t["save_path"]
                files = [(os.path.join(sp, f["name"]), f["name"]) for f in qb.files(ih)]
        except Exception: pass
    if not files:
        return {"ok": False, "err": "qb/tr 里都找不到该种子的文件"}
    force_tv = files_indicate_tv(files)
    m = None
    # 支持显式指定类型: movie/79064 或 tv/79064(TMDB 的 id 在剧/影里是两套独立编号!
    # 同一个 79064,tv 是《富贵男》、movie 才是《手机》,不能撞到哪个算哪个)
    want = ""
    mp = re.match(r'^(movie|tv|电影|剧集|电视剧)\s*[/:：\s]\s*(\d+)$', query, re.I)
    if mp:
        want = "tv" if mp.group(1).lower() in ("tv", "剧集", "电视剧") else "movie"
        query = mp.group(2)
    if query.isdigit():
        cands = []
        # 多集文件与手动填的数字 ID 冲突时，信文件清单；不能再把 E01-E32 送到电影库。
        for mt in (["tv"] if force_tv else (["tv", "movie"] if not want else [want])):
            try:
                d = _tmdb_call(f"/{mt}/{query}", language="zh-CN")
                if d.get("id"):
                    cands.append({"mtype": mt, "id": d["id"], "tmdb_name": d.get("name") or d.get("title"),
                                  "orig": d.get("original_name") or d.get("original_title") or "",
                                  "year": (d.get("first_air_date") or d.get("release_date") or "")[:4],
                                  "poster": d.get("poster_path") or "",   # 别漏!漏了海报墙和刮削包的 poster.jpg 都是空的
                                  "overview": d.get("overview") or "",
                                  "conf": "manual", "q": query})
            except Exception: continue
        if cands:
            low = re.sub(r'[^a-z0-9一-鿿]', '', (name or "").lower())
            hint = "tv" if force_tv or meta_is_tv(name or "") else "movie"
            def score(x):
                s = 0
                for t in (x.get("tmdb_name"), x.get("orig")):
                    tt = re.sub(r'[^a-z0-9一-鿿]', '', (t or "").lower())
                    if tt and tt in low: s += 3          # 片名出现在种子名里 = 强证据
                if x.get("year") and x["year"] in (name or ""): s += 2   # 年份对得上
                if x["mtype"] == hint: s += 1            # 种子名像剧/像影
                return s
            m = max(cands, key=score)
    else:                 # 否则当片名搜
        cand = _tmdb_search(query, tv_only=force_tv)
        if force_tv and any(r.get("media_type") == "tv" for r in cand):
            cand = [r for r in cand if r.get("media_type") == "tv"]
        if cand:
            r = cand[0]
            m = {"mtype": "tv" if r.get("media_type") == "tv" else "movie", "id": r.get("id"),
                 "tmdb_name": r.get("name") or r.get("title"), "year": _ryear(r),
                 "poster": r.get("poster_path") or "", "overview": r.get("overview") or "",
                 "conf": "manual", "q": query}
    if not m:
        return {"ok": False, "err": "TMDB 查不到，试试直接填 TMDB id"}
    # 改识别前记下旧目标:入库过的条目改对之后,旧的错误目录要清掉,否则 Emby 里两条并存
    old = None
    try:
        c = db(); row = c.execute("SELECT target,status FROM media WHERE info_hash=?", (ih,)).fetchone(); c.close()
        if row and row[1] == "done": old = row[0]
    except Exception: pass
    cat = media_category(name or "", m)
    dest, n = do_organize(ih, name or "", files, m, cat)
    cleaned = _drop_old_media_dir(old, dest)
    r = {"ok": True, "name": f"{m['tmdb_name']} ({m['year']})", "n": n}
    if cleaned: r["cleaned"] = cleaned
    return r

def _drop_old_media_dir(old, new):
    """改识别后删掉旧的错误媒体目录。只删媒体库根目录之下的子目录,且里面全是硬链接——
    真实数据在下载目录由 tr/qb 持有,删这份副本不丢数据(硬链接减引用而已)。"""
    if not old or not new or os.path.realpath(old) == os.path.realpath(new): return ""
    roots = [os.path.realpath(p) for p in (CFG["MEDIA_TV"], CFG["MEDIA_MOVIE"],
                                           CFG["MEDIA_ANIME"], CFG["MEDIA_MUSIC"]) if p]
    try:
        rp = os.path.realpath(old)
        if not os.path.isdir(rp): return ""
        if rp in roots: return ""                                  # 绝不删库根
        if not any(rp.startswith(r + os.sep) for r in roots): return ""   # 必须在库内
        import shutil as _sh
        _sh.rmtree(rp)
        logmsg("INFO", f"改识别:已清理旧目录 {os.path.basename(rp)}(硬链接副本,源数据仍在下载目录)")
        return os.path.basename(rp)
    except Exception as e:
        logmsg("WARN", f"清理旧目录失败 {str(e)[:50]}")
        return ""

def _preseed(ih, name, mates):
    """下载时预存的同组候选辅种：不搜索,直接拿已知站点的种子来比对注入"""
    try:
        tr = tr_conn(); t = None
        for _ in range(20):                 # 最多等10分钟: 源必须校验到100%,否则注入的全是残种
            time.sleep(30)
            t = next((x for x in tr.torrents() if x["hashString"].lower() == ih.lower()), None)
            if t and t.get("percentDone", 0) >= 1: break
        if not t:
            logmsg("WARN", f"预存辅种: tr里没找到 {name[:32]}"); return
        if t.get("percentDone", 0) < 1:
            logmsg("WARN", f"预存辅种取消: 源校验只有{round(t.get('percentDone',0)*100,1)}%,数据有问题,不注入垃圾 {name[:30]}")
            return
        cands = [{"downloadUrl": m.get("url",""), "size": m.get("size",0), "indexer": m.get("site","")} for m in mates]
        logmsg("INFO", f"⚡ 预存辅种开跑(下载时已知 {len(cands)} 个站): {name[:36]}")
        # 预存候选只是第一轮快速比对；没有命中时仍须以「英文剧名+季号」
        # 精确重搜。此前传空列表会跳过这一轮，导致 S04 等内容只能靠手动重搜。
        run_match(tr, t, cross_seed_queries(name), pre_results=cands)
    except Exception as e:
        logmsg("ERROR", f"预存辅种异常 {name[:28]}: {e}")
    finally:
        try:
            c = db(); c.execute("DELETE FROM pending_seed WHERE name=?", (name,)); c.commit(); c.close()
        except Exception: pass

def qb_watcher():
    """每分钟看一眼 qb：有下载完成的就整理+转种。比 MP 的定时插件快，自然接管。"""
    time.sleep(20)
    while True:
        try:
            qb = qb_conn()
            tl = qb.torrents()
            health_check("qb", True)
            for t in tl:
                if t.get("progress", 0) < 1: continue
                # 老版在这里用 media 表有没有记录来决定跳不跳过,把入库和交棒绑死了:
                # 入库过一次(哪怕是 hold),交棒失败也再没人管。现在交给 process_completed 分别判断。
                process_completed(qb, t)
        except Exception as e:
            health_check("qb", False)
            logmsg("ERROR", f"qb监控异常: {e}")
        time.sleep(60)

# ============ Prowlarr ============
def prowlarr_search(query, cats=None):
    """聚合接口:一次问全部站点,**等到最慢的站也回来为止**(timeout=150)。

    ⚠️ 辅种专用,别拿 prowlarr_search_fan 替换它。两条路的目标是相反的:
       · 搜索(交互) → 用 fan 版:动态收网、丢慢站、按类型筛站、只等主力站。
                      人在等结果,少而精、快才是对的。
       · 辅种(后台) → 用本函数:不筛类型、不丢任何站、慢站也等。
                      **覆盖面就是种子的存活率** —— 少辅一个站就少一份保活,
                      而且后台跑没人等,慢一点毫无代价。
    实测:辅种累计覆盖 50 个站,单个内容平均辅到 18 个站(大明王朝1566 达 42 站)。
    """
    u = CFG["PROWLARR_URL"] + "/api/v1/search?query=" + urllib.parse.quote(query) + "&type=search"
    for c in (cats or []):          # Torznab 分类: 2000影 5000剧 5070动漫 7030漫画 3000音乐
        u += "&categories=" + str(c)
    req = urllib.request.Request(u, headers={"X-Api-Key":CFG["PROWLARR_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=150))
def prowlarr_download(url):
    req = urllib.request.Request(url, headers={"X-Api-Key":CFG["PROWLARR_KEY"]})
    return urllib.request.urlopen(req, timeout=25).read()

# ============ §9 策略:四条线各自怎么干(是数据,不是代码) ============
# 同一套零件(身份/账本/解析/打分/取数),四种组装方式。这些差异过去散在各函数体里 ——
# 加一个场景就得在三处塞 if,改漏一处就出怪事。现在改一行策略就够。
POLICY = {
    # 找片:人在屏幕前等,少而精,有硬截止,丢慢站无所谓(慢站的种大站基本都有)
    "find":      {"timeout": 9,  "deadline": 22,  "workers": 96, "intent": "browse", "delay": 0},
    # 辅种:一个站都不能漏 —— 所以不设短截止、慢站也等;但压低并发,
    #      别为了一份内容把 Prowlarr 打满(它是后台批量跑的,不是一次性动作)
    "crossseed": {"timeout": 20, "deadline": 300, "workers": 8,  "intent": "",       "delay": 2},
    # 批量收:无人值守,一部片只要一个好种,只问几个大站,覆盖面交给后台辅种
    "harvest":   {"timeout": 9,  "deadline": 25,  "workers": 32, "intent": "collect","delay": 0},
    # 保种:不走搜索,走站点列表页直连翻页(ks_browse),这里只放节流和配额
    "stock":     {"timeout": 30, "deadline": 0,   "workers": 1,  "intent": "",       "delay": 2},
}

# ============ §10 作业·认领:把一个搜索结果认领成「同一份内容的另一个实例」 ============
def _claim_one(tr, t, cid, local_set, r, top):
    """比对单个搜索结果并注入。返回 (matched, injected, 结果串)。
       认领的判据只有一个:文件清单完全相同。名字/大小都只是粗筛。"""
    total = t["totalSize"]
    if not r.get("downloadUrl") or abs(r.get("size", 0) - total) >= total * CFG["SIZE_TOLERANCE"]:
        return 0, 0, ""
    time.sleep(CFG["SNATCH_DELAY"])
    try:
        data = prowlarr_download(r["downloadUrl"])
        if data[:1] != b'd': return 0, 0, ""
        cname, cfiles = torrent_files(data)
        if set(cfiles.items()) != local_set: return 0, 0, ""     # 清单对不上,不是同一份内容
    except Exception:
        return 0, 0, ""
    ih = t["hashString"]; inj = 0; mode = "direct" if cname == top else "link"; res = "matched"
    try: chash = torrent_infohash(data)
    except Exception: chash = ""
    # 同 info_hash(多站挂同一个种子文件):绝不能 tr.add —— tr4 会用新 tracker 顶掉旧的,
    # 断了原站做种。正确做法是给现有种子追加该站 tracker,一个种子同时向多站汇报(IYUU式)。
    if chash and chash.lower() == ih.lower():
        try:
            res = tr_add_trackers(tr, ih, torrent_announces(data))
        except Exception as e:
            logmsg("WARN", f"加tracker失败: {str(e)[:40]}"); res = "duplicate"
        if res == "tracker": inj = 1
        return 1, inj, res
    if cname == top:
        dl_dir = t["downloadDir"]
    else:
        link_top = os.path.join(CFG["DATA_LINK_DIR"], cname); data_top = os.path.join(t["downloadDir"], top)
        for relp in cfiles:
            src = os.path.join(data_top, relp); dst = os.path.join(link_top, relp)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                try: os.link(src, dst)
                except OSError: pass
        dl_dir = CFG["DATA_LINK_DIR"]
    try:
        resp = tr.add(data, dl_dir); rr = resp.get("result"); args = resp.get("arguments", {})
        # tr 对"新增"和"内容已存在"都返回 result=success,靠 arguments 里的键区分
        if "torrent-duplicate" in args:
            res = "duplicate"; dup = args["torrent-duplicate"]
            try:
                if dup.get("id") is not None:
                    if tr_add_trackers(tr, dup["id"], torrent_announces(data)) == "tracker":
                        inj = 1; res = "tracker"
            except Exception as e:
                logmsg("WARN", f"加tracker失败: {str(e)[:40]}")
        elif "torrent-added" in args or rr == "success": inj = 1; res = "injected"
        else: res = "inject_fail:" + str(rr)
    except Exception:
        res = "inject_err"
    return 1, inj, res

def claim_batch(tr, t, cid, local_set, results, ban=None):
    """认领一批结果。返回 (matched, injected)。逐条记 matches 表,边辅边更新计数。"""
    ih = t["hashString"]; top = t["name"]
    ban = ban if ban is not None else [b.strip().lower() for b in CFG["TR_BAN_SITES"].split(",") if b.strip()]
    m = inj = 0
    for r in results:
        if any(b in (r.get("indexer") or "").lower() for b in ban):
            continue                      # 该站 ban 了 tr 客户端,注了也是废种
        try:
            mm, ii, res = _claim_one(tr, t, cid, local_set, r, top)
        except Exception:
            continue
        if not mm: continue
        m += mm; inj += ii
        if cid:                     # 认领成功 = 这个站确实有这份内容,记进覆盖账
            try:
                _um, _nm = site_urlmap()
                led_cov_set(cid, match_site(r.get("indexer") or "", _nm, _um), "seeding")
            except Exception: pass
        c = db()
        c.execute("INSERT INTO matches(info_hash,indexer,matched_name,mode,result,ts) VALUES(?,?,?,?,?,?)",
                  (ih, r.get("indexer"), (r.get("title") or "")[:120], "auto", res, int(time.time())))
        c.execute("UPDATE torrents SET status='injecting', matched=matched+1, injected=injected+? WHERE info_hash=?",
                  (ii, ih))
        c.commit(); c.close()
    return m, inj

# ============ §10 作业·辅种:目标是把这份内容的 coverage pending 消成空 ============
def _register(t, sites=None):
    """把一个 tr 种子登记进总账,返回 (cid, 文件清单, 来源站)。
       来源站必须归一化到 Prowlarr 的索引器名 —— 否则 'ttg' 和 'TTG' 是两个站,
       自己在做种的站会被年年重问一遍(实测踩到过,见 match_site 的注释)。"""
    man = manifest_tr(t)
    cid = content_id(man)
    urlmap, names = site_urlmap()
    sites = sites or names
    source = ""
    for tk in t.get("trackers", []):
        try: host = urllib.parse.urlparse(tk.get("announce", "")).hostname or ""
        except Exception: continue
        if not host: continue
        d = _reg_domain(host.lower())
        source = urlmap.get(d) or match_site(tracker_site(tk.get("announce", "")), sites, urlmap)
        if source: break
    led_touch(cid, t["name"], t.get("totalSize", 0), len(man))
    led_bind(t["hashString"], cid, "tr", source, t.get("downloadDir", ""))
    if source: led_cov_set(cid, source, "source")      # 本来就是从这个站下的,不用再问
    return cid, man, source

def run_match(tr, t, queries, manual=False, pre_results=None):
    """兼容入口:按给定关键词辅种一个种子(手动重搜 / 预存候选 走这里)。
       全站覆盖的自动辅种走 crossseed_one,别用这个 —— 这里不记 coverage。"""
    ih = t["hashString"]; name = t["name"]
    cid, man, source = _register(t)
    local_set = set(man.items())
    c = db()
    c.execute("INSERT OR IGNORE INTO torrents(info_hash,name,size,files,query,status,first_seen) VALUES(?,?,?,?,?,?,?)",
              (ih, name, t["totalSize"], len(man), " / ".join(queries), "searching", int(time.time())))
    c.execute("UPDATE torrents SET query=?, status=?, matched=0, injected=0, source=?, last_searched=?, cid=? WHERE info_hash=?",
              (" / ".join(queries), "searching", source or "?", int(time.time()), cid, ih))
    c.commit(); c.close()

    matched = injected = 0; had_result = False; used = ""
    if pre_results is not None:            # 下载时预存的同组候选,先直接比对注入
        m, inj = claim_batch(tr, t, cid, local_set, pre_results)
        matched, injected, had_result, used = m, inj, True, "下载时预存"
        if matched: queries = []           # 已命中,无须再搜索造成重复请求
    for q in queries:
        try:
            results = prowlarr_search(q); had_result = True
        except Exception as e:
            logmsg("WARN", f"搜索[{q}]失败 {name[:30]}: {e}"); continue
        m, inj = claim_batch(tr, t, cid, local_set, results)
        matched += m; injected += inj; used = q
        # 这里 break 是对的:手动重搜/预存候选求的是「快点辅上」,不是全站覆盖。
        # 全站覆盖走 crossseed_one,那边每个站独立记账,绝不会因为辅到一个就收工。
        if matched > 0: break
    if not had_result:
        set_status(ih, "search_error"); logmsg("ERROR", f"搜索全失败 {name[:40]}"); return
    c = db(); c.execute("UPDATE torrents SET status=?, matched=?, injected=? WHERE info_hash=?",
                        ("done" if matched else "no_match", matched, injected, ih)); c.commit(); c.close()
    logmsg("INFO", f"{'手动' if manual else ''}辅种 {name[:40]} | 命中[{used}] 匹配{matched} 注入{injected}")
    if injected > 0:
        notify(f"🌱 辅种 +{injected} 站", name[:56])

def crossseed_one(tr, t, log=lambda m: None):
    """全站覆盖辅种。与老版的三处关键区别:
       ① 只问账上还欠的站(coverage pending),不是每次全站重来
       ② **不再辅到一个站就 break** —— 每个站独立记账,认领与否互不影响
       ③ 逐站落账:seeding / absent(确实没有) / error(没问成),后两者绝不混为一谈"""
    name = t["name"]
    ban = [b.strip().lower() for b in CFG["TR_BAN_SITES"].split(",") if b.strip()]
    try:
        idx = prowlarr_indexers()
    except Exception as e:
        logmsg("WARN", f"辅种取站点列表失败: {str(e)[:40]}"); return 0, 0
    cid, man, source = _register(t, [i.get("name", "") for i in idx])
    local_set = set(man.items())
    # ban 的站直接落账,永远不问 —— 注了也是废种,不该年年占着 pending 位
    for i in idx:
        if any(b in (i.get("name") or "").lower() for b in ban):
            led_cov_set(cid, i["name"], "banned", "该站ban了tr客户端")
    todo = led_cov_pending(cid, [i["name"] for i in idx])
    if not todo:
        log(f"✅ {name[:32]} 各站都问过了,跳过"); return 0, 0
    todo_ids = [i["id"] for i in idx if i.get("name") in set(todo)]
    pol = POLICY["crossseed"]
    st = {}
    queries = cross_seed_queries(name)
    if not queries:
        logmsg("WARN", f"辅种取不出关键词,跳过: {name[:36]}"); return 0, 0
    results = prowlarr_search_fan(queries, log, per_timeout=pol["timeout"], deadline=pol["deadline"],
                                  only_ids=todo_ids, status=st, workers=pol["workers"])
    ih = t["hashString"]
    c = db()
    c.execute("INSERT OR IGNORE INTO torrents(info_hash,name,size,files,query,status,first_seen) VALUES(?,?,?,?,?,?,?)",
              (ih, name, t["totalSize"], len(man), " / ".join(queries), "searching", int(time.time())))
    c.execute("UPDATE torrents SET query=?, status='searching', matched=0, injected=0, source=?, last_searched=?, cid=? WHERE info_hash=?",
              (" / ".join(queries), source or "?", int(time.time()), cid, ih))
    c.commit(); c.close()
    # 按站分组认领 —— 这是能逐站记账的前提
    bysite = {}
    for r in results:
        bysite.setdefault(r.get("indexer") or "?", []).append(r)
    matched = injected = 0; nseed = nabsent = nerr = 0
    for site in todo:
        if st.get(site) != "ok":
            led_cov_set(cid, site, "error", (st.get(site) or "没回")[:40]); nerr += 1
            continue
        m, inj = claim_batch(tr, t, cid, local_set, bysite.get(site, []), ban=ban)
        matched += m; injected += inj
        if m:
            led_cov_set(cid, site, "seeding"); nseed += 1
        else:
            led_cov_set(cid, site, "absent"); nabsent += 1     # 问过了,这个站确实没有
    c = db(); c.execute("UPDATE torrents SET status=?, matched=?, injected=? WHERE info_hash=?",
                        ("done" if matched else "no_match", matched, injected, ih)); c.commit(); c.close()
    logmsg("INFO", f"辅种 {name[:36]} | 问{len(todo)}站 → 认领{nseed} 确认没有{nabsent} 没问成{nerr} | 注入{injected}")
    if injected > 0:
        notify(f"🌱 辅种 +{injected} 站", name[:56])
    return matched, injected

def cross_seed_one(tr, t):
    """老名字保留(别处还在调),行为已换成覆盖驱动。"""
    return crossseed_one(tr, t)

def manual_research(info_hash, custom_query):
    # 手动兜底：用户指定关键词重搜一个种子
    try:
        tr = tr_conn(); ts = tr.torrents()
        t = next((x for x in ts if x["hashString"] == info_hash), None)
        if not t:
            logmsg("WARN", f"手动重搜: 未找到种子 {info_hash[:12]}"); return
        logmsg("INFO", f"手动重搜 [{custom_query}] <- {t['name'][:40]}")
        run_match(tr, t, [custom_query.strip()], manual=True)
    except Exception as e:
        logmsg("ERROR", f"手动重搜异常: {e}")

def set_status(ih, st):
    c = db(); c.execute("UPDATE torrents SET status=? WHERE info_hash=?", (st, ih)); c.commit(); c.close()

# ============ §11 调度·总账回填 ============
_BACKFILL = {"running": False, "msg": "", "done": 0, "total": 0}
def backfill_ledger():
    """一次性回填:把 tr 里现有的种子全部登记进总账。**只算身份,不发一个搜索请求。**

       为什么需要:升级到覆盖驱动辅种之后,老库里几千个种子在新账上是一片空白。
       不回填的话,辅种调度得靠一轮 15 个慢慢啃,要一周才把账认全,期间面板上
       缺种报告基本是空的,人会以为功能坏了。

       分批拉:tr 的 torrent-get 不支持分页,一次要 4000 个种子的完整文件清单
       就是几十 MB 的 JSON。先用轻字段拿全部 hash,再每批 200 个拉明细。"""
    if _BACKFILL["running"]: return
    _BACKFILL.update(running=True, msg="开始回填…", done=0, total=0)
    try:
        tr = tr_conn()
        lite = tr.torrents(fields=["hashString", "downloadDir", "totalSize"])
        keep = (CFG["KEEP_DIR"] or "").rstrip("/")
        hs = [t["hashString"] for t in lite
              if t.get("totalSize", 0) > 0 and "cross-seed-links" not in t.get("downloadDir", "")]
        _BACKFILL["total"] = len(hs)
        urlmap, names = site_urlmap()
        n = 0
        for i in range(0, len(hs), 200):
            batch = hs[i:i+200]
            try:
                r = tr.call("torrent-get", {"ids": batch, "fields": TR.TFULL})
                for t in r.get("arguments", {}).get("torrents", []):
                    try:
                        cid, man, src = _register(t, names)
                        if _under(t.get("downloadDir", ""), keep):
                            led_role(cid, "stock")      # 保种库存:登记但不参与辅种覆盖
                        led_place(cid, "tr")
                        n += 1
                    except Exception:
                        continue
            except Exception as e:
                logmsg("WARN", f"回填第 {i//200+1} 批失败: {str(e)[:40]}")
            _BACKFILL.update(done=n, msg=f"回填中 {n}/{len(hs)}")
            time.sleep(0.3)
        _BACKFILL["msg"] = f"✅ 回填完成:{n} 份内容已入账"
        logmsg("INFO", f"总账回填完成: {n} 份内容")
    except Exception as e:
        _BACKFILL["msg"] = f"⚠️ 回填失败: {str(e)[:50]}"
        logmsg("ERROR", f"总账回填失败: {e}")
    finally:
        _BACKFILL["running"] = False

# ============ §11 调度·辅种轮次 ============
def scanner():
    """辅种调度。与老版三处不同:
       ① 轻量拉列表(不带 files),只对选中要辅的那几个再拉文件清单 —— 老版每轮把
          4000+ 个种子的完整文件列表全拉一遍,几十 MB JSON,大部分是白拉的。
       ② 优先级按「账上还欠几个站」排,不是先来后到;从没登记过的新内容优先。
       ③ 有预算。老版靠 6 小时冷却拍脑袋跳过整个种子,既拦不住重复问、又漏掉真正欠站的。"""
    time.sleep(5)
    tr = tr_conn()
    while True:
        try:
            lite = tr.torrents(fields=TR.TLITE)
            health_check("tr", True)
            try:
                sites = [i.get("name", "") for i in prowlarr_indexers()]
            except Exception as e:
                logmsg("WARN", f"辅种轮次取站点失败,本轮跳过: {str(e)[:40]}")
                time.sleep(CFG["SCAN_INTERVAL"]); continue
            cand = []; seen = set(); keep = (CFG["KEEP_DIR"] or "").rstrip("/")
            for t in lite:
                dd = t.get("downloadDir", "")
                if t.get("totalSize", 0) <= 0 or "cross-seed-links" in dd: continue
                if _under(dd, keep): continue                      # 保种目录:只做种,不辅种
                key = (t.get("name", ""), t.get("totalSize", 0))   # 同内容的多个副本只处理一次
                if key in seen: continue
                seen.add(key)
                cid = led_cid(t.get("hashString", ""))
                if not cid:
                    cand.append((10 ** 6, t))                      # 从没登记过 = 新内容,最优先
                else:
                    n = len(led_cov_pending(cid, sites))
                    if n: cand.append((n, t))                      # 欠得越多越优先
            cand.sort(key=lambda x: -x[0])
            budget = max(1, CFG["CROSSSEED_BUDGET"])
            todo = cand[:budget]
            logmsg("INFO", f"辅种轮次: tr {len(lite)} 个副本 → {len(seen)} 份内容,"
                           f"账上欠站的 {len(cand)} 份,本轮预算 {len(todo)} 份")
            for _, lt in todo:
                try:
                    full = tr.torrent(lt["hashString"])            # 到这一步才拉文件清单
                    if not full: continue
                    crossseed_one(tr, full)
                except Exception as e:
                    logmsg("ERROR", f"辅种异常 {lt.get('name','')[:30]}: {e}")
                time.sleep(8)   # 种子间隔，别打爆 Prowlarr
        except Exception as e:
            health_check("tr", False)
            logmsg("ERROR", f"辅种轮次异常: {e}")
        time.sleep(CFG["SCAN_INTERVAL"])

# ============ 网页仪表盘 ============
# ⚠️ PAGE 是三引号普通字符串,Python 会先解释一遍反斜杠转义 ——
#    模板里的 JS 禁止写反斜杠转义(例如给引号转义):求值后反斜杠会消失,变成裸引号
#    → JS 语法错误 → 整页白屏,而 ast.parse 对此毫无感觉。踩过两次。
#    要在 JS 字符串里嵌引号,一律走 data 属性:data-x="值" + this.dataset.x。
#    改完必须把求值后的 PAGE 里的 <script> 提出来过 node --check。
PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>观澜 Wavegazer</title><link rel="icon" href="/favicon.ico" type="image/svg+xml"><style>
:root{--ikb:#002FA7;--acc:#ffffff;--accL:#CFE0FF;--pop:#FFD400;--ok:#3ddc84;--warn:#ffd83d;--err:#ff8579;--fg:#fff;--sub:rgba(255,255,255,.78);--line:rgba(255,255,255,.24);--card:rgba(255,255,255,.17);--card2:rgba(255,255,255,.26)}
*{box-sizing:border-box}::selection{background:rgba(255,255,255,.3)}
body{margin:0;color:#fff;font:14px/1.55 -apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC','Microsoft YaHei',sans-serif;-webkit-font-smoothing:antialiased;
background:radial-gradient(1100px 520px at 85% -8%,rgba(255,255,255,.10),transparent 60%),linear-gradient(180deg,#0039c8 0%,#002FA7 38%,#001d77 100%);background-attachment:fixed;background-color:#002FA7}
.wrap{max-width:1140px;margin:0 auto;padding:30px 28px}
h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0 0 2px;text-shadow:0 2px 12px rgba(0,10,60,.35)}
.sub{color:var(--sub);font-size:13px;margin-bottom:22px}
.tabs{display:inline-flex;background:rgba(255,255,255,.12);backdrop-filter:blur(10px);padding:4px;border-radius:13px;gap:2px;margin-bottom:22px}
.tabbtn{padding:7px 18px;border-radius:10px;font-size:13px;font-weight:600;color:rgba(255,255,255,.75);text-decoration:none;transition:.18s;white-space:nowrap}
.tabbtn:hover{color:#fff}
.tabbtn.on{background:#fff;color:var(--ikb);box-shadow:0 2px 10px rgba(0,10,60,.35)}
.tab{display:none}.tab.active{display:block;animation:fade .25s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.card{background:var(--card);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:20px;padding:6px 0;margin-bottom:20px;overflow:hidden}
.card h2{font-size:15px;font-weight:600;margin:16px 20px 10px;letter-spacing:-.01em}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.stat{background:rgba(255,255,255,.94);border-radius:20px;padding:18px 20px;box-shadow:0 10px 30px rgba(0,10,60,.35)}
.stat .n{font-size:30px;font-weight:800;letter-spacing:-.02em;color:var(--ikb)}
.stat .l{color:rgba(0,30,110,.6);font-size:12px;margin-top:2px;font-weight:500}
table{width:100%;border-collapse:collapse}
th{color:var(--sub);font-weight:500;font-size:12px;text-align:left;padding:8px 20px;border-top:none}
td{text-align:left;padding:11px 20px;border-top:1px solid var(--line);font-size:13px}
tr:hover td{background:rgba(255,255,255,.05)}
.b{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap}
.done{background:rgba(61,220,132,.22);color:#8dffbd}.nomatch{background:rgba(255,216,61,.2);color:#ffe680}
.searching{background:rgba(255,255,255,.22);color:#fff}.err{background:rgba(255,133,121,.25);color:#ffc4bd}
.name{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name a{color:#fff;text-decoration:none}.name a:hover{color:var(--accL);text-decoration:underline}
a{color:var(--accL);text-decoration:none}
.src{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;background:rgba(255,255,255,.16);color:#fff;font-weight:500;white-space:nowrap}
.mut{color:var(--sub)}.r{text-align:right}
.rs{display:flex;gap:6px}
.rs input{background:rgba(255,255,255,.14);border:none;color:#fff;border-radius:9px;padding:6px 10px;font-size:12px;width:130px;outline:none}
.rs input::placeholder{color:rgba(255,255,255,.45)}
.rs input:focus{box-shadow:0 0 0 2.5px rgba(255,255,255,.5)}
.rs button{background:#fff;color:var(--ikb);border:0;border-radius:980px;padding:5px 13px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;transition:.15s}
.rs button:hover{transform:translateY(-1px)}
#toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(6px);background:#fff;color:var(--ikb);padding:12px 22px;border-radius:14px;font-size:13px;font-weight:700;opacity:0;transition:.3s cubic-bezier(.2,.8,.3,1);pointer-events:none;box-shadow:0 10px 34px rgba(0,10,60,.5)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.searchbar{display:flex;gap:12px;padding:2px 20px 12px}
.searchbar input{flex:1;background:rgba(255,255,255,.16);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.25);color:#fff;border-radius:16px;padding:16px 22px;font-size:16px;outline:none;transition:.18s}
.searchbar input:focus{box-shadow:0 0 0 3px rgba(255,255,255,.45);background:rgba(255,255,255,.22)}
.searchbar input::placeholder{color:rgba(255,255,255,.55)}
.searchbar button{background:#fff;color:var(--ikb);border:0;border-radius:16px;padding:0 36px;font-size:16px;font-weight:800;cursor:pointer;transition:.15s;box-shadow:0 6px 20px rgba(0,10,60,.35)}
.searchbar button:hover{transform:translateY(-1px)}.searchbar button:active{transform:scale(.97)}
.sname{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dlbtn{background:#fff;color:var(--ikb);border:0;border-radius:980px;padding:5px 16px;font-size:12px;font-weight:700;cursor:pointer;transition:.15s}
.dlbtn:hover{transform:translateY(-1px)}.dlbtn:disabled{opacity:.6;cursor:default;transform:none}
.fbar{display:flex;gap:8px;padding:0 20px 14px;flex-wrap:wrap;align-items:center}
.fpill{padding:6px 16px;border-radius:980px;background:rgba(255,255,255,.14);cursor:pointer;font-size:13px;font-weight:600;color:rgba(255,255,255,.8);user-select:none;transition:.18s}
.fpill:hover{color:#fff;background:rgba(255,255,255,.2)}
.fpill.on{background:#fff;color:var(--ikb)}
/* 找人是「换一种搜法」,不是筛选,所以独立于 fpill,免得被 activeF() 当成分类 */
.ppill{padding:6px 16px;border-radius:980px;background:rgba(255,212,0,.18);cursor:pointer;font-size:13px;font-weight:600;
 color:#ffe98a;user-select:none;transition:.18s;border:1px solid rgba(255,212,0,.42)}
.ppill:hover{background:rgba(255,212,0,.28);color:#fff}
.ppill.on{background:var(--pop,#FFD400);color:#4a3400;border-color:transparent}
.pgrp{display:flex;align-items:baseline;gap:10px;padding:16px 16px 8px;font-size:15px;font-weight:700}
.pgrp .c{font-size:12px;font-weight:600;color:var(--sub)}
.perscard{display:flex;gap:14px;align-items:center;padding:12px 14px;border-radius:16px;background:var(--card2);
 cursor:pointer;margin:8px 0;transition:.18s;border:1px solid transparent}
.perscard:hover{background:rgba(255,255,255,.24);border-color:rgba(255,255,255,.4);transform:translateY(-2px)}
/* img 是替换元素,给它 display:flex 会把图渲染没(图其实加载成功),所以图和占位符分开写 */
.perscard img,.perscard .np{width:54px;height:54px;border-radius:50%;flex:none;background:var(--card2)}
.perscard img{display:block;object-fit:cover}
.perscard .np{display:flex;align-items:center;justify-content:center;font-size:24px}
.phero{display:flex;gap:16px;align-items:center;padding:4px 2px 12px}
.phero img,.phero .np{width:76px;height:76px;border-radius:50%;flex:none;background:var(--card2)}
.phero img{display:block;object-fit:cover}
.phero .np{display:flex;align-items:center;justify-content:center;font-size:32px}
.pcard .prole{font-size:11px;color:var(--sub);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.morebtn{margin:10px 16px;padding:7px 16px;border-radius:980px;background:rgba(255,255,255,.16);border:none;color:#fff;
 cursor:pointer;font-size:12px;font-weight:600;font-family:inherit}
.morebtn:hover{background:rgba(255,255,255,.26)}
/* 选种弹窗:片单页不被替换,关掉就回到原位,可以连着点下一部 */
#wk-ov{position:fixed;inset:0;background:rgba(0,18,70,.6);backdrop-filter:blur(8px);z-index:60;display:flex;
 align-items:center;justify-content:center;padding:20px;opacity:0;pointer-events:none;transition:.22s}
#wk-ov.show{opacity:1;pointer-events:auto}
#wk-box{width:min(940px,96vw);max-height:88vh;display:flex;flex-direction:column;overflow:hidden;
 background:rgba(255,255,255,.17);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.3);
 border-radius:22px;box-shadow:0 30px 80px rgba(0,10,60,.6);transform:translateY(12px) scale(.985);
 transition:.22s cubic-bezier(.2,.8,.3,1)}
#wk-ov.show #wk-box{transform:none}
#wk-head{display:flex;gap:14px;align-items:center;padding:15px 18px;border-bottom:1px solid var(--line);flex:none}
#wk-head img{width:46px;aspect-ratio:2/3;object-fit:cover;border-radius:8px;flex:none;display:block}
#wk-head .ph{width:46px;aspect-ratio:2/3;border-radius:8px;flex:none;background:var(--card2);display:flex;
 align-items:center;justify-content:center;font-size:20px}
#wk-ttl{font-size:16px;font-weight:800}
#wk-sub{font-size:12px;color:var(--sub);margin-top:2px}
#wk-body{overflow-y:auto;padding:2px 4px 14px}
#wk-body table{margin-top:0}
.wkx{margin-left:auto;flex:none;width:32px;height:32px;border-radius:50%;border:none;cursor:pointer;font-size:17px;
 background:rgba(255,255,255,.18);color:#fff;font-family:inherit;line-height:1}
.wkx:hover{background:rgba(255,255,255,.32)}
.wkbar{height:6px;border-radius:99px;background:rgba(255,255,255,.2);overflow:hidden;margin:12px 0 8px}
.wkbar i{display:block;height:100%;width:0;background:#FFD400;transition:width .45s cubic-bezier(.3,.8,.4,1)}
.wkpad{padding:16px 18px}
.wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(136px,1fr));gap:18px;padding:16px 20px}
.pcard{cursor:pointer;border-radius:14px;transition:.22s cubic-bezier(.2,.8,.3,1)}
.pcard{transition:transform .45s cubic-bezier(.22,.9,.32,1),opacity .45s ease}
.wall:hover .pcard{opacity:.82}
.wall .pcard:hover{opacity:1;transform:translateY(-5px) scale(1.045);z-index:2;position:relative}
.pcard.sel .pw,.pcard.sel .ph{box-shadow:0 0 0 3px #fff,0 12px 32px rgba(0,10,60,.6)}
.pcard.anchor .pw,.pcard.anchor .ph{outline:2px solid #FFD400;outline-offset:-2px}
.hitbadge{position:absolute;right:7px;top:7px;background:#FFD400;color:#3a2a00;font-size:11px;font-weight:800;
border-radius:980px;padding:3px 9px;box-shadow:0 4px 14px rgba(0,10,60,.45);letter-spacing:.02em}
.ownbadge{position:absolute;left:7px;top:7px;background:rgba(61,220,132,.94);color:#00351a;font-size:11px;font-weight:800;
border-radius:980px;padding:3px 9px;box-shadow:0 4px 14px rgba(0,10,60,.45);letter-spacing:.02em}
/* ---- 豆瓣片单货架:横滚一排海报,点一张就去全站找源 ---- */
.dbtabs{display:flex;gap:8px;padding:2px 16px 12px;overflow-x:auto;-webkit-overflow-scrolling:touch}
.dbtabs::-webkit-scrollbar{display:none}
.dbtab{flex:none;padding:5px 14px;border-radius:980px;background:rgba(255,255,255,.14);cursor:pointer;
 font-size:12.5px;font-weight:600;color:rgba(255,255,255,.8);user-select:none;transition:.18s;white-space:nowrap}
.dbtab:hover{color:#fff;background:rgba(255,255,255,.22)}
.dbtab.on{background:#fff;color:var(--ikb)}
.dbflow{overflow-x:auto;overflow-y:hidden;padding-bottom:12px}
.dbtrack{display:flex;gap:14px;padding:0 16px 4px;width:max-content;min-height:40px;align-items:flex-start}
.dbitem{width:118px;flex:none;cursor:pointer;transition:transform .24s cubic-bezier(.2,.8,.3,1)}
.dbitem:hover{transform:translateY(-5px)}
.dbitem .pw{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:12px;display:block;
 background:var(--card2);box-shadow:0 6px 18px rgba(0,10,60,.45)}
.dbitem .ph{width:100%;aspect-ratio:2/3;border-radius:12px;background:var(--card2);
 display:flex;align-items:center;justify-content:center;font-size:32px;box-shadow:0 6px 18px rgba(0,10,60,.45)}
.dbrate{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.62);color:#FFD400;font-size:11px;
 font-weight:800;padding:1px 8px;border-radius:980px}
.dbsrc{font-size:11px;font-weight:400;color:var(--sub);margin-left:6px}
.cachetip{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 16px 10px;padding:8px 14px;
 border-radius:12px;background:rgba(255,212,0,.14);border:1px solid rgba(255,212,0,.4);font-size:12.5px;color:var(--sub)}
.cachetip button{padding:3px 12px;border-radius:980px;border:none;cursor:pointer;font-family:inherit;
 font-size:12px;font-weight:600;background:rgba(255,255,255,.2);color:#fff}
.cachetip button:hover{background:rgba(255,255,255,.34)}
.pcard.owned .pw,.pcard.owned .ph{outline:2px solid rgba(61,220,132,.75);outline-offset:-2px}
.libbar{display:flex;gap:10px;align-items:center;padding:4px 20px 12px;flex-wrap:wrap}
.libbar input{flex:1;min-width:180px;background:rgba(255,255,255,.14);border:none;color:#fff;border-radius:12px;padding:10px 15px;font-size:13.5px;outline:none}
.libbar input:focus{box-shadow:0 0 0 2.5px rgba(255,255,255,.5)}
.libbar input::placeholder{color:rgba(255,255,255,.62)}
.rfix{position:absolute;top:7px;right:7px;z-index:3;width:26px;height:26px;padding:0;cursor:pointer;
display:flex;align-items:center;justify-content:center;border-radius:980px;
background:rgba(0,20,80,.42);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.30);
color:#fff;opacity:0;transform:scale(.82);transition:opacity .2s,transform .2s cubic-bezier(.2,.8,.3,1),background .2s}
.rcard:hover .rfix{opacity:1;transform:scale(1)}
.rfix:hover{background:#fff;color:var(--ikb);border-color:#fff;box-shadow:0 4px 14px rgba(0,10,60,.45)}
.rfix svg{display:block}
.rfix.busy{background:var(--pop);color:#00206e;border-color:var(--pop)}
.rfix.busy svg{animation:rfspin .8s linear infinite}
@keyframes rfspin{to{transform:rotate(360deg)}}
.rcard.dim{opacity:.16}
.rcard.hit .rbob{outline:2px solid var(--pop);outline-offset:3px;border-radius:12px}
.pcard .pw{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:12px;background:var(--card2);display:block;transition:.22s;box-shadow:0 6px 18px rgba(0,10,60,.45)}
.pcard:hover .pw{box-shadow:0 14px 36px rgba(0,10,60,.65)}
.pcard .ph{width:100%;aspect-ratio:2/3;border-radius:12px;background:var(--card2);display:flex;align-items:center;justify-content:center;font-size:36px}
.pname{font-size:13px;font-weight:600;margin-top:9px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pmeta{font-size:11px;color:var(--sub);margin-top:3px}
.pbar{height:6px;background:rgba(255,255,255,.18);border-radius:3px;margin-top:8px;overflow:hidden}
.pbar i{display:block;height:100%;background:var(--pop);border-radius:3px;transition:width .5s ease}
.pbar i.full{background:var(--ok)}
.grpsec{border-top:1px solid var(--line);margin-top:6px}
.gt{font-size:15px;font-weight:600;margin-bottom:4px;letter-spacing:-.01em}
.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:20px;padding:6px 20px 18px}
.dcard{position:relative}
.dwrap{position:relative;border-radius:14px;overflow:hidden;box-shadow:0 10px 26px rgba(0,10,60,.5);background:var(--card2)}
.dpos{width:100%;aspect-ratio:2/3;object-fit:cover;display:block}
.dph{width:100%;aspect-ratio:2/3;display:flex;align-items:center;justify-content:center;font-size:38px;color:rgba(255,255,255,.5)}
.dpct{position:absolute;left:0;right:0;bottom:0;padding:26px 10px 8px;font-size:19px;font-weight:800;letter-spacing:-.02em;
background:linear-gradient(180deg,transparent,rgba(0,15,70,.86));text-shadow:0 2px 8px rgba(0,10,60,.6)}
.dcx{position:absolute;top:7px;right:7px;background:rgba(0,15,70,.62);backdrop-filter:blur(6px);border:none;color:#fff;border-radius:980px;
width:26px;height:26px;font-size:13px;cursor:pointer;opacity:0;transition:.18s;line-height:1;padding:0}
.dcard:hover .dcx{opacity:1}
.dcx:hover{background:rgba(255,90,80,.95)}
.dfree{position:absolute;top:7px;left:7px;background:rgba(255,212,0,.92);color:#00206e;border-radius:980px;padding:2px 8px;font-size:11px;font-weight:800}
.voy{padding:0;overflow:hidden}
.voysea{position:relative;height:150px;background:linear-gradient(180deg,#5b8dff 0%,#2a63e8 42%,#123fc4 100%)}
.voyw{position:absolute;left:0;bottom:0;width:200%;height:78%;animation:voyflow 13s linear infinite}
.voyw2{height:66%;animation-duration:9s;animation-direction:reverse}
.voyw3{height:52%;animation-duration:6.5s}
@keyframes voyflow{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.voyboat{position:absolute;bottom:52px;left:10px;line-height:0;animation:voybob 3.4s ease-in-out infinite alternate;
transition:left 2.4s cubic-bezier(.4,.15,.35,1);filter:drop-shadow(0 6px 14px rgba(0,10,60,.55));z-index:3}
@keyframes voybob{from{transform:translateY(2px) rotate(-6deg)}to{transform:translateY(-7px) rotate(6deg)}}
.voyhome{position:absolute;right:20px;bottom:54px;line-height:0;z-index:2;filter:drop-shadow(0 6px 16px rgba(0,10,60,.45))}
.voylamp{animation:voyglow 2.8s ease-in-out infinite alternate;transform-origin:23px 15px}
@keyframes voyglow{from{opacity:.14;transform:scale(.8)}to{opacity:.5;transform:scale(1.5)}}
.voydock .voylamp{animation-duration:1.1s}                       /* 靠岸:灯塔加急闪,像在指挥卸货 */
.voydock .voyboat{animation-duration:5s}                          /* 港内风平浪静,船不再大幅摇 */
@keyframes voybobcalm{from{transform:translateY(1px) rotate(-2deg)}to{transform:translateY(-3px) rotate(2deg)}}
.voydock .voyboat{animation-name:voybobcalm}
.voytext{padding:14px 22px 18px;display:flex;justify-content:space-between;align-items:baseline;gap:14px;flex-wrap:wrap}
.voystage{font-size:14px;font-weight:800;letter-spacing:.02em}
.voynum{font-size:13px;color:var(--sub)}
.voynum b{font-size:21px;color:var(--pop);font-weight:800;margin-right:3px}
.mtile{background:linear-gradient(160deg,rgba(255,255,255,.30),rgba(255,255,255,.10))}
.rph{width:108px;aspect-ratio:2/3;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:34px;color:rgba(255,255,255,.55);box-shadow:0 5px 16px rgba(0,10,60,.5)}
.mgridwrap{overflow:visible;-webkit-mask-image:none;mask-image:none;padding:10px 0 20px}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(122px,1fr));gap:16px 14px;padding:0 20px}
.mgrid .rcard{flex:none;width:auto}
.mgrid .rcard img,.mgrid .rph{width:100%}
.mbadge{position:absolute;left:8px;bottom:8px}
.mbadge .b{backdrop-filter:blur(8px);box-shadow:0 4px 14px rgba(0,10,60,.4)}
.dtt{font-size:13.5px;font-weight:700;letter-spacing:-.01em;margin-top:9px;line-height:1.35;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.dsub{font-size:11.5px;color:var(--sub);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dmeta{font-size:11.5px;color:var(--sub);margin-top:5px;line-height:1.5}
.hero{position:relative;border-radius:22px;overflow:hidden;text-align:center;padding:36px 20px 28px;margin-bottom:20px;
background:#0039c8;box-shadow:0 20px 54px rgba(0,10,60,.5);border:1px solid rgba(255,255,255,.18)}
.herovid{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:0}
.hero::after{content:'';position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,25,95,.30) 0%,rgba(0,47,167,.55) 62%,rgba(0,47,167,.92) 100%);z-index:1}
.hero>*:not(.herovid){position:relative;z-index:2}

.herotitle{font-size:30px;font-weight:800;letter-spacing:-.02em;margin-bottom:22px;text-shadow:0 3px 18px rgba(0,10,60,.55)}
.herotitle .mut{font-weight:500;font-size:14px;color:rgba(255,255,255,.82)}
.hero .searchbar{max-width:780px;margin:0 auto;padding:0}
.hero .fbar{justify-content:center;padding:18px 0 0}
#sresult:not(:empty){background:var(--card);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:20px;margin-bottom:20px;overflow:hidden}
.recentcard{overflow:hidden}
.rflow{overflow:hidden;padding:56px 0 36px;-webkit-mask-image:linear-gradient(90deg,transparent,#000 4%,#000 96%,transparent);mask-image:linear-gradient(90deg,transparent,#000 4%,#000 96%,transparent)}
.rtrack{display:flex;gap:14px;width:max-content;padding:0 20px;will-change:transform}
@keyframes bob{from{transform:translateY(-4px) rotate(-.5deg)}to{transform:translateY(4px) rotate(.5deg)}}
.rbob{animation:bob 4.2s ease-in-out infinite alternate}
.rcard:nth-child(2n) .rbob{animation-duration:5.1s;animation-delay:-1.7s}
.rcard:nth-child(3n) .rbob{animation-duration:4.6s;animation-delay:-2.9s}
.rcard:nth-child(5n) .rbob{animation-duration:5.6s;animation-delay:-.8s}
#im-ov{position:fixed;inset:0;background:rgba(0,18,70,.55);backdrop-filter:blur(8px);z-index:50;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:.25s}
#im-ov.show{opacity:1;pointer-events:auto}
#im-box{display:flex;gap:22px;max-width:640px;margin:20px;background:rgba(255,255,255,.16);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.3);border-radius:22px;padding:24px;box-shadow:0 30px 80px rgba(0,10,60,.6);transform:translateY(12px) scale(.97);transition:.25s cubic-bezier(.2,.8,.3,1)}
#im-ov.show #im-box{transform:none}
#im-box img{width:170px;aspect-ratio:2/3;object-fit:cover;border-radius:14px;box-shadow:0 10px 30px rgba(0,10,60,.5);flex-shrink:0}
#im-t{font-size:19px;font-weight:800;margin-bottom:8px}
#im-p{font-size:13px;line-height:1.75;color:rgba(255,255,255,.88);max-height:220px;overflow-y:auto}
#im-a{display:inline-block;margin-top:14px;background:#fff;color:var(--ikb);font-weight:800;border-radius:980px;padding:8px 22px;font-size:13px}
.rcard{flex:0 0 108px;position:relative;cursor:pointer;transition:transform .45s cubic-bezier(.22,.9,.32,1);transform-origin:center 62%;will-change:transform}
.rcard img{width:108px;aspect-ratio:2/3;object-fit:cover;border-radius:10px;background:var(--card2);box-shadow:0 5px 16px rgba(0,10,60,.5);display:block}
.rname{font-size:12px;font-weight:600;margin-top:7px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ryear{font-size:11px;color:var(--sub);margin-top:1px}
.sgrp{margin:14px 0 6px;font-size:14px;font-weight:700}
.srow{display:grid;grid-template-columns:210px 1fr;gap:10px;align-items:center;margin:8px 0}
.srow label{font-size:13px;color:rgba(255,255,255,.85)}
.srow input{background:rgba(255,255,255,.14);border:none;color:#fff;border-radius:10px;padding:9px 13px;font-size:13px;outline:none;width:100%}
.srow input:focus{box-shadow:0 0 0 2.5px rgba(255,255,255,.5)}
.shint{grid-column:2;font-size:11px;color:var(--sub);margin-top:-4px}
.ksin{background:rgba(255,255,255,.14);border:none;color:#fff;border-radius:10px;padding:9px 13px;font-size:13px;outline:none}
.ksin:focus{box-shadow:0 0 0 2.5px rgba(255,255,255,.5)}
.ksin::placeholder,.srow input::placeholder{color:rgba(255,255,255,.72)}
select.ksin option{color:#00206e}
.chip{display:inline-block;background:rgba(255,255,255,.16);border-radius:980px;padding:2px 10px;font-size:11px;margin:2px 3px 2px 0}
.chip.on{background:rgba(80,220,140,.25);color:#b8ffd6}
.chip.off{background:rgba(255,255,255,.09);color:var(--sub)}
.chip.ban{background:rgba(255,80,80,.28);color:#ffc9c9;font-weight:700}
.chip.wait{background:rgba(255,212,0,.22);color:#ffeb99}
.chip.err{background:rgba(255,133,121,.24);color:#ffc9c9}
.covbar{display:inline-block;width:64px;height:6px;border-radius:980px;background:rgba(255,255,255,.16);vertical-align:middle;overflow:hidden}
.covbar>i{display:block;height:100%;background:var(--ok)}
#xf-ov{position:fixed;inset:0;background:rgba(0,18,70,.55);backdrop-filter:blur(8px);z-index:60;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:.25s}
#xf-ov.show{opacity:1;pointer-events:auto}
#xf-box{width:min(680px,92vw);max-height:86vh;overflow-y:auto;background:rgba(255,255,255,.16);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.3);border-radius:22px;padding:24px;box-shadow:0 30px 80px rgba(0,10,60,.6)}
.xfl{display:block;font-size:12px;font-weight:700;margin:12px 0 4px;color:rgba(255,255,255,.85)}
.xfta{width:100%;background:rgba(0,20,90,.35);border:1px solid rgba(255,255,255,.22);color:#fff;border-radius:10px;padding:9px 12px;font-size:12.5px;line-height:1.6;outline:none;resize:vertical;font-family:ui-monospace,Menlo,monospace}
/* 折叠面板(次要功能收起,不挤主流程) */
.acc{margin:6px 20px;border:1px solid var(--line);border-radius:14px;overflow:hidden}
.acchead{padding:11px 16px;font-size:13px;font-weight:800;cursor:pointer;display:flex;align-items:center;gap:8px;user-select:none;background:rgba(255,255,255,.06);transition:.15s}
.acchead:hover{background:rgba(255,255,255,.11)}
.acchead::before{content:'▸';transition:.2s;color:var(--sub);font-size:12px}
.acc.open .acchead::before{transform:rotate(90deg)}
.accbody{display:none;padding:12px 16px 14px}
.acc.open .accbody{display:block}
/* 统一次要按钮:半透明白边 ghost 风格 */
.btn-ghost{background:rgba(255,255,255,.18)!important;color:#fff!important}
/* 空状态引导 */
.empty{text-align:center;padding:34px 20px;color:var(--sub)}
.empty .ei{font-size:38px;opacity:.6;margin-bottom:8px}
.empty .et{font-size:15px;font-weight:700;color:#fff;margin-bottom:4px}
/* 错误卡片:醒目+可重试 */
.errbox{margin:6px 20px 14px;padding:16px 18px;border-radius:14px;background:rgba(255,90,80,.14);border:1px solid rgba(255,90,80,.4);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.errbox .eic{font-size:26px}
/* 键盘可访问性:所有可点元素给焦点轮廓 */
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2.5px solid var(--pop);outline-offset:2px}
/* 动画降级:系统开了"减少动态效果"就停掉海报河/漂浮/波浪 */
@media (prefers-reduced-motion:reduce){.rbob,.rtrack,.voyw,.voyboat,.voylamp,.herovid{animation:none!important}}
/* ============ 移动端适配 ============ */
@media (max-width:768px){
 html,body{overflow-x:hidden}
 .wrap{padding:18px 12px}
 h1{font-size:21px}
 .tabs{display:flex;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%;border-radius:12px;scrollbar-width:none}
 .tabs::-webkit-scrollbar{display:none}
 .tabbtn{padding:9px 15px;white-space:nowrap;flex:0 0 auto}
 .stats{grid-template-columns:1fr 1fr;gap:10px}
 .card{overflow-x:auto}                       /* 超宽表格在卡片内横滚,不撑破页面 */
 .card table{min-width:max-content}
 .dgrid{grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:12px;padding:6px 14px 16px}
 .wall{grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:12px;padding:14px}
 .dbitem{width:96px}
 .mgrid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr))}
 .srow{grid-template-columns:1fr;gap:4px;margin:12px 0}
 .srow label{font-size:12.5px}
 .searchbar{flex-direction:column}
 .searchbar input,.searchbar button{width:100%}
 .hero{padding:24px 14px 20px}
 .rcard{flex-basis:88px}.rcard img,.rcard .rph{width:88px}
 #im-box{flex-direction:column;max-width:92vw;align-items:center;text-align:left}
 #im-box img{width:130px}
 #xf-box{width:94vw}
}
</style></head><body><div class=wrap>
<h1 style="display:flex;align-items:center;gap:11px"><svg width="34" height="34" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"><rect width="64" height="64" rx="14" fill="#0a2fb5"/><circle cx="46" cy="17" r="7.5" fill="#FFD400"/><path d="M2 37c7-9 15-9 21 0s15 9 21 0 12-8 18-3v30H2z" fill="#ffffff" opacity="0.95"/><path d="M2 47c7-7 13-7 19 0s15 7 21 0 14-7 20-1v18H2z" fill="#CFE0FF" opacity="0.9"/></svg>观澜 <span style="font-size:15px;font-weight:600;color:rgba(255,255,255,.6);letter-spacing:.04em">Wavegazer</span></h1><div class=sub>观影观澜 · 搜索 / 下载 / 刮削 / 保种 / 辅种 —— 一个人的影音港湾</div>
<div class=tabs>
<a href="#search" class="tabbtn" data-t="search">🔍 搜索下载</a>
<a href="#dl" class="tabbtn" data-t="dl">⬇️ 下载管理</a>
<a href="#media" class="tabbtn" data-t="media">📥 整理入库</a>
<a href="#seed" class="tabbtn" data-t="seed">🌱 辅种</a>
<a href="#keep" class="tabbtn" data-t="keep">🌊 做种运营</a>
<a href="#health" class="tabbtn" data-t="health">🩺 做种健康</a>
<a href="#logs" class="tabbtn" data-t="logs">📋 日志</a>
<a href="#setup" class="tabbtn" data-t="setup">⚙️ 设置</a>
</div>{{LOGOUT}}
<div id=tab-dl class=tab>
<div class=card><h2>⬇️ 下载中 <span class=mut style=font-weight:400>· qb 实时进度 · 4 秒刷新 · 下载完自动入库+转种,随后见「整理入库」</span></h2><div id=dlist style="padding:2px 16px 12px"><span class=mut>载入中…</span></div></div>
<div class=card><h2>最近完成的流水线</h2><div id=ddone></div></div>
</div>
<div id=tab-search class=tab>
<div class=hero>
<video class=herovid autoplay muted loop playsinline poster="/api/bg" src="/api/bgv?v=2"></video>
<div class=herotitle>今晚观什么澜?<div class=mut style="margin-top:6px">全站搜索 · 海报点选 · 一键下载,剩下的交给流水线</div></div>
<div class=searchbar><input id=q placeholder="片名 / 剧名 / 专辑,回车即搜" onkeydown="if(event.key=='Enter')doSearch()"><button onclick=doSearch()>搜索</button></div>
<div class=fbar>
<span class=fpill data-f=movie onclick=tgF(this)>🎬 电影</span>
<span class=fpill data-f=tv onclick=tgF(this)>📺 电视剧</span>
<span class=fpill data-f=anime onclick=tgF(this)>🎌 动漫</span>
<span class=fpill data-f=book onclick=tgF(this)>📖 漫画/书</span>
<span class=fpill data-f=music onclick=tgF(this)>🎵 音乐</span>
<span class=ppill id=ppill onclick=tgP(this)>👤 找人</span>
<span class=ppill id=apill onclick=tgA(this)>🎵 找歌手</span>
</div>
</div>
<div class=card id=dbshelf><h2>🎞 豆瓣经典榜 <span class=mut style=font-weight:400>· 按豆瓣评分排的高分经典 · 点海报直接全站找源,选个站就能下</span><span class=dbsrc id=dbsrc></span>
<button class=dlbtn style="padding:4px 13px;font-size:12px;margin-left:8px" onclick=dbMore()>换一批</button>
<button class=dlbtn style="padding:4px 13px;font-size:12px;margin-left:6px" onclick=batchOpen()>📦 批量下载本榜</button></h2>
<div id=batchbox style="padding:0 20px 12px;display:none"></div>
<div class=dbtabs id=dbtabs></div>
<div class=dbflow><div class=dbtrack id=dbtrack><span class=mut style=padding:12px>载入中…</span></div></div></div>
<div id=sresult></div>
<div class=stats id=dash>
<div class=stat><div class=n id=d-disk>—</div><div class=l id=d-diskl>存储剩余</div></div>
<div class=stat><div class=n id=d-speed>—</div><div class=l id=d-speedl>实时速率</div></div>
<div class=stat><div class=n id=d-media>—</div><div class=l id=d-medial>媒体库</div></div>
<div class=stat><div class=n style=color:#E8A400 id=d-seed>—</div><div class=l id=d-seedl>做种中</div></div>
</div>
<div class="card recentcard"><h2>🎬 最近入库 <span class=mut style=font-weight:400>· 点海报看简介</span></h2><div class=rflow><div class=rtrack id=rtrack>{{RECENT}}</div></div></div>
<div id=im-ov onclick="this.classList.remove('show')"><div id=im-box onclick="event.stopPropagation()"><img id=im-img><div><div id=im-t></div><div id=im-p></div><a id=im-a target=_blank>在 Emby 中打开 →</a></div></div></div>
<div id=wk-ov onclick=closeWk()><div id=wk-box onclick="event.stopPropagation()">
<div id=wk-head></div><div id=wk-body></div></div></div>
</div>
<div id=tab-media class=tab>
<div class=card><h2>🩺 媒体库体检 <span class=mut style=font-weight:400>· 查分类错放/季目录缺失 —— 这两类毛病会持续产生,别等发现了再翻库</span>
<button class=dlbtn style="padding:5px 16px;font-size:12px;margin-left:8px" onclick="libAudit(this)">开始体检</button></h2>
<div id=libaudit style="padding:2px 20px 14px"><span class=mut>点「开始体检」扫描媒体库</span></div></div>
<div class=card><h2>📥 整理入库 <span class=mut style=font-weight:400>· 下载完成自动识别→硬链接进 Emby 媒体库 · 按分类陈列 · 待确认的可手动填 TMDB id/片名</span></h2>
<div class=libbar><input id=libq placeholder="🔍 查查库里有没有 —— 输片名,下载前先确认别重复" oninput="libFind()">
<span class=mut id=libmsg>共 {{MEDIACOUNT}} 项</span></div>{{MEDIA}}</div>
</div>
<div id=tab-seed class=tab>
<div class=stats>
<div class=stat><div class=n>{{TOTAL}}</div><div class=l>已处理种子</div></div>
<div class=stat><div class=n style=color:var(--pop)>{{INJECT}}</div><div class=l>累计辅种注入</div></div>
<div class=stat><div class=n>{{DONE}}</div><div class=l>有匹配的种子</div></div>
<div class=stat><div class="n mut">{{NOMATCH}}</div><div class=l>无匹配</div></div>
</div>
<div class=card><h2>辅种记录 <span class=mut style=font-weight:400>· 每 {{INTERVAL}}s 扫描 · 点种子名看来源和去向</span> <button class=dlbtn style="padding:5px 16px;font-size:12px" onclick="researchAll(this)">🔁 重搜全部无匹配({{NOMATCH}})</button> <span class=mut id=raMsg style=font-size:12px></span></h2><table><tr><th>种子</th><th>来源</th><th>搜索词</th><th class=r>在辅站数</th><th>状态</th><th>手动辅种</th></tr>{{ROWS}}</table></div>
</div>
<div id=tab-health class=tab>
<div class=stats id=hstat>
<div class=stat><div class=n id=h-total>—</div><div class=l>tr 做种总数</div></div>
<div class=stat><div class=n style=color:var(--err) id=h-off>—</div><div class=l>tracker 掉线(白做)</div></div>
<div class=stat><div class="n mut" id=h-dead>—</div><div class=l>0-peer 冷种</div></div>
<div class=stat><div class=n style=color:#E8A400 id=h-up>—</div><div class=l>累计上传</div></div>
</div>
<div class=card><h2>🩺 做种健康 <span class=mut style=font-weight:400>· tracker 掉线=在做无效种,该处理 · 5分钟缓存 <button class=dlbtn style="padding:5px 16px;font-size:12px" onclick="healthLoad(this,1)">重新体检</button></span></h2>
<div id=health style="padding:2px 20px 16px"><span class=mut>进入自动体检…</span></div></div>
</div>
<div id=tab-keep class=tab>
<div class=card><h2>🌊 批量保种 <span class=mut style=font-weight:400>· 选站拉列表 → 筛选勾选 → 批量推 qb,下载完自动转 tr 做种 · 隔离在保种专用目录:不辅种/不入库/不打扰正常流水线 · 磁盘低于保护线自动暂停 · 清退看下面「③ 库存台账」</span></h2>
<div style="padding:4px 20px 8px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:13px">
<span style="font-weight:800">① 选站和条件</span>
<select id=ks-ix class=ksin style="min-width:160px"><option value="">加载站点中…</option></select>
<input id=ks-q class=ksin placeholder="关键词(留空=全站最新)" style="flex:1;min-width:140px">
<span class=mut>|</span>
单种体积≤<input id=ks-fsize class=ksin style="width:64px" placeholder="不限" oninput="ksRender()">GB
做种数≤<input id=ks-fseed class=ksin style="width:56px" placeholder="不限" oninput="ksRender()">
<label style="display:flex;align-items:center;gap:4px;cursor:pointer"><input type=checkbox id=ks-ffree onchange="ksRender()">🆓只要免费</label>
<label style="display:flex;align-items:center;gap:4px;cursor:pointer"><input type=checkbox id=ks-foff onchange="ksRender()">🏅只要官种</label>
<span class=mut>(都留空=什么都要)</span>
</div>
<div style="margin:6px 20px 8px;padding:12px 16px;background:rgba(255,212,0,.13);border:1px solid rgba(255,212,0,.4);border-radius:14px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:13px">
<span style="font-weight:800">② 🤖 全自动保种(推荐)</span>
共保<input id=ks-tgt class=ksin style="width:80px" placeholder="2048">GB
<button class=dlbtn style="padding:8px 24px;background:var(--pop);color:#00206e" onclick="ksAuto(this)">🚀 开始自动保种</button>
<span class=mut>就这一个按钮:自动翻页拉取整站,按①的条件过滤(含🆓),已有的跳过,边拉边下,够量自动停。2T=2048</span>
</div>
<div class=acc><div class=acchead onclick="accToggle(this)">✋ 手动挑选(想自己一个个挑就点开)</div>
<div class=accbody>
<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:13px;margin-bottom:8px">
<button class=dlbtn style="padding:7px 16px" onclick="ksFetch(false,this)">拉取列表</button>
<button class="dlbtn btn-ghost" style="padding:7px 14px" onclick="ksFetch(true,this)">翻下一页</button>
<span class=mut>→ 下面勾选 →</span>
<button class="dlbtn btn-ghost" style="padding:7px 14px" onclick="ksAll()">全选</button>
<button class=dlbtn style="padding:7px 18px;background:var(--pop);color:#00206e" onclick="ksPush(this)">⬇️ 推送选中</button>
</div>
<div id=ks-list><span class=mut>选个站点开拉。空关键词=按站内最新排列。</span></div>
</div></div>
<div class=acc><div class=acchead onclick="accToggle(this)">⚡ 抢免费守候(刷上传,想开就点开)</div>
<div class=accbody>
<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:13px">
<span class=mut>定时盯①选的站,新出的🆓免费种自动抢下做种回吐上传</span>
<button class=dlbtn id=fw-btn style="padding:7px 20px" onclick="fwToggle(this)">开启守候</button>
<span class=mut id=fw-stat></span>
</div></div></div>
</div>
<div class=card><h2>📦 保种任务 <span class=mut style=font-weight:400>· 队列逐个下载推 qb · <button class="dlbtn btn-ghost" style="padding:4px 12px;font-size:12px" onclick="ksStop()">⏹ 停止清空</button> <button class=dlbtn style="padding:4px 12px;font-size:12px" onclick="ksRetry(this)">♻️ 重试失败</button> <button class="dlbtn btn-ghost" style="padding:4px 12px;font-size:12px" onclick="ksClear(this)">🗑 清历史记录</button></span></h2>
<div id=ks-stat style="padding:0 20px 16px"><span class=mut>暂无任务</span></div></div>
<div class=card><h2>🧭 缺种报告 <span class=mut style=font-weight:400>· 三档分开:<b>已在站</b>=正在做种 / <b>确认没有</b>=问过了这站真没有 / <b>还没问</b>=账上还欠着,不算缺 · 点「⚡ 补问」立刻把欠的站问一遍,不用等辅种轮次 · 只有「确认没有」才值得发种,资料包带禁转标记直接拦</span> <button class=dlbtn style="padding:5px 16px;font-size:12px" onclick="gapLoad(this)">刷新</button></h2>
<div id=gap style="padding:0 20px 16px"><span class=mut>点「刷新」生成(要请求 Prowlarr,几秒钟)</span></div></div>
<div id=xf-ov onclick="this.classList.remove('show')"><div id=xf-box onclick="event.stopPropagation()">
<div style="font-size:17px;font-weight:800;margin-bottom:4px">🚚 发种资料包 <span class=mut id=xf-meta style=font-weight:400></span></div>
<div class=mut style="font-size:12px" id=xf-tip></div>
<label class=xfl>主标题</label><textarea id=xf-t class=xfta rows=2></textarea>
<label class=xfl>副标题</label><textarea id=xf-s class=xfta rows=1></textarea>
<label class=xfl>简介(bbcode)</label><textarea id=xf-d class=xfta rows=7></textarea>
<div style="margin-top:6px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
<button class=dlbtn style="background:var(--pop);color:#00206e" onclick="xfShot(this)">🎬 生成 MediaInfo + 截图</button>
<span class=mut id=xf-shotmsg style="font-size:12px"></span>
</div>
<div id=xf-shotout style="display:none">
<label class=xfl>MediaInfo</label><textarea id=xf-mi class=xfta rows=8 style="font-size:11.5px"></textarea>
<label class=xfl>截图(bbcode,已传 pixhost)</label><textarea id=xf-sh class=xfta rows=3></textarea>
<div id=xf-shthumbs style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"></div>
</div>
<div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap">
<button class=dlbtn onclick="xfCopy('xf-t',this)">复制主标题</button>
<button class=dlbtn onclick="xfCopy('xf-s',this)">复制副标题</button>
<button class=dlbtn onclick="xfCopy('xf-d',this)">复制简介</button>
<button class=dlbtn onclick="xfCopy('xf-mi',this)">复制MediaInfo</button>
<button class=dlbtn onclick="xfCopy('xf-sh',this)">复制截图</button>
</div></div></div>

<div class=card><h2>③ 库存台账 <span class=mut style=font-weight:400>· 灌进来的东西也得有出口 · 按证据硬度分三档,硬证据才建议清 · 删数据不可逆,只动保种目录</span>
<button class=dlbtn style="float:right;padding:5px 14px;font-size:12px" onclick="stockLoad(this)">盘点</button></h2>
<div id=stock style="padding:4px 20px 16px"><span class=mut>点「盘点」查看保种库存现在还值不值得占盘</span></div></div>
</div>
<div id=tab-logs class=tab>
<div class=card><h2>最近活动</h2><table><tr><th style=width:150px>时间</th><th>消息</th></tr>{{LOGS}}</table></div>
</div>
<div id=tab-setup class=tab>
<div class=card><h2>⚙️ 连接设置 <span class=mut style=font-weight:400>· 填好各服务地址,点测试验证,保存即热生效(无需重启)</span></h2>
<div id=setform style="padding:4px 20px 16px"><span class=mut>加载中…</span></div>
<div style="padding:0 20px 20px;display:flex;gap:12px;align-items:center">
<button class=dlbtn style="padding:11px 34px;font-size:14px" onclick="saveSettings(this)">💾 保存全部</button>
<button class="dlbtn btn-ghost" style="padding:11px 26px;font-size:14px" onclick="testAll(this)">🔌 测试全部连接</button>
<span id=set-msg class=mut></span>
</div>
<div id=testout style="padding:0 20px 16px;font-size:13px;line-height:2"></div>
</div>
</div>
<div class=sub style=text-align:center>观澜 Wavegazer · 一个人的影音港湾 · MIT 开源</div>
</div><div id=toast></div>
<script>
var _dlT=null;var _t=null;var _ksT=null;var _dashT=null;
var _of=window.fetch;   // 会话过期(401)自动送回登录页,不再半死不活
window.fetch=function(){return _of.apply(this,arguments).then(function(r){
 if(r.status==401){location.href='/login';}
 return r;});};
var _noReload=false;   // 有体检结果这类"看完才有用"的内容时置位,挡住自动刷新
function armReload(t){
 clearTimeout(_t);_t=null;
 if(_noReload)return;   // 结果还在屏幕上,别把人家正在看/正在复制的东西刷没了
 if(t=='seed'||t=='media'||t=='logs')_t=setTimeout(()=>location.reload(),20000);  // 只有表格页才自动刷新
}
var _curTab='search';
function startPolls(t){       // 只给当前 tab 装轮询;页面不可见时一律不装(省电省流量)
 clearInterval(_dlT);clearInterval(_ksT);clearInterval(_dashT);
 if(document.hidden)return;
 _dashT=setInterval(pollDash,5000);          // 仪表盘只在页面可见时刷
 if(t=='dl'){pollDl();_dlT=setInterval(pollDl,4000);}
 if(t=='keep'){ksInit();_ksT=setInterval(ksStatus,3000);}
}
function showTab(t){
 document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));
 document.querySelectorAll('.tabbtn').forEach(e=>e.classList.remove('on'));
 var el=document.getElementById('tab-'+t);(el||document.getElementById('tab-search')).classList.add('active');
 var b=document.querySelector('.tabbtn[data-t="'+(el?t:'search')+'"]');if(b)b.classList.add('on');
 _curTab=el?t:'search';
 armReload(_curTab);
 startPolls(_curTab);
 if(t=='health'){healthLoad(null,0);}
}
document.addEventListener('visibilitychange',function(){
 if(document.hidden){clearInterval(_dlT);clearInterval(_ksT);clearInterval(_dashT);clearTimeout(_t);}
 else{startPolls(_curTab);armReload(_curTab);}
});
var _hLoaded=false;
function healthLoad(btn,force){
 if(btn){btn.disabled=true;btn.textContent='体检中…';}
 else if(_hLoaded)return;
 var el=document.getElementById('health');
 if(!_hLoaded)el.innerHTML='<span class=mut>正在读 tr 全部种子的 tracker 状态,几秒钟…</span>';
 fetch('/api/health'+(force?'?force=1':'')).then(r=>r.json()).then(function(d){
  if(btn){btn.disabled=false;btn.textContent='重新体检';}
  _hLoaded=true;
  if(!d.ok){el.innerHTML='<span class=mut>体检失败: '+(d.err||'')+'</span>';return;}
  document.getElementById('h-total').textContent=d.total;
  document.getElementById('h-off').textContent=d.offline.length;
  document.getElementById('h-dead').textContent=d.dead_n;
  document.getElementById('h-up').textContent=d.up_total;
  var h='';
  if(d.offline.length){
   h+='<div class=sgrp>🔴 Tracker 掉线('+d.offline.length+') <span class=mut style=font-weight:400>· announce 失败=这些种在做但没向站汇报,等于白做,去 tr 里删了重加或换 tracker</span></div>';
   h+='<table><tr><th>种子</th><th class=r>体积</th><th>掉线的 tracker</th></tr>';
   d.offline.forEach(function(x){
    var tk=x.trackers.map(t=>'<div class=mut style="font-size:12px">'+t.host+' — '+t.msg+'</div>').join('');
    h+='<tr><td class=name title="'+x.name.replace(/"/g,'')+'">'+x.name+'</td><td class=r>'+x.sizeh+'</td><td>'+tk+'</td></tr>';
   });
   h+='</table>';
  }
  if(d.errored.length){
   h+='<div class=sgrp>⚠️ tr 报错种子('+d.errored.length+')</div><table><tr><th>种子</th><th class=r>体积</th><th>错误</th></tr>';
   d.errored.forEach(function(x){h+='<tr><td class=name>'+x.name+'</td><td class=r>'+x.sizeh+'</td><td class=mut>'+x.err+'</td></tr>';});
   h+='</table>';
  }
  if(d.dead.length){
   h+='<div class=sgrp>🧊 0-peer 冷种('+d.dead_n+') <span class=mut style=font-weight:400>· 没人下也没别的做种者,占空间但不产上传 · 保种目录的已排除 · 要清理去 tr 里删 · 按闲置降序</span></div>';
   h+='<table><tr><th>种子</th><th class=r>体积</th><th class=r>闲置</th><th class=r>累计上传</th></tr>';
   d.dead.forEach(function(x){h+='<tr><td class=name title="'+x.name.replace(/"/g,'')+'">'+x.name+'</td><td class=r>'+x.sizeh+'</td><td class=r>'+x.idle+' 天</td><td class=r class=mut>'+x.up+'</td></tr>';});
   h+='</table>';
  }
  if(!d.offline.length&&!d.errored.length&&!d.dead.length)h='<div style="padding:20px;text-align:center;font-size:15px">🎉 全部健康,没有掉线 tracker、报错种子或 0-peer 冷种</div>';
  el.innerHTML=h;
 }).catch(function(){if(btn){btn.disabled=false;btn.textContent='重新体检';}el.innerHTML='<span class=mut>体检出错</span>';});
}
var SM={downloading:'⬇️ 下载中',stalledDL:'🐢 等速度',metaDL:'🧲 元数据',forcedDL:'⬇️ 下载中',pausedDL:'⏸ 暂停',queuedDL:'⏳ 排队',allocating:'分配空间',uploading:'✅ 完成·待转种',stalledUP:'✅ 完成·待转种',queuedUP:'✅ 完成·待转种',forcedUP:'✅ 完成·待转种',checkingDL:'🔍 校验中',checkingUP:'🔍 校验中',checkingResumeData:'🔍 校验中',error:'❌ 错误',missingFiles:'❌ 文件缺失'};
var STM={done:['✅ 已入库+转种','done'],hold:['⚠️ 待确认(去整理入库页处理)','nomatch'],processing:['🔄 整理中','searching'],error:['❌ 出错','err']};
function pollDl(){
 fetch('/api/downloads').then(r=>r.json()).then(function(d){
  var el=document.getElementById('dlist');if(!el)return;
  var dl=d.dl||[];
  if(d.err){el.className='';el.innerHTML='<div class=errbox><span class=eic>🔌</span><div style="flex:1;min-width:180px"><div style="font-weight:800">连不上 qBittorrent</div><div class=mut style="font-size:12.5px">'+d.err+' · 检查 qb 是否在跑、设置里地址/凭据是否对</div></div><button class=dlbtn onclick="gotoSetup()">去设置</button> <button class="dlbtn btn-ghost" onclick="pollDl()">重试</button></div>';}
  else if(!dl.length){el.className='';el.innerHTML='<div class=empty><div class=ei>🌊</div><div class=et>当前没有下载任务</div><div>去「搜索」找片下载,下载完会自动入库+转种到 tr</div></div>';}
  else{
   el.innerHTML='';el.className='dgrid';
   dl.forEach(function(t){
    var card=document.createElement('div');card.className='dcard';
    var wrap=document.createElement('div');wrap.className='dwrap';
    if(t.poster){var im=document.createElement('img');im.className='dpos';im.loading='lazy';im.src='/api/poster?p='+encodeURIComponent(t.poster);wrap.appendChild(im);}
    else{var ph=document.createElement('div');ph.className='dph';ph.textContent='⬇️';wrap.appendChild(ph);}
    var pct=document.createElement('div');pct.className='dpct';pct.textContent=t.progress+'%';wrap.appendChild(pct);
    var cx=document.createElement('button');cx.className='dcx';cx.textContent='✕';cx.title='取消下载';
    cx.onclick=function(){
     if(!confirm('取消下载「'+(t.tmdb||t.name.slice(0,30))+'」? 将从 qb 移除任务并删除已下载的数据。'))return;
     cx.disabled=true;
     fetch('/api/canceldl?hash='+encodeURIComponent(t.hash)).then(r=>r.json()).then(function(d){
      if(d.ok){toast('已取消并清理');pollDl();}
      else{toast('取消失败：'+(d.err||''));cx.disabled=false;}
     }).catch(()=>{cx.disabled=false;});
    };
    wrap.appendChild(cx);card.appendChild(wrap);
    var tt=document.createElement('div');tt.className='dtt';
    tt.textContent=t.tmdb?(t.tmdb+(t.year?' ('+t.year+')':'')):t.name.slice(0,50);
    tt.title=t.name;card.appendChild(tt);
    var pb=document.createElement('div');pb.className='pbar';var pi=document.createElement('i');
    pi.style.width=t.progress+'%';if(t.progress>=100)pi.className='full';pb.appendChild(pi);card.appendChild(pb);
    var i=document.createElement('div');i.className='dmeta';
    i.textContent=(SM[t.state]||t.state)+' · '+t.sizeh+' · '+t.speed+(t.eta?' · 剩'+t.eta:'')+' · 做种'+t.seeds;
    card.appendChild(i);
    el.appendChild(card);
   });
  }
  var dd=document.getElementById('ddone');if(!dd)return;
  dd.innerHTML='';
  var tbl=document.createElement('table');
  var hd=document.createElement('tr');hd.innerHTML='<th style=width:110px>时间</th><th>下载名</th><th>识别为</th><th>状态</th>';tbl.appendChild(hd);
  (d.done||[]).forEach(function(x){
   var tr=document.createElement('tr');
   var c1=document.createElement('td');c1.className='mut';c1.textContent=x.ts;
   var c2=document.createElement('td');c2.className='sname';c2.style.maxWidth='380px';c2.title=x.name;c2.textContent=x.name;
   var c3=document.createElement('td');c3.textContent=x.tmdb;
   var st=STM[x.status]||[x.status,'err'];
   var c4=document.createElement('td');var sp=document.createElement('span');sp.className='b '+st[1];sp.textContent=st[0];c4.appendChild(sp);
   tr.appendChild(c1);tr.appendChild(c2);tr.appendChild(c3);tr.appendChild(c4);tbl.appendChild(tr);
  });
  dd.appendChild(tbl);
 }).catch(()=>{});
}
/* ===== 批量保种 / 缺种报告 / 转种资料包 ===== */
var _ksItems=[],_ksPage=0,_ksInited=false;
function ksInit(){
 ksStatus();
 if(_ksInited)return;_ksInited=true;
 fetch('/api/ks/indexers').then(r=>r.json()).then(function(d){
  var sel=document.getElementById('ks-ix');sel.innerHTML='';
  if(!d.ok||!d.list.length){sel.innerHTML='<option value="">取不到站点(检查Prowlarr)</option>';return;}
  d.list.forEach(function(i){var o=document.createElement('option');o.value=i.id;o.textContent=i.name;sel.appendChild(o);});
 }).catch(()=>{});
}
function ksFetch(more,btn){
 var ix=document.getElementById('ks-ix').value;
 if(!ix){toast('先选站点');return;}
 _ksPage=more?_ksPage+1:0;
 if(btn){btn.disabled=true;btn.dataset.t=btn.textContent;btn.textContent='拉取中…';}
 var q=encodeURIComponent(document.getElementById('ks-q').value.trim());
 fetch('/api/ks/list?ix='+ix+'&q='+q+'&page='+_ksPage).then(r=>r.json()).then(function(d){
  if(btn){btn.disabled=false;btn.textContent=btn.dataset.t;}
  if(!d.ok){toast('拉取失败: '+(d.err||''));if(more)_ksPage--;return;}
  if(!d.items.length){toast('第'+(_ksPage+1)+'页没有种子了,真到底了');if(more)_ksPage--;return;}
  _ksItems=d.items;ksRender();   // 每次整页替换:勾选→推送→翻下一页,循环
  toast('第'+(_ksPage+1)+'页: '+d.items.length+'条');
 }).catch(function(){if(btn){btn.disabled=false;btn.textContent=btn.dataset.t;}toast('拉取失败');if(more)_ksPage--;});
}
function ksFiltered(){
 var mg=parseFloat(document.getElementById('ks-fsize').value)||0;
 var ms=document.getElementById('ks-fseed').value.trim();
 var fo=document.getElementById('ks-ffree').checked;
 var oo=document.getElementById('ks-foff').checked;
 return _ksItems.filter(function(x){
  if(fo&&!x.free)return false;
  if(oo&&!x.off)return false;
  if(mg&&x.size>mg*1073741824)return false;
  if(ms!==''&&x.seeders>parseInt(ms))return false;
  return true;});
}
function ksRender(){
 if(!_ksItems.length)return;   // 还没拉过列表,别把提示语刷掉
 var el=document.getElementById('ks-list'),fs=ksFiltered();
 if(!fs.length){el.innerHTML='<span class=mut>没有符合条件的种子</span>';return;}
 var h='<table><tr><th style=width:30px></th><th>种子名</th><th class=r>体积</th><th class=r>做种</th><th>发布</th></tr>';
 fs.slice(0,400).forEach(function(x,i){
  var nm=x.name.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  h+='<tr><td><input type=checkbox class=kscb data-i='+_ksItems.indexOf(x)+'></td>'
   +'<td class=name title="'+nm+'">'+(x.free?'<span class="chip on">🆓</span> ':'')+(x.off?'<span class="chip off" style="background:rgba(255,212,0,.28);color:#ffe98a">🏅官种</span> ':'')+(x.noxfer?'<span class="chip ban">🚫禁转</span> ':'')+nm+'</td>'
   +'<td class=r>'+x.sizeh+'</td><td class=r>'+x.seeders+'</td><td class=mut>'+x.date+'</td></tr>';
 });
 h+='</table><div class=mut style=margin-top:6px>显示 '+Math.min(fs.length,400)+' / 符合 '+fs.length+' / 已拉 '+_ksItems.length+' 条 · 禁转种可保种但转种助手会拦</div>';
 el.innerHTML=h;
}
function ksAll(){
 var cbs=document.querySelectorAll('.kscb');
 var allOn=[...cbs].every(c=>c.checked);   // 全勾着=再点一次取消全选
 cbs.forEach(c=>c.checked=!allOn);
}
function ksPush(btn){
 var picks=[];
 document.querySelectorAll('.kscb:checked').forEach(function(c){var x=_ksItems[parseInt(c.dataset.i)];if(x)picks.push({name:x.name,size:x.size,url:x.url});});
 if(!picks.length){toast('先勾选种子');return;}
 var tot=picks.reduce((a,b)=>a+b.size,0);
 if(!confirm('推送 '+picks.length+' 个种子进保种队列,共约 '+(tot/1073741824).toFixed(1)+' GB。确定?'))return;
 btn.disabled=true;
 fetch('/api/ks/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ix:document.getElementById('ks-ix').value,items:picks})})
 .then(r=>r.json()).then(function(d){btn.disabled=false;toast(d.ok?('已入队 '+d.n+' 个,后台逐个拉取'):'失败');ksStatus();})
 .catch(function(){btn.disabled=false;toast('失败');});
}
function mFix(ev,btn){
 ev.stopPropagation();ev.preventDefault();     // 别触发卡片的"看简介"
 var cur=btn.dataset.n||'';
 var v=prompt('识别错了?填片名、或 TMDB id(数字)。同一个 id 在电影和剧集里是两套编号,想指定就写 movie/79064 或 tv/12345。当前识别为「'+cur+'」',cur);
 if(v===null)return;
 v=v.trim();if(!v)return;
 var icon=btn.innerHTML;                      // 存住 SVG,失败时还原,别被文字冲掉
 btn.disabled=true;btn.style.opacity='1';btn.classList.add('busy');
 fetch('/api/reid?hash='+encodeURIComponent(btn.dataset.h)+'&q='+encodeURIComponent(v))
 .then(r=>r.json()).then(function(d){
  if(d.ok){toast('已重新入库:'+(d.name||v));setTimeout(()=>location.reload(),1200);}
  else{toast('改识别失败:'+(d.err||''));btn.innerHTML=icon;btn.disabled=false;btn.classList.remove('busy');}
 }).catch(function(){toast('出错了');btn.innerHTML=icon;btn.disabled=false;btn.classList.remove('busy');});
}
function accToggle(el){el.parentNode.classList.toggle('open');}
function gotoSetup(){location.hash='#setup';}
function ksStop(){fetch('/api/ks/stop').then(r=>r.json()).then(()=>{toast('已停止,队列清空');ksStatus();});}
function ksRetry(btn){btn.disabled=true;
 fetch('/api/ks/retry').then(r=>r.json()).then(function(d){btn.disabled=false;
  toast(d.n?('已重排 '+d.n+' 个失败项,后台重下'):'没有失败项');ksStatus();});}
function ksClear(btn){
 if(!confirm('清除保种任务里已完成/已转tr/失败/跳过的历史记录?(不影响正在做种的种子)'))return;
 btn.disabled=true;
 fetch('/api/ks/clear?what=all').then(r=>r.json()).then(function(d){btn.disabled=false;
  toast('已清 '+d.n+' 条历史');ksStatus();});}
function researchAll(btn){
 if(!confirm('对所有「无匹配」的种子重新全站搜一遍?后台逐个跑(每个约40秒,有节流),耗时较长。'))return;
 btn.disabled=true;
 fetch('/api/researchall').then(r=>r.json()).then(function(){
  btn.disabled=false;document.getElementById('raMsg').textContent='🔁 已在后台重搜,完成后刷新页面看结果';
  toast('批量重搜已启动');});}
function skipOne(h,btn){btn.disabled=true;
 fetch('/api/skip?hash='+encodeURIComponent(h)).then(r=>r.json()).then(function(d){
  if(d.ok){var c=btn.closest('.dcard');if(c)c.style.display='none';toast('已跳过');}
  else{btn.disabled=false;toast('失败');}});}
function skipAll(btn){
 if(!confirm('把所有「待确认」的条目标记为跳过?(不入库,以后不再提示)'))return;
 btn.disabled=true;
 fetch('/api/skipall').then(r=>r.json()).then(function(d){toast('已跳过 '+d.n+' 个');location.reload();});}
function ksAuto(btn){
 var ix=document.getElementById('ks-ix').value;
 var tgt=parseFloat(document.getElementById('ks-tgt').value)||0;
 if(!ix){toast('先选站点');return;}
 if(!tgt){toast('填目标量(GB),2T=2048');return;}
 var q=document.getElementById('ks-q').value.trim();
 var mg=parseFloat(document.getElementById('ks-fsize').value)||0;
 var ms=document.getElementById('ks-fseed').value.trim();
 if(!confirm('自动翻页拉取该站种子直到入队约 '+tgt+' GB(套用当前筛选,已有的自动跳过),边拉边下。继续?'))return;
 btn.disabled=true;
 fetch('/api/ks/auto?ix='+ix+'&q='+encodeURIComponent(q)+'&target='+tgt+'&fsize='+mg+'&fseed='+(ms===''?'-1':ms)+'&free='+(document.getElementById('ks-ffree').checked?1:0)+'&off='+(document.getElementById('ks-foff').checked?1:0))
 .then(r=>r.json()).then(function(d){btn.disabled=false;toast(d.ok?'🤖 自动拉取已启动,看下方任务进度':'启动失败: '+(d.err||''));ksStatus();})
 .catch(function(){btn.disabled=false;toast('启动失败');});
}
function ksStatus(){
 fetch('/api/ks/status').then(r=>r.json()).then(function(d){
  var el=document.getElementById('ks-stat');if(!el)return;
  var h='<div style="font-size:13px;line-height:2">'
   +(d.running?'🔄 执行中: <b>'+(d.cur||'…')+'</b>':'⏸ 空闲')
   +' · 队列 <b>'+d.queued+'</b> · 已推qb <b style=color:var(--pop)>'+d.pushed+'</b> · 已转tr <b style=color:#7dffb0>'+d.done+'</b> · 失败 '+d.error
   +' · 磁盘余 '+d.free+'GB'
   +(d.af||d.afmsg?'<br>'+(d.af?'🤖 ':'')+(d.afmsg||''):'')
   +(d.msg?'<br>'+d.msg:'')+'</div>';
  el.innerHTML=h;
  var fb=document.getElementById('fw-btn'),fst=document.getElementById('fw-stat');
  if(fb){
   if(d.fw){fb.textContent='🔴 关闭守候';fb.style.background='rgba(255,120,110,.9)';fb.style.color='#fff';}
   else{fb.textContent='⚡ 开启守候';fb.style.background='';fb.style.color='';}
   fst.textContent=(d.fw?'守候中(每'+d.fwmin+'分钟) · ':'未开启 · ')+(d.fwmsg||'');
  }
 }).catch(()=>{});
}
function fwToggle(btn){
 var on=btn.textContent.indexOf('关闭')>=0;
 if(on){fetch('/api/ks/watch?ix=').then(r=>r.json()).then(()=>{toast('守候已关闭');ksStatus();});return;}
 var ix=document.getElementById('ks-ix').value;
 if(!ix){toast('先在①里选要守候的站点');return;}
 fetch('/api/ks/watch?ix='+ix).then(r=>r.json()).then(function(d){toast(d.ok?'⚡ 守候已开启,新免费种自动抢':'失败');ksStatus();});
}
var _stockPick={dead:[],offline:[],idle_big:[]};
function stockSec(key,title,rows,gb,hint,cls){
 if(!rows.length)return '';
 var h='<div style="margin:14px 0 6px"><b>'+title+'</b> <span class=mut>'+rows.length+' 个 · 共 '+gb+' GB · '+hint+'</span>'
  +' <button class=dlbtn style="padding:3px 10px;font-size:11px;margin-left:6px" data-sk="'+key+'" onclick="stockAll(this.dataset.sk)">全选本档</button></div><table>';
 rows.forEach(function(r,i){
  h+='<tr><td style="width:26px"><input type=checkbox data-k="'+key+'" data-h="'+r.hash+'"></td>'
   +'<td class=name title="'+r.name.replace(/"/g,'')+'">'+r.name.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</td>'
   +'<td class=r>'+r.sizeh+'</td><td class=mut style="font-size:12px">'+r.why+'</td></tr>';
 });
 return h+'</table>';
}
function stockAll(k){document.querySelectorAll('#stock input[data-k="'+k+'"]').forEach(function(c){c.checked=true;});}
function stockLoad(btn){
 if(btn){btn.disabled=true;btn.textContent='盘点中…';}
 fetch('/api/stock').then(r=>r.json()).then(function(d){
  if(btn){btn.disabled=false;btn.textContent='盘点';}
  var el=document.getElementById('stock');
  if(!d.ok){el.innerHTML='<span class=mut>'+(d.err||'盘点失败')+'</span>';return;}
  var h='<div class=mut style="margin-bottom:6px">库存 '+d.n+' 个 · 占盘 '+d.total+' · 累计上传 '+d.up_total
   +' · 磁盘剩余 '+d.free_gb+'GB(保护线 '+d.guard_gb+'GB)</div>';
  h+=stockSec('dead','🔴 站点已删种',d.dead,d.dead_gb,'tracker 自己说这个种没了,继续做纯浪费,清了不影响考核','');
  h+=stockSec('offline','🟠 tracker 连不上',d.offline,d.offline_gb,'也可能是站点抽风或 cookie 过期,清之前值得看一眼','');
  h+=stockSec('idle_big','⚪ 大体积零上传',d.idle_big,d.idle_gb,'只是陈列 —— 保种本来就是备着,没上传不等于没价值,自己判断','');
  if(!d.dead.length&&!d.offline.length&&!d.idle_big.length)
   h+='<div style="margin-top:10px">✅ 库存都健康('+d.alive_n+' 个正常做种),没有该清的</div>';
  else h+='<div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
   +'<button class=dlbtn style="background:rgba(255,120,100,.34)" onclick="stockEvict()">🗑 清退勾选的(连数据一起删)</button>'
   +'<span class=mut style="font-size:12px">不可逆。只会动保种目录里的种子,媒体库资产会被后端拒绝。</span></div>';
  el.innerHTML=h;
 }).catch(function(){if(btn){btn.disabled=false;btn.textContent='盘点';}});
}
function stockEvict(){
 var hs=[];document.querySelectorAll('#stock input[type=checkbox]:checked').forEach(function(c){hs.push(c.dataset.h);});
 if(!hs.length){toast('先勾选要清的');return;}
 if(!confirm('确定清退 '+hs.length+' 个保种种子?数据会一起删除,不可恢复。'))return;
 fetch('/api/stock/evict',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hashes:hs})})
  .then(r=>r.json()).then(function(d){
   if(!d.ok){toast('清退失败: '+(d.err||''));return;}
   toast('已清退 '+d.removed.length+' 个,腾出 '+d.freed+(d.refused.length?' · '+d.refused.length+' 个被护栏拒绝':''));
   stockLoad();
  }).catch(function(){toast('清退出错');});
}
function gapLoad(btn){
 if(btn){btn.disabled=true;btn.textContent='生成中…';}
 fetch('/api/gap').then(r=>r.json()).then(function(d){
  if(btn){btn.disabled=false;btn.textContent='刷新';}
  var el=document.getElementById('gap');
  if(!d.ok||!d.rows.length){el.innerHTML='<span class=mut>暂无数据(先让辅种跑起来)</span>';return;}
  var st=d.stat||{};
  var h='<div class=mut style="margin:0 0 10px">账上共 '+(st.content||0)+' 份内容 · 已做种 '+(st.seeding||0)+' 站次 · 问过确实没有 '+(st.absent||0)+' 站次'+(st.error?' · <span style="color:var(--err)">没问成 '+st.error+' 站次</span>':'')+'</div>';
  h+='<table><tr><th>内容</th><th class=r>体积</th><th>问遍了没</th><th>已在站</th><th>账上状况</th><th></th></tr>';
  d.rows.forEach(function(r){
   var nm=r.name.replace(/&/g,'&amp;').replace(/</g,'&lt;');
   var tot=d.sites||1, asked=r.seeded.length+r.absent.length+r.banned.length;
   var pct=Math.min(100,Math.round(asked*100/tot));
   var owe=r.pending.length+r.errs.length;
   var on=r.seeded.map(x=>'<span class="chip on">'+x+'</span>').join('')||'<span class=mut>—</span>';
   var bits=[];
   if(r.pending.length)bits.push('<span class="chip wait" title="'+r.pending.join(" ")+'">还没问 '+r.pending.length+' 站</span>');
   if(r.errs.length)bits.push('<span class="chip err" title="'+r.errs.join(" ")+'">没问成 '+r.errs.length+' 站</span>');
   if(r.absent.length)bits.push('<span class="chip off" title="'+r.absent.join(" ")+'">确认没有 '+r.absent.length+' 站</span>');
   h+='<tr data-sites="'+tot+'"><td class=name title="'+nm+'">'+nm+'</td><td class=r>'+r.sizeh+'</td>'
    +'<td><span class=covbar><i style="width:'+pct+'%"></i></span> <span class=mut>'+asked+'/'+tot+'</span></td>'
    +'<td class=gseed>'+on+'</td>'
    +'<td class=gmiss>'+(bits.length?bits.join(''):'<span class=mut>全问遍了 🎉</span>')+'</td>'
    +'<td style="white-space:nowrap">'
    +(owe?'<button class=dlbtn style="padding:5px 12px;font-size:12px;background:rgba(255,212,0,.28);color:#fff" data-h="'+r.hash+'" onclick="gapFill(this.dataset.h,this)">⚡ 补问 '+owe+' 站</button> ':'')
    +(r.absent.length?'<button class=dlbtn style="padding:5px 12px;font-size:12px" data-h="'+r.hash+'" onclick="xfer(this.dataset.h)">🚚 发种资料包</button>':'')
    +'</td></tr>';
  });
  el.innerHTML=h+'</table>';
 }).catch(function(){if(btn){btn.disabled=false;btn.textContent='刷新';}});
}
function gapFill(h,btn){
 btn.disabled=true;var o=btn.textContent;btn.textContent='补问中…';
 fetch('/api/gapfill?hash='+h).then(r=>r.json()).then(function(d){
  btn.disabled=false;
  if(!d.ok){btn.textContent=o;toast('补问失败: '+(d.err||''));return;}
  toast('补问完成:认领 '+d.matched+' 站,注入 '+d.injected+' 站;账上还欠 '+(d.pending.length+d.errs.length)+' 站');
  gapLoad();
 }).catch(function(){btn.disabled=false;btn.textContent=o;toast('补问出错');});
}
function gapV(h,btn){
 btn.disabled=true;btn.dataset.o=btn.textContent;btn.textContent='核实中…约30秒';
 fetch('/api/gapverify?hash='+h).then(r=>r.json()).then(function(d){
  btn.disabled=false;btn.textContent=d.ok?'✅ 已核实':btn.dataset.o;
  if(!d.ok){toast('核实失败: '+(d.err||''));return;}
  var tr=btn.closest('tr');
  tr.querySelector('.gseed').innerHTML=d.have.map(s=>'<span class="chip on">'+s+'</span>').join('')||'<span class=mut>各站都没搜到</span>';
  var off=d.missing.slice(0,14).map(s=>'<span class="chip off">'+s+'</span>').join('')+(d.missing.length>14?'<span class=mut> +'+(d.missing.length-14)+'</span>':'');
  tr.querySelector('.gmiss').innerHTML=d.missing.length?off:'<span class=mut>全覆盖 🎉</span>';
  toast('实搜「'+d.q+'」: '+d.have.length+' 站有,'+d.missing.length+' 站真缺');
 }).catch(function(){btn.disabled=false;btn.textContent=btn.dataset.o;toast('核实出错');});
}
var _xfHash='';
function xfer(h){
 _xfHash=h;
 fetch('/api/xfer?hash='+h).then(r=>r.json()).then(function(d){
  if(!d.ok){toast(d.banned?('🚫 '+d.err):('失败: '+(d.err||'')));return;}
  document.getElementById('xf-meta').textContent=' · '+d.sizeh+' · '+d.files+' 个文件';
  document.getElementById('xf-tip').textContent=d.tip;
  document.getElementById('xf-t').value=d.title;
  document.getElementById('xf-s').value=d.sub;
  document.getElementById('xf-d').value=d.desc;
  document.getElementById('xf-shotout').style.display='none';
  document.getElementById('xf-shotmsg').textContent='';
  document.getElementById('xf-mi').value='';document.getElementById('xf-sh').value='';
  document.getElementById('xf-shthumbs').innerHTML='';
  document.getElementById('xf-ov').classList.add('show');
 });
}
function xfShot(btn){
 if(!_xfHash)return;
 btn.disabled=true;
 var msg=document.getElementById('xf-shotmsg');
 msg.textContent='🎬 正在抽帧+读取媒体信息+上传图床,约十几秒…';
 fetch('/api/xfershot?hash='+_xfHash+'&go=1').then(r=>r.json()).then(function(){
  (function poll(){
   fetch('/api/xfershot?hash='+_xfHash).then(r=>r.json()).then(function(d){
    if(!d.done){setTimeout(poll,2000);return;}
    btn.disabled=false;
    if(d.err){msg.textContent='❌ '+d.err;return;}
    document.getElementById('xf-shotout').style.display='block';
    var NL=String.fromCharCode(10);
    document.getElementById('xf-mi').value=d.mediainfo||'';
    var bb=(d.shots||[]).map(function(s){return '[img]'+s[1]+'[/img]';}).join(' ');
    document.getElementById('xf-sh').value=bb;
    var tb=document.getElementById('xf-shthumbs');tb.innerHTML='';
    (d.shots||[]).forEach(function(s){var a=document.createElement('a');a.href=s[0];a.target='_blank';
     var im=document.createElement('img');im.src=s[1];im.style.cssText='height:64px;border-radius:6px';a.appendChild(im);tb.appendChild(a);});
    // MediaInfo + 截图 自动追加到简介末尾(先去掉上一次追加的,避免重复)
    var dsc=document.getElementById('xf-d');
    var cut=dsc.value.indexOf('[quote]');
    var head=(cut>=0?dsc.value.slice(0,cut):dsc.value).trim();
    dsc.value=head+NL+NL+'[quote]'+(d.mediainfo||'')+'[/quote]'+NL+bb;
    msg.textContent='✅ 已生成 '+(d.shots||[]).length+' 张截图,并追加进简介';
   }).catch(function(){btn.disabled=false;msg.textContent='❌ 轮询出错';});
  })();
 }).catch(function(){btn.disabled=false;msg.textContent='❌ 启动失败';});
}
var _XFLBL={'xf-t':'复制主标题','xf-s':'复制副标题','xf-d':'复制简介','xf-mi':'复制MediaInfo','xf-sh':'复制截图'};
function xfCopy(id,btn){
 var ta=document.getElementById(id);ta.select();
 try{navigator.clipboard.writeText(ta.value);}catch(e){document.execCommand('copy');}
 btn.textContent='✅ 已复制';setTimeout(function(){btn.textContent=_XFLBL[id]||'复制';},1200);
}
showTab((location.hash||'#search').slice(1));
window.addEventListener('hashchange',function(){showTab(location.hash.slice(1)||'search');});

function dockify(el){
 if(!el)return;
 var raf=null,mx=null;
 function apply(){
  raf=null;
  var cards=el.children;
  for(var i=0;i<cards.length;i++){
   var c=cards[i],r=c.getBoundingClientRect();
   var d=Math.abs(mx-(r.left+r.width/2));
   var t=Math.max(0,1-d/260);        // 磁场半径260px
   t=t*t;                             // 平方衰减: 越近隆起越陡
   var s=0.94+(1.30-0.94)*t, y=-10*t; // 众星捧月: 正主1.30,远处集体退到0.94衬托
   c.style.transform='scale('+s.toFixed(3)+') translateY('+y.toFixed(1)+'px)';
   c.style.zIndex=t>0.4?2:1;
  }
 }
 el.addEventListener('mousemove',function(e){mx=e.clientX;if(!raf)raf=requestAnimationFrame(apply);});
 el.addEventListener('mouseleave',function(){
  var cards=el.children;
  for(var i=0;i<cards.length;i++){cards[i].style.transform='';cards[i].style.zIndex='';}
 });
}
function riverify(flow,speed){          // 让一条海报河流动起来(克隆无缝+悬停暂停+磁吸放大)
 if(!flow)return;
 var track=flow.firstElementChild;
 if(!track||track.children.length<2)return;
 var setW=track.scrollWidth+14;                       // 一组的宽度(含尾部间隙)
 if(track.scrollWidth>flow.clientWidth*0.9){          // 够长才流动+克隆无缝
  [].slice.call(track.children).forEach(function(c){track.appendChild(c.cloneNode(true));});
  var off=0,paused=false;
  flow.addEventListener('mouseenter',function(){paused=true;});
  flow.addEventListener('mouseleave',function(){paused=false;});
  (function step(){
   if(!paused){off+=(speed||0.4);if(off>=setW)off-=setW;track.style.transform='translateX('+(-off)+'px)';}
   requestAnimationFrame(step);
  })();
 }
 dockify(track);
}
riverify(document.querySelector('.rflow'),0.4);
for(var mi=0;mi<6;mi++){riverify(document.getElementById('mflow'+mi),0.32);}  // 每个分类一条河
var _libTot='共 {{MEDIACOUNT}} 项';
function libFind(){
 var q=document.getElementById('libq').value.trim().toLowerCase();
 var msg=document.getElementById('libmsg'),hit=0,tot=0;
 document.querySelectorAll('#tab-media .rcard').forEach(function(c){
  tot++;
  var nm=(c.querySelector('.rname')||{}).textContent||'';
  var t=(c.getAttribute('title')||'')+' '+nm;
  var ok=!q||t.toLowerCase().indexOf(q)>=0;
  c.classList.toggle('dim',!!q&&!ok);
  c.classList.toggle('hit',!!q&&ok);
  if(q&&ok)hit++;
 });
 if(!q){msg.textContent=_libTot;msg.style.color='';return;}
 msg.textContent=hit?'✅ 库里已经有了,别重复下':'❌ 库里没有,可以放心下';
 msg.style.color=hit?'#7dffb0':'var(--pop)';
}
function mToggle(i,btn){
 var fl=document.getElementById('mflow'+i),gd=document.getElementById('mgrid'+i);
 if(!fl||!gd)return;
 if(!btn.dataset.o)btn.dataset.o=btn.textContent;
 var open=gd.style.display!='none';
 gd.style.display=open?'none':'block';
 fl.style.display=open?'block':'none';
 btn.textContent=open?btn.dataset.o:'收起';
}
(function(){
 document.addEventListener('click',function(e){       // 全局委托:首页河 + 各分类河/网格都能点
  var c=e.target.closest?e.target.closest('.rcard'):null;
  if(!c||!c.dataset.tid||c.dataset.tid=='0')return;
  var img=c.querySelector('img'),nm=c.querySelector('.rname'),yr=c.querySelector('.ryear');
  document.getElementById('im-img').src=img?img.src:'';
  document.getElementById('im-t').textContent=(nm?nm.textContent:'')+(yr&&yr.textContent?' ('+yr.textContent+')':'');
  document.getElementById('im-p').textContent='简介加载中…';
  var a=document.getElementById('im-a');a.href='{{EMBYPUB}}';
  document.getElementById('im-ov').classList.add('show');       // 秒开,不等网络
  fetch('/api/overview?mt='+c.dataset.mt+'&id='+c.dataset.tid).then(r=>r.json()).then(function(d){
   if(!d.ok){document.getElementById('im-p').textContent='简介获取失败';return;}
   document.getElementById('im-p').textContent=d.overview;
   if(d.emby)a.href='{{EMBYPUB}}'+d.emby;                        // 直达该剧页面
  }).catch(function(){document.getElementById('im-p').textContent='简介获取失败';});
 });
})();
function fmtB(n,s){n=n||0;var u=['B','KB','MB','GB','TB'];var i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(n>=100||i==0?0:1)+u[i]+(s||'');}
function pollDash(){
 fetch('/api/dashboard').then(r=>r.json()).then(function(d){
  if(d.disk){document.getElementById('d-disk').textContent=fmtB(d.disk.free);
   document.getElementById('d-diskl').textContent='存储剩余 · 共'+fmtB(d.disk.total)+' 已用'+Math.round(d.disk.used/d.disk.total*100)+'%';}
  var down=((d.qb&&d.qb.down)||0)+((d.tr&&d.tr.down)||0);
  var up=((d.qb&&d.qb.up)||0)+((d.tr&&d.tr.up)||0);
  document.getElementById('d-speed').textContent='↓'+fmtB(down,'/s');
  document.getElementById('d-speedl').textContent='↑'+fmtB(up,'/s')+' 保种上传中';
  if(d.media){document.getElementById('d-media').textContent=(d.media.movie+d.media.tv+d.media.anime)+' 部';
   document.getElementById('d-medial').textContent='影视 · 另有 '+d.media.song+' 首音乐';}
  if(d.tr){document.getElementById('d-seed').textContent=d.tr.count;
   document.getElementById('d-seedl').textContent='做种中 · 今日已上传 '+fmtB(d.tr.up_today);}
 }).catch(()=>{});
}
pollDash();   // 首屏拉一次;之后的定时刷新由 startPolls 按 tab+可见性管理
function loadSettings(){
 fetch('/api/settings').then(r=>r.json()).then(function(d){
  var f=document.getElementById('setform');if(!f||!d.ok)return;
  f.innerHTML='';var unset=0;
  d.groups.forEach(function(g){
   var h=document.createElement('div');h.className='sgrp';h.textContent=g.name;f.appendChild(h);
   g.fields.forEach(function(x){
    var row=document.createElement('div');row.className='srow';
    var lb=document.createElement('label');lb.textContent=x.label;row.appendChild(lb);
    var inp=document.createElement('input');inp.id='set-'+x.key;inp.value=x.value;
    if(x.secret)inp.type='password';
    if(x.hint)inp.placeholder=x.hint;
    row.appendChild(inp);f.appendChild(row);
    if(x.hint){var ht=document.createElement('div');ht.className='shint';ht.textContent=x.hint;/*占位提示已够,略*/}
    if(!x.value&&(x.key=='PROWLARR_KEY'||x.key=='TR_URL'||x.key=='QB_URL'))unset++;
   });
  });
  if(unset>0){var b=document.querySelector('.tabbtn[data-t="setup"]');if(b)b.textContent='⚙️ 设置 ❗';}
 });
}
loadSettings();
function saveSettings(btn){
 btn.disabled=true;var d={};
 document.querySelectorAll('#setform input').forEach(function(i){d[i.id.slice(4)]=i.value;});
 fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
 .then(r=>r.json()).then(function(r){
  btn.disabled=false;
  document.getElementById('set-msg').textContent=r.ok?'✅ 已保存并热生效':'❌ '+(r.err||'保存失败');
  setTimeout(()=>{document.getElementById('set-msg').textContent='';},4000);
 }).catch(()=>{btn.disabled=false;});
}
function testAll(btn){
 btn.disabled=true;
 var out=document.getElementById('testout');out.innerHTML='';
 var svcs=[['tr','Transmission'],['qb','qBittorrent'],['prowlarr','Prowlarr'],['tmdb','TMDB'],['emby','Emby'],['lrcapi','LrcApi'],['wecom','企业微信']];
 var done=0;
 svcs.forEach(function(sv){
  var line=document.createElement('div');line.textContent='⏳ '+sv[1]+' 测试中…';out.appendChild(line);
  fetch('/api/test?svc='+sv[0]).then(r=>r.json()).then(function(d){
   line.textContent=(d.ok?'✅ ':'❌ ')+sv[1]+': '+d.msg;
   line.style.color=d.ok?'var(--ok)':'var(--err)';
   if(++done==svcs.length)btn.disabled=false;
  }).catch(function(){line.textContent='❌ '+sv[1]+': 请求失败';line.style.color='var(--err)';if(++done==svcs.length)btn.disabled=false;});
 });
}
function research(h,el){
 var inp=el.parentNode.querySelector('input');var q=inp.value.trim();
 if(!q){inp.focus();return;}
 clearTimeout(_t);el.disabled=true;el.textContent='搜索中';
 fetch('/research?hash='+encodeURIComponent(h)+'&q='+encodeURIComponent(q))
  .then(r=>r.json()).then(d=>{toast('已触发重搜 ['+q+']，稍后刷新查看');setTimeout(()=>location.reload(),8000);})
  .catch(e=>{toast('触发失败');el.disabled=false;el.textContent='重搜';});
}
function toast(m){var t=document.getElementById('toast');t.textContent=m;t.className='show';setTimeout(()=>t.className='',3000);}
function mkRow(x,rs){
 var tr=document.createElement('tr');
 var c1=document.createElement('td');c1.className='sname';c1.title=x.title+'（点击打开站点种子详情页）';
 if(x.info){var a=document.createElement('a');a.href=x.info;a.target='_blank';a.rel='noreferrer';a.textContent=x.title;a.style.color='var(--fg)';c1.appendChild(a);}
 else{c1.textContent=x.title;}
 if(x.alts&&x.alts.length){var mb=document.createElement('span');mb.className='mut';
  mb.style.cssText='font-size:11px;margin-left:8px;white-space:nowrap';
  mb.textContent='+'+x.alts.length+' 站';mb.title='另有 '+x.alts.length+' 个站有同一个发布，已合并';c1.appendChild(mb);}
 if(x.zhkind){var zb=document.createElement('span');
  zb.style.cssText='font-size:11px;margin-left:8px;padding:1px 7px;border-radius:9px;background:rgba(64,190,120,.18);color:#3fbf6f;white-space:nowrap';
  zb.textContent=x.zhkind;
  zb.title=(x.zhrank>1?'种子名里明写了中文音轨（不是字幕）':'多条音轨，很可能含国语但种子名没写明，下载前建议确认');
  if(x.zhrank>2)zb.style.cssText+=';font-weight:600';
  if(x.zhrank<2)zb.style.cssText+=';background:rgba(200,160,60,.18);color:#c9a13f';
  c1.appendChild(zb);}
 var cq=document.createElement('td');cq.className='mut';cq.style.cssText='font-size:12px;white-space:nowrap';
 cq.textContent=((x.res?x.res+'p':'')+' '+(x.src||'')).trim()||'—';
 var cg=document.createElement('td');cg.className='mut';cg.style.fontSize='12px';cg.textContent=x.grp||'—';
 var c2=document.createElement('td');var sp=document.createElement('span');sp.className='src';sp.textContent=x.site;c2.appendChild(sp);
 var c3=document.createElement('td');c3.className='r';c3.textContent=x.sizeh;
 var c4=document.createElement('td');c4.className='r';c4.textContent=x.seeders;
 var c5=document.createElement('td');var b=document.createElement('button');b.className='dlbtn';b.textContent='下载';b.onclick=function(){dl(b,x,rs);};c5.appendChild(b);
 tr.appendChild(c1);tr.appendChild(cq);tr.appendChild(cg);tr.appendChild(c2);tr.appendChild(c3);tr.appendChild(c4);tr.appendChild(c5);
 return tr;
}
/* 一部多季的剧,种子是几十条乱序堆在一起的。按季分段、每季内把「推荐制作组」顶到最前,
   挑起来才不用逐条读文件名。推荐组 = 覆盖季数最多的那个组(平手比总做种数) ——
   同一个组的画质命名习惯一致,混着下以后 Emby 里会很难看。 */
function mkTable(rs){
 var wrap=document.createElement('div');
 var packs=[],seasons={},rest=[];
 rs.forEach(function(x){
  if(x.pack)packs.push(x);
  else if(x.ss){if(!seasons[x.ss])seasons[x.ss]=[];seasons[x.ss].push(x);}
  else rest.push(x);
 });
 var keys=Object.keys(seasons).map(Number).sort(function(a,b){return a-b;});
 var grouped=keys.length>=2||(packs.length>0&&keys.length>=1);
 var cov={},tot={},best='',bn=0;
 if(grouped){
  keys.forEach(function(k){
   var seen={};
   seasons[k].forEach(function(x){
    if(!x.grp)return;
    tot[x.grp]=(tot[x.grp]||0)+x.seeders;
    if(!seen[x.grp]){seen[x.grp]=1;cov[x.grp]=(cov[x.grp]||0)+1;}
   });
  });
  Object.keys(cov).forEach(function(g){
   if(cov[g]>bn||(cov[g]==bn&&(tot[g]||0)>(tot[best]||0))){bn=cov[g];best=g;}
  });
 }
 var only=false,zhonly=false;
 var nzh=rs.filter(function(x){return x.zhaud;}).length;
 function zcmp(a,b){return (b.zhrank||0)-(a.zhrank||0);}
 function build(){
  var tbl=document.createElement('table');
  var hd=document.createElement('tr');
  hd.innerHTML='<th>标题</th><th>画质</th><th>制作组</th><th>站点</th><th class=r>大小</th><th class=r>做种</th><th></th>';
  tbl.appendChild(hd);
  if(!grouped){
   rs.filter(function(x){return !zhonly||x.zhaud;})
     .slice().sort(function(a,b){return zcmp(a,b)||b.seeders-a.seeders;})
     .forEach(function(x){tbl.appendChild(mkRow(x,rs));});
   return tbl;}
  function sec(label,items){
   var list=items.filter(function(x){return (!only||x.grp==best)&&(!zhonly||x.zhaud);});
   if(!list.length)return;
   var tr=document.createElement('tr'),td=document.createElement('td');
   td.colSpan=7;td.style.cssText='padding:9px 16px 3px;font-weight:600;font-size:13px;opacity:.8';
   td.textContent=label+'（'+list.length+'）';tr.appendChild(td);tbl.appendChild(tr);
   list.slice().sort(function(a,b){
    var pa=(best&&a.grp==best)?0:1,pb=(best&&b.grp==best)?0:1;
    return zcmp(a,b)||pa-pb||b.seeders-a.seeders;
   }).forEach(function(x){tbl.appendChild(mkRow(x,rs));});
  }
  sec('📦 合集 / 跨季包',packs);
  keys.forEach(function(k){sec('第 '+k+' 季',seasons[k]);});
  sec('· 判不出季的',rest);
  return tbl;
 }
 if(grouped&&best){
  var tip=document.createElement('div');tip.className='mut';
  tip.style.cssText='padding:4px 16px 6px;font-size:12px';
  var ts=document.createElement('span');
  ts.textContent='建议统一用 '+best+'（覆盖 '+bn+'/'+keys.length+' 季，已顶到每季最前）　';
  var tb=document.createElement('button');tb.className='dlbtn';
  tb.style.cssText='padding:2px 9px;font-size:12px';tb.textContent='只看 '+best;
  tb.onclick=function(){
   only=!only;tb.textContent=(only?'看全部':'只看 '+best);
   wrap.replaceChild(build(),wrap.lastChild);
  };
  tip.appendChild(ts);tip.appendChild(tb);wrap.appendChild(tip);
 }
 var zt=document.createElement('div');zt.className='mut';
 zt.style.cssText='padding:4px 16px 8px;font-size:12px';
 if(nzh){
  var zs=document.createElement('span');
  zs.textContent='🔊 有 '+nzh+' 个带中文音轨，已排在前面　';
  var zbn=document.createElement('button');zbn.className='dlbtn';
  zbn.style.cssText='padding:2px 9px;font-size:12px';zbn.textContent='只看中文音轨';
  zbn.onclick=function(){zhonly=!zhonly;zbn.textContent=(zhonly?'看全部':'只看中文音轨');
   wrap.replaceChild(build(),wrap.lastChild);};
  zt.appendChild(zs);zt.appendChild(zbn);
 }else{
  zt.textContent='🔇 这批结果里没有一个标了中文音轨 —— 多半是这部片子本来就没有国配，不是没搜到';
 }
 wrap.appendChild(zt);
 wrap.appendChild(build());
 return wrap;
}
var _sd=null,_sf='';   /* _sf = 当前这批结果是用哪个类型范围搜回来的 */
var FCN={movie:'电影',tv:'电视剧',anime:'动漫',book:'漫画/书',music:'音乐'};
function tgF(el){
 var was=el.classList.contains('on');
 document.querySelectorAll('.fpill').forEach(e=>e.classList.remove('on'));
 if(!was)el.classList.add('on');
 var f=activeF();
 if(!_sd)return;                      /* 还没搜过:点类型只是设定下次搜索的范围 */
 if(f==_sf||_sf==''){renderWall();return;}   /* 上次是全站搜,手里有全类型结果,本地过滤就够 */
 /* 上次是限定搜,现在换类型 —— 新类型的种子压根没去站上搜过,本地过滤只会得到一片空白,必须重搜 */
 var q=document.getElementById('q').value.trim();
 if(!q){renderWall();return;}
 toast(f?('改搜「'+FCN[f]+'」,重新找源…'):'改为全站搜,重新找源…');
 doSearch();
}
function activeF(){var e=document.querySelector('.fpill.on');return e?e.dataset.f:'';}
function renderWall(){
 var d=_sd,box=document.getElementById('sresult'),f=activeF();
 var gs=(d.groups||[]).filter(g=>!f||g.cat==f);
 var ot=(d.other||[]).filter(x=>!f||x.cat==f);
 box.innerHTML='';
 var _bk=mkBack();if(_bk)box.appendChild(_bk);
 if(!gs.length&&!ot.length){
  var _e=document.createElement('div');_e.className='mut';_e.style.padding='10px 16px';
  _e.textContent='该类型下没有结果，点掉类型看全部';box.appendChild(_e);return;}
 var wall=document.createElement('div');wall.className='wall';
 var sel=document.createElement('div');sel.id='selres';
 function pick(card,rs,label,quiet){
  document.querySelectorAll('.pcard').forEach(c=>c.classList.remove('sel'));
  card.classList.add('sel');sel.innerHTML='';
  var hd=document.createElement('div');hd.className='gt';hd.style.padding='6px 16px 0';
  hd.textContent=label+' — 选择站点下载';sel.appendChild(hd);
  sel.appendChild(mkTable(rs));
  if(!quiet)sel.scrollIntoView({behavior:'smooth',block:'nearest'});
 }
 var CN={movie:'电影',tv:'剧集',anime:'动漫',music:'音乐'};
 var anchorCard=null,anchorG=null;
 gs.forEach(function(g){
  var card=document.createElement('div');card.className='pcard';
  if(g.owned)card.classList.add('owned');
  if(g.anchor){card.classList.add('anchor');if(!anchorCard){anchorCard=card;anchorG=g;}}
  var pwrap=document.createElement('div');pwrap.style.position='relative';
  if(g.posterurl){var im=document.createElement('img');im.className='pw';im.loading='lazy';im.src=g.posterurl;pwrap.appendChild(im);}
  else if(g.poster){var im=document.createElement('img');im.className='pw';im.loading='lazy';im.src='/api/poster?p='+encodeURIComponent(g.poster);pwrap.appendChild(im);}
  else{var ph=document.createElement('div');ph.className='ph';ph.textContent=g.cat=='music'?'🎵':(g.cat=='anime'?'🎌':(g.mtype=='tv'?'📺':'🎬'));pwrap.appendChild(ph);}
  if(g.owned){var ob=document.createElement('div');ob.className='ownbadge';ob.textContent='✓ '+g.owned;pwrap.appendChild(ob);}
  if(g.anchor){var hb=document.createElement('div');hb.className='hitbadge';hb.textContent='🎯 你找的';pwrap.appendChild(hb);}
  card.appendChild(pwrap);
  var nm=document.createElement('div');nm.className='pname';nm.textContent=g.name+(g.year?' ('+g.year+')':'');card.appendChild(nm);
  var mt=document.createElement('div');mt.className='pmeta';mt.textContent=(CN[g.cat]||CN[g.mtype])+' · '+g.results.length+' 个种 · 最高做种 '+(g.results[0]?g.results[0].seeders:0);card.appendChild(mt);
  card.title=g.overview||'';
  card.onclick=function(){pick(card,g.results,g.name+(g.year?' ('+g.year+')':''));};
  wall.appendChild(card);
 });
 if(ot.length){
  var card=document.createElement('div');card.className='pcard';
  var ph=document.createElement('div');ph.className='ph';ph.textContent='🧩';card.appendChild(ph);
  var nm=document.createElement('div');nm.className='pname';nm.textContent='未识别 / 其他';card.appendChild(nm);
  var mt=document.createElement('div');mt.className='pmeta';mt.textContent=ot.length+' 个种';card.appendChild(mt);
  card.onclick=function(){pick(card,ot,'未识别 / 其他');};
  wall.appendChild(card);
 }
 box.appendChild(wall);box.appendChild(sel);
 /* 锚定命中的那部直接把种子列表摊开:点了片单/搜了片名,下一步就该是选站下载,不该再点一次海报 */
 if(anchorCard&&anchorG&&(anchorG.results||[]).length)
  pick(anchorCard,anchorG.results,anchorG.name+(anchorG.year?' ('+anchorG.year+')':''),true);
}
/* ---- 豆瓣片单货架 ---- */
/* 第三项 = 搜索时的类型限定:榜单自己就知道是剧还是电影,没必要再去全类别捞一遍 */
var DBS=[['cn_tv','🇨🇳 国产剧','tv'],['us_tv','🇺🇸 美剧','tv'],['uk_tv','🇬🇧 英剧','tv'],['jp_tv','🇯🇵 日剧','tv'],
 ['kr_tv','🇰🇷 韩剧','tv'],['jp_anime','🎌 日本动画','anime'],['doc','📽 纪录片','tv'],['classic','🎬 经典电影','movie']];
var _dbcol='cn_tv',_dbtype='tv',_dbstart=0,_dbback=false;
function dbInit(){
 var tb=document.getElementById('dbtabs');if(!tb)return;
 DBS.forEach(function(c){
  var e=document.createElement('span');e.className='dbtab';e.textContent=c[1];e.dataset.c=c[0];
  e.onclick=function(){if(_dbcol==c[0])return;_dbcol=c[0];_dbtype=c[2]||'';_dbstart=0;dbLoad();};
  tb.appendChild(e);
 });
 dbLoad();
}
function dbMsg(txt){
 var tr=document.getElementById('dbtrack');tr.innerHTML='';
 var m=document.createElement('span');m.className='mut';m.style.padding='12px';m.textContent=txt;tr.appendChild(m);
}
function dbLoad(){
 document.querySelectorAll('.dbtab').forEach(function(e){e.classList.toggle('on',e.dataset.c==_dbcol);});
 dbMsg('载入中…');
 fetch('/api/douban?col='+_dbcol+'&start='+_dbstart).then(r=>r.json()).then(function(d){
  var src=document.getElementById('dbsrc');
  if(!d.ok||!(d.items||[]).length){
   dbMsg('这个榜单暂时取不到（'+((d&&d.err)||'豆瓣没响应')+'），换个榜单或稍后再看');
   if(src)src.textContent='';return;}
  if(src)src.textContent=(d.src=='tmdb'?'· 豆瓣没连上，先用 TMDB 高分榜顶着':'· 来自豆瓣');
  var tr=document.getElementById('dbtrack');tr.innerHTML='';
  d.items.forEach(function(it){tr.appendChild(dbCard(it));});
  tr.parentNode.scrollLeft=0;
 }).catch(function(){dbMsg('榜单请求出错，检查网络或豆瓣代理设置');});
}
function dbMore(){_dbstart+=24;if(_dbstart>168)_dbstart=0;dbLoad();}
function dbCard(it){
 var d=document.createElement('div');d.className='dbitem';
 var w=document.createElement('div');w.style.position='relative';
 var src=it.cover?('/api/dbimg?u='+encodeURIComponent(it.cover))
        :(it.tmdb_poster?('/api/poster?p='+encodeURIComponent(it.tmdb_poster)):'');
 /* 这里刻意不用 loading=lazy:横滚容器里的懒加载在浏览器看来「永远没进视口」,
    24 张海报会全部停在 0x0 不发请求(实测 currentSrc 一直是空的,换成 eager 立刻就出图)。
    货架固定 24 张、服务端已预热到本地磁盘,直接加载最稳。 */
 if(src){var im=document.createElement('img');im.className='pw';im.alt=it.title;im.src=src;w.appendChild(im);}
 else{var ph=document.createElement('div');ph.className='ph';ph.textContent='🎬';w.appendChild(ph);}
 if(it.rate){var rt=document.createElement('div');rt.className='dbrate';rt.textContent='★ '+it.rate;w.appendChild(rt);}
 if(it.owned){var ob=document.createElement('div');ob.className='ownbadge';ob.textContent='✓ 已有';w.appendChild(ob);}
 d.appendChild(w);
 var nm=document.createElement('div');nm.className='pname';nm.textContent=it.title;d.appendChild(nm);
 var mt=document.createElement('div');mt.className='pmeta';mt.textContent=(it.year||'')+(it.owned?' · 库里有了':'');d.appendChild(mt);
 d.title=it.sub||it.title;
 d.onclick=function(){dbPick(it);};
 return d;
}
/* 榜单里的剧常常按季收录(「老友记 第十季」「绝命毒师  第五季」)。
   点进来八成是想要整部剧,所以砍掉季号搜全剧;季号一砍,那个年份也是这一季的年份,一并丢掉,
   否则会拿第十季的年份去卡全剧的种子。不带季号的条目(电影/单本剧)才保留年份限定。 */
function dbQuery(it){
 var t=(it.title||'').split('  ').join(' ').trim(),season=false;
 var p=t.indexOf(' 第');
 if(p>0&&t.charAt(t.length-1)=='季'){t=t.substring(0,p).trim();season=true;}
 return t+((!season&&it.year)?' '+it.year:'');
}
function dbPick(it){
 var box=document.getElementById('sresult');
 _backto=null;_dbback=true;_pmode=false;
 var pp=document.getElementById('ppill');if(pp)pp.classList.remove('on');
 var q=dbQuery(it);
 document.getElementById('q').value=q;
 /* 榜单已经告诉我们是剧还是电影,把类型按钮也点上,搜索范围和界面保持一致 */
 _sf=_dbtype;
 document.querySelectorAll('.fpill').forEach(function(e){e.classList.toggle('on',e.dataset.f==_dbtype);});
 box.innerHTML=VOYAGE;
 box.scrollIntoView({behavior:'smooth',block:'start'});
 fetch('/api/search2?q='+encodeURIComponent(q)+'&f='+_sf).then(r=>r.json()).then(function(d){
  if(!d.ok){box.innerHTML='<div class=mut style="padding:10px 16px">提交失败：'+(d.err||'')+'</div>';return;}
  pollJob(d.id,box,Date.now());
 }).catch(function(){box.innerHTML='<div class=mut style="padding:10px 16px">提交出错</div>';});
}
/* ---- 找人:演员/导演片单。全程 DOM API 拼,不拼 HTML 字符串 ---- */
var _pmode=false,_pd=null,_pmore=false,_backto=null;
function tgP(el){
 _pmode=!_pmode;
 el.classList.toggle('on',_pmode);
 var q=document.getElementById('q');
 q.placeholder=_pmode?'演员 / 导演名字,回车列出他的全部作品':'片名 / 剧名 / 专辑,回车即搜';
 q.focus();
}
function pmsg(box,txt){
 box.innerHTML='';
 var c=document.createElement('div');c.className='card';
 var m=document.createElement('div');m.className='mut';m.textContent=txt;
 c.appendChild(m);box.appendChild(c);
}
function avatar(path,ph){
 if(path){var im=document.createElement('img');im.loading='lazy';
  im.src='/api/poster?p='+encodeURIComponent(path);return im;}
 var d=document.createElement('div');d.className='np';d.textContent=ph;return d;
}
function doPerson(q){
 var box=document.getElementById('sresult');
 _pd=null;_backto=null;_dbback=false;
 pmsg(box,'正在 TMDB 查「'+q+'」的资料…');
 fetch('/api/person?q='+encodeURIComponent(q)).then(r=>r.json()).then(function(d){
  if(!d.ok){pmsg(box,'查不了：'+(d.err||''));return;}
  var l=d.list||[];
  if(!l.length){pmsg(box,'没找到叫「'+q+'」的人。TMDB 对冷门或港台演员有时只收英文名,试试拼音/英文名。');return;}
  if(l.length===1){loadCredits(l[0].id);return;}
  renderPersons(l,q);
 }).catch(function(){pmsg(box,'请求出错');});
}
function renderPersons(l,q){
 var box=document.getElementById('sresult');box.innerHTML='';
 var c=document.createElement('div');c.className='card';
 var h=document.createElement('h2');h.textContent='👤 叫「'+q+'」的有 '+l.length+' 位,你要找哪个?';
 c.appendChild(h);
 l.forEach(function(p){
  var r=document.createElement('div');r.className='perscard';
  r.appendChild(avatar(p.profile,'👤'));
  var t=document.createElement('div');t.style.minWidth='0';
  var n=document.createElement('div');n.style.fontWeight='700';
  n.textContent=p.name+(p.dept?'  ·  '+p.dept:'');t.appendChild(n);
  var s=document.createElement('div');s.className='mut';s.style.fontSize='12px';
  s.textContent=p.known||'（TMDB 没列代表作）';t.appendChild(s);
  r.appendChild(t);
  r.onclick=function(){loadCredits(p.id);};
  c.appendChild(r);
 });
 box.appendChild(c);
}
function loadCredits(pid){
 var box=document.getElementById('sresult');
 pmsg(box,'正在拉片单…');
 fetch('/api/personcredits?id='+encodeURIComponent(pid)).then(r=>r.json()).then(function(d){
  if(!d.ok){pmsg(box,'取片单失败：'+(d.err||''));return;}
  _pd=d;_pmore=false;renderCredits();
 }).catch(function(){pmsg(box,'请求出错');});
}
function renderCredits(){
 var d=_pd,box=document.getElementById('sresult');if(!d)return;
 box.innerHTML='';_backto=null;
 var c=document.createElement('div');c.className='card';
 var hero=document.createElement('div');hero.className='phero';
 hero.appendChild(avatar(d.person.profile,'👤'));
 var ht=document.createElement('div');ht.style.minWidth='0';
 var hn=document.createElement('div');hn.style.fontSize='19px';hn.style.fontWeight='800';
 hn.textContent=d.person.name+(d.person.dept?'  ·  '+d.person.dept:'');ht.appendChild(hn);
 var hs=document.createElement('div');hs.className='mut';hs.style.fontSize='12px';hs.style.marginTop='3px';
 hs.textContent='共 '+d.stat.total+' 部作品 · 电影 '+d.stat.movie+' · 电视剧 '+d.stat.tv+' · 已入库 '+d.stat.owned+' 部';
 ht.appendChild(hs);hero.appendChild(ht);c.appendChild(hero);
 var hint=document.createElement('div');hint.className='mut';hint.style.fontSize='12px';
 hint.textContent='绿框=库里已有 · 点没入库的海报才去各站搜种(一次只搜一部,不打爆站)';
 c.appendChild(hint);box.appendChild(c);
 [['movie','🎬 电影'],['tv','📺 电视剧']].forEach(function(pair){
  var arr=d[pair[0]]||[];if(!arr.length)return;
  var show=_pmore?arr:arr.filter(function(x){return !x.minor;});
  var hid=arr.length-show.length;
  if(!show.length){show=arr;hid=0;}
  var g=document.createElement('div');g.className='pgrp';
  var gt=document.createElement('span');gt.textContent=pair[1];g.appendChild(gt);
  var gc=document.createElement('span');gc.className='c';
  gc.textContent=arr.length+' 部 · 已入库 '+arr.filter(function(x){return x.owned;}).length;
  g.appendChild(gc);box.appendChild(g);
  var wall=document.createElement('div');wall.className='wall';
  show.forEach(function(it){wall.appendChild(workCard(it));});
  box.appendChild(wall);
  if(hid>0){
   var b=document.createElement('button');b.className='morebtn';
   b.textContent='＋ 展开 '+hid+' 部冷门/客串';
   b.onclick=function(){_pmore=true;renderCredits();};
   box.appendChild(b);
  }
 });
}
function workCard(it){
 var card=document.createElement('div');card.className='pcard';
 if(it.owned)card.classList.add('owned');
 var pw=document.createElement('div');pw.style.position='relative';
 if(it.poster){var im=document.createElement('img');im.className='pw';im.loading='lazy';
  im.src='/api/poster?p='+encodeURIComponent(it.poster);pw.appendChild(im);}
 else{var ph=document.createElement('div');ph.className='ph';
  ph.textContent=it.mtype==='tv'?'📺':'🎬';pw.appendChild(ph);}
 if(it.owned){var ob=document.createElement('div');ob.className='ownbadge';
  ob.textContent='✓ '+it.owned;pw.appendChild(ob);}
 card.appendChild(pw);
 var nm=document.createElement('div');nm.className='pname';
 nm.textContent=it.name+(it.year?' ('+it.year+')':'');card.appendChild(nm);
 var mt=document.createElement('div');mt.className='pmeta';
 mt.textContent=(it.mtype==='tv'?'剧集':'电影')+(it.eps?' · '+it.eps+' 集':'');card.appendChild(mt);
 if(it.role){var rl=document.createElement('div');rl.className='prole';
  rl.textContent=it.role;card.appendChild(rl);}
 card.title=it.overview||'';
 card.onclick=function(){searchWork(it);};
 return card;
}
function searchWork(it){
 var box=document.getElementById('sresult');
 box.innerHTML=VOYAGE;
 _backto=(_pd&&_pd.person)?_pd.person.name:'';
 var q=it.name+(it.year?' '+it.year:'');
 _sf=(it.mtype=='tv')?'tv':(it.mtype=='movie'?'movie':'');   /* 片单里已知是剧还是影,直接限定 */
 fetch('/api/search2?q='+encodeURIComponent(q)+'&f='+_sf).then(r=>r.json()).then(function(d){
  if(!d.ok){pmsg(box,'提交失败：'+(d.err||''));return;}
  pollJob(d.id,box,Date.now());
 }).catch(function(){pmsg(box,'提交出错');});
}
function mkBack(){
 if(_backto){
  var b=document.createElement('button');b.className='morebtn';
  b.textContent='← 返回 '+_backto+' 的作品';
  b.onclick=function(){renderCredits();};
  return b;}
 if(_dbback){
  var b2=document.createElement('button');b2.className='morebtn';
  b2.textContent='← 返回豆瓣片单';
  b2.onclick=function(){_dbback=false;document.getElementById('sresult').innerHTML='';
   document.getElementById('dbshelf').scrollIntoView({behavior:'smooth',block:'center'});};
  return b2;}
 return null;
}
var _amode=false;
function tgA(el){
 _amode=!_amode; el.classList.toggle('on',_amode);
 if(_amode&&_pmode){_pmode=false;var p=document.getElementById('ppill');if(p)p.classList.remove('on');}
 var q=document.getElementById('q');
 q.placeholder=_amode?'歌手名字，回车列出全部专辑（按年代排序 + 经典度推荐）':'片名 / 剧名 / 专辑,回车即搜';
 q.focus();
}
function doArtist(a){
 var box=document.getElementById('sresult');
 _backto=null;_dbback=false;_noReload=true;
 box.innerHTML='<div class="voy card"><div class=mut style="padding:16px 20px" id=arTip>正在问 iTunes 要「'+a+'」的专辑年表…</div></div>';
 fetch('/api/artist?q='+encodeURIComponent(a)).then(r=>r.json()).then(function(d){
  if(!d.ok){box.innerHTML='<div class=mut style="padding:10px 16px">'+(d.err||'提交失败')+'</div>';return;}
  artPoll(d.id,box,Date.now());
 }).catch(function(){box.innerHTML='<div class=mut style="padding:10px 16px">提交出错</div>';});
}
function artPoll(id,box,t0){
 fetch('/api/artstat?id='+id).then(r=>r.json()).then(function(j){
  if(!j.ok){box.innerHTML='<div class=mut style="padding:10px 16px">'+(j.err||'任务丢失')+'</div>';return;}
  if(!j.fin){
   var e=document.getElementById('arTip');
   if(e)e.textContent=((j.log||[]).slice(-1)[0]||'搜索中')+' · 已 '+Math.round((Date.now()-t0)/1000)+' 秒';
   setTimeout(function(){artPoll(id,box,t0);},1200);return;
  }
  artRender(j.result||{},box);
 }).catch(function(){setTimeout(function(){artPoll(id,box,t0);},2500);});
}
function artRender(d,box){
 box.innerHTML='';
 if(!d.ok){box.innerHTML='<div class=mut style="padding:10px 16px">'+(d.err||'失败')+'</div>';return;}
 var rows=d.rows||[];
 if(!rows.length){
  box.innerHTML='<div class=empty><div class=ei>🎵</div><div class=et>没找到「'+d.artist+'」的专辑</div>'+
   '<div>'+(d.nodisc?'iTunes 上查不到这个歌手，试试英文名或换个写法':'站上没有这些专辑的无损资源')+'</div></div>';
  return;}
 var card=document.createElement('div');card.className='card';
 var h=document.createElement('h2');
 h.textContent='🎵 '+d.artist+' — '+rows.length+' 张专辑（按发行年代排序）';
 var sub=document.createElement('span');sub.className='mut';sub.style.fontWeight='400';
 sub.textContent=' · ⭐ 推荐 = 正规专辑 + 分轨 + 做种最多；精选/Live 自动往后排';
 h.appendChild(sub);card.appendChild(h);
 var wrap=document.createElement('div');wrap.style.padding='0 16px 12px';
 var tb=document.createElement('table');
 var hr=document.createElement('tr');
 ['','年份','专辑','曲目','选中的资源','站点','大小','做种'].forEach(function(x){
  var th=document.createElement('th');th.textContent=x;hr.appendChild(th);});
 tb.appendChild(hr);
 rows.forEach(function(r,i){
  var tr=document.createElement('tr');
  var c0=document.createElement('td');
  var cb=document.createElement('input');cb.type='checkbox';cb.className='asel';cb.dataset.i=i;
  cb.checked=!!r.rec;c0.appendChild(cb);
  var c1=document.createElement('td');c1.className='mut';c1.textContent=r.year||'';
  var c2=document.createElement('td');
  c2.textContent=r.album;
  if(r.rec){var st=document.createElement('span');st.style.cssText='color:#FFD400;margin-left:4px';st.textContent='⭐';c2.appendChild(st);}
  if(!r.studio){var nb=document.createElement('span');nb.className='mut';nb.style.fontSize='11px';
   nb.textContent=' 精选/Live';c2.appendChild(nb);}
  var c3=document.createElement('td');c3.className='mut r';c3.textContent=r.tracks||'';
  var c4=document.createElement('td');c4.className='sname';c4.title=r.rel;c4.textContent=r.rel;
  if(!r.split){var w=document.createElement('span');w.style.cssText='color:#FFD400;margin-left:4px';
   w.textContent='[整轨]';c4.appendChild(w);}
  var c5=document.createElement('td');var sp=document.createElement('span');sp.className='src';sp.textContent=r.site;c5.appendChild(sp);
  var c6=document.createElement('td');c6.className='r';c6.textContent=r.sizeh;
  var c7=document.createElement('td');c7.className='r';c7.textContent=r.seeders;
  [c0,c1,c2,c3,c4,c5,c6,c7].forEach(function(x){tr.appendChild(x);});
  tb.appendChild(tr);
 });
 wrap.appendChild(tb);
 var bar=document.createElement('div');bar.style.marginTop='12px';
 var all=document.createElement('button');all.className='dlbtn';all.style.marginRight='8px';
 all.textContent='全选/全不选';
 all.onclick=function(){var cs=document.querySelectorAll('.asel');var v=!cs[0].checked;
  cs.forEach(function(x){x.checked=v;});};
 var rec=document.createElement('button');rec.className='dlbtn';rec.style.marginRight='8px';
 rec.textContent='只选推荐';
 rec.onclick=function(){document.querySelectorAll('.asel').forEach(function(x){
  x.checked=!!rows[parseInt(x.dataset.i)].rec;});};
 var go=document.createElement('button');go.className='dlbtn';go.textContent='推送到 qb 下载';
 go.onclick=function(){
  var picked=[];document.querySelectorAll('.asel').forEach(function(x){
   if(x.checked){var r=rows[parseInt(x.dataset.i)];picked.push({title:r.album,rel:r.rel,url:r.url});}});
  if(!picked.length){toast('一张都没选');return;}
  go.disabled=true;go.textContent='推送中…（每张间隔 2 秒）';
  fetch('/api/batchgo',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({rows:picked,cat:'音乐'})}).then(r=>r.json()).then(function(x){
    go.textContent=x.ok?('✅ 已推送 '+x.added+' 张'+(x.failed?('，失败 '+x.failed):'')):'推送失败';
    toast(x.ok?('已推送 '+x.added+' 张到 qb'):(x.err||'失败'));
   }).catch(function(){go.disabled=false;go.textContent='推送到 qb 下载';toast('推送出错');});
 };
 bar.appendChild(all);bar.appendChild(rec);bar.appendChild(go);wrap.appendChild(bar);
 var note=document.createElement('div');note.className='mut';note.style.cssText='margin-top:6px;font-size:12px';
 note.textContent='默认已勾选推荐的。下完自动进 Navidrome：原目录结构硬链接 → LrcApi 抓歌词 → iTunes 抓封面。[整轨] 的一张专辑是一个大文件，Navidrome 认不出单曲，尽量别选。';
 wrap.appendChild(note);
 card.appendChild(wrap);box.appendChild(card);
}
function doSearch(){
 var q=document.getElementById('q').value.trim();if(!q)return;
 clearTimeout(_t);
 var box=document.getElementById('sresult');
 if(_amode){doArtist(q);return;}
 if(_pmode){doPerson(q);return;}
 _backto=null;_dbback=false;
 box.innerHTML=VOYAGE;
 _sf=activeF();          /* 记下本次搜索的类型范围,后面点类型时才知道要不要重搜 */
 fetch('/api/search2?q='+encodeURIComponent(q)+'&f='+_sf).then(r=>r.json()).then(function(d){
  if(!d.ok){box.innerHTML='<div class=mut style="padding:10px 16px">提交失败：'+(d.err||'')+'</div>';return;}
  pollJob(d.id,box,Date.now());
 }).catch(e=>{box.innerHTML='<div class=mut style="padding:10px 16px">提交出错</div>';});
}
var VOYAGE=''
+'<div class="voy card"><div class=voysea>'
+'<div class=voyboat><svg viewBox="0 0 46 46" width="38" height="38">'
+'<path d="M22.5 3 C22.5 3 12.5 15 9.5 27 L22.5 27 Z" fill="#ffffff" opacity=".96"/>'
+'<path d="M25.5 11 C25.5 11 32 18 35 27 L25.5 27 Z" fill="#CFE0FF" opacity=".9"/>'
+'<path d="M23.6 2 C24.2 2 24.6 2.5 24.5 3.1 L24.2 28 L22.8 28 L22.6 3.1 C22.6 2.5 23 2 23.6 2 Z" fill="#fff"/>'
+'<path d="M5 30 C5 30 14 33 23 33 C32 33 41 30 41 30 C41 30 37.5 39.5 32 41 C27 42.4 19 42.4 14 41 C8.5 39.5 5 30 5 30 Z" fill="#FFD400"/>'
+'<path d="M5.6 30.6 C13 32.6 33 32.6 40.4 30.6" stroke="#fff" stroke-width="1.4" fill="none" opacity=".55"/></svg></div>'
+'<div class=voyhome><svg viewBox="0 0 46 54" width="40" height="47">'
+'<circle class=voylamp cx="23" cy="15" r="10" fill="#FFD400" opacity=".26"/>'
+'<path d="M17 22 C17 22 16 34 13.5 46 L32.5 46 C30 34 29 22 29 22 Z" fill="#ffffff" opacity=".95"/>'
+'<path d="M16.4 28 L29.6 28 C29.8 30.4 30 32.6 30.3 34 L15.7 34 C16 32.6 16.2 30.4 16.4 28 Z" fill="#CFE0FF" opacity=".95"/>'
+'<rect x="16.4" y="13.5" width="13.2" height="8.5" rx="2.4" fill="#FFD400"/>'
+'<path d="M14.5 13.5 C14.5 13.5 18 5 23 4 C28 5 31.5 13.5 31.5 13.5 Z" fill="#ffffff" opacity=".95"/>'
+'<circle cx="23" cy="3" r="1.8" fill="#FFD400"/>'
+'<path d="M9 46 L37 46 C38.2 46 39 46.9 39 48 C39 49.1 38.2 50 37 50 L9 50 C7.8 50 7 49.1 7 48 C7 46.9 7.8 46 9 46 Z" fill="#ffffff" opacity=".62"/></svg></div>'
+'<svg class="voyw voyw1" viewBox="0 0 1200 120" preserveAspectRatio="none">'
+'<path d="M0,62 C150,26 300,98 450,62 C600,26 750,98 900,62 C1050,26 1200,98 1350,62 L1350,120 L0,120 Z" fill="#1443c9"/>'
+'<path d="M0,62 C150,26 300,98 450,62 C600,26 750,98 900,62 C1050,26 1200,98 1350,62" fill="none" vector-effect="non-scaling-stroke" stroke="#ffffff" stroke-width="1.5" opacity=".5"/></svg>'
+'<svg class="voyw voyw2" viewBox="0 0 1200 120" preserveAspectRatio="none">'
+'<path d="M0,76 C150,46 300,106 450,76 C600,46 750,106 900,76 C1050,46 1200,106 1350,76 L1350,120 L0,120 Z" fill="#0b34ae"/>'
+'<path d="M0,76 C150,46 300,106 450,76 C600,46 750,106 900,76 C1050,46 1200,106 1350,76" fill="none" vector-effect="non-scaling-stroke" stroke="#ffffff" stroke-width="1.6" opacity=".66"/></svg>'
+'<svg class="voyw voyw3" viewBox="0 0 1200 120" preserveAspectRatio="none">'
+'<path d="M0,92 C200,70 320,114 520,92 C700,72 820,114 1020,92 C1160,78 1240,106 1400,92 L1400,120 L0,120 Z" fill="#04248c"/>'
+'<path d="M0,92 C200,70 320,114 520,92 C700,72 820,114 1020,92 C1160,78 1240,106 1400,92" fill="none" vector-effect="non-scaling-stroke" stroke="#ffffff" stroke-width="1.6" opacity=".85"/></svg>'
+'</div>'
+'<div class=voytext><div class=voystage>启航</div><div class=voynum><b id=voyhits>0</b> 条线索已入网</div></div></div>';
var _voyT=0;
function pollJob(id,box,t0){
 fetch('/api/searchstat?id='+id).then(r=>r.json()).then(function(j){
  if(!j.ok){box.innerHTML='<div class=mut style="padding:10px 16px">'+(j.err||'任务丢失')+'</div>';return;}
  if(!j.done){
   var p=j.prog||{},tot=p.total||0,dn=p.done||0;
   // 船位老实跟着港口数走:走了多少港,就在海图上的多少处。全部走完=靠岸卸货(识别配图)
   var docked=(p.stage=='归航')||(tot&&dn>=tot);
   var pct=docked?93:(6+(tot?dn/tot:0)*87);
   if(!box.querySelector('.voy')){box.innerHTML=VOYAGE;}
   var boat=box.querySelector('.voyboat');
   if(boat)boat.style.left='calc('+pct.toFixed(1)+'% - 19px)';
   if(docked)box.querySelector('.voysea').classList.add('voydock');
   var st=box.querySelector('.voystage');
   var stx=docked?('已靠岸 · 清点渔获'+(tot?'(走遍 '+tot+' 港)':'')):((p.stage||'启航')+(tot?' · 途经 '+dn+'/'+tot+' 港':''));
   if(st)st.textContent=stx+' · 已行 '+Math.round((Date.now()-t0)/1000)+' 秒';
   var hs=box.querySelector('#voyhits');
   if(hs)hs.textContent=p.hits||0;
   setTimeout(function(){pollJob(id,box,t0);},1200);
   return;
  }
  var d=j.result||{};
  if(!d.ok){box.innerHTML='<div class=mut style="padding:10px 16px">搜索失败：'+(d.err||'')+'</div>';return;}
  if(!(d.groups||[]).length&&!(d.other||[]).length){
   box.innerHTML='<div class=empty><div class=ei>🔍</div><div class=et>没搜到结果</div><div>试试英文片名 · 换个更短的关键词 · 或去设置检查 Prowlarr 连接</div></div>';
   var _bk=mkBack();if(_bk)box.insertBefore(_bk,box.firstChild);
   return;}
  _sd=d;saveSearch();renderWall();
 }).catch(function(){setTimeout(function(){pollJob(id,box,t0);},2500);});
}
/* ---- 搜索结果落盘。一次全站搜要十几秒,刷新一下就没了、还得从头再搜一遍,这是真难受。
       存 localStorage:刷新、关标签页、甚至关浏览器再回来,结果都还在。 ---- */
var SKEY='gl_lastsearch';
function saveSearch(){
 try{
  if(!_sd)return;
  localStorage.setItem(SKEY,JSON.stringify({q:document.getElementById('q').value,
   f:_sf,d:_sd,db:_dbback,bk:_backto,ts:Date.now()}));
 }catch(e){ /* 配额满/隐私模式:存不下就算了,不能因为缓存失败把搜索结果搞崩 */ }
}
function ageTxt(ms){
 var m=Math.round(ms/60000);
 if(m<1)return '刚刚';
 if(m<60)return m+' 分钟前';
 var h=Math.round(m/60); return h<24?(h+' 小时前'):(Math.round(h/24)+' 天前');
}
function restoreSearch(){
 var raw=null;
 try{raw=localStorage.getItem(SKEY);}catch(e){return;}
 if(!raw)return;
 var s=null;
 try{s=JSON.parse(raw);}catch(e){return;}
 if(!s||!s.d||!(s.d.groups||[]).length&&!(s.d.other||[]).length)return;
 if(Date.now()-(s.ts||0)>6*3600*1000){try{localStorage.removeItem(SKEY);}catch(e){}return;}  /* 太旧的种子信息不可信 */
 _sd=s.d;_sf=s.f||'';_dbback=!!s.db;_backto=s.bk||null;
 document.getElementById('q').value=s.q||'';
 document.querySelectorAll('.fpill').forEach(function(e){e.classList.toggle('on',!!_sf&&e.dataset.f==_sf);});
 renderWall();
 var box=document.getElementById('sresult'),tip=document.createElement('div');
 tip.className='cachetip';
 var t=document.createElement('span');
 t.textContent='上次搜「'+(s.q||'')+'」的结果 · '+ageTxt(Date.now()-(s.ts||0))+' · 做种数可能已变';
 var b=document.createElement('button');b.textContent='重新搜索';b.onclick=function(){doSearch();};
 var c=document.createElement('button');c.textContent='清空';c.onclick=function(){clearSearch();};
 tip.appendChild(t);tip.appendChild(b);tip.appendChild(c);
 box.insertBefore(tip,box.firstChild);
}
function clearSearch(){
 try{localStorage.removeItem(SKEY);}catch(e){}
 _sd=null;_sf='';_dbback=false;_backto=null;
 document.getElementById('sresult').innerHTML='';
 document.querySelectorAll('.fpill').forEach(function(e){e.classList.remove('on')});
}
function dl(b,x,rs){
 b.disabled=true;b.textContent='下载中…';
 var mates=(rs||[]).filter(o=>o.url!=x.url).map(o=>({url:o.url,size:o.size||0,site:o.site}));
 fetch('/api/dl',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({url:x.url,cat:x.cat||'',mates:mates})}).then(r=>r.json()).then(function(d){
  if(d.ok){b.textContent='✅ 已下';b.style.background='var(--ok)';toast('已推送下载,点「⬇️ 下载」页看实时进度');}
  else{b.textContent='失败';b.disabled=false;toast('下载失败：'+(d.err||''));}
 }).catch(e=>{b.textContent='失败';b.disabled=false;toast('下载出错');});
}
function batchOpen(){
 var b=document.getElementById('batchbox');
 if(b.style.display!='none'){b.style.display='none';return;}
 b.style.display='block';b.innerHTML='';
 var lab=DBS.filter(function(x){return x[0]==_dbcol;}).map(function(x){return x[1];})[0]||_dbcol;
 var c=document.createElement('div');c.className='cachetip';c.style.marginLeft='0';
 var t=document.createElement('span');
 t.textContent='从「'+lab+'」批量下载：只搜几个大站选最优版本，下完由后台辅种铺到全部站点。';
 var sel=document.createElement('select');sel.style.cssText='margin:0 6px;padding:3px 8px;border-radius:8px';
 [25,50,100,250].forEach(function(n){var o=document.createElement('option');o.value=n;o.textContent='前 '+n+' 部';sel.appendChild(o);});
 var k=document.createElement('label');k.style.cssText='font-size:12px;margin-right:8px';
 var kb=document.createElement('input');kb.type='checkbox';kb.style.marginRight='4px';
 k.appendChild(kb);k.appendChild(document.createTextNode('优先 4K'));
 var go=document.createElement('button');go.textContent='开始扫描';
 go.onclick=function(){batchPlan(sel.value,kb.checked?1:0,go);};
 c.appendChild(t);c.appendChild(sel);c.appendChild(k);c.appendChild(go);b.appendChild(c);
 var d=document.createElement('div');d.id='batchres';b.appendChild(d);
}
function batchPlan(n,k,btn){
 _noReload=true;clearTimeout(_t);_t=null;
 btn.disabled=true;btn.textContent='扫描中…';
 var box=document.getElementById('batchres');
 box.innerHTML='<div class=mut id=bpTip>准备中…</div>';
 fetch('/api/batchplan?col='+_dbcol+'&n='+n+'&k='+k).then(r=>r.json()).then(function(d){
  if(!d.ok){box.innerHTML='<div class=mut>提交失败：'+(d.err||'')+'</div>';btn.disabled=false;btn.textContent='开始扫描';return;}
  batchPoll(d.id,box,btn);
 }).catch(function(){box.innerHTML='<div class=mut>提交出错</div>';btn.disabled=false;btn.textContent='开始扫描';});
}
function batchPoll(id,box,btn){
 fetch('/api/batchstat?id='+id).then(r=>r.json()).then(function(j){
  if(!j.ok){box.innerHTML='<div class=mut>'+(j.err||'任务丢失')+'</div>';btn.disabled=false;btn.textContent='开始扫描';return;}
  if(!j.fin){
   var e=document.getElementById('bpTip');
   if(e)e.textContent='扫描中 '+j.done+'/'+j.total+' —— 正在查「'+(j.cur||'')+'」（每部约 10 秒，可以先干别的）';
   setTimeout(function(){batchPoll(id,box,btn);},1500);return;
  }
  btn.disabled=false;btn.textContent='重新扫描';
  batchRender(j.rows||[],box);
 }).catch(function(){setTimeout(function(){batchPoll(id,box,btn);},3000);});
}
function batchRender(rows,box){
 box.innerHTML='';
 var can=rows.filter(function(r){return r.url;});
 var skip=rows.filter(function(r){return !r.url;});
 var head=document.createElement('div');head.className='gt';head.style.margin='8px 0 6px';
 head.textContent='可下载 '+can.length+' 部 · 跳过 '+skip.length+' 部';box.appendChild(head);
 var tb=document.createElement('table');
 var hr=document.createElement('tr');
 ['','片名','评分','选中的版本','站点','大小','做种'].forEach(function(h){
  var th=document.createElement('th');th.textContent=h;hr.appendChild(th);});
 tb.appendChild(hr);
 can.forEach(function(r){
  var tr=document.createElement('tr');
  var c0=document.createElement('td');
  var cb=document.createElement('input');cb.type='checkbox';cb.checked=true;cb.dataset.i=rows.indexOf(r);
  cb.className='bsel';c0.appendChild(cb);
  var c1=document.createElement('td');c1.textContent=r.title+(r.year?' ('+r.year+')':'');
  var c2=document.createElement('td');c2.className='mut';c2.textContent=r.rate?('★'+r.rate):'';
  var c3=document.createElement('td');c3.className='sname';c3.title=r.rel;c3.textContent=r.rel;
  if(r.noxfer){var w=document.createElement('span');w.className='mut';w.style.color='#FFD400';
   w.textContent=' [禁转]';c3.appendChild(w);}
  var c4=document.createElement('td');var sp=document.createElement('span');sp.className='src';sp.textContent=r.site;c4.appendChild(sp);
  var c5=document.createElement('td');c5.className='r';c5.textContent=r.size;
  var c6=document.createElement('td');c6.className='r';c6.textContent=r.seeders;
  [c0,c1,c2,c3,c4,c5,c6].forEach(function(x){tr.appendChild(x);});
  tb.appendChild(tr);
 });
 box.appendChild(tb);
 if(skip.length){
  var sh=document.createElement('div');sh.className='gt';sh.style.margin='12px 0 6px';
  sh.textContent='跳过的';box.appendChild(sh);
  var st=document.createElement('table');
  skip.forEach(function(r){
   var tr=document.createElement('tr');
   var a=document.createElement('td');a.textContent=r.title+(r.year?' ('+r.year+')':'');
   var b2=document.createElement('td');b2.className='mut';b2.textContent=r.skip||'';
   tr.appendChild(a);tr.appendChild(b2);st.appendChild(tr);});
  box.appendChild(st);
 }
 if(can.length){
  var bar=document.createElement('div');bar.style.marginTop='12px';
  var all=document.createElement('button');all.className='dlbtn';all.style.marginRight='8px';
  all.textContent='全选/全不选';
  all.onclick=function(){var cs=document.querySelectorAll('.bsel');var v=!cs[0].checked;
   cs.forEach(function(x){x.checked=v;});};
  var go=document.createElement('button');go.className='dlbtn';
  go.textContent='推送到 qb 下载';
  go.onclick=function(){
   var picked=[];document.querySelectorAll('.bsel').forEach(function(x){
    if(x.checked)picked.push(rows[parseInt(x.dataset.i)]);});
   if(!picked.length){toast('一部都没选');return;}
   go.disabled=true;go.textContent='推送中…（每部间隔 2 秒）';
   fetch('/api/batchgo',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rows:picked})}).then(r=>r.json()).then(function(d){
     go.textContent=d.ok?('✅ 已推送 '+d.added+' 部'+(d.failed?('，失败 '+d.failed):'')):'推送失败';
     toast(d.ok?('已推送 '+d.added+' 部到 qb，去「下载管理」看进度'):(d.err||'失败'));
    }).catch(function(){go.disabled=false;go.textContent='推送到 qb 下载';toast('推送出错');});
  };
  bar.appendChild(all);bar.appendChild(go);box.appendChild(bar);
  var note=document.createElement('div');note.className='mut';note.style.cssText='margin-top:6px;font-size:12px';
  note.textContent='下载完会自动识别→硬链接进媒体库→转种到 tr→后台辅种铺开到几十个站。[禁转] 标记的种不会被转出去，但正常做种。';
  box.appendChild(note);
 }
}
function libAudit(btn){
 var box=document.getElementById('libaudit');
 if(btn){btn.disabled=true;btn.textContent='体检中…';}
 _noReload=true; clearTimeout(_t); _t=null;   /* 挡住本页 20 秒一次的整页重载 */
 var t0=Date.now();
 box.innerHTML='<span class=mut id=laTip>扫描中…首次要问 TMDB 每部片的类型，约十几秒</span>';
 var tick=setInterval(function(){var e=document.getElementById('laTip');
  if(e)e.textContent='扫描中…已等 '+Math.round((Date.now()-t0)/1000)+' 秒（首次要问 TMDB 每部片的类型）';},1000);
 fetch('/api/libaudit?force=1').then(r=>r.json()).then(function(d){
  clearInterval(tick);
  if(btn){btn.disabled=false;btn.textContent='重新体检';}
  if(!d.ok){box.innerHTML='<span class=mut>体检失败：'+(d.err||'')+'</span>';return;}
  box.innerHTML='';
  var wc=d.wrong_cat||[],fl=d.flat||[],uk=d.unknown||[],ms=d.missing||[],ex=d.extras||[];
  var lock=document.createElement('div');lock.className='cachetip';lock.style.marginLeft='0';
  var lt=document.createElement('span');lt.textContent='📌 已暂停本页的自动刷新，结果不会自己消失。看完点右边恢复。';
  var lb=document.createElement('button');lb.textContent='恢复自动刷新';
  lb.onclick=function(){_noReload=false;armReload('media');lock.remove();};
  lock.appendChild(lt);lock.appendChild(lb);box.appendChild(lock);
  var sum=document.createElement('div');sum.className='mut';sum.style.marginBottom='10px';
  sum.textContent='共 '+d.total+' 部 · 分类错放 '+wc.length+' · 季目录缺失 '+fl.length+' · 特典待归位 '+ex.length+' · 待人工确认 '+uk.length+' · 目录丢失 '+ms.length;
  box.appendChild(sum);
  function sec(title,items,render){
   if(!items.length)return;
   var h=document.createElement('div');h.className='gt';h.style.margin='10px 0 6px';h.textContent=title;box.appendChild(h);
   var t=document.createElement('table');
   items.forEach(function(x){var tr=document.createElement('tr');render(tr,x);t.appendChild(tr);});
   box.appendChild(t);
  }
  sec('❌ 分类错放（会被自动修正）',wc,function(tr,x){
   var a=document.createElement('td');a.textContent=x.name;
   var b=document.createElement('td');b.className='mut';b.textContent=x.why;
   var c2=document.createElement('td');c2.className='mut';c2.style.fontSize='12px';c2.textContent=x.target+' → '+x.to;
   tr.appendChild(a);tr.appendChild(b);tr.appendChild(c2);});
  sec('📁 季目录缺失（会被自动修正）',fl,function(tr,x){
   var a=document.createElement('td');a.textContent=x.name;
   var b=document.createElement('td');b.className='mut';
   var ks=Object.keys(x.seasons).map(function(k){return k=='0'?('无季号 '+x.seasons[k]+'个'):('S'+k+' '+x.seasons[k]+'个');});
   b.textContent=ks.join('  ');
   tr.appendChild(a);tr.appendChild(b);});
  sec('🎁 特典待归位（可一键，移入 Season 00）',ex,function(tr,x){
   var a=document.createElement('td');a.textContent=x.name;
   var b=document.createElement('td');b.className='mut';b.textContent=x.why;
   tr.appendChild(a);tr.appendChild(b);tr.appendChild(document.createElement('td'));});
  sec('❓ 待人工确认（自动判不了，不硬来）',uk,function(tr,x){
   var a=document.createElement('td');a.textContent=x.name;
   var b=document.createElement('td');b.className='mut';
   b.textContent=x.why?x.why:('TMDB 条目没填类型（tmdbid '+x.tmdbid+'）');
   var c3=document.createElement('td');
   if(!x.why){   // 只有「TMDB没类型」这种才提供手动归类;命名太乱那种得先理文件
    ['动漫','电视剧','电影'].forEach(function(cn){
     var bt=document.createElement('button');bt.className='dlbtn';
     bt.style.cssText='padding:3px 10px;font-size:12px;margin-right:6px';
     bt.textContent='归为'+cn;
     bt.onclick=function(){libSetCat(x.target,cn,bt);};
     c3.appendChild(bt);});
   }
   tr.appendChild(a);tr.appendChild(b);tr.appendChild(c3);});
  sec('⚠️ 目录丢失',ms,function(tr,x){
   var a=document.createElement('td');a.textContent=x.name;
   var b=document.createElement('td');b.className='mut';b.textContent=x.target;
   tr.appendChild(a);tr.appendChild(b);});
  if(wc.length||fl.length||ex.length){
   var f=document.createElement('button');f.className='dlbtn';f.style.margin='12px 0 0';
   f.textContent='一键修正（'+(wc.length+fl.length+ex.length)+' 项）';
   f.onclick=function(){libFix(f);};box.appendChild(f);
   var note=document.createElement('div');note.className='mut';note.style.marginTop='6px';note.style.fontSize='12px';
   note.textContent='媒体库文件是硬链接，移动只改目录项不动数据，做种不受影响。修完记得在 Emby 里扫描一次媒体库。';
   box.appendChild(note);
  }else if(!uk.length&&!ms.length){
   var okd=document.createElement('div');okd.textContent='✅ 媒体库很干净，没发现问题';box.appendChild(okd);
  }
 }).catch(function(e){clearInterval(tick);
  if(btn){btn.disabled=false;btn.textContent='重试体检';}
  box.innerHTML='<span class=mut>请求失败（'+e+'）。若是超时，再点一次——类型已缓存，第二次很快。</span>';});
}
function libSetCat(target,cat,btn){
 btn.disabled=true;btn.textContent='处理中…';
 fetch('/api/libcat?target='+encodeURIComponent(target)+'&cat='+encodeURIComponent(cat))
  .then(r=>r.json()).then(function(d){
   if(d.ok){toast('已归为'+cat+'，并搬到 '+d.to);libAudit(null);}
   else{toast('失败：'+(d.err||''));btn.disabled=false;btn.textContent='归为'+cat;}
  }).catch(function(){toast('请求出错');btn.disabled=false;btn.textContent='归为'+cat;});
}
function libFix(btn){
 btn.disabled=true;btn.textContent='修正中…';
 fetch('/api/libfix').then(r=>r.json()).then(function(d){
  if(!d.ok){toast('修正失败：'+(d.err||''));btn.disabled=false;btn.textContent='一键修正';return;}
  toast('已搬库 '+d.moved+' 部、分季 '+d.seasoned+' 部'+((d.errs||[]).length?('，'+d.errs.length+' 项有问题'):''));
  libAudit(null);
 }).catch(function(){toast('修正出错');btn.disabled=false;btn.textContent='一键修正';});
}
function reid(h,el){
 var inp=el.parentNode.querySelector('input');var v=inp.value.trim();
 if(!v){inp.focus();return;}
 clearTimeout(_t);el.disabled=true;el.textContent='入库中…';
 fetch('/api/reid?hash='+encodeURIComponent(h)+'&q='+encodeURIComponent(v))
  .then(r=>r.json()).then(d=>{if(d.ok){toast('已入库：'+(d.name||''));setTimeout(()=>location.reload(),1500);}
   else{toast('失败：'+(d.err||''));el.disabled=false;el.textContent='确认入库';}})
  .catch(e=>{toast('出错');el.disabled=false;el.textContent='确认入库';});
}
dbInit();       /* 放在最末尾:DBS/_dbcol 是 var,提前调用时它们还是 undefined */
restoreSearch();/* 刷新页面后把上次的搜索结果捞回来 */
</script></body></html>"""

DETAIL = """<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>种子详情 · 观澜</title><link rel="icon" href="/favicon.ico" type="image/svg+xml"><style>
:root{--ikb:#002FA7;--acc:#fff;--accL:#CFE0FF;--ok:#3ddc84;--warn:#ffd83d;--fg:#fff;--sub:rgba(255,255,255,.78);--line:rgba(255,255,255,.24);--card:rgba(255,255,255,.17)}
*{box-sizing:border-box}body{margin:0;color:#fff;font:14px/1.6 -apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC','Microsoft YaHei',sans-serif;-webkit-font-smoothing:antialiased;
background:linear-gradient(180deg,#0039c8 0%,#002FA7 38%,#001d77 100%);background-attachment:fixed;background-color:#002FA7}
.wrap{max-width:840px;margin:0 auto;padding:32px 28px}a{color:var(--accL);text-decoration:none}
.back{font-size:13px;font-weight:600}.title{font-size:19px;font-weight:700;margin:14px 0 4px;word-break:break-all;letter-spacing:-.02em}
.card{background:var(--card);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:20px;padding:18px 20px;margin:16px 0}
.kv{display:grid;grid-template-columns:92px 1fr;gap:8px 16px;font-size:14px}.kv .k{color:var(--sub)}
.src{display:inline-block;padding:2px 11px;border-radius:20px;font-size:13px;background:rgba(255,255,255,.16);color:#fff;font-weight:500}
.big{display:inline-block;padding:2px 13px;border-radius:20px;font-size:15px;font-weight:700;background:rgba(61,220,132,.22);color:#8dffbd}
h2{font-size:15px;font-weight:600;margin:0 0 4px}.card h2{margin-bottom:12px}
table{width:100%;border-collapse:collapse}th{color:var(--sub);font-weight:500;font-size:12px;text-align:left;padding:8px 6px;border-top:none}
td{text-align:left;padding:9px 6px;border-top:1px solid var(--line);font-size:13px}.mut{color:var(--sub)}
</style></head><body><div class=wrap>
<div class=back><a href="/">← 返回列表</a></div>
<div class=title>{{NAME}}</div>
<div class=card><div class=kv>
<div class=k>来源站</div><div><span class=src>{{SRC}}</span> <span class=mut>← 你从这个站下载的</span></div>
<div class=k>大小</div><div>{{SIZE}} GiB · {{FILES}} 个文件</div>
<div class=k>搜索关键词</div><div class=mut>{{QUERY}}</div>
<div class=k>辅种结果</div><div><span class=big>{{INJECTED}}</span> 个站做种 <span class=mut>(匹配 {{MATCHED}})</span></div>
</div></div>
<div class=card><h2>辅种去向 · 这份数据同时在这些站做种</h2>
<table><tr><th>站点</th><th>方式</th><th>结果</th></tr>{{MROWS}}</table></div>
<div class=mut style=text-align:center;font-size:12px>观澜 Wavegazer</div>
</div></body></html>"""

_DASH_MEDIA = {}
_OVCACHE = {}
_EMBY_SID = ""
_DLMETA = {}
def _dlmeta(h, name):
    """下载页海报：每个种子只做一次 TMDB 识别，缓存住(轮询4秒一次,不能每次都查)"""
    m = _DLMETA.get(h)
    if m is None:
        try: mm = tmdb_match(name)
        except Exception: mm = None
        if mm and mm.get("conf") == "low": mm = None    # 低置信别放错海报误导
        m = {"tmdb": (mm or {}).get("tmdb_name","") or "", "year": (mm or {}).get("year","") or "",
             "poster": (mm or {}).get("poster","") or ""}
        if len(_DLMETA) > 500: _DLMETA.clear()
        _DLMETA[h] = m
    return m


def music_clean(t):
    """音乐种子名清洗成 iTunes 可搜的 '歌手 专辑' 形式"""
    s = re.sub(r'\[[^\]]*\]|【[^】]*】|\([^)]*\)', ' ', t)
    s = re.sub(r'\b(FLAC|APE|WAV|WV|MP3|AAC|OGG|DSD|DSF|DFF|SACD|MQA|Hi-?Res|\d+bit|\d+kHz|320K|'
               r'CD\d*|\dCD|WEB|BD|24-\d+|16-\d+|无损|专辑|合集|精选集?|全集|正版|首版|限定盘?|日版|港版|台版|新歌\+?)\b', ' ', s, flags=re.I)
    s = re.sub(r'(19|20)\d{2}', ' ', s)
    s = re.sub(r'[.\-_/+·]+', ' ', s)
    return ' '.join(s.split())[:40]

_MUSIC_CACHE = {}
def music_match(cleaned):
    """iTunes Search 认专辑：返回 {artist,album,year,art,id} 或 None,缓存6小时"""
    hit = _MUSIC_CACHE.get(cleaned)
    if hit and time.time() - hit[1] < 21600: return hit[0]
    m = None
    try:
        qq = urllib.parse.urlencode({"term": cleaned, "media": "music", "entity": "album", "limit": 1, "country": "cn"})
        r = json.load(urllib.request.urlopen("https://itunes.apple.com/search?" + qq, timeout=12))
        res = r.get("results") or []
        if res:
            x = res[0]
            m = {"artist": x.get("artistName",""), "album": x.get("collectionName",""),
                 "year": (x.get("releaseDate") or "")[:4],
                 "art": (x.get("artworkUrl100") or "").replace("100x100", "300x300"),
                 "id": x.get("collectionId")}
    except Exception: pass
    if len(_MUSIC_CACHE) > 1000: _MUSIC_CACHE.clear()
    _MUSIC_CACHE[cleaned] = (m, time.time())
    return m

_LIBIX = {"t": 0, "d": None}
def library_index():
    """本地媒体库索引(缓存60秒):tmdbid→已入库标注、片名集合。用来给搜索结果打「已有」防重复下载"""
    if _LIBIX["d"] and time.time() - _LIBIX["t"] < 60:
        return _LIBIX["d"]
    ids = {}; keys = {}; names = {}
    try:
        c = db()
        for tid, tn, yr, cat, mty in c.execute(
                "SELECT tmdbid,tmdb_name,year,cat,mtype FROM media WHERE status='done'").fetchall():
            if tn: names.setdefault(tn, set()).add(yr or "")
            if tid:
                lab = f"已入库{('·'+cat) if cat else ''}"
                ids[int(tid)] = lab                       # 老兜底:mtype 缺失的历史数据只能按数字认
                if mty: keys[f"{mty}:{int(tid)}"] = lab    # TMDB 的 movie/tv 是两套独立 id,必须带类型
        c.close()
    except Exception: pass
    d = {"ids": ids, "keys": keys, "names": names}
    _LIBIX.update(t=time.time(), d=d)
    return d

_SEASON_SUFFIX = re.compile(r'\s*第[0-9一二三四五六七八九十百]+[季部]\s*$')
def _strip_season(t):
    """剥掉标题尾部的季号:「老友记 第十季」→「老友记」。
       豆瓣剧集榜按季收录,库里存的是剧集级身份,不剥就永远对不上。"""
    t = (t or "").replace("  ", " ").strip()
    return _SEASON_SUFFIX.sub("", t).strip() or t

def owned_label(ix, mtype, tid, name="", year=""):
    """查某作品是否已入库。优先 mtype:id 精确命中,mtype 缺失的老数据退回纯数字,最后同名+年份兜底。"""
    if tid:
        hit = ix["keys"].get(f"{mtype}:{int(tid)}")
        if hit: return hit
        # keys 里已登记该 id 的另一种类型 → 说明这是撞号,不是同一部作品,别误标
        if any(k.endswith(f":{int(tid)}") for k in ix["keys"]): return ""
        hit = ix["ids"].get(int(tid))
        if hit: return hit
    # 同名兜底必须带年份:剧版/影版同名太常见(库里 2003 电影《手机》≠ 2010 电视剧《手机》)
    yrs = ix["names"].get(name) if name else None
    if not yrs and name and len(name) >= 3:
        # 兜底:库里可能存成「猫和老鼠（五十周年纪念版）157集」,豆瓣只叫「猫和老鼠」。
        # 只认「短名 + 括号补充说明」这一种形式 —— 单纯的前缀匹配会把
        # 「三国演义」误配到「三国演义3D」,那是两部不同的剧。
        # 宁可漏判也不能误判:误判会让批量下载**跳过**你其实没有的片子。
        for k, v in ix["names"].items():
            if not k or len(k) < 3: continue
            a, b = (name, k) if len(name) <= len(k) else (k, name)
            if b.startswith(a) and b[len(a):].lstrip()[:1] in ("（", "(", "【", "[", "「"):
                yrs = v; break
    if not yrs: return ""
    year = str(year or "")
    if not year.isdigit() or not any(y.isdigit() for y in yrs):
        return "同名已有"                            # 有一边没年份,只能给个弱提示
    for y in yrs:
        if y.isdigit() and abs(int(y) - int(year)) <= 1: return "同名已有"
    return ""

def search_group(q, results, log=lambda m: None, anchor=None):
    """Prowlarr 结果 → 做种过滤 + TMDB 识别分组。log 回调用于搜索过程直播。
       anchor 是「用户这次要找的那部」,给了就先用它认领种子:认得出的直接进主卡片排第一,
       剩下的才走原来那套「按种子名解析→识别」的流程。"""
    def catlab(r):
        ids = [c.get("id", 0) for c in (r.get("categories") or [])]
        if any(i == 5070 for i in ids): return "anime"
        if any(2000 <= i < 3000 for i in ids): return "movie"
        if any(5000 <= i < 6000 for i in ids): return "tv"
        if any(3000 <= i < 4000 for i in ids): return "music"
        if any(7000 <= i < 8000 for i in ids): return "book"
        return ""
    out = []
    for r in results:
        url = r.get("downloadUrl") or r.get("guid") or ""
        if not url: continue
        out.append(parse_release({
            "title": r.get("title",""), "site": r.get("indexer",""),
            "sizeh": human_size(r.get("size",0)), "size": r.get("size",0),
            "seeders": r.get("seeders") or 0,
            "url": url, "cat": catlab(r), "info": r.get("infoUrl") or ""}))
    out.sort(key=lambda x: -score_release(x, "browse"))
    out = out[:100]
    # 音乐走 iTunes 识别(TMDB不管音乐)，其余走 TMDB
    for x in out:
        if x["cat"] != "music" and meta_is_music(x["title"]):
            x["cat"] = "music"
    music = [x for x in out if x["cat"] == "music"]
    out = [x for x in out if x["cat"] != "music"]
    mgroups = {}
    if music:
        mkeys = {}
        for x in music:
            ck = music_clean(x["title"]).lower()
            x["mk"] = ck
            mkeys.setdefault(ck, {"rep": music_clean(x["title"]), "n": 0})["n"] += 1
        mtodo = [kv for kv in sorted(mkeys.items(), key=lambda kv: -kv[1]["n"])[:15] if kv[0]]
        mmatched = {}
        for i, (ck, info) in enumerate(mtodo):
            mm = music_match(info["rep"])
            if mm:
                mmatched[ck] = mm
                log(f"🎵 识别专辑 {i+1}/{len(mtodo)}: {info['rep'][:30]} → {mm['artist']} - {mm['album']}")
            else:
                log(f"🧩 识别专辑 {i+1}/{len(mtodo)}: {info['rep'][:30]} → 未识别")
        for x in music:
            mm = mmatched.get(x.pop("mk", ""))
            if mm:
                g = mgroups.setdefault(mm["id"], {"name": f"{mm['album']} · {mm['artist']}", "year": mm["year"],
                                                  "mtype": "music", "cat": "music", "poster": "",
                                                  "posterurl": mm["art"], "overview": mm["artist"], "results": []})
                g["results"].append(x)
            else:
                x.pop("k", None); out.append(x)   # 认不出的专辑回到普通流(最终进未识别)
    owned = library_index()
    # ---- 先让锚点认领:用 TMDB 别名(中文名/原名/各语种译名)直接把属于这部片的种子挑出来 ----
    anch = None
    if anchor and anchor.get("alias"):
        qyear = anchor.get("qyear") or ""
        claimed = []; rest = []
        for x in out:
            if _alias_hit(x["title"], anchor["alias"]) and _year_ok(x["title"], qyear):
                claimed.append(x)
            else:
                rest.append(x)
        if claimed:
            out = rest
            anch = {"name": anchor["name"], "year": anchor["year"], "mtype": anchor["mtype"],
                    "id": anchor["id"], "cat": "anime" if anchor.get("anime") else anchor["mtype"],
                    "poster": anchor.get("poster", ""), "overview": (anchor.get("overview") or "")[:110],
                    "owned": owned_label(owned, anchor["mtype"], anchor["id"], anchor["name"], anchor["year"]),
                    "anchor": True, "results": claimed}
            log(f"🎯 锚定《{anchor['name']}》({anchor['year']}) — 直接认领 {len(claimed)} 个种"
                + (f",按年份 {qyear} 剔掉了其它版本" if qyear else ""))
        else:
            log(f"🎯 锚定《{anchor['name']}》,但站里没有名字对得上的种,转入常规识别")
    keys = {}
    for x in out:
        k = extract_query(x["title"]).lower()
        x["k"] = k
        info = keys.setdefault(k, {"rep": x["title"], "n": 0})
        info["n"] += 1
    matched = {}
    # 以前只识别出现次数最多的 12 组(串行,一组一两秒),冷门剧排在 12 名开外就永远进不了「其他」以外的地方。
    # 改成并发 8 路 + 放宽到 24 组:更全、还更快。
    todo = [(k, i) for k, i in sorted(keys.items(), key=lambda kv: -kv[1]["n"])[:24] if k]
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        def _ident(t):
            k, info = t
            try: return k, info, tmdb_match(info["rep"])
            except Exception: return k, info, None
        with ThreadPoolExecutor(max_workers=8) as tex:
            for idx, (k, info, m) in enumerate(tex.map(_ident, todo)):
                if m and m["conf"] != "low":
                    matched[k] = m
                    log(f"🔎 识别 {idx+1}/{len(todo)}: {info['rep'][:36]} → {m['tmdb_name']} ({m['year']})")
                else:
                    log(f"🧩 识别 {idx+1}/{len(todo)}: {info['rep'][:36]} → 未识别,归入其他")
    groups = {}; other = []
    akey = (anchor["mtype"], anchor["id"]) if anchor else None
    for x in out:
        m = matched.get(x.pop("k"))
        if m and anch and (m["mtype"], m["id"]) == akey:
            anch["results"].append(x)      # 名字没对上但识别到同一部 → 并回主卡片,别单开一张重复的
            continue
        if m:
            gk = (m["mtype"], m["id"])
            g = groups.setdefault(gk, {"name": m["tmdb_name"], "year": m["year"], "mtype": m["mtype"],
                                       "id": m["id"],   # 前端「找人」弹窗靠它认出哪个分组才是点的那部片
                                       "cat": "anime" if m.get("anime") else m["mtype"],
                                       "poster": m.get("poster",""), "overview": (m.get("overview") or "")[:110],
                                       "owned": owned_label(owned, m["mtype"], m["id"], m["tmdb_name"], m["year"]),
                                       "results": []})
            g["results"].append(x)
        else:
            other.append(x)
    def seed_filter(rs):
        good = [x for x in rs if x["seeders"] >= CFG["MIN_SEEDERS"]]
        if not good: good = rs[:max(1, round(len(rs) * 0.2))]
        return dedupe_releases(good)
    allg = list(groups.values()) + list(mgroups.values())
    for g in allg:
        g["results"] = seed_filter(g["results"])
    if anch:
        anch["results"] = seed_filter(sorted(anch["results"], key=lambda x: -x["seeders"]))
    if other: other = seed_filter(other)
    # 用户给了年份就当真:年份对不上的作品整卡片下沉,不许再插在要找的那部前面
    qyear = (anchor or {}).get("qyear") or ""
    def offyear(g):
        if not qyear: return 0
        gy = str(g.get("year") or "")
        return 0 if (gy.isdigit() and abs(int(gy) - int(qyear)) <= 1) else 1
    glist = sorted(allg, key=lambda g: (offyear(g), -(g["results"][0]["seeders"] if g["results"] else 0)))
    if anch: glist = [anch] + glist
    return {"ok": True, "groups": glist, "other": other,
            "anchor": bool(anch), "qyear": qyear}

# ============ 豆瓣经典片单货架(首页「今晚观什么澜」下面那一排) ============
# 走豆瓣分类排行榜接口 search_subjects,关键是 sort=rank —— 按评分排,出来的才是「经典」:
# 国产剧头几位是大明王朝1566/西游记/红楼梦,日剧是白色巨塔,韩剧是请回答1988。
# 换成热门榜就全是本周新番,不是用户要的东西。
# 豆瓣认 Referer,不带就 403/404,所以必须自己代发;豆瓣挂了也不许首页开天窗 → 回落 TMDB 高分榜。
DOUBAN_SHELVES = [
    ("cn_tv",   "🇨🇳 国产剧",  "tv",    "国产剧"),
    ("us_tv",   "🇺🇸 美剧",    "tv",    "美剧"),
    ("uk_tv",   "🇬🇧 英剧",    "tv",    "英剧"),
    ("jp_tv",   "🇯🇵 日剧",    "tv",    "日剧"),
    ("kr_tv",   "🇰🇷 韩剧",    "tv",    "韩剧"),
    ("jp_anime","🎌 日本动画",  "tv",    "日本动画"),
    ("doc",     "📽 纪录片",   "tv",    "纪录片"),
    ("classic", "🎬 经典电影",  "movie", "经典"),
]
DOUBAN_COLS = {k: (lab, mt, tag) for k, lab, mt, tag in DOUBAN_SHELVES}
_DB_CACHE = {}
_DB_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")

def _douban_open(url, referer, timeout=15):
    op = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": CFG["DOUBAN_PROXY"], "https": CFG["DOUBAN_PROXY"]})) if CFG["DOUBAN_PROXY"] \
        else urllib.request.build_opener()
    req = urllib.request.Request(url, headers={
        "Referer": referer, "User-Agent": _DB_UA,
        "Accept": "application/json, text/plain, */*"})
    return op.open(req, timeout=timeout)

def _dbimg_cache(url):
    """豆瓣图床的文件名(pXXXXXXX.jpg)全局唯一,直接拿来当缓存名。非法名字返回 None。"""
    from urllib.parse import urlparse
    p = urlparse(url or "")
    base = os.path.basename(p.path or "")
    if p.scheme not in ("http", "https") \
       or not re.match(r'^([a-z0-9.-]+\.)?(doubanio\.com|douban\.com)$', (p.hostname or "").lower()) \
       or not re.match(r'^[A-Za-z0-9._-]{3,64}\.(jpg|jpeg|png|webp)$', base):
        return None
    return os.path.join(os.path.dirname(CFG["DB"]), "dbimg", base)

def _dbimg_warm(url):
    """提前把海报抓到本地。不预热的话:24 张图 × 浏览器每域名 6 条连接,
       每条都要现去豆瓣拉一次 —— 首次打开货架海报会一张张慢慢冒出来,十几秒才齐。"""
    cache = _dbimg_cache(url)
    if not cache or os.path.exists(cache): return
    try:
        data = _douban_open(url, "https://movie.douban.com/", timeout=10).read()
        if not data: return
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "wb") as f: f.write(data)
        os.replace(tmp, cache)          # 半截文件不许进缓存,否则一张坏图会被缓存 6 小时
    except Exception:
        pass

def _douban_year(mt, sid):
    """排行榜接口不给年份,只能按 id 去补。年份对搜索很关键:西游记 1986 和 2010 是两码事。"""
    try:
        d = json.load(_douban_open(f"https://m.douban.com/rexxar/api/v2/{mt}/{sid}",
                                   f"https://m.douban.com/movie/subject/{sid}/", timeout=8))
        return str(d.get("year") or "")[:4]
    except Exception:
        return ""

def _douban_fetch(col, start, count):
    lab, mt, tag = DOUBAN_COLS[col]
    u = ("https://movie.douban.com/j/search_subjects?type=" + mt
         + "&tag=" + urllib.parse.quote(tag) + "&sort=rank"
         + f"&page_limit={count}&page_start={start}")
    d = json.load(_douban_open(u, "https://movie.douban.com/explore"))
    out = []
    for it in (d.get("subjects") or []):
        t = (it.get("title") or "").strip()
        if not t: continue
        out.append({"title": t, "year": "", "id": str(it.get("id") or ""),
                    "cover": it.get("cover") or "", "rate": str(it.get("rate") or ""),
                    "sub": "", "url": it.get("url") or ""})
    if out:   # 并发补年份 + 预热海报,给 12 秒预算,补不齐就算了(没年份也能搜,只是少一层限定)
        from concurrent.futures import ThreadPoolExecutor, wait
        ex = ThreadPoolExecutor(max_workers=10)
        def fill(x):
            if x["id"]: x["year"] = _douban_year(mt, x["id"])
        try:
            fs = [ex.submit(fill, x) for x in out] + [ex.submit(_dbimg_warm, x["cover"]) for x in out]
            wait(fs, timeout=12)
        finally:
            try: ex.shutdown(wait=False, cancel_futures=True)
            except TypeError: ex.shutdown(wait=False)
    return out

def _douban_fallback(col, count):
    """豆瓣不通时的兜底:TMDB 高分榜。字段和豆瓣那边对齐,前端无感。"""
    mt = DOUBAN_COLS.get(col, ("", "tv", ""))[1]
    try:
        rs = _tmdb_call(f"/{mt}/top_rated", language="zh-CN", page=1).get("results", [])
    except Exception:
        return []
    return [{"title": (r.get("name") or r.get("title") or ""), "year": _ryear(r),
             "cover": "", "tmdb_poster": r.get("poster_path") or "", "id": "",
             "rate": str(round(r.get("vote_average") or 0, 1)),
             "sub": (r.get("overview") or "")[:60], "url": ""} for r in rs[:count]]

def douban_shelf(col, start=0, count=24):
    """一个榜单的片单,缓存 6 小时(按 榜单+起点 分开缓存,「换一批」不会顶掉别的)。"""
    if col not in DOUBAN_COLS: return {"ok": False, "err": "未知榜单"}
    ck = f"{col}|{start}|{count}"
    hit = _DB_CACHE.get(ck)
    if hit and time.time() - hit[1] < 21600:
        items, src = hit[0]
    else:
        src = "douban"
        try:
            items = _douban_fetch(col, start, count)
            if not items: raise RuntimeError("空列表")
        except Exception as e:
            logmsg("WARN", f"豆瓣榜单[{col}]取不到({str(e)[:40]}),回落 TMDB 高分榜")
            items = _douban_fallback(col, count); src = "tmdb"
        if len(_DB_CACHE) > 60: _DB_CACHE.clear()
        _DB_CACHE[ck] = ((items, src), time.time())
    # 已入库标注实时算:库天天在变,不能跟着片单一起冻 6 小时
    ix = library_index()
    out = []
    for it in items:
        x = dict(it)
        # 豆瓣榜按「季」收录:标题是「老友记 第十季」、年份是那一季的 2003,
        # 而库里存的是剧集级的「老友记」+ 首播年 1994 —— 名字和年份双双对不上,
        # 直接比对会把满库的剧全标成"没有"。所以:剥掉季号,并且带季号时不比年份。
        base = _strip_season(x["title"])
        x["owned"] = owned_label(ix, "", 0, base,
                                 "" if base != x["title"] else (x.get("year") or ""))
        out.append(x)
    return {"ok": True, "col": col, "src": src, "start": start,
            "label": DOUBAN_COLS[col][0], "items": out}

# ============ 歌手专辑(按年代排序 + 经典度评分 + 推荐) ============
# 为什么不是简单搜一下:PT 搜索结果是按做种数堆在一起的一坨,分不清哪张是正传、哪张是精选、
# 哪张是 Live,更看不出发行顺序。收藏一个歌手要的是**按年代铺开的正规专辑**,
# 所以先问 iTunes 要完整年表,再拿每张去 PT 找源。

CFG.setdefault("MUSIC_SITES", os.environ.get("MUSIC_SITES", "OpenCD,AGSVPT,CarPT,M-Team,DICMusic"))

_SPLIT_RE  = re.compile(r'分轨|分軌|Multi\s*File', re.I)          # 一首歌一个文件,Navidrome 才认单曲
_WHOLE_RE  = re.compile(r'整轨|整軌|Single\s*File', re.I)          # 整张一个大文件+CUE,播放器难用
_LOSS_RE   = re.compile(r'\b(FLAC|APE|WAV|DSD|DSF|SACD|WV|TAK)\b', re.I)
_SINGLE_RE = re.compile(r'单曲|單曲|\bSingle\b|\bEP\b', re.I)
# 精选/现场/翻唱等「非正传」标记 —— 收藏优先正规录音室专辑
_NONSTUDIO = re.compile(r'精选|精選|合辑|合輯|全集|全收录|典藏|珍藏|新歌\s*\+|BEST|Greatest|Collection|'
                        r'现场|現場|演唱会|演唱會|Live|Concert|Remix|伴奏|卡拉|OST|原声', re.I)

def itunes_discography(artist):
    """问 iTunes 要某歌手的专辑年表(按发行日期升序)。这是「按时间排列」的数据来源 ——
       PT 站的种子名里年份写法五花八门,靠它归不出可靠的年表。"""
    try:
        q = urllib.parse.urlencode({"term": artist, "media": "music", "entity": "musicArtist",
                                    "limit": 1, "country": "cn"})
        r = json.load(urllib.request.urlopen("https://itunes.apple.com/search?" + q, timeout=15))
        res = r.get("results") or []
        if not res: return []
        aid = res[0].get("artistId")
        q2 = urllib.parse.urlencode({"id": aid, "entity": "album", "limit": 200, "country": "cn"})
        r2 = json.load(urllib.request.urlopen("https://itunes.apple.com/lookup?" + q2, timeout=20))
        out = []
        for x in r2.get("results", []):
            if x.get("wrapperType") != "collection": continue
            nm = x.get("collectionName") or ""
            if not nm: continue
            out.append({"album": nm, "year": (x.get("releaseDate") or "")[:4],
                        "date": (x.get("releaseDate") or "")[:10],
                        "tracks": x.get("trackCount") or 0,
                        "art": (x.get("artworkUrl100") or "").replace("100x100", "600x600"),
                        "artist": x.get("artistName") or artist})
        out.sort(key=lambda a: a["date"] or "9999")
        return out
    except Exception as e:
        logmsg("WARN", f"iTunes 年表取不到[{artist}]: {str(e)[:40]}")
        return []

_CJKC = re.compile(r'[一-鿿]')
# iTunes 会把版本信息缀在专辑名里(「虚度 - Single」「葉惠美 (Remastered)」),
# 而种子名里没有这些词 —— 不剥掉,严格包含就永远对不上,只能靠模糊蒙,一蒙就错配。
# 注意 **不剥 Live/演唱会**:那是另一张作品,剥了会让 Live 版去抢正传专辑的种子。
_ALBSUF = re.compile(r"""\s*[-–—]\s*(?:single|ep|maxi\s*single)\s*$"""
                     r"""|\s*[\(（](?:deluxe|remaster(?:ed)?|expanded|bonus|special|"""
                     r"""anniversary|reissue|edition)[^)）]*[\)）]\s*$""", re.I)
# Live 版和录音室版是两张不同的作品,名字却只差一个「(Live)」。
# 不校验的话,「范特西 (Live)」会因为名字更长而先挑,把正传《范特西》的分轨种子抢走 ——
# 用户拿到的是演唱会版,而正传只能退而求其次。两边的 Live 属性必须一致才算配上。
_LIVEW = re.compile(r'live|unplugged|concert|演唱會|演唱会|巡迴|巡回|现场|現場', re.I)

def _alb_core(x):
    """剥掉版本后缀,最多剥三层(「X - Single (Remastered)」这种叠着的)。"""
    x = (x or "").strip()
    for _ in range(3):
        y = _ALBSUF.sub("", x).strip()
        if y == x or not y: break
        x = y
    return x

def artist_albums(artist, log=lambda m: None):
    """歌手专辑总览:iTunes 年表打底 → 每张去 PT 找源 → 按年代排序 + 标推荐。"""
    sites = [x for x in CFG["MUSIC_SITES"].split(",") if x.strip()]
    disc = itunes_discography(artist)
    log(f"🎼 iTunes 年表:{len(disc)} 张专辑")
    # 一次把歌手全部资源捞回来,再按专辑名归位 —— 比每张专辑搜一次省几十倍请求
    res = prowlarr_search_fan([artist], log=log, cats=FILTER_CATS["music"], only=sites)
    pool = []
    for r in res:
        t = (r.get("title") or "").strip()
        if not t or not _LOSS_RE.search(t): continue     # 只收无损
        pool.append(parse_release({
            "title": t, "site": r.get("indexer",""), "size": r.get("size") or 0,
            "sizeh": human_size(r.get("size") or 0), "seeders": r.get("seeders") or 0,
            "url": r.get("downloadUrl") or r.get("guid") or ""}))
    def norm(x): return re.sub(r'[^0-9a-z一-鿿]+', '', (x or "").lower())
    def hit(key, title):
        """专辑名对不对得上。严格包含是主路;模糊匹配**只对中文开放**。

        为什么要模糊:iTunes 给的常是繁体(葉惠美/十一月的蕭邦),种子名简繁混用,
        差一个字就整张漏。汉字之间繁简大多只差个别字,按字符重合能救回来。

        为什么模糊路必须挡住拉丁字母:字母就那 26 个、在任何标题里都反复出现,
        重合率对它没有区分力。实测「For The Children」的字母集能在
        「齐秦-丝路1996-FLAC分轨-Chris@OpenCD」里凑齐 11/13 —— 纯噪音也能过线。
        所以英文名一律只认严格包含,宁可漏也不能错配。"""
        t = norm(title)
        if key in t: return True
        kc = set(_CJKC.findall(key))
        if len(kc) < 3: return False                 # 汉字太少,不够识别,只走包含
        miss = sum(1 for ch in kc if ch not in t)
        return miss <= max(1, int(len(kc) * 0.25))   # 短名允许缺 1 字(繁简),长名按比例
    # iTunes 常把同一张碟拆成好几条(单曲版/EP 版/重发版),名字年份一模一样只是曲目数不同。
    # 不先合掉,它们会各自去匹配、各自选中**同一个种子**,界面上就是两行一样的东西。
    seen = {}
    for a in disc:
        k = (norm(_alb_core(a["album"])), a.get("year") or "")
        if k in seen and (seen[k].get("tracks") or 0) >= (a.get("tracks") or 0): continue
        seen[k] = a
    disc = list(seen.values())

    rows = []
    used = set()          # 已被认领的种子。一个种子只能归一张专辑,否则重复推同一个 hash
    # 长名字更具体,先让它挑,免得被短名字抢走(「虚度」不该抢走「虚度的季节」的种子)
    for a in sorted(disc, key=lambda x: -len(norm(_alb_core(x["album"])))):
        key = norm(_alb_core(a["album"]))
        if not key or len(key) < 2: continue
        lv = bool(_LIVEW.search(a["album"]))
        cands = [p for p in pool if p["url"] not in used and hit(key, p["title"])
                 and bool(_LIVEW.search(p["title"])) == lv]
        if not cands: continue
        for c in cands: c["_s"] = score_release(c, "music")
        cands.sort(key=lambda c: -c["_s"])
        best = cands[0]
        used.add(best["url"])
        rows.append({"album": a["album"], "year": a["year"], "date": a["date"],
                     "tracks": a["tracks"], "art": a["art"],
                     "rel": best["title"], "site": best["site"], "sizeh": best["sizeh"],
                     "size": best["size"], "seeders": best["seeders"], "url": best["url"],
                     "score": round(best["_s"]), "alts": len(cands),
                     "studio": not best["nonstudio"],
                     "split": best["split"] is True})
    rows.sort(key=lambda r: r["date"] or "9999")
    # 推荐:正传 + 分轨 + 分数排前列。经典度靠做种数,但正传和分轨是硬门槛
    ranked = sorted([r for r in rows if r["studio"] and r["split"]], key=lambda r: -r["score"])
    top = {id(r) for r in ranked[:12]}
    for r in rows: r["rec"] = id(r) in top
    log(f"✅ 匹配到 {len(rows)} 张有源,其中推荐 {sum(1 for r in rows if r['rec'])} 张")
    return {"ok": True, "artist": artist, "rows": rows,
            "nodisc": len(disc) == 0, "pool": len(pool)}

# ============ 榜单批量下载 ============
# 为什么是「一部一个种」而不是「一个大包」:
#   ① 大包一个 TMDB 身份套几百部,识别必错,包内文件名不规范时事后拆不动(用户吃过这个亏);
#   ② **辅种覆盖面**:大包只有存了同一个包的站能辅,单片各站都有,能铺到几十个站 —— 覆盖面就是存活率;
#   ③ 已有的能跳过,不重复占空间;出问题单片重下即可。
# 搜索只问几个大站(少而精,一部片只需要一个好种),覆盖面交给后台辅种全站铺开。

CFG.setdefault("PREFER_ZH_AUDIO", os.environ.get("PREFER_ZH_AUDIO", "1") not in ("0", "", "false"))
CFG.setdefault("BATCH_SITES", os.environ.get("BATCH_SITES", "Keep Friends,M-Team,HDSky,OurBits,HDHome"))
CFG.setdefault("BATCH_MIN_GB", float(os.environ.get("BATCH_MIN_GB", "2")))
CFG.setdefault("BATCH_MAX_GB", float(os.environ.get("BATCH_MAX_GB", "25")))

_BJOBS = {}
_AJOBS = {}
def _ajob_run(jid, artist):
    job = _AJOBS[jid]
    def log(m): job["log"].append(m)
    try:
        job["result"] = artist_albums(artist, log)
    except Exception as e:
        job["result"] = {"ok": False, "err": str(e)[:100]}
        logmsg("ERROR", f"歌手专辑失败[{artist}]: {e}")
    job["fin"] = True

def _pick_release(results, prefer_4k=False):
    """从一堆候选里挑一个最适合收藏的。规则(按优先级):
       ① 体积落在 BATCH_MIN_GB~MAX_GB 之间 —— 太小是渣画质,太大是原盘/REMUX 不适合批量囤;
       ② 做种数越多越好 —— 既下得快,也说明是公认的好版本;
       ③ 同做种数下优先 x265/HEVC(同画质体积小)。
       返回 None 表示这批候选都不合适,宁可不下也不下垃圾。"""
    lo, hi = CFG["BATCH_MIN_GB"] * 1024**3, CFG["BATCH_MAX_GB"] * 1024**3
    cand = [parse_release(r) for r in results if lo <= (r.get("size") or 0) <= hi]
    if not cand: return None
    return max(cand, key=lambda r: score_release(r, "collect", prefer_4k))

def _bjob_run(jid, col, total, prefer_4k):
    job = _BJOBS[jid]
    sites = [x for x in CFG["BATCH_SITES"].split(",") if x.strip()]
    ix = library_index()
    done = 0
    try:
        items = []
        start = 0
        while len(items) < total:
            d = douban_shelf(col, start, min(50, total - len(items)))
            got = d.get("items") or []
            if not got: break
            items.extend(got); start += len(got)
        items = items[:total]
        job["total"] = len(items)
        for it in items:
            if job.get("stop"): break
            done += 1; job["done"] = done
            job["cur"] = it["title"]
            owned = owned_label(ix, "", 0, it["title"], it.get("year") or "")
            if owned:
                job["rows"].append({"title": it["title"], "year": it.get("year") or "",
                                    "rate": it.get("rate") or "", "skip": "库里已有"})
                continue
            q = it["title"] + ((" " + it["year"]) if it.get("year") else "")
            try:
                name, year = split_query(q)
                anchor = query_anchor(name, year, "movie")
                res = prowlarr_search_fan([name], cats=FILTER_CATS["movie"], only=sites)
                g = search_group(q, res, anchor=anchor)
                grp = next((x for x in (g.get("groups") or []) if x.get("anchor")), None)
                pick = _pick_release(grp["results"], prefer_4k) if grp else None
            except Exception as e:
                job["rows"].append({"title": it["title"], "year": it.get("year") or "",
                                    "rate": it.get("rate") or "", "skip": f"搜索出错:{type(e).__name__}"})
                continue
            if not pick:
                job["rows"].append({"title": it["title"], "year": it.get("year") or "",
                                    "rate": it.get("rate") or "",
                                    "skip": f"没有 {CFG['BATCH_MIN_GB']:g}~{CFG['BATCH_MAX_GB']:g}GB 的合适版本"})
                continue
            job["rows"].append({"title": it["title"], "year": it.get("year") or "",
                                "rate": it.get("rate") or "", "tmdb": grp["name"],
                                "rel": pick["title"], "site": pick["site"], "size": pick["sizeh"],
                                "seeders": pick["seeders"], "url": pick["url"],
                                "noxfer": noxfer(pick["title"])})
            time.sleep(1.2)     # 节流:别把站点当压测靶子
    except Exception as e:
        job["err"] = str(e)[:120]
    job["fin"] = True

def batch_download(rows, cat=""):
    """把选中的种子推给 qb。逐个加、留间隔 —— 一秒钟几十个下载请求会被站点盯上。"""
    ok = fail = 0; errs = []
    for r in rows:
        try:
            data = prowlarr_download(r["url"])
            qb_conn().add(data, category=cat or CFG["QB_CATEGORY"] or "电影")
            ok += 1
            logmsg("INFO", f"榜单批量 → qb: {r.get('title','')} | {r.get('rel','')[:40]}")
        except Exception as e:
            fail += 1; errs.append(f"{r.get('title','?')}: {str(e)[:40]}")
        time.sleep(CFG["SNATCH_DELAY"] or 2)
    return {"ok": True, "added": ok, "failed": fail, "errs": errs[:8]}

# ============ 人物搜索(演员/导演片单) ============
# 和片名搜索反着来:片名搜索是「先扫66站→再识别成作品」,人物搜索是「先问 TMDB 要片单→只对你点的那部去搜种」。
# 一个演员动辄 70+ 部,全部扇出 = 70×66 次站点请求,能把 Prowlarr 和站点都打爆,所以这里绝不预搜种子。
_DEPT_CN = {"Acting": "演员", "Directing": "导演", "Writing": "编剧", "Production": "制片",
            "Sound": "音乐", "Camera": "摄影", "Editing": "剪辑", "Art": "美术",
            "Crew": "剧组", "Costume & Make-Up": "服化", "Visual Effects": "视效"}
# 主创才算「作品」,场务/助理之类挂名不列
_CREW_JOBS = {"Director", "Writer", "Screenplay", "Story", "Producer", "Executive Producer",
              "Novel", "Original Music Composer", "Creator"}
_CREW_CN = {"Director": "导演", "Writer": "编剧", "Screenplay": "编剧", "Story": "原著",
            "Producer": "制片", "Executive Producer": "监制", "Novel": "原著",
            "Original Music Composer": "配乐", "Creator": "创作"}

def tmdb_person_search(q):
    """搜人名 → 候选人物列表。同名的人不少(实测「王志文」返回 2 个),交给前端让用户点选,不闭眼取第一个。"""
    if not CFG["TMDB_KEY"]: return []
    try:
        rs = _tmdb_call("/search/person", query=q, language="zh-CN", include_adult="false").get("results", [])
    except Exception as e:
        logmsg("WARN", f"TMDB 人物搜索失败[{q}]: {e}"); return []
    out = []
    for r in rs[:8]:
        kf = [(k.get("title") or k.get("name") or "") for k in (r.get("known_for") or [])]
        dp = r.get("known_for_department") or ""
        out.append({"id": r.get("id"), "name": r.get("name") or "",
                    "dept": _DEPT_CN.get(dp, dp), "pop": round(r.get("popularity") or 0, 1),
                    "profile": r.get("profile_path") or "",
                    "known": " · ".join([x for x in kf if x][:3])})
    return out

def tmdb_person_credits(pid):
    """某人的全部影视作品 → 按电影/电视剧分组 + 标注哪些已入库。"""
    if not CFG["TMDB_KEY"]: return {"ok": False, "err": "未配置 TMDB Key"}
    try:
        info = _tmdb_call(f"/person/{pid}", language="zh-CN")
        cr = _tmdb_call(f"/person/{pid}/combined_credits", language="zh-CN")
    except Exception as e:
        return {"ok": False, "err": f"TMDB 取片单失败: {str(e)[:60]}"}
    ix = library_index()
    seen = {}
    def add(x, role, weight):
        mt = x.get("media_type")
        if mt not in ("movie", "tv"): return
        tid = x.get("id")
        if not tid: return
        k = f"{mt}:{tid}"
        g = seen.get(k)
        if not g:
            nm = x.get("title") or x.get("name") or ""
            yr = (x.get("release_date") or x.get("first_air_date") or "")[:4]
            g = seen[k] = {
                "id": tid, "mtype": mt, "name": nm, "year": yr,
                "poster": x.get("poster_path") or "", "overview": (x.get("overview") or "")[:150],
                "vote": x.get("vote_count") or 0, "pop": round(x.get("popularity") or 0, 1),
                "eps": x.get("episode_count") or 0, "roles": [], "w": 0,
                "owned": owned_label(ix, mt, tid, nm, yr)}
        if role and role not in g["roles"]: g["roles"].append(role)
        g["w"] = max(g["w"], weight)
        g["eps"] = max(g["eps"], x.get("episode_count") or 0)
    for x in (cr.get("cast") or []):
        add(x, (x.get("character") or "").strip(), 2)
    for x in (cr.get("crew") or []):
        job = x.get("job") or ""
        if job in _CREW_JOBS: add(x, _CREW_CN.get(job, job), 3 if job == "Director" else 1)
    items = list(seen.values())
    for g in items:
        g["role"] = " / ".join(g.pop("roles")[:2])
        # 没票没热度的多是客串/访谈/未上映,不删(删了容易误伤冷门老剧),标记出来让前端可折叠
        g["minor"] = g["vote"] < 3 and g["pop"] < 2.0
    items.sort(key=lambda g: (g["year"] or "0", g["w"], g["vote"]), reverse=True)
    mv = [g for g in items if g["mtype"] == "movie"]
    tv = [g for g in items if g["mtype"] == "tv"]
    own = len([g for g in items if g["owned"]])
    return {"ok": True,
            "person": {"id": pid, "name": info.get("name") or "",
                       "dept": _DEPT_CN.get(info.get("known_for_department") or "",
                                            info.get("known_for_department") or ""),
                       "profile": info.get("profile_path") or "",
                       "bio": (info.get("biography") or "")[:220],
                       "birth": (info.get("birthday") or "")[:4]},
            "movie": mv, "tv": tv,
            "stat": {"total": len(items), "owned": own, "movie": len(mv), "tv": len(tv)}}

def prowlarr_indexers():
    req = urllib.request.Request(CFG["PROWLARR_URL"] + "/api/v1/indexer", headers={"X-Api-Key": CFG["PROWLARR_KEY"]})
    return [i for i in json.load(urllib.request.urlopen(req, timeout=15)) if i.get("enable")]

# ---- 站点产出权重:大站资源多,收网门槛该由它们说了算,不能让只回 9 条的小站拖住 ----
_IXW = {"t": 0, "d": {}}
IX_MIN_SAMPLES = 2      # 攒够几次才算「摸清底细」
def ix_weights():
    """各站历史 {站名: (平均产出条数, 样本数)},缓存 5 分钟。"""
    if _IXW["d"] and time.time() - _IXW["t"] < 300: return _IXW["d"]
    d = {}
    try:
        c = db()
        c.execute("""CREATE TABLE IF NOT EXISTS ixstat(
            name TEXT PRIMARY KEY, n INTEGER DEFAULT 0, res INTEGER DEFAULT 0, ms INTEGER DEFAULT 0)""")
        for nm, n, res in c.execute("SELECT name,n,res FROM ixstat WHERE n>0").fetchall():
            # 上限 40:防某个巨站权重过大,它一家回来就触发收网,把别的大站全甩了
            d[nm] = (max(0.4, min(40.0, res / float(n))), n)
        c.close()
    except Exception: pass
    _IXW.update(t=time.time(), d=d)
    return d

def ix_w(W, name):
    return W.get(name, (1.0, 0))[0]

def ix_is_major(name, W):
    """手动点名的主力站(设置里的 MAJOR_SITES,按站名模糊匹配)。
       但如果它已经连着好多次一条都拿不回来(站挂了/cookie 过期),就别再让它拖着每一次搜索 ——
       点名的意思是「它有货时值得等」,不是「死了也要陪葬」。"""
    nl = (name or "").lower()
    if not any(t.strip() and t.strip().lower() in nl for t in CFG["MAJOR_SITES"].split(",")):
        return False
    w, n = W.get(name, (1.0, 0))
    return not (n >= 5 and w <= 0.4)

def ix_record(stats):
    """stats: {站名: 本次返回条数}。累加进历史,下次搜索的权重就更准。"""
    if not stats: return
    try:
        c = db()
        c.execute("""CREATE TABLE IF NOT EXISTS ixstat(
            name TEXT PRIMARY KEY, n INTEGER DEFAULT 0, res INTEGER DEFAULT 0, ms INTEGER DEFAULT 0)""")
        c.executemany("INSERT INTO ixstat(name,n,res) VALUES(?,1,?) "
                      "ON CONFLICT(name) DO UPDATE SET n=n+1, res=res+excluded.res",
                      [(k, int(v)) for k, v in stats.items()])
        c.commit(); c.close()
        _IXW["t"] = 0        # 让下次重新读
    except Exception: pass

def _ix_buckets(ix):
    """这个站声明支持哪些大类(2=影 3=乐 5=剧 7=书)。子分类也要算进去。"""
    out = set()
    for c in ((ix.get("capabilities") or {}).get("categories") or []):
        out.add((c.get("id") or 0) // 1000)
        for s in (c.get("subCategories") or []): out.add((s.get("id") or 0) // 1000)
    return out

def _ix_match(ix, cats):
    """按大类粗筛,不按具体子类 —— 宁可多问一个站,也不能因为人家没声明 5070 就漏掉动漫。
       声明信息拿不到时一律放行(未知 ≠ 没有)。"""
    if not cats: return True
    b = _ix_buckets(ix)
    if not b: return True
    return any((c // 1000) in b for c in cats)

# 前端那排类型按钮 → Torznab 分类号。先点类型再搜,站点那边就只回这一类,
# 少了电子书/评书音频/写真这些同名杂项,识别阶段也少跑几组。
FILTER_CATS = {"movie": [2000], "tv": [5000], "anime": [5070], "book": [7000], "music": [3000]}
FILTER_CN   = {"movie": "电影", "tv": "电视剧", "anime": "动漫", "book": "漫画/书", "music": "音乐"}

def prowlarr_search_fan(queries, log=lambda m: None, per_timeout=None, deadline=None, cats=None,
                        only=None, only_ids=None, status=None, workers=0):
    """⚠️ 交互搜索专用。辅种走 prowlarr_search(聚合接口),那边要的是「一个站都不能少」,
       本函数会主动丢慢站,用在辅种上会悄悄削掉覆盖面 —— 种子还在,只是活不长,极难察觉。

       MP式分站并发：每站独立请求 + 单站超时 + 全局截止。
       ① 线程池以前只有 32 个位子,66 个站要排两三波,一波 25 秒 → 光扇出就能耗掉 40 秒。
          现在一次性铺开,所有站同时发车,总耗时 = 最慢的那个站,而不是波数 × 波长。
       ② 再加一道全局截止:到点就带着已经拿到的结果收网,掉队的站不等 —— 搜索时间从此有上限。
       queries 可以是多个检索词(片名 + TMDB 别名),一起扔进同一个池,只花一波的时间。

       ⚠️ 上面那句「辅种别用本函数」已经作废(2026-08-19)。辅种现在**也**走这里,但走的是
       另一套 Policy:only_ids 精确点名要问的站、deadline 放到很大、status 出参逐站记成败。
       区别不在函数,在策略 —— 这正是重构的要点:取数只有一个入口,四条线用 Policy 分开。

       status: 传一个 dict 进来,函数按站名填 "ok"(问到了,不管有没有货) / "error"(超时或异常)。
               辅种靠它区分「这个站确实没有」和「这个站没问成」—— 老代码把两者混为一谈,
               所以缺种报告只能靠反推,注释里自己都写了「搜不到≠一定没有」。
       only_ids: 精确指定索引器 id 列表(不是模糊匹配站名)。辅种只问 coverage 里还欠的站。
       workers: 并发上限,0=按站数铺开。辅种压低它,别为了一份内容把 Prowlarr 打满。"""
    if isinstance(queries, str): queries = [queries]
    queries = list(dict.fromkeys([q.strip() for q in queries if q and q.strip()]))
    if not queries: return []
    per_timeout = per_timeout or CFG["SEARCH_TIMEOUT"]
    deadline    = deadline or CFG["SEARCH_DEADLINE"]
    try:
        idx = prowlarr_indexers()
        if not idx: raise RuntimeError("无可用站点")
    except Exception as e:
        log(f"⚠️ 取站点列表失败({str(e)[:30]})，退回聚合搜索"); return prowlarr_search(queries[0], cats)
    from concurrent.futures import ThreadPoolExecutor, wait
    if only_ids:
        want = {int(x) for x in only_ids}
        keep = [i for i in idx if int(i.get("id", 0)) in want]
        if keep:
            idx = keep
            log(f"🎯 只问账上还欠的 {len(keep)} 个站")
        else:
            log("✅ 这份内容各站都问过了,本轮无需再问"); return []
    if only:
        # 批量下载专用:一部片只需要一个好种,没必要问 66 个站。
        # 覆盖面交给后台辅种(那边走 prowlarr_search 全站),这里只求快、且不给站点添压力。
        keep = [i for i in idx if any(o.strip().lower() in i.get("name", "").lower()
                                      for o in only if o.strip())]
        if keep:
            log(f"🎯 只搜指定站点:{len(keep)} 个({'、'.join(i['name'] for i in keep[:6])})")
            idx = keep
    if cats:
        keep = [i for i in idx if _ix_match(i, cats)]
        if keep and len(keep) < len(idx):
            log(f"🗂 按类型筛站:{len(idx)} → {len(keep)} 个(其余站没有这一类,问了也是白问)")
            idx = keep
    tasks = [(ix, qq) for qq in queries for ix in idx]
    total = len(tasks)
    results = []; seen = set(); lock = threading.Lock(); done = [0]; ok = [0]
    W = ix_weights()
    wtot = sum(ix_w(W, i.get("name", "")) for i in idx) * len(queries)
    wdone = [0.0]; yield_ = {}
    # 「必须等」的站 = 主力站(产出达平均 2 倍) + 还没摸清底细的站。
    #   · 大站慢一点也得等 —— 丢了它丢的正是最该要的那批;小站慢就丢,它那点种大站基本都有。
    #   · 未知 ≠ 小!新站头几次必须给足时间,否则会锁死:没历史→被当小站丢→丢了就没产出记录
    #     →权重永远涨不起来→永远被丢。攒够 IX_MIN_SAMPLES 次(成功失败都算)才开始按权重区别对待。
    #   · 设置里手动点名的站(MAJOR_SITES)一律算主力,不管它历史产出多少。
    avgw = (wtot / len(queries)) / max(1, len(idx))
    left = set(); named = []
    for i in idx:
        nm = i.get("name", "")
        w, n = W.get(nm, (1.0, 0))
        if ix_is_major(nm, W):
            left.add(nm); named.append(nm)
        elif n < IX_MIN_SAMPLES or w >= max(2.0, avgw * 2):
            left.add(nm)
    if named:
        log(f"⭐ 主力站(点名必等): {'、'.join(sorted(named))}")
    catq = "".join("&categories=" + str(c) for c in (cats or []))
    def one(t):
        ix, qq = t
        u = (CFG["PROWLARR_URL"] + "/api/v1/search?query=" + urllib.parse.quote(qq)
             + "&type=search&indexerIds=" + str(ix["id"]) + catq)
        req = urllib.request.Request(u, headers={"X-Api-Key": CFG["PROWLARR_KEY"]})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=per_timeout))
        except Exception:
            with lock:
                done[0] += 1; wdone[0] += ix_w(W, ix.get("name",""))
                # 失败也要记 0 产出:否则某个大站彻底挂了,它的历史权重还挂在高位,
                # 「等主力站」这条规则就会让每次搜索都陪它耗到硬截止。记了 0,均值自己会掉下来。
                nm = ix.get("name", "?")
                yield_[nm] = yield_.get(nm, 0)
                left.discard(nm)
                if status is not None: status[nm] = "error"     # 没问成 ≠ 这站没有
                log(f"  ✗ {nm} 超时/失败，跳过 · 进度 {done[0]}/{total}")
            return
        with lock:
            done[0] += 1; wdone[0] += ix_w(W, ix.get("name","")); n = 0
            for it in (r or []):
                key = it.get("guid") or it.get("downloadUrl") or ""
                if key and key in seen: continue      # 多检索词会撞出同一个种,按 guid 去重
                if key: seen.add(key)
                results.append(it); n += 1
            yield_[ix.get("name","?")] = yield_.get(ix.get("name","?"), 0) + len(r or [])
            left.discard(ix.get("name","?"))
            if status is not None: status[ix.get("name","?")] = "ok"   # 问成了(有没有货是另一回事)
            if n:
                ok[0] += 1
                log(f"  ✓ {ix.get('name','?')} 返回 {n} 条 · 进度 {done[0]}/{total}")
            else:
                log(f"  · {ix.get('name','?')} 无结果 · 进度 {done[0]}/{total}")
    # 动态收网。实测这 66 个站:p50=2.9s、p80=4.0s,但尾巴上有 4 个站要 13~30 秒。
    # 死等固定超时 = 每次搜索都按最慢的那个站计时;而 8 秒能拿 87% 的结果、12 秒也才 92%,
    # 多等的 4 秒换来的 5% 还基本是快站上已有的重复种。
    # 门槛按「产出权重」算而不是按站点个数:M-Team 这种大站回来一个顶小站好几个,
    # 只回 9 条的小站没资格拖着大部队。权重来自各站历史平均产出(ix_weights),自己学。
    ex = ThreadPoolExecutor(max_workers=min(total, workers or 96))
    need = wtot * CFG["SEARCH_QUORUM"] / 100.0
    cutoff = None
    try:
        fs = [ex.submit(one, t) for t in tasks]
        t0 = time.time()
        while True:
            el = time.time() - t0
            with lock: dn, wd = done[0], wdone[0]
            if dn >= total or el >= deadline: break
            with lock: nleft = len(left)
            if cutoff is None and wd >= need and not nleft:
                cutoff = min(el + CFG["SEARCH_GRACE"], deadline)
                log(f"⚡ 主力站已全部归航({el:.1f}s,产出权重 {wd:.0f}/{wtot:.0f}),"
                    f"再给掉队的小站 {CFG['SEARCH_GRACE']:g} 秒就收网")
            if cutoff is not None and el >= cutoff: break
            time.sleep(0.2)
    finally:
        # 掉队线程还在跑,但它们只往加锁的 results 里追加,拿快照就不受影响
        try: ex.shutdown(wait=False, cancel_futures=True)
        except TypeError: ex.shutdown(wait=False)      # 3.8 及以下没有 cancel_futures
    with lock:
        snap = list(results); fin = done[0]; yl = dict(yield_)
    ix_record(yl)          # 把本次各站产出记进历史,权重越用越准
    if fin < total:
        log(f"⏰ 收网:{total - fin} 路还没回,不等了(慢站的种基本快站也有)")
    log(f"📦 {ok[0]}/{total} 路有结果，共 {len(snap)} 条")
    return snap

_SJOBS = {}
def _sjob_run(jid, q, filt=""):
    job = _SJOBS[jid]
    job.setdefault("prog", {"done": 0, "total": 0, "hits": 0, "stage": "启航"})
    def log(m):
        job["log"].append(m)
        # 顺带解析出结构化进度,前端只画浪不读日志
        mm = re.search(r"进度 (\d+)/(\d+)", m)
        if mm:
            job["prog"]["done"] = int(mm.group(1)); job["prog"]["total"] = int(mm.group(2))
            job["prog"]["stage"] = "巡海"
        mh = re.search(r"返回 (\d+) 条", m)
        if mh: job["prog"]["hits"] += int(mh.group(1))
    try:
        name, year = split_query(q)
        cats = FILTER_CATS.get(filt)
        if cats:
            log(f"🎯 已限定「{FILTER_CN[filt]}」,只搜这一类 —— 同名的电子书/评书/写真不会再混进来")
        anchor = query_anchor(name, year, filt)
        if anchor:
            log(f"🎯 已钉住《{anchor['name']}》({anchor['year']})"
                + (f" · 年份限定 {year}" if year else "") + f" · 收到 {len(anchor['alias'])} 个别名")
        else:
            log(f"ℹ️ TMDB 没钉住「{name}」,按原词全站搜")
        pol = POLICY["find"]
        log(f"🚀 分站并发搜索(单站 {pol['timeout']} 秒超时,全局 {pol['deadline']} 秒收网)…")
        t0 = time.time()
        results = prowlarr_search_fan([name], log, per_timeout=pol["timeout"],
                                      deadline=pol["deadline"], cats=cats, workers=pol["workers"])
        # 别名兜底:只在主查询确实没捞着的时候才补(同站串行,会实打实多花几秒)。
        # altqs 已经按命名体系去过重,补的是**不同的召回入口**,不是同一个词的三种拼写。
        alts = (anchor or {}).get("altqs") or []
        if anchor and CFG["SEARCH_ALIAS"] and alts:
            hit = sum(1 for r in results if _alias_hit(r.get("title", ""), anchor["alias"]))
            if hit < CFG["SEARCH_ALIAS_MIN"]:
                log(f"🔁 主查询只认出 {hit} 条,用别名「{'」「'.join(alts)}」再补一波")
                seen = {r.get("guid") or r.get("downloadUrl") or "" for r in results}
                for r in prowlarr_search_fan(alts, log, per_timeout=pol["timeout"],
                                             deadline=pol["deadline"], cats=cats, workers=pol["workers"]):
                    k = r.get("guid") or r.get("downloadUrl") or ""
                    if k and k in seen: continue
                    if k: seen.add(k)
                    results.append(r)
            else:
                log(f"✅ 主查询已认出 {hit} 条,不用别名补搜了(省一波,同站请求 Prowlarr 会排队)")
        job["prog"]["stage"] = "归航"
        log(f"⏱ 搜索耗时 {int(time.time()-t0)} 秒。做种数过滤 + TMDB 识别配图…")
        job["result"] = search_group(q, results, log, anchor=anchor)
        log("✅ 完成")
    except Exception as e:
        job["result"] = {"ok": False, "err": str(e)[:80]}
        log(f"❌ 失败: {str(e)[:60]}")
    job["done"] = True

# ============ 保种 / 缺种 / 转种(资深PT三件套) ============
# 禁转红线:命中任一标记坚决不转,不给确认后门(转了要被请喝茶的)
_NOXFER = re.compile(r'禁转|禁止转|独家|独占|首发禁|exclusive|excl\b|\[禁\]', re.I)
def noxfer(title): return bool(_NOXFER.search(title or ""))

def prowlarr_browse(ixid, query="", offset=0, limit=100):
    """拉某站种子列表:空关键词=最新种子,Prowlarr 透传站点翻页"""
    u = (CFG["PROWLARR_URL"] + "/api/v1/search?query=" + urllib.parse.quote(query)
         + f"&type=search&indexerIds={int(ixid)}&limit={int(limit)}&offset={int(offset)}")
    req = urllib.request.Request(u, headers={"X-Api-Key": CFG["PROWLARR_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=60))

# ---- 直连站点扒列表页:Prowlarr 搜索接口大多不支持翻页(实测第2页返回0条),整站保种只能自己来 ----
_SITE_CACHE = {}
def site_conn(ixid):
    """从 Prowlarr 索引器配置里拿站点 baseUrl + Cookie(缓存10分钟)"""
    ixid = int(ixid)
    hit = _SITE_CACHE.get(ixid)
    if hit and time.time() - hit[2] < 600: return hit[0], hit[1]
    req = urllib.request.Request(CFG["PROWLARR_URL"] + f"/api/v1/indexer/{ixid}",
                                 headers={"X-Api-Key": CFG["PROWLARR_KEY"]})
    d = json.load(urllib.request.urlopen(req, timeout=15))
    base = (d.get("indexerUrls") or [""])[0].rstrip("/")
    ck = next((f.get("value") for f in d.get("fields", [])
               if "cookie" in (f.get("name") or "").lower() and f.get("value")), "")
    _SITE_CACHE[ixid] = (base, ck, time.time())
    return base, ck

_NEXUS_ROW = re.compile(r'<a\s[^>]*?title="([^"]+)"[^>]*?href="details\.php\?id=(\d+)')     # title在前(空格数不定)
_NEXUS_ROW2 = re.compile(r'<a\s[^>]*?href="details\.php\?id=(\d+)[^"]*"[^>]*?title="([^"]+)"')  # href在前的站
_NEXUS_UNIT = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
def nexus_browse(ixid, page):
    """扒 NexusPHP 站 torrents.php 第 page 页(0起),解析标题/体积/做种数/下载链接"""
    import html as _html
    base, ck = site_conn(ixid)
    if not base or not ck: raise RuntimeError("该站在Prowlarr里没有Cookie")
    req = urllib.request.Request(f"{base}/torrents.php?page={int(page)}&incldead=1",
                                 headers={"Cookie": ck, "User-Agent": "Mozilla/5.0"})
    h = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    if "login.php" in h[:3000] and "logout" not in h: raise RuntimeError("Cookie失效,去Prowlarr更新")
    ms = [(m, m.group(1), m.group(2)) for m in _NEXUS_ROW.finditer(h)]
    if not ms:
        ms = [(m, m.group(2), m.group(1)) for m in _NEXUS_ROW2.finditer(h)]
    items = []
    for k, (m, title, tid) in enumerate(ms):
        seg = h[m.end(): ms[k+1][0].start() if k+1 < len(ms) else m.end()+6000]
        if f"download.php?id={tid}" not in seg: continue   # 不是种子行(公告等)
        sz = sd = 0; dt = ""
        sm = re.search(r'>([\d.]+)<br ?/?>\s*(TB|GB|MB|KB)<', seg)
        if sm: sz = int(float(sm.group(1)) * _NEXUS_UNIT[sm.group(2)])
        dm = re.search(r'#seeders">(?:<b>)?(\d+)', seg)
        if dm: sd = int(dm.group(1))
        tm = re.search(r'<span title="(\d{4}-\d{2}-\d{2})', seg)
        if tm: dt = tm.group(1)
        # NexusPHP 优惠标记: pro_free / pro_free2up = 免费(下载不计流量,刷上传神种)
        free = 1 if ("pro_free" in seg or "pro_twoupfree" in seg) else 0
        # 官种标记:站点自制/官方组发布,有些站保种考核只认官种
        off = 1 if re.search(r'>\s*(官方|官种|官組|官组)\s*<', seg) else 0
        items.append({"title": _html.unescape(title), "size": sz, "seeders": sd, "free": free, "off": off,
                      "downloadUrl": f"{base}/download.php?id={tid}", "publishDate": dt})
    return items

def ks_browse(ixid, query, page):
    """保种列表统一入口:无关键词→直连扒页(真分页);有关键词/直连失败→Prowlarr搜索兜底"""
    if not query:
        try: return nexus_browse(ixid, page)
        except Exception as e:
            if page > 0: raise                     # 深页只有直连能给,失败就明说
            logmsg("WARN", f"直连扒页失败({str(e)[:36]}),回退Prowlarr")
    return prowlarr_browse(ixid, query, page * 100)

def ks_download(url, ixid):
    """下载种子:站点直链带Cookie,Prowlarr代理链走APIKey"""
    if "download.php" in url and ixid:
        base, ck = site_conn(ixid)
        req = urllib.request.Request(url, headers={"Cookie": ck, "User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(req, timeout=60).read()
    return prowlarr_download(url)

_KS = {"running": False, "stop": False, "msg": "", "cur": ""}
def keepseed_worker():
    """批量保种执行器:逐个下载种子推 qb,节流 + 磁盘水位保护"""
    _KS.update(running=True, stop=False, msg="")
    fails = 0
    try:
        while not _KS["stop"]:
            free_gb = shutil.disk_usage("/data").free / 2**30
            if free_gb < CFG["KEEP_MIN_FREE_GB"]:
                _KS["msg"] = f"⛔ 磁盘剩余 {free_gb:.0f}GB 低于保护线 {CFG['KEEP_MIN_FREE_GB']}GB,任务暂停"
                logmsg("WARN", "批量保种触发磁盘保护线,暂停"); break
            c = db(); row = c.execute("SELECT id,name,size,url,indexer FROM keepseed WHERE status='queued' ORDER BY id LIMIT 1").fetchone(); c.close()
            if not row: _KS["msg"] = "✅ 队列清空"; break
            rid, name, size, url, ix = row
            _KS["cur"] = name[:48]
            try:
                data = ks_download(url, int(ix) if str(ix).isdigit() else 0)
                if data[:1] != b'd': raise RuntimeError("非种子文件")
                res = qb_conn().add(data, category="保种", tags="packseed,keepseed", savepath=CFG["KEEP_DIR"])
                st, err = ("pushed", "") if "Ok" in res else ("error", (res.strip()[:40] or "qb拒绝"))
            except Exception as e:
                st, err = "error", str(e)[:60]
            c = db(); c.execute("UPDATE keepseed SET status=?, err=? WHERE id=?", (st, err, rid)); c.commit(); c.close()
            if st == "error":
                logmsg("WARN", f"保种拉取失败 {name[:36]}: {err}")
                fails += 1
                # 熔断:连续失败多半是 qb 挂了/被自己登封禁了,继续猛推只会雪上加霜
                if fails >= 5:
                    _KS["msg"] = f"⛔ 连续 {fails} 个失败(最后一个: {err}),已自动暂停。修好后点「♻️ 重试失败」"
                    logmsg("ERROR", f"批量保种连续失败{fails}次,熔断暂停: {err}")
                    notify("⛔ 保种任务已暂停", f"连续 {fails} 个推送失败\n{err}\n检查 qb 是否正常,修好后去面板重试")
                    break
                time.sleep(10)                        # 失败后多喘一会儿
            else:
                fails = 0
            time.sleep(max(CFG["SNATCH_DELAY"], 3))   # 节流:别把站打炸
    finally:
        _KS.update(running=False, cur="")

_KSF = {"running": False, "stop": False, "msg": ""}
def ks_autofill(ixid, query, target_gb, max_gb, max_seed, free_only=False, off_only=False):
    """自动拉满:后台翻页拉站内列表,按筛选条件入队,累计到目标体积收工。边拉边下。"""
    _KSF.update(running=True, stop=False, msg="🤖 自动拉取启动…")
    try:
        # 去重底账:保种队列历史 + tr 里已做种的(名字+大小)
        c = db()
        have = {(r[0], r[1]) for r in c.execute("SELECT name,size FROM keepseed").fetchall()}
        c.close()
        try: have |= {(t["name"], t["totalSize"]) for t in tr_conn().torrents()}
        except Exception: pass
        total = added = 0; target = target_gb * 2**30; seen_urls = set()
        for page in range(400):           # 页数兜底,防意外空转
            if _KSF["stop"]: _KSF["msg"] = f"⏹ 已停止:入队{added}个 {human_size(total)}"; break
            try:
                rs = ks_browse(ixid, query, page)
            except Exception as e:
                _KSF["msg"] = f"⚠️ 第{page+1}页拉取失败({str(e)[:36]}),已入队{added}个 {human_size(total)}"; break
            newu = [r for r in rs if r.get("downloadUrl") and r["downloadUrl"] not in seen_urls]
            if not newu:
                _KSF["msg"] = f"📄 站点翻到底了:入队{added}个 {human_size(total)}(目标{target_gb}GB)"; break
            c = db()
            for r in newu:
                seen_urls.add(r["downloadUrl"])
                nm, sz, sd = r.get("title") or "", r.get("size") or 0, r.get("seeders", 0) or 0
                if sz <= 0: continue
                if free_only and not r.get("free"): continue
                if off_only and not r.get("off"): continue
                if max_gb and sz > max_gb * 2**30: continue
                if max_seed is not None and sd > max_seed: continue
                if (nm, sz) in have: continue       # 队列里有过/tr已做种,不重复下
                have.add((nm, sz))
                c.execute("INSERT INTO keepseed(name,size,url,indexer,status,err,ts) VALUES(?,?,?,?,?,?,?)",
                          (nm, sz, r["downloadUrl"], str(ixid), "queued", "", int(time.time())))
                total += sz; added += 1
                if total >= target: break
            c.commit(); c.close()
            if added and not _KS["running"]:        # 边拉边下,不等翻完
                threading.Thread(target=keepseed_worker, daemon=True).start()
            if total >= target:
                _KSF["msg"] = f"✅ 目标达成:入队{added}个,共{human_size(total)}"; break
            _KSF["msg"] = f"🤖 已翻{page+1}页 · 入队{added}个 · {human_size(total)} / 目标{target_gb}GB"
            time.sleep(2)                           # 页间节流
        logmsg("INFO", f"自动保种拉取结束: {added}个 {human_size(total)}")
        if added and not _KS["running"]:
            threading.Thread(target=keepseed_worker, daemon=True).start()
    finally:
        _KSF["running"] = False

_FW = {"msg": "还没开始", "grab": 0}
def free_watcher():
    """抢免费守候:定时盯站点最新页,新出的免费种自动入队抢下(刷上传:免费下载+做种回吐)"""
    time.sleep(30)
    while True:
        ix = 0
        try: ix = int(CFG["FREE_WATCH_IX"] or 0)
        except Exception: pass
        if ix:
            try:
                free_gb = shutil.disk_usage("/data").free / 2**30
                if free_gb < CFG["KEEP_MIN_FREE_GB"]:
                    _FW["msg"] = f"⛔ 磁盘低于保护线,守候暂停({free_gb:.0f}GB)"
                else:
                    items = nexus_browse(ix, 0)
                    c = db()
                    have = {(r[0], r[1]) for r in c.execute("SELECT name,size FROM keepseed").fetchall()}
                    n = 0
                    for it in items:
                        if not it.get("free"): continue
                        if CFG["FREE_OFFICIAL"] and not it.get("off"): continue   # 只认官种模式
                        if CFG["FREE_MAX_GB"] and it["size"] > CFG["FREE_MAX_GB"] * 2**30: continue
                        if (it["title"], it["size"]) in have: continue
                        c.execute("INSERT INTO keepseed(name,size,url,indexer,status,err,ts) VALUES(?,?,?,?,?,?,?)",
                                  (it["title"], it["size"], it["downloadUrl"], str(ix), "queued", "", int(time.time())))
                        n += 1
                    c.commit(); c.close()
                    if n:
                        _FW["grab"] += n
                        logmsg("INFO", f"⚡ 抢到 {n} 个新免费种,累计 {_FW['grab']}")
                        notify("⚡ 抢免费", f"新入 {n} 个免费种(累计{_FW['grab']}),已推下载")
                        if not _KS["running"]:
                            threading.Thread(target=keepseed_worker, daemon=True).start()
                    _FW["msg"] = f"{time.strftime('%H:%M')} 巡查:页内免费 {sum(1 for i in items if i.get('free'))} 个,新抢 {n},累计 {_FW['grab']}"
            except Exception as e:
                _FW["msg"] = f"⚠️ 巡查失败: {str(e)[:40]}"
        time.sleep(max(180, CFG["FREE_WATCH_MIN"] * 60))   # 最短3分钟,别把站刷毛了

def gap_report():
    """缺种矩阵:直接读 coverage —— 这是**记下来的事实**,不是反推的猜测。

       老版从 matches 表倒推「哪些站没出现过」,把两件完全不同的事混成一个「缺」:
         · 问过了,这个站确实没有   → 真缺,可以去发种
         · 压根没问过               → 不知道,不是缺
       混在一起的后果是缺种列表虚高,文案只能写「搜不到≠一定没有」,还得另加一个
       「🔍 逐站核实」按钮去补救。现在这两件事在账上就是分开的,不需要补救。"""
    urlmap, all_sites = site_urlmap()
    rows = []
    for rec in led_recent(120, skip_role="stock"):
        cid, name, size = rec["cid"], rec["name"], rec["size"]
        cov = led_cov_get(cid)
        seeded  = sorted(s_ for s_, v in cov.items() if v[0] in ("seeding", "source"))
        absent  = sorted(s_ for s_, v in cov.items() if v[0] == "absent")
        errs    = sorted(s_ for s_, v in cov.items() if v[0] == "error")
        banned  = sorted(s_ for s_, v in cov.items() if v[0] == "banned")
        pending = sorted(led_cov_pending(cid, all_sites))
        ih = led_any_hash(cid)
        rows.append({"name": name, "hash": ih, "cid": cid, "sizeh": human_size(size or 0),
                     "seeded": seeded, "absent": absent, "errs": errs,
                     "pending": [p for p in pending if p not in errs],
                     "banned": banned,
                     # missing 保留给老前端:语义收窄成「问过了确实没有」,不再掺没问过的
                     "missing": absent})
    # 排序:账上还欠得最多的排最前 —— 那才是「还没辅全」,而不是「辅不到」
    rows.sort(key=lambda x: (-(len(x["pending"]) + len(x["errs"])), -len(x["absent"])))
    return {"ok": True, "sites": len(all_sites), "rows": rows,
            "stat": led_cov_stats()}

_BATCH = {"research": False, "msg": ""}
def research_all_worker():
    _BATCH["research"] = True
    try:
        c = db(); rows = c.execute("SELECT info_hash,query,name,size FROM torrents WHERE status='no_match' GROUP BY name,size").fetchall(); c.close()
        logmsg("INFO", f"🔁 批量重搜 {len(rows)} 个无匹配内容…")
        for i, (ih, q, name, _sz) in enumerate(rows):
            _BATCH["msg"] = f"重搜中 {i+1}/{len(rows)}: {name[:24]}"
            try: manual_research(ih, q or extract_query(name) or name)
            except Exception as e: logmsg("WARN", f"重搜失败 {name[:24]}: {str(e)[:30]}")
            time.sleep(3)   # 节流,别打爆站
        _BATCH["msg"] = f"✅ 批量重搜完成({len(rows)} 个)"
        logmsg("INFO", "批量重搜完成")
    finally:
        _BATCH["research"] = False

# ============ 媒体库体检 ============
# 为什么要常驻而不是写个一次性脚本:分类和目录结构会**持续**出问题 ——
# 欧美式发布名的动漫认不出字幕组标记、TMDB 有些条目压根没填类型、新剧陆续下载导致季目录缺失。
# 头痛医头的话每隔一阵就得重新翻一遍库。做成体检:随时能查、能一键修、修完自证。
_LIBAUDIT = {"ts": 0, "d": None}

_GENRE_CACHE = {}       # "tv:123" → True/False/None,一个条目的类型不会天天变,存久点
def _genre_anime(mtype, tid):
    """问 TMDB 这个条目是不是动画(genre 16)。返回 True/False/None(查不到或条目没填类型)。"""
    k = f"{mtype or 'tv'}:{tid}"
    if k in _GENRE_CACHE: return _GENRE_CACHE[k]
    v = None
    try:
        d = tmdb_details(mtype or "tv", tid)
        gs = d.get("genres")
        # 条目本身没填类型(TMDB 上的劣质用户条目) → None,判不了,交给人
        v = any(g.get("id") == 16 for g in gs) if gs else None
    except Exception:
        v = None
    if len(_GENRE_CACHE) > 3000: _GENRE_CACHE.clear()
    _GENRE_CACHE[k] = v
    return v

def _genre_warm(rows):
    """并发把这批条目的类型问回来填进缓存。
       串行查 63 部要 44 秒(单次 0.7s),浏览器等不及就断了 —— 体检必须并发。"""
    todo = {(mt or "tv", tid) for _t, mt, tid, _n, _c in rows
            if tid and f"{mt or 'tv'}:{tid}" not in _GENRE_CACHE}
    if not todo: return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda x: _genre_anime(x[0], x[1]), todo))

_SDIR_RE = re.compile(r'^Season\s+\d{1,2}$', re.I)
def library_audit(force=False):
    """扫媒体库,找出三类毛病:
       ① 分类错  —— TMDB 说是动画却在剧集库(或反过来)
       ② 结构乱  —— 剧集文件平铺在根目录,没有 Season XX 子目录
       ③ 判不了  —— TMDB 条目缺类型信息,得人工改识别
       只报告不修改;修由 /api/libfix 执行。"""
    if not force and _LIBAUDIT["d"] and time.time() - _LIBAUDIT["ts"] < 600:
        return _LIBAUDIT["d"]
    wrong_cat, flat, unknown, missing, extras = [], [], [], [], []
    try:
        c = db()
        rows = c.execute("SELECT DISTINCT target,mtype,tmdbid,tmdb_name,cat FROM media "
                         "WHERE status='done' AND target!='' AND tmdbid IS NOT NULL").fetchall()
        c.close()
    except Exception as e:
        return {"ok": False, "err": str(e)[:80]}
    _genre_warm(rows)          # 先并发把类型问回来,后面循环就全是缓存命中
    anime_root = CFG["MEDIA_ANIME"] or CFG["MEDIA_TV"]
    for tgt, mtype, tid, tname, cat in rows:
        if not os.path.isdir(tgt):
            missing.append({"name": tname, "target": tgt}); continue
        ani = _genre_anime(mtype, tid)
        want_root = (anime_root if ani else
                     (CFG["MEDIA_MOVIE"] if mtype == "movie" else CFG["MEDIA_TV"]))
        if ani is None:
            unknown.append({"name": tname, "tmdbid": tid, "target": tgt})
        elif CFG["MEDIA_ANIME"] and os.path.dirname(tgt.rstrip("/")) != want_root.rstrip("/"):
            wrong_cat.append({"name": tname, "tmdbid": tid, "target": tgt,
                              "to": os.path.join(want_root, os.path.basename(tgt)),
                              "why": "TMDB 标为动画" if ani else "TMDB 未标为动画"})
        if (mtype or "tv") == "tv":
            # 根目录直接躺着剧集文件 = 没分季
            loose = {}; loose_names = []; has_sdir = False
            try:
                for f in os.listdir(tgt):
                    fp = os.path.join(tgt, f)
                    if os.path.isdir(fp):
                        if _SDIR_RE.match(f): has_sdir = True
                        continue
                    if os.path.splitext(f)[1].lower() not in _VIDEO_EXT: continue
                    ss = _season_of(f)
                    loose.setdefault(ss or 0, 0)
                    loose[ss or 0] += 1
                    if not ss: loose_names.append(f)
            except Exception: continue
            if loose:
                tot_f = sum(loose.values()); noseason = loose.get(0, 0)
                # 一部剧里超过 30% 的文件抠不出季号(命名不统一,比如「中国四大名著」这种四部合集),
                # 硬分季会变成「一部分进 Season 01、一部分留根目录」的半吊子状态,比全平铺更糟。
                # 这种整部跳过、单列出来交给人 —— 自动化要知道自己什么时候不该动手。
                messy = tot_f and noseason / tot_f > 0.30
                item = {"name": tname, "target": tgt,
                        "seasons": {str(k): v for k, v in sorted(loose.items())},
                        "files": tot_f, "noseason": noseason}
                if messy and has_sdir and noseason == tot_f:
                    # Season 目录已经齐了、顶层只剩几个抠不出季号的 —— 这不是「命名乱」,
                    # 是 SP/片头片尾/纪录片/衍生电影这类**特典没归位**(实测 12 部全是这样)。
                    # 判它不需要猜季号,所以能自动修:Emby 的惯例是特典进 Season 00(特别篇)。
                    item["why"] = ("Season 目录已齐,%d 个特典散在根目录:" % tot_f) + "、".join(
                        os.path.splitext(x)[0][:26] for x in loose_names[:3]) + ("…" if tot_f > 3 else "")
                    item["names"] = loose_names
                    extras.append(item)
                elif messy:
                    item["why"] = f"{noseason}/{tot_f} 个文件解析不出季号,命名不统一,自动分季只会更乱"
                    unknown.append(item)
                else:
                    flat.append(item)
    d = {"ok": True, "wrong_cat": wrong_cat, "flat": flat, "unknown": unknown, "missing": missing,
         "extras": extras, "total": len(rows), "ts": int(time.time())}
    _LIBAUDIT.update(ts=time.time(), d=d)
    return d

def library_fix(do_cat=True, do_season=True, do_specials=True):
    """按体检结果修正。全部用 os.rename:
       媒体库文件是硬链接(和下载目录共享 inode),rename 只改目录项不动数据,**做种完全不受影响**。
       同文件系统内是原子操作,不复制、不占额外空间。"""
    a = library_audit(force=True)
    if not a.get("ok"): return a
    moved = seasoned = 0; errs = []
    remap = {}
    if do_cat:
        for it in a["wrong_cat"]:
            src, dst = it["target"], it["to"]
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst): errs.append(f"{it['name']}: 目标已存在,跳过"); continue
                os.rename(src, dst); remap[src] = dst; moved += 1
                logmsg("INFO", f"媒体库归位: {it['name']} → {dst}")
            except Exception as e:
                errs.append(f"{it['name']}: {str(e)[:50]}")
    if do_season:
        for it in a["flat"]:
            base = remap.get(it["target"], it["target"])
            if not os.path.isdir(base): continue
            n = 0
            try:
                for f in list(os.listdir(base)):
                    fp = os.path.join(base, f)
                    if not os.path.isfile(fp): continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in _VIDEO_EXT and ext not in (".srt", ".ass", ".sub", ".nfo"): continue
                    if f in ("tvshow.nfo", "movie.nfo"): continue
                    ss = _season_of(f)
                    if not ss: continue        # 抠不到季号的原地不动,不瞎猜
                    sd = os.path.join(base, f"Season {ss:02d}")
                    os.makedirs(sd, exist_ok=True)
                    dst = os.path.join(sd, f)
                    if os.path.exists(dst): continue
                    os.rename(fp, dst); n += 1
                if n: seasoned += 1; logmsg("INFO", f"媒体库分季: {it['name']} 移动 {n} 个文件")
            except Exception as e:
                errs.append(f"{it['name']}: {str(e)[:50]}")
    specials = 0
    if do_specials:
        # 特典进 Season 00。**保留原文件名**:这些片子多半不在 TMDB 的特别篇列表里,
        # 硬改成 S00E01 会被 Emby 匹配成别的特典、挂上错的标题;留原名至少名字是对的。
        for it in a.get("extras", []):
            base = remap.get(it["target"], it["target"])
            if not os.path.isdir(base): continue
            sd = os.path.join(base, "Season 00"); n = 0
            try:
                for f in it.get("names", []):
                    if not os.path.isfile(os.path.join(base, f)): continue
                    os.makedirs(sd, exist_ok=True)
                    stem = os.path.splitext(f)[0]
                    for g in [f] + [stem + e for e in (".srt", ".ass", ".sub", ".nfo")]:
                        gp = os.path.join(base, g)
                        if not os.path.isfile(gp): continue
                        dst = os.path.join(sd, g)
                        if os.path.exists(dst): continue
                        os.rename(gp, dst)
                    n += 1
                if n:
                    specials += 1
                    logmsg("INFO", f"特典归位: {it['name']} → Season 00 ({n} 个)")
            except Exception as e:
                errs.append(f"{it['name']}: {str(e)[:50]}")
    if remap:
        try:
            c = db()
            for o, nw in remap.items(): c.execute("UPDATE media SET target=? WHERE target=?", (nw, o))
            c.commit(); c.close()
        except Exception as e: errs.append(f"数据库更新失败: {str(e)[:40]}")
    _LIBAUDIT["d"] = None
    try: emby_refresh()
    except Exception: pass
    return {"ok": True, "moved": moved, "seasoned": seasoned, "specials": specials, "errs": errs}

def library_setcat(target, cat):
    """人工指定某部片的分类并立刻搬库。
       用于 TMDB 条目没填类型、自动判不了的情况(比如猫和老鼠那个 tmdbid=325591)。
       比"为了让自动分类认出来而去挂一个内容不匹配的 TMDB 条目"正确得多 ——
       那样会让每集标题/剧情全部错位。分类写进库,以后体检不再报它。"""
    roots = {"动漫": CFG["MEDIA_ANIME"] or CFG["MEDIA_TV"],
             "电视剧": CFG["MEDIA_TV"], "电影": CFG["MEDIA_MOVIE"]}
    if cat not in roots: return {"ok": False, "err": "分类只能是 动漫/电视剧/电影"}
    if not os.path.isdir(target): return {"ok": False, "err": "目录不存在"}
    dst = os.path.join(roots[cat], os.path.basename(target.rstrip("/")))
    try:
        if os.path.realpath(dst) != os.path.realpath(target):
            if os.path.exists(dst): return {"ok": False, "err": "目标已存在,请先处理"}
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(target, dst)
        c = db()
        c.execute("UPDATE media SET cat=?, target=? WHERE target=?", (cat, dst, target))
        c.commit(); c.close()
        _LIBAUDIT["d"] = None
        logmsg("INFO", f"人工归类: {os.path.basename(dst)} → {cat}")
        try: emby_refresh()
        except Exception: pass
        return {"ok": True, "to": dst}
    except Exception as e:
        return {"ok": False, "err": str(e)[:80]}

_HEALTH_CACHE = {"ts": 0, "d": None}
def health_report(force=False):
    """做种健康体检:tracker掉线(白做)、0-peer冷种、tr错误种子。缓存5分钟(遍历几千种较重)"""
    if not force and _HEALTH_CACHE["d"] and time.time() - _HEALTH_CACHE["ts"] < 300:
        return _HEALTH_CACHE["d"]
    fields = ["name", "totalSize", "status", "error", "errorString", "peersConnected",
              "activityDate", "trackerStats", "downloadDir", "uploadRatio", "uploadedEver"]
    try:
        ts = tr_conn().call("torrent-get", {"fields": fields}).get("arguments", {}).get("torrents", [])
    except Exception as e:
        return {"ok": False, "err": str(e)[:60]}
    now = time.time()
    keep = (CFG["KEEP_DIR"] or "").rstrip("/")
    offline, dead, errored = [], [], []
    up_total = 0
    for t in ts:
        name = t.get("name", ""); size = t.get("totalSize", 0)
        dd = t.get("downloadDir", "")
        up_total += t.get("uploadedEver", 0) or 0
        if t.get("error"):
            errored.append({"name": name, "sizeh": human_size(size), "err": (t.get("errorString") or "")[:80]})
        bad = []; best_seed = -1; best_leech = 0
        for tk in t.get("trackerStats", []):
            if tk.get("lastAnnounceTime", 0) > 0 and not tk.get("lastAnnounceSucceeded"):
                bad.append({"host": tk.get("host", "?"), "msg": (tk.get("lastAnnounceResult") or "无响应")[:50]})
            sc = tk.get("seederCount", -1)
            if sc is not None and sc >= 0: best_seed = max(best_seed, sc)
            best_leech = max(best_leech, tk.get("leecherCount", 0) or 0)
        if bad:
            offline.append({"name": name, "sizeh": human_size(size), "trackers": bad})
        # 0-peer 冷种:做种状态,tracker报告只有自己(或没有)在做种、且无人下载。保种目录的冷种是本意,不算异常
        idle = int((now - t.get("activityDate", now)) / 86400) if t.get("activityDate") else 0
        if t.get("status") == 6 and 0 <= best_seed <= 1 and best_leech == 0 and not _under(dd, keep):
            dead.append({"name": name, "sizeh": human_size(size), "idle": idle,
                         "up": human_size(t.get("uploadedEver", 0) or 0)})
    dead.sort(key=lambda x: -x["idle"])
    d = {"ok": True, "total": len(ts), "up_total": human_size(up_total),
         "offline": offline, "dead": dead[:200], "dead_n": len(dead), "errored": errored}
    _HEALTH_CACHE.update(ts=time.time(), d=d)
    return d

# ============ §10 作业·保种库存:灌进来的东西,也得有出口 ============
# 老版只有「灌」没有「清」:ks_autofill 一路灌到磁盘保护线就停 —— 停了之后呢?没有下文。
# 更糟的是 health_report **故意排除了保种目录**(注释:「保种目录的冷种是本意」),
# 于是全站最该被审视的那批种子恰恰没人看。盘满了只能人工去 tr 里一个个翻。
_UNREG = re.compile(r'unregistered|not registered|torrent not found|未注册|not exist', re.I)
_STOCK_CACHE = {"ts": 0, "d": None}

def stock_report(force=False):
    """保种库存台账:每份库存现在还值不值得占着盘。淘汰依据按**证据硬度**排序,
       硬证据才建议清,软证据只陈列给人看 —— 判不出就别替人做主(这是踩过的规矩)。"""
    if not force and _STOCK_CACHE["d"] and time.time() - _STOCK_CACHE["ts"] < 300:
        return _STOCK_CACHE["d"]
    keep = (CFG["KEEP_DIR"] or "").rstrip("/")
    if not keep:
        return {"ok": False, "err": "没有配置保种目录 KEEP_DIR"}
    fields = ["hashString", "name", "totalSize", "downloadDir", "trackerStats",
              "uploadedEver", "addedDate", "activityDate", "status", "error", "errorString"]
    try:
        ts = tr_conn().call("torrent-get", {"fields": fields}).get("arguments", {}).get("torrents", [])
    except Exception as e:
        return {"ok": False, "err": str(e)[:60]}
    now = time.time()
    dead, offline, idle_big, alive = [], [], [], []
    total = up_total = 0
    for t in ts:
        dd = t.get("downloadDir", "")
        if not _under(dd, keep): continue             # 只看保种库存
        size = t.get("totalSize", 0) or 0; total += size
        up = t.get("uploadedEver", 0) or 0; up_total += up
        age = int((now - (t.get("addedDate") or now)) / 86400)
        row = {"hash": t.get("hashString", ""), "name": t.get("name", ""), "size": size,
               "sizeh": human_size(size), "up": human_size(up), "upraw": up, "age": age}
        gone = [tk.get("host", "?") for tk in t.get("trackerStats", [])
                if _UNREG.search(tk.get("lastAnnounceResult") or "")]
        bad = [tk.get("host", "?") for tk in t.get("trackerStats", [])
               if tk.get("lastAnnounceTime", 0) > 0 and not tk.get("lastAnnounceSucceeded")
               and not _UNREG.search(tk.get("lastAnnounceResult") or "")]
        if gone:
            row["why"] = "站点已删种(tracker 报未注册):" + "、".join(gone[:3])
            dead.append(row)
        elif bad:
            row["why"] = "tracker 连不上:" + "、".join(bad[:3])
            offline.append(row)
        elif up == 0 and age >= 30 and size >= 20 * 2**30:
            row["why"] = f"做了 {age} 天一点没上传,占 {row['sizeh']}"
            idle_big.append(row)
        else:
            alive.append(row)
    for L in (dead, offline, idle_big): L.sort(key=lambda x: -x["size"])
    free_gb = 0
    try: free_gb = round(shutil.disk_usage("/data").free / 2**30)
    except Exception: pass
    d = {"ok": True, "n": len(dead) + len(offline) + len(idle_big) + len(alive),
         "total": human_size(total), "up_total": human_size(up_total),
         "free_gb": free_gb, "guard_gb": CFG["KEEP_MIN_FREE_GB"],
         # 硬证据:站点自己说这个种没了,继续做是纯浪费,清了不影响任何考核
         "dead": dead[:100], "dead_gb": round(sum(x["size"] for x in dead) / 2**30, 1),
         # 中等证据:可能是站点抽风/cookie 过期,清之前值得看一眼
         "offline": offline[:100], "offline_gb": round(sum(x["size"] for x in offline) / 2**30, 1),
         # 软证据:只陈列,不建议自动清 —— 保种本来就是「备着」,没上传不等于没价值
         "idle_big": idle_big[:100], "idle_gb": round(sum(x["size"] for x in idle_big) / 2**30, 1),
         "alive_n": len(alive)}
    _STOCK_CACHE.update(ts=time.time(), d=d)
    return d

def stock_evict(hashes, delete_data=True):
    """清退保种库存。**不可逆**,所以护栏写死在这里,不给绕过的口子:
         ① 只清数据目录在 KEEP_DIR 之下的 —— 媒体库资产一律拒绝
         ② 逐个复核 tr 里的真实 downloadDir,不信前端传来的任何东西
         ③ 媒体库那侧的硬链接不归这里管(保种库存本来就没有媒体库副本)
       返回实际清掉的清单,让调用方能核对。"""
    keep = (CFG["KEEP_DIR"] or "").rstrip("/")
    if not keep: return {"ok": False, "err": "没有配置保种目录,拒绝执行"}
    hs = [h for h in (hashes or []) if h]
    if not hs: return {"ok": False, "err": "没有指定要清的种子"}
    try:
        tr = tr_conn()
        ts = tr.call("torrent-get", {"fields": ["hashString", "name", "totalSize", "downloadDir"]}) \
               .get("arguments", {}).get("torrents", [])
    except Exception as e:
        return {"ok": False, "err": str(e)[:60]}
    byh = {t["hashString"].lower(): t for t in ts}
    ok, refused, freed = [], [], 0
    for h in hs:
        t = byh.get(h.lower())
        if not t:
            refused.append({"hash": h[:12], "why": "tr 里找不到"}); continue
        dd = t.get("downloadDir", "")
        if not _under(dd, keep):                       # 硬护栏:不在保种目录,一律不碰
            refused.append({"hash": h[:12], "name": t.get("name", "")[:40],
                            "why": f"数据不在保种目录({dd[:40]}),拒绝删除"}); continue
        try:
            tr.call("torrent-remove", {"ids": [t["hashString"]],
                                       "delete-local-data": bool(delete_data)})
            ok.append({"name": t.get("name", "")[:60], "sizeh": human_size(t.get("totalSize", 0))})
            freed += t.get("totalSize", 0) or 0
            cid = led_cid(t["hashString"])
            if cid: led_place(cid, "gone")
        except Exception as e:
            refused.append({"hash": h[:12], "name": t.get("name", "")[:40], "why": str(e)[:40]})
    _STOCK_CACHE["d"] = None
    if ok:
        logmsg("INFO", f"保种清退 {len(ok)} 个,腾出 {human_size(freed)}"
                       + ("(含数据)" if delete_data else "(仅删任务,数据保留)"))
    return {"ok": True, "removed": ok, "refused": refused, "freed": human_size(freed)}

def gap_fill(ih):
    """立刻把这份内容账上欠的站问一遍(不等辅种轮次)。严格口径:文件清单必须完全一致。"""
    try:
        tr = tr_conn()
        t = tr.torrent(ih)
        if not t: return {"ok": False, "err": "tr 里找不到该种子"}
        m, inj = crossseed_one(tr, t)
        cid = content_id(manifest_tr(t))
        cov = led_cov_get(cid)
        urlmap, all_sites = site_urlmap()
        return {"ok": True, "matched": m, "injected": inj,
                "seeded": sorted(k for k, v in cov.items() if v[0] in ("seeding", "source")),
                "absent": sorted(k for k, v in cov.items() if v[0] == "absent"),
                "errs":   sorted(k for k, v in cov.items() if v[0] == "error"),
                "pending": sorted(led_cov_pending(cid, all_sites))}
    except Exception as e:
        return {"ok": False, "err": str(e)[:80]}

def gap_verify(ih):
    """⚠️ 这是**发种查重**,不是辅种核实 —— 两件事,口径故意不同,别合并:
         · 辅种(crossseed_one):文件清单必须一模一样,差一个字节就不是同一份数据
         · 发种查重(本函数):这个站上**有没有这部片**(任何版本都算),免得重复占坑
       所以本函数用宽松口径:标题搜得到就算「有」。发种前用它,辅种别用它。"""
    c = db(); t = c.execute("SELECT name,size FROM torrents WHERE info_hash=?", (ih,)).fetchone(); c.close()
    if not t: return {"ok": False, "err": "找不到该种子"}
    name, size = t
    q = extract_query(name) or name
    try: results = prowlarr_search_fan(q)
    except Exception as e: return {"ok": False, "err": str(e)[:60]}
    try: all_sites = [i.get("name", "?") for i in prowlarr_indexers()]
    except Exception: all_sites = []
    ban = [b.strip().lower() for b in CFG["TR_BAN_SITES"].split(",") if b.strip()]
    # 宽松口径:该站能搜到这个剧(标题相关)即算"有"——发种是别重复占坑,不是文件级辅种
    have = {(r.get("indexer") or "") for r in results if r.get("indexer")}
    low = {s.lower() for s in have}
    missing = [s for s in all_sites if s.lower() not in low
               and not any(s.lower() in l or l in s.lower() for l in low)
               and not any(b in s.lower() for b in ban)]
    return {"ok": True, "have": sorted(have), "missing": missing, "sites": len(all_sites), "q": q}

# ---- MediaInfo(ffprobe) + 截图(ffmpeg)+ pixhost 免费图床 ----
def _ff(name):
    """定位 ffprobe/ffmpeg:优先 /config/bin 的静态二进制,回退 PATH"""
    import shutil as _sh
    local = os.path.join(os.path.dirname(CFG["DB"]), "bin", name)
    if os.path.exists(local) and os.access(local, os.X_OK): return local
    return _sh.which(name)

_VIDEXT = (".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".wmv", ".mov", ".flv", ".webm", ".iso")
def _biggest_video(ih):
    """种子里最大的视频文件绝对路径(容器内)。辅种副本也能定位到同一份数据。"""
    try:
        for src in (tr_conn().torrents(), qb_conn().torrents()):
            for t in src:
                h = t.get("hashString") or t.get("hash") or ""
                if h.lower() != ih.lower(): continue
                base = t.get("downloadDir") or t.get("save_path") or ""
                files = t.get("files") or []
                cand = []
                for f in files:
                    nm = f.get("name") or ""
                    sz = f.get("length") or f.get("size") or 0
                    if nm.lower().endswith(_VIDEXT): cand.append((sz, os.path.join(base, nm)))
                if not cand and files:   # qb 的 files 需另取;这里兜底用 content_path
                    p = t.get("content_path") or (os.path.join(base, t.get("name", "")))
                    return p
                if cand:
                    cand.sort(reverse=True); return cand[0][1]
    except Exception: pass
    return ""

def gen_mediainfo(path):
    """ffprobe → 发种用的 MediaInfo 文本"""
    fp = _ff("ffprobe")
    if not fp: raise RuntimeError("容器内没有 ffprobe")
    import subprocess
    out = subprocess.run([fp, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                         capture_output=True, timeout=60).stdout
    d = json.loads(out or b"{}")
    fmt = d.get("format", {}); streams = d.get("streams", [])
    L = ["General"]
    L.append(f"Complete name  : {os.path.basename(path)}")
    L.append(f"Format         : {fmt.get('format_name','?')}")
    dur = float(fmt.get("duration", 0) or 0)
    if dur: L.append(f"Duration       : {int(dur//3600)}h {int(dur%3600//60)}min {int(dur%60)}s")
    if fmt.get("size"): L.append(f"File size      : {human_size(int(fmt['size']))}")
    if fmt.get("bit_rate"): L.append(f"Overall bitrate: {int(int(fmt['bit_rate'])/1000)} kb/s")
    vi = ai = ti = 0
    _LANG = {"chi":"Chinese","zho":"Chinese","eng":"English","jpn":"Japanese","kor":"Korean","und":""}
    for s in streams:
        ct = s.get("codec_type")
        if ct == "video":
            vi += 1; L.append(f"\nVideo #{vi}")
            L.append(f"Format         : {s.get('codec_name','?').upper()} {s.get('profile','')}".rstrip())
            L.append(f"Resolution     : {s.get('width','?')}x{s.get('height','?')}")
            fr = s.get("r_frame_rate", "0/1")
            try:
                a, b = fr.split("/"); fps = float(a)/float(b) if float(b) else 0
                if fps: L.append(f"Frame rate     : {fps:.3f} fps")
            except Exception: pass
            if s.get("bit_rate"): L.append(f"Bit rate       : {int(int(s['bit_rate'])/1000)} kb/s")
            if s.get("pix_fmt"): L.append(f"Color          : {s['pix_fmt']}")
        elif ct == "audio":
            ai += 1; L.append(f"\nAudio #{ai}")
            L.append(f"Format         : {s.get('codec_name','?').upper()}")
            if s.get("channels"): L.append(f"Channels       : {s['channels']}ch")
            if s.get("sample_rate"): L.append(f"Sampling rate  : {int(s['sample_rate'])/1000:.1f} kHz")
            lg = (s.get("tags") or {}).get("language", "")
            if lg: L.append(f"Language       : {_LANG.get(lg, lg)}")
        elif ct == "subtitle":
            ti += 1
            lg = (s.get("tags") or {}).get("language", "")
            L.append(f"\nText #{ti}       : {s.get('codec_name','?')} {(_LANG.get(lg,lg))}".rstrip())
    return "\n".join(L)

SHOTS_DIR = os.path.join(os.path.dirname(CFG["DB"]), "shots")
def gen_shots(path, ih, n=4):
    """ffmpeg 均匀抽 n 帧 → 存本地 → 自托管公网直链(seed.leesy.cc/api/shot)。不依赖墙外图床。"""
    fm = _ff("ffmpeg"); fp = _ff("ffprobe")
    if not fm: raise RuntimeError("容器内没有 ffmpeg")
    import subprocess
    os.makedirs(SHOTS_DIR, exist_ok=True)
    dur = 0
    if fp:
        try:
            o = subprocess.run([fp, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                               capture_output=True, timeout=30).stdout
            dur = float((o or b"0").strip() or 0)
        except Exception: pass
    if dur <= 0: dur = 3600
    base = (CFG["PUBLIC_URL"] or "").rstrip("/")
    shots = []
    for i in range(n):
        ts = dur * (i + 1) / (n + 1)                    # 避开片头片尾,均匀取
        fn = f"{ih[:16]}_{i}.jpg"
        jpg = os.path.join(SHOTS_DIR, fn)
        try:
            subprocess.run([fm, "-y", "-ss", str(int(ts)), "-i", path, "-vframes", "1",
                            "-vf", "scale=1280:-2", "-q:v", "3", jpg], capture_output=True, timeout=60)
            if os.path.exists(jpg) and os.path.getsize(jpg) > 0:
                url = (base + "/api/shot?f=" + fn) if base else ("/api/shot?f=" + fn)
                shots.append((url, url))                 # (点开大图, 缩略) —— 自托管同一张
        except Exception as e:
            logmsg("WARN", f"截图{i}失败: {str(e)[:40]}")
    return shots

_XSHOT = {}   # ih -> {done,err,mediainfo,shots,ts}
def _xshot_run(ih):
    _XSHOT[ih] = {"done": False, "err": "", "mediainfo": "", "shots": [], "ts": time.time()}
    try:
        path = _biggest_video(ih)
        if not path: raise RuntimeError("找不到视频文件(种子可能不在tr/qb里)")
        if not os.path.exists(path): raise RuntimeError(f"文件不存在: {path[:60]}")
        try: _XSHOT[ih]["mediainfo"] = gen_mediainfo(path)
        except Exception as e: _XSHOT[ih]["mediainfo"] = f"(MediaInfo 生成失败: {str(e)[:50]})"
        _XSHOT[ih]["shots"] = gen_shots(path, ih)
        if not (CFG["PUBLIC_URL"] or "").strip():
            _XSHOT[ih]["err"] = "截图已生成,但没设 PUBLIC_URL,PT 站看不到图。去设置填『本面板公网地址』"
        _XSHOT[ih]["done"] = True
        logmsg("INFO", f"发种资料: MediaInfo+{len(_XSHOT[ih]['shots'])}张截图 已就绪 {os.path.basename(path)[:36]}")
    except Exception as e:
        _XSHOT[ih].update(done=True, err=str(e)[:80])
        logmsg("WARN", f"发种资料生成失败: {str(e)[:60]}")

def xfer_pack(ih):
    """半自动发种资料包:标题/副标题/TMDB简介bbcode。禁转标记硬拦截。"""
    c = db()
    t = c.execute("SELECT name,size,files FROM torrents WHERE info_hash=?", (ih,)).fetchone()
    if not t: c.close(); return {"ok": False, "err": "找不到该种子"}
    name, size, nfiles = t
    # 禁转检测:本种名字 + 各站搜到的同内容标题,一个带标记就全线拦截
    titles = [name] + [x[0] or "" for x in c.execute("SELECT matched_name FROM matches WHERE info_hash=?", (ih,)).fetchall()]
    hit = next((tt for tt in titles if noxfer(tt)), None)
    # 一级:精确 name/info_hash 命中入库记录
    m = c.execute("SELECT tmdbid,tmdb_name,year,mtype,poster FROM media WHERE tmdb_name!='' AND status='done' AND (name=? OR info_hash=?) LIMIT 1", (name, ih)).fetchone()
    # 二级:库里按识别出的中文名模糊配(辅种副本名字/hash都和入库那条不同,精确必落空)
    if not (m and m[0]):
        q = extract_query(name)
        if q:
            m = c.execute("SELECT tmdbid,tmdb_name,year,mtype,poster FROM media WHERE tmdb_name!='' AND status='done' "
                          "AND (tmdb_name LIKE ? OR ? LIKE '%'||tmdb_name||'%') ORDER BY LENGTH(tmdb_name) DESC LIMIT 1",
                          (f"%{q}%", q)).fetchone()
    c.close()
    if hit:
        return {"ok": False, "banned": True, "err": f"检测到禁转/独家标记,坚决不转: {hit[:80]}"}
    sub, desc = "", ""
    # 三级:库里也没有(纯保种的种)→ 从种子名直接问 TMDB 现查
    if not (m and m[0]):
        try:
            mm = tmdb_match(extract_query(name) or name)
            if mm and mm.get("conf") != "low":
                m = (mm["id"], mm["tmdb_name"], mm.get("year") or "", mm["mtype"], mm.get("poster") or "")
        except Exception: pass
    if m and m[0]:
        tid, tname, yr, mtype, poster = m
        sub = f"{tname} ({yr})" if yr else tname
        try:
            d = tmdb_details(mtype or "tv", tid) or {}
            ov = d.get("overview") or ""
            lines = [f"[img]https://image.tmdb.org/t/p/original{poster}[/img]" if poster else "",
                     f"◎片名  {tname} ({yr})", f"◎类型  {'剧集' if (mtype or 'tv')=='tv' else '电影'}",
                     f"◎TMDB  https://www.themoviedb.org/{mtype or 'tv'}/{tid}",
                     "", "◎简介", ov]
            desc = "\n".join(l for l in lines if l is not None)
        except Exception:
            desc = f"◎片名  {sub}\n◎TMDB  https://www.themoviedb.org/{mtype or 'tv'}/{tid}"
    if not sub:                    # TMDB 也认不出(冷门/写真/纪录)——给个可编辑的兜底,别留空让人以为坏了
        sub = extract_query(name) or name
        desc = desc or f"◎片名  {sub}\n◎大小  {human_size(size)} · {nfiles} 个文件\n\n(未匹配到 TMDB,简介请自行补充)"
    return {"ok": True, "title": name, "sub": sub, "desc": desc,
            "sizeh": human_size(size), "files": nfiles,
            "tip": "上传时直接用原站 .torrent(NexusPHP 会自动换 passkey 重签);目标站若强制 source 标记需重制种子。发布前请再核对目标站发种规则。"}
# ============ 登录页(告别浏览器原生 Basic 弹窗) ============
_SESS = {}          # token -> 过期时间戳
SESS_DAYS = 30
def new_session():
    import secrets
    t = secrets.token_urlsafe(24)
    _SESS[t] = time.time() + SESS_DAYS * 86400
    if len(_SESS) > 200:                       # 顺手清过期的
        for k, v in list(_SESS.items()):
            if v < time.time(): _SESS.pop(k, None)
    return t
def sess_ok(tok):
    exp = _SESS.get(tok or "")
    return bool(exp and exp > time.time())

LOGIN_PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>观澜 Wavegazer · 登录</title><link rel="icon" href="/favicon.ico" type="image/svg+xml"><style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:#fff;font:14px/1.55 -apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC','Microsoft YaHei',sans-serif;-webkit-font-smoothing:antialiased;
display:flex;align-items:center;justify-content:center;overflow:hidden;
background:radial-gradient(1100px 520px at 85% -8%,rgba(255,255,255,.10),transparent 60%),linear-gradient(180deg,#0039c8 0%,#002FA7 38%,#001d77 100%);background-color:#002FA7}
#bgv{position:fixed;inset:0;width:100%;height:100%;object-fit:cover;z-index:0;opacity:.55}
#veil{position:fixed;inset:0;z-index:1;background:linear-gradient(180deg,rgba(0,32,120,.45),rgba(0,25,100,.82))}
.box{position:relative;z-index:2;width:min(400px,92vw);padding:38px 34px 30px;border-radius:26px;
background:rgba(255,255,255,.15);backdrop-filter:blur(26px);border:1px solid rgba(255,255,255,.30);box-shadow:0 30px 90px rgba(0,10,60,.55);text-align:center}
.brand{display:flex;align-items:center;justify-content:center;gap:11px;font-size:25px;font-weight:800;letter-spacing:-.02em}
.en{font-size:14px;font-weight:600;color:rgba(255,255,255,.62);letter-spacing:.05em;margin-top:3px}
.tip{color:rgba(255,255,255,.7);font-size:12.5px;margin:6px 0 24px}
input{width:100%;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.16);color:#fff;border-radius:13px;padding:13px 16px;font-size:14px;outline:none;margin-bottom:12px;transition:.18s}
input::placeholder{color:rgba(255,255,255,.62)}
input:focus{background:rgba(255,255,255,.22);box-shadow:0 0 0 3px rgba(255,255,255,.45)}
button{width:100%;background:#fff;color:#002FA7;border:none;border-radius:980px;padding:13px;font-size:15px;font-weight:800;cursor:pointer;transition:.18s;margin-top:6px}
button:hover{transform:translateY(-1px);box-shadow:0 10px 26px rgba(0,10,60,.4)}
button:active{transform:none}
.err{color:#FFD400;font-size:13px;font-weight:700;min-height:20px;margin-top:12px}
.foot{position:fixed;bottom:18px;left:0;right:0;text-align:center;color:rgba(255,255,255,.5);font-size:12px;z-index:2}
</style></head><body>
<video id=bgv autoplay muted loop playsinline poster="/api/bg" src="/api/bgv?v=2"></video><div id=veil></div>
<form class=box method=post action="/login">
<div class=brand><svg width="34" height="34" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"><rect width="64" height="64" rx="14" fill="#0a2fb5"/><circle cx="46" cy="17" r="7.5" fill="#FFD400"/><path d="M2 37c7-9 15-9 21 0s15 9 21 0 12-8 18-3v30H2z" fill="#ffffff" opacity="0.95"/><path d="M2 47c7-7 13-7 19 0s15 7 21 0 14-7 20-1v18H2z" fill="#CFE0FF" opacity="0.9"/></svg>观澜</div>
<div class=en>WAVEGAZER</div>
<div class=tip>观影观澜 · 站在岸上看自己的海</div>
<input name=u placeholder="用户名" autocomplete=username autofocus>
<input name=p type=password placeholder="密码" autocomplete=current-password>
<button type=submit>进 港</button>
<div class=err>{{ERR}}</div>
</form>
<div class=foot>观澜 Wavegazer · 一个人的影音港湾 · MIT 开源</div>
</body></html>"""

FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
 '<rect width="64" height="64" rx="14" fill="#002FA7"/>'
 '<circle cx="46" cy="17" r="7.5" fill="#FFD400"/>'
 '<path d="M2 37c7-9 15-9 21 0s15 9 21 0 12-8 18-3v30H2z" fill="#ffffff" opacity="0.95"/>'
 '<path d="M2 47c7-7 13-7 19 0s15 7 21 0 14-7 20-1v18H2z" fill="#CFE0FF" opacity="0.9"/>'
 '</svg>').encode()

class Handler(BaseHTTPRequestHandler):
    def log_message(s, *a): pass
    def _cookie(s, name):
        for kv in (s.headers.get("Cookie") or "").split(";"):
            k, _, v = kv.strip().partition("=")
            if k == name: return v
        return ""
    def _auth_ok(s):
        u, p = CFG["AUTH_USER"], CFG["AUTH_PASS"]
        if not u:  # 未配置账号密码 = 不启用登录
            return True
        if sess_ok(s._cookie("gl_sess")):        # 网页登录会话(自家登录页发的)
            return True
        hdr = s.headers.get("Authorization", "")  # 保留 Basic:给 curl/脚本用
        if hdr.startswith("Basic "):
            try:
                dec = base64.b64decode(hdr[6:]).decode("utf-8", "ignore")
                gu, gp = dec.split(":", 1)
                if gu == u and gp == p:
                    return True
            except Exception:
                pass
        if s.path.startswith("/api"):            # 接口:给 401 让前端自己跳
            s.send_response(401); s.send_header("Content-Type", "application/json")
            s.end_headers(); s.wfile.write(b'{"ok":false,"err":"unauth"}')
        else:                                    # 页面:跳自家登录页
            s.send_response(302); s.send_header("Location", "/login"); s.end_headers()
        return False
    def _login_page(s, err=""):
        body = LOGIN_PAGE.replace("{{ERR}}", esc(err)).encode()
        s.send_response(200); s.send_header("Content-Type", "text/html; charset=utf-8")
        s.send_header("Cache-Control", "no-store")
        s.send_header("Content-Length", str(len(body)))
        s.end_headers(); s.wfile.write(body)
    def _login_post(s):
        try:
            ln = int(s.headers.get("Content-Length", "0"))
            f = urllib.parse.parse_qs(s.rfile.read(ln).decode("utf-8", "ignore"))
        except Exception:
            f = {}
        gu = (f.get("u", [""])[0]).strip(); gp = f.get("p", [""])[0]
        if gu == CFG["AUTH_USER"] and gp == CFG["AUTH_PASS"] and CFG["AUTH_USER"]:
            tok = new_session()
            s.send_response(302); s.send_header("Location", "/")
            s.send_header("Set-Cookie", f"gl_sess={tok}; Path=/; Max-Age={SESS_DAYS*86400}; HttpOnly; SameSite=Lax")
            s.end_headers()
            logmsg("INFO", "面板登录成功")
        else:
            time.sleep(1)                        # 挡一下暴力猜密码
            s._login_page("用户名或密码不对,再试试")
    def do_POST(s):
        if s.path.startswith("/api/wecom"):
            s._wecom_post(); return
        if s.path.startswith("/api/stock/evict"):
            if not s._auth_ok(): return
            try:
                ln = int(s.headers.get("Content-Length", "0"))
                body = json.loads(s.rfile.read(ln).decode("utf-8", "ignore") or "{}")
            except Exception:
                body = {}
            s._send_json(stock_evict(body.get("hashes") or [], bool(body.get("delete_data", True)))); return
        if s.path.startswith("/login"):
            s._login_post(); return
        if not s._auth_ok():
            return
        if s.path.startswith("/api/dl"):
            try:
                ln = int(s.headers.get("Content-Length", "0"))
                body = json.loads(s.rfile.read(ln) or b"{}")
            except Exception:
                body = {}
            s._dl_run((body.get("url") or "").strip(), (body.get("cat") or "").strip(), body.get("mates") or [])
            return
        if s.path.startswith("/api/settings"):
            s._settings_post(); return
        if s.path.startswith("/api/batchgo"):
            s._batchgo(); return
        if s.path.startswith("/api/ks/add"):
            s._ks_add(); return
        s.send_response(404); s.end_headers()
    def do_GET(s):
        if s.path.startswith("/api/wecom"):
            s._wecom_get(); return
        if s.path.startswith("/api/poster"):
            s._poster(); return          # 公开海报,免登录(图文通知的图要外网可达)
        if s.path.startswith("/api/cover"):
            s._cover(); return           # 专辑封面,免登录(和海报同待遇)
        if s.path.startswith("/api/shot"):
            s._shot(); return            # 发种截图,免登录(PT站要外网可达)
        if s.path.startswith("/api/bgv"):
            s._bgv(); return             # 海浪视频背景,免登录
        if s.path.startswith("/api/bg"):
            s._bg(); return              # 首页海浪背景图,免登录
        if s.path.startswith("/login"):
            if not CFG["AUTH_USER"] or sess_ok(s._cookie("gl_sess")):
                s.send_response(302); s.send_header("Location", "/"); s.end_headers(); return
            s._login_page(); return
        if s.path.startswith("/logout"):
            _SESS.pop(s._cookie("gl_sess"), None)
            s.send_response(302); s.send_header("Location", "/login")
            s.send_header("Set-Cookie", "gl_sess=; Path=/; Max-Age=0")
            s.end_headers(); return
        if s.path.startswith("/favicon"):
            s.send_response(200); s.send_header("Content-Type", "image/svg+xml")
            s.send_header("Cache-Control", "max-age=604800")
            s.send_header("Content-Length", str(len(FAVICON_SVG)))
            s.end_headers(); s.wfile.write(FAVICON_SVG); return
        if not s._auth_ok():
            return
        if s.path.startswith("/research"):
            s._research(); return
        if s.path.startswith("/torrent"):
            s._detail(); return
        if s.path.startswith("/api/douban"):
            s._douban(); return
        if s.path.startswith("/api/dbimg"):
            s._dbimg(); return
        if s.path.startswith("/api/personcredits"):
            s._personcredits(); return
        if s.path.startswith("/api/person"):
            s._person(); return
        if s.path.startswith("/api/search2"):
            s._search2(); return
        if s.path.startswith("/api/searchstat"):
            s._searchstat(); return
        if s.path.startswith("/api/search"):
            s._search(); return
        if s.path.startswith("/api/dl"):
            s._dl(); return
        if s.path.startswith("/api/reid"):
            s._reid(); return
        if s.path.startswith("/api/poster"):
            s._poster(); return
        if s.path.startswith("/api/overview"):
            s._overview(); return
        if s.path.startswith("/api/settings"):
            s._settings_get(); return
        if s.path.startswith("/api/test"):
            s._svc_test(); return
        if s.path.startswith("/api/dashboard"):
            s._dashboard(); return
        if s.path.startswith("/api/ks/"):
            s._ks(); return
        if s.path.startswith("/api/artist"):
            s._artist(); return
        if s.path.startswith("/api/artstat"):
            s._artstat(); return
        if s.path.startswith("/api/batchplan"):
            s._batchplan(); return
        if s.path.startswith("/api/batchstat"):
            s._batchstat(); return
        if s.path.startswith("/api/libaudit"):
            s._libaudit(); return
        if s.path.startswith("/api/libcat"):
            s._libcat(); return
        if s.path.startswith("/api/libfix"):
            s._libfix(); return
        if s.path.startswith("/api/health"):
            from urllib.parse import urlparse, parse_qs
            force = parse_qs(urlparse(s.path).query).get("force") == ["1"]
            s._send_json(health_report(force)); return
        if s.path.startswith("/api/researchall"):
            if not _BATCH["research"]:
                threading.Thread(target=research_all_worker, daemon=True).start()
            s._send_json({"ok": True, "running": True}); return
        if s.path.startswith("/api/skipall"):
            c = db(); n = c.execute("UPDATE media SET status='skip' WHERE status='hold'").rowcount; c.commit(); c.close()
            s._send_json({"ok": True, "n": n}); return
        if s.path.startswith("/api/skip"):
            from urllib.parse import urlparse, parse_qs
            h = (parse_qs(urlparse(s.path).query).get("hash", [""])[0]).strip()
            c = db(); n = c.execute("UPDATE media SET status='skip' WHERE info_hash=? AND status='hold'", (h,)).rowcount; c.commit(); c.close()
            s._send_json({"ok": bool(n)}); return
        if s.path.startswith("/api/backfill"):
            if not _BACKFILL["running"]:
                threading.Thread(target=backfill_ledger, daemon=True).start()
            s._send_json({"ok": True, **_BACKFILL}); return
        if s.path.startswith("/api/stock"):
            s._send_json(stock_report(force="force" in s.path)); return
        if s.path.startswith("/api/gapfill"):
            h = urllib.parse.parse_qs(urllib.parse.urlparse(s.path).query).get("hash", [""])[0]
            s._send_json(gap_fill(h)); return
        if s.path.startswith("/api/gapverify"):
            from urllib.parse import urlparse, parse_qs
            q_ = parse_qs(urlparse(s.path).query)
            s._send_json(gap_verify((q_.get("hash", [""])[0]).strip())); return
        if s.path.startswith("/api/gap"):
            s._send_json(gap_report()); return
        if s.path.startswith("/api/xfershot"):
            from urllib.parse import urlparse, parse_qs
            q_ = parse_qs(urlparse(s.path).query)
            ih = (q_.get("hash", [""])[0]).strip()
            st = _XSHOT.get(ih)
            if q_.get("go") == ["1"] and (not st or st.get("done")):
                threading.Thread(target=_xshot_run, args=(ih,), daemon=True).start()
                s._send_json({"ok": True, "started": True}); return
            if not st: s._send_json({"ok": True, "started": False, "done": False}); return
            s._send_json({"ok": True, "done": st["done"], "err": st["err"],
                          "mediainfo": st["mediainfo"], "shots": st["shots"]}); return
        if s.path.startswith("/api/xfer"):
            from urllib.parse import urlparse, parse_qs
            q_ = parse_qs(urlparse(s.path).query)
            s._send_json(xfer_pack((q_.get("hash", [""])[0]).strip())); return
        if s.path.startswith("/api/downloads"):
            s._downloads(); return
        if s.path.startswith("/api/canceldl"):
            s._canceldl(); return
        if s.path.startswith("/api"):
            s._json(); return
        c = db()
        # 按内容(名字+大小)去重统计，辅种副本不重复计数
        t_total = c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM torrents GROUP BY name,size)").fetchone()[0]
        t_inject = c.execute("SELECT COALESCE(SUM(mi),0) FROM (SELECT MAX(injected) mi FROM torrents GROUP BY name,size)").fetchone()[0]
        t_done = c.execute("SELECT COUNT(*) FROM (SELECT MAX(matched) m FROM torrents GROUP BY name,size) WHERE m>0").fetchone()[0]
        t_nomatch = t_total - t_done
        rows = ""
        for r in c.execute("SELECT name,query,matched,injected,status,info_hash,source,MAX(injected) FROM torrents GROUP BY name,size ORDER BY last_searched DESC LIMIT 100").fetchall():
            st = {"done":"done","no_match":"nomatch","searching":"searching","injecting":"searching"}.get(r[4],"err")
            label = {"done":"完成","no_match":"无匹配","searching":"搜索中","injecting":"辅种中"}.get(r[4], r[4])
            # 旧记录的 query 列为空时，仍把这次实际使用的自动关键词展示出来。
            # 否则用户面对「无匹配」只会看到一片空白，没法判断要不要改关键词重搜。
            used_query = r[1] or extract_query(r[0])
            manual = f"<div class=rs><input placeholder='自定义关键词' value=''><button onclick=\"research('{esc(r[5])}',this)\">重搜</button></div>"
            # 在辅站数=该内容实际在多少个站做种(注入/已存在/加tracker都算,不管是谁辅上的)
            seeded = c.execute("SELECT COUNT(DISTINCT indexer) FROM matches WHERE info_hash=? AND result IN ('injected','duplicate','tracker')", (r[5],)).fetchone()[0]
            rows += (f"<tr><td class=name title='{esc(r[0])}'><a href='/torrent?hash={esc(r[5])}'>{esc(r[0])}</a></td>"
                     f"<td><span class=src>{esc(r[6] or '?')}</span></td><td class=mut>{esc(used_query)}</td>"
                     f"<td class='r' style='color:var(--ok);font-weight:700'>{seeded}</td>"
                     f"<td><span class='b {st}'>{label}</span></td><td>{manual}</td></tr>")
        # 整理入库记录:按分类分组的海报墙
        smap = {"done":("已入库","done"),"hold":("待确认","nomatch"),"processing":("处理中","searching"),"error":("出错","err"),"skip":("跳过","nomatch")}
        CATS = [("电影","🎬"),("电视剧","📺"),("动漫","🎌"),("音乐","🎵"),("漫画/书","📖")]
        buckets = {c: [] for c, _ in CATS}; pend = []
        for r in c.execute("SELECT info_hash,name,cat,tmdb_name,year,status,target,poster,tmdbid,mtype FROM media ORDER BY ts DESC LIMIT 300").fetchall():
            ih,nm,cat,tn,yr,stt,tgt,pos,tid,mty = r
            if stt in ("hold","error","processing"): pend.append(r); continue
            key = cat if cat in buckets else ("漫画/书" if cat in ("漫画","书籍","图书") else "电影")
            buckets[key].append(r)
        def mcard(r):
            """媒体海报卡:和「最近入库」同款(可流动/磁吸放大/点开简介)"""
            ih,nm,cat,tn,yr,stt,tgt,pos,tid,mty = r
            title = tn or nm
            if pos:
                thumb = f"<img loading=lazy src='/api/poster?p={urllib.parse.quote(pos)}'>"
            else:
                ico = {"音乐":"🎵","漫画/书":"📖"}.get(cat, "🎬")
                thumb = f"<div class='rph mtile'>{ico}</div>"
            mt = mty or ("tv" if cat in ("电视剧", "动漫") else "movie")
            # 认错了随时能改:填片名或 TMDB id 重新识别(高置信度也可能错,比如《手机》)
            # 线性铅笔图标(跟着 currentColor 变色),比 emoji 干净,和整体设计语言一致
            fix = (f"<button class=rfix title='识别错了?点这里改' data-h='{esc(ih)}' "
                   f"data-n='{esc(title)}' onclick='mFix(event,this)'>"
                   f"<svg viewBox='0 0 24 24' width='13' height='13' fill='none' stroke='currentColor' "
                   f"stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
                   f"<path d='M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17v3z'/>"
                   f"<path d='M14.5 6.5l3 3'/></svg></button>")
            # 标出季号:同一部剧下了好几季时,库里存的是剧集级匹配(名字+首播年都一样),
            # 卡片长得一模一样根本分不清谁是谁。季号从种子名解析,补在年份后面。
            sub = esc(yr or "")
            if mt == "tv":
                ss = _season_of(nm or "")
                if ss: sub = (sub + " · " if sub else "") + f"第{ss}季"
            return (f"<div class=rcard title='{esc(nm)}' data-mt='{esc(mt)}' data-tid='{tid or 0}'><div class=rbob>{thumb}{fix}"
                    f"<div class=rname>{esc(title)}</div><div class=ryear>{sub}</div></div></div>")
        media_rows = ""
        if pend:
            nhold = sum(1 for r in pend if r[5] == "hold")
            skipall = (f" <button class=dlbtn style='padding:4px 12px;font-size:12px;background:rgba(255,255,255,.2);color:#fff' "
                       f"onclick='skipAll(this)'>🚫 全部跳过({nhold})</button>") if nhold else ""
            media_rows += ("<div class=sgrp style='padding:0 20px'>⚠️ 待确认 / 处理中 "
                           "<span class=mut style=font-weight:400>· 填 TMDB id 或片名一键入库,不想要的直接跳过</span>" + skipall + "</div><div class=dgrid>")
            for r in pend:
                ih,nm,cat,tn,yr,stt,tgt,pos,tid,mty = r
                lbl, cls = smap.get(stt, (stt, "err"))
                skipbtn = (f"<button onclick=\"skipOne('{esc(ih)}',this)\" style='background:rgba(255,255,255,.16);color:#fff'>跳过</button>"
                           if stt == "hold" else "")
                media_rows += (f"<div class=dcard data-h='{esc(ih)}'><div class=dwrap><div class='dph mtile'>❓</div>"
                               f"<div class=mbadge><span class='b {cls}'>{esc(lbl)}</span></div></div>"
                               f"<div class=dtt title='{esc(nm)}'>{esc(nm)}</div>"
                               f"<div class=rs style='margin-top:6px'><input placeholder='TMDB id 或 片名' value=''>"
                               f"<button onclick=\"reid('{esc(ih)}',this)\">确认</button>{skipbtn}</div></div>")
            media_rows += "</div>"
        def acard(a):
            """专辑卡:真封面 + 专辑名 + 歌手·N首歌(音乐按专辑聚合,不再一首歌一张灰砖)"""
            if a["cover"]:
                thumb = f"<img loading=lazy src='/api/cover?p={urllib.parse.quote(a['cover'])}'>"
            else:
                thumb = "<div class='rph mtile'>🎵</div>"
            sub = (a["artist"] + " · " if a["artist"] else "") + f"{a['n']} 首"
            return (f"<div class=rcard title='{esc(a['album'])}'><div class=rbob>{thumb}"
                    f"<div class=rname>{esc(a['album'])}</div><div class=ryear>{esc(sub)}</div></div></div>")
        try: albums = music_albums()
        except Exception: albums = []
        for ci, (cname, icon) in enumerate(CATS):
            if cname == "音乐":
                # 音乐走目录扫描出的专辑卡(有封面、按专辑合并、标歌曲数)
                if not albums: continue
                river = "".join(acard(a) for a in albums[:20])
                grid = "".join(acard(a) for a in albums)
                items = albums
            else:
                items = buckets.get(cname) or []
                if not items: continue
                # 每类一条河:默认只放最新 20 个(流动展示),点「全部」摊开成网格
                river = "".join(mcard(r) for r in items[:20])
                grid = "".join(mcard(r) for r in items)
            more = (f" <button class=dlbtn style='padding:3px 14px;font-size:12px;margin-left:6px' "
                    f"onclick=\"mToggle({ci},this)\">全部 {len(items)} 项</button>") if len(items) > 5 else ""
            media_rows += (f"<div class=sgrp style='padding:0 20px'>{icon} {cname} "
                           f"<span class=mut style=font-weight:400>· {len(items)} 项</span>{more}</div>"
                           f"<div class=rflow id='mflow{ci}'><div class=rtrack>{river}</div></div>"
                           f"<div class='rflow mgridwrap' id='mgrid{ci}' style='display:none'>"
                           f"<div class=mgrid>{grid}</div></div>")
        recent = ""
        for r in c.execute("SELECT tmdb_name,year,poster,mtype,tmdbid FROM media WHERE status='done' AND poster IS NOT NULL AND poster != '' ORDER BY ts DESC LIMIT 14").fetchall():
            recent += (f"<div class=rcard data-mt='{esc(r[3] or 'tv')}' data-tid='{r[4] or 0}'><div class=rbob>"
                       f"<img loading=lazy src='/api/poster?p={urllib.parse.quote(r[2])}'>"
                       f"<div class=rname>{esc(r[0])}</div><div class=ryear>{esc(r[1] or '')}</div></div></div>")
        logs = ""
        for r in c.execute("SELECT ts,level,msg FROM log ORDER BY id DESC LIMIT 40").fetchall():
            logs += f"<tr><td class=mut>{time.strftime('%m-%d %H:%M:%S', time.localtime(r[0]))}</td><td>{esc(r[2])}</td></tr>"
        c.close()
        html = (PAGE.replace("{{INTERVAL}}", str(CFG["SCAN_INTERVAL"]))
                    .replace("{{TOTAL}}", str(t_total)).replace("{{INJECT}}", str(t_inject))
                    .replace("{{DONE}}", str(t_done)).replace("{{NOMATCH}}", str(t_nomatch))
                    .replace("{{ROWS}}", rows or "<tr><td colspan=6><div class=empty><div class=ei>🌱</div><div class=et>还没有辅种记录</div><div>后台每隔一阵扫描 tr 里的种子,自动全站找同内容注入 · 有种子后这里就有了</div></div></td></tr>")
                    .replace("{{MEDIACOUNT}}", str(sum(len(v) for k, v in buckets.items() if k != "音乐") + len(albums)))
                    .replace("{{MEDIA}}", media_rows or "<div class=mut style='padding:4px 20px 16px'>暂无入库记录</div>")
                    .replace("{{RECENT}}", recent or "<div class=mut style='padding:4px 0 8px'>还没有带海报的入库记录,下一部片就有了</div>")
                    .replace("{{EMBYPUB}}", os.environ.get("EMBY_PUBLIC", "https://emby.leesy.cc"))
                    .replace("{{LOGOUT}}", ('<a href="/logout" class="tabbtn" style="float:right;color:rgba(255,255,255,.55)" '
                                            'title="退出登录">🚪 退出</a>') if CFG["AUTH_USER"] else "")
                    .replace("{{LOGS}}", logs or "<tr><td colspan=2><div class=empty><div class=ei>📋</div><div class=et>还没有活动日志</div></div></td></tr>"))
        b = html.encode("utf-8")
        s.send_response(200); s.send_header("Content-Type","text/html; charset=utf-8")
        s.send_header("Cache-Control","no-cache, no-store, must-revalidate")
        s.send_header("Content-Length",str(len(b))); s.end_headers(); s.wfile.write(b)
    def _detail(s):
        from urllib.parse import urlparse, parse_qs
        h = (parse_qs(urlparse(s.path).query).get("hash",[""])[0]).strip()
        c = db()
        t = c.execute("SELECT name,source,size,files,query,status,matched,injected FROM torrents WHERE info_hash=?", (h,)).fetchone()
        if not t:
            c.close(); s.send_response(404); s.end_headers(); s.wfile.write(b"not found"); return
        # 每个目标站只显示一次，取最新一条(MAX(ts) 时 sqlite 会带出同一行的其它列)
        ms = c.execute("SELECT indexer,matched_name,mode,result,MAX(ts) FROM matches WHERE info_hash=? GROUP BY indexer ORDER BY indexer", (h,)).fetchall()
        c.close()
        mrows = ""
        rmap = {"injected":"✅ 已注入做种","tracker":"✅ 已加Tracker做种(同hash)","duplicate":"⚠️ 已存在","matched":"匹配","inject_err":"注入出错"}
        for m in ms:
            rr = rmap.get(m[3], m[3]); col = "var(--ok)" if m[3]=="injected" else ("var(--warn)" if m[3]=="duplicate" else "var(--sub)")
            mrows += f"<tr><td><span class=src>{esc(m[0])}</span></td><td class=mut>{'加Tracker' if m[2]=='tracker' else ('硬链接' if m[2]=='link' else '同名直注')}</td><td style='color:{col}'>{rr}</td></tr>"
        injected = t[7]
        page = (DETAIL.replace("{{NAME}}", esc(t[0])).replace("{{SRC}}", esc(t[1] or '?'))
                .replace("{{SIZE}}", f"{t[2]/1024**3:.2f}").replace("{{FILES}}", str(t[3]))
                .replace("{{QUERY}}", esc(t[4])).replace("{{MATCHED}}", str(t[6])).replace("{{INJECTED}}", str(injected))
                .replace("{{MROWS}}", mrows or "<tr><td colspan=3 class=mut>暂无辅种记录</td></tr>"))
        b = page.encode("utf-8"); s.send_response(200); s.send_header("Content-Type","text/html; charset=utf-8"); s.send_header("Content-Length",str(len(b))); s.end_headers(); s.wfile.write(b)
    def _research(s):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(s.path).query)
        h = (q.get("hash",[""])[0]).strip(); cq = (q.get("q",[""])[0]).strip()
        ok = bool(h and cq)
        if ok:
            threading.Thread(target=manual_research, args=(h, cq), daemon=True).start()
        b = json.dumps({"ok": ok}).encode()
        s.send_response(200); s.send_header("Content-Type","application/json"); s.send_header("Content-Length",str(len(b))); s.end_headers(); s.wfile.write(b)
    def _json(s):
        c = db(); data = {"torrents": [dict(zip(["name","query","matched","injected","status"], r)) for r in c.execute("SELECT name,query,matched,injected,status FROM torrents ORDER BY last_searched DESC LIMIT 200").fetchall()]}; c.close()
        b = json.dumps(data, ensure_ascii=False).encode(); s.send_response(200); s.send_header("Content-Type","application/json; charset=utf-8"); s.end_headers(); s.wfile.write(b)
    def _wecom_get(s):
        # 企微后台保存回调配置时的 URL 验证:验签→解密 echostr→回明文
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(s.path).query)
        sig = q.get("msg_signature", [""])[0]; ts = q.get("timestamp", [""])[0]
        nc = q.get("nonce", [""])[0]; ec = q.get("echostr", [""])[0]
        try:
            if _wecom_sig(ts, nc, ec) != sig: raise ValueError("签名不符")
            msg, _rid = wecom_decrypt(ec)
            b = msg.encode()
            s.send_response(200); s.send_header("Content-Length", str(len(b))); s.end_headers(); s.wfile.write(b)
            logmsg("INFO", "企微回调 URL 验证通过 ✅")
        except Exception as e:
            logmsg("WARN", f"企微URL验证失败: {str(e)[:40]}")
            s.send_response(400); s.end_headers()
    def _wecom_post(s):
        # 收消息:验签→解密→文本交给会话逻辑(异步),立刻回空(躲开企微5秒超时)
        from urllib.parse import urlparse, parse_qs
        import xml.etree.ElementTree as ET
        q = parse_qs(urlparse(s.path).query)
        sig = q.get("msg_signature", [""])[0]; ts = q.get("timestamp", [""])[0]; nc = q.get("nonce", [""])[0]
        try:
            ln = int(s.headers.get("Content-Length", "0"))
            enc = ET.fromstring(s.rfile.read(ln)).findtext("Encrypt") or ""
            if _wecom_sig(ts, nc, enc) != sig: raise ValueError("签名不符")
            xmlmsg, _rid = wecom_decrypt(enc)
            root = ET.fromstring(xmlmsg)
            if (root.findtext("MsgType") or "") == "text":
                threading.Thread(target=wecom_on_text, args=(root.findtext("Content") or "",), daemon=True).start()
        except Exception as e:
            logmsg("WARN", f"企微消息处理失败: {str(e)[:40]}")
        s.send_response(200); s.send_header("Content-Length", "0"); s.end_headers()
    def _send_json(s, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        s.send_response(200); s.send_header("Content-Type","application/json; charset=utf-8"); s.send_header("Content-Length",str(len(b))); s.end_headers(); s.wfile.write(b)
    def _reid(s):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(s.path).query)
        h = (q.get("hash",[""])[0]).strip(); v = (q.get("q",[""])[0]).strip()
        if not (h and v): s._send_json({"ok":False,"err":"参数缺失"}); return
        try:
            s._send_json(manual_organize(h, v))
        except Exception as e:
            logmsg("ERROR", f"手动入库异常: {e}"); s._send_json({"ok":False,"err":str(e)[:80]})
    def _search(s):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(s.path).query)
        q = (qs.get("q",[""])[0]).strip()
        cats = [c for c in (qs.get("cats",[""])[0]).split(",") if c.isdigit()]
        if not q: s._send_json({"ok":False,"err":"关键词为空"}); return
        name, year = split_query(q)
        try:
            results = prowlarr_search(name, cats)
        except Exception as e:
            logmsg("WARN", f"搜索下载查询失败[{q}]: {e}"); s._send_json({"ok":False,"err":str(e)[:80]}); return
        s._send_json(search_group(q, results, anchor=query_anchor(name, year)))
    def _person(s):
        from urllib.parse import urlparse, parse_qs
        q = (parse_qs(urlparse(s.path).query).get("q",[""])[0]).strip()
        if not q: s._send_json({"ok":False,"err":"关键词为空"}); return
        if not CFG["TMDB_KEY"]: s._send_json({"ok":False,"err":"未配置 TMDB Key,人物搜索用不了"}); return
        s._send_json({"ok":True,"list":tmdb_person_search(q)})
    def _personcredits(s):
        from urllib.parse import urlparse, parse_qs
        pid = (parse_qs(urlparse(s.path).query).get("id",[""])[0]).strip()
        if not pid.isdigit(): s._send_json({"ok":False,"err":"人物 id 不合法"}); return
        s._send_json(tmdb_person_credits(int(pid)))
    def _douban(s):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(s.path).query)
        col = (qs.get("col", [""])[0]).strip()
        st  = (qs.get("start", ["0"])[0]).strip()
        start = int(st) if st.isdigit() and int(st) <= 200 else 0
        try:
            s._send_json(douban_shelf(col, start))
        except Exception as e:
            s._send_json({"ok": False, "err": str(e)[:80]})
    def _dbimg(s):
        """豆瓣图片代理:豆瓣图床认 Referer,浏览器直连必然 403,只能自己带头去取。
           只放行豆瓣自家图床域名,免得这个端点被当成任意 URL 抓取器。"""
        from urllib.parse import urlparse, parse_qs, unquote
        u = unquote((parse_qs(urlparse(s.path).query).get("u", [""])[0]).strip())
        cache = _dbimg_cache(u)
        if not cache:
            s.send_response(404); s.end_headers(); return
        if not os.path.exists(cache): _dbimg_warm(u)     # 预热漏掉的(换一批点太快)现抓
        try:
            data = open(cache, "rb").read()
        except Exception:
            s.send_response(404); s.end_headers(); return
        ext = os.path.basename(cache).rsplit(".", 1)[-1].lower()
        s.send_response(200)
        s.send_header("Content-Type", {"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg"))
        s.send_header("Cache-Control", "max-age=604800"); s.send_header("Content-Length", str(len(data)))
        s.end_headers(); s.wfile.write(data)
    def _artist(s):
        from urllib.parse import urlparse, parse_qs
        a = (parse_qs(urlparse(s.path).query).get("q",[""])[0]).strip()
        if not a: s._send_json({"ok":False,"err":"歌手名为空"}); return
        jid = str(int(time.time()*1000)) + "-" + base64.b16encode(os.urandom(2)).decode()
        _AJOBS[jid] = {"log": [], "fin": False, "result": None, "ts": time.time()}
        threading.Thread(target=_ajob_run, args=(jid, a), daemon=True).start()
        for k in [k for k, v in list(_AJOBS.items()) if time.time()-v["ts"] > 1800 and k != jid]:
            _AJOBS.pop(k, None)
        s._send_json({"ok": True, "id": jid})
    def _artstat(s):
        from urllib.parse import urlparse, parse_qs
        j = _AJOBS.get((parse_qs(urlparse(s.path).query).get("id",[""])[0]).strip())
        if not j: s._send_json({"ok":False,"err":"任务不存在或已过期"}); return
        s._send_json({"ok":True,"fin":j["fin"],"log":j["log"][-6:],
                      "result": j["result"] if j["fin"] else None})
    def _batchplan(s):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(s.path).query)
        col = (q.get("col",["classic"])[0]).strip()
        n = int((q.get("n",["50"])[0]) or 50); n = max(1, min(n, 250))
        p4k = (q.get("k",[""])[0]) == "1"
        jid = str(int(time.time()*1000)) + "-" + base64.b16encode(os.urandom(2)).decode()
        _BJOBS[jid] = {"rows": [], "done": 0, "total": n, "cur": "", "fin": False, "ts": time.time()}
        threading.Thread(target=_bjob_run, args=(jid, col, n, p4k), daemon=True).start()
        for k in [k for k, v in list(_BJOBS.items()) if time.time()-v["ts"] > 3600 and k != jid]:
            _BJOBS.pop(k, None)
        s._send_json({"ok": True, "id": jid})
    def _batchstat(s):
        from urllib.parse import urlparse, parse_qs
        j = _BJOBS.get((parse_qs(urlparse(s.path).query).get("id",[""])[0]).strip())
        if not j: s._send_json({"ok": False, "err": "任务不存在或已过期"}); return
        s._send_json({"ok": True, "done": j["done"], "total": j["total"], "cur": j.get("cur",""),
                      "fin": j["fin"], "err": j.get("err",""), "rows": j["rows"]})
    def _batchgo(s):
        try:
            n = int(s.headers.get("Content-Length") or 0)
            _b = json.loads(s.rfile.read(n) or b"{}")
            rows = _b.get("rows") or []; _cat = _b.get("cat") or ""
        except Exception as e:
            s._send_json({"ok": False, "err": f"请求解析失败:{e}"}); return
        if not rows: s._send_json({"ok": False, "err": "没有选中任何片子"}); return
        s._send_json(batch_download(rows, _cat))
    def _libaudit(s):
        from urllib.parse import urlparse, parse_qs
        f = (parse_qs(urlparse(s.path).query).get("force",[""])[0]) == "1"
        try: s._send_json(library_audit(force=f))
        except Exception as e: s._send_json({"ok":False,"err":str(e)[:80]})
    def _libcat(s):
        from urllib.parse import urlparse, parse_qs, unquote
        q = parse_qs(urlparse(s.path).query)
        tgt = unquote((q.get("target",[""])[0]).strip()); cat = unquote((q.get("cat",[""])[0]).strip())
        if not tgt: s._send_json({"ok":False,"err":"缺少 target"}); return
        s._send_json(library_setcat(tgt, cat))
    def _libfix(s):
        try: s._send_json(library_fix())
        except Exception as e:
            logmsg("ERROR", f"媒体库修正失败: {e}"); s._send_json({"ok":False,"err":str(e)[:80]})
    def _search2(s):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(s.path).query)
        q = (qs.get("q",[""])[0]).strip()
        filt = (qs.get("f",[""])[0]).strip()
        if filt not in FILTER_CATS: filt = ""
        if not q: s._send_json({"ok":False,"err":"关键词为空"}); return
        jid = str(int(time.time()*1000)) + "-" + base64.b16encode(os.urandom(2)).decode()  # 加随机尾防同毫秒撞号
        _SJOBS[jid] = {"log": [], "done": False, "result": None, "ts": time.time()}
        threading.Thread(target=_sjob_run, args=(jid, q, filt), daemon=True).start()
        for k in [k for k, v in list(_SJOBS.items()) if time.time()-v["ts"] > 600 and k != jid]:
            _SJOBS.pop(k, None)
        s._send_json({"ok":True,"id":jid})
    def _searchstat(s):
        from urllib.parse import urlparse, parse_qs
        jid = (parse_qs(urlparse(s.path).query).get("id",[""])[0]).strip()
        j = _SJOBS.get(jid)
        if not j: s._send_json({"ok":False,"err":"任务不存在或已过期"}); return
        s._send_json({"ok":True,"log":j["log"],"done":j["done"],"prog":j.get("prog") or {},
                      "result":(j["result"] if j["done"] else None)})
    def _canceldl(s):
        from urllib.parse import urlparse, parse_qs
        h = (parse_qs(urlparse(s.path).query).get("hash",[""])[0]).strip()
        if not h: s._send_json({"ok":False,"err":"缺hash"}); return
        try:
            qb = qb_conn()
            t = next((x for x in qb.torrents() if x.get("hash")==h), None)
            # 护栏:只能取消**还没下完**的。原先对任意 hash 直接删任务+删数据 ——
            # 传进来一个已完成的,数据就没了:已入库的媒体库那份是硬链接(inode 还在,文件不会消失),
            # 但做种那份的数据被删 = 立刻掉种。已完成的要删,走「做种运营」页,那里有目录边界护栏。
            if t and (t.get("progress") or 0) >= 1:
                s._send_json({"ok": False, "err": "这个已经下载完成了,取消下载不适用。"
                                                 "要删请去「🌊 做种运营」——那里会先确认数据在不在保种目录"})
                return
            qb.delete(h, delete_files=True)   # 删任务+已下数据(取消=不要了)
            _DLMETA.pop(h, None)
            logmsg("INFO", f"用户取消下载: {(t or {}).get('name','')[:44]}")
            s._send_json({"ok":True})
        except Exception as e:
            s._send_json({"ok":False,"err":str(e)[:60]})
    def _overview(s):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(s.path).query)
        mt = (qs.get("mt", ["tv"])[0]) or "tv"; tid = (qs.get("id", ["0"])[0])
        if not tid.isdigit() or tid == "0": s._send_json({"ok": False}); return
        key = (mt, tid)
        hit = _OVCACHE.get(key)
        if hit and time.time() - hit[1] < 86400:
            s._send_json(hit[0]); return
        d = tmdb_details("movie" if mt == "movie" else "tv", int(tid))
        name = d.get("name") or d.get("title") or ""
        emby = ""
        try:
            if CFG["EMBY_URL"] and CFG["EMBY_KEY"] and name:
                E = CFG["EMBY_URL"].rstrip("/") + "/emby"; k = CFG["EMBY_KEY"]
                global _EMBY_SID
                if not _EMBY_SID:
                    _EMBY_SID = json.load(urllib.request.urlopen(E + "/System/Info?api_key=" + k, timeout=8)).get("Id", "")
                it = json.load(urllib.request.urlopen(
                    E + "/Items?IncludeItemTypes=Series,Movie&Recursive=true&SearchTerm="
                    + urllib.parse.quote(name) + "&Limit=1&api_key=" + k, timeout=8)).get("Items") or []
                if it:
                    emby = f"/web/index.html#!/item?id={it[0]['Id']}&serverId={_EMBY_SID}"
        except Exception: pass
        resp = {"ok": True, "name": name,
                "year": (d.get("first_air_date") or d.get("release_date") or "")[:4],
                "overview": d.get("overview") or "暂无简介",
                "poster": d.get("poster_path") or "", "emby": emby}
        if len(_OVCACHE) > 500: _OVCACHE.clear()
        _OVCACHE[key] = (resp, time.time())
        s._send_json(resp)
    def _settings_get(s):
        groups = []
        for gname, fields in SETTING_GROUPS:
            groups.append({"name": gname, "fields": [
                {"key": k, "label": lb, "hint": h, "secret": sec, "value": str(CFG.get(k, ""))}
                for k, lb, h, sec in fields]})
        s._send_json({"ok": True, "groups": groups})
    def _settings_post(s):
        try:
            ln = int(s.headers.get("Content-Length", "0"))
            body = json.loads(s.rfile.read(ln) or b"{}")
            save_settings({k: v for k, v in body.items() if isinstance(v, (str, int, float))})
            logmsg("INFO", f"设置已更新({len(body)}项),热生效")
            s._send_json({"ok": True})
        except Exception as e:
            s._send_json({"ok": False, "err": str(e)[:80]})
    def _svc_test(s):
        from urllib.parse import urlparse, parse_qs
        svc = (parse_qs(urlparse(s.path).query).get("svc", [""])[0])
        try:
            if svc == "tr":
                v = tr_conn().call("session-get", {})["arguments"].get("version", "?")
                s._send_json({"ok": True, "msg": f"Transmission {v}"})
            elif svc == "qb":
                v = qb_conn()._get("/api/v2/app/version").decode("utf-8", "ignore")
                s._send_json({"ok": True, "msg": f"qBittorrent {v}"})
            elif svc == "prowlarr":
                req = urllib.request.Request(CFG["PROWLARR_URL"].rstrip("/") + "/api/v1/indexer",
                                             headers={"X-Api-Key": CFG["PROWLARR_KEY"]})
                n = len(json.load(urllib.request.urlopen(req, timeout=10)))
                s._send_json({"ok": True, "msg": f"Prowlarr 连通,{n} 个索引器"})
            elif svc == "tmdb":
                d = _tmdb_call("/configuration")
                s._send_json({"ok": bool(d.get("images")), "msg": "TMDB 连通(代理OK)" if d.get("images") else "TMDB 响应异常"})
            elif svc == "emby":
                d = json.load(urllib.request.urlopen(
                    CFG["EMBY_URL"].rstrip("/") + "/emby/System/Info?api_key=" + CFG["EMBY_KEY"], timeout=10))
                s._send_json({"ok": True, "msg": f"Emby {d.get('Version','?')}"})
            elif svc == "lrcapi":
                urllib.request.urlopen(CFG["LRCAPI_URL"].rstrip("/") + "/lyrics?title=test", timeout=8).read()
                s._send_json({"ok": True, "msg": "LrcApi 连通"})
            elif svc == "wecom":
                base = (CFG["WECOM_PROXY"] or "https://qyapi.weixin.qq.com").rstrip("/")
                d = json.load(_wecom_opener().open(
                    f"{base}/cgi-bin/gettoken?corpid={CFG['WECOM_CORPID']}&corpsecret={CFG['WECOM_SECRET']}", timeout=12))
                ok = d.get("errcode") == 0
                s._send_json({"ok": ok, "msg": "企微凭证有效" if ok else f"企微: {d.get('errmsg','')[:40]}"})
            else:
                s._send_json({"ok": False, "msg": "未知服务"})
        except Exception as e:
            s._send_json({"ok": False, "msg": str(e)[:70]})
    def _dashboard(s):
        out = {}
        try:
            import shutil
            du = shutil.disk_usage("/data")
            out["disk"] = {"total": du.total, "used": du.used, "free": du.free}
        except Exception: pass
        try:
            st = tr_conn().call("session-stats", {})["arguments"]
            out["tr"] = {"up": st.get("uploadSpeed", 0), "down": st.get("downloadSpeed", 0),
                         "count": st.get("torrentCount", 0), "active": st.get("activeTorrentCount", 0),
                         "up_today": (st.get("current-stats") or {}).get("uploadedBytes", 0),
                         "up_total": (st.get("cumulative-stats") or {}).get("uploadedBytes", 0)}
        except Exception: pass
        try:
            q = json.loads(qb_conn()._get("/api/v2/transfer/info").decode())
            out["qb"] = {"down": q.get("dl_info_speed", 0), "up": q.get("up_info_speed", 0)}
        except Exception: pass
        global _DASH_MEDIA
        try:
            if time.time() - _DASH_MEDIA.get("ts", 0) > 600:
                cnt = {"movie": 0, "tv": 0, "anime": 0, "song": 0}
                for root, key in ((CFG["MEDIA_MOVIE"], "movie"), (CFG["MEDIA_TV"], "tv"),
                                  (CFG["MEDIA_ANIME"], "anime")):   # 动漫库留空=归进 tv,这里跳过免重复计数
                    if root and os.path.isdir(root):
                        ents = os.listdir(root)
                        cnt[key] = sum(1 for e in ents if os.path.isdir(os.path.join(root, e))) +                                    sum(1 for e in ents if e.lower().endswith((".mkv", ".mp4", ".ts", ".avi")))
                mroot = CFG["MEDIA_MUSIC"]
                if os.path.isdir(mroot):
                    n = 0
                    for _r, _d, fs in os.walk(mroot):
                        n += sum(1 for f in fs if f.lower().endswith((".flac", ".mp3", ".ape", ".wav", ".m4a")))
                    cnt["song"] = n
                _DASH_MEDIA = {"ts": time.time(), "cnt": cnt}
            out["media"] = _DASH_MEDIA["cnt"]
        except Exception: pass
        s._send_json(out)
    def _downloads(s):
        out = {"dl": [], "done": []}
        try:
            for t in qb_conn().torrents():
                eta = t.get("eta", 0) or 0; prog = (t.get("progress") or 0)
                if eta >= 8640000 or prog >= 1: etas = ""
                elif eta >= 3600: etas = f"{eta//3600}时{eta%3600//60}分"
                elif eta >= 60: etas = f"{eta//60}分{eta%60}秒"
                else: etas = f"{eta}秒"
                meta = _dlmeta(t.get("hash",""), t.get("name",""))
                out["dl"].append({"hash": t.get("hash",""), "name": t.get("name",""), "progress": round(prog*100, 1),
                                  "sizeh": human_size(t.get("size",0)), "speed": human_size(t.get("dlspeed",0))+"/s",
                                  "eta": etas, "state": t.get("state",""), "seeds": t.get("num_seeds",0),
                                  "tmdb": meta["tmdb"], "year": meta["year"], "poster": meta["poster"]})
        except Exception as e:
            out["err"] = str(e)[:60]
        c = db()
        for r in c.execute("SELECT name,cat,tmdb_name,year,status,ts FROM media ORDER BY ts DESC LIMIT 10").fetchall():
            out["done"].append({"name": r[0], "cat": r[1] or "", "tmdb": (f"{r[2]} ({r[3]})" if r[2] else "—"),
                                "status": r[4], "ts": time.strftime("%m-%d %H:%M", time.localtime(r[5] or 0))})
        c.close()
        s._send_json(out)
    def _bgv(s):
        p = os.path.join(os.path.dirname(CFG["DB"]), "wave.mp4")
        if not os.path.exists(p):
            s.send_response(404); s.end_headers(); return
        size = os.path.getsize(p)
        rng = s.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0); end = min(int(b) if b else size - 1, size - 1)
            s.send_response(206)
            s.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            start, end = 0, size - 1
            s.send_response(200)
        length = end - start + 1
        s.send_header("Content-Type", "video/mp4"); s.send_header("Accept-Ranges", "bytes")
        s.send_header("Cache-Control", "max-age=604800"); s.send_header("Content-Length", str(length))
        s.end_headers()
        with open(p, "rb") as f:
            f.seek(start); s.wfile.write(f.read(length))
    def _bg(s):
        p = os.path.join(os.path.dirname(CFG["DB"]), "wave.jpg")
        if not os.path.exists(p):
            s.send_response(404); s.end_headers(); return
        data = open(p, "rb").read()
        s.send_response(200); s.send_header("Content-Type", "image/jpeg")
        s.send_header("Cache-Control", "max-age=604800"); s.send_header("Content-Length", str(len(data)))
        s.end_headers(); s.wfile.write(data)
    def _poster(s):
        # 海报代理：TMDB 图片国内不通，走 TMDB_PROXY 抓取并缓存在 /config/posters
        from urllib.parse import urlparse, parse_qs
        p = (parse_qs(urlparse(s.path).query).get("p",[""])[0]).strip()
        if not re.match(r'^/[A-Za-z0-9._-]+\.(jpg|jpeg|png)$', p):
            s.send_response(404); s.end_headers(); return
        cache = os.path.join(os.path.dirname(CFG["DB"]), "posters", p.lstrip("/"))
        data = None
        if os.path.exists(cache):
            data = open(cache, "rb").read()
        else:
            try:
                op = urllib.request.build_opener(urllib.request.ProxyHandler(
                    {"http":CFG["TMDB_PROXY"],"https":CFG["TMDB_PROXY"]})) if CFG["TMDB_PROXY"] else urllib.request.build_opener()
                data = op.open("https://image.tmdb.org/t/p/w185" + p, timeout=20).read()
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                open(cache, "wb").write(data)
            except Exception:
                s.send_response(404); s.end_headers(); return
        s.send_response(200); s.send_header("Content-Type","image/jpeg")
        s.send_header("Cache-Control","max-age=604800"); s.send_header("Content-Length",str(len(data)))
        s.end_headers(); s.wfile.write(data)
    def _shot(s):
        from urllib.parse import urlparse, parse_qs
        f = (parse_qs(urlparse(s.path).query).get("f",[""])[0]).strip()
        if not re.match(r'^[A-Za-z0-9._-]+\.jpg$', f):    # 只允许 basename,防目录穿越
            s.send_response(404); s.end_headers(); return
        fp = os.path.join(SHOTS_DIR, f)
        if not os.path.exists(fp):
            s.send_response(404); s.end_headers(); return
        data = open(fp, "rb").read()
        s.send_response(200); s.send_header("Content-Type","image/jpeg")
        s.send_header("Cache-Control","max-age=604800"); s.send_header("Content-Length",str(len(data)))
        s.end_headers(); s.wfile.write(data)
    def _cover(s):
        """音乐专辑封面:只允许音乐库根目录之内的相对路径,realpath 二次校验防穿越"""
        from urllib.parse import urlparse, parse_qs, unquote
        p = unquote((parse_qs(urlparse(s.path).query).get("p", [""])[0]).strip())
        root = os.path.realpath(CFG["MEDIA_MUSIC"])
        fp = os.path.realpath(os.path.join(root, p))
        if ".." in p or not fp.startswith(root + os.sep) or not os.path.isfile(fp) \
           or os.path.splitext(fp)[1].lower() not in (".jpg", ".jpeg", ".png"):
            s.send_response(404); s.end_headers(); return
        data = open(fp, "rb").read()
        s.send_response(200)
        s.send_header("Content-Type", "image/png" if fp.lower().endswith(".png") else "image/jpeg")
        s.send_header("Cache-Control", "max-age=604800"); s.send_header("Content-Length", str(len(data)))
        s.end_headers(); s.wfile.write(data)
    # ---- 批量保种 ----
    def _ks(s):
        from urllib.parse import urlparse, parse_qs
        q_ = parse_qs(urlparse(s.path).query)
        act = urlparse(s.path).path.rsplit("/", 1)[-1]
        if act == "indexers":
            try:
                s._send_json({"ok": True, "list": [{"id": i["id"], "name": i.get("name", "?")} for i in prowlarr_indexers()]})
            except Exception as e:
                s._send_json({"ok": False, "err": str(e)[:60]})
            return
        if act == "list":
            try:
                rs = ks_browse(int(q_.get("ix", ["0"])[0]), (q_.get("q", [""])[0]).strip(), int(q_.get("page", ["0"])[0]))
                items = [{"name": r.get("title") or "", "size": r.get("size") or 0, "sizeh": human_size(r.get("size") or 0),
                          "seeders": r.get("seeders", 0), "url": r.get("downloadUrl") or "",
                          "free": r.get("free", 0), "off": r.get("off", 0),
                          "date": (r.get("publishDate") or "")[:10], "noxfer": noxfer(r.get("title") or "")}
                         for r in rs if r.get("downloadUrl")]
                s._send_json({"ok": True, "items": items})
            except Exception as e:
                s._send_json({"ok": False, "err": str(e)[:80]})
            return
        if act == "status":
            c = db()
            cnt = dict(c.execute("SELECT status,COUNT(*) FROM keepseed GROUP BY status").fetchall())
            recent = [{"name": r[0], "st": r[1], "err": r[2] or ""} for r in
                      c.execute("SELECT name,status,err FROM keepseed ORDER BY id DESC LIMIT 25").fetchall()]
            c.close()
            free_gb = 0
            try: free_gb = round(shutil.disk_usage("/data").free / 2**30)
            except Exception: pass
            s._send_json({"ok": True, "running": _KS["running"], "cur": _KS["cur"], "msg": _KS["msg"],
                          "queued": cnt.get("queued", 0), "pushed": cnt.get("pushed", 0),
                          "done": cnt.get("done", 0), "error": cnt.get("error", 0), "free": free_gb,
                          "af": _KSF["running"], "afmsg": _KSF["msg"],
                          "fw": CFG["FREE_WATCH_IX"], "fwmsg": _FW["msg"], "fwmin": CFG["FREE_WATCH_MIN"]})
            return
        if act == "stop":
            _KS["stop"] = True; _KSF["stop"] = True
            c = db(); c.execute("UPDATE keepseed SET status='skip' WHERE status='queued'"); c.commit(); c.close()
            s._send_json({"ok": True}); return
        if act == "retry":     # 批量重试:失败项重新排队
            c = db(); n = c.execute("UPDATE keepseed SET status='queued', err='' WHERE status='error'").rowcount
            c.commit(); c.close()
            if n and not _KS["running"]:
                threading.Thread(target=keepseed_worker, daemon=True).start()
            s._send_json({"ok": True, "n": n}); return
        if act == "clear":     # 批量清理历史记录: what=done|error|skip|all
            what = q_.get("what", ["done"])[0]
            sql = {"done": "status='done'", "error": "status='error'", "skip": "status='skip'",
                   "all": "status IN ('done','error','skip')"}.get(what, "status='done'")
            c = db(); n = c.execute("DELETE FROM keepseed WHERE " + sql).rowcount; c.commit(); c.close()
            s._send_json({"ok": True, "n": n}); return
        if act == "auto":
            if _KSF["running"]:
                s._send_json({"ok": False, "err": "自动拉取已在跑"}); return
            try:
                ixid = int(q_.get("ix", ["0"])[0]); tgt = float(q_.get("target", ["0"])[0])
                mg = float(q_.get("fsize", ["0"])[0] or 0)
                fs = q_.get("fseed", [""])[0]
                ms = None if fs in ("", "-1") else int(fs)
            except Exception:
                s._send_json({"ok": False, "err": "参数不对"}); return
            if not ixid or tgt <= 0:
                s._send_json({"ok": False, "err": "要选站点并填目标量"}); return
            threading.Thread(target=ks_autofill,
                             args=(ixid, (q_.get("q", [""])[0]).strip(), tgt, mg, ms,
                                   q_.get("free", ["0"])[0] == "1", q_.get("off", ["0"])[0] == "1"), daemon=True).start()
            s._send_json({"ok": True}); return
        if act == "watch":     # 抢免费守候开关: ix=站点id 开 / ix=空 关
            ixv = (q_.get("ix", [""])[0]).strip()
            save_settings({"FREE_WATCH_IX": ixv})
            _FW["msg"] = "已开启,等下一轮巡查" if ixv else "已关闭"
            logmsg("INFO", f"抢免费守候: {'开启 站点id='+ixv if ixv else '关闭'}")
            s._send_json({"ok": True}); return
        s._send_json({"ok": False, "err": "未知操作"})
    def _ks_add(s):
        try:
            ln = int(s.headers.get("Content-Length", "0"))
            body = json.loads(s.rfile.read(ln) or b"{}")
        except Exception:
            body = {}
        items = body.get("items") or []
        ix = str(body.get("ix") or "")
        n = 0
        c = db()
        have = {(r[0], r[1]) for r in c.execute("SELECT name,size FROM keepseed WHERE status IN ('queued','pushed','done')").fetchall()}
        for it in items[:500]:
            if not it.get("url"): continue
            if (it.get("name") or "", it.get("size") or 0) in have: continue   # 已推过的不重复(qb会拒收记成失败)
            have.add((it.get("name") or "", it.get("size") or 0))
            # 注意:禁转种照样可以保种(只是不能转出去),不拦队列;禁转红线在转种助手里守
            c.execute("INSERT INTO keepseed(name,size,url,indexer,status,err,ts) VALUES(?,?,?,?,?,?,?)",
                      (it.get("name") or "", it.get("size") or 0, it["url"], ix, "queued", "", int(time.time())))
            n += 1
        c.commit(); c.close()
        if n and not _KS["running"]:
            threading.Thread(target=keepseed_worker, daemon=True).start()
        logmsg("INFO", f"批量保种入队 {n} 个")
        s._send_json({"ok": True, "n": n})
    def _dl(s):
        from urllib.parse import urlparse, parse_qs
        qs_ = parse_qs(urlparse(s.path).query)
        s._dl_run((qs_.get("url",[""])[0]).strip(), (qs_.get("cat",[""])[0]).strip(), [])
    def _dl_run(s, u, ucat, mates):
        if not u: s._send_json({"ok":False,"err":"缺少下载链接"}); return
        try:
            data = prowlarr_download(u)
            if data[:1] != b'd': s._send_json({"ok":False,"err":"返回的不是种子文件"}); return
            try: cname, _ = torrent_files(data)
            except Exception: cname = ""
            catmap = {"music":"音乐","anime":"动漫","tv":"电视剧","movie":"电影"}
            cat = catmap.get(ucat) or CFG["QB_CATEGORY"] or media_category(cname or "", None)
            if cname and mates:   # 预存同组其他站的种子,下载完成后免搜索直接辅种
                try:
                    c = db(); c.execute("INSERT OR REPLACE INTO pending_seed(name,data,ts) VALUES(?,?,?)",
                                        (cname, json.dumps(mates[:60], ensure_ascii=False), int(time.time())))
                    c.commit(); c.close()
                except Exception: pass
            res = qb_conn().add(data, category=cat, tags="packseed")
            ok = "Ok" in res
            logmsg("INFO", f"搜索下载 → qb[{cat}]: {(cname or u)[:40]} [{res.strip()[:16] or 'ok'}]")
            s._send_json({"ok":ok, "err":"" if ok else (res[:60] or "qb 拒绝")})
        except Exception as e:
            logmsg("ERROR", f"搜索下载失败: {e}"); s._send_json({"ok":False,"err":str(e)[:80]})

def esc(t): return (t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("'","&#39;")

# ============ 稳定性:自动备份 / DB清理 / 心跳告警 ============
def backup_data():
    """每天把 DB + settings 打包到 /config/backups,保留最近 N 份"""
    import tarfile, glob
    bdir = os.path.join(os.path.dirname(CFG["DB"]), "backups")
    os.makedirs(bdir, exist_ok=True)
    out = os.path.join(bdir, "backup-" + time.strftime("%Y%m%d-%H%M%S") + ".tar.gz")
    with tarfile.open(out, "w:gz") as tf:
        for f in (CFG["DB"], SETTINGS_FILE):
            if os.path.exists(f): tf.add(f, arcname=os.path.basename(f))
    try: os.chmod(out, 0o600)   # 含 settings 密钥
    except Exception: pass
    baks = sorted(glob.glob(os.path.join(bdir, "backup-*.tar.gz")))
    for old in baks[:-CFG["BACKUP_KEEP"]] if CFG["BACKUP_KEEP"] > 0 else []:
        try: os.remove(old)
        except Exception: pass
    logmsg("INFO", f"🗄 已备份 → {os.path.basename(out)}(保留最近 {CFG['BACKUP_KEEP']} 份)")

def cleanup_db():
    """清 N 天前的日志 + 过期预存辅种,VACUUM 回收空间"""
    c = db()
    cut = int(time.time()) - CFG["LOG_KEEP_DAYS"] * 86400
    n = c.execute("DELETE FROM log WHERE ts < ?", (cut,)).rowcount
    c.execute("DELETE FROM pending_seed WHERE ts < ?", (int(time.time()) - 30 * 86400,))
    c.commit(); c.close()
    try:
        v = sqlite3.connect(CFG["DB"]); v.isolation_level = None
        v.execute("VACUUM"); v.close()
    except Exception: pass
    if n: logmsg("INFO", f"🧹 清理 {n} 条 {CFG['LOG_KEEP_DAYS']} 天前的日志,已 VACUUM")

def housekeeper():
    time.sleep(120)
    last_bak = last_clean = last_health = 0
    while True:
        try:
            if time.time() - last_bak > 86400:   backup_data(); last_bak = time.time()
            if time.time() - last_clean > 86400:  cleanup_db(); last_clean = time.time()
            if time.time() - last_health > 86400:   # 每天体检一次,掉线 tracker 推告警
                last_health = time.time()
                r = health_report(force=True)
                if r.get("ok") and r.get("offline"):
                    notify("🩺 做种健康提醒", f"{len(r['offline'])} 个种子 tracker 掉线(在做无效种),"
                           f"另有 {r['dead_n']} 个 0-peer 冷种。面板『做种健康』查看")
        except Exception as e:
            logmsg("ERROR", f"维护任务异常: {e}")
        time.sleep(3600)

_HEALTH = {"qb": {"fail": 0, "seen": False, "alerted": False},
           "tr": {"fail": 0, "seen": False, "alerted": False}}
def health_check(name, ok):
    """qb/tr 掉线告警。三条防误报的规矩(都是踩坑换来的):
    ①必须先成功连上过一次才可能告警——刚重启/服务还没起来的那几轮不算数;
    ②阈值提到 5 次(高负载下偶发超时很正常,别一抖就喊);
    ③同一次故障只告警一次,恢复后才重新武装。"""
    st = _HEALTH[name]
    if ok:
        if st["alerted"]:
            notify(f"✅ {name} 已恢复", f"{name} 连接恢复正常,已能读写")
        st.update(fail=0, seen=True, alerted=False)
    else:
        st["fail"] += 1
        if st["seen"] and not st["alerted"] and st["fail"] >= 5:
            st["alerted"] = True
            notify(f"⚠️ {name} 连接异常", f"已连续 {st['fail']} 次连不上 {name},检查下服务/网络/凭据")

def main():
    init_db()
    org = "开" if CFG["ORGANIZE"] and CFG["TMDB_KEY"] else "关"
    logmsg("INFO", f"PackSeed 启动，监听 {CFG['PORT']}，扫描间隔 {CFG['SCAN_INTERVAL']}s，整理入库[{org}]")
    if CFG["WECOM_TOKEN"] and CFG["WECOM_AESKEY"]:
        logmsg("INFO", f"企微双向交互就绪(AES自检{'✅' if aes_selftest() else '❌失败!'}),回调: /api/wecom")
    try:   # 账本还是空的(首次升级到覆盖驱动辅种)→ 后台回填一次,别让人对着空面板发愣
        if led_cov_stats()["content"] == 0:
            logmsg("INFO", "总账为空,后台回填 tr 现有种子(只算身份,不发搜索请求)")
            threading.Thread(target=backfill_ledger, daemon=True).start()
    except Exception: pass
    threading.Thread(target=scanner, daemon=True).start()
    threading.Thread(target=qb_watcher, daemon=True).start()
    threading.Thread(target=notify_worker, daemon=True).start()
    threading.Thread(target=free_watcher, daemon=True).start()
    threading.Thread(target=housekeeper, daemon=True).start()
    try:   # 保种队列有存货(上次重启打断的)则自动续跑
        c = db(); nq = c.execute("SELECT COUNT(*) FROM keepseed WHERE status='queued'").fetchone()[0]; c.close()
        if nq:
            logmsg("INFO", f"保种队列续跑: 还有 {nq} 个排队")
            threading.Thread(target=keepseed_worker, daemon=True).start()
    except Exception: pass
    ThreadingHTTPServer(("0.0.0.0", CFG["PORT"]), Handler).serve_forever()

if __name__ == "__main__":
    main()
