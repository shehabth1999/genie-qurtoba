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
from qurtoba.tools._amounts import (
    normalize_amount, _ar_to_ascii, _arabic_normalize,
    _CURRENCY_WORDS, _THOUSAND_FORMS, _THOUSAND_DUAL, _MILLION_FORMS, _MILLION_DUAL,
)
from qurtoba.tools.transactions import _normalize_phone

logger = logging.getLogger(__name__)

# Fee/deduction INSTRUCTIONS carry a number that is a FEE reference, never a transfer
# amount: «لو هيخصم 15 اخصمها» (if it deducts 15, deduct it), «اخصم الرسوم», «العمولة عليا».
# A line mentioning deduction / fees / commission is a note — its number must NEVER become
# a transfer amount (that created a phantom 15 EGP op and orphaned the real amount). The
# خصم root (خصم/يخصم/تخصم/هيخصم/اخصم/اخصمها/خصمها) is caught by the substring «خصم».
_FEE_NOTE_RE = re.compile(r'خصم|رسوم|عمول|مصاريف')

# Reference / receipt-serial phrases whose number is an ID, NEVER a transfer amount:
# «رقم العملية 5», «الرقم المرجعي 12345», «reference 88». (A bare «رقم» is NOT here — that
# often precedes a phone; only these reference-phrase roots count.)
_REF_NOTE_RE = re.compile(r'العملي|مرجع|reference|receipt|رقم الايصال|رقم الإيصال')

# Strong "this text is a spelled-out NUMBER" signal — thousand/million multiplier words and
# hundreds roots. Used to tell the LLM to READ a value the deterministic parser can't catch
# («خمسين الف»=50000, «خمسمائة»=500). Plain names (شاكر, الحرمين, أمين) contain none of these.
_SPELLED_NUM_HINTS = ('الف', 'الاف', 'الوف', 'الفين', 'مليون', 'مليونين',
                      'مية', 'مائة', 'مئة', 'متين', 'ميتين', 'مئتين')


def _looks_like_spelled_amount(text: str) -> bool:
    """True if `text` MIGHT carry an amount written in Arabic words (contains a multiplier/
    hundreds root). A conservative HINT only — it deliberately over-matches (a name like
    «سمية» contains «مية»), so the surfaced `note` tells the LLM to read a real number word
    but IGNORE a person's name. `_arabic_normalize` is applied so «ألفت»/«الفت» match alike."""
    t = _arabic_normalize(_ar_to_ascii(text or ''))
    return any(h in t for h in _SPELLED_NUM_HINTS)


def _coerce_fallback(v: Any) -> Optional[float]:
    """LLM-provided FALLBACK amount → a clean positive whole number, or None.

    This is the "no fail" path: the LLM reads a value the deterministic parser can't
    (an amount written in Arabic words, «خمسين الف»=50000) and passes the NUMBER as a
    per-message fallback. Run it through the SAME normalizer so 50000 / "50000" / "٥٠٠٠٠"
    all land the same; reject anything non-numeric, fractional, or ≤0 (a bad fallback is
    ignored, never allowed to create a wrong amount)."""
    if v is None:
        return None
    r = normalize_amount(v)
    if r.get('ok') and r['value'] and float(r['value']).is_integer() and r['value'] > 0:
        return float(r['value'])
    return None


# Non-digit words that legitimately sit next to (or glued onto) an amount: currency words,
# the thousand/million MULTIPLIER words, and cash/wallet TYPE keywords. Anything else glued
# to a digit is a NAME/serial. (Latin k/m kept for "20k"/"2.5m".)
_GLUE_OK_WORDS = (set(_CURRENCY_WORDS) | _THOUSAND_FORMS | _THOUSAND_DUAL
                  | _MILLION_FORMS | _MILLION_DUAL
                  | {'كاش', 'فورى', 'فوري', 'امان', 'طاير', 'محفظه', 'محفظة',
                     'فودافون', 'اتصالات', 'اورانج', 'وي', 'k', 'm'})


