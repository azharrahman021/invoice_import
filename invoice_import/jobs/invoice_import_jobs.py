from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import add_to_date, now, now_datetime

from invoice_import.services.ai_parser import parse_invoice
from invoice_import.services.file_utils import get_file_path
from invoice_import.services.ocr_service import extract_ocr
from invoice_import.services.pi_creator import create_draft_purchase_invoice


MAX_RETRIES = 3
JOB_TIMEOUT_SECONDS = 900


def enqueue_invoice_import(doc, method: str | None = None) -> None:
    if _cleanup_linked_status(doc.name):
        return
    if not _should_enqueue(doc):
        return

    doc.db_set("status", "Queued", update_modified=True)
    doc.db_set("queued_at", now(), update_modified=False)
    frappe.enqueue(
        "invoice_import.jobs.invoice_import_jobs.process_invoice_import",
        queue="long",
        timeout=JOB_TIMEOUT_SECONDS,
        enqueue_after_commit=True,
        invoice_import_name=doc.name,
        job_name=f"invoice_import::{doc.name}",
    )


def process_invoice_import(invoice_import_name: str) -> None:
    doc = frappe.get_doc("Invoice Import", invoice_import_name)
    if _cleanup_linked_status(doc.name):
        return
    if doc.status in {"Draft Created", "Duplicate", "Cancelled"}:
        return

    _set_status(doc.name, "Processing")
    _log(doc.name, "Started invoice import processing.")

    try:
        file_path = get_file_path(doc.attachment)
        template = _get_template_context(doc.invoice_template)
        ocr_result = extract_ocr(file_path)
        _log(doc.name, f"OCR complete using {ocr_result.provider}; pages={ocr_result.pages}.")
        if not template:
            inferred_template = _infer_template_from_text(ocr_result.text)
            if inferred_template:
                doc.db_set("invoice_template", inferred_template["name"], update_modified=True)
                template = _get_template_context(inferred_template["name"])
                _log(doc.name, f"Auto-selected template: {inferred_template['template_name']}")
        if template:
            _log(doc.name, f"Using template: {template.get('template_name') or template.get('name')}")

        extracted = parse_invoice(ocr_result, file_path=file_path, template=template)
        confidence_score = round(float(extracted.get("confidence") or ocr_result.confidence) * 100, 2)
        _save_extraction(doc.name, ocr_result.text, extracted, confidence_score)

        purchase_invoice, warnings = create_draft_purchase_invoice(frappe.get_doc("Invoice Import", doc.name), extracted)
        if purchase_invoice and frappe.db.get_value("Invoice Import", doc.name, "status") != "Duplicate":
            status = "Draft Created" if confidence_score >= 95 and not warnings else "Review Required"
            frappe.db.set_value(
                "Invoice Import",
                doc.name,
                {
                    "status": status,
                    "linked_purchase_invoice": purchase_invoice,
                    "processed_at": now(),
                    "processing_logs": _append_log(
                        frappe.db.get_value("Invoice Import", doc.name, "processing_logs"),
                        "\n".join([f"Draft Purchase Invoice created: {purchase_invoice}", *warnings]),
                    ),
                },
                update_modified=True,
            )
        elif frappe.db.get_value("Invoice Import", doc.name, "status") != "Duplicate":
            frappe.db.set_value(
                "Invoice Import",
                doc.name,
                {
                    "status": "Review Required",
                    "processed_at": now(),
                    "processing_logs": _append_log(
                        frappe.db.get_value("Invoice Import", doc.name, "processing_logs"),
                        "\n".join(warnings or ["Review required before Purchase Invoice creation."]),
                    ),
                },
                update_modified=True,
            )
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        _handle_failure(invoice_import_name, exc)
        raise


def retry_stale_imports() -> None:
    stale_time = add_to_date(now_datetime(), minutes=-30)
    stale = frappe.get_all(
        "Invoice Import",
        filters={
            "status": ["in", ["Queued", "Processing", "Failed"]],
            "retry_count": ["<", MAX_RETRIES],
            "modified": ["<", stale_time],
        },
        pluck="name",
        limit_page_length=50,
    )
    for name in stale:
        doc = frappe.get_doc("Invoice Import", name)
        doc.db_set("status", "Uploaded")
        enqueue_invoice_import(doc, "retry_stale_imports")


