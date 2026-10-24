// 3D 虚拟臂：实体 = 真臂实时读数，半透明黄色 = 目标。拖手（解逆运动学，保持手掌朝向）或拖某一节（像转旋钮）来设目标，
// 目标走页面的「边拖边动」跟随模式发给 arm_service，每一帧在服务端照样查限位 / 禁区。
// 关节链、末端长度、限位都从 /teach/model 取，和 arm_kinematics / arm_safety 同一套（坐标 z 朝上，单位 m）。
import * as THREE from 'three';
import { OrbitControls } from '/static/OrbitControls.js';

const api = window.armApi;
const box = document.getElementById('arm3d');
const hud = document.getElementById('arm3d_hud');
const D = Math.PI / 180;

let model = null;
try {
  const r = await fetch('/teach/model', { cache: 'no-store' });
  model = await r.json();
  if (!model.ok) throw Error(model.error || r.status);
} catch (e) {
  box.textContent = '3D 视图加载失败：' + e.message;
  throw e;
}
const N = model.chain.length;
const lower = model.lower_deg.map((v, i) => i === 0 ? Math.max(v, -model.base_limit_deg) : v);
const upper = model.upper_deg.map((v, i) => i === 0 ? Math.min(v, model.base_limit_deg) : v);

/* ---------- 正运动学：和 arm_kinematics.frames 一样，返回每节的 4×4 世界矩阵 + 末端 ---------- */
function fk(qDeg) {
  let m = new THREE.Matrix4();
  const frames = [];
  model.chain.forEach(([, xyz, axis], i) => {
    m = m.clone().multiply(new THREE.Matrix4().makeTranslation(xyz[0], xyz[1], xyz[2]))
      .multiply(new THREE.Matrix4().makeRotationAxis(new THREE.Vector3(...axis), qDeg[i] * D));
    frames.push(m);
  });
  const tool = new THREE.Vector3(model.tool_length, 0, 0).applyMatrix4(frames[N - 1]);
  return { frames, tool, pos: frames.map(f => new THREE.Vector3().setFromMatrixPosition(f)) };
}
const clampQ = q => q.map((v, i) => Math.min(upper[i], Math.max(lower[i], v)));
// 保持手朝向（和服务端 orient_locked 一样）：J4 = J2 − J3，J5 = J6 = 0 → 手朝前、拇指朝上、手掌竖直
const orientOn = () => !!(api.state() && api.state().arm && api.state().arm.orient_lock);
const lockQ = q => { const r = q.slice(); r[3] = r[1] - r[2]; r[4] = 0; r[5] = 0; return r; };

/* ---------- 场景 ---------- */
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
box.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0f1b25);
const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 20);
camera.up.set(0, 0, 1);
camera.position.set(0.72, -0.82, 0.55);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0.12, 0, 0.22);
controls.update();
scene.add(new THREE.HemisphereLight(0xdfefff, 0x203040, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.4);
sun.position.set(1, -1, 2);
scene.add(sun);
const grid = new THREE.GridHelper(2, 20, 0x4a6578, 0x2a3e4c);
grid.rotation.x = Math.PI / 2;
scene.add(grid);
// 桌面示意：J1 负方向（y < 0）那一侧有桌子（只画出来，不做检查）
const table = new THREE.Mesh(new THREE.PlaneGeometry(1.3, 0.9),
  new THREE.MeshBasicMaterial({ color: 0xc8a66b, transparent: true, opacity: 0.16, side: THREE.DoubleSide }));
table.position.set(0.25, -0.45, 0.001);
scene.add(table);
scene.add(label('桌面（J1 负方向）', new THREE.Vector3(0.25, -0.85, 0.02), '#d6bf87'));
scene.add(label('J1 正方向', new THREE.Vector3(0.0, 0.6, 0.02), '#8fa6b4'));
(model.keep_out || []).forEach(z => {
  const s = z.max.map((v, i) => v - z.min[i]);
  const m = new THREE.Mesh(new THREE.BoxGeometry(...s),
    new THREE.MeshBasicMaterial({ color: 0xfd6978, transparent: true, opacity: 0.15 }));
  m.position.set(...z.min.map((v, i) => v + s[i] / 2));
  scene.add(m);
});

function label(text, at, color) {
  const c = document.createElement('canvas');
  c.width = 512; c.height = 96;
  const g = c.getContext('2d');
  g.font = '600 44px system-ui,sans-serif';
  g.fillStyle = color;
  g.textBaseline = 'middle';
  g.fillText(text, 8, 48);
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false, transparent: true }));
  s.scale.set(0.32, 0.06, 1);
  s.center.set(0, 0.5);
  s.position.copy(at);
  return s;
}

