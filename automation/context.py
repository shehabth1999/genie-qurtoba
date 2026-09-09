"""Glue between the automation and the existing tools / send paths.

* ``call_tool`` runs a registered @tool exactly as the agent would (same context
  object, same side effects) and leaves the same ``tool_call`` / ``tool`` trace rows
  in the chat, so the CRM chat view and the eval runner see what happened.
* ``send_quoted`` / ``send_plain`` deliver fixed lines through the system-send path
  (the outbound gate lets system text through untouched) and mark the turn answered.
* ``consume`` watermarks inbound rows the automation has finished with.
"""
import contextvars
import json
import logging
import time
from datetime import timedelta
from typing import Any, Callable, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

AUTOMATION_TAG = 'qurtoba_automation'
_REPLYING = contextvars.ContextVar('qurtoba_automation_reply', default=False)


def in_automation_reply() -> bool:
    """True while the automation is delivering one of its own customer-facing lines
    (the eval scorer counts those as the agent's replies)."""
    return bool(_REPLYING.get())


def tool_context(conversation, partner):
    from modules.aistudio.tools.context import ToolExecutionContext
    if partner is None and conversation is not None:
        partner = getattr(conversation, 'social_partner', None)
    return ToolExecutionContext(partner=partner, conversation=conversation,
                                workflow_type='partner_flow', trigger_source='automation')


def _ai_partner():
    from qurtoba.extensions import system_sender
    return system_sender()


def _trace(conversation, tool_name: str, tool_input: Dict[str, Any], tool_output: Any) -> None:
    """Write the tool_call / tool pair the agent executor writes, so the chat shows the trace."""
    try:
        from django.db import transaction
        from django.utils import timezone
        from modules.chat.models import Message
        sender = _ai_partner()
        if sender is None or conversation is None:
            return
        call_id = f'auto_{int(time.time() * 1000)}'
        out = tool_output if isinstance(tool_output, str) else json.dumps(tool_output, ensure_ascii=False, default=str)
        with transaction.atomic():
            now = timezone.now()
            Message.objects.create(
                conversation=conversation, sender=sender, type='tool_call', direction='outbound', created_at=now,
                content={'tool_call_id': call_id, 'tool_name': tool_name, 'tool_input': tool_input,
                         'ai_content': AUTOMATION_TAG},
            )
            Message.objects.create(
                conversation=conversation, sender=sender, type='tool', direction='outbound',
                created_at=now + timedelta(microseconds=1),
                content={'tool_call_id': call_id, 'tool_name': tool_name, 'tool_output': out[:20000]},
            )
    except Exception:
        logger.warning('automation: could not write tool trace for %s', tool_name, exc_info=True)


def call_tool(conversation, partner, fn: Callable, **kwargs) -> Dict[str, Any]:
    """Run a @tool function with a live context; never raises — errors come back as a dict."""
    name = getattr(getattr(fn, '_tool_info', None), 'name', None) or getattr(fn, '__name__', 'tool')
    ctx = tool_context(conversation, partner)
    try:
        result = fn(ctx, **kwargs)
    except Exception as exc:
        logger.exception('automation: tool %s raised', name)
        result = {'success': False, 'error_type': 'exception', 'error': str(exc)}
    _trace(conversation, name, kwargs, result)
    log('tool', conversation, tool=name, ok=bool(isinstance(result, dict) and result.get('success')))
    return result if isinstance(result, dict) else {'success': True, 'result': result}


def send_quoted(conversation, message_id: Optional[str], text: str, *, once_minutes: int = 15) -> bool:
    """Send `text` quoted on inbound `message_id` (unquoted if it cannot be resolved).

    Idempotent per message: the same line is never sent twice on the same message
    within `once_minutes` (the burst may be re-planned on every turn of the window).
    """
    if not text:
        return False
    if message_id and said_recently(conversation, message_id, text, minutes=once_minutes):
        log('reply_skip', conversation, mid=str(message_id)[:8], text=text[:40])
        return False
    from qurtoba.tools.transactions import _send_quoted_text
    partner = getattr(conversation, 'social_partner', None)
    token = _REPLYING.set(True)
    try:
        ok = bool(_send_quoted_text(conversation, partner, message_id, text))
    finally:
        _REPLYING.reset(token)
    log('reply', conversation, mid=str(message_id)[:8] if message_id else None, text=text[:60], ok=ok)
    return ok


