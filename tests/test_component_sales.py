"""Independent examples and boundary checks for approved component minimum sales."""
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import bitable_board as bb
from server import aftersale_live as al

P = 'LAYJMZPG01CR-2L'
R = 'LAYJMZPG01CR-2R'
P3 = 'LAYJMZPG01CR-3L'
M3 = 'LAYJMZPG01CR-3M'
R3 = 'LAYJMZPG01CR-3R'
TEAMS = {'Team 1': {'accounts': ['A', 'B']}, 'Team 2': {'accounts': ['C']},
         'Team 3': {'accounts': ['D']}}
PAIR = {P: {'display_name': '双人产品', 'wf_part_number': 'W2'},
        P3: {'display_name': '三人产品', 'wf_part_number': 'W3'},
        'PLAIN': {'display_name': '普通产品', 'wf_part_number': 'WP'}}
NOW = datetime(2026, 11, 24, tzinfo=bb.UTC8)


def row(sku, qty, account='A', team='Team 1', month='2026-04', platform='Amazon'):
    return {'sku': sku, 'qty': qty, 'invalid': qty is None, 'account': account,
            'display_name': account, '_source_team': team, 'month': month, 'platform': platform}


def build(rows, **kwargs):
    params = dict(now=NOW, source_meta={}, teams=TEAMS, pair_map=PAIR,
                  aftersale_summary={'connected': False})
    params.update(kwargs)
    return bb.build_snapshot(rows, [], **params)


def shop(snap, account='A'):
    return next(s for t in snap['teams'] for s in t['shops'] if s['account'] == account)


def sold(snap, sku=P, month='2026-04', account='A'):
    return next(s for s in shop(snap, account)['skus'] if s['sku'] == sku)['sold'][month]


