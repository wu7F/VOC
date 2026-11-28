"""G02 complete-package rules: separate old/new packages, DG corner, LEFT rollups, AF integration."""
import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from server import bitable_board as bb
from server import component_sales as cs
from server import aftersale_live as al

NOW = datetime(2026, 11, 24, tzinfo=bb.UTC8)
TEAMS = {'Team 1': {'accounts': ['A', 'B']}, 'Team 2': {'accounts': ['C']},
         'Team 3': {'accounts': ['D']}}


def row(sku, qty, account='A', team='Team 1', month='2026-04', platform='Amazon'):
    return {'sku': sku, 'qty': qty, 'invalid': qty is None, 'account': account,
            'display_name': account, '_source_team': team, 'month': month, 'platform': platform}


def build(rows, **kwargs):
    params = dict(now=NOW, source_meta={}, teams=TEAMS, pair_map={},
                  aftersale_summary={'connected': False})
    params.update(kwargs)
    return bb.build_snapshot(rows, [], **params)


def shop(snap, account='A'):
    return next(s for t in snap['teams'] for s in t['shops'] if s['account'] == account)


def sold(snap, sku, month='2026-04', account='A'):
    return next(s for s in shop(snap, account)['skus'] if s['sku'] == sku)['sold'][month]


def pair(sku, display='X'):
    return {sku: {'display_name': display, 'wf_part_number': 'W'}}


class G02TwoSeatPackageTests(unittest.TestCase):
    CR_L = 'LAYJMZPG02CR-2L'
    CR_R = 'LAYJMZPG02CR-2R'
    CR_NL = 'LAYJMZPG02CR-2LEFT'
    CR_NR = 'LAYJMZPG02CR-2RIGHT'
    DG_L = 'LAYJMZPG02DG-2L'
    DG_R = 'LAYJMZPG02DG-2R'
    DG_NL = 'LAYJMZPG02DG-2LEFT'
    DG_NR = 'LAYJMZPG02DG-2RIGHT'

    def pm(self):
        return {self.CR_L: {'display_name': 'CR', 'wf_part_number': 'WCR'},
                self.DG_L: {'display_name': 'DG', 'wf_part_number': 'WDG'},
                self.CR_NL: {'display_name': 'CR new LEFT', 'wf_part_number': 'WCRNL'},
                self.DG_NL: {'display_name': 'DG new LEFT', 'wf_part_number': 'WDGNL'}}

    def test_old_and_new_packages_sum_per_color(self):
        for L, R, NL, NR in ((self.CR_L, self.CR_R, self.CR_NL, self.CR_NR),
                             (self.DG_L, self.DG_R, self.DG_NL, self.DG_NR)):
            with self.subTest(L=L):
                s = build([row(L, 10), row(R, 8), row(NL, 7), row(NR, 5)], pair_map=self.pm())
                self.assertEqual(sold(s, L), 13)

    def test_one_version_absent_contributes_nothing(self):
        s = build([row(self.CR_L, 10), row(self.CR_R, 8)], pair_map=self.pm())
        self.assertEqual(sold(s, self.CR_L), 8)
        s2 = build([row(self.CR_NL, 7), row(self.CR_NR, 5)], pair_map=self.pm())
        self.assertEqual(sold(s2, self.CR_L), 5)

    def test_missing_part_in_one_version_only_other_counts(self):
        s = build([row(self.CR_L, 10), row(self.CR_NL, 7), row(self.CR_NR, 5)], pair_map=self.pm())
        self.assertEqual(sold(s, self.CR_L), 5)
        s2 = build([row(self.CR_L, 10), row(self.CR_R, 8), row(self.CR_NL, 7)], pair_map=self.pm())
        self.assertEqual(sold(s2, self.CR_L), 8)

    def test_explicit_zero_version_contributes_zero(self):
        s = build([row(self.CR_L, 10), row(self.CR_R, 0), row(self.CR_NL, 7), row(self.CR_NR, 5)],
                  pair_map=self.pm())
        self.assertEqual(sold(s, self.CR_L), 5)

    def test_no_cross_color_or_version_borrowing(self):
        s = build([row(self.CR_L, 10), row(self.DG_R, 8), row(self.CR_NL, 7), row(self.DG_NR, 5)],
                  pair_map=self.pm())
        self.assertEqual(sold(s, self.CR_L), 0)
        self.assertEqual(sold(s, self.DG_L), 0)

    def test_new_left_standalone_does_not_double_count_or_leak(self):
        rows = [row(self.CR_L, 10), row(self.CR_R, 8), row(self.CR_NL, 7), row(self.CR_NR, 5)]
        before = copy.deepcopy(rows)
        s = build(rows, pair_map=self.pm())
        self.assertEqual(rows, before)
        skus = {x['sku'] for x in shop(s)['skus']}
        self.assertNotIn(self.CR_NL, skus)
        self.assertNotIn(self.CR_NR, skus)
        self.assertNotIn(self.CR_NL, {p['sku'] for p in s['products']})
        self.assertEqual(s['unpaired']['total_units'], 0)


