#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_bedside_core.py — 手板执行端核心逻辑离线单测(零硬件依赖)

覆盖三块纯/近纯逻辑:
  1) 警报:毫秒 id 唯一性 / event_id 去重 / ack / 掉电恢复(alarms.json 重载)
  2) 语音词表:长词优先匹配语义(_voice_open_match)
  3) 安全:留言 id 白名单(_msg_paths 防路径穿越)

设计要点:
  - AST白名单提取纯逻辑,不导入硬件服务顶层或任何驱动/SDK;
  - 警报和留言路径指到临时目录,不改环境变量,结束清理;
  - 实际报警函数、词表和路径规则来自service_handboard源码,不启动HTTP/线程。

跑法: python -m unittest test_bedside_core -v      (或 pytest 也认)
退出码: 0=全过;GitHub Actions bedside-ci 在跑。
"""
import json
import sys
import tempfile
import unittest

def _import_service():
    """只提取仓库报警/词表/路径纯逻辑,不导入服务顶层、串口或SDK。"""
    from core_fixture import load_service
    return load_service(AL_DIR)


AL_DIR_CONTEXT = tempfile.TemporaryDirectory(prefix="bedside_test_alarm_")
AL_DIR = AL_DIR_CONTEXT.name
svc = _import_service()


def tearDownModule():
    AL_DIR_CONTEXT.cleanup()


# ---------- 警报 ----------

class TestAlarmIds(unittest.TestCase):
    def setUp(self):
        svc._alarm_mem.clear()

    def test_ms_suffix_no_collision_same_second(self):
        """同一条内连续 50 次建账(同秒窗口内),id 必须全不撞号。"""
        ids = [svc.alarm_new("fall", "t")["id"] for _ in range(50)]
        self.assertEqual(len(ids), len(set(ids)), "同秒 id 撞号: %s" % ids[:4])

    def test_id_format(self):
        aid = svc.alarm_new()["id"]
        self.assertTrue(aid.startswith("al_"), aid)
        self.assertRegex(aid, r"^al_\d{8}_\d{6}_\d{3}$", "id 形态异常: %s" % aid)


class TestAlarmDedup(unittest.TestCase):
    def setUp(self):
        svc._alarm_mem.clear()

    def test_dedup_same_event_id(self):
        a = svc.alarm_incoming("fall", "cam-A", 0.9, eid="evt-1")
        b = svc.alarm_incoming("fall", "cam-A", 0.9, eid="evt-1")
        self.assertFalse(a["dedup"])
        self.assertTrue(b["dedup"])
        self.assertEqual(a["id"], b["id"])

    def test_dedup_survives_ack(self):
        """已 ack 的事件重发仍去重(不会「确认过就再响一次」)。"""
        a = svc.alarm_incoming("fall", "", None, eid="evt-2")
        svc.alarms_ack(a["id"])
        b = svc.alarm_incoming("fall", "", None, eid="evt-2")
        self.assertTrue(b["dedup"])
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["acked"])

    def test_no_eid_always_new(self):
        x = svc.alarm_incoming("fall", "", None)
        y = svc.alarm_incoming("fall", "", None)
        self.assertFalse(x["dedup"])
        self.assertNotEqual(x["id"], y["id"])

    def test_different_eids_different_alarms(self):
        a = svc.alarm_incoming("fall", "", None, eid="e1")
        b = svc.alarm_incoming("fall", "", None, eid="e2")
        self.assertNotEqual(a["id"], b["id"])


class TestAlarmPersist(unittest.TestCase):
    def test_persist_and_reload(self):
        """掉电恢复:新建→写盘→清内存→alarms_load→回来。"""
        svc._alarm_mem.clear()
        a = svc.alarm_new("fall", "persist-src")
        with open(svc.ALARMS_JSON, "r", encoding="utf-8") as f:
            disk = json.load(f)
        self.assertTrue(any(x["id"] == a["id"] for x in disk), "警报没写盘")
        svc._alarm_mem.clear()
        svc.alarms_load()
        self.assertTrue(any(x["id"] == a["id"] for x in svc._alarm_mem))


# ---------- 语音词表(长词优先) ----------

class TestVoiceOpenMatch(unittest.TestCase):
    @staticmethod
    def norm(s):
        return s.replace(" ", "").lower()

    def test_exact_hit(self):
        self.assertEqual(svc._voice_open_match(self.norm("比个ok")), "ok")

    def test_longest_keyword_wins(self):
        """"比一个ok的手势" 命中"ok手势"(4字)而非 "ok"(2字)。"""
        self.assertEqual(svc._voice_open_match(self.norm("比一个ok的手势")), "ok")

    def test_message_intents(self):
        self.assertEqual(svc._voice_open_match(self.norm("我想留言")), "message_record")
        self.assertEqual(svc._voice_open_match(self.norm("听留言")), "message_play")
        self.assertEqual(svc._voice_open_match(self.norm("有没有回话")), "message_play")

    def test_rehab_gestures(self):
        self.assertEqual(svc._voice_open_match(self.norm("拉个勾")), "pinch_4")
        self.assertEqual(svc._voice_open_match(self.norm("卷小指")), "curl_pk")

    def test_miss_returns_none(self):
        self.assertIsNone(svc._voice_open_match(self.norm("打开电视")))

    def test_action_whitelist_covers_vocab(self):
        """白名单集合必须装下词表全部目标(不然词匹配到执行被拒)。"""
        known = {"rps_random"} | set(svc.WAYPOINTS) | {"message_record", "message_play"}
        for kw, act in svc.VOICE_OPEN.items():
            self.assertTrue(act in svc.ALL_ACTIONS or act in known,
                            "词 [%s] 的动作 %s 不在 ALL_ACTIONS/白名单" % (kw, act))


# ---------- 留言 id 安全 ----------

class TestMsgPathSafety(unittest.TestCase):
    def test_traversal_rejected(self):
        for bad in ("../evil", "..\\evil", "a/b", "a.b", "", "x" * 65,
                    "中文", "sp ace", "$pwn"):
            _, wav = svc._msg_paths(bad)
            self.assertIsNone(_, "路径穿越 id 未拦截: %r" % bad)
            self.assertIsNone(wav)

    def test_valid_id_accepted(self):
        part, wav = svc._msg_paths("msg_20261024_215005")
        self.assertTrue(part.endswith("msg_20261024_215005.part"))
        self.assertTrue(wav.endswith("msg_20261024_215005.wav"))
        self.assertTrue(str(part).startswith(svc.MSG_DIR))


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False, verbosity=2).result
    finally:
        AL_DIR_CONTEXT.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)   # 失败必须让进程退出码非零(CI 才会变红)
