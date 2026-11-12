#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m6-prerun.py —— M6 文本链路预跑。

测的不是「真人语音体验」，是「逻辑层能不能撑住 10 场」：
会不会卡死在同一题、8 个话题能不能走完、追问/换题/澄清是否按设计触发、单场多少钱。

和 M6评测方案.md 的区别必须说清楚：
  · 这里没有麦克风、没有 TTS、没有打断。曲线救国的是「文本轮次压测」。
  · 产物写到 runs-m6-text/，不进 runs/，不参与 tools.py summary 的统计。
  · 结论只能写「文本链路」，不能写成「真人评测」。

用法：
    python dev/m6-prerun.py            # 跑满 10 场
    python dev/m6-prerun.py --only 1   # 只跑第 1 场（调试用）
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent as A  # noqa: E402

OUT_DIR = ROOT / "runs-m6-text"
CONFIG = A.load_config(ROOT / "config.json")

# 10 场里必须同一份简历，否则输入 token 和问题都会变（方案第二节第 8 条）
RESUME = """曾依依　AI 产品经理（实习）
本科四年级，2027 年 6 月毕业。求职方向：AI 产品经理 / 产品实习生。

项目一　竞品体检 Agent（2026.10–2026.11）
独立完成的一条 LLM 竞品横评流水线：六步流程 + 两道人工卡点。
一句话需求进，出评测方案、评测集、逐条打分和归因报告。
最近一次完整运行：12 条用例 × 3 个被测对象 = 36 条评分记录，
每条按正确性、合规性、简洁性各 0–2 分打分，逐格可复算。
三个对象均分分别是 4.00 / 4.75 / 4.08（满分 72，按 6 分制算）。
过程中因为判据写错作废过两轮数据，后来把判据收成七条硬约束。

项目二　面试陪练 Agent（2026.10–2026.11）
语音面试陪练工具，桌面 Web，本地运行。10 分钟打一场 8 个话题的语音面试。
判断逻辑按「实字数 / 有没有数字和结果 / 有没有放弃词」三档分流，
分成追问、澄清、换题、收尾四种动作，每种都有次数上限防止卡死。
前端用浏览器语音识别和播报，支持打断恢复。

项目三　海报提案 Agent
输入产品卖点，输出三套海报提案和对应的文案。

技能：Python 标准库、Vue、Unity C#、Figma。
"""

# 四种候选人。和方案第三节的场次分配一一对应。
PERSONA = {
    "认真": {
        "prompt": (
            "你在面试 AI 产品经理实习岗。回答要具体：说清楚做了什么、怎么做的、"
            "结果是多少。**每段回答必须包含至少一个具体数字**（百分比、人数、天数、"
            "条数、分数都可以），并且说清结果。60-120 字，口语化，像真人说话，不要分点。"
        ),
        "max_tokens": 180,
    },
    "含糊": {
        "prompt": (
            "你在面试，但答得很含糊。每次只答一句话，10-18 字，"
            "不要给任何数字，不要给结果，说完就停。不要反问。"
        ),
        "max_tokens": 60,
    },
    "跑题": None,   # 固定短句，见 SKIP_LINES
    "放弃": None,
}

# 跑题型：答非所问 / 反问。长度控制在 2–10 字，能同时压到 CLARIFY 和 PROBE 两支。
OFF_TRACK = ["这个您觉得呢？", "啊？", "您先说说是什么意思吧", "嗯……我先想想", "能再说一遍吗"]
# 放弃型：都带放弃词，且整句很短，该走 SWITCH 而不是 PROBE。
GIVE_UP = ["不知道。", "这个我没做过。", "没接触过，不太清楚。", "这块我没做过。"]

# 场次分配：6 场认真、2 场含糊、1 场跑题、1 场放弃
PLAN = ["认真"] * 6 + ["含糊"] * 2 + ["跑题", "放弃"]


