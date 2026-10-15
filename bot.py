# -*- coding: utf-8 -*-
"""
钉钉遥控 Claude Code 机器人

手机钉钉发消息 -> 本机 `claude -p` 执行 -> 结果发回手机
使用钉钉 Stream 长连接模式：无需公网 IP、无需端口转发。

用法:
    python bot.py            启动机器人（需先在 config.json 填好 app_key / app_secret）
    python bot.py --check    自检：配置、claude 命令、白名单设置
    python bot.py --test "任务描述"
                             本地试跑一次 claude -p 管线（不走钉钉），用于验证权限配置
"""
import argparse
import asyncio
import copy
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
import dingtalk_stream
from dingtalk_stream import AckMessage

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SETTINGS_PATH = BASE_DIR / "bot_settings.json"
LOG_PATH = BASE_DIR / "bot.log"

# ---------------- 日志 ----------------
log = logging.getLogger("dingbot")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
def _rotate_log(keep_bytes: int = 5 * 1024 * 1024) -> None:
    """启动时把过大的 bot.log 挪成 bot.old.log（只留一代），防止无限增长。"""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > keep_bytes:
            old = LOG_PATH.with_suffix(".old.log")
            old.unlink(missing_ok=True)
            LOG_PATH.rename(old)
    except Exception:
        pass  # 已有实例占着文件时挪不动，无妨（双开本来就不允许）


_rotate_log()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def _fatal_hook(tp, v, tb):
    """pythonw 下 stdout/stderr 是 None，裸异常会无声蒸发——留个尸检记录。"""
    log.critical("进程级未捕获异常", exc_info=(tp, v, tb))


sys.excepthook = _fatal_hook


