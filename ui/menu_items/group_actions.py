# -*- coding: utf-8 -*-
"""WhatsApp customer groups (owner decision 2026-09-23): two actions on a chat message.

«موظف ⇄ عميل» — the sender becomes office staff (its contact's «Is an Employee»), in EVERY group at
once; the AI then reads that member's lines as context only. Pressed again on a staff member's message:
back to customer.

«ربط الجروب بعميل الرقم ده» — link the message's group to the Qurtoba customer its sender stands for
(used when the automatic link found no customer, or more than one, among the members).
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
            {
                "operation": "append",
                "target": "actions",
                "content": {
                    "name": "action_qurtoba_link_group_to_sender",
                    "string": _("ربط الجروب بعميل الرقم ده"),
                    "icon": "Link",
                    "type": "server",
                    "as": "button",
                    "selection_required": True,
                    "confirm_required": True,
                    "invisible": {
                        "or": [
                            {"field": "_selected_count", "operator": "gt", "value": 1},
                            {"field": "direction", "operator": "ne", "value": "inbound"},
                        ],
                    },
                },
            },
        ],
    },
}
