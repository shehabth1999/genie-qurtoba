# -*- coding: utf-8 -*-
from django.utils.translation import gettext as _

menu_dict = {
    "qurtoba_append_chat_actions_omnichannel": {
        "_inherit": "chat_main_menu_omnichannel",
        "inheritance_operations": [
            # عملية جديدة — debt/transaction form with account selector (isDown=False)
            {
                "operation": "append",
                "target": "actions",
                "content": {
                    "string": _("عملية جديدة"),
                    "icon": "CreditCard",
                    "name": "action_qurtoba_new_transaction",
                    "type": "server",
                    "as": "button",
                    "view_type": ["form"],
                    "confirm_required": False,
                },
            },
            # سداد — payment/collection form (شراء كاش / شراء فورى, isDown=True)
            {
                "operation": "append",
                "target": "actions",
                "content": {
                    "string": _("سداد"),
                    "icon": "CheckCircle",
                    "name": "action_qurtoba_new_debt",
                    "type": "server",
                    "as": "button",
                    "view_type": ["form"],
                    "confirm_required": False,
                },
            },
            # فحص الرصيد — sends balance message on the conversation
            {
                "operation": "append",
                "target": "actions",
                "content": {
                    "string": _("فحص الرصيد"),
                    "icon": "Wallet",
                    "name": "action_qurtoba_check_balance",
                    "type": "server",
                    "as": "button",
                    "view_type": ["form"],
                    "confirm_required": False,
                },
            },
            # المعاملات — list of this customer's transactions
            {
                "operation": "append",
                "target": "actions",
                "content": {
                    "string": _("المعاملات"),
                    "icon": "FileText",
                    "name": "action_qurtoba_transactions",
                    "type": "server",
                    "as": "button",
                    "view_type": ["form"],
                    "confirm_required": False,
                },
            },
        ]
    }
}
