from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class SupplierUOMConversionAlias(Document):
    def validate(self) -> None:
        if not flt(self.conversion_factor) and flt(self.source_qty):
            self.conversion_factor = flt(self.target_qty) / flt(self.source_qty)
        if flt(self.conversion_factor) <= 0:
            frappe.throw(frappe._("Conversion Factor must be greater than zero."))

