# -*- coding: utf-8 -*-
"""AST 扫 TaskRunner：找被读但从未赋值的 self 属性 + 新实例冒烟。
改 TaskRunner/DingBot 结构后跑一遍：python _test_runner_attrs.py
（曾抓住：编辑插入方法时把 __init__ 尾部挤进别的方法体，live_* 属性丢失）"""
import ast

src = open(r"D:\研\claude-remote-bot\bot.py", encoding="utf-8").read()
tree = ast.parse(src)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TaskRunner")

init_sets, any_sets, loads = set(), set(), set()
fn_of_set = {}
for node in ast.walk(cls):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        if isinstance(node.ctx, ast.Store):
            any_sets.add(node.attr)
        elif isinstance(node.ctx, ast.Load):
            loads.add(node.attr)
for fn in cls.body:
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for node in ast.walk(fn):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "self" and isinstance(node.ctx, ast.Store)):
                fn_of_set.setdefault(node.attr, set()).add(fn.name)
                if fn.name == "__init__":
                    init_sets.add(node.attr)

# 1) 被读但全类从没赋值 → 剩下的应只有方法名（方法调用也是 Load）
meths = {f.name for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
orphans = sorted(loads - any_sets - meths)
assert not orphans, "被读取但从未赋值的属性: %s" % orphans
# 2) 有赋值但 __init__ 没有 → 新实例可能没有；逐个人工确认
risky = sorted(a for a in any_sets - init_sets)
risky = [a for a in risky if fn_of_set[a] - {"__init__"} and "__init__" not in fn_of_set[a]]
print("SET_ONLY_OUTSIDE_INIT(需人工确认):", risky)

import bot
r = bot.TaskRunner(bot.load_config())
need = ["cfg", "reply", "send_file", "queue", "pending", "current", "usage_all",
        "approval", "_worker", "live_wanted", "live_proc", "live_file", "live_path"]
bad = [a for a in need if not hasattr(r, a)]
assert not bad, "新实例缺属性: %s" % bad
r.status_text()
print("RUNNER_ATTRS_OK")
