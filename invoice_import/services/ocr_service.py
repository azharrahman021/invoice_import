from __future__ import annotations

import os
import tempfile
from pathlib import Path

import frappe

from invoice_import.services.file_utils import is_pdf
from invoice_import.services.image_preprocessor import preprocess_image
from invoice_import.services.types import OCRResult


def extract_ocr(file_path: str) -> OCRResult:
    """Extract text from a digital PDF, scanned PDF, or image."""
    if os.getenv("GOOGLE_DOCUMENTAI_PROCESSOR"):
        google_result = _try_google_document_ai(file_path)
        if google_result.text.strip():
            return google_result

    if is_pdf(file_path):
        text = _extract_digital_pdf_text(file_path)
        if len(text.strip()) > 80:
            return OCRResult(
                text=text,
                provider="local-pdf",
                confidence=0.82,
                pages=_count_pdf_pages(file_path),
            )
        return _extract_scanned_pdf(file_path)

    return _extract_image_with_tesseract(file_path)


def _extract_digital_pdf_text(file_path: str) -> str:
    try:
        import fitz
    except Exception:
        fitz = None

    try:
        import pdfplumber
    except Exception as exc:
        frappe.log_error(f"pdfplumber unavailable: {exc}", "Invoice Import OCR")
        return ""

    chunks: list[str] = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            if page_text.strip():
                chunks.append(page_text)
            elif fitz is not None:
                try:
                    with fitz.open(file_path) as fitz_doc:
                        chunks.append(fitz_doc[page.page_number - 1].get_text("text") or "")
                except Exception:
                    pass
    return "\n".join(chunks)


def _extract_scanned_pdf(file_path: str) -> OCRResult:
    try:
        import fitz
    except Exception as exc:
        frappe.throw(frappe._("PyMuPDF is required for scanned PDF OCR: {0}").format(exc))

    doc = fitz.open(file_path)
    page_text: list[str] = []
    for page_number, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image_path = Path(tempfile.gettempdir()) / f"invoice_import_page_{os.getpid()}_{page_number}.png"
        pix.save(str(image_path))
        page_text.append(_extract_image_with_tesseract(str(image_path)).text)
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass
    return OCRResult(text="\n\n".join(page_text), provider="pymupdf+tesseract", confidence=0.68, pages=len(doc))


def _extract_image_with_tesseract(file_path: str) -> OCRResult:
    try:
        import pytesseract
        from PIL import Image
    except Exception as exc:
        frappe.throw(frappe._("Tesseract OCR dependencies are not installed: {0}").format(exc))

    processed_path = preprocess_image(file_path)
    with Image.open(processed_path) as image:
        text = pytesseract.image_to_string(image, config="--oem 3 --psm 6")
    return OCRResult(text=text, provider="tesseract", confidence=0.62)


def _count_pdf_pages(file_path: str) -> int:
    try:
        import fitz

        with fitz.open(file_path) as doc:
            return len(doc)
    except Exception:
        return 1


def _try_google_document_ai(file_path: str) -> OCRResult:
    try:
        from google.cloud import documentai
    except Exception:
        return OCRResult(text="", provider="google-document-ai-unavailable", confidence=0)

    processor_name = os.getenv("GOOGLE_DOCUMENTAI_PROCESSOR", "")
    if not processor_name:
        return OCRResult(text="", provider="google-document-ai-unconfigured", confidence=0)

    with open(file_path, "rb") as handle:
        content = handle.read()

    mime_type = "application/pdf" if is_pdf(file_path) else "image/png"
    client = documentai.DocumentProcessorServiceClient()
    request = documentai.ProcessRequest(
        name=processor_name,
        raw_document=documentai.RawDocument(content=content, mime_type=mime_type),
    )
    result = client.process_document(request=request)
    confidence = 0.85
    pages = len(result.document.pages) or 1
    return OCRResult(text=result.document.text or "", provider="google-document-ai", confidence=confidence, pages=pages)
