# -*- coding: utf-8 -*-
"""
手机网页面板：挂在 bot 进程内的轻量控制台（aiohttp 由 dingtalk-stream 自带）。

设计要点：
- 面板和钉钉共用同一条消息处理管线：面板发的内容（含点选会话/模型/模式，
  都是注入对应命令）会构造一条"虚拟消息"走 DingBot.process()，行为与手机
  发消息完全一致；回复照常发进钉钉聊天，同时镜像到面板。
- 注入依赖"最近一次手机真实消息"缓存（DingBot._last_data）：里面有回复用的
  sessionWebhook。所以手机至少发过一条消息后，面板才是完整可用的。
- 图片/文件上传走 /api/media：存 incoming/ 后按手机同款流程交给 claude 查看。
- 访问范围：本机 / 局域网 / Tailscale 虚拟网（外网方案见 设置指南.md）。
  安全 = panel_token（config.json，首次启动自动生成）+ Tailscale 账号隔离。
- 支持 PWA：手机浏览器"添加到主屏幕"后获得独立图标，接近 App 体验。
"""
import io
import json
import logging
import re
import time
from pathlib import Path

from aiohttp import web

log = logging.getLogger("dingbot.panel")

BASE_DIR = Path(__file__).resolve().parent
INCOMING_DIR = BASE_DIR / "incoming"
MEDIA_MAX = 20 * 1024 * 1024
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp"}

# ---------------- PWA 图标（启动时用 Pillow 画，不依赖外部资源） ----------------

