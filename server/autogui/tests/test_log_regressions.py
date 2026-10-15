"""Offline regression tests; runnable with unittest without device/pytest/network."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'server/autogui'))
from autogui.log_timeline import LogCache, process_key, project
from autogui.core.decision_log import DecisionLog
from autogui.brain.glm_client import GLMClient, GLMError
from autogui.config import GLMConfig, Config
from autogui.core.graph import Runner


def dashboard():
    spec = importlib.util.spec_from_file_location('dashboard_log_regression', ROOT / 'dist/dashboard_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(text='{"type":"BACK"}', in_tok=12, out_tok=4, include_usage=True):
    return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok) if include_usage else None)]


class TimelineTests(unittest.TestCase):
    def test_legacy_usage_not_counted_twice_and_includes_initial_route(self):
        events = [dict(t=0, step=0, phase='route', model_tokens=10),
                  dict(t=1, step=1, phase='glm_response', call_id=1, in_tok=12, out_tok=4, latency_ms=60000),
                  dict(t=1.1, step=1, phase='decide', in_tok=12, out_tok=4),
                  dict(t=1.2, step=1, phase='act', msg='OK')]
        detail = project(events, 'test-20261015-100000')
        self.assertEqual(detail['stats']['total_tokens'], 26)
        self.assertEqual(detail['steps'][1]['inference_time'], 60)
        self.assertIsNone(detail['steps'][-1]['execution_time'])
        self.assertIsNone(detail['steps'][0]['gap_time'])
        self.assertAlmostEqual(detail['steps'][-1]['gap_time'], .1)
        self.assertIsNone(detail['steps'][-1]['total_time'])

    def test_json_retry_responses_and_summary_count_once(self):
        events = [dict(step=1, phase='glm_response', call_id=1, in_tok=10, out_tok=2),
                  dict(step=1, phase='glm_response', call_id=2, in_tok=20, out_tok=3),
                  dict(step=1, phase='decide', in_tok=30, out_tok=5)]
        self.assertEqual(project(events, 'test')['stats']['total_tokens'], 35)

    def test_finish_reason_usage_and_routes_are_kept(self):
        detail = project([dict(t=61, phase='finish', msg='interrupted', success=False, steps=2,
                               usage=dict(total_tokens=150, calls=3, retries=1, latency_ms=60000), routes={'tree': 2})], 'test')
        self.assertEqual(detail['status'], 'cancelled')
        self.assertEqual(detail['reason'], 'interrupted')
        self.assertEqual(detail['steps'][0]['action']['action'], 'finish')
        self.assertEqual(detail['usage']['calls'], 3)
        self.assertEqual(detail['routes']['tree'], 2)
        self.assertEqual(detail['stats']['total_time'], 61)

    def test_snapshot_is_authoritative_even_with_debug_events(self):
        events = [dict(step=1, phase='glm_response', in_tok=100, out_tok=50),
                  dict(step=1, phase='model', state='end', tokens={'input': 100, 'output': 50},
                       usage_total={'total_tokens': 150, 'calls': 1, 'usage_complete': False}),
                  dict(step=1, phase='decide', in_tok=100, out_tok=50),
                  dict(phase='finish', msg='failed', success=False,
                       usage={'total_tokens': 150, 'calls': 1, 'usage_complete': False})]
        detail = project(events, 'test')
        self.assertEqual(detail['stats']['total_tokens'], 150)
        self.assertFalse(detail['stats']['usage_complete'])

    def test_cache_tail_partial_unicode_and_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.jsonl';cache = LogCache()
            path.write_bytes(b'{"phase":"setup","t":0}\n')
            detail, _ = cache.read(path)
            with patch.object(Path, 'open', side_effect=AssertionError('unchanged file reread')):
                self.assertIs(cache.read(path)[0], detail)
            line = json.dumps(dict(phase='decide', t=1, msg='中文'), ensure_ascii=False).encode() + b'\n'
            split = line.index('中'.encode()) + 1
            with path.open('ab') as fh:fh.write(line[:split])
            self.assertEqual(len(cache.read(path)[1]), 1)
            with path.open('ab') as fh:fh.write(line[split:])
            self.assertEqual(cache.read(path)[1][-1]['msg'], '中文')
            path.write_text('{"phase":"finish","success":true}', encoding='utf-8')
            self.assertEqual(cache.read(path)[0]['status'], 'done')
            self.assertEqual(len(cache.read(path)[1]), 1)

    def test_python_and_offline_projection_contract(self):
        fixtures = [[], [dict(t=0, phase='setup', task='测试')],
                    [dict(t=1.1, wall='12:00:01', step=1, phase='decide', in_tok=12, out_tok=4),
                     dict(t=1.6, wall='12:00:01', step=1, phase='act', duration_ms=100),
                     dict(t=2, phase='finish', msg='complete', success=True, steps=1, usage={'total_tokens':16})],
                    [dict(step=0, phase='route', model_tokens=10),
                     dict(t=2, step=1, phase='glm_response', call_id=1, in_tok=20, out_tok=3),
                     dict(t=2.1, step=1, phase='decide', in_tok=20, out_tok=3)]]
        script = "const v=require(process.argv[1]);let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(e=>v.project(e,'test-20261015-100000')))));"
        out = subprocess.run(['node','-e',script,str(ROOT/'dist/log_view.js')], input=json.dumps(fixtures), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(out.stdout), [project(e, 'test-20261015-100000') for e in fixtures])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.m = dashboard();self.m.LOGS_ROOT = Path(self.tmp.name)

    def write(self, stem, events):
        path = self.m.LOGS_ROOT / (stem + '.jsonl')
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding='utf-8')
        return path

    def test_sort_by_time_and_unknown_legacy_tasks(self):
        for stem in ['音乐-20261014-150500', '导航-20261015-162800']:
            self.write(stem, [dict(t=1, step=1, phase='decide', in_tok=12, out_tok=4)])
        tasks = self.m.list_tasks()
        self.assertTrue(tasks[0]['task_dir'].startswith('导航'))
        self.assertEqual(tasks[0]['status'], 'unknown')
        self.assertEqual(tasks[0]['total_tokens'], 16)

    def test_live_process_stays_running_after_long_log_silence(self):
        self.write('live', [dict(t=0, phase='setup', pid=os.getpid(), process_key=process_key(os.getpid()), started_at=time.time()-90)])
        result = self.m.get_live('live', -1)
        self.assertEqual(result['status'], 'running')
        self.assertGreaterEqual(result['stats']['total_time'], 89)

    def test_pid_reuse_is_not_a_live_task(self):
        self.write('dead', [dict(t=1, phase='setup', pid=os.getpid(), process_key='old-process')])
        self.assertEqual(self.m.get_live('dead')['status'], 'interrupted')

    def test_cancelled_sidecar_and_output_path_validation(self):
        path = self.write('stopped', [dict(t=1, phase='setup')])
        path.with_suffix('.run.json').write_text(json.dumps(dict(state='cancelled', reason='用户停止任务', started_at=10, ended_at=20, run_log='../../secret.log')))
        self.assertEqual(self.m.get_live('stopped')['status'], 'cancelled')
        self.assertEqual(self.m.get_live('stopped')['stats']['total_time'], 10)
        self.assertIsNone(self.m.task_run_log('stopped'))
        self.assertIsNone(self.m.get_task_detail('../secret'))

    def test_incremental_pages_include_finish_and_reset_on_truncate(self):
        path = self.write('many', [dict(t=i, step=1, phase='decide') for i in range(450)])
        first = self.m.get_live('many', -1)
        self.assertEqual(len(first['updates']), 200)
        self.assertTrue(first['has_more'])
        second = self.m.get_live('many', first['cursor'])
        self.assertEqual(second['updates'][0]['idx'], 200)
        self.write('many', [dict(t=2, phase='finish', msg='complete', success=True)])
        reset = self.m.get_live('many', second['cursor'])
        self.assertTrue(reset['reset'])
        self.assertEqual(reset['updates'][0]['action']['action'], 'finish')


class LauncherTests(unittest.TestCase):
    def launch(self, mode):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        m = dashboard();m.PROJECT_ROOT = root;m.LOGS_ROOT = root / 'logs'
        self.addCleanup(m.stop_task)
        main = """import sys,time
