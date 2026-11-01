/* app.js —— 面试陪练前端：流式对话 · 分句播报 · 打断检测 · 指标 HUD */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const chat = $('chat');
  const input = $('textInput');
  const micBtn = $('micBtn');
  const sendBtn = $('sendBtn');
  const endBtn = $('endBtn');
  const pauseBtn = $('pauseBtn');
  const startBtn = $('startBtn');
  const tipbar = $('tipbar');

  const S = {
    session: null,
    role: 'ai_pm',
    rounds: 8,
    level: 'normal',      // easy / normal / hard：只影响判定阈值和提问深度
    voice: true,
    mock: true,
    busy: false,
    finished: false,
    started: false,
    // 语音
    speaking: false,
    listening: false,
    wantListen: false,
    lastAsrAt: 0,        // 最后一次收到识别事件的时间，用来发现「mic 开着但没事件」
    lastVoiceAt: 0,      // 最后一次「真的有人说话」的时间。和 lastAsrAt 分开：
                         // 心跳重开麦克风也会刷新 lastAsrAt，混用会让冷场判断永远不触发
    asrRestarts: 0,      // 本轮连续重连次数，超过上限就降级打字
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
    timer: { silence: 0, barge: 0, silenceNudge: 0 },
    silenceMs: 4500,     // 说完了停多久算说完。3 秒实测还是太急，用户停一下就被打断；跟 config 走
    nudgeMs: 5000,       // 轮到你说、却一个字都没有，多久催一句（服务端可覆盖）
    closeMs: 5000,       // 催过之后还没动静，多久把这一场收掉（服务端可覆盖）→ 累计 10 秒
    nudges: 0,           // 已经催过几次；用户一开口就清零
    nudgeFails: 0,       // 连续失败次数：服务端没有这个接口时别一直打
    paused: false,       // 手动暂停：不识别、不播报、不提交、不催场
    typing: false,       // 鼠标在输入框里：这时不把识别结果往里写
    lastKeyAt: 0,        // 最后一次真的敲键盘的时间。"光标在框里"不算在打字
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

  // 输入框是从一行长到四行的 textarea：识别出来的话几十个字，单行框只看得见开头，
  // 用户不知道自己后面说了什么。这里让它跟着内容长，并且始终滚到最新一行。
  function growInput() {
    input.style.height = 'auto';
    const h = Math.min(input.scrollHeight || 46, 116);
    input.style.height = Math.max(46, h) + 'px';
    input.scrollTop = input.scrollHeight;
  }

  function clearInput() {
    input.value = '';
    growInput();
  }

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

  function paintSpend() {
    const d = S.spend;
    const el = $('mSpend');
    if (!el) return;
    if (!d) { el.textContent = '—'; return; }
    const total = Number(d.total_cny || 0);
    el.textContent = `¥${total.toFixed(4)}（${d.live_sessions || 0} 场真实）`;
    el.title = `真实模式累计 ¥${total.toFixed(4)}，共 ${d.live_sessions || 0} 场；`
      + `演示模式 ${d.mock_sessions || 0} 场不计入（那是估算值，没真花钱）`;
  }

  // 累计花费要把「这一轮刚花的」算进去：服务端落盘比 done 事件晚一点点，
  // 所以这里等 400ms 再拉，避免看到旧数字。
  async function loadSpend(delay) {
    if (delay) await new Promise((r) => setTimeout(r, delay));
    try {
      const r = await fetch('/api/spend');
      S.spend = await r.json();
      paintSpend();
    } catch (e) { /* 拉不到就保持原值，不打扰用户 */ }
  }

  function paintMic() {
    micBtn.classList.toggle('on', S.listening);
    micBtn.disabled = !S.session || S.finished || S.busy;
    pauseBtn.disabled = !S.session || S.finished;
    pauseBtn.classList.toggle('on', S.paused);
    pauseBtn.textContent = S.paused ? '继续' : '暂停';
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
    if (S.paused) { refreshStatus(); return; }
    if (S.voice && !S.asrDown) {
      if (!S.listening) startRec();
      refreshStatus();
    } else {
      refreshStatus();
      input.disabled = false;
      input.focus();
    }
    resumeSilence();
    armSilence();          // 它说完了，接下来该你说：开始计冷场
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
    // 没听见他插什么（比如他只是点了下麦克风）就别记一次打断——
    // 记了后端会生成"没听清你插什么"的回复，纯噪声。
    if ((heard || '').trim()) {
      post('/api/interrupt', { session: S.session, ratio, spoken, heard });
    }
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
  // 提示文案要分清浏览器：用户明明在用 Edge，还让他"换成 Edge"，等于把问题指错方向
  const IS_EDGE = /Edg\//.test(navigator.userAgent || '');
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
      S.lastAsrAt = performance.now();
      paintMic();
      refreshStatus();
    };

    rec.onspeechstart = () => {
      S.hadSpeech = true;
      S.lastAsrAt = performance.now();
      S.lastVoiceAt = performance.now();
      // 这里不再直接判打断。外放时麦克风会拾到面试官自己的声音，
      // "一有起音就断"等于让它自己把自己打断（实测一场能误触五六次）。
      // 真正的打断交给 onresult：识别出 ≥4 个有意义的字才算。
    };

    rec.onresult = (e) => {
      S.lastAsrAt = performance.now();
      S.lastVoiceAt = performance.now();
      S.asrRestarts = 0;                  // 出字了就说明引擎活着，重连计数清零
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const t = r[0] && r[0].transcript ? r[0].transcript : '';
        if (r.isFinal) S.finalText += t;
        else interim += t;
      }
      S.interimText = interim;
      const merged = (S.finalText + interim).trim();
      // 鼠标在框里打字的时候不要写：否则识别结果会把用户正在打的字冲掉。
      // 点进输入框会把麦克风关掉，所以正常情况下这里也不会被调用。
      if (!S.typing) {
        // 留够一整段，靠输入框自己换行 + 滚到底来显示最新内容
        input.value = merged.slice(-400);
        growInput();
      }

      // 边说边听：识别到成句的内容，就算打断，不等 VAD
      if (S.speaking && merged.length >= 4 && canBarge(merged)) {
        interruptNow(merged);
        return;
      }
      S.nudges = 0;                     // 他开口了，催场次数清零
      armSilence();                     // 冷场计时从头算
      resetSilence();
    };

    rec.onerror = (e) => {
      S.listening = false;
      paintMic();
      // 留一行日志：这次到底是哪种错，别让用户只能看到"连不上"三个字
      console.warn('[asr] error:', e.error, '| browser:', IS_EDGE ? 'Edge' : 'other');
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        degrade('麦克风没有被允许，或者系统里关掉了语音识别。已切到打字模式——'
          + '允许麦克风后，点左侧「语音」开关可以再试一次。');
      } else if (e.error === 'no-speech') {
        setStatus('没听到声音', '');
      } else if (e.error === 'network') {
        degrade(IS_EDGE
          ? '识别服务连不上（网络或代理的问题，Edge 也一样会断）。已切到打字模式——'
            + '点左侧「语音」开关可以再试一次。'
          : '这个浏览器的识别服务连不上（Chrome 走境外服务时常见）。已切到打字模式——'
            + '换成 Edge 打开通常就好了。');
      } else if (e.error === 'audio-capture') {
        degrade('找不到麦克风设备（可能是被别的软件占用了）。已切到打字模式。');
      }
    };

    rec.onend = () => {
      S.listening = false;
      paintMic();
      if (S.wantListen && S.voice && S.session && !S.finished) setTimeout(startRec, 220);
    };

    // 识别引擎会「假活」：麦克风按钮是开着的、S.listening 也是 true，
    // 但 onstart/onend/onresult 一个都不再来。实测一场对练在第 15 轮之后停在这个状态，
    // 表现就是"语音开着，不出字，也不提交"。onend 那条重启链救不了它——它压根不 fire。
    setInterval(asrGuardTick, 2000);
  }

  /**
   * 识别心跳。判断条件是"该它听的时候，连续一段时间一个识别事件都没有"。
   *
   * 注意这里只做一件事：把麦克风重开，不改模式、不降级。因为"用户在想事情"和
   * "引擎死了"在事件层面长得一模一样——真静音时重开不会丢任何字（半句话一定会
   * 先出 result，见 6.5），而引擎假活时重开是唯一的解药。宁可多开几次麦克风，
   * 也不要在用户思考 20 秒之后把他踢成打字模式。
   */
  function asrGuardTick() {
    if (!rec || !S.session || S.finished || S.paused) return;
    if (!S.voice || S.asrDown || !S.wantListen) return;
    if (S.speaking || S.busy) return;              // 它在说话 / 还在生成，不开麦

    if (!S.listening) {                            // 该听却没在听：接上，最多 5 秒一次
      if (performance.now() - (S.lastAsrAt || 0) > 5000) startRec();
      return;
    }

    // 前 3 次 12 秒救一次，还不活就放慢到 60 秒，别一直折腾麦克风
    const idleMs = performance.now() - (S.lastAsrAt || 0);
    if (idleMs < (S.asrRestarts > 3 ? 60000 : 12000)) return;

    S.asrRestarts += 1;
    if (S.asrRestarts === 4) {
      tip('<b>识别连着几次没反应</b>　一直这样的话先点一下麦克风按钮、或者直接打字，别在这儿耗。');
    }
    S.lastAsrAt = performance.now();
    try { rec.abort(); } catch (e) { /* noop */ }
    S.listening = false;
    // 留一行日志：用户下次说"又不出字了"，打开控制台就能看出是不是这个心跳在救
    console.warn(`[asr] ${Math.round(idleMs / 1000)}s 没有任何识别事件，重开麦克风（第 ${S.asrRestarts} 次）`);
    setTimeout(startRec, 60);
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
    if (!rec || !S.session || S.finished || S.paused) return;
    // 先表决心：我要听。这句必须放在 early return 之前——
    // 否则「stop 之后立刻 start」时（提交完这一轮就会这样），
    // start 被 listening 挡回去，wantListen 留在 false，
    // 随后的 onend 看到 wantListen=false 就不重连了，麦克风从此静默。
    S.wantListen = true;
    if (S.listening) return;
    // 注意：这里绝对不能清 finalText。
    // 识别引擎在静默几秒后会自己 onend，代码再 startRec 把它接上——
    // 如果这时把缓冲清了，用户说到一半的半句话就没了（"前半段没录进去"）。
    // 清空只发生在「这一轮已经提交」之后。
    S.lastAsrAt = performance.now();      // 重开一次，心跳重新计时
    try { rec.start(); } catch (e) { /* 已在跑 */ }
  }

  function stopRec() {
    S.wantListen = false;
    clearTimeout(S.timer.silence);
    if (rec && S.listening) { try { rec.stop(); } catch (e) { /* noop */ } }
    // 不要等 onend 再改状态：引擎假活的时候 onend 永远不来，
    // 按钮就会一直卡在"开着"，点也点不动。这里主观认定它已经停了。
    S.listening = false;
    paintMic();
  }

  function resetSilence() {
    clearTimeout(S.timer.silence);
    if (S.paused || S.speaking || S.busy) return;   // 暂停 / 它在说 / 还在生成：不判"说完"
    S.timer.silence = setTimeout(() => {
      if (!S.paused && !S.speaking && !S.busy && S.finalText.trim()) commitSpeech();
    }, S.silenceMs);
  }

  // 播报 / 生成结束时补一次。否则用户趁它说话时讲的内容，
  // 因为当时 speaking=true 没计时，播完之后就再也没人管了——表现就是"我不说了它也没反应"。
  function resumeSilence() {
    if (!S.finalText.trim() || S.speaking || S.busy) return;
    resetSilence();
  }

  /* ------------------------------------------------------------ 暂停 */

  // 用户要的：一句话说到一半想歇口气、或者想改成打字，能把整场冻住，再按一下接着来。
  // 冻住的时候不识别、不播报、不提交、不催场——一切自动行为都停，只保留手动发送。
  function togglePause(force) {
    const want = typeof force === 'boolean' ? force : !S.paused;
    if (want === S.paused) return;
    S.paused = want;

    if (want) {
      disarmSilence();
      clearTimeout(S.timer.silence);
      stopRec();
      S.speechToken += 1;
      try { speechSynthesis.cancel(); } catch (e) { /* noop */ }
      S.speaking = false;
      S.utter = null;
      S.queue.length = 0;
      input.disabled = false;          // 暂停时至少还能打字
      setStatus('已暂停 · 按「继续」接着来', '');
    } else {
      setStatus('继续', '');
      if (S.session && !S.finished) {
        if (S.voice && !S.asrDown) startRec();
        armSilence();
        resumeSilence();
      }
    }
    paintMic();
    refreshStatus();
  }

  /* ------------------------------------------------------------ 冷场 */

  // 轮到你说，却一个字都没来。真人面试官不会干等：先催一句，还没动静就换个更基础的问法。
  // 判断只看"有没有识别事件 / 有没有在打字"，不去猜用户在想什么。

  function disarmSilence() {
    clearTimeout(S.timer.silenceNudge);
  }

  function armSilence() {
    disarmSilence();
    if (!S.session || S.finished || S.paused || S.nudgeFails >= 3) return;
    // 第一次是「催一句」，催过之后等的是「再没动静就收尾」，两个时长不一样
    const wait = S.nudges > 0 ? S.closeMs : S.nudgeMs;
    S.timer.silenceNudge = setTimeout(fireSilence, wait);
  }

  // 每次跳过都把理由写进控制台。这个功能已经因为"静默跳过"排查过一轮了，
  // 下次再不动，打开控制台看 [nudge] 那一行就知道卡在哪个条件上。
  function nudgeSkip(reason) {
    console.info('[nudge] 跳过：' + reason);
  }

  // 返回"为什么现在不催"，空串表示可以催。
  function nudgeBlockedReason() {
    if (!S.session) return '没有会话';
    if (S.finished) return '这一场已结束';
    if (S.paused) return '已暂停';
    // 打字模式（识别不可用 / 手动关了语音）不催场、不收场：
    // 用户手上有键盘，5 秒催一句、10 秒结束太粗暴（实测把人从打字里直接踢下线）
    if (S.asrDown || !S.voice) return '在打字模式，不催场';
    if (S.busy) return '正在生成，等它说完';
    if (S.speaking) return '播报没结束（一直这样的话是 TTS 没回调）';
    if ((S.finalText || '').trim() || (S.interimText || '').trim()) return '识别缓冲里还有话';
    // 只有"刚敲过键盘"才算在打字。光标停在框里不算——那是上一个 bug：
    // 用户点过一次输入框，S.typing 就一直是 true，冷场永远不触发。
    if (S.typing && performance.now() - (S.lastKeyAt || 0) < 6000) return '正在打字';
    if (S.lastVoiceAt && performance.now() - S.lastVoiceAt < 4000) return '刚刚才说过话';
    return '';
  }

  async function fireSilence() {
    const why = nudgeBlockedReason();
    if (why) {
      nudgeSkip(why);
      armSilence();     // 跳过也必须把下一次挂上，否则冷场从此失效
      return;
    }
    console.info(`[nudge] 触发：${S.nudges > 0 ? '第二次沉默，收尾' : '主动接一句'}`);
    S.nudges += 1;                 // 第 2 次由服务端收尾并结束本场
    S.busy = true;
    // 和第 1 轮一样重置播报状态：不重置的话，催场那句话会接在上一轮的文字后面
    // （实测出现过「上一个问题 + 不着急，想到什么说什么」拼在同一个气泡里）
    S.tSpeechEnd = performance.now();
    S.tFirstToken = 0;
    S.tFirstAudio = 0;
    S.tDone = 0;
    S.fullText = '';
    S.sentChars = 0;
    S.spokenChars = 0;
    S.interrupted = false;
    S.reportedAudio = false;
    S.bubble = null;
    S.queue.length = 0;
    paintHUD();
    paintMic();
    refreshStatus();
    try {
      await stream('/api/silence', { session: S.session }, handleEvent);
      S.nudgeFails = 0;
    } catch (e) {
      S.nudgeFails += 1;      // 旧进程还没有这个接口，别每轮都撞墙
    }
    S.busy = false;
    paintMic();
    refreshStatus();
    armSilence();
  }

  function commitSpeech() {
    const text = (S.finalText || '').trim();
    const had = S.hadSpeech;
    const empty = !text;
    stopRec();
    clearInput();
    S.finalText = '';
    S.interimText = '';
    // 没字就不发。原来只要"起过音"就发一个空轮，服务端会判成"没听清，再说一遍"，
    // 用户只是想把麦克风关掉去打字，结果挨了一句"我没听清"——纯噪声。
    if (empty) return;
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
    input.placeholder = '语音开着就直接说；想打字就在框里点一下，语音会自动让开';
    endBtn.disabled = false;
    S.paused = false;
    S.typing = false;
    paintMic();

    // 开场也算一轮，计时基线必须在这里重置。
    // 之前没重置，第一轮的三段耗时是从「页面加载」算起的——实测录到过 23005ms，
    // 这个假数会直接混进复盘里的 P50。
    S.tSpeechEnd = performance.now();
    S.tFirstToken = 0;
    S.tFirstAudio = 0;
    S.tDone = 0;
    S.fullText = '';
    S.sentChars = 0;
    S.spokenChars = 0;
    S.asrRestarts = 0;
    S.lastVoiceAt = 0;

    try {
      await stream('/api/session/start', { role: S.role, rounds: S.rounds, level: S.level }, (ev) => {
        if (ev.t === 'session') {
          S.session = ev.id;
          S.mock = ev.mode === 'mock';
          S.stats.maxQ = ev.max_question;
          input.disabled = false;        // 开了局就能打字，不用先关麦克风
          paintStats();
          paintMic();
          // 开场白也要能被打断：不在这里开麦，等它念完才开，
          // 用户在开场白期间说的话就全丢了（"第一句前半段没录进去"）
          if (S.voice) startRec();
          tip(S.mock
            ? '演示模式：没有检测到模型 Key，面试官由本地脚本扮演。配好 <b>DEEPSEEK_API_KEY</b> 后重启即可换成真模型。'
            : `真实模型 · ${S.rounds} 个话题 · 一场约 ¥0.03`);
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
    S.nudgeFails = 0;
    armSilence();
  }

  async function sendTurn(text, asrEmpty) {
    if (!S.session || S.busy || S.finished) return;
    S.busy = true;
    // 发出去了就别再占着输入框：光标留在框里会让 S.typing 一直是 true，
    // 冷场判断会以为用户还在打字，永远不催。（这个坑实测踩到过）
    S.typing = false;
    S.nudges = 0;
    if (document.activeElement === input) input.blur();
    disarmSilence();                 // 已经开口了，冷场计时作废
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
    input.disabled = false;            // 生成完了就给回输入权（语音开着也能打字）
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
        // hint 是后端按报错类型给的「照这个做」，比原始报错有用得多，所以放前排
        const hint = ev.hint ? `<br><b>→ ${ev.hint}</b>` : '';
        tip(`<b>模型报错：${ev.msg}</b>${hint}<br>已用兜底话术接住，会话没断。`);
        break;
      }
      case 'done': {
        S.tDone = performance.now();
        paintHUD();
        if (S.bubble && S.bubble.caret) S.bubble.caret.remove();
        applyState(ev.state);
        postTiming();
        loadSpend(400);           // 每轮结束刷一次累计花费
        if (ev.finished) { finishSession(); }
        else {
          refreshStatus();
          input.disabled = false;   // 语音开着也能打字：不再强制先关麦克风
          armSilence();       // 拿到新问题了，开始等他回答
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
    S.paused = false;
    disarmSilence();
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
      ['难度', m.level_name || '标准'],
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

    renderSamples(r);
  }

  // 逐题：他问的 / 你当时答的 / 可以参考的答法。默认收起，点开才看。
  function renderSamples(r) {
    const box = $('samples');
    const wrap = $('samplesWrap');
    box.innerHTML = '';
    const rounds = r.per_round || [];
    const pairs = r.pairs || [];
    const n = Math.max(rounds.length, pairs.length);
    if (!n) { wrap.hidden = true; return; }
    wrap.hidden = false;

    if (r.note) {
      // 模型这次没给出完整复盘时，必须说清楚，别让人以为"模型就是给不出参考答法"
      const warn = document.createElement('div');
      warn.className = 'sample-note';
      warn.textContent = r.note;
      box.appendChild(warn);
    }

    for (let i = 0; i < n; i++) {
      const round = rounds[i] || {};
      const pair = pairs[i] || {};
      const sample = (round.sample || '').trim();
      const qText = pair.q || round.q || `第 ${i + 1} 个话题`;

      const item = document.createElement('div');
      item.className = 'sample-item';

      const head = document.createElement('button');
      head.className = 'sample-head';
      head.type = 'button';

      const q = document.createElement('span');
      q.className = 'sample-q';
      q.textContent = qText;
      const cmt = document.createElement('span');
      cmt.className = 'sample-cmt';
      cmt.textContent = round.comment || (sample ? '' : '这一题没有参考答法');
      q.appendChild(cmt);

      const tg = document.createElement('span');
      tg.className = 'sample-toggle';
      tg.textContent = sample ? '看参考答法' : '无范本';

      head.appendChild(q);
      head.appendChild(tg);

      const body = document.createElement('div');
      body.className = 'sample-body';
      body.hidden = true;
      if (pair.a) {
        const lab = document.createElement('span');
        lab.className = 'lab2';
        lab.textContent = '你当时答的';
        body.appendChild(lab);
        const mine = document.createElement('div');
        mine.className = 'mine';
        mine.textContent = pair.a;
        body.appendChild(mine);
      }
      if (sample) {
        const lab2 = document.createElement('span');
        lab2.className = 'lab2';
        lab2.textContent = '答题思路（从哪切入、怎么组织）';
        body.appendChild(lab2);
        body.appendChild(Object.assign(document.createElement('div'), { textContent: round.approach || '（这次没给）' }));

        const lab3 = document.createElement('span');
        lab3.className = 'lab2';
        lab3.textContent = '该备的数据（面试前先填好这几个数）';
        body.appendChild(lab3);
        body.appendChild(Object.assign(document.createElement('div'), { textContent: round.data || '（这次没给）' }));

        const lab4 = document.createElement('span');
        lab4.className = 'lab2';
        lab4.textContent = '可以这样开口（范本，不要求背）';
        body.appendChild(lab4);
        body.appendChild(Object.assign(document.createElement('div'), { textContent: sample }));
      } else if (r.mode === 'mock' || r.generated_by === 'template') {
        const note = document.createElement('div');
        note.className = 'mine';
        note.textContent = '演示模式不生成参考答法——这一项要接上真模型才有。';
        body.appendChild(note);
      }

      head.addEventListener('click', () => {
        body.hidden = !body.hidden;
        tg.textContent = body.hidden ? (sample ? '看参考答法' : '无范本') : '收起';
      });

      item.appendChild(head);
      item.appendChild(body);
      box.appendChild(item);
    }
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
      <div class="empty-tips"><span>8 个话题约 10 分钟</span><span>一场约 ¥0.03</span><span>不需要注册</span></div>`;
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
      if (seg.id === 'levelSeg') S.level = btn.dataset.v;
    });
  });

  $('voiceSwitch').addEventListener('click', () => {
    const sw = $('voiceSwitch');
    S.voice = !sw.classList.contains('on');
    S.tts = S.voice;                    // 手动关掉就是真不想听了
    sw.classList.toggle('on', S.voice);
    $('voiceHint').textContent = S.voice ? '它会念，你说完自动接话' : '只打字，不播音';

    if (!S.voice) return;

    // 重新打开语音 = 给识别一次真正的重试：重置判死标记、重连、马上开麦。
    // 原来只把 asrDown 清成 false，但既没重连也没提示，用户只会觉得"开关点了没用"。
    S.asrDown = false;
    S.asrRetry = (S.asrRetry || 0) + 1;
    if (!SR) {
      degrade('这个浏览器没有语音识别（Chrome / Edge 才有 Web Speech API）。已切到打字模式。');
      return;
    }
    tip('语音重新打开了，正在重连识别…');
    S.lastAsrAt = 0;
    if (S.session && !S.finished && !S.paused) {
      startRec();
      armSilence();
    }
    refreshStatus();
  });

  startBtn.addEventListener('click', startSession);
  endBtn.addEventListener('click', endEarly);

  sendBtn.addEventListener('click', () => {
    const v = input.value.trim();
    if (!v) return;
    clearInput();
    sendTurn(v, false);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing) return;
    if (e.shiftKey) return;                 // Shift+Enter 换行，不发送
    const v = input.value.trim();
    if (!v) return;
    clearInput();
    sendTurn(v, false);
  });

  // 打字时把框撑开，并把「冷场计时」从头算——人在输入说明不是冷场。
  // 注意这里不能 disarm：清掉之后没人再武装，冷场就永远不触发了。
  input.addEventListener('input', () => {
    growInput();
    S.lastKeyAt = performance.now();
    armSilence();
  });

  input.addEventListener('keydown', () => { S.lastKeyAt = performance.now(); });

  micBtn.addEventListener('click', () => {
    if (S.paused) return;
    if (S.asrDown) {
      // 识别已经判死：点了也不会有反应，必须说清楚；并告诉他怎么救回来
      tip(IS_EDGE
        ? '<b>识别服务现在连不上</b>　点麦克风没用，先打字；点左侧「语音」开关可以重连一次。'
        : '<b>识别服务现在连不上</b>　先打字；换成 Edge 打开通常就好了。');
      return;
    }
    if (S.speaking) { interruptNow(''); return; }   // 它在说：照旧打断，但不算一次"插话"
    if (S.listening) { stopRec(); setStatus('麦克风关了 · 可以打字', ''); refreshStatus(); return; }
    if (S.finalText.trim()) { commitSpeech(); return; }
    startRec();
  });

  pauseBtn.addEventListener('click', () => togglePause());

  // 鼠标点进输入框 = 我要打字：这时把麦克风静静关掉（不提交、不记打断），
  // 否则识别结果会一直往框里写，把正在打的字冲掉。
  input.addEventListener('focus', () => {
    S.typing = true;
    if (S.listening) stopRec();
  });
  input.addEventListener('blur', () => {
    S.typing = false;
    if (S.session && !S.finished && !S.paused && S.voice && !S.asrDown && !S.listening) startRec();
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
      // 没有复盘不等于记录没了：中途刷新/关页面也会留下完整对话。
      // 之前这里直接弹一句提示就关掉了，等于把能看的东西藏起来。
      if (d.turns && d.turns.length) {
        showTranscriptOnly(d);
        return;
      }
      tip('这一场没有任何记录（可能刚开局就关了）。');
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

  // 只有对话、没有复盘的那一场（中途刷新 / 提前关掉）
  function showTranscriptOnly(d) {
    $('scoreNum').textContent = '—';
    $('scoreRing').style.setProperty('--p', 0);
    $('reportVerdict').textContent = '这一场没有生成复盘';
    $('reportSub').textContent = `${d.role || ''} · ${d.time || ''} · 中途结束`;
    $('strengths').innerHTML = '';
    $('improvements').innerHTML = '';
    $('reportTable').innerHTML = '';
    renderTranscript(d.turns);
    $('transcriptWrap').hidden = false;
    $('downloadBtn').hidden = true;
    $('againBtn').textContent = '再练一场';
    $('histModal').hidden = true;
    $('modal').hidden = false;
  }

  function renderTranscript(turns) {    const box = $('transcript');
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

  window.addEventListener('beforeunload', (e) => {
    try { speechSynthesis.cancel(); } catch (err) { /* noop */ }
    stopRec();
    // 刷新会把页面上的对话弄丢（记录还在 runs/，但过程看不见了）。
    // 用户已经踩过一次，所以这里必须拦一下。
    if (S.session && !S.finished) {
      e.preventDefault();
      e.returnValue = '这一场还没结束，刷新会丢掉页面上的对话。';
      return e.returnValue;
    }
  });

  /* ------------------------------------------------------------ 启动 */

  initRec();

  fetch('/api/health')
    .then((r) => r.json())
    .then((h) => {
      S.mock = !!h.mock;
      // 三个时间别弄混：silenceMs = "说完"判定；nudgeMs = 冷场催一句；closeMs = 催过之后再等多久收场。
      // 这里曾经把 silence_seconds 写进 S.silenceMs，结果每句话都要等 10 秒才提交，看起来像"没发出去"。
      if (h.silence_seconds) S.nudgeMs = h.silence_seconds * 1000;
      if (h.silence_close_seconds) S.closeMs = h.silence_close_seconds * 1000;
      if (h.turn_silence_seconds) S.silenceMs = h.turn_silence_seconds * 1000;
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
  paintSpend();
  loadSpend();
  loadHistory();
})();
