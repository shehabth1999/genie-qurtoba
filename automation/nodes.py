"""Bodies of the workflow-3 function nodes.

Each AI-Studio function node is a three-line ``execute`` that imports one of these.
The node receives the whole workflow state as ``input_data`` and the live
``conversation`` / ``partner`` objects as globals.

Graph (see management/commands/qurtoba_workflow_v2.py):
    AI switch on? (gate) — off → ai_off ('' = nothing at all, messages marked handled)
    → linked? → route (off-hours switch / receipt / money) → transfers (creates, no model)
           → needs AI? → ai_context → agent_thinker → model_done (logs the turn time) | done ('' = silent turn)
    off-hours → off_hours_context → agent_off_hours (balance + statement + reply tools only)
           → off_hours_done (fixed closed notice if nothing reached the customer; messages marked handled)

Both switches (AI on/off, manual off-hours) are read by ``qurtoba.switches``, never the clock.

Nothing here may raise: a raised error would make the engine retry the node up to
``max_retries`` times, re-running the money path. Errors are logged, a human is
alerted, and the turn ends silently.
"""
import logging
from typing import Any, Dict

from .context import alert_human, log

logger = logging.getLogger(__name__)

ROUTE_NODE_ID = 'function_route'
TRANSFERS_NODE_ID = 'function_transfers'


def _node_output(input_data: Dict[str, Any], node_id: str):
    try:
        results = (input_data or {}).get('__node_results__') or {}
        return ((results.get(node_id) or {}).get('output_data') or {}).get('__output__')
    except Exception:
        return None


def _route_of(input_data: Dict[str, Any], conversation, partner) -> Dict[str, Any]:
    from .router import route
    r = _node_output(input_data, ROUTE_NODE_ID)
    if isinstance(r, dict) and r.get('intent'):
        return r
    return route(conversation, partner, input_data)


def _nothing_for_ai(summary: str) -> Dict[str, Any]:
    """A money-path result that created nothing and leaves nothing for the model."""
    return {'items': 0, 'replies': 0, 'created': [], 'leftovers': [], 'others': [], 'needs_ai': False,
            'summary': summary}


def gate_node(input_data, conversation, partner) -> Dict[str, Any]:
    """First node of the graph: is the AI switched on for this account (إعدادات قرطبة)?

    ``ai_enabled`` False routes the turn to ``ai_off_node``: nothing runs, no transaction, no
    payment, no reply. Never raises; a failure reads as OFF (fail closed)."""
    try:
        from qurtoba.switches import account_flags
        enabled = bool(account_flags(conversation).get('ai_enabled'))
    except Exception as exc:
        logger.exception('automation gate failed, treating the AI as off')
        log('node_error', conversation, node='gate', error=str(exc)[:200])
        enabled = False
    if not enabled:
        log('ai_off', conversation)
    out = {'ai_enabled': enabled, 'linked': bool(getattr(partner, 'qurtoba_customer_id', None))}
    try:
        from qurtoba.groups import is_group
        if enabled and is_group(conversation):
            out['linked'] = bool(_group_link(conversation, partner))
    except Exception as exc:
        logger.exception('automation gate: group link failed')
        log('node_error', conversation, node='gate_group_link', error=str(exc)[:200])
        out['linked'] = False
    return out


def _group_link(conversation, partner):
    """A WhatsApp customer group: its Qurtoba customer (linked automatically when exactly one customer is
    among the members), and the connected number marked staff. ``partner`` is the group's placeholder."""
    from .context import cache_get, cache_set
    from qurtoba.groups import customer_inbound, ensure_group_link, mark_own_number_staff
    account = getattr(conversation, 'social_account', None)
    if account is not None and not cache_get(f'qurtoba:own_number_staff:{account.pk}'):
        mark_own_number_staff(account)
        cache_set(f'qurtoba:own_number_staff:{account.pk}', 1, 3600)
    speakers = [m.sender for m in customer_inbound(conversation).filter(ai_consumed_at__isnull=True)
                .select_related('sender').order_by('-created_at')[:10] if m.sender_id]
    return ensure_group_link(conversation, partner, speakers=speakers)


