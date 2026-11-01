#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py —— 本地服务（纯 Python 标准库，0 第三方依赖）

    python server.py            # 起在 http://127.0.0.1:8000
    python server.py --mock     # 强制演示模式（不调模型，不花钱）
    python server.py --port 8123 --no-open

接口
    GET  /                     web/index.html
    GET  /api/health           健康检查（含 live / mock 判定）
    POST /api/session/start    开局，流式返回第一句
    POST /api/turn             提交一轮回答，流式返回
    POST /api/interrupt        记录一次打断
    POST /api/timing           回传三段耗时（首字 / 首音 / 整轮）
    POST /api/report           生成复盘，落盘 runs/<时间戳>/
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import agent as A

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
RUNS = BASE / "runs"
CONFIG = A.load_config()
SESSIONS = {}
LOCK = threading.Lock()
STATE = {"mock": False}
VERSION = "0.1"


class Session:
    def __init__(self, role: str, rounds: int, level: str = "normal"):
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.created = time.time()
        self.agent = A.InterviewAgent(
            CONFIG, self.id, role, rounds,
            A.LLMClient(CONFIG, force_mock=STATE["mock"]),
            level=level,
        )

    def save(self, report: dict | None = None) -> str:
        try:
            folder = self.agent.save(RUNS, report)
            return str(folder)
        except Exception as e:                                    # 落盘失败不影响对话
            log(f"[warn] 落盘失败: {e}")
            return ""


