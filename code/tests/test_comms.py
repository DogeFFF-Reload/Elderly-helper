"""通话信令 + 语音留言：老人端 ↔ 家属端。

全部用假时钟：页面“在线”= 最近 15 秒内报过到（saw_client）；
推进时间的小工具 hold() 会按秒给指定页面续命，没续命的页面就按约定断线。
"""
import http.client
import io
import json
import threading
import unittest
import wave

from helpers import events, make_bus, said

import master_service
from agent import intent
from agent.comms import KEEP_VOICE, MAX_VOICE_BYTES


def contact(bus, cid):
    return next(c for c in bus.cfg["contacts"] if c["id"] == cid)


def line(bus, key, cid="daughter", **params):
    """按总线执行台词时的同一组参数渲染 lines.json 里的台词。"""
    c = contact(bus, cid)
    args = {"contact": c["name"], "pron": c["pron"]}
    args.update(params)
    return bus.llm.scripted(key, **args)


def notifications(bus, kind=None, contact_id=None):
    return [n for n in bus.notifications
            if (kind is None or n["kind"] == kind)
            and (contact_id is None or n["contact"] == contact_id)]


def hold(clock, bus, clients, seconds, step=1.0):
    """推进 seconds 秒；每步给 clients 里的页面续命（其余页面自然超时离线）。"""
    end = clock.t + seconds
    while clock.t < end - 1e-9:
        clock.t = min(end, clock.t + step)
        for name in clients:
            bus.saw_client(name)
        bus.tick()


def online(bus, *parties):
    for party in parties:
        bus.saw_client("elder" if party == "elder" else "family:%s" % party)


def start_call(bus, frm="daughter", to="elder", emergency=False):
    online(bus, frm, to)
    res = bus.call_start(frm, to, emergency=emergency)
    assert res["ok"], res
    return res["call"]["id"]


def connect_call(bus, frm="daughter", to="elder"):
    """发起 → offer → answer，返回 (call_id, answer 结果)。"""
    cid = start_call(bus, frm, to)
    assert bus.call_offer(cid, frm, "v=0\r\no=- offer")["ok"]
    res = bus.call_answer(cid, to, "v=0\r\no=- answer")
    assert res["ok"], res
    return cid, res


def wav_bytes(seconds=1.5, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class CallFlowTest(unittest.TestCase):
    def test_family_to_elder_full_flow(self):
        bus, clock, arm = make_bus()
        online(bus, "daughter", "elder")

        res = bus.call_start("daughter", "elder")
        self.assertTrue(res["ok"])
        call = res["call"]
        self.assertEqual(call["state"], "ringing")
        self.assertFalse(call["auto_answer"])
        self.assertEqual(call["from"], "daughter")
        self.assertEqual(call["to"], "elder")
        self.assertIn(line(bus, "call_incoming"), said(bus))
        cid = call["id"]

        r = bus.call_offer(cid, "elder", "offer-from-wrong-party")
        self.assertFalse(r["ok"])
        self.assertIn("不是这通电话的发起方", r["error"])

        r = bus.call_answer(cid, "elder", "answer-too-early")
        self.assertFalse(r["ok"])
        self.assertIn("没准备好", r["error"])

        self.assertTrue(bus.call_offer(cid, "daughter", "v=0\r\nsdp-offer")["ok"])
        self.assertEqual(bus.call_sdp(cid, "offer"),
                         {"ok": True, "sdp": "v=0\r\nsdp-offer"})

        r = bus.call_answer(cid, "elder", "v=0\r\nsdp-answer")
        self.assertTrue(r["ok"])
        self.assertEqual(r["call"]["state"], "active")
        self.assertTrue(bus.call_sdp(cid, "offer")["ok"])

        hold(clock, bus, ("elder", "family:daughter"), 5)
        r = bus.call_hangup(cid, "daughter")
        self.assertTrue(r["ok"])
        self.assertEqual(r["call"]["end_reason"], "ended")
        self.assertEqual(r["call"]["end_text"], "已挂断")
        self.assertIn("5 秒", events(bus, "call")[-1]["text"])
        self.assertEqual(bus.snapshot()["comms"]["call"]["end_reason"], "ended")

        self.assertFalse(bus.call_sdp(cid, "offer")["ok"])
        self.assertFalse(bus.call_sdp(cid, "answer")["ok"])


class CallStartRejectTest(unittest.TestCase):
    def test_elder_page_offline_refuses_family_call(self):
        bus, clock, arm = make_bus()
        online(bus, "daughter")
        r = bus.call_start("daughter", "elder")
        self.assertFalse(r["ok"])
        self.assertIn("不在线", r["error"])

    def test_initiator_page_offline(self):
        bus, clock, arm = make_bus()
        online(bus, "elder")
        r = bus.call_start("daughter", "elder")
        self.assertFalse(r["ok"])
        self.assertIn("没打开", r["error"])

    def test_stranger_and_self_are_refused(self):
        bus, clock, arm = make_bus()
        online(bus, "daughter", "elder")
        r = bus.call_start("stranger", "elder")
        self.assertFalse(r["ok"])
        r = bus.call_start("elder", "elder")
        self.assertFalse(r["ok"])

    def test_second_start_while_ringing_is_busy(self):
        bus, clock, arm = make_bus()
        start_call(bus, "daughter", "elder")
        r = bus.call_start("elder", "daughter")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("busy"))


