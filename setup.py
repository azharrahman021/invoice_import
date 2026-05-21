from setuptools import find_packages, setup

from invoice_import import __version__ as version


setup(
    name="invoice_import",
    version=version,
    description="AI assisted invoice OCR importer for ERPNext Purchase Invoices",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
)
