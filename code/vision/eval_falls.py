"""Offline rule analysis: explicit hash-bound labels, no filename guessing or devices."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import uuid
import zipfile

import config
from gesture.fall_rules import FallDetector
from vision import synth
from vision.input_quality import InputQuality
from vision.kpio import load_rules, read_keypoints

VERDICTS = {'pass': '标签检查通过', 'mismatch': '与标签不符', 'unlabeled': '未标注，仅观察',
            'unavailable': '输入不足，不计入效果统计', 'error': '输入或标签错误'}


def evaluate(frames, rules, aspect, *, quality=None, alarms=None):
    detector = FallDetector(rules, aspect=aspect)
    quality = quality if quality is not None else InputQuality(log=lambda *_: None)
    alarms = [] if alarms is None else alarms
    for t, kp in frames:
        quality.before_frame(t, aspect, detector)
        if kp is not None:
            if not isinstance(kp, dict) or any(
                p is not None and (not isinstance(p, (list, tuple)) or len(p) not in (2, 3) or
                                   any(type(v) not in (int, float) or not math.isfinite(v) for v in p))
                for p in kp.values()
            ):
                raise ValueError('骨架坐标无效')
        event = detector.update(t, kp)
        quality.after_frame(t, kp, aspect, detector)
        if event:
            alarms.append(event)
    return alarms


def read_labels(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('recordings'), list):
        raise ValueError('标签需要 schema_version=1 和 recordings 列表')
    labels = {}
    for entry in data['recordings']:
        if not isinstance(entry, dict) or set(entry) - {'file', 'sha256', 'expected_events', 'window', 'event_onsets_s'}:
            raise ValueError('标签字段无效')
        if not isinstance(entry.get('file'), str) or not entry['file']:
            raise ValueError('标签缺少录制文件')
        key = (path.parent / entry['file']).resolve()
        digest, expected = entry.get('sha256'), entry.get('expected_events')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('标签需要录制 SHA256')
        if type(expected) is not int or expected < 0 or key in labels:
            raise ValueError('事件数须为非负整数，文件不能重复')
        window = entry.get('window')
        if window is not None and (not isinstance(window, list) or len(window) != 2 or
                                  any(type(v) not in (int, float) or not math.isfinite(v) for v in window) or
                                  not 0 <= window[0] < window[1]):
            raise ValueError('window 需要有效的 [开始秒, 结束秒]')
        onsets = entry.get('event_onsets_s')
        if onsets is not None and (not isinstance(onsets, list) or len(onsets) != expected or
                any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in onsets) or
                any(b <= a for a, b in zip(onsets, onsets[1:])) or
                (window and any(not window[0] <= v < window[1] for v in onsets))):
            raise ValueError('事件开始时间须递增、在窗口内，数量与标签一致')
        labels[key] = entry
    return labels


def evaluate_case(name, frames, *, aspect=4/3, rules=None, expected_events=None,
                  source_kind='recorded', onsets=None):
    quality = InputQuality(log=lambda *_: None)
    row = {'name': name, 'source_kind': source_kind, 'expected_events': expected_events,
           'events': [], 'status': 'error', 'label_eligible': False}
    try:
        evaluate(frames, rules, aspect, quality=quality, alarms=row['events'])
        if not quality.usable_frames or quality.gaps:
            row['status'] = 'unavailable'
        elif expected_events is None:
            row['status'] = 'unlabeled'
        else:
            row['status'] = 'pass' if len(row['events']) == expected_events else 'mismatch'
            row['label_eligible'] = True
        if onsets is not None and len(row['events']) == len(onsets):
            delays = [round(event['t'] - onset, 4) for event, onset in zip(row['events'], onsets)]
            row['onset_delays_s'] = delays if all(d >= 0 for d in delays) else None
            if any(d < 0 for d in delays) and row['status'] == 'pass':
                row['status'] = 'mismatch'
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        row['error'] = type(exc).__name__
        quality.set_state('input_error', quality.last_t)
    row['input_quality'] = quality.snapshot()
    return row


def evaluate_file(path, rules, label=None):
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if label and digest != label['sha256']:
            raise ValueError('标签哈希不匹配')
        meta, frames = read_keypoints(path)
        if not isinstance(meta, dict):
            raise ValueError('骨架元数据无效')
        previous = None
        for t, _ in frames:
            if not math.isfinite(t) or t < 0 or (previous is not None and t <= previous):
                raise ValueError('骨架时间无效')
            previous = t
        if label and label.get('window'):
            start, end = label['window']
            frames = [(t, kp) for t, kp in frames if start <= t < end]
        row = evaluate_case(path.name, frames, aspect=meta.get('aspect', 4/3), rules=rules,
                            expected_events=label['expected_events'] if label else None,
                            source_kind='synthetic' if str(meta.get('source', '')).startswith('synthetic:') else 'recorded',
                            onsets=label.get('event_onsets_s') if label else None)
        row['sha256'] = digest
        if label and label.get('window'):
            row['window'] = label['window']
        return row
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'name': path.name, 'source_kind': 'recorded', 'status': 'error', 'error': type(exc).__name__,
                'label_eligible': False, 'expected_events': label['expected_events'] if label else None, 'events': []}


def summarize(rows, source_kind):
    valid = [r for r in rows if r['source_kind'] == source_kind and r['label_eligible']]
    falls = [r for r in valid if r['expected_events'] > 0]
    normal = [r for r in valid if r['expected_events'] == 0]
    return {'labeled_fall_clips': len(falls), 'fall_clips_with_event': sum(bool(r['events']) for r in falls),
            'labeled_normal_clips': len(normal), 'normal_clips_with_event': sum(bool(r['events']) for r in normal),
            'event_count_matches': sum(r['status'] == 'pass' for r in valid), 'eligible_clips': len(valid)}


def write_report(output, rows, rules):
    report = {'schema_version': 1, 'mode': 'offline_rule_evaluation', 'cases': rows,
              'ok': all(r['status'] in ('pass', 'unlabeled') for r in rows),
              'recorded_labels': summarize(rows, 'recorded'), 'synthetic_labels': summarize(rows, 'synthetic'),
              'rules': rules, 'devices_opened': False,
              'limits': 'Unlabeled input supplies no accuracy claim. Missing usable poses or input gaps are excluded. Clip counts are not event recall. Synthetic and recorded inputs are counted separately.'}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    rows_html = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>' % (
        html.escape(r['name']), '合成' if r['source_kind'] == 'synthetic' else '录制',
        '未标注' if r['expected_events'] is None else str(r['expected_events']), len(r['events']),
        VERDICTS[r['status']]) for r in rows)
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>CareMate 视觉离线分析</title>
<style>body{font:17px/1.8 'Microsoft YaHei',sans-serif;background:#f2f5fa;color:#203547;max-width:1100px;margin:32px auto;padding:24px}table{width:100%;border-collapse:collapse;background:white}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}pre{white-space:pre-wrap;background:white;padding:20px}</style>
<h1>视觉离线分析</h1><p>本次只读取保存的坐标，不打开摄像头、主程序、语音或执行端。</p>
<p>不根据文件名猜动作。未标注、没有可用骨架或存在超过3秒的输入缺口时，不据此计算识别效果。没有跌倒标签就不能给出跌倒检出率。合成与录制分开统计。</p>
<table><tr><th>文件/场景</th><th>来源</th><th>预期事件数</th><th>实际事件数</th><th>结论</th></tr>ROWS</table><h2>输入与事件详情</h2><pre>DETAILS</pre></html>'''
    page = page.replace('ROWS</table>', rows_html+'</table>').replace('DETAILS</pre>', html.escape(json.dumps(report, ensure_ascii=False, indent=2))+'</pre>')
    (output/'report.html').write_text(page, encoding='utf-8')
    with zipfile.ZipFile(output.with_suffix('.zip'), 'x', zipfile.ZIP_DEFLATED) as package:
        for name in ('report.html', 'report.json'):
            package.write(output/name, name)
    return report


def main(argv=None):
    config.utf8_console()
    parser = argparse.ArgumentParser(description='无设备视觉分析：显式标签与输入质量检查，不根据文件名猜测')
    parser.add_argument('paths', nargs='*', type=Path)
    parser.add_argument('--latest-recording', action='store_true', help='选择最新电脑坐标；默认未标注')
    parser.add_argument('--labels', type=Path, help='明确标签 JSON，包含录制 SHA256')
    parser.add_argument('--synthetic', action='store_true')
    parser.add_argument('--rules')
    parser.add_argument('--set', action='append', metavar='KEY=VALUE')
    parser.add_argument('--bed', action='append', metavar='x1,y1,x2,y2')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.latest_recording and args.paths:
        parser.error('--latest-recording 与显式文件路径不能同时使用')
    paths = []
    for path in args.paths:
        paths.extend(sorted(path.glob('*.jsonl')) if path.is_dir() else [path])
    if args.latest_recording:
        candidates = list((config.CODE_DIR/'data/pc_pose').glob('*/keypoints.jsonl'))
        if not candidates:
            parser.error('没有已保存的电脑骨架，请指定文件；本工具不会打开摄像头')
        paths.append(max(candidates, key=lambda p:p.stat().st_mtime))
    paths = list(dict.fromkeys(p.resolve() for p in paths))
    if not paths and not args.synthetic:
        parser.error('指定坐标文件、--latest-recording 或 --synthetic')
    try:
        labels = read_labels(args.labels.resolve()) if args.labels else {}
        if set(labels)-set(paths):
            raise ValueError('标签引用了未被选择的录制文件')
        rules = load_rules(args.rules, args.set, args.bed)
        json.dumps(rules, allow_nan=False)
    except (OSError, ValueError, TypeError, KeyError):
        parser.error('无法读取规则或标签；检查格式、文件引用与字段')
    output = args.output or config.CODE_DIR/'data/vision_evaluation'/(
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:6])
    if output.exists() or output.with_suffix('.zip').exists():
        parser.error('结果目录或压缩包已存在，请使用新目录')
    rows = []
    for name, scenario in (synth.SCENARIOS.items() if args.synthetic else []):
        merged = dict(scenario.get('rules', {}))
        merged.update(rules)
        rows.append(evaluate_case('synthetic:'+name, synth.build(name), rules=merged,
                                  expected_events=len(scenario['expect']), source_kind='synthetic'))
    rows.extend(evaluate_file(path, rules, labels.get(path)) for path in paths)
    output.mkdir(parents=True)
    report = write_report(output, rows, rules)
    for row in rows:
        print('%s：%s；事件%d次' % (row['name'], VERDICTS[row['status']], len(row['events'])))
    print('未标注文件只报告观察结果；录制与合成输入分别统计。')
    print('结果：%s\n交接包：%s' % ((output/'report.html').resolve(), output.with_suffix('.zip').resolve()))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
