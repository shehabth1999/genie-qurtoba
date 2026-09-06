"""
qurtoba.ai_guard — the extension-owned outbound gate for AI replies.

Core carries no product-specific reply rules; this module owns all of them and
attaches to the ONE place every WhatsApp send goes through
(``WhatsAppAPIService.send_and_broadcast``), installed from ``QurtobaConfig.ready``.

It only ever looks at TEXT sends attributed to the AI partner that the extension
did not author itself. Everything the extension sends on purpose (the 👍 ack,
the balance, the statement, Cash-SYS notices, receipts) is wrapped in
``system_send()`` and passes through untouched.

The rules, in order — ``decide()`` returns the first one that fires:

  1. system_template         — the 👍/👍🏿 ack and the Cash-SYS outcome notices
                               are statements of fact about money; only the
                               system may send them (incident 2026-08-06).
  2. reply_already_delivered — a tool answered this turn (it said so via
                               ``mark_reply_delivered``) and this text is a
                               status claim about that delivery («Created. Stay
                               silent.» — conversation 13f58d64, 2026-08-29).
  3. non_message             — «(لا رد)», a lone «.»: nothing a customer could
                               read as a reply.
  4. non_arabic              — Latin-script text is the model thinking, not a
                               customer message.
  5. internal_note           — «(مش للعميل)», «internal only»: a note to the operator.
  6. echo_with_note          — the customer's last message repeated back with a
                               parenthetical.
  7. self_narration          — «تم التنفيذ», «Done.»: the agent reporting on itself.
  8. duplicate               — the identical text to the same conversation
                               within a few seconds (a re-run that already sent).
  9. duplicate_question (C)  — a second agent reply that QUOTES THE SAME inbound
                               message as an earlier agent reply within 90 s (a
                               restated question). Replies quoting different
                               inbounds — one per fault in a batch — all pass.
 10. unquoted_agent_text (A) — office rule 2026-09-05: the agent must answer
                               through the reply tool, never through its final
                               output. An AI text with no ``reply_to_id`` and no
                               ``reply_to_message_id`` is NEVER delivered as-is.
                               (B) If it passed every rule above, the newest
                               inbound is not a transaction message (no phone,
                               no amount) and no quoted agent reply has gone out
                               since that inbound, the gate FORWARDS it as a
                               quoted reply on that inbound so a courtesy turn
                               («السلام عليكم», «شكراً») is not left silent.
                               Otherwise it is dropped — on a transaction turn
                               the sweeper re-runs the abdicated message.

Rule 2 is turn-scoped, not time-scoped: the tool's mark counts only if no
inbound message arrived after it, so a lingering flag can never eat the answer
to the customer's NEXT message.

Contract (shared with the sandbox evaluation, which calls it directly)::

    decide(content, message_type, conversation, system_partner, *,
           reply_to_id=None, reply_to_message_id=None)
        -> {'action': 'send' | 'block' | 'forward',
            'reason': str | None,          # rule name; set for block AND forward
            'forward_to': Message | None}  # the inbound to quote, for forward
    block_reason(...same arguments...) -> str | None
        # the reason only when decide() says block; None for send AND forward,
        # so callers written against the old gate behave as before.

When in doubt about CONTENT the message is SENT; when in doubt about WHERE an
unquoted text belongs it is DROPPED (money must not be answered by guesswork).
Any exception inside the gate is logged and the send proceeds as written.
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
    # Third-person status about the system/turn, seen in the 2026-09-03 sandbox run:
    # «اليوم السابق كان فيه تحويل لنفس المبلغ … وفي انتظار رد العميل على سؤال التأكيد اللي النظام بعته»
    re.compile(r'(في\s+)?انتظار\s+رد'),
    re.compile(r'النظام\s+(بعت|أرسل|ارسل|هيبعت|هيرسل|رد)'),
    re.compile(r'^\s*(و)?اليوم\s+السابق'),
    re.compile(r'^\s*(الدور|التيرن|الجولة)\s+(خلص|انتهى|اكتمل)'),
    # «لا يوجد تأكيد جديد بعد سؤال التكرار. لا شيء لأكرره هذا الدور.» (sandbox E2)
    re.compile(r'^\s*(لا|مفيش|ما\s*فيش)\s+(يوجد|توجد|فيه)?\s*(تأكيد|رد|شيء|شئ|حاجة|جديد)'),
    re.compile(r'^\s*(لا|مفيش)\s+(شيء|شئ|حاجة)\s+(ل|أ|ا|ت)'),
    re.compile(r'(هذا|في\s+هذا|ده)\s+الدور\b'),
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


# After a tool already delivered the customer's answer (👍, balance, statement, a
# quoted question), the agent's own trailing text is redundant ONLY when it is a
# status claim about that delivery. Text that carries money content — a number,
# an amount, a question, an instruction — is a second, legitimate message (the
# rejection of a bad number in a mixed batch; the question about a rerouted
# remainder) and must go out. Sandbox evaluation 2026-09-03 caught the gate
# swallowing exactly those.
_SUCCESS_CLAIM_RE = re.compile(
    r'(اتنفذ|اتسجل|اتبعت|تم\s+(ال)?تنفيذ|تم\s+استلام|تم\s+(إنشاء|انشاء|تسجيل|إرسال|ارسال)|اكتمل|بنجاح|'
    r'لا\s+رد|no\s+reply|zero\s+(chars|characters|output)|created|posted|انتظار\s+رد|النظام\s+(بعت|أرسل|ارسل)|'
    r'nothing\s+(further|else|more)|silent|silence|auto-?ack|👍|✅)',
    re.I,
)
_ARABIC_RE = re.compile(r'[؀-ۿ]')
_LATIN_RE = re.compile(r'[A-Za-z]')


def is_non_arabic(output: str) -> bool:
    """Text written in Latin script — the agent's laws say Arabic only, so this is
    never a customer message (it is the model thinking in English). One quoted
    Arabic word inside an English sentence («…replies «تأكيد»») does not make it
    Arabic: Latin letters must not outnumber Arabic letters three to one."""
    text = str(output or '')
    latin = len(_LATIN_RE.findall(text))
    arabic = len(_ARABIC_RE.findall(text))
    if latin == 0:
        return False
    return arabic == 0 or latin > 3 * arabic


def is_redundant_after_tool_reply(output: str) -> bool:
    text = str(output or '').strip()
    if not text:
        return False
    if is_self_narration(text) or is_non_message(text) or is_non_arabic(text):
        return True
    if _HAS_REAL_CONTENT_RE.search(text):
        return False
    return bool(_SUCCESS_CLAIM_RE.search(text))


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


# ── Internal notes and echoes ────────────────────────────────────────────────
#
# 2026-09-03 13:22, conversation 13f58d64: the customer asked «الحساب كام» and
# the agent's whole reply was «الحساب كام (معلومة المدير، مش للعميل)» — the
# customer's own words echoed back with the model's private annotation (a
# paraphrase of the live-context comment marking the balance as internal).
# No narration pattern matched, it has letters, it is not a template — the
# gate let it through. Two narrow rules close that shape:
#   • a phrase that can only be a note to the operator, anywhere in the text;
#   • the customer's last inbound message repeated back with nothing after it
#     but a parenthetical.
# Both are blocked outright: neither can ever be a real answer.

_INTERNAL_NOTE_RE = re.compile(
    r'(مش\s+لل?عميل|معلوم[ةه]\s+المدير|للمدير\s+(فقط|بس)|ملاحظ[ةه]\s+داخلي[ةه]|'
    r'داخلي[ةه]?\s+(فقط|بس)|internal\s+(only|note|use)|\(\s*internal\s*\)|'
    r'not\s+for\s+the\s+customer|note\s+to\s+self|for\s+the\s+manager\s+only)',
    re.I,
)

_TRAILING_PAREN_RE = re.compile(r'^[\(（\[].{1,120}[\)）\]]$', re.DOTALL)


def is_internal_note(output: str) -> bool:
    """A phrase that only ever addresses the operator, never the customer."""
    text = str(output or '')
    return bool(text.strip()) and bool(_INTERNAL_NOTE_RE.search(text))


def _last_inbound_text(conversation_id) -> Optional[str]:
    try:
        from modules.chat.models import Message
        content = (
            Message.objects.filter(conversation_id=conversation_id, direction='inbound')
            .order_by('-created_at')
            .values_list('content', flat=True)
            .first()
        )
    except Exception:
        logger.exception('ai_guard: last-inbound lookup failed for %s', conversation_id)
        return None
    return _text_of(content)


def is_echo_with_note(output: str, conversation_id) -> bool:
    """The customer's last message repeated back, followed only by a parenthetical."""
    text = str(output or '').strip()
    if not text or not conversation_id:
        return False
    inbound = (_last_inbound_text(conversation_id) or '').strip()
    if len(inbound) < 3:
        return False
    norm_out = _normalize_for_match(text)
    norm_in = _normalize_for_match(inbound)
    if not norm_in or not norm_out.startswith(norm_in):
        return False
    rest = text[len(inbound):].strip() if text.startswith(inbound) else norm_out[len(norm_in):].strip()
    return bool(rest) and bool(_TRAILING_PAREN_RE.match(rest))


