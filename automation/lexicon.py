"""Keyword tables and text normalisation for the deterministic router.

Every table is a compiled regex over NORMALISED text (see ``norm``): Arabic-Indic
digits → ASCII, أ/إ/آ → ا, ى → ي, ة → ه, harakat and tatweel removed, whitespace
collapsed, lower-cased. Write the patterns in that normalised spelling.
"""
import re
from typing import Iterable

from qurtoba.tools._amounts import _ar_to_ascii, _arabic_normalize

_HARAKAT_RE = re.compile(r'[ً-ْـ]')


def norm(text) -> str:
    """Normalise customer text for keyword matching."""
    t = _arabic_normalize(_ar_to_ascii(str(text or '')))
    t = t.replace('ة', 'ه')
    t = _HARAKAT_RE.sub('', t)
    return ' '.join(t.split()).lower()


def _rx(patterns: Iterable[str]) -> re.Pattern:
    return re.compile('|'.join(f'(?:{p})' for p in patterns))


# ── intents ──────────────────────────────────────────────────────────────────

CANCEL = _rx([
    r'\bالغاء\b', r'\bالغي\b', r'\bالغيه\b', r'\bالغيها\b', r'\bلغي\b', r'\bلغيها\b',
    r'\bاوقف\b', r'\bوقف\b', r'\bوقفها\b', r'\bكنسل\b', r'\bcancel\b', r'\bغلط\b',
    r'\bمتحولش\b', r'\bمتبعتش\b', r'\bما تحولش\b', r'\bمش عايز اح?ول\b',
])

# «تم؟» / «وصل؟» / «الباقي فين» — a question about a transfer already sent.
STATUS = _rx([
    r'\bتم\b', r'\bتمت\b', r'\bخلص\b', r'\bخلصت\b', r'\bوصل\b', r'\bوصلت\b', r'\bاتحول\b',
    r'\bاتحولت\b', r'\bاتنفذ\b', r'\bاتنفذت\b', r'\bحصل ايه\b', r'\bفين الايصال\b',
    r'\bالايصال\b', r'\bفين الفلوس\b', r'\bالباقي\b', r'\bباقي المبلغ\b', r'\bبعت\b.*\bبس\b',
    r'\bاتقبل\b', r'\bلسه\b', r'\bمتمتش\b', r'\bمتنفذتش\b', r'\bمتحولتش\b', r'\bفين\b',
])

# A SUBSET status question → the daily tool with send_report=false, not the 3-row status tool.
STATUS_SUBSET = _rx([
    r'\bمتمتش\b', r'\bمتنفذتش\b', r'\bمتحولتش\b', r'\bلسه\b', r'\bكام اتنفذ\b',
    r'\bتحويلاتي انا\b', r'\bاللي انا بعت', r'\bاللي اتنفذ\b', r'\bاللي متنفذش\b',
])

BALANCE = _rx([
    r'\bرصيد', r'\bحسابي\b', r'\bحسابك\b', r'\bالحساب كام\b', r'\bحساب كام\b', r'\bكام عليا\b',
    r'\bعليا كام\b', r'\bعليه كام\b', r'\bكام عليه\b', r'\bالمديونيه\b', r'\bمديونيت',
    r'\bالحساب\b.*\bكام\b', r'\bليا كام\b', r'\bكام ليا\b', r'\bكام عندك\b', r'\bكام عندي\b',
    r'\bعندي كام\b', r'\bكام اجمالي\b', r'\bاجمالي كام\b',
])

STATEMENT = _rx([
    r'\bكشف\b', r'\bتقرير\b', r'\bحركات\b', r'\bالعمليات\b', r'\bstatement\b',
    r'\bتحويلات(ي|ك|نا)?\b.*\b(النهارده|النهارضه|اليوم|امبارح|امبارح|الليله)\b',
    r'\b(النهارده|النهارضه|اليوم)\b.*\bتحويلات', r'\bاللي اتعمل\b',
])

