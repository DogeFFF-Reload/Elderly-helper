"""联调自检：/api/nodes 报的事实对不对；preflight 把它翻成"能不能开演 + 去哪修"。"""
import copy
import json
import threading
import unittest
import urllib.request

from helpers import make_bus

import master_service
from tools import preflight

GOOD = {
    "bus": {"uptime": 60, "state": "IDLE", "events": 3, "night": False},
    "arm": {"online": True, "url": "http://127.0.0.1:8013", "done": 2, "failed": 0, "dropped": 0, "last_error": None},
    "box": {"online": True, "id": "box1", "age": 0.2},
    "vision": {"online": True, "age": 0.3},
    "clients": {"tts_player": 0.2, "asr_bridge": 0.3},
    "heard": None,
    "llm": {"mode": "scripted", "enabled": False, "llm": 0, "fallback": 0, "last_error": None, "cooldown_left": 0},
    "params_changed": {},
}


def nodes(**patch):
    """在 GOOD 上改几项：clients / heard / params_changed 整体替换，其余按字段合并。"""
    n = copy.deepcopy(GOOD)
    for key, value in patch.items():
        if key in ("clients", "heard", "params_changed"):
            n[key] = value
        else:
            n[key].update(value)
    return n


def levels(rows):
    return {name: level for level, name, _, _ in rows}


def row(rows, name):
    return next(r for r in rows if r[1] == name)


class CheckTest(unittest.TestCase):
    def test_all_green_can_start(self):
        rows = preflight.check(nodes())
        self.assertEqual(set(levels(rows).values()), {"ok"})
        out = []
        self.assertEqual(preflight.report(rows, "http://x", out.append), 0)
        self.assertIn("可以开演", out[-1])

    def test_arm_offline_blocks_and_points_to_x5b(self):
        rows = preflight.check(nodes(arm={"online": False}))
        level, _, text, fix = row(rows, "执行端")
        self.assertEqual(level, "fail")
        self.assertIn("X5-B", fix)
        self.assertNotIn("X5-C", fix, "只有两块 X5，执行端和主脑在同一块")
        out = []
        self.assertEqual(preflight.report(rows, "http://x", out.append), 1)
        self.assertIn("执行端", out[-1])

    def test_arm_rejecting_poses_is_a_warning(self):
        rows = preflight.check(nodes(arm={"failed": 1, "last_error": "finger_fist：rejected unknown pose"}))
        self.assertEqual(levels(rows)["执行端"], "warn")
        self.assertIn("poses.py", row(rows, "执行端")[3])

    def test_no_tts_player_means_nobody_speaks(self):
        rows = preflight.check(nodes(clients={"asr_bridge": 0.1}))
        self.assertEqual(levels(rows)["喇叭"], "fail")
        rows = preflight.check(nodes(clients={"tts_player": 30}))
        self.assertEqual(levels(rows)["喇叭"], "fail", "30 秒没来拉 = 已经退出了")
        rows = preflight.check(nodes(clients={"未标名@127.0.0.1": 0.2}))
        self.assertEqual(levels(rows)["喇叭"], "warn", "旧版 tts_player 不报名字，不能直接判挂")

    def test_fallback_backed_nodes_only_warn(self):
        rows = preflight.check(nodes(vision={"online": False, "age": None}, box={"online": False, "age": None},
                                     clients={"tts_player": 0.1}))
        lv = levels(rows)
        self.assertEqual((lv["视觉"], lv["床头盒"], lv["麦克风"]), ("warn", "warn", "warn"))
        self.assertIn("触发摔倒", row(rows, "视觉")[3])
        self.assertEqual(preflight.report(rows, "http://x", lambda s: None), 0)

    def test_heard_counts_as_microphone(self):
        rows = preflight.check(nodes(clients={"tts_player": 0.1}, heard={"text": "你好", "age": 3}))
        self.assertEqual(levels(rows)["麦克风"], "ok")

    def test_leftover_state_and_params_warned(self):
        rows = preflight.check(nodes(bus={"state": "SOS"},
                                     params_changed={"ask_wait_day": [30.0, 5.0], "night_override": ["on", "auto"]}))
        self.assertIn("重置", row(rows, "状态机")[3])
        level, _, text, _ = row(rows, "演示参数")
        self.assertEqual(level, "warn")
        self.assertIn("ask_wait_day=30（默认 5）", text)
        self.assertIn("night_override=on（默认 auto）", text)

    def test_llm_errors_warn_not_block(self):
        rows = preflight.check(nodes(llm={"mode": "llm", "enabled": True, "last_error": "timeout", "cooldown_left": 40}))
        self.assertEqual(levels(rows)["LLM"], "warn")
        rows = preflight.check(nodes(llm={"mode": "llm", "enabled": False}))
        self.assertEqual(levels(rows)["LLM"], "warn")


class NodesEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus, cls.clock, cls.arm = make_bus()
        cls.server = master_service.serve(cls.bus, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop_ticker.set()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.bus.reset()
        self.bus.clients.clear()
        self.bus.set_params({"ask_wait_day": 5})

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_subscribers_named_by_client_param(self):
        self.get("/api/events?since=0&client=tts_player")
        self.get("/api/events?since=0")
        clients = self.get("/api/nodes")["clients"]
        self.assertIn("tts_player", clients)
        self.assertTrue(any(k.startswith("未标名@") for k in clients))

    def test_voice_bus_client_reports_its_name(self):
        from voice import bus_client
        bus_client.EventFollower(self.base, lambda ev: None).poll_once()
        self.assertIn(bus_client.CLIENT, self.bus.clients)

    def test_vision_liveness_follows_skeleton_stream(self):
        self.assertFalse(self.get("/api/nodes")["vision"]["online"])
        self.bus.update_skeleton({"keypoints": {"nose": [0.5, 0.2]}, "fallen": False})
        self.assertTrue(self.get("/api/nodes")["vision"]["online"])
        self.clock.advance(4)
        v = self.get("/api/nodes")["vision"]
        self.assertFalse(v["online"])
        self.assertEqual(v["age"], 4.0)

    def test_params_changed_reported(self):
        self.bus.set_params({"ask_wait_day": 30})
        self.assertEqual(self.get("/api/nodes")["params_changed"], {"ask_wait_day": [30.0, 5.0]})

    def test_preflight_end_to_end(self):
        out = []
        self.assertEqual(preflight.main(["--server", self.base], out=out.append), 1, "没有 tts_player")
        self.assertTrue(any("❌" in ln and "喇叭" in ln for ln in out), out)
        self.get("/api/events?since=0&client=tts_player")
        out = []
        self.assertEqual(preflight.main(["--server", self.base], out=out.append), 0, "\n".join(out))

    def test_wait_retries_until_green(self):
        calls = []

        def sleep(_):
            calls.append(1)
            self.get("/api/events?since=0&client=tts_player")   # 等的过程中 tts_player 起来了
        out = []
        self.assertEqual(preflight.main(["--server", self.base, "--wait", "30"], out=out.append, sleep=sleep), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("还差 喇叭", out[0])

    def test_bus_unreachable(self):
        from test_arm_link import closed_url
        out = []
        self.assertEqual(preflight.main(["--server", closed_url()], out=out.append), 2)
        self.assertIn("主脑连不上", out[0])


if __name__ == "__main__":
    unittest.main()
