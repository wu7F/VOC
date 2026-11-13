"""Monthly service behavior uses a frozen clock and synthetic provider receipts."""
import sys
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import sales_schedule as schedule
from server.sales_lock import try_lock


class SalesScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = dict(url='https://example.feishu.cn/sheets/SYNTHETIC?sheet=sheet1',
                        write_back=True, auto_sync=True, platform='AmazonCs')
        self.clock = datetime(2026, 10, 31, 16, tzinfo=timezone.utc).timestamp()

    def tick(self, out=None, read=None, now=None):
        with (patch.object(schedule.feishu_sales, 'load_settings', return_value=self.cfg),
              patch.object(schedule.eccang_sales, 'status', return_value={}),
              patch.object(schedule.eccang_sales, 'sync', return_value=out or
                           {'complete': True, 'sync': {}, 'writeback': {'status': 'ok'}}) as sync,
              patch.object(schedule.feishu_sales, 'sync', return_value=read or {'complete': True}) as pull):
            result = schedule.tick(self.root, now=now or self.clock)
            return result, sync.call_count, pull.call_count

    def test_one_per_shanghai_month_survives_restart_and_catches_up(self):
        st, calls, pulls = self.tick()
        self.assertEqual(st['last_month'], '2026-11')
        self.assertEqual((calls, pulls), (1, 1))
        self.assertEqual(self.tick()[1:], (0, 0))
        december = datetime(2026, 11, 30, 16, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.tick(now=december)[0]['last_month'], '2026-12')

    def test_failure_is_not_complete_and_retries_after_backoff(self):
        st, _, pulls = self.tick(out={'complete': True, 'sync': {'error': 'synthetic write failure'}})
        self.assertNotIn('last_month', st)
        self.assertTrue(st['error'])
        self.assertEqual(pulls, 0)
        self.assertEqual(self.tick(now=self.clock + 60)[1], 0)
        self.assertEqual(self.tick(now=self.clock + 3601)[0]['last_month'], '2026-11')

    def test_readback_failure_does_not_advance_month(self):
        st, calls, pulls = self.tick(read={'complete': False, 'error': 'synthetic'})
        self.assertNotIn('last_month', st)
        self.assertEqual((calls, pulls), (1, 1))

    def test_disabled_and_other_process_lock_do_nothing(self):
        self.cfg['auto_sync'] = False
        self.assertEqual(self.tick()[1:], (0, 0))
        self.cfg['auto_sync'] = True
        with try_lock(self.root, 'feishu-sales-monthly') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.tick()[1:], (0, 0))

    def test_target_change_gets_new_monthly_receipt(self):
        self.tick()
        self.cfg['url'] = self.cfg['url'].replace('sheet1', 'sheet2')
        self.assertEqual(self.tick()[1:], (1, 1))


if __name__ == '__main__':
    unittest.main()
