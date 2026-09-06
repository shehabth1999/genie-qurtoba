"""The money path, without a model.

``decide`` is pure: planner output + a few facts about the conversation → a list of
actions (create these items, reply this line on that message, confirm the held
repeats, watermark these rows). ``run`` gathers the facts, calls the planner and
the create tool through the same @tool functions the agent used, and applies the
decisions. Every rule below is the cash / fawry prompt rule it replaces, with the
prompt line quoted in the comment.
"""
import time
from typing import Any, Dict, List, Optional

from qurtoba.tools.planning import _classify_message, _looks_like_spelled_amount
from qurtoba.tools.transactions import _normalize_phone
from . import lexicon as L
from . import replies as R
from .arabic_numbers import parse_arabic_amount
from .context import (alert_human, asked_recently, cache_delete, cache_get, cache_set, call_tool,
                      consume, log, send_quoted)

REROUTE_KEY = 'qurtoba:reroute_owed:{conv}'      # set by tasks._send_reroute_ask / _send_cancel_notice
REROUTE_TTL = 24 * 3600
MULTI_KEY = 'qurtoba:multi_pending:{conv}'       # «تقصد X لكل رقم ولا تقسيمه؟» waiting for its answer
NONCASH_KEY = 'qurtoba:noncash_pending:{conv}'   # «أي حساب فورى؟ 1) … 2) …» waiting for its answer
PENDING_TTL = 3600


# ── pure decision table ──────────────────────────────────────────────────────