/* ---------- 臂的网格：每节一根圆柱 + 关节球 + 手 ---------- */
const UP = new THREE.Vector3(0, 1, 0);
function makeArm(color, opacity, pickable) {
  const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.2, transparent: opacity < 1, opacity, depthWrite: opacity >= 1 });
  const group = new THREE.Group();
  const base = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.08, 0.06, 32), mat);
  base.rotation.x = Math.PI / 2;
  base.position.z = 0.03;
  base.userData.joint = 0;
  group.add(base);
  const links = [], balls = [];
  for (let i = 0; i < N; i++) {
    const link = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 1, 20), mat);
    link.userData.joint = i;
    links.push(link); group.add(link);
    const ball = new THREE.Mesh(new THREE.SphereGeometry(i < 3 ? 0.04 : 0.032, 20, 14), mat);
    ball.userData.joint = i;
    balls.push(ball); group.add(ball);
  }
  // 手：法兰 → 转接段 → 手掌（扁盒，掌面朝 link6 的 y）+ 拇指（link6 的 +z）
  const adapter = new THREE.Mesh(new THREE.CylinderGeometry(0.028, 0.028, 1, 20), mat);
  adapter.userData.hand = true;
  const handMat = mat.clone(); handMat.color = new THREE.Color(pickable ? 0x9fd3ff : color);
  const palm = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.035, 0.085), handMat);
  palm.userData.hand = true;
  const thumb = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.022, 0.022), handMat);
  thumb.userData.hand = true;
  const tip = new THREE.Mesh(new THREE.SphereGeometry(pickable ? 0.028 : 0.018, 18, 14),
    new THREE.MeshBasicMaterial({ color: pickable ? 0xffe27a : color, transparent: true, opacity: Math.min(1, opacity + 0.2) }));
  tip.userData.hand = true;
  const halo = new THREE.Mesh(new THREE.SphereGeometry(0.05, 18, 14),
    new THREE.MeshBasicMaterial({ color: 0xffe27a, transparent: true, opacity: pickable ? 0.12 : 0, depthWrite: false }));
  halo.userData.hand = true;
  halo.visible = pickable;
  group.add(adapter, palm, thumb, tip, halo);
  scene.add(group);

  function bar(mesh, a, b, r) {
    const d = new THREE.Vector3().subVectors(b, a);
    const len = Math.max(d.length(), 1e-4);
    mesh.position.copy(a).addScaledVector(d, 0.5);
    mesh.quaternion.setFromUnitVectors(UP, d.divideScalar(len));
    mesh.scale.set(r, len, r);
  }
  function update(qDeg) {
    const f = fk(qDeg);
    for (let i = 0; i < N; i++) {
      const a = f.pos[i], b = i + 1 < N ? f.pos[i + 1] : f.pos[i];
      bar(links[i], i === 0 ? new THREE.Vector3(0, 0, 0.06) : a, b, i < 3 ? 0.03 : 0.024);
      balls[i].position.copy(a);
    }
    const flange = f.pos[N - 1];
    const R = new THREE.Matrix4().extractRotation(f.frames[N - 1]);
    const x = new THREE.Vector3(1, 0, 0).applyMatrix4(R);
    const z = new THREE.Vector3(0, 0, 1).applyMatrix4(R);
    const palmAt = flange.clone().addScaledVector(x, model.tool_length * 0.72);
    bar(adapter, flange, flange.clone().addScaledVector(x, model.tool_length * 0.42), 1);
    palm.position.copy(palmAt);
    palm.quaternion.setFromRotationMatrix(R);
    thumb.position.copy(palmAt).addScaledVector(z, 0.055).addScaledVector(x, -0.01);
    thumb.quaternion.setFromRotationMatrix(R);
    tip.position.copy(f.tool);
    halo.position.copy(f.tool);
    return f;
  }
  return { group, update, meshes: [base, ...links, ...balls, adapter, palm, thumb, tip, halo] };
}
const real = makeArm(0x75e1c1, 1, true);
const ghost = makeArm(0xf3d27a, 0.35, false);

