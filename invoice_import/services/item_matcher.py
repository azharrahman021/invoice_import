from __future__ import annotations

import re
from typing import Any

import frappe

from invoice_import.services.types import ItemMatch

ALIAS_PRIORITY_MIN_COUNT = 8
ALIAS_PRIORITY_MIN_HITS = 20


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

    prioritize_alias = _should_prioritize_aliases(supplier=supplier, template=template)
    alias = _alias_match(description, supplier=supplier, template=template)
    if prioritize_alias and alias:
        return ItemMatch(alias[0], alias[1], alias[2])

    hsn_sac = _normalize_hsn(item.get("hsn_sac") or "")
    if hsn_sac:
        hsn_match = _match_by_hsn(description, hsn_sac, threshold=threshold)
        if hsn_match:
            return hsn_match

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

    weighted = _weighted_item_match(description, item.get("hsn_sac") or "", threshold=threshold)
    if weighted:
        return weighted

    if alias:
        return ItemMatch(alias[0], alias[1], alias[2])

    return ItemMatch(
        None,
        0,
        "unmatched",
        skipped=True,
        comment=f"Skipped unmatched item: {description}",
    )


def _match_by_hsn(description: str, hsn_sac: str, threshold: int = 82) -> ItemMatch | None:
    normalized = _normalize_hsn(hsn_sac)
    if not normalized:
        return None

    candidates = frappe.db.sql(
        """
        select name, item_code, item_name, description, gst_hsn_code
        from `tabItem`
        where disabled = 0
          and has_variants = 0
          and is_purchase_item = 1
          and ifnull(gst_hsn_code, '') <> ''
          and (
            gst_hsn_code = %(hsn)s
            or %(hsn)s like concat(gst_hsn_code, '%%')
            or gst_hsn_code like concat(%(hsn)s, '%%')
          )
        limit 1000
        """,
        {"hsn": normalized},
        as_dict=True,
    )
    if not candidates:
        return None
    if len(candidates) == 1:
        return ItemMatch(candidates[0].name, 100, "hsn_exact")

    try:
        from rapidfuzz import fuzz
    except Exception:
        return None

    query_norm = _normalize_description(description)
    query_tokens = _tokenize_for_weighting(query_norm)
    query_specs = _extract_spec_tokens(query_tokens)
    best_item = None
    best_score = 0.0
    for row in candidates:
        score, method = _score_weighted_candidate(query_norm, query_tokens, normalized, query_specs, row, fuzz)
        if _normalize_hsn(getattr(row, "gst_hsn_code", "") or "") == normalized:
            score += 10.0
        if score > best_score:
            best_score = score
            best_item = row

    if not best_item or best_score < max(85, threshold):
        return None
    return ItemMatch(best_item.name, float(round(best_score, 2)), "hsn_weighted")


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


def _weighted_item_match(description: str, hsn_sac: str, threshold: int = 82) -> ItemMatch | None:
    try:
        from rapidfuzz import fuzz
    except Exception:
        return None

    query_norm = _normalize_description(description)
    if not query_norm:
        return None
    query_tokens = _tokenize_for_weighting(query_norm)
    query_hsn = _normalize_hsn(hsn_sac)
    query_specs = _extract_spec_tokens(query_tokens)

    items = frappe.get_all(
        "Item",
        fields=["name", "item_code", "item_name", "description", "item_group", "brand", "gst_hsn_code"],
        filters={"disabled": 0, "has_variants": 0, "is_purchase_item": 1},
        limit_page_length=10000,
    )
    best_item = None
    best_score = 0.0
    best_method = "weighted"
    for row in items:
        score, method = _score_weighted_candidate(query_norm, query_tokens, query_hsn, query_specs, row, fuzz)
        if score > best_score:
            best_score = score
            best_item = row
            best_method = method

    if not best_item or best_score < max(85, threshold):
        return None
    return ItemMatch(best_item.name, float(round(best_score, 2)), best_method)


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


