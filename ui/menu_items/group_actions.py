# -*- coding: utf-8 -*-
"""WhatsApp customer groups (owner decision 2026-09-23): one action on a chat message.

«موظف ⇄ عميل» — the sender becomes office staff (its contact's «Is an Employee»), in EVERY group at
once; the AI then reads that member's lines as context only. Pressed again on a staff member's message:
back to customer.

The group's Qurtoba customer is set on the whole chat instead («ربط الجروب بعميل قرطبة», chat_patch.py) —
never from a member's number (owner decision 2026-09-26).
"""
from django.utils.translation import gettext as _

menu_dict = {
    "qurtoba_group_message_actions": {
        "_inherit": "message_actions_menu_item",
        "inheritance_operations": [
            {
                "operation": "append",
                "target": "actions",
                "content": {
                    "name": "action_qurtoba_toggle_staff",      # MUST match the @action method name
                    "string": _("موظف ⇄ عميل"),
                    "icon": "UserCog",
                    "type": "server",
                    "as": "button",
                    "selection_required": True,
                    "confirm_required": False,
                    "invisible": {"field": "direction", "operator": "ne", "value": "inbound"},
                },
            },
        ],
    },
}