/* ---------- 小眼睛：每个关节一圈彩色转环 + 名字，告诉你拖哪里转哪个关节 ---------- */
const JCOLORS = [0xff8a65, 0xffd54f, 0x81c784, 0x4fc3f7, 0xba68c8, 0xf06292];
const JNAMES = ['J1 底座', 'J2 肩部', 'J3 肘部', 'J4 腕部俯仰', 'J5 腕部左右', 'J6 手腕旋转'];
const helpers = new THREE.Group();
helpers.visible = false;
scene.add(helpers);
const rings = [], ringTags = [];
for (let i = 0; i < N; i++) {
  const r = i < 3 ? 0.075 : 0.055, end = Math.PI * 1.7;
  const ring = new THREE.Mesh(new THREE.TorusGeometry(r, 0.0045, 8, 48, end),
    new THREE.MeshBasicMaterial({ color: JCOLORS[i], depthTest: false, transparent: true, opacity: 0.95 }));
  const arrow = new THREE.Mesh(new THREE.ConeGeometry(0.012, 0.028, 12),
    new THREE.MeshBasicMaterial({ color: JCOLORS[i], depthTest: false }));
  arrow.position.set(r * Math.cos(end), r * Math.sin(end), 0);   // 箭头 = 这个关节转正角度的方向
  arrow.rotation.z = end;
  ring.add(arrow);
  ring.renderOrder = 10;
  rings.push(ring);
  const tag = label(JNAMES[i] + ' ↻', new THREE.Vector3(), '#' + JCOLORS[i].toString(16).padStart(6, '0'));
  tag.renderOrder = 11;
  tag.scale.multiplyScalar(0.75);
  ringTags.push(tag);
  helpers.add(ring, tag);
}
const handTag = label('拖手：手跟着鼠标走', new THREE.Vector3(), '#ffe27a');
handTag.scale.multiplyScalar(0.8);
helpers.add(handTag);
const Z = new THREE.Vector3(0, 0, 1);
function updateHelpers(qDeg) {
  const f = fk(qDeg);
  for (let i = 0; i < N; i++) {
    const R = new THREE.Matrix4().extractRotation(f.frames[i]);
    const axis = new THREE.Vector3(...model.chain[i][2]).applyMatrix4(R).normalize();
    rings[i].position.copy(f.pos[i]);
    rings[i].quaternion.setFromUnitVectors(Z, axis);   // 环面垂直于转轴
    const next = i + 1 < N ? f.pos[i + 1] : f.tool;
    ringTags[i].position.copy(f.pos[i]).lerp(next, 0.5).add(new THREE.Vector3(0, 0, 0.035));
  }
  handTag.position.copy(f.tool).add(new THREE.Vector3(-0.04, 0, 0.09));
}
const eyeBtn = document.getElementById('arm3d_eye');
function setEye(on) {
  helpers.visible = on;
  eyeBtn.classList.toggle('on', on);
  eyeBtn.title = on ? '隐藏可拖动部位提示' : '显示可拖动部位提示';
  try { localStorage.setItem('arm3d_eye', on ? '1' : '0'); } catch (_) { }
}
eyeBtn.onclick = () => setEye(!helpers.visible);
let eyeSaved = null;
try { eyeSaved = localStorage.getItem('arm3d_eye'); } catch (_) { }
setEye(eyeSaved !== '0');

