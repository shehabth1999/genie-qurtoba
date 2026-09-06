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
MULTI_KEY = 'qurtoba:multi_pending:{conv}'       # «تقصد X لكل رقم ولا تقسيمه؟» waiting for its answer
NONCASH_KEY = 'qurtoba:noncash_pending:{conv}'   # «أي حساب فورى؟ 1) … 2) …» waiting for its answer
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
           'consume': consumed, 'reroute_used': False, 'pending': None, 'list_confirm': None}
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
    # position → CONFIRM the matching before executing» — every positional pair, as ONE question.
    list_confirm = bool(plan.get('list_pattern')) and not (list_pending or {}).get('confirmed')

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
        if L.is_yes(text):
            if repeat_pending:
                out['confirm_repeats'] = True          # «تحب أكررها؟» → أيوة (the tool creates it)
                no_phones.update(held_phones)          # …so this turn must not create it again
            elif list_pending and list_pending.get('phones'):
                yes_phones.update(list_pending['phones'])   # «تأكيد المطابقة» → the whole list
                list_confirm = False
            elif phone:
                yes_phones.add(phone)                  # «تأكيد» on a held high value / a list pairing
            consumed.append(a['message_id'])
        elif L.is_no(text):
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
            # «رديت بـ«100 ج» على تأكيد الـ100,000 — قصدك …؟» — ask ONCE, on the reply itself.
            q = (a.get('question_text') or '')[:40]
            replies.append((a['message_id'], R.UNCLEAR_ANSWER.format(text=text[:30], question=q)))

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
        out['list_confirm'] = {'phones': [p['account_number'] for p in to_confirm]}

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
            if no_phone_anywhere and len(accounts) == 1:
                ty, acc = accounts[0]
                items.append({'type': ty, 'value': float(val), 'account_number': acc, 'source_message_id': mid})
                continue
            if no_phone_anywhere and len(accounts) > 1 and out['pending'] is None:
                options = ' '.join(f'{i}) {ty} {n}' for i, (ty, n) in enumerate(accounts, 1))
                replies.append((mid, R.WHICH_ACCOUNT_ANY.format(options=options)))
                out['pending'] = {'amount': float(val), 'options': [n for _t, n in accounts],
                                  'types': {n: ty for ty, n in accounts}, 'message_id': mid}
                consumed.append(mid)
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
        cands = [r for r in rejected if r['message_id'] not in used and r.get('asked') and r.get('amount')
                 and r['at'] < o['at']]
        if len(cands) != 1:
            continue
        r = cands[0]
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


def _registered_accounts(partner) -> List[tuple]:
    """[(type, number)] registered for the customer (فورى/أمان/طاير)."""
    customer = getattr(partner, 'qurtoba_customer', None)
    if customer is None:
        return []
    try:
        rows = list(customer.account_entries.all().order_by('type', 'account_number'))
        if rows:
            return [(r.type, str(r.account_number)) for r in rows]
        from qurtoba.models import _parse_accounts
        return [(t, str(n)) for t, n in _parse_accounts(customer.accounts or '')]
    except Exception:
        return []


