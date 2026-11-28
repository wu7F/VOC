# -*- coding: utf-8 -*-
"""Amazon 原因字段切换（2026-10）后端行为测试。"""
import json
import unittest

from server import aftersale_live as al
import tests.test_aftersale_live as aftersale_live_module


class AmazonReasonFieldTests(unittest.TestCase):
    def setUp(self):
        self._live = aftersale_live_module.LiveAfterSaleTests('test_same_id_in_separate_tables_is_allowed')
        self._live.setUp()
        self.addCleanup(self._live.doCleanups)
        self.rows = self._live.rows
        self.pair = self._live.pair
        self.sales = self._live.sales
        self.map_path = self._live.map_path

    def run_result(self):
        return self._live.run_result()

    def _rec(self, rid, sku, date, text=None, new=None, shop='行善'):
        fields = {'SKU': sku, '易仓sku': '', '原文': text or '', '日期': date, '店铺名称': shop}
        if new is not None:
            fields[al.REASON_FIELD_NAME] = new
        return {'record_id': rid, 'fields': fields}

    def _field_items(self, r):
        return r['reasons']['field_items']

    def test_sept30_uses_raw_category_and_ignores_new_field(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('a', 'SELLER-A', '2026-09-30', text='Product arrived damaged', new='包装破损')]
        r = self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['reasons']['items'][0]['category'], '破损损坏')
        self.assertEqual(self._field_items(r), [])
        self.assertEqual(r['reasons']['field_record_count'], 0)
        self.assertEqual(r['reasons']['legacy_record_count'], 1)

    def test_oct1_unknown_label_not_reclassified_despite_raw_text(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('b', 'SELLER-A', '2026-10-01', text='Product arrived damaged', new='神秘标签')]
        r = self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['reasons']['items'], [])
        items = self._field_items(r)
        self.assertEqual([(x['category'], x['count']) for x in items], [('神秘标签', 1)])
        self.assertEqual(r['reasons']['field_record_count'], 1)

    def test_october_source_backshift_to_june_keeps_flat_policy(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('c', 'SELLER-A', '2026-10-05', text='damage', new='包装破损')]
        r = self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['reasons']['items'], [])
        items = self._field_items(r)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['month'], '2026-06')
        self.assertEqual(items[0]['category'], '包装破损')

    def test_single_oct_row_two_labels_flat_counts_and_share(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('d', 'SELLER-A', '2026-10-10', text='x', new='包装破损,铁架变形')]
        r = self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['total']['numerator'], 1)
        items = {x['category']: x for x in self._field_items(r)}
        self.assertEqual(set(items), {'包装破损', '铁架变形'})
        for x in items.values():
            self.assertEqual(x['count'], 1)
            self.assertEqual(x['share'], 50)
        self.assertEqual(r['total']['denominator'], 18)
        self.assertEqual(r['total']['rate'], round(100 / 18, 4))

    def test_mixed_legacy_and_new_same_assigned_month_stay_separate(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('e1', 'SELLER-A', '2026-09-20', text='Product arrived damaged'),
            self._rec('e2', 'SELLER-A', '2026-10-02', text='damage', new='破损损坏')]
        r = self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['total']['numerator'], 2)
        legacy = r['reasons']['items']
        flat = self._field_items(r)
        self.assertEqual([(x['category'], x['count']) for x in legacy], [('破损损坏', 1)])
        self.assertEqual([(x['category'], x['count']) for x in flat], [('破损损坏', 1)])
        self.assertEqual(legacy[0]['month'], flat[0]['month'])

    def test_parentheses_kept_single_label_and_slash_preserved(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('f', 'SELLER-A', '2026-10-03', text='x', new='使用不安全（倾倒，塌陷）')]
        r = self.run_result()
        self.assertEqual([x['category'] for x in self._field_items(r)], ['使用不安全（倾倒，塌陷）'])
        self.assertEqual(al._split_reason_field('退货/换货'), ['退货/换货'])

    def test_split_only_top_level_separators(self):
        self.assertEqual(al._split_reason_field('a;b\nc|d，e'), ['a', 'b', 'c', 'd', 'e'])
        self.assertEqual(al._split_reason_field('a（b;c）d'), ['a（b;c）d'])
        self.assertEqual(al._split_reason_field('a(b;c)d'), ['a(b;c)d'])
        self.assertEqual(al._split_reason_field('a[b;c]d'), ['a[b;c]d'])
        self.assertEqual(al._split_reason_field('a【b;c】d'), ['a【b;c】d'])
        self.assertEqual(al._split_reason_field('a（b）[c]【d】(e)'), ['a（b）[c]【d】(e)'])
        self.assertEqual(al._split_reason_field('退货/换货'), ['退货/换货'])

    def test_duplicate_trimmed_label_once_and_unknown_punctuation_kept(self):
        self.assertEqual(al._split_reason_field(' 破损 ,破损, 破损 '), ['破损'])
        self.assertEqual(al._split_reason_field('a?b'), ['a?b'])

    def test_empty_missing_numeric_list_become_unknown(self):
        for value in ('', '   ', None, 5, ['x']):
            with self.subTest(value=value):
                self.assertEqual(al._split_reason_field(value), [al.REASON_FIELD_UNKNOWN])
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('g', 'SELLER-A', '2026-10-04', text='x', new='')]
        r = self.run_result()
        self.assertEqual(r['total']['numerator'], 1)
        self.assertEqual([x['category'] for x in self._field_items(r)], [al.REASON_FIELD_UNKNOWN])
        for value in ('', None, 5, ['x']):
            with self.subTest(integration=value):
                self.rows[al.AFTERSALE_TABLE_1] = [
                    self._rec('g2', 'SELLER-A', '2026-10-04', text='x', new=value)]
                r2 = self.run_result()
                self.assertTrue(r2['connected'])
                self.assertEqual(r2['total']['numerator'], 1)
                self.assertEqual(r2['total']['denominator'], 18)
                items = self._field_items(r2)
                self.assertEqual([(x['category'], x['count']) for x in items], [(al.REASON_FIELD_UNKNOWN, 1)])
                self.assertEqual(r2['reasons']['items'], [])

    def test_yearless_date_excluded_with_same_diagnostic(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('h', 'SELLER-A', '10.05', text='x', new='包装破损')]
        r = self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['total']['numerator'], 0)
        self.assertTrue(any(x['code'] == 'missing_year' for x in r['diagnostics']))

    def test_new_field_required_metadata_missing_or_wrong_type_fails_safe(self):
        old = al.fetch_fields
        for meta in ({}, {al.REASON_FIELD_NAME: {'type': 2}}):
            with self.subTest(meta=meta):
                def fake(token, app, table, http=None, required_fields=None, _m=meta):
                    base = {n: {'type': 1} for n in required_fields}
                    if al.REASON_FIELD_NAME in _m:
                        base[al.REASON_FIELD_NAME] = _m[al.REASON_FIELD_NAME]
                    else:
                        base.pop(al.REASON_FIELD_NAME, None)
                    return base
                al.fetch_fields = fake
                try:
                    r = self.run_result()
                    self.assertFalse(r['connected'])
                    self.assertIsNone(r['summary']['numerator'])
                finally:
                    al.fetch_fields = old

    def test_no_raw_complaint_or_record_ids_in_result(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('secret-id-1', 'SELLER-A', '2026-10-06', text='private-raw-text', new='包装破损')]
        r = self.run_result()
        blob = json.dumps(r, ensure_ascii=False)
        self.assertNotIn('private-raw-text', blob)
        self.assertNotIn('secret-id-1', blob)

    def test_actual_source_filters_unchanged(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
            self._rec('i1', 'SELLER-T01', '2026-10-07', text='x', new='包装破损'),
            self._rec('i2', 'SELLER-A', '2026-10-08', text='x', new='包装破损', shop='未知店铺')]
        r = self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['total']['numerator'], 0)
        codes = {x['code'] for x in r['diagnostics']}
        self.assertIn('t01_excluded', codes)
        self.assertIn('unknown_shop', codes)


if __name__ == '__main__':
    unittest.main()
