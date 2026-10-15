# -*- coding: utf-8 -*-
"""读取面板 /api/state,把当前任务状态写成 UTF-8 摘要(控制台是 GBK 打不了 emoji)。"""
import json
import urllib.request

tok = json.load(open('config.json', encoding='utf-8'))['panel_token']
req = urllib.request.Request('http://127.0.0.1:8900/api/state',
                             headers={'X-Panel-Token': tok})
d = json.loads(urllib.request.urlopen(req, timeout=5).read())

t = d.get('task')
lines = []
lines.append('任务: ' + (t['title'][:40] if t else '无'))
lines.append('阶段: ' + (t['phase'] if t else '-'))
if t:
    lines.append('时长: %.1f 分' % t['mins'])
lines.append('审批在等: ' + json.dumps(d.get('approval'), ensure_ascii=False))
tr = d.get('trace') or []
lines.append('trace 条数: %d' % len(tr))
for e in tr[-4:]:
    lines.append(' - %s %s' % (e['k'], (e.get('name') or e.get('text', ''))[:60]))
lines.append('本任务 usage: ' + json.dumps(d.get('usage'), ensure_ascii=False))
open('_state_out.txt', 'w', encoding='utf-8').write('\n'.join(lines))
print('written')
