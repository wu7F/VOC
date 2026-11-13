"""Offline regression tests for duplicate starts and complete sales snapshots."""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = BASE / 'src' if (BASE / 'src' / 'server').is_dir() else BASE
SOURCE = Path(os.environ.get('VOC_TEST_SRC', str(DEFAULT_SOURCE))).resolve()
sys.path.insert(0, str(SOURCE))
es = importlib.import_module('server.eccang_sales')


class SyncRegression(unittest.TestCase):
    def setUp(self):
        es._CANCEL.clear()
        es._SYNC['running'] = False

    def test_duplicate_start_status_and_cancel_return_without_deadlock(self):
        # An isolated process ensures the original non-reentrant lock cannot
        # leave the rest of the regression suite permanently blocked.
        code = r'''
import json, sys, threading
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from server import eccang_sales as es
es._SYNC['running'] = True
es.eccang.load_config = lambda _: {}
blocked = []
for fn in (es.start_sync, es.sync, es.status, es.cancel):
    t = threading.Thread(target=fn, args=(None,), daemon=True)
    t.start()
    t.join(.3)
    if t.is_alive(): blocked.append(fn.__name__)
assert not blocked, 'Duplicate-start deadlock blocks: ' + ', '.join(blocked)
'''
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(SOURCE)], capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_concurrent_background_starts_reserve_one_worker(self):
        code = r'''
import sys, threading
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from server import eccang_sales as es
release = threading.Event()
entered = threading.Event()
calls = []
def inner(*args, **kwargs):
    calls.append(1)
    entered.set()
    release.wait(2)
    with es._SYNC_LOCK:
        es._SYNC['running'] = False
    return es.status(None)
try:
    with patch.object(es, '_sync_inner', side_effect=inner), patch.object(es.eccang, 'load_config', return_value={}):
        starters = [threading.Thread(target=es.start_sync, args=(None,), daemon=True) for _ in range(2)]
        for t in starters: t.start()
        assert entered.wait(1)
        for t in starters:
            t.join(.3)
            assert not t.is_alive(), 'Background start must return promptly'
        assert len(calls) == 1, calls
        assert es.status(None)['sync']['running']
finally:
    release.set()
'''
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(SOURCE)], capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def run_sync(self, initial=None, fail=False, cancel=False, full=False):
        with tempfile.TemporaryDirectory(prefix='voc-sync-') as tmp:
            root = Path(tmp)
            if initial is not None:
                es._save(root, deepcopy(initial))
            requests = []
            writes = []
            save = es._save
            def capture(_, value):
                writes.append(deepcopy(value))
                save(root, value)
            def call(method, biz, **kw):
                account = biz['condition']['user_account_list'][0]
                requests.append(deepcopy(biz))
                if fail:
                    raise es.eccang.EccangError('synthetic failure')
                if cancel:
                    es._CANCEL.set()
                return {'total': 1, 'data': [{'order_code': 'SYNTH-' + account,
                          'date_create': '2026-10-20 00:00:00', 'product_count': 5}]}
            cfg = {'app_key':'SYNTHETIC','app_secret':'SYNTHETIC','service_id':'SYNTHETIC'}
            with patch.object(es.eccang, 'load_config', return_value=cfg), \
                 patch.object(es.eccang, 'call', side_effect=call), \
                 patch.object(es, 'accounts_for_shops', return_value=['ACCOUNT_A','ACCOUNT_B']), \
                 patch.object(es, '_save', side_effect=capture):
                result = es.sync(root, full=full)
            return es.load(root), requests, writes, result

    def complete_cache(self):
        c = es.empty_cache()
        c.update(complete=True, synced_at=123, sys_cursor='2026-10-19 00:00:00',
                 accounts=['ACCOUNT_A','ACCOUNT_B'], done_accounts=['ACCOUNT_A','ACCOUNT_B'],
                 order_index={'SYNTH-OLD':{'a':'ACCOUNT_A','m':'2026-10'}},
                 qty={'ACCOUNT_A':{'2026-10':10}})
        return c

    def test_complete_snapshot_lifecycle_and_failure(self):
        complete, calls, writes, result = self.run_sync(full=True)
        self.assertIs(complete.get('complete'), True)
        self.assertTrue(es.is_complete(complete))
        self.assertTrue(result['complete'])
        self.assertTrue(all(c.get('complete') is False for c in writes[:-1]))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c['condition']['date_create_sys_start'] == '2026-01-01 00:00:00' for c in calls))

        for fail, cancel in ((True, False), (False, True)):
            with self.subTest(fail=fail, cancel=cancel):
                c, _, writes, _ = self.run_sync(self.complete_cache(), fail=fail, cancel=cancel)
                self.assertIs(c.get('complete'), False)
                self.assertFalse(es.is_complete(c))
                self.assertEqual(c['synced_at'], 123)
                self.assertTrue(all(w.get('complete') is False for w in writes))

        c, calls, _, _ = self.run_sync(self.complete_cache())
        self.assertTrue(es.is_complete(c))
        self.assertTrue(all(x['condition']['date_create_sys_start'] == '2026-10-19 00:00:00' for x in calls))

    def test_legacy_and_partial_increment_require_full_rebuild(self):
        for legacy in (True, False):
            initial = self.complete_cache()
            if legacy:
                del initial['complete']
            else:
                initial['complete'] = False
            with self.subTest(legacy=legacy):
                c, calls, _, _ = self.run_sync(initial)
                self.assertTrue(es.is_complete(c))
                self.assertTrue(all(x['condition']['date_create_sys_start'] == '2026-01-01 00:00:00' for x in calls))
                self.assertNotIn('SYNTH-OLD', c['order_index'])

    def test_new_partial_full_snapshot_resumes_completed_accounts(self):
        initial = es.empty_cache()
        initial.update(complete=False, accounts=['ACCOUNT_A','ACCOUNT_B'],
                       done_accounts=['ACCOUNT_A'],
                       order_index={'SYNTH-OLD':{'a':'ACCOUNT_A','m':'2026-10'}},
                       qty={'ACCOUNT_A':{'2026-10':10}})
        c, calls, _, _ = self.run_sync(initial)
        self.assertTrue(es.is_complete(c))
        self.assertEqual([x['condition']['user_account_list'] for x in calls], [['ACCOUNT_B']])
        self.assertIn('SYNTH-OLD', c['order_index'])

    def test_changed_accounts_require_full_history_not_recent_increment(self):
        for previous in (['ACCOUNT_A'], ['ACCOUNT_A','ACCOUNT_B','ACCOUNT_C']):
            with self.subTest(previous=previous):
                initial = self.complete_cache()
                initial['accounts'] = previous
                initial['done_accounts'] = previous
                c, calls, _, _ = self.run_sync(initial)
                self.assertTrue(es.is_complete(c))
                self.assertTrue(all(x['condition']['date_create_sys_start'] == '2026-01-01 00:00:00' for x in calls))
                self.assertNotIn('SYNTH-OLD', c['order_index'])

    def test_complete_marker_requires_timestamp_and_all_accounts(self):
        c = self.complete_cache()
        self.assertTrue(es.is_complete(c))
        for fields in ({'complete':False}, {'synced_at':0}, {'accounts':[]}, {'done_accounts':['ACCOUNT_A']}):
            with self.subTest(fields=fields):
                altered = dict(c, **fields)
                self.assertFalse(es.is_complete(altered))


if __name__ == '__main__':
    unittest.main()