def _should_prioritize_aliases(supplier: str | None = None, template: str | None = None) -> bool:
    filters: dict[str, Any] = {"is_active": 1}
    if supplier:
        filters["supplier"] = supplier
    if template:
        filters["template"] = template

    aliases = frappe.get_all(
        "Supplier Item Alias",
        filters=filters,
        fields=["item_code", "hit_count"],
        limit_page_length=5000,
    )
    if not aliases:
        return False

    total_hits = sum(int(getattr(row, "hit_count", 0) or 0) for row in aliases)
    unique_items = len({str(getattr(row, "item_code", "") or "").strip() for row in aliases if str(getattr(row, "item_code", "") or "").strip()})
    return len(aliases) >= ALIAS_PRIORITY_MIN_COUNT and (total_hits >= ALIAS_PRIORITY_MIN_HITS or unique_items >= 5)


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


def _tokenize_for_weighting(description: str) -> list[str]:
    tokens = []
    for token in re.split(r"\s+", _normalize_description(description)):
        cleaned = token.strip(".,;:/()[]{}").lower()
        if cleaned:
            tokens.append(cleaned)
    return tokens


def _extract_spec_tokens(tokens: list[str]) -> set[str]:
    specs: set[str] = set()
    for token in tokens:
        if re.search(r"\d", token):
            specs.add(token)
            continue
        if token in {"cpvc", "pvc", "hdpe", "upvc", "gi", "ms", "ss"}:
            specs.add(token)
    return specs


def _score_weighted_candidate(
    query_norm: str,
    query_tokens: list[str],
    query_hsn: str,
    query_specs: set[str],
    row,
    fuzz,
) -> tuple[float, str]:
    candidate_parts = [
        str(getattr(row, "item_code", "") or ""),
        str(getattr(row, "item_name", "") or ""),
        str(getattr(row, "description", "") or ""),
        str(getattr(row, "item_group", "") or ""),
        str(getattr(row, "brand", "") or ""),
        str(getattr(row, "gst_hsn_code", "") or ""),
    ]
    candidate_norm = _normalize_description(" ".join(candidate_parts))
    if not candidate_norm:
        return 0.0, "weighted"

    text_score = max(
        float(fuzz.WRatio(query_norm, candidate_norm)),
        float(fuzz.token_set_ratio(query_norm, candidate_norm)),
    )

    candidate_tokens = _tokenize_for_weighting(candidate_norm)
    candidate_token_set = set(candidate_tokens)
    overlap = len(set(query_tokens) & candidate_token_set)
    overlap_score = (overlap / max(len(set(query_tokens)), 1)) * 30.0

    candidate_specs = _extract_spec_tokens(candidate_tokens)
    spec_overlap = len(query_specs & candidate_specs)
    spec_score = (spec_overlap / max(len(query_specs), 1)) * 18.0 if query_specs else 0.0

    hsn_score = 0.0
    candidate_hsn = _normalize_hsn(getattr(row, "gst_hsn_code", "") or "")
    if query_hsn and candidate_hsn:
        if query_hsn == candidate_hsn:
            hsn_score = 35.0
        elif query_hsn.startswith(candidate_hsn) or candidate_hsn.startswith(query_hsn):
            hsn_score = 28.0

    group_score = 0.0
    group_value = _normalize_description(getattr(row, "item_group", "") or "")
    if group_value:
        group_tokens = set(_tokenize_for_weighting(group_value))
        group_overlap = len(group_tokens & set(query_tokens))
        if group_overlap:
            group_score = min(12.0, group_overlap * 3.0)

    brand_score = 0.0
    brand_value = _normalize_description(getattr(row, "brand", "") or "")
    if brand_value and brand_value.lower() in query_norm:
        brand_score = 8.0

    score = (text_score * 0.35) + overlap_score + spec_score + hsn_score + group_score + brand_score
    method = "weighted"
    if hsn_score >= 28.0:
        method = "weighted_hsn"
    elif spec_score >= 10.0:
        method = "weighted_spec"
    return min(score, 100.0), method


def _normalize_hsn(value: str) -> str:
    return re.sub(r"\D+", "", str(value or "").strip())


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
