# -*- coding: utf-8 -*-
"""验证:提取服务器上页面(或模块内 PAGE_HTML)的 <script>,写 _page_check.js 供 node 检查。"""
import re
import sys

import panel

m = re.search(r'<script>(.*)</script>', panel.PAGE_HTML, re.S)
open('_page_check.js', 'w', encoding='utf-8').write(m.group(1))
print('extracted', len(m.group(1)), 'chars')
