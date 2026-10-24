"""Saved-data audits use generated CSV fixtures; no camera, audio, or network."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from camera import phone_calibration_report as reports
from camera.phone_calibrate import FIELDS, STAGES, phase_statistics


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "test_session"
        self.directory.mkdir()
        self.phases = []
        rows, events = [], []
        sequence = 0
        for r in range(1, 3):
            for stage, name, _ in STAGES:
                subset = []
                for i, energy in enumerate(("", 1.2, 1.4, 1.6)):
                    sequence += 1
                    subset.append(dict(round=r, stage=stage, received_utc="2026-10-24T00:00:00+00:00",
                                       session_s=sequence * .5, stage_s=i * .5, source_sequence=sequence,
                                       frame_age_s=.01, delta_s="" if i == 0 else .5,
                                       baseline=int(i == 0), energy=energy, centroid_y="" if i == 0 else .5))
                rows.extend(subset)
                self.phases.append(dict(round=r, stage=stage, name=name, status="complete",
                                        statistics=phase_statistics(subset, 2, .5)))
                events.append(dict(event="phase_end", round=r, stage=stage, status="complete"))
        self.rows = rows
        with (self.directory / "samples.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        self.summary = dict(session_id="test_session", status="complete", message="done",
                            started_utc="2026-10-24T00:00:00+00:00", ended_utc="2026-10-24T00:01:00+00:00",
                            settings=dict(rounds=2, duration_s=2, sample_interval_s=.5), phases=self.phases)
        self.save_summary()
        events.append(dict(event="session_end", status="complete"))
        (self.directory / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    def save_summary(self):
        (self.directory / "summary.json").write_text(json.dumps(self.summary), encoding="utf-8")

    def test_operator_camera_annotations_exclude_moving_rounds_from_baseline(self):
        report = reports.analyze(self.directory, fixed_rounds=[2], moving_rounds=[1], operator_note="operator confirmed")
        self.assertTrue(report["integrity"]["ok"], report["integrity"])
        self.assertEqual(report["sample_count"], 24)
        self.assertEqual(report["energy_count"], 18)
        self.assertEqual(report["fixed_complete_rounds"], [2])
        self.assertEqual(report["aggregates"]["fixed_camera"]["walk"]["energy"]["count"], 3)
        self.assertEqual(report["aggregates"]["all_scheduled"]["walk"]["energy"]["count"], 6)
        self.assertIsNone(report["fall_threshold_recommendation"])

    def test_unconfirmed_camera_is_not_assumed_fixed(self):
        report = reports.analyze(self.directory)
        self.assertEqual(report["fixed_complete_rounds"], [])
        self.assertEqual(report["aggregates"]["fixed_camera"]["stand"]["samples"], 0)

    def test_summary_tampering_is_detected(self):
        self.phases[0]["statistics"]["energy_max"] = 19
        self.save_summary()
        report = reports.analyze(self.directory)
        self.assertFalse(report["integrity"]["ok"])
        self.assertTrue(any("energy_max" in error for error in report["integrity"]["errors"]))

    def test_duplicate_source_frames_and_nonfinite_values_detected(self):
        path = self.directory / "samples.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[1]["source_sequence"] = rows[0]["source_sequence"]
        def write():
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
        write()
        self.assertFalse(reports.analyze(self.directory)["integrity"]["ok"])
        rows[1]["energy"] = "nan"
        write()
        with self.assertRaises(ValueError):
            reports.analyze(self.directory)

    def test_conflicting_or_out_of_range_annotations_rejected(self):
        for fixed, moved in (([1], [1]), ([3], [])):
            with self.subTest(fixed=fixed, moved=moved), self.assertRaises(ValueError):
                reports.analyze(self.directory, fixed_rounds=fixed, moving_rounds=moved)

    def test_replay_is_strict_and_cooldown_uses_saved_timestamps(self):
        phase = dict(round=1, stage="walk", rows=[dict(energy=energy, session_s=t)
                                                for t, energy in enumerate((1, 1, 1, 2, 2, 2))])
        result = reports.replay([phase], thresholds=[1], conn=3, cooldown=30)
        self.assertEqual(result[0]["count"], 1)
        self.assertEqual(result[0]["simulated_events"][0]["session_s"], 5)
        self.assertEqual(reports.replay([phase], thresholds=[38])[0]["above_metric_maximum"], True)


if __name__ == "__main__":
    unittest.main()
