# -*- coding: utf-8 -*-
"""«ربط الجروب بعميل قرطبة» — the wizard staff use to say which Qurtoba customer a WhatsApp group is for
(owner decision 2026-09-26: never automatic). Opened pre-filled by
ConversationQurtobaExtension.action_qurtoba_link_group; Save runs
ConversationQurtobaExtension.action_qurtoba_save_group_link on that group. An empty customer removes the link."""
from django.utils.translation import gettext as _

qurtoba_group_link_wizard_form = {
    "key": "qurtoba_group_link_wizard_form",
    "name": _("ربط الجروب بعميل قرطبة"),
    "priority": 1,
    "module": "qurtoba",
    "model": "qurtoba.qurtobagrouplinkwizard",
    "view_type": "form",
    "body": {
        "sheet": {
            "sections": [
                {
                    "title": "",
                    "groups": [
                        {
                            "fullWidth": True,
                            "fields": [
                                {"name": "group_name", "string": _("الجروب"), "widget": "char", "readonly": True},
                                {"name": "customer", "string": _("عميل قرطبة"), "widget": "relation",
                                 "displayName": "name", "required": False,
                                 "help": _("الجروب ده لعميل قرطبة مين؟ أي حد في الجروب (غير الموظفين) هيتعامل على "
                                           "حساب العميل ده. سيبه فاضي عشان تشيل الربط.")},
                            ],
                        },
                    ],
                }
            ]
        }
    },
}
