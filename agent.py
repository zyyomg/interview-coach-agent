# -*- coding: utf-8 -*-
"""agent.py —— 对话引擎：状态机 · 动作判定 · 流式生成 · 指标与成本

核心设计（策划书 4.2）：**动作由代码判，话由模型说。**
- 判定不调模型：0 延迟、0 token、规则可解释、参数可调
- 生成只负责"把这一句说得像面试官"

外部只需要三样东西：agent.begin() / agent.handle_user() / agent.report()
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"

# ---------------------------------------------------------------- 动作

CLARIFY = "clarify"
PROBE = "probe"
SWITCH = "switch"
ASK = "ask"
CLOSE = "close"
NUDGE = "nudge"      # 候选人不说话，面试官主动催一句

LABEL = {CLARIFY: "澄清", PROBE: "追问", SWITCH: "换题", ASK: "提问", CLOSE: "收尾", NUDGE: "催场"}

GIVEUP_WORDS = (
    "不知道", "没做过", "跳过", "不会", "不清楚", "忘了",
    "没接触", "不太了解", "没经验", "没想过", "说不清", "过吧",
)

_NUM = re.compile(r"\d|[二三四五六七八九十百千万亿两]+(个|人|天|周|月|年|小时|分钟|次|倍|版|条|份|块|万|千)")
_RESULT = re.compile(r"提升|降低|提高|下降|增长|减少|从.{1,10}到|导致|结果|所以|因此|相比|转化|留存|成本|耗时|延迟|涨|跌")
_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:…\-—~`\"'“”‘’（）()【】\[\]《》<>]+")

# 「一个项目」「一下」这类是虚指，先把它们去掉再找数字，否则"我负责了一个项目"会被判成有细节
_VAGUE_QTY = ("一个", "一下", "一点", "一些", "一样", "一直", "一起", "一次", "一方面")

# 澄清话术：代码模板，**不调模型**（策划书 5.3 的关键点）
CLARIFY_LINES = (
    "我这边没听清，你再说一遍？",
    "声音有点断，刚才那句能再说一次吗？",
    "没接到，麻烦重新说一下？",
)

# 出异常时代模型说的一句。按原因分开，是因为「Key 失效」和「网络抖一下」
# 对用户是两件事：前者要他停下去改配置，后者他继续说就好。
FALLBACK_LINES = {
    "auth": "我这边接口没通，先停一下。",
    "balance": "我这边接口欠费了，先停一下。",
    "model": "我这边模型配错了，先停一下。",
    "rate": "我这边太急了，稍等再问。",
    "timeout": "我这边卡了一下，你继续说。",
    "network": "我这边断了一下，你继续说。",
    "unknown": "我这边卡了一下，你继续说。",
}


def normalize(text: str) -> str:
    """去掉标点与空白，只留内容——判定按"实字数"算，不按字符数。"""
    return _PUNCT.sub("", text or "")


def has_detail(norm: str, strict: bool = False) -> bool:
    """有没有"具体内容"。

    strict=False（简单/标准档）：有数字**或**结果词就算。
    strict=True（困难档）：数字**和**结果词都要有——只报一个数不算，
    要说清楚"从多少到多少、影响了什么"，否则追问。
    """
    for w in _VAGUE_QTY:
        norm = norm.replace(w, "")
    num = bool(_NUM.search(norm))
    res = bool(_RESULT.search(norm))
    return (num and res) if strict else (num or res)


def level_cfg(cfg: dict, level: str) -> dict:
    """取当前难度的参数。配置里没有就退回标准档。"""
    levels = cfg.get("levels") or {}
    return levels.get(level) or levels.get("normal") or {}


def decide(text: str, asr_empty: bool, probes: int, clarifys: int, cfg: dict,
           switches: int = 0, level: str = "normal") -> dict:
    """纯函数：给定输入与当前题状态，返回该做什么 + 为什么。

    顺序即优先级，和策划书 4.2 的判定树一一对应。
    每个动作都有上限——没有上限的判定会把候选人卡死在同一题上。

    难度只影响三个数（短句阈值、细节判定严不严）和之后生成时的提示词，
    **不改变判定顺序**。
    """
    turn_cfg = cfg.get("turn", {})
    lv = level_cfg(cfg, level)
    empty_chars = int(turn_cfg.get("empty_chars", 4))
    short_chars = int(lv.get("short_chars", turn_cfg.get("short_chars", 22)))
    strict = bool(lv.get("strict_detail", False))
    max_probe = int(cfg.get("max_probe_per_question", 2))
    max_clar = int(cfg.get("max_clarify_per_question", 2))
    max_switch = int(cfg.get("max_switch_per_question", 2))
    norm = normalize(text)
    n = len(norm)

    if asr_empty or n == 0:
        return {"action": CLARIFY, "reason": "识别为空（0 字）", "chars": n}

    # 放弃词优先于"太短"：「不知道」只有 3 个字，但语义明确，该换题不是该反问。
    # 但它必须**当真是这句话的意思**：用户说 200 多个字、中间带一句「不知道从何下手」，
    # 被当成放弃就直接换题了（实测踩到过）。所以下面两个条件满足其一才算：
    #   · 放弃词占了全文 1/4 以上；
    #   · 整句很短（≤ giveup_chars）又没有数字/结果——典型就是"这块我没接触过"。
    hit = next((w for w in GIVEUP_WORDS if w in norm), None)
    if hit:
        dominant = len(normalize(hit)) * 4 >= n
        short_giveup = n <= int(turn_cfg.get("giveup_chars", 24)) and not has_detail(norm)
        if dominant or short_giveup:
            if switches >= max_switch:
                return {"action": ASK, "reason": f"本题已换题 {max_switch} 次，记为未答，进下一题", "chars": n}
            return {"action": SWITCH, "reason": f"含放弃词「{hit}」，且整句就是在说不会", "chars": n}
        # 只是顺嘴提到 → 按正常内容判（继续往下走长度 / 细节那几档）

    if n < empty_chars:
        if clarifys >= max_clar:
            return {"action": PROBE, "reason": "澄清已到上限，改为追问推进", "chars": n}
        return {"action": CLARIFY, "reason": f"实字数 {n} < {empty_chars}，当没说清处理", "chars": n}

    if probes >= max_probe:
        return {"action": ASK, "reason": f"本题追问已到上限 {max_probe} 次", "chars": n}

    if n < short_chars:
        return {"action": PROBE, "reason": f"偏短（{n} 字 < {short_chars}）", "chars": n}

    if not has_detail(norm, strict=strict):
        why = "数字和结果都要有" if strict else "没有数字也没有结果"
        return {"action": PROBE, "reason": f"{why}（{n} 字），属「有内容没细节」", "chars": n}

    return {"action": ASK, "reason": "够具体，进入下一题", "chars": n}


# ---------------------------------------------------------------- 计数与成本

def est_tokens(text: str) -> int:
    """无 usage 时的兜底估算：中文约 1 token / 1.5 字，英文约 4 字符 / token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fa5]", text))
    other = max(0, len(text) - cjk)
    return max(1, int(round(cjk / 1.5 + other / 4)))


def is_peak(ts: float | None = None) -> bool:
    """现在是不是高峰时段。

    DeepSeek 的高峰是北京时间工作日 9:00-12:00、14:00-18:00，其余（含午休、
    晚上、周末）半价。节假日按工作日算——这里没有中国的节假日日历，
    所以节假日那几天会**略微高估**成本，宁可多报不少报。
    """
    t = time.localtime(ts if ts is not None else time.time())
    if t.tm_wday >= 5:                     # 5=周六 6=周日
        return False
    return 9 <= t.tm_hour < 12 or 14 <= t.tm_hour < 18


