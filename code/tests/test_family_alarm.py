"""Actual bedside contract, lost-response retries, background delivery. No devices."""
import json
from concurrent.futures import ThreadPoolExecutor
import socket
import threading
import unittest
from unittest.mock import patch

from family_alarm_receiver import AlarmReceiver
from gesture.fall_rules import FallDetector
from vision import synth
from vision.family_alarm import AlarmClient, AlarmPoster, base_url, request_json
from vision.fall_bridge import keypoint_source, run


class FamilyAlarmTest(unittest.TestCase):
    def setUp(self):
        connect = socket.create_connection

        def loopback_only(address, *args, **kwargs):
            self.assertEqual(address[0], "127.0.0.1", "Real service must never be contacted")
            return connect(address, *args, **kwargs)

        guard = patch("socket.create_connection", side_effect=loopback_only)
        guard.start()
        self.addCleanup(guard.stop)

    def test_actual_receiver_stores_one_alarm_for_retries_and_omits_fake_confidence(self):
        with AlarmReceiver() as receiver:
            client = AlarmClient(receiver.url, mode="live", sleep=lambda _: None)
            self.assertEqual(client.preflight()["status"], "dry_accepted")
            first = client.deliver("test-one")
            second = client.deliver("test-one")
            self.assertEqual(first["status"], "bedside_accepted")
            self.assertEqual(first["alarm_id"], second["alarm_id"])
            self.assertFalse(first["dedup"])
            self.assertTrue(second["dedup"])
            self.assertIsNone(first["phone_received"])
            self.assertEqual(len(receiver.alarms), 1)
            self.assertFalse(receiver.alarms[0]["acked"])
            for body in receiver.requests:
                self.assertNotIn("conf", body)
                self.assertNotIn("keypoints", body)
            stored = request_json("GET", receiver.url + "/alarms")
            self.assertEqual(stored["alarms"][0]["event_id"], "test-one")

    def test_lost_http_response_retries_same_event_and_receiver_deduplicates(self):
        with AlarmReceiver() as receiver:
            receiver.drop_next = True
            sleeps = []
            client = AlarmClient(receiver.url, mode="live", sleep=sleeps.append)
            result = client.deliver("lost-response")
            self.assertEqual(result["attempts"], 2)
            self.assertTrue(result["dedup"])
            self.assertEqual(sleeps, [1])
            self.assertEqual([r["event_id"] for r in receiver.requests], ["lost-response"] * 2)
            self.assertEqual(len(receiver.alarms), 1)

    def test_detector_to_family_poll_and_ack_for_fast_slow_and_normal_inputs(self):
        from verify_fall_chain import check_case
        for scenario in ("fall", "slow_floor", "sit"):
            with self.subTest(scenario=scenario):
                result = check_case(scenario)
                self.assertTrue(result["ok"])
                self.assertEqual(result["simulated_family_poll_count"], 0 if scenario == "sit" else 1)
                self.assertIsNone(result["phone_received"])
                self.assertFalse(result["real_phone_notification_verified"])

    def test_timeout_after_storage_also_keeps_the_same_id(self):
        with AlarmReceiver() as receiver:
            count = 0

            def transport(*args, **kwargs):
                nonlocal count
                result = request_json(*args, **kwargs)
                count += 1
                if count == 1:
                    raise TimeoutError("response not observed")
                return result

            result = AlarmClient(receiver.url, mode="live", transport=transport,
                                 sleep=lambda _: None).deliver("timeout-stored")
            self.assertEqual(result["attempts"], 2)
            self.assertTrue(result["dedup"])
            self.assertEqual(len(receiver.alarms), 1)

    def test_dry_does_not_store_an_alarm_in_actual_receiver(self):
        with AlarmReceiver() as receiver:
            client = AlarmClient(receiver.url, mode="dry")
            client.preflight()
            self.assertEqual(client.deliver("dry-event")["status"], "dry_accepted")
            self.assertEqual(receiver.alarms, [])
            self.assertTrue(all(r["dry"] is True for r in receiver.requests))

    def test_concurrent_retries_store_one_alarm_with_one_fresh_receipt(self):
        with AlarmReceiver() as receiver:
            barrier = threading.Barrier(6)

            def send(_index):
                barrier.wait(timeout=3)
                return AlarmClient(receiver.url, mode="live").deliver("concurrent-event")

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(send, range(6)))
            self.assertEqual(len(receiver.alarms), 1)
            self.assertTrue(all(r["status"] == "bedside_accepted" for r in results))
            self.assertEqual(sum(not r["dedup"] for r in results), 1)
            self.assertEqual(len({r["alarm_id"] for r in results}), 1)

    def test_distinct_events_in_the_same_millisecond_have_distinct_alarm_ids(self):
        with AlarmReceiver() as receiver, patch("time.time", return_value=1800000000.123):
            client = AlarmClient(receiver.url, mode="live")
            first, second = client.deliver("first"), client.deliver("second")
            self.assertNotEqual(first["alarm_id"], second["alarm_id"])
            self.assertEqual(len(receiver.alarms), 2)

    def test_off_mode_never_uses_transport(self):
        client = AlarmClient(transport=lambda *a, **kw: self.fail("Network call in off mode"))
        self.assertEqual(client.preflight()["status"], "disabled")
        result = client.deliver("off-event")
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["attempts"], 0)

    def test_bad_receipts_are_not_reported_as_success(self):
        good = {"ok": True, "kind": "fall", "src": "cam-A", "event_id": "one",
                "id": "al_1", "dedup": False}
        responses = [None, [], {"ok": False}, {"ok": True},
                     dict(good, event_id="other"), dict(good, src="other"),
                     dict(good, dry=True), dict(good, dedup="false"), dict(good, id="")]
        for response in responses:
            with self.subTest(response=response):
                client = AlarmClient(mode="live", attempts=1,
                                     transport=lambda *a, **kw: response)
                self.assertEqual(client.deliver("one")["status"], "failed")

    def test_failure_is_bounded_and_does_not_leak_error_text(self):
        bodies, waits = [], []

        def fail(method, url, body, **kwargs):
            bodies.append(dict(body))
            raise TimeoutError("secret-url-password")

        client = AlarmClient(mode="live", transport=fail, sleep=waits.append)
        result = client.deliver("persist-this-id")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(waits, [1, 2])
        self.assertEqual(len({b["event_id"] for b in bodies}), 1)
        self.assertIsNone(result["bedside_stored"])
        self.assertNotIn("secret", json.dumps(result))

    def test_preflight_rejects_older_or_unknown_receiver_before_any_post(self):
        for health in ({"ok": True, "ver": "5.6"}, {"ok": True}, [], {"ok": False, "ver": "5.7"}):
            calls = []

            def transport(method, url, **kw):
                calls.append(method)
                return health

            with self.subTest(health=health), self.assertRaises(ValueError):
                AlarmClient(mode="dry", transport=transport).preflight()
            self.assertEqual(calls, ["GET"])

    def test_dry_requires_explicit_dry_echo_with_matching_event(self):
        for response in ({"ok": True}, {"ok": True, "dry": True, "echo": {}},
                         {"ok": True, "dry": False, "echo": {"kind": "fall", "src": "cam-A", "event_id": "dry"}}):
            with self.subTest(response=response):
                client = AlarmClient(mode="dry", attempts=1, transport=lambda *a, **k: response)
                self.assertEqual(client.deliver("dry")["status"], "failed")

    def test_synthetic_detector_contract_all_nine_scenarios(self):
        with AlarmReceiver() as receiver:
            for name, scenario in synth.SCENARIOS.items():
                with self.subTest(name=name):
                    receiver.alarms.clear()
                    poster = AlarmPoster(AlarmClient(receiver.url, mode="live"), log=lambda _: None)
                    try:
                        events = run(keypoint_source(synth.build(name), 4/3, realtime=False),
                                     FallDetector(scenario.get("rules")), poster, "test",
                                     photos=False, log=lambda _: None)
                    finally:
                        self.assertTrue(poster.close(3))
                    self.assertEqual(len(events), len(scenario["expect"]))
                    self.assertEqual(len(receiver.alarms), len(events))
                    self.assertEqual(len({a["event_id"] for a in receiver.alarms}), len(events))

    def test_background_network_wait_cannot_block_detector_and_id_is_journaled_first(self):
        entered, release = threading.Event(), threading.Event()
        journal = []

        def slow(method, url, body, **kw):
            self.assertTrue(any(r["event_id"] == body["event_id"] for r in journal))
            entered.set()
            release.wait(3)
            return dict(body, ok=True, id="al_one", dedup=False)

        poster = AlarmPoster(AlarmClient(mode="live", transport=slow), log=lambda _: None,
                             on_change=journal.append)
        self.addCleanup(release.set)
        try:
            events = run(keypoint_source(synth.build("fall"), 4/3, realtime=False),
                         FallDetector(), poster, "test", photos=False, log=lambda _: None)
            self.assertEqual(len(events), 1)
            self.assertTrue(entered.wait(1))
            self.assertFalse(poster.close(.01))
            self.assertEqual(poster.snapshot()[0]["status"], "queued")
        finally:
            release.set()
            self.assertTrue(poster.close(3))
        self.assertEqual(poster.snapshot()[0]["status"], "bedside_accepted")

    def test_queue_overflow_is_visible_and_sender_can_close(self):
        entered, release = threading.Event(), threading.Event()

        def slow(method, url, body, **kw):
            entered.set()
            release.wait(3)
            return dict(body, ok=True, id="al_one", dedup=False)

        poster = AlarmPoster(AlarmClient(mode="live", transport=slow), queue_size=1, log=lambda _: None)
        try:
            poster.fall("test")
            self.assertTrue(entered.wait(1))
            poster.fall("test")
            poster.fall("test")
            self.assertEqual(poster.snapshot()[-1]["error_type"], "queue_full")
        finally:
            release.set()
            self.assertTrue(poster.close(3))
        with self.assertRaises(RuntimeError):
            poster.fall("after-close")

    def test_invalid_configuration_is_rejected_before_network(self):
        for options in ({"src": "x" * 41}, {"src": ""}, {"timeout": float("nan")},
                        {"attempts": 0}, {"mode": "yes"}, {"backoff": (-1, 2)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AlarmClient(**options)
        for url in ("ftp://localhost", "http://u:p@host", "http://host/pose/wave",
                    "http://host?password=secret", "http://host:bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                base_url(url)
        client = AlarmClient()
        for eid in (None, "", "x" * 65):
            with self.subTest(eid=eid), self.assertRaises(ValueError):
                client.deliver(eid)


if __name__ == "__main__":
    unittest.main()
