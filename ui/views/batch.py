# -*- coding: utf-8 -*-
# Batch extensions — qurtoba module adds views to other modules.
from django.utils.translation import gettext as _


# WhatsApp Account form — adds a "إعدادات قرطبة" tab with per-type service
# toggles. Each boolean controls whether the AI agent is allowed to create
# that transfer type for this WhatsApp account.
whatsapp_account_form_qurtoba_batch = {
    "key": "whatsapp_account_form_qurtoba_batch",
    "name": "WhatsApp Account - Qurtoba Settings",
    "model": "whatsapp.whatsappaccount",
    "view_type": "form",
    "priority": 50,
    "inherit_mode": "extension",
    "inherit_id": "whatsapp_business_account_form_view",
    "module": "whatsapp",
    "inheritance_operations": [
        {
            "operation": "append",
            "target": "sheet.tabs",
            "content": {
                "title": _("إعدادات قرطبة"),
                "sections": [
                    {
                        "title": _("الخدمات المتاحة للوكيل الذكي"),
                        "groups": [
                            {
                                "title": _("تحويلات كاش"),
                                "fields": [
                                    {
                                        "name": "qurtoba_allow_cash",
                                        "string": _("كاش (أقل من 10,000)"),
                                        "widget": "switch",
                                        "help": _("السماح بإنشاء معاملات كاش العادية للمبالغ أقل من 10,000."),
                                    },
                                    {
                                        "name": "qurtoba_allow_cash_10",
                                        "string": _("كاش(10) (من 10,000 إلى 19,999)"),
                                        "widget": "switch",
                                    },
                                    {
                                        "name": "qurtoba_allow_cash_20",
                                        "string": _("كاش(20) (20,000 فأكثر)"),
                                        "widget": "switch",
                                    },
                                    {
                                        "name": "qurtoba_allow_cash_5",
                                        "string": _("كاش(5) — محجوز"),
                                        "widget": "switch",
                                        "help": _("نوع محجوز للاستخدام المستقبلي."),
                                    },
                                ],
                            },
                            {
                                "title": _("تحويلات أخرى"),
                                "fields": [
                                    {
                                        "name": "qurtoba_allow_fawry",
                                        "string": _("فورى"),
                                        "widget": "switch",
                                    },
                                    {
                                        "name": "qurtoba_allow_aman",
                                        "string": _("أمان"),
                                        "widget": "switch",
                                    },
                                    {
                                        "name": "qurtoba_allow_tayer",
                                        "string": _("طاير"),
                                        "widget": "switch",
                                    },
                                    {
                                        "name": "qurtoba_allow_service_fee",
                                        "string": _("مصاريف خدمه"),
                                        "widget": "switch",
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        },
    ],
}
