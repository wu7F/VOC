"""End-to-end synthetic ERP -> remote worksheet -> monthly rate calculation."""
from pathlib import Path
import json
import re
import sys
import tempfile
import unittest
from urllib.parse import unquote
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import aftersale, eccang_sales as es, feishu, feishu_sales as fs


class SalesFlowTests(unittest.TestCase):
    def test_source_correction_rerun_updates_remote_and_rate_without_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'shop_account_map.json').write_text(
                json.dumps({'合成店': ['SYNTH_ACCOUNT']}), encoding='utf-8')
            fs.save_settings(root, url='https://example.feishu.cn/sheets/SYNTHETIC?sheet=one',
                             write_back=True)
            cells = [['店铺', 'SKU', '日期', '销量件数'],
                     ['合成零售后店', 'ZERO', '2026-10-01', 20]]
            grid = 20
            revision = 1
            orders = [{'order_code': 'SYNTH-1', 'date_create': '2026-10-20 00:00:00',
                       'product_count': 5, 'order_details': [{'sku': 'SKU1', 'qty': 5}]}]

            def provider(url, *, method='GET', body=None, **kwargs):
                nonlocal revision, grid
                if '/sheets/query' in url:
                    return {'data': {'sheets': [{'sheet_id': 'one', 'resource_type': 'sheet',
                           'grid_properties': {'row_count': grid, 'column_count': 4}}]}}
                if method == 'GET' and '/values/' in url:
                    match = re.search(r'!A(\d+):D(\d+)', unquote(url))
                    first, last = map(int, match.groups())
                    values = cells[first - 1:last]
                    return {'data': {'revision': revision, 'valueRange': {'values': values}}}
                if method == 'PUT':
                    value = body['valueRange']
                    first = int(re.search(r'!A(\d+):', value['range']).group(1))
                    rows = value['values']
                    while len(cells) < first - 1 + len(rows):
                        cells.append([''] * 4)
                    cells[first - 1:first - 1 + len(rows)] = [list(r) for r in rows]
                    revision += 1
                    return {'data': {'updatedRows': len(rows), 'updatedCells': len(rows) * 4}}
                if method == 'POST' and '/dimension_range' in url:
                    count = body['dimension']['length']
                    grid += count
                    return {'data': {'addCount': count}}
                raise AssertionError('Unexpected synthetic provider operation')

            with (patch.object(feishu, 'load_config', return_value={'app_id': 'SYNTH', 'app_secret': 'SYNTH'}),
                  patch.object(feishu, 'get_tenant_token', return_value='SYNTH'),
                  patch.object(feishu, '_http_json', side_effect=provider),
                  patch.object(es.eccang, 'load_config', return_value={'app_key': 'SYNTH', 'app_secret': 'SYNTH', 'service_id': 'SYNTH'}),
                  patch.object(es, 'accounts_for_shops', return_value=['SYNTH_ACCOUNT']),
                  patch.object(es.eccang, 'call', side_effect=lambda *a, **k: {'data': orders, 'total': len(orders)})):
                es._SYNC['running'] = False
                self.assertEqual(es.sync(root, full=True)['writeback']['status'], 'ok')
                self.assertTrue(fs.sync(root)['complete'])
                first = [r for r in cells[1:] if r and r[0] == '合成店']
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0][3], 5)
                orders[0]['product_count'] = orders[0]['order_details'][0]['qty'] = 10
                self.assertEqual(es.sync(root, full=True)['writeback']['status'], 'ok')
                self.assertTrue(fs.sync(root)['complete'])
                corrected = [r for r in cells[1:] if r and r[0] == '合成店']
                self.assertEqual(len(corrected), 1)
                self.assertEqual(corrected[0][3], 10)
                ledger = root / 'ledger.csv'
                ledger.write_text('店铺,SKU,日期,售后原因\n合成店,SKU1,2026-10-21,破损\n缺销量店,UNKNOWN,2026-10-21,破损\n', encoding='utf-8-sig')
                report = aftersale.run_aftersale(files=[ledger], platform='AmazonCs', category='',
                                                 out_dir=root / 'out', data_dir=root)['aftersale']
                shops = {s['shop']: s for s in report['shops']}
                self.assertEqual(report['rate_basis'], 'feishu_qty')
                self.assertEqual(report['sales']['qty_used'], 30)
                self.assertEqual(shops['合成店']['rate'], .1)
                self.assertEqual(shops['合成零售后店']['rate'], 0)
                self.assertIsNone(shops['缺销量店']['rate'])


if __name__ == '__main__':
    unittest.main()
