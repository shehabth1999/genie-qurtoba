"""Deterministic handlers for the non-money intents (SHARED ROLES, in code)."""
from typing import Any, Dict, Optional

from . import lexicon as L
from . import replies as R
from .context import alert_human, asked_recently, call_tool, consume, log, said_recently, send_quoted


def _primary(route: Dict[str, Any]) -> Optional[str]:
    return route.get('primary_id')


def _batch_texts(conversation, route: Dict[str, Any]) -> Dict[str, str]:
    from modules.chat.models import Message
    out = {}
    for m in Message.objects_all.filter(conversation=conversation, id__in=route.get('batch_ids') or []):
        c = m.content if isinstance(m.content, dict) else {}
        out[str(m.id)] = str(c.get('text') or c.get('transcription') or '')
    return out


# ── balance ──────────────────────────────────────────────────────────────────

def balance(conversation, partner, route: Dict[str, Any]) -> str:
    """«Call qurtoba_send_customer_balance_to_chat FRESH, then output ZERO characters.»"""
    from qurtoba.tools.conversation import qurtoba_send_customer_balance_to_chat
    res = call_tool(conversation, partner, qurtoba_send_customer_balance_to_chat)
    if not res.get('success'):
        alert_human(conversation, partner, f'فشل إرسال الرصيد تلقائياً: {res.get("error")}')
    consume(conversation, route.get('batch_ids') or [])
    return ''


# ── daily statement ──────────────────────────────────────────────────────────

def statement(conversation, partner, route: Dict[str, Any]) -> str:
    """«Call qurtoba_get_customer_daily_transactions (omit send_report) — the TOOL posts it.»"""
    from qurtoba.tools.reports import qurtoba_get_customer_daily_transactions
    kwargs: Dict[str, Any] = {}
    if route.get('report_date'):
        kwargs['report_date'] = route['report_date']
    res = call_tool(conversation, partner, qurtoba_get_customer_daily_transactions, **kwargs)
    if not res.get('success') or res.get('report_sent') is False:
        send_quoted(conversation, _primary(route), R.STATEMENT_FAILED)
    consume(conversation, route.get('batch_ids') or [])
    return ''


# ── status ───────────────────────────────────────────────────────────────────

def status(conversation, partner, route: Dict[str, Any]) -> str:
    """«تم؟» → check_transaction_status (quoted number message → its id) and copy pretty_ar;
    a SUBSET ask → the daily tool with send_report=false and a short list of what is in flight;
    «الإيصال اتقبل؟» → check_payment_status."""
    from qurtoba.tools.transactions import qurtoba_check_transaction_status, qurtoba_check_payment_status
    from qurtoba.tools.reports import qurtoba_get_customer_daily_transactions
    from qurtoba.tools.planning import _classify_message
    primary = _primary(route)
    texts = _batch_texts(conversation, route)
    text = texts.get(primary, '')
    t = L.norm(text)

    if route.get('sub') == 'subset' or L.STATUS_SUBSET.search(t):
        res = call_tool(conversation, partner, qurtoba_get_customer_daily_transactions, send_report=False)
        pending = [x for x in (res.get('transactions') or []) if x.get('bucket') == 'in_flight']
        if not res.get('success'):
            send_quoted(conversation, primary, R.STATEMENT_FAILED)
        elif not pending:
            send_quoted(conversation, primary, R.SUBSET_NONE_PENDING)
        else:
            lines = [R.SUBSET_HEADER] + [f"{x.get('account_number') or ''} — {R._fmt(x.get('value'))} ⏳" for x in pending[:15]]
            send_quoted(conversation, primary, '\n'.join(lines))
        consume(conversation, route.get('batch_ids') or [])
        return ''

    quoted_id = route.get('quoted_id')
    quoted_is_image = False
    quoted_has_phone = False
    if quoted_id:
        from modules.chat.models import Message
        q = Message.objects_all.filter(conversation=conversation, id=quoted_id).first()
        if q is not None:
            quoted_is_image = q.type == 'image'
            qc = q.content if isinstance(q.content, dict) else {}
            quoted_has_phone = bool(_classify_message(str(qc.get('text') or ''))['phones'])
    if quoted_is_image or L.PAYMENT.search(t):
        res = call_tool(conversation, partner, qurtoba_check_payment_status,
                        source_message_id=quoted_id if quoted_is_image else None)
    else:
        kwargs = {'source_message_id': quoted_id} if quoted_has_phone else {}
        res = call_tool(conversation, partner, qurtoba_check_transaction_status, **kwargs)
    line = (res.get('pretty_ar') or '').strip() if res.get('success') else ''
    if not line:
        line = R.STATUS_NOTHING if res.get('success') else R.WAIT
        if not res.get('success'):
            alert_human(conversation, partner, f'فشل فحص الحالة تلقائياً: {res.get("error")}')
    send_quoted(conversation, primary, line)
    consume(conversation, route.get('batch_ids') or [])
    return ''


