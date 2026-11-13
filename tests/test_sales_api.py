"""Sales settings never need credentials in requests or responses."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import app as api
from fastapi import HTTPException


class SalesApiTests(unittest.TestCase):
    def test_reject_erp_export_for_wrong_platform_and_readonly_monthly(self):
        with patch.object(api.feishu_sales, 'save_settings') as save:
            for req in (api.FeishuSalesReq(platform='WayfairCs', write_back=True),
                        api.FeishuSalesReq(auto_sync=True, write_back=False),
                        api.FeishuSalesReq(platform='Amazon')):
                with self.assertRaises(HTTPException) as caught:
                    api.api_feishu_sales_save(req)
                self.assertEqual(caught.exception.status_code, 400)
            save.assert_not_called()

    def test_settings_save_only_non_secret_fields(self):
        req = api.FeishuSalesReq(url='https://example.feishu.cn/sheets/SYNTHETIC?sheet=one',
                                 write_back=True, auto_sync=True)
        with (patch.object(api.feishu_sales, 'save_settings') as save,
              patch.object(api, 'api_feishu_sales', return_value={'configured': True})):
            self.assertTrue(api.api_feishu_sales_save(req)['ok'])
            self.assertEqual(save.call_args.kwargs, req.model_dump())
            self.assertEqual(set(save.call_args.kwargs), {'url', 'platform', 'write_back', 'auto_sync'})

    def test_configured_feishu_does_not_trigger_eccang_on_ledger_import(self):
        with (patch.object(api.feishu_sales, 'load_settings', return_value={'url': 'SYNTHETIC', 'platform': 'AmazonCs'}),
              patch.object(api.eccang_sales, 'start_sync') as start,
              patch.object(api.eccang_sales, 'status') as status):
            api._maybe_sync_sales()
            start.assert_not_called()
            status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
