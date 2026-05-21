from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt


def find_duplicate_purchase_invoice(supplier: str | None, data: dict[str, Any], tolerance: float = 1.0) -> str | None:
    invoice_number = (data.get("invoice_number") or "").strip()
    grand_total = flt(data.get("grand_total"))
    if not supplier or not invoice_number:
        return None

    candidates = frappe.get_all(
        "Purchase Invoice",
        filters={"supplier": supplier, "bill_no": invoice_number, "docstatus": ["<", 2]},
        fields=["name", "grand_total", "rounded_total"],
        limit_page_length=20,
    )
    for candidate in candidates:
        totals = [flt(candidate.grand_total), flt(candidate.rounded_total)]
        if any(abs(total - grand_total) <= tolerance for total in totals):
            return candidate.name
    return None