sys.path.insert(0, SOURCE)
from autogui.core.decision_log import DecisionLog
log=DecisionLog('launched', LOGS, verbose=False)
log.phase('setup','ready',task=sys.argv[1])
print('TASK_STDOUT_VISIBLE',flush=True)
time.sleep(DELAY)
log.summary(success=True,status='complete',steps=1,usage={'calls':1,'total_tokens':16},route_counts={},vlm_reflections=0,elapsed=0.7)
""".replace('SOURCE', repr(str(ROOT / 'server/autogui'))).replace('LOGS', repr(str(m.LOGS_ROOT))).replace('DELAY', '10' if mode == 'cancel' else '0.7')
        (root / 'main.py').write_text(main)
        return m

    def test_launched_stdout_is_saved_and_finish_survives_monitor(self):
        m = self.launch('finish')
        terminal = io.StringIO()
        with patch('sys.stdout', terminal):
            stem, error = m.launch_task('test launched')
            self.assertIsNone(error)
            deadline = time.monotonic() + 4
            while m.get_live(stem)['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.02)
        result = m.get_live(stem)
        self.assertEqual(result['status'], 'done')
        path = m.task_run_log(stem)
        self.assertIsNotNone(path)
        self.assertIn('TASK_STDOUT_VISIBLE', path.read_text())
        self.assertIn('TASK_STDOUT_VISIBLE', terminal.getvalue())

    def test_stop_records_cancellation_and_elapsed_time(self):
        m = self.launch('cancel')
        with patch('sys.stdout', io.StringIO()):
            stem, error = m.launch_task('test cancel')
            self.assertIsNone(error)
            self.assertTrue(m.stop_task())
            deadline = time.monotonic() + 4
            while m.get_live(stem)['status'] != 'cancelled' and time.monotonic() < deadline:
                time.sleep(.02)
        result = m.get_live(stem)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['reason'], '用户停止任务')
        self.assertGreater(result['stats']['total_time'], 0)

    def test_favicon_and_shared_script_are_served(self):
        m = dashboard()
        handler = object.__new__(m.Handler)
        for url, expected in [('/favicon.ico',204),('/log_view.js',200)]:
            handler.path = url
            records = []
            handler._send = lambda code, body, kind: records.append((code, body, kind))
            handler.do_GET()
            self.assertEqual(records[0][0], expected)


class ActionTimingTests(unittest.TestCase):
    def test_action_execution_is_measured_separately_from_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(log_dir=tmp,verbose=False,skills={'enabled':False})
            cfg.agent.delay_after_action = 0
            runner = Runner(cfg=cfg,task='timing')
            runner.log = DecisionLog('timing',tmp,verbose=False)
            with patch.object(runner,'_dispatch',return_value='OK back'):
                result = runner.act({'action':{'type':'BACK'},'step':1})
            records = [e for e in runner.log.events if e['phase']=='act']
            self.assertEqual([e['state'] for e in records],['start','end'])
            self.assertGreaterEqual(records[-1]['duration_ms'],0)
            self.assertEqual(runner.log.events[-1]['phase'],'wait')
            self.assertEqual(result['last_action']['type'],'BACK')
            runner.log.close()


class ClientTests(unittest.TestCase):
    def run_client(self, answers, *, debug=False, retries=1):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        log = DecisionLog('test', self.tmp.name, verbose=False, debug=debug);self.addCleanup(log.close)
        queue = iter(answers)
        def create(**kwargs):
            result = next(queue)
            if isinstance(result, Exception):raise result
            return result
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return GLMClient(GLMConfig(api_key='unused', max_retries=retries), client=client, log=log), log

    def test_usage_without_debug_and_request_summary(self):
        glm, log = self.run_client([response()])
        text, usage = glm.chat([], kind='decide')
        self.assertEqual(usage.total_tokens, 16)
        self.assertEqual(glm.usage.calls, 1)
        records = [e for e in log.events if e['phase']=='model']
        self.assertEqual([e['state'] for e in records], ['start','end'])
        self.assertEqual(records[-1]['usage_total']['total_tokens'], 16)
        self.assertEqual(project(log.events, 'test')['stats']['total_tokens'], 16)

    def test_empty_response_retry_counts_tokens_once(self):
        glm, log = self.run_client([response(text='',in_tok=10,out_tok=1),response(in_tok=20,out_tok=2)], debug=True)
        with patch('autogui.brain.glm_client.time.sleep'):
            _, usage = glm.chat([])
        self.assertEqual(usage.total_tokens, 33)
        self.assertEqual(glm.usage.total_tokens, 33)
        self.assertEqual(glm.usage.calls, 2)
        self.assertEqual(glm.usage.retries, 1)
        self.assertEqual(project(log.events, 'test')['stats']['total_tokens'], 33)

    def test_stream_progress_is_rate_limited_and_does_not_add_usage(self):
        glm, log = self.run_client([])
        clock = [0.0]
        def stream():
            for tick, event in zip([6.0, 13.0], response()):
                clock[0] = tick
                yield event
        glm._client.chat.completions.create = lambda **kwargs: stream()
        with patch('autogui.brain.glm_client.time.perf_counter', side_effect=lambda:clock[0]):
            glm.chat([])
        progress = [e for e in log.events if e.get('state') == 'progress']
        self.assertEqual(len(progress), 2)
        self.assertEqual(glm.usage.total_tokens, 16)
        self.assertEqual(glm.usage.calls, 1)

    def test_transport_error_recorded_and_usage_incomplete(self):
        glm, log = self.run_client([RuntimeError('fail')],retries=0)
        with self.assertRaises(GLMError):glm.chat([])
        self.assertEqual(glm.usage.calls, 1)
        self.assertFalse(glm.usage.usage_complete)
        self.assertEqual(log.events[-1]['status'],'error')
        self.assertGreaterEqual(log.events[-1]['latency_ms'],0)

    def test_json_retry_usage_and_calls(self):
        glm, log = self.run_client([response(text='bad json'),response()])
        self.assertEqual(glm.chat_json([])['type'],'BACK')
        self.assertEqual(glm.last_call.total_tokens,32)
        self.assertEqual(glm.usage.total_tokens,32)
        self.assertEqual(glm.usage.retries,1)
        self.assertEqual(project(log.events,'test')['stats']['total_tokens'],32)

    def test_log_first_event_has_process_identity_and_monotonic_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=DecisionLog('clock',tmp,verbose=False)
            with patch('autogui.core.decision_log.time.time',return_value=0):log.phase('setup','ready')
            self.assertGreaterEqual(log.events[0]['t'],0)
            self.assertEqual(log.events[0]['pid'],os.getpid())
            self.assertEqual(log.events[0]['process_key'],process_key(os.getpid()))
            log.close()


if __name__=='__main__':unittest.main()
