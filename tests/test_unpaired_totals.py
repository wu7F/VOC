"""Independent additive-contract tests for per-Team unpaired SKU diagnostics."""
import copy
from datetime import datetime
import unittest

from server import bitable_board as bb


TEAMS = {
    'Team 1': {'accounts': ['A', 'B']},
    'Team 2': {'accounts': ['C']},
    'Team 3': {'accounts': ['D']},
}
PAIR = {'P': {'display_name': 'Paired', 'wf_part_number': 'WF-P'}}


def row(sku, qty, account='A', team='Team 1', month='2026-04', **extra):
    return dict(platform='Amazon', account=account, display_name=account,
                sku=sku, qty=qty, invalid=qty is None, month=month,
                _source_team=team, **extra)


def build(rows, pair_map=PAIR):
    return bb.build_snapshot(rows, [], now=datetime(2026, 11, 24, tzinfo=bb.UTC8),
                             source_meta={}, teams=TEAMS, pair_map=pair_map,
                             aftersale_summary={'connected': False})


def by_sku(snapshot, team=0):
    return {item['sku']: item for item in snapshot['teams'][team]['unpaired_sku_totals']}


class UnpairedTotalsTests(unittest.TestCase):
    def test_same_sku_sums_across_shops_and_months_but_not_teams(self):
        snap = build([row('U', 3), row('U', 4, month='2026-05'),
                      row('U', 5, account='B'), row('U', 9, account='C', team='Team 2')])
        self.assertEqual(by_sku(snap)['U'], {'sku': 'U', 'units': 12, 'known_qty': 12, 'has_unknown': False})
        self.assertEqual(by_sku(snap, 1)['U']['units'], 9)
        self.assertEqual(snap['unpaired']['total_units'], 21)
        for team in snap['teams']:
            self.assertEqual(sum(x['units'] for x in team['unpaired_sku_totals']), team['unpaired_units'])

    def test_zero_and_duplicate_rows_follow_existing_unpaired_totals(self):
        snap = build([row('ZERO', 0), row('DUP', 2), row('DUP', 2)])
        self.assertEqual(by_sku(snap)['ZERO']['units'], 0)
        self.assertEqual(by_sku(snap)['DUP']['units'], 4)
        self.assertEqual(snap['teams'][0]['unpaired_units'], 4)
        self.assertEqual(snap['teams'][0]['unpaired_skus'], ['DUP', 'ZERO'])

    def test_invalid_part_preserves_unknown_and_known_subtotal(self):
        snap = build([row('PARTIAL', 7), row('PARTIAL', None), row('KNOWN', 3), row('INVALID', None)])
        items = by_sku(snap)
        self.assertEqual(items['PARTIAL'], {'sku': 'PARTIAL', 'units': None, 'known_qty': 7, 'has_unknown': True})
        self.assertIsNone(items['INVALID']['units'])
        self.assertEqual(items['INVALID']['known_qty'], 0)
        self.assertEqual(items['KNOWN']['units'], 3)
        self.assertIsNone(snap['teams'][0]['unpaired_units'])
        self.assertEqual(snap['teams'][0]['unpaired']['known_qty'], 10)
        self.assertEqual(sum(x['known_qty'] for x in items.values()), 10)

    def test_existing_scope_filters_apply_before_new_aggregation(self):
        wrong_platform = row('OTHER', 20)
        wrong_platform['platform'] = 'Wayfair'
        rows = [row('U', 5), row('P', 10), wrong_platform,
                row('EARLY', 30, month='2026-03'), row('FUTURE', 40, month='2026-12'),
                row('WRONG_TABLE', 50, account='C', team='Team 1'),
                row('UNKNOWN_ACCOUNT', 60, account='UNKNOWN')]
        snap = build(rows)
        self.assertEqual(list(by_sku(snap)), ['U'])
        self.assertEqual(by_sku(snap)['U']['units'], 5)
        self.assertEqual(snap['teams'][0]['shops'][0]['months']['2026-04'], 10)
        self.assertEqual(snap['unpaired']['total_units'], 5)

    def test_empty_team_and_disabled_pairing_produce_no_unpaired_entries(self):
        self.assertEqual(build([])['teams'][0]['unpaired_sku_totals'], [])
        snap = build([row('U', 5)], pair_map=None)
        self.assertTrue(all(t['unpaired_sku_totals'] == [] for t in snap['teams']))
        self.assertEqual(snap['unpaired']['total_units'], 0)

    def test_only_aggregates_added_and_input_not_mutated(self):
        rows = [row('<SKU>', 6, order_id='private-order', record_id='private-record', text='private-text')]
        before = copy.deepcopy(rows)
        snap = build(rows)
        self.assertEqual(rows, before)
        item = by_sku(snap)['<SKU>']
        self.assertEqual(set(item), {'sku', 'units', 'known_qty', 'has_unknown'})
        self.assertNotIn('private-', str(snap))


if __name__ == '__main__':
    unittest.main()
