"""The money path, without a model.

``decide`` is pure: planner output + a few facts about the conversation → a list of
actions (create these items, reply this line on that message, confirm the held
repeats, watermark these rows). ``run`` gathers the facts, calls the planner and
the create tool through the same @tool functions the agent used, and applies the
decisions. Every rule below is the cash / fawry prompt rule it replaces, with the
prompt line quoted in the comment.

Division of labour (owner decision 2026-09-06): Python CREATES — every clean pair is
created the moment it arrives, no model in the path. Python does NOT interpret the
customer: whatever is left after the creates (a number without an amount, an
unreadable amount, a held high value, a question, a greeting) is handed to the AI as
``leftovers`` with a suggested line, and the AI decides what to say. The fixed lines
are sent by Python only when ``QURTOBA_AUTOMATION_REPLIES`` is True.
"""
import time
from typing import Any, Dict, List, Optional

from qurtoba.tools.planning import _classify_message, _looks_like_spelled_amount
from qurtoba.tools.transactions import _normalize_phone
from . import lexicon as L
from . import replies as R
from .arabic_numbers import parse_arabic_amount
from .context import (alert_human, asked_recently, cache_delete, cache_get, cache_set, call_tool,
                      consume, log, said_recently, send_quoted)

REROUTE_KEY = 'qurtoba:reroute_owed:{conv}'      # set by tasks._send_reroute_ask / _send_cancel_notice
REROUTE_TTL = 24 * 3600
LIST_KEY = 'qurtoba:list_confirm:{conv}'         # «تأكيد المطابقة» for a positional list, waiting for أيوة/لأ
CORRECTION_KEY = 'qurtoba:correction_pending:{conv}'   # «تقصد تحويل X على الرقم ده؟ ابعت حول» waiting for حول
PENDING_TTL = 3600


# ── pure decision table ──────────────────────────────────────────────────────

