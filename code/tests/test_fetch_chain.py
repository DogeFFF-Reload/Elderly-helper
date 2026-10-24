"""L1 固定格位取物：语音 → 取物信号（关键词 / LLM）→ 白名单 → iPhone 格位确认 → 臂。"""
import json
import threading
import time
import unittest

from helpers import events, make_bus, said

import master_service
from agent import fetch_intent


class FetchLLM:
    """假 LLM：取物解析的提问回 reply（JSON 原文）；润色台词一律回空 → 台词走 scripted，不进冷却。"""

    def __init__(self, reply=None, sleep=0.0):
        self.reply, self.sleep = reply, sleep
        self.asked = []

    def __call__(self, messages, timeout):
        if "取物意图解析器" in messages[0]["content"]:
            self.asked.append(messages[-1]["content"])
            time.sleep(self.sleep)
            r = self.reply
            return json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r
        return ""


def fetch_events(bus):
    return [e["text"] for e in events(bus, "fetch")]


class SignalTest(unittest.TestCase):
    def setUp(self):
        self.cat = fetch_intent.FetchCatalog()

    def test_keyword_signal_names_item_and_slot(self):
        sig = fetch_intent.keyword_signal("帮我把水杯拿过来", self.cat)
        self.assertEqual((sig["item"], sig["slot"], sig["pose"], sig["skill"], sig["source"], sig["confirm"]),
                         ("cup", "1", "fetch_slot1", "fetch_item", "keyword", False))

    def test_first_mentioned_item_wins(self):
        self.assertEqual(fetch_intent.keyword_signal("把药和水拿给我", self.cat)["item"], "medicine")
        self.assertEqual(fetch_intent.keyword_signal("把老花镜递给我", self.cat)["item"], "glasses")

    def test_llm_reply_must_be_catalog_id(self):
        sig, why = fetch_intent.parse_llm('{"item": "tissue", "why": "要擦鼻涕"}', self.cat)
        self.assertEqual((sig["item"], sig["source"], sig["confirm"]), ("tissue", "llm", True))
        for reply in ('{"item": "press_sos"}', '{"item": "fetch_slot1"}', '{"item": ["cup"]}',
                      "好的我去拿水杯", "[1, 2]", ""):
            self.assertIsNone(fetch_intent.parse_llm(reply, self.cat)[0], reply)
        self.assertEqual(fetch_intent.parse_llm('{"item": null}', self.cat), (None, "没听出要拿东西"))

    def test_llm_reply_with_code_fence_still_parses(self):
        sig, _ = fetch_intent.parse_llm('```json\n{"item": "cup", "why": "口渴"}\n```', self.cat)
        self.assertEqual(sig["item"], "cup")

    def test_catalog_matches_whitelist_and_arm_poses(self):
        from hand_controller import arm_safety as S
        from hand_controller.poses import POSES
        from agent.supervisor import SKILLS_PATH
        spec = json.loads(SKILLS_PATH.read_text(encoding="utf-8"))["agent_allowed"]
        arm_skills = S.skill_names()
        for item in self.cat.items.values():
            self.assertIn(item["skill"], spec, item["id"])
            self.assertTrue(any(i in item["name"] for i in spec[item["skill"]]["items"]), "%s 不在白名单" % item["name"])
            self.assertIn(item["pose"], arm_skills, "%s 的动作臂不认" % item["id"])
            self.assertIn(item["pose"], POSES)
        self.assertEqual(len({i["slot"] for i in self.cat.items.values()}), len(self.cat.items), "一格只放一样")


class KeywordFetchTest(unittest.TestCase):
    def test_named_items_go_to_their_slot(self):
        for text, pose in (("把水杯递给我", "fetch_slot1"), ("帮我拿一下眼镜", "fetch_slot2"),
                           ("把纸巾拿给我", "fetch_slot3"), ("把药递给我", "deliver_medicine")):
            bus, clock, arm = make_bus()
            self.assertEqual(bus.voice(text)["intent"], "deliver", text)
            self.assertEqual(arm.sent, [(pose, 1)], text)

    def test_fetch_line_and_timeline(self):
        bus, clock, arm = make_bus()
        bus.voice("把水杯递给我")
        self.assertIn("水杯", said(bus)[-1])
        self.assertIn("第 1 格", fetch_events(bus)[-1])
        self.assertIn("老人点名", fetch_events(bus)[-1])

    def test_unknown_items_blocked_and_list_what_we_can_fetch(self):
        for text in ("把剪刀递给我", "把遥控器递给我"):
            bus, clock, arm = make_bus()
            bus.voice(text)
            self.assertEqual(arm.sent, [], text)
            self.assertTrue(events(bus, "blocked"), text)
            self.assertIn("水杯", said(bus)[-1])

    def test_suspended_during_safety_chain(self):
        bus, clock, arm = make_bus()
        bus.fall("卧室")
        clock.advance(5.2, bus)
        bus.voice("把水杯递给我")
        self.assertNotIn("fetch_slot1", arm.poses(), "手#1 正在按 SOS，不能被取物挪走")
        self.assertIn("已挂起", events(bus, "blocked")[-1]["text"])

    def test_estop_blocks_fetch(self):
        bus, clock, arm = make_bus()
        bus.set_estop(True)
        bus.voice("把眼镜拿给我")
        self.assertNotIn("fetch_slot2", arm.poses())


