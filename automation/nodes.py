"""Bodies of the workflow-3 function nodes.

Each AI-Studio function node is a three-line ``execute`` that imports one of these.
The node receives the whole workflow state as ``input_data`` and the live
``conversation`` / ``partner`` objects as globals.

Graph (see management/commands/qurtoba_workflow_v2.py):
    AI switch on? (gate) — off → ai_off ('' = nothing at all, messages marked handled)
    → linked? → route (off-hours switch / receipt / money) → transfers (creates, no model)
           → needs AI? → ai_context → agent_thinker → model_done (logs the turn time) | done ('' = silent turn)

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
    return {'ai_enabled': enabled}


def ai_off_node(input_data, conversation, partner) -> str:
    """The AI is switched off: end the turn with NOTHING (no transaction, no payment, no reply)
    and mark this turn's messages handled, so switching the AI back on never replays them into a
    transfer a human may already have made by hand."""
    from .context import consume
    try:
        from .router import load_batch_rows
        ids = [str(m.id) for m in load_batch_rows(conversation, input_data)]
        n = consume(conversation, ids)
        log('ai_off_consumed', conversation, count=n, batch=[i[:8] for i in ids])
    except Exception as exc:
        logger.exception('automation ai_off failed')
        log('node_error', conversation, node='ai_off', error=str(exc)[:200])
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
        texts = []
        for m in Message.objects_all.filter(conversation=conversation, id__in=route.get('batch_ids') or []).select_related('reply_to').order_by('created_at'):
            c = m.content if isinstance(m.content, dict) else {}
            q = getattr(m, 'reply_to', None)
            quote = ''
            if q is not None:
                qc = q.content if isinstance(q.content, dict) else {}
                who = 'you' if getattr(q, 'direction', None) == 'outbound' else 'the customer'
                quote = f' [replying to {who}: «{str(qc.get("text") or qc.get("caption") or "")[:60]}»]'
            texts.append(f"[message_id: {m.id}] ({m.type}){quote} {str(c.get('text') or c.get('transcription') or c.get('caption') or '')[:300]}")
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
    except Exception as exc:
        logger.exception('automation model_done failed')
        log('node_error', conversation, node='model_done', error=str(exc)[:200])
    # Every customer-facing word goes through the reply tool; the model's plain output («Done»,
    # a summary) is thrown away here — so nothing ever reaches the outbound gate from it.
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
    'function_ai_off': _code('ai_off_node'),
    'function_route': _code('route_node'),
    'function_transfers': _code('transfers_node'),
    'function_off_hours': _code('off_hours_node'),
    'function_done': _code('done_node'),
    'function_ai_context': _code('ai_context_node'),
    'function_model_done': _code('model_done_node'),
}
