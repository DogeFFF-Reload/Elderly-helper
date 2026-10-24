// 三块屏共用：轮询 /api/poll；发送统一 UTF-8 JSON
const Bus = (() => {
  let since = 0;
  const listeners = [];

  async function post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body || {}),
    });
    return r.json();
  }

  async function tick() {
    let payload;
    try {
      const r = await fetch("/api/poll?since=" + since, { cache: "no-store" });
      const data = await r.json();
      // 总线重启（id 回退）或收到 reset 事件 → 各屏清空本地时间线
      let reset = data.last_id < since;
      if (reset) since = 0;
      for (const ev of data.events) {
        if (ev.type === "reset") reset = true;
        since = Math.max(since, ev.id);
      }
      payload = { state: data.state, events: data.events, reset, online: true };
    } catch (e) {
      payload = { state: null, events: [], reset: false, online: false };
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
    start: () => tick(),
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
  // 事件图标：大屏和操作台共用
  icon(ev) {
    return ({
      fall: "⚠️", ask: "❓", ok: "✅", sos: "🚨", notify: "📨", family_ack: "💚", dial: "📞",
      health: "❤️", deliver: "💊", teach: "📖", message: "✉️", echo: "💬", exercise: "🖐️",
      blocked: "⛔", agent: "🤖", night: "🌙", arm: "🦾", heard: "🗣️", say: "🔊", button: "🟢",
      reset: "↺", system: "⚙️", estop: "🛑", med: "⏰",
    })[ev.type] || "•";
  },
  contactName(state, id) {
    const c = state && state.contacts.find((x) => x.id === id);
    return c ? c.name : id;
  },
};
