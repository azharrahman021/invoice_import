from __future__ import annotations

import json
from pathlib import Path

import frappe

from invoice_import.jobs.invoice_import_jobs import _infer_template_from_text, reprocess
from invoice_import.services.ai_parser import normalize_invoice_json
from invoice_import.services.file_utils import get_file_path, is_pdf
from invoice_import.services.item_matcher import match_item
from invoice_import.services.learning import learn_aliases_from_payload
from invoice_import.services.pi_creator import create_draft_purchase_invoice
from invoice_import.services.uom_conversion import learn_uom_conversions_from_payload


@frappe.whitelist()
def enqueue_reprocess(invoice_import: str) -> dict[str, str]:
    doc = frappe.get_doc("Invoice Import", invoice_import)
    doc.check_permission("write")
    reprocess(doc.name)
    return {"status": "queued"}


@frappe.whitelist()
def create_purchase_invoice_from_review(invoice_import: str, extracted_json: str | None = None) -> dict[str, str]:
    doc = frappe.get_doc("Invoice Import", invoice_import)
    doc.check_permission("write")

    original_data = json.loads(doc.extracted_json or "{}")
    corrected_data = normalize_invoice_json(json.loads(extracted_json or doc.extracted_json or "{}"))
    if not doc.invoice_template:
        inferred_template = _infer_template_from_review(doc, corrected_data, original_data)
        if inferred_template:
            doc.db_set("invoice_template", inferred_template, update_modified=True)
            doc.invoice_template = inferred_template
    _save_learning_example(doc, original_data, corrected_data)
    pi_name, warnings = create_draft_purchase_invoice(doc, corrected_data)
    doc.db_set("extracted_json", json.dumps(corrected_data, indent=2, sort_keys=True, default=str))
    if pi_name:
        doc.db_set("linked_purchase_invoice", pi_name)
        doc.db_set("status", "Review Required")
        if warnings:
            doc.db_set("processing_logs", "\n".join(filter(None, [doc.processing_logs, *warnings])))
        return {"purchase_invoice": pi_name, "status": "created", "warnings": warnings}

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
        example = frappe.new_doc("Supplier Invoice Template Example")
        example.template = doc.invoice_template
        example.invoice_import = doc.name
        example.source_summary = _summarize_learning_example(corrected_data)
        example.original_json = json.dumps(original_data, indent=2, sort_keys=True, default=str)
        example.corrected_json = json.dumps(corrected_data, indent=2, sort_keys=True, default=str)
        example.notes = "Auto-saved from review correction."
        example.insert(ignore_permissions=True)
        _save_item_aliases(doc, original_data, corrected_data)
        _save_uom_conversions(doc, original_data, corrected_data)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to save learning example for {doc.name}")


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
            if original_description:
                item["source_description"] = original_description
            if original_description and not item.get("description"):
                item["description"] = original_description
            if item_name:
                item["item_name"] = item_name
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
