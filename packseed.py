#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PackSeed —— 辅种服务 (cross-seed 替代)
按"大小粗筛 + 文件清单精确比对"辅种，绕过名字解析，能辅跨季合集。
纯标准库：无第三方依赖。自带 sqlite 记录 + 网页仪表盘。
"""
import os, re, json, time, base64, sqlite3, threading, urllib.request, urllib.parse, socket, traceback
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
}

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
    try: c.execute("ALTER TABLE media ADD COLUMN save TEXT")   # 下载内容的磁盘路径(content_path)
    except Exception: pass
    c.commit(); c.close()

_WECOM = {"tok": "", "exp": 0}
_NQUEUE = []   # 未送达通知队列,后台线程重投
def _wecom_opener():
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
        d = search_group(q, prowlarr_search_fan(q))
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
        QB().add(data, category=cat, tags="packseed")
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
        qb = QB()
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

def wecom_on_text(text):
    text = (text or "").strip()
    if not text: return
    if text in ("撤回", "取消下载"):
        _chat_undo(); return
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
    notify(f"🔍 收到「{text}」,全站搜索中…", "约 40~60 秒,结果稍后推送")
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

def torrent_infohash(data):
    import hashlib
    return hashlib.sha1(bencode(bdecode(data)[b"info"])).hexdigest()

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
                raise
        raise RuntimeError("tr rpc fail")
    def torrents(s):
        r = s.call("torrent-get", {"fields":["hashString","name","totalSize","files","downloadDir","trackers","percentDone"]})
        return r.get("arguments", {}).get("torrents", [])
    def add(s, torrent_bytes, download_dir):
        return s.call("torrent-add", {"metainfo":base64.b64encode(torrent_bytes).decode(),"download-dir":download_dir,"paused":False})

# ============ qBittorrent WebUI（搜索下载的目标） ============
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
    def _post(s, path, params):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(s.url+path, data=data,
              headers=s._headers({"Content-Type":"application/x-www-form-urlencoded"}))
        return urllib.request.urlopen(req, timeout=30).read().decode("utf-8","ignore")
    def _get(s, path):
        req = urllib.request.Request(s.url+path, headers=s._headers())
        return urllib.request.urlopen(req, timeout=30).read()
    def add(s, data, category="", tags=""):
        b = "----packseed" + str(int(time.time()*1000)); parts = []
        def field(name, val):
            parts.append(("--"+b+"\r\nContent-Disposition: form-data; name=\""+name+"\"\r\n\r\n"+val+"\r\n").encode())
        parts.append(("--"+b+"\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"t.torrent\"\r\n"
                      "Content-Type: application/x-bittorrent\r\n\r\n").encode()); parts.append(data); parts.append(b"\r\n")
        if category: field("category", category)
        if tags: field("tags", tags)
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
CJK = re.compile(r'[一-鿿]')
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
def tmdb_match(name):
    hit = _TMDB_CACHE.get(name)
    if hit and time.time() - hit[1] < 21600:
        return hit[0]
    m = _tmdb_match_raw(name)
    if len(_TMDB_CACHE) > 2000: _TMDB_CACHE.clear()
    _TMDB_CACHE[name] = (m, time.time())
    return m

def _tmdb_match_raw(name):
    """解析 name → 匹配 TMDB。返回 dict(mtype,id,tmdb_name,year,conf,q) 或 None。结果缓存6小时。"""
    if not CFG["TMDB_KEY"]: return None
    tc, te = _anime_title(name)          # 番组命名优先(动漫)
    if not (tc or te):
        tc, te = meta_title_cn(name), meta_title_en(name)
    year = meta_year(name)
    # 名字里带集数/季标记 → 明确是剧集，别让剧场版/总集篇/舞台剧抢走匹配
    want_tv = bool(re.search(r'\[\d{1,4}(-\d{1,4})?(v\d)?\]|第\d{1,4}[话話集]|[-–]\s*\d{1,4}\s*[\[\(]|'
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
                return {"mtype":mt,"id":d["id"],"tmdb_name":(d.get("name") or d.get("title")),
                        "year":(d.get("first_air_date") or d.get("release_date") or "")[:4],"conf":"manual","q":str(tid)}
        except Exception: pass
    return None

def meta_is_music(n):
    return bool(re.search(r'\b(FLAC|APE|WAV|DSD|DSF|SACD|MQA|24bit|24-96|24-192|Hi-?Res|无损|MP3|320K)\b', n, re.I)
                and not re.search(r'\b(\d{3,4}[pi]|x26[45]|HEVC|BluRay|WEB-?DL|REMUX)\b', n, re.I))

def media_category(name, m):
    """qb 分类：音乐 > 动漫 > 电视剧 > 电影"""
    if meta_is_music(name): return "音乐"
    if meta_is_anime(name): return "动漫"
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

# ============ 整理入库（硬链接） + 转种 + 通知 Emby ============
def _safe(s): return re.sub(r'[\\/:*?"<>|]+',' ',(s or "")).strip()

def organize_files(files, m, cat):
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
            dst = os.path.join(dest_dir, os.path.basename(rel))
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
    c.execute("INSERT OR REPLACE INTO media(info_hash,name,cat,mtype,tmdbid,tmdb_name,year,target,conf,status,files,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (ih, name, "音乐", "music", None, name[:60], "", root, "music", "done", n, int(time.time())))
    c.commit(); c.close()
    lrc = fetch_lyrics(files, root)
    cov = fetch_covers(files, root)
    logmsg("INFO", f"音乐入库 {name[:44]} | {n}个文件 + {lrc}首歌词 + {cov}张封面 → {root}")
    notify("🎵 音乐已入库", f"{name[:56]}\n{n}个文件 · {lrc}首歌词 · {cov}张封面,Navidrome可听")
    return root, n

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
        for f in sorted(os.listdir(dest)):
            fp = os.path.join(dest, f)
            if not os.path.isfile(fp): continue
            stem, ext = os.path.splitext(f)
            is_video = ext.lower() in _VIDEO_EXT; is_sub = ext.lower() in (".srt",".ass",".sub")
            if not (is_video or is_sub): continue
            mm = _SE_RE.search(stem)
            if mm: ss, ee = int(mm.group(1)), int(mm.group(2))
            else:
                m2 = _EP_RE.search(stem)
                if not m2: continue
                ss, ee = 1, int(m2.group(1))
            newstem = f"{title} - S{ss:02d}E{ee:02d}"
            newf = newstem + ext
            if f != newf:
                np = os.path.join(dest, newf)
                if os.path.exists(np): newstem = stem
                else: os.rename(fp, np)
            if is_video:
                nfop = os.path.join(dest, newstem + ".nfo")
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
    c.execute("INSERT OR REPLACE INTO media(info_hash,name,cat,mtype,tmdbid,tmdb_name,year,target,conf,status,files,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (ih, name, cat, m["mtype"], m["id"], m["tmdb_name"], m["year"], "", m["conf"], "processing", len(files), int(time.time())))
    c.commit(); c.close()
    dest, n = organize_files(files, m, cat)
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
    c.execute("INSERT OR REPLACE INTO media(info_hash,name,cat,mtype,tmdbid,tmdb_name,year,target,conf,status,files,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (ih, name, cat, "", None, "", "", "", "", "hold", 0, int(time.time())))
    c.commit(); c.close()
    logmsg("WARN", f"整理待确认({reason}): {name[:44]}")
    notify("⚠️ 入库待确认", f"{name[:56]}\n{reason},去面板『整理入库』填片名或TMDB id一键入库")

def transfer_to_tr(qb, ih, name, save_path):
    """qb→tr 转种：tr 指向同一数据目录，校验后从 qb 删任务(数据保留)"""
    try:
        data = qb.export(ih)
        if data[:1] != b'd':
            logmsg("WARN", f"转种失败(导出非种子) {name[:36]}"); return False
        tr = TR(); resp = tr.add(data, save_path); args = resp.get("arguments", {})
        added = args.get("torrent-added") or args.get("torrent-duplicate")
        if added:
            if added.get("id"):
                try: tr.call("torrent-verify", {"ids": [added["id"]]})
                except Exception: pass
            qb.delete(ih, delete_files=False)
            logmsg("INFO", f"转种 qb→tr 完成(数据保留): {name[:40]}")
            return True
        logmsg("WARN", f"转种 tr 拒绝 {name[:36]}: {resp.get('result')}")
    except Exception as e:
        logmsg("ERROR", f"转种异常 {name[:30]}: {e}")
    return False

def process_completed(qb, t):
    """qb 一个种子下载完成后的全套处理"""
    ih = t["hash"]; name = t["name"]; sp = t["save_path"]
    try:
        files = [(os.path.join(sp, f["name"]), f["name"]) for f in qb.files(ih)]
    except Exception as e:
        logmsg("ERROR", f"取qb文件列表失败 {name[:30]}: {e}"); return
    if t.get("category") == "音乐" or meta_is_music(name):
        try: organize_music(ih, name, files)
        except Exception as e: logmsg("ERROR", f"音乐入库异常 {name[:30]}: {e}")
        if transfer_to_tr(qb, ih, name, sp):
            c = db(); row = c.execute("SELECT data FROM pending_seed WHERE name=?", (name,)).fetchone(); c.close()
            if row:
                try:
                    threading.Thread(target=_preseed, args=(ih, name, json.loads(row[0])), daemon=True).start()
                except Exception: pass
        return
    m = tmdb_match(name)
    cat = media_category(name, m)
    if m and m["conf"] in ("high", "mid"):
        try:
            do_organize(ih, name, files, m, cat)
        except Exception as e:
            logmsg("ERROR", f"入库异常 {name[:30]}: {e}")
            c = db(); c.execute("UPDATE media SET status='error' WHERE info_hash=?", (ih,)); c.commit(); c.close()
    else:
        hold_media(ih, name, cat, "识别置信度不足" if m else "TMDB无匹配")
    if transfer_to_tr(qb, ih, name, sp):
        c = db(); row = c.execute("SELECT data FROM pending_seed WHERE name=?", (name,)).fetchone(); c.close()
        if row:
            try:
                mates = json.loads(row[0])
                threading.Thread(target=_preseed, args=(ih, name, mates), daemon=True).start()
            except Exception: pass

def manual_organize(ih, query):
    """待确认条目：用户给 TMDB id 或片名，重新匹配并入库。数据可能已转到 tr。"""
    name = sp = None; files = []
    try:
        tr = TR()
        t = next((x for x in tr.torrents() if x["hashString"].lower() == ih.lower()), None)
        if t:
            name = t["name"]; sp = t["downloadDir"]
            files = [(os.path.join(sp, f["name"]), f["name"]) for f in t.get("files", [])]
    except Exception: pass
    if not files:
        try:
            qb = QB()
            t = next((x for x in qb.torrents() if x["hash"].lower() == ih.lower()), None)
            if t:
                name = t["name"]; sp = t["save_path"]
                files = [(os.path.join(sp, f["name"]), f["name"]) for f in qb.files(ih)]
        except Exception: pass
    if not files:
        return {"ok": False, "err": "qb/tr 里都找不到该种子的文件"}
    m = None
    if query.isdigit():   # 纯数字 = TMDB id，剧/影都试
        for mt in ("tv", "movie"):
            try:
                d = _tmdb_call(f"/{mt}/{query}", language="zh-CN")
                if d.get("id"):
                    m = {"mtype": mt, "id": d["id"], "tmdb_name": d.get("name") or d.get("title"),
                         "year": (d.get("first_air_date") or d.get("release_date") or "")[:4], "conf": "manual", "q": query}
                    break
            except Exception: continue
    else:                 # 否则当片名搜
        cand = _tmdb_search(query)
        if cand:
            r = cand[0]
            m = {"mtype": "tv" if r.get("media_type") == "tv" else "movie", "id": r.get("id"),
                 "tmdb_name": r.get("name") or r.get("title"), "year": _ryear(r), "conf": "manual", "q": query}
    if not m:
        return {"ok": False, "err": "TMDB 查不到，试试直接填 TMDB id"}
    cat = media_category(name or "", m)
    dest, n = do_organize(ih, name or "", files, m, cat)
    return {"ok": True, "name": f"{m['tmdb_name']} ({m['year']})", "n": n}

def _preseed(ih, name, mates):
    """下载时预存的同组候选辅种：不搜索,直接拿已知站点的种子来比对注入"""
    try:
        tr = TR(); t = None
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
        run_match(tr, t, [], pre_results=cands)
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
            if CFG["ORGANIZE"] and CFG["TMDB_KEY"]:
                qb = QB()
                for t in qb.torrents():
                    if t.get("progress", 0) < 1: continue
                    ih = t["hash"]
                    c = db(); row = c.execute("SELECT status FROM media WHERE info_hash=?", (ih,)).fetchone(); c.close()
                    if row: continue          # done/hold/error 都不重复自动处理，hold 走手动确认
                    logmsg("INFO", f"qb 下载完成，整理+转种: {t['name'][:44]}")
                    process_completed(qb, t)
        except Exception as e:
            logmsg("ERROR", f"qb监控异常: {e}")
        time.sleep(60)

# ============ Prowlarr ============
def prowlarr_search(query, cats=None):
    u = CFG["PROWLARR_URL"] + "/api/v1/search?query=" + urllib.parse.quote(query) + "&type=search"
    for c in (cats or []):          # Torznab 分类: 2000影 5000剧 5070动漫 7030漫画 3000音乐
        u += "&categories=" + str(c)
    req = urllib.request.Request(u, headers={"X-Api-Key":CFG["PROWLARR_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=150))
def prowlarr_download(url):
    req = urllib.request.Request(url, headers={"X-Api-Key":CFG["PROWLARR_KEY"]})
    return urllib.request.urlopen(req, timeout=25).read()

# ============ 核心：按给定关键词辅种一个种子 ============
def run_match(tr, t, queries, manual=False, pre_results=None):
    ih = t["hashString"]; name = t["name"]; total = t["totalSize"]
    top = name; rel = {}
    for f in t.get("files", []):
        fn = f["name"]
        rel[fn[len(top)+1:] if fn.startswith(top+"/") else fn] = f["length"]
    local_set = set(rel.items())
    trackers = [tk.get("announce","") for tk in t.get("trackers", [])]
    source = next((tracker_site(a) for a in trackers if tracker_site(a)), "") or "?"
    c = db()
    c.execute("INSERT OR IGNORE INTO torrents(info_hash,name,size,files,query,status,first_seen) VALUES(?,?,?,?,?,?,?)",
              (ih, name, total, len(rel), " / ".join(queries), "searching", int(time.time())))
    c.execute("UPDATE torrents SET query=?, status=?, matched=0, injected=0, source=?, last_searched=? WHERE info_hash=?", (" / ".join(queries), "searching", source, int(time.time()), ih))
    c.commit(); c.close()

    def process(results):
        m = inj = 0
        ban = [b.strip().lower() for b in CFG["TR_BAN_SITES"].split(",") if b.strip()]
        for r in results:
            if not r.get("downloadUrl") or abs(r.get("size",0)-total) >= total*CFG["SIZE_TOLERANCE"]:
                continue
            if any(b in (r.get("indexer") or "").lower() for b in ban):
                continue                      # 该站 ban 了 Transmission 客户端,注了也是废种
            time.sleep(CFG["SNATCH_DELAY"])
            try:
                data = prowlarr_download(r["downloadUrl"])
                if data[:1] != b'd': continue
                cname, cfiles = torrent_files(data)
                if set(cfiles.items()) != local_set: continue
                m += 1; same = (cname == top); mode = "direct" if same else "link"; res = "matched"
                # 同 info_hash(多站挂同一个种子文件)：绝不能 tr.add(tr4会用新tracker顶掉旧的断了原站做种)，
                # 直接给现有种子追加该站 tracker —— 一个种子同时向多站汇报(IYUU式)
                try: chash = torrent_infohash(data)
                except Exception: chash = ""
                if chash and chash.lower() == ih.lower():
                    try:
                        res = tr_add_trackers(tr, ih, torrent_announces(data))
                    except Exception as e:
                        logmsg("WARN", f"加tracker失败: {str(e)[:40]}"); res = "duplicate"
                    if res == "tracker": inj += 1
                    mode = "tracker"
                    c = db()
                    c.execute("INSERT INTO matches(info_hash,indexer,matched_name,mode,result,ts) VALUES(?,?,?,?,?,?)",
                              (ih, r.get("indexer"), cname[:120], mode, res, int(time.time())))
                    c.execute("UPDATE torrents SET status='injecting', matched=matched+1, injected=injected+? WHERE info_hash=?",
                              (1 if res == "tracker" else 0, ih))
                    c.commit(); c.close()
                    continue
                if same:
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
                    # 注意：tr 对"新增"和"内容已存在"都返回 result=success，靠 arguments 里的键区分
                    if "torrent-duplicate" in args:
                        # 同 info_hash(多站同一种子文件) → IYUU式: 给现有种子加新站tracker
                        res = "duplicate"; dup = args["torrent-duplicate"]
                        try:
                            if dup.get("id") is not None:
                                if tr_add_trackers(tr, dup["id"], torrent_announces(data)) == "tracker":
                                    inj += 1; res = "tracker"
                        except Exception as e:
                            logmsg("WARN", f"加tracker失败: {str(e)[:40]}")
                    elif "torrent-added" in args or rr == "success": inj += 1; res = "injected"
                    else: res = "inject_fail:"+str(rr)
                except Exception: res = "inject_err"
                c = db()
                c.execute("INSERT INTO matches(info_hash,indexer,matched_name,mode,result,ts) VALUES(?,?,?,?,?,?)",
                        (ih, r.get("indexer"), cname[:120], mode, res, int(time.time())))
                # 边辅边更新计数，仪表盘搜索途中就能看到实时进度(状态标"辅种中")
                c.execute("UPDATE torrents SET status='injecting', matched=matched+1, injected=injected+? WHERE info_hash=?",
                          (1 if res == "injected" else 0, ih))
                c.commit(); c.close()
            except Exception:
                continue
        return m, inj

    matched = injected = 0; had_result = False; used = ""
    if pre_results is not None:            # 下载时预存的同组候选，直接比对注入，不再搜索
        m, inj = process(pre_results)
        matched, injected, had_result, used = m, inj, True, "下载时预存"
        queries = []
    for q in queries:
        try:
            results = prowlarr_search(q); had_result = True
        except Exception as e:
            logmsg("WARN", f"搜索[{q}]失败 {name[:30]}: {e}"); continue
        m, inj = process(results)
        matched += m; injected += inj; used = q
        if matched > 0: break   # 这个关键词辅到了，不必再试下一个(如英文兜底)
    if not had_result:
        set_status(ih, "search_error"); logmsg("ERROR", f"搜索全失败 {name[:40]}"); return
    c = db(); c.execute("UPDATE torrents SET status=?, matched=?, injected=? WHERE info_hash=?",
              ("done" if matched else "no_match", matched, injected, ih)); c.commit(); c.close()
    logmsg("INFO", f"{'手动' if manual else ''}辅种 {name[:40]} | 命中[{used}] 匹配{matched} 注入{injected}")
    if injected > 0:
        notify(f"🌱 辅种 +{injected} 站", name[:56])

def cross_seed_one(tr, t):
    # 自动关键词：中文主搜 + 英文兜底
    name = t["name"]
    query = extract_query(name); query_en = extract_english(name)
    queries = [query] + ([query_en] if query_en and query_en.lower() != query.lower() else [])
    run_match(tr, t, queries)

def manual_research(info_hash, custom_query):
    # 手动兜底：用户指定关键词重搜一个种子
    try:
        tr = TR(); ts = tr.torrents()
        t = next((x for x in ts if x["hashString"] == info_hash), None)
        if not t:
            logmsg("WARN", f"手动重搜: 未找到种子 {info_hash[:12]}"); return
        logmsg("INFO", f"手动重搜 [{custom_query}] <- {t['name'][:40]}")
        run_match(tr, t, [custom_query.strip()], manual=True)
    except Exception as e:
        logmsg("ERROR", f"手动重搜异常: {e}")

def set_status(ih, st):
    c = db(); c.execute("UPDATE torrents SET status=? WHERE info_hash=?", (st, ih)); c.commit(); c.close()

# ============ 扫描循环 ============
def scanner():
    time.sleep(5)
    tr = TR()
    while True:
        try:
            torrents = tr.torrents()
            now = int(time.time())
            c = db()
            # 按“内容”(名字+大小)去重：辅种产生的副本和原种是同一内容，只处理一次，不重复辅种
            done_content = {(r[0], r[1]) for r in c.execute("SELECT name,size FROM torrents WHERE status IN ('done','no_match')").fetchall()}
            cooldown_content = {(r[0], r[1]) for r in c.execute("SELECT name,size FROM torrents WHERE last_searched > ?", (now-21600,)).fetchall()}
            c.close()
            todo = []; seen_content = set(done_content) | set(cooldown_content)
            for t in torrents:
                key = (t.get("name",""), t.get("totalSize",0))
                if key in seen_content: continue                        # 该内容已处理/冷却中/本轮已排入
                if t.get("totalSize",0) <= 0 or "cross-seed-links" in t.get("downloadDir",""): continue
                seen_content.add(key); todo.append(t)
            logmsg("INFO", f"扫描: tr共{len(torrents)}个副本, 去重后待辅种{len(todo)}个内容")
            for t in todo:
                try: cross_seed_one(tr, t)
                except Exception as e: logmsg("ERROR", f"辅种异常 {t.get('name','')[:30]}: {e}")
                time.sleep(8)  # 种子间隔，别打爆 Prowlarr
        except Exception as e:
            logmsg("ERROR", f"扫描异常: {e}")
        time.sleep(CFG["SCAN_INTERVAL"])

# ============ 网页仪表盘 ============
PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>PackSeed</title><style>
:root{--bg:#000;--card:#1c1c1e;--card2:#2c2c2e;--fg:#f5f5f7;--sub:#98989d;--acc:#0a84ff;--ok:#30d158;--warn:#ffd60a;--err:#ff453a;--line:#2c2c2e}
*{box-sizing:border-box}::selection{background:rgba(10,132,255,.35)}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC','Microsoft YaHei',sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1140px;margin:0 auto;padding:32px 28px}
h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0 0 2px}
.sub{color:var(--sub);font-size:13px;margin-bottom:24px;letter-spacing:.01em}
.tabs{display:inline-flex;background:var(--card);padding:4px;border-radius:12px;gap:2px;margin-bottom:24px}
.tabbtn{padding:7px 18px;border-radius:9px;font-size:13px;font-weight:500;color:var(--sub);text-decoration:none;transition:.18s;white-space:nowrap}
.tabbtn:hover{color:var(--fg)}
.tabbtn.on{background:#48484a;color:#fff;box-shadow:0 1px 4px rgba(0,0,0,.3)}
.tab{display:none}.tab.active{display:block;animation:fade .25s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.card{background:var(--card);border-radius:18px;padding:6px 0;margin-bottom:20px;overflow:hidden}
.card h2{font-size:15px;font-weight:600;margin:16px 20px 10px;letter-spacing:-.01em}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.stat{background:var(--card);border-radius:18px;padding:18px 20px}
.stat .n{font-size:30px;font-weight:700;letter-spacing:-.02em}.stat .l{color:var(--sub);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse}
th{color:var(--sub);font-weight:500;font-size:12px;text-align:left;padding:8px 20px;border-top:none}
td{text-align:left;padding:11px 20px;border-top:1px solid var(--line);font-size:13px}
tr:hover td{background:rgba(255,255,255,.025)}
.b{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:500}
.done{background:rgba(48,209,88,.16);color:var(--ok)}.nomatch{background:rgba(255,214,10,.14);color:var(--warn)}
.searching{background:rgba(10,132,255,.16);color:#64a8ff}.err{background:rgba(255,69,58,.15);color:var(--err)}
.name{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name a{color:var(--fg);text-decoration:none}.name a:hover{color:var(--acc)}
a{color:var(--acc);text-decoration:none}
.src{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;background:rgba(10,132,255,.13);color:#64a8ff;font-weight:500}
.mut{color:var(--sub)}.r{text-align:right}
.rs{display:flex;gap:6px}
.rs input{background:var(--card2);border:none;color:var(--fg);border-radius:8px;padding:6px 10px;font-size:12px;width:130px;outline:none}
.rs input:focus{box-shadow:0 0 0 2.5px rgba(10,132,255,.45)}
.rs button{background:var(--acc);color:#fff;border:0;border-radius:980px;padding:5px 13px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;transition:.15s}
.rs button:hover{filter:brightness(1.12)}
#toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(6px);background:rgba(44,44,46,.92);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);color:#fff;padding:11px 20px;border-radius:14px;font-size:13px;font-weight:500;opacity:0;transition:.3s cubic-bezier(.2,.8,.3,1);pointer-events:none;box-shadow:0 8px 30px rgba(0,0,0,.45)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.searchbar{display:flex;gap:10px;padding:2px 20px 12px}
.searchbar input{flex:1;background:var(--card2);border:none;color:var(--fg);border-radius:12px;padding:12px 16px;font-size:15px;outline:none;transition:.18s}
.searchbar input:focus{box-shadow:0 0 0 3px rgba(10,132,255,.45)}
.searchbar input::placeholder{color:#636366}
.searchbar button{background:var(--acc);color:#fff;border:0;border-radius:12px;padding:12px 26px;font-size:14px;font-weight:600;cursor:pointer;transition:.15s}
.searchbar button:hover{filter:brightness(1.12)}.searchbar button:active{transform:scale(.97)}
.sname{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dlbtn{background:var(--acc);color:#fff;border:0;border-radius:980px;padding:5px 16px;font-size:12px;font-weight:600;cursor:pointer;transition:.15s}
.dlbtn:hover{filter:brightness(1.12)}.dlbtn:disabled{opacity:.55;cursor:default}
.fbar{display:flex;gap:8px;padding:0 20px 14px;flex-wrap:wrap;align-items:center}
.fpill{padding:6px 16px;border-radius:980px;background:var(--card2);cursor:pointer;font-size:13px;font-weight:500;color:var(--sub);user-select:none;transition:.18s}
.fpill:hover{color:var(--fg)}
.fpill.on{background:var(--acc);color:#fff}
.wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(136px,1fr));gap:18px;padding:16px 20px}
.pcard{cursor:pointer;border-radius:14px;padding:0;transition:.22s cubic-bezier(.2,.8,.3,1)}
.pcard:hover{transform:translateY(-3px)}
.pcard.sel .pw,.pcard.sel .ph{box-shadow:0 0 0 3px var(--acc),0 10px 30px rgba(0,0,0,.5)}
.pcard .pw{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:12px;background:var(--card2);display:block;transition:.22s;box-shadow:0 4px 16px rgba(0,0,0,.35)}
.pcard:hover .pw{box-shadow:0 12px 32px rgba(0,0,0,.55)}
.pcard .ph{width:100%;aspect-ratio:2/3;border-radius:12px;background:var(--card2);display:flex;align-items:center;justify-content:center;font-size:36px;transition:.22s}
.pname{font-size:13px;font-weight:600;margin-top:9px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;letter-spacing:-.01em}
.pmeta{font-size:11px;color:var(--sub);margin-top:3px}
.pbar{height:5px;background:var(--card2);border-radius:3px;margin-top:8px;overflow:hidden}
.pbar i{display:block;height:100%;background:var(--acc);border-radius:3px;transition:width .5s ease}
.pbar i.full{background:var(--ok)}
.grpsec{border-top:1px solid var(--line);margin-top:6px}
.gt{font-size:15px;font-weight:600;margin-bottom:4px;letter-spacing:-.01em}
.dcard{display:flex;gap:16px;padding:14px 20px;border-bottom:1px solid var(--line);align-items:flex-start}
.dcard:last-child{border-bottom:none}
.dpos{width:58px;height:87px;object-fit:cover;border-radius:10px;background:var(--card2);flex-shrink:0;box-shadow:0 3px 10px rgba(0,0,0,.35)}
.dph{width:58px;height:87px;border-radius:10px;background:var(--card2);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:24px}
.dtt{font-size:14px;font-weight:600;letter-spacing:-.01em}.dtt .mut{font-weight:400;font-size:12px}
</style></head><body><div class=wrap>
<h1>🌱 PackSeed</h1><div class=sub>搜索下载 · 刮削入库 · 转种保种 · 全站辅种 —— 全自动</div>
<div class=tabs>
<a href="#search" class="tabbtn" data-t="search">🔍 搜索下载</a>
<a href="#dl" class="tabbtn" data-t="dl">⬇️ 下载管理</a>
<a href="#media" class="tabbtn" data-t="media">📥 整理入库</a>
<a href="#seed" class="tabbtn" data-t="seed">🌱 辅种</a>
<a href="#logs" class="tabbtn" data-t="logs">📋 日志</a>
</div>
<div id=tab-dl class=tab>
<div class=card><h2>⬇️ 下载中 <span class=mut style=font-weight:400>· qb 实时进度 · 4 秒刷新 · 下载完自动入库+转种,随后见「整理入库」</span></h2><div id=dlist style="padding:2px 16px 12px"><span class=mut>载入中…</span></div></div>
<div class=card><h2>最近完成的流水线</h2><div id=ddone></div></div>
</div>
<div id=tab-search class=tab>
<div class=card><h2>🔍 搜索下载 <span class=mut style=font-weight:400>· 选类型缩小范围 · 海报墙点选 · 一键下到 qb</span></h2>
<div class=searchbar><input id=q placeholder="输入片名，回车搜索" onkeydown="if(event.key=='Enter')doSearch()"><button onclick=doSearch()>搜索</button></div>
<div class=fbar><span class=mut style=font-size:12px>类型:</span>
<span class=fpill data-f=movie onclick=tgF(this)>🎬 电影</span>
<span class=fpill data-f=tv onclick=tgF(this)>📺 电视剧</span>
<span class=fpill data-f=anime onclick=tgF(this)>🎌 动漫</span>
<span class=fpill data-f=book onclick=tgF(this)>📖 漫画/书</span>
<span class=fpill data-f=music onclick=tgF(this)>🎵 音乐</span>
<span class=mut style=font-size:12px>· 搜完点着切换,不用重搜 · 不选=全部</span></div>
<div id=sresult></div></div>
</div>
<div id=tab-media class=tab>
<div class=card><h2>📥 整理入库 <span class=mut style=font-weight:400>· 下载完成自动识别→硬链接进 Emby 媒体库 · 待确认的可手动填 TMDB id/片名</span></h2><table><tr><th>下载名</th><th>分类</th><th>识别为</th><th>状态</th><th>目标/操作</th></tr>{{MEDIA}}</table></div>
</div>
<div id=tab-seed class=tab>
<div class=stats>
<div class=stat><div class=n>{{TOTAL}}</div><div class=l>已处理种子</div></div>
<div class=stat><div class=n style=color:var(--ok)>{{INJECT}}</div><div class=l>累计辅种注入</div></div>
<div class=stat><div class=n>{{DONE}}</div><div class=l>有匹配的种子</div></div>
<div class=stat><div class=n class=mut>{{NOMATCH}}</div><div class=l>无匹配</div></div>
</div>
<div class=card><h2>辅种记录 <span class=mut style=font-weight:400>· 每 {{INTERVAL}}s 扫描 · 点种子名看来源和去向 · 辅不上可手动关键词重搜</span></h2><table><tr><th>种子</th><th>来源</th><th>搜索词</th><th class=r>在辅站数</th><th>状态</th><th>手动辅种</th></tr>{{ROWS}}</table></div>
</div>
<div id=tab-logs class=tab>
<div class=card><h2>最近活动</h2><table><tr><th style=width:150px>时间</th><th>消息</th></tr>{{LOGS}}</table></div>
</div>
<div class=sub style=text-align:center>PackSeed · 一个人的 PT 全家桶 · MIT 开源</div>
</div><div id=toast></div>
<script>
var _dlT=null;
function showTab(t){
 document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));
 document.querySelectorAll('.tabbtn').forEach(e=>e.classList.remove('on'));
 var el=document.getElementById('tab-'+t);(el||document.getElementById('tab-search')).classList.add('active');
 var b=document.querySelector('.tabbtn[data-t="'+(el?t:'search')+'"]');if(b)b.classList.add('on');
 clearInterval(_dlT);
 if(t=='dl'){clearTimeout(_t);pollDl();_dlT=setInterval(pollDl,4000);}
}
var SM={downloading:'⬇️ 下载中',stalledDL:'🐢 等速度',metaDL:'🧲 元数据',forcedDL:'⬇️ 下载中',pausedDL:'⏸ 暂停',queuedDL:'⏳ 排队',allocating:'分配空间',uploading:'✅ 完成·待转种',stalledUP:'✅ 完成·待转种',queuedUP:'✅ 完成·待转种',forcedUP:'✅ 完成·待转种',checkingDL:'🔍 校验中',checkingUP:'🔍 校验中',checkingResumeData:'🔍 校验中',error:'❌ 错误',missingFiles:'❌ 文件缺失'};
var STM={done:['✅ 已入库+转种','done'],hold:['⚠️ 待确认(去整理入库页处理)','nomatch'],processing:['🔄 整理中','searching'],error:['❌ 出错','err']};
function pollDl(){
 fetch('/api/downloads').then(r=>r.json()).then(function(d){
  var el=document.getElementById('dlist');if(!el)return;
  var dl=d.dl||[];
  if(d.err){el.innerHTML='<span class=mut>qb 连接失败：'+d.err+'</span>';}
  else if(!dl.length){el.innerHTML='<span class=mut>qb 里暂无任务 —— 下载完成的会自动入库+转种到 tr,见下方记录</span>';}
  else{
   el.innerHTML='';
   dl.forEach(function(t){
    var row=document.createElement('div');row.className='dcard';
    if(t.poster){var im=document.createElement('img');im.className='dpos';im.loading='lazy';im.src='/api/poster?p='+encodeURIComponent(t.poster);row.appendChild(im);}
    else{var ph=document.createElement('div');ph.className='dph';ph.textContent='⬇️';row.appendChild(ph);}
    var col=document.createElement('div');col.style.cssText='flex:1;min-width:0';
    var tt=document.createElement('div');tt.className='dtt';
    tt.textContent=t.tmdb?(t.tmdb+(t.year?' ('+t.year+')':'')):t.name.slice(0,50);
    if(t.tmdb){var sm=document.createElement('span');sm.className='mut';sm.textContent='  '+t.name.slice(0,56);tt.appendChild(sm);}
    var pb=document.createElement('div');pb.className='pbar';var pi=document.createElement('i');
    pi.style.width=t.progress+'%';if(t.progress>=100)pi.className='full';pb.appendChild(pi);
    var i=document.createElement('div');i.className='mut';i.style.cssText='font-size:12px;margin-top:6px';
    i.textContent=(SM[t.state]||t.state)+' · '+t.sizeh+' · '+t.speed+(t.eta?' · 剩'+t.eta:'')+' · 做种'+t.seeds+' · '+t.progress+'%';
    col.appendChild(tt);col.appendChild(pb);col.appendChild(i);
    row.appendChild(col);
    var cx=document.createElement('button');cx.className='dlbtn';cx.style.cssText='background:transparent;border:1px solid var(--line);color:var(--sub);flex-shrink:0';
    cx.textContent='✕ 取消';
    cx.onclick=function(){
     if(!confirm('取消下载「'+(t.tmdb||t.name.slice(0,30))+'」? 将从 qb 移除任务并删除已下载的数据。'))return;
     cx.disabled=true;cx.textContent='取消中…';
     fetch('/api/canceldl?hash='+encodeURIComponent(t.hash)).then(r=>r.json()).then(function(d){
      if(d.ok){toast('已取消并清理');pollDl();}
      else{toast('取消失败：'+(d.err||''));cx.disabled=false;cx.textContent='✕ 取消';}
     }).catch(()=>{cx.disabled=false;cx.textContent='✕ 取消';});
    };
    row.appendChild(cx);el.appendChild(row);
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
showTab((location.hash||'#search').slice(1));
window.addEventListener('hashchange',function(){showTab(location.hash.slice(1)||'search');});
var _t=setTimeout(()=>location.reload(),15000);
function research(h,el){
 var inp=el.parentNode.querySelector('input');var q=inp.value.trim();
 if(!q){inp.focus();return;}
 clearTimeout(_t);el.disabled=true;el.textContent='搜索中';
 fetch('/research?hash='+encodeURIComponent(h)+'&q='+encodeURIComponent(q))
  .then(r=>r.json()).then(d=>{toast('已触发重搜 ['+q+']，稍后刷新查看');setTimeout(()=>location.reload(),8000);})
  .catch(e=>{toast('触发失败');el.disabled=false;el.textContent='重搜';});
}
function toast(m){var t=document.getElementById('toast');t.textContent=m;t.className='show';setTimeout(()=>t.className='',3000);}
function mkTable(rs){
 var tbl=document.createElement('table');
 var hd=document.createElement('tr');hd.innerHTML='<th>标题</th><th>站点</th><th class=r>大小</th><th class=r>做种</th><th></th>';tbl.appendChild(hd);
 rs.forEach(function(x){
  var tr=document.createElement('tr');
  var c1=document.createElement('td');c1.className='sname';c1.title=x.title+'（点击打开站点种子详情页）';
  if(x.info){var a=document.createElement('a');a.href=x.info;a.target='_blank';a.rel='noreferrer';a.textContent=x.title;a.style.color='var(--fg)';c1.appendChild(a);}
  else{c1.textContent=x.title;}
  var c2=document.createElement('td');var sp=document.createElement('span');sp.className='src';sp.textContent=x.site;c2.appendChild(sp);
  var c3=document.createElement('td');c3.className='r';c3.textContent=x.sizeh;
  var c4=document.createElement('td');c4.className='r';c4.textContent=x.seeders;
  var c5=document.createElement('td');var b=document.createElement('button');b.className='dlbtn';b.textContent='下载';b.onclick=function(){dl(b,x,rs);};c5.appendChild(b);
  tr.appendChild(c1);tr.appendChild(c2);tr.appendChild(c3);tr.appendChild(c4);tr.appendChild(c5);tbl.appendChild(tr);
 });
 return tbl;
}
var _sd=null;
function tgF(el){
 var was=el.classList.contains('on');
 document.querySelectorAll('.fpill').forEach(e=>e.classList.remove('on'));
 if(!was)el.classList.add('on');
 if(_sd)renderWall();
}
function activeF(){var e=document.querySelector('.fpill.on');return e?e.dataset.f:'';}
function renderWall(){
 var d=_sd,box=document.getElementById('sresult'),f=activeF();
 var gs=(d.groups||[]).filter(g=>!f||g.cat==f);
 var ot=(d.other||[]).filter(x=>!f||x.cat==f);
 box.innerHTML='';
 if(!gs.length&&!ot.length){box.innerHTML='<div class=mut style="padding:10px 16px">该类型下没有结果，点掉类型看全部</div>';return;}
 var wall=document.createElement('div');wall.className='wall';
 var sel=document.createElement('div');sel.id='selres';
 function pick(card,rs,label){
  document.querySelectorAll('.pcard').forEach(c=>c.classList.remove('sel'));
  card.classList.add('sel');sel.innerHTML='';
  var hd=document.createElement('div');hd.className='gt';hd.style.padding='6px 16px 0';
  hd.textContent=label+' — 选择站点下载';sel.appendChild(hd);
  sel.appendChild(mkTable(rs));
  sel.scrollIntoView({behavior:'smooth',block:'nearest'});
 }
 var CN={movie:'电影',tv:'剧集',anime:'动漫',music:'音乐'};
 gs.forEach(function(g){
  var card=document.createElement('div');card.className='pcard';
  if(g.posterurl){var im=document.createElement('img');im.className='pw';im.loading='lazy';im.src=g.posterurl;card.appendChild(im);}
  else if(g.poster){var im=document.createElement('img');im.className='pw';im.loading='lazy';im.src='/api/poster?p='+encodeURIComponent(g.poster);card.appendChild(im);}
  else{var ph=document.createElement('div');ph.className='ph';ph.textContent=g.cat=='music'?'🎵':(g.cat=='anime'?'🎌':(g.mtype=='tv'?'📺':'🎬'));card.appendChild(ph);}
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
}
function doSearch(){
 var q=document.getElementById('q').value.trim();if(!q)return;
 clearTimeout(_t);
 var box=document.getElementById('sresult');
 box.innerHTML='<div class=mut style="padding:10px 16px">正在提交搜索任务…</div>';
 fetch('/api/search2?q='+encodeURIComponent(q)).then(r=>r.json()).then(function(d){
  if(!d.ok){box.innerHTML='<div class=mut style="padding:10px 16px">提交失败：'+(d.err||'')+'</div>';return;}
  pollJob(d.id,box,Date.now());
 }).catch(e=>{box.innerHTML='<div class=mut style="padding:10px 16px">提交出错</div>';});
}
function pollJob(id,box,t0){
 fetch('/api/searchstat?id='+id).then(r=>r.json()).then(function(j){
  if(!j.ok){box.innerHTML='<div class=mut style="padding:10px 16px">'+(j.err||'任务丢失')+'</div>';return;}
  if(!j.done){
   var el=Math.round((Date.now()-t0)/1000);
   var wrap=document.createElement('div');wrap.style.cssText='padding:10px 16px;font-size:13px;line-height:1.8';
   var tm=document.createElement('div');tm.style.cssText='color:var(--acc);font-weight:600;margin-bottom:4px';
   tm.textContent='⏱ 搜索进行中 · 已用 '+el+' 秒';wrap.appendChild(tm);
   (j.log||[]).forEach(function(m,i){
    var ln=document.createElement('div');ln.style.color=(i==j.log.length-1)?'var(--fg)':'var(--sub)';
    ln.textContent=m;wrap.appendChild(ln);
   });
   box.innerHTML='';box.appendChild(wrap);
   setTimeout(function(){pollJob(id,box,t0);},1500);
   return;
  }
  var d=j.result||{};
  if(!d.ok){box.innerHTML='<div class=mut style="padding:10px 16px">搜索失败：'+(d.err||'')+'</div>';return;}
  if(!(d.groups||[]).length&&!(d.other||[]).length){box.innerHTML='<div class=mut style="padding:10px 16px">没搜到结果，换个关键词试试</div>';return;}
  _sd=d;renderWall();
 }).catch(function(){setTimeout(function(){pollJob(id,box,t0);},2500);});
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
function reid(h,el){
 var inp=el.parentNode.querySelector('input');var v=inp.value.trim();
 if(!v){inp.focus();return;}
 clearTimeout(_t);el.disabled=true;el.textContent='入库中…';
 fetch('/api/reid?hash='+encodeURIComponent(h)+'&q='+encodeURIComponent(v))
  .then(r=>r.json()).then(d=>{if(d.ok){toast('已入库：'+(d.name||''));setTimeout(()=>location.reload(),1500);}
   else{toast('失败：'+(d.err||''));el.disabled=false;el.textContent='确认入库';}})
  .catch(e=>{toast('出错');el.disabled=false;el.textContent='确认入库';});
}
</script></body></html>"""

