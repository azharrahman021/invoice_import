from __future__ import annotations

import json
import re
from pathlib import Path

import frappe
from frappe.desk.search import search_widget

from invoice_import.jobs.invoice_import_jobs import _infer_template_from_text, reprocess
from invoice_import.services.ai_parser import normalize_invoice_json
from invoice_import.services.file_utils import get_file_path, is_pdf
from invoice_import.services.item_matcher import match_item
from invoice_import.services.learning import learn_aliases_from_payload
from invoice_import.services.pi_creator import create_draft_purchase_invoice, update_draft_purchase_invoice
from invoice_import.services.uom_conversion import (
    learn_purchase_uoms_from_payload,
    learn_uom_conversions_from_payload,
)
from invoice_import.invoice_import.doctype.supplier_invoice_template.supplier_invoice_template import (
    infer_template_defaults,
)
from invoice_import.invoice_import.doctype.invoice_import.invoice_import import (
    infer_previous_import_defaults,
)


@frappe.whitelist()
def get_template_defaults(
    template_name: str | None = None,
    supplier: str | None = None,
    reference_purchase_invoice: str | None = None,
) -> dict[str, str]:
    template_name = str(template_name or "").strip()
    if template_name:
        company = frappe.db.get_value("Supplier Invoice Template", template_name, "company") or ""
        warehouse = frappe.db.get_value("Supplier Invoice Template", template_name, "warehouse") or ""
        if company or warehouse:
            return {"company": str(company or "").strip(), "warehouse": str(warehouse or "").strip()}
        supplier = frappe.db.get_value("Supplier Invoice Template", template_name, "supplier") or ""
        reference_purchase_invoice = frappe.db.get_value("Supplier Invoice Template", template_name, "reference_purchase_invoice") or ""
        return infer_template_defaults(supplier=supplier, reference_purchase_invoice=reference_purchase_invoice)
    return infer_template_defaults(
        supplier=str(supplier or "").strip() or None,
        reference_purchase_invoice=str(reference_purchase_invoice or "").strip() or None,
    )


@frappe.whitelist()
def get_recent_import_defaults() -> dict[str, str]:
    return infer_previous_import_defaults()


@frappe.whitelist()
def enqueue_reprocess(invoice_import: str) -> dict[str, str]:
    doc = frappe.get_doc("Invoice Import", invoice_import)
    doc.check_permission("write")
    reprocess(doc.name)
    return {"status": "queued"}


@frappe.whitelist()
def create_purchase_invoice_from_review(
    invoice_import: str,
    extracted_json: str | None = None,
    action: str | None = None,
) -> dict[str, str]:
    doc = frappe.get_doc("Invoice Import", invoice_import)
    doc.check_permission("write")

    original_data = json.loads(doc.extracted_json or "{}")
    corrected_data = normalize_invoice_json(json.loads(extracted_json or doc.extracted_json or "{}"))
    if not doc.invoice_template:
        inferred_template = _infer_template_from_review(doc, corrected_data, original_data)
        if inferred_template:
            doc.db_set("invoice_template", inferred_template, update_modified=True)
            doc.invoice_template = inferred_template
    action = _normalize_review_action(action, doc.linked_purchase_invoice)
    pi_name = ""
    warnings = []
    updated_existing_pi = False
    if action == "update_existing" and doc.linked_purchase_invoice:
        pi_name, warnings = update_draft_purchase_invoice(doc.linked_purchase_invoice, doc, corrected_data)
        updated_existing_pi = True
    elif action == "create_new":
        pi_name, warnings = create_draft_purchase_invoice(doc, corrected_data)
    elif doc.linked_purchase_invoice:
        pi_name, warnings = update_draft_purchase_invoice(doc.linked_purchase_invoice, doc, corrected_data)
        updated_existing_pi = True
    else:
        pi_name, warnings = create_draft_purchase_invoice(doc, corrected_data)

    if pi_name:
        _save_learning_example(doc, original_data, corrected_data)
        _save_purchase_uoms(doc, corrected_data)
    doc.db_set("extracted_json", json.dumps(corrected_data, indent=2, sort_keys=True, default=str))
    if pi_name:
        doc.db_set("linked_purchase_invoice", pi_name)
        doc.db_set("status", "Draft Created")
        if warnings:
            doc.db_set("processing_logs", "\n".join(filter(None, [doc.processing_logs, *warnings])))
        return {"purchase_invoice": pi_name, "status": "updated" if updated_existing_pi else "created", "warnings": warnings}

    doc.db_set("status", "Review Required")
    doc.db_set("processing_logs", "\n".join(filter(None, [doc.processing_logs, *warnings])))
    return {"purchase_invoice": "", "status": "review_required", "warnings": warnings}


