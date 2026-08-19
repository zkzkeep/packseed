#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""观澜的分层守卫 + 测试总入口。改完代码跑这个:  python3 check.py

为什么要有它:单文件重构最容易死在「分了区,调用还是乱穿」—— 两个月后又摞回原样。
所以约束不能只写在注释里,得能被机器查出来。"""
import ast, io, os, re, subprocess, sys, glob, tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC  = os.path.join(ROOT, "packseed.py")
src  = io.open(SRC, encoding="utf-8").read()
tree = ast.parse(src)
bad  = []

def fail(rule, detail):
    bad.append((rule, detail))
    print(f"  ✗ [{rule}] {detail}")

print("① 语法")
print("  ✓ packseed.py 能解析")

print("② 铁律:总账三张表只能经 _led() 访问")
# 唯一 SQL 出口。别处直连 = 账本随时可能被绕过写脏,这条一破,整套身份体系就不可信了。
LEDGER_TABLES = ("content", "instance", "coverage")
ALLOWED = {"_led", "init_db"}
for node in ast.walk(tree):
    if not isinstance(node, ast.FunctionDef) or node.name in ALLOWED: continue
    if node.name.startswith("led_"): continue
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Constant) or not isinstance(sub.value, str): continue
        t = sub.value
        if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", t, re.I): continue
        for tb in LEDGER_TABLES:
            if re.search(r"\b(FROM|INTO|UPDATE|JOIN)\s+%s\b" % tb, t, re.I):
                fail("唯一SQL出口", f"{node.name}() 第 {sub.lineno} 行直连了 {tb} 表,应改走 led_* 函数")
if not any(r == "唯一SQL出口" for r, _ in bad):
    print("  ✓ 没有绕过账本的直连")

print("③ 铁律:删数据必须过目录边界护栏 _under()")
# 裸 startswith 会让 /keepseed-old 被当成 /keepseed 之下,删错数据不可逆。
for node in ast.walk(tree):
    if not isinstance(node, ast.FunctionDef): continue
    body = ast.get_source_segment(src, node) or ""
    danger = ("delete-local-data" in body) or ("rmtree" in body) or ("delete_files=True" in body)
    # 白名单:护栏形式不同但确实有,理由写在这里,新增的删除代码必须先在这里说清楚
    EXEMPT = {
        "_drop_old_media_dir": "护栏是「必须在媒体库根之下、且不是库根本身」,函数内自带",
        "_chat_undo":          "护栏是「30 分钟窗口 + 只认 _CHAT.last 记下的那一个」",
        "_canceldl":           "护栏是「只能取消 progress<1 的,下完的一律拒绝」",
    }
    if danger and "_under(" not in body and node.name not in EXEMPT:
        fail("删除护栏", f"{node.name}() 会删数据,却没有目录边界检查,也不在白名单里")
if not any(r == "删除护栏" for r, _ in bad):
    print("  ✓ 删数据的地方都有边界检查")

print("④ 铁律:模板里的 JS 不许写反斜杠转义")
# PAGE 是三引号普通字符串,Python 会先吃掉一层反斜杠 → JS 变成裸引号 → 整页白屏。
for m in re.finditer(r'onclick="[^"]*\\', src):
    ln = src[:m.start()].count("\n") + 1
    fail("模板转义", f"第 {ln} 行 onclick 里有反斜杠转义,求值后会变成裸引号,改用 data-* 属性")
if not any(r == "模板转义" for r, _ in bad):
    print("  ✓ 没有会被吃掉的转义")

print("⑤ 分区标记")
secs = re.findall(r"# =+ (§\d+[^=\n]*)", src)
print("  ✓ " + " / ".join(s.strip().split()[0] for s in secs) if secs else "  ✗ 没有分区标记")

print("\n⑥ 跑测试")
tests = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
for t in tests:
    r = subprocess.run([sys.executable, t], capture_output=True, text=True)
    name = os.path.basename(t)
    if r.returncode == 0:
        n = r.stdout.count("✓")
        print(f"  ✓ {name} ({n} 项)")
    else:
        print(f"  ✗ {name}")
        print("     " + "\n     ".join((r.stdout + r.stderr).strip().split("\n")[-12:]))
        bad.append(("测试", name))

print()
if bad:
    print(f"❌ {len(bad)} 项没过")
    sys.exit(1)
print("✅ 全部通过")
