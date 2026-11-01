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

/* ---------------------------------------------------------------- 假时钟 */
let now = 0;
const timers = [];
const intervals = [];
global.performance = { now: () => now };
global.requestAnimationFrame = (fn) => { queueMicrotask(fn); return 0; };
global.setTimeout = (fn, ms) => { timers.push({ at: now + (ms || 0), fn }); return timers.length; };
global.clearTimeout = () => {};
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
  return {
    tag,
    _h: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: { setProperty() {} },
    dataset: {},
    appendChild() {}, removeChild() {}, remove() {},
    addEventListener(type, fn) { this._h[type] = fn; },
    removeEventListener() {},
    querySelector: () => el('div'),
    querySelectorAll: () => [],
    focus() {}, blur() {},
    click() { if (this._h.click) this._h.click(); },
    setAttribute() {}, getAttribute: () => '',
    scrollHeight: 0, scrollTop: 0, clientHeight: 0, value: '',
    textContent: '', innerHTML: '', disabled: false, hidden: false,
    get parentElement() { if (!this._p) this._p = el('div'); return this._p; },
  };
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
global.fetch = async (url, opts) => {
  const u = String(url);
  if (u.includes('/api/history')) return { ok: true, json: async () => ({ ok: true, items: [] }) };
  if (u.includes('/api/timing')) { try { timingPosts.push(JSON.parse(opts.body)); } catch (e) { /* ignore */ } return ndjson([{ ok: true }]); }
  if (u.includes('/api/silence')) { silencePosts += 1; return ndjson([]); }
  if (u.includes('/api/session/start')) return ndjson(START);
  if (u.includes('/api/turn')) return ndjson(TURN);
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
  eval(src.replace('const S = {', 'const S = global.__S = {'));   // eslint-disable-line no-eval

  const S = global.__S;
  check(sr.instances.length === 1, `装载后只建了一个识别实例（${sr.instances.length}）`);

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
  advance(2200);
  await flush();
  advance(400);
  check(sr.startCalls > beforeTurn, `一轮说完了，麦克风自动接上（start ${sr.startCalls - beforeTurn} 次）`);

  // ③ 冷场：轮到你说，却一直没动静。面试官要主动接一句。
  //    注意这期间心跳会重开麦克风，不能因此把冷场计时搅黄（两者用不同时间戳）。
  advance(5000);
  const beforeNudge = silencePosts;
  advance(17000);
  await flush();
  check(silencePosts > beforeNudge, `冷场 20 秒 → 主动催场（调了 ${silencePosts - beforeNudge} 次）`);

  console.log(bad ? `\n${bad} 条不对` : '\n全部通过');
  process.exit(bad ? 1 : 0);
})();
