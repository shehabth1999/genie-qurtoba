"""One way to tell the office something inside a chat: an INTERNAL note that @mentions every active
staff login (never delivered to the customer), shown live in the chat, plus an inbox + push
notification deep-linked to it — the shape the split-request tool established on 2026-09-13.

Used where the system must not stay quiet towards the office: a Cash-SYS «done» after a «canceled»,
a transfer that never reached Qurtoba after every retry, a model turn that failed twice.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def staff_users() -> List[Any]:
    """Every active staff login with a partner, the AI excluded."""
    from django.contrib.auth import get_user_model
    return list(get_user_model().objects.filter(is_active=True)
                .exclude(partner__isnull=True).exclude(partner__ai_agent=True)
                .select_related('partner').order_by('id'))


def post_staff_note(conversation, lines: List[str], *, subject: str, body: str,
                    reply_to=None, dedupe_key: Optional[str] = None, dedupe_ttl: int = 3600) -> Dict[str, Any]:
    """Post the note and notify the staff. Best-effort: never raises; returns what happened.

    ``dedupe_key`` (cache) makes a repeated call within ``dedupe_ttl`` a no-op, so a retried task
    never posts the same note twice."""
    out: Dict[str, Any] = {'posted': False, 'notified': False, 'note_id': None, 'staff': []}
    if conversation is None:
        return out
    try:
        from django.core.cache import cache
        if dedupe_key and not cache.add(f'qurtoba:staff_note:{dedupe_key}', 1, dedupe_ttl):
            out['deduped'] = True
            return out
    except Exception:
        pass
    try:
        from django.utils import timezone
        from modules.chat.models import Message
        from qurtoba.extensions import system_sender
        staff = staff_users()
        text_lines = [l for l in lines if l is not None]
        if staff:
            text_lines += ['', ' '.join(f'@{u.id}' for u in staff)]
        note = Message.objects.create(
            conversation=conversation, sender=system_sender(), type='text', direction='outbound', is_internal=True,
            content={'text': '\n'.join(text_lines)}, reply_to=reply_to, status='saved',
            mentions=[{'id': str(u.id), 'name': getattr(u.partner, 'name', '') or '', 'type': 'user'} for u in staff],
        )
        out.update(posted=True, note_id=str(note.id), staff=[u.id for u in staff])
    except Exception:
        logger.exception('staff note: could not post the internal note')
        return out
    try:
        conversation.last_message_time = timezone.now()
        conversation.save(update_fields=['last_message_time'])
        from modules.chat.services.chat_bridge_service import ChatBridgeService
        ChatBridgeService()._send_to_conversation_participants_personalized(conversation, note.id)
    except Exception:
        logger.warning('staff note: live broadcast failed', exc_info=True)
    if staff:
        try:
            from modules.notifications.services import post_notification
            post_notification(
                partner_ids=[u.partner_id for u in staff],
                subject=subject,
                body=body,
                notification_type='inbox',
                is_push=True,
                record=note,
                url=f'/chat/?chat={conversation.id}&to={note.id}',
                action={'type': 'open_chat', 'conversation_id': str(conversation.id), 'message_id': str(note.id)},
                category='chat_mention',
            )
            out['notified'] = True
        except Exception:
            logger.warning('staff note: notification failed', exc_info=True)
    return out