class ComponentSalesTests(unittest.TestCase):
    def test_two_and_three_component_examples_use_min_not_sum_or_max(self):
        s = build([row(P, 316), row(R, 310), row(P3, 94), row(M3, 94), row(R3, 92)])
        self.assertEqual(sold(s), 310)
        self.assertEqual(sold(s, P3), 92)
        self.assertEqual(shop(s)['months']['2026-04'], 402)
        self.assertEqual(shop(s)['units_known'], 402)

    def test_merge_grain_is_shop_product_month_and_team(self):
        s = build([row(P, 10), row(R, 8), row(P, 6, month='2026-05'), row(R, 4, month='2026-05'),
                   row(R, 9, account='B'), row(P, 11, account='B'),
                   row(P, 3, account='C', team='Team 2'), row(R, 4, account='C', team='Team 2')])
        self.assertEqual(sold(s), 8)
        self.assertEqual(sold(s, month='2026-05'), 4)
        self.assertEqual(sold(s, account='B'), 9)
        self.assertEqual(sold(s, account='C'), 3)

    def test_missing_required_component_yields_zero_but_observed_zero_is_min(self):
        self.assertEqual(sold(build([row(R, 5)])), 0)
        self.assertEqual(sold(build([row(P, 5)])), 0)
        self.assertEqual(sold(build([row(P, 5), row(R, 0)])), 0)
        self.assertIsNone(sold(build([row(R, 5)]), month='2026-05'))

    def test_missing_required_third_component_yields_zero(self):
        s = build([row(P3, 94), row(M3, 94)])
        self.assertEqual(sold(s, P3), 0)
        self.assertEqual(shop(s)['months']['2026-04'], 0)
        self.assertEqual(shop(s)['units_known'], 0)

    def test_missing_required_seventh_component_yields_zero(self):
        target = 'LAYJHLR8005DG-LT'
        parts = ['LAYJHLR8005DG-LT', 'LAYJHLR8005DG-ME1', 'LAYJHLR8005DG-ZJKB',
                 'LAYJHLR8005DG-ZJDZ', 'LAYJHLR8005DG-ME2', 'LAYJHLR8005DG-RT']
        pair = {target: {'display_name': '七件套', 'wf_part_number': 'W7'}}
        s = build([row(p, 9) for p in parts], pair_map=pair)
        self.assertEqual(sold(s, target), 0)
        complete = build([row(p, 9) for p in parts] + [row('LAYJHLR8005DG-CJ', 7)], pair_map=pair)
        self.assertEqual(sold(complete, target), 7)

    def test_missing_and_invalid_precedence_keeps_unknown(self):
        s = build([row(P, None)])
        self.assertIsNone(sold(s))
        self.assertIsNone(shop(s)['months']['2026-04'])
        self.assertIn('2026-04', shop(s)['missing_months'])

    def test_complete_required_group_still_uses_min(self):
        s = build([row(P, 316), row(R, 310)])
        self.assertEqual(sold(s), 310)
        self.assertEqual(shop(s)['months']['2026-04'], 310)

    def test_entirely_absent_group_stays_absent(self):
        s = build([row('PLAIN', 4)])
        skus = {x['sku'] for x in shop(s)['skus']}
        self.assertNotIn(P, skus)
        self.assertNotIn(P3, skus)
        self.assertIn('PLAIN', skus)
        self.assertIsNone(shop(s)['months']['2026-05'])

    def test_g02_two_box_required_component_missing_yields_zero(self):
        g02 = 'LAYJMZPG02CR-2L'
        pair = {g02: {'display_name': 'G02', 'wf_part_number': 'WG'}}
        s = build([row('LAYJMZPG02CR-2R', 5)], pair_map=pair)
        self.assertEqual(sold(s, g02), 0)

    def test_observed_alias_overrides_canonical_confirmed_zero_backfill(self):
        s = build([row(R, 5)], confirmed_zero_skus={P})
        self.assertEqual(sold(s), 0)
        self.assertEqual(sold(s, month='2026-05'), 0)
        self.assertEqual(shop(s)['months']['2026-04'], 0)
        self.assertIsNone(shop(s)['months']['2026-05'])

    def test_observed_invalid_is_not_a_missing_component(self):
        s = build([row(P, 5), row(R, None), row('PLAIN', 4)])
        self.assertIsNone(sold(s))
        self.assertIsNone(shop(s)['months']['2026-04'])
        self.assertIn('2026-04', shop(s)['missing_months'])

    def test_duplicate_original_component_is_unknown_but_distinct_parts_are_not_duplicates(self):
        good = build([row(P, 5), row(R, 4)])
        self.assertEqual(sold(good), 4)
        self.assertFalse(any(d['code'] == 'duplicate_key' for d in good['diagnostics']))
        bad = build([row(P, 5), row(R, 4), row(R, 4)])
        self.assertIsNone(sold(bad))
        self.assertIsNone(shop(bad)['months']['2026-04'])
        self.assertTrue(any(d['code'] == 'duplicate_key' for d in bad['diagnostics']))
        missing_dup = build([row(R, 4), row(R, 4)])
        self.assertIsNone(sold(missing_dup))
        self.assertTrue(any(d['code'] == 'duplicate_key' for d in missing_dup['diagnostics']))

    def test_unrelated_product_duplicate_policy_is_preserved(self):
        s = build([row('PLAIN', 2), row('PLAIN', 3)])
        self.assertIsNone(sold(s, 'PLAIN'))
        self.assertIsNone(shop(s)['months']['2026-04'])
        self.assertTrue(any(d['code'] == 'duplicate_key' for d in s['diagnostics']))

    def test_existing_admission_checks_run_before_minimum(self):
        s = build([row(P, 8), row(R, 6), row(R, 0, account='A', team='Team 2'),
                   row(R, 0, platform='Wayfair'), row(R, 0, month='2026-03'),
                   row(R, 0, month='2026-12'), row(R, 0, account='UNKNOWN')])
        self.assertEqual(sold(s), 6)
        self.assertEqual(s['teams'][0]['merged_sku_totals'][0]['units'], 6)

    def test_equivalent_account_spelling_keeps_one_product_month(self):
        s = build([row(P, 8, account=' a '), row(R, 6, account='A')])
        self.assertEqual(sold(s), 6)
        self.assertEqual(shop(s)['months']['2026-04'], 6)

    def test_only_fixed_aliases_with_current_paired_target_are_admitted(self):
        s = build([row(P, 10), row(R, 10), row(R + '-NEW2', 2)])
        self.assertEqual(sold(s), 10)
        self.assertIn(R + '-NEW2', s['teams'][0]['unpaired_skus'])
        missing_target = build([row(R, 3)], pair_map={'PLAIN': PAIR['PLAIN']})
        self.assertEqual(missing_target['teams'][0]['unpaired_skus'], [R])
        self.assertEqual(missing_target['teams'][0]['merged_sku_totals'], [])

    def test_no_pairing_retains_original_raw_sales_behavior(self):
        s = build([row(P, 10), row(R, 9)], pair_map=None)
        self.assertEqual(shop(s)['months']['2026-04'], 19)
        self.assertEqual({x['sku'] for x in shop(s)['skus']}, {P, R})

    def test_audit_raw_component_counts_do_not_feed_sales_and_inputs_do_not_mutate(self):
        rows = [row(P, 100), row(R, 20), row('UNPAIRED', 3)]
        before = copy.deepcopy(rows)
        s = build(rows)
        self.assertEqual(rows, before)
        self.assertEqual(shop(s)['units_known'], 20)
        self.assertEqual(s['unpaired']['total_units'], 3)
        audit = s['teams'][0]['merged_sku_totals'][0]
        self.assertEqual(audit, {'sku': R, 'target_sku': P, 'units': 20, 'known_qty': 20, 'has_unknown': False})
        self.assertNotIn(R, s['teams'][0]['unpaired_skus'])
        self.assertEqual(s['teams'][0]['unpaired_skus'], ['UNPAIRED'])


