"""Burst → intent, deterministically.

``classify_rows`` is pure (no database) so it is unit-tested on the real message
shapes; ``route`` is the thin wrapper the workflow's function node calls: it loads
the current batch + the still-unprocessed burst from the database, classifies, and
returns a small dict the conditional node branches on.

Priority when a burst mixes things (first hit wins):
    receipt > cancel > transfer > status > balance > statement > social > freetext
Rows whose own intent differs from the winner are returned as ``secondary`` so the
handling node can still answer them (a greeting riding next to a transfer is noise,
but a balance question next to a transfer is answered after the transfer).
"""
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

from qurtoba.tools.planning import _classify_message, _looks_like_spelled_amount
from . import lexicon as L
from .arabic_numbers import parse_arabic_amount


class Intent:
    RECEIPT = 'receipt'
    CANCEL = 'cancel'
    TRANSFER = 'transfer'
    STATUS = 'status'
    BALANCE = 'balance'
    STATEMENT = 'statement'
    SOCIAL = 'social'
    OFF_HOURS = 'off_hours'
    FREETEXT = 'freetext'
    NOISE = 'noise'          # a name line, an emoji — nothing to do


_PRIORITY = [Intent.RECEIPT, Intent.CANCEL, Intent.TRANSFER, Intent.STATUS, Intent.BALANCE,
             Intent.STATEMENT, Intent.SOCIAL, Intent.FREETEXT]

_MSG_ID_RE = re.compile(r'\[message_id:\s*([0-9a-fA-F-]{36})\]')
_DATE_RE = re.compile(r'(20\d\d-\d\d-\d\d)')


def _has_money_signal(text: str) -> bool:
    """A phone or a whole amount in the text, or a spelled amount the number parser can read."""
    cls = _classify_message(text)
    if cls['phones'] or cls['amounts']:
        return True
    if _looks_like_spelled_amount(text) and parse_arabic_amount(text) is not None:
        return True
    return False


def classify_text(text: str, *, quotes_our_question: bool = False, pending_question: bool = False) -> Dict[str, Any]:
    """Intent of ONE text message. Returns {intent, sub, flags}."""
    raw = str(text or '')
    t = L.strip_waw(L.norm(raw))
    flags: Dict[str, Any] = {}
    if not t:
        return {'intent': Intent.NOISE, 'sub': None, 'flags': flags}

    # An answer to our own question (quoted on it, or a bare yes/no/amount right after it)
    # belongs to the money path — the planner folds it into its `answers`.
    if quotes_our_question or pending_question:
        if L.is_yes(raw) or L.is_no(raw) or _has_money_signal(raw):
            return {'intent': Intent.TRANSFER, 'sub': 'answer', 'flags': flags}

    if L.CANCEL.search(t):
        return {'intent': Intent.CANCEL, 'sub': None, 'flags': flags}

    if L.INSTAPAY.search(t):
        return {'intent': Intent.TRANSFER, 'sub': 'instapay', 'flags': flags}
    noncash = L.noncash_type(t)
    money = _has_money_signal(raw)
    has_phone = bool(_classify_message(raw)['phones'])
    if noncash and (money or re.search(r'\d{4,}', t)):
        return {'intent': Intent.TRANSFER, 'sub': 'noncash', 'flags': {'type': noncash}}
    if L.RECEIPT_WHERE.search(t):
        return {'intent': Intent.STATUS, 'sub': 'one', 'flags': flags}
    # payment wording without a phone → the receipt path; a bare «الإيصال اهو» counts, a long
    # sentence that merely mentions الإيصال («ابعتلي صورة الإيصال تاني») does not.
    if L.PAYMENT.search(t) and not has_phone and (len(t.split()) <= 3 or not re.search(r'الايصال', t)):
        return {'intent': Intent.RECEIPT, 'sub': 'words', 'flags': {}}
    if money:
        if L.STATUS_SUBSET.search(t) and not _classify_message(raw)['phones']:
            return {'intent': Intent.STATUS, 'sub': 'subset', 'flags': flags}
        return {'intent': Intent.TRANSFER, 'sub': 'cash', 'flags': flags}

    if L.STATUS_SUBSET.search(t):
        return {'intent': Intent.STATUS, 'sub': 'subset', 'flags': flags}
    if L.BALANCE.search(t):
        return {'intent': Intent.BALANCE, 'sub': None, 'flags': flags}
    if L.STATEMENT.search(t):
        m = _DATE_RE.search(t)
        return {'intent': Intent.STATEMENT, 'sub': None,
                'flags': {'report_date': m.group(1) if m else None,
                          'yesterday': bool(L.STATEMENT_YESTERDAY.search(t))}}
    if L.STATUS.search(t) and (L.is_question(raw) or len(t.split()) <= 4):
        return {'intent': Intent.STATUS, 'sub': 'one', 'flags': flags}

    if L.THANKS.search(t):
        return {'intent': Intent.SOCIAL, 'sub': 'thanks', 'flags': flags}
    if L.AVAILABILITY.search(t) and L.is_question(raw) or (L.AVAILABILITY.search(t) and len(t.split()) <= 3):
        return {'intent': Intent.SOCIAL, 'sub': 'availability', 'flags': flags}
    if L.GREETING.search(t):
        sub = 'wellbeing' if re.search(r'ازيك|عامل|اخبار|كيف', t) else \
              'morning' if 'صباح' in t else 'evening' if 'مساء' in t else 'greeting'
        return {'intent': Intent.SOCIAL, 'sub': sub, 'flags': flags}
    if L.is_only_emoji(raw) or L.is_yes(raw) or L.is_no(raw):
        return {'intent': Intent.NOISE, 'sub': 'ack', 'flags': flags}
    if len(t.split()) <= 2 and not L.is_question(raw):
        return {'intent': Intent.NOISE, 'sub': 'short', 'flags': flags}
    return {'intent': Intent.FREETEXT, 'sub': None, 'flags': flags}