class RingTimeoutTest(unittest.TestCase):
    def test_no_answer_turns_into_missed(self):
        bus, clock, arm = make_bus()
        online(bus, "elder")
        cid = start_call(bus, "elder", "daughter")
        self.assertTrue(cid)

        hold(clock, bus, ("elder",), 46)
        call = bus.snapshot()["comms"]["call"]
        self.assertEqual(call["end_reason"], "missed")
        self.assertEqual(call["end_text"], "未接")

        notes = notifications(bus, "call", "daughter")
        self.assertEqual(len(notes), 1)
        self.assertIn("没接通", notes[0]["text"])
        self.assertIn(line(bus, "call_missed"), said(bus))


class HangupTest(unittest.TestCase):
    def test_declined_by_callee(self):
        bus, clock, arm = make_bus()
        cid = start_call(bus, "daughter", "elder")
        r = bus.call_hangup(cid, "elder")
        self.assertTrue(r["ok"])
        self.assertEqual(r["call"]["end_reason"], "declined")
        self.assertEqual(r["call"]["end_text"], "拒接")

    def test_cancelled_by_caller_and_second_hangup_is_already(self):
        bus, clock, arm = make_bus()
        cid = start_call(bus, "elder", "daughter")
        r = bus.call_hangup(cid, "elder")
        self.assertTrue(r["ok"])
        self.assertEqual(r["call"]["end_reason"], "cancelled")
        self.assertEqual(r["call"]["end_text"], "已取消")

        again = bus.call_hangup(cid, "elder")
        self.assertEqual(again, {"ok": True, "already": True})


class OfflineTest(unittest.TestCase):
    def test_callee_page_gone_after_answer_is_dropped(self):
        bus, clock, arm = make_bus()
        cid, res = connect_call(bus)
        self.assertEqual(res["call"]["state"], "active")
        hold(clock, bus, ("elder",), 16)
        call = bus.snapshot()["comms"]["call"]
        self.assertEqual(call["end_reason"], "dropped")
        self.assertEqual(call["end_text"], "断线")
        self.assertEqual(events(bus, "call")[-1]["level"], "warn")

    def test_caller_page_gone_while_ringing_is_offline(self):
        bus, clock, arm = make_bus()
        start_call(bus, "daughter", "elder")
        hold(clock, bus, ("elder",), 16)
        call = bus.snapshot()["comms"]["call"]
        self.assertEqual(call["end_reason"], "offline")
        self.assertEqual(call["end_text"], "发起方页面已关闭")


class VoiceRequestCallTest(unittest.TestCase):
    def test_elder_online_dials_daughter_directly(self):
        bus, clock, arm = make_bus()
        online(bus, "elder")
        r = bus.voice("给女儿打个电话")
        self.assertEqual(r["intent"], "call_family")
        call = bus.snapshot()["comms"]["call"]
        self.assertEqual((call["from"], call["to"], call["state"]),
                         ("elder", "daughter", "ringing"))
        self.assertIn(line(bus, "call_dialing"), said(bus))
        self.assertEqual(notifications(bus, "call"), [])

    def test_elder_offline_falls_back_to_callback_notification(self):
        bus, clock, arm = make_bus()
        r = bus.voice("给女儿打个电话")
        self.assertEqual(r["intent"], "call_family")
        self.assertIsNone(bus.snapshot()["comms"]["call"])
        notes = notifications(bus, "call")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["contact"], "daughter")
        self.assertIn(line(bus, "call_family"), said(bus))


