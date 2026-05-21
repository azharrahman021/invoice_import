INVOICE_EXTRACTION_SYSTEM_PROMPT = (
    "You are an invoice extraction engine. Extract structured purchase invoice data from this OCR text "
    "or invoice image. Return only valid JSON. Preserve numerical accuracy. Identify taxes separately. "
    "Detect line items carefully even from noisy OCR."
)

INVOICE_EXTRACTION_JSON_SCHEMA = {
    "name": "purchase_invoice_extraction",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "supplier": {"type": "string"},
            "supplier_gstin": {"type": "string"},
            "supplier_vat": {"type": "string"},
            "supplier_address": {"type": "string"},
            "supplier_phone": {"type": "string"},
            "supplier_email": {"type": "string"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string"},
            "due_date": {"type": "string"},
            "currency": {"type": "string"},
            "po_number": {"type": "string"},
            "payment_terms": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "description": {"type": "string"},
                        "qty": {"type": "number"},
                        "uom": {"type": "string"},
                        "rate": {"type": "number"},
                        "amount": {"type": "number"},
                        "tax_percent": {"type": "number"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["description", "qty", "rate", "amount", "tax_percent", "confidence"],
                },
            },
            "subtotal": {"type": "number"},
            "taxes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "description": {"type": "string"},
                        "rate": {"type": "number"},
                        "amount": {"type": "number"},
                        "account_head": {"type": "string"},
                    },
                    "required": ["description", "rate", "amount"],
                },
            },
            "grand_total": {"type": "number"},
            "confidence": {"type": "number"},
            "field_confidence": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "supplier",
            "invoice_number",
            "invoice_date",
            "currency",
            "items",
            "subtotal",
            "taxes",
            "grand_total",
            "confidence",
            "warnings",
        ],
    },
}
