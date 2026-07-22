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
    c.execute("""CREATE TABLE IF NOT EXISTS log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, msg TEXT)""")
    c.commit(); c.close()

def logmsg(level, msg):
    try:
        c = db(); c.execute("INSERT INTO log(ts,level,msg) VALUES(?,?,?)", (int(time.time()), level, msg[:500])); c.commit(); c.close()
    except: pass
    print(f"[{level}] {msg}", flush=True)

# ============ bencode ============
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
        r = s.call("torrent-get", {"fields":["hashString","name","totalSize","files","downloadDir","trackers"]})
        return r.get("arguments", {}).get("torrents", [])
    def add(s, torrent_bytes, download_dir):
        return s.call("torrent-add", {"metainfo":base64.b64encode(torrent_bytes).decode(),"download-dir":download_dir,"paused":False})

# ============ Prowlarr ============
def prowlarr_search(query):
    u = CFG["PROWLARR_URL"] + "/api/v1/search?query=" + urllib.parse.quote(query) + "&type=search"
    req = urllib.request.Request(u, headers={"X-Api-Key":CFG["PROWLARR_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=150))
def prowlarr_download(url):
    req = urllib.request.Request(url, headers={"X-Api-Key":CFG["PROWLARR_KEY"]})
    return urllib.request.urlopen(req, timeout=25).read()

# ============ 核心：按给定关键词辅种一个种子 ============
def run_match(tr, t, queries, manual=False):
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
        for r in results:
            if not r.get("downloadUrl") or abs(r.get("size",0)-total) >= total*CFG["SIZE_TOLERANCE"]:
                continue
            time.sleep(CFG["SNATCH_DELAY"])
            try:
                data = prowlarr_download(r["downloadUrl"])
                if data[:1] != b'd': continue
                cname, cfiles = torrent_files(data)
                if set(cfiles.items()) != local_set: continue
                m += 1; same = (cname == top); mode = "direct" if same else "link"; res = "matched"
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
                    resp = tr.add(data, dl_dir); rr = resp.get("result")
                    if rr == "success": inj += 1; res = "injected"
                    elif "torrent-duplicate" in resp.get("arguments", {}): res = "duplicate"
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
<title>PackSeed 辅种</title><style>
:root{--bg:#0f1117;--card:#1a1d27;--fg:#e6e8ee;--sub:#8b90a0;--acc:#7c5cff;--ok:#2eb872;--warn:#e0a03e;--err:#e05353;--line:#2a2e3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--sub);margin-bottom:20px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.stat .n{font-size:28px;font-weight:700}.stat .l{color:var(--sub);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px 0;margin-bottom:20px;overflow:hidden}
.card h2{font-size:15px;margin:14px 16px 8px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 16px;border-top:1px solid var(--line);font-size:13px}
th{color:var(--sub);font-weight:600;border-top:none}
.b{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px}
.done{background:rgba(46,184,114,.15);color:var(--ok)}.nomatch{background:rgba(224,160,62,.15);color:var(--warn)}
.searching{background:rgba(124,92,255,.15);color:var(--acc)}.err{background:rgba(224,83,83,.15);color:var(--err)}
.name{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name a{color:var(--fg);text-decoration:none}.name a:hover{color:var(--acc)}
a{color:var(--acc)}
.src{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;background:rgba(124,92,255,.13);color:#b3a4ff}
.mut{color:var(--sub)}.r{text-align:right}
.rs{display:flex;gap:6px}.rs input{background:#0f1117;border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:4px 8px;font-size:12px;width:120px}
.rs button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:4px 10px;font-size:12px;cursor:pointer;white-space:nowrap}
.rs button:hover{opacity:.85}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--acc);color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;opacity:0;transition:.3s;pointer-events:none}
#toast.show{opacity:1}
</style></head><body><div class=wrap>
<h1>🌱 PackSeed 辅种</h1><div class=sub>按文件清单精确比对辅种 · 每 {{INTERVAL}} 秒扫描一次 · 自动刷新 · 辅不上的可手动填关键词重搜</div>
<div class=stats>
<div class=stat><div class=n>{{TOTAL}}</div><div class=l>已处理种子</div></div>
<div class=stat><div class=n style=color:var(--ok)>{{INJECT}}</div><div class=l>累计辅种注入</div></div>
<div class=stat><div class=n>{{DONE}}</div><div class=l>有匹配的种子</div></div>
<div class=stat><div class=n class=mut>{{NOMATCH}}</div><div class=l>无匹配</div></div>
</div>
<div class=card><h2>种子辅种记录 <span class=mut style=font-weight:400>· 点种子名看来源站和辅种去向</span></h2><table><tr><th>种子</th><th>来源</th><th>搜索词</th><th class=r>匹配</th><th class=r>注入</th><th>状态</th><th>手动辅种</th></tr>{{ROWS}}</table></div>
<div class=card><h2>最近活动</h2><table><tr><th style=width:150px>时间</th><th>消息</th></tr>{{LOGS}}</table></div>
<div class=sub style=text-align:center>PackSeed · 自制辅种 · 替代 cross-seed</div>
</div><div id=toast></div>
<script>
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
</script></body></html>"""

DETAIL = """<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>种子详情 · PackSeed</title><style>
:root{--bg:#0f1117;--card:#1a1d27;--fg:#e6e8ee;--sub:#8b90a0;--acc:#7c5cff;--ok:#2eb872;--warn:#e0a03e;--line:#2a2e3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:24px}a{color:var(--acc);text-decoration:none}
.back{font-size:13px}.title{font-size:18px;font-weight:700;margin:12px 0 4px;word-break:break-all}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:16px 0}
.kv{display:grid;grid-template-columns:90px 1fr;gap:6px 14px;font-size:14px}.kv .k{color:var(--sub)}
.src{display:inline-block;padding:1px 9px;border-radius:20px;font-size:13px;background:rgba(124,92,255,.15);color:#b3a4ff}
.big{display:inline-block;padding:2px 12px;border-radius:20px;font-size:15px;font-weight:700;background:rgba(46,184,114,.16);color:var(--ok)}
h2{font-size:15px;margin:0 0 4px}.card h2{margin-bottom:12px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-top:1px solid var(--line);font-size:13px}
th{color:var(--sub);font-weight:600;border-top:none}.mut{color:var(--sub)}
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
    def do_GET(s):
        if not s._auth_ok():
            return
        if s.path.startswith("/research"):
            s._research(); return
        if s.path.startswith("/torrent"):
            s._detail(); return
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
            rows += (f"<tr><td class=name title='{esc(r[0])}'><a href='/torrent?hash={esc(r[5])}'>{esc(r[0])}</a></td>"
                     f"<td><span class=src>{esc(r[6] or '?')}</span></td><td class=mut>{esc(r[1])}</td>"
                     f"<td class=r>{r[2]}</td><td class='r' style='color:var(--ok)'>{r[3]}</td>"
                     f"<td><span class='b {st}'>{label}</span></td><td>{manual}</td></tr>")
        logs = ""
        for r in c.execute("SELECT ts,level,msg FROM log ORDER BY id DESC LIMIT 40").fetchall():
            logs += f"<tr><td class=mut>{time.strftime('%m-%d %H:%M:%S', time.localtime(r[0]))}</td><td>{esc(r[2])}</td></tr>"
        c.close()
        html = (PAGE.replace("{{INTERVAL}}", str(CFG["SCAN_INTERVAL"]))
                    .replace("{{TOTAL}}", str(t_total)).replace("{{INJECT}}", str(t_inject))
                    .replace("{{DONE}}", str(t_done)).replace("{{NOMATCH}}", str(t_nomatch))
                    .replace("{{ROWS}}", rows or "<tr><td colspan=7 class=mut>暂无记录，等待首次扫描…</td></tr>")
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
        ms = c.execute("SELECT indexer,matched_name,mode,result,ts FROM matches WHERE info_hash=? ORDER BY id", (h,)).fetchall()
        c.close()
        mrows = ""
        rmap = {"injected":"✅ 已注入做种","duplicate":"⚠️ 已存在","matched":"匹配","inject_err":"注入出错"}
        for m in ms:
            rr = rmap.get(m[3], m[3]); col = "var(--ok)" if m[3]=="injected" else ("var(--warn)" if m[3]=="duplicate" else "var(--sub)")
            mrows += f"<tr><td><span class=src>{esc(m[0])}</span></td><td class=mut>{'硬链接' if m[2]=='link' else '同名直注'}</td><td style='color:{col}'>{rr}</td></tr>"
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

def esc(t): return (t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("'","&#39;")

def main():
    init_db()
    logmsg("INFO", f"PackSeed 启动，监听 {CFG['PORT']}，扫描间隔 {CFG['SCAN_INTERVAL']}s")
    threading.Thread(target=scanner, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", CFG["PORT"]), Handler).serve_forever()

if __name__ == "__main__":
    main()