class G02ThreeSeatPackageTests(unittest.TestCase):
    CR = ('LAYJMZPG02CR-3L', 'LAYJMZPG02CR-3M', 'LAYJMZPG02CR-3R')
    CRN = ('LAYJMZPG02CR-3LEFT', 'LAYJMZPG02CR-3MIDDLE', 'LAYJMZPG02CR-3RIGHT')
    DG = ('LAYJMZPG02DG-3L', 'LAYJMZPG02DG-3M', 'LAYJMZPG02DG-3R')
    DGN = ('LAYJMZPG02DG-3LEFT', 'LAYJMZPG02DG-3MIDDLE', 'LAYJMZPG02DG-3RIGHT')

    def pm(self):
        return {self.CR[0]: {'display_name': 'CR3', 'wf_part_number': 'WCR3'},
                self.DG[0]: {'display_name': 'DG3', 'wf_part_number': 'WDG3'}}

    def test_old_and_new_three_seat_sum(self):
        for old, new in ((self.CR, self.CRN), (self.DG, self.DGN)):
            with self.subTest(old=old):
                rows = [row(old[0], 10), row(old[1], 8), row(old[2], 6),
                        row(new[0], 5), row(new[1], 4), row(new[2], 3)]
                s = build(rows, pair_map=self.pm())
                self.assertEqual(sold(s, old[0]), 9)

    def test_each_version_missing_part_yields_zero_for_that_version(self):
        for old, new in ((self.CR, self.CRN), (self.DG, self.DGN)):
            with self.subTest(old=old):
                rows = [row(old[0], 10), row(old[1], 8), row(new[0], 5), row(new[1], 4), row(new[2], 3)]
                s = build(rows, pair_map=self.pm())
                self.assertEqual(sold(s, old[0]), 3)

    def test_backrest_ignored_and_same_color_grain(self):
        rows = [row(self.CR[0], 10), row(self.CR[1], 8), row(self.CR[2], 6),
                row('LAYJMZPG02CR-BACKREST', 999), row('LAYJMZPG02CR-3AR', 1)]
        s = build(rows, pair_map=self.pm())
        self.assertEqual(sold(s, self.CR[0]), 6)
        self.assertEqual(s['unpaired']['total_units'], 0)


