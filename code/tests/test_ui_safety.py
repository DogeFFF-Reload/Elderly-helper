"""Execute the actual UI JavaScript with a fake DOM; no browser, audio or devices."""
import shutil
import subprocess
import unittest
from pathlib import Path


class UiSafetyTest(unittest.TestCase):
    def test_alarm_receipts_and_medication_refresh(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "UI regression needs Node.js on PATH")
        result = subprocess.run([node, str(Path(__file__).with_name("ui_safety_cases.js"))],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UI safety cases passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
