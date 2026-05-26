from __future__ import annotations

import frappe


def execute() -> None:
    rows = frappe.get_all("Supplier Invoice Template", fields=["name", "supplier", "template_name", "supplier_name"])
    for row in rows:
        supplier_name = ""
        if row.supplier:
            supplier_name = frappe.db.get_value("Supplier", row.supplier, "supplier_name") or ""
        value = supplier_name or row.template_name or row.name
        if (row.supplier_name or "") == value:
            continue
        frappe.db.set_value("Supplier Invoice Template", row.name, "supplier_name", value, update_modified=False)
