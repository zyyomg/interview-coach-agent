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
import base64
import io
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import agent as A

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
RUNS = BASE / "runs"
CONFIG = A.load_config()
SESSIONS = {}
LOCK = threading.Lock()
STATE = {"mock": False}
VERSION = "0.1"

# 上传体积上限。只挡误传的大文件（视频、安装包、整本画册），不是内容限制：
# 解析只取文字，字数另有 RESUME_MAX_CHARS 管着，体积再大也是白传。
# 定 20MB 是因为图片多的作品集 PDF 就是 5MB 量级——原先把上限写 5MB，反而挡住正常册子。
UPLOAD_MAX_BYTES = 20 * 1024 * 1024
RESUME_MAX_CHARS = 20000                 # 解析后保留的字数，防粘贴一整本书
# 作品集是多份文件拼起来的，单份按简历的额度读，合并后另设一个上限：
# 再多也没用——进提示词的只有 config 里 portfolio.max_chars 那点字。
PORTFOLIO_MAX_CHARS = 30000
PORTFOLIO_MAX_FILES = int((CONFIG.get("portfolio") or {}).get("max_files", 6))


def _clean_resume(text: str, limit: int = RESUME_MAX_CHARS) -> str:
    """把解析出来的简历压干净：去零宽字符、压掉多余空行、限制长度。"""
    t = (text or "").replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()[:limit]


def _decode_text(raw: bytes) -> str:
    """中文简历常见的几种编码都试一遍。Word 存出来的 txt 常是 GBK。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _docx_text(raw: bytes) -> str:
    """.docx 就是一个 zip：读 word/document.xml，按段落抽 <w:t> 的文字。

    只用标准库（zipfile + ElementTree）。表格里的段落也能抽到，
    因为 w:p 在 w:tbl 里同样是 w:p。
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines = []
    for p in root.iter(ns + "p"):
        buf = []
        for node in p.iter():
            if node.tag == ns + "t" and node.text:
                buf.append(node.text)
            elif node.tag == ns + "tab":
                buf.append(" ")
            elif node.tag == ns + "br":
                buf.append("\n")
        line = "".join(buf).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------- PDF

def _pdf_streams(raw: bytes) -> list:
    """把 PDF 里所有 stream 抽出来，返回 [(对象号, 内容)]，FlateDecode 的顺手解压。

    不做完整语法解析，只按 "stream ... endstream" 切、看前面 1200 字节里有没有
    FlateDecode。对象号靠往前找最近的 "N 0 obj"。
    """
    out = []
    pos = 0
    for m in re.finditer(rb"stream\r?\n", raw):
        start = m.end()
        # 字体文件是压缩二进制，里面照样可能出现 "stream" / "endstream" 字节；
        # 真正的流一定紧跟 ">>"，而且不会落在上一段流里面。
        if m.start() < pos or b">" not in raw[max(0, m.start() - 4):m.start()]:
            continue
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        pos = end + 9
        head = raw[max(0, m.start() - 800):m.start()]
        num = -1
        for om in re.finditer(rb"(\d+)\s+\d+\s+obj\b", head):
            num = int(om.group(1))
        body = raw[start:end].rstrip(b"\r\n")
        if b"FlateDecode" in head[-400:]:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                try:
                    body = zlib.decompressobj().decompress(body)
                except zlib.error:
                    continue
        out.append((num, body))
    return out


def _pdf_page_streams(raw: bytes, streams: list) -> list:
    """只留页面内容流（/Contents 指向的那些）。

    不筛的话，字体文件里恰好有 "BT" 字节也会被当成内容流解，
    解出来是一段乱码，混在简历后面。
    """
    refs = set()
    for m in re.finditer(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", raw):
        for n in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1)):
            refs.add(int(n))
    if refs:
        picked = [body for num, body in streams if num in refs]
        if picked:
            return picked
    return [body for _, body in streams]


def _utf16be(hexstr: bytes) -> str:
    try:
        return bytes.fromhex(hexstr.decode()).decode("utf-16-be", "ignore")
    except ValueError:
        return ""


