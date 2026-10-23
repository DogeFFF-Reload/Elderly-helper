"""离线评估跌倒规则：把录下来的骨架文件过一遍，看漏报、误报和报警延迟。调参就靠它。

文件名里带 "fall" 的应该报警（≥1 次），其余视为日常动作，不应报警。

  python -m vision.eval_falls recordings/                       # 一个目录下所有 .jsonl
  python -m vision.eval_falls recordings/ --set lying_angle=50  # 试一组参数
  python -m vision.eval_falls --synthetic                       # 内置合成场景自检

录制：python -m vision.fall_bridge --camera --record-keypoints recordings/fall_01.jsonl
"""
import argparse
import sys
from pathlib import Path

from config import utf8_console
from gesture.fall_rules import DEFAULT_RULES, FallDetector
from vision import synth
from vision.kpio import load_rules, read_keypoints


def evaluate(frames, rules, aspect):
    d = FallDetector(rules, aspect=aspect)
    alarms = []
    for t, kp in frames:
        ev = d.update(t, kp)
        if ev:
            alarms.append(ev)
    return alarms


def run_cases(cases, rules):
    """cases: [(名字, 是否应报警, aspect, frames, 额外规则)] → (行, 是否全对)。"""
    rows, all_ok = [], True
    for name, should_alarm, aspect, frames, extra in cases:
        merged = dict(rules)
        merged.update(extra or {})
        alarms = evaluate(frames, merged, aspect)
        ok = bool(alarms) == should_alarm
        all_ok &= ok
        verdict = "OK" if ok else ("漏报" if should_alarm else "误报")
        detail = "；".join("%.1fs %s" % (a["t"], a["kind"]) for a in alarms) or "—"
        rows.append((name, "应报警" if should_alarm else "不应报警", detail, verdict))
    return rows, all_ok


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description="跌倒规则离线评估")
    ap.add_argument("paths", nargs="*", help=".jsonl 文件或目录")
    ap.add_argument("--synthetic", action="store_true", help="用内置合成场景自检")
    ap.add_argument("--rules", help="规则 JSON 文件")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", help="覆盖单个参数，可重复")
    ap.add_argument("--bed", action="append", metavar="x1,y1,x2,y2", help="床区，可重复")
    args = ap.parse_args()

    rules = load_rules(args.rules, args.set, args.bed)
    cases = []
    if args.synthetic:
        for name, sc in synth.SCENARIOS.items():
            cases.append(("synthetic:" + name, bool(sc["expect"]), 4 / 3, synth.build(name), sc.get("rules")))
    files = []
    for p in args.paths:
        p = Path(p)
        files += sorted(p.glob("*.jsonl")) if p.is_dir() else [p]
    for f in files:
        meta, frames = read_keypoints(f)
        cases.append((f.name, "fall" in f.stem.lower(), meta.get("aspect", 4 / 3), frames, None))
    if not cases:
        ap.error("给出录制文件/目录，或加 --synthetic")

    rows, all_ok = run_cases(cases, rules)
    width = max(len(r[0]) for r in rows) + 2
    for name, expect, detail, verdict in rows:
        print("%-*s %-6s %-28s %s" % (width, name, expect, detail, verdict))
    falls = [r for r in rows if r[1] == "应报警"]
    adl = [r for r in rows if r[1] == "不应报警"]
    print("\n跌倒命中 %d/%d，日常动作无误报 %d/%d" % (
        sum(r[3] == "OK" for r in falls), len(falls), sum(r[3] == "OK" for r in adl), len(adl)))
    changed = {k: v for k, v in rules.items() if DEFAULT_RULES.get(k) != v}
    if changed:
        print("本次参数：%s" % changed)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
