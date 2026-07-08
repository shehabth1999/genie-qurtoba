"""
Qurtoba reporting tools for AI Studio agents.

  * qurtoba_get_customer_daily_transactions — full day's activity for the
    chat's linked customer, grouped into:
      - executed     (cash_sys_done=True OR non-cash and synced)
      - in_flight    (synced but waiting on cash-sys completion)
      - pending_txn  (over-limit, queued in QurtobaPendingTransaction)
      - pending_pay  (سداد, queued in QurtobaPendingPayment)

    Also returns `pretty_ar` — a single ready-to-send Arabic block so the chat
    agent can post it verbatim in ONE outbound message. This avoids the agent
    splitting the report into 4-5 messages.
"""
from datetime import date as date_cls, datetime
from typing import Any, Dict, List, Optional

from modules.aistudio.tools import tool


def _parse_iso_date(value: Optional[str]) -> Optional[date_cls]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        return None


# ── helpers for the pretty_ar block ─────────────────────────────────────────

def _fmt_int(n) -> str:
    """Format an amount with thousands separator, no decimals."""
    try:
        return f'{int(round(float(n))):,}'
    except (TypeError, ValueError):
        return str(n)


def _short_time(t: Optional[str]) -> str:
    """'13:08:23' -> '13:08'. Empty/None -> '—:—'."""
    if not t:
        return '—:—'
    return t[:5]


# Transaction lines are rendered in the order: مبلغ . نوع . رقم . وقت
# (amount . type . number . time), fields separated by " . ".

def _txn_line(idx: int, amount, type_label, account, time, suffix: str = '') -> str:
    parts = [
        _fmt_int(amount),
        str(type_label or ''),
        str(account or '—'),
        _short_time(time),
    ]
    return f'{idx}. ' + ' . '.join(parts) + suffix


def _line_for_executed(idx: int, r: dict) -> str:
    fee = r.get('cash_sys_fee')
    suffix = f' (رسوم {_fmt_int(fee)})' if fee else ''
    return _txn_line(idx, r.get('value', 0), r.get('type', ''),
                     r.get('account_number'), r.get('time'), suffix)


def _line_for_in_flight(idx: int, r: dict) -> str:
    return _txn_line(idx, r.get('value', 0), r.get('type', ''),
                     r.get('account_number'), r.get('time'))


def _line_for_pending_txn(idx: int, p: dict) -> str:
    return _txn_line(idx, p.get('value', 0), p.get('type', ''),
                     p.get('account_number'), p.get('time'), '  (تجاوز الحد)')


def _line_for_pending_pay(idx: int, p: dict) -> str:
    return _txn_line(idx, p.get('value', 0), f'سداد {p.get("type", "")}'.strip(),
                     p.get('account_number'), p.get('time'))


def _build_pretty_ar(
    *,
    customer_name: str,
    report_date_iso: str,
    executed: List[dict],
    in_flight: List[dict],
    pending_txn: List[dict],
    pending_pay: List[dict],
    total_debit: float,
    total_credit: float,
    current_balance: float,
) -> str:
    # Convert YYYY-MM-DD → DD/MM/YYYY for Arabic display.
    try:
        y, m, d = report_date_iso.split('-')
        date_display = f'{int(d):02d}/{int(m):02d}/{y}'
    except Exception:
        date_display = report_date_iso

    lines: List[str] = []
    lines.append(f'كشف حساب اليوم — {date_display}')
    lines.append(customer_name)
    lines.append('')

    if executed:
        lines.append(f'✅ منفذة ({len(executed)}):')
        for i, r in enumerate(executed, 1):
            lines.append(_line_for_executed(i, r))
        lines.append('')

    if in_flight:
        lines.append(f'⏳ قيد التنفيذ ({len(in_flight)}):')
        for i, r in enumerate(in_flight, 1):
            lines.append(_line_for_in_flight(i, r))
        lines.append('')

    if pending_txn:
        lines.append(f'📋 قيد المراجعة — تحويلات ({len(pending_txn)}):')
        for i, p in enumerate(pending_txn, 1):
            lines.append(_line_for_pending_txn(i, p))
        lines.append('')

    if pending_pay:
        lines.append(f'📋 قيد المراجعة — سدادات ({len(pending_pay)}):')
        for i, p in enumerate(pending_pay, 1):
            lines.append(_line_for_pending_pay(i, p))
        lines.append('')

    if not (executed or in_flight or pending_txn or pending_pay):
        lines.append('لا توجد عمليات اليوم.')
        lines.append('')

    lines.append(f'💸 إجمالي التحويلات: {_fmt_int(total_debit)} جنيه')
    if total_credit:
        lines.append(f'💵 إجمالي السداد: {_fmt_int(total_credit)} جنيه')
    lines.append(f'🏦 الرصيد الحالي: {_fmt_int(current_balance)} جنيه')

    return '\n'.join(lines).rstrip()


