# -*- coding: utf-8 -*-
"""
Review-queue views for the two pending-approval models:
  - QurtobaPendingTransaction (over-limit debt items)
  - QurtobaPendingPayment     (every سداد — screenshot required)

Each model gets:
  * a list view — bulk approve/deny from the header, a default "pending" filter
    (overridable via the search view), and the customer's credit limit + balance
    inline so the reviewer can judge at a glance;
  * a form view — payload fields are EDITABLE (the reviewer can correct a misread
    amount/number before approving), with a credit-status footer;
  * a search view — text search + state filters (so the list is no longer locked
    by a menu domain).

Server-side @action decorators on the models do the approve/deny work and return
`on_success: refresh`, so the list refreshes after each run.
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

# Header buttons are only meaningful while the row is pending.
_INVISIBLE_IF_NOT_PENDING = {"field": "review_state", "operator": "ne", "value": "pending"}

# Default (overridable) filter — list opens on the "not done" / pending set.
_PENDING_DEFAULT_FILTER = {
    "filters": {
        "operator": "and",
        "filters": [
            {"field": "review_state", "operator": "eq", "value": "pending"},
        ],
    },
}


def _bulk_actions(approve_name, deny_name, approve_label):
    """Approve/deny buttons for a list header — operate on the selected rows."""
    return [
        {
            "name": approve_name,
            "string": approve_label,
            "icon": "CheckCircle",
            "type": "server",
            "as": "button",
            "variant": "success",
            "selection_required": True,
            "confirm_required": True,
        },
        {
            "name": deny_name,
            "string": _("رفض"),
            "icon": "XCircle",
            "type": "server",
            "as": "button",
            "variant": "danger",
            "selection_required": True,
            "confirm_required": True,
        },
    ]


def _header_actions(approve_name, deny_name, approve_label):
    """Approve/deny buttons for a form header — hidden once the row is decided."""
    return [
        {
            "name": approve_name,
            "string": approve_label,
            "icon": "CheckCircle",
            "type": "server",
            "as": "button",
            "variant": "success",
            "selection_required": False,
            "confirm_required": True,
            "invisible": _INVISIBLE_IF_NOT_PENDING,
        },
        {
            "name": deny_name,
            "string": _("رفض"),
            "icon": "XCircle",
            "type": "server",
            "as": "button",
            "variant": "danger",
            "selection_required": False,
            "confirm_required": True,
            "invisible": _INVISIBLE_IF_NOT_PENDING,
        },
    ]


# Credit-status footer — current balance + the customer's credit limit (EGP).
def _credit_footer():
    return {
        "position": "end",
        "fields": [
            {"name": "customer_balance",     "string": _("الرصيد الحالي"),  "widget": "number", "readonly": True},
            {"separator": "thin"},
            {"name": "customer_grade", "string": _("الدرجة"), "widget": "number", "readonly": True, "highlight": True},
        ],
    }


# ===========================================================================
# Pending Transaction
# ===========================================================================

qurtoba_pending_transaction_list_view = {
    "key": "qurtoba_pending_transaction_list_view",
    "name": _("Pending Qurtoba Transactions"),
    "model": "qurtoba.qurtobapendingtransaction",
    "menu_item": "qurtoba_menu_pending_transactions",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "header": {
            "actions": _bulk_actions(
                "action_approve_pending_transaction",
                "action_deny_pending_transaction",
                _("اعتماد"),
            ),
        },
        "tree": {
            "default_filter": _PENDING_DEFAULT_FILTER,
            "fields": [
                {"name": "id",                  "widget": "number",   "string": _("ID")},
                {"name": "customer",            "widget": "relation", "string": _("Customer"), "displayName": "name"},
                {"name": "type",                "widget": "text",     "string": _("Type")},
                {"name": "value",               "widget": "number",   "string": _("Amount")},
                {"name": "account_number",      "widget": "text",     "string": _("Account #")},
                {"name": "customer_grade","widget": "number",   "string": _("الدرجة")},
                {"name": "customer_balance",    "widget": "number",   "string": _("الرصيد الحالي")},
                {"name": "review_state",        "widget": "select",   "string": _("State"), "options": _STATE_OPTIONS},
                {"name": "created_at",          "widget": "datetime", "string": _("Created")},
            ],
        },
    },
}


qurtoba_pending_transaction_search_view = {
    "key": "qurtoba_pending_transaction_search_view",
    "name": _("Pending Qurtoba Transactions Search"),
    "model": "qurtoba.qurtobapendingtransaction",
    "menu_item": "qurtoba_menu_pending_transactions",
    "view_type": "search",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "search": {
            "search_fields": [
                {"name": ["account_number", "notes"], "string": _("بحث"), "widget": "text"},
            ],
            "filters": [
                {"name": "pending",  "string": _("قيد المراجعة"), "filter": {"field": "review_state", "operator": "eq", "value": "pending"}},
                {"name": "approved", "string": _("موافق عليه"),   "filter": {"field": "review_state", "operator": "eq", "value": "approved"}},
                {"name": "denied",   "string": _("مرفوض"),        "filter": {"field": "review_state", "operator": "eq", "value": "denied"}},
            ],
            "group_by": [
                {"name": "review_state", "string": _("الحالة")},
                {"name": "customer",     "string": _("العميل")},
            ],
        },
    },
}


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
            "actions": _header_actions(
                "action_approve_pending_transaction",
                "action_deny_pending_transaction",
                _("اعتماد"),
            ),
        },
        "sheet": {
            "title": {
                "fields": [
                    {"name": "type",         "widget": "text",   "string": _("Type")},
                    {"name": "value",        "widget": "number", "string": _("Amount")},
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
                                {"name": "account_number", "widget": "text",     "string": _("Account #")},
                                {"name": "reason",         "widget": "select",   "string": _("Reason"), "options": _TXN_REASON_OPTIONS, "readonly": True},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "notes", "widget": "textarea", "string": _("Notes")},
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
            ],
            "footer": _credit_footer(),
        }
    },
}


# ===========================================================================
# Pending Payment
# ===========================================================================

qurtoba_pending_payment_list_view = {
    "key": "qurtoba_pending_payment_list_view",
    "name": _("Pending Qurtoba Payments"),
    "model": "qurtoba.qurtobapendingpayment",
    "menu_item": "qurtoba_menu_pending_payments",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "header": {
            "actions": _bulk_actions(
                "action_approve_pending_payment",
                "action_deny_pending_payment",
                _("اعتماد السداد"),
            ),
        },
        "tree": {
            "default_filter": _PENDING_DEFAULT_FILTER,
            "fields": [
                {"name": "id",                    "widget": "number",   "string": _("ID")},
                {"name": "customer",              "widget": "relation", "string": _("Customer"), "displayName": "name"},
                {"name": "type",                  "widget": "select",   "string": _("Type"), "options": _PAY_TYPE_OPTIONS},
                {"name": "value",                 "widget": "number",   "string": _("Amount")},
                {"name": "account_number",        "widget": "text",     "string": _("Source Phone")},
                {"name": "screenshot_attachment", "widget": "image",    "string": _("Screenshot")},
                {"name": "customer_grade",  "widget": "number",   "string": _("الدرجة")},
                {"name": "customer_balance",      "widget": "number",   "string": _("الرصيد الحالي")},
                {"name": "review_state",          "widget": "select",   "string": _("State"), "options": _STATE_OPTIONS},
                {"name": "created_at",            "widget": "datetime", "string": _("Created")},
            ],
        },
    },
}


qurtoba_pending_payment_search_view = {
    "key": "qurtoba_pending_payment_search_view",
    "name": _("Pending Qurtoba Payments Search"),
    "model": "qurtoba.qurtobapendingpayment",
    "menu_item": "qurtoba_menu_pending_payments",
    "view_type": "search",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "search": {
            "search_fields": [
                {"name": ["account_number", "notes", "customer_confirmation_text"], "string": _("بحث"), "widget": "text"},
            ],
            "filters": [
                {"name": "pending",  "string": _("قيد المراجعة"), "filter": {"field": "review_state", "operator": "eq", "value": "pending"}},
                {"name": "approved", "string": _("موافق عليه"),   "filter": {"field": "review_state", "operator": "eq", "value": "approved"}},
                {"name": "denied",   "string": _("مرفوض"),        "filter": {"field": "review_state", "operator": "eq", "value": "denied"}},
            ],
            "group_by": [
                {"name": "review_state", "string": _("الحالة")},
                {"name": "type",         "string": _("النوع")},
                {"name": "customer",     "string": _("العميل")},
            ],
        },
    },
}


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
            "actions": _header_actions(
                "action_approve_pending_payment",
                "action_deny_pending_payment",
                _("اعتماد السداد"),
            ),
        },
        "sheet": {
            "title": {
                "fields": [
                    {"name": "type",         "widget": "select", "string": _("Type"), "options": _PAY_TYPE_OPTIONS},
                    {"name": "value",        "widget": "number", "string": _("Amount")},
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
                                {"name": "customer",       "widget": "relation", "string": _("Customer"), "displayName": "name", "readonly": True},
                                {"name": "partner",        "widget": "relation", "string": _("Chat Partner"), "displayName": "name", "readonly": True},
                                {"name": "account_number", "widget": "text",     "string": _("Source Phone")},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "customer_confirmation_text", "widget": "textarea", "string": _("Customer Confirmation"), "readonly": True},
                                {"name": "notes",                      "widget": "textarea", "string": _("Notes")},
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
            ],
            "footer": _credit_footer(),
        }
    },
}
