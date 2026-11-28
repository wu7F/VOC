"""Final narrow rules: 8018 exclusion, G02 ignored components, 0458 two-seat packages."""
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


class Raw8018ExclusionTests(unittest.TestCase):
    def test_raw_sku_and_alias_8018_excluded_but_boundaries_kept(self):
        rows = [row('LAYJMZP8018CR-2L', 5), row('LAYJCR8018-2R', 5),
                row('18018', 3), row('80180', 4)]
        s = build(rows, pair_map=None)
        skus = {x['sku'] for x in shop(s)['skus']}
        self.assertNotIn('LAYJMZP8018CR-2L', skus)
        self.assertNotIn('LAYJCR8018-2R', skus)
        self.assertIn('18018', skus)
        self.assertIn('80180', skus)
        self.assertEqual(shop(s)['months']['2026-04'], 7)

    def test_8018_excluded_from_products_and_forced_zero(self):
        pm = {'LAYJMZP8018CR-2L': {'display_name': '8018双人', 'wf_part_number': 'W'},
              'PLAIN': {'display_name': '普通', 'wf_part_number': 'WP'}}
        s = build([row('PLAIN', 4)], pair_map=pm,
                  confirmed_zero_skus={'LAYJMZP8018CR-2L'})
        self.assertNotIn('LAYJMZP8018CR-2L', {p['sku'] for p in s['products']})
        self.assertNotIn('LAYJMZP8018CR-2L', {x['sku'] for x in shop(s)['skus']})

    def test_authoritative_pair_display_8018_excluded_but_g01_shop_label_kept(self):
        pm = {'OTHER': {'display_name': '8018系列', 'wf_part_number': 'W'},
              'LAYJMZPG01CR-2L': {'display_name': 'G01双人', 'wf_part_number': 'W'},
              'LAYJMZPG01CR-2R': {'display_name': 'G01双人', 'wf_part_number': 'W'}}
        l = row('LAYJMZPG01CR-2L', 6)
        l['display_name'] = '8018店'
        s = build([row('OTHER', 5), l, row('LAYJMZPG01CR-2R', 4)], pair_map=pm)
        skus = {x['sku'] for x in shop(s)['skus']}
        self.assertNotIn('OTHER', skus)
        self.assertEqual(sold(s, 'LAYJMZPG01CR-2L'), 4)
        self.assertEqual(s['unpaired']['total_units'], 0)


class G02IgnoredComponentTests(unittest.TestCase):
    def test_base_single_item_remains_despite_ignored_components(self):
        base = 'LAYJMZPG02CR'
        pm = pair(base)
        rows = [row(base, 5), row('LAYJMZPG02CR-BACKREST', 999),
                row('LAYJMZPG02CR-AR', 1), row('LAYJMZPG02CR-2AR', 1),
                row('LAYJMZPG02CR-3AR', 1), row('LAYJMZPG02CR-BACKREST', None)]
        s = build(rows, pair_map=pm)
        self.assertEqual(sold(s, base), 5)
        self.assertEqual(s['unpaired']['total_units'], 0)
        self.assertEqual(s['teams'][0]['unpaired_skus'], [])

    def test_ignored_only_with_no_pair_map_produces_no_output(self):
        s = build([row('LAYJMZPG02CR-BACKREST', 9)], pair_map=None)
        self.assertEqual(shop(s)['skus'], [])
        self.assertEqual(s['unpaired']['total_units'], 0)

    def test_helper_direct_and_empty_active_targets(self):
        self.assertTrue(cs.is_ignored_g02_component('LAYJMZPG02CR-AR'))
        self.assertTrue(cs.is_ignored_g02_component('LAYJMZPG02DG-3AR'))
        self.assertFalse(cs.is_ignored_g02_component('LAYJMZPG02CR-2L'))
        self.assertFalse(cs.is_ignored_g02_component('LAYJMZP202641DBR-AR'))
        out = cs.merge_component_rows([row('LAYJMZPG02CR-BACKREST', 9)], None, [])
        self.assertEqual(out, [])
        pair_map = {}
        out2 = cs.merge_component_rows([row('LAYJMZPG02CR-BACKREST', 9)], pair_map, [])
        self.assertEqual(out2, [])

    def test_202641_ar_requirement_unaffected(self):
        t = 'LAYJMZP202641DBR-2LT'
        pm = pair(t)
        s = build([row(t, 5), row('LAYJMZP202641DBR-2RT', 4)], pair_map=pm)
        self.assertEqual(sold(s, t), 0)
        full = build([row(t, 5), row('LAYJMZP202641DBR-2RT', 4),
                      row('LAYJMZP202641DBR-AR', 3)], pair_map=pm)
        self.assertEqual(sold(full, t), 3)