def reprocess(invoice_import_name: str) -> None:
    doc = frappe.get_doc("Invoice Import", invoice_import_name)
    doc.db_set("status", "Uploaded")
    enqueue_invoice_import(doc, "manual_reprocess")


def _should_enqueue(doc) -> bool:
    return bool(
        doc.name
        and doc.attachment
        and doc.status in {"Uploaded", "Failed"}
        and not doc.linked_purchase_invoice
        and int(doc.retry_count or 0) < MAX_RETRIES
    )


def _save_extraction(name: str, raw_text: str, extracted: dict[str, Any], confidence_score: float) -> None:
    frappe.db.set_value(
        "Invoice Import",
        name,
        {
            "raw_ocr_text": raw_text,
            "extracted_json": json.dumps(extracted, indent=2, sort_keys=True, default=str),
            "confidence_score": confidence_score,
        },
        update_modified=True,
    )


def _handle_failure(name: str, exc: Exception) -> None:
    retry_count = int(frappe.db.get_value("Invoice Import", name, "retry_count") or 0) + 1
    status = "Failed" if retry_count >= MAX_RETRIES else "Uploaded"
    frappe.db.set_value(
        "Invoice Import",
        name,
        {
            "status": status,
            "retry_count": retry_count,
            "last_error": str(exc),
            "processing_logs": _append_log(
                frappe.db.get_value("Invoice Import", name, "processing_logs"),
                f"Attempt {retry_count} failed: {exc}",
            ),
        },
        update_modified=True,
    )
    frappe.log_error(frappe.get_traceback(), f"Invoice Import failed: {name}")
    if status == "Uploaded":
        frappe.enqueue(
            "invoice_import.jobs.invoice_import_jobs.process_invoice_import",
            queue="long",
            timeout=JOB_TIMEOUT_SECONDS,
            enqueue_after_commit=True,
            invoice_import_name=name,
            job_name=f"invoice_import_retry::{name}::{retry_count}",
        )


def _set_status(name: str, status: str) -> None:
    frappe.db.set_value("Invoice Import", name, "status", status, update_modified=True)


def _log(name: str, message: str) -> None:
    frappe.db.set_value(
        "Invoice Import",
        name,
        "processing_logs",
        _append_log(frappe.db.get_value("Invoice Import", name, "processing_logs"), message),
        update_modified=True,
    )


def _append_log(existing: str | None, message: str) -> str:
    return "\n".join(filter(None, [existing, message]))


def _get_template_context(template_name: str | None) -> dict[str, Any] | None:
    if not template_name:
        return None

    try:
        template = frappe.get_doc("Supplier Invoice Template", template_name)
    except frappe.DoesNotExistError:
        return None

    if not template.is_active:
        return None

    reference_profile = _build_reference_purchase_invoice_profile(template.reference_purchase_invoice)
    examples = frappe.get_all(
        "Supplier Invoice Template Example",
        filters={"template": template.name, "is_active": 1},
        fields=["name", "source_summary", "corrected_json", "notes"],
        order_by="modified desc",
        limit_page_length=5,
    )

    return {
        "name": template.name,
        "template_name": template.template_name,
        "supplier": template.supplier,
        "reference_purchase_invoice": template.reference_purchase_invoice,
        "reference_purchase_invoice_profile": reference_profile,
        "format_notes": template.format_notes,
        "sample_invoice_file": template.sample_invoice_file,
        "item_table_header": getattr(template, "item_table_header", "") or "",
        "item_column_order": getattr(template, "item_column_order", "") or "",
        "field_mappings": [
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
        ],
        "examples": examples,
    }


