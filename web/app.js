/* app.js —— 面试陪练前端：流式对话 · 分句播报 · 打断检测 · 指标 HUD */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const chat = $('chat');
  const input = $('textInput');
  const micBtn = $('micBtn');
  const sendBtn = $('sendBtn');
  const endBtn = $('endBtn');
  const startBtn = $('startBtn');
  const tipbar = $('tipbar');

  const S = {
    session: null,
    role: 'ai_pm',
    rounds: 8,
    voice: true,
    mock: true,
    busy: false,
    finished: false,
    started: false,
    // 语音
    speaking: false,
    listening: false,
    wantListen: false,
    hadSpeech: false,
    tts: true,           // 播报开关，和"能不能识别"分开：识别挂了也不该闭嘴
    asrDown: false,
    finalText: '',
    interimText: '',
    queue: [],
    speechToken: 0,
    utter: null,
    speakReadyAt: 0,     // cancel() 之后要等一会儿再 speak，否则会被静默丢弃
    speakStartAt: 0,
    lastInterruptAt: 0,
    // 本轮
    turn: 0,
    fullText: '',
    sentChars: 0,
    spokenChars: 0,
    interrupted: false,
    bubble: null,
    // 计时
    tSpeechEnd: 0,
    tFirstToken: 0,
    tFirstAudio: 0,
    tDone: 0,
    reportedAudio: false,
    timer: { silence: 0, barge: 0 },
    silenceMs: 2000,     // 说完了停多久算说完。1.2 秒太急，用户喘口气就被当成说完了
    // 统计
    stats: { turns: 0, probe: 0, clarify: 0, switch: 0, interrupts: 0, question: 0, maxQ: 8, cost: 0, inTok: 0, outTok: 0 },
    report: null,
    markdown: '',
    history: [],
  };

  /* ------------------------------------------------------------ 工具 */

  const ms = (v) => (typeof v === 'number' && isFinite(v) ? Math.round(v) + 'ms' : '—');

  async function post(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  async function stream(url, body, onEvent) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line) continue;
        try { onEvent(JSON.parse(line)); } catch (e) { /* ignore */ }
      }
    }
  }

  let stickBottom = true;   // 用户往上翻的时候不要把他拽回底部
  chat.addEventListener('scroll', () => {
    stickBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  });

  function scrollDown(force) {
    if (!force && !stickBottom) return;
    requestAnimationFrame(() => {
      chat.scrollTop = chat.scrollHeight;
      stickBottom = true;
    });
  }

  function setStatus(text, kind) {
    $('statusText').textContent = text;
    const p = $('pulse');
    p.className = 'pulse' + (kind ? ' ' + kind : '');
  }

  function tip(html) { tipbar.innerHTML = html || ''; }

  // 状态文字只在这一处算，避免各回调互相覆盖
  function refreshStatus() {
    if (S.finished) return;
    if (S.speaking) { setStatus('面试官在说 · 你可以直接打断', 'speak'); return; }
    if (S.busy) { setStatus('思考中…', 'think'); return; }
    if (S.session) { setStatus(S.listening ? '在听你说…' : '轮到你说', S.listening ? 'rec' : 'on'); return; }
    setStatus('还没开始', '');
  }

  /* ------------------------------------------------------------ 气泡 */

  function inner() {
    let box = chat.querySelector('.chat-inner');
    if (!box) {
      box = document.createElement('div');
      box.className = 'chat-inner';
      chat.appendChild(box);
    }
    return box;
  }

  const TAG_CLASS = { clarify: 'clarify', probe: 'probe', switch: 'switch', ask: '', close: 'close' };

  function addAgent(label, action, reason) {
    const wrap = document.createElement('div');
    wrap.className = 'msg agent';
    const col = document.createElement('div');
    col.className = 'col';

    const line = document.createElement('div');
    line.className = 'tagline';
    const tag = document.createElement('span');
    tag.className = 'tag ' + (TAG_CLASS[action] || '');
    tag.textContent = label || '提问';
    line.appendChild(tag);
    if (reason) {
      const r = document.createElement('span');
      r.textContent = reason;
      line.appendChild(r);
    }

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    const txt = document.createElement('span');
    const caret = document.createElement('span');
    caret.className = 'caret';
    bubble.appendChild(txt);
    bubble.appendChild(caret);

    col.appendChild(line);
    col.appendChild(bubble);

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = '面';

    wrap.appendChild(avatar);
    wrap.appendChild(col);
    inner().appendChild(wrap);
    scrollDown();
    return { txt, bubble, caret, reasonEl: line };
  }

  function addUser(text) {
    const wrap = document.createElement('div');
    wrap.className = 'msg me';
    const col = document.createElement('div');
    col.className = 'col';
    const bubble = document.createElement('div');
    bubble.className = 'bubble' + (text ? '' : ' ghosted');
    bubble.textContent = text || '（没听清，跳过这句）';
    col.appendChild(bubble);
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = '你';
    wrap.appendChild(avatar);
    wrap.appendChild(col);
    inner().appendChild(wrap);
    scrollDown();
    return bubble;
  }

  function addNotice(text) {
    const d = document.createElement('div');
    d.className = 'notice';
    d.innerHTML = text;
    inner().appendChild(d);
    scrollDown();
    return d;
  }

  function markInterrupt(ratio) {
    if (!S.bubble) return;
    const flag = document.createElement('div');
    flag.className = 'interrupt-flag';
    flag.textContent = `已打断 · 播报 ${Math.round(ratio * 100)}%`;
    S.bubble.bubble.appendChild(flag);
  }

  /* ------------------------------------------------------------ HUD */

  function paintHUD() {
    $('hudFirst').textContent = S.tFirstToken ? ms(S.tFirstToken - S.tSpeechEnd) : '—';
    $('hudAudio').textContent = S.tFirstAudio ? ms(S.tFirstAudio - S.tSpeechEnd) : '—';
    $('hudTotal').textContent = S.tDone ? ms(S.tDone - S.tSpeechEnd) : '—';

    const f = S.tFirstToken ? S.tFirstToken - S.tSpeechEnd : 0;
    const a = S.tFirstAudio ? S.tFirstAudio - S.tSpeechEnd : 0;
    $('hudFirst').parentElement.className = 'chip' + (f > 800 ? ' warn' : '');
    $('hudAudio').parentElement.className = 'chip' + (a > 1500 ? ' warn' : '');
  }

  function paintStats() {
    const s = S.stats;
    const started = !!S.session || s.question > 0;
    $('mQ').textContent = started ? `${Math.max(1, s.question)} / ${s.maxQ}` : '— / —';
    $('mBar').style.width = started ? Math.min(100, (s.question / Math.max(1, s.maxQ)) * 100) + '%' : '0%';
    $('mTurns').textContent = s.turns;
    $('mProbe').textContent = s.probe;
    $('mClarify').textContent = s.clarify;
    $('mInterrupt').textContent = s.interrupts;
    $('mCost').textContent = '¥' + (s.cost || 0).toFixed(4) + (S.mock ? ' · 估算' : '');
    $('mCost').title = S.mock
      ? '演示模式：按价目表估算，没有真的调用模型，不会产生费用'
      : '真实调用模型，按实测 token 计费';
    $('mTokens').textContent = `${(s.inTok || 0).toLocaleString()} / ${(s.outTok || 0).toLocaleString()}`;
  }

  function applyState(st) {
    if (!st) return;
    S.stats.turns = st.answered || 0;
    S.stats.probe = st.probes || 0;
    S.stats.clarify = st.clarifys || 0;
    S.stats.interrupts = st.interrupts || 0;
    S.stats.question = st.question || 1;
    S.stats.maxQ = st.max_question || S.rounds;
    S.stats.cost = st.cost || 0;
    if (st.tokens) { S.stats.inTok = st.tokens.in || 0; S.stats.outTok = st.tokens.out || 0; }
    paintStats();
  }

  function paintMic() {
    micBtn.classList.toggle('on', S.listening);
    micBtn.disabled = !S.session || S.finished || S.busy;
  }

  /* ------------------------------------------------------------ 语音播报 */

  function enqueue(text, base) {
    if (!S.tts || !text) return;
    if (S.interrupted) return;        // 这一轮已经被打断，后半句不再念
    S.queue.push({ text, base, tries: 0 });
    if (!S.speaking) pump();
  }

  function pump() {
    if (S.speaking) return;
    const item = S.queue.shift();
    if (!item) { onSpeechIdle(); return; }

    // cancel() 是异步的，紧接着 speak() 会被浏览器丢掉（表现就是"有字幕没声音"）
    const wait = (S.speakReadyAt || 0) - performance.now();
    if (wait > 0) {
      S.queue.unshift(item);
      setTimeout(pump, wait + 20);
      return;
    }

    const token = S.speechToken;
    S.speaking = true;
    S.speakStartAt = performance.now();
    setStatus('面试官在说…', 'speak');

    const u = new SpeechSynthesisUtterance(item.text);
    u.lang = 'zh-CN';
    u.rate = 1.02;
    u.pitch = 1;

    u.onstart = () => {
      S.speakStartAt = performance.now();
      if (!S.tFirstAudio) {
        S.tFirstAudio = performance.now();
        paintHUD();
        postTiming();
      }
    };
    u.onboundary = (e) => {
      if (typeof e.charIndex === 'number') S.spokenChars = item.base + e.charIndex;
    };
    u.onend = () => {
      if (token !== S.speechToken) return;
      S.spokenChars = item.base + item.text.length;
      S.speaking = false;
      S.utter = null;
      pump();
    };
    u.onerror = () => {
      if (token !== S.speechToken) return;
      S.speaking = false;
      S.utter = null;
      pump();
    };

    S.utter = u;
    try {
      if (speechSynthesis.paused) speechSynthesis.resume();
      speechSynthesis.speak(u);
    } catch (e) {
      S.speaking = false;
      pump();
      return;
    }

    // 页面切走过、或引擎卡住时，speak() 不报错也不出声。450ms 还没起来就补一次。
    setTimeout(() => {
      if (token !== S.speechToken || !S.speaking) return;
      if (speechSynthesis.speaking || speechSynthesis.pending) return;
      if (item.tries >= 1) return;
      item.tries += 1;
      S.speaking = false;
      S.queue.unshift(item);
      S.speakReadyAt = performance.now() + 150;
      try { speechSynthesis.cancel(); } catch (e) { /* noop */ }
      pump();
    }, 450);
  }

  function onSpeechIdle() {
    S.speaking = false;
    if (!S.session || S.finished) return;
    if (S.voice && !S.asrDown) {
      if (!S.listening) startRec();
      refreshStatus();
    } else {
      refreshStatus();
      input.disabled = false;
      input.focus();
    }
    resumeSilence();
  }

  // 判断"这段声音是不是它自己"：外放时麦克风会拾到 TTS，
  // 一响就断会变成自激——它把自己打断，用户还以为坏了。
  function looksLikeEcho(text) {
    const t = (text || '').replace(/\s/g, '');
    const full = (S.fullText || '').replace(/\s/g, '');
    if (!t || !full) return false;
    if (full.includes(t)) return true;
    let hit = 0;
    for (const ch of t) if (full.includes(ch)) hit++;
    return hit / t.length > 0.85;
  }

  function canBarge(text) {
    const now = performance.now();
    if (S.speakStartAt && now - S.speakStartAt < 500) return false;   // 刚出声，给麦克风一点缓冲
    if (S.lastInterruptAt && now - S.lastInterruptAt < 2000) return false; // 一次回答只断一次
    return !looksLikeEcho(text);
  }

  function interruptNow(heard) {
    if (!S.speaking) return;
    const total = Math.max(1, S.fullText.length);
    const ratio = Math.min(0.98, (S.spokenChars || 0) / total);
    const spoken = S.fullText.slice(0, Math.max(0, Math.min(S.fullText.length, S.spokenChars || 0)));
    S.speechToken++;
    try { speechSynthesis.cancel(); } catch (e) { /* noop */ }
    S.speaking = false;
    S.utter = null;
    S.queue.length = 0;
    S.interrupted = true;
    S.lastInterruptAt = performance.now();
    S.speakReadyAt = performance.now() + 160;
    S.stats.interrupts++;
    markInterrupt(ratio);
    paintStats();
    post('/api/interrupt', { session: S.session, ratio, spoken, heard: heard || '' });
    // 识别本来就在跑（边说边听），这里不重启，否则会把刚识别到的话清掉
    if (S.voice && !S.asrDown && !S.listening) startRec();
    refreshStatus();
  }

  // 切走标签页时浏览器会把 speechSynthesis 挂起，回来要手动接上，
  // 否则会出现"有字幕、没声音"
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    try { speechSynthesis.resume(); } catch (e) { /* noop */ }
  });

  /* ------------------------------------------------------------ 语音识别 */

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;

  function initRec() {
    if (!SR) return;
    rec = new SR();
    rec.lang = 'zh-CN';
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      S.listening = true;
      paintMic();
      refreshStatus();
    };

    rec.onspeechstart = () => {
      S.hadSpeech = true;
      // 这里不再直接判打断。外放时麦克风会拾到面试官自己的声音，
      // "一有起音就断"等于让它自己把自己打断（实测一场能误触五六次）。
      // 真正的打断交给 onresult：识别出 ≥4 个有意义的字才算。
    };

    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const t = r[0] && r[0].transcript ? r[0].transcript : '';
        if (r.isFinal) S.finalText += t;
        else interim += t;
      }
      S.interimText = interim;
      const merged = (S.finalText + interim).trim();
      input.value = merged.slice(-90);

      // 边说边听：识别到成句的内容，就算打断，不等 VAD
      if (S.speaking && merged.length >= 4 && canBarge(merged)) {
        interruptNow(merged);
        return;
      }
      resetSilence();
    };

    rec.onerror = (e) => {
      S.listening = false;
      paintMic();
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        degrade('麦克风没被允许，或者识别服务不可用。已切到打字模式。');
      } else if (e.error === 'no-speech') {
        setStatus('没听到声音', '');
      } else if (e.error === 'network') {
        degrade('语音识别连不上服务（Chrome 走境外服务时常见）。已切到打字模式，换成 Edge 通常就好了。');
      }
    };

    rec.onend = () => {
      S.listening = false;
      paintMic();
      if (S.wantListen && S.voice && S.session && !S.finished) setTimeout(startRec, 220);
    };
  }

  function degrade(msg) {
    S.voice = false;
    S.asrDown = true;
    S.wantListen = false;
    $('voiceSwitch').classList.remove('on');
    $('voiceHint').textContent = '识别用不了，改打字；它还会念';
    input.disabled = false;
    input.focus();
    tip(`<b>${msg}</b>　播报没关，还能听见它说话。`);
    setStatus('文字输入 · 仍会播报', '');
    // 不要在这里 cancel 播报：识别挂了不代表它该闭嘴
  }

  function startRec() {
    if (!rec || S.listening || !S.session || S.finished) return;
    // 注意：这里绝对不能清 finalText。
    // 识别引擎在静默几秒后会自己 onend，代码再 startRec 把它接上——
    // 如果这时把缓冲清了，用户说到一半的半句话就没了（"前半段没录进去"）。
    // 清空只发生在「这一轮已经提交」之后。
    S.wantListen = true;
    try { rec.start(); } catch (e) { /* 已在跑 */ }
  }

  function stopRec() {
    S.wantListen = false;
    clearTimeout(S.timer.silence);
    if (rec && S.listening) { try { rec.stop(); } catch (e) { /* noop */ } }
  }

  function resetSilence() {
    clearTimeout(S.timer.silence);
    if (S.speaking || S.busy) return;      // 面试官在说或还在生成，不判"说完"
    S.timer.silence = setTimeout(() => {
      if (!S.speaking && !S.busy && S.finalText.trim()) commitSpeech();
    }, S.silenceMs);
  }

  // 播报 / 生成结束时补一次。否则用户趁它说话时讲的内容，
  // 因为当时 speaking=true 没计时，播完之后就再也没人管了——表现就是"我不说了它也没反应"。
  function resumeSilence() {
    if (!S.finalText.trim() || S.speaking || S.busy) return;
    resetSilence();
  }

  function commitSpeech() {
    const text = (S.finalText || '').trim();
    const had = S.hadSpeech;
    const empty = !text;
    stopRec();
    input.value = '';
    S.finalText = '';
    S.interimText = '';
    if (empty && !had) return;
    sendTurn(text, empty);
  }

  /* ------------------------------------------------------------ 主流程 */

  async function startSession() {
    if (S.busy) return;
    S.busy = true;
    startBtn.disabled = true;
    startBtn.textContent = '准备中…';

    chat.querySelectorAll('.chat-inner, .empty').forEach((n) => n.remove());
    S.finished = false;
    S.started = true;
    S.turn = 0;
    S.asrDown = false;
    S.interrupted = false;
    S.lastInterruptAt = 0;
    S.speakReadyAt = 0;
    S.speaking = false;
    S.queue.length = 0;
    try { speechSynthesis.cancel(); } catch (e) { /* noop */ }
    S.speechToken++;
    S.stats = { turns: 0, probe: 0, clarify: 0, switch: 0, interrupts: 0, question: 1, maxQ: S.rounds, cost: 0, inTok: 0, outTok: 0 };
    paintStats();
    setStatus('面试官上线', 'think');
    input.disabled = true;
    input.placeholder = '语音开着就直接说；也可以打字，回车发送';
    endBtn.disabled = false;

    try {
      await stream('/api/session/start', { role: S.role, rounds: S.rounds }, (ev) => {
        if (ev.t === 'session') {
          S.session = ev.id;
          S.mock = ev.mode === 'mock';
          S.stats.maxQ = ev.max_question;
          paintStats();
          paintMic();
          // 开场白也要能被打断：不在这里开麦，等它念完才开，
          // 用户在开场白期间说的话就全丢了（"第一句前半段没录进去"）
          if (S.voice) startRec();
          tip(S.mock
            ? '演示模式：没有检测到模型 Key，面试官由本地脚本扮演。配好 <b>DEEPSEEK_API_KEY</b> 后重启即可换成真模型。'
            : `真实模型 · ${S.rounds} 个话题 · 一场约 ¥0.05`);
        } else {
          handleEvent(ev);
        }
      });
    } catch (e) {
      setStatus('开局失败', '');
      tip(`<b>连不上本地服务：${e.message}</b>`);
    }

    S.busy = false;
    startBtn.disabled = false;
    startBtn.textContent = '重新开始';
    paintMic();
  }

  async function sendTurn(text, asrEmpty) {
    if (!S.session || S.busy || S.finished) return;
    S.busy = true;
    S.tSpeechEnd = performance.now();
    S.tFirstToken = 0;
    S.tFirstAudio = 0;
    S.tDone = 0;
    S.fullText = '';
    S.sentChars = 0;
    S.spokenChars = 0;
    S.queue.length = 0;
    S.interrupted = false;
    S.reportedAudio = false;
    S.bubble = null;
    // 新一轮开始，上一轮的识别缓冲才清
    S.finalText = '';
    S.interimText = '';
    S.hadSpeech = false;
    paintHUD();
    paintMic();
    input.disabled = true;
    setStatus('思考中…', 'think');

    addUser(text);
    // 边说边听：不等面试官说完就开麦，这样"打断"才是真的能打断
    if (S.voice) startRec();

    try {
      await stream('/api/turn', { session: S.session, text, asr_empty: !!asrEmpty }, handleEvent);
    } catch (e) {
      setStatus('连接断了', '');
      tip(`<b>这一轮没送达（${e.message}），可以再说一次。</b>`);
    }

    S.busy = false;
    paintMic();
    refreshStatus();
    if (!S.voice && !S.finished) { input.disabled = false; input.focus(); }
    resumeSilence();
  }

  function handleEvent(ev) {
    switch (ev.t) {
      case 'meta': {
        S.turn = ev.turn;
        S.bubble = addAgent(ev.label, ev.action, ev.reason);
        if (ev.action === 'close') endBtn.disabled = true;
        break;
      }
      case 'tok': {
        if (!S.tFirstToken) { S.tFirstToken = performance.now(); paintHUD(); }
        S.fullText += ev.d;
        if (S.bubble) S.bubble.txt.textContent = S.fullText;
        break;
      }
      case 'sent': {
        const base = S.sentChars;
        S.sentChars += ev.d.length;
        enqueue(ev.d, base);
        break;
      }
      case 'usage': {
        S.stats.cost = ev.cost || S.stats.cost;
        if (typeof ev.in === 'number') S.stats.inTok = ev.in;
        if (typeof ev.out === 'number') S.stats.outTok = ev.out;
        paintStats();
        break;
      }
      case 'err': {
        tip(`<b>模型报错：${ev.msg}</b>　已用兜底话术接住，会话没断。`);
        break;
      }
      case 'done': {
        S.tDone = performance.now();
        paintHUD();
        if (S.bubble && S.bubble.caret) S.bubble.caret.remove();
        applyState(ev.state);
        postTiming();
        if (ev.finished) { finishSession(); }
        else {
          refreshStatus();
          if (!S.voice) { input.disabled = false; input.focus(); }
        }
        break;
      }
      default:
        break;
    }
  }

  function postTiming() {
    if (!S.session || !S.turn) return;
    post('/api/timing', {
      session: S.session,
      turn: S.turn,
      first_token_ms: S.tFirstToken ? Math.round(S.tFirstToken - S.tSpeechEnd) : null,
      first_audio_ms: S.tFirstAudio ? Math.round(S.tFirstAudio - S.tSpeechEnd) : null,
      total_ms: S.tDone ? Math.round(S.tDone - S.tSpeechEnd) : null,
    });
  }

  async function finishSession() {
    S.finished = true;
    stopRec();
    paintMic();
    input.disabled = true;
    input.placeholder = '本场结束了 · 点左上角「开始对练」再来一场';
    sendBtn.disabled = true;
    endBtn.disabled = true;
    setStatus('对练结束，正在复盘…', 'think');
    tip('面试官在整理复盘，稍等几秒。');
    const data = await post('/api/report', { session: S.session });
    if (!data || !data.report) {
      setStatus('复盘失败', '');
      tip('<b>复盘没生成出来，会话记录已存在 runs/ 目录。</b>');
      return;
    }
    S.report = data.report;
    S.markdown = data.markdown || '';
    const mm = data.report.metrics || {};
    S.stats = {
      turns: mm.answered || 0,
      probe: (mm.actions || {}).probe || 0,
      clarify: (mm.actions || {}).clarify || 0,
      switch: (mm.actions || {}).switch || 0,
      interrupts: (mm.interrupts || {}).count || 0,
      question: mm.questions || S.stats.question,
      maxQ: mm.max_questions || S.stats.maxQ,
      cost: mm.cost_cny || 0,
      inTok: (mm.usage || {}).in || 0,
      outTok: (mm.usage || {}).out || 0,
    };
    paintStats();
    renderReport(data.report);
    $('transcriptWrap').hidden = true;      // 本场复盘不显示回顾，回顾在「往期」里看
    $('downloadBtn').hidden = false;
    $('modal').hidden = false;
    setStatus('已复盘', 'on');
    tip('记录已存到 runs/，可以下载复盘；下一场在「往期复盘」里能翻回来。');
    loadHistory();
  }

  async function endEarly() {
    if (!S.session || S.busy) return;
    if (!confirm('提前结束这一场？已有的问答会照常生成复盘。')) return;
    stopRec();
    try { speechSynthesis.cancel(); } catch (e) {}
    S.speechToken++;
    S.speaking = false;
    S.queue.length = 0;
    await finishSession();
  }

  /* ------------------------------------------------------------ 复盘 */

  function renderReport(r) {
    const m = r.metrics || {};
    const lat = m.latency || {};
    const u = m.usage || {};

    $('scoreNum').textContent = r.score != null ? r.score : '—';
    $('scoreRing').style.setProperty('--p', Math.max(0, Math.min(100, r.score || 0)));
    $('reportVerdict').textContent = r.verdict || '复盘';
    $('reportSub').textContent =
      `${m.role || ''} · ${m.duration_s || 0}s · ${m.questions || 0}/${m.max_questions || 0} 个话题` +
      (r.mode === 'mock' ? ' · 演示模式' : '');

    const ul = $('strengths');
    ul.innerHTML = '';
    (r.strengths || []).forEach((s) => {
      const li = document.createElement('li');
      li.textContent = s;
      ul.appendChild(li);
    });

    const ol = $('improvements');
    ol.innerHTML = '';
    (r.improvements || []).forEach((it) => {
      const li = document.createElement('li');
      if (typeof it === 'string') {
        li.textContent = it;
      } else {
        const b = document.createElement('b');
        b.textContent = it.issue || '';
        li.appendChild(b);
        li.appendChild(document.createTextNode(' → ' + (it.fix || '')));
      }
      ol.appendChild(li);
    });

    const rows = [
      ['时长', (m.duration_s || 0) + ' 秒'],
      ['话题', `${m.questions || 0} / ${m.max_questions || 0}`],
      ['回答轮次', `${m.user_turns || 0}（澄清 ${(m.actions || {}).clarify || 0}）`],
      ['平均回答', `${m.avg_answer_chars || 0} 字`],
      ['追问 / 换题', `${(m.actions || {}).probe || 0} / ${(m.actions || {}).switch || 0}`],
      ['首字 P50 / P95', `${ms(lat.first_token_p50)} / ${ms(lat.first_token_p95)}`],
      ['首音 P50 / P95', `${ms(lat.first_audio_p50)} / ${ms(lat.first_audio_p95)}`],
      ['整轮 P50 / P95', `${ms(lat.total_p50)} / ${ms(lat.total_p95)}`],
      ['打断 / 不重复', `${(m.interrupts || {}).count || 0} 次 / ${(m.interrupts || {}).recovery_rate == null ? '—' : Math.round(m.interrupts.recovery_rate * 100) + '%'}`],
      ['token 入 / 出', `${(u.in || 0).toLocaleString()} / ${(u.out || 0).toLocaleString()}`],
      ['本场成本', `¥${(m.cost_cny || 0).toFixed(4)}${u.mock ? '（演示，估算）' : ''}`],
    ];
    const tbl = $('reportTable');
    tbl.innerHTML = '';
    rows.forEach(([k, v]) => {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td');
      td1.textContent = k;
      const td2 = document.createElement('td');
      td2.textContent = v;
      tr.appendChild(td1);
      tr.appendChild(td2);
      tbl.appendChild(tr);
    });
  }

  function resetAll() {
    stopRec();
    try { speechSynthesis.cancel(); } catch (e) {}
    S.speechToken++;
    chat.querySelectorAll('.chat-inner').forEach((n) => n.remove());
    S.queue.length = 0;
    S.speaking = false;
    S.interrupted = false;
    const box = document.createElement('div');
    box.className = 'empty';
    box.id = 'empty';
    box.innerHTML = `
      <div class="empty-mark">对练</div>
      <h2>选个岗位，直接开口。</h2>
      <p>面试官一次只问一个问题：你答得含糊它追问，答得漂亮它换题，你随时可以打断它。结束后给你一份复盘和一组数据。</p>
      <div class="empty-tips"><span>8 个话题约 10 分钟</span><span>一场约 ¥0.05</span><span>不需要注册</span></div>`;
    chat.appendChild(box);
    S.session = null;
    S.finished = false;
    S.started = false;
    S.stats = { turns: 0, probe: 0, clarify: 0, switch: 0, interrupts: 0, question: 0, maxQ: S.rounds, cost: 0, inTok: 0, outTok: 0 };
    paintStats();
    paintHUD();
    $('hudFirst').textContent = '—';
    $('hudAudio').textContent = '—';
    $('hudTotal').textContent = '—';
    input.value = '';
    input.disabled = true;
    input.placeholder = '还没开始 · 点左上角「开始对练」';
    sendBtn.disabled = true;
    endBtn.disabled = true;
    micBtn.disabled = true;
    startBtn.disabled = false;
    startBtn.textContent = '开始对练';
    setStatus('还没开始', '');
    tip('');
  }

  /* ------------------------------------------------------------ 绑定 */

  document.querySelectorAll('.seg').forEach((seg) => {
    seg.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      seg.querySelectorAll('button').forEach((b) => b.classList.remove('on'));
      btn.classList.add('on');
      seg.dataset.value = btn.dataset.v;
      if (seg.id === 'roleSeg') S.role = btn.dataset.v;
      if (seg.id === 'roundSeg') S.rounds = parseInt(btn.dataset.v, 10);
    });
  });

  $('voiceSwitch').addEventListener('click', () => {
    const sw = $('voiceSwitch');
    S.voice = !sw.classList.contains('on');
    S.tts = S.voice;                    // 手动关掉就是真不想听了
    if (S.voice) S.asrDown = false;     // 手动重开，就给识别一次重试机会
    sw.classList.toggle('on', S.voice);
    $('voiceHint').textContent = S.voice ? '它会念，你说完自动接话' : '只打字，不播音';
    if (S.voice) {
      if (SR) tip('用语音时建议戴耳机：否则扬声器的声音可能被麦克风当成「你插话」，触发误打断。');
      else degrade('这个浏览器没有语音识别（Chrome / Edge 才有 Web Speech API）。已切到打字模式。');
    }
  });

  startBtn.addEventListener('click', startSession);
  endBtn.addEventListener('click', endEarly);

  sendBtn.addEventListener('click', () => {
    const v = input.value.trim();
    if (!v) return;
    input.value = '';
    sendTurn(v, false);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      const v = input.value.trim();
      if (!v) return;
      input.value = '';
      sendTurn(v, false);
    }
  });

  micBtn.addEventListener('click', () => {
    if (S.speaking) { interruptNow(''); return; }
    if (S.listening) { commitSpeech(); return; }
    if (S.finalText.trim()) { commitSpeech(); return; }
    startRec();
  });

  // 空格按住说话
  let spaceDown = false;
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || e.repeat || document.activeElement === input) return;
    e.preventDefault();
    if (!S.session || S.finished || S.busy) return;
    if (S.speaking) { interruptNow(''); return; }
    if (!spaceDown) { spaceDown = true; startRec(); }
  });
  document.addEventListener('keyup', (e) => {
    if (e.code !== 'Space') return;
    if (!spaceDown) return;
    spaceDown = false;
    if (S.listening) commitSpeech();
  });

  $('closeModal').addEventListener('click', () => { $('modal').hidden = true; });
  $('againBtn').addEventListener('click', () => { $('modal').hidden = true; resetAll(); });
  $('downloadBtn').addEventListener('click', () => {
    if (!S.markdown) return;
    const blob = new Blob([S.markdown], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `复盘-${new Date().toISOString().slice(0, 10)}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  });

  /* ---------------------------------------------------------- 往期复盘 */

  function fmtTime(s) {
    const m = String(s || '').match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?/);
    return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6] || '00'}` : (s || '');
  }

  async function loadHistory() {
    try {
      const r = await fetch('/api/history');
      const d = await r.json();
      S.history = (d && d.items) || [];
      const n = S.history.length;
      $('histCount').textContent = n ? String(n) : '';
      const last = S.history[0];
      const btn = $('histBtn');
      if (btn) {
        btn.title = n
          ? `往期 ${n} 场 · 最近 ${fmtTime(last.time)}`
            + (last.score != null ? ` · ${last.score} 分` : '')
          : '往期复盘（还没练过）';
      }
    } catch (e) { /* 服务没起来就静默 */ }
  }

  function openHistory() {
    const list = $('histList');
    list.innerHTML = '';
    const items = S.history || [];
    if (!items.length) {
      const p = document.createElement('p');
      p.className = 'hist-desc';
      p.textContent = '还没有记录。练一场，结束时会自动存下来。';
      list.appendChild(p);
    }
    items.forEach((it) => {
      const b = document.createElement('button');
      b.className = 'hist-item';

      const sc = document.createElement('span');
      sc.className = 'score' + (it.score == null ? ' none' : '');
      sc.textContent = it.score == null ? '未完' : String(it.score);

      const meta = document.createElement('div');
      meta.className = 'meta';
      const title = document.createElement('b');
      title.textContent = `${fmtTime(it.time)} · ${it.role || '—'}`;
      const sub = document.createElement('span');
      sub.textContent = `${it.questions || 0}/${it.max_questions || '—'} 个话题 · ` +
        `${it.mode === 'mock' ? '演示' : '真实模型'}${it.has_report ? '' : ' · 无复盘'}`;
      meta.appendChild(title);
      meta.appendChild(sub);

      const v = document.createElement('span');
      v.className = 'verdict';
      v.textContent = it.verdict || '';

      b.appendChild(sc);
      b.appendChild(meta);
      b.appendChild(v);
      b.addEventListener('click', () => showHistoryDetail(it.id));
      list.appendChild(b);
    });
    $('histModal').hidden = false;
  }

  async function showHistoryDetail(id) {
    let d = null;
    try {
      const r = await fetch('/api/history?id=' + encodeURIComponent(id));
      d = await r.json();
    } catch (e) {
      return;
    }
    if (!d || d.error) return;
    if (!d.report) {
      tip('这一场没有生成复盘（可能中途关掉了），但对话记录还在 runs/ 里。');
      $('histModal').hidden = true;
      return;
    }
    $('histModal').hidden = true;
    renderReport(d.report);
    renderTranscript(d.turns);
    $('transcriptWrap').hidden = false;
    $('downloadBtn').hidden = true;          // 历史场次没有现成的 markdown
    $('againBtn').textContent = '再练一场';
    $('modal').hidden = false;
  }

  function renderTranscript(turns) {
    const box = $('transcript');
    box.innerHTML = '';
    (turns || []).forEach((t) => {
      if (!t || !t.text) return;
      const isUser = t.role === 'user';
      const row = document.createElement('div');
      row.className = 'turn ' + (isUser ? 'user' : 'agent');

      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = isUser ? '你' : '面试官';

      row.appendChild(who);
      if (!isUser && t.action) {
        const act = document.createElement('span');
        act.className = 'act';
        act.textContent = t.action;
        row.appendChild(act);
      }
      const say = document.createElement('span');
      say.className = 'say';
      say.textContent = t.text;
      row.appendChild(say);
      box.appendChild(row);
    });
  }

  $('histBtn').addEventListener('click', async () => {
    await loadHistory();
    openHistory();
  });
  $('closeHist').addEventListener('click', () => { $('histModal').hidden = true; });
  $('histModal').addEventListener('click', (e) => {
    if (e.target === $('histModal')) $('histModal').hidden = true;
  });

  window.addEventListener('beforeunload', () => {
    try { speechSynthesis.cancel(); } catch (e) {}
    stopRec();
  });

  /* ------------------------------------------------------------ 启动 */

  initRec();

  fetch('/api/health')
    .then((r) => r.json())
    .then((h) => {
      S.mock = !!h.mock;
      const dot = $('modeDot');
      dot.classList.add(S.mock ? 'mock' : 'live');
      $('modeText').textContent = S.mock
        ? '演示模式 · 未配置模型 Key'
        : `真实模型 · ${h.model}`;
      if (S.mock) {
        tip('演示模式：面试官由本地脚本扮演，回复是预设的，成本数字是估算值。配好 <b>DEEPSEEK_API_KEY</b> 重启即可切换。');
      }
      if (!SR) {
        $('voiceSwitch').classList.remove('on');
        S.voice = false;
        S.asrDown = true;
        $('voiceHint').textContent = '这个浏览器不支持语音识别，用打字';
        tip('当前浏览器没有 Web Speech API，已切到打字模式。用 <b>Edge</b> 打开可以语音对练。');
      }
    })
    .catch(() => {
      $('modeText').textContent = '服务未连接';
    });

  paintStats();
  loadHistory();
})();