# ── tool ────────────────────────────────────────────────────────────────────

@tool(
    name='qurtoba_get_customer_daily_transactions',
    display_name='Get Customer Qurtoba Transactions for a Day',
    description=(
        'Use this tool when the partner asks for a daily transactions report '
        '(كشف حساب اليوم / تقرير اليوم / حركات اليوم / كل تحويلات اليوم / تفصيلي). '
        'It returns every Qurtoba record AND every pending-review row dated to '
        'the requested day for the customer linked to this chat. '
        'INPUTS: '
        '1) report_date (optional) — ISO date YYYY-MM-DD. Omit/empty = today. '
        'OUTPUT: '
        '- Structured fields: transactions[], pending_transactions[], pending_payments[], '
        'totals, current_balance, grade_limit. '
        '- **pretty_ar**: a single ready-to-send Arabic block already grouped into '
        '"منفذة / قيد التنفيذ / قيد المراجعة" with per-line time, type, amount, account, '
        'and fee. Each line includes a HH:MM timestamp from the record. '
        'HOW TO USE: '
        '- "Show me everything" ask (كشف حساب / تقرير اليوم / كل تحويلات اليوم) → send '
        'pretty_ar AS THE WHOLE REPLY in ONE outbound message — do not split, do not add '
        'prologue/epilogue, do not invent extra notes. The grouping, totals, and timestamps '
        'are already in the string. '
        '- A FILTERED ask about a SUBSET (e.g. "which ones didn\'t go through" / "التحويلات '
        'اللي متمتش" / "how many are still pending" / "which already executed" / "show me '
        'only the كاش ones") → do NOT paste pretty_ar (it shows EVERYTHING, not the subset '
        'asked for). Instead read `transactions[]` yourself — each item has `bucket`: '
        '"executed" (done) or "in_flight" (NOT done yet — this is "لم تتم"/"متمتش"). Filter '
        'to exactly what was asked (ALL matching items, not just the most recent few) and '
        'compose your OWN short natural-language reply listing them. '
        '- Do NOT use qurtoba_check_transaction_status for a "which/how many are still '
        'pending" question — with no source_message_id it only returns the LATEST 3 records '
        'of today regardless of status (not filtered by pending/executed at all), so it will '
        'give an incomplete or wrong answer whenever there are more than 3 relevant records. '
        '- Do NOT claim "the system has no timestamps" — every record has a time and it '
        'is already rendered into pretty_ar. '
        'Do NOT use this tool for a different customer or for multi-day periods.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=60,
)
def qurtoba_get_customer_daily_transactions(
    context,
    report_date: Optional[str] = None,
) -> Dict[str, Any]:
    conv = getattr(context, 'conversation', None)
    partner = getattr(context, 'partner', None)
    if partner is None and conv is not None:
        partner = getattr(conv, 'social_partner', None)

    if partner is None:
        return {
            'success': False,
            'error': 'No active conversation/partner in context.',
            'error_type': 'no_conversation',
        }

    customer = getattr(partner, 'qurtoba_customer', None)
    if customer is None:
        return {
            'success': False,
            'error': 'The current chat partner is not linked to any Qurtoba customer.',
            'error_type': 'partner_not_linked',
        }

    parsed_date = _parse_iso_date(report_date)
    if report_date and parsed_date is None:
        return {
            'success': False,
            'error': f"Invalid report_date '{report_date}'. Expected ISO format YYYY-MM-DD.",
            'error_type': 'invalid_date',
        }

    from django.utils import timezone
    target_date = parsed_date or timezone.localdate()

    from qurtoba.models import (
        QurtobaRecord,
        QurtobaPendingTransaction,
        QurtobaPendingPayment,
    )

    qs = (
        QurtobaRecord.objects
        .filter(customer=customer, date=target_date)
        .order_by('time', 'id')
    )

    total_debit = 0.0
    total_credit = 0.0
    transactions: List[dict] = []
    executed: List[dict] = []
    in_flight: List[dict] = []

    _CASH_TYPES = {'كاش', 'كاش(5)', 'كاش(10)', 'كاش(20)'}

    for r in qs:
        amount = float(r.value or 0)
        if r.is_down:
            total_credit += amount
        else:
            total_debit += amount

        # Bucket the record:
        #   - cash type → in_flight unless cash_sys_done=True
        #   - non-cash  → executed if qurtoba_synced (Qurtoba has it), else in_flight
        is_cash = r.type in _CASH_TYPES
        if is_cash:
            bucket = 'executed' if r.cash_sys_done else 'in_flight'
        else:
            bucket = 'executed' if r.qurtoba_synced else 'in_flight'

        row = {
            'record_id': r.pk,
            'type': r.type,
            'value': amount,
            'is_payment': bool(r.is_down),
            'is_seller_collection': bool(r.is_seller),
            'is_done': bool(r.is_done),
            'account_number': r.account_number,
            'time': r.time.strftime('%H:%M:%S') if r.time else None,
            'notes': r.notes,
            'qurtoba_synced': bool(r.qurtoba_synced),
            'qurtoba_record_id': r.qurtoba_record_id,
            'cash_sys_done': bool(r.cash_sys_done),
            'cash_sys_fee': r.cash_sys_fee,
            'bucket': bucket,
        }
        transactions.append(row)
        (executed if bucket == 'executed' else in_flight).append(row)

    # Pending review queues — only those *created* today (use created_at date).
    p_txn_qs = (
        QurtobaPendingTransaction.objects
        .filter(customer=customer, created_at__date=target_date, review_state='pending')
        .order_by('created_at', 'id')
    )
    pending_transactions = []
    for p in p_txn_qs:
        pending_transactions.append({
            'pending_id':     p.pk,
            'type':           p.type,
            'value':          float(p.value or 0),
            'account_number': p.account_number,
            'reason':         p.reason,
            'time':           p.created_at.strftime('%H:%M:%S'),
        })

    p_pay_qs = (
        QurtobaPendingPayment.objects
        .filter(customer=customer, created_at__date=target_date, review_state='pending')
        .order_by('created_at', 'id')
    )
    pending_payments = []
    for p in p_pay_qs:
        pending_payments.append({
            'pending_id':     p.pk,
            'type':           p.type,
            'value':          float(p.value or 0),
            'account_number': p.account_number,
            'time':           p.created_at.strftime('%H:%M:%S'),
        })

    customer.refresh_from_db(fields=['balance'])
    grade_limit = (customer.grade or 0) * 1000

    pretty_ar = _build_pretty_ar(
        customer_name=customer.name,
        report_date_iso=target_date.isoformat(),
        executed=executed,
        in_flight=in_flight,
        pending_txn=pending_transactions,
        pending_pay=pending_payments,
        total_debit=total_debit,
        total_credit=total_credit,
        current_balance=customer.balance or 0,
    )

    return {
        'success': True,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'report_date': target_date.isoformat(),
        'transaction_count': len(transactions),
        'executed_count': len(executed),
        'in_flight_count': len(in_flight),
        'pending_transactions_count': len(pending_transactions),
        'pending_payments_count': len(pending_payments),
        'totals': {
            'total_debit': total_debit,
            'total_credit': total_credit,
            'net_change': total_debit - total_credit,
        },
        'current_balance': customer.balance or 0,
        'grade_limit': grade_limit,
        'transactions': transactions,
        'pending_transactions': pending_transactions,
        'pending_payments': pending_payments,
        'pretty_ar': pretty_ar,
    }
