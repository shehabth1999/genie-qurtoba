"""
Qurtoba reporting tools for AI Studio agents.

  * qurtoba_get_customer_daily_transactions — full day's activity for the
    chat's linked customer, grouped into:
      - executed     (cash_sys_done=True OR non-cash and synced)
      - in_flight    (synced but waiting on cash-sys completion)
      - pending_txn  (over-limit, queued in QurtobaPendingTransaction)
      - pending_pay  (سداد, queued in QurtobaPendingPayment)

    One QurtobaCustomer can be reached on SEVERAL WhatsApp numbers
    (base.Partner.qurtoba_customer is many-partners → one-customer), so the
    day's rows are sectioned by the phone that requested them — the asking
    phone first, then the other phones, then «بواسطة قرطبة» for rows that carry
    no partner (entered by an accountant/collector inside Qurtoba and synced
    in). Attribution comes from the `partner` FK that the create path already
    stamps on every chat-born record and pending row.

    DELIVERY: this tool POSTS the statement itself — one WhatsApp message per
    phone section, sub-chunked so no message can reach WhatsApp's 4096-char
    limit — and the agent must then output ZERO characters, exactly like
    qurtoba_send_customer_balance_to_chat. Call it with send_report=False to
    read the data WITHOUT posting (for a filtered/subset question the agent
    answers in its own words).

    Amounts of zero or less are never shown to the customer and never counted
    into the displayed totals; `hidden_nonpositive_count` reports how many were
    withheld so nothing is silently dropped.
"""
import logging
import re
import time
from datetime import date as date_cls, datetime
from typing import Any, Dict, List, Optional, Tuple

from modules.aistudio.tools import tool

from qurtoba.tools._phone import _normalize_phone

logger = logging.getLogger(__name__)

_XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


# WhatsApp rejects a text body over 4096 chars. Messages are built to a lower
# budget so a chunk can never land on the boundary — the remainder covers the
# monospace fences, the section heading repeated on a continuation, and the
# multi-byte Arabic the limit is actually counted in.
_WHATSAPP_TEXT_LIMIT = 4096
_CHUNK_BUDGET = 3200


def _parse_iso_date(value: Optional[str]) -> Optional[date_cls]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        return None


# ── helpers for the rendered block ──────────────────────────────────────────

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


# ── column alignment ────────────────────────────────────────────────────────
#
# Transaction lines keep the original field order — مبلغ . نوع . رقم . وقت —
# but every column is padded to the widest cell in its section, so the dots
# line up down the block no matter that «كاش» is 3 chars and «كاش(20)» is 7,
# or that one amount is 1,000 and the next is 100,000. Amounts are RIGHT
# aligned (digits read by magnitude), text columns are LEFT aligned.
#
# Padding only renders as columns in a fixed-width font, so the lines go inside
# a WhatsApp monospace block — see _fence(). The bucket headings stay outside
# it so their emoji keep their normal size.

_COL_SEP = ' . '


def _cells(idx: int, amount, type_label, account, time, suffix: str = '') -> Tuple[str, ...]:
    """One row as raw, unpadded cells: (idx, amount, type, account, time, suffix)."""
    return (
        f'{idx}.',
        _fmt_int(amount),
        str(type_label or ''),
        str(account or '—'),
        _short_time(time),
        suffix,
    )


def _cells_for_executed(idx: int, r: dict) -> Tuple[str, ...]:
    fee = r.get('cash_sys_fee')
    suffix = f' (رسوم {_fmt_int(fee)})' if fee else ''
    return _cells(idx, r.get('value', 0), r.get('type', ''),
                  r.get('account_number'), r.get('time'), suffix)


def _cells_for_in_flight(idx: int, r: dict) -> Tuple[str, ...]:
    return _cells(idx, r.get('value', 0), r.get('type', ''),
                  r.get('account_number'), r.get('time'))


def _cells_for_pending_txn(idx: int, p: dict) -> Tuple[str, ...]:
    return _cells(idx, p.get('value', 0), p.get('type', ''),
                  p.get('account_number'), p.get('time'), '  (تجاوز الحد)')


def _cells_for_pending_pay(idx: int, p: dict) -> Tuple[str, ...]:
    return _cells(idx, p.get('value', 0), f'سداد {p.get("type", "")}'.strip(),
                  p.get('account_number'), p.get('time'))


def _column_widths(rows: List[Tuple[str, ...]]) -> Tuple[int, ...]:
    """Widest cell per column across every row that shares one alignment scope."""
    if not rows:
        return (0, 0, 0, 0, 0)
    return tuple(max(len(r[i]) for r in rows) for i in range(5))


