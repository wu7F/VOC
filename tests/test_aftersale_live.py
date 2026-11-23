import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import aftersale_live as al
from server.aftersale import _reason_category

class LiveAfterSaleTests(unittest.TestCase):
    def setUp(self):
        self.old_fields, self.old_records = al.fetch_fields, al.fetch_records
        self.rows = {al.AFTERSALE_TABLE_1: [], al.AFTERSALE_TABLE_2: []}
        def fake_fields(token, app, table, http=None, required_fields=None):
            return {name: {'field_name': name, 'type': typ} for name, typ in required_fields.items()}
        def fake_records(token, app, table, http=None): return self.rows[table], 1
        al.fetch_fields, al.fetch_records = fake_fields, fake_records
        self.addCleanup(self._restore_fetch_functions)
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.map_path = Path(tempdir.name) / 'voc-map-test.csv'
        self.map_path.write_text('seller_sku,account,warehouse_sku,qty\nSELLER-A,xingshan_US,SKU-X,1\nSELLER-T01,xingshan_US,SRT01-BR,1\nSELLER-AMB,xingshan_US,SKU-X,1\nSELLER-AMB,xingshan_US,SKU-Y,1\n', encoding='utf-8')
        self.pair = {'SKU-X': {'sku':'SKU-X','display_name':'Chair'}, 'SKU-Y': {'sku':'SKU-Y','display_name':'Chair2'}, 'SRT01-BR': {'sku':'SRT01-BR','display_name':'T01 Chair'}}
        self.sales = {'ok':True,'teams':[{'name':'Team 1','shops':[{'account':'xingshan_US','skus':[{'sku':'SKU-X','sold':{'2026-04':10,'2026-05':0,'2026-06':5}}, {'sku':'SKU-Y','sold':{'2026-04':3}}, {'sku':'SRT01-BR','sold':{'2026-04':9}}]},{'account':'KornerHK_US','skus':[]},{'account':'southwest_US','skus':[]}]},{'name':'Team 2','shops':[{'account':'Wekas_US','skus':[]},{'account':'kaifengzhejie_US','skus':[]}]},{'name':'Team 3','shops':[{'account':'maoyitong_US','skus':[]},{'account':'AmazonMJSKY_US_US','skus':[]},{'account':'RUIBAO_US_US','skus':[]}]}]}
    def _restore_fetch_functions(self): al.fetch_fields, al.fetch_records = self.old_fields, self.old_records
    def run_result(self): return al.load('token','app',self.pair,self.sales,mapping_path=self.map_path,fetched_at='2026-11-20T19:00:00+08:00')
    def test_exact_mapping_t01_unknown_year_backshift_and_privacy(self):
        self.rows[al.AFTERSALE_TABLE_1] = [
          {'record_id':'synthetic-1','fields':{'SKU':'SELLER-A','易仓sku':'','原文':'破损-damage','日期':'2026-05-20','店铺名称':'行善'}},
          {'record_id':'synthetic-2','fields':{'SKU':'SELLER-T01','易仓sku':'','原文':'private-synthetic-t01','日期':'2026-04-20','店铺名称':'行善'}},
          {'record_id':'synthetic-3','fields':{'SKU':'SELLER-A','易仓sku':'','原文':'private-synthetic-no-year','日期':'5.21','店铺名称':'行善'}},
          {'record_id':'synthetic-4','fields':{'SKU':'SELLER-AMB','易仓sku':'','原文':'private-synthetic-ambiguous','日期':'2026-04-20','店铺名称':'行善'}},
          {'record_id':'synthetic-5','fields':{'SKU':'SELLER-A','易仓sku':'','原文':'private-synthetic-unknown','日期':'2026-06-01','店铺名称':'未知店铺'}},
        ]
        r=self.run_result(); serialized=json.dumps(r,ensure_ascii=False)
        self.assertTrue(r['connected']); self.assertNotIn('private-synthetic',serialized); self.assertNotIn('synthetic-1',serialized)
        self.assertEqual(r['total']['numerator'],1)
        self.assertTrue(any(x['code']=='missing_year' for x in r['diagnostics']))
        self.assertTrue(any(x['code']=='ambiguous_pair' for x in r['diagnostics']))
        self.assertTrue(any(x['code']=='unknown_shop' for x in r['diagnostics']))
        self.assertFalse(any(x['sku']=='SRT01-BR' for x in r['sku_store_month']))
        self.assertTrue(any(x['sku']=='SKU-X' and x['month']=='2026-04' and x['numerator']==1 for x in r['sku_store_month']))
        self.assertEqual(r['reasons']['items'][0]['category'],'破损损坏')
    def test_future_direct_warehouse_sku_and_reason_month(self):
        self.rows[al.AFTERSALE_TABLE_2]=[{'record_id':'synthetic-6','fields':{'SKU':'ignored','易仓sku':'SKU-X','原文':'synthetic broken','日期':'2026-06-15','店铺账号':'xingshan_US'}}]
        r=self.run_result(); self.assertTrue(r['connected']); self.assertEqual(r['total']['numerator'],1); self.assertEqual(r['reasons']['items'][0]['month'],'2026-06')
    def test_partial_source_failure_fails_closed(self):
        old=al.fetch_records
        def fail(token,app,table,http=None):
            if table==al.AFTERSALE_TABLE_2: raise RuntimeError('secret')
            return [],1
        al.fetch_records=fail
        try:
            r=self.run_result(); self.assertFalse(r['connected']); self.assertIsNone(r['summary']['numerator']); self.assertNotIn('secret',json.dumps(r))
        finally: al.fetch_records=old
    def test_duplicate_id_fails_closed(self):
        self.rows[al.AFTERSALE_TABLE_1]=[{'record_id':'same','fields':{}},{'record_id':'same','fields':{}}]
        r=self.run_result(); self.assertFalse(r['connected']); self.assertEqual(r['diagnostics'][0]['code'],'record_id_duplicate')
    def test_same_id_in_separate_tables_is_allowed(self):
        self.rows[al.AFTERSALE_TABLE_1]=[{'record_id':'same','fields':{}}]
        self.rows[al.AFTERSALE_TABLE_2]=[{'record_id':'same','fields':{}}]
        r=self.run_result(); self.assertTrue(r['connected'])
    def test_incomplete_sales_snapshot_fails_closed(self):
        self.sales={'ok':True,'teams':[]}
        r=self.run_result(); self.assertFalse(r['connected']); self.assertIsNone(r['summary']['numerator'])
    def test_all_accounts_fallback_only_when_unique_in_pair_map(self):
        self.map_path.write_text(
            'seller_sku,account,warehouse_sku,qty\n'
            'SELLER-GENERIC,所有账户,SKU-X,1\n'
            'SELLER-GENERIC-AMB,所有账户,SKU-X,1\n'
            'SELLER-GENERIC-AMB,所有账户,SKU-Y,1\n'
            'SELLER-SPECIFIC,xingshan_US,SKU-Y,1\n'
            'SELLER-SPECIFIC,所有账户,SKU-X,1\n'
            'SELLER-T01,所有账户,SRT01-BR,1\n', encoding='utf-8')
        self.rows[al.AFTERSALE_TABLE_1] = [
            {'record_id':'generic-1','fields':{'SKU':'SELLER-GENERIC','易仓sku':'','原文':'damage','日期':'2026-05-20','店铺名称':'行善'}},
            {'record_id':'generic-amb','fields':{'SKU':'SELLER-GENERIC-AMB','易仓sku':'','原文':'damage','日期':'2026-05-20','店铺名称':'行善'}},
            {'record_id':'specific-wins','fields':{'SKU':'SELLER-SPECIFIC','易仓sku':'','原文':'damage','日期':'2026-05-20','店铺名称':'行善'}},
        ]
        r=self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['total']['numerator'],2)
        self.assertTrue(any(x.get('sku')=='SKU-X' for x in r['sku_store_month']))
        diag = {x['code']: x for x in r['diagnostics']}
        self.assertEqual(diag['ambiguous_pair']['message'].rsplit('=',1)[-1], '1')
        self.assertTrue(any(x.get('sku')=='SKU-Y' for x in r['sku_store_month']))

    def test_generic_mapping_does_not_bypass_t01_exclusion(self):
        self.map_path.write_text(
            'seller_sku,account,warehouse_sku,qty\n'
            'SELLER-T01,所有账户,SRT01-BR,1\n', encoding='utf-8')
        self.rows[al.AFTERSALE_TABLE_1] = [
            {'record_id':'generic-t01','fields':{'SKU':'SELLER-T01','易仓sku':'','原文':'private-t01','日期':'2026-05-20','店铺名称':'行善'}}]
        r=self.run_result()
        self.assertTrue(r['connected'])
        self.assertEqual(r['total']['numerator'],0)
        self.assertTrue(any(x['code']=='t01_excluded' for x in r['diagnostics']))
    def test_original_text_reason_categories(self):
        cases = {
            'Product arrived damaged': '破损损坏',
            'Missing parts from the package': '缺件漏发',
            'The chair wobbles and does not close': '功能故障',
            'Too small for the room': '尺寸规格',
            'Late delivery by carrier': '物流配送',
            'Assembly instructions are unclear': '安装使用',
            'I want a refund': '要求退货',
            'Please send a replacement': '要求换货',
            '': '未注明原因',
            'unmatched synthetic wording': '其他问题',
        }
        for text, expected in cases.items():
            with self.subTest(text=expected):
                self.assertEqual(_reason_category(text), expected)
    def test_confirmed_shop_aliases_and_other_unmapped_labels(self):
        self.assertEqual(al._canonical_account('贸易通'), 'maoyitong_US')
        self.assertEqual(al._canonical_account('买几天'), 'AmazonMJSKY_US_US')
        for label in ('2', '数字标签', '东玻', '其他排除'):
            self.assertIsNone(al._canonical_account(label))
    def test_full_date_with_time_is_supported(self):
        self.assertEqual(al._parse_date_month('2026-06-15 00:00'), '2026-06')
        self.assertEqual(al._parse_date_month('2026/06/15 00:00'), '2026-06')
        self.assertIsNone(al._parse_date_month('6.15'))

if __name__=='__main__': unittest.main()
