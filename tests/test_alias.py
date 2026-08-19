# -*- coding: utf-8 -*-
"""别名扇出的真验证。用搜索体系文档里记下的真实别名池 —— 那次实测「三个词扇出去 +28% 时间、
   只多认领 0~5 条」,根因就是挑出来的三个词是同一个入口的三种拼写。"""
import os, sys, tempfile, importlib.util
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fail = []
def ck(c, msg):
    print(("  ✓ " if c else "  ✗ ") + msg)
    if not c: fail.append(msg)

print("— 文档里记下的真实案例:白色巨塔 —")
pool = ["Shiroi Kyotou", "Shiroi kyotô", "Shiroi Kyoto", "白い巨塔", "The White Tower", "白色巨塔"]
p = m.alias_plan(pool, "白色巨塔", k=2)
print("     挑出:", p)
ck(len(p) == 2, "挑了 2 个")
lat = [x for x in p if m._script_of(x) == "latin"]
ck(len(lat) <= 1, "**罗马字的三种拼写只出一个** —— 老逻辑挑到哪个都一样,等于白扇")
ck(any(m._script_of(x) == "kana" for x in p), "日文原名(假名)被选进来了 —— 这才是另一个召回入口")
ck("白色巨塔" not in p, "跟原查询等价的词不重复扇")

print("— 证伪:全是近似重复时,不硬凑数 —")
p2 = m.alias_plan(["Shiroi Kyotou", "Shiroi kyotô", "Shiroi Kyoto"], "白色巨塔", k=2)
print("     挑出:", p2)
ck(len(p2) == 1, "三个变体只值一个词(硬凑两个就是浪费一波搜索)")

print("— 拼写差异大的同文字名字要各算一个入口 —")
p3 = m.alias_plan(["Da Ming Wang Chao", "The Ming Dynasty 1566"], "大明王朝1566", k=2)
print("     挑出:", p3)
ck(len(p3) == 2, "拼音名和英文意译名是两个入口,都要")

print("— 韩剧:英文名 + 谚文原名 —")
p4 = m.alias_plan(["Reply 1988", "응답하라 1988", "Eungdaphara 1988"], "请回答1988", k=2)
print("     挑出:", p4)
ck(any(m._script_of(x) == "hangul" for x in p4), "谚文原名入选")
ck(len(p4) == 2, "谚文 + 拉丁各一个")

print("— 归一化能识破变音符和大小写 —")
ck(m._norm_alias("Shiroi kyotô") == m._norm_alias("SHIROI KYOTO"), "变音符/大小写归一后相同")
ck(m._norm_alias("白い巨塔") != m._norm_alias("白色巨塔"), "不同的名字不会被归一成同一个")
print("— 文字判定 —")
for txt, want in [("白色巨塔","han"),("しろいきょとう","kana"),("응답하라","hangul"),("The Tower","latin")]:
    ck(m._script_of(txt) == want, f"{txt} → {want}")

print("\n" + ("❌ 失败 %d 项: %s" % (len(fail), fail) if fail else "✅ 全部通过"))
sys.exit(1 if fail else 0)
