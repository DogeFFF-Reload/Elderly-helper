"""Offline audit of saved phone calibration data. Never opens devices or URLs."""
import argparse
from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import zipfile

if __package__:
    from .phone_calibrate import STAGES, percentile, phase_statistics
else:
    from phone_calibrate import STAGES, percentile, phase_statistics


def distribution(values):
    return {"count": len(values), "min": min(values) if values else None,
            "median": statistics.median(values) if values else None,
            "mean": statistics.mean(values) if values else None,
            "p95": percentile(values, .95), "max": max(values) if values else None}


def replay(phases, thresholds=(.5, 1, 1.5, 2, 3, 18, 38), conn=3, cooldown=30):
    """Watch rule on recorded segments only; reset streak at each segment."""
    results = []
    for threshold in thresholds:
        events = []
        last_fire = -math.inf
        for phase in phases:
            streak = 0
            for row in phase["rows"]:
                if row["energy"] == "":
                    streak = 0
                    continue
                streak = streak + 1 if float(row["energy"]) > threshold else 0
                now = float(row["session_s"])
                if streak >= conn and now - last_fire > cooldown:
                    events.append({"round": phase["round"], "stage": phase["stage"], "session_s": now})
                    last_fire, streak = now, 0
        results.append({"threshold": threshold, "conn": conn, "cooldown_s": cooldown,
                        "simulated_events": events, "count": len(events),
                        "above_metric_maximum": threshold >= 21.25})
    return results