/* ---------- 姿态库标记 + 脚本路径预览 ---------- */
const marks = new THREE.Group();
scene.add(marks);
let marksKey = '';
let poseMarks = [];
function refreshMarks(liveQ) {
  if (dragging && dragging.kind === 'pose') return;
  const lib = api.lib(), steps = api.steps();
  const key = JSON.stringify([lib.poses, steps, liveQ && liveQ.map(v => Math.round(v))]);
  if (key === marksKey) return;
  marksKey = key;
  marks.clear();
  poseMarks = [];
  const poses = lib.poses || {};
  Object.entries(poses).forEach(([n, p]) => {
    const t = fk(p.q_deg).tool;
    const s = new THREE.Mesh(new THREE.SphereGeometry(0.02, 18, 12),
      new THREE.MeshBasicMaterial({ color: p.locked ? 0x8a96a0 : 0xc9a2ff, transparent: true, opacity: 0.9 }));
    s.position.copy(t);
    s.userData.pose = n;
    s.userData.locked = !!p.locked;
    const tag = label((p.locked ? '🔒 ' : '') + n, t.clone().add(new THREE.Vector3(0.025, 0, 0.025)), p.locked ? '#a9b6bf' : '#c9a2ff');
    s.userData.tag = tag;
    poseMarks.push(s);
    marks.add(s, tag);
  });
  // 脚本路径：从真臂现在的位置出发，依次连姿态 / 录制点（预设动作原地做，不画）
  const pts = liveQ ? [fk(liveQ).tool] : [];
  const up = (v, cm) => v.clone().add(new THREE.Vector3(0, 0, cm / 100));
  steps.forEach(st => {
    if (st.pose !== undefined && poses[st.pose]) {
      const p = poses[st.pose], r = p.route, t = fk(p.q_deg).tool;
      if (r && r.mode === 'lift' && pts.length) pts.push(up(pts[pts.length - 1], r.cm), up(t, r.cm));   // 示意：原地抬起、到正上方
      if (r && r.mode === 'via') r.via.forEach(v => { if (poses[v]) pts.push(fk(poses[v].q_deg).tool); });
      pts.push(t);
    } else if (st.move) pts.push(fk(st.move.map(v => v / D)).tool);
  });
  if (pts.length > 1) {
    const g = new THREE.BufferGeometry().setFromPoints(pts);
    marks.add(new THREE.Line(g, new THREE.LineDashedMaterial({ color: 0xff9fd0, dashSize: 0.02, gapSize: 0.012 })).computeLineDistances());
  }
}

/* ---------- 状态 ---------- */
let live = null;            // 真臂关节角（°）
let target = null;          // 目标（°）
let dragging = null;        // {kind:'joint', j, ...} | {kind:'hand', ...}
let lastTouch = 0;
async function poll() {
  try {
    const s = await (await fetch('/status', { cache: 'no-store' })).json();
    const q = s.arm && s.arm.joints_deg;
    if (q && q.length === N && q.every(v => Math.abs(v) < 400)) {
      live = q;
      if (!dragging && Date.now() - lastTouch > 2500 && !(s.arm.busy || s.arm.queued)) target = q.slice();
    }
    hudState(s);
  } catch (e) { hud.textContent = '连接中断：' + e.message; }
  setTimeout(poll, 200);
}
poll();

function hudState(s) {
  const f = live && fk(live);
  const lines = [];
  if (!s.teaching) lines.push('⚠ 先在上面点「进入点动示教」才能拖动（现在只能旋转视角）');
  if (f) lines.push('手心 ' + ['x', 'y', 'z'].map((k, i) => k + ' ' + (f.tool.getComponent(i) * 100).toFixed(1)).join('  ') + ' cm');
  if (target && live && target.some((v, i) => Math.abs(v - live[i]) > 0.8))
    lines.push('目标 ' + target.map(v => v.toFixed(1)).join(' / ') + '°');
  if (s.arm && s.arm.last_error && s.arm.current === 'teach_servo') lines.push('✕ ' + s.arm.last_error);
  hud.textContent = lines.join('\n');
}

