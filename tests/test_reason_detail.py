"""Independent regression cases for additive keyword reason details (synthetic only)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import aftersale_live as live
from server.aftersale import _reason_category
from server.reason_detail import reason_subcategory

UNKNOWN = '其他（未细分）'


class KeywordDetailTests(unittest.TestCase):
    def check(self, category, cases):
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(reason_subcategory(category, text), expected)

    def test_explicit_damage_types(self):
        self.check('破损损坏', [
            ('The box was damaged', '包装破损'),
            ('包装破损', '包装破损'),
            ('The chair leg is cracked', '断裂开裂'),
            ('扶手断裂', '断裂开裂'),
            ('There are scratches and dents on the product', '划痕凹陷'),
            ('The fabric has a hole', '面料破洞'),
            ('There is a hole in the fabric', '面料破洞'),
            ('面料有破洞', '面料破洞'),
            ('Damaged in transit', '运输受损'),
            ('运输途中损坏', '运输受损'),
            ('The metal has rust', '生锈掉漆'),
            ('The frame is bent', '产品变形'),
        ])

    def test_generic_damage_never_guesses_subtype(self):
        self.check('破损损坏', [(t, UNKNOWN) for t in (
            'Product damaged', 'defective', 'poor quality', 'flimsy',
            'box', 'shipping', 'The box arrived. The product is damaged.',
            'I trust the whole product and feel confident', '')])

    def test_negated_and_conditional_damage(self):
        self.check('破损损坏', [(t, UNKNOWN) for t in (
            'The box is not damaged', 'No cracks', 'not broken',
            'not scratched', 'no holes in the fabric', '没有破洞',
            '未破损', '没有开裂', 'If damaged please contact us', '是否破损')])

    def test_other_explicit_complaints(self):
        self.check('其他问题', [
            ('Wrong color', '颜色色差'), ('颜色有色差', '颜色色差'),
            ('A strong odor', '异味'), ('有刺鼻臭味', '异味'),
            ('The cover is stained', '污渍脏污'), ('有污渍', '污渍脏污'),
            ('This is not comfortable', '舒适度'), ('坐着不舒服', '舒适度'),
            ('Fabric has a hole', '面料破洞'),
            ('They sent the wrong item', '发错商品'),
        ])

    def test_other_neutral_or_negated_is_unknown(self):
        self.check('其他问题', [(t, UNKNOWN) for t in (
            'Thank you. Please contact us.', 'color blue', 'comfortable',
            'odor', 'The whole fabric looks good', 'no stains', 'not dirty',
            'no bad smell', '面料有弹性')])

    def test_existing_category_names_are_used(self):
        self.check('尺寸规格', [('too small', '尺寸偏小'), ('too large', '尺寸偏大')])
        self.check('缺件漏发', [('missing screws', '缺少配件')])
        self.check('功能故障', [('does not work', '无法工作'), ('wobbly', '松动不稳')])
        self.check('物流配送', [('delivery delayed', '配送延迟')])
        for category in ('未注明原因', 'unknown'):
            self.assertEqual(reason_subcategory(category, 'cracked'), UNKNOWN)

    def test_one_label_for_overlapping_signals(self):
        result = reason_subcategory('破损损坏', 'cracked and scratched in transit')
        self.assertIsInstance(result, str)
        self.assertEqual(result, '断裂开裂')
        self.assertEqual(reason_subcategory('破损损坏', 'the box was damaged and product cracked'), '包装破损')

    def test_packaging_mention_and_policy_text_do_not_infer_damage_or_requests(self):
        self.check('破损损坏', [('The box is intact but product damaged', UNKNOWN)])
        self.check('要求退货', [('refund policy', UNKNOWN), ('退货政策', UNKNOWN),
                              ('不需要退款', UNKNOWN), ('I want a refund', '退款诉求')])
        self.check('要求换货', [('replacement instructions', UNKNOWN), ('换货说明', UNKNOWN),
                              ('请换货', '换货诉求')])

    def test_does_not_change_or_reclassify_primary(self):
        before = _reason_category('Fabric has a hole')
        self.assertEqual(before, '其他问题')
        self.assertEqual(reason_subcategory(before, 'Fabric has a hole'), '面料破洞')
        self.assertEqual(_reason_category('Fabric has a hole'), before)


class AggregateDetailTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mapping = Path(tmp.name) / 'mapping.csv'
        self.mapping.write_text('seller_sku,account,warehouse_sku,qty\nSELLER,xingshan_US,S,1\n', encoding='utf-8')
        self.pair = {'S': {'display_name': 'Chair'}, 'T': {'display_name': 'Desk'},
                     'T01': {'display_name': 'T01 Chair'}}
        self.sales = {'ok': True, 'teams': [
            {'name': name, 'shops': [
                {'account': account, 'skus': [
                    {'sku': sku, 'sold': {'2026-04': 10, '2026-05': 0, '2026-06': 5}}
                    for sku in self.pair]}
                for account in accounts]}
            for name, accounts in live.TEAM_MEMBERS]}
        self.rows = {live.AFTERSALE_TABLE_1: [], live.AFTERSALE_TABLE_2: []}

    def add(self, text, account='xingshan_US', sku='S', date='2026-05-15'):
        self.rows[live.AFTERSALE_TABLE_2].append({
            'record_id': 'PRIVATE-SYNTHETIC-ID-%s' % len(self.rows[live.AFTERSALE_TABLE_2]),
            'fields': {'SKU': '', '易仓sku': sku, '原文': text, '日期': date, '店铺账号': account}})

    def result(self, module=live):
        def fields(token, app, table, http=None, required_fields=None):
            return {n: {'field_name': n, 'type': t} for n, t in required_fields.items()}
        with patch.object(module, 'fetch_fields', fields), patch.object(
                module, 'fetch_records', lambda token, app, table, http=None: (self.rows[table], 1)):
            return module.load('synthetic-token', 'synthetic-app', self.pair, self.sales,
                               mapping_path=self.mapping, fetched_at='FIXED-TEST-TIME')

    def test_existing_primary_counts_rates_and_exclusions(self):
        for text in ('box was damaged', 'cracked', 'scratched', 'fabric has a hole',
                     'too small', 'missing screws', 'not working', 'bad smell',
                     'wrong color', 'unmatched synthetic wording', '', 'return please'):
            self.add(text)
        self.add('broken', sku='T01')
        self.add('broken', account='unknown')
        self.add('broken', date='5.15')
        self.add('broken', sku='UNPAIRED')
        out = self.result()
        self.assertTrue(out['connected'])
        self.assertEqual(out['summary'], {'numerator': 12, 'denominator': 240, 'rate': 5.0})
        expected = {'破损损坏': 3, '其他问题': 4, '尺寸规格': 1, '缺件漏发': 1,
                    '功能故障': 1, '未注明原因': 1, '要求退货': 1}
        self.assertEqual({r['category']: r['count'] for r in out['reasons']['items']}, expected)
        self.assertTrue(all(r['month'] == '2026-04' for r in out['reasons']['items']))

    def test_children_partition_parents_and_share_uses_parent(self):
        for text in ('box was damaged', 'cracked', 'product damaged', 'wrong color'):
            self.add(text)
        self.add('bad smell', sku='T', date='2026-06-15')
        out = self.result()
        self.assertEqual(out['summary']['numerator'], 5)
        for parent in out['reasons']['items']:
            keys = ('account', 'sku', 'month', 'category')
            children = [c for c in out['reasons']['subcategory_items']
                        if all(c[k] == parent[k] for k in keys)]
            self.assertEqual(sum(c['count'] for c in children), parent['count'])
            for child in children:
                self.assertEqual(child['share'], round(child['count'] * 100 / parent['count'], 4))
        self.assertTrue(any(c['subcategory'] == UNKNOWN for c in out['reasons']['subcategory_items']))

    def test_month_shift_and_scope_use_existing_assignment(self):
        self.add('box was damaged')  # May is explicit zero -> April
        self.add('cracked', sku='T', date='2026-06-15')
        self.add('scratched', account='Wekas_US', date='2026-06-15')
        items = self.result()['reasons']['subcategory_items']
        self.assertTrue(any(c['sku'] == 'S' and c['account'] == 'xingshan_US' and c['month'] == '2026-04' for c in items))
        self.assertFalse(any(c['month'] == '2026-05' for c in items))
        self.assertEqual(sum(c['count'] for c in items), 3)

    def test_aggregate_response_never_exposes_source_details(self):
        self.add('wrong color PRIVATE-CUSTOMER-EMAIL-AND-ORDER')
        out = self.result()
        raw = json.dumps(out, ensure_ascii=False)
        self.assertNotIn('PRIVATE-', raw)
        for item in out['reasons']['subcategory_items']:
            self.assertEqual(set(item), {'account', 'sku', 'month', 'category', 'subcategory', 'count', 'share'})

    def test_no_records_and_failure_are_distinct(self):
        out = self.result()
        self.assertTrue(out['connected'])
        self.assertEqual(out['reasons']['subcategory_items'], [])
        self.sales = {'ok': True, 'teams': []}
        out = self.result()
        self.assertFalse(out['connected'])
        self.assertIsNone(out['reasons']['subcategory_items'])


if __name__ == '__main__':
    unittest.main()
