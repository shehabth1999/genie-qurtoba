"""
Qurtoba transaction receipt-image generator.

Renders a styled PNG receipt that mirrors the cash-app confirmation card the
partners are used to seeing: a white top section with the headline transfer
details, and a gray footer block (rounded bottom corners) with the operation
timing, the (masked) source number, and the executing device label.

The geometry, fonts, sizes and colors are matched to the real card
(`image.png`, 674x318). We render internally at a supersampled scale and
downscale to the native size so the text is crisp at the exact target size.

Arabic is shaped + bidi-ordered before drawing. We prefer Pillow's RAQM layout
engine (HarfBuzz + FriBidi); when it is unavailable we fall back to
`arabic_reshaper` + `python-bidi`.

Public entry point:
    render_receipt_png(record, *, fee, sim_number, device_name) -> bytes
"""
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont, features

logger = logging.getLogger(__name__)

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'assets', 'fonts')
# The cash-app card's "bold" is lighter than Cairo-Bold — SemiBold matches it
# (heavier than the regular value text, but not as thick as full Bold).
_FONT_BOLD = os.path.join(_FONT_DIR, 'Cairo-SemiBold.ttf')
_FONT_REGULAR = os.path.join(_FONT_DIR, 'Cairo-Regular.ttf')

_RAQM = features.check('raqm')

# Explicit bidi controls: LEFT-TO-RIGHT EMBEDDING … POP DIRECTIONAL FORMATTING.
# Wrapping a token in these forces it to lay out left-to-right even inside an
# RTL Arabic line. Both are zero-width.
_LRE = '‪'
_PDF = '‬'

# ── colors ───────────────────────────────────────────────────────────────────
_WHITE = (255, 255, 255)
_GRAY = (235, 235, 235)        # footer block background — clearly darker than the white top
_INK_HEAD = (12, 12, 12)       # bold headline lines
_INK_FOOT = (18, 18, 18)       # footer lines (weight, not color, carries the emphasis)

# ── native geometry (matches image.png) ──────────────────────────────────────
_W0, _H0 = 674, 318
_GRAY_TOP = 171                # gray footer starts here; flat top, full width
_CORNER_R = 17                 # rounded BOTTOM corners only
_SS = 3                        # internal supersample, downscaled to native at the end

# Headlines: (ink_top_y, font_size). Both bold. Native (S=1) units.
_HEAD_LINES = (
    (42, 35),    # «تم تحويل … لرقم …»
    (105, 34),   # «مصاريف الخدمة … جنيه»
)
# Footer: each line has a BOLD label + REGULAR value (the cash-app card draws the
# label heavier than the data beside it). The last line is bold throughout.
_FOOT_SIZE = 24
_Y_TIME = 186     # «توقيت العملية : {data}»
_Y_SIM = 235      # «الرقم المحول منه : {data}»
_Y_DEVICE = 283   # «( {device} )» — bold

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
    """
    01062961186 -> 0102***4594 (keep first 4 + last 4, mask the middle).

    The result is wrapped in an explicit LTR embedding (LRE … PDF) so the '***'
    in the middle — a bidi-neutral run — does not split the number into two
    segments that get visually reversed inside the RTL Arabic line (which made
    it render as 4594***0102). The control chars are zero-width and honored by
    both the RAQM/FriBidi path and the python-bidi fallback.
    """
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if not digits:
        return '-'
    masked = digits if len(digits) <= 8 else f'{digits[:4]}***{digits[-4:]}'
    return f'{_LRE}{masked}{_PDF}'


def _arabic_datetime(dt) -> str:
    """'01 يونيو , 02:17 م' from a (localized) datetime; '-' when missing."""
    if not dt:
        return '-'
    month = _AR_MONTHS[dt.month] if 1 <= dt.month <= 12 else ''
    hour24 = dt.hour
    suffix = 'ص' if hour24 < 12 else 'م'
    hour12 = hour24 % 12 or 12
    return f'{dt.day:02d} {month} , {hour12:02d}:{dt.minute:02d} {suffix}'


def _fmt_amount(value) -> str:
    # No thousands separator — the cash-app card prints the raw integer (22000,
    # not 22,000), so we mirror it exactly.
    try:
        return f'{float(value):.0f}'
    except (TypeError, ValueError):
        return str(value or 0)


def _fmt_fee(value) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return str(value or 0)
    if num == 0:
        return 'صفر'
    return f'{num:g}'


def _device_line(sim_code, device_name, operator) -> str:
    """
    The footer's last line: «( {sim_code} - {device_name} - {operator} )» — the
    SIM short code, the device label, and the operator who executed the transfer,
    joined exactly as the cash-app card shows them. Missing parts are dropped so
    we never render stray «-» or «None».
    """
    parts = [str(p).strip() for p in (sim_code, device_name, operator)
             if p not in (None, '', '-') and str(p).strip()]
    return f'( {" - ".join(parts)} )' if parts else '-'