def decide(plan: Dict[str, Any], *, hv_threshold: float, repeat_pending,
           reroute: Optional[Dict[str, Any]], texts: Dict[str, str],
           accounts: Optional[List[tuple]] = None, list_pending: Optional[Dict[str, Any]] = None,
           hv_pending: Optional[str] = None) -> Dict[str, Any]:
    """Planner output → actions. No I/O.

    Returns {'items': [...create items...], 'replies': [(message_id, text)],
             'confirm_repeats': bool, 'clear_repeats': bool, 'consume': [ids],
             'reroute_used': bool, 'pending': {...} | None}
    """
    items: List[Dict[str, Any]] = []
    replies: List[tuple] = []
    consumed: List[str] = []
    out = {'items': items, 'replies': replies, 'confirm_repeats': False, 'clear_repeats': False,
           'consume': consumed, 'reroute_used': False, 'pending': None, 'list_confirm': None, 'to_model': []}
    if not plan or not plan.get('success'):
        return out
    accounts = accounts or []
    # «Amount only, no phone (محتاج 500) → the registered-accounts view: exactly one → use it;
    # more than one → أي حساب؟; none → ask for the number.» A BROKEN phone next to the amount
    # («0106001000 ⏎ 590», 10 digits) is a cash attempt, never an amount for a registered account:
    # the customer gets «ابعت رقم صحيح 11 رقم» on that message and nothing is created.
    broken_phone_mids = {i.get('message_id') for i in plan.get('ignored') or [] if i.get('reason') == 'broken_phone'}
    no_phone_anywhere = not (plan.get('pairs') or []) and not any(
        o.get('kind') == 'phone' for o in plan.get('orphans') or []) and not broken_phone_mids

    pairs = [dict(p) for p in plan.get('pairs') or []]
    # The planner keeps a pair's reason in `ambiguous` (separator_ambiguous / list_pairing),
    # not on the pair itself.
    reasons = {a.get('source_message_id'): a.get('reason') for a in plan.get('ambiguous') or [] if a.get('reason')}
    for p in pairs:
        p.setdefault('reason', reasons.get(p.get('source_message_id')))
    yes_phones, no_phones = set(), set()
    # Pairs the create tool is HOLDING as a same-day repeat («تحب أكررها؟» already asked):
    # never re-submit them — the tool would ask again on every turn. `repeat_pending` may be the
    # dict of pending signatures ({'كاش|01…|10100.00': {...}}) or a bool.
    held = {}
    if isinstance(repeat_pending, dict):
        held = {(v.get('account_number'), float(v.get('value') or 0)): k for k, v in repeat_pending.items() if isinstance(v, dict)}
    held_phones = {acc for acc, _v in held}
    # «list_pattern=true → the numbers and amounts arrived as two separate lists, paired by
    # position → CONFIRM the matching» — the POSITIONAL pairs (confidence low), as ONE question.
    # Self-contained pairs in the same burst are clean and are created at once (2026-09-06 13:12:
    # one guess held 25 clean transfers).
    list_confirm = False

    # «Answers are not requests» — an inbound quoting our question, or a bare yes/no/amount
    # right after it, is the ANSWER (planner `answers`). Apply it, never re-ask.
    unclear_answers = set()
    for a in plan.get('answers') or []:
        kind, text, phone = a.get('kind'), a.get('text') or '', a.get('about_phone')
        if kind == 'amount_reply' and 'مبلغ كبير' in (a.get('question_text') or ''):
            # «100 ج» to «مبلغ كبير — محتاج تأكيد» → «قصدك نأكد الـ100,000 ولا المبلغ 100 بس؟»
            src_txt = texts.get(a.get('about_message_id') or '', '')
            orig = (_classify_message(src_txt).get('amounts') or [None])[0]
            replies.append((a['message_id'], R.UNCLEAR_HV_ANSWER.format(text=text[:20], amount=R._fmt(orig or '?'))))
            unclear_answers.add(a['message_id'])
            continue
        if kind == 'amount_reply' and a.get('applied_to'):
            continue                                   # already folded into its pair
        if L.is_bare_yes(text):
            if repeat_pending:
                out['confirm_repeats'] = True          # «تحب أكررها؟» → أيوة (the tool creates it)
                no_phones.update(held_phones)          # …so this turn must not create it again
            elif list_pending and list_pending.get('phones'):
                yes_phones.update(list_pending['phones'])   # «تأكيد المطابقة» → the whole list
                list_confirm = False
            elif phone:
                yes_phones.add(phone)                  # «تأكيد» on a held high value / a list pairing
            consumed.append(a['message_id'])
        elif L.is_bare_no(text):
            if repeat_pending:
                out['clear_repeats'] = True
                no_phones.update(held_phones)          # dropped: never re-submit the held pair
                replies.append((a['message_id'], R.REPEAT_DECLINED))
            elif list_pending and list_pending.get('phones'):
                no_phones.update(list_pending['phones'])
                list_confirm = False
                replies.append((a['message_id'], R.DECLINED))
            elif phone:
                no_phones.add(phone)
                replies.append((a['message_id'], R.DECLINED))
            consumed.append(a['message_id'])
        elif kind == 'amount_reply' and a.get('value') is not None and not a.get('applied_to'):
            continue                                   # the planner will have re-paired it next turn
        else:
            # a reply in the customer's own words → meaning → the model decides (answer_pending)
            out['to_model'].append({'message_id': a['message_id'], 'text': text, 'question': (a.get('question_text') or '')[:60]})

    # Reroute answer: «the partner's next BARE phone number is the answer → create the owed
    # amount + the new number as a brand-new transaction». A phone WITH an amount is never it.
    reroute_amount = float(reroute['amount']) if reroute and reroute.get('amount') else None

    to_confirm: List[Dict[str, Any]] = []
    for p in pairs:
        src, phone, value = p.get('source_message_id'), p.get('account_number'), p.get('value')
        reason, conf = p.get('reason'), p.get('confidence')
        if phone in no_phones:
            consumed.append(src)
            continue
        if (phone, float(value or 0)) in held:
            continue                                   # waiting for أيوة/لأ — the tool already asked
        if p.get('answer_message_id') in unclear_answers:
            continue                                   # re-valued by an unclear reply — asked instead
        if hv_pending and phone == hv_pending and phone not in yes_phones and value is not None \
                and float(value) >= hv_threshold:
            continue                                   # held by the high-value question — waiting for «تأكيد»
        if reason == 'separator_ambiguous':
            raw = _last_line(texts.get(src, ''))
            replies.append((src, R.UNREADABLE_AMOUNT.format(raw=raw or value)))
            continue
        if reason == 'answer_matches_neither_option' and phone not in yes_phones:
            replies.append((p.get('answer_message_id') or src, R.NEITHER_OPTION.format(amount=R._fmt(value), phone=phone)))
            continue
        if (conf == 'low' or list_confirm) and phone not in yes_phones and reason != 'answer_to_question':
            # «list_pattern=true OR any low pair → positional guess → CONFIRM the matching»
            to_confirm.append(p)
            continue
        item = {'type': 'كاش', 'value': value, 'account_number': phone, 'source_message_id': src}
        if value is not None and float(value) >= hv_threshold and phone in yes_phones:
            item['confirm_high_value'] = True
        items.append(item)

    if to_confirm:
        # ONE question for the whole positional list, quoted on its first number message.
        lines = [R.LIST_CONFIRM_HEADER] + [f"{p['account_number']} ← {R._fmt(p['value'])}" for p in to_confirm] + [R.LIST_CONFIRM_TAIL]
        replies.append((to_confirm[0].get('source_message_id'), '\n'.join(lines)))
        out['list_confirm'] = {'phones': [p['account_number'] for p in to_confirm],
                               'pairs': [{'account_number': p['account_number'], 'value': p['value'],
                                          'source_message_id': p.get('source_message_id')} for p in to_confirm]}

    answered_mids = {a.get('message_id') for a in plan.get('answers') or []}
    for o in plan.get('orphans') or []:
        mid, kind, val = o.get('message_id'), o.get('kind'), o.get('value')
        if mid in answered_mids:
            continue                                   # it was an answer to our question, not a new orphan
        if kind == 'phone':
            if reroute_amount and not out['reroute_used']:
                items.append({'type': 'كاش', 'value': reroute_amount, 'account_number': val,
                              'source_message_id': mid, 'reroute': True})
                out['reroute_used'] = True
                continue
            hint = next((i for i in plan.get('ignored') or [] if i.get('message_id') == mid), None)
            amt = _hint_amount(hint.get('text') if hint else None)
            if amt:
                replies.append((mid, R.ORPHAN_PHONE_HINT.format(phone=val, amount=R._fmt(amt))))
                continue
            replies.append((mid, R.ORPHAN_PHONE.format(phone=val)))
        else:
            if mid in broken_phone_mids:
                replies.append((mid, R.BAD_NUMBER))
                continue
            replies.append((mid, R.ORPHAN_AMOUNT.format(amount=R._fmt(val))))

    for mid in broken_phone_mids:
        if not any(r[0] == mid for r in replies):
            replies.append((mid, R.BAD_NUMBER))

    if reroute_amount and not out['reroute_used']:
        # «A number that arrives WITH an amount is NOT the reroute answer … create exactly what the
        # message says, then ask ONE quoted question about the still-owed reroute amount.»
        first = next((i for i in items if i.get('type') == 'كاش' and not i.get('reroute')), None)
        if first is not None:
            replies.append((first.get('source_message_id'), R.REROUTE_OWED_QUESTION.format(amount=R._fmt(reroute_amount))))

    if plan.get('needs_resend'):
        target = max((m for m in texts), default=None)
        replies.append((target, R.RESEND_FLOOD))
    return out


