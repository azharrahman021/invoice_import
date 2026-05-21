from __future__ import annotations

from typing import Any

import frappe


def learn_from_purchase_invoice(doc, method: str | None = None) -> None:
    supplier = getattr(doc, "supplier", None)
    template_name = _get_template_for_supplier(supplier)
    for row in doc.items or []:
        source_description = _normalize_text(getattr(row, "description", "") or getattr(row, "item_name", "") or "")
        if not source_description or not getattr(row, "item_code", None):
            continue
        _persist_learning(
            template=template_name,
            supplier=supplier,
            source_description=source_description,
            item_code=row.item_code,
            alias_type="pi",
            notes=f"Learned from submitted Purchase Invoice {doc.name}",
        )


def learn_aliases_from_payload(
    template: str | None,
    supplier: str | None,
    original_items: list[dict[str, Any]],
    corrected_items: list[dict[str, Any]],
    source_doc: str | None = None,
) -> None:
    for index, corrected in enumerate(corrected_items):
        source = _normalize_text((original_items[index].get("description") if index < len(original_items) else "") or "")
        target = _normalize_text(corrected.get("item_code") or corrected.get("item_name") or corrected.get("description") or "")
        if not source or not target:
            continue
        item_code = _resolve_item_code(target)
        if not item_code:
            continue
        _persist_learning(
            template=template,
            supplier=supplier,
            source_description=source,
            item_code=item_code,
            alias_type="review",
            notes=f"Learned from review {source_doc or ''}".strip(),
        )


def _persist_learning(
    template: str | None,
    supplier: str | None,
    source_description: str,
    item_code: str,
    alias_type: str,
    notes: str,
) -> None:
    if frappe.db.exists("DocType", "Supplier Item Alias"):
        existing = frappe.db.get_value(
            "Supplier Item Alias",
            {
                "is_active": 1,
                "source_description": source_description,
                "item_code": item_code,
                **({"template": template} if template else {}),
                **({"supplier": supplier} if supplier else {}),
            },
            "name",
        )
        if existing:
            alias = frappe.get_doc("Supplier Item Alias", existing)
            alias.hit_count = (alias.hit_count or 0) + 1
            alias.save(ignore_permissions=True)
            return

        alias = frappe.new_doc("Supplier Item Alias")
        alias.template = template
        alias.supplier = supplier
        alias.source_description = source_description
        alias.item_code = item_code
        alias.alias_type = alias_type
        alias.hit_count = 1
        alias.is_active = 1
        alias.notes = notes
        alias.insert(ignore_permissions=True)
        return

    _append_alias_to_template_notes(template or _get_template_for_supplier(supplier), source_description, item_code, notes)


def _resolve_item_code(item_name_or_code: str) -> str | None:
    return (
        frappe.db.get_value("Item", {"item_code": item_name_or_code}, "name")
        or frappe.db.get_value("Item", {"item_name": item_name_or_code}, "name")
    )


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _get_template_for_supplier(supplier: str | None) -> str | None:
    if not supplier:
        return None
    return frappe.db.get_value("Supplier Invoice Template", {"supplier": supplier, "is_active": 1}, "name")


def _append_alias_to_template_notes(template_name: str | None, source_description: str, item_code: str, notes: str) -> None:
    if not template_name:
        return
    template = frappe.get_doc("Supplier Invoice Template", template_name)
    marker = "\n\nLearned Aliases:\n"
    alias_line = f"- {source_description} => {item_code}"
    existing = template.format_notes or ""
    if "Learned Aliases:" not in existing:
        template.format_notes = (existing.rstrip() + marker + alias_line + f"  # {notes}").strip()
    else:
        template.format_notes = (existing.rstrip() + "\n" + alias_line + f"  # {notes}").strip()
    template.save(ignore_permissions=True)