class VoiceAnswerHangupTest(unittest.TestCase):
    def test_answer_then_decline_by_voice(self):
        bus, clock, arm = make_bus()
        start_call(bus, "daughter", "elder")

        r = bus.voice("接电话")
        self.assertEqual(r["intent"], "call_answer")
        self.assertTrue(bus.snapshot()["comms"]["call"]["answer_req"])

        r = bus.voice("不接")
        self.assertEqual(r["intent"], "call_hangup")
        call = bus.snapshot()["comms"]["call"]
        self.assertEqual(call["end_reason"], "declined")
        self.assertEqual(call["ended_by"], "elder")

    def test_hangup_while_active_and_no_call(self):
        bus, clock, arm = make_bus()
        cid, _ = connect_call(bus)
        r = bus.voice("挂了")
        self.assertEqual(r["intent"], "call_hangup")
        self.assertEqual(bus.snapshot()["comms"]["call"]["end_reason"], "ended")

        idle_bus, idle_clock, idle_arm = make_bus()
        r = idle_bus.voice("挂了")
        self.assertNotEqual(r["intent"], "call_hangup")
        self.assertIsNone(idle_bus.snapshot()["comms"]["call"])


class FallEmergencyCallTest(unittest.TestCase):
    def test_escalation_calls_daughter_then_replaces_with_son(self):
        bus, clock, arm = make_bus()
        online(bus, "elder")
        self.assertEqual(bus.fall("卧室")["state"], "ASKING")
        hold(clock, bus, ("elder",), 6)

        self.assertEqual(bus.fsm.state, "SOS")
        self.assertEqual(len(notifications(bus, "fall", "daughter")), 1)
        call = bus.snapshot()["comms"]["call"]
        self.assertEqual((call["from"], call["to"]), ("elder", "daughter"))
        self.assertTrue(call["emergency"])
        self.assertEqual(call["state"], "ringing")

        bus2, clock2, arm2 = make_bus(family_ack_timeout=20)
        online(bus2, "elder")
        bus2.fall("卧室")
        hold(clock2, bus2, ("elder",), 6)
        self.assertEqual(bus2.fsm.state, "SOS")
        self.assertEqual(bus2.fsm.snapshot()["contact"], "daughter")

        hold(clock2, bus2, ("elder",), 21)
        self.assertEqual(bus2.fsm.snapshot()["contact"], "son")
        self.assertEqual(len(notifications(bus2, "fall", "son")), 1)
        call = bus2.snapshot()["comms"]["call"]
        self.assertEqual((call["from"], call["to"]), ("elder", "son"))
        self.assertTrue(call["emergency"])
        replaced = [h for h in bus2.snapshot()["comms"]["history"] if h["end_reason"] == "replaced"]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0]["to"], "daughter")

    def test_elder_offline_still_notifies_but_does_not_call(self):
        bus, clock, arm = make_bus()
        bus.fall("卧室")
        clock.advance(6, bus)
        self.assertEqual(bus.fsm.state, "SOS")
        self.assertEqual(len(notifications(bus, "fall", "daughter")), 1)
        self.assertIsNone(bus.snapshot()["comms"]["call"])

    def test_family_call_during_guard_chain_auto_answers(self):
        bus, clock, arm = make_bus()
        self.assertEqual(bus.fall("卧室")["state"], "ASKING")
        self.assertNotEqual(bus.fsm.state, "IDLE")
        online(bus, "daughter", "elder")
        r = bus.call_start("daughter", "elder")
        self.assertTrue(r["ok"])
        self.assertTrue(r["call"]["auto_answer"])
        self.assertTrue(r["call"]["emergency"] is False)


