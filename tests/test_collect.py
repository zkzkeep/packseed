# -*- coding: utf-8 -*-
"""经典电影收藏的真验证。重点盯老版那三个硬伤有没有真治好:
   抓不全 / 4K(或指定画质)挑不出来 / 任务丢失。"""
import os, sys, re, tempfile, importlib.util
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
DB = tempfile.mktemp(suffix=".db"); os.environ["DB_PATH"] = DB
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CFG["DB"] = DB; m.init_db()
fail = []
def ck(c, msg):
    print(("  ✓ " if c else "  ✗ ") + msg)
    if not c: fail.append(msg)

def R(title, size_gb, seeders):
    return {"title": title, "size": int(size_gb * 1024**3), "seeders": seeders,
            "indexer": "站A", "downloadUrl": "u", "site": "站A"}

print("— 1. 画质是硬要求,不是加分项(老版的病:嘴上要4K,拿回来1080p) —")
cands = [R("Some.Movie.2019.1080p.WEB-DL.x264-GRP", 12, 500),   # 做种数碾压
         R("Some.Movie.2019.2160p.WEB-DL.x265-GRP", 45, 20)]    # 但这才是要的画质
p = m.pick_for_collect(cands, want_res=2160, strict=True)
ck(p and p["res"] == 2160, "要 2160p 就只给 2160p —— 哪怕 1080p 的做种数是它 25 倍")
p = m.pick_for_collect(cands, want_res=1080, strict=True)
ck(p and p["res"] == 1080, "要 1080p 就只给 1080p")

print("— 2. 挑不到目标画质时返回 None,绝不静默降级 —")
only1080 = [R("Movie.2019.1080p.WEB-DL-A", 12, 300), R("Movie.2019.1080p.BluRay-B", 20, 100)]
ck(m.pick_for_collect(only1080, want_res=2160, strict=True) is None,
   "只有 1080p 而要 2160p → None(单列出来让人决定,不偷偷换货)")
p = m.pick_for_collect(only1080, want_res=2160, strict=False)
ck(p and p["res"] == 1080, "显式允许降级时才退而求其次")

print("— 3. 体积窗口跟着画质走(老版一个全局 2~25GB 窗口把 4K 全过滤了) —")
ck(m.RES_WINDOW[2160][1] >= 100, "2160p 上限够装 REMUX(%dGB)" % m.RES_WINDOW[2160][1])
huge = [R("Movie.2019.2160p.UHD.BluRay.REMUX", 300, 50)]      # 300GB 太离谱
ck(m.pick_for_collect(huge, 2160, True) is None, "离谱体积(300GB)仍然拒绝")
ok4k = [R("Movie.2019.2160p.WEB-DL.x265", 45, 50)]
ck(m.pick_for_collect(ok4k, 2160, True) is not None, "正常 4K 体积(45GB)能过 —— 老版这里会被 25GB 上限毙掉")
tiny = [R("Movie.2019.1080p.WEB-DL", 0.5, 999)]
ck(m.pick_for_collect(tiny, 1080, True) is None, "体积过小(渣画质)照样拒绝")

print("— 4. 同画质里挑做种多的 —")
same = [R("Movie.2019.1080p.WEB-DL-A", 12, 50), R("Movie.2019.1080p.WEB-DL-B", 13, 800)]
ck(m.pick_for_collect(same, 1080, True)["seeders"] == 800, "同画质按做种数取优")

print("— 5. Top250 页面解析(离线 fixture,不联网) —")
FIX = ('<div class="item"><div class="pic"><em>1</em>'
       '<a href="https://movie.douban.com/subject/1234567/"><img alt="某部电影"></a></div>'
       '<div class="info"><div class="hd"><a href="https://movie.douban.com/subject/1234567/">'
       '<span class="title">某部电影</span><span class="title">&nbsp;/&nbsp;Some Movie</span></a></div>'
       '<div class="bd"><p>导演: 某人&nbsp;&nbsp;&nbsp;主演: 某某<br>1994&nbsp;/&nbsp;美国&nbsp;/&nbsp;剧情</p>'
       '<div class="star"><span class="rating_num" property="v:average">9.7</span></div></div></div></div>')
ms = list(m._T250_ITEM.finditer(FIX))
ck(len(ms) == 1, "结构能匹配")
if ms:
    ck(ms[0].group(1) == "1234567" and ms[0].group(2) == "某部电影" and ms[0].group(4) == "9.7",
       "id/片名/评分都取对了")
    ck(re.search(r'(\d{4})&nbsp;', ms[0].group(3)).group(1) == "1994", "年份取对了")
# 证伪:<p class=""> 那种写法应当匹配不到 —— 这正是第一版写错的地方
BAD = re.compile(r'<div class="item">.*?subject/(\d+)/.*?<span class="title">([^<]+)</span>'
                 r'.*?<p class="">(.*?)</p>.*?property="v:average">([\d.]+)<', re.S)
ck(len(list(BAD.finditer(FIX))) == 0, "证伪:写成 <p class=\"\"> 一条都匹配不到(第一版就栽在这)")

print("— 6. 片单落库:去重 + 断点续跑 —")
rows = [{"dbid": str(i), "title": f"片{i}", "year": "2000", "rate": "9.0", "rank": i} for i in range(1, 11)]
ck(m.collect_add(rows, "douban_top250") == 10, "首次入队 10 部")
ck(m.collect_add(rows, "douban_top250") == 0, "重复运行不会灌出重复行")
st = m.collect_stats()
ck(st["total"] == 10 and st["by"].get("pending") == 10, "状态在库里,进程重启也还在")
m._led("UPDATE collect SET status='ready', size=? WHERE id<=3", (20 * 1024**3,))
st = m.collect_stats()
ck(st["ready_n"] == 3, "ready 计数正确")
ck(st["ready_size"] == 60 * 1024**3, "容量预估把体积加起来了(%s)" % st["ready_sizeh"])
ck("guard_gb" in st and "fits" in st, "带磁盘水位判断,不会闷头把盘推满")

os.unlink(DB)
print("\n" + ("❌ 失败 %d 项: %s" % (len(fail), fail) if fail else "✅ 全部通过"))
sys.exit(1 if fail else 0)