def _pdf_cmap(streams: list) -> dict:
    """收集 ToUnicode：CID -> 字符。

    Identity-H 的中文 PDF，字符串里存的是字形编号，不查这张表只会得到乱码。
    多个字体的表合并到一起（简历一般就一两个字体），冲突时先到先得。
    """
    cmap = {}
    for _, s in streams:
        if b"beginbfchar" not in s and b"beginbfrange" not in s:
            continue
        for block in re.findall(rb"beginbfchar(.*?)endbfchar", s, re.S):
            for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                cmap[int(src, 16)] = _utf16be(dst)
        for block in re.findall(rb"beginbfrange(.*?)endbfrange", s, re.S):
            for lo, hi, dst in re.findall(
                    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                a, b, base = int(lo, 16), int(hi, 16), int(dst, 16)
                if b < a or b - a > 65535:
                    continue
                for k in range(b - a + 1):
                    cmap.setdefault(a + k, chr(base + k))
    return cmap


def _pdf_unescape(s: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(s)
    table = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
             0x28: 40, 0x29: 41, 0x5C: 92}
    while i < n:
        c = s[i]
        if c != 0x5C or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt in table:
            out.append(table[nxt])
            i += 2
        elif 0x30 <= nxt <= 0x37:            # 八进制
            j, val = i + 1, 0
            while j < n and j < i + 4 and 0x30 <= s[j] <= 0x37:
                val = val * 8 + (s[j] - 0x30)
                j += 1
            out.append(val & 0xFF)
            i = j
        elif nxt in (10, 13):                # 续行
            i += 2
        else:
            out.append(nxt)
            i += 2
    return bytes(out)


def _pdf_objects(raw: bytes) -> dict:
    """{对象号: 对象体}。只用来读字体字典这类小对象。"""
    objs = {}
    for m in re.finditer(rb"(\d+)\s+\d+\s+obj\b(.*?)endobj", raw, re.S):
        objs.setdefault(int(m.group(1)), m.group(2))
    return objs


def _pdf_font_maps(raw: bytes, streams: list) -> dict:
    """{资源名 /F1: (cmap, 是否两字节)}。

    一份 PDF 里常有多个子集字体，每个字体自己的字形编号各不相同。
    全并成一张表会出现「同一个编号在两个字体里是两个字」，正文后面就挂一串乱码。
    """
    objs = _pdf_objects(raw)
    by_num = {num: body for num, body in streams}
    names = {}
    for m in re.finditer(rb"/Font\s*<<(.*?)>>", raw, re.S):
        for name, num in re.findall(rb"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+\d+\s+R", m.group(1)):
            names[name.decode("latin-1")] = int(num)
    out = {}
    for name, num in names.items():
        body = objs.get(num, b"")
        tm = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", body)
        if not tm:
            out[name] = ({}, False)
            continue
        cm = _pdf_cmap([(-1, by_num.get(int(tm.group(1)), b""))])
        out[name] = (cm, any(k > 0xFF for k in cm))
    return out


def _pdf_join_wrapped(lines: list) -> list:
    """把「同一个英文单词被切成两行」的碎片接回去。

    PDF 里英文常被拆成多个 Tj（"Py" + "thon"），中文一般整行走。
    """
    out = []
    for ln in lines:
        if out and re.search(r"[A-Za-z0-9]$", out[-1]) and re.match(r"[a-z0-9]", ln):
            out[-1] += ln
        else:
            out.append(ln)
    return out


def _pdf_text_builtin(raw: bytes) -> str:
    """内置解析：只用标准库，能读「有文字层」的 PDF。

    限制说清楚：扫描件、图片型 PDF、加密 PDF 读不出来——这不是偷懒，
    是文件里本来就没有字，只有图。
    """
    streams = _pdf_streams(raw)
    cmap = _pdf_cmap(streams)
    wide = any(k > 0xFF for k in cmap)       # Identity-H：一个字两个字节
    fonts = _pdf_font_maps(raw, streams)
    use_cmap, use_wide = cmap, wide
    last_name = ""
    lines = []
    for data in _pdf_page_streams(raw, streams):
        if b"BT" not in data:
            continue
        cur = []
        i, n = 0, len(data)
        while i < n:
            c = data[i]
            if c in b" \t\r\n":
                i += 1
            elif c == 0x25:                       # % 注释
                j = data.find(b"\n", i)
                i = n if j < 0 else j + 1
            elif c == 0x28 or (c == 0x3C and data[i + 1:i + 2] != b"<"):
                if c == 0x28:
                    depth, j = 1, i + 1
                    while j < n and depth:
                        if data[j] == 0x5C:
                            j += 2
                            continue
                        if data[j] == 0x28:
                            depth += 1
                        elif data[j] == 0x29:
                            depth -= 1
                        j += 1
                    bs = _pdf_unescape(data[i + 1:j - 1])
                    i = j
                else:
                    j = data.find(b">", i + 1)
                    if j < 0:
                        break
                    hx = re.sub(rb"\s", b"", data[i + 1:j])
                    if len(hx) % 2:
                        hx += b"0"
                    try:
                        bs = bytes.fromhex(hx.decode())
                    except ValueError:
                        bs = b""
                    i = j + 1
                if use_wide:
                    codes = [int.from_bytes(bs[k:k + 2], "big") for k in range(0, len(bs) - 1, 2)]
                else:
                    codes = list(bs)
                cur.append("".join(use_cmap.get(x, "" if use_wide else chr(x))
                                   for x in codes if x))
            elif c == 0x2F:                       # /Name
                j = i + 1
                while j < n and data[j] not in b" \t\r\n/[]<>()":
                    j += 1
                last_name = data[i + 1:max(j, i + 1)].decode("latin-1")
                i = max(j, i + 1)
            elif c in b"+-.0123456789":
                j = i + 1
                while j < n and data[j] not in b" \t\r\n/[]<>()":
                    j += 1
                i = max(j, i + 1)
            else:                                 # 操作符
                j = i
                while j < n and data[j] not in b" \t\r\n/[]<>()":
                    j += 1
                op = data[i:j]
                i = max(j, i + 1)
                if op == b"Tf" and last_name in fonts:
                    use_cmap, use_wide = fonts[last_name]
                if op in (b"Tj", b"TJ", b"'", b'"', b"Td", b"TD", b"T*", b"ET", b"Tm"):
                    if cur:
                        lines.append("".join(cur))
                        cur = []
        if cur:
            lines.append("".join(cur))
    return "\n".join(_pdf_join_wrapped(lines))


def _pdf_with_pymupdf(raw: bytes) -> str:
    import pymupdf                                    # 装了就用，中文支持最好
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def _pdf_with_pypdf(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    if getattr(reader, "is_encrypted", False):
        reader.decrypt("")                            # 只试空密码，别的密码不该猜
    return "\n".join((page.extract_text() or "") for page in reader.pages)


_PDF_READABLE = re.compile(
    r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefA-Za-z0-9\s.,;:%()\-/·、，。：；！？（）《》“”‘’…—]")


def _pdf_text_ok(text: str) -> bool:
    """这段抽出来的东西像不像人话。

    有些 PDF 缺字体宽度表，库能吐出「一串控制符」，非空但没用——
    与其把乱码喂给模型，不如换一条路再试。
    """
    s = (text or "").strip()
    if len(s) < 40:
        return False
    good = len(_PDF_READABLE.findall(s))
    return good / max(1, len(s)) > 0.75


def _pdf_text(raw: bytes) -> tuple:
    """PDF 取文字，返回 (text, engine)。

    先试本机装了的库（不写进依赖，装了就用），读出来不像人话就换下一条，
    最后退回内置解析。三条路都可能读不出字——扫描件没有文字层，谁读都一样。
    """
    for engine, fn in (("pymupdf", _pdf_with_pymupdf), ("pypdf", _pdf_with_pypdf)):
        try:
            text = fn(raw)
        except ImportError:
            continue
        except Exception:
            continue
        if _pdf_text_ok(text):
            return text, engine
    text = _pdf_text_builtin(raw)
    return text, "builtin" if _pdf_text_ok(text) else "none"


def parse_resume(name: str, raw: bytes, what: str = "resume") -> dict:
    """解析简历或作品集文件。返回 {ok, text, chars} 或 {ok: False, kind, hint}。

    解析逻辑完全一样（都是"从文档里抽字"），只有出错提示要分清在看什么：
    选作品集时弹一句"可能是图片型简历"，用户会以为自己选错文件了。
    """
    noun = "作品集" if what == "portfolio" else "简历"
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    if ext not in ("docx", "pdf", "txt", "md", "markdown"):
        return {
            "ok": False, "kind": "type",
            "hint": "支持 .docx / .pdf / .txt / .md。老式 .doc 请另存为 .docx，或直接粘贴文字。",
        }
    engine = ""
    if ext == "pdf":
        try:
            text, engine = _pdf_text(raw)
        except Exception:
            text, engine = "", "none"
        text = _clean_resume(text)
        if engine == "none" or len(text) < 40:
            return {
                "ok": False, "kind": "pdf_scan",
                "hint": "这份 PDF 没有文字层（扫描件或图片），读不出字。"
                        "用 Word/WPS 打开另存为 .docx，或把文字粘到下面。",
            }
        return {"ok": True, "text": text, "chars": len(text), "name": name,
                "engine": engine, "kind": what}
    try:
        text = _docx_text(raw) if ext == "docx" else _decode_text(raw)
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return {
            "ok": False, "kind": "broken",
            "hint": "这个文件打不开。如果是扫描件或加密文档，请把文字复制出来粘贴。",
        }
    text = _clean_resume(text)
    if len(text) < 40:
        return {
            "ok": False, "kind": "empty",
            "hint": f"只读到几十个字，可能是图片型{noun}。把文字复制出来粘贴到下面更稳。",
        }
    return {"ok": True, "text": text, "chars": len(text), "name": name, "kind": what}


class Session:
    def __init__(self, role: str, rounds: int, level: str = "normal",
                 resume_text: str = "", resume_name: str = "", form: str = "",
                 portfolio_text: str = "", portfolio_name: str = "",
                 portfolio_files: int = 0):
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.created = time.time()
        self.agent = A.InterviewAgent(
            CONFIG, self.id, role, rounds,
            A.LLMClient(CONFIG, force_mock=STATE["mock"]),
            level=level, resume_text=resume_text, resume_name=resume_name, form=form,
            portfolio_text=portfolio_text, portfolio_name=portfolio_name,
            portfolio_files=portfolio_files,
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
                "turn_silence_seconds": (CONFIG.get("turn") or {}).get("silence_seconds", 4.5),
                "portfolio_chars": (CONFIG.get("portfolio") or {}).get("max_chars", 1600),
                "portfolio_max_files": PORTFOLIO_MAX_FILES,
                "upload_max_bytes": UPLOAD_MAX_BYTES,
            })
            return

        if path == "/api/spend":
            return self._json({"ok": True, **_spend_summary()})

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

        if path == "/api/resume/parse":
            return self._resume(body)

        if path == "/api/session/start":
            # role 既可以是预设 key（general），也可以直接是岗位名。
            # 界面现在只剩「通用岗位」和手填两种，手填由 agent 那边洗（限长、限字符）。
            role = str(body.get("role") or "general").strip()[:24]
            rounds = int(body.get("rounds") or CONFIG.get("max_questions", 8))
            rounds = max(4, min(20, rounds))
            level = str(body.get("level") or CONFIG.get("level") or "normal")
            resume_text = str(body.get("resume_text") or "").strip()[:RESUME_MAX_CHARS]
            resume_name = str(body.get("resume_name") or "").strip()[:120]
            portfolio_text = str(body.get("portfolio_text") or "").strip()[:PORTFOLIO_MAX_CHARS]
            portfolio_name = str(body.get("portfolio_name") or "").strip()[:200]
            try:
                portfolio_files = int(body.get("portfolio_files") or 0)
            except (TypeError, ValueError):
                portfolio_files = 0
            portfolio_files = max(0, min(PORTFOLIO_MAX_FILES, portfolio_files))
            form = str(body.get("form") or "").strip().lower()
            if form not in ("casual", "formal"):
                form = "formal" if resume_text else "casual"
            # 「正式」就是读简历，没有简历不该静悄悄降级成体验式——
            # 用户会以为自己在打正式场，实际问的全是通用题。
            # 作品集替代不了简历：简历问的是"你这个人"，作品集只是产物。
            if form == "formal" and len(resume_text) < 60:
                return self._json({
                    "ok": False, "kind": "no_resume",
                    "hint": "正式面试要读简历：先选一个 .docx / .pdf / .txt，或把简历文字粘进去；"
                            "只想随手练就切回「体验式」。",
                }, 400)
            return self._start(role, rounds, level, resume_text, resume_name, form,
                              portfolio_text, portfolio_name, portfolio_files)

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

    def _resume(self, body: dict):
        """解析简历 / 作品集。文件以 base64 传上来——不引 multipart 解析库，也不落盘。"""
        what = "portfolio" if str(body.get("kind") or "") == "portfolio" else "resume"
        noun = "作品集" if what == "portfolio" else "简历"
        name = str(body.get("name") or "resume")
        data = str(body.get("data") or "")
        if not data:
            return self._json({"ok": False, "kind": "empty", "hint": "没有收到文件内容"}, 400)
        try:
            raw = base64.b64decode(data.split(",")[-1], validate=False)
        except Exception:
            return self._json({"ok": False, "kind": "broken", "hint": "文件读取失败，换一个再试"}, 400)
        if len(raw) > UPLOAD_MAX_BYTES:
            return self._json({
                "ok": False, "kind": "big",
                "hint": f"文件超过 {UPLOAD_MAX_BYTES // 1024 // 1024}MB。"
                        f"单个{noun}文件不该这么大，检查一下是不是选错了。",
            }, 400)
        out = parse_resume(name, raw, what)
        return self._json(out, 200 if out.get("ok") else 400)

    def _start(self, role: str, rounds: int, level: str = "normal",
               resume_text: str = "", resume_name: str = "", form: str = "",
               portfolio_text: str = "", portfolio_name: str = "",
               portfolio_files: int = 0):
        sess = Session(role, rounds, level, resume_text, resume_name, form,
                       portfolio_text, portfolio_name, portfolio_files)
        with LOCK:
            SESSIONS[sess.id] = sess
        self._open_stream()
        emit = self._emitter()
        emit({"t": "session", "id": sess.id, "role": sess.agent.role_key,
              "role_name": sess.agent.role_name, "max_question": sess.agent.max_questions,
              "level": sess.agent.level_key, "level_name": sess.agent.level.get("name", "标准"),
              "mode": sess.agent.llm.mode, "form": sess.agent.form,
              "form_name": sess.agent.form_name,
              "resume": bool(sess.agent.resume_used),
              "resume_chars": len(sess.agent.resume_used),
              "portfolio": bool(sess.agent.portfolio_used),
              "portfolio_chars": len(sess.agent.portfolio_used),
              "portfolio_files": sess.agent.portfolio_files})
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


def _spend_summary() -> dict:
    """接上 Key 以来的累计花费：把 runs/ 里**真实模式**场次的成本加起来。

    演示模式的成本是按价目估算的，没真花钱，所以单独计数、不进合计——
    把估算值混进"花了多少钱"里就是假账。
    """
    total = 0.0
    live_n = 0
    mock_n = 0
    if not RUNS.exists():
        return {"total_cny": 0.0, "live_sessions": 0, "mock_sessions": 0}
    for folder in RUNS.iterdir():
        if not folder.is_dir():
            continue
        sess = folder / "session.json"
        if not sess.exists():
            continue
        try:
            d = json.loads(sess.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cost = float((d.get("metrics") or {}).get("cost_cny") or 0)
        if d.get("mode") == "live":
            total += cost
            live_n += 1
        else:
            mock_n += 1
    return {"total_cny": round(total, 4), "live_sessions": live_n, "mock_sessions": mock_n}


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
