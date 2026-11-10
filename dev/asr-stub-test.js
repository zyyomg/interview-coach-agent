#!/usr/bin/env node
/**
 * dev/asr-stub-test.js —— 语音状态机的桩测试（可选，需要 node，不参与运行时）
 *
 *     node dev/asr-stub-test.js
 *
 * 为什么要有它：语音链路是历史上 bug 最密的地方（9 个 bug 里 6 个在这儿），
 * 而且以前只能真开麦试。这里用假的 SpeechRecognition / speechSynthesis /
 * fetch 把状态机跑一遍，不需要麦克风、不需要起服务，就能验证三件事：
 *
 *   1. 点开始之后麦克风确实开了
 *   2. 识别引擎「假活」（状态是 listening，但事件一个都不来）时，心跳能把它救回来
 *   3. 说完一轮提交之后，麦克风能自己接上（不接上就是"它问完我就不能说话了"）
 *   4. 开场那一轮的首字耗时从「点开始」算起，而不是从「页面加载」算起
 *
 * 第 2、4 条是 2026-11-01 用户实测暴露出来的：一场对练在第 15 轮之后语音不出字，
 * 服务端记录里那场的第一轮首字是 23005ms——因为计时基线没在开局重置。
 */

const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, '..', 'web', 'app.js');

// app.js 里的 [nudge]/[asr] 诊断日志默认静音，想看就 VERBOSE=1
if (!process.env.VERBOSE) {
  const info = console.info;
  console.info = () => {};
  console.warn = () => {};
  void info;
}

/* ---------------------------------------------------------------- 假时钟 */
let now = 0;
const timers = [];
const intervals = [];
global.performance = { now: () => now };
global.requestAnimationFrame = (fn) => { queueMicrotask(fn); return 0; };
let timerSeq = 0;
global.setTimeout = (fn, ms) => { const id = ++timerSeq; timers.push({ id, at: now + (ms || 0), fn }); return id; };
// 以前这里是空函数，清掉的计时器照样会响——于是"该清的没清"在假时钟下测不出来，
// 反过来"明明挂上了却没有"也测不出来。要跟浏览器一样真的能取消。
global.clearTimeout = (id) => {
  const i = timers.findIndex((t) => t.id === id);
  if (i >= 0) timers.splice(i, 1);
};
global.setInterval = (fn, ms) => { intervals.push({ every: ms || 1, fn, next: now + (ms || 1) }); return intervals.length - 1; };

function advance(ms) {
  const target = now + ms;
  for (let guard = 0; guard < 5000; guard++) {
    let bestAt = Infinity;
    let bestKind = null;
    let bestIdx = -1;
    timers.forEach((t, i) => { if (t.at <= target && t.at < bestAt) { bestAt = t.at; bestKind = 't'; bestIdx = i; } });
    intervals.forEach((iv, i) => { if (iv.next <= target && iv.next < bestAt) { bestAt = iv.next; bestKind = 'i'; bestIdx = i; } });
    if (bestKind === null) break;
    now = bestAt;
    if (bestKind === 't') timers.splice(bestIdx, 1)[0].fn();
    else { const iv = intervals[bestIdx]; iv.next += iv.every; iv.fn(); }
  }
  now = target;
}

const flush = () => new Promise((r) => setImmediate(r));

/* ---------------------------------------------------------------- 假 DOM */
function el(tag) {
  const o = {
    tag,
    _h: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: { setProperty() {} },
    dataset: {},
    appendChild(c) { this._kids.push(c); return c; }, removeChild() {}, remove() {},
    addEventListener(type, fn) { this._h[type] = fn; },
    removeEventListener() {},
    querySelector: () => el('div'),
    querySelectorAll: () => [],
    focus() {}, blur() {},
    click() { if (this._h.click) this._h.click(); },
    setAttribute() {}, getAttribute: () => '',
    scrollHeight: 0, scrollTop: 0, clientHeight: 0, value: '',
    textContent: '', disabled: false, hidden: false,
    _kids: [],
    get parentElement() { if (!this._p) this._p = el('div'); return this._p; },
  };
  // innerHTML = '' 是"清空重画"，假 DOM 也得真的清空，否则测不出到底渲染了几条
  Object.defineProperty(o, 'innerHTML', {
    get() { return o._html || ''; },
    set(v) { o._html = v; o._kids.length = 0; },
  });
  return o;
}