class ManualEmergencyCallTest(unittest.TestCase):
    def test_elder_manual_emergency_notifies_family(self):
        bus, clock, arm = make_bus()
        online(bus, "elder")
        r = bus.call_start("elder", "daughter", emergency=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["call"]["emergency"])
        self.assertEqual(r["call"]["state"], "ringing")
        notes = notifications(bus, "call", "daughter")
        self.assertEqual(len(notes), 1)
        self.assertIn("紧急呼叫", notes[0]["text"])

    def test_family_emergency_flag_is_ignored(self):
        bus, clock, arm = make_bus()
        online(bus, "daughter", "elder")
        r = bus.call_start("daughter", "elder", emergency=True)
        self.assertTrue(r["ok"])
        self.assertFalse(r["call"]["emergency"])
        self.assertFalse(r["call"]["auto_answer"])


class VoiceMessageTest(unittest.TestCase):
    def test_store_and_auto_play_when_elder_page_online(self):
        bus, clock, arm = make_bus()
        r = bus.voice_upload("daughter", "elder", "audio/webm;codecs=opus", b"...", "3.2")
        self.assertTrue(r["ok"])
        self.assertTrue(r["voice_id"])
        self.assertEqual(r["to"], ["elder"])
        msg = bus.messages[-1]
        self.assertEqual(msg["kind"], "voice")
        self.assertEqual(msg["dur"], 3.2)
        self.assertFalse(msg["heard"])
        self.assertIsNone(msg["text"])
        self.assertEqual(msg["voice_id"], r["voice_id"])
        self.assertIn(line(bus, "voice_from_family_later"), said(bus))
        self.assertIsNone(bus.snapshot()["comms"]["play"])

        bus.saw_client("elder")
        r2 = bus.voice_upload("daughter", "elder", "audio/webm", b"abc", "2")
        self.assertTrue(r2["ok"])
        self.assertIn(line(bus, "voice_from_family"), said(bus))
        play = bus.snapshot()["comms"]["play"]
        self.assertEqual(play["id"], r2["voice_id"])
        self.assertEqual(play["target"], "page")
        self.assertTrue(play["auto"])

    def test_box_player_and_wav_duration_from_header(self):
        bus, clock, arm = make_bus()
        bus.saw_client("elder-box")
        data = wav_bytes(1.5)
        r = bus.voice_upload("daughter", "elder", "audio/wav", data, None)
        self.assertTrue(r["ok"])
        self.assertEqual(r["dur"], 1.5)
        self.assertEqual(bus.messages[-1]["dur"], 1.5)
        play = bus.snapshot()["comms"]["play"]
        self.assertEqual(play["id"], r["voice_id"])
        self.assertEqual(play["target"], "box")
        self.assertTrue(play["auto"])

    def test_elder_to_all_creates_one_audio_two_messages(self):
        bus, clock, arm = make_bus()
        r = bus.voice_upload("elder", "all", "audio/webm", b"elder-voice", "3")
        self.assertTrue(r["ok"])
        self.assertEqual(r["to"], ["daughter", "son"])
        msgs = [m for m in bus.messages if m.get("kind") == "voice" and m["voice_id"] == r["voice_id"]]
        self.assertEqual(len(msgs), 2)
        self.assertEqual({m["to"] for m in msgs}, {"daughter", "son"})
        self.assertEqual(len(notifications(bus, "voice")), 2)
        self.assertEqual({n["contact"] for n in notifications(bus, "voice")}, {"daughter", "son"})
        self.assertEqual(bus.voice_audio(r["voice_id"])["data"], b"elder-voice")

        self.assertTrue(bus.voice_heard(r["voice_id"], "son")["ok"])
        by_to = {m["to"]: m for m in msgs}
        self.assertTrue(by_to["son"]["heard"])
        self.assertFalse(by_to["daughter"]["heard"])

    def test_elder_to_one_family_notifies_only_that_contact(self):
        bus, clock, arm = make_bus()
        r = bus.voice_upload("elder", "son", "audio/webm", b"hello", "2")
        self.assertTrue(r["ok"])
        self.assertEqual(r["to"], ["son"])
        notes = notifications(bus, "voice")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["contact"], "son")
        self.assertEqual(notes[0]["voice_id"], r["voice_id"])
        self.assertEqual(notes[0]["dur"], 2.0)

    def test_rejections(self):
        bus, clock, arm = make_bus()
        cases = [
            (("daughter", "elder", "text/plain", b"x", "1"), "不支持的录音格式"),
            (("daughter", "elder", "audio/webm", b"", "1"), "录音是空的"),
            (("daughter", "elder", "audio/webm", b"x", "0.2"), "说话时间太短"),
            (("daughter", "elder", "audio/webm", b"\x00" * (MAX_VOICE_BYTES + 1), "1"), "录音太大"),
            (("stranger", "elder", "audio/webm", b"x", "1"), "只能在"),
        ]
        for args, needle in cases:
            r = bus.voice_upload(*args)
            self.assertFalse(r["ok"], args)
            self.assertIn(needle, r["error"], args)
        self.assertEqual(bus.messages, [])

    def test_heard_requires_right_party(self):
        bus, clock, arm = make_bus()
        r = bus.voice_upload("daughter", "elder", "audio/webm", b"x", "1")
        vid = r["voice_id"]
        wrong = bus.voice_heard(vid, "son")
        self.assertFalse(wrong["ok"])
        self.assertIn("不是发给你的", wrong["error"])
        self.assertTrue(bus.voice_heard(vid, "elder")["ok"])
        msg = next(m for m in bus.messages if m.get("voice_id") == vid)
        self.assertTrue(msg["heard"])
        self.assertTrue(msg["heard_ts"])

    def test_only_last_keep_voice_clips_are_kept(self):
        bus, clock, arm = make_bus()
        ids = []
        for _ in range(KEEP_VOICE + 1):
            r = bus.voice_upload("daughter", "elder", "audio/webm", b"x", "1")
            self.assertTrue(r["ok"])
            ids.append(r["voice_id"])
        self.assertIsNone(bus.voice_audio(ids[0]))
        self.assertIsNotNone(bus.voice_audio(ids[1]))
        self.assertEqual(len([m for m in bus.messages if m.get("kind") == "voice"]), KEEP_VOICE + 1)


