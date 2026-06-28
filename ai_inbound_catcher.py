"""Durable catcher for the "AI/outbound message stored as INBOUND" bug.

Whenever a NEW inbound message is created that looks like it actually originated
from the AI / the business (so the agent would wrongly re-read its own reply as a
customer request), we append a full record — INCLUDING the Python stack trace of
who created it — to a durable log file. The file survives chat cleanups, so you
can open it anytime and the stack trace names the exact code path that produced
the misdirected row.

Log file:  /home/genie/genie/logs/ai_inbound_catcher.log

Detection signals (any one fires the catch):
  * sender is an AI/agent partner (ai_agent=True)
  * message text is a known agent artifact (👍 / تأكيد / تم تنفيذ / …)
  * the WhatsApp wamid (social_id) already exists on an OUTBOUND row  → self-echo
  * the inbound sender is NOT the conversation's customer (social_partner)
"""
import json
import logging
import os
import traceback
from datetime import datetime

from django.db.models.signals import post_save

logger = logging.getLogger('qurtoba.ai_inbound_catcher')

LOG_PATH = '/home/genie/genie/logs/ai_inbound_catcher.log'

# Agent reply artifacts — text that should only ever come FROM the agent.
_AI_MARKERS = (
    '👍', '✅', 'تأكيد', 'المبلغ لـ', 'الرقم للمبلغ', 'غير واضح', 'تم تنفيذ',
    'تم الإيقاف', 'لحظة', 'وعليكم السلام', 'تحت أمرك', 'من فضلك ارسل رقم',
    'أي رقم', 'مصاريف الخدمة', 'أرسل صورة الإيصال', 'باقي التحويلات',
    'الخدمة فورى', 'غير مدعومة', 'غير مسجل',
)

# AI/agent partner ids, resolved lazily once.
_ai_partner_ids = None


def _get_ai_partner_ids():
    global _ai_partner_ids
    if _ai_partner_ids is None:
        try:
            from modules.base.models import Partner
            _ai_partner_ids = set(
                Partner.all_objects.filter(ai_agent=True).values_list('id', flat=True)
            )
        except Exception:
            _ai_partner_ids = set()
    return _ai_partner_ids


def _detect(instance):
    """Return a short reason if this message will be SEEN as inbound/customer wrongly."""
    direction = getattr(instance, 'direction', None)
    content = instance.content
    text = content.get('text') if isinstance(content, dict) else None
    sp_id = getattr(getattr(instance, 'conversation', None), 'social_partner_id', None)

    if direction == 'inbound':
        # An AI/business message stored as an inbound row (data bug / echo).
        if instance.sender_id and instance.sender_id in _get_ai_partner_ids():
            return 'inbound_sender_is_ai_partner'
        if text and any(mk in text for mk in _AI_MARKERS):
            return 'inbound_text_is_agent_artifact'
        sid = getattr(instance, 'social_id', None)
        if sid:
            try:
                from modules.chat.models import Message
                if (Message.objects_all.filter(social_id=sid, direction='outbound')
                        .exclude(id=instance.id).exists()):
                    return 'inbound_wamid_matches_outbound_echo'
            except Exception:
                pass
        if sp_id and instance.sender_id and instance.sender_id != sp_id:
            return 'inbound_sender_not_customer'

    elif direction == 'outbound':
        # DISPLAY bug: an outbound (AI) message whose sender is the CUSTOMER /
        # a no-user partner renders on the customer side in the CRM, so the agent's
        # own reply looks inbound. This is the actual root cause (e.g. the
        # whatsapp_reply_to_message quoted reply sent without system_partner).
        try:
            if sp_id and instance.sender_id == sp_id:
                return 'OUTBOUND_attributed_to_customer_renders_as_inbound'
            sender = instance.sender
            if sender is not None and not getattr(sender, 'has_user', True):
                return 'OUTBOUND_sender_has_no_user_renders_as_inbound'
        except Exception:
            pass

    return None


def catch_ai_inbound(sender, instance, created, **kwargs):
    try:
        if not created:
            return
        reason = _detect(instance)
        if not reason:
            return

        content = instance.content
        text = content.get('text') if isinstance(content, dict) else str(content)
        stack = ''.join(traceback.format_stack(limit=30))
        header = {
            'caught_at': datetime.utcnow().isoformat() + 'Z',
            'reason': reason,
            'message_id': str(instance.id),
            'conversation_id': str(getattr(instance, 'conversation_id', None)),
            'direction': instance.direction,
            'sender_id': instance.sender_id,
            'social_id': getattr(instance, 'social_id', None),
            'type': instance.type,
            'text': text,
        }
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write('=' * 90 + '\n')
            fh.write(json.dumps(header, ensure_ascii=False, indent=2) + '\n')
            fh.write('--- creation stack ---\n')
            fh.write(stack + '\n')
        logger.error('AI-INBOUND CAUGHT [%s] msg=%s conv=%s text=%r',
                     reason, instance.id, getattr(instance, 'conversation_id', None),
                     (text or '')[:90])
    except Exception:
        logger.exception('ai_inbound_catcher: failed while inspecting a message')


def register():
    post_save.connect(catch_ai_inbound, sender=None, dispatch_uid='qurtoba_ai_inbound_catcher',
                      weak=False)
    # Connect specifically to the chat Message model.
    try:
        from modules.chat.models import Message
        post_save.disconnect(dispatch_uid='qurtoba_ai_inbound_catcher')
        post_save.connect(catch_ai_inbound, sender=Message,
                          dispatch_uid='qurtoba_ai_inbound_catcher', weak=False)
        logger.info('ai_inbound_catcher registered on chat.Message -> %s', LOG_PATH)
    except Exception:
        logger.exception('ai_inbound_catcher: failed to register')