def send_plain(conversation, text: str) -> bool:
    """A standalone (unquoted) fixed line through the system-send path."""
    if not text:
        return False
    try:
        from modules.chat.services.omnichannel_send_service import OmnichannelSendService
        from qurtoba.ai_guard import mark_reply_delivered, system_send
        from qurtoba.extensions import _get_system_partner
        token = _REPLYING.set(True)
        try:
            with system_send():
                OmnichannelSendService().send_and_broadcast(
                    partner=conversation.social_partner, content={'text': str(text)}, message_type='text',
                    conversation=conversation, system_partner=_get_system_partner(conversation), websocket=True,
                )
        finally:
            _REPLYING.reset(token)
        mark_reply_delivered(conversation)
        log('reply', conversation, text=text[:60], ok=True, plain=True)
        return True
    except Exception:
        logger.warning('automation: plain send failed', exc_info=True)
        return False


def said_recently(conversation, message_id, text: str, *, minutes: int = 15) -> bool:
    """True if an outbound with this exact text already quotes `message_id` recently."""
    try:
        from django.utils import timezone
        from modules.chat.models import Message
        qs = Message.objects_all.filter(
            conversation=conversation, direction='outbound', type='text', reply_to_id=str(message_id),
            created_at__gte=timezone.now() - timedelta(minutes=minutes),
        )
        norm = ' '.join(str(text).split())
        for m in qs.order_by('-created_at')[:10]:
            c = m.content if isinstance(m.content, dict) else {}
            if ' '.join(str(c.get('text') or '').split()) == norm:
                return True
    except Exception:
        logger.warning('automation: said_recently lookup failed', exc_info=True)
    return False


def asked_recently(conversation, message_id, *, minutes: int = 15) -> bool:
    """True if any outbound question already quotes `message_id` recently."""
    try:
        from django.utils import timezone
        from modules.chat.models import Message
        for m in Message.objects_all.filter(
                conversation=conversation, direction='outbound', type='text', reply_to_id=str(message_id),
                created_at__gte=timezone.now() - timedelta(minutes=minutes)).order_by('-created_at')[:10]:
            c = m.content if isinstance(m.content, dict) else {}
            if '؟' in str(c.get('text') or '') or '?' in str(c.get('text') or ''):
                return True
    except Exception:
        pass
    return False


def consume(conversation, message_ids: Iterable[str]) -> int:
    """Watermark inbound rows so no later turn re-reads them."""
    n = 0
    ids = [str(i) for i in message_ids if i]
    if not ids:
        return 0
    try:
        from modules.chat.models import Message
        for m in Message.objects_all.filter(conversation=conversation, id__in=ids, ai_consumed_at__isnull=True):
            if m.mark_ai_consumed(None):
                n += 1
    except Exception:
        logger.warning('automation: consume failed', exc_info=True)
    return n


def alert_human(conversation, partner, note: str) -> None:
    try:
        from qurtoba.tools.conversation import alert_qurtoba_human
        call_tool(conversation, partner, alert_qurtoba_human, note=note[:500])
    except Exception:
        logger.warning('automation: alert_human failed', exc_info=True)


def log(event: str, conversation=None, **fields) -> None:
    try:
        from qurtoba.tools._debuglog import log_event
        log_event(f'auto_{event}', conversation=conversation, **fields)
    except Exception:
        pass


def cache_get(key: str):
    try:
        from django.core.cache import cache
        return cache.get(key)
    except Exception:
        return None


def cache_set(key: str, value, ttl: int) -> None:
    try:
        from django.core.cache import cache
        cache.set(key, value, ttl)
    except Exception:
        pass


def cache_delete(key: str) -> None:
    try:
        from django.core.cache import cache
        cache.delete(key)
    except Exception:
        pass
