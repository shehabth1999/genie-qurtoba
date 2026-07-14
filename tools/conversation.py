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
        'Post the customer\'s current Qurtoba balance INTO the chat (visible to the customer). '
        'No inputs — the customer is resolved from the active conversation. The tool posts the '
        'number itself, showing ONLY the debt/credit («عليك X جنيه» / «ليك X جنيه» / «مفيش '
        'مديونية») — never grade, limit, remaining credit, or over-limit. Call it fresh on a '
        'balance question; call it at most ONCE per request (re-sending spams the customer).'
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
        'Marks the recent still-open inbound messages as handled — call it when the customer '
        'aborts a WHOLE not-yet-created transfer burst («الغاء/وقف/غلط» = scrap what I just '
        'sent), so the aborted numbers/amounts don\'t linger and get re-paired when they resend. '
        'NOT for cancelling one op among several, nor an already-executed transfer.'
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
        'Silently push a notification to the team so a human steps in — does NOT stop the AI or '
        'message the customer. `note` = a short specific reason + context (customer, amount, '
        'phone/account, exactly what they asked).'
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