class G02CornerPackageTests(unittest.TestCase):
    LT = 'LAYJMZPG02DG-LT'
    SEVEN = ('LAYJMZPG02DG-LT', 'LAYJMZPG02DG-ME1', 'LAYJMZPG02DG-ZJKB',
             'LAYJMZPG02DG-ZJDZ', 'LAYJMZPG02DG-ME2', 'LAYJMZPG02DG-RT', 'LAYJMZPG02DG-CJ')
    EIGHT = ('LAYJMZPG02DG-KBT', 'LAYJMZPG02DG-2LT', 'LAYJMZPG02DG-2MT',
             'LAYJMZPG02DG-ZJKBT', 'LAYJMZPG02DG-ZJDZT', 'LAYJMZPG02DG-3MT',
             'LAYJMZPG02DG-2RT', 'LAYJMZPG02DG-CJT')

    def pm(self):
        return pair(self.LT, 'DGcorner')

    def test_both_complete_sum(self):
        rows = [row(p, 4) for p in self.SEVEN] + [row(p, 2) for p in self.EIGHT]
        s = build(rows, pair_map=self.pm())
        self.assertEqual(sold(s, self.LT), 6)

    def test_each_eight_missing_independently_zeroes_that_version(self):
        for missing in self.EIGHT:
            with self.subTest(missing=missing):
                rows = [row(p, 4) for p in self.SEVEN]
                rows += [row(p, 2) for p in self.EIGHT if p != missing]
                s = build(rows, pair_map=self.pm())
                self.assertEqual(sold(s, self.LT), 4)

    def test_only_eight_or_only_seven(self):
        eight = build([row(p, 2) for p in self.EIGHT], pair_map=self.pm())
        self.assertEqual(sold(eight, self.LT), 2)
        seven = build([row(p, 4) for p in self.SEVEN], pair_map=self.pm())
        self.assertEqual(sold(seven, self.LT), 4)

    def test_absent_both_no_target(self):
        s = build([row('PLAIN', 4)], pair_map=dict(self.pm(), PLAIN=pair('PLAIN')['PLAIN']))
        self.assertNotIn(self.LT, {x['sku'] for x in shop(s)['skus']})

    def test_invalid_or_duplicate_observed_part_unknown(self):
        rows = [row(p, 4) for p in self.SEVEN] + [row(p, 2) for p in self.EIGHT]
        bad = build(rows + [row(self.EIGHT[0], None)], pair_map=self.pm())
        self.assertIsNone(sold(bad, self.LT))
        dup = build(rows + [row(self.EIGHT[0], 2)], pair_map=self.pm())
        self.assertIsNone(sold(dup, self.LT))

    def test_whole_no_record_seat_canonical_not_synthesized(self):
        seat = 'LAYJMZPG02DG-2L'
        pm = dict(self.pm(), **{seat: {'display_name': 'DG2', 'wf_part_number': 'W'}})
        s = build([row('PLAIN', 4)], pair_map=pm)
        self.assertNotIn(seat, {x['sku'] for x in shop(s)['skus']})

    def test_missing_part_in_new_three_seat_returns_old_complete(self):
        seat = 'LAYJMZPG02DG-3L'
        pm = dict(self.pm(), **{seat: {'display_name': 'DG3', 'wf_part_number': 'W'}})
        rows = [row('LAYJMZPG02DG-3L', 10), row('LAYJMZPG02DG-3M', 8), row('LAYJMZPG02DG-3R', 6),
                row('LAYJMZPG02DG-3LEFT', 5), row('LAYJMZPG02DG-3MIDDLE', 4)]
        s = build(rows, pair_map=pm)
        self.assertEqual(sold(s, seat), 6)

    def test_corner_rows_count_only_corner_not_seat(self):
        seat = 'LAYJMZPG02DG-2L'
        pm = dict(self.pm(), **{seat: {'display_name': 'DG2', 'wf_part_number': 'W'}})
        rows = [row(p, 4) for p in self.SEVEN] + [row(p, 2) for p in self.EIGHT]
        rows += [row(seat, 10), row('LAYJMZPG02DG-2R', 8)]
        s = build(rows, pair_map=pm)
        self.assertEqual(sold(s, self.LT), 6)
        self.assertEqual(sold(s, seat), 8)

    def test_short_zjkbt_zjdzt_cannot_fulfill_long_parts(self):
        rows = [row(p, 4) for p in self.SEVEN]
        rows += [row(p, 2) for p in self.EIGHT if p not in ('LAYJMZPG02DG-ZJKBT', 'LAYJMZPG02DG-ZJDZT')]
        rows += [row('LAYJG02DG-ZJKBT', 2), row('LAYJG02DG-ZJDZT', 2)]
        s = build(rows, pair_map=self.pm())
        self.assertEqual(sold(s, self.LT), 4)


class PairMapGuardTests(unittest.TestCase):
    def test_new_left_paired_but_old_target_missing_kept_standalone(self):
        nl = 'LAYJMZPG02CR-2LEFT'
        pm = pair(nl, 'newleft')
        s = build([row(nl, 7)], pair_map=pm)
        self.assertIn(nl, {p['sku'] for p in s['products']})
        self.assertIn(nl, {x['sku'] for x in shop(s)['skus']})
        self.assertNotIn('LAYJMZPG02CR-2L', {p['sku'] for p in s['products']})