def _fatal_hook_threading(a):
    log.critical("线程未捕获异常", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


threading.excepthook = _fatal_hook_threading

# ---------------- 权限白名单 ----------------
# 允许机器人里的 claude 执行的命令（前缀匹配）。按需增删。
# 注意：Windows 上 shell 工具叫 PowerShell，Linux/Mac 下叫 Bash，
# 规则必须同时覆盖两个命名空间，否则形同虚设。
BASH_ALLOW = [
    "git status", "git diff", "git log", "git show", "git branch",
    "git add", "git commit", "git push", "git pull", "git fetch",
    "git checkout", "git stash", "git restore",
    "python", "python -m", "pip", "pytest",
    "node", "npm run", "npm install", "npm test", "npx",
    "ls", "cat", "grep", "find", "head", "tail", "wc",
    "dir", "type", "where", "findstr", "Get-ChildItem", "Get-Content",
]
# 无论怎么配都禁止的高危命令（deny 优先级高于 allow）
BASH_DENY = [
    "rm", "rmdir", "rd", "del", "erase", "ri", "Remove-Item",
    "format", "shutdown", "taskkill", "Stop-Process", "Stop-Computer",
    "Stop-Service", "Clear-Content", "Set-ItemProperty",
    "git push --force", "git reset --hard", "git clean",
    "git checkout --", "mkfs", "dd",
]

def _shell_rules(cmds):
    """生成同时覆盖 Bash 和 PowerShell 工具的权限规则。"""
    return [f"{tool}({c}:*)" for tool in ("Bash", "PowerShell") for c in cmds]

SAFE_TOOLS = (
    _shell_rules(BASH_ALLOW)
    + ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"]
)
DENY_TOOLS = _shell_rules(BASH_DENY)

HELP_TEXT = (
    "🤖 我是跑在你电脑上的 Claude Code，使用方法：\n\n"
    "直接发文字 = 派任务（我会读写工作目录里的文件、执行白名单内的命令）\n"
    "发图片/文件 = 我直接查看内容（截图、报错截图、PDF、docx 都行）\n"
    "连续发消息 = 自动接着上一条对话继续（我能记住上下文）\n\n"
    "命令：\n"
    "/s 会话名 —— 切换/新建分类会话（如 /s 项目A、/s 求职），不同工作互不干扰\n"
    "/s —— 列出所有会话；/s 删 名称 —— 删除会话\n"
    "/new 新会话 —— 清空当前会话，重新开始\n"
    "/交接 —— 会话太长时收尾：本会话把要点写进 记忆/<会话名>.md 后清空重开，下一条消息自动读记忆接续\n"
    "/stop（或 停止/中断/退出）—— 紧急中断：终止正在跑的任务并清空排队的任务（发错了随时用）\n"
    "任务遇到需要批准的操作时，会发来询问（带序号）：回 1 允许 / 2 拒绝 / 3 你的指示 / 4 同类不再问 / 5 本任务后续全部自动放行\n"
    "/status 状态 —— 查看队列、正在跑的任务和当前会话\n"
    "/live —— 开/关电脑上的实时过程窗口（弹窗显示手机任务的思考过程；关掉窗口也会自动停用）\n"
    "/mode —— 查看/切换权限模式（/mode 0 默认审批 / 1 自动编辑 / 2 全自动）\n"
    "/model —— 查看/切换模型（/model 0 恢复默认 / 序号或直接发模型名）\n"
    "/tail —— 查看正在运行的任务最近输出（思考/工具/回复）\n"
    "/usage 消耗 —— 查看 token 消耗（本任务与历史累计）\n"
    "/ls 路径 —— 看电脑文件夹（支持各盘：/ls、/ls D:/、/ls E:/资料）\n"
    "/file 文件路径 —— 把电脑文件发到聊天（≤20MB，C 盘外均可）\n"
    "任务里提到 PDF 文件会自动把文件发进聊天，手机直接看\n"
    "/help 帮助 —— 显示本说明"
)

# 交接模式：会话太长（变慢、变贵、早期细节被压缩掉）时，把上下文沉淀成记忆文件再重开。
# 旧会话写（它有全部上下文）→ 清空会话 → 新会话第一条任务自动先读记忆文件。
HANDOFF_WRITE_PROMPT = (
    "【会话交接】本会话即将收尾归档，这是一次纯整理任务，不要执行其他操作。\n"
    "请把这次会话的关键信息写入记忆文件 `{mem}`（Markdown 中文），包含：\n"
    "1. 这个会话在做什么（目标/背景）\n"
    "2. 已完成的重要事项与关键结论\n"
    "3. 当前进行到哪、有什么未完成（下一步）\n"
    "4. 关键文件/目录/命令/参数\n"
    "5. 给下一轮会话的注意事项\n"
    "若文件已存在，在其基础上更新合并（保留仍有效的，删掉过时的）。写完只回复：已写入记忆。"
)
PICKUP_PREFIX = (
    "【会话接续】这是『{sname}』会话新一轮的开始。先读记忆文件 `{mem}` 了解此前的工作背景，"
    "再处理下面的任务；记忆与任务冲突时以任务为准。\n\n"
)


def write_settings_file() -> Path:
    """生成传给 claude -p 的权限配置文件（只对机器人派生的任务生效）。

    defaultMode 取 config.json 的 permission_mode（/mode 命令可切换）：
    - default: 白名单外的操作发手机审批（最稳，默认）
    - acceptEdits: 文件编辑自动放行，其余仍发手机审批
    - bypass: 白名单外不再询问、直接执行（BASH_DENY 硬拦截仍生效！）
    """
    mode = (load_config().get("permission_mode") or "default").strip()
    data = {
        "permissions": {
            "defaultMode": {"default": "default",
                            "acceptEdits": "acceptEdits",
                            "bypass": "bypassPermissions"}.get(mode, "default"),
            "allow": SAFE_TOOLS,
            "deny": DENY_TOOLS,
            # 让任务也能读写 C 盘以外的盘（含外接盘，每次任务时动态探测）
            "additionalDirectories": [f"{d}:/" for d in shared_drives()],
        }
    }
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return SETTINGS_PATH


PERMISSION_MODES = {
    "default": "默认：白名单外的操作发手机审批（最稳）",
    "acceptEdits": "自动接受文件编辑，其余仍发手机审批",
    "bypass": "全自动：不再发审批询问，直接执行（rm 等硬拦截仍生效）",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"❌ 找不到配置文件 {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return cfg


def find_claude() -> str | None:
    return shutil.which("claude")


def approval_desc(request: dict) -> str:
    """把权限请求压缩成一行人话，方便发到手机上看（太长没法读，也没必要）。"""
    tool = request.get("tool_name") or request.get("display_name") or "工具"
    inp = request.get("input") or {}
    if isinstance(inp, dict):
        if "command" in inp:
            s = str(inp["command"])
        elif "file_path" in inp:
            s = str(inp["file_path"])
        elif "pattern" in inp:
            s = str(inp["pattern"])
        else:
            s = json.dumps(inp, ensure_ascii=False)
    else:
        s = str(inp)
    s = re.sub(r"\s+", " ", s).strip()  # 多行命令压成一行
    if len(s) > 80:  # 手机上能看懂是啥操作就行，细节不用全给
        s = s[:80] + "…"
    return f"{tool}: {s}"


def cleanup_live_dir(keep_days: int = 3) -> None:
    """清理过期的实时过程记录文件（每个手机任务一个，跑完就没用了）。"""
    d = BASE_DIR / "live"
    if not d.exists():
        return
    cutoff = time.time() - keep_days * 86400
    for f in d.glob("*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def cleanup_incoming_dir(keep_days: int = 7) -> None:
    """清理过期的 incoming 媒体（手机/面板发来的图片文件，claude 看完就没用了）。"""
    d = BASE_DIR / "incoming"
    if not d.exists():
        return
    cutoff = time.time() - keep_days * 86400
    for f in d.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def open_live_window(live_path: Path, title: str):
    """开一个 PowerShell 窗口实时滚动显示手机任务的执行过程。

    原理：任务过程写入 live_path（UTF-8 文本），窗口里 Get-Content -Wait
    持续 tail。返回 Popen 句柄：进程退出（poll() 非 None）＝窗口被人关了。
    """
    ps = (
        f"$host.UI.RawUI.WindowTitle='手机任务·{title}';"
        f"Get-Content -Wait -Encoding UTF8 '{live_path}'"
    )
    try:
        return subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception:
        log.exception("打开实时过程窗口失败")
        return None


def live_write_event(write, d: dict):
    """把 stream-json 事件压缩成几行人话写进实时过程窗口。

    返回 (状态词, 行列表)：状态词供 /status 显示当前阶段，
    行列表供 /tail 在手机上回看（live 窗口没开时也照常收集）。
    """
    rows = []

    def emit(s: str):
        rows.append(s)
        write(s)

    t = d.get("type")
    if t == "assistant":
        phase = None
        for b in (d.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                s = (b.get("thinking") or "").strip()
                if s:
                    emit("[思考] " + s)
                    phase = "🤔 思考中"
            elif bt == "text":
                s = (b.get("text") or "").strip()
                if s:
                    emit("[回复] " + s)
                    phase = "✍️ 生成回复中"
            elif bt == "tool_use":
                inp = json.dumps(b.get("input") or {}, ensure_ascii=False)
                emit(f"[工具] {b.get('name', '')} {inp[:200]}")
                phase = f"🔧 正在用 {b.get('name', '工具')}"
        return phase, rows
    if t == "control_request":
        req = d.get("request") or {}
        if req.get("subtype") == "can_use_tool":
            emit(f"[等审批] {approval_desc(req)}")
            return "⏳ 等你审批（回 1/2/3/4/5）", rows
    return None, rows


def record_trace(cur: dict, d: dict):
    """把 stream-json 事件折叠成面板可渲染的思维链（cur["trace"]），
    并按 API 轮次累计 token 消耗（cur["usage"]）。live_write_event 的结构化版。

    trace 条目：{"k": think/text/tool/ask, "text": …, "name": 工具名,
                "u": {"in": 输入, "out": 输出, "cache": 缓存读} 或 None}
    usage：{"in","out","cache","turns","cost"}（cost 仅 result 事件给出时存在）
    """
    t = d.get("type")
    if t == "assistant":
        msg = d.get("message") or {}
        usage = msg.get("usage") or {}
        turn = None
        if usage.get("output_tokens") or usage.get("input_tokens"):
            out = int(usage.get("output_tokens") or 0)
            tin = (int(usage.get("input_tokens") or 0)
                   + int(usage.get("cache_creation_input_tokens") or 0))
            cread = int(usage.get("cache_read_input_tokens") or 0)
            u = cur.setdefault("usage", {"in": 0, "out": 0, "cache": 0, "turns": 0})
            u["out"] += out
            u["in"] += tin
            u["cache"] += cread
            u["turns"] += 1
            turn = {"in": tin, "out": out, "cache": cread}
        trace = cur.setdefault("trace", [])
        for b in (msg.get("content") or []):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                s = (b.get("thinking") or "").strip()
                if s:
                    trace.append({"k": "think", "text": s[:3000], "u": turn})
            elif bt == "text":
                s = (b.get("text") or "").strip()
                if s:
                    trace.append({"k": "text", "text": s[:3000], "u": turn})
            elif bt == "tool_use":
                inp = b.get("input") or {}
                brief = (inp.get("command") or inp.get("file_path")
                         or inp.get("pattern") or inp.get("url")
                         or inp.get("path") or "")
                if not brief:
                    try:
                        brief = json.dumps(inp, ensure_ascii=False)
                    except Exception:
                        brief = str(inp)
                trace.append({"k": "tool", "name": b.get("name", "tool"),
                              "text": str(brief)[:3000], "u": turn})
        del trace[:-200]
    elif t == "control_request":
        req = d.get("request") or {}
        if req.get("subtype") == "can_use_tool":
            cur.setdefault("trace", []).append(
                {"k": "ask", "text": approval_desc(req), "u": None})
    elif t == "result":
        cost = d.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            cur.setdefault("usage", {"in": 0, "out": 0, "cache": 0,
                                     "turns": 0})["cost"] = round(cost, 4)


async def run_claude_stream(cfg: dict, text: str, session_id: str | None,
                            on_approval=None, on_proc=None,
                            on_event=None) -> dict:
    """以 stream-json 双向模式跑一次 claude -p。

    与旧的"一次性 json 输出"不同，这条管线是全双工的：
    任务通过 stdin 送入，claude 遇到需要批准的操作时会在 stdout 吐出
    control_request 事件，把审批决定写回 stdin 即可放行/拒绝（实测 2.1.160）。
    这就是"手机上回 1/2/3 审批"的技术底座。

    on_approval(request) -> {"behavior": "allow", "updatedInput": ...}
                           或 {"behavior": "deny", "message": "..."}
    不传 on_approval 时一律自动拒绝（无人值守的保守行为）。
    on_proc(proc) 进程启动后回调（/stop 靠它拿到进程句柄）。

    返回 dict：result / session_id / is_error / models / ok / err /
              timeout / timeout_minutes
    """
    res = {"result": "", "session_id": session_id, "is_error": False,
           "models": "", "ok": True, "err": "", "timeout": False,
           "timeout_minutes": float(cfg.get("task_timeout_minutes", 30))}
    claude = find_claude()
    if not claude:
        res["ok"] = False
        res["err"] = "找不到 claude 命令，请确认本机已安装 Claude Code"
        return res
    workdir = cfg.get("work_dir") or str(BASE_DIR)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    cmd = [
        claude, "-p",
        "--output-format", "stream-json", "--input-format", "stream-json",
        "--verbose",
        "--permission-mode", "default",
        "--permission-prompt-tool", "stdio",
        "--settings", str(write_settings_file()),
    ]
    model = (cfg.get("model") or "").strip()
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=workdir, env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,  # stream-json 单行事件可能很大，默认 64KB 会把 readline 撑爆
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if on_proc:
        try:
            on_proc(proc)
        except Exception:
            pass
    stderr_tail: list[str] = []  # 保留最后若干行，出错时排障

    async def _drain_stderr():
        while True:
            try:
                line = await proc.stderr.readline()
            except ValueError:  # 单行超长，丢弃这坨继续
                try:
                    await proc.stderr.read(1 << 20)
                except Exception:
                    return
                continue
            if not line:
                break
            stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            del stderr_tail[:-50]

    drain = asyncio.create_task(_drain_stderr())
    deadline = asyncio.get_running_loop().time() + res["timeout_minutes"] * 60
    try:
        first = {"type": "user",
                 "message": {"role": "user",
                             "content": [{"type": "text", "text": text + FILE_SEND_PROTOCOL}]}}
        proc.stdin.write((json.dumps(first) + "\n").encode())
        await proc.stdin.drain()
        while True:
            remain = deadline - asyncio.get_running_loop().time()
            if remain <= 0:
                res["timeout"] = True
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remain)
            except asyncio.TimeoutError:
                res["timeout"] = True
                break
            except ValueError:
                # 单行超长（已放宽到 8MB，极端情况兜底）：丢弃这块继续读
                try:
                    await proc.stdout.read(1 << 20)
                except Exception:
                    pass
                continue
            if not line:
                break  # 进程退出
            try:
                d = json.loads(line)
            except Exception:
                continue
            if on_event:
                try:
                    on_event(d)
                except Exception:
                    pass
            t = d.get("type")
            if t == "control_request" and (d.get("request") or {}).get("subtype") == "can_use_tool":
                decision = None
                if on_approval:
                    try:
                        decision = await on_approval(d.get("request") or {},
                                                    d.get("request_id"))
                    except Exception:
                        log.exception("审批回调出错，按拒绝处理")
                if not isinstance(decision, dict):
                    decision = {"behavior": "deny", "message": "无人值守，自动拒绝"}
                # 应答信封格式照抄官方 claude-agent-sdk 的 query.py：
                # subtype=success + 决定放在嵌套 response 里，缺一层都会 ZodError
                resp = {"type": "control_response",
                        "response": {"subtype": "success",
                                     "request_id": d.get("request_id"),
                                     "response": decision}}
                try:
                    proc.stdin.write((json.dumps(resp) + "\n").encode())
                    await proc.stdin.drain()
                except Exception:
                    res["ok"] = False
                    res["err"] = "向 claude 发送审批结果失败，进程可能已退出"
                    break
            elif t == "system" and d.get("subtype") == "init":
                res["session_id"] = d.get("session_id") or res["session_id"]
            elif t == "result":
                res["result"] = (d.get("result") or "").strip()
                res["is_error"] = bool(d.get("is_error"))
                res["models"] = ",".join((d.get("modelUsage") or {}).keys())
                res["session_id"] = d.get("session_id") or res["session_id"]
                break
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                kill_tree(proc)
        drain.cancel()
        try:
            await drain
        except BaseException:
            pass
        if res["timeout"]:
            res["ok"] = False
        elif not res["result"] and res["ok"] and proc.returncode not in (0, None):
            res["ok"] = False
            tail = "\n".join(stderr_tail[-10:])
            res["err"] = f"claude 异常退出（码 {proc.returncode}），未返回结果"
            if tail:
                res["err"] += f"\n{tail[-800:]}"
    return res


def chunk_text(s: str, size: int = 2800, max_chunks: int = 6) -> list[str]:
    """把长文本按钉钉能接受的长度切块（尽量在换行处切）。"""
    parts: list[str] = []
    while s and len(parts) < max_chunks:
        if len(s) <= size:
            parts.append(s)
            s = ""
            break
        cut = s.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(s[:cut])
        s = s[cut:].lstrip("\n")
    if s:  # 真正剩余没发出去的内容才提示截断
        parts[-1] += f"\n\n…（内容过长已截断，完整内容见 {LOG_PATH}）"
    return parts


def fmt_elapsed(mins: float) -> str:
    return f"{mins:.0f}分钟" if mins >= 1 else f"{mins * 60:.0f}秒"


# 任务文本里提到这些后缀的文件时，自动把文件发到手机聊天（钉钉可原生预览 PDF）。
# 想扩展（如 .docx）往集合里加后缀即可。
AUTO_SEND_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
                  ".txt", ".md"}

# claude 主动发文件的约定：任务消息尾部告知协议，回复里出现标记时 bot 负责推送
SEND_FILE_RE = re.compile(r"\[SEND_FILE[:：]\s*([^\]]+)\]")
FILE_SEND_PROTOCOL = (
    "\n\n[系统约定]当用户要你把电脑上的文件发给他（说\"发给我/发送/传给我\"等），"
    "在最终回复的末尾为每个文件单独写一行：[SEND_FILE:文件的完整路径]。"
    "要求：文件真实存在、不在 C 盘、不超过 20MB。其余情况不要使用该标记。"
)
_PATH_TOKEN_RE = re.compile(r"[^\s，。；、！？（()）【】\[\]\"']+")


def extract_sendable_files(cfg: dict, text: str) -> list[Path]:
    """从任务文本里抽出提到的、真实存在的可自动发送文件（去重、按出现顺序）。"""
    out: list[Path] = []
    seen: set[str] = set()
    for tok in _PATH_TOKEN_RE.findall(text):
        t = tok.strip(".,;:!?，。；：！？）)'\"”")
        if not t or Path(t).suffix.lower() not in AUTO_SEND_EXTS:
            continue
        p = resolve_allowed(cfg, t)
        if p is None or not p.is_file():
            continue
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# 审批答复的自然语言映射：手机上直接回"好/可以/继续"也等于按 1
APPROVE_WORDS = frozenset({
    "1", "y", "yes", "ok", "okay", "allow", "okk",
    "好", "好的", "好啊", "好吧", "行", "可以的", "可以", "同意", "允许", "批准",
    "通过", "继续", "是的", "对的", "对", "嗯", "嗯嗯", "没问题", "确认", "确定",
})
DENY_WORDS = frozenset({
    "2", "n", "no", "deny",
    "不", "不要", "不行", "不好", "不同意", "不批", "不批准", "不允许",
    "拒绝", "别", "算了", "取消", "否",
})
# 审批等待期间也放行的中文命令别名（斜杠命令统一按 "/" 前缀放行）
COMMAND_WORDS = frozenset({"停止", "退出", "中断", "状态", "帮助", "新会话"})


def approval_answer_kind(text: str) -> str | None:
    """把手机回复翻译成审批决定：
    'allow' / 'allow_all'(允许且同类不再问) / 'deny' / 'guide' / None（不是答复）。"""
    t = text.strip().lower().rstrip("。.!！？?～~ ")
    if t in APPROVE_WORDS:
        return "allow"
    if t == "4":
        return "allow_all"
    if t == "5":
        return "allow_task"
    if t in DENY_WORDS:
        return "deny"
    if t == "3" or t.startswith(("3 ", "3：", "3:")):
        return "guide"
    return None


def approval_key(request: dict) -> str:
    """审批请求的"同类"判据：工具名 + 命令首个词（文件类操作按工具名）。"""
    name = request.get("tool_name") or request.get("display_name") or "?"
    inp = request.get("input") or {}
    cmd = inp.get("command") if isinstance(inp, dict) else None
    if isinstance(cmd, str) and cmd.strip():
        return f"{name}:{cmd.strip().split()[0][:40]}"
    return name


# ---------------- 看目录 / 发文件 ----------------

_token_cache = {"token": None, "expire_at": 0.0}


def get_access_token(cfg: dict) -> str:
    """获取钉钉 API access_token（带缓存，提前 2 分钟过期）。"""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire_at"]:
        return _token_cache["token"]
    r = requests.get(
        "https://oapi.dingtalk.com/gettoken",
        params={"appkey": cfg["app_key"], "appsecret": cfg["app_secret"]},
        timeout=15,
    )
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"获取 access_token 失败: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expire_at"] = now + int(data.get("expires_in", 7200)) - 120
    return _token_cache["token"]


def upload_media(cfg: dict, path: Path) -> str:
    """上传文件到钉钉，返回 media_id（用于机器人发文件消息）。"""
    token = get_access_token(cfg)
    r = requests.post(
        f"https://oapi.dingtalk.com/media/upload?access_token={token}&type=file",
        files={"media": (path.name, open(path, "rb"))},
        timeout=300,
    )
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"上传文件失败: {data}")
    return data["media_id"]


