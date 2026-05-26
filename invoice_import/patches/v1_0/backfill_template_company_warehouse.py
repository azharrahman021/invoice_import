from __future__ import annotations

import frappe

from invoice_import.invoice_import.doctype.supplier_invoice_template.supplier_invoice_template import (
    infer_template_defaults,
)


def execute() -> None:
    rows = frappe.get_all(
        "Supplier Invoice Template",
        fields=["name", "supplier", "reference_purchase_invoice", "company", "warehouse"],
    )
    for row in rows:
        if row.company and row.warehouse:
            continue
        defaults = infer_template_defaults(row.supplier, row.reference_purchase_invoice)
        values = {}
        if not row.company and defaults.get("company"):
            values["company"] = defaults["company"]
        if not row.warehouse and defaults.get("warehouse"):
            values["warehouse"] = defaults["warehouse"]
        if values:
            frappe.db.set_value("Supplier Invoice Template", row.name, values, update_modified=False)