def ai_off_node(input_data, conversation, partner) -> str:
    """The AI is switched off: end the turn with NOTHING (no transaction, no payment, no reply)
    and mark this turn's messages handled, so switching the AI back on never replays them into a
    transfer a human may already have made by hand."""
    from .context import consume
    try:
        from .router import load_batch_rows
        ids = [str(m.id) for m in load_batch_rows(conversation, input_data)]
        n = consume(conversation, ids)
        from qurtoba.switches import mark_offline_cancelled
        mark_offline_cancelled(conversation, ids, 'ai_off')      # cancelled on arrival: never a transfer later
        log('ai_off_consumed', conversation, count=n, batch=[i[:8] for i in ids])
    except Exception as exc:
        logger.exception('automation ai_off failed')
        log('node_error', conversation, node='ai_off', error=str(exc)[:200])
    return ''


NOT_LINKED_ONCE_MINUTES = 60


def _text_sent_recently(conversation, text: str, minutes: int) -> bool:
    """True when this exact outbound text already went to this conversation within `minutes`."""
    from datetime import timedelta
    from django.utils import timezone
    from modules.chat.models import Message
    want = ' '.join(str(text).split())
    for m in (Message.objects_all.filter(conversation=conversation, direction='outbound', type='text',
                                         created_at__gte=timezone.now() - timedelta(minutes=minutes))
              .order_by('-created_at')[:30]):
        c = m.content if isinstance(m.content, dict) else {}
        if ' '.join(str(c.get('text') or '').split()) == want:
            return True
    return False


def not_linked_node(input_data, conversation, partner) -> str:
    """The WhatsApp number is not linked to any Qurtoba customer. Nothing runs; the office's notice goes out ONCE
    per conversation per hour — a retried or parallel run can never send it twice (2026-09-10, conversation
    fd4a2734: a crashed run was retried and the notice went out twice); every message of the turn is marked
    handled and CANCELLED ON ARRIVAL, so linking the account later never turns it into a transfer
    (owner decision 2026-09-14)."""
    from django.core.cache import cache
    from . import replies as R
    from .context import consume, send_plain
    try:
        from .router import load_batch_rows
        ids = [str(m.id) for m in load_batch_rows(conversation, input_data)]
        from qurtoba.groups import is_group
        if is_group(conversation):
            # A customer group nobody linked yet: nothing is said INSIDE the group — the office already got
            # «اربط الجروب ده بعميل» (qurtoba.groups.ensure_group_link, once an hour). Same refusal as 1:1.
            consume(conversation, ids)
            from qurtoba.switches import mark_offline_cancelled
            mark_offline_cancelled(conversation, ids, 'not_linked')
            log('not_linked', conversation, batch=[i[:8] for i in ids], group=True)
            return ''
        key = f'qurtoba:not_linked_notice:{conversation.id}'
        if cache.add(key, 1, NOT_LINKED_ONCE_MINUTES * 60):
            if _text_sent_recently(conversation, R.NOT_LINKED, NOT_LINKED_ONCE_MINUTES):
                log('not_linked_notice_skip', conversation, why='already_in_chat')
            elif not send_plain(conversation, R.NOT_LINKED):
                cache.delete(key)                 # not delivered: the next message may try again
        else:
            log('not_linked_notice_skip', conversation, why='sent_this_hour')
        consume(conversation, ids)
        from qurtoba.switches import mark_offline_cancelled
        mark_offline_cancelled(conversation, ids, 'not_linked')
        log('not_linked', conversation, batch=[i[:8] for i in ids])
    except Exception as exc:
        logger.exception('automation not_linked failed')
        log('node_error', conversation, node='not_linked', error=str(exc)[:200])
    return ''


def route_node(input_data, conversation, partner) -> Dict[str, Any]:
    from .router import route
    try:
        return route(conversation, partner, input_data)
    except Exception as exc:
        logger.exception('automation route failed')
        log('node_error', conversation, node='route', error=str(exc)[:200])
        return {'intent': 'transfer', 'batch_ids': [], 'rows': [], 'off_hours': False, 'quoted_id': None}


def transfers_node(input_data, conversation, partner) -> Dict[str, Any]:
    """Create every clean transfer now; report what is left for the AI."""
    from .transfers import run
    # Safety net behind the gate node: a graph built before the gate existed, or a switch
    # flipped between the gate and here, must still create NOTHING. Read fresh.
    try:
        from qurtoba.switches import account_flags
        flags = account_flags(conversation)
    except Exception:
        logger.exception('automation transfers: switch read failed, creating nothing')
        flags = {'ai_enabled': False, 'off_hours': False}
    if not flags.get('ai_enabled'):
        ai_off_node(input_data, conversation, partner)
        return _nothing_for_ai('The AI is switched off for this account: nothing was created.')
    if flags.get('off_hours'):
        off_hours_node(input_data, conversation, partner)
        return _nothing_for_ai('The off-hours switch is on: nothing was created.')
    try:
        return run(conversation, partner, _route_of(input_data, conversation, partner))
    except Exception as exc:
        logger.exception('automation transfers failed')
        log('node_error', conversation, node='transfers', error=str(exc)[:200])
        try:
            alert_human(conversation, partner, f'خطأ في مسار التحويلات التلقائي: {str(exc)[:200]}')
        except Exception:
            pass
        # let the AI look at the turn rather than leaving the customer with nothing
        return {'items': 0, 'replies': 0, 'created': [], 'leftovers': [], 'others': [], 'needs_ai': True,
                'summary': 'The automatic money path failed on this turn (a human was alerted). Read the messages yourself; '
                           'never create a transfer.'}