@frappe.whitelist()
def learn_item_alias_from_review(invoice_import: str, source_description: str, item_code: str) -> dict[str, str]:
    doc = frappe.get_doc("Invoice Import", invoice_import)
    doc.check_permission("write")
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(frappe._("Select a valid Item before learning an alias."))

    template = doc.invoice_template
    if not template:
        inferred_template = _infer_template_from_review(doc, {}, {})
        if inferred_template:
            doc.db_set("invoice_template", inferred_template, update_modified=True)
            template = inferred_template
    supplier = frappe.db.get_value("Supplier Invoice Template", template, "supplier") if template else None
    learn_aliases_from_payload(
        template=template,
        supplier=supplier,
        original_items=[{"description": source_description}],
        corrected_items=[{"item_code": item_code}],
        source_doc=doc.name,
    )
    return {"status": "learned"}


@frappe.whitelist()
def learn_uom_conversion_from_review(
    invoice_import: str,
    source_description: str,
    item_code: str,
    source_qty: float,
    source_uom: str,
    target_qty: float,
) -> dict[str, str]:
    doc = frappe.get_doc("Invoice Import", invoice_import)
    doc.check_permission("write")
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(frappe._("Select a valid Item before learning a UOM conversion."))

    template = doc.invoice_template
    if not template:
        inferred_template = _infer_template_from_review(doc, {}, {})
        if inferred_template:
            doc.db_set("invoice_template", inferred_template, update_modified=True)
            template = inferred_template
    supplier = frappe.db.get_value("Supplier Invoice Template", template, "supplier") if template else None
    learn_uom_conversions_from_payload(
        template=template,
        supplier=supplier,
        original_items=[{"description": source_description, "source_qty": source_qty, "source_uom": source_uom}],
        corrected_items=[{"item_code": item_code, "qty": target_qty}],
        source_doc=doc.name,
    )
    return {"status": "learned"}


def _save_learning_example(doc, original_data: dict, corrected_data: dict) -> None:
    if not doc.invoice_template:
        return

    try:
        example_name = frappe.db.get_value(
            "Supplier Invoice Template Example",
            {"invoice_import": doc.name, "template": doc.invoice_template},
            "name",
        )
        example = frappe.get_doc("Supplier Invoice Template Example", example_name) if example_name else frappe.new_doc("Supplier Invoice Template Example")
        example.template = doc.invoice_template
        example.invoice_import = doc.name
        example.source_summary = _summarize_learning_example(corrected_data)
        example.original_json = json.dumps(original_data, indent=2, sort_keys=True, default=str)
        example.corrected_json = json.dumps(corrected_data, indent=2, sort_keys=True, default=str)
        example.notes = "Auto-saved from review correction."
        if example_name:
            example.save(ignore_permissions=True)
        else:
            example.insert(ignore_permissions=True)
        _save_item_aliases(doc, original_data, corrected_data)
        _save_uom_conversions(doc, original_data, corrected_data)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to save learning example for {doc.name}")


