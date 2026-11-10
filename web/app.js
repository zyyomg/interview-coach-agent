/* app.js —— 面试陪练前端：流式对话 · 分句播报 · 打断检测 · 指标 HUD */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  // 体积说人话：5.0MB / 812KB。提示里要写清"你传的是多大、上限多大"，不然用户没法判断该不该换文件。
  const mb = (b) => (b >= 1024 * 1024
    ? (b / 1024 / 1024).toFixed(1).replace(/\.0$/, '') + 'MB'
    : Math.max(1, Math.round(b / 1024)) + 'KB');
  // fetch 对"服务没开"只抛 TypeError，对"返回的不是 JSON"抛 SyntaxError。
  // 两种都写成"读取失败"的话，用户会去怀疑文件——所以按类型分开说。
  const netHint = (e, fallback) => (
    e instanceof TypeError ? '连不上本地服务，确认服务还开着'
      : e instanceof SyntaxError ? '服务返回异常，看终端有没有报错'
        : fallback);
  const chat = $('chat');
  const input = $('textInput');
  const micBtn = $('micBtn');
  const sendBtn = $('sendBtn');
  const endBtn = $('endBtn');
  const pauseBtn = $('pauseBtn');
  const startBtn = $('startBtn');
  const tipbar = $('tipbar');
  const resumeBox = $('resumeBox');
  const resumeDrop = $('resumeDrop');
  const resumeFile = $('resumeFile');
  const resumePaste = $('resumePaste');
  const resumeMeta = $('resumeMeta');
  const pfBox = $('pfBox');
  const pfAddBtn = $('pfAddBtn');
  const pfPasteBtn = $('pfPasteBtn');
  const pfFile = $('pfFile');
  const pfPaste = $('pfPaste');
  const pfList = $('pfList');
  const pfMeta = $('pfMeta');
  const roleInput = $('roleInput');
  const roleSeg = $('roleSeg');
  const roleHistBtn = $('roleHistBtn');
  const roleHist = $('roleHist');
  const formHint = $('formHint');

  // 岗位只有两档：手写（优先）+ 通用。预设列表永远不全，写进去的那句才是准的。
  const ROLE_LABEL = { ai_pm: 'AI 产品经理', pm: '产品经理', general: '通用岗位' };
  const ROLE_HIST_KEY = 'interview.roleHistory';
  const ROLE_HIST_MAX = 8;

  const S = {
    session: null,
    form: 'casual',       // casual 体验式（不带简历）/ formal 正式（必须带简历）
    resumeText: '',       // 解析/粘贴出来的简历正文
    resumeName: '',
    portfolio: [],        // 作品集：可以多份，[{name, chars, text}]
    pfText: '',           // 合并后发给服务端的正文（每份带【作品集N：文件名】标记）
    pfName: '',           // 文件名拼起来，只用于落盘记录
    pfLimit: 1600,        // 进提示词的字数，由 /api/health 下发，只用于提示
    pfMaxFiles: 6,
    uploadMaxBytes: 20 * 1024 * 1024,  // 单份文件体积上限，同样由 /api/health 下发
    role: 'general',      // 只有一个预设：通用
    roleTitle: '',        // 自己写的岗位名；有值时优先于 role
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
    timer: { silence: 0, barge: 0, silenceNudge: 0, asrProbe: 0, asrRetry: 0 },
    asrFails: 0,        // 识别连续断线次数，成功出字就清零；重连带指数退避
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
    // 服务端拒绝开局时会带一句人话（比如「正式面试要先给简历」）。
    // 直接抛 HTTP 400，用户只看到一串状态码，不知道该改哪里。
    if (!res.ok || !res.body) {
      let hint = '';
      try { const d = await res.json(); hint = d.hint || ''; } catch (e) { /* 不是 JSON 就算了 */ }
      throw new Error(hint || ('HTTP ' + res.status));
    }
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

  // 造一个识别器并绑好回调。
  //
  // 关键：**重连要换一个新对象**。已经卡死的 SpeechRecognition 实例再 start()
  // 通常一个事件都不会回（引擎内部连接已经废了），只有换新的才真的能活过来。
  // 之前重连是复用同一个 rec，所以"点语音开关重连"看着有反应、实际连不上。
  function makeRec() {
    if (!SR) return null;
    const r = new SR();
    r.lang = 'zh-CN';
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 1;

    r.onstart = () => {
      S.listening = true;
      S.lastAsrAt = performance.now();
      paintMic();
      refreshStatus();
    };

    r.onspeechstart = () => {
      S.hadSpeech = true;
      S.lastAsrAt = performance.now();
      S.lastVoiceAt = performance.now();
      // 这里不再直接判打断。外放时麦克风会拾到面试官自己的声音，
      // "一有起音就断"等于让它自己把自己打断（实测一场能误触五六次）。
      // 真正的打断交给 onresult：识别出 ≥4 个有意义的字才算。
    };

    r.onresult = (e) => {
      S.lastAsrAt = performance.now();
      S.lastVoiceAt = performance.now();
      S.asrRestarts = 0;                  // 出字了就说明引擎活着，重连计数清零
      S.asrDown = false;
      S.asrFails = 0;                     // 也把"断线次数"清零，下次断了重新从 4 秒开始
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        const t = res[0] && res[0].transcript ? res[0].transcript : '';
        if (res.isFinal) S.finalText += t;
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

    r.onerror = (e) => {
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
        // 在线识别断线是常态，不是"坏了"：不要直接降级成打字并永久放弃，
        // 而是保留语音、后台按退避自动重连（实测用户每次都得手动点开关，点得很烦）
        S.asrDown = true;
        paintMic();
        scheduleAsrRetry('网络');
      } else if (e.error === 'audio-capture') {
        degrade('找不到麦克风设备（可能是被别的软件占用了）。已切到打字模式。');
      }
    };

    r.onend = () => {
      S.listening = false;
      paintMic();
      if (S.wantListen && S.voice && S.session && !S.finished) setTimeout(startRec, 220);
    };

    return r;
  }

  function initRec() {
    rec = makeRec();
    if (!rec) return;
    // 识别引擎会「假活」：麦克风按钮是开着的、S.listening 也是 true，
    // 但 onstart/onend/onresult 一个都不再来。实测一场对练在第 15 轮之后停在这个状态，
    // 表现就是"语音开着，不出字，也不提交"。onend 那条重启链救不了它——它压根不 fire。
    setInterval(asrGuardTick, 2000);
  }

  /**
   * 统一的重连入口：换新识别器 + 解除暂停 + 给结果反馈。
   *
   * 为什么要把"解除暂停"也放进来：暂停时 startRec() 会直接返回，
   * 用户按语音开关、按麦克风都毫无反应——实测就卡在这个组合上，
   * 越按越像坏了。所以任何一条"我要用语音"的路径都先解除暂停。
   */
  function retryAsr() {
    if (!SR) return;
    if (S.paused) {
      S.paused = false;
      paintMic();
    }
    S.voice = true;
    S.tts = true;
    S.asrDown = false;
    S.asrRestarts = 0;
    S.lastAsrAt = 0;
    $('voiceSwitch').classList.add('on');
    $('voiceHint').textContent = '它会念，你说完自动接话';
    try { if (rec) rec.abort(); } catch (e) { /* noop */ }
    rec = makeRec();                 // 换新对象，不复用可能已死的那个
    S.listening = false;
    if (S.session && !S.finished) {
      startRec();
      armSilence();
    }
    refreshStatus();
    paintMic();
    tip('正在重连识别…');

    clearTimeout(S.timer.asrProbe);
    S.timer.asrProbe = setTimeout(() => {
      if (S.listening) {
        tip('识别重新连上了，继续说就行。');
        return;
      }
      S.asrDown = true;
      paintMic();
      scheduleAsrRetry('连不上');
    }, 2500);
  }

  /**
   * 识别断线后的自动重连：指数退避 4s → 7s → 12s → 20s → 30s 封顶。
   *
   * Web Speech 是在线识别，隔一阵就断一次，属于常态。让它自动接回来，
   * 用户不用每次自己去点开关；断的这段时间照样可以打字，不耽误回答。
   */
  function scheduleAsrRetry(reason) {
    if (!S.session || S.finished || !S.voice || S.paused) return;
    S.asrFails = (S.asrFails || 0) + 1;
    const wait = Math.min(30, Math.round(4 * Math.pow(1.7, S.asrFails - 1)));
    setStatus(`识别断了（${reason}），${wait} 秒后自动重连…`, '');
    tip(`<b>识别断线</b>　${wait} 秒后自动重连（第 ${S.asrFails} 次）；这期间可以直接打字，这场不会因此结束。`);
    clearTimeout(S.timer.asrRetry);
    S.timer.asrRetry = setTimeout(() => {
      if (S.session && !S.finished && S.voice && !S.paused) retryAsr();
    }, wait * 1000);
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
        // 恢复时如果识别是判死状态，别只调 startRec（它会在死对象上白试一次），
        // 直接走重连入口，换一个新识别器
        if (S.voice && S.asrDown) retryAsr();
        else if (S.voice) startRec();
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

  // ---- 简历模式 ----
  function setResumeMeta(text, cls) {
    resumeMeta.textContent = text;
    resumeMeta.className = 'resume-meta' + (cls ? ' ' + cls : '');
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result || '').split(',')[1] || '');
      r.onerror = () => reject(r.error || new Error('read error'));
      r.readAsDataURL(file);
    });
  }

  // 拖进来的文件不受 accept 约束，得自己挡一道。
  // .doc 老格式服务端读不出，先在本地拦住，省得白传一次 base64
  const DOC_EXT = /\.(docx|pdf|txt|md|markdown)$/i;

  // 浏览器默认把拖进来的文件当导航打开——不拦住整页就跳走了。
  // dragenter/dragleave 会在子元素之间反复触发，用计数才不会闪。
  function dropZone(el, onFiles) {
    if (!el) return;
    let depth = 0;
    const isFiles = (e) => !!e.dataTransfer
      && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0;
    el.addEventListener('dragenter', (e) => {
      if (!isFiles(e)) return;
      e.preventDefault(); depth += 1; el.classList.add('drag');
    });
    el.addEventListener('dragover', (e) => {
      if (!isFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    el.addEventListener('dragleave', () => {
      depth = Math.max(0, depth - 1);
      if (!depth) el.classList.remove('drag');
    });
    el.addEventListener('drop', (e) => {
      if (!isFiles(e)) return;
      e.preventDefault(); depth = 0; el.classList.remove('drag');
      const files = Array.prototype.slice.call(e.dataTransfer.files || []);
      if (files.length) onFiles(files);
    });
  }

  function pickResume(file) {
    if (!file) return;
    if (!DOC_EXT.test(file.name || '')) {
      setResumeMeta('只支持 .docx / .pdf / .txt / .md。老式 .doc 请在 Word 里另存为 .docx，或把正文粘到下面。', 'bad');
      return;
    }
    if (file.size > S.uploadMaxBytes) {
      setResumeMeta(`文件 ${mb(file.size)}，超过 ${mb(S.uploadMaxBytes)} 上限——检查一下是不是选错了。`, 'bad');
      return;
    }
    setResumeMeta('正在读取…');
    fileToBase64(file).then(async (b64) => {
      try {
        const res = await fetch('/api/resume/parse', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: file.name, data: b64 }),
        });
        const d = await res.json();
        if (!d.ok) {
          setResumeMeta(d.hint || '读不出来', 'bad');
          tip(d.hint || '简历读取失败');
          return;
        }
        S.resumeText = d.text || '';
        S.resumeName = d.name || file.name;
        resumePaste.value = S.resumeText;
        try { resumePaste.scrollTop = 0; } catch (e) {}   // 让用户先看到开头，不是结尾
        setResumeMeta(`已读 ${S.resumeName} · ${d.chars} 字 · 不写入会话记录；提问时会随提示词发给模型`, 'ok');
      } catch (e) {
        setResumeMeta(netHint(e, '读取失败，换一个文件，或直接把文字粘到下面。'), 'bad');
      }
    }).catch((e) => setResumeMeta(netHint(e, '读取失败，换一个文件，或直接把文字粘到下面。'), 'bad'));
  }

  /* ---- 作品集：一份不够，作品集天然是多份文件 ---- */

  function pfTotal() {
    return S.portfolio.reduce((n, f) => n + f.chars, 0);
  }

  // 合并成一段给后端。每份前面加标记：后端按标记分配提示词字数，
  // 不会因为第一份太长就把后面几份全挤掉。
  function buildPortfolio() {
    const parts = S.portfolio.map(
      (f, i) => `【作品集${i + 1}：${f.name}】\n${f.text}`);
    const extra = (pfPaste.value || '').trim();
    if (extra) parts.push(`【作品集补充说明】\n${extra}`);
    S.pfText = parts.join('\n\n');
    S.pfName = S.portfolio.length
      ? S.portfolio.map((f) => f.name).join('、').slice(0, 200)
      : (extra ? '粘贴的作品集说明' : '');
  }

  function paintPortfolio() {
    pfList.innerHTML = '';
    pfList.hidden = S.portfolio.length === 0;
    S.portfolio.forEach((f, i) => {
      const row = document.createElement('div');
      row.className = 'pf-item';
      const name = document.createElement('span');
      name.className = 'pf-name';
      name.textContent = f.name;
      name.title = f.name;
      const n = document.createElement('span');
      n.className = 'pf-n';
      n.textContent = `${(f.chars || 0).toLocaleString()} 字`;
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'pf-x';
      x.title = '移除这份';
      x.textContent = '×';
      x.addEventListener('click', () => {
        S.portfolio.splice(i, 1);
        buildPortfolio();
        paintPortfolio();
      });
      row.appendChild(name);
      row.appendChild(n);
      row.appendChild(x);
      pfList.appendChild(row);
    });

    const chars = pfTotal() + (pfPaste.value || '').trim().length;
    if (!chars) {
      pfMeta.textContent = '可选，最多 6 份，也能直接拖进来。加了它，面试官会挑里面的东西问。';
      pfMeta.className = 'resume-meta';
      return;
    }
    const n = S.portfolio.length;
    pfMeta.textContent =
      (n ? `共 ${n} 份 · ` : '粘贴内容 · ') + `${chars.toLocaleString()} 字 · 面试官看前 ${S.pfLimit} 字`
      + (n > 1 ? '（每份都留开头）' : '') + ' · 不写入会话记录';
    pfMeta.className = 'resume-meta ok';
  }

  async function addPortfolioFiles(files) {
    const picked = Array.from(files || []);
    if (!picked.length) return;
    const room = S.pfMaxFiles - S.portfolio.length;
    if (room <= 0) {
      tip(`作品集最多 ${S.pfMaxFiles} 份，先移掉一份再加。`);
      return;
    }
    const batch = picked.slice(0, room);
    if (picked.length > room) tip(`最多 ${S.pfMaxFiles} 份，这次只加了前 ${room} 份。`);

    let failed = 0, lastHint = '';
    pfMeta.textContent = '正在读取…';
    pfMeta.className = 'resume-meta';
    // 逐个解析，不并发：文件不多，串行能保证列表顺序和选择顺序一致
    for (const file of batch) {
      if (!DOC_EXT.test(file.name || '')) {
        failed += 1; lastHint = `${file.name} 不是 .docx / .pdf / .txt / .md`;
        continue;
      }
      if (file.size > S.uploadMaxBytes) {
        failed += 1; lastHint = `${file.name} ${mb(file.size)}，超过 ${mb(S.uploadMaxBytes)}`;
        continue;
      }
      try {
        const b64 = await fileToBase64(file);
        const res = await fetch('/api/resume/parse', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: file.name, data: b64, kind: 'portfolio' }),
        });
        const d = await res.json();
        if (!d.ok) {
          failed += 1; lastHint = d.hint || '读不出来';
          continue;
        }
        // 同名视作替换：重新选一次同一个文件不该出现两行
        S.portfolio = S.portfolio.filter((x) => x.name !== (d.name || file.name));
        S.portfolio.push({
          name: d.name || file.name, chars: d.chars || 0, text: d.text || '',
        });
      } catch (e) {
        failed += 1;
        // 这里以前一律写「读取失败」，结果是：服务已经关掉时，用户看到"读不出来"，
        // 会以为是这份 PDF 有问题，反复换文件。网络错误和解析错误必须分开说。
        lastHint = netHint(e, '读不出来');
      }
    }
    buildPortfolio();
    paintPortfolio();
    if (failed) {
      pfMeta.textContent = `${failed} 份没读出来：${lastHint}（其余已加上）`;
      pfMeta.className = 'resume-meta bad';
      tip(`作品集有 ${failed} 份没读出来：${lastHint}`);
    }
  }


  // ---- 岗位 / 面试方式 ----
  function roleLabel() {
    return (S.roleTitle || ROLE_LABEL[S.role] || ROLE_LABEL.general).trim();
  }

  function paintRole() {
    const el = $('emptyRole');
    if (el) el.textContent = roleLabel();
    roleInput.classList.toggle('on', !!S.roleTitle);
    // 只留一个「通用岗位」：写了岗位它就该灭，删空了再亮回来
    roleSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', !S.roleTitle));
  }

  /* ---- 历史岗位：写过的不用再写一遍 ---- */

  function readRoleHistory() {
    try {
      const arr = JSON.parse(localStorage.getItem(ROLE_HIST_KEY) || '[]');
      if (!Array.isArray(arr)) return [];
      return arr.filter((x) => typeof x === 'string' && x.trim()).slice(0, ROLE_HIST_MAX);
    } catch (e) {
      return [];
    }
  }

  function writeRoleHistory(list) {
    try { localStorage.setItem(ROLE_HIST_KEY, JSON.stringify(list.slice(0, ROLE_HIST_MAX))); } catch (e) {}
  }

  // 只在真的开了一场之后记，避免把打错一半的字也存进去
  function rememberRole(name) {
    const n = (name || '').trim().slice(0, 16);
    if (!n || n === ROLE_LABEL.general) return;   // 通用一直都在，占位置没意义
    const list = [n, ...readRoleHistory().filter((x) => x.toLowerCase() !== n.toLowerCase())];
    writeRoleHistory(list);
    paintRoleHistory();
  }

  function paintRoleHistory() {
    const list = readRoleHistory();
    roleHist.textContent = '';
    roleHistBtn.hidden = list.length === 0;      // 没历史就不摆一个死按钮
    roleHistBtn.textContent = `历史 ${list.length}`;
    if (!list.length) {
      const s = document.createElement('span');
      s.className = 'none';
      s.textContent = '开过一场之后，写过的岗位会记在这里';
      roleHist.appendChild(s);
      return;
    }
    list.forEach((name) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = name;
      b.title = name;
      b.addEventListener('click', () => {
        roleInput.value = name;
        S.roleTitle = name;
        paintRole();
        toggleRoleHistory(false);
      });
      roleHist.appendChild(b);
    });
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'clear';
    clear.textContent = '清空';
    clear.addEventListener('click', () => {
      writeRoleHistory([]);
      paintRoleHistory();
    });
    roleHist.appendChild(clear);
  }

  function toggleRoleHistory(show) {
    const next = show === undefined ? roleHist.hidden : show;
    roleHist.hidden = !next;
    roleHistBtn.classList.toggle('on', next);
  }

  // 体验式 / 正式：只有正式才展开简历和作品集。
  // 这里不再是"按简历提问"的开关——那样会让人以为岗位选择没用，
  // 两档本来就该是两种面试：随手练 vs 拿简历真打。
  function applyForm() {
    const formal = S.form === 'formal';
    resumeBox.hidden = !formal;
    formHint.textContent = formal
      ? '读简历和作品集，从项目里挑着问'
      : '不带材料，按岗位通用题问，随手练';
    formHint.classList.toggle('on', formal);
    if (formal && !S.resumeText) {
      setTimeout(() => { try { resumePaste.focus(); } catch (e) {} }, 60);
    }
  }
  async function startSession() {
    if (S.busy) return;
    if (S.form === 'formal') {
      const text = (resumePaste.value || '').trim();
      if (text.length < 60) {
        tip('正式面试要读简历：先选一个 .docx / .pdf，或把简历文字粘上去。'
            + '只想随手练，就切回「体验式」。');
        return;
      }
      S.resumeText = text;
      S.resumeName = S.resumeName || '粘贴的简历';
      buildPortfolio();        // 粘贴框可能改过，开局前统一合并一次
    }
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
      await stream('/api/session/start', {
        role: S.roleTitle || S.role, rounds: S.rounds, level: S.level, form: S.form,
        resume_text: S.form === 'formal' ? S.resumeText : '',
        resume_name: S.form === 'formal' ? S.resumeName : '',
        portfolio_text: S.form === 'formal' ? S.pfText : '',
        portfolio_name: S.form === 'formal' ? S.pfName : '',
        portfolio_files: S.form === 'formal' ? S.portfolio.length : 0,
      }, (ev) => {
        if (ev.t === 'session') {
          S.session = ev.id;
          rememberRole(S.roleTitle);      // 真开了一场才记，打错一半的字不留
          S.mock = ev.mode === 'mock';
          S.stats.maxQ = ev.max_question;
          S.resumeMode = !!ev.resume;
          input.disabled = false;        // 开了局就能打字，不用先关麦克风
          paintStats();
          paintMic();
          // 开场白也要能被打断：不在这里开麦，等它念完才开，
          // 用户在开场白期间说的话就全丢了（"第一句前半段没录进去"）
          if (S.voice) startRec();
          const pfLine = ev.portfolio
            ? ` + 作品集 ${ev.portfolio_files} 份 ${ev.portfolio_chars} 字`
            : '';
          tip(S.mock
            ? '演示模式：没有检测到模型 Key，面试官由本地脚本扮演。配好 <b>DEEPSEEK_API_KEY</b> 后重启即可换成真模型。'
            : (S.form === 'formal'
              ? `正式面试 · 已读简历 ${ev.resume_chars} 字${pfLine} · 面试官会从材料里挑项目提问`
              : `体验式 · ${ev.role_name || roleLabel()} · ${S.rounds} 个话题 · 一场约 ¥0.03`));
        } else {
          handleEvent(ev);
        }
      });
    } catch (e) {
      setStatus('开局失败', '');
      const m = String((e && e.message) || '');
      tip(`<b>${/^HTTP|fetch|NetworkError/i.test(m) ? '连不上本地服务：' + m : m}</b>`);
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

    const mat = m.material || {};
    const matText = mat.portfolio_files
      ? `简历 ${mat.resume_chars || 0} 字 + 作品集 ${mat.portfolio_files} 份 ${mat.portfolio_chars || 0} 字`
      : (mat.resume_chars ? `简历 ${mat.resume_chars} 字` : '无（按岗位通用题）');

    const rows = [
      ['时长', (m.duration_s || 0) + ' 秒'],
      ['面试方式', m.form_name || '—'],
      ['参考材料', matText],
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
      <h2><span id="emptyRole"></span>，直接开口。</h2>
      <p>面试官一次只问一个问题：你答得含糊它追问，答得漂亮它换题，你随时可以打断它。结束后给你一份复盘和一组数据。</p>
      <div class="empty-tips"><span>8 个话题约 10 分钟</span><span>一场约 ¥0.03</span><span>不需要注册</span></div>`;
    chat.appendChild(box);
    paintRole();                    // 岗位名用 textContent 填，不拼进 HTML
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
      if (seg.id === 'roundSeg') S.rounds = parseInt(btn.dataset.v, 10);
      if (seg.id === 'levelSeg') S.level = btn.dataset.v;
      if (seg.id === 'formSeg') {
        S.form = btn.dataset.v;
        applyForm();
      }
    });
  });

  // 「通用岗位」是唯一剩下的预设：点它就把手写的清掉，退回通用题
  roleSeg.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-v]');
    if (!btn) return;
    S.role = btn.dataset.v;
    S.roleTitle = '';
    roleInput.value = '';
    paintRole();
  });

  // 自己写岗位：写什么就按什么问。一打字「通用」就灭，删空再亮回来。
  roleInput.addEventListener('input', () => {
    S.roleTitle = roleInput.value.trim();
    paintRole();
    if (S.roleTitle) toggleRoleHistory(false);
  });

  roleInput.addEventListener('focus', () => {
    if (readRoleHistory().length) toggleRoleHistory(true);
  });

  roleInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') toggleRoleHistory(false);
    if (e.key === 'Enter') { toggleRoleHistory(false); startBtn.click(); }
  });

  roleHistBtn.addEventListener('click', () => toggleRoleHistory());

  // 点到别处就收起，别占着侧栏
  document.addEventListener('click', (e) => {
    if (roleHist.hidden) return;
    if (roleHist.contains(e.target) || roleHistBtn.contains(e.target) || roleInput.contains(e.target)) return;
    toggleRoleHistory(false);
  });

  $('voiceSwitch').addEventListener('click', () => {
    const sw = $('voiceSwitch');
    S.voice = !sw.classList.contains('on');
    S.tts = S.voice;                    // 手动关掉就是真不想听了
    sw.classList.toggle('on', S.voice);
    $('voiceHint').textContent = S.voice ? '它会念，你说完自动接话' : '只打字，不播音';

    if (!S.voice) {
      // 手动关语音：把排队的自动重连也停掉，别在后台偷偷开麦
      clearTimeout(S.timer.asrRetry);
      clearTimeout(S.timer.asrProbe);
      return;
    }

    // 重新打开语音 = 一次真正的重连：换新识别器、解除暂停、给结果反馈。
    // 原来只把 asrDown 清成 false，既没换对象也没提示，用户只会觉得"开关点了没用"。
    retryAsr();
  });

  startBtn.addEventListener('click', startSession);
  endBtn.addEventListener('click', endEarly);

  resumeFile.addEventListener('change', (e) => pickResume(e.target.files && e.target.files[0]));
  dropZone(resumeDrop, (files) => {
    if (files.length > 1) tip(`简历只读一份，先用 ${files[0].name}；剩下的可以放进作品集。`);
    pickResume(files[0]);
  });
  dropZone(pfBox, (files) => addPortfolioFiles(files));

  // 拖到投放区之外时，浏览器会开个新标签页打开那个文件，整页就没了。
  // 只拦「拖的是文件」的情况，页面里拖选文字不受影响
  const fileDrag = (e) => !!e.dataTransfer
    && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0;
  document.addEventListener('dragover', (e) => { if (fileDrag(e)) e.preventDefault(); });
  document.addEventListener('drop', (e) => {
    if (!fileDrag(e)) return;
    e.preventDefault();
    const t = e.target;
    const inside = t && t.closest && (t.closest('#resumeDrop') || t.closest('#pfBox'));
    // 落在空白处文件会「静默消失」，说一句，免得以为已经传上去了
    if (!inside && S.form === 'formal') tip('要拖到左边「选简历文件」或「作品集」那一块才会读。');
  });
  resumePaste.addEventListener('input', () => {
    S.resumeText = (resumePaste.value || '').trim();
    if (S.resumeText) {
      if (!S.resumeName) S.resumeName = '粘贴的简历';
      setResumeMeta(`${S.resumeText.length} 字 · 不写入会话记录；提问时会随提示词发给模型`, 'ok');
    } else {
      S.resumeName = '';
      setResumeMeta('还没读到内容', '');
    }
  });

  /* 作品集：＋文件 走同一个解析接口，只是传 kind=portfolio（读不出时提示说「作品集」） */
  pfAddBtn.addEventListener('click', () => pfFile.click());
  pfFile.addEventListener('change', (e) => {
    addPortfolioFiles(e.target.files);
    e.target.value = '';            // 清空，才能再选同一个文件
  });
  pfPasteBtn.addEventListener('click', () => {
    const show = pfPaste.hidden;
    pfPaste.hidden = !show;
    pfPasteBtn.textContent = show ? '收起' : '粘贴';
    if (show) setTimeout(() => { try { pfPaste.focus(); } catch (e) {} }, 40);
  });
  pfPaste.addEventListener('input', () => {
    buildPortfolio();
    paintPortfolio();
  });

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
    // 暂停 / 识别判死时，点麦克风的意思都是"我现在想用嘴说"——
    // 直接走重连（内部会解除暂停），而不是默默什么都不做。
    if (S.paused || S.asrDown) { retryAsr(); return; }
    if (S.speaking) { interruptNow(''); return; }   // 它在说：照旧打断，但不算一次"插话"
    if (S.listening) {
      // 开着麦却连续 5 秒一个识别事件都没有 = 引擎假活（心跳每 12 秒才救一次，
      // 用户等不了）。这时候点麦克风的意思是"我还要接着说"，不是"关掉"——
      // 直接换个新识别器，别让第一下变成关麦、白点一次。
      // 已经识别到的半句不清（retryAsr 不动 finalText）。
      if (performance.now() - (S.lastAsrAt || 0) > 5000) { retryAsr(); return; }
      stopRec(); setStatus('麦克风关了 · 可以打字', ''); refreshStatus(); return;
    }
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
      // 作品集能进提示词多少字、最多几份，都跟随后端配置，前端不写死
      if (h.portfolio_chars) S.pfLimit = h.portfolio_chars;
      if (h.portfolio_max_files) S.pfMaxFiles = h.portfolio_max_files;
      if (h.upload_max_bytes) S.uploadMaxBytes = h.upload_max_bytes;
      paintPortfolio();
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
  paintRole();       // 空状态标题跟着岗位走，选了「数据分析师」就显示它
  paintRoleHistory();
  paintPortfolio();
  applyForm();       // 体验式默认不展开简历区
})();