def _render_row(cells: Tuple[str, ...], widths: Tuple[int, ...]) -> str:
    idx, amount, type_label, account, time, suffix = cells
    w_idx, w_amt, w_typ, w_acc, _w_time = widths
    return (
        f'{idx:>{w_idx}} '
        f'{amount:>{w_amt}}{_COL_SEP}'
        f'{type_label:<{w_typ}}{_COL_SEP}'
        f'{account:<{w_acc}}{_COL_SEP}'
        f'{time}'
        f'{suffix}'
    ).rstrip()


def _fence(lines: List[str]) -> List[str]:
    """Wrap tabular lines in a WhatsApp monospace block so the padding aligns."""
    return ['```'] + lines + ['```']


# ── per-phone grouping ──────────────────────────────────────────────────────

# Rows with partner=NULL were not requested by any chat number — an accountant
# or collector entered them in Qurtoba and the sync brought them across. They
# are real transactions on the customer's account, so they are shown, in their
# own trailing section rather than attributed to whoever happens to be asking.
_SYSTEM_GROUP_LABEL = 'بواسطة قرطبة'

# Marks the asking phone's own section so the customer can find it at a glance.
_SELF_MARKER = ' (رقمك)'

# Repeated heading when one phone's rows need more than one message.
_CONT_MARKER = ' — تابع'


def _group_label(partner) -> str:
    """Section heading for a partner: local phone → display name → placeholder.

    A WhatsApp partner can legitimately have no phone (reached by username and
    addressed by bsuid), hence the two fallbacks.
    """
    if partner is None:
        return _SYSTEM_GROUP_LABEL
    phone = _normalize_phone(getattr(partner, 'phone', None))
    if phone:
        return phone
    name = (getattr(partner, 'name', '') or '').strip()
    return name or 'رقم غير معروف'


def _new_group(partner, *, is_self: bool, order: int) -> dict:
    return {
        'partner_id': getattr(partner, 'pk', None) if partner is not None else None,
        'phone': _normalize_phone(getattr(partner, 'phone', None)) if partner is not None else None,
        'label': _group_label(partner),
        'is_self': is_self,
        'executed': [],
        'in_flight': [],
        'pending_transactions': [],
        'pending_payments': [],
        'cancelled': [],           # reversed by Cash-SYS (value 0 now) — shown, never counted
        'totals': {'debit': 0.0, 'credit': 0.0},
        '_order': order,
    }


def _group_has_rows(g: dict) -> bool:
    return bool(g['executed'] or g['in_flight']
                or g['pending_transactions'] or g['pending_payments'])


def _sort_groups(groups: Dict[Any, dict]) -> List[dict]:
    """Asking phone first, then the other phones by first activity, system last."""
    def key(g):
        if g['is_self']:
            return (0, 0)
        if g['partner_id'] is None:
            return (2, 0)
        return (1, g['_order'])
    return sorted(groups.values(), key=key)


# ── message building ────────────────────────────────────────────────────────

def _section_blocks(g: dict) -> List[Tuple[str, List[Tuple[str, ...]]]]:
    """The four buckets of one section as (heading, rows-as-cells) pairs.

    Rows are returned unpadded; widths are applied later across the whole
    section so every bucket in it shares one set of columns.
    """
    blocks: List[Tuple[str, List[Tuple[str, ...]]]] = []
    if g['executed']:
        blocks.append((f'✅ منفذة ({len(g["executed"])}):',
                       [_cells_for_executed(i, r) for i, r in enumerate(g['executed'], 1)]))
    if g['in_flight']:
        blocks.append((f'⏳ قيد التنفيذ ({len(g["in_flight"])}):',
                       [_cells_for_in_flight(i, r) for i, r in enumerate(g['in_flight'], 1)]))
    if g['pending_transactions']:
        blocks.append((f'📋 قيد المراجعة — تحويلات ({len(g["pending_transactions"])}):',
                       [_cells_for_pending_txn(i, p) for i, p in enumerate(g['pending_transactions'], 1)]))
    if g['pending_payments']:
        blocks.append((f'📋 قيد المراجعة — سدادات ({len(g["pending_payments"])}):',
                       [_cells_for_pending_pay(i, p) for i, p in enumerate(g['pending_payments'], 1)]))
    return blocks


def _section_subtotal_lines(g: dict) -> List[str]:
    is_system = g['partner_id'] is None
    debit = g['totals']['debit']
    credit = g['totals']['credit']
    out = []
    if is_system:
        out.append(f'إجمالي التحويلات: {_fmt_int(debit)} جنيه')
        if credit:
            out.append(f'إجمالي السداد: {_fmt_int(credit)} جنيه')
    else:
        out.append(f'إجمالي تحويلات الرقم: {_fmt_int(debit)} جنيه')
        if credit:
            out.append(f'إجمالي سداد الرقم: {_fmt_int(credit)} جنيه')
    return out


def _section_heading(g: dict, *, show_heading: bool) -> Optional[str]:
    if not show_heading:
        return None
    heading = ('🏢 ' if g['partner_id'] is None else '📱 ') + g['label']
    if g['is_self']:
        heading += _SELF_MARKER
    return heading