DETAIL = """<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>种子详情 · PackSeed</title><style>
:root{--bg:#000;--card:#1c1c1e;--card2:#2c2c2e;--fg:#f5f5f7;--sub:#98989d;--acc:#0a84ff;--ok:#30d158;--warn:#ffd60a;--line:#2c2c2e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC','Microsoft YaHei',sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:840px;margin:0 auto;padding:32px 28px}a{color:var(--acc);text-decoration:none}
.back{font-size:13px;font-weight:500}.title{font-size:19px;font-weight:700;margin:14px 0 4px;word-break:break-all;letter-spacing:-.02em}
.card{background:var(--card);border-radius:18px;padding:18px 20px;margin:16px 0}
.kv{display:grid;grid-template-columns:92px 1fr;gap:8px 16px;font-size:14px}.kv .k{color:var(--sub)}
.src{display:inline-block;padding:2px 11px;border-radius:20px;font-size:13px;background:rgba(10,132,255,.13);color:#64a8ff;font-weight:500}
.big{display:inline-block;padding:2px 13px;border-radius:20px;font-size:15px;font-weight:700;background:rgba(48,209,88,.16);color:var(--ok)}
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
<div class=mut style=text-align:center;font-size:12px>PackSeed · 自制辅种</div>
</div></body></html>"""

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