STATEMENT_YESTERDAY = _rx([r'\bامبارح\b', r'\bامبارح\b', r'\bالبارحه\b', r'\bيوم فات\b'])

GREETING = _rx([
    r'\bالسلام\b', r'\bسلام عليكم\b', r'\bصباح الخير\b', r'\bصباح النور\b', r'\bصباح الفل\b',
    r'\bمساء الخير\b', r'\bمساء النور\b', r'\bاهلا\b', r'\bمرحبا\b', r'\bهاي\b', r'\bهلا\b',
    r'\bhello\b', r'\bhi\b', r'\bازيك\b', r'\bازيكم\b', r'\bعامل ايه\b', r'\bعامله ايه\b',
    r'\bاخبارك\b', r'\bعاملين ايه\b', r'\bكيف حالك\b', r'\bكيفك\b',
])

THANKS = _rx([
    r'\bشكرا\b', r'\bشكرااا*\b', r'\bتسلم\b', r'\bتسلمي\b', r'\bمتشكر', r'\bربنا يخليك\b',
    r'\bالله يخليك\b', r'\bجزاك الله\b', r'\bthanks?\b', r'\bthank you\b', r'\bيعطيك العافيه\b',
    r'\bكتر خيرك\b', r'\bمشكور\b', r'\bتمام شكرا\b',
])

AVAILABILITY = _rx([
    r'\bشغالين\b', r'\bشغال\b', r'\bفاتح\b', r'\bفاتحين\b', r'\bمتاح\b', r'\bمتاحين\b',
    r'\bاقدر اطلب\b', r'\bموجودين\b', r'\bموجود\b', r'\bبتشتغلوا\b', r'\bشغالين ولا\b',
])

PAYMENT = _rx([
    r'\bسداد\b', r'\bسددت\b', r'\bسدد\b', r'\bدفعت\b', r'\bحولت ل', r'\bحولتلك', r'\bحولتلكم',
    r'\bايصال\b', r'\bالايصال\b', r'\bسكرين\b', r'\bشراء\b', r'\bالعميل دفع\b', r'\bدفع\b',
])
# «فين الإيصال؟» is a question about OUR receipt for a transfer → status, not a payment.
RECEIPT_WHERE = _rx([r'فين الايصال', r'الايصال فين', r'فين ايصال', r'ايصال فين'])

# A brake, not an interpretation: an order message that also carries a STOP word is held for
# the model instead of being created on sight («01… ⏎ 500 ⏎ الغي», «… متبعتش», «… بكرة»).
HOLD = _rx([r'\bالغ', r'\bلغي', r'\bوقف', r'\bاوقف', r'\bكنسل', r'\bمتبعتش', r'\bماتبعتش', r'\bما تبعتش', r'\bمتحولش',
            r'\bبكره\b', r'\bبكرا\b', r'\bاستني', r'\bاستنى', r'\bمش دلوقتي', r'\bبعدين\b', r'\bلسه\b', r'\bغلط\b',
            r'\bتحصيل', r'\bمندوب', r'\bسداد', r'\bارجع', r'\bرجع', r'\bاسترجاع'])

# Multi-number requests — «قسم/وزّع المبلغ» (manual at the office) vs «لكل رقم» (same amount each).
SPLIT = _rx([r'\bقسم\b', r'\bقسمه\b', r'\bقسمها\b', r'\bوزع\b', r'\bوزعه\b', r'\bوزعها\b', r'\bتقسيم\b'])
PER_NUMBER = _rx([r'\bلكل رقم\b', r'\bلكل واحد\b', r'\bكل رقم\b', r'\bكل واحد\b', r'\bلكل نمره\b'])

# ── transfer types ───────────────────────────────────────────────────────────

FAWRY = _rx([r'\bفوري\b', r'\bفوى\b', r'\bfawry\b', r'\bفورى\b'])
AMAN = _rx([r'\bامان\b', r'\baman\b'])
TAYER = _rx([r'\bطاير\b', r'\bطايره\b'])
INSTAPAY = _rx([r'انستا', r'instapay', r'insta ?pay', r'\bipn\b', r'\binsta\b'])   # a product name, not meaning