def _place_line(canvas, cx, text, font, fill, top_y):
    """
    Draw `text` so its INK top sits exactly at `top_y` and it is centered on cx.

    We render onto a transparent layer, crop to the actual inked bbox, then paste
    — this places by the visible glyph extent (matching how the base card was
    measured), independent of font ascent/line-gap padding.
    """
    layer = Image.new('RGBA', (canvas.width, font.size * 3), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((cx, font.size), text, font=font, fill=fill + (255,), anchor='ms')
    bbox = layer.getbbox()
    if not bbox:
        return
    crop = layer.crop(bbox)
    canvas.paste(crop, (cx - crop.width // 2, top_y), crop)


def _place_runs(canvas, cx, runs, fill, top_y):
    """
    Draw multiple weight runs on ONE shared baseline (so an RTL "label : value"
    line can mix Cairo-Bold for the label and Cairo-Regular for the value), then
    crop to the combined ink, center on cx, and set the ink top at `top_y`.

    `runs` is in visual right-to-left order: the first run is the rightmost
    (the bold label), the last run is the leftmost (the regular value).
    """
    max_size = max(f.size for _, f in runs)
    probe = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    widths = [probe.textlength(t, font=f) for t, f in runs]
    layer_w = max(canvas.width, int(sum(widths)) + 40)
    layer = Image.new('RGBA', (layer_w, max_size * 3), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    baseline = max_size * 2
    right = layer_w - 20
    acc = 0.0
    for (text, font), w in zip(runs, widths):
        ld.text((right - acc, baseline), text, font=font, fill=fill + (255,), anchor='rs')
        acc += w
    bbox = layer.getbbox()
    if not bbox:
        return
    crop = layer.crop(bbox)
    canvas.paste(crop, (cx - crop.width // 2, top_y), crop)


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
        sim_code=record.cash_sys_sim_code,
        device=record.cash_sys_device or device_name,
        operator=record.cash_sys_operator,
        done_at=done_at,
    )


def render_receipt_png_for_txn(txn: dict) -> bytes:
    """
    Render a receipt PNG for a single Cash-SYS transfer brief (one transfer in
    a chain). Reads the per-transfer payload shape:
        {value, transfer_to, fee, sim_number, sim_code, device_name, operator,
         executed_at}

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
        sim_code=txn.get('sim_code'),
        device=txn.get('device_name'),
        operator=txn.get('operator'),
        done_at=done_at,
    )


def _render(*, value, account, fee, sim, done_at, sim_code=None, device=None,
            operator=None) -> bytes:
    """
    Core PNG renderer — shared by the record-level and per-transfer entry points.
    Renders at `_SS`× supersample then downscales to the native 674x318 card.
    """
    from django.utils import timezone
    account_number = account or '-'
    device_line = _device_line(sim_code, device, operator)
    done_at = done_at or timezone.now()
    done_at = timezone.localtime(done_at) if timezone.is_aware(done_at) else done_at

    ss = _SS
    W, H = _W0 * ss, _H0 * ss
    img = Image.new('RGB', (W, H), _WHITE)
    draw = ImageDraw.Draw(img)

    # gray footer block: flat top at _GRAY_TOP, full width, rounded bottom corners
    try:
        draw.rounded_rectangle(
            [0, _GRAY_TOP * ss, W - 1, H - 1],
            radius=_CORNER_R * ss, fill=_GRAY,
            corners=(False, False, True, True),
        )
    except TypeError:  # very old Pillow without `corners` — square footer
        draw.rectangle([0, _GRAY_TOP * ss, W, H], fill=_GRAY)

    cx = W // 2

    # ── bold headlines ──
    head_texts = (
        _shape(f'تم تحويل {_fmt_amount(value)} جنية لرقم {account_number}'),
        _shape(f'مصاريف الخدمة {_fmt_fee(fee)} جنيه'),
    )
    for (top_y, size), text in zip(_HEAD_LINES, head_texts):
        _place_line(img, cx, text, _font(_FONT_BOLD, size * ss), _INK_HEAD, top_y * ss)

    # ── footer: BOLD label (right) + REGULAR value (left) on one baseline ──
    fbold = _font(_FONT_BOLD, _FOOT_SIZE * ss)
    freg = _font(_FONT_REGULAR, _FOOT_SIZE * ss)
    _place_runs(img, cx, [
        (_shape('توقيت العملية : '), fbold),
        (_shape(_arabic_datetime(done_at)), freg),
    ], _INK_FOOT, _Y_TIME * ss)
    _place_runs(img, cx, [
        (_shape('الرقم المحول منه : '), fbold),
        (_shape(_mask_number(sim)), freg),
    ], _INK_FOOT, _Y_SIM * ss)
    # last line — «( sim_code - device - operator )», bold throughout
    _place_line(img, cx, _shape(device_line), fbold, _INK_FOOT, _Y_DEVICE * ss)

    img = img.resize((_W0, _H0), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()
