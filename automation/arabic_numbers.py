"""Amounts written in Arabic WORDS → integer, deterministically.

The planner's numeric parser refuses spelled counts («خمسين الف») on purpose: it
cannot read words, and a bare multiplier fallback would give a 50× wrong amount.
Under workflow v1 the language model read those; under v2 this module does.

Supported: units, teens, tens, hundreds (Egyptian and formal spellings), the
thousand / million multipliers, compounds joined with «و», and mixed digit+word
forms («27 الف», «3 الاف و 500»). Anything containing an unknown word returns
None — the caller then asks the customer rather than guessing.
"""
import re
from typing import Optional

from qurtoba.tools._amounts import _CURRENCY_WORDS
from .lexicon import norm

_UNITS = {
    'واحد': 1, 'واحده': 1, 'اتنين': 2, 'اثنين': 2, 'اثنان': 2, 'تلاته': 3, 'ثلاثه': 3, 'اربعه': 4,
    'اربع': 4, 'خمسه': 5, 'خمس': 5, 'سته': 6, 'ست': 6, 'سبعه': 7, 'سبع': 7, 'تمانيه': 8, 'ثمانيه': 8,
    'تمان': 8, 'تسعه': 9, 'تسع': 9, 'عشره': 10, 'عشر': 10,
    'حداشر': 11, 'احد عشر': 11, 'اطناشر': 12, 'اتناشر': 12, 'اثنا عشر': 12, 'تلتاشر': 13, 'ثلاثه عشر': 13,
    'اربعتاشر': 14, 'اربعه عشر': 14, 'خمستاشر': 15, 'خمسه عشر': 15, 'ستاشر': 16, 'سته عشر': 16,
    'سبعتاشر': 17, 'سبعه عشر': 17, 'تمنتاشر': 18, 'ثمانيه عشر': 18, 'تسعتاشر': 19, 'تسعه عشر': 19,
}
_TENS = {
    'عشرين': 20, 'تلاتين': 30, 'ثلاثين': 30, 'اربعين': 40, 'خمسين': 50, 'ستين': 60, 'سبعين': 70,
    'تمانين': 80, 'ثمانين': 80, 'تسعين': 90,
}
_HUNDREDS = {
    'ميه': 100, 'مائه': 100, 'مئه': 100, 'مية': 100, 'متين': 200, 'ميتين': 200, 'مئتين': 200, 'مائتين': 200,
    'تلتميه': 300, 'تلاتميه': 300, 'ثلاثمائه': 300, 'ثلاثميه': 300, 'ثلثمائه': 300,
    'ربعميه': 400, 'اربعميه': 400, 'اربعمائه': 400, 'اربعمئه': 400,
    'خمسميه': 500, 'خمسمائه': 500, 'خمسمئه': 500, 'خمس ميه': 500,
    'ستميه': 600, 'ستمائه': 600, 'ستمئه': 600,
    'سبعميه': 700, 'سبعمائه': 700, 'سبعمئه': 700,
    'تمنميه': 800, 'تمانميه': 800, 'ثمانمائه': 800, 'ثمانمئه': 800,
    'تسعميه': 900, 'تسعمائه': 900, 'تسعمئه': 900,
}
_THOUSAND = {'الف': 1000, 'الاف': 1000, 'تلاف': 1000, 'الوف': 1000, 'الفا': 1000, 'الفات': 1000}
_THOUSAND_DUAL = {'الفين': 2000, 'الفان': 2000}
_MILLION = {'مليون': 1_000_000, 'ملايين': 1_000_000, 'مليونا': 1_000_000}
_MILLION_DUAL = {'مليونين': 2_000_000, 'مليونان': 2_000_000}

_NOISE = set(w.replace('ة', 'ه') for w in _CURRENCY_WORDS) | {'جنيه', 'جنيها', 'ج', 'م', 'ج.م', 'مصري', 'بس', 'فقط', 'لا', 'غير'}

_DIGITS_RE = re.compile(r'^\d+$')


def _split_compound(token: str):
    """«وخمسين» → ('و', 'خمسين'); «خمسمائه» stays whole."""
    if token.startswith('و') and len(token) > 2:
        return ['و', token[1:]]
    return [token]


def parse_arabic_amount(text: str) -> Optional[int]:
    """Parse a spelled amount. Returns the integer, or None if any word is unknown."""
    t = norm(text)
    if not t:
        return None
    tokens = []
    for raw in t.replace('،', ' ').split():
        for tok in _split_compound(raw):
            if tok in ('و', 'و', 'and'):
                continue
            if tok in _NOISE:
                continue
            tokens.append(tok)
    # two-word teens/hundreds («خمس ميه», «احد عشر») — join known bigrams
    joined = []
    i = 0
    while i < len(tokens):
        if i + 1 < len(tokens):
            pair = tokens[i] + ' ' + tokens[i + 1]
            if pair in _UNITS or pair in _HUNDREDS:
                joined.append(pair)
                i += 2
                continue
        joined.append(tokens[i])
        i += 1
    tokens = joined
    if not tokens:
        return None

    total = 0          # finished groups (already multiplied)
    group = 0          # the count being built before a multiplier
    saw_number = False
    for tok in tokens:
        if _DIGITS_RE.match(tok):
            group += int(tok)
            saw_number = True
        elif tok in _UNITS:
            group += _UNITS[tok]
            saw_number = True
        elif tok in _TENS:
            group += _TENS[tok]
            saw_number = True
        elif tok in _HUNDREDS:
            group += _HUNDREDS[tok]
            saw_number = True
        elif tok in _THOUSAND_DUAL:
            total += _THOUSAND_DUAL[tok] if group == 0 else group * 1000
            group = 0
            saw_number = True
        elif tok in _THOUSAND:
            # «٢٧٠٠٠ ألف» restates the unit: a count already ≥ 1000 is NOT scaled again.
            total += group if group >= 1000 else (group or 1) * 1000
            group = 0
            saw_number = True
        elif tok in _MILLION_DUAL:
            total += _MILLION_DUAL[tok] if group == 0 else group * 1_000_000
            group = 0
            saw_number = True
        elif tok in _MILLION:
            total += group if group >= 1_000_000 else (group or 1) * 1_000_000
            group = 0
            saw_number = True
        else:
            return None     # an unknown word — a name, a note — never guess
    total += group
    if not saw_number or total <= 0:
        return None
    return int(total)
