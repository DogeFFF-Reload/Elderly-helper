"""Structured master events, durable retries and restart-safe identities; loopback only."""
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from family_alarm_receiver import AlarmReceiver
from vision.family_alarm import AlarmClient
from vision.rehearsal import MockMaster

SOURCE = Path(__file__).resolve().parents[2] / "bedside_family/bridges/fall_to_family.py"
SPEC = importlib.util.spec_from_file_location("family_bridge_under_test", SOURCE)
bridge_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_module)


class FamilyAlarmBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "bridge.json"
        connect = socket.create_connection

        def loopback_only(address, *args, **kwargs):
            self.assertEqual(address[0], "127.0.0.1")
            return connect(address, *args, **kwargs)

        guard = patch("socket.create_connection", side_effect=loopback_only)
        guard.start()
        self.addCleanup(guard.stop)

    def test_real_master_initial_fall_only_not_speech_photos_repeat_or_cancel(self):
        with AlarmReceiver() as receiver, MockMaster() as master:
            client = AlarmClient(receiver.url, mode="live")
            bridge = bridge_module.Bridge("http://127.0.0.1:%d" % master.port, client,
                                          self.state, log=lambda _: None)
            self.assertTrue(bridge.poll())
            master.request("/mock/fall", {})
            master.bus.events.add("say", "跌倒求救 fall")
            master.bus.events.add("fall", "收到摔倒现场照片", "warn")
            master.request("/mock/fall", {})
            self.assertTrue(bridge.poll())
            master.request("/mock/button?key=ok", {})
            self.assertTrue(bridge.poll())
            self.assertTrue(bridge.poll())
            self.assertEqual(len(receiver.alarms), 1)
            self.assertEqual(bridge.pending, [])
            master.request("/mock/fall", {})
            # The replay clock is explicit, so also advance it for distinct real occurrences.
            self.assertTrue(bridge.poll())
            self.assertEqual(len(receiver.alarms), 2)

    def test_startup_skips_old_alarm_history(self):
        with AlarmReceiver() as receiver, MockMaster() as master:
            master.request("/mock/fall", {})
            bridge = bridge_module.Bridge("http://127.0.0.1:%d" % master.port,
                                          AlarmClient(receiver.url, mode="live"), self.state,
                                          log=lambda _: None)
            self.assertTrue(bridge.poll())
            self.assertEqual(receiver.alarms, [])

    def test_master_snapshot_larger_than_alarm_receipt_limit_still_forwards_fall(self):
        with AlarmReceiver() as receiver, MockMaster() as master:
            bridge = bridge_module.Bridge("http://127.0.0.1:%d" % master.port,
                                          AlarmClient(receiver.url, mode="live"), self.state,
                                          log=lambda _: None)
            self.assertTrue(bridge.poll())
            for _ in range(60):
                master.bus.events.add("say", "x" * 2000)
            master.request("/mock/fall", {})
            self.assertTrue(bridge.poll())
            self.assertEqual(len(receiver.alarms), 1)

    def test_failed_pending_alarm_survives_restart_even_when_master_is_down(self):
        event = {"id": 3, "ts": 1000., "type": "fall", "level": "danger"}
        get = lambda *a, **kw: {"events": [event]}
        sent_ids = []

        def fail(method, url, body, **kw):
            sent_ids.append(body["event_id"])
            raise TimeoutError()

        client = AlarmClient("http://127.0.0.1:9999", mode="live", attempts=1, transport=fail)
        first = bridge_module.Bridge("http://127.0.0.1:8000", client, self.state,
                                    from_start=True, get=get, log=lambda _: None)
        self.assertFalse(first.poll())
        saved_id = json.loads(self.state.read_text(encoding="utf-8"))["pending"][0]["event_id"]

        def success(method, url, body, **kw):
            sent_ids.append(body["event_id"])
            return dict(body, ok=True, id="al_one", dedup=True)

        def master_offline(*a, **kw):
            raise ConnectionError()

        retry = bridge_module.Bridge("http://127.0.0.1:8000",
                                    AlarmClient(client.base, mode="live", attempts=1, transport=success),
                                    self.state, get=master_offline, log=lambda _: None)
        self.assertFalse(retry.poll())  # master is down, but alarm delivery still succeeded
        self.assertEqual(retry.pending, [])
        self.assertEqual(sent_ids, [saved_id, saved_id])

    def test_event_identity_survives_polling_but_does_not_collide_after_master_restart(self):
        event = {"id": 1, "ts": 1000., "type": "fall", "level": "danger"}
        original = bridge_module.event_id("http://master:8000", event)
        self.assertEqual(original, bridge_module.event_id("http://master:8000", dict(event)))
        self.assertNotEqual(original, bridge_module.event_id("http://master:8000", dict(event, ts=2000.)))
        self.assertNotEqual(original, bridge_module.event_id("http://other:8000", event))
        self.assertLessEqual(len(original), 64)

    def test_strict_matching_ignores_all_non_initial_events(self):
        for event in ({"type": "fall", "level": "warn"}, {"type": "fall_hint", "level": "danger"},
                      {"type": "say", "text": "跌倒"}, {"type": "notify", "detail": "fall"}, None):
            with self.subTest(event=event):
                self.assertFalse(bridge_module.looks_like_fall(event))
        self.assertTrue(bridge_module.looks_like_fall({"type": "fall", "level": "danger"}))

    def test_mode_or_endpoint_change_cannot_reuse_other_checkpoint(self):
        bridge = bridge_module.Bridge("http://127.0.0.1:8000", AlarmClient(mode="off"), self.state,
                                     get=lambda *a, **kw: {"events": []}, log=lambda _: None)
        bridge.poll()
        with self.assertRaises(ValueError):
            bridge_module.Bridge("http://127.0.0.1:8000", AlarmClient(mode="live"), self.state)

    def test_live_from_start_is_rejected_before_network(self):
        with context_stderr(), self.assertRaises(SystemExit):
            bridge_module.main(["--mode", "live", "--from-start", "--once"])


def context_stderr():
    import contextlib
    import io
    return contextlib.redirect_stderr(io.StringIO())


if __name__ == "__main__":
    unittest.main()
