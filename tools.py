#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools.py —— 接上 Key 之后才用得上的两条命令，不参与产品运行时。

    python tools.py check            真实模式自检：Key 有效吗、模型名对吗、
                                     真实一轮的首字延迟和开销是多少
    python tools.py summary          汇总 runs/ 里的往期对练，出 M6 那张表
    python tools.py summary --write  同上，并写进 eval/

为什么单独一个文件：server.py 只负责服务，agent.py 只负责对话引擎。
这两件事都是"人偶尔跑一次"的运维动作，塞进核心文件只会让它们变长。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

import agent as A

BASE = Path(__file__).resolve().parent
RUNS = BASE / "runs"
EVAL = BASE / "eval"


# ---------------------------------------------------------------- check

def _mask(key: str) -> str:
    if not key:
        return "（无）"
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "***"


def _dotenv_has_key(path: Path) -> bool:
    """只看 .env 里有没有写，不管值对不对——值对不对交给接口判断。"""
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            if line.split("=", 1)[1].strip().strip('"').strip("'"):
                return True
    return False


def _fail(msg: str) -> int:
    info = A.classify_error(msg)
    print(f"  失败：{info['kind']} —— {msg[:300]}")
    print(f"  怎么办：{info['hint']}")
    return 1


def _models(client: A.LLMClient) -> list:
    req = urlrequest.Request(
        f"{client.base}/models",
        headers={"Authorization": f"Bearer {client.key}"},
    )
    with urlrequest.urlopen(req, timeout=15) as resp:
        obj = json.loads(resp.read().decode("utf-8", "ignore"))
    return [str(m.get("id")) for m in (obj.get("data") or []) if m.get("id")]


def cmd_check(args) -> int:
    # 系统环境变量要在 load_config 之前读，否则 .env 灌进去之后就分不清来源了
    sys_env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    cfg = A.load_config()
    client = A.LLMClient(cfg)

    print("")
    print("  面试陪练 Agent · 真实模式自检")
    print("")

    if not client.key:
        print("  没有找到 DEEPSEEK_API_KEY —— 现在跑起来是演示模式（面试官由本地脚本扮演）。")
        print("")
        print("  接真实模型只差一行，二选一：")
        print(f"    1) 在 {BASE / '.env'} 里写 DEEPSEEK_API_KEY=sk-xxxx")
        print("    2) 临时用环境变量：$env:DEEPSEEK_API_KEY=\"sk-xxxx\"")
        print("")
        print("  改完重新跑 python tools.py check。")
        return 2

    source = "系统环境变量" if sys_env else (".env" if _dotenv_has_key(BASE / ".env") else "config.json")
    print(f"  Key      {_mask(client.key)}（来自{source}）")
    print(f"  模型     {client.model}")
    print(f"  地址     {client.base}")
    print("")

    print("  [1/2] 校验 Key 与模型名 …")
    try:
        ids = _models(client)
    except HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        if e.code == 404:
            print("        该端点没有 /models 列表，跳过模型名校验，直接跑一轮。")
        else:
            return _fail(f"HTTP {e.code}: {detail}")
    except Exception as e:
        return _fail(str(e))
    else:
        if ids and client.model not in ids:
            print(f"        模型名对不上：账号里能用的有 {', '.join(ids[:8])}")
            print(f"        怎么办：把 config.json 的 model 从 {client.model} 改成上面任意一个")
            return 1
        print(f"        OK，{client.model} 在可用列表里（共 {len(ids)} 个模型）")

    print("  [2/2] 发一轮真实对话（约 30 token，几厘钱）…")
    t0 = time.time()
    first_ms = None
    buf = []
    usage = {}
    try:
        for ev in client.stream(
            [{"role": "user", "content": "只回两个字：收到"}],
            temperature=0.2, max_tokens=16,
        ):
            if ev.get("type") == "token":
                if first_ms is None:
                    first_ms = (time.time() - t0) * 1000
                buf.append(ev["text"])
            elif ev.get("type") == "usage":
                usage = ev
    except Exception as e:
        return _fail(str(e))

    text = "".join(buf).strip() or "（空）"
    total_ms = (time.time() - t0) * 1000
    cost = A.cost_cny(usage, cfg.get("price", {}))
    est = "（估算）" if usage.get("estimated") else ""
    window = "高峰" if A.is_peak() else "空闲·半价"

    print(f"        返回「{text[:40]}」")
    print("")
    print(f"  首字     {first_ms:.0f} ms" if first_ms is not None else "  首字     —")
    print(f"  整轮     {total_ms:.0f} ms")
    print(f"  用量     输入 {int(usage.get('miss', 0)) + int(usage.get('hit', 0))} / "
          f"输出 {int(usage.get('out', 0))} token，本轮 ¥{cost:.4f}（{window}）{est}")
    print("")
    print("  OK 真实模式可用。下一步：python server.py")
    print("")
    return 0


# ---------------------------------------------------------------- summary

