"""The two manual switches on a Qurtoba WhatsApp account («إعدادات قرطبة»).

ai_agent_enabled   «تفعيل الرد الآلي (AI)»   OFF → the AI does nothing at all: no transaction,
                   no payment, no reply. The workflow gate ends the turn silently and marks the
                   messages handled, so switching the AI back on never replays them into a
                   transfer a human may already have made by hand.
qurtoba_off_hours  «وضع خارج مواعيد العمل»   ON → no transaction or payment of any kind; the
                   customer gets the off-hours notice. MANUAL ONLY: nothing here, or anywhere in
                   the automation, looks at the clock (owner decision 2026-09-13).

Both are read FRESH from the database on every call, not from the account object the run
loaded, so a switch flipped while a turn is running is honoured by the very next create.

Three layers read them, so no single miss can create money:
  1. the workflow gate node (``automation.nodes.gate_node``), the first node of the graph;
  2. the money path (``automation.nodes.transfers_node``);
  3. every AI create tool (``create_refusal``), the last line: it also covers a switch
     flipped mid-turn and any graph built before the gate existed.
Staff approval actions in the admin never go through the AI tools and are not affected.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

WHATSAPP_ACCOUNT_LABEL = 'whatsapp.whatsappaccount'


def _read_flag(account, field: str):
    """The live column value, inside a savepoint so a failed read can never poison the
    caller's transaction. Raises on any error; the caller decides which way to fail."""
    from django.db import transaction
    with transaction.atomic():
        return type(account)._base_manager.filter(pk=account.pk).values_list(field, flat=True).first()


def account_flags(conversation) -> Dict[str, bool]:
    """``{'ai_enabled': bool, 'off_hours': bool}`` for the conversation's channel account.

    No account, or a channel without these switches (only WhatsApp accounts carry them):
    open, there is nothing to switch. A failure to READ the AI switch fails CLOSED: money is
    never created on a guess, which is the whole point of the switch. A failure to read the
    off-hours switch fails open (the column may not exist yet during a deploy) and is logged.
    """
    flags = {'ai_enabled': True, 'off_hours': False}
    try:
        account = getattr(conversation, 'social_account', None) if conversation is not None else None
    except Exception:
        logger.exception('qurtoba.switches: could not resolve the account, treating the AI as OFF')
        return {'ai_enabled': False, 'off_hours': False}
    if account is None or getattr(account, 'pk', None) is None:
        return flags
    if account._meta.label_lower != WHATSAPP_ACCOUNT_LABEL:
        return flags

    try:
        value = _read_flag(account, 'ai_agent_enabled')
        if value is not None:
            flags['ai_enabled'] = bool(value)
    except Exception:
        logger.exception('qurtoba.switches: could not read ai_agent_enabled for account %s, treating the AI as OFF',
                         account.pk)
        return {'ai_enabled': False, 'off_hours': False}

    try:
        value = _read_flag(account, 'qurtoba_off_hours')
        if value is not None:
            flags['off_hours'] = bool(value)
    except Exception:
        logger.warning('qurtoba.switches: could not read qurtoba_off_hours for account %s, assuming it is off',
                       account.pk, exc_info=True)
    return flags


def create_refusal(conversation, *, what: str = 'transaction') -> Optional[Dict[str, Any]]:
    """None when the AI may create; otherwise the structured refusal an AI create tool returns."""
    flags = account_flags(conversation)
    if not flags['ai_enabled']:
        _log('switch_refused', conversation, reason='ai_disabled', what=what)
        return {
            'success': False,
            'error_type': 'ai_disabled',
            'error': (f'The AI is switched off for this account (إعدادات قرطبة): no {what} was created. '
                      'Do not reply and do not retry; a human handles this chat.'),
            'reply_fully_handled': True,
        }
    if flags['off_hours']:
        from qurtoba.automation.replies import OFF_HOURS
        _log('switch_refused', conversation, reason='off_hours', what=what)
        return {'success': False, 'error_type': 'off_hours', 'error': OFF_HOURS}
    return None


def _log(event: str, conversation, **fields) -> None:
    try:
        from qurtoba.tools._debuglog import log_event
        log_event(event, conversation=conversation, **fields)
    except Exception:
        pass