def ask_candidate(llm: A.LLMClient, persona: str, question: str, i: int) -> str:
    """让「候选人」这一侧说话。跑题/放弃用固定短句，其余调模型。"""
    if persona == "跑题":
        return OFF_TRACK[i % len(OFF_TRACK)]
    if persona == "放弃":
        return GIVE_UP[i % len(GIVE_UP)]

    spec = PERSONA[persona]
    msgs = [
        {"role": "system", "content": spec["prompt"]},
        {"role": "user", "content": (
            f"面试官问：{question or '（面试刚开始，打了个招呼）'}\n\n"
            "只输出你的回答本身，不要加引号、不要加「我：」这类前缀。"
        )},
    ]
    out = []
    for ev in llm.stream(msgs, temperature=0.95, max_tokens=spec["max_tokens"]):
        if ev["type"] == "token":
            out.append(ev["text"])
    text = "".join(out).strip().strip("「」\"'")

    # 后处理：把回答掰到目标分支上，否则测不到想测的东西
    if persona == "含糊":
        text = A.normalize(text)[:16] or "还行吧"
        if len(text) < 5:                     # 太短会掉进 CLARIFY 分支，不是要测的
            text = "就正常做了一些需求"
    elif persona == "认真":
        norm = A.normalize(text)
        if not A.has_detail(norm) or len(norm) < 22:
            # 没数字/结果就去掉会触发追问的句子，补一句带数的
            text = (text + " 具体数据是转化率从 12% 提到 19%，大概做了三周。").strip()
    return text