class Component202641Tests(unittest.TestCase):
    T = 'LAYJMZP202641DBR-2LT'
    RT = 'LAYJMZP202641DBR-2RT'
    AR = 'LAYJMZP202641DBR-AR'

    def pair(self):
        return {self.T: {'display_name': '202641双人', 'wf_part_number': 'W2641'}}

    def test_complete_southwest_like_group_uses_min(self):
        s = build([row(self.T, 2, 'southwest_US', month='2026-11'),
                   row(self.RT, 1, 'southwest_US', month='2026-11'),
                   row(self.AR, 1, 'southwest_US', month='2026-11')],
                  teams=bb.TEAM_TABLES, pair_map=self.pair())
        self.assertEqual(sold(s, self.T, month='2026-11', account='southwest_US'), 1)
        self.assertEqual(shop(s, 'southwest_US')['months']['2026-11'], 1)

    def test_each_required_part_independently_absent_yields_zero(self):
        for missing in (self.T, self.RT, self.AR):
            parts = [p for p in (self.T, self.RT, self.AR) if p != missing]
            s = build([row(p, 5) for p in parts], pair_map=self.pair())
            self.assertEqual(sold(s, self.T), 0, missing)
            self.assertEqual(shop(s)['months']['2026-04'], 0, missing)

    def test_missing_part_with_observed_invalid_is_unknown(self):
        s = build([row(self.T, 5), row(self.RT, None)], pair_map=self.pair())
        self.assertIsNone(sold(s, self.T))
        self.assertIsNone(shop(s)['months']['2026-04'])
        self.assertIn('2026-04', shop(s)['missing_months'])

    def test_missing_part_with_duplicate_is_unknown(self):
        s = build([row(self.T, 5), row(self.RT, 4), row(self.RT, 4)], pair_map=self.pair())
        self.assertIsNone(sold(s, self.T))
        self.assertTrue(any(d['code'] == 'duplicate_key' for d in s['diagnostics']))

    def test_entirely_absent_group_stays_absent(self):
        pair = dict(self.pair())
        pair['PLAIN'] = PAIR['PLAIN']
        s = build([row('PLAIN', 4)], pair_map=pair)
        skus = {x['sku'] for x in shop(s)['skus']}
        self.assertNotIn(self.T, skus)
        self.assertIn('PLAIN', skus)
        self.assertEqual(sold(s, 'PLAIN'), 4)
        self.assertIsNone(shop(s)['months']['2026-05'])


class ComponentAftersaleIntegrationTests(unittest.TestCase):
    def test_live_denominator_month_fallback_and_t01_use_the_merged_sales(self):
        pair = {P: PAIR[P], 'SRT01-BR': {'display_name': 'T01', 'wf_part_number': 'WT'}}
        s = build([row(P, 10, 'xingshan_US'), row(R, 8, 'xingshan_US'),
                   row(P, 5, 'xingshan_US', month='2026-05'), row(R, 0, 'xingshan_US', month='2026-05'),
                   row('SRT01-BR', 152, 'xingshan_US')], teams=bb.TEAM_TABLES, pair_map=pair)
        rows = {al.AFTERSALE_TABLE_1: [{'record_id': 'synthetic-private-id', 'fields': {
            'SKU': 'SELLER', '易仓sku': '', '原文': 'private synthetic damage', '日期': '2026-05-20', '店铺名称': '行善'}}],
                al.AFTERSALE_TABLE_2: []}
        metadata = {name: {} for name in al.REQUIRED_FIELDS + (al.SHOP_FIELD_1, al.SHOP_FIELD_2)}
        with tempfile.TemporaryDirectory() as folder:
            mapping = Path(folder) / 'relation.csv'
            mapping.write_text('seller_sku,account,warehouse_sku,qty\nSELLER,所有账户,' + P + ',1\n', encoding='utf-8')
            with patch.object(al, 'fetch_fields', return_value=metadata), patch.object(al, 'fetch_records', side_effect=lambda token, app, table, http=None: (rows[table], 1)):
                result = al.load('token', 'app', pair, s, mapping_path=mapping, fetched_at=s['fetched_at'])
        self.assertTrue(result['connected'])
        self.assertEqual(result['total']['denominator'], 8)
        self.assertEqual(result['total']['numerator'], 1)
        self.assertEqual(result['total']['rate'], 12.5)
        self.assertTrue(any(x['month'] == '2026-04' and x['numerator'] == 1 and x['denominator'] == 8 for x in result['sku_store_month']))
        self.assertFalse(any(x['sku'] == 'SRT01-BR' for x in result['sku_store_month']))
        self.assertEqual(result['reasons']['items'][0]['month'], '2026-04')
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(shop(s, 'xingshan_US')['units_known'], 160)  # Existing headline includes T01.