def analyze(directory, *, fixed_rounds=(), moving_rounds=(), operator_note=""):
    directory = Path(directory).resolve()
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    with (directory / "samples.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    fixed, moving = set(fixed_rounds), set(moving_rounds)
    rounds = summary["settings"]["rounds"]
    if fixed & moving or not (fixed | moving) <= set(range(1, rounds + 1)):
        raise ValueError("固定和移动轮次不能重叠，且必须属于这次测试。")
    errors = []
    grouped = defaultdict(list)
    previous_seq, previous_time = 0, -math.inf
    for index, row in enumerate(rows, 2):
        key = (int(row["round"]), row["stage"])
        grouped[key].append(row)
        for field in ("session_s", "stage_s", "frame_age_s", "energy", "centroid_y", "delta_s"):
            if row[field] != "" and not math.isfinite(float(row[field])):
                raise ValueError("CSV 第%d行 %s 不是有限数值" % (index, field))
        sequence, timestamp = int(row["source_sequence"]), float(row["session_s"])
        if sequence <= previous_seq or timestamp <= previous_time:
            errors.append("CSV 第%d行源帧序号或时间未递增" % index)
        previous_seq, previous_time = sequence, timestamp
        if float(row["frame_age_s"]) < 0:
            errors.append("CSV 第%d行处理等待时间为负数" % index)
        if row["energy"] != "" and not 0 <= float(row["energy"]) <= 21.25:
            errors.append("CSV 第%d行能量超出理论范围" % index)
        if row["centroid_y"] != "" and not 0 <= float(row["centroid_y"]) <= 1:
            errors.append("CSV 第%d行变化重心超出范围" % index)
    duration = summary["settings"]["duration_s"]
    interval = summary["settings"]["sample_interval_s"]
    phases = []
    for original in summary["phases"]:
        key = (original["round"], original["stage"])
        subset = grouped.pop(key, [])
        recomputed = phase_statistics(subset, duration, interval)
        for name, value in original.get("statistics", {}).items():
            expected = recomputed.get(name)
            match = (math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-9)
                     if type(value) in (int, float) and type(expected) in (int, float) else value == expected)
            if not match:
                errors.append("第%d轮 %s 的摘要 %s 与CSV不一致" % (*key, name))
        for i, row in enumerate(subset):
            if not 0 <= float(row["stage_s"]) < duration:
                errors.append("第%d轮 %s 有阶段外样本" % key)
            if i == 0:
                if row["baseline"] != "1" or any(row[f] != "" for f in ("energy", "centroid_y", "delta_s")):
                    errors.append("第%d轮 %s 的基准样本异常" % key)
            elif (row["baseline"] != "0" or any(row[f] == "" for f in ("energy", "centroid_y", "delta_s"))
                  or not math.isclose(float(row["delta_s"]), float(row["stage_s"]) - float(subset[i - 1]["stage_s"]), abs_tol=1e-7)):
                errors.append("第%d轮 %s 的能量样本或间隔异常" % key)
        if original["status"] == "complete" and not recomputed["sampling_ok"]:
            errors.append("第%d轮 %s 被标成完成但采样质量不合格" % key)
        phases.append({"round": key[0], "stage": key[1], "name": original["name"],
                       "status": original["status"], "camera": "fixed" if key[0] in fixed else "moved" if key[0] in moving else "unconfirmed",
                       "statistics": recomputed, "rows": subset})
    if grouped:
        errors.append("CSV 存在计划以外的轮次或动作")
    expected_keys = [(r, s) for r in range(1, rounds + 1) for s, _, _ in STAGES]
    if [(p["round"], p["stage"]) for p in phases] != expected_keys:
        errors.append("阶段计划与声明的轮数或动作不一致")
    phase_ends = [(e["round"], e["stage"], e["status"]) for e in events if e["event"] == "phase_end"]
    if summary["status"] == "complete" and phase_ends != [(p["round"], p["stage"], "complete") for p in phases]:
        errors.append("完成状态与阶段结束日志不一致")
    if not events or events[-1].get("status") != summary["status"] or events[-1]["event"] != "session_end":
        errors.append("会话结束日志与状态不一致")
    archive = directory.with_suffix(".zip")
    archive_matches = None
    if archive.exists():
        with zipfile.ZipFile(archive) as bundle:
            archive_matches = all(bundle.read(directory.name + "/" + name) == (directory / name).read_bytes()
                                  for name in ("samples.csv", "summary.json", "events.jsonl", "结果说明.txt"))
        if not archive_matches:
            errors.append("ZIP 与旁边原文件内容不一致")
    fixed_phases = [p for p in phases if p["camera"] == "fixed" and p["status"] == "complete" and p["statistics"]["sampling_ok"]]
    aggregates = {}
    for label, selected in (("all_scheduled", phases), ("fixed_camera", fixed_phases)):
        aggregates[label] = {}
        for stage, _, _ in STAGES:
            chosen = [r for p in selected if p["stage"] == stage for r in p["rows"]]
            aggregates[label][stage] = {"samples": len(chosen), "energy": distribution([float(r["energy"]) for r in chosen if r["energy"] != ""])}
    expected_count = len(phases) * math.ceil(duration / interval)
    return {
        "schema_version": 1, "session_id": summary["session_id"], "collection_status": summary["status"],
        "collection_message": summary["message"], "started_utc": summary["started_utc"],
        "elapsed_s": (datetime.fromisoformat(summary["ended_utc"]) - datetime.fromisoformat(summary["started_utc"])).total_seconds(),
        "sample_count": len(rows), "energy_count": sum(r["energy"] != "" for r in rows),
        "planned_sample_count": expected_count, "sample_fraction": len(rows) / expected_count,
        "sampling_intervals_s": distribution([float(r["delta_s"]) for r in rows if r["delta_s"] != ""]),
        "client_frame_age_s": distribution([float(r["frame_age_s"]) for r in rows]),
        "operator_annotation": {"source": "operator_report", "fixed_rounds": sorted(fixed), "moving_rounds": sorted(moving), "note": operator_note},
        "fixed_complete_rounds": [r for r in sorted(fixed) if sum(p["round"] == r for p in fixed_phases) == len(STAGES)],
        "integrity": {"ok": not errors, "errors": errors, "zip_matches": archive_matches,
                      "sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                 for name in ("samples.csv", "summary.json", "events.jsonl")}},
        "aggregates": aggregates,
        "replay_all_scheduled": replay(phases), "replay_fixed_camera": replay(fixed_phases),
        "replay_limit": "Only saved segments; streak reset at boundaries; preparation/transition frames unavailable. No events sent.",
        "fall_threshold_recommendation": None,
        "limits": ["Only scheduled normal actions, no labeled fall examples.",
                   "Camera movement annotations are operator reports; full-body visibility is unverified.",
                   "Receipt timestamps cannot measure camera capture latency.",
                   "No photos, video or skeletons are available for reclassification."],
        "phases": phases,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--fixed-rounds", type=int, nargs="*", default=[])
    parser.add_argument("--moving-rounds", type=int, nargs="*", default=[])
    parser.add_argument("--operator-note", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.session, fixed_rounds=args.fixed_rounds, moving_rounds=args.moving_rounds, operator_note=args.operator_note)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("%s: %d samples, %d comparisons, audit=%s; output=%s" % (
        report["session_id"], report["sample_count"], report["energy_count"], report["integrity"]["ok"], args.output.resolve()))
    return 0 if report["integrity"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