def off_hours_node(input_data, conversation, partner) -> str:
    from . import replies as R
    from .context import consume, said_recently, send_quoted
    try:
        route = _route_of(input_data, conversation, partner)
        mid = (route.get('batch_ids') or [None])[-1]
        if mid and not said_recently(conversation, mid, R.OFF_HOURS, minutes=120):
            send_quoted(conversation, mid, R.OFF_HOURS)
        consume(conversation, route.get('batch_ids') or [])
        from qurtoba.switches import mark_offline_cancelled
        mark_offline_cancelled(conversation, route.get('batch_ids') or [], 'off_hours')
    except Exception as exc:
        logger.exception('automation off-hours failed')
        log('node_error', conversation, node='off_hours', error=str(exc)[:200])
    return ''


def done_node(input_data, conversation, partner) -> str:
    """Silent end of a turn the money path fully settled."""
    return ''


def ai_context_node(input_data, conversation, partner) -> Dict[str, Any]:
    """What the thinking model gets: time, customer, accounts, the money-path summary, the messages."""
    from django.utils import timezone
    try:
        route = _route_of(input_data, conversation, partner)
        summary = _node_output(input_data, TRANSFERS_NODE_ID)
        if not isinstance(summary, dict):
            summary = {}
        customer = getattr(partner, 'qurtoba_customer', None)
        from modules.chat.models import Message
        from qurtoba.groups import is_group, is_staff, staff_lines_since
        group = is_group(conversation)
        texts = []
        first_at = None
        for m in (Message.objects_all.filter(conversation=conversation, id__in=route.get('batch_ids') or [])
                  .select_related('reply_to', 'reply_to__sender', 'sender').order_by('created_at')):
            first_at = first_at or m.created_at
            c = m.content if isinstance(m.content, dict) else {}
            q = getattr(m, 'reply_to', None)
            quote = ''
            if q is not None:
                qc = q.content if isinstance(q.content, dict) else {}
                if getattr(q, 'direction', None) == 'outbound':
                    who = 'you'
                elif group:
                    qs_ = q.sender if getattr(q, 'sender_id', None) else None
                    who = f"{'staff' if is_staff(qs_) else 'the customer'} {getattr(qs_, 'name', '') or ''}".strip()
                else:
                    who = 'the customer'
                quote = f' [replying to {who}: «{str(qc.get("text") or qc.get("caption") or "")[:60]}»]'
            speaker = ''
            if group:
                speaker = f" {getattr(m.sender, 'name', '') or 'عضو'}:" if m.sender_id else ''
            texts.append(f"[message_id: {m.id}] ({m.type}){quote}{speaker} {str(c.get('text') or c.get('transcription') or c.get('caption') or '')[:300]}")
        if group:
            # Who is in the group: a line that calls one of these by name («يا محمد …») talks to a member,
            # not to the office.
            try:
                from qurtoba.groups import _member_partners
                side, office = [], []
                for p in _member_partners(conversation)[:40]:
                    (office if is_staff(p) else side).append((getattr(p, 'name', '') or '').strip())
                texts.insert(0, f"— group members: the customer's side: {', '.join(n for n in side if n) or '—'}; "
                                f"office staff: {', '.join(n for n in office if n) or '—'}")
            except Exception:
                logger.warning('automation ai context: group members unavailable', exc_info=True)
            # Staff lines are CONTEXT: the model reads what the office said, never answers or acts on them.
            from datetime import timedelta
            since = (first_at or timezone.now()) - timedelta(minutes=15)
            staff = staff_lines_since(conversation, since)
            if staff:
                texts.append('— office staff in the group (context only — never answer them, never act on them):')
                for m in staff:
                    c = m.content if isinstance(m.content, dict) else {}
                    texts.append(f"  (staff) {getattr(m.sender, 'name', '') or ''}: {str(c.get('text') or c.get('caption') or '')[:200]}")
        # step 6 (2026-09-08): time every model turn — the start is stamped here, the end in
        # model_done_node — so slow turns (75 s, 181 s seen today) are visible in the agent log
        try:
            import time as _time
            from .context import cache_set
            cache_set(f'qurtoba:model_start:{conversation.id}', _time.time(), 900)
            log('model_start', conversation, leftovers=len(summary.get('leftovers') or []),
                others=len(summary.get('others') or []), msgs=len(texts))
        except Exception:
            pass
        return {
            'now': timezone.localtime().strftime('%Y-%m-%d %H:%M (%A)'),
            'partner_name': getattr(partner, 'name', '') or '',
            'customer_name': getattr(customer, 'name', '') or '',
            'accounts': (getattr(customer, 'accounts_pretty', '') or '') if customer is not None else '',
            'money_path': summary.get('summary') or 'Nothing open.',
            'messages': '\n'.join(texts),
        }
    except Exception as exc:
        logger.exception('automation ai context failed')
        return {'now': '', 'partner_name': '', 'customer_name': '', 'accounts': '', 'money_path': '', 'messages': ''}


