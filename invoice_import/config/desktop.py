from frappe import _


def get_data():
    return [
        {
            "module_name": "Invoice Import",
            "type": "module",
            "label": _("Invoice Import"),
            "color": "blue",
            "icon": "octicon octicon-file",
        }
    ]