def cost_cny(usage: dict, price: dict, peak: bool | None = None) -> float:
    """按命中/未命中分别计价（策划书 7.1）。单位：元。

    peak=None 时按当前时间判断；空闲时段乘 `offpeak_factor`（默认 0.5）。
    """
    if peak is None:
        peak = is_peak()
    factor = 1.0 if peak else float(price.get("offpeak_factor", 0.5))
    return factor * (
        usage.get("hit", 0) * float(price.get("input_hit", 0.04))
        + usage.get("miss", 0) * float(price.get("input_miss", 2.0))
        + usage.get("out", 0) * float(price.get("output", 8.0))
    ) / 1_000_000


def pct(values: list, p: float):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    vals = sorted(vals)
    k = int(round((len(vals) - 1) * p))
    return vals[max(0, min(len(vals) - 1, k))]


def classify_error(msg: str) -> dict:
    """把接口报错归成几类，每类配一句"照着做就行"的提示。

    真实模式下的失败原因，代码能认几类就认几类。之前所有异常都只说
    「我这边卡了一下」——Key 失效和麦克风坏了长得一模一样，排查只能靠猜。

    先扒 "HTTP 4xx" 的状态码再判断，不能直接在整个串里搜 "401"：
    DeepSeek 的报错里带 request_id（uuid），里有几个数字，会误判成别的类型。
    """
    s = str(msg or "")
    low = s.lower()
    m = re.search(r"HTTP\s+(\d{3})", s)
    code = int(m.group(1)) if m else 0

    if code == 401 or "invalid_api_key" in low or "unauthorized" in low or "authentication" in low:
        return {"kind": "auth", "hint": "Key 无效或已过期：检查 .env 里的 DEEPSEEK_API_KEY，改完要重启服务"}
    if code == 402 or "insufficient" in low or "balance" in low:
        return {"kind": "balance", "hint": "账户余额不足：去 DeepSeek 控制台充值后重试"}
    if code == 404 or "model_not_found" in low or "does not exist" in low:
        return {"kind": "model", "hint": "模型名不存在：把 config.json 的 model 改成 deepseek-chat"}
    if code == 429 or "rate limit" in low:
        return {"kind": "rate", "hint": "被限流了：等 10 秒再答一轮，别连续猛说"}
    if "timeout" in low or "timed out" in low:
        return {"kind": "timeout", "hint": "请求超时：到 api.deepseek.com 的链路不通，试试关掉代理"}
    if "getaddrinfo" in low or "name or service" in low or "connection" in low or "urlopen" in low:
        return {"kind": "network", "hint": "连不上 api.deepseek.com：检查网络或代理设置"}
    return {"kind": "unknown", "hint": "看服务窗口里的原始报错；也可以跑 python tools.py check 复现"}


# ---------------------------------------------------------------- 分句

class SentenceSplitter:
    """流式分句：凑满一句就先播，不等全文。

    为什么在逗号处也切：首音时间 = 首句生成时间 + TTS 启动时间。
    等到句号才播，首音会晚 1–2 秒（策划书 5.2 的取舍）。
    """

    END = "。！？!?…；;"
    SOFT = "，,、"

    def __init__(self, max_len: int = 60, soft_len: int = 22):
        self.buf = ""
        self.max_len = max_len
        self.soft_len = soft_len

    def feed(self, chunk: str) -> list:
        self.buf += chunk or ""
        out = []
        while True:
            cut = self._cut()
            if cut is None:
                break
            piece = self.buf[:cut].strip()
            self.buf = self.buf[cut:]
            if piece:
                out.append(piece)
        return out

    def _cut(self):
        for i, ch in enumerate(self.buf):
            if ch in self.END:
                return i + 1
            if ch in self.SOFT and i + 1 >= self.soft_len:
                return i + 1
            if ch == "\n":
                return i + 1
            if i + 1 >= self.max_len:
                return i + 1
        return None

    def flush(self) -> str:
        rest = self.buf.strip()
        self.buf = ""
        return rest


# ---------------------------------------------------------------- 模型

MOCK_BANK = {
    "ai_pm": [
        "先做个自我介绍，重点讲一个你做过的最有代表性的项目。",
        "你刚才说的这个产品，要解决的核心问题是什么？你怎么判断它值得做？",
        "这个需求是从哪来的？你当时怎么决定先做它？",
        "上线之后，你用什么指标判断它成了？数字是多少？",
        "如果模型效果不稳定，用户反馈说答得不对，你会怎么处理？",
        "你怎么评估一次 Agent 对话的质量？除了满意度还看什么？",
        "如果成本要压一半，你会先动哪里？",
        "你和研发意见不一致的时候，一般怎么推动？",
        "这个项目里有没有你后来觉得做错了的决定？",
        "最后，你有什么想问我的？",
    ],
    "pm": [
        "先简单介绍一下你自己，讲一个你主导过的项目。",
        "这个项目的目标是什么？你怎么衡量它做成了？",
        "需求是从哪来的？优先级你是怎么排的？",
        "上线后数据怎么样？和你预期差在哪？",
        "如果资源只够做一半，你会砍掉哪部分？",
        "你和设计、研发有过分歧吗？最后怎么定的？",
        "说一个你做失败的功能，复盘结论是什么？",
        "最后，你有什么想问我的？",
    ],
    "general": [
        "先自我介绍一下，重点讲一段你最有成就感的经历。",
        "这件事里你具体负责哪一部分？",
        "过程中遇到最大的阻力是什么？你怎么解决的？",
        "结果怎么样？有可以量化的部分吗？",
        "如果重来一次，你会改哪里？",
        "你怎么安排优先级？举个例子。",
        "最后，你有什么想问我的？",
    ],
}

MOCK_PROBES = (
    "这块具体是怎么做的？你当时负责哪一部分？",
    "有数据吗？做完之后指标变化了多少？",
    "为什么选这个方案？有没有考虑过别的做法？",
    "你说的这个流程，落到具体动作上是什么？",
)

# 演示模式专用的"回声版"追问：把用户刚说的话复述一遍再问。
# 不接模型时也能让人看出"它听见了我说什么"，否则整场像在读台本。
MOCK_PROBES_ECHO = (
    "你说「{kw}」，这块具体是怎么做的？你当时负责哪一部分？",
    "「{kw}」这个事有数据吗？做完之后指标变化了多少？",
    "「{kw}」，当时为什么这么定？有没有考虑过别的做法？",
    "「{kw}」落到具体动作上，你每天做的是什么？",
)

MOCK_SWITCH = (
    "没关系，这块先跳过。那我们聊点别的，你最近在做的项目里，哪个是你从头跟到尾的？",
    "没做过也正常。换个方向，说一个你自己动手做过的小需求就行，不用多大。",
    "行，那这个不聊了。你平时负责的那块，最近一次改动是什么？",
    "没关系。那说说你手上正在做的事，最近一周你具体在忙什么？",
)

MOCK_CLOSE = "今天先到这儿，我整理一下。"

# 演示模式没有模型，读不懂简历，但这句至少不会穿帮：它只说“从简历里挑”，不编具体项目
MOCK_OPEN_RESUME = "简历我看过了，先挑一个你最想聊的项目，讲讲你具体负责哪块。"