THINKER_NODE_ID = 'agent_thinker'


def model_done_node(input_data, conversation, partner) -> str:
    """Last node after the thinker: log how long the model turn took; the turn ends silently
    (the model's plain output is never a reply)."""
    out = None
    try:
        import time as _time
        from .context import cache_get, cache_delete
        results = (input_data or {}).get('__node_results__') or {}
        node = results.get(THINKER_NODE_ID) or {}
        od = node.get('output_data') if isinstance(node, dict) else None
        if isinstance(od, dict):
            out = od.get('__output__')
            if out is None:
                out = od.get('response') or od.get('output') or od.get('content')
        started = cache_get(f'qurtoba:model_start:{conversation.id}')
        secs = round(_time.time() - float(started), 1) if started else None
        cache_delete(f'qurtoba:model_start:{conversation.id}')
        log('model_done', conversation, seconds=secs, out_len=len(str(out or '')),
            tokens=od.get('tokens_used') if isinstance(od, dict) else None,
            model=od.get('model') if isinstance(od, dict) else None)
        # The agent node swallows a provider failure into {'error', 'fallback': True} (core
        # node_executor): the customer would get nothing. Never silent — one holding line and the
        # office is told (14 Sep 2026: five unanswered turns during a DeepSeek outage).
        if isinstance(od, dict) and (od.get('fallback') or od.get('error')):
            _model_failed_fallback(conversation, partner, input_data, str(od.get('error') or 'model failed')[:200])
    except Exception as exc:
        logger.exception('automation model_done failed')
        log('node_error', conversation, node='model_done', error=str(exc)[:200])
    # Every customer-facing word goes through the reply tool; the model's plain output («Done»,
    # a summary) is thrown away here — so nothing ever reaches the outbound gate from it.
    return ''


def _model_failed_fallback(conversation, partner, input_data, error: str) -> None:
    """The thinking model failed (primary and backup): if nothing reached the customer for this
    batch, send «ثواني وهنرد على حضرتك 🙏» once, quoted on their newest message, and post one internal
    note that mentions the office staff. Both are deduplicated so a retried run adds nothing."""
    from .context import send_quoted, log
    from . import replies as R
    try:
        from modules.chat.models import Message
        route = _route_of(input_data, conversation, partner)
        mids = [str(x) for x in (route.get('batch_ids') or []) if x]
        first = Message.objects_all.filter(id__in=mids).order_by('created_at').first() if mids else None
        reached = first is not None and Message.objects_all.filter(
            conversation=conversation, direction='outbound', is_internal=False, created_at__gt=first.created_at,
        ).exclude(type__in=('tool', 'tool_call')).exists()
        if not reached and mids:
            send_quoted(conversation, mids[-1], R.MODEL_DOWN, once_minutes=30)
        from qurtoba.staff_notes import post_staff_note
        post_staff_note(
            conversation,
            ['⚠️ الموديل وقع ومردّ على العميل',
             f'الخطأ: {error}',
             'العميل اتبلغ «ثواني وهنرد على حضرتك» — محتاج رد يدوي على رسالته.'],
            subject='⚠️ الموديل وقع — رد يدوي مطلوب',
            body=f'{getattr(partner, "name", "") or ""}: الموديل فشل يرد — محتاج رد يدوي.',
            dedupe_key=f'model_down:{conversation.id}', dedupe_ttl=1800,
        )
        log('model_failed', conversation, error=error, customer_told=not reached)
    except Exception:
        logger.exception('automation model fallback failed')