class G02TwoBoxStrictTests(unittest.TestCase):
    CR_L = 'LAYJMZPG02CR-2L'
    CR_R = 'LAYJMZPG02CR-2R'
    DG_L = 'LAYJMZPG02DG-2L'
    DG_R = 'LAYJMZPG02DG-2R'

    def pm(self):
        return {self.CR_L: {'display_name': 'CR', 'wf_part_number': 'WCR'},
                self.DG_L: {'display_name': 'DG', 'wf_part_number': 'WDG'}}

    def test_complete_each_color_independent_and_never_cross_fill(self):
        cr = build([row(self.CR_L, 10), row(self.CR_R, 8)], pair_map=self.pm())
        self.assertEqual(sold(cr, self.CR_L), 8)
        self.assertNotIn(self.DG_L, {x['sku'] for x in shop(cr)['skus']})
        dg = build([row(self.DG_L, 7), row(self.DG_R, 6)], pair_map=self.pm())
        self.assertEqual(sold(dg, self.DG_L), 6)
        self.assertNotIn(self.CR_L, {x['sku'] for x in shop(dg)['skus']})
        both = build([row(self.CR_L, 10), row(self.CR_R, 8),
                      row(self.DG_L, 7), row(self.DG_R, 6)], pair_map=self.pm())
        self.assertEqual(sold(both, self.CR_L), 8)
        self.assertEqual(sold(both, self.DG_L), 6)

    def test_cross_color_parts_cannot_complete_either(self):
        s = build([row(self.CR_L, 10), row(self.DG_R, 8)], pair_map=self.pm())
        self.assertEqual(sold(s, self.CR_L), 0)
        self.assertEqual(sold(s, self.DG_L), 0)

    def test_foreign_aliases_do_not_rescue_or_create(self):
        with self.subTest('CR L + ignored 2RIGHT'):
            s = build([row(self.CR_L, 10), row('LAYJMZPG02CR-2RIGHT', 0)], pair_map=self.pm())
            self.assertEqual(sold(s, self.CR_L), 0)
        with self.subTest('CR L/R + ignored 2RIGHT duplicate'):
            s = build([row(self.CR_L, 10), row(self.CR_R, 8),
                       row('LAYJMZPG02CR-2RIGHT', 0)], pair_map=self.pm())
            self.assertEqual(sold(s, self.CR_L), 8)
        with self.subTest('DG only foreign LT/MT/RT no target row'):
            s = build([row('LAYJMZPG02DG-2LT', 5), row('LAYJMZPG02DG-2MT', 5),
                       row('LAYJMZPG02DG-2RT', 5)], pair_map=self.pm())
            self.assertNotIn(self.DG_L, {x['sku'] for x in shop(s)['skus']})
        with self.subTest('DG full pair unaffected by foreign invalid'):
            s = build([row(self.DG_L, 7), row(self.DG_R, 6),
                       row('LAYJMZPG02DG-2LT', None)], pair_map=self.pm())
            self.assertEqual(sold(s, self.DG_L), 6)

    def test_duplicate_required_row_is_unknown(self):
        s = build([row(self.CR_L, 10), row(self.CR_R, 8), row(self.CR_R, 8)], pair_map=self.pm())
        self.assertIsNone(sold(s, self.CR_L))
        self.assertTrue(any(d['code'] == 'duplicate_key' for d in s['diagnostics']))

    def test_each_color_all_absent_creates_no_target(self):
        s = build([row('PLAIN', 4)], pair_map=dict(self.pm(), PLAIN=pair('PLAIN')['PLAIN']))
        skus = {x['sku'] for x in shop(s)['skus']}
        self.assertNotIn(self.CR_L, skus)
        self.assertNotIn(self.DG_L, skus)


