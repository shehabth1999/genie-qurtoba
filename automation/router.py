"""Turn loading and the ONLY routing Python does: is this a receipt image, is the
off-hours switch on, is the partner linked. Everything else goes down the money path
first (``transfers.run``) and then — if anything is left — to the AI.

Python never interprets what the customer means (owner decision 2026-09-06): a
greeting, a balance question, a cancellation, a complaint are all the AI's to read,
after the clean transfers have already been created.
"""
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional


class Intent:
    RECEIPT = 'receipt'
    TRANSFER = 'transfer'
    OFF_HOURS = 'off_hours'
    NOT_LINKED = 'not_linked'


_MSG_ID_RE = re.compile(r'\[message_id:\s*([0-9a-fA-F-]{36})\]')
_PUNCT_ONLY_RE = re.compile(r'^[\s.,؟?!،…]+$')


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
    # «؟» / «.» quoted on one of the customer's own messages = «you ignored this — again»
    if q is not None and getattr(q, 'direction', None) == 'inbound' and _PUNCT_ONLY_RE.match(str(text or '')):
        qc = q.content if isinstance(q.content, dict) else {}
        text = qc.get('text') or qc.get('transcription') or text
    return {
        'id': str(m.id), 'type': m.type, 'text': str(text),
        'quotes_outbound': bool(q is not None and getattr(q, 'direction', None) == 'outbound'),
        'quotes_image': bool(q is not None and getattr(q, 'type', None) == 'image'),
        'quoted_id': str(q.id) if q is not None else None,
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


def route(conversation, partner, input_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The function-node entry: linked? off-hours? receipt image? — else the money path."""
    input_data = input_data or {}
    off_hours = bool(input_data.get('off_hours'))
    base = {'intent': Intent.TRANSFER, 'batch_ids': [], 'rows': [], 'off_hours': off_hours, 'quoted_id': None}
    if partner is not None and getattr(partner, 'qurtoba_customer_id', None) is None:
        return {**base, 'intent': Intent.NOT_LINKED}

    batch = load_batch_rows(conversation, input_data)
    rows = [_row_dict(m) for m in batch]
    base['batch_ids'] = [r['id'] for r in rows]
    base['rows'] = rows
    base['quoted_id'] = rows[-1]['quoted_id'] if rows else None
    from qurtoba.tools.planning import _classify_message
    has_image = any(r['type'] == 'image' or (r['quotes_image'] and not _classify_message(r['text'])['phones'])
                    for r in rows)
    if has_image:
        base['intent'] = Intent.RECEIPT
    elif off_hours:
        base['intent'] = Intent.OFF_HOURS
    try:
        from qurtoba.tools._debuglog import log_event
        log_event('route', conversation=conversation, intent=base['intent'], batch=[r['id'][:8] for r in rows])
    except Exception:
        pass
    return base
