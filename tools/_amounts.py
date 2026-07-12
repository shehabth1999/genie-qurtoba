"""Deterministic amount normalization for Qurtoba transaction parsing.

Customers write amounts in many forms — Arabic-Indic or ASCII digits, `.`/`,`
used as a THOUSANDS separator or a DECIMAL point, and word multipliers like
"20الف". The agent currently does this conversion in its head, which is a source
of wrong-value transactions. This module makes the conversion deterministic and
auditable so the create path and the planning tool agree on one number.

Public API: ``normalize_amount(raw) -> {ok, value, ambiguous, reason, raw}``.
"""
import math
import re
from typing import Any, Dict

# Arabic-Indic (U+0660–0669) and Eastern-Arabic/Persian (U+06F0–06F9) → ASCII.
# Plus the Arabic separators so "5،400" / "12٬280" / "13٫5" parse like "5,400" etc.
_AR_DIGITS = {
    0x0660: '0', 0x0661: '1', 0x0662: '2', 0x0663: '3', 0x0664: '4',
    0x0665: '5', 0x0666: '6', 0x0667: '7', 0x0668: '8', 0x0669: '9',
    0x06F0: '0', 0x06F1: '1', 0x06F2: '2', 0x06F3: '3', 0x06F4: '4',
    0x06F5: '5', 0x06F6: '6', 0x06F7: '7', 0x06F8: '8', 0x06F9: '9',
    0x060C: ',',   # ، Arabic comma  → thousands separator
    0x066C: ',',   # ٬ Arabic thousands separator
    0x066B: '.',   # ٫ Arabic decimal separator
}

# Arabic word multipliers — matched per whitespace token (after stripping any
# glued leading digits), so a real word like "الفلوس" (the money) is NEVER
# mistaken for ×1000. Spelling variants are unified first by _arabic_normalize
# (أ/إ/آ→ا, ى→ي, tatweel removed), so only these canonical forms are listed:
#   _THOUSAND_FORMS → ×1000, the count comes from the number  ("3 الاف" = 3000)
#   _THOUSAND_DUAL  → 2000 on its own (the word already means "two thousand")
#   bare "الف"      → 1000 on its own
_THOUSAND_FORMS = {'الف', 'الاف', 'الوف', 'الااف', 'الفات', 'الفا'}
_THOUSAND_DUAL = {'الفين', 'الفان'}
_MILLION_FORMS = {'مليون', 'ملايين', 'مليونا'}
_MILLION_DUAL = {'مليونين', 'مليونان'}
# Bare "ك" is intentionally NOT a multiplier — it is a common label prefix
# (e.g. "ك 29"); only a latin k/m glued to a digit (20k, 2.5m) counts.

# Currency / noise words dropped before parsing (never part of the number).
_CURRENCY_WORDS = ('جنيها', 'جنيه', 'جنية', 'ج.م', 'جم', 'egp', 'pounds',
                   'pound', 'le', 'مصرى', 'مصري')


def _ar_to_ascii(s: str) -> str:
    return s.translate(_AR_DIGITS)


def _arabic_normalize(s: str) -> str:
    """Unify alef/ya variants and drop tatweel so word matching is spelling-robust."""
    for a in ('أ', 'إ', 'آ'):
        s = s.replace(a, 'ا')
    return s.replace('ى', 'ي').replace('ـ', '')


