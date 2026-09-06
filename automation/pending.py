"""What the money path is HOLDING behind a fixed yes/no question, and how a yes/no settles it.

Markers (cache, per conversation):
    correction   «تقصد تحويل X على الرقم ده؟ ابعت حول»   → CORRECTION_KEY {type, value, account_number, source_message_id, correction_of}
    list         «تأكيد المطابقة: …»                        → LIST_KEY {pairs:[{account_number,value,source_message_id}]}
    high_value   «مبلغ كبير — محتاج تأكيد»                 → found in the chat (the tool's own hold)
    repeat       «…اتنفذت النهارده بالفعل. تحب أكررها؟»   → the create tool's repeat_pending store

``describe`` renders them for the model; ``answer_pending`` executes a yes or a no
deterministically (it never takes an amount from the model).
"""
from typing import Any, Dict, List, Optional

from . import replies as R
from .context import cache_delete, cache_get, call_tool, consume, log, send_quoted

CORRECTION_KEY = 'qurtoba:correction_pending:{conv}'
LIST_KEY = 'qurtoba:list_confirm:{conv}'
PENDING_MAX_AGE = 15 * 60   # a yes/no question the customer did not answer in 15 min is over


def _fresh(marker) -> bool:
    import time
    try:
        return bool(marker) and (time.time() - float(marker.get('ts') or 0)) <= PENDING_MAX_AGE
    except Exception:
        return False


def clear_pending(conversation) -> None:
    """The customer moved on (a transfer was created since): nothing is waiting any more."""
    key = _conv_key(conversation)
    cache_delete(CORRECTION_KEY.format(conv=key))
    cache_delete(LIST_KEY.format(conv=key))


def _conv_key(conversation) -> str:
    return str(getattr(conversation, 'id', ''))


def pending_state(conversation) -> Dict[str, Any]:
    """Everything the money path is waiting on, as data."""
    from qurtoba.tools.transactions import _list_repeat_pending
    from .transfers import _hv_question_pending
    key = _conv_key(conversation)
    out: Dict[str, Any] = {}
    c = cache_get(CORRECTION_KEY.format(conv=key))
    if c and _fresh(c):
        out['correction'] = c
    elif c:
        cache_delete(CORRECTION_KEY.format(conv=key))
    lst = cache_get(LIST_KEY.format(conv=key))
    if lst and lst.get('pairs') and _fresh(lst):
        out['list'] = lst
    elif lst:
        cache_delete(LIST_KEY.format(conv=key))
    hv_phone, hv_src = _hv_question_pending(conversation)
    if hv_phone:
        out['high_value'] = {'account_number': hv_phone, 'source_message_id': hv_src}
    rep = _list_repeat_pending(conversation) or {}
    if rep:
        out['repeat'] = [{'account_number': v.get('account_number'), 'value': v.get('value'), 'type': v.get('type')}
                         for v in rep.values() if isinstance(v, dict)]
    return out


def describe(conversation) -> List[str]:
    """Lines for the model's <money_path> block."""
    st = pending_state(conversation)
    lines: List[str] = []
    c = st.get('correction')
    if c:
        lines.append(f"  - waiting for «حول»/no: create {R._fmt(c.get('value'))} → {c.get('account_number')} "
                     f"({'a corrected number' if c.get('correction_of') else 'a question-shaped message'})")
    if st.get('list'):
        pairs = ', '.join(f"{p['account_number']} ← {R._fmt(p['value'])}" for p in st['list']['pairs'])
        lines.append(f'  - waiting for yes/no on the positional matching: {pairs}')
    if st.get('high_value'):
        lines.append(f"  - waiting for «تأكيد» on a HIGH-VALUE transfer to {st['high_value']['account_number']}")
    if st.get('repeat'):
        items = ', '.join(f"{r['account_number']} ← {R._fmt(r['value'])}" for r in st['repeat'])
        lines.append(f'  - waiting for yes/no on repeating today\'s transfer(s): {items}')
    return lines


