# -*- coding: utf-8 -*-
"""账本层真验证:能证伪的那种。跑在临时 DB 上,不碰生产。"""
import os, sys, tempfile, importlib.util
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = DB
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CFG["DB"] = DB
m.init_db()
fail = []
def ck(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond: fail.append(msg)

print("— 1. cid 三个来源必须一致(这是整套账的地基) —")
# 构造:一个两文件的剧集种子,顶层目录叫「大明王朝1566」
top = "大明王朝1566"
tr_t = {"name": top, "files": [{"name": top+"/E01.mkv", "length": 1000},
                               {"name": top+"/E02.mkv", "length": 2000}]}
qb_files = [{"name": top+"/E01.mkv", "size": 1000}, {"name": top+"/E02.mkv", "size": 2000}]
tor_files = {"E01.mkv": 1000, "E02.mkv": 2000}       # torrent_files() 的口径:不含顶层
a = m.content_id(m.manifest_tr(tr_t))
b = m.content_id(m.manifest_qb(qb_files, top))
c = m.content_id(tor_files)
ck(a == b == c, "tr / qb / .torrent 三个来源算出同一个 cid: %s" % a)

print("— 2. 必须能证伪:差一个字节就是另一份内容 —")
ck(m.content_id({"E01.mkv":1000,"E02.mkv":2001}) != a, "改一个文件大小 → cid 变了")
ck(m.content_id({"E01.mkv":1000}) != a, "少一个文件 → cid 变了")
ck(m.content_id({"E1.mkv":1000,"E02.mkv":2000}) != a, "改一个文件名 → cid 变了")
print("— 3. 与顺序无关(tr 和 qb 返回顺序可能不同) —")
ck(m.content_id({"E02.mkv":2000,"E01.mkv":1000}) == a, "文件顺序颠倒 → cid 不变")
print("— 4. 单文件种子(顶层名就是文件名,不能剥错) —")
s1 = m.content_id(m.manifest_tr({"name":"x.mkv","files":[{"name":"x.mkv","length":5}]}))
ck(s1 == m.content_id({"x.mkv":5}), "单文件种子口径对齐")

print("— 5. 账本读写 —")
m.led_touch(a, top, 3000, 2); m.led_bind("AABB"*10, a, "tr", "TTG", "/data/x")
ck(m.led_cid("aabb"*10) == a, "info_hash 反查 cid(大小写不敏感)")
g = m.led_get(a); ck(g and g["name"] == top and g["size"] == 3000, "content 取回正确")
m.led_role(a, "library"); m.led_role(a, "stock")
ck(m.led_get(a)["role"] == "library", "角色只升不降:library 不会被 stock 覆盖")
m.led_touch(a, "改了个名", 0, 0)
ck(m.led_get(a)["name"] == top, "展示名稳定,不被后来的改名覆盖")

print("— 6. 覆盖矩阵:pending 的算法(辅种的驱动力) —")
sites = ["TTG","馒头","观众","CHDBits","U2"]
ck(sorted(m.led_cov_pending(a, sites)) == sorted(sites), "全新内容 → 所有站都该问")
m.led_cov_set(a, "TTG", "source"); m.led_cov_set(a, "馒头", "seeding")
m.led_cov_set(a, "观众", "absent"); m.led_cov_set(a, "CHDBits", "error")
p = m.led_cov_pending(a, sites)
ck("TTG" not in p and "馒头" not in p, "已做种的站不再问")
ck("观众" not in p, "刚问过说没有的站,保质期内不重复问")
ck("CHDBits" in p, "出错的站下轮必重试(cookie过期≠资源不存在)")
ck("U2" in p, "从没问过的站仍在待办里")
# 证伪点:把 absent 的时间戳改老,必须重新变回 pending
m._led("UPDATE coverage SET ts=? WHERE cid=? AND site=?", (1, a, "观众"))
ck("观众" in m.led_cov_pending(a, sites), "absent 过了保质期 → 自动转回 pending")
st = m.led_cov_stats()
ck(st["content"] == 1 and st["seeding"] == 2 and st["error"] == 1, "统计口径正确: %s" % st)

os.unlink(DB)
print("\n" + ("❌ 失败 %d 项: %s" % (len(fail), fail) if fail else "✅ 全部通过 (%d 项)" % 0))
sys.exit(1 if fail else 0)