def download_robot_media(cfg: dict, download_code: str) -> str:
    """用手机消息里的 downloadCode 换取图片/文件的临时下载链接。"""
    token = get_access_token(cfg)
    r = requests.post(
        "https://api.dingtalk.com/v1.0/robot/messageFiles/download",
        headers={"x-acs-dingtalk-access-token": token},
        json={"robotCode": cfg["app_key"], "downloadCode": download_code},
        timeout=30,
    )
    d = r.json() if "json" in r.headers.get("content-type", "") else {}
    url = d.get("downloadUrl")
    if not url:
        raise RuntimeError(f"换取下载链接失败: HTTP {r.status_code} {r.text[:150]}")
    return url


def workdir_root(cfg: dict) -> Path:
    return Path(cfg.get("work_dir") or BASE_DIR).resolve()


def shared_drives() -> list[str]:
    """当前可访问的盘符列表（C 盘除外，外接盘即插即用）。"""
    import string
    out = []
    for letter in string.ascii_uppercase:
        if letter == "C":
            continue
        p = Path(f"{letter}:/")
        try:
            if p.exists():
                out.append(letter)
        except OSError:
            continue
    return out


def resolve_allowed(cfg: dict, p: str) -> Path | None:
    """解析用户给的路径；C 盘一律拒绝，其他盘（含外接盘）放行。

    不带盘符的相对路径锚定在工作目录下。
    """
    base = workdir_root(cfg)
    s = p.strip()
    # "E:" / "E:xx" 这类盘符相对写法统一转成绝对形式，避免歧义
    if len(s) >= 2 and s[1] == ":":
        s = s[0] + ":/" + s[2:].lstrip("/\\")
    t = Path(s)
    if not t.is_absolute():
        t = base / t
    t = t.resolve()
    drive = (t.drive or "").upper().rstrip(":")
    if drive == "C":
        return None  # C 盘不开放
    if drive and not Path(f"{drive}:/").exists():
        return None  # 盘不存在（外接盘已拔出等）
    return t


# ---------------- 手机/电脑共享会话（多会话分类版） ----------------

SESSIONS_DIR = BASE_DIR / "sessions"
CURRENT_NAME_FILE = BASE_DIR / "current_session.txt"


def _safe_name(name: str) -> str:
    """会话名只保留安全字符，防止路径注入。"""
    name = re.sub(r'[\\/:*?"<>| ]', "_", name.strip())
    return name[:30]


def current_session_name() -> str:
    if CURRENT_NAME_FILE.exists():
        n = CURRENT_NAME_FILE.read_text(encoding="utf-8").strip()
        if n:
            return n
    return "默认"


def set_current_session_name(name: str) -> None:
    CURRENT_NAME_FILE.write_text(name, encoding="utf-8")


def session_file(name: str) -> Path:
    return SESSIONS_DIR / f"{_safe_name(name)}.txt"


def load_session_id(name: str | None = None) -> str | None:
    f = session_file(name or current_session_name())
    if f.exists():
        return f.read_text(encoding="utf-8").strip() or None
    return None


def save_session_id(name: str, sid: str) -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)
    session_file(name).write_text(sid, encoding="utf-8")
    set_session_title(sid, name)


def memory_file(name: str | None = None) -> Path:
    """分类会话的记忆文件：/交接 时旧会话写，新会话第一条任务自动先读。
    放在工作目录的 记忆/ 下，用户也能直接打开看、手动改。"""
    workdir = Path(load_config().get("work_dir") or BASE_DIR)
    d = workdir / "记忆"
    d.mkdir(exist_ok=True)
    return d / f"{_safe_name(name or current_session_name())}.md"