def _infer_template_from_text(text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()][:20]
    supplier_hint = " ".join(lines[:5])
    full_text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not supplier_hint and not full_text:
        return None

    templates = frappe.get_all(
        "Supplier Invoice Template",
        filters={"is_active": 1},
        fields=["name", "template_name", "supplier", "reference_purchase_invoice"],
        limit_page_length=200,
    )
    if not templates:
        return None

    supplier_hint_l = supplier_hint.lower()
    full_text_l = full_text.lower()
    best = None
    best_score = 0.0
    for row in templates:
        candidates = [row.supplier or "", row.template_name or ""]
        if row.supplier:
            supplier_name = frappe.db.get_value("Supplier", row.supplier, "supplier_name")
            if supplier_name:
                candidates.append(supplier_name)
        if row.reference_purchase_invoice:
            reference = frappe.db.get_value(
                "Purchase Invoice",
                row.reference_purchase_invoice,
                ["supplier", "supplier_name", "bill_no"],
                as_dict=True,
            )
            if reference:
                candidates.extend([reference.get("supplier") or "", reference.get("supplier_name") or "", reference.get("bill_no") or ""])
        alias_hits = _template_alias_hits(row.name, text)
        if alias_hits:
            score = 0.85 + min(alias_hits, 5) * 0.03
            if score > best_score:
                best_score = score
                best = row
        for candidate in candidates:
            candidate_l = candidate.lower().strip()
            if not candidate_l:
                continue
            if candidate_l in supplier_hint_l or candidate_l in full_text_l or supplier_hint_l in candidate_l:
                return row
            score = max(_simple_similarity(candidate_l, supplier_hint_l), _template_text_score(candidate_l, full_text_l))
            if score > best_score:
                best_score = score
                best = row
    return best if best and best_score >= 0.72 else None


def _cleanup_linked_status(name: str) -> bool:
    values = frappe.db.get_value(
        "Invoice Import",
        name,
        ["status", "linked_purchase_invoice"],
        as_dict=True,
    )
    if not values or not values.get("linked_purchase_invoice"):
        return False
    if values.get("status") not in {"Uploaded", "Queued", "Processing", "Failed"}:
        return False

    pi_docstatus = frappe.db.get_value("Purchase Invoice", values.linked_purchase_invoice, "docstatus")
    frappe.db.set_value(
        "Invoice Import",
        name,
        "status",
        "Draft Created" if pi_docstatus == 0 else "Review Required",
        update_modified=True,
    )
    return True


def _template_alias_hits(template_name: str, text: str) -> int:
    notes = frappe.db.get_value("Supplier Invoice Template", template_name, "format_notes") or ""
    text_l = text.lower()
    hits = 0
    for line in notes.splitlines():
        line = line.strip()
        if not line.startswith("- ") or "=>" not in line:
            continue
        alias = line[2:].split("=>", 1)[0].strip().lower()
        if alias and alias in text_l:
            hits += 1
    return hits


def _build_reference_purchase_invoice_profile(purchase_invoice: str | None) -> dict[str, Any] | None:
    if not purchase_invoice:
        return None

    try:
        doc = frappe.get_doc("Purchase Invoice", purchase_invoice)
    except frappe.DoesNotExistError:
        return None

    return {
        "name": doc.name,
        "supplier": doc.supplier,
        "supplier_name": getattr(doc, "supplier_name", "") or "",
        "bill_no": doc.bill_no,
        "bill_date": str(doc.bill_date or ""),
        "posting_date": str(doc.posting_date or ""),
        "grand_total": doc.grand_total,
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "description": row.description,
                "qty": row.qty,
                "uom": row.uom,
                "rate": row.rate,
                "amount": row.amount,
            }
            for row in doc.items
        ],
        "taxes": [
            {
                "description": row.description,
                "account_head": row.account_head,
                "tax_amount": row.tax_amount,
            }
            for row in doc.taxes
        ],
    }


def _simple_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return overlap / max(len(left_tokens), len(right_tokens))


def _template_text_score(candidate: str, text: str) -> float:
    candidate_tokens = {token for token in candidate.split() if len(token) > 2}
    text_tokens = set(text.split())
    if not candidate_tokens or not text_tokens:
        return 0.0
    return len(candidate_tokens & text_tokens) / len(candidate_tokens)
