from __future__ import annotations

import mimetypes
import os
from typing import Final

import frappe
from frappe.model.document import Document
from frappe.utils import cint


ALLOWED_EXTENSIONS: Final[set[str]] = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}
DEFAULT_MAX_UPLOAD_MB: Final[int] = 20
DEFAULT_FORMAT_NOTES: Final[str] = """Header:
- Supplier name appears near top
- Invoice No. label: "Invoice No."
- Invoice Date label: "Date" or "Invoice Date"
- GSTIN appears near supplier name

Item table:
- Item rows start after the heading line
- Columns usually include: description, hsn_sac, qty, uom, rate, amount
- Item names may wrap to next line and should be merged
- Ignore footer totals line

UOM / HSN rules:
- Common UOMs: Nos, PCS, Box, Pack, Roll, Bag, Doz
- HSN is usually 6 to 8 digits
- Allow HSN prefix matching
- Prefer purchase UOM if item has one

Matching rules:
- Prefer learned supplier aliases first
- If no alias exists, use HSN/spec/size/brand matching
- Leave weak matches blank for manual review

Totals / tax rules:
- Grand total appears near bottom
- Ignore numeric-only totals line in item parsing
- Tax summary may appear separately at the end

Known aliases:
- [source description] => [ERPNext item code]
- [source description] => [ERPNext item code]"""


class SupplierInvoiceTemplate(Document):
    def validate(self) -> None:
        self._validate_sample_invoice_file()
        self._sync_supplier_name()
        self._set_default_company_warehouse()
        self._set_default_format_notes()
        if self.is_active is None:
            self.is_active = 1

    def _validate_sample_invoice_file(self) -> None:
        if not self.sample_invoice_file:
            return

        file_doc = _get_file_doc(self.sample_invoice_file)
        file_name = file_doc.file_name or self.sample_invoice_file
        _, ext = os.path.splitext(file_name.lower())
        if ext not in ALLOWED_EXTENSIONS:
            frappe.throw(
                frappe._("Unsupported template file type {0}. Allowed: {1}").format(
                    ext or frappe._("unknown"), ", ".join(sorted(ALLOWED_EXTENSIONS))
                )
            )

        max_upload_mb = cint(os.getenv("INVOICE_IMPORT_MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB)))
        max_bytes = max_upload_mb * 1024 * 1024
        file_size = cint(file_doc.file_size)
        if file_size and file_size > max_bytes:
            frappe.throw(frappe._("Sample invoice exceeds {0} MB limit").format(max_upload_mb))

        guessed_type = mimetypes.guess_type(file_name)[0] or ""
        if guessed_type and not (
            guessed_type.startswith("image/") or guessed_type == "application/pdf"
        ):
            frappe.throw(frappe._("Unsupported MIME type {0}").format(guessed_type))

    def _sync_supplier_name(self) -> None:
        supplier_name = ""
        if self.supplier:
            supplier_name = frappe.db.get_value("Supplier", self.supplier, "supplier_name") or ""
        self.supplier_name = supplier_name or self.template_name or self.name

    def _set_default_format_notes(self) -> None:
        if not (self.format_notes or "").strip():
            self.format_notes = DEFAULT_FORMAT_NOTES

    def _set_default_company_warehouse(self) -> None:
        defaults = infer_template_defaults(self.supplier, self.reference_purchase_invoice)
        if not self.company and defaults.get("company"):
            self.company = defaults["company"]
        if not self.warehouse and defaults.get("warehouse"):
            self.warehouse = defaults["warehouse"]


def infer_template_defaults(supplier: str | None = None, reference_purchase_invoice: str | None = None) -> dict[str, str]:
    company = ""
    warehouse = ""

    if reference_purchase_invoice:
        ref = frappe.db.get_value(
            "Purchase Invoice",
            reference_purchase_invoice,
            ["company", "set_warehouse"],
            as_dict=True,
        )
        if ref:
            company = str(getattr(ref, "company", "") or "").strip()
            warehouse = str(getattr(ref, "set_warehouse", "") or "").strip()
            if not warehouse:
                warehouse = _get_purchase_invoice_warehouse(reference_purchase_invoice)
            if company and not warehouse:
                warehouse = _get_default_warehouse_for_company(company)
        return {"company": company, "warehouse": warehouse}

    supplier = str(supplier or "").strip()
    if supplier:
        recent = frappe.get_all(
            "Purchase Invoice",
            filters={"supplier": supplier, "docstatus": 1},
            fields=["name", "company", "set_warehouse"],
            order_by="modified desc",
            limit_page_length=2,
        )
        for row in recent:
            if not company and getattr(row, "company", None):
                company = str(row.company or "").strip()
            if not warehouse:
                warehouse = str(getattr(row, "set_warehouse", "") or "").strip() or _get_purchase_invoice_warehouse(row.name)
            if company and warehouse:
                break
        if company and not warehouse:
            warehouse = _get_default_warehouse_for_company(company)

    return {"company": company, "warehouse": warehouse}


def _get_purchase_invoice_warehouse(purchase_invoice: str) -> str:
    warehouse = frappe.db.get_value("Purchase Invoice", purchase_invoice, "set_warehouse") or ""
    if warehouse:
        return str(warehouse).strip()
    row = frappe.db.get_value(
        "Purchase Invoice Item",
        {"parent": purchase_invoice, "warehouse": ["!=", ""]},
        "warehouse",
    )
    return str(row or "").strip()


def _get_default_warehouse_for_company(company: str) -> str:
    if not company:
        return ""
    return (
        frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
        or frappe.db.get_single_value("Stock Settings", "default_warehouse")
        or ""
    )


def _get_file_doc(file_url: str):
    file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not file_name:
        frappe.throw(frappe._("Attached file was not found: {0}").format(file_url))
    return frappe.get_doc("File", file_name)