def search_group(q, results, log=lambda m: None):
    """Prowlarr 结果 → 做种过滤 + TMDB 识别分组。log 回调用于搜索过程直播。"""
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
        out.append({"title": r.get("title",""), "site": r.get("indexer",""),
                    "sizeh": human_size(r.get("size",0)), "size": r.get("size",0), "seeders": r.get("seeders") or 0,
                    "url": url, "cat": catlab(r), "info": r.get("infoUrl") or ""})
    out.sort(key=lambda x: x["seeders"], reverse=True)
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
    keys = {}
    for x in out:
        k = extract_query(x["title"]).lower()
        x["k"] = k
        info = keys.setdefault(k, {"rep": x["title"], "n": 0})
        info["n"] += 1
    matched = {}
    todo = [(k, i) for k, i in sorted(keys.items(), key=lambda kv: -kv[1]["n"])[:12] if k]
    for idx, (k, info) in enumerate(todo):
        try: m = tmdb_match(info["rep"])
        except Exception: m = None
        if m and m["conf"] != "low":
            matched[k] = m
            log(f"🔎 识别 {idx+1}/{len(todo)}: {info['rep'][:36]} → {m['tmdb_name']} ({m['year']})")
        else:
            log(f"🧩 识别 {idx+1}/{len(todo)}: {info['rep'][:36]} → 未识别,归入其他")
    groups = {}; other = []
    for x in out:
        m = matched.get(x.pop("k"))
        if m:
            gk = (m["mtype"], m["id"])
            g = groups.setdefault(gk, {"name": m["tmdb_name"], "year": m["year"], "mtype": m["mtype"],
                                       "cat": "anime" if m.get("anime") else m["mtype"],
                                       "poster": m.get("poster",""), "overview": (m.get("overview") or "")[:110],
                                       "results": []})
            g["results"].append(x)
        else:
            other.append(x)
    def seed_filter(rs):
        good = [x for x in rs if x["seeders"] >= CFG["MIN_SEEDERS"]]
        if good: return good
        return rs[:max(1, round(len(rs) * 0.2))]
    allg = list(groups.values()) + list(mgroups.values())
    for g in allg:
        g["results"] = seed_filter(g["results"])
    if other: other = seed_filter(other)
    glist = sorted(allg, key=lambda g: -(g["results"][0]["seeders"] if g["results"] else 0))
    return {"ok": True, "groups": glist, "other": other}