_FILLER = re.compile(
    r"^(我觉得|我认为|我感觉|我负责|我做了|我做过|我们|然后|后来|当时|那个|就是说|其实|反正|大概|我)+"
)
_CLAUSE = re.compile(r"[，。！？；：、,\n.?!;:]+")
_KEEP = re.compile(r"[^\s，。！？；：、,.!?;:…\-—~`\"'“”‘’（）()【】\[\]《》<>]+")


_NOISE = re.compile(r"^[嗯呃啊哦噢诶欸唉哈呀吧嘛呢么，。、\s]+$")


def _snip(text: str, n: int = 12) -> str:
    """从用户回答里抠一小段能复述的原文，用来做「回声式追问」。

    取第一个有实质内容的分句——语音识别出来的句子常以"嗯""呃"开头，
    直接取 parts[0] 会拿到一个语气词，复述出来就是「你说『嗯』」，比不复述更糟。
    """
    t = (text or "").strip()
    parts = [p.strip() for p in _CLAUSE.split(t) if p.strip()]
    picked = ""
    for p in parts:
        if len(p) >= 4 and not _NOISE.match(p):
            picked = p
            break
    if picked:
        t = picked
    elif parts:
        t = parts[0]
    t = _FILLER.sub("", t)
    t = re.sub(r"^[了的]+", "", t)
    # 只在"我是来自…"这种主语被摘掉后残留的"是"上动手，
    # 否则「是否该主动说话」会被剪成「否该主动说话」
    t = re.sub(r"^是(?=来自|在|做|负责|通过|因为|从|用)", "", t)
    t = "".join(_KEEP.findall(t))
    return t[:n].strip()


class LLMClient:
    """模型客户端。没 Key 自动进演示模式（本地脚本扮演面试官），保证项目能立刻跑起来。"""

    def __init__(self, cfg: dict, force_mock: bool = False):
        self.cfg = cfg
        self.model = cfg.get("model", "deepseek-flash")
        self.base = str(cfg.get("api_base", "https://api.deepseek.com")).rstrip("/")
        self.key = (os.environ.get("DEEPSEEK_API_KEY") or cfg.get("api_key") or "").strip()
        self.mock = bool(force_mock or not self.key)
        self.last_error = ""
        self._rng = random.Random(20261031)
        self._bank_pos = {}
        self._rot = 0              # 演示模式话术轮换计数，避免同一句反复出现

    @property
    def mode(self) -> str:
        return "mock" if self.mock else "live"

    # -- 对外唯一入口 ------------------------------------------------
    def stream(self, messages: list, temperature: float = 0.8, max_tokens: int = 240,
               purpose: str = "turn"):
        if self.mock:
            yield from self._mock_stream(messages, max_tokens, purpose)
        else:
            yield from self._live_stream(messages, temperature, max_tokens, purpose)

    # -- 真实模型 ----------------------------------------------------
    def _live_stream(self, messages, temperature, max_tokens, purpose="turn"):
        """调真实的流式接口。

        **思考模式必须显式关掉（对话这一路）**：DeepSeek 默认 thinking=enabled、effort=high，
        模型会先把 token 花在思维链（`reasoning_content`）上，`content` 迟迟为空。
        对语音面试来说这是致命的——首音要等它"想完"才出，一句话能等好几秒；
        而且思维链按输出 token 计费。复盘那一路可以开，靠 `config.json` 的 thinking 区分。
        """
        url = f"{self.base}/chat/completions"
        think = (self.cfg.get("thinking") or {}).get(purpose, "disabled")
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
            "thinking": {"type": think},
        }
        # 思考模式下官方明确说 temperature 不生效，就不发了；非思考模式照旧
        if think != "enabled":
            body["temperature"] = temperature
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.key}",
            "Accept": "text/event-stream",
        }
        req = urlrequest.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
        try:
            resp = urlrequest.urlopen(req, timeout=60)
        except HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            # 兼容性退让：少数端点（或旧版接口）不认这些可选字段，去掉重试一次
            drop = next((k for k in ("stream_options", "thinking") if k in detail), None)
            if drop is None:
                raise RuntimeError(f"HTTP {e.code}: {detail}") from None
            body.pop(drop, None)
            req = urlrequest.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
            resp = urlrequest.urlopen(req, timeout=60)

        usage = {}
        text_len = 0
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        with resp:
            while True:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "ignore").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        text_len += len(piece)
                        yield {"type": "token", "text": piece}

        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        out_tokens = int(usage.get("completion_tokens") or 0)
        estimated = False
        if not prompt_tokens:
            miss = est_tokens("x" * prompt_chars)
            estimated = True
        else:
            if not hit and not miss:
                miss = prompt_tokens
        if not out_tokens:
            out_tokens = max(1, int(text_len / 1.5))
            estimated = True
        yield {"type": "usage", "hit": hit, "miss": miss, "out": out_tokens, "estimated": estimated}

    # -- 演示模式 ----------------------------------------------------
    def _mock_stream(self, messages, max_tokens, purpose):
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        directive = ""
        for m in messages:
            if m.get("role") == "system" and m["content"].startswith("[系统]"):
                directive = m["content"]
        user_msgs = [m["content"] for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1] if user_msgs else ""

        role_key = "general"
        for k, name in (self.cfg.get("roles") or {}).items():
            if name and name in system:
                role_key = k
                break

        # 话题号要从本轮的指令里取（"进入第 N 个话题"），
        # 不能从角色提示里找——那样永远解析不到，题目会一直停在第 1 个。
        m = re.search(r"第\s*(\d+)\s*个话题", directive)
        qidx = int(m.group(1)) if m else 1

        kw = _snip(last_user)
        if len(kw) < 3 or any(w in kw for w in GIVEUP_WORDS):
            kw = ""              # 太短或本身就是"没做过"，别复述了
        # "收尾"要放在最前面判：收尾的指令里常带"不要追问"这类话，
        # 先匹配"追问"就会把收尾演成一次追问（演示模式实测踩到过）
        # 简历模式只能从系统提示词判断：_mock_stream 挂在 LLMClient 上，
        # 拿不到 InterviewAgent 的 resume 字段。
        has_resume = "用简历的规则" in system
        if "收尾" in directive:
            line = MOCK_CLOSE
        # 简历开场要排在「追问」前面：开场指令里有“最值得追问的项目”，
        # 先匹配追问就会把开场演成一次追问（实测踩到：开场问了句“有数据吗”）
        elif has_resume and "第 1 个话题" in directive:
            line = MOCK_OPEN_RESUME
        elif "追问" in directive:
            if kw:
                line = MOCK_PROBES_ECHO[(qidx + len(user_msgs)) % len(MOCK_PROBES_ECHO)].format(kw=kw)
            else:
                line = MOCK_PROBES[(qidx + len(user_msgs)) % len(MOCK_PROBES)]
        elif "更基础" in directive or "更容易" in directive:
            # 换题一定伴随"没做过/不知道"，复述原话只会重复那句放弃，所以这里只换方向
            line = MOCK_SWITCH[self._rot % len(MOCK_SWITCH)]
            self._rot += 1
        else:
            bank = MOCK_BANK.get(role_key, MOCK_BANK["general"])
            line = bank[(qidx - 1) % len(bank)]

        for i in range(0, len(line), 3):
            time.sleep(0.012)
            yield {"type": "token", "text": line[i:i + 3]}
        yield {
            "type": "usage",
            "hit": 0,
            "miss": est_tokens("".join(m.get("content", "") for m in messages)),
            "out": est_tokens(line),
            "estimated": True,
            "mock": True,
        }


# ---------------------------------------------------------------- 会话