class SlotConfirmTest(unittest.TestCase):
    def test_empty_slot_no_arm(self):
        bus, clock, arm = make_bus()
        bus.fetch_slots({"tray": True, "1": False, "2": True, "3": True})
        bus.voice("把水杯递给我")
        self.assertEqual(arm.sent, [])
        self.assertIn("不在架子上", said(bus)[-1])
        bus.voice("把眼镜拿给我")
        self.assertEqual(arm.sent, [("fetch_slot2", 1)])
        self.assertIn("iPhone 确认第 2 格有东西", fetch_events(bus)[-1])

    def test_stale_camera_is_unconfirmed_not_empty(self):
        bus, clock, arm = make_bus()
        bus.fetch_slots({"1": False})
        clock.advance(master_service.SLOT_TIMEOUT + 0.1)
        bus.voice("把水杯递给我")
        self.assertEqual(arm.sent, [("fetch_slot1", 1)], "默认 optional：摄像头离线照样拿")
        self.assertIn("未确认", fetch_events(bus)[-1])

    def test_required_camera(self):
        bus, clock, arm = make_bus(fetch_camera="required")
        bus.voice("把水杯递给我")
        self.assertEqual(arm.sent, [])
        self.assertIn("看不到架子", said(bus)[-1])
        bus.fetch_slots({"1": True})
        bus.voice("把水杯递给我")
        self.assertEqual(arm.sent, [("fetch_slot1", 1)])

    def test_medicine_tray_checked_too(self):
        bus, clock, arm = make_bus()
        bus.fetch_slots({"tray": False})
        bus.voice("把药递给我")
        self.assertEqual(arm.sent, [])

    def test_slot_changes_logged_once(self):
        bus, clock, arm = make_bus()
        bus.fetch_slots({"1": True, "2": True})
        bus.fetch_slots({"1": True, "2": True})
        bus.fetch_slots({"1": False, "2": True})
        notes = [e["text"] for e in events(bus, "system") if "格位" in e["text"]]
        self.assertEqual(len(notes), 2)
        self.assertIn("第 1 格空", notes[-1])

    def test_http_route_and_snapshot(self):
        bus, clock, arm = make_bus()
        master_service.dispatch_post(bus, "/api/fetch/slots", {"slots": {"1": True, "2": False}})
        snap = bus.snapshot()["fetch"]
        self.assertTrue(snap["camera"]["online"])
        seen = {i["id"]: i["seen"] for i in snap["items"]}
        self.assertEqual((seen["cup"], seen["glasses"], seen["tissue"]), (True, False, None))
        self.assertTrue(bus.nodes()["slots"]["online"])
        self.assertFalse(bus.fetch_slots("1=1")["ok"])