def _collect() -> list:
    rows = []
    if not RUNS.exists():
        return rows
    for folder in sorted((p for p in RUNS.iterdir() if p.is_dir())):
        sess = folder / "session.json"
        if not sess.exists():
            continue
        try:
            d = json.loads(sess.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        m = d.get("metrics") or {}
        actions = m.get("actions") or {}
        itr = m.get("interrupts") or {}
        score = None
        rep = folder / "report.json"
        if rep.exists():
            try:
                score = json.loads(rep.read_text(encoding="utf-8")).get("score")
            except (OSError, ValueError):
                pass
        rows.append({
            "folder": folder.name,
            "started": d.get("started_at", ""),
            "role": d.get("role_name", ""),
            "mode": d.get("mode", ""),
            "questions": m.get("questions") or 0,
            "answered": m.get("answered") or 0,
            "probe": actions.get("probe", 0),
            "switch": actions.get("switch", 0),
            "clarify_rate": m.get("clarify_rate") or 0,
            "interrupts": itr.get("count", 0),
            "cost": m.get("cost_cny") or 0,
            "score": score,
            "estimated": bool((m.get("usage") or {}).get("estimated")),
            "timings": d.get("timings") or {},
        })
    return rows


def _pooled(rows: list, field: str):
    vals = []
    for r in rows:
        for t in r["timings"].values():
            v = (t or {}).get(field)
            if isinstance(v, (int, float)):
                vals.append(v)
    return vals


def _ms(v) -> str:
    return f"{v:.0f}ms" if isinstance(v, (int, float)) else "—"


def _table(rows: list) -> list:
    lines = [
        "| # | 时间 | 岗位 | 话题 | 说几轮 | 首字P50 | 首音P50 | 澄清率 | 追问 | 换题 | 打断 | 成本 | 评分 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lat = r["timings"]
        ft = A.pct([(t or {}).get("first_token_ms") for t in lat.values()], 0.5)
        fa = A.pct([(t or {}).get("first_audio_ms") for t in lat.values()], 0.5)
        lines.append(
            f"| {i} | {r['started'][5:16]} | {r['role']} | {r['questions']} | {r['answered']} "
            f"| {_ms(ft)} | {_ms(fa)} | {r['clarify_rate'] * 100:.0f}% "
            f"| {r['probe']} | {r['switch']} | {r['interrupts']} "
            f"| ¥{r['cost']:.4f} | {r['score'] if r['score'] is not None else '—'} |"
        )
    if len(rows) > 1:
        ft = A.pct(_pooled(rows, "first_token_ms"), 0.5)
        fa = A.pct(_pooled(rows, "first_audio_ms"), 0.5)
        tot = A.pct(_pooled(rows, "total_ms"), 0.5)
        scores = [r["score"] for r in rows if isinstance(r["score"], (int, float))]
        cost = sum(r["cost"] for r in rows)
        lines.append(
            f"| 合计 | {len(rows)} 场 | | | | {_ms(ft)} | {_ms(fa)} | "
            f"| | | {sum(r['interrupts'] for r in rows)} | ¥{cost:.4f} "
            f"| {sum(scores) / len(scores):.1f} 均 |"
        )
    return lines


def cmd_summary(args) -> int:
    rows = _collect()
    # 开局就报错、一个字都没答的会话不该算进评测：它没有数据，只有一条错误记录
    empty = [r for r in rows if not r["answered"]]
    rows = [r for r in rows if r["answered"]]
    live = [r for r in rows if r["mode"] == "live"]
    mock = [r for r in rows if r["mode"] != "live"]

    out = ["# 评测汇总", ""]
    if not rows and not empty:
        out += [
            "`runs/` 里还没有任何一场记录。",
            "",
            "跑完一场（`python server.py` → 走完 8 个话题 → 结束复盘）之后再执行一次本命令。",
            "",
        ]
    else:
        out += [f"`runs/` 共 {len(rows) + len(empty)} 场"
                f"（计入统计 {len(rows)} 场：真实 {len(live)}、演示 {len(mock)}）。", ""]
        if empty:
            out += [f"另有 {len(empty)} 场 0 轮回答，多半是开局就报错了，不计入统计。", ""]
        out += ["## 真实模式（M6 评测口径）", ""]
        if live:
            out += _table(live)
            note = (f"> 延迟是全部样本合并后的 P50（{sum(len(r['timings']) for r in live)} 轮计时）。"
                    "成本按接口返回的 usage 计，不是估算。")
            est_n = sum(1 for r in live if r["estimated"])
            if est_n:
                note += f"其中 {est_n} 场接口没返回 usage，这几场的 token 是估算值。"
            out += ["", note]
        else:
            out += ["**0 场 —— M6 还没开始。** 下面的演示模式数据只证明链路通，不能当评测数据用。"]
        out += ["", "## 演示模式（只证明链路，不是评测）", ""]
        if mock:
            out += _table(mock)
            out += ["", "> 演示模式的 token 由本地脚本估，成本是估算值，不是实付。"]
        else:
            out += ["（没有）"]
        out += ["", "---", "> 样本量是 1 个人练的 10 场，只写观察到的，不主张泛化结论。", ""]

    text = "\n".join(out)
    print(text)

    if args.write and rows:
        EVAL.mkdir(parents=True, exist_ok=True)
        path = EVAL / f"评测汇总-{time.strftime('%Y%m%d-%H%M')}.md"
        path.write_text(text, encoding="utf-8")
        print(f"已写入 {path}")

    if not live:
        return 3
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    parser = argparse.ArgumentParser(description="面试陪练 Agent · 运维命令")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("check", help="真实模式自检：Key / 模型名 / 真实一轮")
    p1.set_defaults(func=cmd_check)

    p2 = sub.add_parser("summary", help="汇总 runs/ 里的往期对练")
    p2.add_argument("--write", action="store_true", help="把结果写进 eval/")
    p2.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    # 和控制台编码的约定同 server.py：重定向到文件/管道时才固定 UTF-8
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
