"""一键回归：python tests/run_all.py   （在 code/ 目录下执行）

动代码前跑一次，ALL PASS 才许动；改完再跑，仍 ALL PASS 才算改完。
只跑某几套：python tests/run_all.py fsm help   （按套名包含匹配）
每套后面标了负责人（tools/owners.py），挂了直接找他。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from tools import owners  # noqa: E402


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    only = sys.argv[1:] if argv is None else argv
    suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    if only:
        suite = unittest.TestSuite(t for t in _iter(suite) if any(k in _suite(t) for k in only))
    names = sorted({_suite(t) for t in _iter(suite)})  # run() 之后 suite 会被清空，先取名
    if not names:
        print("没有匹配 %s 的测试套" % " ".join(only))
        return 1
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print()
    broken = []
    for n in names:
        failed = any(_suite(c) == n for c, _ in result.failures + result.errors)
        who = owners.names(owners.TESTS.get(n, ())) or "未登记"
        print("  %-28s %-4s  %s" % (n, "FAIL" if failed else "ok", who))
        if failed:
            broken.append((n, who))
    if not only:
        missing = [n for n in owners.TESTS if n not in names]
        for n in missing:
            print("  %-28s %-4s  %s" % (n, "缺", owners.names(owners.TESTS[n])))
        unregistered = [n for n in names if n not in owners.TESTS]
        if unregistered:
            print("\n  新测试还没登记负责人：%s → tools/owners.py 的 TESTS" % "、".join(unregistered))
    print()
    if result.wasSuccessful():
        print("ALL PASS（%d 套，%d 条）" % (len(names), result.testsRun))
        return 0
    print("FAILED：%d 失败，%d 出错" % (len(result.failures), len(result.errors)))
    for n, who in broken:
        print("  找%s：%s" % (who, n))
    return 1


def _suite(test):
    """测试所属的套名。文件导入失败时 unittest 给的 id 是 unittest.loader._FailedTest.<模块名>，取模块名。"""
    parts = test.id().split(".")
    return parts[-1] if parts[0] == "unittest" else parts[0]


def _iter(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _iter(t)
        else:
            yield t


if __name__ == "__main__":
    sys.exit(main())
