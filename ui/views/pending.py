# -*- coding: utf-8 -*-
"""
Review-queue views for the two pending-approval models:
  - QurtobaPendingTransaction (over-limit debt items)
  - QurtobaPendingPayment     (every سداد — screenshot required)

Each model gets a list view (filtered by review_state) and a form view with
approve / deny header actions. Server-side @action decorators on the models
do the actual approve/deny work.
"""
from django.utils.translation import gettext as _


_STATE_OPTIONS = [
    {"value": "pending",  "label": "قيد المراجعة"},
    {"value": "approved", "label": "موافق عليه"},
    {"value": "denied",   "label": "مرفوض"},
]

_TXN_REASON_OPTIONS = [
    {"value": "grade_limit_exceeded", "label": "تجاوز الحد الائتماني"},
]

_PAY_REASON_OPTIONS = [
    {"value": "payment_review", "label": "سداد بانتظار المراجعة"},
]

_PAY_TYPE_OPTIONS = [
    {"value": "شراء كاش",  "label": "شراء كاش"},
    {"value": "شراء فورى", "label": "شراء فورى"},
]


# Shared visibility — buttons only visible while the row is pending.
_INVISIBLE_IF_NOT_PENDING = {"field": "review_state", "operator": "ne", "value": "pending"}


# ---------------------------------------------------------------------------
# Pending Transaction — list
# ---------------------------------------------------------------------------

qurtoba_pending_transaction_list_view = {
    "key": "qurtoba_pending_transaction_list_view",
    "name": _("Pending Qurtoba Transactions"),
    "model": "qurtoba.qurtobapendingtransaction",
    "menu_item": "qurtoba_menu_pending_transactions",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "tree": {
            "fields": [
                {"name": "id",             "widget": "number",   "string": _("ID")},
                {"name": "customer",       "widget": "relation", "string": _("Customer"), "displayName": "name"},
                {"name": "type",           "widget": "text",     "string": _("Type")},
                {"name": "value",          "widget": "number",   "string": _("Amount")},
                {"name": "account_number", "widget": "text",     "string": _("Account #")},
                {"name": "reason",         "widget": "text",     "string": _("Reason")},
                {"name": "review_state",   "widget": "select",   "string": _("State"), "options": _STATE_OPTIONS},
                {"name": "created_at",     "widget": "datetime", "string": _("Created")},
            ]
        }
    },
}


# ---------------------------------------------------------------------------
# Pending Transaction — form
# ---------------------------------------------------------------------------

qurtoba_pending_transaction_form_view = {
    "key": "qurtoba_pending_transaction_form_view",
    "name": _("Pending Qurtoba Transaction"),
    "model": "qurtoba.qurtobapendingtransaction",
    "menu_item": "qurtoba_menu_pending_transactions",
    "view_type": "form",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "header": {
            "actions": [
                {
                    "name": "action_approve_pending_transaction",
                    "string": _("اعتماد"),
                    "icon": "CheckCircle",
                    "type": "server",
                    "as": "button",
                    "variant": "success",
                    "selection_required": False,
                    "confirm_required": True,
                    "invisible": _INVISIBLE_IF_NOT_PENDING,
                },
                {
                    "name": "action_deny_pending_transaction",
                    "string": _("رفض"),
                    "icon": "XCircle",
                    "type": "server",
                    "as": "button",
                    "variant": "danger",
                    "selection_required": False,
                    "confirm_required": True,
                    "invisible": _INVISIBLE_IF_NOT_PENDING,
                },
            ],
        },
        "sheet": {
            "title": {
                "fields": [
                    {"name": "type",         "widget": "text",   "string": _("Type"),  "readonly": True},
                    {"name": "value",        "widget": "number", "string": _("Amount"),"readonly": True},
                    {"name": "review_state", "widget": "select", "string": _("State"), "options": _STATE_OPTIONS, "readonly": True},
                ]
            },
            "sections": [
                {
                    "title": _("تفاصيل العملية"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "customer",       "widget": "relation", "string": _("Customer"), "displayName": "name", "readonly": True},
                                {"name": "partner",        "widget": "relation", "string": _("Chat Partner"), "displayName": "name", "readonly": True},
                                {"name": "account_number", "widget": "text",     "string": _("Account #"), "readonly": True},
                                {"name": "reason",         "widget": "select",   "string": _("Reason"), "options": _TXN_REASON_OPTIONS, "readonly": True},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "notes",          "widget": "textarea", "string": _("Notes"), "readonly": True},
                            ]
                        },
                    ]
                },
                {
                    "title": _("نتيجة المراجعة"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "denial_reason",  "widget": "textarea", "string": _("Denial Reason"), "required": False},
                                {"name": "reviewer",       "widget": "relation", "string": _("Reviewer"), "displayName": "username", "readonly": True},
                                {"name": "reviewed_at",    "widget": "datetime", "string": _("Reviewed At"), "readonly": True},
                                {"name": "created_record", "widget": "relation", "string": _("Created Record"), "displayName": "id", "readonly": True},
                            ]
                        },
                    ]
                },
            ]
        }
    },
}