def log(msg: str):
    print(msg, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"        # 关连接即帧结束，浏览器边收边渲染
    server_version = f"InterviewCoach/{VERSION}"

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):   # 安静一点，只留一行摘要
        if args and str(args[0]).startswith(("GET /api/health", "GET /static")):
            return
        log(f"  {self.address_string()} {fmt % args}")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/health":
            mock = bool(STATE["mock"] or not _has_key())
            self._json({
                "ok": True, "version": VERSION,
                "mode": "mock" if mock else "live",
                "model": CONFIG.get("model"),
                "mock": mock,
                "questions": CONFIG.get("max_questions"),
                "roles": CONFIG.get("roles"),
                "silence_seconds": CONFIG.get("silence_seconds", 5),
                "silence_close_seconds": CONFIG.get("silence_close_seconds", 5),
            })
            return

        if path == "/api/history":
            qs = parse_qs(urlparse(self.path).query)
            fid = (qs.get("id") or [""])[0]
            if fid:
                data = _history_detail(fid)
                return self._json(data, 200 if "error" not in data else 404)
            return self._json({"ok": True, "items": _history_list()})

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path in ("/", "/index.html"):
            return self._file(WEB / "index.html")

        target = (WEB / path.lstrip("/")).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            return self._json({"error": "forbidden"}, 403)
        if target.is_file():
            return self._file(target)
        return self._json({"error": "not found", "path": path}, 404)

    def _file(self, path: Path):
        if not path.is_file():
            return self._json({"error": "missing", "hint": "确认 web/ 目录完整"}, 404)
        data = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        if path.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif path.suffix in (".html", ".css"):
            ctype = f"text/{path.suffix[1:]}; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()

        if path == "/api/session/start":
            role = str(body.get("role") or "ai_pm")
            rounds = int(body.get("rounds") or CONFIG.get("max_questions", 8))
            rounds = max(4, min(20, rounds))
            level = str(body.get("level") or CONFIG.get("level") or "normal")
            return self._start(role, rounds, level)

        if path == "/api/turn":
            return self._turn(body)

        if path == "/api/silence":
            return self._silence(body)

        if path == "/api/interrupt":
            sess = self._session(body)
            if not sess:
                return
            out = sess.agent.interrupt(body.get("ratio"), body.get("spoken"), body.get("heard"))
            return self._json(out)

        if path == "/api/timing":
            sess = self._session(body)
            if not sess:
                return
            sess.agent.set_timing(
                body.get("turn"),
                body.get("first_token_ms"),
                body.get("first_audio_ms"),
                body.get("total_ms"),
            )
            return self._json({"ok": True})

        if path == "/api/report":
            return self._report(body)

        if path == "/api/end":
            sess = self._session(body)
            if not sess:
                return
            sess.agent.ended_at = time.time()
            return self._json({"ok": True, "folder": sess.save()})

        return self._json({"error": "not found", "path": path}, 404)

    # ------------------------------------------------------------ 流式
    def _open_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

    def _emitter(self):
        closed = {"v": False}

        def emit(ev: dict):
            if closed["v"]:
                return
            try:
                self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                closed["v"] = True                 # 用户关页面，停止后续写入

        return emit

    def _start(self, role: str, rounds: int, level: str = "normal"):
        sess = Session(role, rounds, level)
        with LOCK:
            SESSIONS[sess.id] = sess
        self._open_stream()
        emit = self._emitter()
        emit({"t": "session", "id": sess.id, "role": sess.agent.role_key,
              "role_name": sess.agent.role_name, "max_question": sess.agent.max_questions,
              "level": sess.agent.level_key, "level_name": sess.agent.level.get("name", "标准"),
              "mode": sess.agent.llm.mode})
        with sess.lock:
            sess.agent.begin(emit)
        sess.save()

    def _turn(self, body: dict):
        sess = self._session(body, stream=True)
        if not sess:
            return
        self._open_stream()
        emit = self._emitter()
        with sess.lock:
            sess.agent.handle_user(
                str(body.get("text") or ""),
                bool(body.get("asr_empty")),
                emit,
            )
        sess.save()

    def _silence(self, body: dict):
        """候选人不说话时，让面试官主动接一句。

        前端只在「轮到你说 + 15 秒没有任何识别事件 + 没在打字」时才调，
        所以这里不用再判断条件，直接让 agent 决定是催一句还是换个问法。
        """
        sess = self._session(body, stream=True)
        if not sess:
            return
        self._open_stream()
        emit = self._emitter()
        with sess.lock:
            sess.agent.silence(emit)
        sess.save()

    def _report(self, body: dict):
        sess = self._session(body)
        if not sess:
            return
        agent = sess.agent
        if not agent.finished:
            agent.ended_at = agent.ended_at or time.time()
        try:
            report = agent.report()
        except Exception as e:
            return self._json({"error": f"复盘生成失败: {e}"}, 500)
        markdown = A.render_markdown(report)
        folder = sess.save(report)
        self._json({"report": report, "markdown": markdown, "folder": folder})

    # ------------------------------------------------------------ 工具
    def _session(self, body: dict, stream: bool = False):
        sid = str(body.get("session") or "")
        with LOCK:
            sess = SESSIONS.get(sid)
        if not sess:
            if stream:
                self._open_stream()
                try:
                    self.wfile.write((json.dumps({
                        "t": "err", "msg": "会话不存在或已结束，请刷新页面重新开始"
                    }, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    pass
            else:
                self._json({"error": "session not found"}, 404)
            return None
        return sess


def _has_key() -> bool:
    return bool((os.environ.get("DEEPSEEK_API_KEY") or CONFIG.get("api_key") or "").strip())


def _port_busy(port: int) -> bool:
    """端口上有没有人在听。

    只靠 bind 成功与否判断不够：Windows 上 HTTP 服务默认带 SO_REUSEADDR，
    两个进程能绑同一个端口，新进程绑得上，连接却可能被旧进程接走——
    这比直接报错更难查（实测两个 PID 同时 LISTENING 同一个端口）。
    所以先连一下：连得通就说明有人，跳过去。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_edge() -> str | None:
    """找 Edge 的可执行文件。语音识别只在这条路上稳。"""
    if not sys.platform.startswith("win"):
        return None
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        exe = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if exe.exists():
            return str(exe)
    return None


def open_browser(url: str) -> str:
    """优先用 Edge 打开页面；找不到 Edge 才回落到系统默认浏览器。

    Chrome 的 Web Speech 走境外服务，国内经常连不上——而语音是这个产品的入口，
    所以宁可指定浏览器，也不要让用户撞一次「点了没反应」。
    """
    exe = _find_edge()
    if exe:
        try:
            subprocess.Popen(
                [exe, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return "edge"
        except OSError:
            pass
    webbrowser.open(url)
    return "default"


def _history_list(limit: int = 30) -> list:
    """列出 runs/ 下的往期对练：时间、岗位、分数、结论。"""
    items = []
    if not RUNS.exists():
        return items
    folders = sorted((p for p in RUNS.iterdir() if p.is_dir()), reverse=True)[:limit]
    for folder in folders:
        info = {
            "id": folder.name, "time": "", "role": "", "score": None,
            "verdict": "", "questions": None, "max_questions": None,
            "mode": "", "finished": False, "has_report": False,
        }
        sess = folder / "session.json"
        if sess.exists():
            try:
                d = json.loads(sess.read_text(encoding="utf-8"))
                info.update({
                    "time": d.get("started_at", ""),
                    "role": d.get("role_name", ""),
                    "finished": bool(d.get("finished")),
                    "mode": d.get("mode", ""),
                    "max_questions": d.get("max_questions"),
                    "questions": (d.get("metrics") or {}).get("questions"),
                })
            except (OSError, ValueError):
                pass
        rep = folder / "report.json"
        if rep.exists():
            try:
                r = json.loads(rep.read_text(encoding="utf-8"))
                info["score"] = r.get("score")
                info["verdict"] = r.get("verdict", "")
                info["has_report"] = True
            except (OSError, ValueError):
                pass
        items.append(info)
    return items


def _history_detail(fid: str) -> dict:
    """单场详情：复盘 + 完整对话（面试官和候选人两边的话）。"""
    root = RUNS.resolve()
    folder = (RUNS / fid).resolve()
    try:
        folder.relative_to(root)
    except ValueError:
        return {"error": "forbidden"}
    if not folder.is_dir():
        return {"error": "not found"}

    out = {"id": fid, "report": None, "turns": [], "time": "", "role": ""}
    rep = folder / "report.json"
    if rep.exists():
        try:
            out["report"] = json.loads(rep.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    sess = folder / "session.json"
    if sess.exists():
        try:
            d = json.loads(sess.read_text(encoding="utf-8"))
            out["time"] = d.get("started_at", "")
            out["role"] = d.get("role_name", "")
            out["turns"] = [
                {"role": t.get("role"), "text": t.get("text", ""),
                 "action": t.get("action_label", "")}
                for t in d.get("turns", [])
            ]
        except (OSError, ValueError):
            pass
    return out


def main():
    parser = argparse.ArgumentParser(description="面试陪练 Agent · 本地服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mock", action="store_true", help="强制演示模式，不调用模型")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    STATE["mock"] = args.mock
    RUNS.mkdir(parents=True, exist_ok=True)

    if not WEB.joinpath("index.html").exists():
        log("[fatal] 缺少 web/index.html，请确认目录完整")
        return 1

    mode = "演示模式（本地脚本扮演面试官）" if (args.mock or not _has_key()) else f"真实模型 · {CONFIG.get('model')}"
    port = args.port
    httpd = None
    skipped = []
    for candidate in range(port, port + 12):
        if _port_busy(candidate):          # 有人先听着，别硬绑上去
            skipped.append(candidate)
            continue
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            skipped.append(candidate)
            continue
    if httpd is None:
        log("端口都被占用了，换一个吧：python server.py --port 8123")
        return 1

    url = f"http://127.0.0.1:{port}/"
    log("")
    log("  面试陪练 Agent v0.1")
    log(f"  {url}")
    if skipped:
        # 以前这里会静默地"换个端口"，甚至（Windows 上）两个进程绑同一个端口、
        # 请求随机落到旧进程。那个坑排查了两小时，所以必须把话说清楚。
        log(f"  [!] 端口 {', '.join(map(str, skipped))} 上已经有服务在跑，改起在 {port}"
            f"（本进程 PID {os.getpid()}）")
        log("      同一端口上跑两个服务 = 请求随机落到旧进程，先关掉旧的再重启")
    log(f"  模式：{mode}")
    price = CONFIG.get("price", {})
    log(f"  配置：{CONFIG.get('max_questions')} 个话题 / 每题最多追问 "
        f"{CONFIG.get('max_probe_per_question')} 次 / "
        f"输入 ¥{price.get('input_miss')} · 输出 ¥{price.get('output')}（每百万 token）")
    log("  停止：Ctrl+C")
    log("")

    if not args.no_open:
        threading.Timer(0.6, lambda: open_browser(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\n  已停止。会话记录在 runs/ 目录。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    # 直接开在控制台时，交给 Windows 的 Unicode 接口输出（任何 chcp 下中文都正常）；
    # 只有被重定向到文件/管道时才固定 UTF-8，避免编码报错。
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
