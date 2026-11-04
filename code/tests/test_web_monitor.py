"""Latest-frame display: memory images and loopback sockets, no camera or GUI."""
import json
from http.client import HTTPConnection
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from gesture.fall_rules import FallDetector
from vision.web_monitor import WebMonitor, encode_frame


def eventually(check, seconds=2):
    end = time.monotonic() + seconds
    while not check():
        if time.monotonic() >= end:
            raise AssertionError("Timed out waiting for in-memory worker")
        time.sleep(.005)


class WebMonitorTest(unittest.TestCase):
    def setUp(self):
        connection = socket.create_connection

        def local_only(address, *args, **kwargs):
            self.assertEqual(address[0], "127.0.0.1", "Unexpected non-loopback connection")
            return connection(address, *args, **kwargs)

        guard = patch("socket.create_connection", side_effect=local_only)
        guard.start()
        self.addCleanup(guard.stop)
        self.now = 10.
        self.monitor = WebMonitor(port=0, clock=lambda: self.now, encoder=lambda *_: b"\xff\xd8test\xff\xd9")
        self.addCleanup(self.monitor.close)
        self.detector = FallDetector()
        self.detector.input_status = "observing"

    def request(self, path, method="GET"):
        connection = HTTPConnection("127.0.0.1", self.monitor.address[1], timeout=2)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_default_local_read_only_page_and_status_have_no_action_route(self):
        self.assertEqual(self.monitor.address[0], "127.0.0.1")
        status, headers, data = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("全屏显示", data.decode("utf-8"))
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/fall_alarm", "POST")[0], 405)
        self.assertEqual(self.request("/fall_alarm")[0], 404)
        snapshot = json.loads(self.request("/api/status")[2])
        self.assertEqual(snapshot["mode"], "off")
        self.assertTrue(snapshot["stale"])
        self.assertIsNone(snapshot["phone_received"])
        self.assertEqual(self.request("/frame.jpg")[0], 503)

    def test_two_viewers_read_same_frame_then_stale_frame_is_refused(self):
        self.monitor.show(None, None, self.detector)
        eventually(lambda: self.monitor.snapshot()["frame_ready"])
        a, b = self.request("/frame.jpg"), self.request("/frame.jpg")
        self.assertEqual(a[0], 200)
        self.assertEqual(a[2], b[2])
        self.assertEqual(self.monitor.served_frames, 2)
        self.now += 4
        snapshot = self.monitor.snapshot()
        self.assertTrue(snapshot["stale"])
        self.assertEqual(snapshot["input_state"], "input_error")
        self.assertFalse(snapshot["frame_ready"])
        self.assertEqual(self.request("/frame.jpg")[0], 503)

    def test_old_sequence_query_cannot_replay_a_previous_frame(self):
        self.monitor.encoder = lambda _frame, kp, _detector: kp
        self.monitor.show(None, b"first-image", self.detector)
        eventually(lambda: self.monitor.jpeg_sequence == 1)
        self.now += .3
        self.monitor.show(None, b"current-image", self.detector)
        eventually(lambda: self.monitor.jpeg_sequence == 2)
        self.assertEqual(self.request("/frame.jpg?seq=1")[2], b"current-image")
        self.assertEqual(self.request("/replay")[0], 404)

    def test_slow_encoder_keeps_one_latest_frame_and_does_not_hold_detector(self):
        self.monitor.close()
        entered, release = threading.Event(), threading.Event()
        rendered = []

        def encode(_frame, kp, state):
            rendered.append((kp, state))
            if len(rendered) == 1:
                entered.set()
                release.wait(2)
            return b"jpeg"

        self.monitor = WebMonitor(port=0, encoder=encode, clock=lambda: self.now)
        self.addCleanup(self.monitor.close)
        self.addCleanup(release.set)
        points = {"head": [.1, .2, .99]}
        self.monitor.show(None, points, self.detector)
        self.assertTrue(entered.wait(1))
        self.detector.state = "CANDIDATE"
        points["head"][0] = .9
        start = time.monotonic()
        for _ in range(100):
            self.now += .21
            self.monitor.show(None, points, self.detector)
        self.assertLess(time.monotonic() - start, .75)
        release.set()
        eventually(lambda: self.monitor.jpeg_sequence == 101)
        self.assertEqual(len(rendered), 2)
        self.assertEqual(rendered[0][0]["head"][0], .1)
        self.assertEqual(rendered[0][1].state, "WATCH")
        self.assertEqual(rendered[-1][0]["head"][0], .9)

    def test_render_error_does_not_interrupt_processing_or_expose_exception_message(self):
        def fail(*_args):
            raise ValueError("secret-camera-token")
        self.monitor.encoder = fail
        self.assertTrue(self.monitor.show(None, None, self.detector))
        eventually(lambda: self.monitor.render_error is not None)
        snapshot = self.monitor.snapshot()
        self.assertFalse(snapshot["stale"])
        self.assertFalse(snapshot["frame_ready"])
        self.assertEqual(snapshot["render_error"], "ValueError")
        self.assertNotIn("secret-camera-token", json.dumps(snapshot))

    def test_alarm_receipts_do_not_claim_phone_delivery_or_replace_newer_event(self):
        self.monitor.update(alarm={"event_id": "a", "status": "queued"})
        self.monitor.update(alarm={"event_id": "b", "status": "queued"})
        self.monitor.update(alarm={"event_id": "a", "status": "bedside_accepted"})
        self.monitor.update(alarm={"event_id": "b", "status": "bedside_accepted", "phone_received": None})
        snapshot = self.monitor.snapshot()
        self.assertEqual(snapshot["alarm_count"], 2)
        self.assertEqual(snapshot["latest_alarm"]["event_id"], "b")
        self.assertIsNone(snapshot["phone_received"])
        self.assertIsNone(snapshot["latest_alarm"]["phone_received"])

    def test_slow_viewers_have_bounded_handlers_and_do_not_block_capture_or_close(self):
        sockets = []
        try:
            for _ in range(8):
                connection = socket.create_connection(self.monitor.address, timeout=1)
                sockets.append(connection)
                connection.sendall(b"GET / HTTP/1.0\r\n")
            eventually(lambda: self.monitor.server.slots._value == 0)
            started = time.monotonic()
            self.monitor.show(None, None, self.detector)
            self.monitor.close()
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(self.monitor.http.is_alive())
            self.assertFalse(self.monitor.worker.is_alive())
        finally:
            for connection in sockets:
                connection.close()

    def test_actual_memory_image_encodes_skeleton_without_opening_gui_or_changing_source(self):
        import cv2
        import numpy as np
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        with patch("cv2.imshow", side_effect=AssertionError("No GUI")), \
                patch("cv2.VideoCapture", side_effect=AssertionError("No camera")):
            jpeg = encode_frame(frame, {"head": [.5, .2, .99]}, self.detector)
        decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[1], 960)
        self.assertTrue(np.any(decoded))
        self.assertFalse(np.any(frame))

    @unittest.skipUnless(shutil.which("node"), "Node required for browser script state tests")
    def test_browser_chinese_states_and_late_image_cannot_hide_disconnection(self):
        page = Path(__file__).resolve().parents[1] / "vision/monitor.html"
        script = page.read_text(encoding="utf-8").split("<script>")[1].split("</script>")[0]
        harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const elements={};let resolveImage;
