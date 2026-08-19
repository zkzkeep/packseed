# -*- coding: utf-8 -*-
"""把求值后的 PAGE 里的 JS 提出来过 node --check。
   必须 import 之后取 m.PAGE(求值后的字符串),不能直接正则源码 —— 源码里的转义和
   求值后完全是两回事,这个坑吃过两次白屏。"""
import os, re, sys, subprocess, tempfile, importlib.util
import os as _os
PS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "packseed.py")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
spec = importlib.util.spec_from_file_location("ps", PS)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
for attr in ("PAGE", "LOGIN_PAGE"):
    page = getattr(m, attr, "")
    if not page: continue
    js = "\n;\n".join(re.findall(r"<script[^>]*>(.*?)</script>", page, re.S))
    if not js.strip():
        print(f"  · {attr}: 无内联 JS"); continue
    f = tempfile.mktemp(suffix=".js"); open(f, "w", encoding="utf-8").write(js)
    r = subprocess.run(["node", "--check", f], capture_output=True, text=True)
    print(("  ✓ " if r.returncode == 0 else "  ✗ ") + f"{attr}: {len(js)} 字节 JS")
    if r.returncode:
        print(r.stderr[:900]); bad += 1
    os.unlink(f)
    # 顺带查全角字符:JS 里混进全角括号/引号,Python 侧一点感觉都没有
    for i, line in enumerate(js.split("\n"), 1):
        for ch in "（）；，“”‘’":
            if ch in line and not line.strip().startswith("//"):
                seg = line.strip()[:70]
                if not re.search(r"['\"][^'\"]*" + re.escape(ch), line):
                    print(f"  ⚠️ {attr} 第{i}行有全角字符 {ch}: {seg}"); break
sys.exit(1 if bad else 0)