class LLMFetchTest(unittest.TestCase):
    def test_implicit_need_asks_then_fetches_on_yes(self):
        fake = FetchLLM({"item": "cup", "why": "口渴"})
        bus, clock, arm = make_bus(llm_transport=fake)
        self.assertEqual(bus.voice("我有点渴了")["intent"], "fetch_llm")
        self.assertEqual(arm.sent, [], "LLM 猜的不直接动臂")
        bus.speech.drain()
        self.assertIn("水杯吗", said(bus)[-1])
        self.assertEqual(bus.snapshot()["fetch"]["pending"]["item"], "cup")
        self.assertEqual(bus.voice("对")["intent"], "fetch_confirm")
        self.assertEqual(arm.sent, [("fetch_slot1", 1)])
        self.assertIn("LLM 理解、老人已确认", fetch_events(bus)[-1])

    def test_no_cancels(self):
        bus, clock, arm = make_bus(llm_transport=FetchLLM({"item": "glasses", "why": "看不清"}))
        bus.voice("报纸上的字看不清")
        self.assertEqual(bus.voice("不要")["intent"], "fetch_cancel")
        self.assertEqual(arm.sent, [])
        self.assertIsNone(bus.fetch_pending)

    def test_confirm_window_times_out(self):
        bus, clock, arm = make_bus(llm_transport=FetchLLM({"item": "tissue", "why": "流鼻涕"}))
        bus.voice("我流鼻涕了")
        clock.advance(bus.cfg["fetch_confirm_window"] + 0.5, bus)
        self.assertIsNone(bus.fetch_pending)
        self.assertIn("超时", fetch_events(bus)[-1])
        bus.voice("对")
        self.assertEqual(arm.sent, [])

    def test_llm_cannot_pick_outside_catalog(self):
        for reply in ({"item": "press_sos"}, {"item": "remote"}, "我这就去拿"):
            bus, clock, arm = make_bus(llm_transport=FetchLLM(reply))
            self.assertEqual(bus.voice("我渴了")["intent"], "chitchat")
            self.assertEqual(arm.sent, [], reply)
            self.assertIsNone(bus.fetch_pending)
            self.assertTrue(any("LLM 取物解析回落" in e["text"] for e in events(bus, "system")), reply)

    def test_llm_null_is_plain_chitchat(self):
        bus, clock, arm = make_bus(llm_transport=FetchLLM({"item": None, "why": "闲聊"}))
        self.assertEqual(bus.voice("今天天气真好")["intent"], "chitchat")
        self.assertFalse(any("取物" in e["text"] for e in events(bus, "system")))

    def test_vague_deliver_resolved_by_llm(self):
        fake = FetchLLM({"item": "glasses", "why": "看书用"})
        bus, clock, arm = make_bus(llm_transport=fake)
        bus.voice("把那个看书戴的拿给我")
        self.assertEqual(arm.sent, [])
        bus.voice("是的")
        self.assertEqual(arm.sent, [("fetch_slot2", 1)])

    def test_named_item_never_asks_llm(self):
        fake = FetchLLM({"item": "tissue"})
        bus, clock, arm = make_bus(llm_transport=fake)
        bus.voice("把水杯递给我")
        self.assertEqual((fake.asked, arm.sent), ([], [("fetch_slot1", 1)]))

    def test_estop_between_proposal_and_yes(self):
        bus, clock, arm = make_bus(llm_transport=FetchLLM({"item": "cup"}))
        bus.voice("我渴了")
        bus.set_estop(True)
        bus.voice("对")
        self.assertNotIn("fetch_slot1", arm.poses())

    def test_not_asked_during_safety_chain(self):
        fake = FetchLLM({"item": "cup"})
        bus, clock, arm = make_bus(llm_transport=fake)
        bus.fall("卧室")
        clock.advance(5.2, bus)
        bus.voice("我渴了")
        self.assertEqual(fake.asked, [])

    def test_empty_slot_reported_before_asking_yes(self):
        bus, clock, arm = make_bus(llm_transport=FetchLLM({"item": "cup"}))
        bus.fetch_slots({"1": False})
        bus.voice("我渴了")
        self.assertIsNone(bus.fetch_pending, "架上没有就别问“要不要”")
        bus.speech.drain()
        self.assertIn("不在架子上", said(bus)[-1])

    def test_scripted_mode_never_guesses(self):
        bus, clock, arm = make_bus()
        self.assertEqual(bus.voice("我渴了")["intent"], "chitchat")
        self.assertIsNone(bus.fetch_pending)

    def test_slow_llm_falls_back_without_holding_bus_lock(self):
        fake = FetchLLM({"item": "cup"}, sleep=1.0)
        bus, clock, arm = make_bus(llm_transport=fake, llm_timeout=0.5)
        ticked = threading.Event()
        t = threading.Thread(target=lambda: bus.voice("我渴了"))
        t.start()
        time.sleep(0.1)
        threading.Thread(target=lambda: (bus.tick(), bus.fall("卧室"), ticked.set()), daemon=True).start()
        self.assertTrue(ticked.wait(0.3), "LLM 解析期间状态机照常")
        t.join(2)
        self.assertEqual(arm.sent, [])
        self.assertEqual(bus.fsm.state, "ASKING")


if __name__ == "__main__":
    unittest.main()
