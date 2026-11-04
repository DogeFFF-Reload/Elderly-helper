// 三块屏共用：轮询 /api/poll；发送统一 UTF-8 JSON
const Bus = (() => {
  let since = 0;
  let client = "";   // 老人端 "elder" / 家属端 "family:<id>"：主脑据此知道页面开着（通话要用）
  const listeners = [];
  // 公网钥匙：家属端链接里带 ?key=，记在本机，之后每个请求都带上（局域网用不着，没有就不带）
  let key = "";
  try {
    key = new URLSearchParams(location.search).get("key") || localStorage.getItem("cm-key") || "";
    if (key) localStorage.setItem("cm-key", key);
  } catch (e) {}
  const headers = (extra) => Object.assign(key ? { "X-CareMate-Token": key } : {}, extra || {});
  const withKey = (url) => (key ? url + (url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(key) : url);

  async function post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json; charset=utf-8" }),
      body: JSON.stringify(body || {}),
    });
    return r.json();
  }

  async function tick() {
    let payload;
    try {
      const r = await fetch("/api/poll?since=" + since + (client ? "&client=" + encodeURIComponent(client) : ""),
                            { cache: "no-store", headers: headers() });
      const data = await r.json();
      if (r.status === 401 || r.status === 403) throw Object.assign(new Error(data.error || "denied"), { denied: data.error });
      // 总线重启（id 回退）或收到 reset 事件 → 各屏清空本地时间线
      let reset = data.last_id < since;
      if (reset) since = 0;
      for (const ev of data.events) {
        if (ev.type === "reset") reset = true;
        since = Math.max(since, ev.id);
      }
      payload = { state: data.state, events: data.events, reset, online: true };
    } catch (e) {
      payload = { state: null, events: [], reset: false, online: false, denied: e.denied || null };
    }
    for (const fn of listeners) {
      try { fn(payload); } catch (e) { console.error(e); }
    }
    setTimeout(tick, 400);
  }

  return {
    post,
    voice: (text) => post("/mock/voice", { text }),
    on: (fn) => listeners.push(fn),
    setClient: (name) => { client = name || ""; },
    headers,
    withKey,
    hasKey: () => !!key,
    start: (opts) => { if (opts && opts.client) client = opts.client; tick(); },
  };
})();

const UI = {
  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  time(ts, withSec = true) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + (withSec ? ":" + p(d.getSeconds()) : "");
  },
  secs(x) {
    return x == null ? "" : Math.ceil(x) + "s";
  },
  armStatusKey(s) {
    return [s.estop, s.arm?.mock, s.arm?.online,
      ...["1", "2"].map((id) => [s.hands?.[id]?.pose, s.hands?.[id]?.status])];
  },
  actionStatus(s, hand, pose, label) {
    if (s.estop) return label + "：急停，未执行";
    const h = s.hands?.[String(hand)];
    if (!h || h.pose !== pose) return label + "：暂无当前动作回执";
    if (["offline", "rejected", "stale", "estop", "failed"].includes(h.status)) {
      return label + "：未执行或执行失败，请现场检查";
    }
    if (s.arm?.mock === true) return label + "：模拟动作";
    if (h.status === "ok") return label + "：执行端已接收，完成情况待确认";
    return label + "：等待执行端回执";
  },
  // 事件图标：大屏和操作台共用
  icon(ev) {
    return ({
      fall: "⚠️", ask: "❓", ok: "✅", sos: "🚨", notify: "📨", family_ack: "💚", dial: "📞",
      health: "❤️", deliver: "💊", teach: "📖", message: "✉️", echo: "💬", exercise: "🖐️",
      blocked: "⛔", agent: "🤖", night: "🌙", arm: "🦾", heard: "🗣️", say: "🔊", button: "🟢",
      reset: "↺", system: "⚙️", estop: "🛑", med: "⏰", call: "📞", voice: "🎙️",
    })[ev.type] || "•";
  },
  contactName(state, id) {
    const c = state && state.contacts.find((x) => x.id === id);
    return c ? c.name : id;
  },
};
