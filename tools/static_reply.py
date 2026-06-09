"""
Qurtoba static-reply tool for AI Studio agents.

Exposes a single tool the integrator calls themselves to push a fixed text
message into the current chat (e.g. an out-of-working-hours notice). The
message is delivered through OmnichannelSendService — the same path every
other outbound chat message uses (channel API + WebSocket push).
"""
from typing import Any, Dict

from modules.aistudio.tools import tool

# Default message — out-of-working-hours notice.
DEFAULT_STATIC_MESSAGE = (
    'لا يمكنني خدمتك الان خارج ساعات العمل من الساعة 11.45 للساعة للساعة 9 صباحا'
)


@tool(
    name='qurtoba_send_static_message',
    display_name='Send Static Message to Current Chat',
    description=(
        'Use this tool to send a fixed text message to the customer in the current chat '
        '(for example an out-of-working-hours notice). '
        'INPUTS: '
        '1) message (optional) — the exact text to send. If omitted, a default Arabic '
        'out-of-hours message is sent. '
        'The message is delivered to the customer on their channel (WhatsApp/Messenger/…) '
        'and shown in the chat UI. '
        'Returns success=True with the sent text and the conversation id. '
        'Do NOT call this tool if there is no active customer conversation.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=30,
)
def qurtoba_send_static_message(
    context,
    message: str = DEFAULT_STATIC_MESSAGE,
) -> Dict[str, Any]:
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

    text = (message or DEFAULT_STATIC_MESSAGE).strip() or DEFAULT_STATIC_MESSAGE

    try:
        from modules.chat.services.omnichannel_send_service import OmnichannelSendService
        from qurtoba.extensions import _get_system_partner

        OmnichannelSendService().send_and_broadcast(
            partner=conv.social_partner,
            content={'text': text},
            message_type='text',
            conversation=conv,
            system_partner=_get_system_partner(conv),
            websocket=True,
        )
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'error_type': 'send_failed',
        }

    return {
        'success': True,
        'sent_text': text,
        'conversation_id': conv.pk,
        'note': 'Static message dispatched to the chat (channel delivery + WebSocket push).',
    }
