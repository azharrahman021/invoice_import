from __future__ import annotations

import re
from typing import Any

import frappe
from frappe.utils import cint

from invoice_import.services.types import SupplierMatch


def match_supplier(data: dict[str, Any], threshold: int = 86, auto_create: bool = False) -> SupplierMatch:
    gstin = (data.get("supplier_gstin") or data.get("supplier_vat") or "").strip()
    supplier_name = (data.get("supplier") or "").strip()
    normalized_supplier_name = _normalize_name(supplier_name)

    if gstin:
        match = _match_by_tax_id(gstin)
        if match:
            return SupplierMatch(supplier=match, score=100, method="tax_id")

    if supplier_name:
        exact = frappe.db.get_value("Supplier", {"supplier_name": supplier_name}, "name")
        if exact:
            return SupplierMatch(supplier=exact, score=100, method="exact_name")

        fuzzy = _fuzzy_supplier_match(supplier_name)
        if fuzzy and fuzzy[1] >= threshold:
            return SupplierMatch(supplier=fuzzy[0], score=fuzzy[1], method="fuzzy_name")

        partial = _partial_supplier_match(supplier_name, normalized_supplier_name)
        if partial and partial[1] >= max(70, threshold - 20):
            return SupplierMatch(supplier=partial[0], score=partial[1], method="partial_name")

    if auto_create and supplier_name:
        supplier = _create_supplier(data)
        return SupplierMatch(supplier=supplier, score=75, method="auto_created", created=True)

    warning = f"Supplier not matched: {supplier_name or gstin or 'unknown'}"
    return SupplierMatch(supplier=None, score=0, method="unmatched", warnings=(warning,))


def _match_by_tax_id(tax_id: str) -> str | None:
    fields = ["gstin", "tax_id"]
    for field in fields:
        if frappe.db.has_column("Supplier", field):
            match = frappe.db.get_value("Supplier", {field: tax_id}, "name")
            if match:
                return match
    return None


def _fuzzy_supplier_match(supplier_name: str) -> tuple[str, float] | None:
    try:
        from rapidfuzz import fuzz, process
    except Exception:
        return None

    suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name"], limit_page_length=5000)
    choices = {row.supplier_name or row.name: row.name for row in suppliers}
    if not choices:
        return None
    result = process.extractOne(supplier_name, choices.keys(), scorer=fuzz.WRatio)
    if not result:
        return None
    label, score, _ = result
    return choices[label], float(score)


def _partial_supplier_match(supplier_name: str, normalized_supplier_name: str) -> tuple[str, float] | None:
    try:
        from rapidfuzz import fuzz
    except Exception:
        return None

    suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name"], limit_page_length=5000)
    if not suppliers:
        return None

    best_name = None
    best_score = 0.0
    for row in suppliers:
        for candidate in filter(None, [row.supplier_name, row.name]):
            candidate_norm = _normalize_name(candidate)
            if not candidate_norm:
                continue
            if normalized_supplier_name and (
                normalized_supplier_name in candidate_norm or candidate_norm in normalized_supplier_name
            ):
                return row.name, 98.0
            score = max(
                float(fuzz.partial_ratio(supplier_name, candidate)),
                float(fuzz.token_set_ratio(supplier_name, candidate)),
                float(fuzz.ratio(normalized_supplier_name, candidate_norm)),
            )
            if score > best_score:
                best_score = score
                best_name = row.name

    if best_name:
        return best_name, best_score
    return None


def _create_supplier(data: dict[str, Any]) -> str:
    supplier_name = (data.get("supplier") or "").strip()
    doc = frappe.get_doc(
        {
            "doctype": "Supplier",
            "supplier_name": supplier_name,
            "supplier_type": "Company",
            "supplier_group": _default_supplier_group(),
        }
    )
    tax_id = (data.get("supplier_gstin") or data.get("supplier_vat") or "").strip()
    if tax_id and frappe.db.has_column("Supplier", "tax_id"):
        doc.tax_id = tax_id
    doc.insert(ignore_permissions=True)
    return doc.name


def _normalize_name(value: str) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _default_supplier_group() -> str:
    group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if not group:
        frappe.throw(frappe._("Create at least one Supplier Group before auto-creating suppliers."))
    return group