def is_pure_echo(output: str, conversation_id) -> bool:
    """The customer's last message repeated back verbatim — nothing else.

    2026-09-06: the customer wrote «حاضر ف الانتظار» and the model's turn ended with the
    same words, which the channel delivered as a reply. An echo is never an answer."""
    text = str(output or '').strip()
    if not text or not conversation_id:
        return False
    inbound = (_last_inbound_text(conversation_id) or '').strip()
    if len(inbound) < 3:
        return False
    return _dedupe_key_text(text) == _dedupe_key_text(inbound)


# ── Duplicate suppression ────────────────────────────────────────────────────

_DUPLICATE_WINDOW = 30  # seconds


_PUNCT_RE = re.compile(r'[\s\.\,،؛:;!\?؟\-—–_*"«»\(\)\[\]]+')


def _dedupe_key_text(text: str) -> str:
    """Punctuation-free form: «تم الإيقاف — النظام ينفّذ» and «تم الإيقاف. النظام ينفّذ»
    are the same message (the channel's dash normalisation rewrites one into the other)."""
    return _PUNCT_RE.sub('', _normalize_for_match(text))


def _is_duplicate_send(conversation_id, text: str) -> bool:
    digest = hashlib.sha1(_dedupe_key_text(text).encode('utf-8')).hexdigest()[:16]
    key = f'qurtoba:ai_sent:{conversation_id}:{digest}'
    try:
        # cache.add is SETNX: False means the same text went out moments ago.
        return not cache.add(key, 1, timeout=_DUPLICATE_WINDOW)
    except Exception:
        return False