const document={getElementById(id){return elements[id]||(elements[id]={textContent:'',className:'',style:{},addEventListener(){},set src(v){if(this.onload)this.onload();}});},addEventListener(){}};
const context={document,window:{addEventListener(){}},console,setTimeout:()=>1,clearTimeout(){},AbortController,
 URL:{createObjectURL:()=> 'blob:memory',revokeObjectURL(){}},
 fetch:path=>path.startsWith('/frame')?new Promise(resolve=>{resolveImage=resolve;}):new Promise(()=>{})};
vm.createContext(context);vm.runInContext(SCRIPT,context);
vm.runInContext(`
const s={mode:'live',input_kind:'phone_mjpeg',sequence:3,processing_fps:8,alarm_count:1,
 input_state:'observing',stale:false,frame_ready:false,frame_age_s:.1,jpeg_sequence:1,
 latest_alarm:{event_id:'event-a',status:'bedside_accepted'}};
display(s);
if(!$('mode').textContent.includes('正式报警'))throw Error('Mode label');
if(!$('alarmHelp').textContent.includes('待现场确认'))throw Error('False phone receipt');
display({...s,run_status:'error',input_message:'摄像头登录失败，请核对账号和密码。'});
if(!$('overlayText').textContent.includes('登录失败'))throw Error('Hidden login failure');
if($('state').textContent!=='识别已停止')throw Error('False observation after login failure');
for(const [state,label] of Object.entries(inputs)){display({...s,input_state:state});if($('state').textContent!==label)throw Error('Input state');}
frameAllowed=true;
globalThis.pending=updateImage(2);
offline();
`,context);
resolveImage({ok:true,blob:async()=>new Uint8Array([1])});
context.pending.then(()=>{assert.equal(elements.video.style.display,'none');assert.equal(elements.overlay.style.display,'grid');assert.equal(elements.state.textContent,'状态未知');console.log('Browser states and stale image race passed');}).catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", harness.replace("SCRIPT", json.dumps(script))],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node required for browser script state tests")
    def test_hidden_or_restored_page_jumps_to_current_frame_without_playing_missed_frames(self):
        script = Path(__file__).resolve().parents[1].joinpath("vision/monitor.html").read_text(
            encoding="utf-8").split("<script>")[1].split("</script>")[0]
        harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const elements={},events={},pageEvents={},requests=[],timers=new Map();let timerId=0;
