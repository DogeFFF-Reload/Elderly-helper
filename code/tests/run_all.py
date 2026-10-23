"""一键回归：python tests/run_all.py   （在 code/ 目录下执行）

动代码前跑一次，ALL PASS 才许动；改完再跑，仍 ALL PASS 才算改完。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    names = sorted({t.id().split(".")[0] for t in _iter(suite)})  # run() 之后 suite 会被清空，先取名
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print()
    for n in names:
        failed = any(c.id().startswith(n + ".") for c, _ in result.failures + result.errors)
        print("  %-28s %s" % (n, "FAIL" if failed else "ok"))
    print()
    if result.wasSuccessful():
        print("ALL PASS（%d 套，%d 条）" % (len(names), result.testsRun))
        return 0
    print("FAILED：%d 失败，%d 出错" % (len(result.failures), len(result.errors)))
    return 1


def _iter(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _iter(t)
        else:
            yield t


if __name__ == "__main__":
    sys.exit(main())
