from __future__ import annotations

import json

import frappe
from frappe.model.document import Document


class SupplierInvoiceTemplateExample(Document):
    def validate(self) -> None:
        self._validate_json("corrected_json", required=True)
        self._validate_json("original_json", required=False)

    def _validate_json(self, fieldname: str, required: bool) -> None:
        value = getattr(self, fieldname, None)
        if not value:
            if required:
                frappe.throw(frappe._("{0} is required").format(fieldname.replace("_", " ").title()))
            return
        json.loads(value)