const els = new Map();
global.document = {
  getElementById(id) { if (!els.has(id)) els.set(id, el(id)); return els.get(id); },
  querySelector: () => el('div'),
  querySelectorAll: () => [],
  addEventListener() {},
  createElement: (t) => el(t),
  activeElement: null,
  body: el('body'),
  hidden: false,
};
global.window = global;
global.addEventListener = () => {};
global.removeEventListener = () => {};
global.navigator = { userAgent: 'node' };
global.confirm = () => true;
global.alert = () => {};
global.localStorage = { getItem: () => null, setItem() {} };

/* ------------------------------------------------------- 假识别 / 假播报 */
const sr = { instances: [], startCalls: 0, stopCalls: 0, abortCalls: 0, started: false };
class FakeSR {
  constructor() {
    this.lang = ''; this.continuous = false; this.interimResults = false; this.maxAlternatives = 1;
    sr.instances.push(this);
  }
  start() { sr.startCalls++; sr.started = true; if (this.onstart) this.onstart(); }
  stop() { sr.stopCalls++; sr.started = false; if (this.onend) this.onend(); }
  abort() { sr.abortCalls++; sr.started = false; if (this.onend) this.onend(); }
}
global.SpeechRecognition = FakeSR;
global.webkitSpeechRecognition = FakeSR;

global.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; } };
global.speechSynthesis = {
  paused: false, speaking: false, pending: false,
  speak(u) { queueMicrotask(() => { if (u.onstart) u.onstart(); if (u.onend) u.onend(); }); },
  cancel() {}, resume() {},
};

/* ---------------------------------------------------------------- 假接口 */
function ndjson(events) {
  const buf = Buffer.from(events.map((e) => JSON.stringify(e) + '\n').join(''), 'utf8');
  let sent = false;
  return {
    ok: true,
    body: { getReader: () => ({ read: async () => (sent ? { done: true } : ((sent = true), { done: false, value: buf })) }) },
    json: async () => ({}),
  };
}

const START = [
  { t: 'session', id: 'sess1', mode: 'mock', max_question: 8, role: 'ai_pm', role_name: 'AI 产品经理（实习）' },
  { t: 'meta', turn: 1, action: 'ask', label: '提问', reason: '开场', question: 1, max_question: 8 },
  { t: 'tok', d: '先做个自我介绍？' },
  { t: 'sent', d: '先做个自我介绍？' },
  { t: 'done', turn: 1, question: 1, finished: false,
    state: { question: 1, max_question: 8, turn: 1, answered: 0, cost: 0, tokens: { in: 0, out: 0 }, finished: false, mode: 'mock' } },
];
const TURN = [
  { t: 'meta', turn: 2, action: 'probe', label: '追问', reason: '偏短', question: 1, max_question: 8 },
  { t: 'tok', d: '具体怎么做的？' },
  { t: 'sent', d: '具体怎么做的？' },
  { t: 'done', turn: 2, question: 1, finished: false,
    state: { question: 1, max_question: 8, turn: 2, answered: 1, cost: 0, tokens: { in: 10, out: 5 }, finished: false, mode: 'mock' } },
];

const timingPosts = [];
let silencePosts = 0;
let turnPosts = 0;
global.fetch = async (url, opts) => {
  const u = String(url);
  // health 里带一个假阈值，用来验「配置有没有写错字段」
  if (u.includes('/api/health')) return { ok: true, json: async () => ({ ok: true, mock: true, model: 'x', silence_seconds: 1, turn_silence_seconds: 4.5 }) };
  if (u.includes('/api/history')) return { ok: true, json: async () => ({ ok: true, items: [] }) };
  if (u.includes('/api/timing')) { try { timingPosts.push(JSON.parse(opts.body)); } catch (e) { /* ignore */ } return ndjson([{ ok: true }]); }
  if (u.includes('/api/silence')) { silencePosts += 1; return ndjson([]); }
  if (u.includes('/api/session/start')) return ndjson(START);
  if (u.includes('/api/turn')) { turnPosts += 1; return ndjson(TURN); }
  return ndjson([{ ok: true }]);
};