class TwoSeat0458Tests(unittest.TestCase):
    T = 'LAYJMZP240458LG-2L'
    OLD_R = 'LAYJMZP240458LG-2R'
    NEW_LT = 'LAYJMZP0458LG-2LT'
    NEW_RT = 'LAYJMZP0458LG-2RT'
    NEW_KB = 'LAYJMZP0458LG-2KB'

    def pm(self):
        return pair(self.T)

    def test_complete_old_and_new_sum(self):
        s = build([row(self.T, 10), row(self.OLD_R, 8),
                   row(self.NEW_LT, 7), row(self.NEW_RT, 6), row(self.NEW_KB, 5)],
                  pair_map=self.pm())
        self.assertEqual(sold(s, self.T), 13)

    def test_old_0458_explicit_zero(self):
        s = build([row(self.T, 10), row(self.OLD_R, 0)], pair_map=self.pm())
        self.assertEqual(sold(s, self.T), 0)

    def test_only_old_or_only_new(self):
        old = build([row(self.T, 10), row(self.OLD_R, 8)], pair_map=self.pm())
        self.assertEqual(sold(old, self.T), 8)
        new = build([row(self.NEW_LT, 7), row(self.NEW_RT, 6), row(self.NEW_KB, 5)],
                    pair_map=self.pm())
        self.assertEqual(sold(new, self.T), 5)

    def test_incomplete_package_contributes_zero(self):
        old_bad = build([row(self.T, 10), row(self.NEW_LT, 7), row(self.NEW_RT, 6),
                         row(self.NEW_KB, 5)], pair_map=self.pm())
        self.assertEqual(sold(old_bad, self.T), 5)
        new_bad = build([row(self.T, 10), row(self.OLD_R, 8), row(self.NEW_LT, 7)],
                        pair_map=self.pm())
        self.assertEqual(sold(new_bad, self.T), 8)

    def test_invalid_or_duplicate_makes_unknown_and_absent_no_target(self):
        bad = build([row(self.T, 10), row(self.OLD_R, None)], pair_map=self.pm())
        self.assertIsNone(sold(bad, self.T))
        dup = build([row(self.T, 10), row(self.OLD_R, 8), row(self.OLD_R, 8)],
                    pair_map=self.pm())
        self.assertIsNone(sold(dup, self.T))
        absent = build([row('PLAIN', 4)], pair_map=dict(self.pm(), PLAIN=pair('PLAIN')['PLAIN']))
        self.assertNotIn(self.T, {x['sku'] for x in shop(absent)['skus']})


