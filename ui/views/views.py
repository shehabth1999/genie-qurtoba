# -*- coding: utf-8 -*-
from django.utils.translation import gettext as _

# ---------------------------------------------------------------------------
# Choice lists re-used across multiple views
# ---------------------------------------------------------------------------

_DEBT_OPTIONS = [
    {"value": "كاش",          "label": "كاش"},
    {"value": "كاش(5)",        "label": "كاش(5)"},
    {"value": "كاش(10)",       "label": "كاش(10)"},
    {"value": "كاش(20)",       "label": "كاش(20)"},
    {"value": "فورى",         "label": "فورى"},
    {"value": "أمان",         "label": "أمان"},
    {"value": "طاير",         "label": "طاير"},
    {"value": "مصاريف خدمه", "label": "مصاريف خدمه"},
]

# Display only — includes all types Qurtoba may push (used in list views and general record form)
_COLLECTION_OPTIONS = [
    {"value": "تحصيل",     "label": "تحصيل"},    # collector app only — never in Genie create forms
    {"value": "شراء كاش",  "label": "شراء كاش"},
    {"value": "شراء فورى", "label": "شراء فورى"},
]

_ALL_TYPE_OPTIONS = _DEBT_OPTIONS + _COLLECTION_OPTIONS + [
    {"value": "مندوب", "label": "مندوب"},         # collector-dues API only — never in Genie create forms
]

# Create forms only — customer self-payment types that Genie accountant can enter manually
_CUSTOMER_PAY_OPTIONS = [
    {"value": "شراء كاش",  "label": "شراء كاش"},
    {"value": "شراء فورى", "label": "شراء فورى"},
]

# ---------------------------------------------------------------------------
# Customer list view
# ---------------------------------------------------------------------------