# ── cancellation ─────────────────────────────────────────────────────────────

def cancel(conversation, partner, route: Dict[str, Any]) -> str:
    """«GENERAL cancel of a not-yet-created burst → qurtoba_clear_pending_transfers (posts itself).
    Already created (👍 sent) → alert_qurtoba_human + «لحظة».»"""
    from qurtoba.tools.conversation import qurtoba_clear_pending_transfers
    from qurtoba.tools.planning import _classify_message
    from .router import unprocessed_text_rows
    primary = _primary(route)
    batch_ids = set(route.get('batch_ids') or [])
    pending_rows = [m for m in unprocessed_text_rows(conversation) if str(m.id) not in batch_ids]
    money_pending = any(_classify_message(str((m.content or {}).get('text') or ''))['phones']
                        or _classify_message(str((m.content or {}).get('text') or ''))['amounts']
                        for m in pending_rows)
    texts = _batch_texts(conversation, route)
    if money_pending:
        res = call_tool(conversation, partner, qurtoba_clear_pending_transfers)
        if not res.get('reply_fully_handled'):
            send_quoted(conversation, primary, R.CANCEL_STOPPED)
    else:
        note = 'العميل يطلب إلغاء: «' + ' | '.join(texts.values())[:200] + f'» [message_id: {primary}]'
        alert_human(conversation, partner, note)
        send_quoted(conversation, primary, R.WAIT)
    consume(conversation, batch_ids)
    return ''


# ── courtesy / availability / scope ──────────────────────────────────────────

def social(conversation, partner, route: Dict[str, Any]) -> str:
    primary = _primary(route)
    sub = route.get('sub')
    seed = str(primary or '')
    if sub == 'thanks':
        line = R.pick(R.THANKS, seed)
    elif sub == 'wellbeing':
        line = R.pick(R.WELLBEING, seed)
    elif sub == 'availability':
        line = R.pick(R.AVAILABLE, seed)
    elif sub == 'morning':
        line = R.pick(R.MORNING, seed)
    elif sub == 'evening':
        line = R.pick(R.EVENING, seed)
    else:
        line = R.pick(R.GREETINGS, seed)
    send_quoted(conversation, primary, line)
    consume(conversation, route.get('batch_ids') or [])
    return ''


def off_hours(conversation, partner, route: Dict[str, Any]) -> str:
    primary = _primary(route)
    if not asked_recently(conversation, primary) and not said_recently(conversation, primary, R.OFF_HOURS, minutes=120):
        send_quoted(conversation, primary, R.OFF_HOURS)
    consume(conversation, route.get('batch_ids') or [])
    return ''


def noise(conversation, partner, route: Dict[str, Any]) -> str:
    consume(conversation, route.get('batch_ids') or [])
    return ''


# ── secondary intents riding next to the primary one ─────────────────────────

def run_secondary(conversation, partner, route: Dict[str, Any]) -> None:
    for s in route.get('secondary') or []:
        sub_route = {**route, 'intent': s['intent'], 'sub': s.get('sub'), 'flags': s.get('flags') or {},
                     'primary_id': s['id'], 'batch_ids': [s['id']], 'secondary': [],
                     'report_date': (s.get('flags') or {}).get('report_date') or '', 'quoted_id': None}
        try:
            HANDLERS[s['intent']](conversation, partner, sub_route)
        except Exception:
            log('secondary_error', conversation, intent=s['intent'])


# ── context for the small model ──────────────────────────────────────────────

def freetext_context(conversation, partner, route: Dict[str, Any]) -> Dict[str, Any]:
    """The few facts the free-text model may need; never the balance (grade privacy)."""
    from django.utils import timezone
    customer = getattr(partner, 'qurtoba_customer', None)
    texts = _batch_texts(conversation, route)
    return {
        'now': timezone.localtime().strftime('%Y-%m-%d %H:%M (%A)'),
        'partner_name': getattr(partner, 'name', '') or '',
        'customer_name': getattr(customer, 'name', '') or '',
        'accounts': (getattr(customer, 'accounts_pretty', '') or '') if customer is not None else '',
        'messages': '\n'.join(f'[message_id: {mid}] {txt}' for mid, txt in texts.items()),
        'primary_id': route.get('primary_id') or '',
    }


HANDLERS = {
    'balance': balance,
    'statement': statement,
    'status': status,
    'cancel': cancel,
    'social': social,
    'off_hours': off_hours,
    'noise': noise,
}
