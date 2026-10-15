# -*- coding: utf-8 -*-
"""
手机浏览器浏览电脑文件的服务器（C 盘以外的所有盘，含外接盘）。

启动后手机浏览器打开终端里显示的网址：
  首页列出所有可访问的盘（C 盘不开放），
  点进去像网盘一样浏览目录、在线看文本/图片、下载文件。
外接盘插上后刷新首页即可看到，无需改配置。
"""
import json
import socket
import string
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
PORT = int(cfg.get("file_server_port", 8899))


def drives() -> list[str]:
    """可访问的盘符（C 盘除外）。"""
    out = []
    for letter in string.ascii_uppercase:
        if letter == "C":
            continue
        try:
            if Path(f"{letter}:/").exists():
                out.append(letter)
        except OSError:
            continue
    return out


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class MultiDriveHandler(SimpleHTTPRequestHandler):
    """把 /D/xxx 映射到 D:/xxx，/E/xxx 映射到 E:/xxx。"""

    def translate_path(self, path: str) -> str:
        p = urllib.parse.urlparse(path).path
        parts = p.lstrip("/").split("/", 1)
        if parts and len(parts[0]) == 1 and parts[0].isalpha():
            drive = parts[0].upper()
            rest = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
            if drive == "C":  # C 盘拒绝访问
                return str(BASE_DIR / "__forbidden__" / rest)
            return f"{drive}:/{rest}"
        return super().translate_path(path)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path in ("/", ""):
            self.send_drive_index()
            return
        super().do_GET()

    def send_drive_index(self):
        items = "".join(
            f'<li style="margin:10px 0"><a href="/{d}/" '
            f'style="font-size:20px">{d}:</a></li>'
            for d in drives()
        )
        body = (
            "<html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>电脑文件</title></head><body>"
            f"<h2>📀 选择要浏览的盘</h2><ul>{items}</ul>"
            "<p>（C 盘未开放；外接盘插上后刷新本页）</p></body></html>"
        )
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ip = lan_ip()
    print("=" * 52)
    print(" 手机浏览器访问下面这个网址：")
    print()
    print(f"     http://{ip}:{PORT}")
    print()
    print(f" 可浏览的盘：{', '.join(d + ':' for d in drives())}（C 盘除外）")
    print(" ⚠️ 同一 WiFi 内的设备都能访问，用完请关闭本窗口")
    print(" 按 Ctrl+C 停止")
    print("=" * 52)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), MultiDriveHandler).serve_forever()
    except KeyboardInterrupt:
        print("已停止")