qurtoba_customer_list_view = {
    "key": "qurtoba_customer_list_view",
    "name": _("Qurtoba Customers"),
    "model": "qurtoba.qurtobacustomer",
    "menu_item": "qurtoba_menu_customers",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "header": {
            "actions": [
                {
                    "name": "action_sync_customers",
                    "string": _("مزامنة العملاء"),
                    "icon": "RefreshCw",
                    "type": "server",
                    "as": "button",
                    "variant": "primary",
                    "selection_required": False,
                },
                {
                    "name": "action_sync_accounts",
                    "string": _("مزامنة الحسابات"),
                    "icon": "ListChecks",
                    "type": "server",
                    "as": "button",
                    "variant": "secondary",
                    "selection_required": False,
                },
            ]
        },
        "tree": {
            "fields": [
                {"name": "qurtoba_id", "widget": "number",   "string": _("Customer ID")},
                {"name": "name",       "widget": "text",     "string": _("Name")},
                {"name": "phone_no",   "widget": "text",     "string": _("Phone")},
                {"name": "area",       "widget": "text",     "string": _("Area")},
                {"name": "shop_kind",  "widget": "text",     "string": _("Shop Kind")},
                {"name": "grade",      "widget": "number",   "string": _("Grade")},
                {"name": "device_no",  "widget": "number",   "string": _("Device No")},
                {"name": "balance",    "widget": "number",   "string": _("مديونيات")},
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# Customer form view
# ---------------------------------------------------------------------------

qurtoba_customer_form_view = {
    "key": "qurtoba_customer_form_view",
    "name": _("Qurtoba Customer"),
    "model": "qurtoba.qurtobacustomer",
    "menu_item": "qurtoba_menu_customers",
    "view_type": "form",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "header": {
            "actions_list": [
                {
                    "string": _("الشركاء"),
                    "icon": "Users",
                    "color": "info",
                    "model": "base.partner",
                    "menu_item_key": "contacts_main_menu_partner",
                    "relation_field": "qurtoba_customer",
                    "aggregation": "count",
                    "number_format": "number",
                    "context": {
                        "default_fields": {
                            "qurtoba_customer": "active_id",
                        }
                    },
                },
            ],
            "actions": [
                {
                    "name": "action_new_transaction",
                    "string": _("مديونية جديدة"),
                    "icon": "FilePlus",
                    "type": "server",
                    "as": "button",
                    "variant": "warning",
                    "selection_required": False,
                },
                {
                    "name": "action_new_collection",
                    "string": _("تسجيل تحصيل"),
                    "icon": "CheckCircle",
                    "type": "server",
                    "as": "button",
                    "variant": "success",
                    "selection_required": False,
                },
                {
                    "name": "action_update_balance",
                    "string": _("تحديث المديونية"),
                    "icon": "RefreshCw",
                    "type": "server",
                    "as": "button",
                    "variant": "primary",
                    "selection_required": False,
                },
            ],
        },
        "sheet": {
            "title": {
                "fields": [
                    {"name": "name",    "widget": "text",   "string": _("Name"), "onChange": True},
                    {"name": "balance", "widget": "number", "string": _("مديونيات"), "readonly": True},
                ]
            },
            "sections": [
                {
                    "title": _(""),
                    "groups": [
                        {
                            "fields": [
                                {"name": "qurtoba_id", "widget": "number", "string": _("Customer ID (Qurtoba)"), "readonly": True},
                                {"name": "phone_no",   "widget": "text",   "string": _("Phone")},
                                {"name": "device_no",  "widget": "number", "string": _("Device No"),            "readonly": True},
                                {"name": "grade",      "widget": "number", "string": _("Grade")},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "shop_name", "widget": "text", "string": _("Shop Name")},
                                {"name": "shop_kind", "widget": "text", "string": _("Shop Kind")},
                                {"name": "area",      "widget": "text", "string": _("Area")},
                                {"name": "sur_name",  "widget": "text", "string": _("Sur Name")},
                            ]
                        },
                    ]
                },
            ],
            "tabs": [
                {
                    "title": _("Financial Info"),
                    "sections": [
                        {
                            "title": _(""),
                            "groups": [
                                {
                                    "fullWidth": True,
                                    "fields": [
                                        {"name": "accounts",      "widget": "textarea", "string": _("Accounts")},
                                    ]
                                },
                                {
                                    "fullWidth": True,
                                    "fields": [
                                        {"name": "accounts_data", "widget": "textarea", "string": _("Accounts Data")},
                                    ]
                                },
                                {
                                    "fields": [
                                        {"name": "address", "widget": "text", "string": _("Address")},
                                    ]
                                },
                            ]
                        }
                    ]
                },
                {
                    "title": _("Notes"),
                    "sections": [
                        {
                            "title": _(""),
                            "groups": [
                                {
                                    "fields": [
                                        {"name": "notes",      "widget": "text", "string": _("Notes")},
                                        {"name": "notes_plus", "widget": "text", "string": _("Notes Plus")},
                                    ]
                                },
                            ]
                        }
                    ]
                },
                {
                    "title": _("Partners"),
                    "sections": [
                        {
                            "title": _(""),
                            "groups": [
                                {
                                    "fullWidth": True,
                                    "fields": [
                                        {
                                            "name": "partners",
                                            "widget": "relation",
                                            "string": _("Partners"),
                                            "displayField": "name",
                                            "multiSelect": True,
                                            "action": "slideover",
                                            "creatable": True,
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# All Records list view  (general — shows everything)
# ---------------------------------------------------------------------------

qurtoba_record_list_view = {
    "key": "qurtoba_record_list_view",
    "name": _("Qurtoba Records"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_menu_records",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "tree": {
            "fields": [
                {"name": "type",                     "widget": "select",   "string": _("Type"),     "options": _ALL_TYPE_OPTIONS},
                {"name": "customer",                 "widget": "relation", "string": _("Customer"), "displayField": "name"},
                {"name": "customer_data_qurtoba_id", "widget": "number",   "string": _("Customer ID (Qurtoba)")},
                {"name": "value",                    "widget": "number",   "string": _("Amount")},
                {"name": "rest",                     "widget": "number",   "string": _("Rest")},
                {"name": "is_done",                  "widget": "checkbox", "string": _("Done")},
                {"name": "is_down",                  "widget": "checkbox", "string": _("Payment")},
                {"name": "date",                     "widget": "date",     "string": _("Date")},
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# All Records form view  (general — view / edit any record)
# ---------------------------------------------------------------------------

qurtoba_record_form_view = {
    "key": "qurtoba_record_form_view",
    "name": _("Record"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_menu_records",
    "view_type": "form",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "sheet": {
            "title": {
                "fields": [
                    {"name": "type",  "widget": "select", "string": _("Type"),   "options": _ALL_TYPE_OPTIONS},
                    {"name": "value", "widget": "number", "string": _("Amount")},
                    {"name": "partner", "widget": "relation", "string": _(""), "model": "base.partner", "invisible": True},
                ]
            },
            "sections": [
                {
                    "title": _(""),
                    "groups": [
                        {
                            "fields": [
                                {"name": "customer",                 "widget": "relation", "string": _("Customer"),               "displayField": "name"},
                                {"name": "customer_data_qurtoba_id", "widget": "number",   "string": _("Customer ID (Qurtoba)"), "readonly": True},
                                {"name": "account_number",           "widget": "text",     "string": _("Account Number")},
                                {"name": "date",                     "widget": "date",     "string": _("Date")},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "rest",      "widget": "number",   "string": _("Rest")},
                                {"name": "is_done",   "widget": "checkbox", "string": _("Done")},
                                {"name": "is_down",   "widget": "checkbox", "string": _("Is Payment")},
                                {"name": "is_seller", "widget": "checkbox", "string": _("Collector Side")},
                            ]
                        },
                    ]
                },
                {
                    "title": _("Notes"),
                    "groups": [
                        {
                            "fullWidth": True,
                            "fields": [
                                {"name": "notes", "widget": "text", "string": _("Notes")},
                            ]
                        }
                    ]
                },
                {
                    "title": _("إرسال لقرطبة"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "qurtoba_synced",    "widget": "checkbox", "string": _("تم الإرسال"),  "readonly": True},
                                {"name": "qurtoba_posted_at", "widget": "datetime", "string": _("وقت الإرسال"), "readonly": True},
                                {"name": "qurtoba_record_id", "widget": "number",   "string": _("معرف قرطبة"),  "readonly": True},
                            ]
                        },
                        {
                            "fields": [
                                {
                                    "name": "qurtoba_sync_error",
                                    "widget": "text",
                                    "string": _("خطأ الإرسال"),
                                    "readonly": True,
                                    "invisible": {"field": "qurtoba_sync_error", "operator": "is_null"},
                                },
                            ]
                        },
                    ],
                },
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# Debt Records list view  (isDown=False — adds to customer balance)
# ---------------------------------------------------------------------------

qurtoba_debt_list_view = {
    "key": "qurtoba_debt_list_view",
    "name": _("Debt Records"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_menu_debt",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "header": {
            "actions": [
                {
                    "name": "action_new_debt",
                    "string": _("تسجيل مديونية"),
                    "icon": "Plus",
                    "type": "server",
                    "as": "button",
                    "variant": "primary",
                    "selection_required": False,
                },
            ]
        },
        "tree": {
            "fields": [
                {"name": "type",     "widget": "select",   "string": _("Type"),     "options": _DEBT_OPTIONS},
                {"name": "customer", "widget": "relation", "string": _("Customer"), "displayField": "name"},
                {"name": "value",    "widget": "number",   "string": _("Amount")},
                {"name": "date",     "widget": "date",     "string": _("Date")},
                {"name": "is_done",  "widget": "checkbox", "string": _("Done")},
                {"name": "account_number", "widget": "text", "string": _("Account No")},
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# Debt creation form  (isDown=False, debt types only)
# ---------------------------------------------------------------------------

qurtoba_debt_form_view = {
    "key": "qurtoba_debt_form_view",
    "name": _("تسجيل مديونية"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_menu_debt",
    "view_type": "form",
    "priority": 5,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "sheet": {
            "title": {
                "fields": [
                    {"name": "type",  "widget": "select", "string": _("Type"),   "options": _DEBT_OPTIONS, "required": True},
                    {"name": "value", "widget": "number", "string": _("Amount"), "required": True},
                    {"name": "partner", "widget": "relation", "string": _(""), "model": "base.partner", "invisible": True},
                ]
            },
            "sections": [
                {
                    "title": _(""),
                    "groups": [
                        {
                            "fields": [
                                {"name": "customer",       "widget": "relation", "string": _("Customer"), "displayField": "name", "required": True},
                                {"name": "account_number", "widget": "text",     "string": _("Account Number")},
                                {"name": "date",           "widget": "date",     "string": _("Date"), "defaultValue": "today"},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "is_down",   "widget": "checkbox", "string": _("Is Payment"),    "defaultValue": False, "readonly": True},
                                {"name": "is_seller", "widget": "checkbox", "string": _("Collector Side"), "defaultValue": False, "readonly": True},
                                {"name": "is_done",   "widget": "checkbox", "string": _("Done"),          "defaultValue": False},
                            ]
                        },
                    ]
                },
                {
                    "title": _("Notes"),
                    "groups": [
                        {
                            "fullWidth": True,
                            "fields": [
                                {"name": "notes", "widget": "text", "string": _("Notes")},
                            ]
                        }
                    ]
                },
                {
                    "title": _("إرسال لقرطبة"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "qurtoba_synced",    "widget": "checkbox", "string": _("تم الإرسال"),  "readonly": True},
                                {"name": "qurtoba_posted_at", "widget": "datetime", "string": _("وقت الإرسال"), "readonly": True},
                                {"name": "qurtoba_record_id", "widget": "number",   "string": _("معرف قرطبة"),  "readonly": True},
                            ]
                        },
                        {
                            "fields": [
                                {
                                    "name": "qurtoba_sync_error",
                                    "widget": "text",
                                    "string": _("خطأ الإرسال"),
                                    "readonly": True,
                                    "invisible": {"field": "qurtoba_sync_error", "operator": "is_null"},
                                },
                            ]
                        },
                    ],
                },
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# Collections list view  (isDown=True, isSeller=True — collector collected)
# ---------------------------------------------------------------------------

qurtoba_collection_list_view = {
    "key": "qurtoba_collection_list_view",
    "name": _("Collections"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_menu_collections",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "header": {
            "actions": [
                {
                    "name": "action_new_collection",
                    "string": _("تسجيل تحصيل"),
                    "icon": "Plus",
                    "type": "server",
                    "as": "button",
                    "variant": "success",
                    "selection_required": False,
                },
            ]
        },
        "tree": {
            "fields": [
                {"name": "type",               "widget": "select",   "string": _("Type"),       "options": _COLLECTION_OPTIONS},
                {"name": "customer",           "widget": "relation", "string": _("Customer"),   "displayField": "name"},
                {"name": "value",              "widget": "number",   "string": _("Amount")},
                {"name": "seller_qurtoba_id",  "widget": "number",   "string": _("Collector ID")},
                {"name": "date",               "widget": "date",     "string": _("Date")},
                {"name": "is_done",            "widget": "checkbox", "string": _("Done")},
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# Collection recording form  (isDown=True, isSeller=True, type=تحصيل)
# ---------------------------------------------------------------------------

qurtoba_collection_form_view = {
    "key": "qurtoba_collection_form_view",
    "name": _("تسجيل تحصيل"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_menu_collections",
    "view_type": "form",
    "priority": 5,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "sheet": {
            "title": {
                "fields": [
                    {"name": "type",  "widget": "select", "string": _("Type"),   "options": _CUSTOMER_PAY_OPTIONS, "required": True},
                    {"name": "value", "widget": "number", "string": _("Amount"), "required": True},
                    {"name": "partner", "widget": "relation", "string": _(""), "model": "base.partner", "invisible": True},
                ]
            },
            "sections": [
                {
                    "title": _(""),
                    "groups": [
                        {
                            "fields": [
                                {"name": "customer",          "widget": "relation", "string": _("Customer"),     "displayField": "name", "required": True},
                                {"name": "seller_qurtoba_id", "widget": "number",   "string": _("Collector ID"), "required": True},
                                {"name": "date",              "widget": "date",     "string": _("Date"),         "defaultValue": "today"},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "is_down",   "widget": "checkbox", "string": _("Is Payment"),    "defaultValue": True,  "readonly": True},
                                {"name": "is_seller", "widget": "checkbox", "string": _("Collector Side"), "defaultValue": False, "readonly": True},
                                {"name": "is_done",   "widget": "checkbox", "string": _("Done"),          "defaultValue": False},
                            ]
                        },
                    ]
                },
                {
                    "title": _("Notes"),
                    "groups": [
                        {
                            "fullWidth": True,
                            "fields": [
                                {"name": "notes", "widget": "text", "string": _("Notes")},
                            ]
                        }
                    ]
                },
                {
                    "title": _("إرسال لقرطبة"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "qurtoba_synced",    "widget": "checkbox", "string": _("تم الإرسال"),  "readonly": True},
                                {"name": "qurtoba_posted_at", "widget": "datetime", "string": _("وقت الإرسال"), "readonly": True},
                                {"name": "qurtoba_record_id", "widget": "number",   "string": _("معرف قرطبة"),  "readonly": True},
                            ]
                        },
                        {
                            "fields": [
                                {
                                    "name": "qurtoba_sync_error",
                                    "widget": "text",
                                    "string": _("خطأ الإرسال"),
                                    "readonly": True,
                                    "invisible": {"field": "qurtoba_sync_error", "operator": "is_null"},
                                },
                            ]
                        },
                    ],
                },
            ]
        }
    }
}

# ---------------------------------------------------------------------------
# QUICK DEBT FORM — opened from customer form "معاملة جديدة" button.
# Only shows: customer, type (debt only), value.
# @onchange('customer') → shows credit limit toast.
# @onchange('value')    → inline error if projected balance > grade × 1000.
# ---------------------------------------------------------------------------

qurtoba_quick_debt_form_view = {
    "key": "qurtoba_quick_debt_form_view",
    "name": _("تسجيل مديونية"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_action_quick_debt",
    "view_type": "form",
    "priority": 5,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "sheet": {
            "title": {
                "fields": [
                    {
                        "name": "customer",
                        "widget": "relation",
                        "string": _("العميل"),
                        "displayField": "name",
                        "required": True,
                        "onChange": True,
                    },
                    {"name": "partner", "widget": "relation", "string": _(""), "model": "base.partner", "invisible": True},
                ]
            },
            "sections": [
                {
                    "title": _(""),
                    "groups": [
                        {
                            "fields": [
                                {
                                    "name": "type",
                                    "widget": "select",
                                    "string": _("النوع"),
                                    "options": _DEBT_OPTIONS,
                                    "required": True,
                                    "onChange": True,
                                },
                                {
                                    "name": "selected_account",
                                    "widget": "relation",
                                    "string": _("الحساب"),
                                    "model": "qurtoba.qurtobacustomeraccount",
                                    "displayField": "__str__",
                                    "invisible": {"field": "type", "operator": "in", "value": ["مصاريف خدمه","كاش", "كاش(5)", "كاش(10)", "كاش(20)"]},
                                    "required": False,
                                    "onChange": True,
                                    "help": _("اختر الحساب — يملأ النوع والرقم تلقائياً"),
                                    "domain": {
                                        "filters": {
                                            "operator": "and",
                                            "filters": [
                                                {"field": "customer", "operator": "eq", "value": "{{customer.id}}"}
                                            ]
                                        }
                                    },
                                    "context": {
                                        "default_fields": {
                                            "customer": "{{customer.id}}"
                                        }
                                    },
                                },
                                {
                                    "name": "account_number",
                                    "widget": "text",
                                    "string": _("رقم الحساب"),
                                    "readonly": {"field": "selected_account", "operator": "is_not_null"},
                                },
                                {
                                    "name": "value",
                                    "widget": "number",
                                    "string": _("المبلغ"),
                                    "required": True,
                                    "onChange": True,
                                },
                            ]
                        },
                        {
                            "fields": [
                                {"name": "is_down",   "widget": "checkbox", "string": _(""), "defaultValue": False, "invisible": True},
                                {"name": "is_seller", "widget": "checkbox", "string": _(""), "defaultValue": False, "invisible": True},
                            ]
                        },
                    ]
                },
            ],
            "footer": {
                "position": "end",
                "fields": [
                    {"name": "customer_balance", "string": _("الرصيد الحالي"),  "widget": "number", "readonly": True},
                    {"separator": "thin"},
                    {"name": "grade_limit",      "string": _("الحد الائتماني"), "widget": "number", "highlight": True, "readonly": True},
                    {"separator": "thin"},
                    {"name": "extends_by",       "string": _("تجاوز بـ"),       "widget": "number", "readonly": True},
                ]
            }
        }
    }
}

# ---------------------------------------------------------------------------
# QUICK COLLECTION FORM — opened from customer form action button.
# Title: customer only. Body: type | value+date side by side.
# is_down=True, is_seller=True hidden defaults.
# ---------------------------------------------------------------------------

qurtoba_quick_collection_form_view = {
    "key": "qurtoba_quick_collection_form_view",
    "name": _("تسجيل تحصيل"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_action_quick_collection",
    "view_type": "form",
    "priority": 5,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "sheet": {
            "title": {
                "fields": [
                    {
                        "name": "customer",
                        "widget": "relation",
                        "string": _("العميل"),
                        "displayField": "name",
                        "required": True,
                        "onChange": True,
                    },
                ]
            },
            "sections": [
                {
                    "title": _(""),
                    "groups": [
                        {
                            "fields": [
                                {
                                    "name": "type",
                                    "widget": "select",
                                    "string": _("النوع"),
                                    "options": _CUSTOMER_PAY_OPTIONS,
                                    "required": True,
                                },
                                {
                                    "name": "value",
                                    "widget": "number",
                                    "string": _("المبلغ المحصّل"),
                                    "required": True,
                                },
                            ]
                        },
                        {
                            "fields": [
                                {"name": "is_down",   "widget": "checkbox", "string": _(""), "defaultValue": True,  "invisible": True},
                                {"name": "is_seller", "widget": "checkbox", "string": _(""), "defaultValue": False, "invisible": True},
                            ]
                        },
                    ]
                },
            ],
            "footer": {
                "position": "end",
                "fields": [
                    {"name": "customer_balance", "string": _("الرصيد الحالي"),  "widget": "number", "readonly": True},
                    {"separator": "thin"},
                    {"name": "grade_limit",      "string": _("الحد الائتماني"), "widget": "number", "highlight": True, "readonly": True},
                    {"separator": "thin"},
                    {"name": "extends_by",       "string": _("تجاوز بـ"),       "widget": "number", "readonly": True},
                ]
            }
        }
    }
}

# ---------------------------------------------------------------------------
# QUICK TRANSACTION FORM — for WhatsApp/partner action buttons.
# Shows: customer, selected_account (relation → auto-fills type+number), value.
# Footer: الرصيد الحالي | الحد الائتماني | تجاوز بـ
# ---------------------------------------------------------------------------

qurtoba_quick_transaction_form_view = {
    "key": "qurtoba_quick_transaction_form_view",
    "name": _("معاملة جديدة"),
    "model": "qurtoba.qurtobarecord",
    "menu_item": "qurtoba_action_quick_transaction",
    "view_type": "form",
    "priority": 5,
    "module": "qurtoba",
    "body": {
        "creatable": False,
        "deletable": False,
        "sheet": {
            "title": {
                "fields": [
                    {
                        "name": "customer",
                        "widget": "relation",
                        "string": _("العميل"),
                        "displayField": "name",
                        "required": True,
                        "onChange": True,
                    },
                ]
            },
            "sections": [
                {
                    "title": _(""),
                    "groups": [
                        {
                            "fields": [
                                {
                                    "name": "type",
                                    "widget": "select",
                                    "string": _("النوع"),
                                    "options": _DEBT_OPTIONS,
                                    "required": True,
                                    "onChange": True,
                                },
                                {
                                    "name": "selected_account",
                                    "widget": "relation",
                                    "string": _("الحساب"),
                                    "displayField": "__str__",
                                    "model": "qurtoba.qurtobacustomeraccount",
                                    "required": True,
                                    "onChange": True,
                                    "help": _("اختر الحساب — سيتم ملء النوع والرقم تلقائياً"),
                                    "invisible": {"field": "type", "operator": "in", "value": ["مصاريف خدمه","كاش", "كاش(5)", "كاش(10)", "كاش(20)"]},
                                    "domain": {
                                        "filters": {
                                            "operator": "and",
                                            "filters": [
                                                {"field": "customer", "operator": "eq", "value": "{{customer.id}}"}
                                            ]
                                        }
                                    },
                                    "context": {
                                        "default_fields": {
                                            "customer": "{{customer.id}}"
                                        }
                                    },
                                },
                                {
                                    "name": "account_number",
                                    "widget": "text",
                                    "string": _("رقم الحساب"),
                                    "readonly": {"field": "selected_account", "operator": "is_not_null"},
                                },
                                {
                                    "name": "value",
                                    "widget": "number",
                                    "string": _("المبلغ"),
                                    "required": True,
                                    "onChange": True,
                                    "onChangeTrigger": "change",
                                    "onChangeDebounce": 300,
                                },

                            ]
                        },
                        {
                            "fields": [
                                {"name": "is_down",   "widget": "checkbox", "string": _(""), "defaultValue": False, "invisible": True},
                                {"name": "is_seller", "widget": "checkbox", "string": _(""), "defaultValue": False, "invisible": True},
                            ]
                        },
                    ]
                },
            ],
            "footer": {
                "position": "end",
                "fields": [
                    {"name": "customer_balance", "string": _("الرصيد الحالي"),  "widget": "number", "readonly": True},
                    {"separator": "thin"},
                    {"name": "grade_limit",      "string": _("الحد الائتماني"), "widget": "number", "highlight": True, "readonly": True},
                    {"separator": "thin"},
                    {"name": "extends_by",       "string": _("تجاوز بـ"),       "widget": "number", "readonly": True},
                ]
            }
        }
    }
}