class InterviewAgent:
    """一场对练 = 一个 agent 实例。所有状态在内存里，结束落盘。"""

    def __init__(self, cfg: dict, session_id: str, role_key: str = "ai_pm",
                 max_questions: int | None = None, llm: LLMClient | None = None,
                 level: str = "normal", resume_text: str = "", resume_name: str = ""):
        self.cfg = cfg
        self.session_id = session_id
        self.role_key = role_key if role_key in (cfg.get("roles") or {}) else "general"
        self.role_name = cfg["roles"][self.role_key]
        self.max_questions = int(max_questions or cfg.get("max_questions", 8))
        self.llm = llm or LLMClient(cfg)
        self.level_key = level if level in (cfg.get("levels") or {}) else "normal"
        self.level = level_cfg(cfg, self.level_key)

        # 简历模式：只把前 N 字喂给模型，避免长简历吃掉对话预算。
        # 原文不落盘（session.json 只记文件名和字数）——简历是个人信息，
        # 落盘的东西有可能被截图、被同步、被上传。
        self.resume = (resume_text or "").strip()
        self.resume_name = (resume_name or "").strip()
        self.resume_limit = int((cfg.get("resume") or {}).get("max_chars", 1800))
        self.resume_used = self.resume[: self.resume_limit]

        self.history = []            # 给模型的消息（assistant / user）
        self.turns = []              # 每轮判定记录
        self.interrupts = []         # 打断记录
        self.pending_interrupt = None
        self.timings = {}            # turn_id -> 客户端三段耗时

        self.question_index = 0
        self.probes = 0
        self.clarifys = 0
        self.switches = 0
        self.easier = 0
        self.silences = 0            # 连续几次没开口，用户一说话就清零
        self.turn_seq = 0
        self.finished = False
        self.started_at = time.time()
        self.ended_at = None

        self.usage = {"hit": 0, "miss": 0, "out": 0, "mock": False, "estimated": False}
        self._report_cache = None

    # -- 提示词 ------------------------------------------------------
    def system_prompt(self) -> str:
        depth = self.level.get("ask_note", "")
        resume_block = ""
        if self.resume_used:
            resume_block = (
                "\n候选人的简历（原文节选，只给你出题和追问用，不要照读）：\n"
                "<<<\n"
                f"{self.resume_used}\n"
                ">>>\n"
                "用简历的规则：\n"
                "8. 第 1 个话题必须从简历里挑一个最值得追问的项目或经历，不要问泛泛的自我介绍。\n"
                "9. 追问可以引用简历里的项目名、指标、时间，逼他把简历上的说法讲具体。\n"
                "10. 简历没写的不要编；写了但含糊的地方，优先追问。\n"
                "11. 不评价简历写得好不好，也不逐条念简历。\n"
            )
        return (
            f"你是一场「{self.role_name}」岗位的模拟面试官，正在和候选人做语音面试。\n"
            f"本次共 {self.max_questions} 个话题，现在是第 {max(1, self.question_index)} 个。\n"
            + (f"本场难度：{depth}\n" if depth else "")
            + resume_block
            + "说话规则：\n"
            "1. 每次只说一句话：一个提问，或一句追问。不寒暄、不点评、不一次问两件事。\n"
            "2. 口语、短句，20–45 字。不用列表、不用 markdown、不用括号补充。\n"
            "3. 追问要紧扣候选人上一句里的具体词，问「怎么做的」「数据是多少」「为什么这么做」。\n"
            "4. 不复述候选人的话，不说「很好的回答」。\n"
            "5. 你是面试官不是助手：不解释题目、不给答案、不主动示好。\n"
            "6. 收到 [系统] 指令时严格执行，但输出仍然只有一句口语。\n"
            "7. 只输出这一句话本身，不要加「面试官：」之类的前缀。"
        )

    def _messages(self, directive: str | None = None) -> list:
        limit = int(self.cfg.get("turn", {}).get("history_limit", 30))
        msgs = [{"role": "system", "content": self.system_prompt()}]
        msgs.extend(self.history[-limit:])
        if self.pending_interrupt:
            msgs.append({"role": "system", "content": self.pending_interrupt})
            self.pending_interrupt = None
        if directive:
            msgs.append({"role": "system", "content": f"[系统] {directive}"})
        return msgs

    # -- 生成 --------------------------------------------------------
    def _generate(self, emit, directive: str, max_tokens: int = 200) -> str:
        splitter = SentenceSplitter(
            max_len=int(self.cfg.get("turn", {}).get("max_sentence_chars", 60)),
            soft_len=int(self.cfg.get("turn", {}).get("soft_sentence_chars", 22)),
        )
        chunks = []
        t0 = time.time()
        usage = None
        try:
            for ev in self.llm.stream(self._messages(directive), max_tokens=max_tokens):
                if ev["type"] == "token":
                    chunks.append(ev["text"])
                    emit({"t": "tok", "d": ev["text"]})
                    for sentence in splitter.feed(ev["text"]):
                        emit({"t": "sent", "d": sentence})
                elif ev["type"] == "usage":
                    usage = ev
        except Exception as e:                                   # 兜底：会话不中断
            self.llm.last_error = str(e)
            info = classify_error(str(e))
            emit({"t": "err", "msg": str(e)[:200], "kind": info["kind"], "hint": info["hint"]})
            fallback = FALLBACK_LINES.get(info["kind"], FALLBACK_LINES["unknown"])
            # 气泡是靠 tok 拼出来的，只发 sent 就只剩一个空框（和 CLARIFY 那个 bug 同一类）。
            # 已经收到半截字也要补上，否则字幕和实际播出来的话对不上。
            emit({"t": "tok", "d": fallback})
            emit({"t": "sent", "d": fallback})
            return fallback

        tail = splitter.flush()
        if tail:
            emit({"t": "sent", "d": tail})

        text = "".join(chunks).strip()
        if not text:                                             # 空回复也兜底
            text = FALLBACK_LINES["unknown"]
            emit({"t": "tok", "d": text})                        # 同理：没有 tok 就只有声音没有字幕
            emit({"t": "sent", "d": text})

        if usage:
            self.usage["hit"] += int(usage.get("hit", 0))
            self.usage["miss"] += int(usage.get("miss", 0))
            self.usage["out"] += int(usage.get("out", 0))
            self.usage["mock"] = self.usage["mock"] or bool(usage.get("mock"))
            self.usage["estimated"] = self.usage["estimated"] or bool(usage.get("estimated"))
            emit({
                "t": "usage",
                "in": self.usage["miss"] + self.usage["hit"],
                "out": self.usage["out"],
                "cost": round(self.cost(), 4),
                "mock": self.usage["mock"],
            })

        emit({"t": "latency", "gen_ms": int((time.time() - t0) * 1000)})
        return text

    # -- 开场 --------------------------------------------------------
    def begin(self, emit):
        self.turn_seq += 1
        self.question_index = 1
        turn_id = self.turn_seq
        emit({
            "t": "meta", "turn": turn_id, "action": ASK, "label": LABEL[ASK],
            "reason": "开场第 1 个话题（按简历）" if self.resume_used else "开场第 1 个话题",
            "question": self.question_index,
            "max_question": self.max_questions, "role": self.role_name,
        })
        if self.resume_used:
            directive = "面试开始。先从简历里挑一个最值得追问的项目，直接提出第 1 个话题，一句话。"
        else:
            directive = "面试开始。请直接提出第 1 个话题，一句话。"
        text = self._generate(emit, directive, max_tokens=120)
        self.history.append({"role": "assistant", "content": text})
        self.turns.append({
            "turn": turn_id, "role": "agent", "text": text, "action": ASK,
            "action_label": LABEL[ASK], "reason": "开场", "question": self.question_index,
            "ts": round(time.time() - self.started_at, 3),
        })
        emit({"t": "done", "turn": turn_id, "question": self.question_index,
              "state": self.state(), "finished": False})

    # -- 一轮 --------------------------------------------------------
    def handle_user(self, text: str, asr_empty: bool, emit):
        self.turn_seq += 1
        turn_id = self.turn_seq
        if normalize(text):
            self.silences = 0        # 开口了，冷场计数清零
        decision = decide(text, asr_empty, self.probes, self.clarifys, self.cfg,
                          self.switches, self.level_key)
        action = decision["action"]

        # 记录用户这轮
        self.turns.append({
            "turn": turn_id, "role": "user", "text": text or "",
            "action": action, "action_label": LABEL[action],
            "reason": decision["reason"], "chars": decision["chars"],
            "question": self.question_index, "ts": round(time.time() - self.started_at, 3),
        })
        if normalize(text):
            self.history.append({"role": "user", "content": text})

        # 已经问完最后一个话题：直接收尾，**不要再发一个"提问"的 meta**——
        # 前端收到 meta 就会开一个气泡，接着 meta 又换成"收尾"，于是留下一个闪烁的空气泡。
        if action == ASK and self.question_index >= self.max_questions:
            self._close(emit, turn_id)
            return

        emit({
            "t": "meta", "turn": turn_id, "action": action, "label": LABEL[action],
            "reason": decision["reason"], "question": self.question_index,
            "max_question": self.max_questions, "chars": decision["chars"],
        })

        # ---- 澄清：纯代码，0 token，不推进题号
        if action == CLARIFY:
            self.clarifys += 1
            line = CLARIFY_LINES[(self.turn_seq - 1) % len(CLARIFY_LINES)]
            # 澄清也要先推 tok，否则前端气泡只有一个空框（字幕靠 tok 拼）
            emit({"t": "tok", "d": line})
            emit({"t": "sent", "d": line})
            emit({"t": "usage", "in": 0, "out": 0, "cost": 0, "cached": True})
            self.history.append({"role": "assistant", "content": line})
            self.turns.append({
                "turn": turn_id + 0.1, "role": "agent", "text": line, "action": CLARIFY,
                "action_label": LABEL[CLARIFY], "reason": "模板澄清，未调模型",
                "question": self.question_index, "zero_cost": True,
                "ts": round(time.time() - self.started_at, 3),
            })
            emit({"t": "done", "turn": turn_id, "question": self.question_index,
                  "state": self.state(), "finished": False})
            return

        # ---- 追问 / 换题 / 下一题
        if action == PROBE:
            self.probes += 1
            directive = (
                f"候选人上一句是：「{text}」。就其中一处含糊的地方追问一句，"
                "问具体做了什么、数据是多少，不要换话题，不要评价。"
            )
        elif action == SWITCH:
            self.switches += 1
            self.probes = 0
            self.easier = min(2, self.easier + 1)
            directive = (
                "候选人表示没做过或不知道。换一个更基础、更容易开口的同类问题，"
                "一句话，语气正常，不要说「没关系」以外的安慰话。"
            )
        else:  # ASK
            self.question_index += 1
            self.probes = 0
            self.clarifys = 0
            self.switches = 0
            self.easier = 0
            directive = f"进入第 {self.question_index} 个话题，提一个新问题，一句话。"

        if self.easier and action in (PROBE, ASK):
            directive += "（候选人刚被降过难度，问题保持基础。）"

        text_out = self._generate(emit, directive, max_tokens=220)
        if self.interrupts:
            self.check_recovery(text_out)          # 粗判：有没有重复被打断前说过的那段
        self.history.append({"role": "assistant", "content": text_out})
        self.turns.append({
            "turn": turn_id + 0.1, "role": "agent", "text": text_out, "action": action,
            "action_label": LABEL[action], "reason": "模型生成",
            "question": self.question_index, "ts": round(time.time() - self.started_at, 3),
        })
        emit({"t": "done", "turn": turn_id, "question": self.question_index,
              "state": self.state(), "finished": False})

    def _close(self, emit, turn_id: int, reason: str | None = None, directive: str | None = None):
        emit({"t": "meta", "turn": turn_id, "action": CLOSE, "label": LABEL[CLOSE],
              "reason": reason or f"已完成 {self.max_questions} 个话题", "question": self.question_index,
              "max_question": self.max_questions})
        line = self._generate(
            emit,
            directive or "这是最后一个话题之后的收尾。只说一句收尾话，不总结、不点评。",
            max_tokens=80,
        )
        self.history.append({"role": "assistant", "content": line})
        self.finished = True
        self.ended_at = time.time()
        self.turns.append({
            "turn": turn_id + 0.1, "role": "agent", "text": line, "action": CLOSE,
            "action_label": LABEL[CLOSE], "reason": reason or "收尾",
            "question": self.question_index,
            "ts": round(time.time() - self.started_at, 3),
        })
        emit({"t": "done", "turn": turn_id, "question": self.question_index,
              "state": self.state(), "finished": True})

    # -- 冷场 --------------------------------------------------------
    def silence(self, emit):
        """候选人一直没开口时，面试官主动接一句；再没动静就把这场收了。

        为什么不走 decide()：空文本在那儿会被判成 CLARIFY（那是给识别失败用的），
        连催两次就变成「我没听清」，用户会以为麦克风坏了——而这里的事实是
        「他没说话」，该做的是催一句，再没人接就把面试收掉、给他一份复盘。

        第 1 次：催一句（模板，0 token）
        第 2 次：收尾并结束本场（前端会把复盘弹出来）
        之后：什么都不发，已经结束了
        """
        if self.silences >= 2:
            return      # 已经催过、也收过了，别重复

        self.silences += 1
        self.turn_seq += 1
        turn_id = self.turn_seq

        if self.silences == 1:
            line = "不着急，想到什么说什么，我们接着聊。"
            emit({"t": "meta", "turn": turn_id, "action": NUDGE, "label": LABEL[NUDGE],
                  "reason": "一直没开口，先催一句", "question": self.question_index,
                  "max_question": self.max_questions})
            emit({"t": "tok", "d": line})
            emit({"t": "sent", "d": line})
            emit({"t": "usage", "in": 0, "out": 0, "cost": round(self.cost(), 4), "cached": True})
            self.history.append({"role": "assistant", "content": line})
            self.turns.append({
                "turn": turn_id + 0.1, "role": "agent", "text": line, "action": NUDGE,
                "action_label": LABEL[NUDGE], "reason": "模板催场，未调模型",
                "question": self.question_index, "zero_cost": True,
                "ts": round(time.time() - self.started_at, 3),
            })
            emit({"t": "done", "turn": turn_id, "question": self.question_index,
                  "state": self.state(), "finished": False})
            return

        # 催过之后还是没动静：不硬撑，收尾并结算这一场
        self._close(
            emit, turn_id,
            reason="催过之后仍然没开口，本场到此为止",
            directive="候选人催过一次还是没开口，这场就到这里。只说一句收尾话，"
                      "不点评、不总结，语气正常。",
        )

    # -- 打断 --------------------------------------------------------
    def interrupt(self, ratio: float, spoken: str, heard: str) -> dict:
        """记录一次打断。下一轮告诉模型：你停在哪、他插了什么，别再重复。"""
        ratio = max(0.0, min(1.0, float(ratio or 0)))
        rec = {
            "at": round(time.time() - self.started_at, 3),
            "ratio": round(ratio, 3),
            "spoken_chars": len(spoken or ""),
            "spoken": (spoken or "")[-80:],
            "heard": (heard or "")[:80],
            "turn": self.turn_seq,
            "recovered": None,
        }
        self.interrupts.append(rec)

        if heard:
            note = (
                f"[系统] 你刚才说到「…{rec['spoken']}」时被候选人打断（已播报约 {int(ratio * 100)}%），"
                f"他插话说：「{heard}」。先回应他这句，不要重复你被打断前说过的内容。"
                "也不要把没说完的部分补念一遍——他已经在说别的事了。"
            )
        else:
            note = (
                f"[系统] 你刚才说到「…{rec['spoken']}」时被打断（已播报约 {int(ratio * 100)}%），"
                "但没听清他插了什么，可能是没听清或者网络卡了。用一句话问他要不要你重说一遍，"
                "不要自顾自往下讲。"
            )
        self.pending_interrupt = note
        return {"ok": True, "ratio": ratio, "interrupts": len(self.interrupts)}

    def check_recovery(self, new_text: str):
        """代码粗判：新回复有没有重复被打断前说过的那段。非盲评、只是代理指标。"""
        if not self.interrupts:
            return
        rec = self.interrupts[-1]
        if rec["recovered"] is not None:
            return
        if self.turn_seq != rec["turn"] + 1:      # 只判被打断后的那一轮，别拿后面的话硬比
            return
        spoken = normalize(rec["spoken"])
        fresh = normalize(new_text)
        if not spoken or not fresh:
            rec["recovered"] = None
            return
        head = spoken[:12]
        rec["recovered"] = not (len(head) >= 6 and fresh.startswith(head))

    # -- 计时 --------------------------------------------------------
    def set_timing(self, turn_id, first_token_ms=None, first_audio_ms=None, total_ms=None):
        """客户端分两次上报（首字先到、首音后到），按非空值合并。"""
        key = str(turn_id)
        cur = self.timings.setdefault(
            key, {"first_token_ms": None, "first_audio_ms": None, "total_ms": None}
        )
        if first_token_ms is not None:
            cur["first_token_ms"] = first_token_ms
        if first_audio_ms is not None:
            cur["first_audio_ms"] = first_audio_ms
        if total_ms is not None:
            cur["total_ms"] = total_ms

    # -- 状态与指标 --------------------------------------------------
    def state(self) -> dict:
        answered = [t for t in self.turns if t["role"] == "user" and t["action"] != CLARIFY]
        return {
            "question": self.question_index,
            "max_question": self.max_questions,
            "turn": self.turn_seq,
            "answered": len(answered),
            "probes": self.probes,
            "clarifys": self.clarifys,
            "interrupts": len(self.interrupts),
            "cost": round(self.cost(), 4),
            "tokens": {"in": self.usage["miss"] + self.usage["hit"], "out": self.usage["out"]},
            "finished": self.finished,
            "mode": self.llm.mode,
        }

    def cost(self) -> float:
        return cost_cny(self.usage, self.cfg.get("price", {}))

    def metrics(self) -> dict:
        user_turns = [t for t in self.turns if t["role"] == "user"]
        answered = [t for t in user_turns if t["action"] != CLARIFY]
        actions = {k: 0 for k in (CLARIFY, PROBE, SWITCH, ASK)}
        for t in user_turns:
            actions[t["action"]] = actions.get(t["action"], 0) + 1

        first_token = [v["first_token_ms"] for v in self.timings.values() if v.get("first_token_ms")]
        first_audio = [v["first_audio_ms"] for v in self.timings.values() if v.get("first_audio_ms")]
        total = [v["total_ms"] for v in self.timings.values() if v.get("total_ms")]
        chars = [t.get("chars", 0) for t in answered]

        recovered = [r["recovered"] for r in self.interrupts if r["recovered"] is not None]
        return {
            "session_id": self.session_id,
            "role": self.role_name,
            "duration_s": round((self.ended_at or time.time()) - self.started_at, 1),
            "questions": self.question_index,
            "max_questions": self.max_questions,
            "user_turns": len(user_turns),
            "answered": len(answered),
            "avg_answer_chars": round(sum(chars) / len(chars), 1) if chars else 0,
            "actions": actions,
            "clarify_rate": round(actions[CLARIFY] / len(user_turns), 3) if user_turns else 0,
            "probe_rate": round(actions[PROBE] / len(user_turns), 3) if user_turns else 0,
            "switch_rate": round(actions[SWITCH] / len(user_turns), 3) if user_turns else 0,
            "latency": {
                "first_token_p50": pct(first_token, 0.5),
                "first_token_p95": pct(first_token, 0.95),
                "first_audio_p50": pct(first_audio, 0.5),
                "first_audio_p95": pct(first_audio, 0.95),
                "total_p50": pct(total, 0.5),
                "total_p95": pct(total, 0.95),
                "samples": len(total),
            },
            "interrupts": {
                "count": len(self.interrupts),
                "avg_ratio": round(sum(r["ratio"] for r in self.interrupts) / len(self.interrupts), 2)
                if self.interrupts else 0,
                "recovered": sum(1 for r in recovered if r),
                "checked": len(recovered),
                "recovery_rate": round(sum(1 for r in recovered if r) / len(recovered), 2) if recovered else None,
            },
            "usage": {
                "in": self.usage["miss"] + self.usage["hit"],
                "out": self.usage["out"],
                "hit": self.usage["hit"],
                "miss": self.usage["miss"],
                "mock": self.usage["mock"],
                "estimated": self.usage["estimated"],
            },
            "cost_cny": round(self.cost(), 4),
            "mode": self.llm.mode,
            "level": self.level_key,
            "level_name": self.level.get("name", "标准"),
        }

    # -- 复盘 --------------------------------------------------------
    def report(self) -> dict:
        if getattr(self, "_report_cache", None):       # 复盘调模型要钱，只生成一次
            return self._report_cache
        m = self.metrics()
        gen = self._llm_report(m) if self.llm.mode == "live" else None
        if not gen:
            gen = self._template_report(m)
            if self.llm.mode == "live":
                # 真实模式却没拿到模型复盘：多半是 JSON 被 max_tokens 截断。
                # 必须显式告诉用户，否则他会以为"模型就是给不出参考答法"（实测踩过）
                gen["note"] = ("这次的复盘没拿到完整的模型返回（通常是输出被截断），"
                               "已用模板兜底——所以没有参考答法。下一场会重试。")
        gen["metrics"] = m
        gen["transcript"] = [
            {"role": t["role"], "text": t["text"], "action": t.get("action_label", "")}
            for t in self.turns if t["role"] == "user"
        ]
        # 一问一答的配对：复盘里要能"看着自己的原话"对照参考答法
        pairs, last_q = [], None
        for t in self.turns:
            if t["role"] == "agent":
                last_q = t["text"]
            elif t["role"] == "user" and last_q:
                pairs.append({"q": last_q, "a": t["text"]})
                last_q = None
        gen["pairs"] = pairs[:12]
        gen["mode"] = "live" if self.llm.mode == "live" else "mock"
        self._report_cache = gen
        return gen

    def _llm_report(self, m: dict):
        transcript = "\n".join(
            f"{'面' if t['role'] == 'agent' else '候'}：{t['text']}"
            for t in self.turns if t.get("action") != CLOSE
        )
        prompt = (
            "下面是一场模拟面试的完整记录和客观指标。请给出一份复盘，只输出 JSON，不要任何解释文字。\n\n"
            f"【记录】\n{transcript[-3000:]}\n\n"
            f"【指标】回答 {m['answered']} 个话题，平均 {m['avg_answer_chars']} 字/答，"
            f"追问 {m['actions']['probe']} 次，澄清 {m['actions']['clarify']} 次，"
            f"换题 {m['actions']['switch']} 次，打断 {m['interrupts']['count']} 次。\n"
            f"本场难度：{m.get('level_name', '标准')}。\n\n"
            "输出格式：\n"
            '{"score": 0-100 的整数, "verdict": "一句话总评，20 字内",'
            ' "strengths": ["优势1", "优势2", "优势3"],'
            ' "improvements": [{"issue": "问题", "fix": "具体改法"}],'
            ' "per_round": [{"q": "问题简述（15 字内）", "comment": "一句话点评他答得怎么样",'
            ' "approach": "答题思路：从哪个方向切入、按什么顺序讲（30-60 字）",'
            ' "data": "这道题该准备哪些数据（一句话，点名要报的数字或事实）",'
            ' "sample": "参考答法，80-120 字"}]}\n'
            "要求：\n"
            "1. 优势和改进各 3 条，必须引用候选人原话里的具体词；改进项要给可执行动作，"
            "不要写「多练习」这种空话。\n"
            "2. per_round 最多 7 条，优先选被追问过的、答得最差的几题（不用每题都写）。\n"
            "3. 三个字段各有分工，都写短一点：\n"
            "   · approach = 怎么想（≤40 字）：先说什么、再说什么，这类题该从哪个角度切入；\n"
            "   · data = 该备哪些数（≤25 字）：具体到「评测集条数、卡点条数、bad case 前后比例」这种；\n"
            "   · sample = 怎么开口（60-90 字）：候选人第一人称，2-3 句，必须带一个具体数字或真实例子"
            "（比如「把评测集从 20 条扩到 200 条，bad case 从 31% 降到 12%」），"
            "不要写「要有数据支撑」这种空话。\n"
            "4. sample 写的是**更好的答法**，不是重复候选人说过的话。"
        )
        # 输出量估计要准：每条 per_round 三段话 ≈ 300–400 token，
        # 7 条就 2000 多。之前给 1400，JSON 必然在中间被切断（实测 2478 字处断）。
        for max_tok, extra in (
            (2800, ""),
            (3200, "\n\n这次一定要输出**完整合法**的 JSON：sample 压到 60 字内，per_round 最多 5 条。"),
        ):
            try:
                buf = []
                for ev in self.llm.stream(
                    [{"role": "system", "content": "你是资深面试官，说话直接、具体、不客套。"},
                     {"role": "user", "content": prompt + extra}],
                    temperature=0.3, max_tokens=max_tok, purpose="report",
                ):
                    if ev["type"] == "token":
                        buf.append(ev["text"])
                    elif ev["type"] == "usage":
                        self.usage["hit"] += int(ev.get("hit", 0))
                        self.usage["miss"] += int(ev.get("miss", 0))
                        self.usage["out"] += int(ev.get("out", 0))
                text = "".join(buf)
                gen = self._parse_json(text)
                if gen:
                    return gen
                # 记下失败原因，方便排查是截断还是格式问题
                self.llm.last_error = f"复盘 JSON 不完整（{len(text)} 字，末尾：{text[-24:]!r}）"
            except Exception as e:
                self.llm.last_error = str(e)
        return None

    @staticmethod
    def _parse_json(text: str):
        if not text:
            return None
        raw = text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict) or "score" not in obj:
            return None
        return obj

    def _template_report(self, m: dict) -> dict:
        """没接模型时的模板复盘。基于指标，不假装懂内容。"""
        strengths, improvements = [], []
        if m["avg_answer_chars"] >= 70:
            strengths.append(f"回答平均 {m['avg_answer_chars']:.0f} 字，信息密度够，撑得住追问")
        if m["interrupts"]["count"] == 0:
            strengths.append("全程没有被打断，表达节奏是稳的")
        if m["actions"]["switch"] == 0:
            strengths.append("没有出现「不知道」类放弃回答，遇到不会的题也在接")
        if m["questions"] >= m["max_questions"]:
            strengths.append(f"完整走完 {m['max_questions']} 个话题，没有中途放弃")
        # 兜底补齐不能重复同一句：同一句话连写两遍，读者第一反应是这份复盘拼出来的。
        # 模板复盘只谈指标、不假装懂内容，所以兜底也只说「记录本身」这类可核实的事。
        fillers = [
            "完成了整场对练，样本已记录，可用于对比下一场",
            f"逐轮都有原文可回看（{m['user_turns']} 次开口），复盘不靠印象",
            f"本场 {m['questions']} 个话题走完，下一场换个岗位就能横向对比",
        ]
        for s in fillers:
            if len(strengths) >= 3:
                break
            if s not in strengths:
                strengths.append(s)

        if m["probe_rate"] >= 0.35:
            improvements.append({
                "issue": f"{int(m['probe_rate'] * 100)}% 的回答需要追问，说明细节给得不够",
                "fix": "每个回答补一个数字：多少人、多少天、从 X 到 Y",
            })
        if m["clarify_rate"] >= 0.15:
            improvements.append({
                "issue": f"澄清触发率 {int(m['clarify_rate'] * 100)}%，偏高",
                "fix": "开口先给结论句，别用「嗯、就是」起头，识别更容易"
            })
        if m["avg_answer_chars"] < 60:
            improvements.append({
                "issue": f"平均回答只有 {m['avg_answer_chars']:.0f} 字，展开不足",
                "fix": "用「背景 20 字 + 我做了什么 40 字 + 结果数字 20 字」的三段式"
            })
        if m["actions"]["switch"] >= 2:
            improvements.append({
                "issue": f"{m['actions']['switch']} 次换题，说明有几类问题完全没准备",
                "fix": "把「没做过」换成「没直接做过，但我做过类似的是…」，先接住再转"
            })
        # 同理：改进项也不能重复同一句。兜底只放「由指标能直接看出来」的几条。
        pool = [{
            "issue": "收尾没有主动提问",
            "fix": "准备 1 个反问：团队现在最想解决的问题是什么",
        }]
        if m["questions"] < m["max_questions"]:
            pool.append({
                "issue": f"只走了 {m['questions']} 个话题，样本偏少",
                "fix": "先按同一个岗位多跑几场，再谈「我哪里弱」才有依据",
            })
        if m["interrupts"]["count"] > 0:
            pool.append({
                "issue": f"被打断 {m['interrupts']['count']} 次，说明有抢话或超时",
                "fix": "每答控制在 40 秒内，先给结论再补细节，别从头讲起",
            })
        seen = {i["issue"] for i in improvements}
        for item in pool:
            if len(improvements) >= 3:
                break
            if item["issue"] not in seen:
                improvements.append(item)
                seen.add(item["issue"])

        score = 55
        score += min(15, int(m["avg_answer_chars"] / 8))
        score += 10 if m["actions"]["switch"] == 0 else 0
        score -= int(m["clarify_rate"] * 40)
        score = max(40, min(88, score))

        # 一句话结论按本场的实际问题选，不要每场都是同一句（往期列表会并排显示）
        if m["questions"] < max(2, m["max_questions"] // 2):
            verdict = "话题没走完，有几类问题接不住。"
        elif m["actions"]["switch"] >= 2:
            verdict = "能接住一部分，但每次卡住都靠换题。"
        elif m["probe_rate"] >= 0.35:
            verdict = "细节偏薄，三分之一的回答都被追问。"
        elif m["avg_answer_chars"] >= 70 and m["actions"]["switch"] == 0:
            verdict = "内容够厚，撑得住追问，收尾再主动一点。"
        elif m["clarify_rate"] >= 0.15:
            verdict = "内容还行，但开口太碎，好几次没听清。"
        else:
            verdict = "能答完，但细节和数字还不够硬。"

        return {
            "score": score,
            "verdict": verdict,
            "strengths": strengths[:3],
            "improvements": improvements[:3],
            "per_round": [
                # 演示模式没有模型，给不出参考答法——这里留空，前端会说明原因，
                # 不假装自己懂内容（模板复盘只谈指标）
                {"q": f"第 {t['question']} 个话题", "comment": t["reason"],
                 "approach": "", "data": "", "sample": ""}
                for t in self.turns if t["role"] == "user" and t["action"] != CLARIFY
            ][:8],
            "generated_by": "template",
        }

    # -- 落盘 --------------------------------------------------------
    def dump(self) -> dict:
        return {
            "session_id": self.session_id,
            "role": self.role_key,
            "role_name": self.role_name,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "duration_s": round((self.ended_at or time.time()) - self.started_at, 1),
            "max_questions": self.max_questions,
            "finished": self.finished,
            "mode": self.llm.mode,
            "turns": self.turns,
            "interrupts": self.interrupts,
            "timings": self.timings,
            "usage": self.usage,
            "metrics": self.metrics(),
            # 只记模式、文件名、字数——简历原文不进磁盘
            "resume": {
                "mode": "resume" if self.resume_used else "role",
                "name": self.resume_name,
                "chars": len(self.resume),
                "in_prompt_chars": len(self.resume_used),
            },
        }

    def save(self, runs_dir: Path, report: dict | None = None) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
        folder = Path(runs_dir) / f"{stamp}-{self.session_id[:6]}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "session.json").write_text(
            json.dumps(self.dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if report:
            (folder / "report.md").write_text(render_markdown(report), encoding="utf-8")
            (folder / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return folder


# ---------------------------------------------------------------- 报告渲染

def render_markdown(report: dict) -> str:
    m = report.get("metrics", {})
    lat = m.get("latency", {})
    itr = m.get("interrupts", {})
    u = m.get("usage", {})

    def ms(v):
        return f"{v:.0f} ms" if isinstance(v, (int, float)) else "—"

    lines = [
        f"# 对练复盘 · {m.get('role', '')}",
        "",
        f"> {report.get('verdict', '')}　**{report.get('score', '—')} / 100**",
        "",
        "## 三条优势",
        *[f"- {s}" for s in report.get("strengths", [])],
        "",
        "## 三条改进",
    ]
    for i, item in enumerate(report.get("improvements", []), 1):
        if isinstance(item, dict):
            lines.append(f"{i}. **{item.get('issue', '')}** → {item.get('fix', '')}")
        else:
            lines.append(f"{i}. {item}")

    lines += [
        "",
        "## 本场数据",
        "| 项 | 值 |",
        "|---|---|",
        f"| 时长 | {m.get('duration_s', 0)} 秒 |",
        f"| 话题 | {m.get('questions', 0)} / {m.get('max_questions', 0)} |",
        f"| 回答轮次 | {m.get('user_turns', 0)}（澄清 {m.get('actions', {}).get('clarify', 0)}） |",
        f"| 平均回答 | {m.get('avg_answer_chars', 0)} 字 |",
        f"| 追问 / 换题 | {m.get('actions', {}).get('probe', 0)} / {m.get('actions', {}).get('switch', 0)} |",
        f"| 澄清触发率 | {m.get('clarify_rate', 0) * 100:.0f}% |",
        "",
        "### 延迟（说完 → 各节点）",
        "| 指标 | P50 | P95 |",
        "|---|---|---|",
        f"| 首字 | {ms(lat.get('first_token_p50'))} | {ms(lat.get('first_token_p95'))} |",
        f"| 首音 | {ms(lat.get('first_audio_p50'))} | {ms(lat.get('first_audio_p95'))} |",
        f"| 整轮 | {ms(lat.get('total_p50'))} | {ms(lat.get('total_p95'))} |",
        "",
        "### 打断",
        f"- 次数 {itr.get('count', 0)}，平均打断位置 {int((itr.get('avg_ratio') or 0) * 100)}%",
        "- 被打断后它立刻停、不补念；这里看的是它有没有重复说过的话",
        f"- 不重复率 {itr.get('recovery_rate') if itr.get('recovery_rate') is not None else '—'}"
        f"（代码粗判 / 自评，非盲评，样本 {itr.get('checked', 0)}）",
        "",
        "### 成本（实测）",
        f"- 输入 {u.get('in', 0):,} token（命中缓存 {u.get('hit', 0):,}）／输出 {u.get('out', 0):,} token",
        f"- 合计 **¥{m.get('cost_cny', 0):.4f}**"
        + ("（演示模式，token 为估算值）" if u.get("mock") else ""),
        "",
        "## 逐轮",
    ]
    for row in report.get("per_round", []):
        lines.append(f"### {row.get('q', '')}")
        if row.get("comment"):
            lines.append(f"- 点评：{row.get('comment')}")
        if row.get("approach"):
            lines.append(f"- **答题思路**：{row.get('approach')}")
        if row.get("data"):
            lines.append(f"- **该备的数据**：{row.get('data')}")
        if row.get("sample"):
            lines.append(f"- **参考答法**：{row.get('sample')}")
        elif report.get("mode") == "mock" or report.get("generated_by") == "template":
            lines.append("- 参考答法：这一场没有（演示模式，或模型返回不完整）")
        lines.append("")

    lines += [
        "",
        "---",
        "> 评分是模型自评，非盲评；只有作者一个用户，样本量为 1，不主张泛化结论。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 配置

def _load_dotenv(path: Path) -> None:
    """极简 .env 读取：只认 KEY=VALUE，已存在的环境变量优先，不覆盖。"""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and not os.environ.get(key):
            os.environ[key] = val


def load_config(path: Path | str | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    _load_dotenv(BASE_DIR / ".env")   # Key 优先级：系统环境变量 > .env > config.json
    cfg = {}
    if path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("model", "deepseek-flash")
    cfg.setdefault("api_base", "https://api.deepseek.com")
    cfg.setdefault("max_questions", 8)
    cfg.setdefault("max_probe_per_question", 2)
    cfg.setdefault("max_clarify_per_question", 2)
    cfg.setdefault("max_switch_per_question", 2)
    cfg.setdefault("silence_seconds", 5)
    cfg.setdefault("silence_close_seconds", 5)
    cfg.setdefault("thinking", {"turn": "disabled", "report": "enabled"})
    cfg.setdefault("turn", {})
    for k, v in (("empty_chars", 4), ("short_chars", 22), ("max_sentence_chars", 60),
                 ("soft_sentence_chars", 22), ("history_limit", 30)):
        cfg["turn"].setdefault(k, v)
    cfg.setdefault("price", {"input_miss": 2.0, "input_hit": 0.04, "output": 8.0, "offpeak_factor": 0.5})
    cfg.setdefault("roles", {"ai_pm": "AI 产品经理（实习）", "pm": "产品经理", "general": "通用岗位"})
    return cfg
