"""
qurtoba.ai_guard — the extension-owned outbound gate for AI replies.

Core carries no product-specific reply rules; this module owns all of them and
attaches to the ONE place every WhatsApp send goes through
(``WhatsAppAPIService.send_and_broadcast``), installed from ``QurtobaConfig.ready``.

It only ever looks at TEXT sends attributed to the AI partner that the extension
did not author itself. Everything the extension sends on purpose (the 👍 ack,
the balance, the statement, Cash-SYS notices, receipts) is wrapped in
``system_send()`` and passes through untouched.

Four layers, in order — a hit on any of them drops the message:

  1. system-only templates   — the 👍/👍🏿 ack and the Cash-SYS outcome notices
                               are statements of fact about money; only the
                               system may send them (incident 2026-08-06).
  2. reply already delivered — a tool answered this turn (it said so via
                               ``mark_reply_delivered``); whatever the agent
                               types afterwards is narration («Created. Stay
                               silent.» — conversation 13f58d64, 2026-08-29).
  3. non-message / narration — «(لا رد)», a lone «.», «Done.», «تم التنفيذ»:
                               nothing a customer could read as a reply.
  4. duplicate               — the identical text to the same conversation
                               within a few seconds (a re-run that already sent).

Layer 2 is turn-scoped, not time-scoped: the tool's mark counts only if no
inbound message arrived after it, so a lingering flag can never eat the answer
to the customer's NEXT message.

When in doubt the message is SENT. Any exception inside the gate is logged and
the send proceeds — a swallowed answer is a customer left waiting.
"""
import contextvars
import hashlib
import logging
import re
import time
from functools import wraps
from typing import Optional

from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── Extension-authored sends ─────────────────────────────────────────────────

_system_send_flag = contextvars.ContextVar('qurtoba_system_send', default=False)


class system_send:
    """Context manager: everything sent inside it is the extension's own voice."""

    def __enter__(self):
        self._token = _system_send_flag.set(True)
        return self

    def __exit__(self, *exc):
        _system_send_flag.reset(self._token)
        return False


def in_system_send() -> bool:
    return bool(_system_send_flag.get())


# ── "A tool already answered this turn" ──────────────────────────────────────

_REPLY_DONE_TTL = 300  # seconds; the turn is over long before this expires


def _reply_key(conversation_id) -> str:
    return f'qurtoba:ai_reply_done:{conversation_id}'


def mark_reply_delivered(conversation) -> None:
    """A tool delivered THE reply for the current turn; agent text is now noise.

    Call it only when the tool's result needs no words from the agent — a fully
    clean create, a posted balance, a posted statement. A part-rejected bulk must
    keep the agent's voice for the rejection, so it must NOT mark.
    """
    conv_id = getattr(conversation, 'id', None) or conversation
    if not conv_id:
        return
    try:
        cache.set(_reply_key(conv_id), time.time(), timeout=_REPLY_DONE_TTL)
    except Exception:
        logger.exception('ai_guard: could not mark reply delivered for %s', conv_id)


def clear_reply_delivered(conversation_id) -> None:
    try:
        cache.delete(_reply_key(conversation_id))
    except Exception:
        pass


def reply_already_delivered(conversation_id) -> bool:
    """True when a tool marked this turn answered and no inbound arrived since."""
    try:
        marked_at = cache.get(_reply_key(conversation_id))
    except Exception:
        return False
    if not marked_at:
        return False
    try:
        from modules.chat.models import Message
        last_inbound = (
            Message.objects.filter(conversation_id=conversation_id, direction='inbound')
            .order_by('-created_at')
            .values_list('created_at', flat=True)
            .first()
        )
    except Exception:
        logger.exception('ai_guard: inbound lookup failed for %s', conversation_id)
        return False
    if last_inbound is not None and last_inbound.timestamp() > float(marked_at):
        # The customer wrote again after the tool replied — a new turn.
        return False
    return True


