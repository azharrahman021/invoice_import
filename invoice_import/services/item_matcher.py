from __future__ import annotations

from typing import Any

import frappe

from invoice_import.services.types import ItemMatch


def match_item(
    item: dict[str, Any],
    threshold: int = 82,
    supplier: str | None = None,
    template: str | None = None,
) -> ItemMatch:
    item_code_or_name = _normalize_description(item.get("item_code") or item.get("item_name") or "")
    if item_code_or_name:
        exact_code = frappe.db.get_value(
            "Item",
            {"item_code": item_code_or_name, "disabled": 0, "has_variants": 0, "is_purchase_item": 1},
            "name",
        )
        if exact_code:
            return ItemMatch(exact_code, 100, "exact_code")
        exact_name = frappe.db.get_value(
            "Item",
            {"item_name": item_code_or_name, "disabled": 0, "has_variants": 0, "is_purchase_item": 1},
            "name",
        )
        if exact_name:
            return ItemMatch(exact_name, 100, "exact_name")

    description = _normalize_description(item.get("description") or "")
    if not description:
        description = item_code_or_name
    if not description:
        return ItemMatch(None, 0, "missing_description", skipped=True, comment="Missing item description")

    alias = _alias_match(description, supplier=supplier, template=template)
    if alias:
        return ItemMatch(alias[0], alias[1], alias[2])

    exact = frappe.db.get_value(
        "Item",
        {"item_code": description, "disabled": 0, "has_variants": 0, "is_purchase_item": 1},
        "name",
    ) or frappe.db.get_value(
        "Item",
        {"item_name": description, "disabled": 0, "has_variants": 0, "is_purchase_item": 1},
        "name",
    )
    if exact:
        return ItemMatch(exact, 100, "exact")

    fuzzy = _fuzzy_item_match(description)
    if fuzzy and fuzzy[1] >= threshold:
        return ItemMatch(fuzzy[0], fuzzy[1], "fuzzy")

    return ItemMatch(
        None,
        fuzzy[1] if fuzzy else 0,
        "unmatched",
        skipped=True,
        comment=f"Skipped unmatched item: {description}",
    )


def _fuzzy_item_match(description: str) -> tuple[str, float] | None:
    try:
        from rapidfuzz import fuzz, process
    except Exception:
        return None

    items = frappe.get_all(
        "Item",
        fields=["name", "item_code", "item_name", "description"],
        filters={"disabled": 0, "has_variants": 0, "is_purchase_item": 1},
        limit_page_length=10000,
    )
    choices: dict[str, str] = {}
    for row in items:
        for value in (row.item_code, row.item_name, row.description):
            if value:
                choices[str(value)] = row.name
    if not choices:
        return None
    result = process.extractOne(description, choices.keys(), scorer=fuzz.WRatio)
    if not result:
        return None
    label, score, _ = result
    return choices[label], float(score)


def _alias_match(
    description: str,
    supplier: str | None = None,
    template: str | None = None,
) -> tuple[str, float, str] | None:
    filters: dict[str, Any] = {"is_active": 1}
    if supplier:
        filters["supplier"] = supplier
    if template:
        filters["template"] = template

    aliases = frappe.get_all(
        "Supplier Item Alias",
        filters=filters,
        fields=["item_code", "source_description", "hit_count"],
        limit_page_length=5000,
    )
    if aliases:
        exact = frappe.db.get_value("Supplier Item Alias", {**filters, "source_description": description}, ["name", "item_code"])
        if exact:
            alias_name, item_code = exact
            if not _is_matchable_item(item_code):
                return None
            frappe.db.set_value(
                "Supplier Item Alias",
                alias_name,
                "hit_count",
                (frappe.db.get_value("Supplier Item Alias", alias_name, "hit_count") or 0) + 1,
            )
            return item_code, 100.0, "alias_exact"

        try:
            from rapidfuzz import fuzz, process
        except Exception:
            return None

        choices = {row.source_description: row for row in aliases if row.source_description}
        if choices:
            result = process.extractOne(description, choices.keys(), scorer=fuzz.WRatio)
            if result:
                label, score, _ = result
                if score >= 88:
                    row = choices[label]
                    if not _is_matchable_item(row.item_code):
                        return None
                    return row.item_code, float(score), "alias_fuzzy"

    template_notes_aliases = _parse_template_aliases(template)
    if not template_notes_aliases:
        return None

    exact = template_notes_aliases.get(description)
    if exact:
        if not _is_matchable_item(exact):
            return None
        return exact, 100.0, "template_note_exact"

    try:
        from rapidfuzz import fuzz, process
    except Exception:
        return None

    result = process.extractOne(description, template_notes_aliases.keys(), scorer=fuzz.WRatio)
    if not result:
        return None
    label, score, _ = result
    if score < 88:
        return None
    if not _is_matchable_item(template_notes_aliases[label]):
        return None
    return template_notes_aliases[label], float(score), "template_note_fuzzy"


def _normalize_description(description: str) -> str:
    cleaned = " ".join(str(description).split()).strip()
    tokens = cleaned.split()
    while tokens:
        tail = tokens[-1].lower().strip(".,;:/()[]{}")
        if tail in {"pcs", "pc", "psc", "box", "pac", "pack", "pkt", "kgs", "kg", "nos", "no", "mtr", "mt", "ltr", "lt", "l"}:
            tokens.pop()
            continue
        break
    return " ".join(tokens).strip()


def _is_matchable_item(item_code: str | None) -> bool:
    if not item_code:
        return False
    values = frappe.db.get_value("Item", item_code, ["disabled", "has_variants", "is_purchase_item"])
    return not bool(values[0] or values[1] or not values[2])


def _parse_template_aliases(template: str | None) -> dict[str, str]:
    if not template:
        return {}
    template_notes = frappe.db.get_value("Supplier Invoice Template", template, "format_notes") or ""
    aliases: dict[str, str] = {}
    for line in template_notes.splitlines():
        line = line.strip()
        if not line.startswith("- ") or "=>" not in line:
            continue
        try:
            left, right = line[2:].split("=>", 1)
        except ValueError:
            continue
        source = left.strip()
        item_code = right.split("#", 1)[0].strip()
        if source and item_code:
            aliases[source] = item_code
    return aliases
