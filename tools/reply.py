"""
The agent's reply tool for every channel — WhatsApp Cloud API and WhatsApp Web (customer groups).

Core's ``whatsapp_reply_to_message`` only works on the Cloud API: on a WhatsApp Web chat it cannot
even build its service. This tool sends through OmnichannelSendService, which picks the channel of
the conversation, and ALWAYS quotes a message: the one the model names, else the newest customer
line of the chat (in a group, never a staff member's — qurtoba.groups). The text goes through the
outbound gate like any agent text (it is NOT a system send).
"""
import logging
from typing import Any, Dict

from modules.aistudio.tools import tool

logger = logging.getLogger(__name__)


def _target_message(conv, message_id: str):
    from modules.chat.models import Message
    from qurtoba.groups import customer_inbound, is_group, is_staff
    mid = str(message_id or '').strip()
    if mid:
        m = (Message.objects_all.filter(id=mid, conversation=conv, direction='inbound')
             .select_related('sender').first()) if len(mid) == 36 else None
        if m is not None and not (is_group(conv) and is_staff(m.sender if m.sender_id else None)):
            return m
    return customer_inbound(conv).order_by('-created_at').first()


@tool(
    name='qurtoba_reply_to_message',
    display_name='Reply to the customer (quoted)',
    description=(
        'Send your words to the customer as a reply that QUOTES one of their messages. Works in 1:1 chats '
        'and in the customer\'s WhatsApp group. '
        'INPUTS: 1) text — exactly what to send (short Egyptian Arabic). '
        '2) message_id (optional) — the [message_id: …] of the customer message you are answering; if '
        'omitted, the newest customer message is quoted (never a staff member\'s line). '
        'One reply per customer message. If the result says blocked, do NOT resend the same words.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=60,
    side_effect=True,
)
def qurtoba_reply_to_message(context, text: str, message_id: str = '') -> Dict[str, Any]:
    conv = getattr(context, 'conversation', None)
    if conv is None:
        return {'success': False, 'error_type': 'no_conversation', 'error': 'No active conversation.'}
    body = str(text or '').strip()
    if not body:
        return {'success': False, 'error_type': 'empty_text', 'error': 'Nothing to send.'}
    target = _target_message(conv, message_id)
    try:
        from modules.chat.services.omnichannel_send_service import OmnichannelSendService
        from qurtoba.extensions import _get_system_partner
        res = OmnichannelSendService().send_and_broadcast(
            partner=conv.social_partner,
            content={'text': body},
            message_type='text',
            conversation=conv,
            system_partner=_get_system_partner(conv),
            reply_to_message_id=getattr(target, 'social_id', None) if target is not None else None,
            reply_to_id=str(target.id) if target is not None else None,
            websocket=True,
        )
    except Exception as exc:
        logger.warning('qurtoba_reply_to_message: send failed', exc_info=True)
        return {'success': False, 'error_type': 'send_failed', 'error': str(exc)[:200]}
    if isinstance(res, dict) and res.get('success') is False:
        blocked = bool(res.get('blocked')) or 'qurtoba_ai_guard' in str(res.get('error') or '')
        return {
            'success': False,
            'error_type': 'blocked' if blocked else 'send_failed',
            'error': str(res.get('error') or '')[:200],
            'note': ('The outbound gate refused these words — do not resend them.' if blocked
                     else 'The message could not be delivered.'),
        }
    return {
        'success': True,
        'quoted_message_id': str(target.id) if target is not None else None,
        'note': 'Sent.',
    }