def resolve_noncash(text: str, type_name: str, accounts: List[tuple], pending: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pure. A فورى/أمان/طاير request → {'item': {...}} or {'reply': text} (+ 'pending' to store).

    Rules (fawry prompt ACCOUNT GUARD / MISSING ACCOUNT): the account must match a
    registered one EXACTLY under the SAME type; type given + account missing → the
    only registered account of that type, or ask which; a registered account under
    another type → the wrong-type line.
    """
    t = L.norm(text)
    digits = [d for d in _digit_runs(t)]
    of_type = [(ty, n) for ty, n in accounts if ty == type_name]
    numbers = {n: ty for ty, n in accounts}
    account = next((d for d in digits if d in numbers), None)
    # Two numbers and one of them is a ≥5-digit run that matches no registered account →
    # the customer typed an account we do not know («فوري 5555555 500»).
    unknown_account = None
    if account is None and len(digits) >= 2:
        unknown_account = next((d for d in digits if len(d) >= 5 and not (len(d) == 11 and d.startswith('01'))), None)
    amounts = [d for d in digits if d != account and d != unknown_account and not (len(d) == 11 and d.startswith('01'))]
    # a spelled amount («الفين فورى»)
    amount = None
    if amounts:
        from qurtoba.tools._amounts import normalize_amount
        r = normalize_amount(amounts[-1])
        amount = r['value'] if r.get('ok') and float(r['value']).is_integer() else None
    if amount is None and _looks_like_spelled_amount(t):
        amount = parse_arabic_amount(_strip_type_words(t))

    if account is not None and numbers[account] != type_name:
        return {'reply': R.WRONG_TYPE.format(account=account, registered_type=numbers[account], requested_type=type_name)}
    if account is None:
        if unknown_account is not None and of_type:
            reg = '، '.join(f'{ty} {n}' for ty, n in of_type)
            return {'reply': R.NOT_REGISTERED.format(account=unknown_account, registered=reg)}
        if not of_type:
            return {'reply': R.NO_ACCOUNT_OF_TYPE.format(type=type_name)}
        if len(of_type) == 1:
            account = of_type[0][1]
        else:
            options = ' '.join(f'{i}) {n}' for i, (_t, n) in enumerate(of_type, 1))
            return {'reply': R.WHICH_ACCOUNT.format(type=type_name, options=options),
                    'pending': {'type': type_name, 'amount': amount, 'options': [n for _t, n in of_type]}}
    if amount is None:
        return {'reply': R.NONCASH_AMOUNT_QUESTION.format(type=type_name, account=account),
                'pending': {'type': type_name, 'account': account, 'amount': None}}
    return {'item': {'type': type_name, 'value': float(amount), 'account_number': account}}


def _digit_runs(t: str) -> List[str]:
    import re
    return [d.replace(',', '').replace('.', '') for d in re.findall(r'\d[\d.,]*', t)]


def _strip_type_words(t: str) -> str:
    import re
    return re.sub(r'فوري|فورى|امان|طاير|fawry|aman', ' ', t)


def _multi_number(text: str) -> Optional[Dict[str, Any]]:
    """≥2 phones and ONE amount in one message → {phones, amount, mode} (mode: each / split / ask)."""
    cls = _classify_message(text)
    if len(cls['phones']) < 2 or len(cls['amounts']) != 1:
        return None
    t = L.norm(text)
    mode = 'each' if L.PER_NUMBER.search(t) else 'split' if L.SPLIT.search(t) else 'ask'
    return {'phones': cls['phones'], 'amount': cls['amounts'][0], 'mode': mode}


# ── the run ──────────────────────────────────────────────────────────────────

def run(conversation, partner, route: Dict[str, Any]) -> Dict[str, Any]:
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
    accounts = _registered_accounts(partner)

    # answers to a pending «أي حساب؟» / «المبلغ لـ فورى …؟» / «لكل رقم ولا تقسيم؟» / «ابعت حول»
    noncash_pending = cache_get(NONCASH_KEY.format(conv=conv_key))
    multi_pending = cache_get(MULTI_KEY.format(conv=conv_key))
    correction_pending = cache_get(CORRECTION_KEY.format(conv=conv_key))

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
        if L.INSTAPAY.search(t):
            pre_replies.append((mid, R.INSTAPAY))
            pre_consume.append(mid)
            continue
        if correction_pending and mid in (route.get('batch_ids') or []) and not cls['phones'] and not cls['amounts']:
            if L.is_yes(text):
                pre_items.append({'type': 'كاش', 'value': float(correction_pending['value']),
                                  'account_number': correction_pending['account_number'],
                                  'source_message_id': correction_pending['source_message_id']})
                pre_consume += [mid, correction_pending.get('correction_of')]
                cache_delete(CORRECTION_KEY.format(conv=conv_key)); correction_pending = None
                continue
            if L.is_no(text):
                pre_replies.append((mid, R.CORRECTION_DECLINED))
                pre_consume += [mid, correction_pending.get('correction_of'), correction_pending.get('source_message_id')]
                cache_delete(CORRECTION_KEY.format(conv=conv_key)); correction_pending = None
                continue
        if multi_pending and mid in (route.get('batch_ids') or []) and not cls['phones']:
            if L.PER_NUMBER.search(t) or L.is_yes(text):
                for ph in multi_pending['phones']:
                    pre_items.append({'type': 'كاش', 'value': multi_pending['amount'], 'account_number': ph,
                                      'source_message_id': multi_pending['message_id']})
                cache_delete(MULTI_KEY.format(conv=conv_key)); multi_pending = None
                pre_consume.append(mid)
                continue
            if L.SPLIT.search(t):
                alert_human(conversation, partner, f'العميل يطلب تقسيم مبلغ {multi_pending["amount"]} على عدة أرقام {multi_pending["phones"]}')
                pre_replies.append((mid, R.SPLIT_INFO))
                cache_delete(MULTI_KEY.format(conv=conv_key)); multi_pending = None
                pre_consume.append(mid)
                continue
        if noncash_pending and mid in (route.get('batch_ids') or []) and not cls['phones']:
            chosen = _noncash_answer(text, noncash_pending)
            if chosen is not None:
                pre_items.append({**chosen, 'source_message_id': noncash_pending['message_id']})
                cache_delete(NONCASH_KEY.format(conv=conv_key)); noncash_pending = None
                pre_consume.append(mid)
                continue
        noncash = L.noncash_type(t)
        if noncash:
            res = resolve_noncash(text, noncash, accounts)
            if res.get('item'):
                pre_items.append({**res['item'], 'source_message_id': mid})
            else:
                pre_replies.append((mid, res['reply']))
                if res.get('pending'):
                    cache_set(NONCASH_KEY.format(conv=conv_key), {**res['pending'], 'message_id': mid}, PENDING_TTL)
            pre_consume.append(mid)
            continue
        multi = _multi_number(text)
        if multi:
            if multi['mode'] == 'each':
                for ph in multi['phones']:
                    pre_items.append({'type': 'كاش', 'value': multi['amount'], 'account_number': ph, 'source_message_id': mid})
            elif multi['mode'] == 'split':
                alert_human(conversation, partner, f'العميل يطلب تقسيم مبلغ {multi["amount"]} على عدة أرقام {multi["phones"]} [message_id: {mid}]')
                pre_replies.append((mid, R.SPLIT_INFO))
            else:
                pre_replies.append((mid, R.PER_NUMBER_QUESTION.format(amount=R._fmt(multi['amount']))))
                cache_set(MULTI_KEY.format(conv=conv_key), {'phones': multi['phones'], 'amount': multi['amount'], 'message_id': mid}, PENDING_TTL)
            pre_consume.append(mid)
            continue
        if any(i.get('reason') == 'broken_phone' for i in cls.get('ignored') or []) and not cls['phones']:
            continue                                   # a bad number keeps its (spelled) amount to itself
        if not cls['amounts'] and _looks_like_spelled_amount(text):
            words = text
            for ph in cls['phones']:
                words = words.replace(ph, ' ')
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
                             'asked': said_recently(conversation, mid, R.BAD_NUMBER, minutes=360)})
    if plan.get('success') and rejected and any(o.get('kind') == 'phone' for o in plan.get('orphans') or []):
        orphan_phones = [{'message_id': o['message_id'], 'value': o['value'], 'at': rows[o['message_id']].created_at}
                         for o in plan['orphans'] if o.get('kind') == 'phone' and o.get('message_id') in rows]
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
        if hv_phone and not cls['phones'] and len(cls['amounts']) == 1 and not (L.is_yes(text) or L.is_no(text)):
            # «100 ج» to the high-value hold (its line carries no «؟», so the planner does not see an answer)
            plan.setdefault('answers', []).append({'message_id': mid, 'text': text, 'kind': 'amount_reply',
                                                   'value': float(cls['amounts'][0]), 'about_phone': hv_phone,
                                                   'about_message_id': hv_src, 'question_text': R.HIGH_VALUE})
            continue
        if not (L.is_yes(text) or L.is_no(text)):
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
    if decision.get('pending'):
        pend = decision['pending']
        cache_set(NONCASH_KEY.format(conv=conv_key),
                  {'type': None, 'amount': pend['amount'], 'options': pend['options'], 'types': pend['types'],
                   'message_id': pend['message_id']}, PENDING_TTL)

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
    summary['summary'] = render_ai_summary(summary)
    log('transfers', conversation, items=summary['items'], replies=summary['replies'], needs_ai=summary['needs_ai'],
        leftovers=[l['kind'] for l in leftovers] or None, others=len(others) or None,
        pairs=len(plan.get('pairs') or []), orphans=len(plan.get('orphans') or []))
    return summary


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
    lo = summary.get('leftovers') or []
    if lo:
        lines.append('OPEN ITEMS — each needs ONE quoted reply on its message_id (suggested wording given; keep it or adapt it, never invent an amount):')
        for l in lo:
            lines.append(f"  - [message_id: {l.get('message_id')}] kind={l.get('kind')} «{l.get('text')}» → suggested: «{l.get('suggested_reply')}»")
    ot = summary.get('others') or []
    if ot:
        lines.append('OTHER MESSAGES from the customer this turn (not money — understand and answer them):')
        lines += [f"  - [message_id: {o.get('message_id')}] ({o.get('type')}) {o.get('text')}" for o in ot]
    if not lines:
        lines.append('Nothing open: every message was a clean transfer and is created.')
    return '\n'.join(lines)


def _noncash_answer(text: str, pending: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The customer's reply to «أي حساب …؟ 1) … 2) …» or «المبلغ لـ فورى …؟»."""
    t = L.norm(text)
    digits = _digit_runs(t)
    if pending.get('options'):
        chosen = None
        for d in digits:
            if d in pending['options']:
                chosen = d
            elif d.isdigit() and 1 <= int(d) <= len(pending['options']) and len(d) == 1:
                chosen = pending['options'][int(d) - 1]
        if chosen is None:
            return None
        amount = pending.get('amount')
        if amount is None:
            others = [d for d in digits if d != chosen and not (len(d) == 1)]
            if others:
                from qurtoba.tools._amounts import normalize_amount
                r = normalize_amount(others[-1]); amount = r['value'] if r.get('ok') else None
        if amount is None:
            return None
        ty = pending.get('type') or (pending.get('types') or {}).get(chosen)
        if not ty:
            return None
        return {'type': ty, 'value': float(amount), 'account_number': chosen}
    if pending.get('account') and pending.get('amount') is None:
        from qurtoba.tools._amounts import normalize_amount
        val = None
        if digits:
            r = normalize_amount(digits[-1]); val = r['value'] if r.get('ok') and float(r['value']).is_integer() else None
        if val is None and _looks_like_spelled_amount(t):
            val = parse_arabic_amount(t)
        if val is None:
            return None
        return {'type': pending['type'], 'value': float(val), 'account_number': pending['account']}
    return None


def _hv_question_pending(conversation):
    """(phone, source message id) of the transfer our last «مبلغ كبير — محتاج تأكيد» line (≤ 6 h)
    is holding, else (None, None)."""
    try:
        from datetime import timedelta
        from django.utils import timezone
        from modules.chat.models import Message
        m = (Message.objects_all.filter(conversation=conversation, direction='outbound', type='text', active=True,
                                        created_at__gte=timezone.now() - timedelta(hours=6))
             .select_related('reply_to').order_by('-created_at').first())
        if m is None:
            return None, None
        txt = (m.content or {}).get('text') if isinstance(m.content, dict) else ''
        if not txt or not str(txt).startswith('مبلغ كبير'):
            return None, None
        q = m.reply_to
        if q is None or getattr(q, 'direction', None) != 'inbound':
            return None, None
        phones = _classify_message(_text_of(q)).get('phones') or []
        return (phones[0], str(q.id)) if phones else (None, None)
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
