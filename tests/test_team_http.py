"""Synthetic CSV -> stored report -> real local HTTP responses, no provider calls."""
import csv
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from server import aftersale, feishu_sales
from server.store import Store


class TeamHttpTests(unittest.TestCase):
    def test_synthetic_csv_report_and_summary_share_the_final_contract_over_http(self):
        with tempfile.TemporaryDirectory(prefix="voc-team-http-") as tmp:
            root = Path(tmp)
            settings = feishu_sales.save_settings(root,
                url="https://example.feishu.cn/sheets/SYNTHETIC?sheet=teams")
            quantities = [("行善", 100), ("香港", 200), ("SOUTHWEST", 300),
                          ("威卡斯", 100), ("浙捷", 900), ("茂易通", 50),
                          ("麦吉天", 100), ("初颂", 150)]
            cache = feishu_sales.build_cache([feishu_sales.HEADERS] + [
                [shop, "MR221118BR", "2026-10-01", qty] for shop, qty in quantities], settings, root)
            feishu_sales._save(root, feishu_sales._CACHE, cache)
            events = []
            for shop, count in [("行善", 1), ("香港", 1), ("香港店", 1),
                                ("SOUTHWEST", 3), ("威卡斯", 2), ("浙捷", 8), ("UnknownShop", 1)]:
                events.extend({"店铺": shop, "SKU": "MR221118BR", "日期": "2026-10-20",
                               "售后原因": "破损"} for _ in range(count))
            csv_path = root / "synthetic-orders.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(events[0]))
                writer.writeheader()
                writer.writerows(events)
            store = Store(root / "runs")
            record = store.add(name="合成 Team HTTP 验收", platform="AmazonCs", category="",
                brands="", comment_limit=20, files=[csv_path.name], engine="offline")
            aftersale.run_aftersale(files=[csv_path], platform="AmazonCs", category="",
                out_dir=store.dataset_dir(record["id"]), data_dir=root)
            store.update(record["id"], status="done", progress=100)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
                cwd=SOURCE, env={**os.environ, "VOC_DATA_DIR": str(root), "PYTHONUTF8": "1"},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                local_http = build_opener(ProxyHandler({}))
                def read(path):
                    with local_http.open(f"http://127.0.0.1:{port}" + path, timeout=2) as response:
                        return response.read().decode("utf-8")
                deadline = time.monotonic() + 10
                while True:
                    self.assertIsNone(proc.poll(), "Synthetic HTTP service exited before readiness")
                    try:
                        read("/api/health")
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            self.fail("Synthetic HTTP service did not become ready")
                        time.sleep(.05)
                report = json.loads(read(f"/api/datasets/{record['id']}/result"))["aftersale"]
                summary = json.loads(read("/api/aftersale/summary"))
                self.assertEqual([g["team"] for g in report["teams"]], ["Team 1", "Team 2", "Team 3"])
                self.assertEqual([g["rate"] for g in report["teams"]], [.01, .01, 0])
                self.assertEqual([g["sales_qty"] for g in report["teams"]], [600, 1000, 300])
                self.assertEqual(report["sales"]["numerator_used"], 16)
                self.assertEqual(report["overall_rate"], round(16 / 1900, 4))
                self.assertEqual(len(report["shops"]), 9)  # Eight source shops plus UnknownShop.
                self.assertEqual([g["rate"] for g in summary["teams"]], [.01, .01, 0])
                self.assertEqual([g["sales_qty"] for g in summary["teams"]], [600, 1000, 300])
                self.assertEqual(summary["overall"]["rate"], report["overall_rate"])
                required = {"team", "members", "records", "aftersale", "rate_numerator", "sales_qty",
                            "rate", "rate_basis", "missing_shops", "excluded_shops", "unmatched_shops",
                            "rate_warning", "rate_denominators", "shops"}
                for response in (report, summary):
                    for group in response["teams"]:
                        self.assertTrue(required.issubset(group))
                        self.assertEqual([member["account"] for member in group["shops"]], group["members"])
                        self.assertNotIn("name", group)
                        self.assertNotIn("accounts", group)
                self.assertIn("UnknownShop", report["teams"][0]["unmatched_shops"])
                self.assertIn('id="asSumTeams"', read("/"))
                self.assertIn("function renderAftersaleTeams(", read("/app.js"))
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
