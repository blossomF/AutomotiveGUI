"""Versioned log projection shared by the dashboard; standard library only.

Usage snapshots are authoritative. Legacy event totals use one source per step:
responses when present, otherwise decision/route summaries, never both.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def process_key(pid):
    """Return process creation identity, avoiding PID reuse and Windows kill(0)."""
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return '<exited>' if ctypes.get_last_error() == 87 else None
            try:
                exit_code = wintypes.DWORD()
                if kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
                    return '<exited>'
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                    return None
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(handle)
        # The command name in /proc/stat may contain spaces and parentheses.
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def tokens(ev):
    tok = ev.get('tokens')
    if isinstance(tok, dict):
        return {'input': int(number(tok.get('input', tok.get('prompt_tokens')))),
                'output': int(number(tok.get('output', tok.get('completion_tokens'))))}
    if isinstance(tok, (int, float)):
        return {'input': int(number(tok)), 'output': 0}
    if 'in_tok' in ev or 'out_tok' in ev:
        return {'input': int(number(ev.get('in_tok'))), 'output': int(number(ev.get('out_tok')))}
    if 'model_tokens' in ev:
        return {'input': int(number(ev['model_tokens'])), 'output': 0}
    return {'input': 0, 'output': 0}


def project(events, session_id):
    match = re.search(r'-(\d{8})-(\d{6})$', session_id)
    start = None
    if match:
        try:
            start = datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S')
        except ValueError:
            pass
    task = next((e['task'] for e in events if isinstance(e.get('task'), str) and e['task'].strip()),
                re.sub(r'-\d{8}-\d{6}$', '', session_id))
    title = f'{start:%m-%d %H:%M:%S} {task}' if start else task
    steps, counts, groups = [], {}, {}
    finish, snapshot = None, None
    previous = None
    last_step = 0
    for idx, ev in enumerate(events):
        phase = ev.get('phase', 'log')
        step = ev.get('step', last_step)
        step = step if isinstance(step, int) and not isinstance(step, bool) else last_step
        last_step = step
        counts[step] = counts.get(step, 0) + 1
        t = number(ev.get('t'))
        wall = ev.get('wall') or ((start + timedelta(seconds=t)).strftime('%H:%M:%S') if start else None)
        tok = tokens(ev)
        duration = ev.get('duration_ms')
        model_ms = ev.get('latency_ms', ev.get('model_ms'))
        inference = round(number(model_ms) / 1000, 3) if model_ms is not None else None
        execution = round(number(duration) / 1000, 3) if duration is not None and phase == 'act' else None
        steps.append({'idx': idx, 'step': step, 'substep': counts[step], 'wall_time': wall,
                      'action': {'action': phase, 'summary': ev.get('msg', ''),
                                 'explain': ev.get('msg', ''), **{k: v for k, v in ev.items()
                                 if k not in ('t', 'wall', 'step', 'phase', 'msg')}},
                      'at_time': t, 'gap_time': None if previous is None else round(max(0, t - previous), 3),
                      'inference_time': inference, 'execution_time': execution,
                      'duration_time': round(number(duration) / 1000, 3) if duration is not None else None,
                      'total_time': None, 'tokens': tok, 'image': None, 'evidence': None, 'noop': False})
        previous = t
        if isinstance(ev.get('usage_total'), dict):
            snapshot = ev['usage_total']
        if phase == 'finish':
            finish = ev
            continue
        if phase == 'model' and ev.get('state') != 'end':
            continue
        group = groups.setdefault(step, {'response': {}, 'fallback': [], 'modern': []})
        if phase == 'model' and ev.get('state') == 'end':
            group['modern'].append(ev)
        elif phase == 'glm_response':
            group['response'][str(ev.get('call_id', idx))] = ev
        elif phase in ('decide', 'route') and any(k in ev for k in ('tokens', 'in_tok', 'out_tok', 'model_tokens')):
            group['fallback'].append(ev)
    legacy_tokens, legacy_calls = 0, 0
    for group in groups.values():
        source = group['modern'] or list(group['response'].values()) or group['fallback']
        legacy_tokens += sum(sum(tokens(e).values()) for e in source)
        legacy_calls += sum(int(number(e.get('calls', e.get('glms', 1)))) for e in source)
    usage = dict((finish or {}).get('usage') or snapshot or {})
    total_tokens = int(number(usage.get('total_tokens'), legacy_tokens))
    usage.setdefault('calls', legacy_calls)
    usage.setdefault('total_tokens', total_tokens)
    known_usage = (int(number(usage.get('usage_reports'))) > 0 if 'usage_reports' in usage
                   else 'total_tokens' in ((finish or {}).get('usage') or snapshot or {}) or any(any(k in e for k in ('tokens', 'in_tok', 'out_tok', 'model_tokens')) for e in events))
    elapsed = number((finish or {}).get('elapsed', (finish or {}).get('t', previous)))
    graph_steps = int(number((finish or {}).get('steps'), len({s['step'] for s in steps if s['step'] > 0})))
    reason = str((finish or {}).get('msg', ''))
    status = 'unknown'
    if finish:
        status = 'done' if finish.get('success', reason == 'complete') else {
            'interrupted': 'cancelled', 'cancelled': 'cancelled', 'error': 'error', 'timeout': 'timeout'
        }.get(reason, 'aborted')
    return {'task_dir': session_id, 'session_id': session_id, 'task': task, 'title': title,
            'status': status, 'reason': reason, 'complete': finish is not None,
            'usage': usage, 'routes': (finish or {}).get('routes') or {},
            'stats': {'total_steps': graph_steps, 'total_time': round(elapsed, 1),
                      'total_tokens': total_tokens, 'usage_known': known_usage,
                      'usage_complete': bool(finish and finish.get('usage_complete', usage.get('usage_complete', known_usage))),
                      'avg_step': round(elapsed / graph_steps, 2) if graph_steps else None},
            'steps': steps, 'cursor': len(events) - 1, 'event_count': len(events)}


class LogCache:
    """Tail complete UTF-8 lines only; unchanged files never get re-read/re-parsed."""
    def __init__(self, limit=32):
        self.limit = limit
        self.entries = OrderedDict()
        self.lock = threading.RLock()

    def read(self, path):
        path = Path(path)
        with self.lock:
            st = path.stat()
            key = str(path.resolve())
            signature = (st.st_ino, st.st_size, st.st_mtime_ns)
            item = self.entries.get(key)
            if item is None or signature[0] != item['signature'][0] or st.st_size < item['offset'] or (
                    st.st_size == item['signature'][1] and signature != item['signature']):
                item = {'offset': 0, 'events': [], 'signature': None, 'detail': None}
            if signature != item['signature']:
                with path.open('rb') as fh:
                    fh.seek(item['offset'])
                    data = fh.read()
                # An in-flight final line is retried on the next read.
                end = data.rfind(b'\n') + 1
                chunk = data[:end]
                # Legacy completed logs may have no final newline. Parse only if valid.
                if end < len(data):
                    try:
                        tail = json.loads(data[end:].decode('utf-8'))
                        if isinstance(tail, dict):
                            chunk = data
                    except (ValueError, UnicodeError):
                        pass
                for line in chunk.splitlines():
                    try:
                        ev = json.loads(line.decode('utf-8'))
                        if isinstance(ev, dict):
                            item['events'].append(ev)
                    except (ValueError, UnicodeError):
                        pass
                item['offset'] += len(chunk)
                item['signature'] = signature
                item['detail'] = project(item['events'], path.stem)
            self.entries[key] = item
            self.entries.move_to_end(key)
            while len(self.entries) > self.limit:
                self.entries.popitem(last=False)
            return item['detail'], item['events']