# ── System-only templates ────────────────────────────────────────────────────
#
# Registered from apps.py with the exact strings the Cash-SYS webhook sends, so
# the agent can never replay them out of chat history (on 2026-08-06 it told a
# customer a 28,000 transfer was cancelled and unrecorded — neither was true).
# Registered whole AND line-by-line because the agent reproduced individual
# lines as separate messages.

_SYSTEM_TEMPLATE_LINES: set = set()


def _normalize_for_match(text: str) -> str:
    """Collapse whitespace, strip markdown emphasis and tatweel for comparison."""
    t = re.sub(r'[*_~`]', '', str(text or ''))
    t = t.replace('ـ', '')                 # Arabic tatweel
    t = re.sub(r'[\s‏‎]+', ' ', t)  # incl. bidi marks
    return t.strip().strip('.!،,').strip()


def register_system_templates(*texts) -> None:
    for text in texts:
        if not text:
            continue
        whole = _normalize_for_match(text)
        if whole:
            _SYSTEM_TEMPLATE_LINES.add(whole)
        for line in str(text).split('\n'):
            norm = _normalize_for_match(line)
            if len(norm) >= 8 or norm in ('👍', '👍🏿'):
                _SYSTEM_TEMPLATE_LINES.add(norm)


def is_system_template_impersonation(output: str) -> bool:
    if not output or not _SYSTEM_TEMPLATE_LINES:
        return False
    whole = _normalize_for_match(output)
    if whole in _SYSTEM_TEMPLATE_LINES:
        return True
    for line in str(output).split('\n'):
        if _normalize_for_match(line) in _SYSTEM_TEMPLATE_LINES:
            return True
    return False


# ── Non-message / self-narration ─────────────────────────────────────────────
#
# DELIBERATELY NARROW: a message fires only when EVERY sentence is a known
# self-report shape and none carries a digit or a question. A real answer with a
# stray narration line is still sent; a swallowed answer is the worse failure.

_NARRATION_RES = [
    # Arabic self-reports observed 2026-08-06/07.
    re.compile(r'^\s*تم\s+الرد\s+(على|علي)\b'),
    re.compile(r'^\s*لا\s+(يوجد|توجد)\s+(طلبات|رسائل|معاملات|عمليات)\b.*\bمعلق'),
    re.compile(r'^\s*تم\s+الرد\s+على\s+(جميع|كل)\b'),
    re.compile(r'^\s*(تم\s+التنفيذ|تمت\s+المعالجة|خلصت|تم\s+كل\s+شيء)\s*[.!]?\s*$'),
    re.compile(r'^\s*تم\s+\S*\s*(إنشاء|انشاء|تنفيذ|تسجيل|إرسال|ارسال)\b.*\b(بنجاح|تمام)\b'),
    re.compile(r'^\s*تم\s+(إنشاء|انشاء|تنفيذ|تسجيل)\s+.*\bبنجاح\b'),
    re.compile(r'^\s*لا\s+(حاجة|داعي)\s+(ل|لإ|للـ?)'),
    # English self-reports observed in conversation 13f58d64 (2026-08-25..30):
    # «created. silence.», «Created. The tool sent 👍. Stay silent.», «Done.»
    re.compile(r'^\s*(successfully (created|executed|registered|sent)|no (reply|response) (is )?(needed|required))\b', re.I),
    re.compile(r'^\s*(replied to|responded to|no pending|nothing pending|all messages (have been )?(handled|answered))\b', re.I),
    re.compile(r'^\s*(created|done|sent|handled|executed|ok|okay|silence|silent|noted)\s*$', re.I),
    re.compile(r'^\s*(stay|staying|remain|remaining|keep|keeping)\s+silent\b', re.I),
    re.compile(r'^\s*the\s+tool\s+(already\s+)?(sent|replied|posted|handled|acknowledged)\b', re.I),
    re.compile(r'^\s*(no|nothing)\s+(further|more|additional)?\s*(reply|response|message|output)\b', re.I),
    re.compile(r'^\s*(created|registered|executed)\s+(the|a|an)\s+\w+', re.I),
]

# Any digit, or a question mark, means the line carries real content for the
# customer (an amount, an account, a question) — never treat it as narration.
_HAS_REAL_CONTENT_RE = re.compile(r'[\d٠-٩]|[?؟]')

