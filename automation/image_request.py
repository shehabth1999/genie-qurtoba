"""«فين الصورة؟» — the customer asks for the transfer's screenshot / receipt image.

Owner decision 2026-09-26: the AI never handles it. The customer gets ONE quoted «لحظة», and the office
staff get an internal note in the chat — mentioning them, quoting the customer's message — plus a push
notification that opens the chat on that message. No model call, so the request can never be mistaken
for a payment receipt (the thinker's handoff to agent_payments crashed the run: 2026-09-25, «الصورة»).

Only a SHORT message whose subject is the image («الصورة», «فين الصوره», «ابعت صورة التحويل», «الاسكرين
لو سمحت») counts: no number, no amount, at most six words.
"""
import logging
import re
from typing import Any, List

from . import lexicon as L

logger = logging.getLogger(__name__)

REPLY = 'لحظة'
_MAX_WORDS = 6
_DEDUPE_TTL = 60 * 60
# Normalised text (L.norm: ة→ه, أإآ→ا, no harakat, lower case).
_IMAGE_RE = re.compile(r'(?:^|\s)(?:ال)?(?:صوره|صور|سكرين|اسكرين|سكرينه|اسكرينه|سكرينشوت|اسكرينشوت|screen|screenshot|ايصال)(?:\s|$|[؟?!.])')


def is_image_request(text: str) -> bool:
    t = L.norm(text)
    if not t or re.search(r'\d', t) or len(t.split()) > _MAX_WORDS:
        return False
    return bool(_IMAGE_RE.search(t + ' '))


def handle(conversation, source) -> bool:
    """Tell the staff and answer «لحظة». Idempotent per message. True when handled."""
    from django.core.cache import cache
    from django.utils import timezone
    from modules.chat.models import Message
    from qurtoba.extensions import system_sender
    from qurtoba.groups import chat_partner
    from qurtoba.tools._debuglog import log_event
    from qurtoba.tools.split import _staff_users
    from qurtoba.tools.transactions import _send_quoted_text

    if not cache.add(f'qurtoba:image_request:{conversation.id}:{source.id}', 1, _DEDUPE_TTL):
        return True
    text = ((source.content or {}).get('text') or '').strip() if isinstance(source.content, dict) else ''
    partner = chat_partner(conversation, getattr(source, 'sender', None))
    customer = getattr(partner, 'qurtoba_customer', None)
    customer_name = getattr(customer, 'name', '') or getattr(partner, 'name', '') or ''
    asker = getattr(getattr(source, 'sender', None), 'name', '') or ''
    staff: List[Any] = _staff_users()

    lines = ['📷 العميل بيسأل على الصورة', f'العميل: {customer_name}']
    if getattr(conversation, 'is_group', False) and asker and asker != customer_name:
        lines.append(f'اللي سأل: {asker}')
    lines.append(f'رسالته: «{text}»')
    if staff:
        lines += ['', ' '.join(f'@{u.id}' for u in staff)]
    try:
        note = Message.objects.create(
            conversation=conversation, sender=system_sender(), type='text', direction='outbound', is_internal=True,
            content={'text': '\n'.join(lines)}, reply_to=source, status='saved',
            mentions=[{'id': str(u.id), 'name': getattr(u.partner, 'name', '') or '', 'type': 'user'} for u in staff],
        )
    except Exception:
        logger.exception('qurtoba image request: could not post the staff note')
        return False

    try:
        conversation.last_message_time = timezone.now()
        conversation.save(update_fields=['last_message_time'])
        from modules.chat.services.chat_bridge_service import ChatBridgeService
        ChatBridgeService()._send_to_conversation_participants_personalized(conversation, note.id)
    except Exception:
        logger.warning('qurtoba image request: live broadcast of the note failed', exc_info=True)

    notified = False
    if staff:
        try:
            from modules.notifications.services import post_notification
            post_notification(
                partner_ids=[u.partner_id for u in staff],
                subject='📷 عميل بيسأل على الصورة',
                body=f'{customer_name}: «{text}»',
                notification_type='inbox', is_push=True, record=note,
                url=f'/chat/?chat={conversation.id}&to={source.id}',
                action={'type': 'open_chat', 'conversation_id': str(conversation.id), 'message_id': str(source.id)},
                category='chat_mention',
            )
            notified = True
        except Exception:
            logger.warning('qurtoba image request: staff notification failed', exc_info=True)

    sent = _send_quoted_text(conversation, getattr(conversation, 'social_partner', None), str(source.id), REPLY)
    log_event('image_request', conversation=conversation, mid=str(source.id)[:8], staff=[u.id for u in staff],
              notified=notified, replied=bool(sent))
    return True
