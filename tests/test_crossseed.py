# -*- coding: utf-8 -*-
"""辅种覆盖驱动的真验证:用真 bencode 种子,只 mock 网络边界。"""
import os, sys, tempfile, importlib.util
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
DB = tempfile.mktemp(suffix=".db"); os.environ["DB_PATH"] = DB
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CFG.update(DB=DB, SNATCH_DELAY=0, TR_BAN_SITES="U2Ban", DATA_LINK_DIR=tempfile.mkdtemp())
m.init_db()
fail = []
def ck(c, msg):
    print(("  ✓ " if c else "  ✗ ") + msg)
    if not c: fail.append(msg)

TOP = "大明王朝1566.1080p"
FILES = [("E01.mkv", 1000), ("E02.mkv", 2000)]
def mk_torrent(announce):
    info = {b'name': TOP.encode(), b'piece length': 262144, b'pieces': b'x'*20,
            b'files': [{b'path': [p.encode()], b'length': sz} for p, sz in FILES]}
    return m.bencode({b'announce': announce.encode(), b'info': info})

TORRENT = mk_torrent("http://tracker.ttg.com/announce")
tr_t = {"hashString": "a1"*20, "name": TOP, "totalSize": 3000, "downloadDir": "/data/dl",
        "trackers": [{"announce": "http://tracker.ttg.com/announce"}],
        "files": [{"name": TOP+"/"+p, "length": sz} for p, sz in FILES], "percentDone": 1}

SITES = [{"id":1,"name":"TTG","indexerUrls":["https://totheglory.im/"]},
         {"id":2,"name":"馒头","indexerUrls":["https://kp.m-team.cc/"]},
         {"id":3,"name":"观众","indexerUrls":["https://audiences.me/"]},
         {"id":4,"name":"CHDBits","indexerUrls":["https://ptchdbits.co/"]},
         {"id":5,"name":"U2","indexerUrls":["https://u2.dmhy.org/"]},
         {"id":6,"name":"U2Ban","indexerUrls":["https://banned.example/"]}]
HAS = {"馒头", "U2"}          # 这两个站真有货
DEAD = {"CHDBits"}            # 这个站超时
asked = []                    # 记录每轮问了哪些站

class FakeTR:
    def add(s, data, d): return {"result":"success","arguments":{"torrent-added":{"id":7}}}
    def call(s, *a, **k): return {"result":"success","arguments":{}}
m.prowlarr_indexers = lambda: SITES
m.prowlarr_download = lambda url: TORRENT
def fake_fan(queries, log=None, per_timeout=None, deadline=None, cats=None,
             only=None, only_ids=None, status=None, workers=0):
    names = {i["name"] for i in SITES if i["id"] in set(only_ids or [])}
    asked.append(sorted(names))
    out = []
    for n in sorted(names):
        if n in DEAD:
            if status is not None: status[n] = "error"
            continue
        if status is not None: status[n] = "ok"
        if n in HAS:
            out.append({"indexer": n, "title": TOP, "size": 3000, "downloadUrl": "http://x/"+n})
    return out
m.prowlarr_search_fan = fake_fan
m.notify = lambda *a, **k: None

print("— 第 1 轮:全新内容,该问所有非来源站 —")
matched, injected = m.crossseed_one(FakeTR(), tr_t)
cid = m.content_id(m.manifest_tr(tr_t))
cov = {k: v[0] for k, v in m.led_cov_get(cid).items()}
ck(asked[0] == ["CHDBits","U2","观众","馒头"], "问了 4 个站(TTG是来源不问、U2Ban被ban不问): %s" % asked[0])
ck(m.match_site("ttg", [i["name"] for i in SITES]) == "TTG", "大小写不同也认得出同一个站")
_um, _nm = m.site_urlmap()
ck(m.match_site("audiences", _nm, _um) == "观众", "站名和域名完全不像时,靠 Prowlarr 的 indexerUrls 反查")
ck(m.match_site("m-team", _nm, _um) == "馒头", "kp.m-team.cc → 馒头")
ck(cov.get("TTG") == "source", "来源站记 source")
ck(cov.get("U2Ban") == "banned", "ban 站记 banned,永不占 pending 位")
ck(cov.get("馒头") == "seeding" and cov.get("U2") == "seeding", "两个有货的站都认领了")
ck(cov.get("观众") == "absent", "问过没货 → absent")
ck(cov.get("CHDBits") == "error", "没问成 → error(不是 absent!)")
print("     ↳ 这一条就是老版的病:老版辅到「馒头」立刻 break,U2 永远不会被问,而且没人知道漏了它")
ck(matched == 2, "认领了 2 个站的种子(老版只会认领 1 个) matched=%d" % matched)

print("— 第 2 轮:同一份内容再跑,只该问账上还欠的 —")
m.crossseed_one(FakeTR(), tr_t)
ck(asked[1] == ["CHDBits"], "只重问出错的站,不再骚扰已确认的: %s" % asked[1])

print("— 3. absent 过期后自动回到待办 —")
m._led("UPDATE coverage SET ts=1 WHERE cid=? AND site='观众'", (cid,))
m.crossseed_one(FakeTR(), tr_t)
ck("观众" in asked[2], "absent 过保质期 → 重新问一次: %s" % asked[2])

print("— 4. 全覆盖后彻底静默 —")
m.led_cov_set(cid, "CHDBits", "absent"); m.led_cov_set(cid, "观众", "absent")
n0 = len(asked); m.crossseed_one(FakeTR(), tr_t)
ck(len(asked) == n0, "各站都问过了 → 一个请求都不发(老版每 6 小时全量重来一次)")

print("— 5. 账本三个维度是正交的 —")
m.led_role(cid, "library"); m.led_place(cid, "tr")
g = m.led_get(cid)
ck(g["role"]=="library" and g["place"]=="tr" and m.led_cov_get(cid).get("馒头")[0]=="seeding",
   "角色/位置/覆盖 三者互不干扰(老版全挤在一个 status 字段里)")

os.unlink(DB)
print("\n" + ("❌ 失败 %d 项: %s" % (len(fail), fail) if fail else "✅ 全部通过"))
sys.exit(1 if fail else 0)
