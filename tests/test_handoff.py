# -*- coding: utf-8 -*-
"""交棒(qb→tr)的真验证。重点复现老版那个病:入库过一次之后,交棒失败就永远没人管了。"""
import os, sys, tempfile, importlib.util
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
DB = tempfile.mktemp(suffix=".db"); os.environ["DB_PATH"] = DB
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CFG.update(DB=DB, ORGANIZE=True, TMDB_KEY="x")
m.init_db()
fail = []
def ck(c, msg):
    print(("  ✓ " if c else "  ✗ ") + msg)
    if not c: fail.append(msg)

TOP = "某部剧.S01.1080p"
QBF = [{"name": TOP+"/E01.mkv", "size": 1000}, {"name": TOP+"/E02.mkv", "size": 2000}]
info = {b'name': TOP.encode(), b'piece length': 262144, b'pieces': b'x'*20,
        b'files': [{b'path': [b'E01.mkv'], b'length': 1000}, {b'path': [b'E02.mkv'], b'length': 2000}]}
TORRENT = m.bencode({b'announce': b'http://tracker.x.com/announce', b'info': info})
qbt = {"hash": "bb"*20, "name": TOP, "save_path": "/data/dl", "progress": 1, "tags": "", "size": 3000}

deleted = []; tr_ok = [False]; organized = []
class FakeQB:
    def files(s, h): return QBF
    def export(s, h): return TORRENT
    def delete(s, h, delete_files=False): deleted.append(h)
class FakeTR:
    def add(s, d, p):
        return {"result":"success","arguments":{"torrent-added":{"id":9}}} if tr_ok[0] else {"result":"boom","arguments":{}}
    def call(s, *a, **k): return {}
m.tr_conn = lambda: FakeTR()
m.tmdb_match = lambda n, force_tv=False: None          # 识别不出 → 走 hold
m.media_category = lambda n, mm: "电视剧"
m.notify = lambda *a, **k: None
qb = FakeQB()
cid = m.content_id(m.manifest_qb(QBF, TOP))

print("— 第 1 轮:识别不出 → hold,交棒又失败 —")
m.process_completed(qb, qbt)
def media_row():
    return m._led("SELECT status FROM media WHERE info_hash=?", (qbt["hash"],), fetch="one")
ck(media_row() and media_row()[0] == "hold", "入库走了 hold 分支(真写进 media 表)")
g = m.led_get(cid)
ck(g and g["xfer_fail"] == 1, "交棒失败记了账 xfer_fail=%s" % (g or {}).get("xfer_fail"))
ck(not deleted, "交棒没成功 → 绝不能从 qb 删任务(删了数据就没人管了)")

print("— 第 2 轮:老版的病灶 —— 入库有记录了,交棒还该不该重试 —")
m.process_completed(qb, qbt)
ck(m._led("SELECT COUNT(*) FROM media", fetch="one")[0] == 1, "不重复入库(hold 记录还在,只有 1 条)")
ck(m.led_get(cid)["xfer_fail"] == 2, "**交棒仍然重试了** —— 老版到这里就永远不管了")

print("— 第 3 轮起:连败到上限就收手,不再每分钟骚扰 —")
for _ in range(5): m.process_completed(qb, qbt)
f = m.led_get(cid)["xfer_fail"]
ck(f == m.XFER_FAIL_LIMIT, "连败 %d 次后停止自动重试(实际 %d)" % (m.XFER_FAIL_LIMIT, f))

print("— 人工重置后能复活,tr 恢复了就交棒成功 —")
tr_ok[0] = True
m.led_xfer_reset(cid)
m.process_completed(qb, qbt)
g = m.led_get(cid)
ck(g["xfer_fail"] == 0 and g["place"] == "tr", "交棒成功:失败计数清零,位置记成 tr")
ck(deleted == [qbt["hash"]], "交棒成功才从 qb 删任务(保留数据) —— 一个种子只能一个客户端做种")
ck(m.led_has_tr(cid), "tr 实例已入账")

print("— 再跑一轮:已交棒的内容不该再动 —")
n = len(deleted); m.process_completed(qb, qbt)
ck(len(deleted) == n, "已交棒 → 不重复操作")

os.unlink(DB)
print("\n" + ("❌ 失败 %d 项: %s" % (len(fail), fail) if fail else "✅ 全部通过"))
sys.exit(1 if fail else 0)
