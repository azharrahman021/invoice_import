from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any

import frappe
from frappe.utils import flt, getdate

from invoice_import.services.template_extractor import extract_template_regions
from invoice_import.services.prompts import INVOICE_EXTRACTION_JSON_SCHEMA, INVOICE_EXTRACTION_SYSTEM_PROMPT
from invoice_import.services.types import OCRResult


def parse_invoice(
    ocr_result: OCRResult,
    file_path: str | None = None,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use a configured LLM when available, otherwise return a deterministic OCR-based draft."""
    template_extracted = extract_template_regions(file_path, template) if file_path and template else {}
    if os.getenv("OPENAI_API_KEY"):
        try:
            parsed = _parse_with_openai(ocr_result, file_path, template)
            normalized = normalize_invoice_json(_merge_template_extraction(parsed, template_extracted))
            return _calibrate_confidence(normalized, ocr_result, template_extracted)
        except Exception as exc:
            frappe.log_error(frappe.get_traceback(), f"OpenAI invoice parsing failed: {exc}")

    parsed = _heuristic_parse(ocr_result.text, template)
    normalized = normalize_invoice_json(_merge_template_extraction(parsed, template_extracted))
    return _calibrate_confidence(normalized, ocr_result, template_extracted)


def normalize_invoice_json(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") or []
    normalized_items: list[dict[str, Any]] = []
    item_confidences: list[float] = []

    for index, item in enumerate(items, start=1):
        description = str(
            item.get("description")
            or item.get("item")
            or item.get("item_code")
            or item.get("item_name")
            or item.get("item_description")
            or ""
        ).strip()
        if not description:
            description = f"Unidentified item {index}"
        confidence = _as_confidence(item.get("confidence"), default=0.65)
        item_confidences.append(confidence)
        item_code = str(item.get("item_code") or item.get("item_name") or item.get("item_description") or description).strip()
        normalized_items.append(
            {
                "item_code": item_code,
                "item_name": str(item.get("item_name") or item.get("item_code") or item_code or description).strip(),
                "description": description,
                "source_qty": flt(item.get("source_qty") or item.get("accepted_qty") or item.get("qty") or item.get("quantity") or 1),
                "source_uom": str(item.get("source_uom") or item.get("uom") or "Nos").strip() or "Nos",
                "accepted_qty": flt(item.get("accepted_qty") or item.get("qty") or item.get("quantity") or 1),
                "qty": flt(item.get("accepted_qty") or item.get("qty") or item.get("quantity") or 1),
                "uom": str(item.get("uom") or "Nos").strip() or "Nos",
                "rate": flt(item.get("rate")),
                "price_list_rate": flt(item.get("price_list_rate") or item.get("rate")),
                "amount": flt(item.get("amount")),
                "hsn_sac": str(item.get("hsn_sac") or "").strip(),
                "mrp": flt(item.get("mrp")),
                "tax_percent": flt(item.get("tax_percent") or item.get("tax_rate")),
                "confidence": confidence,
            }
        )

    taxes = payload.get("taxes") or []
    normalized_taxes = [
        {
            "description": str(tax.get("description") or tax.get("account_head") or "Tax").strip(),
            "rate": flt(tax.get("rate")),
            "amount": flt(tax.get("amount")),
            "account_head": str(tax.get("account_head") or "").strip(),
        }
        for tax in taxes
    ]

    subtotal = flt(payload.get("subtotal"))
    if not subtotal:
        subtotal = sum(flt(item.get("amount")) for item in normalized_items)

    grand_total = flt(payload.get("grand_total") or payload.get("total"))
    if not grand_total:
        grand_total = subtotal + sum(flt(tax.get("amount")) for tax in normalized_taxes)

    confidence_values = [
        _as_confidence(payload.get("confidence"), default=0.7),
        *(item_confidences or [0.65]),
    ]
    confidence = round(sum(confidence_values) / len(confidence_values), 4)

    return {
        "supplier": str(payload.get("supplier") or payload.get("supplier_name") or "").strip(),
        "supplier_gstin": str(payload.get("supplier_gstin") or payload.get("gstin") or "").strip(),
        "supplier_vat": str(payload.get("supplier_vat") or payload.get("vat") or "").strip(),
        "supplier_address": str(payload.get("supplier_address") or payload.get("address") or "").strip(),
        "supplier_phone": str(payload.get("supplier_phone") or payload.get("phone") or "").strip(),
        "supplier_email": str(payload.get("supplier_email") or payload.get("email") or "").strip(),
        "invoice_number": str(payload.get("invoice_number") or payload.get("bill_no") or "").strip(),
        "invoice_date": _normalize_date(payload.get("invoice_date")),
        "due_date": _normalize_date(payload.get("due_date")),
        "currency": str(payload.get("currency") or "INR").strip().upper(),
        "po_number": str(payload.get("po_number") or "").strip(),
        "payment_terms": str(payload.get("payment_terms") or "").strip(),
        "items": normalized_items,
        "subtotal": subtotal,
        "taxes": normalized_taxes,
        "grand_total": grand_total,
        "confidence": confidence,
        "field_confidence": payload.get("field_confidence") or {},
        "warnings": list(payload.get("warnings") or []),
    }


def _merge_template_extraction(base: dict[str, Any], template_extracted: dict[str, Any]) -> dict[str, Any]:
    if not template_extracted:
        return base

    merged = dict(base or {})
    for key in ("supplier", "supplier_gstin", "supplier_vat", "supplier_address", "supplier_phone", "supplier_email", "invoice_number", "invoice_date", "due_date", "currency", "po_number", "payment_terms", "subtotal", "grand_total"):
        value = template_extracted.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value

    template_items = template_extracted.get("items") or []
    if template_items:
        merged["items"] = _merge_template_items(base.get("items") or [], template_items)

    warnings = list(base.get("warnings") or [])
    warnings.append("Applied graphical template extraction.")
    merged["warnings"] = warnings
    return merged


def _calibrate_confidence(data: dict[str, Any], ocr_result: OCRResult, template_extracted: dict[str, Any]) -> dict[str, Any]:
    items = data.get("items") or []
    if not items:
        return data

    score = float(data.get("confidence") or 0)
    if ocr_result.provider == "local-pdf":
        score = max(score, 0.72)
        if data.get("supplier") and data.get("invoice_number"):
            score = max(score, 0.82)
        if template_extracted.get("items"):
            score = max(score, 0.86)
        if data.get("grand_total"):
            score = max(score, min(0.92, score + 0.03))
    data["confidence"] = round(score, 4)
    return data


def _merge_template_items(base_items: list[dict[str, Any]], template_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not template_items:
        return base_items
    if not base_items:
        return template_items

    merged: list[dict[str, Any]] = []
    max_len = max(len(base_items), len(template_items))
    for index in range(max_len):
        base_row = base_items[index] if index < len(base_items) else {}
        template_row = template_items[index] if index < len(template_items) else {}
        row = dict(base_row)
        for key in ("item_code", "item_name", "description", "source_qty", "source_uom", "accepted_qty", "qty", "uom", "hsn_sac", "rate", "price_list_rate", "amount", "mrp", "tax_percent", "confidence"):
            value = template_row.get(key)
            if value not in (None, "", [], {}):
                row[key] = value
        merged.append(row)
    return merged


def _parse_with_openai(
    ocr_result: OCRResult,
    file_path: str | None,
    template: dict[str, Any] | None,
) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI()
    model = os.getenv("INVOICE_IMPORT_OPENAI_MODEL", "gpt-4.1-mini")
    template_context = _format_template_context(template)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"{INVOICE_EXTRACTION_SYSTEM_PROMPT}\n\n"
                f"{template_context}\n\n"
                f"OCR provider: {ocr_result.provider}\n\n"
                f"OCR text:\n{ocr_result.text[:50000]}"
            ).strip(),
        }
    ]

    # The OCR text is the reliable baseline. Vision support can be enabled by adding a file URL adapter here.
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": INVOICE_EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_schema", "json_schema": INVOICE_EXTRACTION_JSON_SCHEMA},
        temperature=0,
    )
    raw = response.choices[0].message.content or "{}"
    return json.loads(raw)


def _heuristic_parse(text: str, template: dict[str, Any] | None = None) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    invoice_number = _find_value(
        lines,
        _template_patterns(template, "invoice_number")
        + [r"invoice\s*(?:no|number|#)\s*[:\-]?\s*(.+)", r"bill\s*(?:no|number)\s*[:\-]?\s*(.+)"],
    )
    invoice_date = _find_value(
        lines,
        _template_patterns(template, "invoice_date")
        + [r"invoice\s*date\s*[:\-]?\s*(.+)", r"date\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})"],
    )
    gstin = _find_value(lines, _template_patterns(template, "supplier_gstin") + [r"(?:gstin|gst)\s*[:\-]?\s*([0-9A-Z]{15})"])
    grand_total = _find_amount(
        lines,
        _template_label_patterns(template, "grand_total")
        + [r"grand\s*total", r"total\s*amount", r"amount\s*payable", r"net\s*payable"],
    )
    subtotal = _find_amount(
        lines,
        _template_label_patterns(template, "subtotal") + [r"subtotal", r"sub\s*total", r"taxable\s*value"],
    )

    supplier = lines[0] if lines else ""
    supplier = _find_value(lines, _template_patterns(template, "supplier") + [r"^(.*)$"]) or supplier
    item_lines = _guess_item_lines(lines)
    items = [_parse_item_line(line, index) for index, line in enumerate(item_lines, start=1)]

    return {
        "supplier": supplier,
        "supplier_gstin": gstin,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "currency": "INR",
        "items": items,
        "subtotal": subtotal,
        "taxes": [],
        "grand_total": grand_total or subtotal,
        "confidence": 0.45,
        "warnings": ["Parsed with heuristic fallback; review is required."],
    }


def _find_value(lines: list[str], patterns: list[str]) -> str:
    for line in lines:
        for pattern in patterns:
            match = re.search(pattern, line, flags=re.I)
            if match:
                return match.group(1).strip()
    return ""


def _find_amount(lines: list[str], labels: list[str]) -> float:
    for line in reversed(lines):
        if any(re.search(label, line, flags=re.I) for label in labels):
            amounts = re.findall(r"[-+]?\d[\d,]*\.?\d*", line)
            if amounts:
                return flt(amounts[-1].replace(",", ""))
    return 0.0


def _guess_item_lines(lines: list[str]) -> list[str]:
    start_index = _find_item_table_start(lines)
    candidates: list[str] = []
    for line in lines[start_index:]:
        if _looks_like_item_row(line):
            candidates.append(line)
        elif candidates and re.search(r"total|taxable|round off|amount", line, re.I):
            break
    return candidates[:50]


def _parse_item_line(line: str, index: int) -> dict[str, Any]:
    tokens = [token for token in re.split(r"\s+", line.strip()) if token]
    numbers = [flt(value.replace(",", "")) for value in re.findall(r"[-+]?\d[\d,]*\.?\d*", line)]
    if not tokens:
        return {
            "description": f"OCR item line {index}",
            "qty": 1,
            "uom": "Nos",
            "rate": 0,
            "amount": 0,
            "tax_percent": 0,
            "confidence": 0.35,
        }

    serial_like = bool(re.fullmatch(r"\d+", tokens[0])) and len(numbers) >= 3
    offset = 1 if serial_like else 0
    hsn_index = _find_hsn_token_index(tokens, start_index=offset)
    uom_index = _find_uom_token_index(tokens, start_index=(hsn_index + 1 if hsn_index is not None else offset))

    qty = 1.0
    rate = numbers[-2] if len(numbers) >= 2 else 0
    amount = numbers[-1] if numbers else 0
    after_uom_numbers: list[float] = []

    if uom_index is not None:
        qty_index = uom_index - 1
        if qty_index > (hsn_index if hsn_index is not None else offset):
            qty = _parse_numeric_token(tokens[qty_index]) or qty
        after_uom_numbers = [
            _parse_numeric_token(token)
            for token in tokens[uom_index + 1 :]
            if _parse_numeric_token(token) or re.fullmatch(r"0+(?:\.0+)?", token)
        ]
        if len(after_uom_numbers) >= 2:
            rate = after_uom_numbers[0] or rate
            if qty == 1.0:
                qty = after_uom_numbers[1] or qty
            amount = after_uom_numbers[-1] or amount
        elif len(after_uom_numbers) == 1:
            rate = after_uom_numbers[0] or rate
        elif qty == 1.0 and len(numbers) >= 3:
            qty = numbers[-3]
            rate = numbers[-4] if len(numbers) >= 4 else rate
    elif len(numbers) >= 2:
        qty = numbers[1]

    description = _extract_item_description(tokens, offset=offset, hsn_index=hsn_index, uom_index=uom_index)
    hsn_sac = tokens[hsn_index] if hsn_index is not None else ""
    return {
        "description": description or f"OCR item line {index}",
        "source_qty": qty,
        "source_uom": tokens[uom_index] if uom_index is not None else "Nos",
        "qty": qty,
        "uom": "Nos",
        "hsn_sac": hsn_sac,
        "rate": rate,
        "amount": amount,
        "tax_percent": 0,
        "confidence": 0.35,
    }


def _extract_item_description(
    tokens: list[str],
    offset: int = 0,
    hsn_index: int | None = None,
    uom_index: int | None = None,
) -> str:
    before_hsn = tokens[offset:hsn_index] if hsn_index is not None and hsn_index > offset else []
    while before_hsn and re.fullmatch(r"[-+]?\d[\d,]*\.?\d*", before_hsn[-1]):
        before_hsn.pop()
    after_hsn = tokens[(hsn_index + 1) if hsn_index is not None else offset : uom_index] if uom_index is not None else tokens[(hsn_index + 1) if hsn_index is not None else offset :]
    while after_hsn and re.fullmatch(r"[-+]?\d[\d,]*\.?\d*", after_hsn[-1]):
        after_hsn.pop()

    before_text = " ".join(before_hsn).strip(" -|")
    after_text = " ".join(after_hsn).strip(" -|")

    if before_text and re.search(r"[A-Za-z]", before_text):
        return before_text
    if after_text and re.search(r"[A-Za-z]", after_text):
        return _strip_trailing_supplier_uom(after_text)
    if before_text:
        return before_text
    if after_text:
        return _strip_trailing_supplier_uom(after_text)
    return ""


def _normalize_date(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = getdate(value)
        if isinstance(parsed, date):
            return parsed.isoformat()
    except Exception:
        return str(value).strip()
    return str(value).strip()


def _as_confidence(value: Any, default: float) -> float:
    score = flt(value)
    if score > 1:
        score = score / 100
    if score <= 0:
        score = default
    return max(0.0, min(score, 1.0))


def _is_plausible_item_row(item: dict[str, Any], description: str) -> bool:
    text = re.sub(r"\s+", " ", str(description or "")).strip()
    lowered = text.lower()
    if not text:
        return False

    bad_terms = (
        "phone",
        "mobile",
        "gstin",
        "address",
        "bank",
        "branch",
        "account",
        "declaration",
        "authorized",
        "signatory",
        "total",
        "taxable",
        "round off",
        "amount",
        "in words",
        "email",
        "state :",
        "area :",
        "for yahiya stores",
    )
    if any(term in lowered for term in bad_terms):
        return False

    qty = flt(item.get("accepted_qty") or item.get("qty") or item.get("quantity") or 0)
    rate = flt(item.get("rate") or item.get("price_list_rate") or 0)
    amount = flt(item.get("amount") or 0)
    hsn_sac = str(item.get("hsn_sac") or "").strip()

    if "|" in text and not hsn_sac:
        return False
    if qty <= 0 and rate <= 0 and amount <= 0:
        return False
    if qty > 100000 or rate > 100000 or amount > 10000000:
        return False
    if not hsn_sac and len(re.findall(r"[A-Za-z]+", text)) < 2:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    return True


def _format_template_context(template: dict[str, Any] | None) -> str:
    if not template:
        return ""

    lines = [f"Supplier invoice template: {template.get('template_name') or template.get('name')}"]
    if template.get("supplier"):
        lines.append(f"Supplier: {template.get('supplier')}")
    if template.get("format_notes"):
        lines.append("Format notes:")
        lines.append(str(template.get("format_notes")).strip())
    reference_profile = template.get("reference_purchase_invoice_profile") or {}
    if reference_profile:
        lines.append("Reference purchase invoice:")
        lines.append(
            f"- {reference_profile.get('name') or ''} | supplier: {reference_profile.get('supplier_name') or reference_profile.get('supplier') or ''} | bill_no: {reference_profile.get('bill_no') or ''} | posting_date: {reference_profile.get('posting_date') or ''} | grand_total: {reference_profile.get('grand_total') or ''}"
        )
        ref_items = reference_profile.get("items") or []
        if ref_items:
            lines.append("Reference item pattern:")
            for row in ref_items[:30]:
                lines.append(
                    f"- {row.get('item_name') or row.get('item_code') or ''} -> {row.get('item_code') or ''} | desc: {row.get('description') or ''} | qty: {row.get('qty') or ''} | uom: {row.get('uom') or ''} | rate: {row.get('rate') or ''} | amount: {row.get('amount') or ''}"
                )
    mappings = template.get("field_mappings") or []
    if template.get("item_table_header") or template.get("item_column_order"):
        lines.append("Item table profile:")
        if template.get("item_table_header"):
            lines.append(f"- header: {template.get('item_table_header')}")
        if template.get("item_column_order"):
            lines.append(f"- column_order: {template.get('item_column_order')}")
    if mappings:
        lines.append("Field mappings:")
        for row in mappings:
            source_label = str(row.get("source_label") or "").strip()
            target_field = str(row.get("target_field") or "").strip()
            page_number = str(row.get("page_number") or "").strip()
            value_hint = str(row.get("value_hint") or "").strip()
            region_json = str(row.get("region_json") or "").strip()
            extra = f" | hint: {value_hint}" if value_hint else ""
            page_extra = f" | page: {page_number}" if page_number else ""
            if region_json:
                page_extra += f" | region: {region_json[:600]}"
            lines.append(f"- {source_label} -> {target_field}{extra}{page_extra}")
    examples = template.get("examples") or []
    if examples:
        lines.append("Learned corrections from prior reviews:")
        for example in examples[:5]:
            summary = str(example.get("source_summary") or "").strip()
            corrected_json = str(example.get("corrected_json") or "").strip()
            notes = str(example.get("notes") or "").strip()
            lines.append(f"- {summary}")
            if notes:
                lines.append(f"  note: {notes}")
            if corrected_json:
                lines.append("  corrected_json:")
                lines.append(corrected_json[:1200])
    return "\n".join(lines).strip()


def _find_item_table_start(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        normalized = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
        if _looks_like_item_header(normalized):
            if _has_item_rows_ahead(lines[index + 1 : index + 6]):
                return index + 1
        if "description" in normalized and "qty" in normalized and "rate" in normalized:
            if _has_item_rows_ahead(lines[index + 1 : index + 6]):
                return index + 1
    return 0


def _looks_like_item_header(compact: str) -> bool:
    if not compact:
        return False
    return (
        "description" in compact
        and "qty" in compact
        and ("rate" in compact or "amount" in compact)
        and ("hsn" in compact or "uom" in compact or "unit" in compact or "tax" in compact)
    )


def _looks_like_item_row(line: str) -> bool:
    tokens = [token for token in re.split(r"\s+", line.strip()) if token]
    if len(tokens) < 6 or not re.fullmatch(r"\d+", tokens[0]):
        return False

    normalized = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
    if any(term in normalized for term in ("total", "taxable", "round off", "grand total", "net total", "phone", "mobile", "customer")):
        return False

    hsn_index = _find_hsn_token_index(tokens, start_index=1)
    if hsn_index is None:
        return False

    uom_index = _find_uom_token_index(tokens, start_index=hsn_index + 1)
    if uom_index is None:
        return False

    numeric_count = len(re.findall(r"[-+]?\d[\d,]*\.?\d*", line))
    return numeric_count >= 5


def _has_item_rows_ahead(lines: list[str]) -> bool:
    return any(_looks_like_item_row(line) for line in lines)


def _strip_trailing_supplier_uom(description: str) -> str:
    cleaned = re.sub(r"\s+", " ", description).strip()
    tokens = cleaned.split()
    if not tokens:
        return cleaned

    known_uoms = {
        "pcs",
        "pc",
        "psc",
        "box",
        "pac",
        "pack",
        "pkt",
        "kgs",
        "kg",
        "nos",
        "num",
        "no",
        "mtr",
        "mt",
        "ltr",
        "lt",
        "l",
    }
    while tokens:
        tail = tokens[-1].lower().strip(".,;:/()[]{}")
        if tail in known_uoms:
            tokens.pop()
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?(?:ltr|lt|l|kg|kgs|pc|pcs|psc|box|pkt|pac|mtr)", tail):
            tokens.pop()
            continue
        break
    return " ".join(tokens).strip()


def _find_uom_token_index(tokens: list[str], start_index: int = 0) -> int | None:
    known_uoms = {
        "pcs",
        "pc",
        "psc",
        "box",
        "pac",
        "pack",
        "pkt",
        "kgs",
        "kg",
        "nos",
        "num",
        "no",
        "mtr",
        "mt",
        "ltr",
        "lt",
        "l",
    }
    for index in range(start_index, len(tokens)):
        cleaned = tokens[index].lower().strip(".,;:/()[]{}")
        if cleaned in known_uoms:
            return index
    return None


def _find_hsn_token_index(tokens: list[str], start_index: int = 0) -> int | None:
    fallback = None
    for index in range(start_index, len(tokens)):
        cleaned = tokens[index].strip(".,;:/()[]{}")
        if re.fullmatch(r"\d{6,8}", cleaned):
            return index
        if fallback is None and re.fullmatch(r"\d{4,5}", cleaned):
            fallback = index
    return fallback


def _parse_numeric_token(value: str) -> float:
    try:
        return flt(str(value).replace(",", ""))
    except Exception:
        return 0.0


def _template_patterns(template: dict[str, Any] | None, target_field: str) -> list[str]:
    return [rf"{pattern}\s*[:\-]?\s*(.+)" for pattern in _template_label_patterns(template, target_field)]


def _template_label_patterns(template: dict[str, Any] | None, target_field: str) -> list[str]:
    if not template:
        return []

    patterns: list[str] = []
    for row in template.get("field_mappings") or []:
        if str(row.get("target_field") or "").strip() != target_field:
            continue
        source_label = str(row.get("source_label") or "").strip()
        if source_label:
            patterns.append(re.escape(source_label))
    return patterns