def decide(plan: Dict[str, Any], *, hv_threshold: float, repeat_pending: bool,
           reroute: Optional[Dict[str, Any]], texts: Dict[str, str],
           accounts: Optional[List[tuple]] = None) -> Dict[str, Any]:
    """Planner output → actions. No I/O.

    Returns {'items': [...create items...], 'replies': [(message_id, text)],
             'confirm_repeats': bool, 'clear_repeats': bool, 'consume': [ids],
             'reroute_used': bool, 'pending': {...} | None}
    """
    items: List[Dict[str, Any]] = []
    replies: List[tuple] = []
    consumed: List[str] = []
    out = {'items': items, 'replies': replies, 'confirm_repeats': False, 'clear_repeats': False,
           'consume': consumed, 'reroute_used': False, 'pending': None}
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
    yes_phones, no_phones = set(), set()

    # «Answers are not requests» — an inbound quoting our question, or a bare yes/no/amount
    # right after it, is the ANSWER (planner `answers`). Apply it, never re-ask.
    for a in plan.get('answers') or []:
        kind, text, phone = a.get('kind'), a.get('text') or '', a.get('about_phone')
        if kind == 'amount_reply' and a.get('applied_to'):
            continue                                   # already folded into its pair
        if L.is_yes(text):
            if repeat_pending:
                out['confirm_repeats'] = True          # «تحب أكررها؟» → أيوة
            elif phone:
                yes_phones.add(phone)                  # «تأكيد» on a held high value / a list pairing
            consumed.append(a['message_id'])
        elif L.is_no(text):
            if repeat_pending:
                out['clear_repeats'] = True
                replies.append((a['message_id'], R.REPEAT_DECLINED))
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

    for p in pairs:
        src, phone, value = p.get('source_message_id'), p.get('account_number'), p.get('value')
        reason, conf = p.get('reason'), p.get('confidence')
        if phone in no_phones:
            consumed.append(src)
            continue
        if reason == 'separator_ambiguous':
            raw = _last_line(texts.get(src, ''))
            replies.append((src, R.UNREADABLE_AMOUNT.format(raw=raw or value)))
            continue
        if reason == 'answer_matches_neither_option' and phone not in yes_phones:
            replies.append((p.get('answer_message_id') or src, R.NEITHER_OPTION.format(amount=R._fmt(value), phone=phone)))
            continue
        if conf == 'low' and phone not in yes_phones:
            # «list_pattern=true OR any low pair → positional guess → CONFIRM the matching»
            replies.append((src, R.LIST_CONFIRM.format(phone=phone, amount=R._fmt(value))))
            continue
        item = {'type': 'كاش', 'value': value, 'account_number': phone, 'source_message_id': src}
        if value is not None and float(value) >= hv_threshold and phone in yes_phones:
            item['confirm_high_value'] = True
        items.append(item)

    for o in plan.get('orphans') or []:
        mid, kind, val = o.get('message_id'), o.get('kind'), o.get('value')
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

    if plan.get('needs_resend'):
        target = max((m for m in texts), default=None)
        replies.append((target, R.RESEND_FLOOD))
    return out


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

    # answers to a pending «أي حساب؟» / «المبلغ لـ فورى …؟» / «لكل رقم ولا تقسيم؟»
    noncash_pending = cache_get(NONCASH_KEY.format(conv=conv_key))
    multi_pending = cache_get(MULTI_KEY.format(conv=conv_key))

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
        if not cls['amounts'] and _looks_like_spelled_amount(text):
            words = text
            for ph in cls['phones']:
                words = words.replace(ph, ' ')
            val = parse_arabic_amount(words)
            if val:
                fallback_amounts[mid] = float(val)

    for mid, text in pre_replies:
        if send_quoted(conversation, mid, text):
            summary['replies'] += 1
    if pre_consume:
        consume(conversation, pre_consume)          # handled here → the planner must not see them

    # The planner (authoritative DB fetch; `amount` fallbacks by message id survive it).
    planner_input = [{'message_id': mid, 'text': _text_of(m), **({'amount': fallback_amounts[mid]} if mid in fallback_amounts else {})}
                     for mid, m in rows.items() if mid not in pre_consume and m.type == 'text']
    plan = {'success': False}
    if planner_input:
        plan = call_tool(conversation, partner, qurtoba_plan_transactions, messages=planner_input)

    reroute = cache_get(REROUTE_KEY.format(conv=conv_key))
    if reroute and not _reroute_still_valid(conversation, partner, reroute):
        cache_delete(REROUTE_KEY.format(conv=conv_key)); reroute = None

    decision = decide(plan, hv_threshold=_high_value_threshold(), repeat_pending=bool(_list_repeat_pending(conversation)),
                      reroute=reroute, texts={mid: _text_of(m) for mid, m in rows.items()}, accounts=accounts)
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
    if items:
        clean = [{k: v for k, v in i.items() if k != 'reroute'} for i in items]
        created_result = call_tool(conversation, partner, qurtoba_create_new_transactions_bulk, transactions=clean)
        summary['items'] = len(clean)
        _handle_create_result(conversation, partner, created_result, clean, summary)
        if decision['reroute_used'] and created_result.get('success'):
            cache_delete(REROUTE_KEY.format(conv=conv_key))

    for mid, text in decision['replies']:
        if send_quoted(conversation, mid, text):
            summary['replies'] += 1
    if decision['consume']:
        consume(conversation, decision['consume'])

    # Rows that carried nothing for the money path (a name line, a greeting next to the
    # numbers) are finished with — never let them linger into the next burst.
    noise = [mid for mid, m in rows.items() if m.type == 'text' and mid not in pre_consume
             and not _classify_message(_text_of(m))['phones'] and not _classify_message(_text_of(m))['amounts']
             and mid not in fallback_amounts and not any(a.get('message_id') == mid for a in plan.get('answers') or [])]
    if noise:
        consume(conversation, noise)

    from .intents import run_secondary
    run_secondary(conversation, partner, route)
    log('transfers', conversation, **{k: v for k, v in summary.items()},
        pairs=len(plan.get('pairs') or []), orphans=len(plan.get('orphans') or []))
    return summary


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


def _handle_create_result(conversation, partner, result: Dict[str, Any], items: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    """REPLY PROTOCOL: a clean item gets nothing (the tool spoke); a faulty item gets ONE quoted line."""
    if not result.get('success'):
        alert_human(conversation, partner, f'فشل إنشاء التحويلات تلقائياً: {result.get("error")}')
        return
    for r in result.get('results') or []:
        idx = r.get('index')
        src = r.get('source_message_id') or (items[idx].get('source_message_id') if isinstance(idx, int) and idx < len(items) else None)
        status = r.get('status')
        if status == 'needs_confirmation' and (r.get('high_value') or r.get('needs_confirmation')):
            if not r.get('already_asked') and not asked_recently(conversation, src):
                if send_quoted(conversation, src, R.HIGH_VALUE):
                    summary['replies'] += 1
        elif status == 'rejected':
            et = r.get('error_type')
            if et == 'invalid_account_number':
                text = R.BAD_NUMBER
            elif et == 'source_mismatch':
                alert_human(conversation, partner, f'source_mismatch أثناء الإنشاء التلقائي: {r}')
                continue
            else:
                text = r.get('error') or R.NOT_UNDERSTOOD
            if send_quoted(conversation, src, text):
                summary['replies'] += 1
            consume(conversation, [src])
        # created / pending_review / duplicate / repeat_asked / account_corrected → the tool already spoke
