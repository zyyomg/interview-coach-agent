#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 01/02/03/06 提炼成 8 页投递版作品集，输出自包含 HTML（图片内嵌）。
数据来源全部可核：文档正文 + runs/ 实测 + web/style.css。
用法：python dev/build-作品集pdf.py
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "作品集" / "figma"
OUT = ROOT / "作品集" / "面试陪练Agent-作品集.html"


def img_uri(name: str, w: int = 1600) -> str:
    im = Image.open(FIG / name).convert("RGB")
    h = round(im.height * w / im.width)
    im = im.resize((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True, progressive=True)
    b = buf.getvalue()
    print(f"  {name} -> {len(b)//1024} KB")
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: "Microsoft YaHei", "PingFang SC", -apple-system, sans-serif;
       color: #12141A; background: #fff;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.page { width: 210mm; height: 297mm; padding: 17mm 18mm 15mm; position: relative;
        page-break-after: always; overflow: hidden; background: #fff; }
.page:last-of-type { page-break-after: auto; }

.kicker { font-size: 7.6pt; color: #2B55D6; letter-spacing: .14em; font-weight: 700; }
h2 { font-size: 18pt; line-height: 1.34; margin: 2.5mm 0 1.5mm; font-weight: 700; letter-spacing: -.2px; }
.lead { font-size: 9.4pt; color: #5A6473; line-height: 1.8; }
.rule { height: 2px; background: #12141A; width: 16mm; margin: 3.5mm 0 4mm; }

h3 { font-size: 10pt; font-weight: 700; margin: 0 0 2mm; color: #12141A; }
p, li { font-size: 9.3pt; line-height: 1.82; color: #2A2F3A; }
.small { font-size: 8.2pt; color: #5A6473; line-height: 1.75; }

table { width: 100%; border-collapse: collapse; font-size: 8.6pt; }
th { text-align: left; background: #F4F5F7; padding: 2mm 2.4mm; font-weight: 700;
     color: #5A6473; border-bottom: 1px solid #E6E8EC; font-size: 8pt; letter-spacing: .04em; }
td { padding: 2.1mm 2.4mm; border-bottom: 1px solid #EEF0F3; vertical-align: top; line-height: 1.6; }
td.num { color: #98A1B0; font-variant-numeric: tabular-nums; width: 7mm; }
.tag { display: inline-block; font-size: 6.8pt; font-weight: 700; padding: .6mm 1.6mm;
       border-radius: 2mm; vertical-align: 1px; }
.p0 { background: #EEF2FF; color: #2B55D6; }
.p1 { background: #F4F5F7; color: #5A6473; }
.ok { background: #E3F7EF; color: #067A5B; }
.bad { background: #FEF7E0; color: #A16207; }
.mid { background: #EEF0F3; color: #4B5563; }

.cards { display: grid; grid-template-columns: 1fr 1fr; gap: 3.5mm; }
.card { border: 1px solid #E6E8EC; border-radius: 2.5mm; padding: 3.2mm 3.5mm; }
.card h3 { font-size: 9.2pt; margin-bottom: 1.8mm; }
.card ul { list-style: none; }
.card li { font-size: 8.5pt; line-height: 1.7; padding-left: 3.2mm; position: relative; color: #2A2F3A; }
.card li::before { content: ""; position: absolute; left: 0; top: 2.1mm; width: 1.2mm;
                   height: 1.2mm; background: #C5D2F8; border-radius: 50%; }

.quote { border-left: 2.5px solid #2B55D6; background: #FAFBFF; padding: 3.2mm 3.8mm;
         font-size: 9.1pt; line-height: 1.82; color: #2A2F3A; }
.metric { display: flex; gap: 3mm; }
.metric div { flex: 1; border: 1px solid #E6E8EC; border-radius: 2.5mm; padding: 2.6mm 3mm; }
.metric b { display: block; font-size: 15pt; color: #2B55D6; line-height: 1.2; }
.metric span { font-size: 7.3pt; color: #5A6473; }

.shot { border: 1px solid #E6E8EC; border-radius: 2mm; overflow: hidden; }
.shot img { display: block; width: 100%; }
.cap { display: flex; gap: 2.2mm; align-items: baseline; margin-top: 1.6mm; }
.cap b { font-size: 8.7pt; white-space: nowrap; }
.cap span { font-size: 8pt; color: #5A6473; line-height: 1.6; }

.foot { position: absolute; left: 18mm; right: 18mm; bottom: 8mm;
        display: flex; justify-content: space-between;
        font-size: 6.9pt; color: #98A1B0; border-top: 1px solid #E6E8EC; padding-top: 2.4mm; }

.cover-h1 { font-size: 34pt; font-weight: 700; letter-spacing: -0.8px; line-height: 1.12; }
.cover-sub { font-size: 11pt; color: #5A6473; margin-top: 3mm; letter-spacing: .02em; }
.cover-one { font-size: 13pt; line-height: 1.68; color: #12141A; margin-top: 12mm;
             max-width: 150mm; font-weight: 400; }
.meta { margin-top: 12mm; border-top: 1px solid #E6E8EC; }
.meta div { display: flex; gap: 4mm; padding: 2.4mm 0; border-bottom: 1px solid #EEF0F3; font-size: 8.6pt; }
.meta b { flex: 0 0 26mm; color: #98A1B0; font-weight: 400; }
.meta span { flex: 1; }
.links { margin-top: 7mm; display: flex; gap: 4mm; }
.links div { flex: 1; background: #F4F5F7; border-radius: 2.5mm; padding: 3mm 3.5mm; }
.links em { display: block; font-style: normal; font-size: 6.9pt; color: #98A1B0;
            letter-spacing: .1em; margin-bottom: 1.2mm; }
.links code { font-family: "Consolas", monospace; font-size: 7.6pt; color: #2B55D6; word-break: break-all; }
"""


def page(inner: str, no: int, label: str) -> str:
    return f"""<section class="page">{inner}
  <div class="foot"><span>面试陪练 Agent · 作品集</span><span>{label}</span><span>{no:02d} / 08</span></div>
</section>"""


def build() -> str:
    print("内嵌三屏图 …")
    p1, p2, p3 = img_uri("01-屏@2x.png"), img_uri("02-屏@2x.png"), img_uri("03-屏@2x.png")

    pages = []

    # ---------------- 01 封面
    pages.append(page(f"""
  <div class="kicker">个人项目 · AI 产品经理</div>
  <h1 class="cover-h1" style="margin-top:6mm">面试陪练 Agent</h1>
  <div class="cover-sub">AI 语音面试陪练工具 ｜ 桌面 Web ｜ 本地运行</div>
  <p class="cover-one">10 分钟打一场 8 个话题的语音面试，<br>结束后拿到一份能照着改的复盘。</p>
  <div class="meta">
    <div><b>我的角色</b><span>需求定义 · PRD · 界面设计 · 前端实现 · 验收测试</span></div>
    <div><b>技术</b><span>Python 标准库（零第三方依赖）+ DeepSeek；浏览器语音识别与播报</span></div>
    <div><b>时间</b><span>2026.10 – 2026.11</span></div>
    <div><b>状态</b><span>本地版已上线；10 场真人评测进行中（当前 1 / 10 场走满 8 话题）</span></div>
  </div>
  <div class="links">
    <div><em>设计稿 · 可点击原型</em><code>figma.com/proto/MeGvma1k5lRYE9b3mfJf5c</code></div>
    <div><em>代码仓库</em><code>github.com/zyyomg/interview-coach-agent</code></div>
  </div>
  <div style="position:absolute; left:18mm; right:18mm; bottom:22mm">
    <div class="shot"><img src="{p1}" alt="01 首页"></div>
  </div>
""", 1, "封面"))

    # ---------------- 02 问题与定位
    pages.append(page("""
  <div class="kicker">01 问题</div>
  <h2>准备面试的人不缺题，<br>缺的是开口的机会和被纠错的机会</h2>
  <div class="rule"></div>
  <table style="margin-bottom:6mm">
    <tr><th style="width:30%">现有做法</th><th>为什么解决不了</th></tr>
    <tr><td><b>真人模拟面试</b><br><span class="small">¥300–800 / 小时</span></td>
        <td>约人、约时间都麻烦，一场只能覆盖一家公司；投递量大的人练不起。</td></tr>
    <tr><td><b>对着镜子练</b></td><td>没有追问。练的是背稿，不是应答。</td></tr>
    <tr><td><b>刷题库</b></td><td>知道"该说什么"，不知道"说得够不够具体"。真实面试里被杀掉的往往是数据说不清，不是不会答。</td></tr>
  </table>

  <div class="quote">
    <b>核心洞察：</b>AI 产品岗的面试官会追着简历上的一个数字问三层，答不上来就判定"这段经历是包装的"。<br>
    <b>这种追问压力，只能靠反复被追问来练——而这件事恰好是可以自动化的。</b>
  </div>

  <h3 style="margin-top:6mm">目标</h3>
  <div class="metric">
    <div><b>10</b><span>分钟打完一场</span></div>
    <div><b>8</b><span>个话题，到上限自动收尾</span></div>
    <div><b>¥0.05</b><span>单场成本上限（实测更低）</span></div>
  </div>

  <h3 style="margin-top:6mm">给谁做</h3>
  <p>求职 AI 产品经理 / 产品经理实习的在校生与转岗者。<b>有时间、缺机会、预算低</b>，投递量大——一家公司一场面试。</p>

  <h3 style="margin-top:5mm">不做什么（明确划出去）</h3>
  <table>
    <tr><td class="num">1</td><td><b>不做真人面试的替代品</b>——定位是高频低成本的练习工具</td></tr>
    <tr><td class="num">2</td><td><b>不做题库</b>——题目由模型按岗位名生成，不人工维护题集，因为题库会过期</td></tr>
    <tr><td class="num">3</td><td><b>不做账号体系与云端</b>——本地跑，数据留在自己机器上</td></tr>
    <tr><td class="num">4</td><td><b>不做视频面试、不做简历优化建议</b>——各有各的问题域，混进来会让复盘失焦</td></tr>
  </table>

  <h3 style="margin-top:5.5mm">两个用户故事</h3>
  <div class="quote">
    <b>故事一（体验式，练表达）</b><br>
    「我明天要面一家 AI 公司，不知道会被问什么。我想先随便打一场，看看自己一开口是不是又在背概念。」<br>
    <span class="small">→ 打开就点开始，不问岗位以外的任何信息；8 个话题打完，复盘告诉我「你有 4 次回答没给数字」。</span>
  </div>
  <div class="quote" style="margin-top:2.5mm">
    <b>故事二（正式，练简历）</b><br>
    「我简历上写了日活 400，我怕被追问。我想让面试官就盯着我简历问。」<br>
    <span class="small">→ 上传简历 → 开场就问「这个 400 是怎么统计的」；我含糊过去，它追到「哪个后台、打开算还是登录算」。</span>
  </div>
""", 2, "问题与定位"))

    # ---------------- 03 需求结构
    pages.append(page("""
  <div class="kicker">02 结构</div>
  <h2>四个模块，19 条功能需求，<br>每条都带验收标准</h2>
  <div class="rule"></div>
  <p class="lead" style="margin-bottom:5mm">优先级只有两档：<span class="tag p0">P0</span> 没有它产品不成立；<span class="tag p1">P1</span> 影响体验但可延后。下面列的是 P0。</p>

  <div class="cards">
    <div class="card">
      <h3>1 · 面试对练</h3>
      <ul>
        <li>开局四项配置：岗位 / 话题数 / 难度 / 面试方式</li>
        <li>判定四种动作：澄清 / 追问 / 换题 / 推进</li>
        <li>每题动作上限各 2 次，到上限强制推进</li>
        <li>语音按句播报，播报中可被打断，打断即停</li>
        <li>识别断线退避重连（4/7/12/20/30 秒），不结束本场</li>
      </ul>
    </div>
    <div class="card">
      <h3>2 · 复盘</h3>
      <ul>
        <li>总分 0–100 + 20 字内总评</li>
        <li>3 条优势必须引用候选人原话</li>
        <li>3 条改进，每条含问题 + 可执行改法</li>
        <li>逐题四件套：点评 / 思路 / 该备的数 / 60–90 字参考答法</li>
        <li>模型返回不完整就说"这次没有范本"，不编造</li>
      </ul>
    </div>
    <div class="card">
      <h3>3 · 数据与统计</h3>
      <ul>
        <li>本次指标：追问 / 澄清 / 换题 / 打断次数</li>
        <li>首字延迟、首音延迟</li>
        <li>成本逐轮累加接口返回的 token，分时段计价</li>
        <li>接口没给用量时退回估算并标注，不冒充实测</li>
        <li>往期场次可回看复盘</li>
      </ul>
    </div>
    <div class="card" style="background:#FAFBFC">
      <h3>4 · 明确不做</h3>
      <ul>
        <li>不做题库：岗位题由模型生成</li>
        <li>不做账号体系：本地单机</li>
        <li>不做视频面试：语音已经够用</li>
        <li>不做简历优化建议</li>
        <li>不做作品集排版评审：只拿它出题，不评价好不好看</li>
      </ul>
    </div>
  </div>

  <h3 style="margin-top:6mm">几条能看出「边界意识」的验收标准</h3>
  <table>
    <tr><th style="width:16%">编号</th><th style="width:34%">需求</th><th>验收标准（节选）</th></tr>
    <tr><td class="num">F05</td><td>面试方式</td><td>只给作品集不给简历 → 仍然拦下。正式场要的是「人」，不是产物</td></tr>
    <tr><td class="num">F06</td><td>简历导入</td><td>扫描件 / 图片型 PDF 读不出字 → 明确提示另存或粘贴，<b>不静默降级</b>；正文不落盘，只记文件名与字数</td></tr>
    <tr><td class="num">F11</td><td>动作上限</td><td>连续 4 次「不知道」后必须推进到下一话题，不得卡死</td></tr>
    <tr><td class="num">F16</td><td>逐题复盘</td><td>模型返回不完整时明确说明「这次没有范本」，<b>不假装有</b></td></tr>
  </table>

  <p class="small" style="margin-top:4mm">为什么验收标准写到这个程度：「不静默降级」「不假装有」这种要求写在需求里，才不会在实现时被省掉。<b>产品经理能守住的东西，往往就是这几句「不许」。</b></p>

  <h3 style="margin-top:4.5mm">从变更记录能看出产品在收敛</h3>
  <table>
    <tr><th style="width:14%">日期</th><th style="width:40%">改了什么</th><th>为什么改</th></tr>
    <tr><td class="num">11-01</td><td>「打断恢复」改为「打断即停」</td><td>实测打断后补念反而让人以为它没听懂，且补偿性复述会污染下一轮上下文</td></tr>
    <tr><td class="num">11-08</td><td>作品集提示词从「按总字数截断」改为「按份分配」</td><td>作品集一份动辄几千字，按总字数截断等于只读了第一份</td></tr>
  </table>
""", 3, "需求结构"))

    # ---------------- 04 核心机制
    pages.append(page("""
  <div class="kicker">03 核心机制</div>
  <h2>一条有上限的判定树：<br>宁可问得浅，不能把人钉死在一道题上</h2>
  <div class="rule"></div>
  <p class="lead" style="margin-bottom:4mm">候选人说完一段话，系统按<b>固定顺序</b>判定该做什么。顺序不能乱——它是产品行为本身。</p>

  <table>
    <tr><th style="width:8%">顺序</th><th style="width:52%">条件</th><th style="width:14%">动作</th><th>为什么排在这</th></tr>
    <tr><td class="num">1</td><td>识别为空，或实字数 = 0</td><td><span class="tag mid">澄清</span></td><td>没听见 ≠ 答得差</td></tr>
    <tr><td class="num">2</td><td>命中放弃词，且占全文 1/4 以上</td><td><span class="tag bad">换题</span></td><td>"不知道"只有 3 个字，但语义明确</td></tr>
    <tr><td class="num">3</td><td>实字数 &lt; 4</td><td><span class="tag mid">澄清</span></td><td>语气词当没说清处理</td></tr>
    <tr><td class="num">4</td><td>本题追问已达 2 次</td><td><span class="tag ok">推进</span></td><td>到上限必须走</td></tr>
    <tr><td class="num">5</td><td>实字数 &lt; 22（困难档 34）</td><td><span class="tag p0">追问</span></td><td>偏短 = 没展开</td></tr>
    <tr><td class="num">6</td><td>没有数字，也没有结果</td><td><span class="tag p0">追问</span></td><td>面试最常见的失分点</td></tr>
    <tr><td class="num">7</td><td>其他</td><td><span class="tag ok">推进</span></td><td>够具体，换话题</td></tr>
  </table>

  <div class="cards" style="margin-top:5mm">
    <div class="card">
      <h3>为什么要设上限</h3>
      <p class="small">每题追问、澄清、换题各最多 2 次，到上限强制推进。<b>最糟的体验不是问得难，是卡在同一题上出不来。</b>没有上限的追问，在实现上就等价于死循环。</p>
    </div>
    <div class="card">
      <h3>难度只改三件事</h3>
      <p class="small">短句阈值（16 / 22 / 34 字）、是否强制要「数字 + 结果」、提问深度提示。<b>判定顺序不变</b>——难度不该改变产品逻辑，只该改变严格程度。</p>
    </div>
  </div>

  <h3 style="margin-top:5mm">一次真实的翻车：判断词义要看权重，不能只看有没有出现</h3>
  <div class="quote">
    早期版本里，用户说 200 多字、中间顺嘴一句「不知道从何下手」，系统直接换题了——把一段有内容的回答当成放弃。<br>
    修法不是把「不知道」从放弃词里删掉，而是加一个约束：<b>放弃词要占全文 1/4 以上才算放弃</b>。同一个词，在「不知道」和「这个细节不知道从何下手，但整体思路是……」里不是一个意思。
  </div>

  <h3 style="margin-top:4mm">一条交互铁律</h3>
  <p style="margin-bottom:4mm"><b>任何自动行为都必须可被用户打断。</b>播报可打断、生成可打断、冷场计时会因为打字而重置，打断之后本轮作废、只出字幕不出声。产品是陪练，不是播放器。</p>

  <p class="small" style="margin:3mm 0 2mm"><b>四条异常，四条不同的处理</b>——同样是「出错」，处理方式不该一样：</p>
  <table>
    <tr><th style="width:20%">异常</th><th>处理</th></tr>
    <tr><td><b>无网络</b>（识别）</td><td>保留语音，退避重连 4/7/12/20/30 秒，不降级——断线是常态，不该判死刑</td></tr>
    <tr><td><b>无麦克风权限</b></td><td>降级为打字，<b>播报继续</b>——识别不可用 ≠ 不能听它说话</td></tr>
    <tr><td><b>引擎假活</b></td><td>心跳每 12 秒换新识别器，点一下麦克风立即重连</td></tr>
  </table>
""", 4, "核心机制"))

    # ---------------- 05 界面
    pages.append(page(f"""
  <div class="kicker">04 界面</div>
  <h2>三个页面，一条闭环</h2>
  <div class="rule"></div>

  <div class="shot"><img src="{p1}" alt="01 首页"></div>
  <div class="cap"><b>01 首页</b><span>岗位、话题数、难度、面试方式四项，选完即时反馈；只有「正式」模式才展开简历区，且没给简历会被前后端各拦一次。</span></div>

  <div style="display:grid; grid-template-columns:1fr 1fr; gap:5mm; margin-top:5mm">
    <div>
      <div class="shot"><img src="{p2}" alt="02 对练中"></div>
      <div class="cap"><b>02 对练中</b><span>一次只问一个问题，单句上限 60 字。HUD 显示话题进度、追问次数与本场花费。</span></div>
    </div>
    <div>
      <div class="shot"><img src="{p3}" alt="03 复盘"></div>
      <div class="cap"><b>03 复盘</b><span>总分 + 3 条优势 + 3 条改进 + 逐题四件套，可下载 .md 并归档。</span></div>
    </div>
  </div>

  <p class="small" style="margin-top:4mm">界面尺寸与配色取自代码 <code style="font-family:Consolas,monospace">web/style.css</code>（304px 侧栏、14px 基准字号、13px 气泡圆角），设计稿与上线产品是同一套数值体系。</p>

  <h3 style="margin-top:5mm">界面上的三个「少」</h3>
  <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:4mm">
    <div class="card">
      <h3 style="font-size:8.7pt">一次只问一个</h3>
      <p class="small">单句上限 60 字，超长强制拆句。真人面试官不会一口气抛三个问题；一次问三个，候选人只会挑最好答的那个答。</p>
    </div>
    <div class="card">
      <h3 style="font-size:8.7pt">HUD 只放三个数</h3>
      <p class="small">话题进度、追问次数、本场花费。练的时候盯着数据看，就练不好表达——数据是复盘时用的，不是练的时候用的。</p>
    </div>
    <div class="card">
      <h3 style="font-size:8.7pt">优势必须引用原话</h3>
      <p class="small">不引用原话的夸奖等于空话：用户没法验证，也就没法复制。同理，3 条改进每条都要给可执行的改法。</p>
    </div>
  </div>
""", 5, "界面"))

    # ---------------- 06 设计系统
    pages.append(page("""
  <div class="kicker">05 设计系统</div>
  <h2>4 组组件、12 个颜色样式、8 个文字样式</h2>
  <div class="rule"></div>
  <p class="lead" style="margin-bottom:4mm">切组件的依据不是「长得像就合成一个」，而是<b>「会不会一起变」</b>。</p>

  <table>
    <tr><th style="width:16%">组件</th><th style="width:10%">变体</th><th style="width:32%">属性</th><th>切分依据</th></tr>
    <tr><td><b>Button</b></td><td>4</td><td><code>Type</code> × <code>State</code></td>
        <td>primary / ghost 是两种按钮；default / hover / disabled 是同一种按钮的三个状态</td></tr>
    <tr><td><b>Seg</b></td><td>2</td><td><code>State</code></td><td>单选控件的两个状态：off / on</td></tr>
    <tr><td><b>Message</b></td><td>2</td><td><code>Who</code></td><td>agent / me 两侧结构不同（我方无标签行）</td></tr>
    <tr><td><b>Composer</b></td><td>1</td><td>—</td><td>输入条整体，内部元素不单独成组件</td></tr>
  </table>

  <div class="cards" style="margin-top:5mm">
    <div class="card">
      <h3>Button 为什么不是四个平铺变体</h3>
      <p class="small">平铺更快，但会丢信息——「状态」这个概念消失了。拆成两个维度后，规则变成可验证的：<b>同一 Type 下的三个 State，尺寸和内边距必须完全一致，只允许颜色不同</b>。不一致的话，切换状态时按钮会跳。</p>
    </div>
    <div class="card">
      <h3>Message 只做 2 个变体，是取舍</h3>
      <p class="small">完整形态 6 种（提问/追问/澄清/换题/收尾/我方）。这 6 种差异只有小胶囊颜色和文案，复用价值低于维护成本，静态形态在 02 屏里都有画。<b>这是主动收敛，不是漏做</b>——写进交付说明的「待完善」里。</p>
    </div>
  </div>

  <h3 style="margin-top:5.5mm">设计变量与代码同源</h3>
  <table>
    <tr><th style="width:24%">设计稿样式名</th><th style="width:26%">CSS 变量</th><th style="width:16%">值</th><th>用在哪</th></tr>
    <tr><td><code>accent</code></td><td><code>--accent</code></td><td><code>#2B55D6</code></td><td>主按钮、强调文字、选中态</td></tr>
    <tr><td><code>accent-soft</code></td><td><code>--accent-soft</code></td><td><code>#EEF2FF</code></td><td>选中底、标签胶囊底</td></tr>
    <tr><td><code>ink</code> / <code>ink2</code> / <code>ink3</code></td><td><code>--ink</code> 等</td><td><code>#12141A</code> …</td><td>正文 / 次要 / 提示三级文字</td></tr>
    <tr><td><code>line</code> / <code>line-2</code></td><td><code>--line</code> 等</td><td><code>#E6E8EC</code></td><td>卡片描边、分隔线</td></tr>
  </table>
  <p class="small" style="margin-top:3mm">代码的 <code style="font-family:Consolas,monospace">:root</code> 里共 18 个颜色变量；代码里实际出现 18 种字号（9–24px），设计稿收敛成 8 个文字样式。<b>设计稿相对代码唯一「变少」的地方就是字号</b>——代码是长出来的，稿子得能讲清层级。</p>

  <h3 style="margin-top:5mm">验收：90 秒能不能看完</h3>
  <table>
    <tr><td class="num">1</td><td>打开设计文件 → 停在封面页，上面写了产品是什么、文件里有什么、怎么看</td></tr>
    <tr><td class="num">2</td><td>切到「页面」→ 三屏并排，1440 × 900</td></tr>
    <tr><td class="num">3</td><td>右上角 ▶ →「开始对练」→「结束」→「再练一场」，回到首页</td></tr>
    <tr><td class="num">4</td><td>切到「组件」→ 四组组件，选中后右侧面板能看到 Type / State / Who 这些属性</td></tr>
  </table>

  <div class="quote" style="margin-top:5mm">
    <b>这份稿子的真相：</b>它是产品做完之后反推的，不是先设计后开发。它能证明「把已上线界面抽象成设计系统」的能力，不能证明「从零做设计推演」——这一点写进了交付说明的「已知不足」，不藏着。
  </div>
""", 6, "设计系统"))

    # ---------------- 07 验证
    pages.append(page("""
  <div class="kicker">06 验证</div>
  <h2>10 条用例全过，<br>但结论不是「没问题」</h2>
  <div class="rule"></div>
  <p class="lead" style="margin-bottom:4mm">执行方式是脚本调用 HTTP 接口、逐事件解析返回，不是点页面点出来的。每条用例的请求与真实返回都存在 <code style="font-family:Consolas,monospace">证据/验收-20261108.json</code> 里，可逐条复核。</p>

  <table>
    <tr><th style="width:9%">编号</th><th style="width:26%">场景</th><th style="width:30%">实际结果</th><th style="width:9%">结论</th></tr>
    <tr><td class="num">TC02</td><td>回答偏短（17 字）</td><td>动作 = 追问；"那十几个人你是怎么选的，访谈完得出了什么结论？"</td><td><span class="tag ok">通过</span></td></tr>
    <tr><td class="num">TC03</td><td>只有语气词「嗯」</td><td>动作 = 澄清；"声音有点断，刚才那句能再说一次吗？"</td><td><span class="tag ok">通过</span></td></tr>
    <tr><td class="num">TC05</td><td>连续 4 次「不知道」</td><td>换题 → 换题 → 推进 → 换题，进入第 2 题，没卡死</td><td><span class="tag ok">通过</span></td></tr>
    <tr><td class="num">TC07</td><td>正式模式缺简历</td><td>HTTP 400，提示"正式面试要读简历……只想随手练就切回体验式"</td><td><span class="tag ok">通过</span></td></tr>
    <tr><td class="num">TC09</td><td>走完 4 话题 + 复盘</td><td>开场引用简历里的"日活 400"；复盘 58 分，4 条逐题全部带范本</td><td><span class="tag ok">通过</span></td></tr>
  </table>
  <p class="small" style="margin-top:2mm">另有 TC01 / 04 / 06 / 08 / 10 五条，覆盖体验式开局、555 字超长输入、重复提交、docx 解析、打断计数，全部通过。</p>

  <h3 style="margin-top:5mm">三条缺陷（全过只说明设计好的路径通了）</h3>
  <table>
    <tr><th style="width:11%">编号</th><th style="width:44%">问题</th><th style="width:10%">严重度</th><th>状态</th></tr>
    <tr><td class="num">BUG-01</td><td>题号显示慢一拍：强制推进时 HUD 仍是旧题号，下一轮才更新</td><td><span class="tag mid">轻微</span></td><td>待修（1 行）</td></tr>
    <tr><td class="num">BUG-02</td><td><b>重复提交会白吃一个话题</b>：服务端无幂等键，第二次被当成新回答，直接推进并多花一次调用</td><td><span class="tag bad">一般</span></td><td>遗留，接入多人前必修</td></tr>
    <tr><td class="num">BUG-03</td><td>识别断连期间说的话无留存，重连后要重说（浏览器不提供音频流，属能力边界）</td><td><span class="tag bad">一般</span></td><td>不改，靠提示语缓解</td></tr>
  </table>

  <div class="cards" style="margin-top:5mm">
    <div class="card">
      <h3>能不能上线</h3>
      <p class="small"><b>可以，作为本地单人练习工具。</b>主流程（开局 → 多轮问答 → 自动收尾 → 复盘带范本）闭环完整，缺简历、重复提交、连续放弃三种异常都不会把用户卡死。<br><b>不建议多人共用</b>：没有账号体系和会话隔离，接口无幂等键，多人用会互相污染数据。</p>
    </div>
    <div class="card">
      <h3>一句实话</h3>
      <p class="small">这 10 条用例是我自己设计的，覆盖的是<b>我知道会出问题的地方</b>（调试时踩过的坑）。它能证明"这些路径是通的"，不能证明"产品好用"。好不好用要看 10 场真人真麦的对练，跑完才有资格说。</p>
    </div>
  </div>

  <p class="small" style="margin-top:3.5mm"><b>用例是怎么设计的：</b>从六个维度各取至少一条——开局配置、判定逻辑、简历解析、复盘产出、异常输入、统计口径；<b>优先测调试时踩过的坑，不测「显然能过」的路径</b>。TC01 的期望值最初写成"首句以问号结尾"被判失败，后来发现是用例写错了、不是产品错了——这一条故意留在报告里，因为验收标准本身也要能被验收。</p>
""", 7, "验证"))

    # ---------------- 08 数据与边界
    pages.append(page("""
  <div class="kicker">07 数据</div>
  <h2>现在有什么数据，<br>没有什么数据</h2>
  <div class="rule"></div>

  <div style="display:grid; grid-template-columns:1fr 1fr; gap:6mm">
    <div>
      <h3>已有的实测（可复核）</h3>
      <table>
        <tr><td class="num">6</td><td>场真实对练，36 轮计时</td></tr>
        <tr><td class="num">734<span style="font-size:6.5pt">ms</span></td><td>首字延迟 P50（说完 → 首字出现）</td></tr>
        <tr><td class="num">1035<span style="font-size:6.5pt">ms</span></td><td>首音延迟 P50（说完 → 开口出声）</td></tr>
        <tr><td class="num">¥0.0553</td><td>6 场累计成本；单场 8 话题最低 ¥0.0136</td></tr>
        <tr><td class="num">4</td><td>次打断记录，打断后不重复率 1.0</td></tr>
        <tr><td class="num">1</td><td>场走满 8/8 话题（766 秒，自评 58 分）</td></tr>
      </table>
      <p class="small" style="margin-top:2.5mm">成本按接口返回的 usage 逐轮累加，不是按价目表估算。延迟为全部样本合并后的 P50。</p>
    </div>
    <div>
      <h3>还没有的</h3>
      <table>
        <tr><td style="width:100%"><b>10 场评测未完成（1 / 10）</b><br><span class="small">1 场走满 8 话题，且岗位不是目标岗。目标：10 场 AI 产品经理完整对练。</span></td></tr>
        <tr><td><b>评分是模型自评，非盲评</b><br><span class="small">只能当参考，不能当客观指标。</span></td></tr>
        <tr><td><b>样本量为 1</b><br><span class="small">只有作者一个用户，不主张泛化结论。</span></td></tr>
        <tr><td><b>外放回声未测</b><br><span class="small">所有场次都戴耳机。不戴耳机时会不会自己打断自己，还没有数据。</span></td></tr>
      </table>
    </div>
  </div>

  <p style="margin-top:4.5mm; margin-bottom:1.8mm"><b>一个被推翻的估算</b></p>
  <div class="quote">
    PRD 里写的目标是「单场 ≤ ¥0.05」，那是按官方价目算的。实测 8 个话题的一场是 <b>¥0.0136</b>——低了一个量级，原因是上下文缓存命中了 3712 个 token。<br>
    <b>估算和实测差 3 倍的时候，该改的是文档，不是把实测数据往估算上凑。</b>
  </div>

  <h3 style="margin-top:4mm">为什么能这么便宜：成本结构（那一场实测）</h3>
  <table>
    <tr><th style="width:34%">项</th><th style="width:22%">用量</th><th>说明</th></tr>
    <tr><td>输入 token</td><td>16,374（命中缓存 3,712）</td><td>简历 + 话题 + 最近 30 轮上下文，轮与轮之间大部分不变</td></tr>
    <tr><td>输出 token</td><td><b>213</b></td><td>面试官一次只说一句话（单句上限 60 字）——<b>成本低不是省出来的，是产品形态决定的</b></td></tr>
    <tr><td>合计</td><td>¥0.0136</td><td>8 个话题、9 轮回答。输出只有 213 token 是关键——做成「一次给三段建议」的形态，输出会翻十倍，成本优势就不成立</td></tr>
  </table>

  <h3 style="margin-top:4.5mm">下一步（按优先级）</h3>
  <table>
    <tr><td class="num">1</td><td><b>跑完 10 场真人真麦对练</b>——把完成率、追问有效性从「目标值」变成「实测值」，这是目前唯一缺的证据</td></tr>
    <tr><td class="num">2</td><td>修 BUG-01；加请求 ID 修 BUG-02（重复提交会白吃话题，多人使用前必须解决）</td></tr>
    <tr><td class="num">3</td><td>复盘把 3 条改进收敛成 1 条（现在给 3 条，用户反而不知道先改哪个）；补外放场景测试——不戴耳机时的回声自激，是目前唯一会毁掉整场体验的问题</td></tr>
  </table>

  <div style="margin-top:4mm; border-top:1px solid #E6E8EC; padding-top:3mm">
    <p class="small">代码 <b>github.com/zyyomg/interview-coach-agent</b>（公开）　｜　设计稿 <b>figma.com/proto/MeGvma1k5lRYE9b3mfJf5c</b>　｜　数字均可在 <code style="font-family:Consolas,monospace">runs/</code> 与 <code style="font-family:Consolas,monospace">证据/</code> 复核</p>
  </div>
""", 8, "数据与边界"))

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>面试陪练 Agent · 作品集</title>
<meta name="color-scheme" content="light">
<style>{CSS}</style></head>
<body>
{''.join(pages)}
</body></html>"""

    OUT.write_text(html, encoding="utf-8")
    print(f"\n写入 {OUT}  ({OUT.stat().st_size // 1024} KB)")
    return str(OUT)


if __name__ == "__main__":
    import sys
    build()
    if "--pdf" in sys.argv:
        from playwright.sync_api import sync_playwright
        pdf = OUT.with_suffix(".pdf")
        with sync_playwright() as pw:
            b = pw.chromium.launch(channel="msedge")
            pg = b.new_page(viewport={"width": 794, "height": 1123})
            pg.goto(OUT.resolve().as_uri())
            pg.wait_for_timeout(1200)
            pg.pdf(path=str(pdf), format="A4", print_background=True,
                   prefer_css_page_size=True,
                   margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            b.close()
        print(f"PDF  {pdf}  ({pdf.stat().st_size // 1024} KB)")
