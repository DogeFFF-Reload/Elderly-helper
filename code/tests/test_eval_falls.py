"""Offline analysis must not invent labels or count missing input as success."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vision import eval_falls as evaluation, synth
from vision.kpio import KeypointWriter
from vision.rehearsal import builtin_cases


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        guard = patch.object(socket, 'create_connection', side_effect=AssertionError('No network or devices'))
        guard.start()
        self.addCleanup(guard.stop)

    def recording(self, name='fall_named_but_normal.jsonl', scene='sit', source='pc_camera:0'):
        path = self.root / name
        writer = KeypointWriter(path, source)
        for t, kp in synth.build(scene):
            writer.write(t, kp, 4/3)
        writer.close()
        return path

    def label(self, path, expected=0, **extra):
        return {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'expected_events': expected, **extra}

    def test_filename_never_supplies_an_expected_result(self):
        for name, scene in (('fall_normal.jsonl', 'sit'), ('normal_fall.jsonl', 'fall')):
            row = evaluation.evaluate_file(self.recording(name, scene), {})
            self.assertEqual(row['status'], 'unlabeled')
            self.assertIsNone(row['expected_events'])
            self.assertFalse(row['label_eligible'])
            self.assertEqual(len(row['events']), int(scene == 'fall'))

    def test_gap_resets_candidate_like_live_bridge_and_is_excluded_from_statistics(self):
        frames = next(frames for name, frames, *_ in builtin_cases() if name == 'input_gap')
        row = evaluation.evaluate_case('gap', frames, expected_events=0)
        self.assertEqual(row['events'], [])
        self.assertEqual(row['input_quality']['input_gaps'], 1)
        self.assertEqual(row['status'], 'unavailable')
        self.assertFalse(row['label_eligible'])

    def test_no_usable_pose_cannot_pass_a_normal_label(self):
        row = evaluation.evaluate_case('empty', [(i/10, None) for i in range(10)], expected_events=0)
        self.assertEqual(row['status'], 'unavailable')
        self.assertFalse(row['label_eligible'])

    def test_invalid_time_and_nonfinite_coordinates_fail_explicitly(self):
        frames = synth.build('sit')[:4]
        for bad in ([(0, frames[0][1]), (0, frames[1][1])], [(float('nan'), None)],
                    [(0, {'head': [float('nan'), .3]})]):
            row = evaluation.evaluate_case('bad', bad, expected_events=0)
            self.assertEqual(row['status'], 'error')
            self.assertFalse(row['label_eligible'])

    def test_labels_are_bound_to_exact_file_bytes(self):
        path = self.recording()
        label = self.label(path)
        self.assertEqual(evaluation.evaluate_file(path, {}, label)['status'], 'pass')
        path.write_text(path.read_text(encoding='utf-8')+'\n', encoding='utf-8')
        row = evaluation.evaluate_file(path, {}, label)
        self.assertEqual(row['status'], 'error')
        self.assertFalse(row['label_eligible'])

    def test_window_scopes_label_and_onsets_require_real_timing(self):
        path = self.recording(scene='fall')
        row = evaluation.evaluate_file(path, {}, self.label(path, window=[0, 1]))
        self.assertEqual(row['status'], 'pass')
        self.assertTrue(all(e['t'] < 1 for e in row['events']))
        full = evaluation.evaluate_file(path, {}, self.label(path, 1, event_onsets_s=[2]))
        self.assertGreater(full['onset_delays_s'][0], 0)
        early = evaluation.evaluate_file(path, {}, self.label(path, 1, event_onsets_s=[7]))
        self.assertIsNone(early['onset_delays_s'])
        self.assertEqual(early['status'], 'mismatch')

    def test_manifest_rejects_duplicates_bad_windows_and_bad_event_counts(self):
        path = self.recording()
        manifest = self.root / 'labels.json'
        valid = self.label(path)
        manifest.write_text(json.dumps({'schema_version': 1, 'recordings': [valid]}), encoding='utf-8')
        self.assertIn(path.resolve(), evaluation.read_labels(manifest))
        for entries in ([valid, valid], [self.label(path, True)], [self.label(path, window=[2, 1])],
                        [self.label(path, 1, event_onsets_s=[float('nan')])]):
            manifest.write_text(json.dumps({'schema_version': 1, 'recordings': entries}), encoding='utf-8')
            with self.assertRaises(ValueError):
                evaluation.read_labels(manifest)

    def test_synthetic_recordings_and_unlabeled_files_do_not_enter_recorded_label_statistics(self):
        normal = self.recording('normal.jsonl')
        synthetic = self.recording('synthetic.jsonl', 'fall', source='synthetic:fall')
        rows = [evaluation.evaluate_file(normal, {}, self.label(normal)),
                evaluation.evaluate_file(synthetic, {}, self.label(synthetic, 1)),
                evaluation.evaluate_file(normal, {})]
        self.assertEqual(evaluation.summarize(rows, 'recorded')['eligible_clips'], 1)
        self.assertEqual(evaluation.summarize(rows, 'synthetic')['eligible_clips'], 1)

    def test_synthetic_repeat_fall_checks_event_count(self):
        row = evaluation.evaluate_case('refall', synth.build('refall'), expected_events=2, source_kind='synthetic')
        self.assertEqual(row['status'], 'pass')
        self.assertEqual(len(row['events']), 2)
        row = evaluation.evaluate_case('refall', synth.build('refall'), expected_events=1)
        self.assertEqual(row['status'], 'mismatch')

    def test_cli_saves_report_without_models_network_or_overwriting(self):
        path = self.recording()
        output = self.root / 'output'
        with contextlib.redirect_stdout(io.StringIO()), patch.dict('sys.modules', {'cv2': None, 'mediapipe': None}):
            self.assertEqual(evaluation.main([str(path), '--output', str(output)]), 0)
        report = json.loads((output/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['recorded_labels']['eligible_clips'], 0)
        self.assertEqual(report['cases'][0]['status'], 'unlabeled')
        with zipfile.ZipFile(output.with_suffix('.zip')) as package:
            self.assertIsNone(package.testzip())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            evaluation.main([str(path), '--output', str(output)])


if __name__ == '__main__':
    unittest.main()
