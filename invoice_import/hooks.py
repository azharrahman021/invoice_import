from . import __version__ as app_version

app_name = "invoice_import"
app_title = "Invoice Import"
app_publisher = "ERPNext Invoice Import"
app_description = "AI assisted invoice OCR importer for ERPNext Purchase Invoices"
app_email = "admin@example.com"
app_license = "MIT"

required_apps = ["frappe", "erpnext"]

doctype_js = {
    "Invoice Import": "public/js/invoice_import.js",
    "Supplier Invoice Template": "public/js/supplier_invoice_template.js",
}

scheduler_events = {
    "hourly": [
        "invoice_import.jobs.invoice_import_jobs.retry_stale_imports",
    ],
}

doc_events = {
    "Invoice Import": {
        "after_insert": "invoice_import.jobs.invoice_import_jobs.enqueue_invoice_import",
        "on_update": "invoice_import.jobs.invoice_import_jobs.enqueue_invoice_import",
    },
    "Purchase Invoice": {
        "on_submit": "invoice_import.services.learning.learn_from_purchase_invoice",
    },
}