def _broken_number_amount(text: str) -> Optional[float]:
    """The amount written in a message whose number is BROKEN («0106013464 ⏎ الفين جنيه» → 2000):
    digits, or Arabic words. None when the message carries no amount or a valid number."""
    cls = _classify_message(text)
    if cls['phones'] or not any(i.get('reason') == 'broken_phone' for i in cls.get('ignored') or []):
        return None
    if cls['amounts']:
        return float(cls['amounts'][0])
    if _looks_like_spelled_amount(text):
        import re
        val = parse_arabic_amount(re.sub(r'\d[\d\s.,]*', ' ', text))
        return float(val) if val else None
    return None


def match_corrections(orphan_phones: List[Dict[str, Any]], rejected: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pure. A bare number sent AFTER we asked for a correct number IS the correction: it takes
    the amount of the rejected message. Only when the match is unambiguous (exactly one rejected
    message with an amount before it, already told «ابعت رقم صحيح»).

    orphan_phones: [{message_id, value(phone), at}], rejected: [{message_id, amount, at, asked}]
    → [{type, value, account_number, source_message_id, correction_of}]"""
    items: List[Dict[str, Any]] = []
    used = set()
    for o in sorted(orphan_phones, key=lambda x: x['at']):
        before = [r for r in rejected if r['message_id'] not in used and r.get('amount') and r['at'] < o['at']]
        if len(before) != 1 or not before[0].get('asked'):
            continue                       # two rejected messages → ambiguous; not yet told → not a correction
        r = before[0]
        used.add(r['message_id'])
        items.append({'type': 'كاش', 'value': float(r['amount']), 'account_number': o['value'],
                      'source_message_id': o['message_id'], 'correction_of': r['message_id']})
    return items


def _hint_amount(text: Optional[str]) -> Optional[float]:
    """The whole amount hidden in an `ignored` piece («عبدالله15100» → 15100), if any."""
    import re
    from qurtoba.tools._amounts import normalize_amount
    if not text:
        return None
    for run in re.findall(r'\d[\d.,]*', str(text)):
        r = normalize_amount(run)
        if r.get('ok') and float(r['value']).is_integer() and r['value'] >= 1:
            return float(r['value'])
    return None


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in str(text or '').splitlines() if ln.strip()]
    return lines[-1][:20] if lines else ''


# ── pre-pass: voice, instapay, non-cash, multi-number, spelled amounts ────────

def _text_of(m) -> str:
    c = m.content if isinstance(m.content, dict) else {}
    return str(c.get('text') or c.get('transcription') or '')


# ── the run ──────────────────────────────────────────────────────────────────

_RUN_LOCK_TTL = 90
_RUN_LOCK_WAIT = 45


def _acquire_run_lock(conv_key: str) -> bool:
    """WhatsApp can split one burst into two batches seconds apart; the second run must wait for
    the first to finish creating (both would otherwise plan the same rows and wake the model twice
    — 2026-09-06 13:01). Returns True when the lock is held (or could not be checked)."""
    import time
    try:
        from django.core.cache import cache
    except Exception:
        return True
    key = f'qurtoba:money_path_lock:{conv_key}'
    deadline = time.time() + _RUN_LOCK_WAIT
    while True:
        try:
            if cache.add(key, time.time(), _RUN_LOCK_TTL):
                return True
        except Exception:
            return True
        if time.time() >= deadline:
            return True      # never block a turn forever; the create tool is idempotent anyway
        time.sleep(1.0)


def _release_run_lock(conv_key: str) -> None:
    try:
        from django.core.cache import cache
        cache.delete(f'qurtoba:money_path_lock:{conv_key}')
    except Exception:
        pass


def run(conversation, partner, route: Dict[str, Any]) -> Dict[str, Any]:
    conv_key = str(getattr(conversation, 'id', ''))
    _acquire_run_lock(conv_key)
    try:
        return _run(conversation, partner, route)
    finally:
        _release_run_lock(conv_key)


def _run(conversation, partner, route: Dict[str, Any]) -> Dict[str, Any]:
    from qurtoba.tools.planning import qurtoba_plan_transactions
    from qurtoba.tools.transactions import (_high_value_threshold, _list_repeat_pending,
                                             _clear_repeat_pending, qurtoba_confirm_pending_repeats,
                                             qurtoba_create_new_transactions_bulk)
    from .router import load_batch_rows, unprocessed_text_rows

    summary: Dict[str, Any] = {'items': 0, 'replies': 0}
    conv_key = str(getattr(conversation, 'id', ''))
    batch = load_batch_rows(conversation, {'message': '', 'content': []}) if not route.get('batch_ids') else None
    from modules.chat.models import Message
    batch_rows = list(Message.objects_all.filter(conversation=conversation, id__in=route.get('batch_ids') or [])
                      .select_related('reply_to')) if route.get('batch_ids') else (batch or [])
    older = unprocessed_text_rows(conversation)
    rows = {str(m.id): m for m in older}
    for m in batch_rows:
        rows.setdefault(str(m.id), m)

    pre_items: List[Dict[str, Any]] = []
    pre_replies: List[tuple] = []
    pre_consume: List[str] = []
    fallback_amounts: Dict[str, float] = {}
    accounts: List[tuple] = []

    # a pending «ابعت حول» question: a BARE «حول»/«أيوة»/«لا» is applied here; anything longer is
    # meaning and reaches the model (it calls qurtoba_answer_pending).
    correction_pending = cache_get(CORRECTION_KEY.format(conv=conv_key))
    to_model: List[Dict[str, Any]] = []

    for mid, m in list(rows.items()):
        text = _text_of(m)
        t = L.norm(text)
        cls = _classify_message(text)
        # VOICE — «كاش via voice → NEVER ACT. Ask written, quoted on the voice message.»
        if m.type in ('audio', 'voice'):
            if cls['phones'] or cls['amounts']:
                pre_replies.append((mid, R.VOICE_CASH))
            pre_consume.append(mid)
            continue
        if m.type != 'text':
            continue
        if L.INSTAPAY.search(t):                       # a product name we do not serve — never cash
            pre_replies.append((mid, R.INSTAPAY))
            pre_consume.append(mid)
            continue
        if correction_pending and mid in (route.get('batch_ids') or []) and not cls['phones'] and not cls['amounts']:
            if L.is_bare_yes(text):
                pre_items.append({'type': correction_pending.get('type') or 'كاش', 'value': float(correction_pending['value']),
                                  'account_number': correction_pending['account_number'],
                                  'source_message_id': correction_pending['source_message_id']})
                pre_consume += [x for x in (mid, correction_pending.get('correction_of')) if x]
                cache_delete(CORRECTION_KEY.format(conv=conv_key)); correction_pending = None
                continue
            if L.is_bare_no(text):
                pre_replies.append((mid, R.CORRECTION_DECLINED))
                pre_consume += [x for x in (mid, correction_pending.get('correction_of'), correction_pending.get('source_message_id')) if x]
                cache_delete(CORRECTION_KEY.format(conv=conv_key)); correction_pending = None
                continue
        if len(cls['phones']) >= 2 and len(cls['amounts']) == 1:
            # several numbers with ONE amount: «لكل رقم» / «قسم» / a mistake — meaning → the model
            to_model.append({'message_id': mid, 'kind': 'multi_number', 'text': text[:200]})
            pre_consume.append(mid)
            continue
        if cls['phones'] and cls['amounts'] and _number_inside_prose(text, cls):
            # the number sits INSIDE a sentence («انا بعت لـ 01… امبارح 500 وصلت») — a layout the
            # office never uses for an order; what the sentence means is the model's call
            to_model.append({'message_id': mid, 'kind': 'sentence', 'text': text[:200]})
            pre_consume.append(mid)
            continue
        if not cls['amounts'] and len(cls['phones']) == 1 and _looks_like_spelled_amount(text):
            # arithmetic on a closed vocabulary («خمسين الف»); anything the parser does not know stays
            # for the model (it reads «الفين لكل رقم» and creates)
            words = text.replace(cls['phones'][0], ' ')
            val = parse_arabic_amount(words)
            if val:
                fallback_amounts[mid] = float(val)

    replies_enabled = _python_replies_enabled()
    leftovers: List[Dict[str, Any]] = []

    def _say(mid, text, kind):
        if replies_enabled:
            if send_quoted(conversation, mid, text):
                summary['replies'] += 1
        elif mid and (asked_recently(conversation, mid, minutes=15) or said_recently(conversation, mid, text, minutes=360)):
            log('leftover_already_asked', conversation, mid=str(mid)[:8])   # never ask twice
            # a reported bad number stays unconsumed: the customer's next bare number corrects it
        else:
            leftovers.append({'message_id': mid, 'kind': kind, 'text': (_text_of(rows[mid]) if mid in rows else '')[:80],
                              'suggested_reply': text})

    for mid, text in pre_replies:
        _say(mid, text, 'pre')
    if pre_consume and replies_enabled:
        consume(conversation, pre_consume)          # handled here → the planner must not see them
    elif pre_consume:
        # not answered by Python: keep the rows visible to the AI, but out of the planner
        consume(conversation, [m for m in pre_consume if m not in {l['message_id'] for l in leftovers}])

    # The planner (authoritative DB fetch; `amount` fallbacks by message id survive it).
    planner_input = [{'message_id': mid, 'text': _text_of(m), **({'amount': fallback_amounts[mid]} if mid in fallback_amounts else {})}
                     for mid, m in rows.items() if mid not in pre_consume and m.type == 'text']
    plan = {'success': False}
    if planner_input:
        plan = call_tool(conversation, partner, qurtoba_plan_transactions, messages=planner_input)

    # «ابعت رقم صحيح» → the customer's next bare number is the CORRECTION of that message
    # and takes its amount (2026-09-06: «0106013464 ⏎ الفين جنيه» → «01060134646» was asked
    # «المبلغ؟» again — a person would never ask).
    corrections: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []          # bad-number messages still in the window
    for mid, m in rows.items():
        if m.type != 'text':
            continue
        amt = _broken_number_amount(_text_of(m))
        if amt:
            rejected.append({'message_id': mid, 'amount': amt, 'at': m.created_at,
                             'asked': _replied_on(conversation, mid, minutes=360)})
    if plan.get('success') and rejected and any(o.get('kind') == 'phone' for o in plan.get('orphans') or []):
        orphan_phones = [{'message_id': o['message_id'], 'value': o['value'], 'at': rows[o['message_id']].created_at}
                         for o in plan['orphans'] if o.get('kind') == 'phone' and o.get('message_id') in rows]
        # a correction is a number sent ON ITS OWN after the bad-number line — never a bare
        # number inside a burst whose amount may simply be in the next batch (13:12: 01275362968)
        if plan.get('pairs') or len(orphan_phones) > 1:
            orphan_phones = []
        for c in match_corrections(orphan_phones, rejected):
            # Owner decision 2026-09-06: confirm first — «تقصد تحويل X على الرقم ده؟ ابعت «حول»» —
            # then «حول» creates it instantly (handled in the pre-pass above, no model).
            plan['orphans'] = [o for o in plan['orphans'] if o.get('message_id') != c['source_message_id']]
            cache_set(CORRECTION_KEY.format(conv=conv_key), {**c, 'ts': time.time()}, PENDING_TTL)
            if send_quoted(conversation, c['source_message_id'],
                           R.CORRECTION_CONFIRM.format(amount=R._fmt(c['value']), phone=c['account_number'])):
                summary['replies'] += 1
            log('correction_asked', conversation, phone=c['account_number'], value=c['value'])

    reroute = cache_get(REROUTE_KEY.format(conv=conv_key))
    if reroute and not _reroute_still_valid(conversation, partner, reroute):
        cache_delete(REROUTE_KEY.format(conv=conv_key)); reroute = None
    if reroute is None:
        reroute = _reroute_from_chat(conversation, partner)

    # A bare «أيوة» / «لا» / «تأكيد» that quotes nothing is not in the planner's `answers`
    # (it only extracts quoted replies and bare amounts) — attach it to whatever we are waiting
    # for: a held repeat, a list confirmation, or a high-value hold.
    repeat_pending = _list_repeat_pending(conversation) or {}
    list_pending = cache_get(LIST_KEY.format(conv=conv_key))
    hv_phone, hv_src = _hv_question_pending(conversation)
    answered_ids = {a.get('message_id') for a in plan.get('answers') or []}
    for mid in route.get('batch_ids') or []:
        m = rows.get(mid)
        if m is None or m.type != 'text' or mid in answered_ids:
            continue
        text = _text_of(m)
        cls = _classify_message(text)
        waiting = bool(repeat_pending or list_pending or hv_phone)
        if hv_phone and not cls['phones'] and len(cls['amounts']) == 1 and not (L.is_bare_yes(text) or L.is_bare_no(text)):
            # «100 ج» to the high-value hold (its line carries no «؟», so the planner does not see an answer)
            plan.setdefault('answers', []).append({'message_id': mid, 'text': text, 'kind': 'amount_reply',
                                                   'value': float(cls['amounts'][0]), 'about_phone': hv_phone,
                                                   'about_message_id': hv_src, 'question_text': R.HIGH_VALUE})
            continue
        if not (L.is_bare_yes(text) or L.is_bare_no(text)):
            if waiting and not cls['phones'] and not cls['amounts']:
                to_model.append({'message_id': mid, 'kind': 'pending_answer', 'text': text[:200]})
            continue
        # a yes/no quoted on ANOTHER customer message is about that message, not our question
        q = getattr(m, 'reply_to', None)
        if q is not None and getattr(q, 'direction', None) == 'inbound' and str(q.id) != (hv_src or ''):
            continue
        about = None
        if repeat_pending:
            held = [v.get('account_number') for v in repeat_pending.values() if isinstance(v, dict)]
            about = held[0] if len(held) == 1 else None
        elif list_pending:
            about = None
        elif hv_phone:
            about = hv_phone
        else:
            continue
        plan.setdefault('answers', []).append({'message_id': mid, 'text': text, 'kind': 'reply',
                                               'about_phone': about, 'question_text': ''})

    decision = decide(plan, hv_threshold=_high_value_threshold(), repeat_pending=repeat_pending,
                      reroute=reroute, texts={mid: _text_of(m) for mid, m in rows.items()}, accounts=accounts,
                      list_pending=list_pending, hv_pending=hv_phone)
    if decision.get('list_confirm'):
        cache_set(LIST_KEY.format(conv=conv_key), {**decision['list_confirm'], 'ts': time.time()}, PENDING_TTL)
    elif list_pending:
        cache_delete(LIST_KEY.format(conv=conv_key))
    for tm in decision.get('to_model') or []:
        to_model.append({'message_id': tm['message_id'], 'kind': 'pending_answer', 'text': tm['text'][:200]})

    if decision['confirm_repeats']:
        call_tool(conversation, partner, qurtoba_confirm_pending_repeats)
    if decision['clear_repeats']:
        _clear_repeat_pending(conversation)

    items = pre_items + [i for i in decision['items']]
    created_result = None
    held_items: List[Dict[str, Any]] = []
    created_items: List[Dict[str, Any]] = []
    if items:
        clean = [{k: v for k, v in i.items() if k not in ('reroute', 'correction_of')} for i in items]
        created_result = call_tool(conversation, partner, qurtoba_create_new_transactions_bulk, transactions=clean)
        summary['items'] = len(clean)
        held_items, created_items = _handle_create_result(conversation, partner, created_result, clean, summary,
                                                          replies_enabled=replies_enabled)
        if decision['reroute_used'] and created_result.get('success'):
            cache_delete(REROUTE_KEY.format(conv=conv_key))
        if created_items:
            from .pending import clear_pending
            clear_pending(conversation)          # a created transfer ends any «حول»/list question
            # «The expectation expires: any other transaction since → a bare number is a normal op.»
            # The customer moved on — a rejected message older than what was just created is retired,
            # so a later bare number can never pick up its amount by mistake.
            # Only a rejection the customer was ALREADY told about (an earlier turn) expires: the
            # other transfers of the same burst are not «moving on» (2026-09-06 10:37: the ten
            # creates of the burst retired its own rejected message before it could be corrected).
            newest_created = max((rows[i['source_message_id']].created_at for i in created_items
                                  if i.get('source_message_id') in rows), default=None)
            stale = [r['message_id'] for r in rejected
                     if r['asked'] and newest_created is not None and r['at'] < newest_created]
            if stale:
                consume(conversation, stale)
                cache_delete(CORRECTION_KEY.format(conv=conv_key))
                log('correction_expired', conversation, mids=[x[:8] for x in stale])

    for mid, text in decision['replies']:
        _say(mid, text, 'planner')
    if decision['consume']:
        consume(conversation, decision['consume'])
    for h in held_items:
        leftovers.append({'message_id': h.get('source_message_id'), 'kind': 'high_value_held',
                          'text': f"{h.get('account_number')} ← {R._fmt(h.get('value'))}",
                          'suggested_reply': R.HIGH_VALUE})
    for rj in summary.pop('_rejected', []):
        leftovers.append({**rj, 'text': (_text_of(rows[rj['message_id']]) if rj.get('message_id') in rows else '')[:80]})
    for tm in to_model:
        if not any(l.get('message_id') == tm['message_id'] for l in leftovers):
            leftovers.append({'message_id': tm['message_id'], 'kind': tm['kind'], 'text': tm['text'],
                              'suggested_reply': ''})

    # Everything the customer wrote that the money path did not settle goes to the AI.
    # Python judges nothing here: only a bare single word / punctuation / emoji is dropped.
    others = []
    answered = {a.get('message_id') for a in plan.get('answers') or []}
    for mid, m in rows.items():
        if mid in (decision.get('consume') or []) or mid in {i.get('source_message_id') for i in created_items}:
            continue
        if mid in answered or mid in fallback_amounts or mid in pre_consume:
            continue
        if any(l.get('message_id') == mid for l in leftovers):
            continue
        txt = _text_of(m)
        cls = _classify_message(txt) if m.type == 'text' else {'phones': [], 'amounts': []}
        if cls['phones'] or cls['amounts']:
            continue                     # still part of the money path (an orphan / a held pair)
        if m.type == 'text' and _is_noise_line(txt):
            consume(conversation, [mid])
            continue
        others.append({'message_id': mid, 'type': m.type, 'text': txt[:200]})
        consume(conversation, [mid])     # the model answers it this turn; never re-read next turn

    summary.update({
        'created': [{'account_number': i.get('account_number'), 'value': i.get('value'), 'type': i.get('type')} for i in created_items],
        'leftovers': leftovers,
        'others': others,
        'needs_ai': bool(leftovers or others),
    })
    try:
        from .pending import describe
        summary['pending'] = describe(conversation)
    except Exception:
        summary['pending'] = []
    summary['summary'] = render_ai_summary(summary)
    log('transfers', conversation, items=summary['items'], replies=summary['replies'], needs_ai=summary['needs_ai'],
        leftovers=[l['kind'] for l in leftovers] or None, others=len(others) or None,
        pairs=len(plan.get('pairs') or []), orphans=len(plan.get('orphans') or []))
    return summary


_LAYOUT_OK_WORDS = {'كاش', 'فودافون', 'فدفون', 'اتصالات', 'اورانج', 'وي', 'محفظه', 'المحفظه', 'جنيه', 'جنيها', 'ج', 'م', 'مصري',
                    'الف', 'الاف', 'مبلغ', 'المبلغ', 'رقم', 'الرقم', 'القيمه', 'قيمه', 'القيمة', 'حواله', 'تحويل', 'فوري', 'امان', 'طاير',
                    'المستلم', 'المرسل', 'مستلم', 'حساب', 'الحساب', 'تليفون', 'موبايل', 'نمره', 'النمره', 'النوع', 'صافي'}


def _number_inside_prose(text: str, cls: Dict[str, Any]) -> bool:
    """Layout, not meaning: on the line that holds the phone, words BEFORE the number
    («انا بعت لـ 01…») or four or more words after it («… ده اتحول ولا لسه») make it a
    sentence. A number first, then the amount and a short name («01… المبلغ 20 ألف اسامه البنا»)
    is the office's order format."""
    import re
    for raw in str(text or '').splitlines():
        line = L.norm(raw)
        digits_line = re.sub(r'\D', '', line)
        ph = next((p for p in cls['phones'] if p[-9:] in digits_line), None)
        if ph is None:
            continue
        pos = line.find(ph[-9:])
        before = [w for w in re.findall(r'[a-z\u0600-\u06ff]+', line[:max(pos, 0)]) if w not in _LAYOUT_OK_WORDS and len(w) > 1]
        after = [w for w in re.findall(r'[a-z\u0600-\u06ff]+', line[pos:]) if w not in _LAYOUT_OK_WORDS and len(w) > 1]
        if len(before) >= 1 or len(after) >= 4:
            return True
    return False


def _python_replies_enabled() -> bool:
    try:
        from django.conf import settings as dj
        return bool(getattr(dj, 'QURTOBA_AUTOMATION_REPLIES', False))
    except Exception:
        return False


def _is_noise_line(text: str) -> bool:
    """Only what can carry no meaning at all: empty, punctuation, an emoji. Every word —
    even a single one («طارق» may be a name, «الغاء» is an order) — goes to the AI; Python
    does not decide what a message means."""
    t = ' '.join(str(text or '').split())
    if not t:
        return True
    return L.is_only_emoji(t) or all(ch in '.,،!…-_' for ch in t)


def render_ai_summary(summary: Dict[str, Any]) -> str:
    """The block the AI reads: what the system already did, what is still open."""
    lines = []
    created = summary.get('created') or []
    if created:
        lines.append('CREATED by the system this turn (👍 already sent — say NOTHING about them):')
        lines += [f"  - {c.get('type')} {R._fmt(c.get('value'))} → {c.get('account_number')}" for c in created]
    pend = summary.get('pending') or []
    if pend:
        lines.append('PENDING — the system is HOLDING a transfer behind a yes/no it asked (settle it with qurtoba_answer_pending, never the create tool):')
        lines += pend
    lo = summary.get('leftovers') or []
    if lo:
        lines.append('OPEN ITEMS — each needs YOUR decision on its message_id:')
        for l in lo:
            k = l.get('kind')
            if k == 'multi_number':
                hint = 'several numbers with ONE amount — read it: the same amount to each (create one item per number), a split (alert a human), or unclear (ask)'
            elif k == 'sentence':
                hint = 'a number and an amount INSIDE a sentence — read it: a status question (check_transaction_status), an order (create it), or unclear (ask)'
            elif k == 'pending_answer':
                hint = "the customer's reply to the PENDING question above — decide yes or no and call qurtoba_answer_pending"
            else:
                hint = f"suggested: «{l.get('suggested_reply')}»"
            lines.append(f"  - [message_id: {l.get('message_id')}] kind={k} «{l.get('text')}» → {hint}")
    ot = summary.get('others') or []
    if ot:
        lines.append('OTHER MESSAGES from the customer this turn (not money — understand and answer them):')
        lines += [f"  - [message_id: {o.get('message_id')}] ({o.get('type')}) {o.get('text')}" for o in ot]
    if not lines:
        lines.append('Nothing open: every message was a clean transfer and is created.')
    return '\n'.join(lines)


def _replied_on(conversation, message_id, *, minutes: int = 360) -> bool:
    """True if any outbound text quotes `message_id` within `minutes` (whatever its wording)."""
    try:
        from datetime import timedelta
        from django.utils import timezone
        from modules.chat.models import Message
        return Message.objects_all.filter(conversation=conversation, direction='outbound', type='text',
                                          reply_to_id=str(message_id),
                                          created_at__gte=timezone.now() - timedelta(minutes=minutes)).exists()
    except Exception:
        return False


def _hv_question_pending(conversation):
    """(phone, source message id) of the transfer our «مبلغ كبير — محتاج تأكيد» line (≤ 6 h, among the
    last 12 outbound lines) is holding — as long as that transfer's message is still unconsumed —
    else (None, None)."""
    try:
        from datetime import timedelta
        from django.utils import timezone
        from modules.chat.models import Message
        for m in (Message.objects_all.filter(conversation=conversation, direction='outbound', type='text', active=True,
                                             created_at__gte=timezone.now() - timedelta(hours=6))
                  .select_related('reply_to').order_by('-created_at')[:12]):
            txt = (m.content or {}).get('text') if isinstance(m.content, dict) else ''
            if not txt or not str(txt).startswith('مبلغ كبير'):
                continue
            q = m.reply_to
            if q is None or getattr(q, 'direction', None) != 'inbound' or getattr(q, 'ai_consumed_at', None):
                return None, None
            phones = _classify_message(_text_of(q)).get('phones') or []
            return (phones[0], str(q.id)) if phones else (None, None)
        return None, None
    except Exception:
        return None, None


_REROUTE_LINE = 'محتاجين رقم تانى'
_REMAINDER_RE = None


def _reroute_from_chat(conversation, partner) -> Optional[Dict[str, Any]]:
    """Derive the owed reroute amount from the LAST outbound notice when no cache marker exists
    (worker restart, notice sent before v2, sandbox): «… الباقى ( X ) …» → X; the no-wallet
    notice → the full amount of the transfer it quotes."""
    global _REMAINDER_RE
    try:
        import re
        from django.utils import timezone
        from modules.chat.models import Message
        if _REMAINDER_RE is None:
            _REMAINDER_RE = re.compile(r'الباقى\s*\(\s*([\d,\.]+)\s*\)')
        day_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        last = (Message.objects_all.filter(conversation=conversation, direction='outbound', type='text', active=True,
                                           created_at__gte=day_start)
                .select_related('reply_to').order_by('-created_at').first())
        if last is None:
            return None
        txt = str((last.content or {}).get('text') or '') if isinstance(last.content, dict) else ''
        if _REROUTE_LINE not in txt:
            return None
        amount = None
        m = _REMAINDER_RE.search(txt)
        if m:
            amount = float(m.group(1).replace(',', ''))
        else:
            q = last.reply_to
            rec = getattr(q, 'qurtoba_record', None) if q is not None else None
            if rec is not None and getattr(rec, 'value', None):
                amount = float(rec.value)
            elif q is not None:
                amts = _classify_message(_text_of(q)).get('amounts') or []
                amount = float(amts[0]) if amts else None
            if amount is None and q is not None:
                from qurtoba.models import QurtobaRecord
                customer = getattr(partner, 'qurtoba_customer', None)
                phones = _classify_message(_text_of(q)).get('phones') or []
                if customer is not None and phones:
                    r = QurtobaRecord.objects.filter(customer=customer, account_number=phones[0]).order_by('-id').first()
                    if r is not None and getattr(r, 'original_value', None) or getattr(r, 'value', None):
                        amount = float(getattr(r, 'original_value', None) or r.value)
        if not amount or amount <= 0:
            return None
        marker = {'amount': amount, 'ts': last.created_at.timestamp(), 'kind': 'chat', 'record_id': None}
        return marker if _reroute_still_valid(conversation, partner, marker) else None
    except Exception:
        return None


def _reroute_still_valid(conversation, partner, marker: Dict[str, Any]) -> bool:
    """«The reroute expectation expires: a new day, any other transaction, or any other reply
    since → a bare number is a normal incomplete op.»"""
    try:
        ts = float(marker.get('ts') or 0)
        if time.time() - ts > REROUTE_TTL:
            return False
        from datetime import datetime, timezone as _tz
        since = datetime.fromtimestamp(ts, tz=_tz.utc)
        from django.utils import timezone
        if timezone.localtime(since).date() != timezone.localdate():
            return False
        from qurtoba.models import QurtobaRecord
        customer = getattr(partner, 'qurtoba_customer', None)
        if customer is not None and QurtobaRecord.objects.filter(customer=customer, created_at__gt=since).exists():
            return False
        return True
    except Exception:
        return False


def _handle_create_result(conversation, partner, result: Dict[str, Any], items: List[Dict[str, Any]],
                          summary: Dict[str, Any], *, replies_enabled: bool = False):
    """REPLY PROTOCOL: a clean item gets nothing (the tool spoke); a faulty item gets ONE quoted line —
    sent here when Python replies are on, otherwise handed to the AI as a leftover.
    Returns (held_items, created_items)."""
    held: List[Dict[str, Any]] = []
    created: List[Dict[str, Any]] = []
    if not result.get('success'):
        alert_human(conversation, partner, f'فشل إنشاء التحويلات تلقائياً: {result.get("error")}')
        return held, created
    for r in result.get('results') or []:
        idx = r.get('index')
        item = items[idx] if isinstance(idx, int) and idx < len(items) else {}
        src = r.get('source_message_id') or item.get('source_message_id')
        status = r.get('status')
        if status == 'needs_confirmation' and r.get('confirm_kind', 'high_value') == 'high_value':
            # «Held → on the transfer message: مبلغ كبير — محتاج منك كلمة «تأكيد» …» — once.
            if replies_enabled:
                if not r.get('already_asked') and not asked_recently(conversation, src, minutes=360):
                    if send_quoted(conversation, src, R.HIGH_VALUE):
                        summary['replies'] += 1
            elif not asked_recently(conversation, src, minutes=360):
                held.append({**item, 'source_message_id': src})
        elif status == 'rejected':
            et = r.get('error_type')
            if et == 'invalid_account_number':
                text = R.BAD_NUMBER
            elif et == 'source_mismatch':
                alert_human(conversation, partner, f'source_mismatch أثناء الإنشاء التلقائي: {r}')
                continue
            else:
                text = r.get('error') or R.NOT_UNDERSTOOD
            if replies_enabled:
                if send_quoted(conversation, src, text):
                    summary['replies'] += 1
                consume(conversation, [src])
            else:
                summary.setdefault('_rejected', []).append({'message_id': src, 'kind': 'rejected', 'text': '', 'suggested_reply': text})
        elif status in ('created', 'pending_review'):
            created.append({**item, 'source_message_id': src, 'record_id': r.get('record_id')})
        # duplicate / repeat_asked / account_corrected → the tool already spoke
    return held, created