def prowlarr_indexers():
    req = urllib.request.Request(CFG["PROWLARR_URL"] + "/api/v1/indexer", headers={"X-Api-Key": CFG["PROWLARR_KEY"]})
    return [i for i in json.load(urllib.request.urlopen(req, timeout=15)) if i.get("enable")]

def prowlarr_search_fan(query, log=lambda m: None, per_timeout=25):
    """MP式分站并发：每站独立请求+单站超时，快站先回、慢站丢弃，不再等最慢的站。"""
    try:
        idx = prowlarr_indexers()
        if not idx: raise RuntimeError("无可用站点")
    except Exception as e:
        log(f"⚠️ 取站点列表失败({str(e)[:30]})，退回聚合搜索"); return prowlarr_search(query)
    from concurrent.futures import ThreadPoolExecutor
    results = []; lock = threading.Lock(); done = [0]; ok = [0]
    def one(ix):
        u = (CFG["PROWLARR_URL"] + "/api/v1/search?query=" + urllib.parse.quote(query)
             + "&type=search&indexerIds=" + str(ix["id"]))
        req = urllib.request.Request(u, headers={"X-Api-Key": CFG["PROWLARR_KEY"]})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=per_timeout))
            with lock:
                done[0] += 1
                if r:
                    ok[0] += 1; results.extend(r)
                    log(f"  ✓ {ix.get('name','?')} 返回 {len(r)} 条 · 进度 {done[0]}/{len(idx)}")
        except Exception:
            with lock:
                done[0] += 1
                log(f"  ✗ {ix.get('name','?')} 超时/失败，跳过 · 进度 {done[0]}/{len(idx)}")
    with ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(one, idx))
    log(f"📦 {ok[0]}/{len(idx)} 个站点有结果，共 {len(results)} 条")
    return results