/* ---------- 交互 ---------- */
const ray = new THREE.Raycaster();
const ndc = new THREE.Vector2();
function pointer(e) {
  const r = renderer.domElement.getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, camera);
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}
function screenOf(v) {
  const p = v.clone().project(camera), r = renderer.domElement.getBoundingClientRect();
  return { x: (p.x + 1) / 2 * r.width, y: (1 - p.y) / 2 * r.height };
}
const modeSel = document.getElementById('arm3d_mode');

function dragPlane(point, mode) {
  let normal;
  if (mode === 'free' || mode === 'lead') normal = camera.getWorldDirection(new THREE.Vector3()).negate();
  else { const az = Math.atan2(point.y, point.x); normal = new THREE.Vector3(-Math.sin(az), Math.cos(az), 0); }
  return new THREE.Plane().setFromNormalAndCoplanarPoint(normal, point);
}
function goalOf(d, hitP) {
  const goal = d.start.clone();
  if (d.mode === 'z') goal.z = hitP.z;
  else if (d.mode === 'r') {
    const az = Math.atan2(d.start.y, d.start.x), dr = (hitP.x - d.start.x) * Math.cos(az) + (hitP.y - d.start.y) * Math.sin(az);
    goal.x += dr * Math.cos(az); goal.y += dr * Math.sin(az);
  } else goal.copy(hitP);
  if (d.depth) goal.addScaledVector(camera.getWorldDirection(new THREE.Vector3()), d.depth);   // 拖动时滚轮：前后推远拉近
  return goal;
}
let ghostOverride = null;
// 「编辑姿态位置」开着才能拖紫点；关着时紫点只显示，按在手上永远是拖手（臂停在某个姿态时紫点和手重叠）
const editBtn = document.getElementById('arm3d_editposes');
const editPoses = () => editBtn.classList.contains('on');
editBtn.onclick = () => {
  editBtn.classList.toggle('on');
  editBtn.textContent = editPoses() ? '✎ 编辑姿态位置：开（拖紫点改位置）' : '✎ 编辑姿态位置：关';
};   // 拖姿态标记时，黄色目标臂显示这个姿态（真臂不动）

renderer.domElement.addEventListener('pointerdown', e => {
  if (!live) return;
  pointer(e);
  const mh = editPoses() ? ray.intersectObjects(poseMarks, false)[0] : null;
  if (mh) {
    const n = mh.object.userData.pose;
    if (mh.object.userData.locked) {   // 按住锁住的点：不拖它，也不转视角
      document.getElementById('arm3d_tip').textContent = '🔒「' + n + '」已锁定，到姿态库里解锁才能拖';
      controls.enabled = false;
      dragging = { kind: 'locked' };
      return;
    }
    const pose = api.lib().poses[n];
    controls.enabled = false;
    renderer.domElement.setPointerCapture(e.pointerId);
    const start = mh.object.position.clone(), mode = modeSel.value;
    dragging = { kind: 'pose', name: n, mesh: mh.object, start, q0: pose.q_deg.slice(), q: null, mode, plane: dragPlane(start, mode) };
    ghostOverride = pose.q_deg.slice();
    return;
  }
  if (!api.state() || !api.state().teaching) return;
  const hit = ray.intersectObjects(real.meshes, false)[0];
  if (!hit) return;
  controls.enabled = false;
  renderer.domElement.setPointerCapture(e.pointerId);
  target = (target || live).slice();
  const f = fk(target);
  if (hit.object.userData.hand) {
    const tool = f.tool.clone();
    const mode = modeSel.value;
    dragging = { kind: 'hand', start: tool, q0: target.slice(), qPrev: null, pitch: target[1] - target[2] - target[3],
      plane: dragPlane(tool, mode), mode, depth: 0, lastHit: null };
  } else {
    const j = hit.object.userData.joint;
    if (orientOn() && j >= 3) {
      document.getElementById('arm3d_tip').textContent = '「保持手朝向」开着，J4~J6 跟着肩肘自动转；要单独转腕先关掉';
      controls.enabled = true;
      return;
    }
    const center = f.pos[j];
    const axis = new THREE.Vector3(...model.chain[j][2]).applyMatrix4(new THREE.Matrix4().extractRotation(f.frames[j])).normalize();
    const toCam = new THREE.Vector3().subVectors(camera.position, center).normalize();
    const facing = axis.dot(toCam);
    const c = screenOf(center), p = pointer(e);
    dragging = { kind: 'joint', j, c, facing, a0: Math.atan2(-(p.y - c.y), p.x - c.x), x0: p.x, q0: target[j] };
  }
  lastTouch = Date.now();
});