class G02AftersaleIntegrationTests(unittest.TestCase):
    CR_L = 'LAYJMZPG02CR-2L'
    CR_R = 'LAYJMZPG02CR-2R'
    CR_NL = 'LAYJMZPG02CR-2LEFT'
    CR_NR = 'LAYJMZPG02CR-2RIGHT'
    CR3_L = 'LAYJMZPG02CR-3L'
    CR3_NL = 'LAYJMZPG02CR-3LEFT'

    def _pair(self):
        return {self.CR_L: {'display_name': 'CR2', 'wf_part_number': 'W'},
                self.CR3_L: {'display_name': 'CR3', 'wf_part_number': 'W'},
                self.CR_NL: {'display_name': 'CR2 new LEFT', 'wf_part_number': 'W'},
                self.CR3_NL: {'display_name': 'CR3 new LEFT', 'wf_part_number': 'W'}}

    def _sales(self, sku, qty, month='2026-04'):
        shops = []
        for team, info in bb.TEAM_TABLES.items():
            shops.append({'name': team, 'shops': [
                {'account': a, 'skus': ([{'sku': sku, 'sold': {month: qty}}]
                                        if a == 'xingshan_US' else [])}
                for a in info['accounts']]})
        return {'ok': True, 'teams': shops}

    def _run(self, sales, pair_map, mapping_text, fields_extra=None, extra_record=None):
        fields = {'SKU': 'SELLER', '易仓sku': '', '原文': 'damage', '日期': '2026-04-10',
                  '店铺名称': '行善'}
        if fields_extra:
            fields.update(fields_extra)
        table1 = [{'record_id': 'r1', 'fields': fields}]
        if extra_record is not None:
            table1.append({'record_id': 'r2', 'fields': extra_record})
        rows = {al.AFTERSALE_TABLE_1: table1,
                al.AFTERSALE_TABLE_2: []}
        meta = {n: {} for n in al.REQUIRED_FIELDS + (al.SHOP_FIELD_1, al.SHOP_FIELD_2)}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'm.csv'
            path.write_text(mapping_text, encoding='utf-8')
            with patch.object(al, 'fetch_fields', return_value=meta), \
                 patch.object(al, 'fetch_records',
                              side_effect=lambda t, a, tb, http=None: (rows[tb], 1)):
                return al.load('t', 'a', pair_map, sales, mapping_path=path,
                               fetched_at='2026-11-24T00:00:00+08:00')

    def test_new_left_folds_to_canonical_before_month_fallback(self):
        sales = self._sales(self.CR_L, 8)
        res = self._run(sales, self._pair(),
                        'seller_sku,account,warehouse_sku,qty\nSELLER,所有账户,' + self.CR_NL + ',1\n',
                        fields_extra={'日期': '2026-05-10'})
        self.assertTrue(res['connected'])
        self.assertEqual(res['total']['numerator'], 1)
        self.assertEqual(res['total']['denominator'], 8)
        self.assertTrue(any(x['sku'] == self.CR_L and x['numerator'] == 1
                            and x['month'] == '2026-04'
                            for x in res['sku_store_month']))
        self.assertFalse(any(x['sku'] == self.CR_L and x['numerator'] > 0
                             and x['month'] == '2026-05'
                             for x in res['sku_store_month']))
        self.assertFalse(any(x['sku'] == self.CR_NL for x in res['sku_store_month']))

    def test_old_and_new_left_each_counted_once_same_canonical(self):
        sales = self._sales(self.CR_L, 8)
        res = self._run(sales, self._pair(), 'seller_sku,account,warehouse_sku,qty\n',
                        fields_extra={'易仓sku': self.CR_NL},
                        extra_record={'SKU': 'SELLER_OLD', '易仓sku': self.CR_L,
                                      '原文': 'damage', '日期': '2026-04-11',
                                      '店铺名称': '行善'})
        self.assertTrue(res['connected'])
        self.assertEqual(res['total']['numerator'], 2)
        self.assertEqual(res['total']['denominator'], 8)
        self.assertTrue(any(x['sku'] == self.CR_L and x['numerator'] == 2
                            for x in res['sku_store_month']))
        self.assertFalse(any(x['sku'] == self.CR_NL for x in res['sku_store_month']))

    def test_direct_source_warehouse_new_left_matches_canonical(self):
        sales = self._sales(self.CR_L, 8)
        res = self._run(sales, self._pair(), 'seller_sku,account,warehouse_sku,qty\n',
                        fields_extra={'易仓sku': self.CR_NL})
        self.assertTrue(res['connected'])
        self.assertEqual(res['total']['numerator'], 1)
        self.assertEqual(res['total']['denominator'], 8)

    def test_new_three_left_case(self):
        sales = self._sales(self.CR3_L, 6)
        res = self._run(sales, self._pair(),
                        'seller_sku,account,warehouse_sku,qty\nSELLER,所有账户,' + self.CR3_NL + ',1\n')
        self.assertTrue(res['connected'])
        self.assertEqual(res['total']['numerator'], 1)
        self.assertEqual(res['total']['denominator'], 6)

    def test_unpaired_right_direct_field_ignored(self):
        sales = self._sales(self.CR_L, 8)
        res = self._run(sales, self._pair(), 'seller_sku,account,warehouse_sku,qty\n',
                        fields_extra={'易仓sku': self.CR_NR})
        self.assertTrue(res['connected'])
        self.assertEqual(res['total']['numerator'], 0)

    def test_ambiguous_csv_old_and_new_both_paired_stays_ambiguous(self):
        sales = self._sales(self.CR_L, 8)
        text = ('seller_sku,account,warehouse_sku,qty\n'
                'SELLER,所有账户,' + self.CR_L + ',1\n'
                'SELLER,所有账户,' + self.CR_NL + ',1\n')
        res = self._run(sales, self._pair(), text)
        self.assertTrue(res['connected'])
        self.assertEqual(res['total']['numerator'], 0)


