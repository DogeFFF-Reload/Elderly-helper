"""demo_driver：§3 主线 + 健康步 + 手指操在进程内假时钟上全流程走通；出问题能报出来。"""
import contextlib
import io
import unittest

import helpers  # noqa: F401  （把 code/ 加进 sys.path）

import config
import demo_driver


def local():
    return demo_driver.LocalTarget(config.load(use_local=False))


class DemoDriverTest(unittest.TestCase):
    def run_steps(self, target, names, reset=True):
        out = []
        ok = demo_driver.Driver(target, out=out.append).run(names, reset=reset)
        return ok, out

    def test_main_line_and_p2_all_pass(self):
        ok, out = self.run_steps(local(), list(demo_driver.STEPS))
        self.assertTrue(ok, "\n".join(out))
        self.assertFalse([ln for ln in out if "✗" in ln])
        self.assertEqual(sum(1 for ln in out if "✓" in ln), 9)

    def test_main_line_order(self):
        self.assertEqual(demo_driver.MAIN_LINE, ("health", "medicine", "night_fall"))

    def test_reports_failure(self):
        t = local()
        t.post("/mock/fall", {"room": "卧室"})
        t.wait(6)  # 进入 SOS：臂归状态机，递药应被挂起
        ok, out = self.run_steps(t, ["medicine"], reset=False)
        self.assertFalse(ok)
        self.assertTrue(any("✗" in ln and "递出药盒" in ln for ln in out))

    def test_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(demo_driver.main(["--list"]), 0)
            self.assertEqual(demo_driver.main(["--local", "--steps", "跳舞"]), 2)
        self.assertIn("night_fall", buf.getvalue())
        self.assertIn("没有这一段", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
