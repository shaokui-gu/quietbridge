#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local fixture and content-free analysis; never controls a browser or desktop."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import time
import sys
from urllib.parse import parse_qs, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/mcp'))
from browser_events import validate_render_probe
from preview_metadata import COUNTERS, AGES, LIFECYCLE_EVENTS, number, frame_progress, capture_lifecycle

STAGES = ('visible', 'covered', 'off_space', 'before_screenshot', 'after_screenshot',
          'before_switch', 'after_switch', 'end')


def sample(record, window_id=None):
    if record.get('event') != 'preview.streamProgress':
        return None
    window = record.get('trackingWindowId')
    if not number(window) or (window_id is not None and window != window_id):
        return None
    progress = record.get('frameProgress')
    if not isinstance(progress, dict) or not number(record.get('timestamp')):
        return None
    result = {k: record[k] for k in ('timestamp', 'pid', 'trackingWindowId') if number(record.get(k))}
    generation = capture_lifecycle(record).get('streamGeneration')
    if generation is None:
        return None
    result['streamGeneration'] = generation
    for k in ('targetOnCurrentSpace', 'panelVisible', 'videoLayerReady', 'videoLayerFailed'):
        if type(record.get(k)) is bool:
            result[k] = record[k]
    result['frameProgress'] = frame_progress(progress)
    return result


def lifecycle_sample(record, window_id=None):
    if not isinstance(record.get('event'), str) or record['event'] not in LIFECYCLE_EVENTS or not number(record.get('timestamp')):
        return None
    safe = capture_lifecycle(record)
    if not safe.get('trackingWindowId') or not safe.get('streamGeneration'):
        return None
    if window_id is not None and safe['trackingWindowId'] != window_id:
        return None
    safe.update(event=record['event'], timestamp=record['timestamp'])
    if type(record.get('pid')) is int and record['pid'] > 0:
        safe['pid'] = record['pid']
    return safe


def summarize(samples, lifecycle=()):
    groups = defaultdict(list)
    for item in sorted(samples, key=lambda r: r['timestamp']):
        groups[(item.get('pid'), item['trackingWindowId'], item['streamGeneration'])].append(item)
    intervals = []
    for key, rows in groups.items():
        for a, b in zip(rows, rows[1:]):
            before, after = a['frameProgress'], b['frameProgress']
            delta = {k: after[k] - before[k] for k in COUNTERS if k in before and k in after}
            reset = any(v < 0 for v in delta.values())
            intervals.append({'pid': key[0], 'windowId': key[1], 'streamGeneration': key[2],
                              'start': a['timestamp'], 'end': b['timestamp'],
                              'targetOnCurrentSpaceBefore': a.get('targetOnCurrentSpace'),
                              'targetOnCurrentSpaceAfter': b.get('targetOnCurrentSpace'),
                              'counterReset': reset, 'delta': {} if reset else delta})
    latest = {}
    for event in sorted(lifecycle, key=lambda r: r['timestamp']):
        latest[(event.get('pid'), event['trackingWindowId'], event['streamGeneration'])] = event
    stops = [row for row in latest.values() if row.get('stopState') in ('requested', 'completed', 'failed')]
    return {'sampleCount': len(samples), 'streamCount': len(groups), 'intervals': intervals,
            'samples': samples,
            'captureLifecycle': list(lifecycle), 'captureStops': stops,
            'pendingCaptureStops': sum(r.get('stopState') == 'requested' for r in stops),
            'failedCaptureStops': sum(r.get('stopState') == 'failed' for r in stops),
            'limits': ['Unchanged sparse samples do not prove frozen rendering; correlate with known DOM changes.',
                       'submittedFrames means enqueue, not confirmed presentation.',
                       'Central body probe is a fixed crop, not a DOM rectangle; uniform sparse samples can miss small details.',
                       'Only captureStopCompleted confirms that stream stop API returned successfully; missing logs are not proof of a leak.',
                       'No screenshot is taken; screenshots may themselves affect rendering.']}


def load_samples(paths, since=0, window_id=None, parser=sample):
    result, seen = [], set()
    for path in paths:
        if not path.exists():
            continue
        with path.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    item = parser(record, window_id) if isinstance(record, dict) else None
                except (ValueError, TypeError):
                    continue
                if item and item['timestamp'] >= since:
                    identity = (item.get('event'), item.get('pid'), item.get('trackingWindowId'), item['streamGeneration'], item['timestamp'])
                    if identity not in seen:
                        seen.add(identity)
                        result.append(item)
    return result