class Aftersale8018ExclusionTests(unittest.TestCase):
    def _sales(self, sku, qty):
        shops = []
        for team, info in bb.TEAM_TABLES.items():
            shops.append({'name': team, 'shops': [
                {'account': a, 'skus': ([{'sku': sku, 'sold': {'2026-04': qty}}]
                                        if a == 'xingshan_US' else [])}
                for a in info['accounts']]})
        return {'ok': True, 'teams': shops}

    def _run(self, sales, pair_map, mapping_text, direct=False):
        fields = {'SKU': 'SELLER', '易仓sku': '', '原文': 'damage', '日期': '2026-04-10',
                  '店铺名称': '行善'}
        if direct:
            fields['易仓sku'] = 'LAYJMZP8018CR-2L'
        rows = {al.AFTERSALE_TABLE_1: [{'record_id': 'r1', 'fields': fields}],
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

    def test_direct_and_relation_8018_excluded_even_with_stale_snapshot(self):
        pm = {'LAYJMZP8018CR-2L': {'display_name': '8018', 'wf_part_number': 'W'}}
        sales = self._sales('LAYJMZP8018CR-2L', 5)
        direct = self._run(sales, pm, 'seller_sku,account,warehouse_sku,qty\n', direct=True)
        self.assertTrue(direct['connected'])
        self.assertEqual(direct['total']['denominator'], 0)
        self.assertEqual(direct['total']['numerator'], 0)
        self.assertEqual(direct['reasons']['items'], [])
        rel = self._run(sales, pm, 'seller_sku,account,warehouse_sku,qty\nSELLER,所有账户,LAYJMZP8018CR-2L,1\n')
        self.assertTrue(rel['connected'])
        self.assertEqual(rel['total']['denominator'], 0)
        self.assertEqual(rel['total']['numerator'], 0)
        self.assertEqual(rel['reasons']['items'], [])


class AliasAndCacheTests(unittest.TestCase):
    def test_g02_alias_targets_unchanged(self):
        expected = {
            'LAYJMZPG02DG-2LT': 'LAYJMZPG02DG-2L',
            'LAYJMZPG02DG-2RT': 'LAYJMZPG02DG-2L',
            'LAYJMZPG02DG-3MT': 'LAYJMZPG02DG-3L',
            'LAYJMZPG02DG-CJ': 'LAYJMZPG02DG-LT',
            'LAYJMZPG02DG-ME1': 'LAYJMZPG02DG-LT',
            'LAYJMZPG02DG-ME2': 'LAYJMZPG02DG-LT',
            'LAYJMZPG02DG-RT': 'LAYJMZPG02DG-LT',
            'LAYJMZPG02DG-ZJDZ': 'LAYJMZPG02DG-LT',
        }
        for k, v in expected.items():
            self.assertEqual(cs.COMPONENT_ALIASES[k], v)

    def test_v6_cache_cannot_serve_v7(self):
        self.assertEqual(bb.CACHE_VERSION, 'v7_8018_excluded_0458_packages')
        self.assertNotEqual(bb.CACHE_VERSION, 'v6_component_required_202641')

    def test_old_v6_cache_entry_is_not_served_as_fresh(self):
        from tests import test_component_sales as tcs
        helper = tcs.ComponentCacheTests('test_aftersale_failure_returns_whole_coherent_minimum_policy_snapshot')
        cache = {}
        helper.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        self.assertEqual(key[-1], 'v7_8018_excluded_0458_packages')
        old_key = key[:-1] + ('v6_component_required_202641',)
        old = copy.deepcopy(entry)
        old['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
        cache.clear()
        cache[old_key] = old
        fresh = helper.refresh(cache, (50, 40), False)
        self.assertFalse(fresh['stale'])
        self.assertFalse(fresh['aftersale']['connected'])
        self.assertEqual(tcs.sold(fresh, account='xingshan_US'), 40)
        self.assertNotEqual(fresh['teams'], entry['snapshot']['teams'])

    def test_old_v6_cache_entry_not_served_under_sales_failure(self):
        from tests import test_component_sales as tcs
        helper = tcs.ComponentCacheTests('test_aftersale_failure_returns_whole_coherent_minimum_policy_snapshot')
        cache = {}
        helper.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        old_key = key[:-1] + ('v6_component_required_202641',)
        old = copy.deepcopy(entry)
        old['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
        cache.clear()
        cache[old_key] = old
        fresh = helper.refresh(cache, (50, 40), True, fail_sales=True)
        self.assertFalse(fresh['ok'])
        self.assertFalse(fresh['stale'])
        self.assertNotIn('teams', fresh)


if __name__ == '__main__':
    unittest.main()
