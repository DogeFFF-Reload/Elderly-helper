"""合并前检查的归属规则：谁的分支能改哪些文件（§7.3 协作规则）。"""
import unittest

import helpers  # noqa: F401  （把 code/ 加进 sys.path）

from tools import merge_gate, owners


class OwnersTest(unittest.TestCase):
    def test_owner_of(self):
        cases = {
            "code/master_service.py": "software",
            "code/state_machine/guard_fsm.py": "software",
            "code/agent/llm_client.py": "software",
            "code/agent/health_tracker.py": "captain",
            "code/ui/monitor/index.html": "captain",
            "code/hand_controller/rh56_register_map.py": "hardware",
            "code/vision/fall_bridge.py": "demo",
            "code/tests/test_voice_profile.py": "captain",
            "code/tests/test_exercise.py": "hardware",
            "code/tests/helpers.py": "software",
            "README.md": None,
        }
        for path, role in cases.items():
            self.assertEqual(owners.owner_of(path), role, path)

    def test_branch_roles(self):
        self.assertEqual(owners.role_of_branch("captain/interaction"), "captain")
        self.assertEqual(owners.role_of_branch("demo/vision"), "demo")
        self.assertIsNone(owners.role_of_branch("feature-x"))


class ReviewTest(unittest.TestCase):
    def test_hub_only_by_software(self):
        errors, warnings, _ = merge_gate.review_paths(["code/master_service.py", "code/ui/family/index.html"], "captain")
        self.assertEqual(len(errors), 1)
        self.assertIn("交通枢纽", errors[0])
        self.assertEqual(warnings, [])
        errors, warnings, _ = merge_gate.review_paths(["code/master_service.py"], "software")
        self.assertEqual((errors, warnings), ([], []))

    def test_cross_module_warns(self):
        errors, warnings, _ = merge_gate.review_paths(
            ["code/state_machine/guard_fsm.py", "code/vision/fall_bridge.py", "README.md"], "demo")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("软件", warnings[0])

    def test_unknown_branch_name(self):
        errors, warnings, _ = merge_gate.review_paths(["code/master_service.py"], None)
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("角色" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
