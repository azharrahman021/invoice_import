# Invoice Import

Production-oriented ERPNext/Frappe app scaffold for creating Draft Purchase Invoices from invoice images and PDFs.

## Features

- `Invoice Import` DocType with attachment, status, extracted JSON, confidence, linked Purchase Invoice, and processing logs.
- Background processing with retries through Frappe queues.
- OCR pipeline for digital PDFs, scanned PDFs, and images.
- Optional OpenAI Vision/structured parsing, optional Google Document AI, and Tesseract fallback.
- Supplier matching by GSTIN/VAT, exact name, and fuzzy name.
- Item matching by exact item fields and fuzzy/semantic description scoring.
- Duplicate protection using supplier, invoice number, and grand total.
- Draft Purchase Invoice creation only. No automatic submit.
- Desk form review helpers for side-by-side attachment preview and extracted JSON editing.

## Install

From the bench root:

```bash
bench get-app /path/to/invoice_import
bench --site your.site.name install-app invoice_import
bench --site your.site.name migrate
bench restart
```

For this local scaffold already under `apps/invoice_import`:

```bash
bench --site your.site.name install-app invoice_import
bench --site your.site.name migrate
bench restart
```

## System Dependencies

Install OCR/PDF tools on the server:

```bash
sudo apt-get install -y tesseract-ocr poppler-utils
```

Python dependencies are declared in `pyproject.toml`. If needed:

```bash
./env/bin/pip install -e apps/invoice_import
```

## Environment Variables

No credentials are stored in code.

```bash
export OPENAI_API_KEY="..."
export INVOICE_IMPORT_OPENAI_MODEL="gpt-4.1-mini"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
export GOOGLE_DOCUMENTAI_PROCESSOR="projects/.../locations/.../processors/..."
export INVOICE_IMPORT_MAX_UPLOAD_MB="20"
export INVOICE_IMPORT_AUTO_CREATE_SUPPLIER="0"
export INVOICE_IMPORT_SUPPLIER_THRESHOLD="86"
export INVOICE_IMPORT_ITEM_THRESHOLD="82"
```

## Workflow

1. Create an `Invoice Import` document and attach a JPG, PNG, HEIC, or PDF.
2. The document queues background processing after save.
3. OCR extracts raw text and page images where needed.
4. AI parser returns normalized JSON with confidence metadata.
5. Supplier, item, tax, and duplicate checks run.
6. A Draft Purchase Invoice is created and linked.
7. User reviews the original file and extracted fields before submitting the Purchase Invoice.

## AI Prompt

The internal extraction prompt is stored in `invoice_import/services/prompts.py` and begins:

> You are an invoice extraction engine. Extract structured purchase invoice data from this OCR text or invoice image. Return only valid JSON. Preserve numerical accuracy. Identify taxes separately. Detect line items carefully even from noisy OCR.

## Future Architecture

The service boundaries intentionally leave room for:

- multi-language invoices through provider locale hints and language-specific OCR packs
- handwritten invoices through a vision provider adapter
- supplier layout templates keyed by supplier and GSTIN/VAT
- PO/GRN matching before Purchase Invoice creation
- email and WhatsApp ingestion creating `Invoice Import` records
- vector-based supplier/item memory by adding embeddings to matcher services
- confidence heatmaps by storing OCR bounding boxes in `extracted_json`
