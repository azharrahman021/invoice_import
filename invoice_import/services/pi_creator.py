from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import flt, getdate, nowdate

from invoice_import.services.duplicate_checker import find_duplicate_purchase_invoice
from invoice_import.services.item_matcher import match_item
from invoice_import.services.supplier_matcher import match_supplier
from invoice_import.services.uom_conversion import apply_learned_uom_conversion


def create_draft_purchase_invoice(import_doc, data: dict[str, Any]):
    supplier_match = match_supplier(
        data,
        threshold=int(import_doc.supplier_similarity_threshold or 86),
        auto_create=bool(import_doc.auto_create_supplier),
    )
    if not supplier_match.supplier and getattr(import_doc, "invoice_template", None):
        template_supplier = _get_template_supplier(import_doc.invoice_template)
        if template_supplier:
            supplier_match = frappe._dict(
                supplier=template_supplier,
                score=100,
                method="template_fallback",
                created=False,
                warnings=(),
            )
    warnings = list(data.get("warnings") or [])
    warnings.extend(supplier_match.warnings)
    if not supplier_match.supplier:
        return None, warnings

    duplicate = find_duplicate_purchase_invoice(supplier_match.supplier, data)
    if duplicate:
        frappe.db.set_value(
            "Invoice Import",
            import_doc.name,
            {
                "status": "Duplicate",
                "linked_purchase_invoice": duplicate,
                "processing_logs": _append_log(import_doc.processing_logs, f"Duplicate detected: {duplicate}"),
            },
            update_modified=True,
        )
        return duplicate, warnings

    company = (
        getattr(import_doc, "company", None)
        or data.get("company")
        or frappe.defaults.get_user_default("Company")
        or _get_template_company(getattr(import_doc, "invoice_template", None))
        or frappe.db.get_single_value("Global Defaults", "default_company")
    )
    if not company:
        frappe.throw(frappe._("Default Company is required to create Purchase Invoice."))

    pi = frappe.new_doc("Purchase Invoice")
    pi.company = company
    pi.supplier = supplier_match.supplier
    pi.bill_no = data.get("invoice_number")
    pi.bill_date = getdate(data.get("invoice_date")) if data.get("invoice_date") else nowdate()
    pi.posting_date = nowdate()
    if data.get("due_date"):
        pi.due_date = getdate(data.get("due_date"))
    if data.get("currency"):
        pi.currency = data.get("currency")
    if data.get("payment_terms"):
        pi.payment_terms_template = _match_payment_terms(data.get("payment_terms")) or None

    matched_count = 0
    for index, row in enumerate(data.get("items") or [], start=1):
        qty = flt(row.get("accepted_qty") or row.get("qty") or 1)
        source_rate = flt(row.get("price_list_rate") or row.get("rate"))
        line_amount = flt(row.get("amount"))
        item_match = match_item(
            row,
            threshold=int(import_doc.item_similarity_threshold or 82),
            supplier=supplier_match.supplier,
            template=getattr(import_doc, "invoice_template", None),
        )
        if item_match.skipped or not item_match.item_code:
            warnings.append(f"Item row {index}: {item_match.comment}")
            continue
        if _is_item_disabled(item_match.item_code):
            warnings.append(f"Item row {index}: skipped disabled item {item_match.item_code}")
            continue
        matched_count += 1
        qty, conversion_alias = apply_learned_uom_conversion(
            row=row,
            item_code=item_match.item_code,
            supplier=supplier_match.supplier,
            template=getattr(import_doc, "invoice_template", None),
            qty=qty or 1,
        )
        calculated_rate = source_rate
        if qty and line_amount:
            calculated_rate = line_amount / qty
        elif not calculated_rate and qty:
            calculated_rate = line_amount / qty if line_amount else 0
        if conversion_alias:
            warnings.append(f"Item row {index}: applied UOM conversion {conversion_alias}")
        item_name = _get_item_name(item_match.item_code)
        source_description = _get_source_description(row, item_name)
        pi.append(
            "items",
            {
                "item_code": item_match.item_code,
                "item_name": item_name,
                "description": source_description,
                "qty": qty or 1,
                "uom": _get_item_stock_uom(item_match.item_code),
                "rate": calculated_rate or source_rate,
                "price_list_rate": source_rate or calculated_rate,
                "amount": line_amount or ((calculated_rate or source_rate) * (qty or 1)),
                "conversion_factor": 1,
                "warehouse": getattr(import_doc, "warehouse", None) or None,
            },
        )

    if matched_count == 0:
        warnings.append("No invoice line items matched ERPNext Items. Draft Purchase Invoice was not created.")
        return None, warnings

    for tax in data.get("taxes") or []:
        account_head = _resolve_tax_account(tax, company)
        if not account_head:
            warnings.append(f"Tax skipped because no purchase tax account matched: {json.dumps(tax, default=str)}")
            continue
        pi.append(
            "taxes",
            {
                "charge_type": "Actual",
                "account_head": account_head,
                "description": tax.get("description") or account_head,
                "tax_amount": flt(tax.get("amount")),
            },
        )

    pi.flags.ignore_permissions = True
    if getattr(import_doc, "warehouse", None):
        pi.set("set_warehouse", import_doc.warehouse)
    pi.insert()
    _attach_source_file(pi.name, import_doc.attachment)
    return pi.name, warnings


def _resolve_tax_account(tax: dict[str, Any], company: str) -> str | None:
    account_head = tax.get("account_head")
    if account_head and frappe.db.exists("Account", account_head):
        return account_head
    filters = {"company": company, "is_group": 0}
    if frappe.db.has_column("Account", "account_type"):
        filters["account_type"] = "Tax"
    return frappe.db.get_value("Account", filters, "name")


def _get_item_stock_uom(item_code: str) -> str:
    return frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"


def _get_item_name(item_code: str) -> str:
    return frappe.db.get_value("Item", item_code, "item_name") or item_code


def _get_source_description(row: dict[str, Any], fallback: str) -> str:
    description = str(row.get("description") or row.get("source_description") or "").strip()
    return description or fallback


def _is_item_disabled(item_code: str) -> bool:
    return bool(frappe.db.get_value("Item", item_code, "disabled"))


def _get_template_supplier(template_name: str) -> str | None:
    supplier = frappe.db.get_value("Supplier Invoice Template", template_name, "supplier")
    if supplier:
        return supplier
    reference_pi = frappe.db.get_value("Supplier Invoice Template", template_name, "reference_purchase_invoice")
    if reference_pi:
        supplier = frappe.db.get_value("Purchase Invoice", reference_pi, "supplier")
        if supplier:
            return supplier
    return None


def _get_template_company(template_name: str | None) -> str | None:
    if not template_name:
        return None
    reference_pi = frappe.db.get_value("Supplier Invoice Template", template_name, "reference_purchase_invoice")
    if not reference_pi:
        return None
    return frappe.db.get_value("Purchase Invoice", reference_pi, "company")


def _match_payment_terms(value: str) -> str | None:
    return frappe.db.get_value("Payment Terms Template", {"template_name": value}, "name") or frappe.db.get_value(
        "Payment Terms Template", value, "name"
    )


def _attach_source_file(purchase_invoice: str, file_url: str) -> None:
    file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not file_name:
        return
    file_doc = frappe.get_doc("File", file_name)
    copied = frappe.copy_doc(file_doc)
    copied.attached_to_doctype = "Purchase Invoice"
    copied.attached_to_name = purchase_invoice
    copied.insert(ignore_permissions=True)


def _append_log(existing: str | None, message: str) -> str:
    return "\n".join(filter(None, [existing, message]))
