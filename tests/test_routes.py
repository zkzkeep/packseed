# -*- coding: utf-8 -*-
"""路由冒烟测试:把每个 GET 接口真打一遍,确认没有 500。

为什么非要有它:单元测试碰不到 HTTP 层。之前 stock_report 本身全绿,
但路由里把 _send_json 写成了 _json,一上真实例就 500 —— 只有真起服务才看得出来。"""
import os, sys, json, tempfile, threading, importlib.util, urllib.request, urllib.error
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
DB = tempfile.mktemp(suffix=".db"); os.environ["DB_PATH"] = DB
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CFG.update(DB=DB, AUTH_USER="", KEEP_DIR="/data/downloads/keepseed")
m.init_db()
m.prowlarr_indexers = lambda: [{"id": 1, "name": "站A", "indexerUrls": ["https://a.example/"]}]
class FakeTR:
    def call(s, meth, args): return {"arguments": {"torrents": []}}
    def torrents(s, fields=None): return []
    def torrent(s, ih): return None
m.tr_conn = lambda: FakeTR()
cid = "smoke0000000"
m.led_touch(cid, "冒烟内容", 100, 2); m.led_bind("f"*40, cid, "tr", "站A", "/data/dl")
m.led_cov_set(cid, "站A", "seeding")

from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

ROUTES = ["/", "/api/dashboard", "/api/overview", "/api/downloads", "/api/gap", "/api/stock",
          "/api/health", "/api/libaudit", "/api/searchstat", "/api/settings", "/api/logs",
          "/api/gapfill?hash=" + "f"*40, "/api/artstat", "/api/batchstat", "/api/ks/status"]
fail = []
for r in ROUTES:
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}{r}", timeout=20)
        code, body = resp.status, resp.read(400)
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read(400)
    except Exception as e:
        code, body = 0, str(e).encode()
    ok = code in (200, 404)          # 404 = 该路由本来就没有,不算崩
    print(("  ✓ " if ok else "  ✗ ") + f"{r} → {code}")
    if not ok:
        fail.append(r); print("     " + body.decode("utf-8", "ignore")[:200])
srv.shutdown(); os.unlink(DB)
print(("❌ %d 个接口崩了: %s" % (len(fail), fail)) if fail else "✅ 全部接口无 500")
sys.exit(1 if fail else 0)
