# -*- coding: utf-8 -*-
"""保种清退护栏的真验证。删数据不可逆,护栏必须能挡住每一种绕过尝试。"""
import os, sys, tempfile, importlib.util
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
DB = tempfile.mktemp(suffix=".db"); os.environ["DB_PATH"] = DB
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CFG.update(DB=DB, KEEP_DIR="/data/downloads/keepseed"); m.init_db()
fail = []
def ck(c, msg):
    print(("  ✓ " if c else "  ✗ ") + msg)
    if not c: fail.append(msg)

print("— 路径边界:startswith 的经典陷阱 —")
ck(m._under("/data/downloads/keepseed/x", "/data/downloads/keepseed"), "目录内 → 真")
ck(m._under("/data/downloads/keepseed", "/data/downloads/keepseed"), "目录本身 → 真")
ck(not m._under("/data/downloads/keepseed-old/x", "/data/downloads/keepseed"),
   "**/keepseed-old 不算在 /keepseed 之下**(裸 startswith 会误判成真)")
ck(not m._under("/data/media/tv/x", "/data/downloads/keepseed"), "媒体库 → 假")
ck(not m._under("/data/x", ""), "root 为空 → 一律假,不敢乱删")

removed = []
class FakeTR:
    def call(s, meth, args):
        if meth == "torrent-get":
            return {"arguments": {"torrents": [
                {"hashString":"aa"*20,"name":"库存种","totalSize":10**10,"downloadDir":"/data/downloads/keepseed"},
                {"hashString":"bb"*20,"name":"媒体库的剧","totalSize":10**10,"downloadDir":"/data/media/tv"},
                {"hashString":"cc"*20,"name":"相似目录","totalSize":10**10,"downloadDir":"/data/downloads/keepseed-old"},
            ]}}
        if meth == "torrent-remove":
            removed.extend(args["ids"]); return {}
        return {}
m.tr_conn = lambda: FakeTR()

print("— 清退护栏 —")
r = m.stock_evict(["aa"*20, "bb"*20, "cc"*20, "ff"*20])
names = [x["name"] for x in r["removed"]]
ck(names == ["库存种"], "只清了保种目录里那个: %s" % names)
ck(removed == ["aa"*20], "确实只对它发了 torrent-remove")
why = {x.get("name") or x["hash"]: x["why"] for x in r["refused"]}
ck("媒体库的剧" in why and "拒绝删除" in why["媒体库的剧"], "媒体库资产被拒")
ck("相似目录" in why, "**目录名相似的也被拒** —— 这一条就是裸 startswith 会漏掉的")
ck(any("找不到" in v for v in why.values()), "tr 里不存在的 hash 被拒")

print("— 拒绝在没配保种目录时动手 —")
m.CFG["KEEP_DIR"] = ""
ck(m.stock_evict(["aa"*20])["ok"] is False, "没配 KEEP_DIR → 直接拒绝,不猜")

os.unlink(DB)
print("\n" + ("❌ 失败 %d 项: %s" % (len(fail), fail) if fail else "✅ 全部通过"))
sys.exit(1 if fail else 0)