# ── Quoted-reply bookkeeping (rules C and B) ─────────────────────────────────
#
# One agent reply per quoted inbound per turn. The model sometimes sends its
# question through the reply tool and then restates it in other words (sandbox
# 2026-09-03, J2: «…يتحول على نفس الرقم ده ولا رقم تاني؟» twice). Since
# 2026-09-05 every delivered agent text is a quoted reply, so the marker is keyed
# by the QUOTED inbound id: a second reply on the same inbound within the window
# is dropped, while per-fault replies quoting different inbounds all pass.
#
# A per-conversation "last quoted agent reply at" timestamp is kept alongside
# so rule B can tell whether the turn already produced a quoted reply without a
# database write — the sandbox evaluation calls decide() without sending.
_QUOTED_REPLY_WINDOW = 90  # seconds


def _quoted_key(conversation_id, quoted_id) -> str:
    return f'qurtoba:ai_quoted:{conversation_id}:{quoted_id}'


def _quoted_at_key(conversation_id) -> str:
    return f'qurtoba:ai_quoted_at:{conversation_id}'


def _note_quoted_reply(conversation_id, quoted_id) -> None:
    """An agent reply quoting ``quoted_id`` is going out now."""
    try:
        now = time.time()
        cache.set(_quoted_key(conversation_id, quoted_id), now, timeout=_QUOTED_REPLY_WINDOW)
        cache.set(_quoted_at_key(conversation_id), now, timeout=_QUOTED_REPLY_WINDOW)
    except Exception:
        pass


def _already_quoted(conversation_id, quoted_id) -> bool:
    try:
        return bool(cache.get(_quoted_key(conversation_id, quoted_id)))
    except Exception:
        return False


def _quoted_id_of(reply_to_id, reply_to_message_id) -> Optional[str]:
    """The chat Message id a send quotes, or None when it quotes nothing.

    The core reply tool passes both ids; a caller that passes only the WhatsApp
    id is still a quoted reply — resolve it, and fall back to the WhatsApp id
    itself as the marker key when the row is not ours."""
    if reply_to_id:
        return str(reply_to_id)
    if reply_to_message_id:
        try:
            from modules.chat.models import Message
            mid = (Message.objects_all.filter(social_id=str(reply_to_message_id))
                   .values_list('id', flat=True).first())
            return str(mid) if mid else str(reply_to_message_id)
        except Exception:
            return str(reply_to_message_id)
    return None


