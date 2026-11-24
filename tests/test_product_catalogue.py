import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime
from server import bitable_board as bb, aftersale_live


def _make_pairs():
    return {
        "SKU-001": {"sku": "SKU-001", "display_name": "Alpha Widget", "wf_part_number": "WF-100"},
        "SKU-002": {"sku": "SKU-002", "display_name": "Beta Gadget T01", "wf_part_number": "WF-200"},
        "SKU-003": {"sku": "SKU-003", "display_name": "Gamma Tool", "wf_part_number": "WF-300"},
        "SKU-004": {"sku": "SKU-004", "display_name": "Delta Kit", "wf_part_number": "WF-400"},
    }


def _snapshot(pair_map=None, confirmed_zero_skus=None):
    return bb.build_snapshot(
        [],
        [],
        now=datetime(2026, 11, 24, tzinfo=bb.UTC8),
        source_meta={},
        pair_map=pair_map,
        confirmed_zero_skus=confirmed_zero_skus,
        aftersale_summary={"connected": False},
    )


class ProductCatalogueTests(unittest.TestCase):
    def test_product_metadata_added_without_touching_counts(self):
        base = _snapshot()
        pairs = _make_pairs()
        zero = {"SKU-004"}
        with_pairs = _snapshot(pair_map=pairs, confirmed_zero_skus=zero)
        assert set(with_pairs.keys()) == set(base.keys())
        without_zero = _snapshot(pair_map=pairs)
        for key in base:
            if key != "products":
                assert without_zero[key] == base[key]
        prods = {p["sku"]: p for p in with_pairs["products"]}
        assert set(prods) == set(pairs)
        for sku, meta in pairs.items():
            p = prods[sku]
            assert set(p.keys()) == {"sku", "display_name", "wf_part_number", "confirmed_zero", "t01"}
            assert p["display_name"] == meta["display_name"]
            assert p["wf_part_number"] == meta["wf_part_number"]
            assert p["confirmed_zero"] == (sku in zero)
            assert p["t01"] == aftersale_live._is_t01(sku, meta["display_name"])
        assert pairs == _make_pairs()
        assert prods["SKU-004"]["confirmed_zero"] is True
        assert prods["SKU-001"]["confirmed_zero"] is False


    def test_missing_pair_map_yields_empty_products(self):
        assert _snapshot()["products"] == []

    def test_pairing_table_order_is_preserved(self):
        pairs = _make_pairs()
        keys = list(reversed(pairs))
        ordered = {key:pairs[key] for key in keys}
        assert [p['sku'] for p in _snapshot(pair_map=ordered)['products']] == keys
