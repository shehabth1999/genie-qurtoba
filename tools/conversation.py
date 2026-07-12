"""
Qurtoba conversation tools for AI Studio agents.

Currently exposes:
  * qurtoba_send_customer_balance_to_chat — replicates the "فحص الرصيد" button:
    posts the linked customer's Qurtoba balance into the active chat as an
    outbound message, dispatched through the same ChatBridgeService used by
    the WhatsApp/Messenger integrations.
  * alert_qurtoba_human — push-notify every team member on the conversation so a
    human steps in, WITHOUT disabling the AI and WITHOUT telling the customer.
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
        'The message shows ONLY the outstanding balance/debt — «عليك X جنيه» (owes) / «ليك X '
        'جنيه» (in credit) / «مفيش مديونية» (nothing owed). It does NOT show the grade, credit '
        'limit, remaining credit, or any over-limit amount (those are internal — see Law 6). '
        'Returns success=True with the customer name and balance only. '
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

    # Return ONLY the balance. grade_limit / remaining / over_limit are internal (Law 6 —
    # never revealed to the partner), so they are deliberately NOT handed to the model.
    return {
        'success': True,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'balance': customer.balance or 0,
        'conversation_id': conv.pk,
        'note': 'Balance message dispatched to the chat (WhatsApp/Messenger delivery + WebSocket push).',
    }


@tool(
    name='qurtoba_clear_pending_transfers',
    display_name='Clear the pending (uncreated) transfer burst',
    description=(
        'Call this ONLY when the customer cancels/aborts a whole transfer burst they were '
        'still sending and NOTHING has been created yet (a general «الغاء/وقف/كنسل/غلط» that '
        'means "scrap what I just sent"). It marks the recent still-open inbound messages as '
        'handled so that aborted numbers/amounts do NOT linger and get re-paired or re-created '
        'when the customer resends fresh. Do NOT call it for a cancel of ONE specific transfer '
        'among several (just omit that one and create the rest), nor for an already-executed '
        'transfer (that needs alert_qurtoba_human). After calling, reply «تم الإيقاف…» as usual.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=20,
)
def qurtoba_clear_pending_transfers(context) -> Dict[str, Any]:
    """Watermark the recent unconsumed inbound text messages so an aborted burst can't leak
    into the next planner run. Best-effort; never raises into the tool flow."""
    conv = getattr(context, 'conversation', None)
    if conv is None:
        return {'success': False, 'error_type': 'no_conversation',
                'error': 'No active conversation in context.'}
    try:
        from datetime import timedelta
        from django.conf import settings as _dj
        from django.utils import timezone
        from modules.chat.models import Message
        window = getattr(_dj, 'AI_UNPROCESSED_WINDOW_MIN', 6)
        cutoff = timezone.now() - timedelta(minutes=window)
        rows = Message.objects_all.filter(
            conversation=conv, direction='inbound', active=True, type='text',
            ai_consumed_at__isnull=True, created_at__gte=cutoff,
        )
        cleared = 0
        for m in rows:
            if m.mark_ai_consumed(None):
                cleared += 1
        return {'success': True, 'cleared': cleared}
    except Exception as e:
        return {'success': False, 'error_type': 'clear_failed', 'error': str(e)}


@tool(
    name='alert_qurtoba_human',
    display_name='Alert Qurtoba Human (push only)',
    description=(
        'Send a PUSH notification to every team member on this conversation so a human '
        'steps in. It does NOT disable the AI (the AI keeps handling the chat) and it does '
        'NOT send anything to the customer. '
        'Pass `note` with a short, specific reason a human is needed plus context — customer '
        'name, amount, phone/account number, and exactly what they asked — so the human can '
        'act without reading the whole chat. '
        'After calling this tool, reply to the customer with only «لحظة» — never tell the '
        'customer that a human/colleague will contact them or that you are escalating.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=20,
    parameters_schema={
        'type': 'object',
        'properties': {
            'note': {
                'type': 'string',
                'description': (
                    'Short, specific reason a human is needed + context '
                    '(customer name, amount, number, what was asked).'
                ),
            },
        },
        'required': ['note'],
    },
)
def alert_qurtoba_human(context, note: str = '') -> Dict[str, Any]:
    """Push-notify every team member on the conversation, without disabling AI."""
    conv = getattr(context, 'conversation', None)
    if conv is None:
        return {
            'success': False,
            'error': 'No active conversation in context. This tool requires a live customer chat.',
            'error_type': 'no_conversation',
        }

    try:
        # Push only: no DM spam, no favorite — just an inbox/push alert to all
        # company participants on this conversation. AI handling stays ON.
        conv.alert_human(
            notify_dm=False,
            notify_push=True,
            mark_favorite=False,
            message=(note or None),
        )
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'error_type': 'alert_failed',
        }

    return {
        'success': True,
        'conversation_id': conv.pk,
        'note': 'Push notification sent to all conversation team members. AI handling stays ON.',
    }
