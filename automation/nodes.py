"""Bodies of the workflow-3 function nodes.

Each AI-Studio function node is a three-line ``execute`` that imports one of these.
The node receives the whole workflow state as ``input_data`` and the live
``conversation`` / ``partner`` objects as globals; every body returns the string
that becomes the WhatsApp reply — always ``''`` here (the automation sends its own
lines through the tools), except the router which returns the routing dict.

Nothing here may raise: a raised error would make the engine retry the node up to
``max_retries`` times, re-running the money path. Errors are logged, a human is
alerted, and the turn ends silently.
"""
import logging
from typing import Any, Dict

from .context import alert_human, log

logger = logging.getLogger(__name__)

ROUTE_NODE_ID = 'function_route'


def _route_of(input_data: Dict[str, Any], conversation, partner) -> Dict[str, Any]:
    """The router's result from the workflow state, or a fresh (side-effect-free) routing."""
    from .router import route
    try:
        results = (input_data or {}).get('__node_results__') or {}
        r = ((results.get(ROUTE_NODE_ID) or {}).get('output_data') or {}).get('__output__')
        if isinstance(r, dict) and r.get('intent'):
            return r
    except Exception:
        pass
    return route(conversation, partner, input_data)


def _guarded(name: str, fn, input_data, conversation, partner) -> str:
    try:
        route = _route_of(input_data, conversation, partner)
        fn(conversation, partner, route)
    except Exception as exc:
        logger.exception('automation node %s failed', name)
        log('node_error', conversation, node=name, error=str(exc)[:200])
        try:
            alert_human(conversation, partner, f'خطأ في الأتمتة ({name}): {str(exc)[:200]}')
        except Exception:
            pass
    return ''


def route_node(input_data, conversation, partner) -> Dict[str, Any]:
    from .router import route
    try:
        return route(conversation, partner, input_data)
    except Exception as exc:
        logger.exception('automation route failed')
        log('node_error', conversation, node='route', error=str(exc)[:200])
        return {'intent': 'freetext', 'sub': 'route_error', 'flags': {}, 'primary_id': None,
                'batch_ids': [], 'rows': [], 'secondary': [], 'report_date': ''}


def transfers_node(input_data, conversation, partner) -> str:
    from .transfers import run
    return _guarded('transfers', run, input_data, conversation, partner)


def balance_node(input_data, conversation, partner) -> str:
    from .intents import balance
    return _guarded('balance', balance, input_data, conversation, partner)


def statement_node(input_data, conversation, partner) -> str:
    from .intents import statement
    return _guarded('statement', statement, input_data, conversation, partner)


def status_node(input_data, conversation, partner) -> str:
    from .intents import status
    return _guarded('status', status, input_data, conversation, partner)


def cancel_node(input_data, conversation, partner) -> str:
    from .intents import cancel
    return _guarded('cancel', cancel, input_data, conversation, partner)


def social_node(input_data, conversation, partner) -> str:
    from .intents import social
    return _guarded('social', social, input_data, conversation, partner)


def off_hours_node(input_data, conversation, partner) -> str:
    from .intents import off_hours
    return _guarded('off_hours', off_hours, input_data, conversation, partner)


def noise_node(input_data, conversation, partner) -> str:
    from .intents import noise
    return _guarded('noise', noise, input_data, conversation, partner)


def freetext_context_node(input_data, conversation, partner) -> Dict[str, Any]:
    from .intents import freetext_context
    try:
        return freetext_context(conversation, partner, _route_of(input_data, conversation, partner))
    except Exception as exc:
        logger.exception('automation freetext context failed')
        return {'now': '', 'partner_name': '', 'customer_name': '', 'accounts': '', 'messages': '', 'primary_id': ''}


# The exact code pasted into each function node (kept here so the builder and the
# canvas never drift). `conversation` / `partner` are globals the engine injects.
NODE_CODE = {
    'function_route': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import route_node\n"
        "    return route_node(input_data, conversation, partner)\n"
    ),
    'function_transfers': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import transfers_node\n"
        "    return transfers_node(input_data, conversation, partner)\n"
    ),
    'function_balance': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import balance_node\n"
        "    return balance_node(input_data, conversation, partner)\n"
    ),
    'function_statement': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import statement_node\n"
        "    return statement_node(input_data, conversation, partner)\n"
    ),
    'function_status': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import status_node\n"
        "    return status_node(input_data, conversation, partner)\n"
    ),
    'function_cancel': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import cancel_node\n"
        "    return cancel_node(input_data, conversation, partner)\n"
    ),
    'function_social': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import social_node\n"
        "    return social_node(input_data, conversation, partner)\n"
    ),
    'function_off_hours': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import off_hours_node\n"
        "    return off_hours_node(input_data, conversation, partner)\n"
    ),
    'function_noise': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import noise_node\n"
        "    return noise_node(input_data, conversation, partner)\n"
    ),
    'function_freetext_context': (
        "def execute(input_data):\n"
        "    from qurtoba.automation.nodes import freetext_context_node\n"
        "    return freetext_context_node(input_data, conversation, partner)\n"
    ),
}
