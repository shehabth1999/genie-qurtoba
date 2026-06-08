"""
Qurtoba transaction receipt-image generator.

Renders a styled PNG receipt that mirrors the cash-app confirmation card the
partners are used to seeing: a white top section with the headline transfer
details, and a gray footer block with the operation timing, the (masked)
source number, and the executing device label.

The image is generated server-side with Pillow. Arabic is shaped + bidi-ordered
before drawing. We prefer Pillow's RAQM layout engine (HarfBuzz + FriBidi),
which does correct OpenType shaping and bidi for mixed Arabic/Latin/digits with
no missing-glyph artifacts. When RAQM is unavailable in the build, we fall back
to `arabic_reshaper` + `python-bidi`.

Public entry point:
    render_receipt_png(record, *, fee, sim_number, device_name) -> bytes
"""
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont, features

logger = logging.getLogger(__name__)

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'assets', 'fonts')
_FONT_BOLD = os.path.join(_FONT_DIR, 'Cairo-Bold.ttf')
_FONT_REGULAR = os.path.join(_FONT_DIR, 'Cairo-Regular.ttf')

_RAQM = features.check('raqm')

# ── palette ──────────────────────────────────────────────────────────────────
_WHITE = (255, 255, 255)
_GRAY_BG = (242, 242, 242)
_INK = (33, 33, 33)
_MUTED = (90, 90, 90)
_ACCENT = (20, 20, 20)

# Arabic month names (1-indexed via [m]).
_AR_MONTHS = [
    '', 'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر',
]


def _shape(text) -> str:
    """
    Prepare an Arabic string for drawing.

    With RAQM, HarfBuzz + FriBidi handle shaping/bidi at draw time, so we return
    the raw logical-order string. Without RAQM, we reshape + bidi-order here.
    """
    text = str(text)
    if _RAQM:
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(text))


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    engine = ImageFont.Layout.RAQM if _RAQM else ImageFont.Layout.BASIC
    return ImageFont.truetype(path, size, layout_engine=engine)


def _mask_number(raw) -> str:
    """01062961186 -> 0102***4594 (keep first 4 + last 4, mask the middle)."""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if len(digits) <= 8:
        return digits or '-'
    return f'{digits[:4]}***{digits[-4:]}'


def _arabic_datetime(dt) -> str:
    """'01 يونيو , 12:17 م' from a (localized) datetime; '-' when missing."""
    if not dt:
        return '-'
    month = _AR_MONTHS[dt.month] if 1 <= dt.month <= 12 else ''
    hour24 = dt.hour
    suffix = 'ص' if hour24 < 12 else 'م'
    hour12 = hour24 % 12 or 12
    return f'{dt.day:02d} {month} , {hour12}:{dt.minute:02d} {suffix}'


def _fmt_amount(value) -> str:
    try:
        return f'{float(value):,.0f}'
    except (TypeError, ValueError):
        return str(value or 0)


def _fmt_fee(value) -> str:
    try:
        return f'{float(value or 0):g}'
    except (TypeError, ValueError):
        return str(value or 0)


def _draw_centered(draw, cx, y, text, font, fill):
    """Draw `text` horizontally centered on cx at vertical y. Returns line height."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text((cx - w / 2, y), text, font=font, fill=fill)
    return h


def render_receipt_png(record, *, fee=None, sim_number=None, device_name=None) -> bytes:
    """
    Render the transaction receipt to PNG bytes for a whole `record`.

    Pulls timing/sim/device from the record's persisted cash-sys fields and
    falls back to the values passed from the webhook payload (fee/sim_number/
    device_name) when the record fields are not yet populated.

    Raises on failure — the caller is expected to catch and fall back to text.
    """
    from django.utils import timezone
    done_at = record.cash_sys_done_at or timezone.now()

    return _render(
        value=record.value or 0,
        account=record.account_number or '-',
        fee=record.cash_sys_fee if record.cash_sys_fee is not None else fee,
        sim=record.cash_sys_sim or sim_number,
        device=record.cash_sys_device or device_name or '-',
        done_at=done_at,
    )


def render_receipt_png_for_txn(txn: dict) -> bytes:
    """
    Render a receipt PNG for a single Cash-SYS transfer brief (one transfer in
    a chain). Reads the per-transfer payload shape:
        {value, transfer_to, fee, sim_number, device_name, executed_at}

    Raises on failure — the caller is expected to catch and fall back to text.
    """
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    txn = txn or {}
    executed = txn.get('executed_at')
    done_at = parse_datetime(executed) if isinstance(executed, str) else executed
    done_at = done_at or timezone.now()

    return _render(
        value=txn.get('value') or 0,
        account=txn.get('transfer_to') or '-',
        fee=txn.get('fee'),
        sim=txn.get('sim_number'),
        device=txn.get('device_name') or '-',
        done_at=done_at,
    )


def _render(*, value, account, fee, sim, device, done_at) -> bytes:
    """
    Core PNG renderer — shared by the record-level and per-transfer entry points.
    `done_at` is normalised to local time here.
    """
    from django.utils import timezone
    account_number = account or '-'
    fee_val = fee
    device = device or '-'
    done_at = done_at or timezone.now()
    done_at = timezone.localtime(done_at) if timezone.is_aware(done_at) else done_at

    # ── canvas ────────────────────────────────────────────────────────────────
    W, H = 1024, 520
    TOP_H = 300  # white section height; rest is the gray footer
    img = Image.new('RGB', (W, H), _WHITE)
    draw = ImageDraw.Draw(img)

    # gray footer block + thin separator line
    draw.rectangle([0, TOP_H, W, H], fill=_GRAY_BG)
    draw.line([0, TOP_H, W, TOP_H], fill=(225, 225, 225), width=2)

    cx = W / 2

    # ── top white section ──────────────────────────────────────────────────────
    f_head = _font(_FONT_BOLD, 52)
    f_sub = _font(_FONT_BOLD, 40)

    line1 = _shape(f'تم تحويل {_fmt_amount(value)} جنية لرقم {account_number}')
    line2 = _shape(f'مصاريف الخدمة {_fmt_fee(fee_val)} جنيه')

    y = 82
    h1 = _draw_centered(draw, cx, y, line1, f_head, _ACCENT)
    y += h1 + 60
    _draw_centered(draw, cx, y, line2, f_sub, _ACCENT)

    # ── gray footer section ─────────────────────────────────────────────────────
    f_label = _font(_FONT_BOLD, 34)
    f_foot = _font(_FONT_REGULAR, 34)

    fy = TOP_H + 38
    line_gap = 20

    rows = [
        (_shape(f'توقيت العملية : {_arabic_datetime(done_at)}'), f_label, _MUTED),
        (_shape(f'الرقم المحول منه : {_mask_number(sim)}'), f_label, _MUTED),
        (_shape(f'( {device} )'), f_foot, _INK),
    ]
    for text, font, fill in rows:
        h = _draw_centered(draw, cx, fy, text, font, fill)
        fy += h + line_gap

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()
