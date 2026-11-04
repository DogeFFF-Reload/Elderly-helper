// 老人端 / 家属端共用：语音通话（WebRTC，主脑只转信令）、按住说话录语音留言（WAV 16k）、铃声、留言播放、朗读。
// 依赖 bus.js（Bus.post）。约定见 code/agent/comms.py。
const Comms = (() => {
  // 同一个 Wi-Fi 直连；家属手机在 4G 上时靠 STUN 找到对方的公网地址（国内能连的几个，连不上就只用局域网地址）
  const RTC = { iceServers: [{ urls: ["stun:stun.cloudflare.com:3478", "stun:stun.miwifi.com:3478", "stun:stun.qq.com:3478"] }] };
  const ICE_WAIT = 1500;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const getJSON = (url) => fetch(url, { cache: "no-store", headers: Bus.headers() }).then((r) => r.json());

  // ---------- 声音：音频上下文、铃声、提示音 ----------
  let ctx = null;
  function audio() {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }
  const unlocked = () => !!ctx && ctx.state === "running";

  function tone(freq, t, dur, gain, type = "sine") {
    const c = audio(), o = c.createOscillator(), g = c.createGain();
    o.type = type;
    o.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(c.destination);
    o.start(t);
    o.stop(t + dur + 0.05);
  }
  const PATTERNS = {
    gentle: { every: 2600, play: (t) => [659, 831, 988, 1319].forEach((f, i) => tone(f, t + i * 0.15, 1.1, 0.2)) },
    phone: { every: 2200, play: (t) => { [0, 0.42].forEach((d) => [523, 784].forEach((f, i) => tone(f, t + d + i * 0.12, 0.5, 0.16, "triangle"))); } },
    urgent: { every: 1000, play: (t) => { tone(988, t, 0.28, 0.24, "triangle"); tone(740, t + 0.34, 0.28, 0.24, "triangle"); } },
  };
  const CHIMES = {
    message: (t) => { tone(880, t, 0.35, 0.16); tone(1319, t + 0.12, 0.5, 0.14); },
    sent: (t) => { tone(784, t, 0.25, 0.12); tone(1175, t + 0.1, 0.4, 0.12); },
    end: (t) => { tone(659, t, 0.3, 0.12); tone(494, t + 0.16, 0.45, 0.12); },
    alert: (t) => { [0, 0.3, 0.6].forEach((d) => tone(1046, t + d, 0.22, 0.22, "square")); },
  };
  function chime(name) { try { CHIMES[name](audio().currentTime + 0.02); } catch (e) {} }

  function ringer() {
    let timer = null, kind = null;
    return {
      start(k) {
        if (kind === k) return;
        this.stop();
        kind = k;
        const p = PATTERNS[k];
        const play = () => { try { p.play(audio().currentTime + 0.02); } catch (e) {} };
        play();
        timer = setInterval(play, p.every);
        if (navigator.vibrate) navigator.vibrate([400, 200, 400]);
      },
      stop() { clearInterval(timer); timer = null; kind = null; },
    };
  }

  // 电平（0~1）：麦克风 / 对方声音的光圈
  function meter(stream) {
    try {
      const c = audio(), an = c.createAnalyser();
      an.fftSize = 512;
      c.createMediaStreamSource(stream).connect(an);
      const buf = new Float32Array(an.fftSize);
      return () => {
        an.getFloatTimeDomainData(buf);
        let s = 0;
        for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
        return Math.min(1, Math.sqrt(s / buf.length) * 5);
      };
    } catch (e) { return () => 0; }
  }

  // ---------- 麦克风 / 听筒选择（同一台笔记本演示两端时，家属端选耳机，避免啸叫） ----------
  let scope = "default";
  let prefs = {};
  const sinks = new Set();
  function loadPrefs(name) {
    scope = name;
    try { prefs = JSON.parse(localStorage.getItem("cm-audio:" + scope) || "{}"); } catch (e) { prefs = {}; }
    applySink();
  }
  function setPrefs(p) {
    prefs = Object.assign({}, prefs, p);
    try { localStorage.setItem("cm-audio:" + scope, JSON.stringify(prefs)); } catch (e) {}
    applySink();
  }
  function applySink() {
    for (const el of sinks) if (el.setSinkId) el.setSinkId(prefs.speaker || "").catch(() => {});
  }
  function sinkable(el) { sinks.add(el); if (prefs.speaker && el.setSinkId) el.setSinkId(prefs.speaker).catch(() => {}); return el; }
  async function devices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return { mics: [], speakers: [] };
    const all = await navigator.mediaDevices.enumerateDevices();
    return { mics: all.filter((d) => d.kind === "audioinput"), speakers: all.filter((d) => d.kind === "audiooutput") };
  }
  function openMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(Object.assign(new Error("insecure"), { name: "SecurityError" }));
    }
    const audioC = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    if (prefs.mic) audioC.deviceId = { exact: prefs.mic };
    return navigator.mediaDevices.getUserMedia({ audio: audioC }).catch((e) => {
      if (prefs.mic && e.name === "OverconstrainedError") { setPrefs({ mic: "" }); return openMic(); }   // 选过的耳机拔了
      throw e;
    });
  }
  function micError(e) {
    if (!window.isSecureContext || (e && e.name === "SecurityError")) return "这个地址用不了麦克风：请用 localhost 打开";
    if (e && e.name === "NotAllowedError") return "麦克风没有授权，请在地址栏左边允许";
    if (e && e.name === "NotFoundError") return "没有找到麦克风";
    return "麦克风打不开";
  }

  // ---------- 录音：WAV 16 kHz / 16 bit / 单声道（和床头盒同一格式，X5 能直接放） ----------
  function downsample(buf, inRate, outRate) {
    if (inRate === outRate) return buf;
    const ratio = inRate / outRate, n = Math.floor(buf.length / ratio), out = new Float32Array(n);
    let pos = 0;
    for (let i = 0; i < n; i++) {
      const end = Math.min(buf.length, Math.floor((i + 1) * ratio));
      let s = 0, k = 0;
      for (let j = pos; j < end; j++) { s += buf[j]; k++; }
      out[i] = k ? s / k : 0;
      pos = end;
    }
    return out;
  }
  function encodeWav(samples, rate) {
    const v = new DataView(new ArrayBuffer(44 + samples.length * 2));
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    w(36, "data"); v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const x = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    }
    return new Blob([v.buffer], { type: "audio/wav" });
  }
  function recorder() {
    let stream = null, src = null, proc = null, chunks = [], t0 = 0, lvl = () => 0;
    return {
      async start() {
        const c = audio();
        stream = await openMic();
        src = c.createMediaStreamSource(stream);
        proc = c.createScriptProcessor(4096, 1, 1);
        chunks = [];
        proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
        src.connect(proc);
        proc.connect(c.destination);   // 要接到输出才会跑；不往输出写，听不到回声
        lvl = meter(stream);
        t0 = performance.now();
      },
      elapsed: () => (t0 ? (performance.now() - t0) / 1000 : 0),
      level: () => lvl(),
      stop(keep) {
        const dur = t0 ? (performance.now() - t0) / 1000 : 0;
        if (proc) { proc.onaudioprocess = null; proc.disconnect(); }
        if (src) src.disconnect();
        if (stream) stream.getTracks().forEach((t) => t.stop());
        const rate = ctx ? ctx.sampleRate : 48000;
        const parts = chunks;
        stream = src = proc = null; chunks = []; t0 = 0; lvl = () => 0;
        if (!keep) return null;
        const all = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
        let o = 0;
        for (const p of parts) { all.set(p, o); o += p.length; }
        return { blob: encodeWav(downsample(all, rate, 16000), 16000), dur };
      },
    };
  }

  // 按住说话（松手发送、手指移开按钮取消）；轻点一下 = “再点一下发送”模式。UI 只管按 onState 画。
  // state：idle / arming（等麦克风）/ hold / toggle / sending / sent / error
  function talkButton(btn, { onState, send, maxSecs = 60, minSecs = 0.8 }) {
    const rec = recorder();
    let state = "idle", downAt = 0, raf = 0, cancelArmed = false, pid = null, upEarly = false, timer = 0;
    const emit = (extra) => onState(state, Object.assign({ secs: rec.elapsed(), level: rec.level(), cancel: cancelArmed, max: maxSecs }, extra));
    const later = (ms) => { clearTimeout(timer); timer = setTimeout(() => { state = "idle"; emit(); }, ms); };
    function loop() {
      if (state !== "hold" && state !== "toggle") return;
      emit();
      if (rec.elapsed() >= maxSecs) finish(true);
      else raf = requestAnimationFrame(loop);
    }
    async function begin() {
      state = "arming"; upEarly = false; emit();
      try { await rec.start(); } catch (e) { state = "error"; emit({ error: micError(e) }); later(2600); return; }
      if (state !== "arming") { rec.stop(false); return; }   // 等麦克风的时候被取消了
      state = upEarly ? "toggle" : "hold";
      loop();
    }
    async function finish(keep) {
      cancelAnimationFrame(raf);
      const clip = rec.stop(keep);
      if (!clip) { state = "idle"; emit({ cancelled: true }); return; }
      if (clip.dur < minSecs) { state = "error"; emit({ error: "说话时间太短" }); later(1600); return; }
      state = "sending"; emit();
      try {
        const r = await send(clip);
        if (!r || !r.ok) throw new Error((r && r.error) || "发送失败");
        state = "sent"; emit({ result: r }); chime("sent"); later(1500);
      } catch (e) { state = "error"; emit({ error: e.message || "发送失败" }); later(2600); }
    }
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      audio();
      if (state === "toggle") { finish(true); return; }
      if (state !== "idle") return;
      pid = e.pointerId;
      try { btn.setPointerCapture(pid); } catch (err) {}
      downAt = performance.now(); cancelArmed = false;
      begin();
    });
    btn.addEventListener("pointermove", (e) => {
      if (state !== "hold" || e.pointerId !== pid) return;
      const r = btn.getBoundingClientRect();
      const far = Math.hypot(e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2)) > Math.max(r.width, r.height) * 0.85;
      if (far !== cancelArmed) { cancelArmed = far; emit(); }
    });
    const up = (e) => {
      if (e.pointerId !== pid) return;
      pid = null;
      if (state === "arming") { upEarly = true; return; }
      if (state !== "hold") return;
      if (performance.now() - downAt < 350) { state = "toggle"; emit(); return; }
      finish(!cancelArmed);
    };
    btn.addEventListener("pointerup", up);
    btn.addEventListener("pointercancel", (e) => { if (e.pointerId === pid && state === "hold") { pid = null; finish(false); } });
    btn.addEventListener("contextmenu", (e) => e.preventDefault());
    return {
      state: () => state,
      send: () => { if (state === "toggle" || state === "hold") finish(true); },
      cancel: () => {
        if (state === "arming") { state = "idle"; emit({ cancelled: true }); }
        else if (state === "hold" || state === "toggle") finish(false);
      },
    };
  }

  function sendVoice(from, to, clip) {
    const q = new URLSearchParams({ from, to, dur: clip.dur.toFixed(1) });
    return fetch("/api/voice_msg?" + q, { method: "POST", headers: Bus.headers({ "Content-Type": clip.blob.type }), body: clip.blob })
      .then((r) => r.json());
  }

  // ---------- 留言播放：全页共用一个播放器，同时只放一条 ----------
  const player = (() => {
    const el = sinkable(new Audio());
    let cur = null, onEnd = null;
    const subs = new Set();
    const emit = () => subs.forEach((f) => { try { f(cur); } catch (e) {} });
    const done = () => { const cb = onEnd, id = cur; cur = null; onEnd = null; emit(); if (cb) cb(id); };
    el.addEventListener("ended", done);
    el.addEventListener("error", () => { if (cur != null) { cur = null; onEnd = null; emit(); } });
    return {
      async play(id, opts = {}) {
        if (cur === id) { el.pause(); cur = null; onEnd = null; emit(); return false; }
        speechStop();
        el.pause();
        el.src = Bus.withKey("/api/voice_msg?id=" + encodeURIComponent(id));   // <audio> 带不了请求头，钥匙放 URL 里
        cur = id; onEnd = opts.onEnd || null; emit();
        try { await el.play(); return true; } catch (e) { cur = null; onEnd = null; emit(); throw e; }
      },
      stop() { el.pause(); cur = null; onEnd = null; emit(); },
      current: () => cur,
      position: () => el.currentTime || 0,
      on: (f) => subs.add(f),
    };
  })();

  // ---------- 朗读（喇叭 tts_player 不在线时，页面自己念） ----------
  let zhVoice = null;
  function pickVoice() {
    if (!window.speechSynthesis) return null;
    const vs = speechSynthesis.getVoices();
    zhVoice = vs.find((v) => /zh[-_]CN/i.test(v.lang) && /Huihui|Xiaoxiao|Yaoyao|Tingting/i.test(v.name))
      || vs.find((v) => /zh[-_]CN/i.test(v.lang)) || vs.find((v) => /^zh/i.test(v.lang)) || null;
    return zhVoice;
  }
  if (window.speechSynthesis) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }
  function speak(text, { rate = 0.92 } = {}) {
    return new Promise((resolve) => {
      if (!window.speechSynthesis) return resolve(false);
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = "zh-CN"; u.rate = rate;
      if (zhVoice || pickVoice()) u.voice = zhVoice;
      u.onend = () => resolve(true);
      u.onerror = () => resolve(false);
      speechSynthesis.speak(u);
    });
  }
  function speechStop() { if (window.speechSynthesis) speechSynthesis.cancel(); }

  // ---------- 语音通话 ----------
  // phase：idle / outgoing（我打出去在响）/ incoming（打给我在响）/ connecting / active / ended
  function callClient({ party, onChange }) {
    let call = null, phase = "idle", ended = null, endedId = null;
    let pc = null, local = null, busy = false, offered = false, answered = false, remoteSet = false;
    let micOk = true, muted = false, connectedAt = 0, localLvl = () => 0, remoteLvl = () => 0;
    const audioEl = sinkable(new Audio());
    audioEl.autoplay = true;
    const emit = () => { try { onChange && onChange(api); } catch (e) { console.error(e); } };
    const set = (p) => { if (phase !== p) { phase = p; emit(); } };

    function teardown() {
      if (pc) { try { pc.close(); } catch (e) {} pc = null; }
      if (local) { local.getTracks().forEach((t) => t.stop()); local = null; }
      audioEl.srcObject = null;
      localLvl = remoteLvl = () => 0;
      connectedAt = 0; busy = false;
    }
    function finish(c, reasonOverride) {
      if (!call || endedId === call.id) return;
      endedId = call.id;
      const last = Object.assign({}, call, c || {});
      const secs = connectedAt ? (Date.now() - connectedAt) / 1000 : 0;
      teardown();
      ended = { call: last, reason: reasonOverride || last.end_reason || "ended", mine: last.from === party, secs };
      set("ended");
      setTimeout(() => { if (phase === "ended") { call = null; ended = null; set("idle"); } }, 2600);
    }
    async function mic() {
      try { local = await openMic(); micOk = true; }
      catch (e) {
        micOk = false;   // 没有麦克风也接通：发静音轨，至少能听见对方
        local = audio().createMediaStreamDestination().stream;
        api.micError = micError(e);
      }
      local.getAudioTracks().forEach((t) => (t.enabled = !muted));
      localLvl = meter(local);
    }
    function newPc() {
      pc = new RTCPeerConnection(RTC);
      local.getTracks().forEach((t) => pc.addTrack(t, local));
      pc.ontrack = (e) => {
        const s = e.streams[0] || new MediaStream([e.track]);
        audioEl.srcObject = s;
        audioEl.play().catch(() => {});
        remoteLvl = meter(s);
      };
      pc.onconnectionstatechange = () => {
        if (!pc) return;
        const s = pc.connectionState;
        if (s === "connected") { connectedAt = connectedAt || Date.now(); if (call && call.state === "active") set("active"); emit(); }
        if (s === "failed") hangup("dropped");
      };
    }
    async function gathered() {
      if (pc.iceGatheringState === "complete") return;
      await new Promise((res) => {
        const t = setTimeout(res, ICE_WAIT);
        pc.addEventListener("icegatheringstatechange", () => { if (pc && pc.iceGatheringState === "complete") { clearTimeout(t); res(); } });
      });
    }
    const still = (id) => call && call.id === id && endedId !== id;
    async function makeOffer() {
      busy = true; offered = true;
      const id = call.id;
      try {
        await mic(); if (!still(id)) return;
        newPc();
        await pc.setLocalDescription(await pc.createOffer({ offerToReceiveAudio: true }));
        await gathered(); if (!still(id)) return;
        const r = await Bus.post("/api/call/offer", { id, party, sdp: pc.localDescription.sdp });
        if (!r.ok && still(id)) hangup();
      } catch (e) { console.error(e); if (still(id)) hangup(); }
      finally { busy = false; }
    }
    async function accept() {
      if (!call || call.to !== party || answered || busy) return;
      busy = true; answered = true;
      const id = call.id;
      set("connecting");
      try {
        let sdp = null;   // 主脑发起的呼叫要等对方页面补 offer，多等一会儿
        for (let i = 0; i < 40 && !sdp; i++) {
          const r = await getJSON(`/api/call/sdp?id=${id}&which=offer`);
          if (r.ok) sdp = r.sdp; else await sleep(250);
          if (!still(id)) return;
        }
        if (!sdp) throw new Error("对方还没准备好");
        await mic(); if (!still(id)) return;
        newPc();
        await pc.setRemoteDescription({ type: "offer", sdp });
        await pc.setLocalDescription(await pc.createAnswer());
        await gathered(); if (!still(id)) return;
        const r = await Bus.post("/api/call/answer", { id, party, sdp: pc.localDescription.sdp });
        if (!r.ok) throw new Error(r.error);
      } catch (e) { console.error(e); if (still(id)) hangup(); }
      finally { busy = false; }
    }
    async function applyAnswer() {
      busy = true;
      const id = call.id;
      try {
        const r = await getJSON(`/api/call/sdp?id=${id}&which=answer`);
        if (r.ok && pc && still(id)) { await pc.setRemoteDescription({ type: "answer", sdp: r.sdp }); remoteSet = true; }
      } catch (e) { console.error(e); }
      finally { busy = false; }
    }

    function update(c) {
      const mineCall = c && (c.from === party || c.to === party);
      if (!mineCall || c.state === "ended") {
        if (call && endedId !== call.id) {
          if (c && c.id === call.id) finish(c);
          else finish(null, "reset");   // 主脑那边这通已经没了（重置，或换成了别的通话）
        }
        return;
      }
      if (c.id === endedId) return;   // 我这边已经挂了，等主脑那边也结束
      if (!call || call.id !== c.id) {
        teardown();
        call = c; offered = answered = remoteSet = false; muted = false; micOk = true; api.micError = "";
      }
      call = c;
      const out = c.from === party;
      if (c.state === "ringing") {
        if (out) {
          // 页面中途刷新过：主脑那边有 offer、我这边连接没了 → 这通接不起来，挂掉
          if (c.has_offer && !offered && !pc && !busy) { hangup("dropped"); return; }
          set("outgoing");
          if (!offered && !busy) makeOffer();
        } else {
          if (phase !== "connecting") set("incoming");
          if ((c.auto_answer || c.answer_req) && c.has_offer && !answered && !busy) accept();
        }
      } else if (c.state === "active") {
        if (!out && !answered) { finish(c, "elsewhere"); return; }   // 别的页面替我接了
        if (!pc && !busy) { hangup("dropped"); return; }             // 页面中途刷新过
        if (out && c.has_answer && !remoteSet && !busy) applyAnswer();
        set(connectedAt ? "active" : "connecting");
      }
    }
    async function start(to, opts = {}) {
      if (phase !== "idle" && phase !== "ended") return { ok: false, error: "正在通话中" };
      audio();
      const r = await Bus.post("/api/call/start", { from: party, to, emergency: !!opts.emergency });
      if (r.ok) { endedId = null; update(r.call); }
      return r;
    }
    function hangup(why) {
      if (!call) return;
      const c = call;
      Bus.post("/api/call/hangup", { id: c.id, party }).catch(() => {});
      const reason = why || (c.state === "ringing" ? (c.to === party ? "declined" : "cancelled") : "ended");
      finish({ state: "ended", end_reason: reason }, reason);
    }
    function setMuted(m) { muted = m; if (local) local.getAudioTracks().forEach((t) => (t.enabled = !m)); emit(); }

    const api = {
      party, update, start, accept, hangup, setMuted, micError: "",
      get phase() { return phase; },
      get call() { return call; },
      get ended() { return ended; },
      get muted() { return muted; },
      get micOk() { return micOk; },
      get connectedAt() { return connectedAt; },
      peer() { return call ? (call.from === party ? call.to : call.from) : null; },
      peerName() { return call ? (call.from === party ? call.to_name : call.from_name) : ""; },
      levels() { return { local: muted ? 0 : localLvl(), remote: remoteLvl() }; },
      seconds() { return connectedAt ? (Date.now() - connectedAt) / 1000 : 0; },
    };
    return api;
  }

  // ---------- 小工具 ----------
  function mmss(s) { s = Math.max(0, Math.round(s || 0)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
  // 语音条的波形：按 id 生成固定的高低，同一条每次画出来一样
  function bars(seed, n) {
    let x = (Number(seed) || 1) * 9301 + 49297;
    return Array.from({ length: n }, (_, i) => {
      x = (x * 9301 + 49297) % 233280;
      const env = Math.sin(Math.PI * (i + 0.5) / n) * 0.55 + 0.45;
      return Math.round((0.25 + (x / 233280) * 0.75) * env * 100);
    });
  }

  return { audio, unlocked, chime, ringer, talkButton, sendVoice, player, speak, speechStop, callClient,
           devices, loadPrefs, setPrefs, prefs: () => prefs, openMic, micError, mmss, bars };
})();
