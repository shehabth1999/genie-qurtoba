"""Split requests — one amount divided across several numbers, done BY HAND at the office.

Owner decision 2026-09-13: the AI never splits money. When a customer asks for a split («قسم/وزّع المبلغ على
الأرقام», «نص نص», «بالتساوي»), this tool:

  1. reads the split from the customer's own messages, never from the model's transcription;
  2. posts an INTERNAL note in the chat (never sent to the customer) that @mentions the office staff;
  3. notifies those staff like a receipt waiting for review: inbox + push, deep-linked to the note;
  4. sends the customer ONE short quoted line and marks the turn answered;
  5. marks the split messages handled, so nothing ever replays them into a transfer.

Idempotent per request message: a second call notifies no one and sends nothing.
"""
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

from modules.aistudio.tools import tool

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_BURST_BEFORE_SECONDS = 180     # the split's numbers arrive seconds, at most a few minutes, before «قسم»
_DEDUPE_TTL = 60 * 60


def _staff_users() -> List[Any]:
    """Every active staff login with a partner, the AI excluded: the same people a receipt waiting for
    review notifies."""
    from django.contrib.auth import get_user_model
    return list(get_user_model().objects.filter(is_active=True)
                .exclude(partner__isnull=True).exclude(partner__ai_agent=True)
                .select_related('partner').order_by('id'))


def _split_messages(conversation, source):
    """The customer messages that make up this split, oldest first: the request itself plus every number and
    amount of the same burst. A message that already became a transfer is left out."""
    from django.db.models.functions import Coalesce
    from modules.chat.models import Message
    from qurtoba.models import QurtobaRecord
    from qurtoba.tools.planning import _classify_message

    from qurtoba.groups import exclude_staff
    rows = list(
        exclude_staff(Message.objects_all, conversation)
        .filter(conversation=conversation, direction='inbound', active=True, type='text',
                created_at__gte=source.created_at - timedelta(seconds=_BURST_BEFORE_SECONDS))
        .annotate(_ord=Coalesce('social_sent_at', 'created_at'))
        .order_by('_ord', 'created_at', 'id')
    )
    used = {str(x) for x in QurtobaRecord.objects.filter(origin_message_id__in=[r.id for r in rows])
            .values_list('origin_message_id', flat=True)}
    out = []
    for r in rows:
        if str(r.id) in used:
            continue
        text = (r.content or {}).get('text', '') if isinstance(r.content, dict) else ''
        cls = _classify_message(text or '')
        if r.id == source.id or cls['phones'] or cls['amounts']:
            out.append((r, text or '', cls))
    return out


