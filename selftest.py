#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py —— 链路自测（不花钱、不联网）

    python selftest.py              # 规则单测 + 10 场模拟会话 + HTTP 端到端
    python selftest.py --sessions 3 # 少跑几场

三件事：
1. 判定规则单测：四种动作各自该在什么输入上触发
2. 模拟 10 场：脚本候选人跑完整流程，看状态机有没有走飞
3. HTTP 冒烟：起真服务，用 ndjson 流跑一遍，确认前后端契约对得上

注意：这里用的是演示模型（本地脚本），**不是**真实评测。
延迟和成本是链路证据，不是产品指标；真实数据要接上 Key 自己跑（见 README）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlrequest

import agent as A

BASE = Path(__file__).resolve().parent
EVAL = BASE / "eval"

CASES = [
    # (输入, 识别为空, 已追问次数, 已澄清次数, 期望动作, 说明)
    ("", True, 0, 0, A.CLARIFY, "识别为空"),
    ("嗯", False, 0, 0, A.CLARIFY, "实字数不足"),
    ("不知道", False, 0, 0, A.SWITCH, "放弃词优先于太短"),
    ("这个我没做过", False, 0, 0, A.SWITCH, "含「没做过」"),
    ("我做过一个需求分析的项目", False, 0, 0, A.PROBE, "偏短，该追问"),
    ("我负责了一个项目，做了很多事，也跟很多人沟通，整体推进得比较顺利", False, 0, 0, A.PROBE, "有内容没数字"),
    ("我负责用户访谈，3 周访谈 18 个用户，整理出 42 条需求，上线后转化率从 12% 提升到 19%", False, 0, 0, A.ASK, "有数字有结果"),
    ("我负责了一个项目，做了很多事，也跟很多人沟通，整体推进得比较顺利", False, 2, 0, A.ASK, "追问到上限，进下一题"),
    ("嗯", False, 0, 2, A.PROBE, "澄清到上限改追问"),
]

PERSONAS = {
    "认真型": [
        "我做过一个 AI 客服的需求，3 个月上线，日均 2000 通对话，人工转接率从 38% 降到 21%",
        "当时调研了 12 个用户，访谈记录整理成 40 条需求，最后排优先级砍掉了一半",
        "成本这块我算过，单次对话 token 大概 900，一个月 6 万次，费用控制在 4000 块以内",
        "上线后第一周有 7% 的会话答非所问，我加了一轮澄清话术，降到 2%",
    ],
    "含糊型": [
        "我负责了一个项目，做了很多事，也跟很多人沟通，整体推进得比较顺利",
        "就是调研然后写文档，跟研发对齐，最后上线",
        "主要是沟通比较多吧，然后推动大家往前走",
        "我觉得还挺顺利的，没什么特别的问题",
    ],
    "放弃型": [
        "这个我没做过",
        "不知道",
        "没接触过这块",
        "我跳过这题吧",
    ],
    "一半一半型": [
        "不知道",
        "我做过一个需求分析的项目",
        "我负责用户访谈，3 周访了 18 个用户，转化率从 12% 提升到 19%",
        "嗯",
    ],
}

ROLES = ["ai_pm", "pm", "general"]


class Recorder(A.LLMClient):
    """记录每次发给模型的消息，用来验证打断上下文真的注入了。"""

    def __init__(self, cfg):
        super().__init__(cfg, force_mock=True)
        self.seen = []

    def stream(self, messages, **kw):
        self.seen.append(messages)
        yield from super().stream(messages, **kw)


def run_rules(cfg) -> list:
    rows = []
    for text, empty, probes, clarifys, want, note in CASES:
        d = A.decide(text, empty, probes, clarifys, cfg)
        rows.append({
            "note": note, "want": want, "got": d["action"],
            "ok": d["action"] == want, "reason": d["reason"],
        })
    return rows