renderer.domElement.addEventListener('pointermove', e => {
  if (!dragging) {        // 悬停提示：指到哪一节 / 哪个姿态
    pointer(e);
    const mh = editPoses() ? ray.intersectObjects(poseMarks, false)[0] : null;
    const hit = mh || ray.intersectObjects(real.meshes, false)[0];
    renderer.domElement.style.cursor = hit ? (mh && mh.object.userData.locked ? 'not-allowed' : 'grab') : '';
    const t = mh ? (mh.object.userData.locked ? '🔒「' + mh.object.userData.pose + '」已锁定' : '拖动移动姿态「' + mh.object.userData.pose + '」（' + modeSel.selectedOptions[0].textContent + '，真臂不动）')
      : hit ? (hit.object.userData.hand ? '拖手：' + modeSel.selectedOptions[0].textContent : '拖动转 ' + JNAMES[hit.object.userData.joint]) : '';
    document.getElementById('arm3d_tip').textContent = t;
    return;
  }
  if (dragging.kind === 'locked') return;
  const p = pointer(e);
  lastTouch = Date.now();
  if (dragging.kind === 'joint') {
    const d = dragging;
    let delta;
    if (Math.abs(d.facing) > 0.25) {     // 轴大致朝向 / 背向屏幕：像转旋钮
      let a = Math.atan2(-(p.y - d.c.y), p.x - d.c.x) - d.a0;
      a = Math.atan2(Math.sin(a), Math.cos(a));
      delta = a / D * Math.sign(d.facing);
    } else delta = (p.x - d.x0) * 0.4;  // 轴和屏幕平行：左右拖
    let q = target.slice();
    q[d.j] = d.q0 + delta;
    if (orientOn()) q = lockQ(q);        // 预览：J4 跟着补（服务端同样算）
    target = clampQ(q);
    api.servo({ [d.j + 1]: +target[d.j].toFixed(1) });
  } else if (dragging.kind === 'pose') {
    const d = dragging;
    const hitP = new THREE.Vector3();
    if (!ray.ray.intersectPlane(d.plane, hitP)) return;
    const goal = goalOf(d, hitP), locked = orientOn(), seed = locked ? lockQ(d.q || d.q0) : (d.q || d.q0);
    let q = solveIK(goal, seed, !(d.mode === 'free' || d.mode === 'lead'), locked ? 0 : d.q0[1] - d.q0[2] - d.q0[3])
      || (d.mode === 'lead' && !locked ? solveFree(goal, seed) : null);
    if (q && locked) q = lockQ(q);
    if (!q) { document.getElementById('arm3d_tip').textContent = locked ? '保持手朝向时够不到这里' : '够不到这里'; return; }
    d.q = q;
    ghostOverride = q;
    d.mesh.position.copy(fk(q).tool);
    d.mesh.userData.tag.position.copy(d.mesh.position).add(new THREE.Vector3(0.025, 0, 0.025));
    document.getElementById('arm3d_tip').textContent = '「' + d.name + '」→ ' + q.map(v => v.toFixed(1)).join(' / ') + '°（松手保存）';
  } else {
    const d = dragging;
    const hitP = new THREE.Vector3();
    if (!ray.ray.intersectPlane(d.plane, hitP)) return;
    d.lastHit = hitP.clone();
    moveHand(d);
  }
});