# ---------------------------------------------------------------------------
# Pending Payment — list
# ---------------------------------------------------------------------------

qurtoba_pending_payment_list_view = {
    "key": "qurtoba_pending_payment_list_view",
    "name": _("Pending Qurtoba Payments"),
    "model": "qurtoba.qurtobapendingpayment",
    "menu_item": "qurtoba_menu_pending_payments",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "tree": {
            "fields": [
                {"name": "id",                    "widget": "number",   "string": _("ID")},
                {"name": "customer",              "widget": "relation", "string": _("Customer"), "displayName": "name"},
                {"name": "type",                  "widget": "select",   "string": _("Type"), "options": _PAY_TYPE_OPTIONS},
                {"name": "value",                 "widget": "number",   "string": _("Amount")},
                {"name": "account_number",        "widget": "text",     "string": _("Source Phone")},
                {"name": "screenshot_attachment", "widget": "image",    "string": _("Screenshot")},
                {"name": "review_state",          "widget": "select",   "string": _("State"), "options": _STATE_OPTIONS},
                {"name": "created_at",            "widget": "datetime", "string": _("Created")},
            ]
        }
    },
}


# ---------------------------------------------------------------------------
# Pending Payment — form
# ---------------------------------------------------------------------------

qurtoba_pending_payment_form_view = {
    "key": "qurtoba_pending_payment_form_view",
    "name": _("Pending Qurtoba Payment"),
    "model": "qurtoba.qurtobapendingpayment",
    "menu_item": "qurtoba_menu_pending_payments",
    "view_type": "form",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "header": {
            "actions": [
                {
                    "name": "action_approve_pending_payment",
                    "string": _("اعتماد السداد"),
                    "icon": "CheckCircle",
                    "type": "server",
                    "as": "button",
                    "variant": "success",
                    "selection_required": False,
                    "confirm_required": True,
                    "invisible": _INVISIBLE_IF_NOT_PENDING,
                },
                {
                    "name": "action_deny_pending_payment",
                    "string": _("رفض"),
                    "icon": "XCircle",
                    "type": "server",
                    "as": "button",
                    "variant": "danger",
                    "selection_required": False,
                    "confirm_required": True,
                    "invisible": _INVISIBLE_IF_NOT_PENDING,
                },
            ],
        },
        "sheet": {
            "title": {
                "fields": [
                    {"name": "type",         "widget": "select", "string": _("Type"), "options": _PAY_TYPE_OPTIONS, "readonly": True},
                    {"name": "value",        "widget": "number", "string": _("Amount"), "readonly": True},
                    {"name": "review_state", "widget": "select", "string": _("State"), "options": _STATE_OPTIONS, "readonly": True},
                ]
            },
            "sections": [
                {
                    "title": _("صورة الإيصال"),
                    "groups": [
                        {
                            "fullWidth": True,
                            "fields": [
                                {"name": "screenshot_attachment", "widget": "image", "string": _("Receipt"), "readonly": True},
                            ]
                        },
                    ]
                },
                {
                    "title": _("تفاصيل السداد"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "customer",                   "widget": "relation", "string": _("Customer"), "displayName": "name", "readonly": True},
                                {"name": "partner",                    "widget": "relation", "string": _("Chat Partner"), "displayName": "name", "readonly": True},
                                {"name": "account_number",             "widget": "text",     "string": _("Source Phone"), "readonly": True},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "customer_confirmation_text", "widget": "textarea", "string": _("Customer Confirmation"), "readonly": True},
                                {"name": "notes",                      "widget": "textarea", "string": _("Notes"), "readonly": True},
                            ]
                        },
                    ]
                },
                {
                    "title": _("نتيجة المراجعة"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "denial_reason",  "widget": "textarea", "string": _("Denial Reason"), "required": False},
                                {"name": "reviewer",       "widget": "relation", "string": _("Reviewer"), "displayName": "username", "readonly": True},
                                {"name": "reviewed_at",    "widget": "datetime", "string": _("Reviewed At"), "readonly": True},
                                {"name": "created_record", "widget": "relation", "string": _("Created Record"), "displayName": "id", "readonly": True},
                            ]
                        },
                    ]
                },
            ]
        }
    },
}