def _newest_inbound(conversation_id):
    """The conversation's most recent inbound chat Message (None when there is none)."""
    try:
        from modules.chat.models import Message
        return (
            Message.objects.filter(conversation_id=conversation_id, direction='inbound')
            .order_by('-created_at')
            .first()
        )
    except Exception:
        logger.exception('ai_guard: newest-inbound lookup failed for %s', conversation_id)
        return None


def _is_transaction_message(text: Optional[str]) -> bool:
    """A message carrying at least one phone or one amount is a transaction
    request — the planner's own classifier decides. Unreadable → treated as a
    transaction: forwarding a guessed text onto money is the worse failure."""
    if not text or not str(text).strip():
        return False
    try:
        from qurtoba.tools.planning import _classify_message
        cls = _classify_message(str(text))
        return bool(cls.get('phones')) or bool(cls.get('amounts'))
    except Exception:
        logger.exception('ai_guard: message classification failed')
        return True


def _quoted_reply_since(conversation_id, inbound) -> bool:
    """Has any quoted agent reply gone out since ``inbound`` arrived?

    Three sources, cheapest first: the per-inbound marker, the per-conversation
    "last quoted at" stamp, and the outbound rows themselves (a tool's quoted
    question sent inside ``system_send`` leaves no marker but IS the reply)."""
    if _already_quoted(conversation_id, str(inbound.id)):
        return True
    arrived = getattr(inbound, 'created_at', None)
    arrived_ts = arrived.timestamp() if arrived is not None else None
    try:
        last_quoted = cache.get(_quoted_at_key(conversation_id))
        if last_quoted and arrived_ts is not None and float(last_quoted) > arrived_ts:
            return True
    except Exception:
        pass
    if arrived is None:
        return False
    try:
        from modules.chat.models import Message
        return Message.objects.filter(
            conversation_id=conversation_id, direction='outbound',
            reply_to__isnull=False, created_at__gt=arrived,
        ).exists()
    except Exception:
        logger.exception('ai_guard: outbound lookup failed for %s', conversation_id)
        return True


# ── The gate ─────────────────────────────────────────────────────────────────

def _text_of(content) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        t = content.get('text')
        return t if isinstance(t, str) else None
    return None


def _send():
    return {'action': 'send', 'reason': None, 'forward_to': None}


def _block(reason: str, detail: Optional[str] = None):
    return {'action': 'block', 'reason': reason, 'forward_to': None, 'detail': detail}


def decide(content, message_type, conversation, system_partner, *,
           reply_to_id=None, reply_to_message_id=None) -> dict:
    """What to do with this send.

    Returns ``{'action', 'reason', 'forward_to'}`` (plus an informational
    ``'detail'`` on some blocks):

    * ``send``    — deliver as written.
    * ``block``   — drop; ``reason`` names the rule.
    * ``forward`` — an unquoted agent text that deserves delivery: send it as a
      quoted reply on ``forward_to`` (the newest inbound chat Message);
      ``reason`` is ``'unquoted_agent_text'``.

    The quoted-reply markers (rule C) are recorded HERE for send/forward — not
    in the wrapper — so the sandbox evaluation, which calls decide() without
    sending, sees exactly the production behaviour.
    """
    if in_system_send():
        return _send()
    if message_type != 'text':
        return _send()
    if not getattr(system_partner, 'ai_agent', False):
        return _send()
    text = _text_of(content)
    if not text or not text.strip():
        return _send()
    conv_id = getattr(conversation, 'id', None)

    # ── Content rules: what the text IS, whoever it quotes ──────────────────
    if is_system_template_impersonation(text):
        return _block('system_template')
    if conv_id and reply_already_delivered(conv_id) and is_redundant_after_tool_reply(text):
        return _block('reply_already_delivered')
    if is_non_message(text):
        return _block('non_message')
    if is_non_arabic(text):
        return _block('non_arabic')
    if is_internal_note(text):
        return _block('internal_note')
    if conv_id and is_echo_with_note(text, conv_id):
        return _block('echo_with_note')
    if conv_id and is_pure_echo(text, conv_id):
        return _block('echo')
    if is_self_narration(text):
        return _block('self_narration')
    if conv_id and _is_duplicate_send(conv_id, text):
        return _block('duplicate')

    # ── Rule C: a quoted reply — one per quoted inbound per turn ────────────
    quoted_id = _quoted_id_of(reply_to_id, reply_to_message_id)
    if quoted_id:
        if conv_id and _already_quoted(conv_id, quoted_id):
            return _block('duplicate_question')
        if conv_id:
            _note_quoted_reply(conv_id, quoted_id)
        return _send()

    # ── Rules A/B: unquoted agent text is never delivered as written ────────
    if not conv_id:
        return _block('unquoted_agent_text', 'no_conversation')
    inbound = _newest_inbound(conv_id)
    if inbound is None:
        return _block('unquoted_agent_text', 'no_inbound')
    if _is_transaction_message(_text_of(getattr(inbound, 'content', None))):
        return _block('unquoted_agent_text', 'transaction_turn')
    if reply_already_delivered(conv_id):
        # A tool (balance, statement, static reply, quoted question) already
        # answered this inbound; a trailing courtesy line («حاضر يا فندم 👋»)
        # is filler, never the reply — sandbox 2026-09-05, scenario K1.
        return _block('unquoted_agent_text', 'tool_replied')
    if _quoted_reply_since(conv_id, inbound):
        return _block('unquoted_agent_text', 'already_replied')
    _note_quoted_reply(conv_id, str(inbound.id))
    return {'action': 'forward', 'reason': 'unquoted_agent_text', 'forward_to': inbound}