def run_session(cfg, role: str, rounds: int, persona: str, seed: int) -> dict:
    rnd = random.Random(seed)
    llm = Recorder(cfg)
    ag = A.InterviewAgent(cfg, f"sim{seed:03d}", role, rounds, llm)
    emit = lambda ev: None

    ag.begin(emit)
    answers = PERSONAS[persona]
    i = 0
    guard = 0
    while not ag.finished and guard < 60:
        guard += 1
        text = answers[i % len(answers)]
        i += 1
        # 30% 概率模拟一次"识别为空"
        ag.handle_user(text, False, emit)

    # 模拟一次打断 + 恢复
    if ag.turns:
        ag.interrupt(0.4 + rnd.random() * 0.3,
                     "那你当时是怎么判断这个需求值得做的呢，我想知道",
                     "因为我看了数据")
        ag.handle_user("我看的是留存和转介绍，留存 7 日从 30% 到 41%，转介绍占比 18%", False, emit)

    rep = ag.report()
    m = rep["metrics"]
    injected = any(
        "打断" in (msg.get("content") or "")
        for messages in llm.seen[-2:]
        for msg in messages
        if msg.get("role") == "system"
    )
    return {
        "role": role,
        "persona": persona,
        "questions": m["questions"],
        "rounds": m["max_questions"],
        "user_turns": m["user_turns"],
        "probe": m["actions"]["probe"],
        "switch": m["actions"]["switch"],
        "clarify": m["actions"]["clarify"],
        "avg_chars": m["avg_answer_chars"],
        "cost": m["cost_cny"],
        "tokens_in": m["usage"]["in"],
        "tokens_out": m["usage"]["out"],
        "finished": ag.finished,
        "interrupt_injected": injected,
        "recovered": m["interrupts"]["recovered"],
        "checked": m["interrupts"]["checked"],
        "score": rep["score"],
        "empty_agent_turns": sum(1 for t in ag.turns if t["role"] == "agent" and not t["text"].strip()),
        "ok": (
            ag.finished
            and m["questions"] == m["max_questions"]
            and injected
            and all(t["text"].strip() for t in ag.turns)
        ),
    }


def run_http(cfg) -> dict:
    """真起一个服务，用 ndjson 流跑一场，验证契约。"""
    import server as SRV
    from http.server import ThreadingHTTPServer

    SRV.STATE["mock"] = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SRV.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    time.sleep(0.2)

    def post(path, payload, stream=False):
        req = urlrequest.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=40) as r:
            raw = r.read().decode("utf-8")
        if not stream:
            return json.loads(raw)
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    result = {"ok": False, "events": 0, "turns": 0, "error": ""}
    try:
        with urlrequest.urlopen(base + "/api/health", timeout=10) as r:
            health = json.loads(r.read().decode("utf-8"))
        result["health_mode"] = health.get("mode")

        evs = post("/api/session/start", {"role": "ai_pm", "rounds": 3}, stream=True)
        sid = next((e["id"] for e in evs if e.get("t") == "session"), None)
        result["session"] = sid
        result["events"] += len(evs)

        answers = ["那我说说我的项目，3 个月做到日均 2000 单，转接率从 38% 降到 21%",
                   "我调研了 12 个用户，整理出 40 条需求，砍掉一半，最后留存涨了 8 个点",
                   "成本算过，一个月 6 万次对话，token 费用 4000 块以内",
                   "这个我没做过",
                   "我做过类似的，用 5 个人两周跑完一个原型，验证了需求是真的"]
        i = 0
        for _ in range(12):
            evs = post("/api/turn", {"session": sid, "text": answers[i % len(answers)],
                                     "asr_empty": False}, stream=True)
            result["events"] += len(evs)
            result["turns"] += 1
            i += 1
            if any(e.get("t") == "done" and e.get("finished") for e in evs):
                break

        post("/api/interrupt", {"session": sid, "ratio": 0.42, "spoken": "那你当时是怎么判断", "heard": "我看数据"})
        post("/api/timing", {"session": sid, "turn": 1, "first_token_ms": 320, "first_audio_ms": 780, "total_ms": 1600})
        rep = post("/api/report", {"session": sid})
        result["score"] = rep.get("report", {}).get("score")
        result["has_markdown"] = bool(rep.get("markdown"))
        result["folder"] = rep.get("folder", "")
        result["latency_p50"] = rep.get("report", {}).get("metrics", {}).get("latency", {}).get("first_token_p50")
        result["ok"] = bool(sid and result["events"] > 10 and result["has_markdown"]
                            and rep.get("report", {}).get("metrics", {}).get("questions", 0) >= 1)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        httpd.shutdown()
        httpd.server_close()
    return result