def _is_glued_name_label(tok: str) -> bool:
    """True if `tok` is an Arabic NAME with digits glued to its END — «عبدالله12», «طه13» —
    so those digits are a serial/label, NEVER a transfer amount.

    Rule: a real amount token LEADS with its number («2350..اسامة», «13300جنيه», «30الف»);
    a token that leads with Arabic LETTERS and has the digits trailing is a name+serial.
    The leading letters are checked against `_GLUE_OK_WORDS` so «كاش12080» / «30الف» (a type
    or multiplier lead) are still read as amounts, not labels."""
    t = _arabic_normalize(_ar_to_ascii(tok or '')).lower().strip()
    if not re.search(r'\d', t):
        return False
    m = re.match(r'^([^\d]*)\d', t)          # letters BEFORE the first digit
    lead = re.sub('[^a-z؀-ۿ]', '', m.group(1) if m else '')
    if not lead:
        return False                          # number leads → real amount candidate
    for w in _GLUE_OK_WORDS:
        lead = lead.replace(w, '')
    return bool(lead)                          # leftover real letters before the digit → a name label


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
    # Drop bracketed/parenthesised content — it's a serial / device tag / reference note
    # («(vivo - shehab - 652)», «(124)», «[3]»), NEVER a transfer amount. Removing it stops
    # its digits from being read as an amount. Balanced brackets only (repeat to peel nesting);
    # an amount is never written inside brackets, so this is safe.
    for _ in range(3):
        _new = re.sub(r'\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}', ' ', text)
        if _new == text:
            break
        text = _new
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

        # A leftover token shaped like a phone (leading 0, 10–12 digits) but that
        # FAILED 11-digit normalization is a BROKEN number, never a real amount — real
        # transfer amounts are ≤9 digits. Drop it (as noise → the agent asks for a valid
        # number) so «0100600100» can't become the amount 100,600,100 paired with a phone.
        _kept_rest = []
        for _tok in rest:
            if re.fullmatch(r'0\d{9,11}', _tok):
                has_name = True            # broken phone → noise, not a giant amount
            elif _is_glued_name_label(_tok):
                has_name = True            # «عبدالله12» → name+serial, its digit is NOT an amount
            else:
                _kept_rest.append(_tok)
        rest = _kept_rest

        rest_text = ' '.join(rest).strip()
        if not rest_text:
            continue
        # Fee-deduction instruction («لو هيخصم 15 اخصمها», «اخصم الرسوم») → the number here
        # is a FEE reference, not a transfer amount. Treat the line as a note; extract nothing.
        # Same for a reference/receipt-serial phrase («رقم العملية 5», «الرقم المرجعي 12345»).
        if _FEE_NOTE_RE.search(rest_text) or _REF_NOTE_RE.search(rest_text):
            has_name = True
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


def _cluster_split_by_second(events, msg_sent):
    """Group SPLIT (phone-only / amount-only) events into clusters that share the SAME
    send-SECOND. Returns a list of clusters, each a list of (kind, mid).

    KEY: Meta only fails to preserve order for messages sent in the SAME second — messages
    a full second apart ARE reliably ordered. So the genuinely-reorderable unit is ONE second,
    NOT a multi-second span. (The old 5-second sliding window lumped a legit ~1-msg/second
    stream — e.g. 7 transfers streamed over 5s — into a single fake "instant" and wrongly
    blocked the whole batch as overflow.) Truncate each send-time to the second and group by
    equality. Events with no known send-time are skipped (treated as non-ambiguous)."""
    items = []  # (kind, mid, sent)
    for ev in events:
        if ev[0] in ('phone', 'amount'):
            sent = msg_sent.get(ev[2])
            if sent is not None:
                items.append((ev[0], ev[2], sent))
    items.sort(key=lambda s: s[2])
    clusters = []
    i = 0
    while i < len(items):
        sec = items[i][2].replace(microsecond=0)
        cluster = []
        while i < len(items) and items[i][2].replace(microsecond=0) == sec:
            cluster.append((items[i][0], items[i][1]))
            i += 1
        clusters.append(cluster)
    return clusters


def _same_time_split_mids(events, msg_sent, window_s=None):
    """Message ids of SPLIT messages whose arrival order WhatsApp can't be trusted to preserve
    (same-second), so positional pairing of them is a guess. A same-second cluster holding
    ≥2 phones AND ≥2 amounts is the genuinely-reorderable set → all its ids are returned. A
    lone split (1 phone + 1 amount) in a second has only one pairing, so it is never flagged.
    (`window_s` is accepted for backward compat but ignored — clustering is per-second now.)"""
    flagged = set()
    for cluster in _cluster_split_by_second(events, msg_sent):
        n_ph = sum(1 for k, _m in cluster if k == 'phone')
        n_am = sum(1 for k, _m in cluster if k == 'amount')
        if n_ph >= 2 and n_am >= 2:
            flagged.update(m for _k, m in cluster)
    return flagged


