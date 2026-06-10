# -*- coding: utf-8 -*-
"""
Failed-sync dead-letter queue views for QurtobaSyncProblem.

A list (only open problems, via the menu domain) with a bulk multi-select
**retry** action, and a form with a foreground retry button. The @action
`action_retry_sync` on the model re-runs the push synchronously and reports the
real result.
"""
from django.utils.translation import gettext as _


_STATUS_OPTIONS = [
    {"value": "failed", "label": "فشل"},
    {"value": "done",   "label": "تم"},
]

_OPERATION_OPTIONS = [
    {"value": "push_record", "label": "Push Record"},
]

# Retry button is pointless on an already-resolved row.
_INVISIBLE_IF_DONE = {"field": "status", "operator": "eq", "value": "done"}


# ---------------------------------------------------------------------------
# List — bulk multi-select retry from the header
# ---------------------------------------------------------------------------

qurtoba_sync_problem_list_view = {
    "key": "qurtoba_sync_problem_list_view",
    "name": _("Qurtoba Sync Problems"),
    "model": "qurtoba.qurtobasyncproblem",
    "menu_item": "qurtoba_menu_sync_problems",
    "view_type": "list",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "header": {
            "actions": [
                {
                    "name": "action_retry_sync",
                    "string": _("إعادة المحاولة"),
                    "icon": "RefreshCw",
                    "type": "server",
                    "as": "button",
                    "variant": "primary",
                    "selection_required": True,   # multi-select bulk retry
                    "confirm_required": False,
                },
            ],
        },
        "tree": {
            "fields": [
                {"name": "id",              "widget": "number",   "string": _("ID")},
                {"name": "model_label",     "widget": "text",     "string": _("Model")},
                {"name": "object_id",       "widget": "text",     "string": _("Record #")},
                {"name": "operation",       "widget": "select",   "string": _("Operation"), "options": _OPERATION_OPTIONS},
                {"name": "attempts",        "widget": "number",   "string": _("Attempts")},
                {"name": "status",          "widget": "select",   "string": _("Status"), "options": _STATUS_OPTIONS},
                {"name": "error",           "widget": "text",     "string": _("Last Error")},
                {"name": "last_attempt_at", "widget": "datetime", "string": _("Last Attempt")},
                {"name": "created_at",      "widget": "datetime", "string": _("Created")},
            ]
        },
    },
}


# ---------------------------------------------------------------------------
# Form — details + foreground retry
# ---------------------------------------------------------------------------

qurtoba_sync_problem_form_view = {
    "key": "qurtoba_sync_problem_form_view",
    "name": _("Qurtoba Sync Problem"),
    "model": "qurtoba.qurtobasyncproblem",
    "menu_item": "qurtoba_menu_sync_problems",
    "view_type": "form",
    "priority": 10,
    "module": "qurtoba",
    "body": {
        "header": {
            "actions": [
                {
                    "name": "action_retry_sync",
                    "string": _("إعادة المحاولة الآن"),
                    "icon": "RefreshCw",
                    "type": "server",
                    "as": "button",
                    "variant": "primary",
                    "selection_required": False,
                    "confirm_required": False,
                    "invisible": _INVISIBLE_IF_DONE,
                },
            ],
        },
        "sheet": {
            "title": {
                "fields": [
                    {"name": "model_label", "widget": "text",   "string": _("Model"),  "readonly": True},
                    {"name": "object_id",   "widget": "text",   "string": _("Record #"), "readonly": True},
                    {"name": "status",      "widget": "select", "string": _("Status"), "options": _STATUS_OPTIONS, "readonly": True},
                ]
            },
            "sections": [
                {
                    "title": _("تفاصيل المشكلة"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "operation",       "widget": "select",   "string": _("Operation"), "options": _OPERATION_OPTIONS, "readonly": True},
                                {"name": "attempts",        "widget": "number",   "string": _("Attempts"), "readonly": True},
                                {"name": "last_attempt_at", "widget": "datetime", "string": _("Last Attempt"), "readonly": True},
                            ]
                        },
                        {
                            "fields": [
                                {"name": "error",   "widget": "textarea", "string": _("Last Error"), "readonly": True},
                                {"name": "payload", "widget": "textarea", "string": _("Payload"), "readonly": True},
                            ]
                        },
                    ]
                },
                {
                    "title": _("الحل"),
                    "groups": [
                        {
                            "fields": [
                                {"name": "resolved_at", "widget": "datetime", "string": _("Resolved At"), "readonly": True},
                                {"name": "resolved_by", "widget": "relation", "string": _("Resolved By"), "displayName": "username", "readonly": True},
                            ]
                        },
                    ]
                },
            ]
        }
    },
}
