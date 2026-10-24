// Run the real page scripts, replacing only browser/device boundaries.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");

function page(name) {
  const elements = new Map();
  function el(id) {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: "", textContent: "", children: [], dataset: {}, style: {}, value: "", hidden: false,
      classList: {toggle() {}, remove() {}, add() {}},
      querySelectorAll() { return []; }, querySelector() { return el(id + ":span"); },
      setAttribute() {}, getContext() { return {}; }, focus() {}, addEventListener() {},
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    console, URLSearchParams, location: {search: ""}, localStorage: {getItem() {return null;}, setItem() {}},
    document: {getElementById: el, documentElement: {}, body: el("body"), addEventListener() {}},
    window: {devicePixelRatio: 1}, requestAnimationFrame() {}, setInterval() {}, setTimeout() {},
    getComputedStyle() {return {getPropertyValue() {return "";}};},
    fetch() { throw Error("network must not be used in this test"); },
  });
  vm.runInContext(fs.readFileSync(path.join(root, "ui/shared/bus.js"), "utf8"), context);
  vm.runInContext("Bus.start = () => {}; Bus.on = (fn) => { globalThis.poll = fn; };", context);
  const html = fs.readFileSync(path.join(root, "ui", name, "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  for (const script of scripts) vm.runInContext(script, context, {filename: name + ".js"});
  return {context, el, run: (script) => vm.runInContext(script, context)};
}

const family = page("family");
family.run(`globalThis.s = {
  elder_title: "妈妈", contacts: [{id:"daughter", name:"女儿", pron:"她"}],
  guard:{state:"SOS", notified:["daughter"], room:"客厅", wait:5, acked_by:null, ack_remaining:30},
  hands:{1:{pose:"press_sos", status:"sent"}, 2:{pose:"gentle_hold", status:"sent"}},
  arm:{mock:false, online:true}, estop:false, notifications:[], messages:[], echo:{},
  health:{today:{bp:[], date:"2026-10-24", high_count:0, water_cups:0, summary:"", note:""}},
  med:{enabled:false, times:[], doses:[], utc_offset:8, missed_after:120}
}; renderAlert(s);`);
assert.match(family.el("alert").innerHTML, /等待执行端回执/);
assert.doesNotMatch(family.el("alert").innerHTML, /已按下SOS|已按下 SOS|真机已执行/);
family.run('s.elder_title="爸爸"; s.guard.wait=8; renderAlert(s);');
assert.match(family.el("alert").innerHTML, /爸爸/);
assert.match(family.el("alert").innerHTML, /8 秒/);
family.run('s.hands[1].status = "failed"; renderAlert(s);');
assert.match(family.el("alert").innerHTML, /执行失败/);
family.run('s.hands[1].status = "ok"; renderAlert(s);');
assert.match(family.el("alert").innerHTML, /执行端已接收，完成情况待确认/);
family.run('s.arm.mock = true; renderAlert(s);');
assert.match(family.el("alert").innerHTML, /模拟动作/);
family.run('s.estop = true; renderAlert(s);');
assert.match(family.el("alert").innerHTML, /急停，未执行/);
family.run('s.estop = false; s.hands[1].pose = "home"; renderAlert(s);');
assert.match(family.el("alert").innerHTML, /暂无当前动作回执/);
family.run('s.guard.state="HELP_COMING"; s.guard.acked_by="daughter"; renderAlert(s);');
assert.match(family.el("alert").innerHTML, /显示请求/);

// Use the actual poll callback: a med-only change used to be lost by the render cache.
family.run('s.guard.state="IDLE"; poll({state:s, online:true});');
assert.equal(family.el("med").hidden, true);
family.run('s.med.enabled=true; poll({state:s, online:true});');
assert.equal(family.el("med").hidden, false);
family.run('s.med.doses=[{slot:"20:00",name:"药",state:"taken",taken_at:"20:01"}]; poll({state:s, online:true});');
assert.match(family.el("med").innerHTML, /已服 20:01/);

const monitor = page("monitor");
monitor.context.s = JSON.parse(family.run('JSON.stringify(s)'));
monitor.run(`globalThis.items=[]; patch=(container, entries)=>{items=entries;};
  s.guard.state="SOS"; s.estop=true; s.exercise={active:false}; s.med.pending=null; overlay(s);`);
let banner = monitor.run('items.find(i=>i.key==="sos").html');
assert.match(banner, /家属演示页面已记录/);
assert.match(banner, /急停，未执行/);
assert.match(banner, /未接入外部通知/);
assert.doesNotMatch(banner, /已按下 SOS/);
monitor.run('caption("testCap", "SOS", "ok");');
assert.match(monitor.el("testCap").innerHTML, /完成情况待确认/);
monitor.run('s.estop=false; s.arm.mock=false; twinStatus(s);');
assert.match(monitor.el("twinLink:span").textContent, /完成待现场确认/);
assert.doesNotMatch(monitor.el("twinLink:span").textContent, /真机同步/);
process.stdout.write("UI safety cases passed: receipts, failures, mock, estop, stale pose, med cache, monitor\n");