def _same_time_overflow_mids(events, msg_sent, window_s, max_tx):
    """Message ids of same-SECOND SPLIT clusters carrying MORE THAN ``max_tx`` reorderable
    transactions — the hard cap. A cluster's transaction count is the pairings it forces,
    ``min(#phones, #amounts)``; when that exceeds ``max_tx`` every id in the cluster is
    returned so the caller withholds the WHOLE cluster and asks for a clean resend.

    Now scoped to a SINGLE second (see `_cluster_split_by_second`): a burst streamed over
    several seconds (≤``max_tx`` split per second) is NO LONGER blocked — only a genuine
    same-second flood of >``max_tx`` split transactions is. Self-contained pairs are
    ``('pair', …)`` events, never enter a cluster, and are never counted or blocked.
    (`window_s` accepted for backward compat but ignored.)"""
    overflow = set()
    for cluster in _cluster_split_by_second(events, msg_sent):
        n_ph = sum(1 for k, _m in cluster if k == 'phone')
        n_am = sum(1 for k, _m in cluster if k == 'amount')
        if min(n_ph, n_am) > max_tx:
            overflow.update(m for _k, m in cluster)
    return overflow


def _build_events(messages, msg_text, msg_fallback=None):
    """Ordered event stream from a message list. Each event is one of:
    ('pair', phone, amount, mid, amb) | ('phone', phone, mid) |
    ('amount', value, mid) | ('name', mid). Shared by the planner tool and
    consumed_ids_by_source so both classify/order identically.

    `msg_fallback` — optional {message_id: number} of LLM-read amounts. Used ONLY for a
    message where the deterministic parser caught NO amount (a value written in Arabic
    words): the LLM's number fills the slot so the value is never lost. When the parser
    already found an amount, the fallback is ignored (Python wins)."""
    msg_fallback = msg_fallback or {}
    events = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        mid = str(item.get('message_id') or '').strip() or None
        text = (msg_text or {}).get(mid) or item.get('text') or ''
        cls = _classify_message(text)
        phones, amounts, amb = cls['phones'], cls['amounts'], cls['ambiguous']
        # Fallback: only when Python parsed nothing for this message. Prefer the id-keyed
        # map (survives the DB-authoritative re-fetch that replaces the item list) and fall
        # back to an inline `amount` on the item (no-conversation path).
        if not amounts:
            fb = _coerce_fallback(msg_fallback.get(mid) if mid else None)
            if fb is None:
                fb = _coerce_fallback(item.get('amount'))
            if fb is not None:
                amounts = [fb]
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


def _ev_mid(ev):
    """The message id an event came from (kind-dependent tuple position)."""
    if ev[0] == 'name':
        return ev[1]
    if ev[0] == 'pair':
        return ev[3]
    return ev[2]   # phone / amount