_PLACEHOLDER_WRAPPED_RE = re.compile(r'^[\(\[\{（].{0,40}[\)\]\}）]$', re.DOTALL)


def is_non_message(output: str) -> bool:
    """A bracketed placeholder «(لا رد)», or text with no letter in any script."""
    text = str(output or '').strip()
    if not text:
        return False
    if _PLACEHOLDER_WRAPPED_RE.match(text):
        return True
    if not any(ch.isalpha() for ch in text):
        return True
    return False


def is_self_narration(output: str) -> bool:
    """True when the ENTIRE message is the agent talking about its own actions."""
    text = str(output or '').strip()
    if not text:
        return False
    parts = [p.strip() for p in re.split(r'[.\n!]+', text) if p.strip()]
    if not parts:
        return False
    for part in parts:
        if _HAS_REAL_CONTENT_RE.search(part):
            return False
        if not any(rx.search(part) for rx in _NARRATION_RES):
            return False
    return True


# ── Duplicate suppression ────────────────────────────────────────────────────

_DUPLICATE_WINDOW = 30  # seconds


def _is_duplicate_send(conversation_id, text: str) -> bool:
    digest = hashlib.sha1(text.strip().encode('utf-8')).hexdigest()[:16]
    key = f'qurtoba:ai_sent:{conversation_id}:{digest}'
    try:
        # cache.add is SETNX: False means the same text went out moments ago.
        return not cache.add(key, 1, timeout=_DUPLICATE_WINDOW)
    except Exception:
        return False


# ── The gate ─────────────────────────────────────────────────────────────────

def _text_of(content) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        t = content.get('text')
        return t if isinstance(t, str) else None
    return None


def block_reason(content, message_type, conversation, system_partner) -> Optional[str]:
    """Why this send must be dropped, or None to let it through."""
    if in_system_send():
        return None
    if message_type != 'text':
        return None
    if not getattr(system_partner, 'ai_agent', False):
        return None
    text = _text_of(content)
    if not text or not text.strip():
        return None
    conv_id = getattr(conversation, 'id', None)

    if is_system_template_impersonation(text):
        return 'system_template'
    if conv_id and reply_already_delivered(conv_id):
        return 'reply_already_delivered'
    if is_non_message(text):
        return 'non_message'
    if is_self_narration(text):
        return 'self_narration'
    if conv_id and _is_duplicate_send(conv_id, text):
        return 'duplicate'
    return None


def install() -> bool:
    """Wrap WhatsAppAPIService.send_and_broadcast once. Safe to call repeatedly."""
    try:
        from modules.whatsapp.services.api import WhatsAppAPIService
    except Exception:
        logger.exception('ai_guard: WhatsAppAPIService unavailable; gate NOT installed')
        return False

    original = WhatsAppAPIService.send_and_broadcast
    if getattr(original, '_qurtoba_ai_guard', False):
        return True

    @wraps(original)
    def guarded(self, partner, content, *, message_type='text', conversation=None,
                system_partner=None, **kwargs):
        reason = None
        try:
            reason = block_reason(content, message_type, conversation, system_partner)
        except Exception:
            logger.exception('ai_guard: gate error — sending anyway')
        if reason:
            preview = (_text_of(content) or '')[:120].replace('\n', ' ⏎ ')
            conv_id = getattr(conversation, 'id', None)
            logger.warning('ai_guard: BLOCKED (%s) conv=%s text=%r', reason, conv_id, preview)
            return {
                'success': False,
                'blocked': True,
                'error': f'qurtoba_ai_guard:{reason}',
                'message_id': None,
                'chat_message_id': None,
                'conversation_id': str(conv_id) if conv_id else None,
            }
        return original(self, partner, content, message_type=message_type,
                        conversation=conversation, system_partner=system_partner, **kwargs)

    guarded._qurtoba_ai_guard = True
    guarded._qurtoba_original = original
    WhatsAppAPIService.send_and_broadcast = guarded
    logger.info('ai_guard: outbound gate installed on WhatsAppAPIService.send_and_broadcast')
    return True