def classify_rows(rows: List[Dict[str, Any]], *, pending_question: bool = False,
                  burst_has_money: bool = False, off_hours: bool = False) -> Dict[str, Any]:
    """Classify a batch of message rows.

    Each row: {id, type, text, quotes_outbound(bool), quotes_image(bool)}.
    `burst_has_money` — an older unprocessed row of this conversation still carries a
    phone/amount (an orphan waiting for its answer), so a bare amount/number now is
    part of the money path.
    """
    per_row: List[Dict[str, Any]] = []
    for r in rows:
        rtype = r.get('type') or 'text'
        text = r.get('text') or ''
        if rtype == 'image' or (r.get('quotes_image') and not _has_money_signal(text)):
            per_row.append({'id': r['id'], 'intent': Intent.RECEIPT, 'sub': 'image', 'flags': {}})
            continue
        if rtype in ('audio', 'voice'):
            cls = classify_text(text, pending_question=pending_question)
            cls['flags']['voice'] = True
            if cls['intent'] == Intent.NOISE and not text:
                cls = {'intent': Intent.FREETEXT, 'sub': 'voice_empty', 'flags': {'voice': True}}
            per_row.append({'id': r['id'], **cls})
            continue
        if rtype not in ('text',):
            per_row.append({'id': r['id'], 'intent': Intent.FREETEXT, 'sub': rtype, 'flags': {}})
            continue
        cls = classify_text(text, quotes_our_question=bool(r.get('quotes_outbound')),
                            pending_question=pending_question or burst_has_money)
        per_row.append({'id': r['id'], **cls})

    primary = None
    for intent in _PRIORITY:
        hit = next((p for p in per_row if p['intent'] == intent), None)
        if hit is not None:
            primary = hit
            break
    if primary is None and burst_has_money:
        primary = {'id': rows[-1]['id'] if rows else None, 'intent': Intent.TRANSFER, 'sub': 'pending', 'flags': {}}
    if primary is None:
        primary = {'id': rows[-1]['id'] if rows else None, 'intent': Intent.NOISE, 'sub': None, 'flags': {}}

    intent = primary['intent']
    if off_hours and intent not in (Intent.BALANCE, Intent.STATEMENT, Intent.NOISE):
        intent = Intent.OFF_HOURS

    secondary = [p for p in per_row
                 if p['id'] != primary['id'] and p['intent'] in (Intent.BALANCE, Intent.STATEMENT, Intent.STATUS, Intent.SOCIAL)
                 and p['intent'] != primary['intent']]
    return {
        'intent': intent,
        'sub': primary.get('sub'),
        'flags': primary.get('flags') or {},
        'primary_id': primary.get('id'),
        'rows': per_row,
        'secondary': secondary,
    }


# ── database wrapper ─────────────────────────────────────────────────────────

def batch_ids_from_input(input_data: Dict[str, Any]) -> List[str]:
    """The message ids of THIS batch, from the «[message_id: …]» markers the channel adds."""
    ids: List[str] = []
    for block in _iter_text_blocks(input_data):
        for m in _MSG_ID_RE.finditer(block):
            if m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


def _iter_text_blocks(input_data):
    if not isinstance(input_data, dict):
        return
    msg = input_data.get('message')
    if isinstance(msg, str):
        yield msg
    content = input_data.get('content')
    if isinstance(content, list):
        for entry in content:
            blocks = entry.get('content') if isinstance(entry, dict) else None
            if isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict) and isinstance(b.get('text'), str):
                        yield b['text']
            elif isinstance(entry, dict) and isinstance(entry.get('text'), str):
                yield entry['text']


def _row_dict(m) -> Dict[str, Any]:
    c = m.content if isinstance(m.content, dict) else {}
    text = c.get('text') or c.get('transcription') or c.get('caption') or ''
    q = getattr(m, 'reply_to', None)
    return {
        'id': str(m.id), 'type': m.type, 'text': str(text),
        'quotes_outbound': bool(q is not None and getattr(q, 'direction', None) == 'outbound'),
        'quotes_image': bool(q is not None and getattr(q, 'type', None) == 'image'),
        'quoted_id': str(q.id) if q is not None else None,
        'created_at': m.created_at,
    }