def _render_section_messages(g: dict, *, show_heading: bool, budget: int) -> List[str]:
    """One phone's section as one message — or several, if it is too long.

    Column widths are computed once over the WHOLE section, so a section that
    spills into a second message keeps identical columns across both.
    """
    heading = _section_heading(g, show_heading=show_heading)
    blocks = _section_blocks(g)
    widths = _column_widths([row for _h, rows in blocks for row in rows])
    # With headings collapsed there is only one section, so its subtotal would
    # just restate the grand total in the footer two lines below it.
    subtotal = _section_subtotal_lines(g) if (blocks and show_heading) else []

    messages: List[str] = []
    cur: List[str] = []

    def head_lines(continued: bool) -> List[str]:
        if heading is None:
            return []
        return [heading + (_CONT_MARKER if continued else ''), '']

    def flush():
        if cur:
            messages.append('\n'.join(cur).rstrip())
            cur.clear()

    cur.extend(head_lines(False))

    if not blocks:
        # Only reached for the asking phone on a day it did nothing.
        cur.append('لا توجد عمليات من رقمك اليوم.')
        flush()
        return messages

    for heading_line, rows in blocks:
        # Open the bucket, then add rows one at a time so an oversized bucket
        # splits at a row boundary instead of overflowing.
        pending: List[str] = []

        def emit():
            """Move the buffered rows into the current message."""
            if not pending:
                return
            cur.append(heading_line)
            cur.extend(_fence(pending))
            cur.append('')
            pending.clear()

        for cells in rows:
            line = _render_row(cells, widths)
            # +8 covers the two fence lines and the newlines this row adds.
            projected = len('\n'.join(cur)) + sum(len(x) + 1 for x in pending) + len(line) + len(heading_line) + 8
            if pending and projected > budget:
                emit()
                flush()
                cur.extend(head_lines(True))
            pending.append(line)
        emit()

    for line in subtotal:
        if len('\n'.join(cur)) + len(line) + 1 > budget:
            flush()
            cur.extend(head_lines(True))
        cur.append(line)

    flush()
    return messages


def _build_messages(
    *,
    customer_name: str,
    report_date_iso: str,
    groups: List[dict],
    total_debit: float,
    total_credit: float,
    current_balance: float,
    budget: int = _CHUNK_BUDGET,
) -> List[str]:
    """The whole statement as the exact list of WhatsApp messages to send.

    One message per phone section (more only when a single section is too long
    for one message). The report header rides on the first message and the
    grand totals on the last, so no bubble is spent on either.
    """
    try:
        y, m, d = report_date_iso.split('-')
        date_display = f'{int(d):02d}/{int(m):02d}/{y}'
    except Exception:
        date_display = report_date_iso

    header = f'كشف حساب اليوم — {date_display}\n{customer_name}'

    footer_lines = [f'💸 إجمالي التحويلات: {_fmt_int(total_debit)} جنيه']
    if total_credit:
        footer_lines.append(f'💵 إجمالي السداد: {_fmt_int(total_credit)} جنيه')
    footer_lines.append(f'🏦 الرصيد الحالي: {_fmt_int(current_balance)} جنيه')
    footer = '\n'.join(footer_lines)

    populated = [g for g in groups if _group_has_rows(g)]
    # With at most one populated section the per-phone headings carry no
    # information, so they are dropped and the message reads exactly as the
    # flat statement always has.
    show_headings = len(populated) > 1

    messages: List[str] = []
    if not populated:
        messages.append('لا توجد عمليات اليوم.')
    else:
        for g in groups:
            if not _group_has_rows(g) and not (g['is_self'] and show_headings):
                continue
            messages.extend(_render_section_messages(
                g, show_heading=show_headings, budget=budget))

    messages = [m for m in messages if m.strip()]

    # Header onto the first message, footer onto the last — each moved to its
    # own bubble only if it would push that message over the budget.
    if messages and len(messages[0]) + len(header) + 2 <= budget:
        messages[0] = header + '\n\n' + messages[0]
    else:
        messages.insert(0, header)

    if messages and len(messages[-1]) + len(footer) + 2 <= budget:
        messages[-1] = messages[-1] + '\n\n' + footer
    else:
        messages.append(footer)

    return messages


def _public_group(g: dict) -> dict:
    """Strip the private ordering key before returning the group to the agent."""
    return {k: v for k, v in g.items() if not k.startswith('_')}