def set_session_title(sid: str, title: str, _done: set = set()) -> None:
    """给会话写自定义标题（等同在 claude 里敲 /rename）。

    做法是往 ~/.claude/projects/<项目>/<sid>.jsonl 追加一条 custom-title
    记录（格式照抄 claude 自己的写入逻辑）。效果：
    电脑上 claude --resume 列表显示会话名，终端/VSCode 标签页标题也是它。
    """
    if not sid or (sid, title) in _done:
        return
    _done.add((sid, title))
    projects = Path.home() / ".claude" / "projects"
    f = projects / "d---" / f"{sid}.jsonl"
    if not f.exists():
        for d in projects.iterdir():  # 工作目录变过时按内容定位
            if (d / f"{sid}.jsonl").exists():
                f = d / f"{sid}.jsonl"
                break
        else:
            return
    rec = {"type": "custom-title", "customTitle": title, "sessionId": sid,
           "uuid": str(uuid.uuid4()),
           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    try:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("写入会话标题失败 sid=%s", sid)


def list_sessions() -> list[str]:
    if not SESSIONS_DIR.exists():
        return []
    return sorted(p.stem for p in SESSIONS_DIR.glob("*.txt"))


def migrate_legacy_session() -> None:
    """把旧版单会话文件迁移成 sessions/默认.txt。"""
    legacy = BASE_DIR / "shared_session.txt"
    if legacy.exists() and not session_file("默认").exists():
        sid = legacy.read_text(encoding="utf-8").strip()
        if sid:
            save_session_id("默认", sid)
        legacy.unlink(missing_ok=True)


CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


def _alive_pids() -> set[int]:
    """当前系统里活着的进程 PID 集合（用于过滤残留的注册文件）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            encoding="gbk", errors="replace",  # 中文 Windows 的 tasklist 输出是 GBK
        ).stdout
        pids = set()
        for line in out.splitlines():
            parts = line.split('","')
            if len(parts) > 1 and parts[1].strip('"').isdigit():
                pids.add(int(parts[1]))
        return pids
    except Exception:
        return set()


def pc_open_windows() -> list[str]:
    """电脑上还开着的 claude 交互窗口（VSCode/终端），标注各占用了哪个机器人会话。

    数据源：claude 自己的活性注册表 ~/.claude/sessions/<pid>.json
    （含 pid/sessionId/entrypoint/startedAt），机器人派生的 -p 任务不算。
    """
    sid2name = {}
    for p in SESSIONS_DIR.glob("*.txt"):
        sid = p.read_text(encoding="utf-8").strip()
        if sid:
            sid2name[sid] = p.stem
    alive = _alive_pids()
    lines: list[str] = []
    for f in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = d.get("pid")
        if pid not in alive or d.get("kind") != "interactive":
            continue
        entry = str(d.get("entrypoint", ""))
        where = "VSCode" if "vscode" in entry else "终端"
        started = time.strftime("%H:%M", time.localtime(d.get("startedAt", 0) / 1000))
        name = sid2name.get(d.get("sessionId") or "")
        label = f"【{name}】" if name else "（机器人未管理的会话）"
        lines.append(f"  · {where}窗口 {label} {started} 起开着")
    return lines


def get_lan_ip() -> str:
    """拿本机局域网 IP（UDP connect 只查路由表，不真发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def kill_tree(proc: asyncio.subprocess.Process):
    """Windows 下终止进程及其子进程。"""
    if proc is None or proc.returncode is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def execute(cfg: dict, text: str, session_id: str | None):
    """--test 本地试跑用：审批一律自动允许并打印，便于验证权限配置。

    返回 (回复文本, 新的session_id)。
    """
    async def on_approval(request: dict, request_id=None) -> dict:
        desc = approval_desc(request)
        print(f"[需要批准] {desc}  ->  测试模式自动允许")
        return {"behavior": "allow", "updatedInput": request.get("input") or {}}

    res = await run_claude_stream(cfg, text, session_id, on_approval)
    if res["timeout"]:
        return f"⏰ 任务超时（超过 {res['timeout_minutes']:g} 分钟），已被强制终止。", res["session_id"]
    if not res["ok"]:
        return f"❌ 执行出错：{res['err']}", res["session_id"]
    log.info("任务结束 is_error=%s 结果预览: %s", res["is_error"], res["result"][:300])
    head = "⚠️ 任务执行中报错：\n" if res["is_error"] else "✅ 完成：\n"
    return head + (res["result"] or "（claude 没有返回文本）"), res["session_id"]


class TaskRunner:
    """任务队列：一次只跑一个 claude 进程，其余排队。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.reply = None  # 异步回复函数，由 DingBot 注入
        self.send_file = None  # 异步发文件函数，由 DingBot 注入
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending: list[str] = []  # 排队中的任务文本（用于相同任务去重）
        self.current: dict | None = None
        self.usage_all: dict = self._load_usage_all()
        # 当前等待手机审批的权限请求：{desc, decision, event}
        self.approval: dict | None = None
        self._worker: asyncio.Task | None = None
        # 实时过程窗口状态：默认关，手机发 /live 打开；手动关窗后自动停用
        self.live_wanted = False
        self.live_proc = None      # 窗口进程句柄；poll() 非 None ＝ 被人关了
        self.live_file = None      # 过程记录文件句柄（跨任务复用同一窗口）
        self.live_path: Path | None = None

    @staticmethod
    def _load_usage_all() -> dict:
        """读历史累计 token 消耗（usage.json），坏文件当零起步。"""
        p = BASE_DIR / "usage.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {k: float(d.get(k) or 0) for k in
                        ("in", "out", "cache", "turns", "cost", "tasks")}
        except Exception:
            pass
        return {"in": 0.0, "out": 0.0, "cache": 0.0, "turns": 0.0,
                "cost": 0.0, "tasks": 0.0}

    def _merge_usage_all(self, cur: dict):
        """任务结束把本次消耗并入累计并写 usage.json。"""
        u = cur.get("usage") or {}
        if not (u.get("in") or u.get("out") or u.get("cost")):
            return
        for k in ("in", "out", "cache", "turns", "cost"):
            self.usage_all[k] = self.usage_all.get(k, 0) + float(u.get(k) or 0)
        self.usage_all["tasks"] = self.usage_all.get("tasks", 0) + 1
        try:
            (BASE_DIR / "usage.json").write_text(
                json.dumps(self.usage_all, ensure_ascii=False), encoding="utf-8")
        except Exception:
            log.exception("写入 usage.json 失败")

    def _live_write(self, s: str):
        if self.live_file:
            try:
                self.live_file.write(s + "\n")
                self.live_file.flush()
            except Exception:
                pass

    def open_live(self, sname: str) -> bool:
        """打开（或复用）实时过程窗口。返回 False 表示总开关没开。"""
        if not self.cfg.get("live_window", True):
            return False
        if self.live_proc and self.live_proc.poll() is None:
            self.live_wanted = True
            return True  # 已经开着
        self.stop_live(discard_want=False)
        live_dir = BASE_DIR / "live"
        live_dir.mkdir(exist_ok=True)
        self.live_path = live_dir / f"{sname}.txt"
        try:
            self.live_file = open(self.live_path, "w", encoding="utf-8")
        except OSError:
            log.exception("创建实时过程文件失败")
            return True
        self.live_proc = open_live_window(self.live_path, sname)
        self.live_wanted = True
        self._live_write(f"会话【{sname}】实时过程已开启，等待手机派任务…")
        return True

    def stop_live(self, discard_want: bool = True):
        """关掉实时窗口。discard_want=True 用于主动关闭（/live off、切会话）。"""
        if discard_want:
            self.live_wanted = False
        if self.live_proc and self.live_proc.poll() is None:
            try:
                kill_tree(self.live_proc)
            except Exception:
                pass
        self.live_proc = None  # 主动关闭不留句柄，避免被当成"用户手动关窗"
        if self.live_file:
            try:
                self.live_file.close()
            except Exception:
                pass
        self.live_file = None

    def start(self):
        self._worker = asyncio.create_task(self._worker_loop())

    async def submit(self, incoming, text: str, handoff: bool = False):
        staff = incoming.sender_staff_id or ""
        norm = ("__交接__" if handoff else text.strip())
        if self.current and self.current.get("raw") == norm:
            await self.reply(
                "⚠️ 相同的任务正在执行中，这条已忽略。\n想重跑：先 /stop，或等完成后再发。", incoming
            )
            return
        if norm in self.pending:
            await self.reply("⚠️ 相同的任务已在排队中，这条已忽略。", incoming)
            return
        if self.queue.qsize() >= int(self.cfg.get("max_pending_tasks", 5)):
            await self.reply("⚠️ 任务队列已满，请稍后再试，或用 /stop 停止当前任务。", incoming)
            return
        await self.queue.put({"incoming": incoming, "text": text, "handoff": handoff})
        self.pending.append(norm)
        # 空闲时任务立即开始，由 worker 发开工消息；只有真在排队才提示
        if self.queue.qsize() > 1 or self.current:
            await self.reply(f"已排队，前面还有 {self.queue.qsize()} 个：{norm[:50]}", incoming)

    def stop_current(self) -> str | None:
        if self.current and self.current.get("proc"):
            self.current["stop_requested"] = True
            # 若正卡在等审批，先把审批事件放行，避免空等到超时
            if self.approval and not self.approval["event"].is_set():
                self.approval["decision"] = ("deny", "任务被手动停止")
                self.approval["event"].set()
            kill_tree(self.current["proc"])
            return self.current.get("title", "(未知任务)")
        return None

    def flush_queue(self) -> int:
        """清空排队中的任务（不动正在跑的）。中断误发任务时连带清掉，返回清掉几条。"""
        n = 0
        while True:
            try:
                self.queue.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                break
        self.pending.clear()
        return n

    def status_text(self) -> str:
        lines = [f"📁 工作目录：{self.cfg.get('work_dir') or BASE_DIR}"]
        if self.current:
            mins = (time.time() - self.current["started"]) / 60
            lines.append(f"🏃 正在执行（已 {mins:.1f} 分钟）：{self.current['title']}")
            phase = self.current.get("phase")
            if phase:
                lines.append(f"   当前阶段：{phase}")
        else:
            lines.append("💤 当前没有正在执行的任务")
        if self.approval:
            seq = self.approval.get("seq")
            label = f"【审批 {seq}】" if seq else ""
            lines.append(f"⏳ 有权限请求等你答复：{label}{self.approval['desc']}"
                         "\n回复 1 允许 / 2 拒绝 / 3 你的指示 / 4 允许且同类不再问")
        lines.append(f"📋 排队中：{self.queue.qsize()} 个")
        cur = current_session_name()
        sid = load_session_id(cur)
        lines.append(f"🧠 当前会话：【{cur}】{'（' + sid[:10] + '…）' if sid else '（新会话）'}")
        wins = pc_open_windows()
        if wins:
            lines.append("🖥️ 电脑上开着的 claude 窗口：")
            lines += wins
            lines.append("⚠️ 同一个会话两边别同时聊；电脑侧用完 /exit 退出")
        lines.append(f"🗂️ 共 {len(list_sessions())} 个会话，发 /s 查看/切换")
        lines.append("ℹ️ 电脑上双击 resume_shared.bat 打开同一对话")
        lines.append("ℹ️ 发 /help 查看用法")
        return "\n".join(lines)

    async def _worker_loop(self):
        while True:
            item = await self.queue.get()
            try:
                await self._run(item)
            except Exception:
                log.exception("任务执行出错")
                try:
                    await self.reply("❌ 任务内部出错，详情见 bot.log。", item["incoming"])
                except Exception:
                    pass
            finally:
                self.queue.task_done()

    async def _run(self, item):
        incoming, text = item["incoming"], item["text"]
        handoff = bool(item.get("handoff"))
        sname = current_session_name()
        sid = load_session_id(sname)
        if handoff:
            if not sid:
                # 连发两条 /交接 时第二条会落到这里：会话已清空，别再把记忆文件搅乱
                if "__交接__" in self.pending:
                    self.pending.remove("__交接__")  # 早退不走 finally，这里手动清账
                await self.reply("当前会话没有历史，无需交接（可能刚交接过）。", incoming)
                return
            text = HANDOFF_WRITE_PROMPT.format(mem=memory_file(sname))
            title = "🧳 会话交接（写记忆文件并重开会话）"
        else:
            mem = memory_file(sname)
            if not sid and mem.exists():
                # 新一轮会话的第一条任务：自动先读记忆文件接续
                text = PICKUP_PREFIX.format(sname=sname, mem=mem) + text
            title = text[:50].replace("\n", " ")
        self.current = {"title": title, "raw": text.strip(), "started": time.time(), "proc": None,
                        "phase": "🚀 启动中"}
        try:
            await self.reply(title, incoming)
            # 实时阶段跟踪（/status、/tail 用）+ 实时过程窗口（手机发 /live 打开后才弹）
            def on_event(d):
                try:
                    phase, rows = live_write_event(self._live_write, d)
                except Exception:
                    return
                if self.current:
                    if phase:
                        self.current["phase"] = phase  # live 没开时 _live_write 是空操作，只记状态
                    if rows:
                        buf = self.current.setdefault("recent", [])
                        buf.extend(rows)
                        del buf[:-60]  # 只留最近 60 行供 /tail
                    try:
                        record_trace(self.current, d)  # 思维链 + token 消耗（网页面板用）
                    except Exception:
                        pass
            if self.live_wanted and self.live_proc and self.live_proc.poll() is not None:
                # 窗口进程没了＝用户手动关的：停用并告知（主动关闭走 stop_live，句柄已清）
                self.stop_live()
                await self.reply("实时窗口已关闭，后续任务不再弹窗；想再看，发 /live 打开。", incoming)
            elif self.live_wanted and self.live_file:
                try:
                    self._live_write("=" * 46)
                    self._live_write(f"新任务：{text}")
                except Exception:
                    log.exception("写入实时过程窗口失败")
            # 任务里提到 PDF 就把文件发到手机，钉钉里直接预览（发错不了：仅限真实存在且 C 盘外）
            if self.send_file:
                for p in extract_sendable_files(self.cfg, text):
                    await self.send_file(p, incoming)

            async def on_approval(request: dict, request_id=None) -> dict:
                """claude 遇到需要批准的操作：转发手机，等用户回 1/2/3。

                消息带序号（审批 1、审批 2…），与上一个请求相同会明确标注，
                免得连环审批时分不清"新请求"还是"重复消息"。
                claude 对同一请求重发 control_request 时不再重发询问，
                直接复用已等的答复。
                """
                desc = approval_desc(request)
                cur = self.current
                key = approval_key(request)
                # 本任务内用户回过"4"的同类操作：直接放行不再打扰
                if cur and key in cur.get("auto_allow_keys", set()):
                    log.info("同类自动允许(%s): %s", key, desc)
                    return {"behavior": "allow",
                            "updatedInput": request.get("input") or {}}
                # 用户回过"5"：本任务剩余审批全部自动放行（硬拦截不经过这里，仍生效）
                if cur and cur.get("preauth_all"):
                    log.info("预授权自动允许: %s", desc)
                    return {"behavior": "allow",
                            "updatedInput": request.get("input") or {}}
                seq = (cur.get("ap_seq", 0) if cur else 0) + 1
                if cur:
                    cur["ap_seq"] = seq
                # claude 重发同一请求：不重复打扰，直接等已有事件
                prev = self.approval
                if (prev and not prev["event"].is_set()
                        and request_id is not None
                        and prev.get("request_id") == request_id):
                    log.info("claude 重发同一审批请求，复用已有询问 (id=%s)", request_id)
                else:
                    wait_min = float(self.cfg.get("approval_timeout_minutes", 10))
                    dup = ""
                    if cur is not None and cur.get("ap_last") == desc:
                        dup = "\n⚠️ 内容和上一个请求一样（claude 在重试），不是新操作。"
                    hint = ""
                    if cur is not None:
                        cur["ap_last"] = desc
                        n_same = (cur.get("ap_allow_counts") or {}).get(key) or 0
                        if n_same >= 2:
                            hint = (f"\n💡 同类操作您已连批 {n_same} 次，"
                                    f"回 4 可一次放行本任务内剩余同类。")
                    self.approval = {"desc": desc, "decision": None,
                                     "event": asyncio.Event(),
                                     "request_id": request_id, "seq": seq,
                                     "key": key}
                    await self.reply(
                        f"【审批 {seq}】{desc}{dup}{hint}\n"
                        f"回复 1 允许 / 2 拒绝 / 3 你的指示 / 4 同类不再问 / 5 本任务后续全部自动放行"
                        f"（回\"同意/继续\"也行，{wait_min:g}分钟内有效）",
                        incoming,
                    )
                try:
                    await asyncio.wait_for(
                        self.approval["event"].wait(),
                        timeout=float(self.cfg.get("approval_timeout_minutes", 10)) * 60,
                    )
                except asyncio.TimeoutError:
                    self.approval["decision"] = ("deny", "长时间未答复，自动拒绝")
                act, extra = self.approval["decision"] or ("deny", "未答复")
                self.approval = None
                if act == "allow":
                    log.info("权限批准: %s", desc)
                    if cur is not None:  # 计数供"连批 N 次后提示回 4"用
                        counts = cur.setdefault("ap_allow_counts", {})
                        counts[key] = counts.get(key, 0) + 1
                    return {"behavior": "allow",
                            "updatedInput": request.get("input") or {}}
                log.info("权限拒绝（%s）: %s", extra, desc)
                return {"behavior": "deny", "message": extra or "用户拒绝"}

            # 进度播报：任务执行期间每 progress_minutes 分钟向手机报一次阶段/异常
            progress_min = float(self.cfg.get("progress_minutes", 3))

            async def progress_reporter():
                while True:
                    await asyncio.sleep(progress_min * 60)
                    cur = self.current
                    if not cur:
                        return
                    mins = (time.time() - cur["started"]) / 60
                    proc = cur.get("proc")
                    if proc is not None and proc.poll() is not None:
                        await self.reply(
                            f"⏱ 已 {mins:.0f} 分钟 | ⚠️ claude 进程意外退出，正在收尾…", incoming
                        )
                    else:
                        await self.reply(
                            f"⏱ 进度播报：已运行 {mins:.0f} 分钟 | {cur.get('phase') or '运行中'}",
                            incoming,
                        )

            reporter = asyncio.create_task(progress_reporter())
            try:
                res = await run_claude_stream(
                    self.cfg, text, sid, on_approval,
                    on_proc=lambda p: self.current.update(proc=p),
                    on_event=on_event,
                )
            finally:
                reporter.cancel()
            if self.current.get("stop_requested"):
                await self.reply(f"已手动停止：{title}", incoming)
                return
            if res["timeout"]:
                await self.reply(
                    f"任务超时（超过 {res['timeout_minutes']:g} 分钟）已终止：{title}",
                    incoming,
                )
                return
            if not res["ok"]:
                log.error("任务失败: %s", res["err"])
                await self.reply(f"执行出错：{res['err']}", incoming)
                return
            new_sid = res["session_id"] or sid
            if new_sid:
                save_session_id(sname, new_sid)
            if handoff:
                # 交接：记忆已写好，清空会话；下一条消息从全新会话开始并自动读记忆
                session_file(sname).unlink(missing_ok=True)
                await self.reply(
                    f"🧳 交接完成：本会话要点已写入 {memory_file(sname)}\n"
                    "会话已清空，下一条消息开启全新会话（开头自动读记忆接续）。",
                    incoming,
                )
                return
            result = res["result"] or "（claude 没有返回文本）"
            # claude 按 [SEND_FILE:路径] 约定交来的文件：自动发到手机（自然语言
            # "发给我"也能收到文件，不再依赖任务文本里写死路径）
            send_marks = [m.strip() for m in SEND_FILE_RE.findall(result)]
            if send_marks:
                result = SEND_FILE_RE.sub("", result).rstrip()
            # modelUsage 是接口真实计费的模型，比模型自报身份可靠
            models = res["models"] or "未知"
            elapsed = (time.time() - self.current["started"]) / 60
            log.info("任务结束 is_error=%s 模型=%s 结果预览: %s",
                     res["is_error"], models, result[:300])
            head = "%s | %s | %s | 出错了：\n" % (sname, fmt_elapsed(elapsed), models) \
                if res["is_error"] else "%s | %s | %s\n" % (sname, fmt_elapsed(elapsed), models)
            for part in chunk_text(head + result):
                await self.reply(part, incoming)
            for mp in send_marks:
                fp = resolve_allowed(self.cfg, mp)
                if fp is None or not fp.is_file():
                    log.warning("SEND_FILE 目标不可发: %s", mp)
                    await self.reply(f"⚠️ 未能发送文件：{mp}（不存在、在 C 盘或超过 20MB）",
                                     incoming)
                    continue
                log.info("SEND_FILE 自动发送: %s", fp)
                await self.send_file(fp, incoming)
        finally:
            norm = "__交接__" if item.get("handoff") else item["text"]
            if norm in self.pending:  # 用排队时的去重键清理（text 可能已被交接/接续改写）
                self.pending.remove(norm)
            self.approval = None
            if self.current:
                self._merge_usage_all(self.current)  # 累计 token 消耗（含超时/出错的任务）
            self.current = None
            if self.live_file:  # 窗口跨任务复用：写收尾行，不关文件
                self._live_write("—— 任务已结束，结果已发回手机 ——")


class _PanelCallback:
    """面板指令伪装的回调对象：process() 只访问 .data（见 panel.py）。"""

    def __init__(self, data: dict):
        self.data = data


class DingBot(dingtalk_stream.ChatbotHandler):
    """处理钉钉机器人收到的消息。"""

    def __init__(self, cfg: dict, runner: TaskRunner):
        super().__init__()
        self.cfg = cfg
        self.runner = runner
        self._seen_msgs: dict[str, float] = {}  # message_id -> 时间戳，防钉钉重发
        # 网页面板（panel.py）用：最近一次真实消息的原始数据（含回复用的
        # sessionWebhook），面板发指令时借它构造虚拟消息；以及回复镜像
        self._last_data: dict | None = None
        self.recent_replies: list[dict] = []

    def _is_duplicate(self, msg_id: str, text: str, skip_content: bool = False) -> bool:
        """消息级 + 内容级双保险去重。

        钉钉对未及时签收的消息会在 60 秒后原样重发（messageId 相同）；
        内容级兜底再拦一次：90 秒内完全相同的文本只处理一次。
        审批答复（1/2/3）跳过内容级：短时间内可能要连答几次，
        重发由 messageId 兜底拦截。
        """
        now = time.time()
        keys = []
        if msg_id:
            keys.append(("id:" + msg_id, 600))
        if text and not skip_content:
            keys.append(("txt:" + text, 90))
        for key, _ in keys:
            exp = self._seen_msgs.get(key)
            if exp and now < exp:
                return True
        for key, ttl in keys:
            self._seen_msgs[key] = now + ttl
        if len(self._seen_msgs) > 500:
            self._seen_msgs = {k: v for k, v in self._seen_msgs.items() if v > now}
        return False

    async def reply_async(self, text: str, incoming):
        # 镜像给网页面板（先记后发：webhook 万一失效，面板仍能看到内容）
        self.recent_replies.append({"t": time.strftime("%H:%M:%S"), "text": text})
        del self.recent_replies[:-50]
        await asyncio.to_thread(self.reply_text, text, incoming)

    def panel_snapshot(self) -> dict:
        """网页面板轮询的状态快照（panel.py /api/state 用）。"""
        r = self.runner
        cur = r.current or {}
        cfg = load_config()
        state = {
            "now": time.strftime("%H:%M:%S"),
            "task": None,
            "queue": r.queue.qsize(),
            "approval": None,
            "session": current_session_name(),
            "sessions": list_sessions(),
            "model": cfg.get("model") or "",
            "models": list(cfg.get("models") or []),
            "mode": cfg.get("permission_mode") or "default",
            "modes": dict(PERMISSION_MODES),
            "recent": list((cur.get("recent") or [])[-25:]),
            "trace": list((cur.get("trace") or [])[-30:]),
            "usage": cur.get("usage") or {},
            "usage_all": dict(self.runner.usage_all),
            "replies": self.recent_replies[-30:],
            "dingtalk_ok": self._last_data is not None,
        }
        if r.current:
            state["task"] = {
                "title": r.current.get("title", ""),
                "phase": r.current.get("phase", ""),
                "mins": round((time.time() - r.current["started"]) / 60, 1),
            }
        if r.approval and not r.approval["event"].is_set():
            state["approval"] = {"seq": r.approval.get("seq"), "desc": r.approval["desc"]}
        return state

    async def submit_panel_media(self, path: Path, kind: str):
        """网页面板传来的图片/文件：存盘后按手机同款流程交给 claude 查看。"""
        if self._last_data is None:
            raise RuntimeError("还没拿到钉钉回复通道：请先用手机钉钉发一条消息")
        incoming = dingtalk_stream.ChatbotMessage.from_dict(copy.deepcopy(self._last_data))
        if kind == "image":
            task = (
                f"用户通过网页面板发来了一张图片，已保存到 {path} 。"
                "请先用 Read 工具查看这张图片，然后向用户简要描述你看到了什么，"
                "并询问用户想对它做什么。"
            )
            note = f"📎 图片已收到（{path.stat().st_size / 1024 / 1024:.1f} MB），claude 正在查看…"
        else:
            task = (
                f"用户通过网页面板发来了一个文件{path.name}，已保存到 {path} 。"
                "请先用合适的方式查看这个文件的内容（Read 或相应工具），"
                "然后向用户简要总结，并询问用户想对它做什么。"
            )
            note = f"📎 文件已收到（{path.stat().st_size / 1024:.0f} KB），claude 正在查看…"
        await self.reply_async(note, incoming)
        await self.runner.submit(incoming, task)

    async def inject_from_panel(self, text: str):
        """面板指令注入：借最近一次真实消息构造虚拟消息，走和手机相同的 process()。

        回复会照常发进钉钉聊天（用缓存的 sessionWebhook），同时镜像到面板。
        """
        import panel
        data = copy.deepcopy(self._last_data or {})
        data["msgtype"] = "text"
        data["text"] = {"content": text}
        headers = dict(data.get("headers") or {})
        headers["messageId"] = f"panel-{time.time_ns()}"  # 新 id，防被去重拦下
        data["headers"] = headers
        try:
            await self.process(_PanelCallback(data))
        except Exception as e:
            log.exception("面板指令处理失败")
            return False, f"处理失败：{e}"
        return True, "ok"

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        try:
            # SDK 把 text 包装成 TextContent 对象（.content 属性），不是字典
            raw = getattr(incoming, "text", None)
            content = raw.get("content") if isinstance(raw, dict) else getattr(raw, "content", None)
            text = (content or "").strip()
        except Exception:
            text = ""
        # 原始数据里的 messageId 是驼峰写法
        headers = callback.data.get("headers") or {}
        msg_id = str(headers.get("messageId") or headers.get("message_id") or "")
        if self._is_duplicate(msg_id, text, skip_content=approval_answer_kind(text) is not None):
            log.info("重复消息已忽略（60秒重发）: %r", text[:50])
            return AckMessage.STATUS_OK, "ok"
        staff = getattr(incoming, "sender_staff_id", "") or ""
        allowed = self.cfg.get("allowed_users") or []

        if staff and staff not in allowed:
            log.warning("未授权消息 来自 %s: %r", staff, text[:100])
            await self.reply_async(
                f"⛔ 你（{staff}）不在白名单，消息已被忽略。\n\n"
                "如需授权：把上面这串 ID 填入 config.json 的 "
                "\"allowed_users\" 数组，然后重启机器人。",
                incoming,
            )
            return AckMessage.STATUS_OK, "ok"

        # 白名单已过：缓存这条真实消息（含回复用的 sessionWebhook），供面板注入
        self._last_data = copy.deepcopy(callback.data)

        # 手机发来的图片/文件：下载保存后交给 claude 查看（在审批拦截之前，
        # 因为这类消息没有文字，不该被当成"等审批期间的闲话"）
        msg_type = str(callback.data.get("msgtype") or getattr(incoming, "message_type", "") or "")
        if msg_type in ("picture", "image", "file"):
            await self._handle_incoming_media(msg_type, callback.data.get("content") or {}, incoming)
            return AckMessage.STATUS_OK, "ok"
        if msg_type and msg_type not in ("text", "unknown"):
            log.warning("收到暂不支持的消息类型: %s", msg_type)
            await self.reply_async(
                f"暂时不支持「{msg_type}」类型，可发：文字、图片、文件。", incoming
            )
            return AckMessage.STATUS_OK, "ok"

        log.info("收到指令 来自 %s: %r", staff, text[:100])
        # 手机审批：1/同意类 允许，2/拒绝类 拒绝，3+文字 带指示拒绝
        ap = self.runner.approval
        if ap and not ap["event"].is_set():
            kind = approval_answer_kind(text)
            if kind is not None:
                await self._handle_approval(kind, text, incoming)
                return AckMessage.STATUS_OK, "ok"
            if not (text.startswith("/") or text in COMMAND_WORDS):
                # 等审批期间发来的闲话：提醒怎么答复，不当任务、不丢弃提示
                await self.reply_async(
                    "任务正在等你答复这个权限请求：回复 1 允许 / 2 拒绝 / 3 你的指示"
                    "（说\"同意/继续\"也行）",
                    incoming,
                )
                return AckMessage.STATUS_OK, "ok"
            # 斜杠命令和 停止/状态/帮助 照常往下走
        if not text:
            await self.reply_async("收到空消息，可发：文字、图片、文件。", incoming)
        elif text.lower() in ("/new", "/新会话", "新会话"):
            session_file(current_session_name()).unlink(missing_ok=True)
            await self.reply_async(
                f"🆕 会话【{current_session_name()}】已清空，下一条消息将开始全新对话。", incoming
            )
        elif text.lower() in ("/handoff", "/交接"):
            await self.handle_handoff(incoming)
        elif text.lower() == "/s" or text.lower().startswith(("/s ", "/会话", "/切换")):
            await self.handle_sessions(text, incoming)
        elif text.lower() in ("/stop", "/exit", "/quit", "/停止", "/退出", "/中断",
                              "停止", "退出", "中断", "stop", "exit"):
            t = self.runner.stop_current()
            n = self.runner.flush_queue()
            if t:
                msg = f"🛑 已停止：{t}"
                if n:
                    msg += f"\n同时清掉了排队的 {n} 条任务。"
            else:
                msg = "当前没有正在运行的任务。" + (f"（已清掉排队的 {n} 条）" if n else "")
            await self.reply_async(msg, incoming)
        elif text.lower() in ("/status", "/状态", "状态"):
            await self.reply_async(self.runner.status_text(), incoming)
        elif text.lower().startswith(("/live", "/过程")):
            await self.handle_live(incoming)
        elif text.lower().startswith("/model"):
            await self.handle_model(text, incoming)
        elif text.lower().startswith("/mode"):
            await self.handle_mode(text, incoming)
        elif text.lower().startswith("/tail"):
            await self.handle_tail(incoming)
        elif text.lower().startswith(("/usage", "/消耗")):
            await self.handle_usage(incoming)
        elif text.lower() in ("/help", "/帮助", "帮助"):
            await self.reply_async(HELP_TEXT, incoming)
        elif text.lower().startswith(("/ls", "/目录", "/列表")):
            parts = text.strip().split(maxsplit=1)
            await self.reply_async(self.ls_text(parts[1].strip() if len(parts) > 1 else ""), incoming)
        elif text.lower().startswith(("/file", "/文件", "/发文件")):
            await self.handle_file(text, incoming)
        else:
            if text == "5" and self.runner.current:
                # 空档期预授权：现在没在等的审批，但先记住"本任务后续全放行"
                self.runner.current["preauth_all"] = True
                await self.reply_async(
                    "✅ 本任务后续审批自动放行（硬拦截仍生效；"
                    "任务结束或 /stop 后失效）。", incoming)
            elif text in ("1", "2", "3", "4", "5") and self.runner.current:
                await self.reply_async(
                    "这条 1/2 没有对上正在等待的审批（上一条可能已处理，"
                    "下一条还没问出来）。\n等新的【审批 N】来了再回复即可；"
                    "不想再被连环打扰，回 4 放行同类、回 5 放行本任务剩余全部审批。",
                    incoming,
                )
            elif text in ("1", "2", "3", "4", "5"):
                await self.reply_async(
                    "收到，但现在没有在等的审批，也没有在跑的任务。"
                    "想派活直接发文字；如这是对话内容，请多加几个字（如\"选 2\"）。",
                    incoming,
                )
            elif text.startswith("/"):
                # 打错的斜杠命令不该被当成任务跑掉
                await self.reply_async(
                    f"未知命令 {text.split()[0]}，没有执行。\n发 /help 查看全部命令。",
                    incoming,
                )
            else:
                await self.runner.submit(incoming, text)
        # SDK 的 raw_process 会解包返回值：必须是 (code, message) 元组，
        # 否则 ack 发不出去，钉钉 60 秒后就原样重发（这正是之前重复消息的根源）
        return AckMessage.STATUS_OK, "ok"

    async def handle_handoff(self, incoming):
        """会话交接：让当前会话把要点写进记忆文件，跑完后由 _run 清空会话重开。"""
        sname = current_session_name()
        if not load_session_id(sname):
            await self.reply_async("当前会话还没有历史，无需交接，直接发任务即可。", incoming)
            return
        await self.reply_async(
            f"🧳 开始交接：本会话先把要点写进 {memory_file(sname)}，"
            "写完自动清空重开（需跑一个整理任务，稍等）…", incoming)
        await self.runner.submit(incoming, "", handoff=True)

    async def _handle_approval(self, kind: str, text: str, incoming):
        ap = self.runner.approval
        if kind in ("allow", "allow_all", "allow_task"):
            ap["decision"] = ("allow", None)
            ap["event"].set()
            cur = self.runner.current
            if kind == "allow_all":
                if cur is not None:
                    cur.setdefault("auto_allow_keys", set()).add(ap.get("key") or "?")
                await self.reply_async(
                    f"已允许。本任务内同类操作（{ap.get('key')}）不再询问，直接执行。", incoming)
            elif kind == "allow_task":
                if cur is not None:
                    cur["preauth_all"] = True
                await self.reply_async(
                    "已允许。本任务后续需要审批的操作全部直接放行，不再询问"
                    "（硬拦截仍生效；任务结束或 /stop 后失效）。", incoming)
            else:
                await self.reply_async("已允许，任务继续。", incoming)
        elif kind == "deny":
            ap["decision"] = ("deny", "用户拒绝该操作")
            ap["event"].set()
            await self.reply_async("已拒绝，让 claude 换个方式。", incoming)
        else:
            guide = text[1:].lstrip(" ：:").strip() or "用户给出了补充指示"
            ap["decision"] = ("deny", f"用户拒绝并指示：{guide}")
            ap["event"].set()
            await self.reply_async(f"已转达给 claude：{guide}", incoming)

    async def _handle_incoming_media(self, msg_type: str, content: dict, incoming):
        """手机发来的图片/文件：下载存到 incoming/，作为任务交给 claude 读取。"""
        label = "图片" if msg_type in ("picture", "image") else "文件"
        # 钉钉图片消息 msgtype=picture，downloadCode 在 content 里（老字段 photoDownloadCode 兜底）
        code = content.get("downloadCode") or content.get("photoDownloadCode") or ""
        name = str(content.get("fileName") or content.get("name") or "").strip()
        if not code:
            await self.reply_async(f"❌ 没拿到这张{label}的下载凭证，请重新发一次。", incoming)
            return
        try:
            url = await asyncio.to_thread(download_robot_media, self.cfg, code)
            r = await asyncio.to_thread(requests.get, url, timeout=120)
            r.raise_for_status()
            incoming_dir = BASE_DIR / "incoming"
            incoming_dir.mkdir(exist_ok=True)
            ext = Path(name).suffix.lower()
            if not ext:
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
                ext = {
                    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                    "image/webp": ".webp", "image/heic": ".heic",
                    "application/pdf": ".pdf",
                }.get(ctype, ".bin")
            path = incoming_dir / f"{time.strftime('%m%d_%H%M%S')}{ext}"
            path.write_bytes(r.content)
            size_mb = len(r.content) / 1024 / 1024
            log.info("收到%s: %s (%.2f MB)", label, path, size_mb)
        except Exception as e:
            log.exception("接收%s失败", label)
            await self.reply_async(f"❌ 接收{label}失败：{e}", incoming)
            return
        if msg_type in ("picture", "image"):
            task = (
                f"用户用手机发来了一张图片，已保存到 {path} 。"
                "请先用 Read 工具查看这张图片，然后向用户简要描述你看到了什么，"
                "并询问用户想对它做什么。"
            )
            note = f"📎 图片已收到（{size_mb:.1f} MB），claude 正在查看…"
        else:
            task = (
                f"用户用手机发来了一个文件{name or path.name}，已保存到 {path} 。"
                "请先用合适的方式查看这个文件的内容（Read 或相应工具），"
                "然后向用户简要总结，并询问用户想对它做什么。"
            )
            note = f"📎 文件已收到（{size_mb:.1f} MB），claude 正在查看…"
        await self.reply_async(note, incoming)
        await self.runner.submit(incoming, task)

    async def handle_sessions(self, text: str, incoming):
        """多会话管理：/s 列表，/s 名称 切换或新建，/s 删 名称 删除。"""
        parts = text.strip().split(maxsplit=1)
        sub = parts[1].strip() if len(parts) > 1 else ""

        if not sub:
            names = list_sessions() or [current_session_name()]
            cur = current_session_name()
            lines = ["🗂️ 会话列表："]
            for n in names:
                lines.append(f"  • {n}" + (" 👈当前" if n == cur else ""))
            lines.append("切换/新建：/s 名称（建议按项目命名，如 小论文、求职）")
            lines.append("删除：/s 删 名称｜清空当前会话：/new")
            await self.reply_async("\n".join(lines), incoming)
            return

        if sub.startswith(("删 ", "删除 ")):
            target = _safe_name(sub.split(maxsplit=1)[1])
            if not target:
                await self.reply_async("用法：/s 删 名称", incoming)
                return
            f = session_file(target)
            if f.exists():
                f.unlink()
                msg = f"🗑️ 已删除会话【{target}】"
                if target == current_session_name():
                    msg += "（已删的是当前会话，下次任务将自动新开）"
            else:
                msg = f"❌ 没有这个会话：{target}"
            await self.reply_async(msg, incoming)
            return

        name = _safe_name(sub)
        if not name:
            await self.reply_async("会话名请用文字或数字，如：/s 小论文", incoming)
            return
        exists = session_file(name).exists()
        set_current_session_name(name)
        # 会话换了，旧实时窗口盯的是旧会话文件：主动关掉，新会话首次任务再弹新的
        self.runner.stop_live()
        if not exists:
            SESSIONS_DIR.mkdir(exist_ok=True)
            session_file(name).touch()
        if exists:
            n = (load_session_id(name) or "（空，发送即开始）")[:12]
            await self.reply_async(f"✅ 已切换到会话【{name}】，接着原有上下文继续（{n}）。", incoming)
        else:
            await self.reply_async(
                f"🆕 已创建并切换到新会话【{name}】。\n"
                "这个分类以后的所有任务都会带着自己的独立记忆，和其他会话互不干扰。",
                incoming,
            )

    def ls_text(self, arg: str) -> str:
        """列出文件夹内容；支持各盘（C 盘除外）。"""
        try:
            if arg.strip() in ("/", "全部", "所有盘", "盘"):
                ds = shared_drives()
                lines = ["💾 当前可访问的盘："]
                lines += [f"  📀 {d}:/" for d in ds]
                lines.append("提示：/ls D:/资料 浏览对应文件夹；/file 文件路径 发到聊天")
                return "\n".join(lines)
            if arg:
                p = resolve_allowed(self.cfg, arg)
                if p is None:
                    return "⛔ 出于安全限制，C 盘不开放；其他盘（含外接盘）都可以。"
                if not p.exists():
                    return f"❌ 路径不存在：{arg}"
                if p.is_file():
                    return f"📄 {p.name}（{p.stat().st_size/1024:.0f} KB）\n用 /file {arg} 可以把它发到手机"
            else:
                p = workdir_root(self.cfg)
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except Exception as e:
            return f"❌ 读取失败：{e}"
        lines = [f"📁 {p}"]
        for e in entries[:100]:
            try:
                if e.is_dir():
                    lines.append(f"  📂 {e.name}/")
                else:
                    lines.append(f"  📄 {e.name}（{e.stat().st_size/1024:.0f} KB）")
            except OSError:
                continue
        if len(entries) > 100:
            lines.append(f"  …共 {len(entries)} 项，只显示前 100")
        lines.append("提示：/ls 子目录名 或 /ls E:/资料 继续浏览；/file 文件路径 发到聊天")
        return "\n".join(lines)

    async def send_file_async(self, p: Path, incoming, note: bool = True):
        """把电脑文件发进聊天。/file 命令和任务自动发 PDF 共用。"""
        try:
            size = p.stat().st_size
        except OSError as e:
            await self.reply_async(f"❌ 读不到文件 {p.name}：{e}", incoming)
            return
        if size > 20 * 1024 * 1024:
            await self.reply_async(
                f"❌ 文件 {p.name} 有 {size / 1048576:.1f}MB，超过钉钉 20MB 上限，发不了。", incoming
            )
            return
        if note:
            await self.reply_async(f"📤 发送 {p.name}（{size / 1024:.0f} KB）…", incoming)
        try:
            media_id = await asyncio.to_thread(upload_media, self.cfg, p)
            await asyncio.to_thread(
                self._send_file, media_id, incoming,
                p.name, p.suffix.lstrip(".").lower() or "file",
            )
        except Exception as e:
            log.exception("发送文件失败")
            await self.reply_async(f"❌ 发送失败：{e}", incoming)

    async def handle_live(self, incoming):
        """手机开关实时过程窗口：开→再发一次关；手动关窗也会自动停用。"""
        r = self.runner
        if r.live_wanted and r.live_proc and r.live_proc.poll() is None:
            r.stop_live()
            await self.reply_async("已关闭实时过程窗口。想再看，随时发 /live。", incoming)
            return
        ok = r.open_live(current_session_name())
        if not ok:
            await self.reply_async(
                "实时窗口功能被 config.json 的 live_window=false 停用了。", incoming)
        elif r.live_proc:
            await self.reply_async(
                "已打开：电脑上弹出的窗口会实时显示手机任务的思考/工具过程。"
                "关掉那个窗口即自动停用；也可再发 /live 关闭。", incoming)
        else:
            await self.reply_async("电脑端打开窗口失败，详见 bot.log。", incoming)

    async def handle_mode(self, text: str, incoming):
        """权限模式：/mode 看序号列表；/mode 0 恢复默认；/mode 1/2 或名字切换。"""
        order = list(PERMISSION_MODES.keys())
        arg = text.strip().split(maxsplit=1)
        arg = arg[1].strip().lower() if len(arg) > 1 else ""
        alias = {"默认": "default", "手动": "default", "编辑": "acceptEdits",
                 "自动编辑": "acceptEdits", "全自动": "bypass", "bypasspermissions": "bypass"}
        key = alias.get(arg, arg)
        if arg.isdigit():
            i = int(arg)
            if i == 0:
                key = "default"
            elif 1 <= i <= len(order):
                key = order[i - 1]
            else:
                await self.reply_async(f"序号范围 0-{len(order)}，发 /mode 看列表。", incoming)
                return
        if key in PERMISSION_MODES:
            cfg = load_config()
            cfg["permission_mode"] = key
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            await self.reply_async(
                f"✅ 权限模式已切换：{key}\n{PERMISSION_MODES[key]}\n（下一个任务起生效）",
                incoming,
            )
        else:
            cur = load_config().get("permission_mode") or "default"
            lines = [f"当前权限模式：{cur}", "切换方式：回复序号（0=默认）或名字", ""]
            lines.append("0. default —— 默认（白名单外发手机审批）"
                         + ("　👈当前" if cur == "default" else ""))
            for i, k in enumerate(order, 1):
                lines.append(f"{i}. {k} —— {PERMISSION_MODES[k]}"
                             + ("　👈当前" if cur == k else ""))
            await self.reply_async("\n".join(lines), incoming)

    async def handle_model(self, text: str, incoming):
        """模型：/model 看序号列表；/model 0 恢复 claude 默认；/model N 或名字切换。"""
        models = load_config().get("models") or []
        arg = text.strip().split(maxsplit=1)
        arg = arg[1].strip() if len(arg) > 1 else ""
        if arg:
            val = arg
            if arg.isdigit():
                i = int(arg)
                if i == 0:
                    val = ""
                elif 1 <= i <= len(models):
                    val = models[i - 1]
                else:
                    await self.reply_async(
                        f"序号范围 0-{len(models)}，发 /model 看列表。", incoming)
                    return
            elif arg.lower() == "clear":
                val = ""
            cfg = load_config()
            cfg["model"] = val
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            await self.reply_async(
                f"✅ 模型已切换为：{val or '（claude 默认）'}\n（下一个任务起生效）", incoming)
        else:
            cur = load_config().get("model") or ""
            lines = [f"当前模型：{cur or '（claude 默认）'}",
                     "切换方式：回复序号（0=claude 默认）或直接发模型名", ""]
            lines.append("0. （claude 默认）" + ("　👈当前" if not cur else ""))
            for i, m in enumerate(models, 1):
                lines.append(f"{i}. {m}" + ("　👈当前" if cur == m else ""))
            lines.append("")
            lines.append("候选在 config.json 的 models 数组里增删；"
                         "临时用别的名字可直接发 /model 模型名")
            await self.reply_async("\n".join(lines), incoming)

    async def handle_tail(self, incoming):
        """/tail：手机上回看当前任务最近的思考/工具/回复输出。"""
        cur = self.runner.current
        buf = (cur or {}).get("recent")
        if not buf:
            await self.reply_async("当前没有正在运行的任务（或还没有输出）。", incoming)
            return
        rows = [r[:180] for r in buf[-15:]]
        await self.reply_async("📋 最近输出（最后 %d 行）：\n%s" % (len(rows), "\n".join(rows)),
                               incoming)

    @staticmethod
    def _fmt_tok(n) -> str:
        n = float(n or 0)
        if n >= 100000:
            return f"{n / 1000:.0f}k"
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return f"{n:.0f}"

    async def handle_usage(self, incoming):
        """/usage：本任务与历史累计 token 消耗（网页面板徽章的钉钉版）。"""
        cur = self.runner.current or {}
        t = cur.get("usage") or {}
        a = self.runner.usage_all or {}
        lines = ["🪙 Token 消耗"]
        if t.get("out") or t.get("in"):
            rows = [f"↑输出 {_fmt_tok(t.get('out'))}",
                    f"↓输入 {_fmt_tok(t.get('in'))}",
                    f"⚡缓存 {_fmt_tok(t.get('cache'))}"]
            if t.get("turns"):
                rows.append(f"{int(t['turns'])} 轮")
            if t.get("cost"):
                rows.append(f"${t['cost']:.4f}")
            lines.append("本任务：" + " · ".join(rows))
        else:
            lines.append("本任务：暂无（没有运行中的任务或还没产生消耗）")
        rows = [f"↑{_fmt_tok(a.get('out'))}", f"↓{_fmt_tok(a.get('in'))}",
                f"⚡{_fmt_tok(a.get('cache'))}"]
        if a.get("cost"):
            rows.append(f"${a['cost']:.4f}")
        lines.append(f"历史累计（{int(a.get('tasks') or 0)} 个任务）："
                     + " · ".join(rows))
        lines.append("（自 2026-09-22 起统计；费用为接口回报值，实际以智谱账单为准）")
        await self.reply_async("\n".join(lines), incoming)

    async def handle_file(self, text: str, incoming):
        """把电脑上的文件通过钉钉发到聊天窗口。"""
        parts = text.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            await self.reply_async("用法：/file 文件路径（相对工作目录，也可绝对路径，≤20MB）", incoming)
            return
        p = resolve_allowed(self.cfg, arg)
        if p is None:
            await self.reply_async("⛔ 出于安全限制：C 盘不开放，其他盘（含外接盘）都可以。", incoming)
            return
        if not p.is_file():
            await self.reply_async(f"❌ 找不到文件：{arg}", incoming)
            return
        await self.send_file_async(p, incoming)

    def _send_file(self, media_id: str, incoming, name: str, ftype: str):
        # file 消息要三件套，且字段是驼峰：mediaId / fileName / fileType
        # （只写 media_id 报 401102 miss mediaId；缺 fileType 报 401104 miss fileType）
        r = requests.post(
            incoming.session_webhook,
            json={"msgtype": "file",
                  "file": {"mediaId": media_id, "fileName": name, "fileType": ftype}},
            timeout=60,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or data.get("errcode") != 0:
            raise RuntimeError(f"钉钉拒绝: {r.text[:200]}")
        log.info("文件已发送 media_id=%s", media_id)
        return AckMessage.STATUS_OK, "ok"


def cmd_check(cfg: dict) -> int:
    ok = True
    claude = find_claude()
    print(f"claude 命令      : {claude or '❌ 未找到，请先安装 Claude Code'}")
    ok &= bool(claude)
    ak, sk = cfg.get("app_key", ""), cfg.get("app_secret", "")
    filled = ak and sk and "填" not in ak and "填" not in sk
    print(f"钉钉 AppKey      : {ak[:6] + '…' if filled else '❌ 还没填（config.json）'}")
    ok &= bool(filled)
    users = cfg.get("allowed_users") or []
    print(f"白名单用户       : {users if users else '⚠️ 空（任何人发消息都会被拒并提示其 ID）'}")
    print(f"工作目录         : {cfg.get('work_dir')}")
    print(f"任务超时         : {cfg.get('task_timeout_minutes', 30)} 分钟")
    print(f"权限白名单文件   : {write_settings_file()} （已生成）")
    print("\n结论: " + ("✅ 可以启动：python bot.py" if ok else "❌ 请先解决上面的 ❌ 项"))
    return 0 if ok else 1


async def cmd_test(cfg: dict, task: str) -> int:
    """本地试跑一次 claude -p 管线，不经过钉钉。"""
    print(f"== 测试执行: {task}")
    reply, new_sid = await execute(cfg, task, None)
    print("== 回复内容 ==")
    print(reply[:3000])
    print(f"== session_id: {new_sid}")
    if new_sid:
        reply2, _ = await execute(cfg, "用一句话概括我上一条让你做的事情，不要执行任何命令。", new_sid)
        print("== 追问测试（--resume）==")
        print(reply2[:1000])
    return 0


def main():
    parser = argparse.ArgumentParser(description="钉钉遥控 Claude Code 机器人")
    parser.add_argument("--check", action="store_true", help="自检配置与环境")
    parser.add_argument("--test", metavar="任务", help="本地试跑一次 claude -p（不走钉钉）")
    args = parser.parse_args()

    cfg = load_config()
    migrate_legacy_session()
    cleanup_live_dir()
    cleanup_incoming_dir()
    if args.check:
        sys.exit(cmd_check(cfg))
    if args.test is not None:
        sys.exit(asyncio.run(cmd_test(cfg, args.test)))

    ak, sk = cfg.get("app_key", ""), cfg.get("app_secret", "")
    if not ak or not sk or "填" in ak or "填" in sk:
        sys.exit("❌ 请先在 config.json 里填好 app_key / app_secret（见 设置指南.md）")

    runner = TaskRunner(cfg)
    bot = DingBot(cfg, runner)
    runner.reply = bot.reply_async
    runner.send_file = bot.send_file_async

    # 网页面板：首次启动自动生成令牌写回 config.json（没令牌的请求一律 403）
    if not (cfg.get("panel_token") or "").strip():
        import secrets
        cfg["panel_token"] = secrets.token_urlsafe(18)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        cfg = load_config()
        log.info("已生成面板令牌并写入 config.json（panel_token）")
    panel_port = int(cfg.get("panel_port", 8900))

    credential = dingtalk_stream.Credential(ak, sk)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, bot)

    async def amain():
        asyncio.get_running_loop().set_exception_handler(
            lambda l, ctx: log.error("asyncio 未处理异常: %s", ctx.get("message"),
                                     exc_info=ctx.get("exception")))
        runner.start()  # 必须在事件循环内启动后台任务
        import panel
        try:
            await panel.start_site(cfg, bot)
            lan_ip = get_lan_ip()
            log.info("网页面板已启动：本机 http://127.0.0.1:%d ｜ 局域网 http://%s:%d"
                     " ｜ Tailscale 外网 http://<电脑的100.x.x.x>:%d（令牌见 config.json 的 panel_token）",
                     panel_port, lan_ip, panel_port, panel_port)
        except OSError as e:
            log.warning("网页面板启动失败（不影响钉钉机器人）：%s", e)
        log.info("机器人已启动，等待手机消息…（Ctrl+C 退出）")
        await client.start()  # SDK 的异步长驻入口（含断线重连）

    asyncio.run(amain())


if __name__ == "__main__":
    main()
