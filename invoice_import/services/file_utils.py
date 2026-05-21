from __future__ import annotations

import os
from pathlib import Path

import frappe


def get_file_path(file_url: str) -> str:
    file_doc_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not file_doc_name:
        frappe.throw(frappe._("File not found for URL {0}").format(file_url))
    file_doc = frappe.get_doc("File", file_doc_name)
    return file_doc.get_full_path()


def get_extension(path_or_url: str) -> str:
    return Path(path_or_url).suffix.lower()


def is_pdf(path_or_url: str) -> bool:
    return get_extension(path_or_url) == ".pdf"


def safe_basename(path: str) -> str:
    return os.path.basename(path).replace("\x00", "")