class VoicePlayTest(unittest.TestCase):
    def test_plays_earliest_unheard_on_page(self):
        bus, clock, arm = make_bus()
        bus.saw_client("elder")
        first = bus.voice_upload("daughter", "elder", "audio/webm", b"a", "1")["voice_id"]
        bus.voice_upload("daughter", "elder", "audio/webm", b"b", "1")

        r = bus.voice("听留言")
        self.assertEqual(r["intent"], "voice_play")
        play = bus.snapshot()["comms"]["play"]
        self.assertEqual(play["id"], first)
        self.assertEqual(play["target"], "page")

    def test_plays_on_box_when_page_offline(self):
        bus, clock, arm = make_bus()
        bus.saw_client("elder-box")
        vid = bus.voice_upload("daughter", "elder", "audio/webm", b"a", "1")["voice_id"]
        r = bus.voice("听留言")
        self.assertEqual(r["intent"], "voice_play")
        play = bus.snapshot()["comms"]["play"]
        self.assertEqual(play["id"], vid)
        self.assertEqual(play["target"], "box")

    def test_no_voice_says_none(self):
        bus, clock, arm = make_bus()
        bus.saw_client("elder")
        r = bus.voice("听留言")
        self.assertEqual(r["intent"], "voice_play")
        self.assertIn(line(bus, "voice_none"), said(bus))

    def test_no_screen_says_no_screen(self):
        bus, clock, arm = make_bus()
        r = bus.voice("听留言")
        self.assertEqual(r["intent"], "voice_play")
        self.assertIn(line(bus, "voice_no_screen"), said(bus))


class FallAlertTest(unittest.TestCase):
    def test_alert_goes_to_every_family_and_cancel_after_ok(self):
        bus, clock, arm = make_bus()
        bus.fall("卧室")
        alerts = notifications(bus, "fall_alert")
        self.assertEqual(len(alerts), 2)
        self.assertEqual({n["contact"] for n in alerts}, {"daughter", "son"})

        # 状态机已经不是 IDLE，再报跌倒不再重复发报警
        bus.fall("客厅")
        self.assertEqual(len(notifications(bus, "fall_alert")), 2)

        bus.button("ok")
        self.assertEqual(bus.fsm.state, "IDLE")
        cancels = notifications(bus, "cancel")
        self.assertEqual(len(cancels), 2)
        self.assertEqual({n["contact"] for n in cancels}, {"daughter", "son"})

    def test_sos_cancel_is_not_duplicated(self):
        bus, clock, arm = make_bus()
        bus.fall("卧室")
        clock.advance(6, bus)
        self.assertEqual(bus.fsm.state, "SOS")
        self.assertEqual(len(notifications(bus, "fall", "daughter")), 1)
        bus.fall("客厅")
        self.assertEqual(len(notifications(bus, "fall_alert")), 2)

        r = bus.voice("我没事")
        self.assertIn(r["intent"], ("elder_ok",))
        self.assertEqual(len(notifications(bus, "cancel", "daughter")), 1)
        self.assertEqual(len(notifications(bus, "cancel", "son")), 1)


