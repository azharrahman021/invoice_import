from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt


def apply_learned_uom_conversion(
    *,
    row: dict[str, Any],
    item_code: str,
    supplier: str | None,
    template: str | None,
    qty: float,
) -> tuple[float, str | None]:
    conversion = match_uom_conversion(
        source_description=str(row.get("description") or ""),
        source_uom=str(row.get("uom") or ""),
        item_code=item_code,
        supplier=supplier,
        template=template,
    )
    if not conversion:
        return qty, None

    alias_name, factor = conversion
    frappe.db.set_value(
        "Supplier UOM Conversion Alias",
        alias_name,
        "hit_count",
        (frappe.db.get_value("Supplier UOM Conversion Alias", alias_name, "hit_count") or 0) + 1,
    )
    return flt(qty) * flt(factor), alias_name


def match_uom_conversion(
    *,
    source_description: str,
    source_uom: str,
    item_code: str,
    supplier: str | None,
    template: str | None,
) -> tuple[str, float] | None:
    if not item_code or not frappe.db.exists("DocType", "Supplier UOM Conversion Alias"):
        return None

    filters: dict[str, Any] = {"is_active": 1, "item_code": item_code}
    if template:
        filters["template"] = template
    elif supplier:
        filters["supplier"] = supplier

    rows = frappe.get_all(
        "Supplier UOM Conversion Alias",
        filters=filters,
        fields=["name", "source_description", "source_uom", "conversion_factor"],
        limit_page_length=1000,
    )
    if not rows:
        return None

    normalized_description = _normalize_text(source_description)
    normalized_uom = _normalize_text(source_uom).lower()
    for row in rows:
        if _normalize_text(row.source_description) == normalized_description and (
            not row.source_uom or _normalize_text(row.source_uom).lower() == normalized_uom
        ):
            return row.name, flt(row.conversion_factor)

    try:
        from rapidfuzz import fuzz, process
    except Exception:
        return None

    choices = {row.source_description: row for row in rows if row.source_description}
    result = process.extractOne(normalized_description, choices.keys(), scorer=fuzz.WRatio)
    if not result:
        return None
    label, score, _ = result
    row = choices[label]
    if score < 90:
        return None
    if row.source_uom and normalized_uom and _normalize_text(row.source_uom).lower() != normalized_uom:
        return None
    return row.name, flt(row.conversion_factor)


def learn_uom_conversions_from_payload(
    *,
    template: str | None,
    supplier: str | None,
    original_items: list[dict[str, Any]],
    corrected_items: list[dict[str, Any]],
    source_doc: str | None = None,
) -> None:
    if not frappe.db.exists("DocType", "Supplier UOM Conversion Alias"):
        return

    for index, corrected in enumerate(corrected_items):
        original = original_items[index] if index < len(original_items) else {}
        item_code = _resolve_item_code(corrected.get("item_code") or corrected.get("item_name") or "")
        source_description = _normalize_text(original.get("description") or corrected.get("description") or "")
        source_uom = _normalize_text(original.get("source_uom") or original.get("uom") or corrected.get("source_uom") or corrected.get("uom") or "")
        source_qty = flt(original.get("source_qty") or original.get("accepted_qty") or original.get("qty") or 0)
        target_qty = flt(corrected.get("accepted_qty") or corrected.get("qty") or 0)
        if not item_code or not source_description or source_qty <= 0 or target_qty <= 0:
            continue

        factor = target_qty / source_qty
        if abs(factor - 1) < 0.0001:
            continue
        _persist_uom_conversion(
            template=template,
            supplier=supplier,
            source_description=source_description,
            source_uom=source_uom,
            source_qty=source_qty,
            item_code=item_code,
            target_uom=frappe.db.get_value("Item", item_code, "stock_uom") or corrected.get("uom") or "",
            target_qty=target_qty,
            factor=factor,
            source_doc=source_doc,
        )


def _persist_uom_conversion(
    *,
    template: str | None,
    supplier: str | None,
    source_description: str,
    source_uom: str,
    source_qty: float,
    item_code: str,
    target_uom: str,
    target_qty: float,
    factor: float,
    source_doc: str | None,
) -> None:
    filters = {
        "is_active": 1,
        "source_description": source_description,
        "item_code": item_code,
        **({"template": template} if template else {}),
        **({"supplier": supplier} if supplier else {}),
    }
    existing = frappe.db.get_value("Supplier UOM Conversion Alias", filters, "name")
    if existing:
        doc = frappe.get_doc("Supplier UOM Conversion Alias", existing)
        doc.source_uom = source_uom or doc.source_uom
        doc.source_qty = source_qty
        doc.target_uom = target_uom or doc.target_uom
        doc.target_qty = target_qty
        doc.conversion_factor = factor
        doc.hit_count = (doc.hit_count or 0) + 1
        doc.notes = f"Updated from review {source_doc or ''}".strip()
        doc.save(ignore_permissions=True)
        return

    doc = frappe.new_doc("Supplier UOM Conversion Alias")
    doc.template = template
    doc.supplier = supplier
    doc.source_description = source_description
    doc.source_uom = source_uom
    doc.source_qty = source_qty
    doc.item_code = item_code
    doc.target_uom = target_uom
    doc.target_qty = target_qty
    doc.conversion_factor = factor
    doc.alias_type = "review"
    doc.hit_count = 1
    doc.is_active = 1
    doc.notes = f"Learned from review {source_doc or ''}".strip()
    doc.insert(ignore_permissions=True)


def _resolve_item_code(value: str) -> str | None:
    value = _normalize_text(value)
    if not value:
        return None
    return frappe.db.get_value("Item", {"item_code": value}, "name") or frappe.db.get_value(
        "Item", {"item_name": value}, "name"
    )


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()
