from __future__ import annotations

import mimetypes
import os
from typing import Final

import frappe
from frappe.model.document import Document
from frappe.utils import cint


ALLOWED_EXTENSIONS: Final[set[str]] = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}
DEFAULT_MAX_UPLOAD_MB: Final[int] = 20


class SupplierInvoiceTemplate(Document):
    def validate(self) -> None:
        self._validate_sample_invoice_file()
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


def _get_file_doc(file_url: str):
    file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not file_name:
        frappe.throw(frappe._("Attached file was not found: {0}").format(file_url))
    return frappe.get_doc("File", file_name)
