# -*- coding: utf-8 -*-
"""dev/probe-report.py —— 手动验证：判定树 + 复盘编号归位（不进产品链路）

    python dev/probe-report.py

跑一场 4 轮的短会，逐轮打印 decide() 的动作与依据，再取一次复盘，
按前端的规则（i → pairs[k-1]）检查每道题到底有没有被点评到。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def post(path, payload, timeout=240):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")
    return [json.loads(l) for l in raw.splitlines() if l.strip()]


def post_json(path, payload, timeout=240):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


RESUME = """曾依依　江西科技师范大学 虚拟现实技术 2027 届
项目：面试陪练 Agent（独立）。把"该追问还是该换题"写成 7 条判定树，模型只负责措辞。
项目：竞品体检 Agent（独立）。六步流水线 + 两道人工卡点，210 个评测判定点。
项目：海报提案 Agent（独立）。90-120 分钟压到 41 秒，客户没提的字段一律留空。"""

ANSWERS = [
    "七条判定树按顺序判，先看有没有具体数字，没有就追问，连续两轮没进展才换题。换题上限设成 2。",
    "嗯留在提示词里我认为第1是不够嗯不好意思就够问题我也也不太清楚",
    "竞品体检用 30 个案例乘 7 个字段等于 210 个判定点，其中 120 个本来就该留空，剩下 90 个才算对错。",
    "海报提案 30 条批量的中位数是 41 秒，原来流程要 90 到 120 分钟。",
]


def main():
    start = post("/api/session/start", {
        "role": "general", "rounds": 5, "level": "normal",
        "resume_text": RESUME, "resume_name": "简历.md", "form": "formal",
    })
    sid = next(e.get("session") or e.get("id") for e in start if e.get("t") == "session")
    print("session:", sid)

    for i, a in enumerate(ANSWERS, 1):
        evs = post("/api/turn", {"session": sid, "text": a})
        meta = next((e for e in evs if e.get("t") == "meta"), {})
        print(f"A{i} → {meta.get('action'):<7} | {meta.get('reason')}")

    print("\n=== 复盘 ===")
    t0 = time.time()
    rep = post_json("/api/report", {"session": sid})["report"]
    print("耗时 %.1fs | note: %s" % (time.time() - t0, rep.get("note")))
    pr = rep.get("per_round") or []
    pairs = rep.get("pairs") or []
    print("pairs:", len(pairs), "| per_round:", len(pr))
    for r in pr:
        print(f"   第 {r.get('i')} 题 | sample {len(r.get('sample') or ''):>3} 字 | {r.get('q')}")

    print("\n归位检查（前端按这个贴）：")
    for k, p in enumerate(pairs, 1):
        hit = next((r for r in pr if int(r.get("i") or 0) == k), None)
        ok = "看参考答法" if (hit and hit.get("sample")) else "看原话"
        print(f"  Q{k} {p['q'][:32]:<34} → {ok}")


if __name__ == "__main__":
    main()
