from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import flt

from invoice_import.services.file_utils import is_pdf


ITEM_FIELD_TARGETS = {
    "item_description",
    "item_qty",
    "item_uom",
    "item_hsn_sac",
    "item_rate",
    "item_amount",
    "item_mrp",
}

KNOWN_UOMS = {
    "pcs",
    "pc",
    "psc",
    "pac",
    "pack",
    "pkt",
    "box",
    "kg",
    "kgs",
    "nos",
    "num",
    "mtr",
    "mt",
    "ltr",
    "lt",
    "l",
}

HEADER_FIELD_ALIASES = {
    "bill_no": "invoice_number",
    "po_no": "po_number",
    "po_number": "po_number",
    "total": "grand_total",
}


def extract_template_regions(file_path: str, template: dict[str, Any] | None) -> dict[str, Any]:
    if not template or not template.get("field_mappings") or not is_pdf(file_path):
        return {}

    try:
        import pdfplumber
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"pdfplumber unavailable for template extraction: {exc}")
        return {}

    extracted: dict[str, Any] = {}
    item_pages: dict[int, list[dict[str, Any]]] = defaultdict(list)

    try:
        with pdfplumber.open(file_path) as pdf:
            page_text_cache: dict[int, list[str]] = {}
            for row in template.get("field_mappings") or []:
                target_field = _normalize_target_field(str(row.get("target_field") or "").strip())
                target_field = _canonical_item_target(target_field)
                page_number = int(row.get("page_number") or 1)
                if page_number < 1 or page_number > len(pdf.pages):
                    continue
                page = pdf.pages[page_number - 1]
                source_label = str(row.get("source_label") or "").strip()
                value_hint = str(row.get("value_hint") or "").strip()
                region = _parse_region_json(row.get("region_json"))
                lines = page_text_cache.get(page_number)
                if lines is None:
                    lines = _page_text_lines(page)
                    page_text_cache[page_number] = lines

                if target_field in ITEM_FIELD_TARGETS:
                    item_pages[page_number].append(
                        {
                            "target_field": target_field,
                            "source_label": source_label,
                            "value_hint": value_hint,
                            "region": region,
                        }
                    )
                    continue

                value = ""
                if source_label:
                    value = _extract_value_from_lines(lines, source_label, value_hint=value_hint, target_field=target_field)
                if not value and region:
                    bbox = _region_to_bbox(region, page.width, page.height)
                    if bbox:
                        value = _extract_text_from_bbox(page, bbox)
                if value:
                    extracted[target_field] = value

            item_rows = []
            if item_pages:
                profile = _item_table_profile(template)
                # Scan every page once items are configured so later pages are not missed.
                for page_number, page in enumerate(pdf.pages, start=1):
                    rows = item_pages.get(page_number) or next(iter(item_pages.values()))
                    item_rows.extend(_extract_items_from_text(page, rows, page_number, profile))
            if item_rows:
                extracted["items"] = item_rows
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"Template region extraction failed: {exc}")

    return extracted