def _send_messages(conversation, messages: List[str]) -> Tuple[int, Optional[str]]:
    """Post each message to the chat, in order. Returns (sent_count, error).

    Sequential on purpose: WhatsApp does not guarantee delivery order across
    concurrent sends to the same recipient, and a statement read out of order
    is worse than a slow one.
    """
    from modules.chat.services.omnichannel_send_service import OmnichannelSendService
    from qurtoba.ai_guard import system_send
    from qurtoba.extensions import _get_system_partner

    system_partner = _get_system_partner(conversation)
    service = OmnichannelSendService()
    sent = 0
    for text in messages:
        try:
            with system_send():
                service.send_and_broadcast(
                    partner=conversation.social_partner,
                    content={'text': text},
                    message_type='text',
                    conversation=conversation,
                    system_partner=system_partner,
                    websocket=True,
                )
        except Exception as e:  # noqa: BLE001 — reported back, never raised into the agent
            return sent, str(e)
        sent += 1
    return sent, None


# ── Excel statement ─────────────────────────────────────────────────────────
#
# The text statement is a code-fenced monospace table; at 48 rows it is
# unreadable on a phone (customer report, 2026-08-29). The customer asked for
# ONE Excel file instead, so the statement is delivered as an .xlsx document
# with a three-line summary caption. The text form stays as the fallback when
# the file cannot be built or delivered, so a statement is never lost.

def _date_display(report_date_iso: str) -> str:
    try:
        y, m, d = report_date_iso.split('-')
        return f'{int(d):02d}/{int(m):02d}/{y}'
    except Exception:
        return report_date_iso


def _date_display_ar(report_date_iso: str) -> str:
    """«الثلاثاء 8 سبتمبر (08/09/2026)» — the day name the office uses, plus the numeric date."""
    try:
        from qurtoba.services.daily_totals import fmt_day_ar
        y, m, d = (int(x) for x in report_date_iso.split('-'))
        return f'{fmt_day_ar(date_cls(y, m, d))} ({_date_display(report_date_iso)})'
    except Exception:
        return _date_display(report_date_iso)


_CANCEL_REASON_AR = {
    'no_wallet': 'الرقم مش عليه محفظة',
    'limit': 'الرقم تجاوز الحد',
    'daily_limit': 'الرقم تجاوز الحد اليومي',
    'monthly_limit': 'الرقم تجاوز الحد الشهري',
    'cancel_request': 'ألغي بناءً على طلبك',
    'agent': 'ألغي بواسطة المكتب',
    'office': 'ألغي بواسطة المكتب',
}
_AUTO_NOTE_RE = re.compile(r'^\[auto\]\s*|\s*لعملية\s*#\d+')


def _clean_note(text) -> str:
    """«[auto] مصاريف خدمة لعملية #37144» → «مصاريف خدمة»: no internal ids in a customer file."""
    return _AUTO_NOTE_RE.sub('', str(text or '')).strip()


def _balance_phrase(current_balance: float) -> str:
    """Mirror check_balance_and_send(): direction word first, absolute amount."""
    bal = float(current_balance or 0)
    if bal > 0:
        return f'عليك {_fmt_int(abs(bal))} جنيه'
    if bal < 0:
        return f'ليك {_fmt_int(abs(bal))} جنيه'
    return 'مفيش مديونية'