const document={hidden:false,getElementById(id){return elements[id]||(elements[id]={style:{},textContent:'',addEventListener(){},set src(v){this.onload();}});},addEventListener(n,f){events[n]=f;}};
const context={document,window:{addEventListener(n,f){pageEvents[n]=f;}},console,AbortController,
 setTimeout(f,ms){timers.set(++timerId,{f,ms});return timerId;},clearTimeout(id){timers.delete(id);},
 URL:{createObjectURL:()=> 'blob:latest',revokeObjectURL(){}},
 fetch(path){return new Promise(resolve=>requests.push({path,resolve}));}};
vm.createContext(context);vm.runInContext(SCRIPT,context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const status=n=>({mode:'off',input_kind:'phone_mjpeg',sequence:n,jpeg_sequence:n,processing_fps:10,
 alarm_count:0,input_state:'observing',stale:false,frame_ready:true,frame_age_s:0});
async function show(request,n){request.resolve({ok:true,json:async()=>status(n)});await tick();
 const frame=requests.at(-1);assert.equal(frame.path,'/frame.jpg?seq='+n);
 frame.resolve({ok:true,blob:async()=>new Uint8Array([n])});await tick();}
(async()=>{
 await show(requests[0],1);assert.equal(elements.liveStatus.textContent,'● 实时直播');
 const pollTimer=[...timers.values()].find(t=>t.ms===400);pollTimer.f();const outdated=requests.at(-1);
 document.hidden=true;events.visibilitychange();assert.equal(elements.video.style.display,'none');
 document.hidden=false;events.visibilitychange();const fresh=requests.at(-1);
 outdated.resolve({ok:true,json:async()=>status(2)});await tick();
 assert.equal(elements.video.style.display,'none');
 await show(fresh,100);assert.equal(elements.video.style.display,'block');
 assert.deepEqual(requests.filter(r=>r.path.startsWith('/frame')).map(r=>r.path),['/frame.jpg?seq=1','/frame.jpg?seq=100']);
 pageEvents.pagehide();assert.equal(elements.video.style.display,'none');
 pageEvents.pageshow({persisted:true});await show(requests.at(-1),200);
 assert.equal(elements.liveStatus.textContent,'● 实时直播');
 console.log('Live-only resume passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", harness.replace("SCRIPT", json.dumps(script))],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