def _normalize_review_action(action: str | None, linked_purchase_invoice: str | None) -> str:
    value = str(action or "").strip().lower()
    if value in {"create_new", "new", "create"}:
        return "create_new"
    if value in {"save_existing", "update", "save"}:
        return "update_existing"
    return "update_existing" if linked_purchase_invoice else "create_new"


def _summarize_learning_example(data: dict) -> str:
    items = data.get("items") or []
    return " | ".join(
        [
            f"supplier={data.get('supplier') or ''}",
            f"invoice={data.get('invoice_number') or ''}",
            f"date={data.get('invoice_date') or ''}",
            f"items={len(items)}",
            f"grand_total={data.get('grand_total') or ''}",
        ]
    )


def _infer_template_from_supplier(supplier_name: str) -> str | None:
    supplier_name_l = str(supplier_name or "").strip().lower()
    if not supplier_name_l:
        return None

    templates = frappe.get_all(
        "Supplier Invoice Template",
        filters={"is_active": 1},
        fields=["name", "template_name", "supplier"],
        limit_page_length=200,
    )
    best_name = None
    best_score = 0.0
    for row in templates:
        candidates = [row.supplier or "", row.template_name or ""]
        for candidate in candidates:
            candidate_l = str(candidate or "").strip().lower()
            if not candidate_l:
                continue
            if candidate_l in supplier_name_l or supplier_name_l in candidate_l:
                return row.name
            score = _simple_similarity(candidate_l, supplier_name_l)
            if score > best_score:
                best_score = score
                best_name = row.name
    return best_name if best_score >= 0.72 else None


def _infer_template_from_review(doc, corrected_data: dict, original_data: dict) -> str | None:
    text = doc.raw_ocr_text or ""
    if text:
        inferred = _infer_template_from_text(text)
        if inferred:
            return inferred["name"]
    return _infer_template_from_supplier(corrected_data.get("supplier") or original_data.get("supplier") or "")


