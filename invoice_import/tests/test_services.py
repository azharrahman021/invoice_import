from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from invoice_import.api.invoice_import import _with_item_match_status
from invoice_import.api.invoice_import import _save_learning_example
from invoice_import.api.invoice_import import create_purchase_invoice_from_review
from invoice_import.api.invoice_import import get_item_uom_context
from invoice_import.api.invoice_import import search_item_for_review
from invoice_import.api.invoice_import import search_uom_for_review
from invoice_import.services.ai_parser import normalize_invoice_json
from invoice_import.services.ai_parser import _parse_item_line
from invoice_import.services.learning import learn_from_purchase_invoice
from invoice_import.services.item_matcher import _is_matchable_item
from invoice_import.services.pi_creator import create_draft_purchase_invoice, update_draft_purchase_invoice
from invoice_import.services.uom_conversion import learn_purchase_uoms_from_payload
from invoice_import.invoice_import.doctype.supplier_invoice_template.supplier_invoice_template import (
    DEFAULT_FORMAT_NOTES,
    SupplierInvoiceTemplate,
    infer_template_defaults,
)
from invoice_import.invoice_import.doctype.invoice_import.invoice_import import (
    InvoiceImport,
    infer_previous_import_defaults,
)
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

    def test_parse_item_line_splits_fused_hsn_and_description(self):
        line = "1 85389000METAL BOX 6MD GOLDMEDAL Num 5 80.57 475.39"
        row = _parse_item_line(line, 1)

        self.assertEqual(row["hsn_sac"], "85389000")
        self.assertEqual(row["description"], "METAL BOX 6MD GOLDMEDAL")

    def test_parse_item_line_splits_single_letter_hsn_suffix(self):
        line = "5 39249010H FAUCET WATER TEC WHITE - (40) 3 Num 326.73 620.00 47.30 980.19 9 88.22 9 88.22 1156.62"
        row = _parse_item_line(line, 5)

        self.assertEqual(row["hsn_sac"], "39249010")
        self.assertEqual(row["description"], "FAUCET WATER TEC WHITE - (40)")

    def test_parse_item_line_appends_wrapped_continuation_text(self):
        line = "4 SPRAY PAINT TBOLT BLACK GLOSSY 40012345 PCS 1 1 150.00"
        row = _parse_item_line(line, 4, context_after="39 400ML")

        self.assertEqual(row["description"], "SPRAY PAINT TBOLT BLACK GLOSSY 39 400ML")

    def test_parse_item_line_does_not_append_numeric_totals(self):
        line = "9 SURFACE BOX 8MD LEGRAND 673308 (20) 3 Num 115.50 254.00 54.53 346.49 9 31.18 9 31.18 408.86"
        baseline = _parse_item_line(line, 9)
        row = _parse_item_line(line, 9, context_after="3543.87 318.95 318.95 4181.77")

        self.assertEqual(row["description"], baseline["description"])
        self.assertNotIn("3543.87", row["description"])

    def test_template_extractor_handles_yahiya_row_format(self):
        line = "1 INDIGO EXT EML SHEEN BLK 4LTR 32091090 PCS 655.08 2 1310.17 18.00 235.83 1546.00"
        row = _parse_template_item_line(line, 1)

        self.assertEqual(row["description"], "INDIGO EXT EML SHEEN BLK 4LTR")
        self.assertEqual(row["hsn_sac"], "32091090")
        self.assertEqual(row["source_uom"], "PCS")
        self.assertEqual(row["rate"], 655.08)
        self.assertEqual(row["qty"], 2.0)
        self.assertEqual(row["amount"], 1546.0)

    def test_template_extractor_splits_fused_hsn_and_description(self):
        line = "1 85389000METAL BOX 6MD GOLDMEDAL Num 5 80.57 475.39"
        row = _parse_template_item_line(line, 1)

        self.assertEqual(row["hsn_sac"], "85389000")
        self.assertEqual(row["description"], "METAL BOX 6MD GOLDMEDAL")

    def test_template_extractor_splits_single_letter_hsn_suffix(self):
        line = "5 39249010H FAUCET WATER TEC WHITE - (40) 3 Num 326.73 620.00 47.30 980.19 9 88.22 9 88.22 1156.62"
        row = _parse_template_item_line(line, 5)

        self.assertEqual(row["hsn_sac"], "39249010")
        self.assertEqual(row["description"], "FAUCET WATER TEC WHITE - (40)")

    def test_template_extractor_appends_wrapped_continuation_text(self):
        line = "4 SPRAY PAINT TBOLT BLACK GLOSSY 40012345 PCS 1 1 150.00"
        row = _parse_template_item_line(line, 4, context_after="39 400ML")

        self.assertEqual(row["description"], "SPRAY PAINT TBOLT BLACK GLOSSY 39 400ML")

    def test_template_extractor_does_not_append_numeric_totals(self):
        line = "9 SURFACE BOX 8MD LEGRAND 673308 (20) 3 Num 115.50 254.00 54.53 346.49 9 31.18 9 31.18 408.86"
        baseline = _parse_template_item_line(line, 9)
        row = _parse_template_item_line(line, 9, context_after="3543.87 318.95 318.95 4181.77")

        self.assertEqual(row["description"], baseline["description"])
        self.assertNotIn("3543.87", row["description"])

    def test_supplier_invoice_template_sets_default_format_notes(self):
        template = SupplierInvoiceTemplate.__new__(SupplierInvoiceTemplate)
        template.format_notes = ""
        template._set_default_format_notes()

        self.assertEqual(template.format_notes, DEFAULT_FORMAT_NOTES)

    def test_infer_template_defaults_prefers_reference_purchase_invoice(self):
        fake_db = SimpleNamespace(
            get_value=lambda doctype, filters_or_name, fieldname=None, as_dict=False, *args, **kwargs: (
                SimpleNamespace(company="Fix & Build", set_warehouse="Main Warehouse")
                if doctype == "Purchase Invoice"
                else None
            )
        )
        fake_frappe = SimpleNamespace(db=fake_db)
        with patch("invoice_import.invoice_import.doctype.supplier_invoice_template.supplier_invoice_template.frappe", new=fake_frappe):
            defaults = infer_template_defaults(supplier="Supplier A", reference_purchase_invoice="PINV-1")

        self.assertEqual(defaults["company"], "Fix & Build")
        self.assertEqual(defaults["warehouse"], "Main Warehouse")

    def test_invoice_import_uses_template_defaults_before_global(self):
        template_values = {
            ("Supplier Invoice Template", "SIT-1", "company"): "Fix & Build",
            ("Supplier Invoice Template", "SIT-1", "warehouse"): "Main Warehouse",
        }

        def fake_get_value(doctype, filters_or_name, fieldname=None, *args, **kwargs):
            return template_values.get((doctype, filters_or_name, fieldname))

        fake_db = SimpleNamespace(get_value=fake_get_value)
        fake_frappe = SimpleNamespace(
            db=fake_db,
            defaults=SimpleNamespace(get_user_default=lambda *args, **kwargs: None),
        )

        doc = InvoiceImport.__new__(InvoiceImport)
        doc.invoice_template = "SIT-1"
        doc.company = ""
        doc.warehouse = ""
        doc.supplier_similarity_threshold = 86
        doc.item_similarity_threshold = 82
        doc.auto_create_supplier = 0

        with patch("invoice_import.invoice_import.doctype.invoice_import.invoice_import.frappe", new=fake_frappe):
            doc._set_defaults()

        self.assertEqual(doc.company, "Fix & Build")
        self.assertEqual(doc.warehouse, "Main Warehouse")

    def test_invoice_import_prefers_previous_import_defaults_before_global(self):
        fake_db = SimpleNamespace(
            sql=lambda *args, **kwargs: [{"company": "Fix & Build", "warehouse": "Main Warehouse"}]
        )
        fake_frappe = SimpleNamespace(
            db=fake_db,
            defaults=SimpleNamespace(get_user_default=lambda *args, **kwargs: None),
        )

        doc = InvoiceImport.__new__(InvoiceImport)
        doc.invoice_template = ""
        doc.company = ""
        doc.warehouse = ""
        doc.supplier_similarity_threshold = 86
        doc.item_similarity_threshold = 82
        doc.auto_create_supplier = 0

        with patch("invoice_import.invoice_import.doctype.invoice_import.invoice_import.frappe", new=fake_frappe):
            doc._set_defaults()

        self.assertEqual(doc.company, "Fix & Build")
        self.assertEqual(doc.warehouse, "Main Warehouse")

    def test_infer_previous_import_defaults_returns_latest_values(self):
        fake_db = SimpleNamespace(
            sql=lambda *args, **kwargs: [{"company": "Fix & Build", "warehouse": "Main Warehouse"}]
        )
        fake_frappe = SimpleNamespace(db=fake_db)
        with patch("invoice_import.invoice_import.doctype.invoice_import.invoice_import.frappe", new=fake_frappe):
            defaults = infer_previous_import_defaults()

        self.assertEqual(defaults["company"], "Fix & Build")
        self.assertEqual(defaults["warehouse"], "Main Warehouse")

    def test_matchable_item_skips_templates_and_non_purchase_items(self):
        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: [0, 1, 1])
        with patch("invoice_import.services.item_matcher.frappe", new=SimpleNamespace(db=fake_db)):
            self.assertFalse(_is_matchable_item("ECCL"))

        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: [0, 0, 1])
        with patch("invoice_import.services.item_matcher.frappe", new=SimpleNamespace(db=fake_db)):
            self.assertTrue(_is_matchable_item("CC34"))

    def test_item_matcher_uses_hsn_prefix(self):
        fake_db = SimpleNamespace(
            get_value=lambda *args, **kwargs: None,
            sql=lambda *args, **kwargs: [
                SimpleNamespace(name="ITEM-001", item_code="ITEM-001", item_name="Fiber Disc 100", description="Fiber Disc 100", gst_hsn_code="680530")
            ],
        )
        fake_frappe = SimpleNamespace(db=fake_db, get_all=lambda *args, **kwargs: [])
        with patch("invoice_import.services.item_matcher.frappe", new=fake_frappe):
            from invoice_import.services.item_matcher import match_item

            match = match_item({"description": "SAND DISC ALKON 100 10PC", "hsn_sac": "68053000"}, threshold=82)

        self.assertEqual(match.item_code, "ITEM-001")
        self.assertIn(match.method, {"hsn_exact", "hsn_fuzzy"})

    def test_match_item_prioritizes_aliases_when_coverage_is_strong(self):
        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: None)
        fake_frappe = SimpleNamespace(db=fake_db, get_all=lambda *args, **kwargs: [])
        with patch("invoice_import.services.item_matcher.frappe", new=fake_frappe), patch(
            "invoice_import.services.item_matcher._should_prioritize_aliases",
            return_value=True,
        ), patch(
            "invoice_import.services.item_matcher._alias_match",
            return_value=("ITEM-ALIAS", 99.0, "alias_fuzzy"),
        ), patch(
            "invoice_import.services.item_matcher._weighted_item_match",
            return_value=SimpleNamespace(item_code="ITEM-WEIGHT", score=96, method="weighted"),
        ):
            from invoice_import.services.item_matcher import match_item

            match = match_item({"description": "sample item"}, supplier="SUP-1", template="SIT-1")

        self.assertEqual(match.item_code, "ITEM-ALIAS")
        self.assertEqual(match.method, "alias_fuzzy")

    def test_match_item_uses_weighted_match_when_aliases_are_not_prioritized(self):
        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: None)
        fake_frappe = SimpleNamespace(db=fake_db, get_all=lambda *args, **kwargs: [])
        with patch("invoice_import.services.item_matcher.frappe", new=fake_frappe), patch(
            "invoice_import.services.item_matcher._should_prioritize_aliases",
            return_value=False,
        ), patch(
            "invoice_import.services.item_matcher._alias_match",
            return_value=("ITEM-ALIAS", 99.0, "alias_fuzzy"),
        ), patch(
            "invoice_import.services.item_matcher._weighted_item_match",
            return_value=SimpleNamespace(item_code="ITEM-WEIGHT", score=96, method="weighted_hsn"),
        ):
            from invoice_import.services.item_matcher import match_item

            match = match_item({"description": "sample item"}, supplier="SUP-1", template="SIT-1")

        self.assertEqual(match.item_code, "ITEM-WEIGHT")
        self.assertEqual(match.method, "weighted_hsn")

    def test_item_match_status_preserves_source_description(self):
        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: "Inventory Item Name")
        fake_frappe = SimpleNamespace(db=fake_db, get_all=lambda *args, **kwargs: [])
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

    def test_item_match_status_uses_purchase_uom_default(self):
        def fake_get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [SimpleNamespace(name="ITEM-001", stock_uom="Nos", purchase_uom="Box")]
            if doctype == "UOM Conversion Detail":
                return [
                    SimpleNamespace(parent="ITEM-001", uom="Box"),
                    SimpleNamespace(parent="ITEM-001", uom="Nos"),
                    SimpleNamespace(parent="ITEM-001", uom="Pack"),
                ]
            return []

        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: "Inventory Item Name")
        fake_frappe = SimpleNamespace(db=fake_db, get_all=fake_get_all)
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

        self.assertEqual(result["items"][0]["uom"], "Box")
        self.assertEqual(result["items"][0]["purchase_uom"], "Box")
        self.assertEqual(result["items"][0]["stock_uom"], "Nos")
        self.assertEqual(result["items"][0]["uom_options"], ["Box", "Nos", "Pack"])

    def test_item_match_status_preserves_manual_uom_override(self):
        def fake_get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [SimpleNamespace(name="ITEM-001", stock_uom="Nos", purchase_uom="Box")]
            if doctype == "UOM Conversion Detail":
                return [SimpleNamespace(parent="ITEM-001", uom="Box")]
            return []

        fake_db = SimpleNamespace(get_value=lambda *args, **kwargs: "Inventory Item Name")
        fake_frappe = SimpleNamespace(db=fake_db, get_all=fake_get_all)
        doc = SimpleNamespace(invoice_template="SIT-1", item_similarity_threshold=82)
        data = {
            "items": [
                {
                    "description": "Invoice printed description",
                    "source_uom": "PCS",
                    "uom": "BAG",
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

        self.assertEqual(result["items"][0]["uom"], "BAG")
        self.assertEqual(result["items"][0]["purchase_uom"], "Box")
        self.assertEqual(result["items"][0]["stock_uom"], "Nos")

    def test_get_item_uom_context_returns_allowed_uoms(self):
        def fake_get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [SimpleNamespace(name="ITEM-001", stock_uom="Nos", purchase_uom="Box")]
            if doctype == "UOM Conversion Detail":
                return [
                    SimpleNamespace(parent="ITEM-001", uom="Box"),
                    SimpleNamespace(parent="ITEM-001", uom="Pack"),
                ]
            return []

        fake_frappe = SimpleNamespace(get_all=fake_get_all)
        with patch("invoice_import.api.invoice_import.frappe", new=fake_frappe):
            previous_flags = getattr(frappe.local, "flags", None)
            frappe.local.flags = SimpleNamespace(in_test=True)
            try:
                context = get_item_uom_context("ITEM-001")
            finally:
                frappe.local.flags = previous_flags

        self.assertEqual(context["purchase_uom"], "Box")
        self.assertEqual(context["stock_uom"], "Nos")
        self.assertEqual(context["uom_options"], ["Box", "Nos", "Pack"])

    def test_search_uom_for_review_prefers_item_uoms(self):
        def fake_search_widget(*args, **kwargs):
            return [("Pack",), ("Box",), ("Drum",)]

        with patch("invoice_import.api.invoice_import.search_widget", side_effect=fake_search_widget):
            previous_flags = getattr(frappe.local, "flags", None)
            frappe.local.flags = SimpleNamespace(in_test=True)
            try:
                result = search_uom_for_review(
                    "UOM",
                    "",
                    filters={"preferred_uoms": ["Box", "Nos"]},
                    page_length=5,
                )
            finally:
                frappe.local.flags = previous_flags

        self.assertEqual(result[:3], [("Box",), ("Nos",), ("Pack",)])

    def test_search_item_for_review_supports_word_order_fuzzy_match(self):
        items = [
            SimpleNamespace(
                name="ITEM-001",
                item_code="ITEM-001",
                item_name="SPRAY PAINT TBOLT BLACK GLOSSY 39 400ML",
                description="SPRAY PAINT TBOLT BLACK GLOSSY 39 400ML",
            ),
            SimpleNamespace(
                name="ITEM-002",
                item_code="ITEM-002",
                item_name="PVC REDUCING TEE SDR11 1 X 3/4 INCH SUPREME",
                description="PVC REDUCING TEE SDR11 1 X 3/4 INCH SUPREME",
            ),
        ]

        def fake_get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return items
            return []

        def fake_search_widget(*args, **kwargs):
            return [("ITEM-002", "ITEM-002", "PVC REDUCING TEE SDR11 1 X 3/4 INCH SUPREME", "PVC REDUCING TEE SDR11 1 X 3/4 INCH SUPREME")]

        fake_frappe = SimpleNamespace(get_all=fake_get_all, parse_json=lambda value: value, _=lambda value: value)
        with patch("invoice_import.api.invoice_import.frappe", new=fake_frappe), patch(
            "invoice_import.api.invoice_import.search_widget",
            side_effect=fake_search_widget,
        ):
            previous_flags = getattr(frappe.local, "flags", None)
            frappe.local.flags = SimpleNamespace(in_test=True)
            try:
                result = search_item_for_review("Item", "black spray glossy")
            finally:
                frappe.local.flags = previous_flags

        self.assertEqual(result[0][0], "ITEM-001")

    def test_create_draft_purchase_invoice_preserves_source_description(self):
        appended_rows = []

        class FakePurchaseInvoice:
            def __init__(self):
                self.name = "PI-0001"
                self.items = []
                self.taxes = []
                self.flags = SimpleNamespace(ignore_permissions=False)

            def append(self, table, row):
                appended_rows.append((table, dict(row)))
                getattr(self, table).append(dict(row))

            def insert(self, *args, **kwargs):
                return None

        def fake_get_value(doctype, filters_or_name, fieldname=None, *args, **kwargs):
            if doctype == "Item" and fieldname == "item_name":
                return "Inventory Item Name"
            if doctype == "Item" and fieldname == "stock_uom":
                return "Nos"
            if doctype == "Item" and fieldname == "disabled":
                return 0
            return None

        doc = SimpleNamespace(
            name="INV-IMP-1",
            invoice_template=None,
            supplier_similarity_threshold=86,
            item_similarity_threshold=82,
            auto_create_supplier=0,
            company="Test Company",
            warehouse=None,
            attachment=None,
            processing_logs="",
        )
        data = {
            "supplier": "Test Supplier",
            "invoice_number": "INV-001",
            "invoice_date": "2026-05-21",
            "currency": "INR",
            "items": [
                {
                    "description": "Invoice printed description",
                    "item_name": "Inventory Item Name",
                    "uom": "PCS",
                    "qty": 2,
                    "rate": 100,
                    "amount": 200,
                }
            ],
            "taxes": [],
            "warnings": [],
        }

        fake_db = SimpleNamespace(get_value=fake_get_value, exists=lambda *args, **kwargs: True, has_column=lambda *args, **kwargs: False)
        fake_defaults = SimpleNamespace(get_user_default=lambda *args, **kwargs: None)
        fake_frappe = SimpleNamespace(
            db=fake_db,
            defaults=fake_defaults,
            new_doc=lambda doctype: FakePurchaseInvoice(),
            throw=lambda message: (_ for _ in ()).throw(AssertionError(message)),
            _=lambda value: value,
        )

        with patch("invoice_import.services.pi_creator.frappe", new=fake_frappe), patch(
            "invoice_import.services.pi_creator.match_supplier",
            return_value=SimpleNamespace(supplier="SUP-001", warnings=(), score=100, method="exact", created=False),
        ), patch("invoice_import.services.pi_creator.find_duplicate_purchase_invoice", return_value=None), patch(
            "invoice_import.services.pi_creator.match_item",
            return_value=SimpleNamespace(item_code="ITEM-001", skipped=False, comment="", score=100, method="exact"),
        ), patch(
            "invoice_import.services.pi_creator.apply_learned_uom_conversion",
            return_value=(2, None),
        ), patch(
            "invoice_import.services.pi_creator.nowdate",
            return_value="2026-05-21",
        ):
            pi_name, warnings = create_draft_purchase_invoice(doc, data)

        self.assertEqual(pi_name, "PI-0001")
        self.assertEqual(warnings, [])
        self.assertEqual(appended_rows[0][1]["description"], "Invoice printed description")
        self.assertEqual(appended_rows[0][1]["item_name"], "Inventory Item Name")
        self.assertEqual(appended_rows[0][1]["uom"], "PCS")

    def test_create_draft_purchase_invoice_uses_template_company_fallback(self):
        appended_rows = []

        class FakePurchaseInvoice:
            def __init__(self):
                self.name = "PI-0002"
                self.items = []
                self.taxes = []
                self.flags = SimpleNamespace(ignore_permissions=False)
                self.company = None

            def append(self, table, row):
                appended_rows.append((table, dict(row)))
                getattr(self, table).append(dict(row))

            def insert(self, *args, **kwargs):
                return None

        def fake_get_value(doctype, filters_or_name, fieldname=None, *args, **kwargs):
            if doctype == "Item" and fieldname == "item_name":
                return "Inventory Item Name"
            if doctype == "Item" and fieldname == "stock_uom":
                return "Nos"
            if doctype == "Item" and fieldname == "disabled":
                return 0
            if doctype == "Supplier Invoice Template" and fieldname == "reference_purchase_invoice":
                return "PINV-REF"
            if doctype == "Purchase Invoice" and fieldname == "company":
                return "Template Company"
            return None

        doc = SimpleNamespace(
            name="INV-IMP-2",
            invoice_template="SIT-1",
            supplier_similarity_threshold=86,
            item_similarity_threshold=82,
            auto_create_supplier=0,
            company="",
            warehouse=None,
            attachment=None,
            processing_logs="",
        )
        data = {
            "supplier": "Test Supplier",
            "invoice_number": "INV-002",
            "invoice_date": "2026-05-21",
            "currency": "INR",
            "items": [
                {
                    "description": "Invoice printed description",
                    "item_name": "Inventory Item Name",
                    "qty": 1,
                    "rate": 100,
                    "amount": 100,
                }
            ],
            "taxes": [],
            "warnings": [],
        }

        fake_db = SimpleNamespace(
            get_value=fake_get_value,
            exists=lambda *args, **kwargs: True,
            has_column=lambda *args, **kwargs: False,
            get_single_value=lambda *args, **kwargs: None,
        )
        fake_defaults = SimpleNamespace(get_user_default=lambda *args, **kwargs: None)
        fake_frappe = SimpleNamespace(
            db=fake_db,
            defaults=fake_defaults,
            new_doc=lambda doctype: FakePurchaseInvoice(),
            throw=lambda message: (_ for _ in ()).throw(AssertionError(message)),
            _=lambda value: value,
        )

        with patch("invoice_import.services.pi_creator.frappe", new=fake_frappe), patch(
            "invoice_import.services.pi_creator.match_supplier",
            return_value=SimpleNamespace(supplier="SUP-001", warnings=(), score=100, method="exact", created=False),
        ), patch("invoice_import.services.pi_creator.find_duplicate_purchase_invoice", return_value=None), patch(
            "invoice_import.services.pi_creator.match_item",
            return_value=SimpleNamespace(item_code="ITEM-001", skipped=False, comment="", score=100, method="exact"),
        ), patch(
            "invoice_import.services.pi_creator.apply_learned_uom_conversion",
            return_value=(1, None),
        ), patch(
            "invoice_import.services.pi_creator.nowdate",
            return_value="2026-05-21",
        ):
            pi_name, warnings = create_draft_purchase_invoice(doc, data)

        self.assertEqual(pi_name, "PI-0002")
        self.assertEqual(warnings, [])
        self.assertEqual(appended_rows[0][1]["description"], "Invoice printed description")

    def test_update_draft_purchase_invoice_rebuilds_existing_pi_items(self):
        appended_rows = []

        class FakePurchaseInvoice:
            def __init__(self):
                self.name = "PI-0003"
                self.items = [{"item_code": "OLD", "qty": 1}]
                self.taxes = [{"description": "Old Tax"}]
                self.flags = SimpleNamespace(ignore_permissions=False)
                self.docstatus = 0
                self.company = None
                self.meta = SimpleNamespace(has_field=lambda field: field in {"items", "taxes"})

            def set(self, fieldname, value):
                setattr(self, fieldname, value)

            def append(self, table, row):
                appended_rows.append((table, dict(row)))
                getattr(self, table).append(dict(row))

            def save(self, *args, **kwargs):
                return None

        def fake_get_value(doctype, filters_or_name, fieldname=None, *args, **kwargs):
            if doctype == "Item" and fieldname == "item_name":
                return "Inventory Item Name"
            if doctype == "Item" and fieldname == "stock_uom":
                return "Nos"
            if doctype == "Item" and fieldname == "disabled":
                return 0
            return None

        doc = SimpleNamespace(
            name="INV-IMP-3",
            invoice_template=None,
            supplier_similarity_threshold=86,
            item_similarity_threshold=82,
            auto_create_supplier=0,
            company="Test Company",
            warehouse=None,
            attachment=None,
            processing_logs="",
        )
        data = {
            "supplier": "Test Supplier",
            "invoice_number": "INV-003",
            "invoice_date": "2026-05-21",
            "currency": "INR",
            "items": [
                {
                    "description": "Revised description",
                    "item_name": "Inventory Item Name",
                    "uom": "PCS",
                    "qty": 3,
                    "rate": 100,
                    "amount": 300,
                }
            ],
            "taxes": [],
            "warnings": [],
        }

        fake_db = SimpleNamespace(
            get_value=fake_get_value,
            exists=lambda *args, **kwargs: True,
            has_column=lambda *args, **kwargs: False,
        )
        fake_defaults = SimpleNamespace(get_user_default=lambda *args, **kwargs: None)
        fake_frappe = SimpleNamespace(
            db=fake_db,
            defaults=fake_defaults,
            get_doc=lambda doctype, name: FakePurchaseInvoice(),
            throw=lambda message: (_ for _ in ()).throw(AssertionError(message)),
            _=lambda value: value,
        )

        with patch("invoice_import.services.pi_creator.frappe", new=fake_frappe), patch(
            "invoice_import.services.pi_creator.match_supplier",
            return_value=SimpleNamespace(supplier="SUP-001", warnings=(), score=100, method="exact", created=False),
        ), patch("invoice_import.services.pi_creator.find_duplicate_purchase_invoice", return_value=None), patch(
            "invoice_import.services.pi_creator.match_item",
            return_value=SimpleNamespace(item_code="ITEM-001", skipped=False, comment="", score=100, method="exact"),
        ), patch(
            "invoice_import.services.pi_creator.apply_learned_uom_conversion",
            return_value=(3, None),
        ), patch(
            "invoice_import.services.pi_creator.nowdate",
            return_value="2026-05-21",
        ):
            pi_name, warnings = update_draft_purchase_invoice("PI-0003", doc, data)

        self.assertEqual(pi_name, "PI-0003")
        self.assertEqual(warnings, [])
        self.assertEqual(len(appended_rows), 1)
        self.assertEqual(appended_rows[0][1]["description"], "Revised description")
        self.assertEqual(appended_rows[0][1]["qty"], 3)

    def test_create_purchase_invoice_from_review_does_not_fallback_on_update_failure(self):
        doc = SimpleNamespace(
            name="INV-IMP-4",
            extracted_json='{"items": []}',
            invoice_template=None,
            linked_purchase_invoice="PI-0004",
            raw_ocr_text="",
            processing_logs="",
            check_permission=lambda *args, **kwargs: None,
            db_set=lambda *args, **kwargs: None,
        )

        fake_frappe = SimpleNamespace(
            get_doc=lambda doctype, name: doc,
            get_traceback=lambda: "traceback",
            local=SimpleNamespace(flags=SimpleNamespace(in_test=True)),
        )

        with patch("invoice_import.api.invoice_import.frappe", new=fake_frappe), patch(
            "invoice_import.api.invoice_import.update_draft_purchase_invoice",
            side_effect=RuntimeError("update failed"),
        ), patch(
            "invoice_import.api.invoice_import.create_draft_purchase_invoice"
        ) as mock_create, patch(
            "invoice_import.api.invoice_import._save_learning_example"
        ) as mock_save_learning:
            with self.assertRaisesRegex(RuntimeError, "update failed"):
                create_purchase_invoice_from_review.__wrapped__(
                    "INV-IMP-4",
                    extracted_json='{"items": []}',
                )

        mock_create.assert_not_called()
        mock_save_learning.assert_not_called()

    def test_save_learning_example_updates_existing_record(self):
        saved = []
        example = SimpleNamespace(
            template=None,
            invoice_import=None,
            source_summary=None,
            original_json=None,
            corrected_json=None,
            notes=None,
            save=lambda **kwargs: saved.append(kwargs),
        )

        fake_frappe = SimpleNamespace(
            db=SimpleNamespace(get_value=lambda *args, **kwargs: "EX-0001"),
            get_doc=lambda doctype, name: example,
            new_doc=lambda doctype: (_ for _ in ()).throw(AssertionError("should not insert new example")),
            log_error=lambda *args, **kwargs: None,
            get_traceback=lambda: "traceback",
        )

        doc = SimpleNamespace(name="INV-IMP-5", invoice_template="SIT-1")

        with patch("invoice_import.api.invoice_import.frappe", new=fake_frappe), patch(
            "invoice_import.api.invoice_import._save_item_aliases"
        ), patch("invoice_import.api.invoice_import._save_uom_conversions"):
            _save_learning_example(doc, {"supplier": "A"}, {"supplier": "A", "items": []})

        self.assertEqual(saved, [{"ignore_permissions": True}])

    def test_learn_purchase_uoms_updates_blank_purchase_uom(self):
        values = {}

        def fake_set_value(doctype, name, fieldname, value, update_modified=True):
            values[(doctype, name, fieldname)] = value

        def fake_get_value(doctype, filters_or_name, fieldname=None):
            if doctype == "Item" and fieldname == "name":
                return "ITEM-001"
            if doctype == "Item" and fieldname == "purchase_uom":
                return ""
            if doctype == "Item" and fieldname == "stock_uom":
                return "Nos"
            return None

        fake_db = SimpleNamespace(
            exists=lambda doctype, value: doctype == "UOM" and value == "Box",
            get_value=fake_get_value,
            set_value=fake_set_value,
        )
        fake_frappe = SimpleNamespace(db=fake_db)

        with patch("invoice_import.services.uom_conversion.frappe", new=fake_frappe):
            learn_purchase_uoms_from_payload(
                template=None,
                supplier=None,
                corrected_items=[{"item_code": "ITEM-001", "uom": "Box"}],
                source_doc="INV-IMP-1",
            )

        self.assertEqual(values[("Item", "ITEM-001", "purchase_uom")], "Box")

    def test_learn_from_purchase_invoice_uses_row_description(self):
        captured = []

        def fake_persist_learning(**kwargs):
            captured.append(kwargs)

        row = SimpleNamespace(description="Invoice printed description", item_name="Inventory Item Name", item_code="ITEM-001")
        doc = SimpleNamespace(name="PI-1", supplier="SUP-001", items=[row])

        with patch("invoice_import.services.learning._get_template_for_supplier", return_value="SIT-1"), patch(
            "invoice_import.services.learning._persist_learning",
            side_effect=fake_persist_learning,
        ):
            learn_from_purchase_invoice(doc)

        self.assertEqual(captured[0]["source_description"], "Invoice printed description")


if __name__ == "__main__":
    unittest.main()
