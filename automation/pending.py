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
        out['repeat'] = [{'account_number': v.get('account_number'), 'value': v.get('value'), 'type': v.get('type'),
                          'source_message_id': v.get('source_message_id'), 'asked_ts': v.get('asked_ts')}
                         for v in rep.values() if isinstance(v, dict)]
    return out


# The fixed openings of every yes/no question the money path asks. The question's TIME is read from
# its outbound chat row, not from the cache marker: the marker is re-stamped whenever a still-open
# burst is re-planned (every later turn), so on 2026-09-20 «yes please» looked OLDER than the
# question it answered and was refused (scenario Y21).
_QUESTION_OPENINGS = ('الرقم اللي فات كان غلط', 'المبلغ لـ', 'الأرقام والمبالغ وصلت كقائمتين', 'مبلغ كبير',
                      'عملية كاش', 'تمام — الرقم اللي عليه', 'تمام — المبلغ لـ')


def _question_time(conversation, st: Dict[str, Any]) -> Optional[float]:
    """Unix time of the most recent question behind what is held, or None when unknown."""
    try:
        from datetime import timedelta
        from django.db.models import Q
        from django.utils import timezone
        from modules.chat.models import Message
        cond = Q()
        for opening in _QUESTION_OPENINGS:
            cond |= Q(content__text__startswith=opening)
        q = (Message.objects_all.filter(conversation=conversation, direction='outbound', type='text', active=True,
                                        is_internal=False, created_at__gte=timezone.now() - timedelta(hours=6))
             .filter(cond).order_by('-created_at').first())
        if q is not None:
            return q.created_at.timestamp()
    except Exception:
        pass
    ts: List[float] = []                      # no row found: fall back to the markers' own stamps
    for k in ('correction', 'list'):
        m = st.get(k)
        if m and m.get('ts'):
            ts.append(float(m['ts']))
    for r in st.get('repeat') or []:
        if r.get('asked_ts'):
            ts.append(float(r['asked_ts']))
    return max(ts) if ts else None


def _answer_gate(conversation, st: Dict[str, Any], newest) -> Optional[str]:
    """Why a yes/no must NOT be applied now — the reason, or None when it may.

    Money moves on the customer's word, never on the model's: there must be an inbound message
    newer than our question, and it must not itself be a new number/amount (that is a request,
    not an answer). 2026-09-17 23:48 (chat 13f58d64): the model answered its own repeat question
    with «yes» six seconds after asking; the next unrelated message then released 26,900."""
    if not st:
        return None
    if newest is None:
        return 'no customer message to read as an answer'
    asked = _question_time(conversation, st)
    if asked is not None and newest.created_at.timestamp() <= asked:
        return 'the customer has not answered yet — nothing newer than the question; wait for their reply'
    from qurtoba.tools.planning import _classify_message
    txt = (newest.content or {}).get('text', '') if isinstance(newest.content, dict) else ''
    cls = _classify_message(txt)
    if cls['phones'] or cls['amounts']:
        return 'the newest message is a new number/amount, not an answer — handle it as a request; the hold stays'
    return None


