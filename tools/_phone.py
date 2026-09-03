"""Egyptian mobile normalization shared by the Qurtoba tools.

Extracted from ``transactions.py`` so the reporting tools can label a chat
partner's phone the same way the transfer path validates a destination number,
without a second copy of the rules.

Note there is a DIFFERENT canonical form elsewhere in the codebase:
``modules.whatsapp.utils.phone.normalize_phone`` produces the E.164-minus-plus
form (``201038857982``) and is what ``base.Partner.phone`` is stored as. This
module produces the Egyptian local form (``01038857982``) that Qurtoba and the
customer both read. Convert with this function when displaying a partner phone.

Public API: ``_normalize_phone(raw) -> '01XXXXXXXXX' | None``.
"""
from typing import Optional


def _normalize_phone(raw: Optional[str]) -> Optional[str]:
    """
    Normalize an Egyptian mobile to the canonical local form 01XXXXXXXXX (11 digits).

    Accepts and converts the common country-code / formatted variants:
      +20 1038857982   00201038857982   201038857982   0201038857982
      with spaces, '+', dashes — anything; only digits are kept.

    Egypt's country code is 20 and local mobiles are 11 digits starting with 01,
    so the +20 / 0020 / 20 / 020 forms all map to a leading 0 + the 10-digit
    subscriber number. Returns the 11-digit local form, or None if the input
    cannot be turned into a valid Egyptian mobile (caller asks for a correct one).
    """
    if not raw:
        return None
    d = ''.join(ch for ch in str(raw) if ch.isdigit())
    if not d:
        return None

    # International dialing prefix: 00 20 ...  ->  20 ...
    if d.startswith('00'):
        d = d[2:]
    # Country code, with or without a single leading 0 before it:
    #   020 1038857982  ->  1038857982
    #   20  1038857982  ->  1038857982   (only when long enough to be CC + mobile)
    if d.startswith('020'):
        d = d[3:]
    elif d.startswith('20') and len(d) >= 12:
        d = d[2:]
    # Subscriber number without the leading 0 (10 digits starting with 1) -> add 0
    if len(d) == 10 and d.startswith('1'):
        d = '0' + d
    # NOTE: we deliberately do NOT trim an over-length 01-number (e.g. a 12-digit
    # typo). Chopping a digit off a money-transfer destination is unsafe — let it
    # fail validation so the partner is asked for a correct number instead.
    return d or None