class G02CacheVersionTests(unittest.TestCase):
    def refresh(self, cache, quantities, connected, now=NOW, fail_sales=False):
        P = 'LAYJMZPG01CR-2L'
        R = 'LAYJMZPG01CR-2R'
        PAIR = {P: {'display_name': '双人', 'wf_part_number': 'W2'}}
        normalized = [row(P, quantities[0], 'xingshan_US'), row(R, quantities[1], 'xingshan_US')]
        aftersale = {'connected': connected, 'total': {'numerator': 1, 'denominator': min(quantities), 'rate': 1},
                     'source_as_of': 'test-old-source', 'sales_source_as_of': 'test-old-source'}
        with patch.object(bb.feishu, 'get_tenant_token', return_value='synthetic-token'), \
             patch.object(bb, 'fetch_fields', return_value={}), \
             patch.object(bb, 'fetch_records', side_effect=RuntimeError('read failed') if fail_sales else None, return_value=([], 1)), \
             patch.object(bb, 'fetch_pairing', return_value=(PAIR, len(PAIR), 1)), \
             patch.object(bb, 'normalize', return_value=(normalized, [])), \
             patch.object(al, 'load', return_value=aftersale):
            return bb.get_board('synthetic-app', 'synthetic-secret', refresh=True, now_fn=lambda: now, cache=cache)

    def test_cache_version_is_v8(self):
        self.assertEqual(bb.CACHE_VERSION, 'v8_g02_complete_packages')

    def test_old_v7_cache_rejected_under_aftersale_failure(self):
        cache = {}
        first = self.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        self.assertEqual(key[-1], 'v8_g02_complete_packages')
        old_key = key[:-1] + ('v7_8018_excluded_0458_packages',)
        old = copy.deepcopy(entry)
        old['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
        cache.clear()
        cache[old_key] = old
        fresh = self.refresh(cache, (50, 40), False)
        self.assertFalse(fresh['stale'])
        self.assertFalse(fresh['aftersale']['connected'])
        self.assertNotEqual(fresh['teams'], first['teams'])

    def test_old_v7_cache_rejected_under_sales_failure(self):
        cache = {}
        self.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        old_key = key[:-1] + ('v7_8018_excluded_0458_packages',)
        old = copy.deepcopy(entry)
        old['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
        cache.clear()
        cache[old_key] = old
        fresh = self.refresh(cache, (50, 40), True, fail_sales=True)
        self.assertFalse(fresh['ok'])
        self.assertFalse(fresh['stale'])
        self.assertNotIn('teams', fresh)


if __name__ == '__main__':
    unittest.main()
