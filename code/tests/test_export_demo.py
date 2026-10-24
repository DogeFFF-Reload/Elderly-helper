"""Video export timing, failed encoders and no capture using fake processes."""
import contextlib
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from PIL import Image, ImageFont
except ImportError:
    Image = ImageFont = None

import master_service
from vision import export_demo as export, integrated_demo as demo


class ExportTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    @unittest.skipIf(Image is None, 'Pillow is an optional vision dependency')
    def test_video_sink_uses_pipe_input_and_rejects_camera_frames(self):
        writes = []
        process = SimpleNamespace(stdin=SimpleNamespace(write=lambda b:writes.append(len(b)), close=lambda:None),
                                  wait=lambda **kw:0)
        renderer = SimpleNamespace(fonts={28:ImageFont.load_default()}, render=lambda t:Image.new('RGB', (64,64)))
        with patch.object(export, 'DemoRenderer', return_value=renderer), \
                patch.object(export.subprocess, 'Popen', return_value=process) as start:
            sink = export.VideoSink(self.root, encoder='fake-encoder', fps=2)
            clock = [100.0]
            target = SimpleNamespace(frame=None, now=lambda:clock[0], scene='saved poses', source='recorded')
            sink.show(target)
            clock[0] += 2
            sink.show(target)
            target.scene = 'synthetic fall'
            sink.show(target)
            self.assertEqual(len(sink.scenes), 2)
            self.assertGreater(sink.scenes[1]['video_start_s'], sink.scenes[0]['video_start_s'])
            before = sink.frames
            target.current = {'guard': {'state': 'SOS'}, 'last_say': {'text': 'mock notification'}}
            sink.show(target)
            self.assertEqual(sink.frames-before, sink.fps)
            self.assertEqual(start.call_args.args[0][start.call_args.args[0].index('-i')+1], 'pipe:0')
            self.assertIn('-an', start.call_args.args[0])
            target.frame = object()
            with self.assertRaises(RuntimeError):
                sink.show(target)
            sink.close()
        self.assertEqual(len(writes), sink.frames)
        self.assertFalse(sink.realtime)
        self.assertFalse(sink.keep_open)

    @unittest.skipIf(Image is None, 'Pillow is an optional vision dependency')
    def test_encoder_failure_is_reported(self):
        process = SimpleNamespace(stdin=SimpleNamespace(close=lambda:None), wait=lambda **kw:1)
        with patch.object(export, 'DemoRenderer'), patch.object(export.subprocess, 'Popen', return_value=process):
            sink = export.VideoSink(self.root, encoder='fake-encoder')
            with self.assertRaises(RuntimeError):
                sink.close()
        self.assertTrue(sink.error_log.closed)

    @unittest.skipIf(Image is None, 'Pillow is an optional vision dependency')
    def test_offline_renderer_does_not_import_camera_or_gui_modules(self):
        font = ImageFont.load_default()
        dummy_font = self.root/'font.ttf'
        dummy_font.write_bytes(b'placeholder')
        target = SimpleNamespace(scene='mock', source='synthetic', frame=None, kp=None, logs=[], speed=2,
                                 quality_state='', current={'guard':{'state':'IDLE'}},
                                 master=SimpleNamespace(arm=SimpleNamespace(sent=[])))
        with patch.dict('sys.modules', {'cv2':None, 'mediapipe':None}), patch('PIL.ImageFont.truetype', return_value=font):
            renderer = demo.DemoRenderer(font_path=dummy_font, footer='offline mock')
            self.assertEqual(renderer.render(target).size, (1440,900))

    def test_complete_demo_can_export_without_sleep_hold_open_or_real_actuator(self):
        class OfflineSink:
            realtime = keep_open = native_window = False
            def __init__(self):
                self.scenes = []
            def show(self, target):
                self.scenes.append(target.scene)
                return True
        sink = OfflineSink()
        with patch.object(master_service, 'ArmClient', side_effect=AssertionError('No actuator')), \
                patch.object(demo.time, 'sleep', side_effect=AssertionError('Offline export must not sleep')), \
                patch.dict('sys.modules', {'cv2':None, 'mediapipe':None}):
            report = demo.run_full_demo(self.root, viewer=sink)
        self.assertTrue(report['ok'])
        self.assertIn('完整模拟演示通过', sink.scenes)
        self.assertFalse(report['camera_opened'])

    def test_invalid_limits_and_camera_option_fail_before_encoder_launch(self):
        for args in (['--fps','0'], ['--speed','nan'], ['--camera','0']):
            with self.subTest(args=args), patch.object(export, 'VideoSink', side_effect=AssertionError('No encoder')), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                export.main(args)


if __name__ == '__main__':
    unittest.main()
