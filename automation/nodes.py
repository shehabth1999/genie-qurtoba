"""Bodies of the workflow-3 function nodes.

Each AI-Studio function node is a three-line ``execute`` that imports one of these.
The node receives the whole workflow state as ``input_data`` and the live
``conversation`` / ``partner`` objects as globals.

Graph (see management/commands/qurtoba_workflow_v2.py):
    linked? → route (receipt / off-hours / money) → transfers (creates, no model)
           → needs AI? → ai_context → agent_thinker | done ('' = silent turn)

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


# The exact code pasted into each function node (kept here so the builder and the
# canvas never drift). `conversation` / `partner` are globals the engine injects.
def _code(fn: str) -> str:
    return (
        "def execute(input_data):\n"
        f"    from qurtoba.automation.nodes import {fn}\n"
        f"    return {fn}(input_data, conversation, partner)\n"
    )


NODE_CODE = {
    'function_route': _code('route_node'),
    'function_transfers': _code('transfers_node'),
    'function_off_hours': _code('off_hours_node'),
    'function_done': _code('done_node'),
    'function_ai_context': _code('ai_context_node'),
}