OFF_HOURS_AGENT_NODE_ID = 'agent_off_hours'


def off_hours_context_node(input_data, conversation, partner) -> Dict[str, Any]:
    """What the off-hours agent gets: time, customer, the day a statement should cover, the messages.

    ``statement_day`` is the business day that has just ended (``reporting_day``: before noon it is
    yesterday). It only picks WHICH day a statement shows at night; whether the office is closed is
    decided by the manual switch alone, never by the clock."""
    ctx = ai_context_node(input_data, conversation, partner)
    out = {
        'now': ctx.get('now', ''),
        'partner_name': ctx.get('partner_name', ''),
        'customer_name': ctx.get('customer_name', ''),
        # Not 'messages': a function node's keys are merged into the workflow state, where
        # 'messages' is the engine's own chat-history channel.
        'inbound_messages': ctx.get('messages', ''),
        'statement_day': '',
    }
    try:
        from qurtoba.services.daily_totals import reporting_day
        out['statement_day'] = reporting_day().isoformat()
    except Exception:
        logger.warning('automation off-hours: reporting_day failed', exc_info=True)
    return out


def _outbound_since_batch(conversation, batch_ids) -> bool:
    """True when anything reached the customer after the first message of this turn: a quoted
    refusal, the balance line, the statement document. Tool trace rows do not count."""
    from modules.chat.models import Message
    first = (Message.objects_all.filter(conversation=conversation, id__in=[str(i) for i in batch_ids])
             .order_by('created_at').values_list('created_at', flat=True).first())
    if first is None:
        return False
    return (Message.objects_all.filter(conversation=conversation, direction='outbound', created_at__gte=first)
            .exclude(type__in=('tool', 'tool_call')).exists())


def off_hours_done_node(input_data, conversation, partner) -> str:
    """After the off-hours agent. If nothing at all reached the customer (the model failed or stayed
    silent), send the fixed closed notice so they are never left in silence. Then mark the turn's
    messages handled. The model's plain output is thrown away, exactly like model_done_node."""
    from . import replies as R
    from .context import cache_delete, cache_get, consume, said_recently, send_quoted
    try:
        route = _route_of(input_data, conversation, partner)
        ids = route.get('batch_ids') or []
        try:
            import time as _time
            node = ((input_data or {}).get('__node_results__') or {}).get(OFF_HOURS_AGENT_NODE_ID) or {}
            od = node.get('output_data') if isinstance(node, dict) else None
            started = cache_get(f'qurtoba:model_start:{conversation.id}')
            cache_delete(f'qurtoba:model_start:{conversation.id}')
            log('off_hours_model_done', conversation,
                seconds=round(_time.time() - float(started), 1) if started else None,
                model=od.get('model') if isinstance(od, dict) else None)
        except Exception:
            pass
        if ids and not _outbound_since_batch(conversation, ids):
            mid = ids[-1]
            if not said_recently(conversation, mid, R.OFF_HOURS, minutes=120):
                send_quoted(conversation, mid, R.OFF_HOURS)
            log('off_hours_fallback', conversation, mid=str(mid)[:8])
        consume(conversation, ids)
        from qurtoba.switches import mark_offline_cancelled
        mark_offline_cancelled(conversation, ids, 'off_hours')   # cancelled on arrival: never a transfer later
    except Exception as exc:
        logger.exception('automation off-hours done failed')
        log('node_error', conversation, node='off_hours_done', error=str(exc)[:200])
    return ''


# The exact code pasted into each function node (kept here so the builder and the
# canvas never drift). `conversation` / `partner` are globals the engine injects.
def _code(fn: str) -> str:
    return (
        "def execute(input_data):\n"
        f"    from qurtoba.automation.nodes import {fn}\n"
        f"    return {fn}(input_data, conversation, partner)\n"
    )


NODE_CODE = {
    'function_gate': _code('gate_node'),
    'function_not_linked': _code('not_linked_node'),
    'function_ai_off': _code('ai_off_node'),
    'function_route': _code('route_node'),
    'function_transfers': _code('transfers_node'),
    'function_off_hours': _code('off_hours_node'),
    'function_off_hours_context': _code('off_hours_context_node'),
    'function_off_hours_done': _code('off_hours_done_node'),
    'function_done': _code('done_node'),
    'function_ai_context': _code('ai_context_node'),
    'function_model_done': _code('model_done_node'),
}