class ComponentCacheTests(unittest.TestCase):
    def refresh(self, cache, quantities, connected, now=NOW, fail_sales=False):
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

    def test_aftersale_failure_returns_whole_coherent_minimum_policy_snapshot(self):
        cache = {}
        old_now = datetime(2026, 11, 20, tzinfo=bb.UTC8)
        first = self.refresh(cache, (10, 8), True, old_now)
        saved_cache = copy.deepcopy(cache)
        fallback = self.refresh(cache, (50, 40), False)
        self.assertEqual(sold(fallback, account='xingshan_US'), 8)
        self.assertEqual(fallback['teams'], first['teams'])
        self.assertEqual(fallback['fetched_at'], first['fetched_at'])
        self.assertEqual(fallback['aftersale']['total']['denominator'], 8)
        self.assertEqual(fallback['aftersale']['sales_source_as_of'], first['aftersale']['sales_source_as_of'])
        self.assertTrue(fallback['stale'] and fallback['aftersale']['stale'])
        self.assertEqual(cache, saved_cache)

    def test_sales_read_failure_also_returns_last_minimum_policy_snapshot(self):
        cache = {}
        first = self.refresh(cache, (10, 8), True)
        fallback = self.refresh(cache, (50, 40), True, fail_sales=True)
        self.assertTrue(fallback['stale'])
        self.assertEqual(fallback['teams'], first['teams'])
        self.assertEqual(fallback['aftersale'], first['aftersale'])

    def test_legacy_policy_cache_cannot_supply_old_sales_or_denominators(self):
        cache = {}
        first = self.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        self.assertNotEqual(key[-1], 'v3_aftersale_live')
        legacy_key = key[:-1] + ('v3_aftersale_live',)
        legacy = copy.deepcopy(entry)
        legacy['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
        cache.clear()
        cache[legacy_key] = legacy
        fresh = self.refresh(cache, (50, 40), False)
        self.assertFalse(fresh['aftersale']['connected'])
        self.assertFalse(fresh['stale'])
        self.assertEqual(sold(fresh, account='xingshan_US'), 40)
        self.assertNotEqual(fresh['teams'], first['teams'])

    def test_v4_component_min_cache_cannot_supply_required_subset_policy(self):
        cache = {}
        first = self.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        self.assertEqual(key[-1], 'v8_g02_complete_packages')
        v4_key = key[:-1] + ('v4_component_min',)
        v4 = copy.deepcopy(entry)
        v4['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
        cache.clear()
        cache[v4_key] = v4
        fresh = self.refresh(cache, (50, 40), False)
        self.assertFalse(fresh['aftersale']['connected'])
        self.assertFalse(fresh['stale'])
        self.assertEqual(sold(fresh, account='xingshan_US'), 40)
        self.assertNotEqual(fresh['teams'], first['teams'])

    def test_v5_component_required_subset_cache_cannot_supply_202641_policy(self):
        cache = {}
        first = self.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        self.assertEqual(key[-1], 'v8_g02_complete_packages')
        v5_key = key[:-1] + ('v5_component_required_subset',)
        v5 = copy.deepcopy(entry)
        v5['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
        cache.clear()
        cache[v5_key] = v5
        fresh = self.refresh(cache, (50, 40), False)
        self.assertFalse(fresh['aftersale']['connected'])
        self.assertFalse(fresh['stale'])
        self.assertEqual(sold(fresh, account='xingshan_US'), 40)
        self.assertNotEqual(fresh['teams'], first['teams'])

    def test_v4_and_v5_caches_cannot_supply_202641_policy_under_sales_read_failure(self):
        cache = {}
        first = self.refresh(cache, (10, 8), True)
        key, entry = next(iter(cache.items()))
        self.assertEqual(key[-1], 'v8_g02_complete_packages')
        for old_version in ('v4_component_min', 'v5_component_required_subset'):
            old_key = key[:-1] + (old_version,)
            old = copy.deepcopy(entry)
            old['snapshot']['teams'][0]['shops'][0]['units_known'] = 123456
            cache.clear()
            cache[old_key] = old
            fresh = self.refresh(cache, (50, 40), True, fail_sales=True)
            self.assertFalse(fresh['ok'])
            self.assertFalse(fresh['stale'])
            self.assertNotIn('teams', fresh)