def _simple_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def _save_item_aliases(doc, original_data: dict, corrected_data: dict) -> None:
    original_items = original_data.get("items") or []
    corrected_items = corrected_data.get("items") or []
    supplier = frappe.db.get_value("Supplier Invoice Template", doc.invoice_template, "supplier") if doc.invoice_template else None
    try:
        learn_aliases_from_payload(
            template=doc.invoice_template,
            supplier=supplier,
            original_items=original_items,
            corrected_items=corrected_items,
            source_doc=doc.name,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to save item aliases for {doc.name}")


def _save_uom_conversions(doc, original_data: dict, corrected_data: dict) -> None:
    original_items = original_data.get("items") or []
    corrected_items = corrected_data.get("items") or []
    supplier = frappe.db.get_value("Supplier Invoice Template", doc.invoice_template, "supplier") if doc.invoice_template else None
    try:
        learn_uom_conversions_from_payload(
            template=doc.invoice_template,
            supplier=supplier,
            original_items=original_items,
            corrected_items=corrected_items,
            source_doc=doc.name,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to save UOM conversions for {doc.name}")


def _save_purchase_uoms(doc, corrected_data: dict) -> None:
    corrected_items = corrected_data.get("items") or []
    try:
        learn_purchase_uoms_from_payload(
            template=doc.invoice_template,
            supplier=frappe.db.get_value("Supplier Invoice Template", doc.invoice_template, "supplier") if doc.invoice_template else None,
            corrected_items=corrected_items,
            source_doc=doc.name,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to save purchase UOM defaults for {doc.name}")


@frappe.whitelist()
def get_item_uom_context(item_code: str) -> dict[str, object]:
    item_code = str(item_code or "").strip()
    if not item_code:
        return {}
    return _get_item_uom_context([item_code]).get(item_code, {})


@frappe.whitelist()
def search_uom_for_review(
    doctype: str,
    txt: str,
    searchfield: str | None = None,
    start: int = 0,
    page_length: int = 10,
    filters: str | dict | list | None = None,
    filter_fields=None,
    as_dict: bool = False,
    reference_doctype: str | None = None,
    ignore_user_permissions: bool = False,
):
    del filter_fields, as_dict
    start = int(start or 0)
    page_length = int(page_length or 10)
    if isinstance(filters, str):
        filters = frappe.parse_json(filters) or {}
    elif filters is None:
        filters = {}
    elif isinstance(filters, list):
        filters = {}
    else:
        filters = dict(filters)

    preferred_uoms = _dedupe_preserve_order(_parse_uom_list(filters.pop("preferred_uoms", [])))
    search_filters = {key: value for key, value in filters.items() if value not in (None, "", [], {})}
    results: list[tuple] = []

    preferred_matches = _search_preferred_uoms(preferred_uoms, txt)
    results.extend(preferred_matches)

    remaining = max(page_length - len(results), 0)
    if remaining > 0:
        fallback = search_widget(
            doctype,
            txt or "",
            query=None,
            searchfield=searchfield,
            start=start,
            page_length=max(page_length * 3, remaining),
            filters=search_filters or None,
            reference_doctype=reference_doctype,
            ignore_user_permissions=ignore_user_permissions,
        )
        for row in fallback:
            if not row:
                continue
            value = row[0] if isinstance(row, (list, tuple)) else str(row or "")
            if not value or value in {item[0] for item in results}:
                continue
            results.append((value,))
            if len(results) >= page_length:
                break

    return results[:page_length]


@frappe.whitelist()
def search_item_for_review(
    doctype: str,
    txt: str,
    searchfield: str | None = None,
    start: int = 0,
    page_length: int = 10,
    filters: str | dict | list | None = None,
    filter_fields=None,
    as_dict: bool = False,
    reference_doctype: str | None = None,
    ignore_user_permissions: bool = False,
):
    del filter_fields, as_dict
    start = int(start or 0)
    page_length = int(page_length or 10)
    if isinstance(filters, str):
        filters = frappe.parse_json(filters) or {}
    elif filters is None:
        filters = {}
    elif isinstance(filters, list):
        filters = {}
    else:
        filters = dict(filters)

    search_filters = {key: value for key, value in filters.items() if value not in (None, "", [], {})}
    search_filters.setdefault("disabled", 0)
    search_filters.setdefault("has_variants", 0)
    search_filters.setdefault("is_purchase_item", 1)

    query = str(txt or "").strip()
    if not query:
        return search_widget(
            doctype,
            query,
            searchfield=searchfield,
            start=start,
            page_length=page_length,
            filters=search_filters or None,
            reference_doctype=reference_doctype,
            ignore_user_permissions=ignore_user_permissions,
        )

    query_norm = _normalize_search_text(query)
    query_tokens = [token for token in query_norm.split(" ") if token]

    items = frappe.get_all(
        "Item",
        filters=search_filters,
        fields=["name", "item_code", "item_name", "description"],
        limit_page_length=5000,
    )
    ranked = []
    for row in items:
        score = _score_item_for_review(query_norm, query_tokens, row)
        if score <= 0:
            continue
        ranked.append((score, row))

    ranked.sort(key=lambda entry: (-entry[0], str(entry[1].item_name or entry[1].item_code or entry[1].name or "").lower()))

    results = [
        (
            row.name,
            row.item_code or "",
            row.item_name or "",
            row.description or "",
        )
        for score, row in ranked[:page_length]
    ]

    if len(results) < page_length:
        fallback = search_widget(
            doctype,
            query,
            searchfield=searchfield,
            start=start,
            page_length=max(page_length * 2, page_length - len(results)),
            filters=search_filters or None,
            reference_doctype=reference_doctype,
            ignore_user_permissions=ignore_user_permissions,
        )
        seen = {result[0] for result in results}
        for row in fallback:
            if not row:
                continue
            value = row[0] if isinstance(row, (list, tuple)) else str(row or "")
            if not value or value in seen:
                continue
            results.append(row)
            seen.add(value)
            if len(results) >= page_length:
                break

    return results[:page_length]


@frappe.whitelist()
def get_review_payload(invoice_import: str) -> dict:
    doc = frappe.get_doc("Invoice Import", invoice_import)
    doc.check_permission("read")
    template_name = ""
    template_notes = ""
    reference_purchase_invoice = ""
    reference_purchase_invoice_profile = {}
    field_mappings = []
    if doc.invoice_template:
        template = frappe.get_doc("Supplier Invoice Template", doc.invoice_template)
        template_name = template.template_name
        template_notes = template.format_notes
        reference_purchase_invoice = getattr(template, "reference_purchase_invoice", "") or ""
        if reference_purchase_invoice:
            try:
                ref_doc = frappe.get_doc("Purchase Invoice", reference_purchase_invoice)
                reference_purchase_invoice_profile = {
                    "name": ref_doc.name,
                    "supplier": ref_doc.supplier,
                    "supplier_name": getattr(ref_doc, "supplier_name", "") or "",
                    "bill_no": ref_doc.bill_no,
                    "posting_date": str(ref_doc.posting_date or ""),
                    "grand_total": ref_doc.grand_total,
                }
            except frappe.DoesNotExistError:
                reference_purchase_invoice_profile = {}
        field_mappings = [
            {
                "source_label": row.source_label,
                "target_field": row.target_field,
                "page_number": getattr(row, "page_number", 0),
                "region_json": getattr(row, "region_json", ""),
                "value_hint": getattr(row, "value_hint", ""),
                "required": getattr(row, "required", 0),
                "notes": getattr(row, "notes", ""),
            }
            for row in (template.field_mappings or [])
        ]
    return {
        "name": doc.name,
        "attachment": doc.attachment,
        "invoice_template": doc.invoice_template,
        "company": doc.company,
        "warehouse": doc.warehouse,
        "invoice_template_name": template_name,
        "invoice_template_notes": template_notes,
        "reference_purchase_invoice": reference_purchase_invoice,
        "reference_purchase_invoice_profile": reference_purchase_invoice_profile,
        "field_mappings": field_mappings,
        "status": doc.status,
        "confidence_score": doc.confidence_score,
        "linked_purchase_invoice": doc.linked_purchase_invoice,
        "extracted_json": _with_item_match_status(doc, json.loads(doc.extracted_json or "{}")),
        "processing_logs": doc.processing_logs,
    }


def _with_item_match_status(doc, data: dict) -> dict:
    data = dict(data or {})
    items = []
    supplier = data.get("supplier") or ""
    template = doc.invoice_template or None
    threshold = int(doc.item_similarity_threshold or 82)
    for row in data.get("items") or []:
        item = dict(row or {})
        original_description = str(item.get("source_description") or item.get("description") or item.get("item_name") or item.get("item_code") or "").strip()
        try:
            match = match_item(item, threshold=threshold, supplier=supplier, template=template)
            item_name = frappe.db.get_value("Item", match.item_code, "item_name") if match.item_code else ""
            uom_context = _get_item_uom_context([match.item_code]).get(match.item_code or "", {}) if match.item_code else {}
            source_uom = str(item.get("source_uom") or "").strip()
            current_uom = str(item.get("uom") or "").strip()
            default_uom = str(uom_context.get("purchase_uom") or uom_context.get("stock_uom") or "").strip()
            item["_match_status"] = {
                "item_code": match.item_code or "",
                "item_name": item_name or "",
                "score": round(float(match.score or 0), 2),
                "method": match.method or "",
                "skipped": bool(match.skipped),
                "comment": match.comment or "",
                "quality": _match_quality(match.score, match.method, match.skipped),
            }
            if match.item_code and not item.get("item_code"):
                item["item_code"] = match.item_code
            if match.item_code and not item.get("matched_item_code"):
                item["matched_item_code"] = match.item_code
            if item_name and not item.get("matched_item_name"):
                item["matched_item_name"] = item_name
            if uom_context:
                item["stock_uom"] = uom_context.get("stock_uom") or ""
                item["purchase_uom"] = uom_context.get("purchase_uom") or ""
                item["uom_options"] = uom_context.get("uom_options") or []
            if original_description:
                item["source_description"] = original_description
            if original_description and not item.get("description"):
                item["description"] = original_description
            if default_uom and (not current_uom or current_uom == source_uom):
                item["uom"] = default_uom
            elif current_uom:
                item["uom"] = current_uom
        except Exception:
            item["_match_status"] = {
                "item_code": "",
                "score": 0,
                "method": "error",
                "skipped": True,
                "comment": "Item match check failed",
                "quality": "error",
            }
            if original_description:
                item["source_description"] = original_description
        items.append(item)
    data["items"] = items
    return data


def _get_item_uom_context(item_codes: list[str]) -> dict[str, dict[str, object]]:
    item_codes = sorted({str(code or "").strip() for code in item_codes if str(code or "").strip()})
    if not item_codes:
        return {}

    rows = frappe.get_all(
        "Item",
        filters={"name": ["in", item_codes]},
        fields=["name", "stock_uom", "purchase_uom"],
        limit_page_length=len(item_codes) + 10,
    )
    uom_rows = frappe.get_all(
        "UOM Conversion Detail",
        filters={"parent": ["in", item_codes]},
        fields=["parent", "uom"],
        order_by="idx asc",
        limit_page_length=5000,
    )

    context: dict[str, dict[str, object]] = {}
    for row in rows:
        purchase_uom = str(getattr(row, "purchase_uom", "") or "").strip()
        stock_uom = str(getattr(row, "stock_uom", "") or "").strip()
        context[row.name] = {
            "stock_uom": stock_uom,
            "purchase_uom": purchase_uom,
            "uom_options": _dedupe_preserve_order(
                [purchase_uom, stock_uom]
                + [str(uom_row.uom or "").strip() for uom_row in uom_rows if uom_row.parent == row.name]
            ),
        }
    return context


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _parse_uom_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = [part.strip() for part in value.split(",")]
        return [str(entry or "").strip() for entry in (parsed if isinstance(parsed, list) else [parsed])]
    if isinstance(value, (list, tuple, set)):
        return [str(entry or "").strip() for entry in value]
    return [str(value).strip()]


def _search_preferred_uoms(preferred_uoms: list[str], txt: str) -> list[tuple]:
    preferred_uoms = _dedupe_preserve_order(preferred_uoms)
    if not preferred_uoms:
        return []

    txt_norm = str(txt or "").strip().lower()
    matched = []
    for uom in preferred_uoms:
        if txt_norm and txt_norm not in uom.lower():
            continue
        matched.append((uom,))
    if matched:
        return matched

    return [(uom,) for uom in preferred_uoms]


def _normalize_search_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _score_item_for_review(query_norm: str, query_tokens: list[str], row) -> float:
    fields = [
        str(getattr(row, "item_code", "") or ""),
        str(getattr(row, "item_name", "") or ""),
        str(getattr(row, "description", "") or ""),
        str(getattr(row, "name", "") or ""),
    ]
    haystack = _normalize_search_text(" ".join(fields))
    if not haystack:
        return 0.0

    score = 0.0
    score = max(score, _word_fuzzy_score(query_norm, haystack))
    for field in fields:
        normalized_field = _normalize_search_text(field)
        if not normalized_field:
            continue
        score = max(score, _word_fuzzy_score(query_norm, normalized_field))
        if normalized_field.startswith(query_norm):
            score = max(score, 99.0)
        if query_norm in normalized_field:
            score = max(score, 97.0)

    if query_tokens and all(token in haystack for token in query_tokens):
        score += 6.0
    if query_tokens and any(haystack.startswith(token) for token in query_tokens):
        score += 3.0

    return min(score, 100.0)


def _word_fuzzy_score(query_norm: str, text_norm: str) -> float:
    if not query_norm or not text_norm:
        return 0.0

    try:
        from rapidfuzz import fuzz

        return float(
            max(
                fuzz.WRatio(query_norm, text_norm),
                fuzz.token_sort_ratio(query_norm, text_norm),
                fuzz.token_set_ratio(query_norm, text_norm),
            )
        )
    except Exception:
        query_tokens = [token for token in query_norm.split(" ") if token]
        text_tokens = [token for token in text_norm.split(" ") if token]
        if not query_tokens or not text_tokens:
            return 0.0
        overlap = len(set(query_tokens) & set(text_tokens))
        coverage = overlap / len(set(query_tokens))
        order_bonus = 1.0 if all(token in text_norm for token in query_tokens) else 0.0
        return min(100.0, coverage * 85.0 + order_bonus * 15.0)


def _match_quality(score: float, method: str, skipped: bool) -> str:
    if skipped:
        return "unmatched"
    if method in {"exact_code", "exact_name", "exact", "alias_exact", "template_note_exact"} or score >= 95:
        return "strong"
    if method in {"alias_fuzzy", "template_note_fuzzy"} or score >= 88:
        return "good"
    if score >= 75:
        return "weak"
    return "unmatched"


@frappe.whitelist()
def save_template_field_mappings(template_name: str, field_mappings_json: str) -> dict[str, str]:
    template = frappe.get_doc("Supplier Invoice Template", template_name)
    template.check_permission("write")

    try:
        field_mappings = json.loads(field_mappings_json or "[]")
    except Exception:
        frappe.throw(frappe._("Invalid field mappings payload."))

    if not isinstance(field_mappings, list):
        frappe.throw(frappe._("Field mappings payload must be a list."))

    template.set("field_mappings", [])
    for row in field_mappings:
        if not isinstance(row, dict):
            continue
        child = template.append("field_mappings", {})
        child.source_label = str(row.get("source_label") or "").strip()
        child.target_field = str(row.get("target_field") or "").strip()
        child.page_number = int(row.get("page_number") or 1)
        child.region_json = str(row.get("region_json") or "").strip()
        child.value_hint = str(row.get("value_hint") or "").strip()
        child.required = int(row.get("required") or 0)
        child.notes = str(row.get("notes") or "").strip()
    template.save(ignore_permissions=True)
    return {"status": "saved"}


@frappe.whitelist()
def get_template_sample_page_image(file_url: str, page_number: int = 1, resolution: int = 144) -> dict[str, str | int]:
    file_path = get_file_path(file_url)
    if not is_pdf(file_path):
        frappe.throw(frappe._("Sample file must be a PDF."))

    try:
        import pdfplumber
    except Exception as exc:
        frappe.throw(frappe._("PDF rendering is unavailable: {0}").format(exc))

    with pdfplumber.open(file_path) as pdf:
        if page_number < 1 or page_number > len(pdf.pages):
            frappe.throw(frappe._("Page number out of range."))
        page = pdf.pages[page_number - 1]
        image = page.to_image(resolution=resolution).original
        if image.mode != "RGB":
            image = image.convert("RGB")
        output_dir = Path(frappe.get_site_path("private", "files", "invoice_import_mapper"))
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file_path).stem.replace(" ", "_")
        output_name = f"{safe_name}_p{page_number}.png"
        output_path = output_dir / output_name
        image.save(output_path, format="PNG")
        return {
            "file_url": f"/private/files/invoice_import_mapper/{output_name}",
            "page_width": int(image.width),
            "page_height": int(image.height),
            "page_count": len(pdf.pages),
        }