def _make_icon(size: int) -> bytes:
    """画一枚"机器人遥控"图标：深蓝紫渐变底 + 白色机器人脸。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    top, bottom = (26, 32, 58), (74, 58, 138)  # 深蓝 → 紫 渐变
    for y in range(size):
        k = y / size
        d.line([(0, y), (size, y)],
               fill=(int(top[0] + (bottom[0] - top[0]) * k),
                     int(top[1] + (bottom[1] - top[1]) * k),
                     int(top[2] + (bottom[2] - top[2]) * k)))
    s = size / 512  # 按 512 设计稿缩放
    # 天线
    d.line([(size / 2, size * 0.20), (size / 2, size * 0.30)],
           fill="#e8ecf8", width=int(10 * s))
    d.ellipse([size / 2 - 14 * s, size * 0.20 - 14 * s,
               size / 2 + 14 * s, size * 0.20 + 14 * s], fill="#ffb020")
    # 脸（圆角矩形）
    d.rounded_rectangle([size * 0.24, size * 0.30, size * 0.76, size * 0.74],
                        radius=int(56 * s), outline="#e8ecf8", width=int(14 * s))
    # 眼睛
    for cx in (size * 0.40, size * 0.60):
        d.ellipse([cx - 18 * s, size * 0.46 - 18 * s,
                   cx + 18 * s, size * 0.46 + 18 * s], fill="#e8ecf8")
    # 嘴（一条短线）
    d.rounded_rectangle([size * 0.42, size * 0.62, size * 0.58, size * 0.62 + 10 * s],
                        radius=int(5 * s), fill="#ffb020")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


_MANIFEST = {
    "name": "claude 遥控",
    "short_name": "遥控",
    "description": "Claude Code 远程控制面板",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b0e14",
    "theme_color": "#12161f",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


class _FakeCallback:
    """伪装 dingtalk_stream.CallbackMessage：process() 只用到 .data。"""

    def __init__(self, data: dict):
        self.data = data


def _check_token(bot, request) -> bool:
    tok = (bot.cfg.get("panel_token") or "").strip()
    if not tok:  # 没配 token = 面板关闭，防裸奔
        return False
    got = request.headers.get("X-Panel-Token") or request.query.get("token") or ""
    return got == tok


async def handle_index(bot, request):
    # 页面本身不做 token 校验：它只是空壳（无任何数据），数据全在需要令牌的
    # /api/* 里。这样"添加到主屏幕"保存的干净网址（没有 ?token=）也能打开，
    # 令牌由页面 JS 从 localStorage 取（首次使用时页面内引导粘贴一次）。
    return web.Response(text=PAGE_HTML, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-cache"})  # 防止手机 WebView 拿旧版 JS


async def handle_state(bot, request):
    if not _check_token(bot, request):
        return web.json_response({"err": "bad token"}, status=403)
    return web.json_response(bot.panel_snapshot())


async def handle_send(bot, request):
    if not _check_token(bot, request):
        return web.json_response({"err": "bad token"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"err": "bad json"}, status=400)
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"err": "empty"}, status=400)
    if len(text) > 8000:
        return web.json_response({"err": "太长（>8000 字）"}, status=400)
    if bot._last_data is None:
        return web.json_response(
            {"err": "还没拿到钉钉回复通道：请先用手机钉钉给机器人发一条消息，再回来用面板。"},
            status=409)
    ok, msg = await bot.inject_from_panel(text)
    return web.json_response({"ok": ok, "msg": msg})


async def handle_media(bot, request):
    """面板上传图片/文件：存 incoming/ 后按手机同款流程交给 claude。"""
    if not _check_token(bot, request):
        return web.json_response({"err": "bad token"}, status=403)
    if not (request.content_type or "").startswith("multipart/"):
        return web.json_response({"err": "需要 multipart 表单"}, status=400)
    reader = await request.multipart()
    field = None
    async for part in reader:
        if part.name == "file":
            field = part
            break
    if field is None:
        return web.json_response({"err": "缺少 file 字段"}, status=400)

    raw_name = field.filename or "upload.bin"
    ext = re.sub(r"[^.\w]", "", Path(raw_name).suffix.lower())[:10] or ".bin"
    INCOMING_DIR.mkdir(exist_ok=True)
    path = INCOMING_DIR / f"{time.strftime('%m%d_%H%M%S')}{ext}"
    size = 0
    try:
        with open(path, "wb") as fh:
            while chunk := await field.read_chunk(64 * 1024):
                size += len(chunk)
                if size > MEDIA_MAX:
                    raise ValueError(f"文件超过 20MB 上限（{size // 1048576 + 1} MB）")
                fh.write(chunk)
    except Exception as e:
        path.unlink(missing_ok=True)
        log.warning("面板上传失败: %s", e)
        return web.json_response({"err": str(e)}, status=400)
    log.info("面板上传: %s (%.2f MB)", path, size / 1048576)
    kind = "image" if ext in IMG_EXTS else "file"
    try:
        await bot.submit_panel_media(path, kind)
    except Exception as e:
        log.exception("面板媒体任务提交失败")
        return web.json_response({"err": f"提交失败：{e}"}, status=500)
    return web.json_response({"ok": True})


async def start_site(cfg: dict, bot):
    """启动面板 HTTP 服务（不阻塞，返回后 site 常驻在同一事件循环里）。"""
    import asyncio
    icons = {}
    loop = asyncio.get_event_loop()

    async def _gen():
        for sz in (192, 512):
            icons[sz] = await loop.run_in_executor(None, _make_icon, sz)

    try:
        await _gen()
    except Exception:
        log.exception("生成面板图标失败（PWA 图标将不可用，不影响面板）")

    async def handle_icon(request):
        sz = 512 if "512" in request.path else 192
        data = icons.get(sz)
        if not data:
            return web.Response(status=404)
        return web.Response(body=data, content_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})

    async def handle_manifest(request):
        return web.Response(text=json.dumps(_MANIFEST, ensure_ascii=False),
                            content_type="application/manifest+json")

    app = web.Application()
    app.router.add_get("/", lambda r: handle_index(bot, r))
    app.router.add_get("/api/state", lambda r: handle_state(bot, r))
    app.router.add_post("/api/send", lambda r: handle_send(bot, r))
    app.router.add_post("/api/media", lambda r: handle_media(bot, r))
    app.router.add_get("/icon-192.png", handle_icon)
    app.router.add_get("/icon-512.png", handle_icon)
    app.router.add_get("/manifest.json", handle_manifest)
    # 访问日志写进 bot.log：手机每来一个请求都有记录，"连不上"时一眼分清
    # 是手机没发出请求还是请求没到电脑
    runner = web.AppRunner(app, access_log=logging.getLogger("panel.access"))
    await runner.setup()
    port = int(cfg.get("panel_port", 8900))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return site


# ---------------------------------------------------------------- 页面
PAGE_HTML = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#12161f">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="claude 遥控">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<link rel="icon" href="/icon-192.png">
<title>claude 遥控</title>
<style>
:root{
  --bg0:#0b0e14; --bg1:#12161f; --card:#171d29; --card2:#1c2333;
  --line:#263041; --txt:#e8eef8; --dim:#8b98ad; --faint:#5a6779;
  --acc:#4f8cff; --acc2:#7c5cff; --ok:#34d399; --warn:#ffb020; --bad:#ff6b6b;
  color-scheme:dark;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%}
body{
  background:radial-gradient(1200px 600px at 50% -10%, #1a2340 0%, var(--bg1) 55%, var(--bg0) 100%);
  color:var(--txt);
  font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  display:flex;flex-direction:column;height:100vh;overflow:hidden;
}
/* ---------- 顶部 ---------- */
header{
  padding:calc(10px + env(safe-area-inset-top)) 14px 10px;
  background:linear-gradient(180deg,rgba(38,48,80,.75),rgba(23,29,41,.4));
  border-bottom:1px solid var(--line);backdrop-filter:blur(10px);
}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;letter-spacing:.5px}
.brand .logo{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--acc),var(--acc2));
  display:inline-flex;align-items:center;justify-content:center;font-size:13px}
#fsbtn{margin-left:6px;background:none;border:1px solid rgba(255,255,255,.15);color:var(--dim);
  border-radius:8px;width:26px;height:22px;font-size:13px;line-height:1;padding:0;flex:0 0 auto}
#phase{margin-left:auto;font-size:12px;color:var(--dim);display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:0 0 auto}
.dot.run{background:var(--warn);animation:pulse 1.4s infinite}
.dot.ok{background:var(--ok)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,176,32,.5)}50%{box-shadow:0 0 0 6px rgba(255,176,32,0)}}
#taskline{font-size:12px;color:var(--dim);margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#taskline b{color:var(--warn);font-weight:600}
#badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.badge{background:rgba(79,140,255,.10);border:1px solid rgba(79,140,255,.25);
  border-radius:999px;padding:2px 10px;font-size:11.5px;color:#a8c4f5}
.badge.click{cursor:pointer;transition:transform .1s;display:inline-flex;align-items:center;gap:4px}
.badge.click:active{transform:scale(.93)}
.badge.w{background:rgba(255,176,32,.10);border-color:rgba(255,176,32,.35);color:#ffd489}
.badge .ar{font-size:9px;opacity:.7}
/* ---------- 主区 ---------- */
main{flex:1;overflow-y:auto;padding:12px 12px 6px;scroll-behavior:smooth}
.card{background:linear-gradient(180deg,var(--card2),var(--card));
  border:1px solid var(--line);border-radius:16px;padding:12px 14px;margin-bottom:12px;
  box-shadow:0 6px 18px rgba(0,0,0,.25)}
.card h4{margin:0 0 8px;font-size:12.5px;color:var(--dim);font-weight:600;letter-spacing:.5px}
/* 审批卡 */
.card.appr{
  border:1px solid transparent;border-radius:16px;
  background:linear-gradient(var(--card2),var(--card)) padding-box,
             linear-gradient(120deg,#ffb020,#ff6b6b) border-box;
  animation:slidein .25s ease;
}
@keyframes slidein{from{transform:translateY(-8px);opacity:0}to{transform:none;opacity:1}}
.card.appr h4{color:var(--warn);font-size:13.5px}
.appr .desc{background:rgba(0,0,0,.25);border-radius:10px;padding:8px 10px;
  font:12.5px/1.5 Consolas,monospace;color:#ffe0ad;white-space:pre-wrap;word-break:break-word}
/* 消息气泡 */
.msg{background:var(--card);border:1px solid var(--line);border-radius:4px 14px 14px 14px;
  padding:9px 12px;margin-bottom:9px;white-space:pre-wrap;word-break:break-word;font-size:14px;
  box-shadow:0 3px 10px rgba(0,0,0,.18)}
.msg .t{display:block;color:var(--faint);font-size:10.5px;margin-bottom:3px}
/* 日志 */
.logline{font:11.5px/1.7 Consolas,monospace;color:#93a4bd;word-break:break-all;
  padding:1px 0;border-bottom:1px dashed rgba(38,48,65,.6)}
.logline:last-child{border-bottom:0}
/* 思维链（Claude 风格：可折叠思考块 / 工具行 / token 章数） */
.thk{background:rgba(124,140,255,.06);border:1px solid rgba(124,140,255,.14);
  border-radius:12px;margin-bottom:8px;overflow:hidden}
.thk>.th{color:#8fa0c5;font-style:italic;font-size:13px;cursor:pointer;user-select:none}
.thk>.tb{color:#7d8fb0;font-style:italic;font-size:12.5px;line-height:1.75;white-space:pre-wrap;
  word-break:break-word;display:none;max-height:260px;overflow:auto;padding-top:6px}
.thk.open>.th{color:#aab8dd}
.thk.open>.th .ar{transform:rotate(90deg)}
.thk.open>.tb{display:block}
.tlk{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:8px;overflow:hidden}
.tlk>.th{color:#9fb0cc;font-size:12.5px;cursor:pointer;user-select:none}
.tlk>.th code{color:#6f80a0;font-size:11.5px}
.tlk>.tb{display:none;padding-top:6px}
.tlk.open>.tb{display:block}
.tlk.open>.th .ar{transform:rotate(90deg)}
.th{display:flex;align-items:center;gap:6px;padding:7px 11px}
.th .ar{display:inline-block;transition:transform .15s;font-size:10px;color:var(--faint)}
.tb{padding:0 11px 9px}
.tk{margin-left:auto;background:rgba(124,140,255,.13);color:#93a5f2;border-radius:8px;
  font-size:10px;padding:1.5px 7px;font-style:normal;flex:0 0 auto}
.askline{background:linear-gradient(135deg,rgba(255,176,32,.1),rgba(255,140,50,.06));
  border:1px solid rgba(255,176,32,.25);border-radius:12px;color:#ffd27a;
  font-size:12.5px;padding:8px 11px;margin-bottom:8px;word-break:break-word}
/* 空状态 */
#hint{text-align:center;color:var(--faint);font-size:13px;padding:48px 24px;line-height:2}
#hint .big{font-size:40px;display:block}
/* ---------- 底部 ---------- */
footer{padding:9px 12px calc(10px + env(safe-area-inset-bottom));
  background:linear-gradient(180deg,rgba(23,29,41,.6),rgba(11,14,20,.9));
  border-top:1px solid var(--line);backdrop-filter:blur(10px)}
#inrow{display:flex;gap:8px;align-items:center}
.iconbtn{flex:0 0 auto;width:44px;height:44px;border-radius:13px;border:1px solid var(--line);
  background:var(--card);color:var(--dim);font-size:19px;display:flex;align-items:center;
  justify-content:center;transition:transform .1s}
.iconbtn:active{transform:scale(.9)}
#txt{flex:1;background:rgba(11,14,20,.75);border:1px solid #2c3850;border-radius:13px;
  color:var(--txt);padding:11px 13px;font-size:15px;outline:none;transition:border .15s;min-width:0}
#txt:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(79,140,255,.15)}
#send{flex:0 0 auto;width:70px;height:44px;border:0;border-radius:13px;font-weight:700;font-size:14px;
  color:#fff;background:linear-gradient(135deg,var(--acc),var(--acc2));
  box-shadow:0 4px 14px rgba(90,120,255,.35);transition:transform .1s}
#send:active{transform:scale(.94)}
#quick{display:flex;gap:7px;margin-top:8px;overflow-x:auto;padding-bottom:2px}
#quick::-webkit-scrollbar{display:none}
#quick button{flex:0 0 auto;padding:7px 13px;border-radius:999px;border:1px solid var(--line);
  background:var(--card);color:var(--dim);font-size:12.5px;transition:transform .1s}
#quick button:active{transform:scale(.93)}
#quick button.danger{border-color:rgba(255,107,107,.4);color:#ff9c9c}
/* 通用按钮 */
.row{display:flex;gap:8px;margin-top:9px;flex-wrap:wrap}
button{cursor:pointer;font-family:inherit}
.b{flex:1;min-width:84px;padding:11px 8px;border:0;border-radius:11px;font-size:14px;font-weight:700;transition:transform .1s}
.b:active{transform:scale(.95)}
.b-ok{background:linear-gradient(135deg,#2f9e6e,#34d399);color:#04120c}
.b-no{background:linear-gradient(135deg,#b04747,#ff6b6b);color:#fff}
.b-all{background:linear-gradient(135deg,#a9770f,#ffb020);color:#1d1200}
.b-gray{background:var(--card2);color:var(--txt);border:1px solid var(--line)}
#guide{flex:1;background:rgba(11,14,20,.7);border:1px solid #4a3d1a;border-radius:11px;
  color:var(--txt);padding:10px;font-size:14px;outline:none;min-width:150px}
#guide:focus{border-color:var(--warn)}
#topbtn{position:fixed;right:12px;bottom:110px;width:38px;height:38px;border-radius:50%;
  border:1px solid var(--line);background:var(--card2);color:var(--dim);font-size:16px;display:none;z-index:5}
/* ---------- 底部抽屉（会话/模型/模式选择） ---------- */
#mask{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .2s;z-index:9}
#mask.show{opacity:1;pointer-events:auto}
#sheet{position:fixed;left:0;right:0;bottom:0;z-index:10;
  background:var(--bg1);border-radius:20px 20px 0 0;border-top:1px solid var(--line);
  padding:14px 16px calc(16px + env(safe-area-inset-bottom));
  transform:translateY(105%);transition:transform .22s ease;max-height:70vh;display:flex;flex-direction:column}
#sheet.show{transform:none}
#sheet h4{margin:0 0 4px;font-size:14px;color:var(--txt)}
#sheet .sub{font-size:11.5px;color:var(--faint);margin-bottom:10px}
#sheetBody{overflow-y:auto}
.optinfo{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:10px 13px;font-size:13.5px;line-height:1.6;overflow:auto;max-height:52vh}
.optinfo h4{margin:6px 0 2px;font-size:12.5px;color:var(--dim)}
.optinfo div+div{margin-top:4px}
.opt{width:100%;text-align:left;background:var(--card);border:1px solid var(--line);color:var(--txt);
  border-radius:12px;padding:12px 14px;margin-bottom:8px;font-size:14px;display:flex;align-items:center;gap:8px;
  transition:transform .1s}
.opt:active{transform:scale(.97)}
.opt small{color:var(--dim);font-size:11.5px;display:block;margin-top:1px}
.opt .chk{margin-left:auto;color:var(--ok);font-weight:700}
.opt.new{border-style:dashed;color:#a8c4f5}
#uploading{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20;display:none;
  align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--dim);font-size:13px}
.spin{width:34px;height:34px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--acc);
  animation:rot 0.8s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
</style></head><body>
<header>
  <div class="brand"><span class="logo">🤖</span>claude 遥控
    <span id="phase"><span class="dot" id="dot"></span><span id="phasetxt">连接中…</span></span>
    <button id="fsbtn" onclick="toggleFs()" title="全屏">⛶</button>
  </div>
  <div id="taskline">正在获取状态…</div>
  <div id="badges"></div>
</header>
<main id="feed"><div id="hint"><span class="big">🛰️</span>连接中…</div></main>
<button id="topbtn" onclick="document.getElementById('feed').scrollTo({top:0,behavior:'smooth'})">↑</button>
<footer>
  <div id="inrow">
    <button class="iconbtn" title="拍照给 claude 看" onclick="pick('photo')">📷</button>
    <button class="iconbtn" title="发送文件" onclick="pick('file')">📎</button>
    <input id="txt" placeholder="发任务或命令（等同钉钉里输入）" autocomplete="off">
    <button id="send">发送</button>
  </div>
  <div id="quick">
    <button data-a="sheet">🗂 会话</button>
    <button data-a="new">🆕 新会话</button>
    <button data-q="/status">📊 状态</button>
    <button data-q="/tail">📋 最新输出</button>
    <button data-a="sheet2">🛡 模式</button>
    <button data-q="/live">🖥 过程窗口</button>
    <button data-q="/stop" class="danger">🛑 停止</button>
  </div>
  <input type="file" id="fphoto" accept="image/*" capture="environment" hidden>
  <input type="file" id="ffile" hidden>
</footer>

<div id="mask" onclick="closeSheet()"></div>
<div id="sheet">
  <h4 id="sheetTitle"></h4>
  <div class="sub" id="sheetSub">点选项即切换，效果等同在钉钉里发对应命令</div>
  <div id="sheetBody"></div>
</div>
<div id="uploading"><div class="spin"></div><div id="upmsg">正在上传…</div></div>

<script>
const qs=new URLSearchParams(location.search);
let TOK=qs.get('token')||localStorage.getItem('panel_token')||'';
if(qs.get('token')){localStorage.setItem('panel_token',TOK);
  history.replaceState(null,'',location.pathname);}
let ST=null; // 最近一次状态快照

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

/* ---------- 底部抽屉 ---------- */
function openSheet(title,items){
  document.getElementById('sheetTitle').textContent=title;
  const body=document.getElementById('sheetBody');
  body.innerHTML=items.map((it,i)=>
    it.html
    ?'<div class="optinfo">'+it.html+'</div>'
    :'<button class="opt'+(it.newest?' new':'')+'" data-i="'+i+'">'
    +(it.icon?it.icon+' ':'')+esc(it.label)
    +(it.desc?'<small>'+esc(it.desc)+'</small>':'')
    +(it.cur?'<span class="chk">✓ 当前</span>':'')
    +'</button>').join('');
  body.querySelectorAll('.opt').forEach(btn=>btn.onclick=()=>{
    const it=items[+btn.dataset.i]; closeSheet(); post(it.cmd);
  });
  document.getElementById('mask').classList.add('show');
  document.getElementById('sheet').classList.add('show');
}
function closeSheet(){
  document.getElementById('mask').classList.remove('show');
  document.getElementById('sheet').classList.remove('show');
}
function sheetSession(){
  if(!ST)return;
  const items=(ST.sessions||[]).map(n=>({label:n,cur:n===ST.session,icon:'🗂',cmd:'/s '+n}));
  items.push({label:'新建/清空当前会话',icon:'🆕',newest:true,desc:'新起一条独立记忆的对话',cmd:'/new'});
  openSheet('切换会话',items);
}
function sheetModel(){
  if(!ST)return;
  const cur=ST.model||'';
  const items=[{label:'claude 默认',cur:!cur,icon:'🧠',cmd:'/model 0'}];
  (ST.models||[]).forEach((m,i)=>items.push({label:m,cur:cur===m,icon:'🧠',cmd:'/model '+(i+1)}));
  openSheet('切换模型',items);
}
function sheetMode(){
  if(!ST)return;
  const order=Object.keys(ST.modes||{});
  const items=[{label:'default（默认：白名单外发手机审批）',cur:ST.mode==='default',icon:'🛡',cmd:'/mode 0'}];
  order.forEach((k,i)=>items.push({label:k+' —— '+(ST.modes[k]||''),cur:ST.mode===k,icon:'🛡',cmd:'/mode '+(i+1)}));
  openSheet('切换权限模式',items);
}

/* ---------- 上传 ---------- */
function pick(kind){
  (kind==='photo'?document.getElementById('fphoto'):document.getElementById('ffile')).click();
}
async function upload(file){
  if(!file)return;
  const up=document.getElementById('uploading');
  document.getElementById('upmsg').textContent='正在上传 '+file.name+'…';
  up.style.display='flex';
  try{
    const fd=new FormData(); fd.append('file',file);
    const r=await fetch('/api/media',{method:'POST',headers:{'X-Panel-Token':TOK},body:fd});
    const j=await r.json().catch(()=>({}));
    if(!r.ok||j.err)alert('上传失败：'+(j.err||r.status));
  }catch(e){alert('网络错误：'+e);}
  up.style.display='none';
  setTimeout(poll,400);
}
document.getElementById('fphoto').addEventListener('change',e=>{upload(e.target.files[0]);e.target.value='';});
document.getElementById('ffile').addEventListener('change',e=>{upload(e.target.files[0]);e.target.value='';});

/* ---------- 渲染 ---------- */
function fmtTok(n){n=+n||0;return n>=100000?(Math.round(n/1000)+'k'):(n>=1000?((n/1000).toFixed(1)+'k'):(''+n));}
function usageBadge(u,title){
  if(!u||(!u.out&&!u.in))return '';
  let s='🪙';
  if(u.out)s+=' ↑'+fmtTok(u.out);
  if(u.in)s+=' ↓'+fmtTok(u.in);
  if(u.cache)s+=' ⚡'+fmtTok(u.cache);
  if(u.cost)s+=' · $'+(+u.cost).toFixed(4);
  return '<span class="badge w click" onclick="sheetUsage()" title="'+title+'">'+s+'</span>';
}
function sheetUsage(){
  if(!ST)return;
  const a=ST.usage_all||{},t=ST.usage||{};
  const row=(l,v)=>'<div style="display:flex;justify-content:space-between;padding:3px 0">'
    +'<span style="color:var(--dim)">'+l+'</span><b>'+v+'</b></div>';
  const sec=(title,u)=>{
    if(!u||(!u.out&&!u.in&&!u.turns))return '<div style="color:var(--faint);padding:4px 0">'+title+'：暂无</div>';
    return '<h4>'+title+'</h4>'+row('输出 tokens',fmtTok(u.out||0))+row('输入 tokens',fmtTok(u.in||0))
      +row('缓存命中',fmtTok(u.cache||0))+(u.turns?row('API 轮次',u.turns):'')
      +(u.cost?row('费用','$'+(+u.cost).toFixed(4)):'');
  };
  openSheet('🪙 Token 消耗',[{html:sec('本任务',t)+'</div><div>'+sec('历史累计（'+Math.round(a.tasks||0)+' 个任务）',a)}]);
}
function render(d){
  ST=d;
  const run=!!d.task;
  document.getElementById('dot').className='dot '+(run?'run':(d.approval?'ok':''));
  const ph=run?((d.task.phase||'运行中')+' · '+d.task.mins+'分'):(d.approval?'等你批复':'空闲');
  document.getElementById('phasetxt').textContent=ph;
  document.title=run?('▶ '+ph):'claude 遥控';
  document.getElementById('taskline').innerHTML=run
    ?('🏃 <b>'+esc(d.task.title)+'</b>')
    :'💤 当前没有正在执行的任务';
  const badges=[
    '<span class="badge click" onclick="sheetSession()">🗂 会话 '+esc(d.session)+' <span class="ar">▼</span></span>',
    '<span class="badge click" onclick="sheetModel()">🧠 模型 '+(esc(d.model)||'默认')+' <span class="ar">▼</span></span>',
    '<span class="badge click" onclick="sheetMode()">🛡 模式 '+esc(d.mode)+' <span class="ar">▼</span></span>',
    usageBadge(d.usage_all,'历史累计 token 消耗，点看明细')
  ];
  if(d.usage&&d.usage.out)badges.push(usageBadge(d.usage,'本任务消耗，点看明细').replace('class="badge w click"','class="badge click"'));
  if(d.queue>0)badges.push('<span class="badge w">排队 '+d.queue+'</span>');
  if(!d.dingtalk_ok)badges.push('<span class="badge w">⚠ 手机未发过消息，暂不能发指令</span>');
  document.getElementById('badges').innerHTML=badges.join('');

  let h='';
  if(d.approval){
    h+='<div class="card appr"><h4>⏳【审批 '+d.approval.seq+'】等你批复</h4>'
      +'<div class="desc">'+esc(d.approval.desc)+'</div>'
      +'<div class="row">'
      +'<button class="b b-ok" onclick="appr(1)">✓ 允许</button>'
      +'<button class="b b-no" onclick="appr(2)">✗ 拒绝</button>'
      +'</div><div class="row">'
      +'<button class="b b-all" style="flex:0 0 100%" onclick="appr(4)">✓✓ 允许且本任务内同类不再问</button>'
      +'</div><div class="row">'
      +'<input id="guide" placeholder="拒绝并给指示，如：改用 python">'
      +'<button class="b b-gray" style="flex:0 0 auto;min-width:92px" onclick="appr3()">转达指示</button>'
      +'</div></div>';
  }
  const rows=(d.replies||[]).slice(-30).map(r=>
    '<div class="msg"><span class="t">'+esc(r.t)+'</span>'+esc(r.text)+'</div>').join('');
  const chat=rows
    ?('<div class="card"><h4>💬 机器人回复（镜像钉钉）</h4>'+rows+'</div>')
    :'';
  // 思维链（Claude 风格）：思考块可折叠——最新一块在思考时自动展开
  const tr=d.trace||[];
  let live='';
  if(tr.length){
    const thinking=run&&/思考/.test((d.task&&d.task.phase)||'');
    live='<div class="card"><h4>🧠 思维链（点折叠块展开）</h4>'+tr.map((e,i)=>{
      const last=i===tr.length-1;
      const chip=(e.u&&e.u.out)?'<span class="tk">+'+fmtTok(e.u.out)+'</span>':'';
      if(e.k==='think'){
        return '<div class="thk'+(last&&thinking?' open':'')+'">'
          +'<div class="th"><span class="ar">▶</span>思考'+chip+'</div>'
          +'<div class="tb">'+esc(e.text)+'</div></div>';
      }
      if(e.k==='tool'){
        const one=e.text.indexOf('\\n')<0&&e.text.length<=56;
        return '<div class="tlk"><div class="th"><span class="ar">▶</span>🔧 '+esc(e.name)
          +(one?' <code>'+esc(e.text)+'</code>':'')+chip+'</div>'
          +'<div class="tb"><code>'+esc(e.text)+'</code></div></div>';
      }
      if(e.k==='ask'){
        return '<div class="askline">⏳ 等审批：'+esc(e.text)+'</div>';
      }
      return '<div class="msg" style="margin-bottom:8px">'+esc(e.text)+chip+'</div>';
    }).join('')+'</div>';
  }
  document.getElementById('feed').innerHTML=h+chat+live
    || '<div id="hint"><span class="big">🛰️</span>还没有消息<br>在下方输入任务开始<br>或先用手机钉钉发一条消息</div>';
  const m=document.getElementById('feed');
  const nearBottom=m.scrollHeight-m.scrollTop-m.clientHeight<180;
  if(nearBottom||d.approval)m.scrollTop=m.scrollHeight;
  document.getElementById('topbtn').style.display=
    (m.scrollHeight-m.scrollTop-m.clientHeight>400)?'block':'none';
}

/* ---------- 全屏（App 观感：隐藏地址栏等浏览器框） ---------- */
function toggleFs(){
  const d=document;
  if(d.fullscreenElement||d.webkitFullscreenElement){
    (d.exitFullscreen||d.webkitExitFullscreen).call(d);return;}
  const el=d.documentElement;
  const go=el.requestFullscreen||el.webkitRequestFullscreen;
  if(go)go.call(el);
}
document.addEventListener('fullscreenchange',()=>{
  document.getElementById('fsbtn').style.color=
    document.fullscreenElement?'var(--acc)':'';
});

let failCnt=0;
async function poll(){
  try{
    if(!TOK){
      TOK=prompt('首次使用，请粘贴面板令牌（token）。\\n在电脑上打开 config.json，复制 panel_token 的值：')||'';
      if(!TOK){document.getElementById('feed').innerHTML=
        '<div id="hint"><span class="big">🔑</span>未填令牌，刷新页面可重新输入</div>';return;}
      localStorage.setItem('panel_token',TOK);
    }
    const ctl=new AbortController();
    const timer=setTimeout(()=>ctl.abort(),4000);   // 4 秒无响应视为离线（中继半死时 fetch 会无限挂起）
    const r=await fetch('/api/state',{headers:{'X-Panel-Token':TOK},signal:ctl.signal});
    clearTimeout(timer);
    if(r.status===403){
      localStorage.removeItem('panel_token');TOK='';
      document.getElementById('feed').innerHTML=
        '<div id="hint"><span class="big">🔒</span>令牌不对，已清除<br>刷新页面可重新输入</div>';
      document.getElementById('phasetxt').textContent='令牌错误';return;}
    failCnt=0;
    render(await r.json());
  }catch(e){
    // 连续失败：大概率是手机 Tailscale 掉线或电脑关机，明确提示而不是干等
    if(++failCnt===3){
      document.getElementById('feed').innerHTML=
        '<div id="hint"><span class="big">📡</span>连不上电脑<br>请检查手机 Tailscale 是否开启（Connected）<br>电脑需开机且机器人黑窗在运行<br><small>恢复后本页会自动连上</small></div>';
      document.getElementById('phasetxt').textContent='离线';
    }
  }
}
async function post(text){
  try{
    const r=await fetch('/api/send',{method:'POST',
      headers:{'Content-Type':'application/json','X-Panel-Token':TOK},
      body:JSON.stringify({text})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok||j.err){alert('发送失败：'+(j.err||r.status));}
  }catch(e){alert('网络错误：'+e);}
  setTimeout(poll,400);
}
function appr(n){post(String(n));}
function appr3(){const g=document.getElementById('guide');
  const t=(g.value||'').trim(); if(!t){g.focus();return;}
  g.value=''; post('3 '+t);}
function send(){const el=document.getElementById('txt');
  const t=el.value.trim(); if(!t)return; el.value=''; post(t);}
document.getElementById('send').onclick=send;
document.getElementById('txt').addEventListener('keydown',e=>{if(e.key==='Enter')send();});
document.querySelectorAll('#quick button').forEach(b=>b.onclick=()=>{
  if(b.dataset.q)post(b.dataset.q);
  else if(b.dataset.a==='sheet')sheetSession();
  else if(b.dataset.a==='new')post('/new');
  else if(b.dataset.a==='sheet2')sheetMode();
});
poll();setInterval(poll,2000);
</script>
</body></html>"""
