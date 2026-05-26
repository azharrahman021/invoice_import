from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from invoice_import.services.ai_parser import normalize_invoice_json
from invoice_import.invoice_import.doctype.invoice_import.invoice_import import InvoiceImport


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

    @patch("invoice_import.invoice_import.doctype.invoice_import.invoice_import.frappe.delete_doc")
    @patch("invoice_import.invoice_import.doctype.invoice_import.invoice_import.frappe.get_all")
    def test_on_trash_deletes_learning_examples(self, mock_get_all, mock_delete_doc):
        mock_get_all.return_value = ["EX-00001", "EX-00002"]
        doc = SimpleNamespace(name="INV-IMP-1")

        InvoiceImport._delete_learning_examples(doc)

        mock_get_all.assert_called_once_with(
            "Supplier Invoice Template Example",
            filters={"invoice_import": "INV-IMP-1"},
            pluck="name",
        )
        mock_delete_doc.assert_any_call(
            "Supplier Invoice Template Example",
            "EX-00001",
            force=1,
            ignore_permissions=True,
        )
        mock_delete_doc.assert_any_call(
            "Supplier Invoice Template Example",
            "EX-00002",
            force=1,
            ignore_permissions=True,
        )


if __name__ == "__main__":
    unittest.main()
