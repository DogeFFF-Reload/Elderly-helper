"""Read-only field preflight checks stay conservative and deterministic."""
import io
import json
import unittest
from unittest import mock

from tools import hardware_preflight


class HardwarePreflightTest(unittest.TestCase):
    def test_mock_plan_never_claims_real_ready(self):
        report = hardware_preflight.plan("mock", "COM5", "COM6", False, [])
        self.assertTrue(report["mock_serial"])
        self.assertFalse(report["safe_to_start_real"])
        self.assertEqual(report["missing_ports"], [])

    def test_real_plan_requires_ports_and_non_provisional_map(self):
        report = hardware_preflight.plan(
            "real", "COM5", "COM5", True,
            [{"device": "COM5", "description": "blue box", "hwid": "USB"}],
        )
        # The supplied map remains provisional even if an operator typed the
        # confirmation flag; this is the last safety gate before real writes.
        self.assertTrue(report["register_map_provisional"])
        self.assertFalse(report["safe_to_start_real"])
        self.assertEqual(report["missing_ports"], [])

    def test_json_report_is_machine_readable(self):
        out = io.StringIO()
        with mock.patch.object(hardware_preflight, "serial_ports", return_value=([], None)):
            self.assertEqual(hardware_preflight.main(["--json"], out=out.write), 0)
        payload = json.loads(out.getvalue())
        self.assertIn("hardware", payload)
        self.assertFalse(payload["hardware"]["safe_to_start_real"])


if __name__ == "__main__":
    unittest.main()