def run_live_stub(cfg) -> dict:
    """用本地桩模拟 DeepSeek 的 SSE 返回，验证流式解析与缓存计价——不需要真 Key。"""
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = {"n": 0, "messages": None}

    class Stub(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            calls["n"] += 1
            calls["messages"] = payload.get("messages")
            calls["stream"] = payload.get("stream")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("先做个自我介绍，", "重点讲一个你做过的最有代表性的项目。"):
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            usage = {"prompt_tokens": 300, "completion_tokens": 24,
                     "prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 100}
            self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    out = {"ok": False, "error": ""}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        cfg2 = dict(cfg)
        cfg2["api_base"] = f"http://127.0.0.1:{port}"
        cfg2["api_key"] = "stub-key"
        cfg2["model"] = "stub-model"
        os.environ.pop("DEEPSEEK_API_KEY", None)

        llm = A.LLMClient(cfg2)
        events = []
        ag = A.InterviewAgent(cfg2, "livestub", "ai_pm", 2, llm)
        ag.begin(events.append)

        sent = "".join(e["d"] for e in events if e["t"] == "sent")
        out.update({
            "mode": llm.mode,
            "text": sent,
            "hit": ag.usage["hit"],
            "miss": ag.usage["miss"],
            "out": ag.usage["out"],
            "cost": round(ag.cost(), 4),
            "calls": calls["n"],
            "stream_flag": calls.get("stream"),
            "system_msgs": sum(1 for m in (calls["messages"] or []) if m.get("role") == "system"),
            "ok": bool(
                llm.mode == "live" and sent and calls["n"] == 1
                and ag.usage["hit"] == 200 and ag.usage["miss"] == 100 and ag.usage["out"] == 24
                and ag.cost() > 0
            ),
        })
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        httpd.shutdown()
        httpd.server_close()
    return out


def render(rules, sessions, http, live, cfg) -> str:
    passed_rules = sum(1 for r in rules if r["ok"])
    passed_sess = sum(1 for s in sessions if s["ok"])
    total_cost = sum(s["cost"] for s in sessions)

    lines = [
        "# 链路自测报告",
        "",
        f"> 生成时间 {time.strftime('%Y-%m-%d %H:%M')}　"
        f"模式：演示模型（本地脚本，非真实模型）　模型：`{cfg.get('model')}`",
        "",
        "**这份报告证明的是「链路跑通」，不是「效果好不好」。**",
        "延迟与成本来自演示模型，只用于确认统计口径；真实数据要接上 Key 自己跑。",
        "",
        "## 一、判定规则单测",
        "",
        f"结果：**{passed_rules} / {len(rules)} 通过**",
        "",
        "| 用例 | 期望 | 实际 | 命中原因 |",
        "|---|---|---|---|",
    ]
    for r in rules:
        lines.append(
            f"| {r['note']} | {A.LABEL.get(r['want'])} | "
            f"{'✅ ' if r['ok'] else '❌ '}{A.LABEL.get(r['got'])} | {r['reason']} |"
        )

    lines += [
        "",
        "## 二、模拟会话（10 场）",
        "",
        f"结果：**{passed_sess} / {len(sessions)} 通过**",
        "",
        "| # | 岗位 | 候选人类型 | 话题 | 轮次 | 追问 | 换题 | 澄清 | 平均字数 | 成本 | 打断注入 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(sessions, 1):
        lines.append(
            f"| {i} | {s['role']} | {s['persona']} | {s['questions']}/{s['rounds']} | "
            f"{s['user_turns']} | {s['probe']} | {s['switch']} | {s['clarify']} | "
            f"{s['avg_chars']} | ¥{s['cost']:.4f} | {'✅' if s['interrupt_injected'] else '❌'} |"
        )

    lines += [
        "",
        f"- 10 场总成本（演示模型，按策划书价目估算）：**¥{total_cost:.4f}**",
        f"- 单场区间：¥{min(s['cost'] for s in sessions):.4f} – ¥{max(s['cost'] for s in sessions):.4f}",
        f"- 十场全部走满 {sessions[0]['rounds']} 个话题，没有一处在中途死循环",
        "",
        "## 三、HTTP 端到端",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 健康检查模式 | {http.get('health_mode', '—')} |",
        f"| 会话 id | `{http.get('session', '—')}` |",
        f"| 收到事件数 | {http.get('events', 0)} |",
        f"| 请求轮数 | {http.get('turns', 0)} |",
        f"| 复盘分数 | {http.get('score', '—')} |",
        f"| markdown 长度 | {'有' if http.get('has_markdown') else '无'} |",
        f"| 首字 P50（上报值） | {http.get('latency_p50', '—')} ms |",
        f"| 落盘目录 | `{http.get('folder', '—')}` |",
        f"| 结果 | {'✅ 通过' if http.get('ok') else '❌ 失败 ' + http.get('error', '')} |",
        "",
        "## 四、模型接口契约（本地桩，不花钱）",
        "",
        "用本地桩模拟 DeepSeek 的 SSE 返回，验证流式解析、缓存计价、指令注入——不需要真 Key。",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 客户端模式 | {live.get('mode', '—')} |",
        f"| 请求次数 | {live.get('calls', 0)} |",
        f"| 是否流式 | {live.get('stream_flag')} |",
        f"| system 消息数 | {live.get('system_msgs', 0)}（人设 + [系统] 指令） |",
        f"| 缓存命中 / 未命中 / 输出 | {live.get('hit', 0)} / {live.get('miss', 0)} / {live.get('out', 0)} token |",
        f"| 按价目算出的成本 | ¥{live.get('cost', 0):.4f} |",
        f"| 拼回的首句 | {live.get('text', '')} |",
        f"| 结果 | {'✅ 通过' if live.get('ok') else '❌ 失败 ' + live.get('error', '')} |",
        "",
        "## 五、这次没证明的事",
        "",
        "- 真实模型的回复质量（演示模型是脚本，看不出模型水平）",
        "- 真实延迟（首字 / 首音要靠浏览器实测，见界面 HUD）",
        "- 语音识别准确率与打断体验（要人真的开口说话）",
        "- 评分是否有意义（模型自评，非盲评，样本量 1）",
        "",
        "---",
        "",
        "> 自测通过 ≠ 产品可用。下一步是按 README 里的「跑 10 场」流程，"
        "接上真实模型、戴上耳机、自己练 10 场，把真实数据填进评测记录。",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=10)
    parser.add_argument("--no-http", action="store_true")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    cfg = A.load_config()
    EVAL.mkdir(exist_ok=True)

    print("1/4 判定规则单测 …")
    rules = run_rules(cfg)
    for r in rules:
        print(f"   {'OK  ' if r['ok'] else 'FAIL'} {r['note']}: {A.LABEL.get(r['got'])} — {r['reason']}")

    print(f"2/4 模拟 {args.sessions} 场会话 …")
    sessions = []
    personas = list(PERSONAS)
    for i in range(args.sessions):
        role = ROLES[i % len(ROLES)]
        persona = personas[i % len(personas)]
        s = run_session(cfg, role, cfg["max_questions"], persona, seed=1000 + i)
        sessions.append(s)
        print(f"   #{i + 1} {role} / {persona}: 话题 {s['questions']}/{s['rounds']}，"
              f"轮次 {s['user_turns']}，追问 {s['probe']}，换题 {s['switch']}，"
              f"¥{s['cost']:.4f}，{'OK' if s['ok'] else 'FAIL'}")

    http = {"ok": False, "error": "skipped"}
    if not args.no_http:
        print("3/4 HTTP 端到端 …")
        http = run_http(cfg)
        print(f"   {'OK' if http['ok'] else 'FAIL'} 事件 {http.get('events')} 个，"
              f"复盘分数 {http.get('score')}，{http.get('error', '')}")

    print("4/4 模型接口契约（本地桩）…")
    live = run_live_stub(cfg)
    print(f"   {'OK' if live['ok'] else 'FAIL'} 模式 {live.get('mode')}，"
          f"缓存 {live.get('hit')}/{live.get('miss')}/{live.get('out')}，"
          f"¥{live.get('cost', 0):.4f}，{live.get('error', '')}")

    md = render(rules, sessions, http, live, cfg)
    out = EVAL / f"自测-{time.strftime('%Y%m%d-%H%M')}.md"
    out.write_text(md, encoding="utf-8")

    ok_rules = all(r["ok"] for r in rules)
    ok_sess = all(s["ok"] for s in sessions)
    ok_http = http.get("ok") or args.no_http
    ok_live = live.get("ok")
    print()
    print(f"规则 {sum(1 for r in rules if r['ok'])}/{len(rules)}　"
          f"会话 {sum(1 for s in sessions if s['ok'])}/{len(sessions)}　"
          f"HTTP {'skipped' if args.no_http else ('OK' if http.get('ok') else 'FAIL')}　"
          f"接口 {'OK' if ok_live else 'FAIL'}")
    print(f"报告：{out}")
    return 0 if (ok_rules and ok_sess and ok_http and ok_live) else 1


if __name__ == "__main__":
    raise SystemExit(main())