def block_reason(content, message_type, conversation, system_partner, *,
                 reply_to_id=None, reply_to_message_id=None) -> Optional[str]:
    """Compatibility view of decide(): the reason when it says block, else None.

    A ``forward`` decision returns None — callers written against the old gate
    treat that as "let it through", which is what forwarding is from their
    point of view. New callers use decide() and act on ``forward_to``.
    """
    verdict = decide(content, message_type, conversation, system_partner,
                     reply_to_id=reply_to_id, reply_to_message_id=reply_to_message_id)
    return verdict['reason'] if verdict['action'] == 'block' else None


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
        verdict = _send()
        try:
            verdict = decide(content, message_type, conversation, system_partner,
                             reply_to_id=kwargs.get('reply_to_id'),
                             reply_to_message_id=kwargs.get('reply_to_message_id'))
        except Exception:
            logger.exception('ai_guard: gate error — sending anyway')
        conv_id = getattr(conversation, 'id', None)
        action = verdict.get('action')

        if action == 'block':
            reason = verdict.get('reason') or 'blocked'
            preview = (_text_of(content) or '')[:120].replace('\n', ' ⏎ ')
            logger.warning('ai_guard: BLOCKED (%s%s) conv=%s text=%r', reason,
                           f'/{verdict["detail"]}' if verdict.get('detail') else '',
                           conv_id, preview)
            return {
                'success': False,
                'blocked': True,
                'error': f'qurtoba_ai_guard:{reason}',
                'message_id': None,
                'chat_message_id': None,
                'conversation_id': str(conv_id) if conv_id else None,
            }

        if action == 'forward':
            inbound = verdict.get('forward_to')
            social_id = getattr(inbound, 'social_id', None)
            preview = (_text_of(content) or '')[:120].replace('\n', ' ⏎ ')
            if inbound is not None and social_id:
                kwargs['reply_to_message_id'] = social_id
                kwargs['reply_to_id'] = str(inbound.id)
                logger.warning('ai_guard: FORWARDED as quote conv=%s on=%s text=%r',
                               conv_id, inbound.id, preview)
            else:
                # The inbound has no WhatsApp id (nothing to quote on the wire):
                # deliver it plain rather than leave the courtesy turn silent.
                logger.warning('ai_guard: FORWARDED plain (inbound %s has no social_id) conv=%s text=%r',
                               getattr(inbound, 'id', None), conv_id, preview)
            result = original(self, partner, content, message_type=message_type,
                              conversation=conversation, system_partner=system_partner, **kwargs)
            if conversation is not None and not (isinstance(result, dict) and result.get('success') is False):
                mark_reply_delivered(conversation)
            return result

        return original(self, partner, content, message_type=message_type,
                        conversation=conversation, system_partner=system_partner, **kwargs)

    guarded._qurtoba_ai_guard = True
    guarded._qurtoba_original = original
    WhatsAppAPIService.send_and_broadcast = guarded
    logger.info('ai_guard: outbound gate installed on WhatsAppAPIService.send_and_broadcast')
    return True