def _build_statement_xlsx(
    customer_name: str,
    report_date_iso: str,
    groups: List[dict],
    total_debit: float,
    total_credit: float,
    current_balance: float,
    *,
    generated_at: Optional[str] = None,
) -> bytes:
    """The whole day as one right-to-left worksheet — every number on the account and what
    the office entered («بواسطة قرطبة»), one section each with its own subtotal, then the
    day's totals and the balance. Same rows and buckets the text form prints."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = 'كشف الحساب'
    ws.sheet_view.rightToLeft = True

    bold = Font(bold=True)
    white_bold = Font(bold=True, color='FFFFFF')
    title_font = Font(bold=True, size=15, color='1F3864')
    sub_font = Font(size=11, color='595959')
    head_fill = PatternFill('solid', fgColor='1F3864')
    section_fill = PatternFill('solid', fgColor='D9E1F2')
    subtotal_fill = PatternFill('solid', fgColor='F2F2F2')
    total_fill = PatternFill('solid', fgColor='FFF2CC')
    done_fill = PatternFill('solid', fgColor='E2EFDA')
    wait_fill = PatternFill('solid', fgColor='FFF2CC')
    review_fill = PatternFill('solid', fgColor='FCE4D6')
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center', wrap_text=True)
    thin = Side(style='thin', color='BFBFBF')
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    headers = ['#', 'الوقت', 'النوع', 'الرقم', 'المبلغ', 'الحالة', 'ملاحظات']
    ncols = len(headers)
    AMOUNT_COL = 5

    def _merged_line(text, font=bold, fill=None, align=center, height=None):
        ws.append([text])
        r = ws.max_row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
        cell = ws.cell(row=r, column=1)
        cell.font = font
        cell.alignment = align
        if fill is not None:
            for c in range(1, ncols + 1):
                ws.cell(row=r, column=c).fill = fill
        if height:
            ws.row_dimensions[r].height = height

    def _table_row(values, *, header=False, fill=None):
        ws.append(values)
        r = ws.max_row
        for c in range(1, ncols + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = box
            cell.alignment = right if c == ncols else center
            if header:
                cell.font = white_bold
                cell.fill = head_fill
            elif fill is not None and c == 6:
                cell.fill = fill
        if not header:
            ws.cell(row=r, column=AMOUNT_COL).number_format = '#,##0'

    def _subtotal_row(label, value, fill):
        ws.append([label, None, None, None, float(value or 0)])
        r = ws.max_row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=AMOUNT_COL - 1)
        for c in range(1, ncols + 1):
            cell = ws.cell(row=r, column=c)
            cell.fill = fill
            cell.border = box
            cell.font = bold
            cell.alignment = center
        ws.cell(row=r, column=AMOUNT_COL).number_format = '#,##0'

    def _note_for(row) -> str:
        bits = []
        if row.get('is_payment'):
            bits.append('سداد')
        if row.get('is_seller_collection'):
            bits.append('تحصيل')
        notes = _clean_note(row.get('notes'))
        if notes:
            bits.append(notes[:60])
        if row.get('reason'):
            reason = str(row['reason'])
            bits.append(_CANCEL_REASON_AR.get(reason, reason)[:60])
        return ' — '.join(bits)

    # ── title block ──
    _merged_line(f'كشف حساب يوم {_date_display_ar(report_date_iso)}', font=title_font, height=26)
    _merged_line(f'العميل: {customer_name or ""}', font=Font(bold=True, size=12))
    _merged_line('يشمل كل عمليات اليوم من جميع أرقامك وما سجّله مكتب قرطبة' +
                 (f'  •  صدر في {generated_at}' if generated_at else ''), font=sub_font)
    ws.append([])

    populated = [g for g in groups if _group_has_rows(g) or g.get('cancelled')]
    for g in populated:
        label = g.get('label') or g.get('phone') or ''
        if g.get('partner_id') is None:
            heading = '🏢 ' + label
        else:
            heading = '📱 ' + label + (' (رقمك)' if g.get('is_self') else '')
        _merged_line(heading, fill=section_fill, height=20)
        _table_row(headers, header=True)
        n = 0
        sec_total = 0.0
        for bucket, status, fill in (('executed', '✅ منفذة', done_fill), ('in_flight', '⏳ قيد التنفيذ', wait_fill)):
            for row in g.get(bucket) or []:
                n += 1
                amount = float(row.get('value') or 0)
                if not row.get('is_payment'):
                    sec_total += amount
                _table_row([n, _short_time(row.get('time')), str(row.get('type') or ''),
                            row.get('account_number') or '—', amount, status, _note_for(row)], fill=fill)
        for row in g.get('pending_transactions') or []:
            n += 1
            _table_row([n, _short_time(row.get('time')), str(row.get('type') or ''),
                        row.get('account_number') or '—', float(row.get('value') or 0),
                        '🕓 قيد المراجعة', _note_for(row)], fill=review_fill)
        for row in g.get('pending_payments') or []:
            n += 1
            _table_row([n, _short_time(row.get('time')), str(row.get('type') or ''),
                        '—', float(row.get('value') or 0), '🕓 سداد قيد المراجعة', _note_for(row)], fill=review_fill)
        for row in g.get('cancelled') or []:
            n += 1
            _table_row([n, _short_time(row.get('time')), str(row.get('type') or ''),
                        row.get('account_number') or '—', float(row.get('value') or 0),
                        '❌ ملغاة (غير محسوبة)', _note_for(row)], fill=review_fill)
        _subtotal_row(f'إجمالي تحويلات {label}', sec_total, subtotal_fill)
        ws.append([])

    if not populated:
        _merged_line('لا توجد عمليات مسجلة في هذا اليوم', font=sub_font)
        ws.append([])

    # ── day totals ──
    _subtotal_row('💸 إجمالي التحويلات اليوم (كل الأرقام)', float(total_debit or 0), total_fill)
    if total_credit:
        _subtotal_row('💵 إجمالي السداد اليوم', float(total_credit or 0), total_fill)
    ws.append(['🏦 الرصيد الحالي', None, None, None, _balance_phrase(current_balance)])
    r = ws.max_row
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=AMOUNT_COL - 1)
    ws.merge_cells(start_row=r, start_column=AMOUNT_COL, end_row=r, end_column=ncols)
    for c in range(1, ncols + 1):
        cell = ws.cell(row=r, column=c)
        cell.fill = total_fill
        cell.border = box
        cell.font = bold
        cell.alignment = center

    for idx, width in enumerate((5, 9, 14, 17, 13, 20, 30), start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = 'A5'
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.print_options.horizontalCentered = True

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _statement_caption(customer_name, report_date_iso, total_debit, total_credit, current_balance) -> str:
    lines = [f'كشف حساب {_date_display(report_date_iso)} — {customer_name}',
             f'💸 إجمالي التحويلات: {_fmt_int(total_debit)} جنيه']
    if total_credit:
        lines.append(f'💵 إجمالي السداد: {_fmt_int(total_credit)} جنيه')
    lines.append(f'🏦 الرصيد الحالي: {_balance_phrase(current_balance)}')
    return '\n'.join(lines)


def statement_display_name(report_date_iso: str) -> str:
    return f'كشف حساب {_date_display(report_date_iso)}.xlsx'


def store_statement_xlsx(customer_pk, xlsx: bytes, report_date_iso: str) -> Tuple[Optional[str], str]:
    """Store the file as an Attachment in media storage and return (public URL, display name).
    Meta fetches documents by public URL (chat documents and template headers alike)."""
    from django.core.files.base import ContentFile
    from modules.base.models.attachment import Attachment
    from modules.chat.utils.file_utils import get_media_url_for_attachment

    stored_name = f'qurtoba_statement_{customer_pk}_{report_date_iso}_{int(time.time())}.xlsx'
    attachment = Attachment(name=stored_name, mime_type=_XLSX_MIME, type='document', size=len(xlsx))
    attachment.file.save(stored_name, ContentFile(xlsx), save=True)
    return get_media_url_for_attachment(attachment), statement_display_name(report_date_iso)


def _send_statement_document(conversation, customer_pk, xlsx: bytes,
                             report_date_iso: str, caption: str) -> Tuple[bool, Optional[str]]:
    """Store the file as a chat Attachment and send it as a WhatsApp document.

    Same path the Cash-SYS receipt images take (tasks._build_and_save_receipt_for_txn):
    Meta fetches the file by public URL, so it must live in media storage.
    """
    from django.core.files.base import ContentFile
    from modules.base.models.attachment import Attachment
    from modules.chat.services.omnichannel_send_service import OmnichannelSendService
    from modules.chat.utils.file_utils import get_media_url_for_attachment
    from qurtoba.ai_guard import system_send
    from qurtoba.extensions import _get_system_partner

    url, display_name = store_statement_xlsx(customer_pk, xlsx, report_date_iso)
    if not url:
        return False, 'no public media URL for the statement file'

    with system_send():
        result = OmnichannelSendService().send_and_broadcast(
            partner=conversation.social_partner,
            content={'url': url},
            message_type='document',
            filename=display_name,
            caption=caption,
            conversation=conversation,
            system_partner=_get_system_partner(conversation),
            websocket=True,
        )
    if not isinstance(result, dict) or not result.get('success'):
        return False, (result or {}).get('error') if isinstance(result, dict) else 'send failed'
    return True, None


def collect_customer_day(customer, partner, target_date) -> Dict[str, Any]:
    """Everything that happened on the CUSTOMER's account on `target_date`, from every
    number and from the office (partner=NULL), grouped per originating phone.

    Shared by the on-demand statement tool and the end-of-day reminder so both show the
    same rows, the same buckets and the same totals. `partner` is the phone the statement
    is for (its section leads and is marked «رقمك»); it may be None.
    """
    from qurtoba.models import (
        QurtobaRecord,
        QurtobaPendingTransaction,
        QurtobaPendingPayment,
    )
    # The asking phone. Its section leads the report and is marked «رقمك».
    self_partner_id = getattr(partner, 'pk', None)
    self_phone = _normalize_phone(getattr(partner, 'phone', None))

    # group key → group dict. Key is partner_id, or None for the system section.
    groups: Dict[Any, dict] = {}
    order_counter = 0

    def _group_for(partner_obj, partner_id):
        """Fetch-or-create the section for a row's originating partner.

        Insertion order doubles as the "first activity" sort key: the record
        queryset is already ordered by time, and pendings are scanned after it.
        """
        nonlocal order_counter
        g = groups.get(partner_id)
        if g is None:
            is_self = partner_id is not None and partner_id == self_partner_id
            # Prefer the context's own partner instance for the asking phone —
            # it is the one we know is fully loaded.
            source = partner if is_self else partner_obj
            g = _new_group(source, is_self=is_self, order=order_counter)
            order_counter += 1
            groups[partner_id] = g
        return g

    # Always open the asking phone's section, even on a day it did nothing, so
    # a customer with several numbers still sees where their own line sits.
    _group_for(partner, self_partner_id)

    # select_related('partner'): without it each row below costs an extra query.
    qs = (
        QurtobaRecord.objects
        .select_related('partner')
        .filter(customer=customer, date=target_date)
        .order_by('time', 'id')
    )

    total_debit = 0.0
    total_credit = 0.0
    transactions: List[dict] = []
    hidden_nonpositive = 0

    _CASH_TYPES = {'كاش', 'كاش(5)', 'كاش(10)', 'كاش(20)'}

    for r in qs:
        amount = float(r.value or 0)

        # A zero or negative row is never shown and never counted. Zeros are
        # placeholder/void rows; negatives are corrections that would read as a
        # transfer the customer never asked for. Both are withheld from the
        # statement AND from the totals so the printed lines add up to the
        # printed total; the count is reported so the omission is auditable.
        if amount <= 0:
            original = float(getattr(r, 'cash_sys_original_value', None) or 0)
            if getattr(r, 'cash_sys_state', None) == 'canceled' and original > 0:
                # a transfer the customer asked for that Cash-SYS reversed: listed as «ملغاة»
                # so the day reads complete, excluded from every total
                g = _group_for(r.partner, r.partner_id)
                g['cancelled'].append({
                    'record_id': r.pk, 'type': r.type, 'value': original,
                    'account_number': r.account_number,
                    'time': r.time.strftime('%H:%M:%S') if r.time else None,
                    'reason': getattr(r, 'cash_sys_canceled_reason', None) or '',
                    'partner_id': r.partner_id, 'partner_phone': g['phone'], 'is_self': g['is_self'],
                })
                continue
            hidden_nonpositive += 1
            continue

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

        g = _group_for(r.partner, r.partner_id)
        if r.is_down:
            g['totals']['credit'] += amount
        else:
            g['totals']['debit'] += amount

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
            'partner_id': r.partner_id,
            'partner_phone': g['phone'],
            'is_self': g['is_self'],
        }
        transactions.append(row)
        g['executed' if bucket == 'executed' else 'in_flight'].append(row)

    # Pending review queues — only those *created* today (use created_at date).
    p_txn_qs = (
        QurtobaPendingTransaction.objects
        .select_related('partner')
        .filter(customer=customer, created_at__date=target_date, review_state='pending')
        .order_by('created_at', 'id')
    )
    pending_transactions = []
    for p in p_txn_qs:
        value = float(p.value or 0)
        if value <= 0:
            hidden_nonpositive += 1
            continue
        g = _group_for(p.partner, p.partner_id)
        row = {
            'pending_id':     p.pk,
            'type':           p.type,
            'value':          value,
            'account_number': p.account_number,
            'reason':         p.reason,
            'time':           p.created_at.strftime('%H:%M:%S'),
            'partner_id':     p.partner_id,
            'partner_phone':  g['phone'],
            'is_self':        g['is_self'],
        }
        pending_transactions.append(row)
        g['pending_transactions'].append(row)

    p_pay_qs = (
        QurtobaPendingPayment.objects
        .select_related('partner')
        .filter(customer=customer, created_at__date=target_date, review_state='pending')
        .order_by('created_at', 'id')
    )
    pending_payments = []
    for p in p_pay_qs:
        value = float(p.value or 0)
        if value <= 0:
            hidden_nonpositive += 1
            continue
        g = _group_for(p.partner, p.partner_id)
        row = {
            'pending_id':     p.pk,
            'type':           p.type,
            'value':          value,
            'account_number': p.account_number,
            'time':           p.created_at.strftime('%H:%M:%S'),
            'partner_id':     p.partner_id,
            'partner_phone':  g['phone'],
            'is_self':        g['is_self'],
        }
        pending_payments.append(row)
        g['pending_payments'].append(row)

    ordered_groups = _sort_groups(groups)

    return {
        'self_partner_id': self_partner_id, 'self_phone': self_phone, 'groups': ordered_groups,
        'total_debit': total_debit, 'total_credit': total_credit, 'transactions': transactions,
        'pending_transactions': pending_transactions, 'pending_payments': pending_payments,
        'hidden_nonpositive': hidden_nonpositive,
    }


# ── tool ────────────────────────────────────────────────────────────────────

@tool(
    name='qurtoba_get_customer_daily_transactions',
    display_name='Send Customer Qurtoba Daily Statement',
    description=(
        'Daily statement (كشف حساب اليوم / تقرير / حركات اليوم). '
        '⚠️ THIS TOOL POSTS THE STATEMENT ITSELF — one message per phone number. On success '
        'output ZERO characters, exactly like the balance tool; do NOT retype or summarise it. '
        'INPUTS: report_date optional (ISO YYYY-MM-DD; omit=today). '
        'send_report optional (OMIT to send; default true). '
        '🔴 Set send_report=FALSE for a FILTERED/subset ask («اللي متمتش»/«كام اتنفذ»/«تحويلاتي '
        'انا»/"which are still pending"): nothing is posted, you read transactions[] '
        '(`bucket` = "executed"/"in_flight"; `is_self` = sent from THIS number; `partner_phone` = '
        'which number sent it), filter to ALL matching items and write your own short reply. '
        'A customer can have SEVERAL numbers, so the statement is sectioned per phone (the asking '
        'one marked «رقمك») plus a «بواسطة قرطبة» section for what the accountant entered. '
        'Amounts of zero or less are withheld from the customer (see hidden_nonpositive_count).'
    ),
    category='qurtoba',
    requires_auth=True,
    side_effect=True,
    rate_limit=20,
)
def qurtoba_get_customer_daily_transactions(
    context,
    report_date: Optional[str] = None,
    send_report: Optional[bool] = None,
) -> Dict[str, Any]:
    # Typed Optional and defaulted here rather than `= True`: the LangChain
    # adapter turns every non-required field into Optional[T] with default None
    # and passes it explicitly, so a plain `True` default would be overwritten
    # by None on every call the model makes without the argument — silently
    # turning sending off. Omitted means SEND.
    should_send = True if send_report is None else bool(send_report)

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


    day_data = collect_customer_day(customer, partner, target_date)
    self_partner_id, self_phone = day_data['self_partner_id'], day_data['self_phone']
    ordered_groups, transactions = day_data['groups'], day_data['transactions']
    total_debit, total_credit = day_data['total_debit'], day_data['total_credit']
    pending_transactions, pending_payments = day_data['pending_transactions'], day_data['pending_payments']
    hidden_nonpositive = day_data['hidden_nonpositive']
    customer.refresh_from_db(fields=['balance'])
    grade_limit = (customer.grade or 0) * 1000

    # Footer totals and the balance stay customer-wide on purpose: the debt is
    # the customer's (Qurtoba's Rest table holds one row per customer), so
    # splitting it per phone would be meaningless.
    messages = _build_messages(
        customer_name=customer.name,
        report_date_iso=target_date.isoformat(),
        groups=ordered_groups,
        total_debit=total_debit,
        total_credit=total_credit,
        current_balance=customer.balance or 0,
    )

    executed_count = sum(1 for t in transactions if t['bucket'] == 'executed')

    result = {
        'success': True,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'report_date': target_date.isoformat(),
        'self_partner_id': self_partner_id,
        'self_phone': self_phone,
        'transaction_count': len(transactions),
        'executed_count': executed_count,
        'in_flight_count': len(transactions) - executed_count,
        'pending_transactions_count': len(pending_transactions),
        'pending_payments_count': len(pending_payments),
        'hidden_nonpositive_count': hidden_nonpositive,
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
        'groups': [_public_group(g) for g in ordered_groups],
        'message_count': len(messages),
    }

    if not should_send:
        # Read-only: nothing was posted, so the agent MUST produce the reply.
        # agent_must_speak is what stops _tool_fully_handled_reply from
        # silencing this turn (see node_executor._ACK_SENDING_TOOLS).
        result['report_sent'] = False
        result['agent_must_speak'] = True
        result['note'] = ('Read-only: nothing was posted. Answer the question yourself from '
                          'transactions[] — do not paste the whole statement.')
        return result

    if conv is None:
        result['success'] = False
        result['report_sent'] = False
        result['agent_must_speak'] = True
        result['error_type'] = 'no_conversation'
        result['error'] = 'No active conversation to post the statement into.'
        return result

    from qurtoba.ai_guard import mark_reply_delivered

    # One Excel file with a summary caption is the delivery the customer asked
    # for; the text messages are only the fallback when it cannot be delivered.
    caption = _statement_caption(customer.name, target_date.isoformat(),
                                 total_debit, total_credit, customer.balance or 0)
    try:
        xlsx = _build_statement_xlsx(
            customer_name=customer.name,
            report_date_iso=target_date.isoformat(),
            groups=ordered_groups,
            total_debit=total_debit,
            total_credit=total_credit,
            current_balance=customer.balance or 0,
        )
        delivered, xlsx_error = _send_statement_document(
            conv, customer.pk, xlsx, target_date.isoformat(), caption,
        )
    except Exception as e:  # noqa: BLE001 — fall back to text, never raise into the agent
        delivered, xlsx_error = False, str(e)

    if delivered:
        mark_reply_delivered(conv)
        result['report_sent'] = True
        result['delivery'] = 'xlsx'
        result['messages_sent'] = 1
        result['note'] = ('Statement already posted to the chat as an Excel file with a '
                          'summary caption. Output ZERO characters.')
        return result

    logger.warning('qurtoba statement: xlsx delivery failed (%s); falling back to text', xlsx_error)

    sent, send_error = _send_messages(conv, messages)
    result['report_sent'] = sent > 0
    result['messages_sent'] = sent
    result['delivery'] = 'text'

    if send_error or sent < len(messages):
        # A partial send leaves the customer with half a statement — the agent
        # has to say something, so never suppress its reply here.
        result['success'] = sent > 0
        result['agent_must_speak'] = True
        result['error_type'] = 'send_failed'
        result['error'] = send_error or 'Not every message was delivered.'
        return result

    mark_reply_delivered(conv)
    result['note'] = (f'Statement already posted to the chat as {sent} message(s). '
                      'Output ZERO characters.')
    return result
