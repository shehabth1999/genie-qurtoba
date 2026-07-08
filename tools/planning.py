"""Deterministic transaction-planning aid for the Qurtoba agent.

`qurtoba_plan_transactions` takes a burst of customer messages and returns the
correct phone↔amount pairing, computed in Python with the documented rules —
critically, **name/other tokens are skipped and never occupy the pending slot**,
which is the fix for the cascade where a stray name (e.g. "حمدي") before a bare
amount mis-aligned the whole burst.

This tool is READ-ONLY: it proposes; it never creates anything. The create path
(`qurtoba_create_new_transactions_bulk`) independently re-validates every pair,
so a wrong plan can never commit wrong money.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from modules.aistudio.tools import tool
from qurtoba.tools._amounts import normalize_amount, _ar_to_ascii
from qurtoba.tools.transactions import _normalize_phone

logger = logging.getLogger(__name__)

def _is_phone(s: str) -> Optional[str]:
    """Return the normalized 01XXXXXXXXX form if `s` is an Egyptian mobile, else None.

    Requires ≥9 digits so a short amount (e.g. "25500") is never taken for a phone.
    """
    if sum(c.isdigit() for c in s) < 9:
        return None
    norm = _normalize_phone(s)
    if norm and norm.startswith('01') and len(norm) == 11:
        return norm
    return None


def _is_multiplier_only(line: str) -> bool:
    """True if the line is ONLY a multiplier word (الف/الفين/مليون…) with NO digits.

    A digit-less line normalises to a value ONLY when it is a standalone multiplier
    word (normalize_amount: "الف"→1000), so this also rejects names like "شاكر".
    """
    s = _ar_to_ascii(line or '').strip()
    if not s or any(ch.isdigit() for ch in s):
        return False
    return bool(normalize_amount(s).get('ok'))


def _ends_with_bare_number(line: str) -> bool:
    """True if the line's last token is a bare number (not a phone) — the count a
    trailing multiplier word should attach to (the "60" in "60\\nالف")."""
    s = _ar_to_ascii(line or '').strip()
    if not s:
        return False
    last = s.split()[-1]
    if not re.fullmatch(r'\d[\d.,]*', last):
        return False
    return _is_phone(last) is None   # a phone is never a multiplier's count


def _merge_multiplier_lines(text: str) -> str:
    """Join a bare multiplier word on its OWN line back onto the preceding number.

    Customers split an amount across lines ("60\\nالف" = 60 thousand). The line-based
    classifier would otherwise read that as two amounts (60 and 1000); merging it to
    "60 الف" lets normalize_amount return the single correct value (60000).
    """
    out: List[str] = []
    for ln in (text or '').splitlines():
        stripped = ln.strip()
        if (out and stripped and _is_multiplier_only(stripped)
                and _ends_with_bare_number(out[-1])):
            out[-1] = out[-1].rstrip() + ' ' + stripped
        else:
            out.append(ln)
    return '\n'.join(out)


def _classify_message(text: str) -> Dict[str, Any]:
    """Split one message into its phones, whole-number amounts, and name/other.

    Line-based so a phone and an amount sharing a line ("01... 25500") split
    correctly while a spaced phone ("+20 100 280 4814") stays one number. A
    fractional value (13.75, 6.08) is a commission tally, NOT a transfer amount —
    it is dropped here, never read as an amount.
    """
    text = _merge_multiplier_lines(text or '')
    text = _ar_to_ascii(text)
    phones: List[str] = []
    amounts: List[float] = []
    ambiguous: List[float] = []
    has_name = False

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        tokens = line.split()
        # 1) any single token that is itself a complete phone
        line_phones, rest = [], []
        for tok in tokens:
            norm = _is_phone(tok)
            (line_phones if norm else rest).append(norm or tok)
        # 2) else the whole line may be one spaced/formatted phone (+20 1.. ..)
        if not line_phones:
            joined = _is_phone(line)
            if joined:
                line_phones, rest = [joined], []
        phones.extend(line_phones)

        rest_text = ' '.join(rest).strip()
        if not rest_text:
            continue
        r = normalize_amount(rest_text)
        if r['ok'] and float(r['value']).is_integer():
            amounts.append(r['value'])
            if r.get('ambiguous'):
                ambiguous.append(r['value'])
        elif r.get('reason') == 'multiple_numbers':
            for tok in re.findall(r'\d[\d.,]*', rest_text):
                rr = normalize_amount(tok)
                if rr['ok'] and float(rr['value']).is_integer():
                    amounts.append(rr['value'])
        elif re.sub(r'[\d\s.,+\-]', '', rest_text):
            has_name = True

    return {
        'phones': phones,
        'amounts': amounts,
        'ambiguous': ambiguous,
        'has_name': has_name,
    }


def _same_time_split_mids(events, msg_sent, window_s):
    """Message ids of SPLIT (phone-only / amount-only) messages whose arrival order
    WhatsApp can't be trusted to preserve, so positional pairing of them is a guess.

    Group the split events into ≤``window_s``-second clusters (anchored at each
    cluster's first send-time). A cluster holding ≥2 phones AND ≥2 amounts is the
    genuinely-reorderable set → all its ids are returned. A lone split (1 phone +
    1 amount) has only one possible pairing, so it is never flagged. Messages with
    no known send-time are skipped (treated as non-ambiguous).
    """
    split_items = []  # (kind, mid, sent)
    for ev in events:
        if ev[0] in ('phone', 'amount'):
            sent = msg_sent.get(ev[2])
            if sent is not None:
                split_items.append((ev[0], ev[2], sent))
    split_items.sort(key=lambda s: s[2])
    flagged = set()
    i = 0
    while i < len(split_items):
        start = split_items[i][2]
        cluster = []
        while i < len(split_items) and (split_items[i][2] - start).total_seconds() <= window_s:
            cluster.append(split_items[i])
            i += 1
        n_ph = sum(1 for c in cluster if c[0] == 'phone')
        n_am = sum(1 for c in cluster if c[0] == 'amount')
        if n_ph >= 2 and n_am >= 2:
            flagged.update(c[1] for c in cluster)
    return flagged


def _same_time_overflow_mids(events, msg_sent, window_s, max_tx):
    """Message ids of same-second SPLIT clusters that carry MORE THAN ``max_tx``
    reorderable transactions — the hard cap.

    Same shape as ``_same_time_split_mids`` (group the split phone-only / amount-only
    messages into ≤``window_s``-second clusters), but instead of "is this pairing a
    guess?" it answers "is this burst too big to pair safely AT ALL?". A cluster's
    transaction count is the number of positional pairings it forces, i.e.
    ``min(#phones, #amounts)``. When that exceeds ``max_tx`` every id in the cluster
    is returned so the caller can withhold the WHOLE burst (process none) and ask the
    customer to resend cleanly.

    Self-contained pairs (number+amount in ONE message) are ``('pair', …)`` events,
    never enter ``split_items`` here, and so are never counted or blocked — a customer
    may send any number of complete pairs in the same second and they all process.
    Noise messages carry no phone/amount, emit no split event, and never count.
    """
    split_items = []  # (kind, mid, sent)
    for ev in events:
        if ev[0] in ('phone', 'amount'):
            sent = msg_sent.get(ev[2])
            if sent is not None:
                split_items.append((ev[0], ev[2], sent))
    split_items.sort(key=lambda s: s[2])
    overflow = set()
    i = 0
    while i < len(split_items):
        start = split_items[i][2]
        cluster = []
        while i < len(split_items) and (split_items[i][2] - start).total_seconds() <= window_s:
            cluster.append(split_items[i])
            i += 1
        n_ph = sum(1 for c in cluster if c[0] == 'phone')
        n_am = sum(1 for c in cluster if c[0] == 'amount')
        if min(n_ph, n_am) > max_tx:
            overflow.update(c[1] for c in cluster)
    return overflow


def _build_events(messages, msg_text):
    """Ordered event stream from a message list. Each event is one of:
    ('pair', phone, amount, mid, amb) | ('phone', phone, mid) |
    ('amount', value, mid) | ('name', mid). Shared by the planner tool and
    consumed_ids_by_source so both classify/order identically."""
    events = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        mid = str(item.get('message_id') or '').strip() or None
        text = (msg_text or {}).get(mid) or item.get('text') or ''
        cls = _classify_message(text)
        phones, amounts, amb = cls['phones'], cls['amounts'], cls['ambiguous']
        if len(phones) == 1 and len(amounts) == 1:
            events.append(('pair', phones[0], amounts[0], mid, bool(amb)))
        elif phones and amounts:
            n = min(len(phones), len(amounts))
            for i in range(n):
                events.append(('pair', phones[i], amounts[i], mid, bool(amb)))
            for p in phones[n:]:
                events.append(('phone', p, mid))
            for a in amounts[n:]:
                events.append(('amount', a, mid))
        elif phones:
            for p in phones:
                events.append(('phone', p, mid))
        elif amounts:
            for a in amounts:
                events.append(('amount', a, mid))
        elif cls['has_name']:
            events.append(('name', mid))
    return events


def _pair_events(events):
    """Two-queue FIFO (positional) pairing. Returns
    (pairs, pair_mids, orphans, ambiguous_out, list_pattern).

    pair_mids[i] is the SET of inbound message ids behind pairs[i] — phone+amount
    for a split pair, the single message for a self-contained one. Handles BOTH
    customer conventions (interleaved P A P A …, and parallel list P P P A A A …);
    a pairing made while >1 candidate of the opposite kind waits is a list-pattern
    guess → confidence 'low' + flagged. Names are skipped (never queued)."""
    pairs: List[Dict[str, Any]] = []
    pair_mids: List[set] = []
    orphans: List[Dict[str, Any]] = []
    ambiguous_out: List[Dict[str, Any]] = []
    pending_phones = []   # FIFO of (phone, message_id)
    pending_amounts = []  # FIFO of (value, message_id)
    list_pattern = False

    def emit(phone, value, source_mid, confidence, reason=None, mids=None):
        op = {
            'account_number': phone, 'value': value, 'type': 'كاش',
            'source_message_id': source_mid, 'confidence': confidence,
        }
        pairs.append(op)
        pair_mids.append({m for m in (mids or [source_mid]) if m})
        if reason:
            ambiguous_out.append({**op, 'reason': reason})

    for ev in events:
        kind = ev[0]
        if kind == 'name':
            continue  # names never occupy a slot
        if kind == 'pair':
            # self-contained phone+amount in one message — unambiguous
            emit(ev[1], ev[2], ev[3], 'low' if ev[4] else 'high',
                 'separator_ambiguous' if ev[4] else None, mids=[ev[3]])
        elif kind == 'phone':
            phone, mid = ev[1], ev[2]
            if pending_amounts:
                ambiguous = len(pending_amounts) >= 2   # list/block guess
                value, _amid = pending_amounts.pop(0)   # oldest amount (positional)
                if ambiguous:
                    list_pattern = True
                # source_message_id is ALWAYS the phone message's id
                emit(phone, value, mid, 'low' if ambiguous else 'high',
                     'list_pairing' if ambiguous else None, mids=[mid, _amid])
            else:
                pending_phones.append((phone, mid))
        elif kind == 'amount':
            value, mid = ev[1], ev[2]
            if pending_phones:
                ambiguous = len(pending_phones) >= 2
                phone, pmid = pending_phones.pop(0)     # oldest phone (positional)
                if ambiguous:
                    list_pattern = True
                emit(phone, value, pmid, 'low' if ambiguous else 'high',
                     'list_pairing' if ambiguous else None, mids=[pmid, mid])
            else:
                pending_amounts.append((value, mid))

    for phone, mid in pending_phones:
        orphans.append({'kind': 'phone', 'value': phone, 'message_id': mid})
    for value, mid in pending_amounts:
        orphans.append({'kind': 'amount', 'value': value, 'message_id': mid})

    return pairs, pair_mids, orphans, ambiguous_out, list_pattern


def consumed_ids_by_source(conv):
    """{phone source_message_id -> [all inbound message ids consumed by that pair]}
    for the conversation's CURRENT unprocessed inbound burst, via the SAME
    authoritative fetch + classification + pairing the planner uses.

    The create path watermarks EVERY id here, not just the phone message — so the
    amount message of a split pair can't linger 'unprocessed' and leak into the
    next burst (where it shifts the positional pairing and mis-routes amounts).
    Best-effort: returns {} on any error (never blocks a create)."""
    out: Dict[str, set] = {}
    if conv is None:
        return {}
    try:
        from datetime import timedelta
        from django.conf import settings as _dj
        from django.utils import timezone as _tz
        from django.db.models.functions import Coalesce
        from modules.chat.models import Message as _M
        _cut = _tz.now() - timedelta(minutes=getattr(_dj, 'AI_UNPROCESSED_WINDOW_MIN', 6))
        rows = list(
            _M.objects_all
            .filter(conversation=conv, direction='inbound', active=True, type='text',
                    ai_consumed_at__isnull=True, created_at__gte=_cut)
            .annotate(_ord=Coalesce('social_sent_at', 'created_at'))
            .order_by('_ord', 'id')
        )
        messages, msg_text = [], {}
        for r in rows:
            mid = str(r.id)
            txt = (r.content.get('text') if isinstance(r.content, dict) else '') or ''
            messages.append({'message_id': mid, 'text': txt})
            msg_text[mid] = txt
        pairs, pair_mids, _orph, _amb, _lp = _pair_events(_build_events(messages, msg_text))
        for op, mids in zip(pairs, pair_mids):
            sid = op.get('source_message_id')
            if sid:
                out.setdefault(sid, set()).update(m for m in mids if m)
        return {k: list(v) for k, v in out.items()}
    except Exception:
        logger.warning('consumed_ids_by_source failed', exc_info=True)
        return {}


@tool(
    name='qurtoba_plan_transactions',
    display_name='Plan Burst Transactions (phone↔amount pairing)',
    description=(
        'READ-ONLY planner. Call this FIRST for any burst of 2+ customer messages that '
        'look like cash transactions, BEFORE qurtoba_create_new_transactions_bulk. Pass '
        'the burst messages in time order; the tool pairs each phone number with its '
        'amount deterministically and returns: '
        '- pairs: [{account_number, value, type, source_message_id, confidence}] — feed '
        'these straight into the bulk create tool (source_message_id is already the '
        'PHONE message id for each). '
        '- orphans: [{kind, value, message_id}] — a phone with no amount or an amount '
        'with no phone. Ask ONE short question per orphan; never guess. '
        '- ambiguous: pairs whose amount had an uncertain `.`/`,` reading — confirm if unsure. '
        '- list_pattern (bool) + per-pair confidence ("high"/"low"): TRUE when the numbers and '
        'amounts arrived as TWO SEPARATE LISTS (all numbers, then all amounts) rather than each '
        'number next to its amount. The tool pairs them by position (1st→1st, 2nd→2nd); when '
        'list_pattern is true OR a pair is "low", CONFIRM the matching with the customer before '
        'executing — positional pairing is a reasonable guess, not a certainty. '
        '- needs_resend (bool) + same_time_overflow (bool) + resend: [{account_number, value}] — '
        'a HARD tool decision you cannot override. Too many transactions (>4) arrived in the same '
        'second with numbers/amounts in SEPARATE messages, so their order is unsafe to trust. The '
        'withheld transactions are NOT in `pairs` — do NOT reconstruct or create them. Relay the '
        '`note` in your own words: ask the customer to resend each number with its amount in one '
        'message, max 4 at a time. (Self-contained pairs are never withheld — create those.) '
        'KEY RULES the tool applies for you: names/labels (like "حمدي", "محفظه") are '
        'skipped and never steal an amount; "27.460"→27460 (thousands), "13.75" is a '
        'commission tally (ignored), "20الف"→20000, "الفين"→2000, Arabic digits are handled. '
        'INPUT: messages — an array of {message_id, text} in the order received. Always '
        'include message_id (from each "[message_id: <uuid>]" marker) so the tool reads '
        'the authoritative text and returns correct source ids.'
    ),
    category='qurtoba',
    requires_auth=True,
    parameters_schema={
        'type': 'object',
        'properties': {
            'messages': {
                'type': 'array',
                'minItems': 1,
                'maxItems': 80,
                'items': {
                    'type': 'object',
                    'properties': {
                        'message_id': {
                            'type': ['string', 'null'],
                            'description': 'UUID from the "[message_id: <uuid>]" marker.',
                        },
                        'text': {
                            'type': ['string', 'null'],
                            'description': 'The message text (fallback when message_id is absent).',
                        },
                    },
                },
            },
        },
        'required': ['messages'],
    },
)
def qurtoba_plan_transactions(
    context,
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(messages, list) or not messages:
        return {'success': False, 'error_type': 'empty', 'error': 'مرر قائمة الرسائل.'}

    conv = getattr(context, 'conversation', None)

    # SMARTER PLANNER — don't trust the list the LLM transcribed. The model can silently
    # DROP or reorder a line (we caught it omitting a phone number, which orphaned an
    # amount and made the agent ask a nonsense question). When we have the conversation,
    # pull the UNPROCESSED inbound messages straight from the DB in true send order
    # (social_sent_at, created_at fallback, id tiebreak) and use THAT as the authoritative
    # list. The LLM's array stays only as a fallback when there is no conversation handle.
    if conv is not None:
        try:
            from datetime import timedelta
            from django.conf import settings as _dj
            from django.utils import timezone as _tz
            from django.db.models.functions import Coalesce
            from modules.chat.models import Message as _M
            _cut = _tz.now() - timedelta(minutes=getattr(_dj, 'AI_UNPROCESSED_WINDOW_MIN', 6))
            _rows = list(
                _M.objects_all
                .filter(conversation=conv, direction='inbound', active=True, type='text',
                        ai_consumed_at__isnull=True, created_at__gte=_cut)
                .annotate(_ord=Coalesce('social_sent_at', 'created_at'))
                .order_by('_ord', 'id')
            )
            if _rows:
                messages = [
                    {'message_id': str(r.id),
                     'text': (r.content.get('text') if isinstance(r.content, dict) else '') or ''}
                    for r in _rows
                ]
        except Exception:
            logger.warning('planner: authoritative conversation fetch failed; '
                           'falling back to the LLM-provided list', exc_info=True)

    # Re-fetch authoritative text by message id so the pairing can't be skewed by
    # a mis-quoted text argument. Also grab each message's send-second (social_sent_at,
    # created_at fallback) — used to detect same-second clusters whose order WhatsApp
    # can't guarantee.
    msg_text: Dict[str, str] = {}
    msg_sent: Dict[str, Any] = {}
    if conv is not None:
        try:
            from modules.chat.models import Message
            ids = [str(m.get('message_id')).strip()
                   for m in messages if isinstance(m, dict) and m.get('message_id')]
            if ids:
                for mm in Message.objects_all.filter(conversation=conv, id__in=ids):
                    c = mm.content
                    if isinstance(c, dict) and isinstance(c.get('text'), str):
                        msg_text[str(mm.id)] = c['text']
                    _sent = getattr(mm, 'social_sent_at', None) or getattr(mm, 'created_at', None)
                    if _sent is not None:
                        msg_sent[str(mm.id)] = _sent
        except Exception:
            logger.warning('qurtoba_plan_transactions: text re-fetch failed', exc_info=True)

    # Build the ordered event stream, then pair phones↔amounts positionally
    # (shared with consumed_ids_by_source so watermarking sees the same pairs).
    events = _build_events(messages, msg_text)
    pairs, pair_mids, orphans, ambiguous_out, list_pattern = _pair_events(events)

    # --- Same-time split-ambiguity guard ---------------------------------
    # WhatsApp timestamps only to the second and does NOT guarantee order for
    # messages sent in the same instant. When several transactions arrive
    # together with the number and amount in SEPARATE messages, our positional
    # pairing of that cluster is a guess — better to ask the customer to resend
    # those few (number+amount in one message, or 4-by-4) than risk a wrong match.
    # Detection: group the split (phone-only / amount-only) messages into
    # ≤window-second clusters; a cluster holding ≥2 phones AND ≥2 amounts is the
    # genuinely-reorderable set. A lone split (1 phone + 1 amount) has only one
    # possible pairing, so it is never flagged.
    from django.conf import settings as _dj
    window_s = getattr(_dj, 'AI_SAME_TIME_WINDOW_SEC', 5)
    ambiguous_split_mids = _same_time_split_mids(events, msg_sent, window_s)

    # HARD CAP (deterministic, tool-owned — never the LLM): Meta delivers messages
    # sent in the same second in an unreliable order. When the number and amount are
    # in SEPARATE messages, a large same-second burst can't be paired safely at all.
    # If any same-second split cluster carries MORE THAN `AI_SAME_TIME_MAX_TX` (4)
    # transactions, withhold the WHOLE cluster — process NONE of it — and ask the
    # customer to resend cleanly. Self-contained pairs (number+amount in one message)
    # are not split events, never enter a cluster, and are always processed no matter
    # how many arrive in the same second.
    max_tx = getattr(_dj, 'AI_SAME_TIME_MAX_TX', 4)
    overflow_mids = _same_time_overflow_mids(events, msg_sent, window_s, max_tx)
    blocked_overflow = bool(overflow_mids)

    needs_resend = (getattr(_dj, 'AI_SPLIT_RESEND_GUARD', True)
                    and bool(ambiguous_split_mids)
                    and len(pairs) >= getattr(_dj, 'AI_SPLIT_RESEND_MIN_TX', 5))

    # Union of every mid we must withhold: the hard-capped overflow cluster(s) plus
    # the softer same-second ambiguity set. Withheld pairs are never fed to the bulk
    # tool; they go into `resend` for the agent to ask the customer to re-send.
    withhold_mids = set(overflow_mids)
    if needs_resend:
        withhold_mids |= ambiguous_split_mids

    resend: List[Dict[str, Any]] = []
    if withhold_mids:
        safe_pairs: List[Dict[str, Any]] = []
        for op, mids in zip(pairs, pair_mids):
            if mids & withhold_mids:
                resend.append({'account_number': op['account_number'], 'value': op['value']})
            else:
                safe_pairs.append(op)
        pairs = safe_pairs   # withheld ones are never fed to the bulk tool

    note = 'كل رقم متطابق مع مبلغه.'
    if blocked_overflow:
        note = ('وصلت رسائل كتير في نفس اللحظة (أكتر من 4 تحويلات) والأرقام والمبالغ في رسائل '
                'منفصلة — ترتيب وصولها عندنا مش مضمون، فمش هننفّذ أي تحويل منها عشان ما يحصلش '
                'خطأ في مطابقة رقم بمبلغ. اطلب من العميل بصياغتك الطبيعية (مش جملة ثابتة) إنه '
                'يعيد الإرسال: كل رقم ومبلغه في رسالة واحدة، وبحد أقصى 4 تحويلات في المرة، '
                'ووضّح له السبب باختصار.')
    elif needs_resend:
        note = ('رسائل وصلت في نفس اللحظة وفيها أرقام ومبالغ في رسائل منفصلة — ترتيب الوصول '
                'عندنا ممكن يختلف فلا نضمن أي مبلغ لأي رقم. نفّذ الواضح، واطلب من العميل بصياغتك '
                'الطبيعية (مش جملة ثابتة) يعيد إرسال دي تحديدًا: كل رقم ومبلغه في رسالة واحدة '
                'أو 4 ب 4 منظمة، ووضّح له السبب باختصار.')
    elif orphans:
        note = 'بعض الأرقام أو المبالغ بدون مقابل — اسأل عنها قبل التنفيذ.'
    elif list_pattern:
        note = ('الأرقام والمبالغ وصلت كقائمتين منفصلتين — تم الربط بالترتيب '
                '(الأول بالأول). راجِع المطابقة قبل التنفيذ.')
    return {
        'success': True,
        'pairs': pairs,
        'orphans': orphans,
        'ambiguous': ambiguous_out,
        'list_pattern': list_pattern,
        'needs_resend': bool(needs_resend or blocked_overflow),
        'same_time_overflow': blocked_overflow,
        'resend': resend,
        'summary': {
            'pairs_count': len(pairs),
            'orphans_count': len(orphans),
            'distinct_phones': len({p['account_number'] for p in pairs}),
        },
        'note': note,
    }