/* ---------------------------------------------------------------- 跑 */
let bad = 0;
const check = (ok, msg) => {
  console.log(`${ok ? 'OK  ' : 'FAIL'} ${msg}`);
  if (!ok) bad += 1;
};

(async () => {
  const src = fs.readFileSync(process.env.APP_JS || APP, 'utf8');
  // 只在内存里把内部状态挂出来，不改文件；app.js 是 IIFE，否则外面看不见
  eval(src
    .replace('const S = {', 'const S = global.__S = {')
    .replace(/\}\)\(\);\s*$/, 'global.__ui = { renderSamples, renderReport, resetSilence };\n})();'));   // eslint-disable-line no-eval

  const S = global.__S;
  const resetSilence = global.__ui.resetSilence;
  check(sr.instances.length === 1, `装载后只建了一个识别实例（${sr.instances.length}）`);

  // 冷场阈值要从 health 里读进 nudgeMs，而且不能把「说完判定」的 silenceMs 覆盖掉
  // （曾经写错过：silenceMs 被改成 10 秒，于是每句话都要等 10 秒才提交）
  await flush();
  check(S.nudgeMs === 1000 && S.silenceMs === 4500,
    `冷场阈值来自 health（nudgeMs=${S.nudgeMs}）、说完判定没被覆盖（silenceMs=${S.silenceMs}）`);
  S.nudgeMs = 15000;        // 后面的用例按 15 秒跑，避免 1 秒就催
  S.silenceMs = 4500;

  // 模拟「页面开了 23 秒才点开始」：旧代码会把这一刻当 0 点，录出 23000ms
  now = 23000;
  els.get('startBtn').click();
  await flush();
  await flush();
  advance(50);

  check(sr.startCalls >= 1 && sr.started, `开局就开麦（start ${sr.startCalls} 次）`);
  const t0 = timingPosts[0];
  check(!!t0 && typeof t0.first_token_ms === 'number' && t0.first_token_ms < 1000,
    `开场那一轮的首字耗时从「点开始」算起（上报 ${t0 && t0.first_token_ms} ms）`);

  // ① 假活：状态是 listening，但事件一个都不来
  const rec = sr.instances[0];
  if (rec.onstart) rec.onstart();
  const beforeAbort = sr.abortCalls;
  const beforeStart = sr.startCalls;
  advance(14000);
  await flush();
  check(sr.abortCalls > beforeAbort, `14 秒没事件 → 心跳 abort 重开（${sr.abortCalls - beforeAbort} 次）`);
  check(sr.startCalls > beforeStart, `重开之后又 start 了（${sr.startCalls - beforeStart} 次）`);

  // ② 说完一轮提交后，麦克风要自己接上
  if (rec.onstart) rec.onstart();
  const beforeTurn = sr.startCalls;
  rec.onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: '我是产品经理，做过一个工具' } }] });
  advance(5200);              // 说完判定：15 字 ≈ 4.8 秒，留点余量
  await flush();
  advance(400);
  check(sr.startCalls > beforeTurn, `一轮说完了，麦克风自动接上（start ${sr.startCalls - beforeTurn} 次）`);

  // ④ 冷场：轮到你说，却一直没动静。面试官要主动接一句。
  //    注意这期间心跳会重开麦克风，不能因此把冷场计时搅黄（两者用不同时间戳）。
  advance(5000);
  const beforeNudge = silencePosts;
  advance(17000);
  await flush();
  check(silencePosts > beforeNudge, `冷场 20 秒 → 主动催场（调了 ${silencePosts - beforeNudge} 次）`);

  // ⑤ 点麦克风 = 只关麦，不发空轮
  //    原来「关麦」会走 commitSpeech，识别为空但起过音 → 发一个空轮
  //    → 后端判成 CLARIFY → 面试官说「我没听清，再说一遍」，用户只是想去打字
  if (rec.onstart) rec.onstart();
  const beforeMicTurn = turnPosts;
  els.get('micBtn').click();
  await flush();
  check(turnPosts === beforeMicTurn, `点麦克风只关麦、不发空轮（turn 调用 ${turnPosts - beforeMicTurn} 次）`);

  // ⑥ 暂停：整场冻住，不催场、不提交；再按一下接着来
  if (rec.onstart) rec.onstart();
  els.get('pauseBtn').click();
  await flush();
  const pausedSilence = silencePosts;
  const pausedTurns = turnPosts;
  advance(25000);
  await flush();
  check(S.paused === true, '点「暂停」进入暂停态');
  check(silencePosts === pausedSilence && turnPosts === pausedTurns,
    `暂停期间不催场、不提交（${silencePosts - pausedSilence} / ${turnPosts - pausedTurns}）`);
  els.get('pauseBtn').click();
  await flush();
  advance(50);
  check(S.paused === false && sr.started, '点「继续」恢复识别');

  // ⑦ 光标停在输入框里但没在敲字 → 仍然要催（实测踩过：点过一次框就再也不催了）
  const inputEl = els.get('textInput');
  if (inputEl._h.focus) inputEl._h.focus();
  S.lastKeyAt = performance.now() - 10000;
  const beforeIdleTyping = silencePosts;
  advance(20000);
  await flush();
  check(silencePosts > beforeIdleTyping, `光标在框里但没打字 → 照样催场（${silencePosts - beforeIdleTyping} 次）`);

  // ⑧ 刚敲过键盘 → 不催，而且过了 6 秒还得能恢复（跳过时也要重挂计时）
  S.nudges = 0;          // 按"第一次冷场"的节奏来测
  S.nudgeMs = 3000;
  S.closeMs = 3000;
  S.lastKeyAt = performance.now();
  const beforeTyping = silencePosts;
  advance(4000);
  await flush();
  check(silencePosts === beforeTyping, '刚敲过键盘 → 不催场');
  advance(9000);
  await flush();
  check(silencePosts > beforeTyping, `打字停下 6 秒后仍会催（${silencePosts - beforeTyping} 次）`);

  // ⑨ 复盘渲染：逐题参考答法（有范本 / 演示模式无范本两种都要不报错）
  let sampleOk = true;
  try {
    global.__ui.renderSamples({
      per_round: [{ q: '你做过的最有代表性的项目？', comment: '有细节', sample: '我会说：把评测集从 20 条扩到 200 条，bad case 从 31% 降到 12%' }],
      pairs: [{ q: '你做过的最有代表性的项目？', a: '我做过竞品体检' }],
      mode: 'live',
    });
    global.__ui.renderSamples({ per_round: [{ q: 'x', comment: 'y', sample: '' }], pairs: [], mode: 'mock', generated_by: 'template' });
  } catch (e) {
    sampleOk = false;
    console.error(e);
  }
  check(sampleOk, '复盘渲染：逐题参考答法两种数据都不报错');

  // ⑩ 打字模式（识别不可用 / 手动关了语音）不催场、不收场——
  //    用户手上有键盘，5 秒催一句、10 秒收场太粗暴（实测把人从打字里直接踢下线）
  S.voice = false;
  S.asrDown = true;
  const beforeTypingMode = silencePosts;
  advance(30000);
  await flush();
  check(silencePosts === beforeTypingMode, `打字模式下不催场（${silencePosts - beforeTypingMode} 次）`);
  S.voice = true;
  S.asrDown = false;

  // ⑪ 「暂停 + 识别判死」时点麦克风要能救回来：解除暂停、换新识别器、开麦
  //    实测就卡在这个组合上——暂停时 startRec 直接返回，按什么都没反应
  S.voice = false;
  S.asrDown = true;
  S.paused = true;
  const beforeInstances = sr.instances.length;
  els.get('micBtn').click();
  await flush();
  check(S.paused === false && S.voice === true && S.asrDown === false,
    '暂停 + 识别判死时点麦克风：解除暂停、语音重新打开');
  check(sr.instances.length === beforeInstances + 1 && sr.started,
    `…并且换了新的识别器、真的开麦了（新实例 ${sr.instances.length - beforeInstances} 个）`);

  // ⑫ 识别断线（network）后要自己重连，不能等用户去点开关
  S.asrDown = false;
  S.paused = false;
  S.voice = true;
  S.asrFails = 0;
  const liveRec = sr.instances[sr.instances.length - 1];
  const beforeAuto = sr.instances.length;
  liveRec.onerror({ error: 'network' });          // 模拟在线识别断线
  await flush();
  check(S.asrDown === true, '断线后先标记识别不可用');
  advance(5000);                                   // 第一次退避 4 秒
  await flush();
  check(sr.instances.length > beforeAuto, `断线 4 秒后自动重连（新识别器 ${sr.instances.length - beforeAuto} 个）`);

  // ⑬ 说完判定按「说了多少字」自适应，别再拿固定 4.5 秒切长回答
  //    背景：用户一段长回答说到一半换气 4.5 秒，前半段被发走、识别器重启，
  //    后半段落进新缓冲区，等面试官问完下一题才发出去——两段分属两道题。
  const longRec = sr.instances[sr.instances.length - 1];
  if (longRec.onstart) longRec.onstart();
  const beforeLong = turnPosts;
  longRec.onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: '啊'.repeat(100) } }] });
  advance(5500);
  await flush();
  check(turnPosts === beforeLong, `100 字的回答，停 5.5 秒还不交卷（固定 4.5 秒时这里已经发了）`);
  advance(1500);
  await flush();
  check(turnPosts === beforeLong + 1, `再多等一会才提交（自适应约 6.5 秒）`);

  // ⑭ 复盘按题号归位。模型只点评了第 3 题，那么只有第 3 条能挂上参考答法，
  //    另外两条要明说「未复盘」——按下标配的老写法会把第 3 题的点评贴到第 1 题上，
  //    用户看到的就是「第 2 题挂着第 6 题的点评，后面全是无范本」。
  global.__ui.renderSamples({
    per_round: [{ i: 3, q: '准确率怎么算', comment: '没给分母', sample: '我用 210 个判定点，120 个本来就该留空' }],
    pairs: [
      { q: '问题一', a: '答一' },
      { q: '问题二', a: '答二' },
      { q: '问题三', a: '答三' },
    ],
    mode: 'live',
  });
  const kids = global.document.getElementById('samples')._kids || [];
  const tags = kids.map((it) => {
    const head = (it._kids || [])[0] || {};
    return ((head._kids || [])[1] || {}).textContent;
  });
  check(kids.length === 3, `复盘渲染 3 条（${kids.length}）`);
  check(tags[2] === '看参考答法' && tags[0] === '未复盘' && tags[1] === '未复盘',
    `复盘按题号归位（${tags.join(' / ')}）`);

  // ⑮ 最后一个识别结果正好落在「生成中」时，之后没有任何地方再叫 resumeSilence —
  //    字不能就这么躺在框里。旧代码在 guard 命中时直接 return，计时器再也没挂上，
  //    表现是"麦克风还开着、字也在框里，它就是不发"。
  S.speaking = false;
  S.busy = true;                                   // 假装这一轮还在生成
  S.finalText = '这一段是趁它生成的时候说完的';
  resetSilence();
  advance(20000);
  await flush();
  const beforeStuck = turnPosts;
  check(turnPosts === beforeStuck, '生成期间不交卷，字先留在框里');
  S.busy = false;                                  // 生成结束，但没人再叫 resumeSilence
  advance(2000);
  await flush();
  check(turnPosts === beforeStuck + 1, `生成结束后自动补交（${turnPosts - beforeStuck} 次）`);

  console.log(bad ? `\n${bad} 条不对` : '\n全部通过');
  process.exit(bad ? 1 : 0);
})();
