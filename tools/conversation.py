"""
Qurtoba conversation tools for AI Studio agents.

Currently exposes:
  * qurtoba_send_customer_balance_to_chat — replicates the "فحص الرصيد" button:
    posts the linked customer's Qurtoba balance into the active chat as an
    outbound message, dispatched through the same ChatBridgeService used by
    the WhatsApp/Messenger integrations.
"""
from typing import Any, Dict

from modules.aistudio.tools import tool


@tool(
    name='qurtoba_send_customer_balance_to_chat',
    display_name='Send Qurtoba Balance to Current Chat',
    description=(
        'Use this tool when the customer (or an internal agent) asks to see the current '
        'Qurtoba outstanding balance for the customer linked to this conversation. The '
        'tool fetches the live balance and posts a formatted message INTO THE CURRENT '
        'CHAT (visible to the customer on WhatsApp/Messenger and to the team in the chat '
        'UI). It is the same action as the "فحص الرصيد" button in the conversation panel. '
        'No inputs are required — the customer is resolved automatically from the active '
        'conversation. '
        'The message contains: customer name, outstanding debt (مديونيات), credit limit '
        '(الحد الائتماني = grade × 1000), and either the remaining credit (المتاح) or the '
        'over-limit amount (تجاوز). '
        'Returns success=True with the customer name, balance, grade_limit, and remaining '
        'credit. '
        'Do NOT call this tool if the customer is only asking a question that does not '
        'require their balance to be shown. '
        'Do NOT call this tool more than once per request — the message is sent to the '
        'customer\'s phone and re-sending will spam them.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=20,
)
def qurtoba_send_customer_balance_to_chat(context) -> Dict[str, Any]:
    conv = getattr(context, 'conversation', None)
    partner = getattr(context, 'partner', None)
    if partner is None and conv is not None:
        partner = getattr(conv, 'social_partner', None)

    if conv is None or partner is None:
        return {
            'success': False,
            'error': 'No active conversation in context. This tool requires a live customer chat.',
            'error_type': 'no_conversation',
        }

    customer = getattr(partner, 'qurtoba_customer', None)
    if customer is None:
        return {
            'success': False,
            'error': 'The current chat partner is not linked to any Qurtoba customer.',
            'error_type': 'partner_not_linked',
        }

    try:
        from qurtoba.extensions import check_balance_and_send
        check_balance_and_send(conv, customer)
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'error_type': 'send_failed',
        }

    balance = customer.balance or 0
    grade_limit = (customer.grade or 0) * 1000
    remaining = grade_limit - balance if grade_limit else None

    return {
        'success': True,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'balance': balance,
        'grade_limit': grade_limit,
        'remaining_credit': remaining,
        'over_limit': remaining is not None and remaining < 0,
        'conversation_id': conv.pk,
        'note': 'Balance message dispatched to the chat (WhatsApp/Messenger delivery + WebSocket push).',
    }