def answer_pending(conversation, partner, decision: str, answer_message_id: Optional[str] = None) -> Dict[str, Any]:
    """Apply a yes/no to whatever is held (priority: correction, list, high value, repeat)."""
    from qurtoba.tools.transactions import (_clear_repeat_pending, qurtoba_confirm_pending_repeats,
                                             qurtoba_create_new_transactions_bulk)
    from qurtoba.tools.planning import _classify_message
    key = _conv_key(conversation)
    st = pending_state(conversation)
    yes = decision == 'yes'
    result: Dict[str, Any] = {'success': True, 'handled': False, 'kind': 'none', 'created': [], 'note': ''}

    # The customer's newest message quoting one of THEIR OWN other messages is about that
    # message, not about what we are holding («تأكيد» quoted on a different transfer).
    held_src = (st.get('correction') or {}).get('source_message_id') or (st.get('high_value') or {}).get('source_message_id')
    newest = _newest_inbound(conversation)
    q = getattr(newest, 'reply_to', None) if newest is not None else None
    if yes and st and q is not None and getattr(q, 'direction', None) == 'inbound' and held_src and str(q.id) != str(held_src):
        result['note'] = ('the reply quotes another customer message, not the held one — ask what they mean '
                          'before settling; nothing was executed')
        log('pending_answer', conversation, kind='quoted_elsewhere', yes=yes)
        return result

    if st.get('correction'):
        c = st['correction']
        cache_delete(CORRECTION_KEY.format(conv=key))
        result['kind'] = 'correction'
        if yes:
            res = call_tool(conversation, partner, qurtoba_create_new_transactions_bulk, transactions=[{
                'type': c.get('type') or 'كاش', 'value': float(c['value']), 'account_number': c['account_number'],
                'source_message_id': c.get('source_message_id')}])
            result.update(handled=True, created=_created(res), note='executed the held transfer')
            consume(conversation, [x for x in (c.get('correction_of'),) if x])
        else:
            consume(conversation, [x for x in (c.get('correction_of'), c.get('source_message_id')) if x])
            send_quoted(conversation, answer_message_id or c.get('source_message_id'), R.CORRECTION_DECLINED)
            result.update(handled=True, note='dropped the held transfer; the customer was told')
        log('pending_answer', conversation, kind='correction', yes=yes)
        return result

    if st.get('list'):
        lst = st['list']
        cache_delete(LIST_KEY.format(conv=key))
        result['kind'] = 'list'
        if yes:
            items = [{'type': 'كاش', 'value': float(p['value']), 'account_number': p['account_number'],
                      'source_message_id': p.get('source_message_id')} for p in lst['pairs']]
            res = call_tool(conversation, partner, qurtoba_create_new_transactions_bulk, transactions=items)
            result.update(handled=True, created=_created(res), note='executed the confirmed list')
        else:
            consume(conversation, [p.get('source_message_id') for p in lst['pairs'] if p.get('source_message_id')])
            send_quoted(conversation, answer_message_id or lst['pairs'][0].get('source_message_id'), R.DECLINED)
            result.update(handled=True, note='dropped the list; the customer was told')
        log('pending_answer', conversation, kind='list', yes=yes)
        return result

    if st.get('high_value'):
        hv = st['high_value']
        result['kind'] = 'high_value'
        from modules.chat.models import Message
        src = Message.objects_all.filter(conversation=conversation, id=hv['source_message_id']).first()
        txt = (src.content or {}).get('text', '') if src is not None and isinstance(src.content, dict) else ''
        amounts = _classify_message(txt).get('amounts') or []
        if yes and amounts:
            res = call_tool(conversation, partner, qurtoba_create_new_transactions_bulk, transactions=[{
                'type': 'كاش', 'value': float(amounts[0]), 'account_number': hv['account_number'],
                'source_message_id': hv['source_message_id'], 'confirm_high_value': True}])
            result.update(handled=True, created=_created(res), note='executed the confirmed high value')
        elif not yes:
            consume(conversation, [hv['source_message_id']])
            send_quoted(conversation, answer_message_id or hv['source_message_id'], R.DECLINED)
            result.update(handled=True, note='dropped the high-value transfer; the customer was told')
        log('pending_answer', conversation, kind='high_value', yes=yes)
        return result

    if st.get('repeat'):
        result['kind'] = 'repeat'
        if yes:
            res = call_tool(conversation, partner, qurtoba_confirm_pending_repeats)
            result.update(handled=True, created=[{'account_number': c.get('account_number'), 'value': c.get('value')}
                                                  for c in (res.get('created') or []) if isinstance(c, dict)],
                          note='repeated the held transfer(s)')
        else:
            _clear_repeat_pending(conversation)
            if answer_message_id:
                send_quoted(conversation, answer_message_id, R.REPEAT_DECLINED)
            result.update(handled=True, note='the repeat was dropped')
        log('pending_answer', conversation, kind='repeat', yes=yes)
        return result

    if not yes:
        # Nothing held, but an open (uncreated) number or amount is waiting for its other half —
        # «خلاص متبعتش» / «سيبك منها»: scrap it so a later stray amount can never complete it.
        from qurtoba.tools.conversation import qurtoba_clear_pending_transfers
        res = call_tool(conversation, partner, qurtoba_clear_pending_transfers)
        if res.get('cleared'):
            result.update(handled=True, kind='open_burst', note='the open number/amount was scrapped; the customer was told')
            return result
    result['note'] = 'nothing is pending'
    return result


def _newest_inbound(conversation):
    try:
        from modules.chat.models import Message
        return (Message.objects_all.filter(conversation=conversation, direction='inbound', active=True)
                .select_related('reply_to').order_by('-created_at').first())
    except Exception:
        return None


def _created(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{'account_number': r.get('account_number'), 'value': r.get('value'), 'status': r.get('status')}
            for r in (res.get('results') or []) if isinstance(r, dict)]