def _extract_items_from_text(
    page,
    rows: list[dict[str, Any]],
    page_number: int = 1,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not rows:
        return []

    lines = _page_text_lines(page)
    start_index = _find_item_table_start_index(lines, rows, profile)
    item_lines = _guess_item_lines(lines[start_index:] if start_index < len(lines) else lines)
    return [_parse_item_line(line, index) for index, line in enumerate(item_lines, start=1)]


def _normalize_item_row(row: dict[str, Any]) -> dict[str, Any] | None:
    item_code = _normalize_text(row.get("item_description") or "")
    description = item_code
    qty = _parse_float(row.get("item_qty"))
    rate = _parse_float(row.get("item_rate"))
    amount = _parse_float(row.get("item_amount"))
    uom = _normalize_text(row.get("item_uom") or "")
    hsn_sac = _normalize_text(row.get("item_hsn_sac") or "")
    mrp = _parse_float(row.get("item_mrp"))

    if not any([item_code, description, qty, rate, amount, uom, hsn_sac, mrp]):
        return None

    return {
        "item_code": item_code or description or "Unidentified item",
        "item_name": item_code or description or "Unidentified item",
        "description": description or item_code or "Unidentified item",
        "source_qty": qty or 1,
        "source_uom": uom or "Nos",
        "accepted_qty": qty or 1,
        "qty": qty or 1,
        "uom": uom or "Nos",
        "hsn_sac": hsn_sac,
        "rate": rate,
        "price_list_rate": rate,
        "amount": amount,
        "mrp": mrp,
        "tax_percent": 0,
        "confidence": 0.78 if item_code or description else 0.6,
    }


def _page_text_lines(page) -> list[str]:
    try:
        text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
    except Exception:
        text = ""
    if not text.strip():
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _extract_value_from_lines(lines: list[str], source_label: str, value_hint: str = "", target_field: str = "") -> str:
    labels = _split_source_labels(source_label)
    if not labels:
        return ""

    hint = _normalize_text(value_hint)
    normalized_lines = [line.strip() for line in lines if line.strip()]
    for index, line in enumerate(normalized_lines):
        compact = _normalize_header_line(line)
        for label in labels:
            label_norm = _normalize_header_line(label)
            if not label_norm:
                continue
            if target_field == "invoice_date" and label_norm == "date" and "ack date" in compact:
                continue
            if target_field == "grand_total" and re.match(r"^\d+\s+\S", compact) and len(re.findall(r"\d[\d,]*\.?\d*", line)) >= 4:
                continue
            if label_norm in compact:
                value = _extract_inline_label_value(line, label)
                if value:
                    if "gstin" in label_norm:
                        gstin = re.search(r"\b[0-9A-Z]{15}\b", value, flags=re.I)
                        if gstin:
                            return gstin.group(0)
                    return value
                if index + 1 < len(normalized_lines):
                    next_line = _normalize_text(normalized_lines[index + 1])
                    if next_line and _normalize_header_line(next_line) not in {label_norm, "tax invoice credit", "tax invoice"}:
                        if "gstin" in label_norm:
                            gstin = re.search(r"\b[0-9A-Z]{15}\b", next_line, flags=re.I)
                            if gstin:
                                return gstin.group(0)
                        return next_line
                if hint:
                    return hint
    return ""


def _extract_inline_label_value(line: str, label: str) -> str:
    if not line or not label:
        return ""
    pattern = re.compile(rf"{re.escape(label)}\s*[:\-]?\s*(.+)$", flags=re.I)
    match = pattern.search(line)
    if match:
        return _normalize_text(match.group(1))
    compact = _normalize_header_line(line)
    label_norm = _normalize_header_line(label)
    if compact.startswith(label_norm):
        remainder = line[len(label):].lstrip(" :-\t")
        return _normalize_text(remainder)
    return ""


def _find_item_table_start_index(
    lines: list[str],
    mappings: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> int:
    labels = []
    for row in mappings:
        labels.extend(_split_source_labels(str(row.get("source_label") or "")))
    labels = [label for label in labels if label]
    if not labels:
        labels = ["item", "description", "qty", "uom", "hsn", "rate", "amount"]

    for index, line in enumerate(lines):
        compact = _normalize_header_line(line)
        if _looks_like_profile_header(compact, profile):
            if _has_item_rows_ahead(lines[index + 1 : index + 6]):
                return index + 1
        if _looks_like_item_header(compact):
            if _has_item_rows_ahead(lines[index + 1 : index + 6]):
                return index + 1
        hits = sum(1 for label in labels if _normalize_header_line(label) in compact)
        if hits >= 2 and any(token in compact for token in ("qty", "description", "rate", "amount")):
            if _has_item_rows_ahead(lines[index + 1 : index + 6]):
                return index + 1
        if "description" in compact and "qty" in compact and ("rate" in compact or "amount" in compact):
            if _has_item_rows_ahead(lines[index + 1 : index + 6]):
                return index + 1
    return 0


def _looks_like_item_header(compact: str) -> bool:
    if not compact:
        return False
    compact = compact.lower()
    return (
        "description" in compact
        and "qty" in compact
        and ("rate" in compact or "amount" in compact)
        and ("hsn" in compact or "uom" in compact or "unit" in compact or "tax" in compact)
    )


def _looks_like_profile_header(compact: str, profile: dict[str, Any] | None) -> bool:
    tokens = (profile or {}).get("header_tokens") or []
    if not compact or not tokens:
        return False
    hits = sum(1 for token in tokens if token in compact)
    return hits >= min(4, len(tokens))


def _has_item_rows_ahead(lines: list[str]) -> bool:
    return any(_looks_like_item_row(line) for line in lines)


def _item_table_profile(template: dict[str, Any] | None) -> dict[str, Any]:
    header = str((template or {}).get("item_table_header") or "")
    column_order = str((template or {}).get("item_column_order") or "")
    header_tokens = [
        token
        for token in _normalize_header_line(header).split()
        if token and token not in {"no", "sr", "sl"}
    ]
    columns = [
        _normalize_header_line(column)
        for column in re.split(r"[,|\n]+", column_order)
        if _normalize_header_line(column)
    ]
    return {"header_tokens": header_tokens, "columns": columns}


def _split_source_labels(value: str) -> list[str]:
    parts = re.split(r"[|\n]+", str(value or ""))
    return [_normalize_text(part) for part in parts if _normalize_text(part)]


def _parse_region_json(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        region = value if isinstance(value, dict) else json.loads(str(value))
    except Exception:
        return None
    if not isinstance(region, dict):
        return None
    if region.get("unit") != "percent":
        return None
    return region


def _region_to_bbox(region: dict[str, Any], page_width: float, page_height: float) -> tuple[float, float, float, float] | None:
    try:
        x = float(region.get("x") or 0)
        y = float(region.get("y") or 0)
        width = float(region.get("width") or 0)
        height = float(region.get("height") or 0)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    x0 = max(0.0, page_width * (x / 100.0))
    y0 = max(0.0, page_height * (y / 100.0))
    x1 = min(page_width, page_width * ((x + width) / 100.0))
    y1 = min(page_height, page_height * ((y + height) / 100.0))
    return x0, y0, x1, y1


def _extract_text_from_bbox(page, bbox: tuple[float, float, float, float]) -> str:
    try:
        crop = page.crop(bbox)
        text = crop.extract_text(x_tolerance=1, y_tolerance=3) or ""
        return _normalize_text(text)
    except Exception:
        return ""


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_header_line(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _normalize_text(value).lower()).strip()


def _normalize_target_field(target_field: str) -> str:
    return HEADER_FIELD_ALIASES.get(target_field, target_field)


def _canonical_item_target(target_field: str) -> str:
    return {
        "items.item_code": "item_description",
        "items.description": "item_description",
        "items.qty": "item_qty",
        "items.uom": "item_uom",
        "items.hsn_sac": "item_hsn_sac",
        "items.rate": "item_rate",
        "items.amount": "item_amount",
        "items.mrp": "item_mrp",
    }.get(target_field, target_field)


def _guess_item_lines(lines: list[str]) -> list[str]:
    candidates: list[str] = []
    for line in lines:
        if _looks_like_item_row(line):
            candidates.append(line)
        elif candidates and re.search(r"total|taxable|round off|amount", line, re.I):
            break
    return candidates[:100]


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

    rate = 0.0
    qty = 1.0
    amount = numbers[-1] if numbers else 0.0
    after_uom_numbers: list[float] = []
    if uom_index is not None:
        qty_index = uom_index - 1
        if qty_index > (hsn_index if hsn_index is not None else offset):
            qty = _parse_float(tokens[qty_index]) or qty
        after_uom_numbers = [
            _parse_float(token)
            for token in tokens[uom_index + 1 :]
            if _parse_float(token) or re.fullmatch(r"0+(?:\.0+)?", token)
        ]
        if len(after_uom_numbers) >= 2:
            rate = after_uom_numbers[0] or rate
            if qty == 1.0:
                qty = after_uom_numbers[1] or qty
            amount = after_uom_numbers[-1] or amount
        elif len(after_uom_numbers) == 1:
            rate = after_uom_numbers[0] or rate
        elif qty == 1.0 and len(numbers) >= 2:
            qty = numbers[-3] if len(numbers) >= 3 else numbers[-2]
            rate = numbers[-4] if len(numbers) >= 4 else numbers[-2]
    elif numbers:
        qty = numbers[1] if len(numbers) > 1 else numbers[0]
        rate = numbers[-2] if len(numbers) >= 2 else 0.0

    description = _extract_item_description(tokens, offset=offset, hsn_index=hsn_index, uom_index=uom_index)
    hsn_sac = tokens[hsn_index] if hsn_index is not None else ""
    return {
        "item_code": "",
        "item_name": "",
        "description": description or f"OCR item line {index}",
        "source_qty": qty,
        "source_uom": tokens[uom_index] if uom_index is not None else "Nos",
        "accepted_qty": qty,
        "qty": qty,
        "uom": "Nos",
        "hsn_sac": hsn_sac,
        "rate": rate,
        "price_list_rate": rate,
        "amount": amount,
        "mrp": 0,
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
        return after_text
    if before_text:
        return before_text
    if after_text:
        return after_text
    return ""


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


def _parse_float(value: Any) -> float:
    try:
        return flt(str(value or "").replace(",", ""))
    except Exception:
        return 0.0


def _find_uom_token_index(tokens: list[str], start_index: int = 0) -> int | None:
    for index in range(start_index, len(tokens)):
        cleaned = tokens[index].lower().strip(".,;:/()[]{}")
        if cleaned in KNOWN_UOMS:
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
