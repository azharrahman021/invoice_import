from __future__ import annotations

import unittest

from invoice_import.services.ai_parser import normalize_invoice_json


class TestInvoiceImportParser(unittest.TestCase):
    def test_normalize_invoice_json_sets_defaults(self):
        payload = {
            "supplier": "ABC Traders",
            "invoice_number": "INV-1",
            "invoice_date": "18/05/2026",
            "items": [{"description": "Widget", "qty": "2", "rate": "10", "amount": "20"}],
            "grand_total": "23.60",
        }

        result = normalize_invoice_json(payload)

        self.assertEqual(result["currency"], "INR")
        self.assertEqual(result["items"][0]["qty"], 2.0)
        self.assertEqual(result["items"][0]["amount"], 20.0)
        self.assertIn("confidence", result)


if __name__ == "__main__":
    unittest.main()