/* 拖手：手到哪，整条臂跟到哪。先保持手掌朝向解；「手导向」模式够不到时放开腕部俯仰再解一次 */
function moveHand(d) {
  const goal = goalOf(d, d.lastHit);
  const locked = orientOn();
  const seed = locked ? lockQ(d.qPrev || d.q0) : (d.qPrev || d.q0);
  const lockBase = !(d.mode === 'free' || d.mode === 'lead');
  let q = solveIK(goal, seed, lockBase, locked ? 0 : d.pitch), turned = false;
  if (!q && d.mode === 'lead' && !locked) { q = solveFree(goal, seed); turned = !!q; }
  if (q && locked) q = lockQ(q);
  const tipEl = document.getElementById('arm3d_tip');
  if (!q) { tipEl.textContent = locked ? '保持手朝向时够不到这里（关掉朝向锁可以转腕去够）' : '够不到这里'; return; }
  tipEl.textContent = turned ? '手导向：为了够到这里，腕部俯仰跟着调整了' : (d.mode === 'lead' ? '手导向：整条臂跟着手走（拖动时滚轮 = 推远 / 拉近）' : '');
  d.qPrev = q;
  {
    target = q;
    const send = {};
    q.forEach((v, i) => { if (Math.abs(v - live[i]) > 0.05 || i < 4) send[i + 1] = +v.toFixed(1); });
    api.servo(send);
  }
}

renderer.domElement.addEventListener('wheel', e => {   // 拖手时滚轮推远拉近（不缩放视角）
  if (!dragging || dragging.kind !== 'hand' || !dragging.lastHit) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  dragging.depth += Math.sign(e.deltaY) * 0.01;
  lastTouch = Date.now();
  moveHand(dragging);
}, { passive: false, capture: true });

function endDrag(e) {
  if (!dragging) return;
  const d = dragging;
  dragging = null;
  if (d.kind === 'pose') {
    ghostOverride = null;
    marksKey = '';
    if (d.q) {
      const joints = d.q.map((v, i) => (i + 1) + ':' + v.toFixed(2)).join(',');
      fetch('/teach/pose_set?name=' + encodeURIComponent(d.name) + '&joints=' + joints, { method: 'POST', cache: 'no-store' })
        .then(r => r.json()).then(j => {
          api.log(j.ok ? '已移动姿态「' + d.name + '」' + (j.followers.length ? '，' + j.followers.join('、') + ' 跟着移动' : '') : j.error, !j.ok);
          api.reload();
        }).catch(err => api.log(err.message, true));
    }
  }
  controls.enabled = true;
  lastTouch = Date.now();
  try { renderer.domElement.releasePointerCapture(e.pointerId); } catch (_) { }
}
renderer.domElement.addEventListener('pointerup', endDrag);
renderer.domElement.addEventListener('pointercancel', endDrag);