def run_one(idx: int, persona: str, verbose: bool = True) -> dict:
    sid = f"m6p{idx:02d}"
    ag = A.InterviewAgent(
        CONFIG, sid, "AI 产品经理（实习）",
        max_questions=8, level="normal", form="formal",
        resume_text=RESUME, resume_name="resume.txt",
    )
    cand = A.LLMClient(CONFIG)

    buf: list[str] = []
    errors: list[str] = []
    gens: list[int] = []          # 每次生成的耗时（_generate 里发的 latency 事件）
    ttft: list[int] = []          # 真正的首 token 延迟：从调用到收到第一个 tok
    first = {"t": None}
    t0 = time.time()

    def emit(ev: dict):
        t = ev.get("t")
        if t == "tok":
            if first["t"] is None:
                first["t"] = time.time()
            buf.append(ev.get("d", ""))
        elif t == "latency":
            ms = ev.get("gen_ms")
            if isinstance(ms, int) and ms > 0:
                gens.append(ms)
        elif t == "err":
            errors.append(f"{ev.get('kind')}: {str(ev.get('msg', ''))[:70]}")

    def call(fn, *a):
        """包一层：记录首 token 时间。澄清是模板回复（0 token），不计入。"""
        first["t"] = None
        t_start = time.time()
        fn(*a)
        users = [t for t in ag.turns if t["role"] == "user"]
        templ = bool(users and users[-1].get("action_label") == "澄清")
        if first["t"] and not templ:
            ttft.append(int((first["t"] - t_start) * 1000))

    if verbose:
        print(f"  [{idx:02d}] {persona}型　开始")

    call(ag.begin, emit)
    rounds = 0
    while not ag.finished and rounds < 45:
        q = "".join(buf).strip()
        buf.clear()
        ans = ask_candidate(cand, persona, q, rounds)
        call(ag.handle_user, ans, False, emit)
        rounds += 1
        if verbose:
            acts = [t for t in ag.turns if t["role"] == "user"]
            last = acts[-1] if acts else {}
            print(f"       Q{ag.question_index} → {last.get('action_label', '?'):<4} "
                  f"({last.get('reason', '')[:26]})")

    wall = time.time() - t0
    report = ag.report()
    folder = ag.save(OUT_DIR, report)

    users = [t for t in ag.turns if t["role"] == "user"]
    actions = {}
    for t in users:
        actions[t["action_label"]] = actions.get(t["action_label"], 0) + 1

    m = report.get("metrics", {})
    lat = m.get("latency", {})

    def pctl(vals: list[int], p: float):
        if not vals:
            return None
        s = sorted(vals)
        k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
        return s[k]

    # 首 token 用自己掐的表；生成总耗时单独留一份做参考
    p50 = pctl(ttft, 50)
    p95 = pctl(ttft, 95)

    row = {
        "idx": idx,
        "persona": persona,
        "session": ag.session_id,
        "folder": str(folder.relative_to(ROOT)),
        "finished": ag.finished,
        "questions": ag.question_index,
        "reached_8": ag.question_index >= 8 and ag.finished,
        "rounds": len(users),
        "actions": m.get("actions", actions),
        "cost": round(m.get("cost_cny") or ag.cost(), 4),
        "wall_s": round(wall, 1),
        "first_ms_p50": p50,
        "first_ms_p95": p95,
        "ttft_samples": len(ttft),
        "ttft_all": ttft,
        "gen_ms_p50": pctl(gens, 50),
        "errors": errors,
    }
    if verbose:
        flag = "走满 8" if row["reached_8"] else f"卡在 {row['questions']}"
        print(f"       → {flag}　轮次 {row['rounds']}　¥{row['cost']}　{row['wall_s']}s")
        if errors:
            print(f"       ! {len(errors)} 个错误：{errors[0][:70]}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, default=0, help="只跑第 N 场")
    ap.add_argument("--from", dest="frm", type=int, default=0, help="从第 N 场跑到底")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = [(i + 1, p) for i, p in enumerate(PLAN)]
    if args.only:
        targets = [t for t in targets if t[0] == args.only]
    elif args.frm:
        targets = [t for t in targets if t[0] >= args.frm]

    print(f"\n  M6 文本链路预跑　{len(targets)} 场　输出 → runs-m6-text/\n")
    rows = []
    for idx, persona in targets:
        try:
            rows.append(run_one(idx, persona, verbose=not args.quiet))
        except Exception as e:
            print(f"  [{idx:02d}] 崩了：{type(e).__name__} {str(e)[:120]}")
            rows.append({"idx": idx, "persona": persona, "crashed": str(e)[:160]})

    # 和已经跑过的场次合并（重跑同一场会覆盖旧记录）
    store = OUT_DIR / "prerun-summary.json"
    old = []
    if store.exists():
        try:
            old = json.loads(store.read_text(encoding="utf-8")).get("rows", [])
        except Exception:
            old = []
    fresh = {r["idx"]: r for r in rows}
    merged = [fresh.get(r["idx"], r) for r in old] if old else []
    seen = {r["idx"] for r in merged}
    merged += [r for r in rows if r["idx"] not in seen]
    merged.sort(key=lambda r: r["idx"])
    rows = merged

    ok = [r for r in rows if r.get("reached_8")]
    costs = [r["cost"] for r in rows if r.get("cost")]
    errs = sum(len(r.get("errors") or []) for r in rows)
    # 全局首 token：把所有场的原始值并起来算，不是"各场 P50 的平均"
    all_ttft = sorted(v for r in rows for v in (r.get("ttft_all") or []))
    all_gen = [r["gen_ms_p50"] for r in rows if r.get("gen_ms_p50")]

    def gp(p: float):
        if not all_ttft:
            return None
        k = max(0, min(len(all_ttft) - 1, int(round(p / 100 * (len(all_ttft) - 1)))))
        return all_ttft[k]

    print("\n  " + "-" * 62)
    print(f"  已跑场次　　　{len(rows)} / 10")
    print(f"  走满 8 话题　　{len(ok)} / {len(rows)}")
    if costs:
        print(f"  单场成本　　　均 ¥{statistics.mean(costs):.4f}　最高 ¥{max(costs):.4f}"
              f"　合计 ¥{sum(costs):.4f}")
    if all_ttft:
        print(f"  首 token　　　P50 {gp(50)} ms　P95 {gp(95)} ms"
              f"　（{len(all_ttft)} 个样本，不含语音层）")
    if all_gen:
        print(f"  整句生成　　　P50 {int(statistics.mean(all_gen))} ms（含全部 token）")
    print(f"  接口错误　　　{errs} 次")
    print("  " + "-" * 62 + "\n")

    (OUT_DIR / "prerun-summary.json").write_text(
        json.dumps({"rows": rows, "generated": time.strftime("%Y-%m-%d %H:%M")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