def load_batch_rows(conversation, input_data: Dict[str, Any]):
    """The rows of this turn, in send order.

    Union of (a) the ids the channel marked in the input («[message_id: …]») and (b) every
    still-unconsumed inbound row of the last window, any type. (b) is what makes the turn
    robust: the state does not always carry every marker (2026-09-06: «حسابي كام» sent 2 s
    after a transfer was missing from the markers and never answered), and a row that was
    not consumed by an earlier turn is by definition still waiting for an answer.
    """
    from django.conf import settings as dj
    from django.utils import timezone
    from modules.chat.models import Message
    ids = set(batch_ids_from_input(input_data))
    cut = timezone.now() - timedelta(minutes=getattr(dj, 'AI_UNPROCESSED_WINDOW_MIN', 6))
    qs = Message.objects_all.filter(conversation=conversation, direction='inbound', active=True).select_related('reply_to')
    rows = {str(m.id): m for m in qs.filter(id__in=ids)} if ids else {}
    for m in qs.filter(ai_consumed_at__isnull=True, created_at__gte=cut).exclude(type__in=('tool', 'tool_call')):
        rows.setdefault(str(m.id), m)
    if not rows:
        rows = {str(m.id): m for m in qs.order_by('-created_at')[:1]}
    out = list(rows.values())
    out.sort(key=lambda m: (getattr(m, 'social_sent_at', None) or m.created_at, m.created_at, str(m.id)))
    return out


def unprocessed_text_rows(conversation):
    """Same authoritative fetch the planner uses: unconsumed inbound text in the window."""
    from django.conf import settings as dj
    from django.utils import timezone
    from django.db.models.functions import Coalesce
    from modules.chat.models import Message
    cut = timezone.now() - timedelta(minutes=getattr(dj, 'AI_UNPROCESSED_WINDOW_MIN', 6))
    return list(
        Message.objects_all
        .filter(conversation=conversation, direction='inbound', active=True, type='text',
                ai_consumed_at__isnull=True, created_at__gte=cut)
        .select_related('reply_to')
        .annotate(_ord=Coalesce('social_sent_at', 'created_at'))
        .order_by('_ord', 'created_at', 'id')
    )


def pending_question_exists(conversation, before) -> bool:
    """Our last outbound (within 15 min, before `before`) was a question quoted on an inbound,
    or a same-day repeat is being held — so a bare «أيوة»/amount now is an answer."""
    from django.utils import timezone
    from modules.chat.models import Message
    try:
        from qurtoba.tools.transactions import _list_repeat_pending
        if _list_repeat_pending(conversation):
            return True
    except Exception:
        pass
    last = (Message.objects_all
            .filter(conversation=conversation, direction='outbound', type='text', active=True,
                    created_at__gte=timezone.now() - timedelta(minutes=15), created_at__lt=before)
            .select_related('reply_to').order_by('-created_at').first())
    if last is None:
        return False
    txt = (last.content or {}).get('text') if isinstance(last.content, dict) else ''
    return bool(txt) and L.is_question(txt) and last.reply_to is not None \
        and getattr(last.reply_to, 'direction', None) == 'inbound'


def route(conversation, partner, input_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The function-node entry: classify this batch against the conversation state."""
    input_data = input_data or {}
    off_hours = bool(input_data.get('off_hours'))
    if partner is not None and getattr(partner, 'qurtoba_customer_id', None) is None:
        return {'intent': 'not_linked', 'sub': None, 'flags': {}, 'primary_id': None,
                'batch_ids': [], 'rows': [], 'secondary': [], 'off_hours': off_hours}

    batch = load_batch_rows(conversation, input_data)
    rows = [_row_dict(m) for m in batch]
    batch_ids = {r['id'] for r in rows}
    older = [m for m in unprocessed_text_rows(conversation) if str(m.id) not in batch_ids]
    burst_has_money = any(_has_money_signal((m.content or {}).get('text') or '') for m in older)
    first_at = batch[0].created_at if batch else None
    pending_q = pending_question_exists(conversation, first_at) if first_at else False

    result = classify_rows(rows, pending_question=pending_q, burst_has_money=burst_has_money,
                           off_hours=off_hours)
    result['batch_ids'] = [r['id'] for r in rows]
    result['off_hours'] = off_hours
    result['quoted_id'] = next((r['quoted_id'] for r in rows if r['id'] == result.get('primary_id')), None)
    # date for the statement intent
    flags = result.get('flags') or {}
    if result['intent'] == Intent.STATEMENT and not flags.get('report_date') and flags.get('yesterday'):
        from django.utils import timezone
        flags['report_date'] = (timezone.localdate() - timedelta(days=1)).isoformat()
    result['report_date'] = flags.get('report_date') or ''
    try:
        from qurtoba.tools._debuglog import log_event
        log_event('route', conversation=conversation, intent=result['intent'], sub=result.get('sub'),
                  batch=[r['id'][:8] for r in rows], pending_q=pending_q or None,
                  burst_money=burst_has_money or None, secondary=[s['intent'] for s in result['secondary']] or None)
    except Exception:
        pass
    return result