@tool(
    name='qurtoba_request_split',
    display_name='Request a Manual Split (Qurtoba)',
    description=(
        'Use this tool when the customer asks to SPLIT or DISTRIBUTE one amount across several numbers '
        '(«قسم», «وزّع», «نص نص», «بالتساوي», «بالنص»). Splitting is done by hand at the office, never by you. '
        'Before calling it you MUST have: source_message_id = the [message_id] of the customer message that asks for '
        'the split. The tool itself posts an internal note in this chat that mentions the office staff, notifies them '
        'like a receipt waiting for review, and sends the customer one short quoted reply. It returns '
        'reply_fully_handled=true: after it succeeds output ZERO characters. Do NOT create any transfer for those '
        'numbers, do NOT ask how much per number, and do NOT call alert_qurtoba_human as well.'
    ),
    category='qurtoba',
    side_effect=True,
    parameters_schema={
        'type': 'object',
        'properties': {
            'source_message_id': {
                'type': 'string',
                'description': 'The [message_id] of the customer message that asks for the split.',
            },
            'note': {
                'type': 'string',
                'description': 'Optional: anything the staff must know that the customer messages do not already say.',
            },
        },
        'required': ['source_message_id'],
    },
)
def qurtoba_request_split(context, source_message_id: str, note: Optional[str] = None) -> Dict[str, Any]:
    from django.core.cache import cache
    from django.utils import timezone
    from modules.chat.models import Message
    from qurtoba.automation import replies as R
    from qurtoba.automation.context import consume
    from qurtoba.extensions import system_sender
    from qurtoba.tools._debuglog import log_event
    from qurtoba.tools.transactions import _resolve_conversation_and_customer, _send_quoted_text

    conv, customer, err = _resolve_conversation_and_customer(context)
    if err:
        return err
    match = _UUID_RE.search(str(source_message_id or ''))
    if not match:
        return {'success': False, 'error_type': 'invalid_message_id',
                'error': f"'{source_message_id}' is not a message id; pass the [message_id] of the split request."}
    source = Message.objects_all.filter(id=match.group(0), conversation=conv, direction='inbound').first()
    if source is None:
        return {'success': False, 'error_type': 'message_not_found',
                'error': 'That id is not a customer message in this chat; pass the [message_id] of the split request.'}

    dedupe_key = f'qurtoba:split_request:{conv.id}:{source.id}'
    if not cache.add(dedupe_key, 1, _DEDUPE_TTL):
        log_event('split_request_repeat', conversation=conv, mid=str(source.id)[:8])
        return {'success': True, 'already_requested': True, 'reply_fully_handled': True,
                'note': 'This split was already sent to the office staff and the customer was already told. Output nothing.'}

    try:
        parts = _split_messages(conv, source)
        phones: List[str] = []
        amounts: List[float] = []
        for _, _, cls in parts:
            for p in cls['phones']:
                if p not in phones:
                    phones.append(p)
            amounts += [float(a) for a in cls['amounts']]
        amount_label = ' + '.join(R._fmt(a) for a in amounts) if amounts else 'غير واضح'
        staff = _staff_users()
        customer_name = getattr(customer, 'name', '') or ''

        lines = [
            '📣 طلب تقسيم مبلغ — مطلوب تنفيذه يدوياً',
            f'العميل: {customer_name}',
            f'المبلغ: {amount_label}',
            f'الأرقام ({len(phones)}): ' + ('، '.join(phones) if phones else 'غير واضحة'),
            'رسائل العميل:',
            *[f'«{t.strip()}»' for _, t, _ in parts if t and t.strip()],
        ]
        if note and str(note).strip():
            lines.append(f'ملاحظة: {str(note).strip()[:300]}')
        if staff:
            lines += ['', ' '.join(f'@{u.id}' for u in staff)]

        note_msg = Message.objects.create(
            conversation=conv, sender=system_sender(), type='text', direction='outbound', is_internal=True,
            content={'text': '\n'.join(lines)}, reply_to=source, status='saved',
            mentions=[{'id': str(u.id), 'name': getattr(u.partner, 'name', '') or '', 'type': 'user'} for u in staff],
        )
    except Exception as exc:
        cache.delete(dedupe_key)
        logger.exception('qurtoba_request_split: could not post the internal note')
        return {'success': False, 'error_type': 'note_failed',
                'error': f'Could not post the split request for the staff: {exc}. Call alert_qurtoba_human instead.'}

    # Surface the chat and show the note live, exactly like the chat's own internal system messages.
    try:
        conv.last_message_time = timezone.now()
        conv.save(update_fields=['last_message_time'])
        from modules.chat.services.chat_bridge_service import ChatBridgeService
        ChatBridgeService()._send_to_conversation_participants_personalized(conv, note_msg.id)
    except Exception:
        logger.warning('qurtoba_request_split: live broadcast of the note failed', exc_info=True)

    notified = False
    if staff:
        try:
            from modules.notifications.services import post_notification
            post_notification(
                partner_ids=[u.partner_id for u in staff],
                subject='📣 طلب تقسيم مبلغ',
                body=f'{customer_name}: {amount_label} على {len(phones)} أرقام — مطلوب تنفيذه يدوياً.',
                notification_type='inbox',
                is_push=True,
                record=note_msg,
                url=f'/chat/?chat={conv.id}&to={note_msg.id}',
                action={'type': 'open_chat', 'conversation_id': str(conv.id), 'message_id': str(note_msg.id)},
                category='chat_mention',
            )
            notified = True
        except Exception:
            logger.warning('qurtoba_request_split: staff notification failed', exc_info=True)

    sent = _send_quoted_text(conv, getattr(conv, 'social_partner', None), str(source.id), R.SPLIT_RECEIVED)
    consume(conv, [str(r.id) for r, _, _ in parts])
    log_event('split_request', conversation=conv, mid=str(source.id)[:8], numbers=len(phones),
              amounts=amounts, staff=[u.id for u in staff], notified=notified, replied=bool(sent))

    result = {
        'success': True,
        'reply_fully_handled': bool(sent),
        'numbers': phones,
        'amounts': amounts,
        'staff_mentioned': [getattr(u.partner, 'name', '') for u in staff],
        'staff_notified': notified,
        'note_message_id': str(note_msg.id),
        'customer_reply_sent': bool(sent),
    }
    if not sent:
        result['note'] = f'The customer was NOT told. Reply «{R.SPLIT_RECEIVED}» quoted on {source.id}.'
    return result
