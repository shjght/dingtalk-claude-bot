# -*- coding: utf-8 -*-
"""record_trace 单元验证:模拟 stream-json 事件流,检查思维链与 token 累计。"""
import bot

cur = {}
evts = [
    {"type": "assistant", "message": {"usage": {
        "input_tokens": 100, "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 6000, "output_tokens": 356},
        "content": [
            {"type": "thinking", "thinking": "先看目录…"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls D:/研", "x": 1}},
        ]}},
    {"type": "assistant", "message": {"usage": {
        "input_tokens": 200, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 7000, "output_tokens": 800},
        "content": [{"type": "text", "text": "结论是…"}]}},
    {"type": "control_request", "request": {"subtype": "can_use_tool",
        "tool": {"name": "Bash"}, "input": {"command": "rm -rf /"}, "description": "x"}},
    {"type": "result", "total_cost_usd": 0.0421, "usage": {"output_tokens": 1}},
]

for e in evts:
    bot.record_trace(cur, e)

u = cur["usage"]
assert u["out"] == 356 + 800, u
assert u["in"] == 100 + 50 + 200, u
assert u["cache"] == 6000 + 7000, u
assert u["turns"] == 2, u
assert u["cost"] == 0.0421, u
tr = cur["trace"]
assert [e["k"] for e in tr] == ["think", "tool", "text", "ask"], tr
assert tr[0]["u"]["out"] == 356
assert tr[1]["name"] == "Bash" and tr[1]["text"] == "ls D:/研"
assert tr[3]["u"] is None
print("RECORD_TRACE_OK", u)