_SJOBS = {}
def _sjob_run(jid, q):
    job = _SJOBS[jid]
    def log(m): job["log"].append(m)
    try:
        log(f"🚀 已提交「{q}」→ 分站并发搜索(单站超时25秒,慢站直接跳过)…")
        t0 = time.time()
        results = prowlarr_search_fan(q, log)
        log(f"⏱ 搜索耗时 {int(time.time()-t0)} 秒。做种数过滤 + TMDB 识别配图…")
        job["result"] = search_group(q, results, log)
        log("✅ 完成")
    except Exception as e:
        job["result"] = {"ok": False, "err": str(e)[:80]}
        log(f"❌ 失败: {str(e)[:60]}")
    job["done"] = True

class Handler(BaseHTTPRequestHandler):
    def log_message(s, *a): pass
    def _auth_ok(s):
        u, p = CFG["AUTH_USER"], CFG["AUTH_PASS"]
        if not u:  # 未配置账号密码 = 不启用登录
            return True
        hdr = s.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                dec = base64.b64decode(hdr[6:]).decode("utf-8", "ignore")
                gu, gp = dec.split(":", 1)
                if gu == u and gp == p:
                    return True
            except Exception:
                pass
        s.send_response(401)
        s.send_header("WWW-Authenticate", 'Basic realm="PackSeed"')
        s.send_header("Content-Type", "text/plain; charset=utf-8")
        s.end_headers()
        s.wfile.write("需要登录".encode())
        return False
    def do_POST(s):
        if s.path.startswith("/api/wecom"):
            s._wecom_post(); return
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
        s.send_response(404); s.end_headers()
    def do_GET(s):
        if s.path.startswith("/api/wecom"):
            s._wecom_get(); return
        if s.path.startswith("/api/poster"):
            s._poster(); return          # 公开海报,免登录(图文通知的图要外网可达)
        if not s._auth_ok():
            return
        if s.path.startswith("/research"):
            s._research(); return
        if s.path.startswith("/torrent"):
            s._detail(); return
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
            manual = f"<div class=rs><input placeholder='自定义关键词' value=''><button onclick=\"research('{esc(r[5])}',this)\">重搜</button></div>"
            # 在辅站数=该内容实际在多少个站做种(注入/已存在/加tracker都算,不管是谁辅上的)
            seeded = c.execute("SELECT COUNT(DISTINCT indexer) FROM matches WHERE info_hash=? AND result IN ('injected','duplicate','tracker')", (r[5],)).fetchone()[0]
            rows += (f"<tr><td class=name title='{esc(r[0])}'><a href='/torrent?hash={esc(r[5])}'>{esc(r[0])}</a></td>"
                     f"<td><span class=src>{esc(r[6] or '?')}</span></td><td class=mut>{esc(r[1])}</td>"
                     f"<td class='r' style='color:var(--ok);font-weight:700'>{seeded}</td>"
                     f"<td><span class='b {st}'>{label}</span></td><td>{manual}</td></tr>")
        # 整理入库记录
        media_rows = ""
        cmap = {"电影":"🎬","电视剧":"📺","动漫":"🎌"}
        smap = {"done":("已入库","done"),"hold":("待确认","nomatch"),"processing":("处理中","searching"),"error":("出错","err"),"skip":("跳过","nomatch")}
        for r in c.execute("SELECT info_hash,name,cat,tmdb_name,year,status,target FROM media ORDER BY ts DESC LIMIT 60").fetchall():
            ih,nm,cat,tn,yr,stt,tgt = r
            lbl,cls = smap.get(stt,(stt,"err"))
            if stt == "hold":
                fix = f"<div class=rs><input placeholder='TMDB id 或 片名' value=''><button onclick=\"reid('{esc(ih)}',this)\">确认入库</button></div>"
            elif stt == "done":
                fix = f"<span class=mut title='{esc(tgt or '')}'>{esc((tgt or '').rsplit('/',1)[-1])}</span>"
            else: fix = ""
            media_rows += (f"<tr><td class=name title='{esc(nm)}'>{esc(nm)}</td>"
                           f"<td>{cmap.get(cat,'')}{esc(cat or '')}</td>"
                           f"<td>{esc((tn+' ('+(yr or '')+')') if tn else '—')}</td>"
                           f"<td><span class='b {cls}'>{esc(lbl)}</span></td><td>{fix}</td></tr>")
        logs = ""
        for r in c.execute("SELECT ts,level,msg FROM log ORDER BY id DESC LIMIT 40").fetchall():
            logs += f"<tr><td class=mut>{time.strftime('%m-%d %H:%M:%S', time.localtime(r[0]))}</td><td>{esc(r[2])}</td></tr>"
        c.close()
        html = (PAGE.replace("{{INTERVAL}}", str(CFG["SCAN_INTERVAL"]))
                    .replace("{{TOTAL}}", str(t_total)).replace("{{INJECT}}", str(t_inject))
                    .replace("{{DONE}}", str(t_done)).replace("{{NOMATCH}}", str(t_nomatch))
                    .replace("{{ROWS}}", rows or "<tr><td colspan=6 class=mut>暂无记录，等待首次扫描…</td></tr>")
                    .replace("{{MEDIA}}", media_rows or "<tr><td colspan=5 class=mut>暂无入库记录</td></tr>")
                    .replace("{{LOGS}}", logs or "<tr><td colspan=2 class=mut>—</td></tr>"))
        b = html.encode("utf-8")
        s.send_response(200); s.send_header("Content-Type","text/html; charset=utf-8"); s.send_header("Content-Length",str(len(b))); s.end_headers(); s.wfile.write(b)
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
        try:
            results = prowlarr_search(q, cats)
        except Exception as e:
            logmsg("WARN", f"搜索下载查询失败[{q}]: {e}"); s._send_json({"ok":False,"err":str(e)[:80]}); return
        s._send_json(search_group(q, results))
    def _search2(s):
        from urllib.parse import urlparse, parse_qs
        q = (parse_qs(urlparse(s.path).query).get("q",[""])[0]).strip()
        if not q: s._send_json({"ok":False,"err":"关键词为空"}); return
        jid = str(int(time.time()*1000))
        _SJOBS[jid] = {"log": [], "done": False, "result": None, "ts": time.time()}
        threading.Thread(target=_sjob_run, args=(jid, q), daemon=True).start()
        for k in [k for k, v in list(_SJOBS.items()) if time.time()-v["ts"] > 600 and k != jid]:
            _SJOBS.pop(k, None)
        s._send_json({"ok":True,"id":jid})
    def _searchstat(s):
        from urllib.parse import urlparse, parse_qs
        jid = (parse_qs(urlparse(s.path).query).get("id",[""])[0]).strip()
        j = _SJOBS.get(jid)
        if not j: s._send_json({"ok":False,"err":"任务不存在或已过期"}); return
        s._send_json({"ok":True,"log":j["log"],"done":j["done"],"result":(j["result"] if j["done"] else None)})
    def _canceldl(s):
        from urllib.parse import urlparse, parse_qs
        h = (parse_qs(urlparse(s.path).query).get("hash",[""])[0]).strip()
        if not h: s._send_json({"ok":False,"err":"缺hash"}); return
        try:
            qb = QB()
            t = next((x for x in qb.torrents() if x.get("hash")==h), None)
            qb.delete(h, delete_files=True)   # 删任务+已下数据(取消=不要了)
            _DLMETA.pop(h, None)
            logmsg("INFO", f"用户取消下载: {(t or {}).get('name','')[:44]}")
            s._send_json({"ok":True})
        except Exception as e:
            s._send_json({"ok":False,"err":str(e)[:60]})
    def _downloads(s):
        out = {"dl": [], "done": []}
        try:
            for t in QB().torrents():
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
            res = QB().add(data, category=cat, tags="packseed")
            ok = "Ok" in res
            logmsg("INFO", f"搜索下载 → qb[{cat}]: {(cname or u)[:40]} [{res.strip()[:16] or 'ok'}]")
            s._send_json({"ok":ok, "err":"" if ok else (res[:60] or "qb 拒绝")})
        except Exception as e:
            logmsg("ERROR", f"搜索下载失败: {e}"); s._send_json({"ok":False,"err":str(e)[:80]})

def esc(t): return (t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("'","&#39;")

def main():
    init_db()
    org = "开" if CFG["ORGANIZE"] and CFG["TMDB_KEY"] else "关"
    logmsg("INFO", f"PackSeed 启动，监听 {CFG['PORT']}，扫描间隔 {CFG['SCAN_INTERVAL']}s，整理入库[{org}]")
    if CFG["WECOM_TOKEN"] and CFG["WECOM_AESKEY"]:
        logmsg("INFO", f"企微双向交互就绪(AES自检{'✅' if aes_selftest() else '❌失败!'}),回调: /api/wecom")
    threading.Thread(target=scanner, daemon=True).start()
    threading.Thread(target=qb_watcher, daemon=True).start()
    threading.Thread(target=notify_worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", CFG["PORT"]), Handler).serve_forever()

if __name__ == "__main__":
    main()