NONCASH_TYPES = (('فورى', FAWRY), ('أمان', AMAN), ('طاير', TAYER))


def noncash_type(text: str):
    """The non-cash transfer type named in `text` (already normalised), else None."""
    for type_name, rx in NONCASH_TYPES:
        if rx.search(text):
            return type_name
    return None


# ── answers ──────────────────────────────────────────────────────────────────

_YES_WORDS = {
    'ايوه', 'ايوا', 'ايو', 'اه', 'اها', 'اي', 'نعم', 'تمام', 'ماشي', 'اوك', 'ok', 'okay', 'كرر',
    'كررها', 'تاكيد', 'اكيد', 'موافق', 'يس', 'yes', 'اتفضل', 'نفذ', 'نفذها', 'كمل', 'اكمل',
    'نكمل', 'ننفذ', 'صح', 'اكد', 'مؤكد', 'موكد', 'اعمل', 'اعملها', 'حول', 'حولها', 'ابعت',
    'ابعتها', 'تمم', 'ok.', '👍', 'يب', 'ايوة',
}
_NO_WORDS = {'لا', 'لأ', 'لاء', 'لاا', 'no', 'خلاص', 'الغي', 'الغيها', 'مش', 'ما', 'بلاش', 'كفايه', 'بس', 'غير', 'الا'}
_FILLER = {'يا', 'باشا', 'فندم', 'يافندم', 'يا فندم', 'حبيبي', 'يا باشا', 'يا حبيبي', 'من', 'فضلك',
           'لو', 'سمحت', 'شكرا', 'طيب', 'و', 'كده', 'كدا', 'ياباشا', 'يا كبير', 'كبير', 'الكل', 'كلها', 'كله', 'كلهم'}
_ANSWER_MAX_WORDS = 4      # a yes/no is a SHORT message; a sentence that contains «ما» is not a «no»


def _answer_words(text: str):
    words = [w.strip('.,!؟?،') for w in norm(text).split()]
    return [w for w in words if w and w not in _FILLER]


def is_yes(text: str) -> bool:
    """A clear affirmative answer («أيوة», «تمام كرر», «تأكيد»), nothing else in it."""
    words = _answer_words(text)
    if not words or len(words) > _ANSWER_MAX_WORDS:
        return False
    # «اها كرر الكل», «ايوه اعملها كلها» — a yes word with a harmless tail is still a yes.
    return any(w in _YES_WORDS for w in words) and not any(w in _NO_WORDS for w in words) \
        and not any(ch.isdigit() for w in words for ch in w)


def is_no(text: str) -> bool:
    """A clear negative answer («لأ», «لا خلاص», «بلاش»)."""
    words = _answer_words(text)
    if not words or len(words) > _ANSWER_MAX_WORDS:
        return False
    return any(w in _NO_WORDS for w in words) and not any(w in _YES_WORDS for w in words)


def is_bare_yes(text: str) -> bool:
    """ONE word that is a yes («حول», «أيوة», «تأكيد») — the only yes Python applies itself;
    anything longer is meaning and goes to the model."""
    words = _answer_words(text)
    return len(words) == 1 and words[0] in _YES_WORDS


def is_bare_no(text: str) -> bool:
    words = _answer_words(text)
    return len(words) == 1 and words[0] in _NO_WORDS


_WAW_RE = re.compile(r'(?<!\S)و(?=\S)')


def strip_waw(t: str) -> str:
    """«وحسابي كام» → «حسابي كام»: a conjunction glued to the word must not hide the keyword."""
    return _WAW_RE.sub('', t)


def is_question(text: str) -> bool:
    return '؟' in str(text or '') or '?' in str(text or '')


_ONLY_EMOJI_RE = re.compile(r'^[\s\U0001F300-\U0001FAFF☀-➿⬀-⯿️‍👍👌🙏❤️]+$')


def is_only_emoji(text: str) -> bool:
    return bool(text) and bool(_ONLY_EMOJI_RE.match(str(text)))