def load_probe_results(paths, since=0, task_id=None):
    """Fixed metadata only; result times are extension stage times, not poll times."""
    results = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get('event') != 'browser.render_probe_result':
                        continue
                    if not number(row.get('timestamp')) or row['timestamp'] < since:
                        continue
                    command, task = row.get('commandId'), row.get('taskId')
                    if not isinstance(command, str) or not re.fullmatch('[a-f0-9]{32}', command):
                        continue
                    if not isinstance(task, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', task):
                        continue
                    if task_id is not None and task != task_id:
                        continue
                    result = row.get('result')
                    safe = validate_render_probe(result, {'stage': (result or {}).get('stage')})
                    # Repeated result polls are the same probe, not repeated execution.
                    key = (task, command)
                    entry = dict(timestamp=row['timestamp'], taskId=task, commandId=command, result=safe)
                    if key not in results or entry['timestamp'] < results[key]['timestamp']:
                        results[key] = entry
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    continue
    return sorted(results.values(), key=lambda r: r['result']['startedAt'])


def fixture(step):
    colors = ('#076b64', '#af4117', '#6342a2', '#1659a5')
    color = colors[step % len(colors)]
    return ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QuietBridge Preview Step __STEP__</title>
<style>html{background:COLOR;color:white;font-family:system-ui}body{margin:0}
main{min-height:100vh;display:grid;place-content:center;text-align:center}
h1{font-size:clamp(80px,16vw,230px);margin:0}p{font-size:24px;padding:0 24px}
section{height:75vh;display:grid;place-content:center;background:#ffffff15;font-size:64px}
section:nth-child(odd){background:#00000030}</style>
<main data-preview-step="__STEP__"><p>QuietBridge · 页面绘制诊断</p><h1>STEP __STEP__</h1>
<p>这是静态测试页。每次导航会更换编号和整页颜色。</p></main>
<section>SCROLL 1</section><section>SCROLL 2</section><section>SCROLL 3</section></html>'''
            .replace('COLOR', color).replace('__STEP__', str(step))).encode()


def serve(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                step = int(query.get('step', ['0'])[0])
                if url.path not in ('/', '/probe') or not 0 <= step <= 99:
                    raise ValueError()
            except ValueError:
                self.send_error(404)
                return
            data = fixture(step)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass  # Do not save URLs or any browser data.

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(json.dumps({'event': 'fixture_ready', 'url': f'http://127.0.0.1:{port}/probe?step=0'}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('serve')
    start.add_argument('--port', type=int, default=48763)
    mark = sub.add_parser('mark')
    mark.add_argument('stage', choices=STAGES)
    mark.add_argument('--output', type=Path, required=True)
    analyze = sub.add_parser('analyze')
    analyze.add_argument('--native-log', type=Path, default=Path.home() / 'Library/Logs/quietbridge/native-operations.jsonl')
    analyze.add_argument('--bridge-log', type=Path, default=Path.home() / 'Library/Logs/quietbridge/bridge-operations.jsonl')
    analyze.add_argument('--task-id', help='Filter render probes by task; window filter only applies to native samples')
    analyze.add_argument('--window-id', type=int)
    analyze.add_argument('--since', type=float, default=0)
    analyze.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args.port)
    elif args.command == 'mark':
        args.output.parent.mkdir(parents=True, exist_ok=True)
        record = {'event': 'manual_stage', 'stage': args.stage, 'timestamp': time.time(),
                  'timeUTC': datetime.now(timezone.utc).isoformat()}
        with args.output.open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        print(json.dumps(record))
    else:
        paths = [Path(str(args.native_log) + '.1'), args.native_log]
        rows = load_samples(paths, args.since, args.window_id)
        lifecycle = load_samples(paths, args.since, args.window_id, parser=lifecycle_sample)
        report = summarize(rows, lifecycle)
        report['renderProbes'] = load_probe_results([Path(str(args.bridge_log) + '.1'), args.bridge_log], args.since, args.task_id)
        report['limits'].append('Probe correlation requires the recorded task/window binding; nearby timestamps alone do not prove causation.')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        print(json.dumps({'output': str(args.output), 'sampleCount': len(rows), 'streamCount': report['streamCount'],
                          'captureLifecycleEvents': len(lifecycle), 'pendingCaptureStops': report['pendingCaptureStops'],
                          'failedCaptureStops': report['failedCaptureStops']}))


if __name__ == '__main__':
    main()