class Dial120Test(unittest.TestCase):
    def test_elder_dial_notifies_all_and_logs_danger(self):
        bus, clock, arm = make_bus()
        r = master_service.dispatch_post(bus, "/api/dial120", {})
        self.assertTrue(r["ok"])
        self.assertEqual(len(notifications(bus, "120")), 2)
        self.assertEqual({n["contact"] for n in notifications(bus, "120")}, {"daughter", "son"})
        self.assertIn(line(bus, "dial_done"), said(bus))
        dials = events(bus, "dial")
        self.assertEqual(len(dials), 1)
        self.assertEqual(dials[0]["level"], "danger")

    def test_family_dial_only_logs_one_event(self):
        bus, clock, arm = make_bus()
        master_service.dispatch_post(bus, "/api/dial120", {})
        before = len(events(bus, "dial"))
        r = master_service.dispatch_post(bus, "/api/dial120", {"by": "son"})
        self.assertTrue(r["ok"])
        self.assertEqual(len(events(bus, "dial")), before + 1)
        self.assertEqual(len(notifications(bus, "120")), 2)

    def test_unknown_family_refused(self):
        bus, clock, arm = make_bus()
        r = master_service.dispatch_post(bus, "/api/dial120", {"by": "stranger"})
        self.assertFalse(r["ok"])
        self.assertEqual(events(bus, "dial"), [])
        self.assertEqual(notifications(bus, "120"), [])


class RereadTest(unittest.TestCase):
    def test_reread_family_text_message_twice(self):
        bus, clock, arm = make_bus()
        text = "妈，记得按时吃药哈"
        r = master_service.dispatch_post(bus, "/api/family/message",
                                         {"contact": "daughter", "text": text})
        self.assertTrue(r["ok"])
        expected = bus.llm.scripted("family_message", contact=contact(bus, "daughter")["name"], text=text)
        self.assertIn("您女儿给您留言", expected)
        self.assertEqual(said(bus).count(expected), 1)

        ts = next(m["ts"] for m in reversed(bus.messages) if m.get("text") == text)
        r = master_service.dispatch_post(bus, "/api/elder/reread", {"ts": ts})
        self.assertTrue(r["ok"])
        self.assertEqual(said(bus).count(expected), 2)

        r = master_service.dispatch_post(bus, "/api/elder/reread", {"ts": ts + 1000})
        self.assertFalse(r["ok"])
        self.assertIn("没有这条留言", r["error"])


class ResetTest(unittest.TestCase):
    def test_reset_clears_call_and_audio(self):
        bus, clock, arm = make_bus()
        connect_call(bus)
        vid = bus.voice_upload("daughter", "elder", "audio/webm", b"x", "1")["voice_id"]
        self.assertIsNotNone(bus.voice_audio(vid))

        bus.reset()
        self.assertIsNone(bus.snapshot()["comms"]["call"])
        self.assertIsNone(bus.voice_audio(vid))
        self.assertIsNone(bus.snapshot()["comms"]["play"])


class EchoUnaffectedTest(unittest.TestCase):
    def test_text_and_voice_then_echo(self):
        bus, clock, arm = make_bus()
        master_service.dispatch_post(bus, "/api/family/message",
                                     {"contact": "daughter", "text": "妈，我今天下班就回去"})
        bus.voice_upload("daughter", "elder", "audio/webm", b"voice", "1")
        self.assertTrue(bus.echo_authorize("daughter")["ok"])

        r = bus.voice("我想女儿了")
        self.assertTrue(r["ok"])
        self.assertEqual(r["intent"], "echo")
        self.assertTrue(bus.style.is_authorized("daughter"))


class CommsHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus, cls.clock, cls.arm = make_bus()
        cls.server = master_service.serve(cls.bus, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, args=(0.02,), daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop_ticker.set()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.bus.reset()
        self.bus.clients.clear()

    def request(self, method, path, body=None, ctype=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if ctype:
            h["Content-Type"] = ctype
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data, status = resp.read(), resp.status
        rh = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return status, data, rh

    def test_multipart_upload_poll_and_raw_voice(self):
        boundary = "----CareMateTestBoundary"
        wav = wav_bytes(1.5)

        def part(name, value, filename=None, ctype=None):
            head = '--%s\r\nContent-Disposition: form-data; name="%s"' % (boundary, name)
            if filename:
                head += '; filename="%s"' % filename
            head += "\r\n"
            if ctype:
                head += "Content-Type: %s\r\n" % ctype
            head += "\r\n"
            raw = value if isinstance(value, bytes) else value.encode("utf-8")
            return head.encode("utf-8") + raw + b"\r\n"

        body = b"".join([
            part("audio", wav, "msg.wav", "audio/wav"),
            part("time", "2026-10-24 19:30:05"),
            part("duration", "2.5"),
            part("elder", "hand-A"),
        ]) + ("--%s--\r\n" % boundary).encode("utf-8")

        status, data, _ = self.request(
            "POST", "/api/voice_msg", body,
            ctype="multipart/form-data; boundary=" + boundary)
        self.assertEqual(status, 200)
        j = json.loads(data.decode("utf-8"))
        self.assertTrue(j["ok"])
        self.assertEqual(j["to"], ["daughter", "son"])
        self.assertEqual(j["dur"], 2.5)
        via = [m for m in self.bus.messages
               if m.get("kind") == "voice" and m.get("via") == "hand-A"]
        self.assertEqual(len(via), 2)
        self.assertEqual({m["to"] for m in via}, {"daughter", "son"})

        status, data, _ = self.request("GET", "/api/poll?client=elder")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data.decode("utf-8"))["state"]["comms"]["online"]["elder"])
        self.assertTrue(self.bus.comms.online("elder"))

        raw = b"\x1aE\xdf\xa3raw-webm-bytes"
        status, data, _ = self.request(
            "POST", "/api/voice_msg?from=daughter&to=elder&dur=2", raw, ctype="audio/webm")
        self.assertEqual(status, 200)
        j = json.loads(data.decode("utf-8"))
        self.assertTrue(j["ok"])
        self.assertEqual(j["to"], ["elder"])
        vid = j["voice_id"]

        status, data, rh = self.request("GET", "/api/voice_msg?id=%s" % vid)
        self.assertEqual(status, 200)
        self.assertEqual(data, raw)
        self.assertEqual(rh.get("content-type"), "audio/webm")
        self.assertEqual(self.bus.voice_audio(vid)["data"], raw)

        status, data, _ = self.request("GET", "/api/voice_msg?id=999999")
        self.assertEqual(status, 404)

    def test_oversize_content_length_is_413(self):
        # HTTP 层给 multipart 的表单边界/头部留了 64 KB 余量；发过这个上限才会在读正文前回 413。
        big = MAX_VOICE_BYTES + 64 * 1024 + 1
        status, data, _ = self.request(
            "POST", "/api/voice_msg?from=daughter&to=elder&dur=2", b"x",
            ctype="audio/webm", headers={"Content-Length": str(big)})
        self.assertEqual(status, 413)

    def test_call_sdp_missing_id(self):
        status, data, _ = self.request("GET", "/api/call/sdp?id=999&which=offer")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data.decode("utf-8"))["ok"])


class IntentTest(unittest.TestCase):
    def test_call_and_voice_intents(self):
        self.assertEqual(intent.classify("接电话", {"call_incoming": True}), ("call_answer", {}))
        self.assertEqual(intent.classify("不接", {"call_incoming": True}), ("call_hangup", {}))
        self.assertEqual(intent.classify("挂了", {"call_active": True}), ("call_hangup", {}))
        self.assertNotEqual(intent.classify("挂了", {})[0], "call_hangup")
        self.assertEqual(intent.classify("听留言", {}), ("voice_play", {}))
        bus, clock, arm = make_bus()
        name, slots = intent.classify("给女儿留言：我挺好的", {"contacts": bus.cfg["contacts"]})
        self.assertEqual(name, "message")
        self.assertEqual(slots["contact"], "daughter")


if __name__ == "__main__":
    unittest.main()
