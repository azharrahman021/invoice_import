from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from invoice_import.api.invoice_import import _with_item_match_status
from invoice_import.services.ai_parser import normalize_invoice_json
from invoice_import.services.ai_parser import _parse_item_line
from invoice_import.services.item_matcher import _is_matchable_item
from invoice_import.services.template_extractor import _parse_item_line as _parse_template_item_line
from invoice_import.services.types import OCRResult


class TestServices(unittest.TestCase):
    def test_ocr_result_contract(self):
        result = OCRResult(text="Invoice 1", provider="unit", confidence=0.5)
        self.assertEqual(result.pages, 1)
        self.assertEqual(result.metadata, {})

    def test_normalize_totals(self):
        data = normalize_invoice_json(
            {
                "supplier": "Supplier A",
                "items": [{"description": "A", "qty": 2, "rate": 5, "amount": 10, "tax_percent": 18}],
                "taxes": [{"description": "GST", "rate": 18, "amount": 1.8}],
            }
        )
        self.assertEqual(data["subtotal"], 10)
        self.assertEqual(data["grand_total"], 11.8)

    def test_parse_item_line_extracts_description_after_hsn(self):
        line = "1 39172990 CASIN CAP 3/4 KONSEAL CP34 (100) 6 Num 46.95 98.00 52.09 281.71 9 25.35 9 25.35 332.42"
        row = _parse_item_line(line, 1)

        self.assertEqual(row["description"], "CASIN CAP 3/4 KONSEAL CP34 (100)")
        self.assertEqual(row["hsn_sac"], "39172990")
        self.assertEqual(row["source_uom"], "Num")
        self.assertEqual(row["source_qty"], 6.0)

    def test_parse_item_line_handles_yahiya_row_format(self):
        line = "1 INDIGO EXT EML SHEEN BLK 4LTR 32091090 PCS 655.08 2 1310.17 18.00 235.83 1546.00"
        row = _parse_item_line(line, 1)

        self.assertEqual(row["description"], "INDIGO EXT EML SHEEN BLK 4LTR")
        self.assertEqual(row["hsn_sac"], "32091090")
        self.assertEqual(row["source_uom"], "PCS")
        self.assertEqual(row["rate"], 655.08)
        self.assertEqual(row["source_qty"], 2.0)
        self.assertEqual(row["amount"], 1546.0)

    def test_template_extractor_handles_yahiya_row_format(self):
        line = "1 INDIGO EXT EML SHEEN BLK 4LTR 32091090 PCS 655.08 2 1310.17 18.00 235.83 1546.00"
        row = _parse_template_item_line(line, 1)

        self.assertEqual(row["description"], "INDIGO EXT EML SHEEN BLK 4LTR")
        self.assertEqual(row["hsn_sac"], "32091090")
        self.assertEqual(row["source_uom"], "PCS")
        self.assertEqual(row["rate"], 655.08)
        self.assertEqual(row["qty"], 2.0)
        self.assertEqual(row["amount"], 1546.0)

    def test_matchable_item_skips_templates_and_non_purchase_items(self):
        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: [0, 1, 1])
        with patch("invoice_import.services.item_matcher.frappe", new=SimpleNamespace(db=fake_db)):
            self.assertFalse(_is_matchable_item("ECCL"))

        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: [0, 0, 1])
        with patch("invoice_import.services.item_matcher.frappe", new=SimpleNamespace(db=fake_db)):
            self.assertTrue(_is_matchable_item("CC34"))

    def test_item_match_status_preserves_source_description(self):
        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: "Inventory Item Name")
        fake_frappe = SimpleNamespace(db=fake_db)
        doc = SimpleNamespace(invoice_template="SIT-1", item_similarity_threshold=82)
        data = {
            "items": [
                {
                    "description": "Invoice printed description",
                    "item_name": "Inventory Item Name",
                }
            ]
        }
        with patch("invoice_import.api.invoice_import.frappe", new=fake_frappe), patch(
            "invoice_import.api.invoice_import.match_item",
            return_value=SimpleNamespace(
                item_code="ITEM-001",
                score=97,
                method="exact",
                skipped=False,
                comment="",
            ),
        ):
            result = _with_item_match_status(doc, data)

        self.assertEqual(result["items"][0]["description"], "Invoice printed description")
        self.assertEqual(result["items"][0]["source_description"], "Invoice printed description")
        self.assertEqual(result["items"][0]["item_name"], "Inventory Item Name")
        self.assertEqual(result["items"][0]["_match_status"]["item_code"], "ITEM-001")


if __name__ == "__main__":
    unittest.main()