def _pair_events(events, msg_sent=None, gap_s=None):
    """Two-queue FIFO (positional) pairing. Returns
    (pairs, pair_mids, orphans, ambiguous_out, list_pattern).

    pair_mids[i] is the SET of inbound message ids behind pairs[i] — phone+amount
    for a split pair, the single message for a self-contained one. Handles BOTH
    customer conventions (interleaved P A P A …, and parallel list P P P A A A …);
    a pairing made while >1 candidate of the opposite kind waits is a list-pattern
    guess → confidence 'low' + flagged. Names are skipped (never queued).

    BURST BOUNDARY: when ``msg_sent`` + ``gap_s`` are given, a pending phone/amount
    is NOT carried across a time gap larger than ``gap_s`` seconds — the queues are
    flushed to orphans first. This stops a leftover (e.g. an un-answered orphan
    amount) from an EARLIER burst grabbing a phone in a brand-new burst minutes
    later (the 24200→next-number mis-route). Intra-burst spacing is seconds, so a
    real burst is never split. Without the times it pairs exactly as before."""
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

    def _flush_pending():
        for phone, mid in pending_phones:
            orphans.append({'kind': 'phone', 'value': phone, 'message_id': mid})
        for value, mid in pending_amounts:
            orphans.append({'kind': 'amount', 'value': value, 'message_id': mid})
        pending_phones.clear()
        pending_amounts.clear()

    prev_t = None
    for ev in events:
        # Burst boundary: flush any pending phone/amount that would otherwise cross a
        # large time gap to pair with this (new-burst) message.
        if gap_s and msg_sent is not None:
            t = msg_sent.get(_ev_mid(ev))
            if t is not None:
                if prev_t is not None and (t - prev_t).total_seconds() > gap_s:
                    _flush_pending()
                prev_t = t
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
        messages, msg_text, msg_sent = [], {}, {}
        for r in rows:
            mid = str(r.id)
            txt = (r.content.get('text') if isinstance(r.content, dict) else '') or ''
            messages.append({'message_id': mid, 'text': txt})
            msg_text[mid] = txt
            _s = getattr(r, 'social_sent_at', None) or getattr(r, 'created_at', None)
            if _s is not None:
                msg_sent[mid] = _s
        _gap = getattr(_dj, 'AI_BURST_GAP_SEC', 45)
        pairs, pair_mids, _orph, _amb, _lp = _pair_events(
            _build_events(messages, msg_text), msg_sent, _gap)
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
        '- needs_resend / same_time_overflow (bool) + resend: [{account_number, value}] — a HARD '
        'tool decision you cannot override, and it fires ONLY for a same-second FLOOD (MORE than 3 '
        'transactions arrived in the same second with numbers/amounts in SEPARATE messages, order '
        'unsafe). The withheld ones are NOT in `pairs` — do NOT reconstruct or create them; relay '
        'the `note` (your own words): resend each number with its amount in one message, max 3 at a '
        'time. A same-second SPLIT of ≤3 is NOT withheld — those come back as normal `pairs`, '
        'EXECUTE them (no confirmation). Self-contained pairs are never withheld. '
        '- read_amounts: [{message_id, text}] — messages carrying an amount written in Arabic '
        'WORDS that the tool CANNOT convert to a number (e.g. «خمسين الف», «خمسمائة», «ميتين»). '
        'These usually orphan their phone. YOU can read Arabic number words — so READ the value '
        'yourself and create the op (خمسين الف=50000, خمسمائة=500, ميتين=200, ألفين=2000, خمسة '
        'آلاف=5000). Do NOT ask the customer for an amount that is already there in words, and do '
        'NOT trust any stray number the tool guessed for those lines. '
        'FALLBACK (best — never fail): when a message\'s amount is written in Arabic WORDS, read '
        'the number yourself and pass it on THAT message as `amount` (e.g. {message_id: "...", '
        'text: "خمسين الف", amount: 50000}). The tool then uses your number as this message\'s '
        'value and pairs it normally — no orphan, no read_amounts, no second guess. Only messages '
        'you did NOT supply an amount for come back in read_amounts. Pass `amount` ONLY for '
        'spelled/worded values; for normal digits leave it out and let the tool read them. '
        'KEY RULES the tool applies for you: names/labels (like "حمدي", "محفظه", "الحرمين") are '
        'skipped and never steal an amount; fee-instruction notes ("لو هيخصم 15 اخصمها") are '
        'skipped and their number is never an amount; "27.460"→27460 and "1.380"→1380 (a dot/comma '
        'before 3 digits = thousands), "13.75"/"6.08" are commission tallies (ignored, never an '
        'amount), "20الف"→20000, "30الف"→30000, "الفين"→2000, Arabic-Indic digits (٥٠٠٠→5000) are '
        'handled, country codes normalize ("+20 12 7318 1841"→01273181841). '
        'WORKED EXAMPLES: '
        '(1) «01111568990\\n1370» + «01005161043\\n1370» + «01069411663» + «500 جنيه» → 3 pairs '
        '(1370→01111568990, 1370→01005161043) and the split 500↔01069411663 auto-paired; execute all. '
        '(2) «01012745373 / 24200 / 01105430994 / 13450 / 01226086860 / 6760 / الحرمين / لو هيخصم 15 '
        'اخصمها» → the names + the fee «15» are dropped; returns 3 pairs (24200/13450/6760), '
        'list_pattern=true → confirm the matching, then execute. '
        '(3) «01070458397\\nتحويل 30الف\\nلاشين» → one self-contained pair 30000→01070458397. '
        '(4) «01019525475\\nخمسين الف»: BEST — read 50000 yourself and pass it on that message '
        '({message_id, text, amount: 50000}); the tool returns the pair 50000→01019525475 ready '
        'to execute. If you DON\'T pass amount, the phone orphans and read_amounts=[«خمسين الف»] '
        'tells you to read 50000 and create it. '
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
                        'amount': {
                            'type': ['number', 'null'],
                            'description': (
                                'OPTIONAL fallback amount YOU read for THIS message, as a plain '
                                'number. Pass it ONLY when the amount is written in Arabic WORDS '
                                'the tool can\'t convert (e.g. text «خمسين الف» → amount 50000, '
                                '«خمسمائة» → 500, «ميتين» → 200, «ألفين» → 2000). The tool uses it '
                                'as the value for this message so nothing is lost. If the amount is '
                                'already normal digits, leave this out — the tool reads it itself '
                                'and ignores this field.'
                            ),
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

    # LLM-provided fallback amounts, keyed by message id — captured NOW, before the
    # authoritative DB re-fetch below replaces `messages` (the DB rows don't carry the
    # LLM's `amount`). This is the "no fail" path for values written in Arabic words.
    msg_fallback: Dict[str, float] = {}
    for m in messages:
        if isinstance(m, dict) and m.get('message_id'):
            fb = _coerce_fallback(m.get('amount'))
            if fb is not None:
                msg_fallback[str(m['message_id']).strip()] = fb

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
    # gap_s flushes pending queues at a burst boundary so a stale orphan from an
    # earlier burst can't grab a phone in a new one.
    from django.conf import settings as _dj0
    _gap = getattr(_dj0, 'AI_BURST_GAP_SEC', 45)
    events = _build_events(messages, msg_text, msg_fallback)
    pairs, pair_mids, orphans, ambiguous_out, list_pattern = _pair_events(events, msg_sent, _gap)

    # --- Same-time split-ambiguity guard ---------------------------------
    # WhatsApp timestamps only to the second and does NOT guarantee order for
    # messages sent in the same instant. When several transactions arrive
    # together with the number and amount in SEPARATE messages, our positional
    # pairing of that cluster is a guess — better to ask the customer to resend
    # those few (number+amount in one message, or 3-by-3) than risk a wrong match.
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
    # If any same-second split cluster carries MORE THAN `AI_SAME_TIME_MAX_TX` (3)
    # transactions, withhold the WHOLE cluster — process NONE of it — and ask the
    # customer to resend cleanly. Self-contained pairs (number+amount in one message)
    # are not split events, never enter a cluster, and are always processed no matter
    # how many arrive in the same second.
    max_tx = getattr(_dj, 'AI_SAME_TIME_MAX_TX', 3)
    overflow_mids = _same_time_overflow_mids(events, msg_sent, window_s, max_tx)
    blocked_overflow = bool(overflow_mids)

    # OWNER DECISION (2026-07-12): a same-second SPLIT cluster of ≤max_tx (3) transactions is
    # EXECUTED directly — trust the positional pairing, do NOT ask «تأكيد». Only a genuine
    # same-second FLOOD (> max_tx split) is withheld (overflow). The old soft `needs_resend`
    # (≥5-pair) guard and the 2–3-tx confirmation are removed: they mostly fired on stale/
    # mixed bursts and annoyed the customer («ليه بتبعت تاكيد»). Overflow(>3) is the ONLY
    # same-second gate now; self-contained pairs are never gated.
    withhold_mids = set(overflow_mids)

    resend: List[Dict[str, Any]] = []
    safe_pairs: List[Dict[str, Any]] = []
    for op, mids in zip(pairs, pair_mids):
        if mids & withhold_mids:
            resend.append({'account_number': op['account_number'], 'value': op['value']})
            continue
        # Same-second split within the cap → EXECUTE. Force 'high' so a block-arrival 'low'
        # from _pair_events doesn't make the agent confirm what the owner wants executed.
        if mids & ambiguous_split_mids:
            op['confidence'] = 'high'
        safe_pairs.append(op)
    pairs = safe_pairs   # only overflow(>max_tx) clusters are withheld
    # list_pattern now reflects only genuine positional guesses NOT covered by the same-second
    # execute rule (e.g. a cross-second "all numbers then all amounts" block) — never same-second ≤3.
    list_pattern = any(op.get('confidence') == 'low' for op in pairs)

    # --- Amounts written in WORDS the parser can't convert → hand them to the LLM ---------
    # «خمسين الف» (50000), «خمسمائة» (500): the deterministic parser refuses these (it would
    # otherwise mis-read «خمسين الف» as 1000). But the LLM CAN read Arabic number words. Surface
    # every message that carries a spelled amount which produced NO numeric value, so the agent
    # reads the value itself and includes the op — instead of dropping it or asking the customer.
    read_amounts: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        mid = str(m.get('message_id') or '').strip() or None
        txt = (msg_text.get(mid) if mid else None) or m.get('text') or ''
        cls = _classify_message(txt)
        if cls['amounts']:
            continue
        # LLM already supplied the value for this message — nothing to hand back.
        if _coerce_fallback(msg_fallback.get(mid) if mid else None) is not None \
                or _coerce_fallback(m.get('amount')) is not None:
            continue
        if _looks_like_spelled_amount(txt):
            read_amounts.append({
                'message_id': mid,
                'text': ' '.join(str(txt).split()),
            })

    note = 'كل رقم متطابق مع مبلغه.'
    if blocked_overflow:
        note = ('وصلت رسائل كتير في نفس اللحظة (أكتر من 3 تحويلات) والأرقام والمبالغ في رسائل '
                'منفصلة — ترتيب وصولها عندنا مش مضمون، فمش هننفّذ أي تحويل منها عشان ما يحصلش '
                'خطأ في مطابقة رقم بمبلغ. اطلب من العميل بصياغتك الطبيعية (مش جملة ثابتة) إنه '
                'يعيد الإرسال: كل رقم ومبلغه في رسالة واحدة، وبحد أقصى 3 تحويلات في المرة، '
                'ووضّح له السبب باختصار.')
    elif orphans:
        note = 'بعض الأرقام أو المبالغ بدون مقابل — اسأل عنها قبل التنفيذ.'
    elif list_pattern:
        note = ('الأرقام والمبالغ وصلت كقائمتين منفصلتين — تم الربط بالترتيب '
                '(الأول بالأول). راجِع المطابقة قبل التنفيذ.')
    # A spelled-amount message often orphans its phone (the amount wasn't caught). Always tell
    # the LLM to read those values — this instruction wins over the generic "ask about orphans".
    if read_amounts:
        listed = '، '.join(r['text'] for r in read_amounts)
        note = (f'كلمات ممكن تكون مبالغ متكتبة بالحروف: [{listed}]. لو الكلمة رقم (زي «خمسين الف»=50000، '
                '«خمسمائة»=500، «ألفين»=2000) اقراها وحط قيمتها الرقمية في التحويل بنفسك من غير ما '
                'تسأل العميل. لكن لو الكلمة اسم شخص (زي «سمية»/«سامية»/«حلمية») تجاهلها — دي مش مبلغ. ' + note)

    # Debug log — one line capturing exactly what the planner decided for this burst.
    try:
        from qurtoba.tools._debuglog import log_event
        log_event(
            'planner', conversation=conv,
            in_msgs=len(messages),
            in_ids=[str(m.get('message_id'))[:8] for m in messages
                    if isinstance(m, dict) and m.get('message_id')] or None,
            pairs=[{'acc': p['account_number'], 'val': p['value'], 'conf': p['confidence'],
                    'src': str(p.get('source_message_id'))[:8] if p.get('source_message_id') else None}
                   for p in pairs] or None,
            orphans=[{'kind': o['kind'], 'val': o['value']} for o in orphans] or None,
            fb_used={k[:8]: v for k, v in msg_fallback.items()} or None,
            read_amounts=[r['text'] for r in read_amounts] or None,
            list_pattern=list_pattern or None,
            overflow=blocked_overflow or None,
            resend=[{'acc': r['account_number'], 'val': r['value']} for r in resend] or None,
        )
    except Exception:
        pass

    return {
        'success': True,
        'pairs': pairs,
        'orphans': orphans,
        'ambiguous': ambiguous_out,
        'list_pattern': list_pattern,
        'needs_resend': blocked_overflow,
        'same_time_overflow': blocked_overflow,
        'resend': resend,
        'read_amounts': read_amounts,
        'summary': {
            'pairs_count': len(pairs),
            'orphans_count': len(orphans),
            'distinct_phones': len({p['account_number'] for p in pairs}),
        },
        'note': note,
    }