def normalize_amount(raw: Any) -> Dict[str, Any]:
    """Normalize a written amount to a positive float.

    Returns ``{ok, value, ambiguous, reason, raw}``:
      * ok=False        → not a usable amount (caller treats as invalid_value).
      * ambiguous=True  → a best-guess value is returned but the `.`/`,` reading
                          was genuinely uncertain; callers may confirm.
    Never raises.
    """
    out = {'ok': False, 'value': None, 'ambiguous': False, 'reason': None, 'raw': raw}

    # bool is an int subclass — reject it explicitly.
    if isinstance(raw, bool):
        out['reason'] = 'not_a_number'
        return out
    # Common case: the LLM already passed a clean number.
    if isinstance(raw, (int, float)):
        v = float(raw)
        # Reject NaN/Inf: `nan <= 0` is False, so without this a non-finite value
        # would sail through every downstream guard and become a garbage amount.
        if not math.isfinite(v):
            out['reason'] = 'not_finite'
            return out
        if v <= 0:
            out['reason'] = 'non_positive'
            return out
        out.update(ok=True, value=v)
        return out
    if not isinstance(raw, str):
        out['reason'] = 'unsupported_type'
        return out

    s = _arabic_normalize(_ar_to_ascii(raw)).strip().lower()
    if not s:
        out['reason'] = 'empty'
        return out

    # A minus glued to a digit ("-500", "1-6") is never a valid transfer amount —
    # the later `[^0-9.,]`→space strip would silently swallow the sign and turn
    # "-500" into 500. Refuse up front.
    if re.search(r'-\s*\d', s):
        out['reason'] = 'negative'
        return out

    # --- Word multipliers (token-based, spelling-robust) ---
    # `implied` holds the value when the word ITSELF encodes the count
    # (الفين = 2000) and there is no separate number.
    multiplier = 1
    implied = None
    kept = []
    for token in s.split():
        m = re.match(r'^([0-9.,]*)(.*)$', token)
        num_part, word = m.group(1), m.group(2)
        if word in _MILLION_DUAL:
            multiplier, implied = 1_000_000, 2_000_000
        elif word in _MILLION_FORMS:
            multiplier = 1_000_000
        elif word in _THOUSAND_DUAL:
            multiplier, implied = 1000, 2000
        elif word in _THOUSAND_FORMS:
            multiplier = 1000
        else:
            kept.append(token)          # not a multiplier word — keep as-is
            continue
        if num_part:
            kept.append(num_part)       # the glued/preceding count ("20الف" → "20")
    s = ' '.join(kept)

    # Latin k/m suffix glued to a digit (20k, 2.5m) — only if no Arabic multiplier.
    if multiplier == 1:
        km = re.search(r'\d\s*([km])\b', s)
        if km:
            multiplier = 1000 if km.group(1) == 'k' else 1_000_000
            s = re.sub(r'\s*[km]\b', ' ', s, count=1)

    # Strip currency / noise words.
    for w in _CURRENCY_WORDS:
        s = s.replace(w, ' ')

    # Keep only digits and separators; everything else becomes a space.
    s2 = re.sub(r'[^0-9.,]+', ' ', s).strip()
    tokens = s2.split()
    if not tokens:
        # No digits at all. A standalone multiplier word carries a value
        # (الف → 1000, الفين → 2000, مليون → 1,000,000) — BUT only when nothing else
        # meaningful preceded it. If a SPELLED count word we can't parse is still here
        # («خمسين الف» = 50,000, but «خمسين» is unreadable to us), returning the bare
        # multiplier (1000) would be a catastrophic wrong amount (50× under-charge).
        # Refuse instead → the value is unknown; the caller treats it as no-amount
        # (orphan → the agent, who CAN read Arabic number words, asks/handles it).
        if re.search(r'[^\W\d_]', s):   # any leftover letter = an unparsed spelled count
            out['reason'] = 'spelled_count_unknown'
            return out
        if implied is not None:
            out.update(ok=True, value=float(implied))
            return out
        if multiplier > 1:
            out.update(ok=True, value=float(multiplier))
            return out
        out['reason'] = 'no_number'
        return out
    if len(tokens) != 1:
        out['reason'] = 'multiple_numbers'
        return out
    tok = tokens[0]

    ambiguous = False
    has_dot, has_comma = '.' in tok, ',' in tok
    if has_dot and has_comma:
        # Rightmost separator is the decimal point; the other is thousands.
        dec_sep = '.' if tok.rfind('.') > tok.rfind(',') else ','
        thou_sep = ',' if dec_sep == '.' else '.'
        tok = tok.replace(thou_sep, '').replace(dec_sep, '.')
    elif has_dot or has_comma:
        sep = '.' if has_dot else ','
        if tok.count(sep) > 1:
            tok = tok.replace(sep, '')           # 1.234.567 → all thousands
        else:
            head, _, tail = tok.partition(sep)
            if len(tail) == 3:
                # Egypt has NO fractional amounts: in text a dot/comma is ALWAYS a
                # thousands separator (the business <no_fractions> law). So 3 trailing
                # digits = thousands, DETERMINISTICALLY — never flag for confirmation
                # (1.380→1380, 27.460→27460, 200.000→200000). Asking the customer to
                # confirm a clear thousands amount is needless friction.
                tok = head + tail
            elif len(tail) in (1, 2):
                # 13.75-style: a name-attached commission tally, filtered out as noise
                # by the planner's classifier — not a real transfer amount.
                tok = head + '.' + tail
            else:
                tok = tok.replace(sep, '')        # 0 or >3 trailing → strip

    try:
        count = float(tok)
    except ValueError:
        out['reason'] = 'parse_failed'
        return out
    # Redundant multiplier word: a fully-written count that is ALREADY ≥ the
    # multiplier's magnitude means the word just restates the unit, it does NOT
    # scale again. «٢٧٠٠٠ ألف» = 27000 (NOT 27,000,000); «1000 الف» = 1000. Only a
    # SMALL count scales: «27 ألف» = 27000, «500 ألف» = 500000. This is the documented
    # business rule (core.md: «ألف» never means million) and the fix for a live 1000×
    # over-charge the deterministic planner could commit straight to create.
    if multiplier > 1 and count >= multiplier:
        value = count
    else:
        value = count * multiplier
    if not math.isfinite(value):
        out['reason'] = 'not_finite'
        return out
    if value <= 0:
        out['reason'] = 'non_positive'
        return out

    out.update(ok=True, value=value, ambiguous=ambiguous,
               reason=('separator_ambiguous' if ambiguous else None))
    return out