/* 逆运动学：手心到 goal，保持手掌朝向（J2−J3−J4 不变、J5/J6 不动）；lockBase = 不转底座（只在竖直平面里动） */
function solveIK(goal, q0, lockBase, pitchRef) {
  const pitch = pitchRef ?? (q0[1] - q0[2] - q0[3]);
  const vars = lockBase ? [1, 2] : [0, 1, 2];
  let q = q0.slice();
  const apply = qq => { const r = qq.slice(); r[3] = r[1] - r[2] - pitch; return r; };
  const errOf = qq => {
    const err = new THREE.Vector3().subVectors(goal, fk(apply(qq)).tool);
    if (lockBase) {   // 只看竖直平面内的半径和高度
      const az = qq[0] * D, er = err.x * Math.cos(az) + err.y * Math.sin(az);
      err.set(er * Math.cos(az), er * Math.sin(az), err.z);
    }
    return err;
  };
  for (let it = 0; it < 40; it++) {
    const cur = fk(apply(q)).tool;
    const err = errOf(q);
    if (err.length() < 0.001) break;
    const J = vars.map(k => {
      const qq = q.slice(); qq[k] += 0.05;
      return new THREE.Vector3().subVectors(fk(apply(qq)).tool, cur).divideScalar(0.05);
    });
    // 阻尼最小二乘：dq = Jᵀ (J Jᵀ + λI)⁻¹ e
    const A = new THREE.Matrix3();
    const a = [0, 0, 0, 0, 0, 0, 0, 0, 0];
    for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++)
      a[r * 3 + c] = J.reduce((s, col) => s + col.getComponent(r) * col.getComponent(c), 0) + (r === c ? 1e-4 : 0);
    A.set(...a);
    const y = err.clone().applyMatrix3(A.invert());
    vars.forEach((k, n) => { q[k] += Math.max(-5, Math.min(5, J[n].dot(y))); });
  }
  const out = apply(q);
  if (errOf(q).length() > 0.006) return null;
  if (out.some((v, i) => v < lower[i] - 0.01 || v > upper[i] + 0.01)) return null;
  return out;
}

/* 只管位置：J1~J4 都可以动（J5、J6 不动），每步夹进限位；从 seed 出发，尽量少改关节 */
function solveFree(goal, seed) {
  const vars = [0, 1, 2, 3];
  let q = clampQ(seed.slice());
  for (let it = 0; it < 60; it++) {
    const cur = fk(q).tool;
    const err = new THREE.Vector3().subVectors(goal, cur);
    if (err.length() < 0.001) break;
    const J = vars.map(k => {
      const qq = q.slice(); qq[k] += 0.05;
      return new THREE.Vector3().subVectors(fk(qq).tool, cur).divideScalar(0.05);
    });
    const a = [0, 0, 0, 0, 0, 0, 0, 0, 0];
    for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++)
      a[r * 3 + c] = J.reduce((s, col) => s + col.getComponent(r) * col.getComponent(c), 0) + (r === c ? 1e-4 : 0);
    const y = err.clone().applyMatrix3(new THREE.Matrix3().set(...a).invert());
    vars.forEach((k, n) => { q[k] += Math.max(-5, Math.min(5, J[n].dot(y))); });
    q = clampQ(q);
  }
  return fk(q).tool.distanceTo(goal) <= 0.006 ? q : null;
}

/* ---------- 视角按钮 ---------- */
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => {
  const v = { front: [1.05, 0, 0.35], side: [0.12, -1.1, 0.35], top: [0.15, -0.01, 1.3], iso: [0.72, -0.82, 0.55] }[b.dataset.view];
  camera.position.set(...v);
  controls.target.set(0.12, 0, 0.22);
  controls.update();
});
document.getElementById('arm3d_sync').onclick = () => { if (live) target = live.slice(); lastTouch = 0; };

// 调试 / 测试用：手心、各关节在画布上的像素位置
window.arm3d = {
  toolScreen: () => live && screenOf(fk(target || live).tool),
  jointScreen: j => live && screenOf(fk(target || live).pos[j].clone().lerp(fk(target || live).pos[j + 1] || fk(live).tool, 0.5)),
  target: () => target, live: () => live,
  poseScreen: n => { const m = poseMarks.find(x => x.userData.pose === n); return m && screenOf(m.position); },
};

/* ---------- 渲染 ---------- */
function resize() {
  const w = box.clientWidth, h = box.clientHeight;
  renderer.setSize(w, h, false);
  renderer.domElement.style.width = w + 'px';
  renderer.domElement.style.height = h + 'px';
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(box);
resize();
function frame() {
  if (live) {
    real.update(live);
    const t = ghostOverride || target || live;
    const differs = t.some((v, i) => Math.abs(v - live[i]) > 0.8);
    ghost.group.visible = differs || !!dragging;
    if (ghost.group.visible) ghost.update(t);
    refreshMarks(live);
    if (helpers.visible) updateHelpers(target && dragging && dragging.kind !== 'pose' ? target : live);
  }
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}
frame();