def describe(conversation) -> List[str]:
    """Lines for the model's <money_path> block."""
    st = pending_state(conversation)
    lines: List[str] = []
    c = st.get('correction')
    if c:
        lines.append(f"  - waiting for yes/no: create {R._fmt(c.get('value'))} → {c.get('account_number')} "
                     f"({'a corrected number, «حول» is the yes' if c.get('correction_of') else 'the amount the system read beside the number'})")
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
    """Apply a yes/no to whatever is held (priority: correction, list, high value, repeat).

    ``answer_message_id`` defaults to the customer's newest message — the one they
    just cancelled or confirmed with. The model calls this tool without an id, and a
    «no» on a held repeat used to drop the transfer in SILENCE because the decline line
    was guarded by that id (2026-09-08: «الغاء» twice, then «لغيت ؟», answered by
    nothing at all). Whatever we drop, the customer is told.
    """
    from qurtoba.tools.transactions import (_clear_repeat_pending, qurtoba_confirm_pending_repeats,
                                             qurtoba_create_new_transactions_bulk)
    from qurtoba.tools.planning import _classify_message
    key = _conv_key(conversation)
    st = pending_state(conversation)
    yes = decision == 'yes'
    if not answer_message_id:
        _newest = _newest_inbound(conversation)
        answer_message_id = str(_newest.id) if _newest is not None else None
    result: Dict[str, Any] = {'success': True, 'handled': False, 'kind': 'none', 'created': [], 'note': ''}

    # The customer's newest message quoting one of THEIR OWN other messages is about that
    # message, not about what we are holding («تأكيد» quoted on a different transfer).
    held_src = (st.get('correction') or {}).get('source_message_id') or (st.get('high_value') or {}).get('source_message_id')
    newest = _newest_inbound(conversation)
    # Only a YES moves money, so only a yes needs the customer's own newer answer. A no drops what we hold
    # and is always confirmed to the customer (2026-09-08: a cancel must never be silent).
    why_not = _answer_gate(conversation, st, newest) if yes else None
    if why_not:
        result.update(success=False, error_type='no_customer_answer', note=why_not,
                      kind=next((k for k in ('correction', 'list', 'high_value', 'repeat') if st.get(k)), 'none'))
        log('pending_answer', conversation, kind='no_customer_answer', yes=yes, why=why_not[:80])
        return result
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
            from .transfers import _created_since
            if _created_since(partner, c['account_number'], c['value'], c.get('ts')):
                result.update(handled=True, note='that transfer was already created after the question; nothing to do')
            else:
                res = call_tool(conversation, partner, qurtoba_create_new_transactions_bulk, transactions=[{
                    'type': c.get('type') or 'كاش', 'value': float(c['value']), 'account_number': c['account_number'],
                    'source_message_id': c.get('source_message_id')}])
                result.update(handled=True, created=_created(res), note='executed the held transfer')
            consume(conversation, [x for x in (c.get('correction_of'), c.get('source_message_id')) if x])
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
            # the model read the customer's words as a yes; the tool still refuses when the newest
            # message is a request or older than the question (see _repeat_confirmation_verdict)
            from qurtoba.tools.transactions import _REPEAT_MEANING_VERIFIED
            _tok = _REPEAT_MEANING_VERIFIED.set(True)
            try:
                res = call_tool(conversation, partner, qurtoba_confirm_pending_repeats)
            finally:
                _REPEAT_MEANING_VERIFIED.reset(_tok)
            made = [{'account_number': c.get('account_number'), 'value': c.get('value')}
                    for c in (res.get('created') or []) if isinstance(c, dict)]
            skipped = [s.get('reason') for s in (res.get('skipped') or []) if isinstance(s, dict)]
            result.update(handled=bool(made), created=made,
                          note='repeated the held transfer(s)' if made
                          else f"nothing repeated ({', '.join(skipped) or 'no_pending'}); the hold stays — answer the customer's actual message")
        else:
            _clear_repeat_pending(conversation)
            # ALWAYS confirm a cancel: the customer's own message, else the number
            # message the question was quoted on. Never drop a transfer silently.
            target = answer_message_id or next(
                (r.get('source_message_id') for r in st['repeat'] if isinstance(r, dict) and r.get('source_message_id')),
                None)
            told = send_quoted(conversation, target, R.REPEAT_DECLINED) if target else False
            result.update(handled=True,
                          note='the repeat was dropped; the customer was told' if told
                               else 'the repeat was dropped (the confirmation line could not be sent — say it yourself)')
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
    """The customer's newest line — in a group never a staff member's: a staff «تمام» answers nothing."""
    try:
        from qurtoba.groups import customer_inbound
        return (customer_inbound(conversation)
                .select_related('reply_to').order_by('-created_at').first())
    except Exception:
        return None


def _created(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{'account_number': r.get('account_number'), 'value': r.get('value'), 'status': r.get('status')}
            for r in (res.get('results') or []) if isinstance(r, dict)]
