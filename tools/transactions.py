"""
Qurtoba transaction tools for AI Studio agents.

Tools exposed:
  * qurtoba_create_new_transactions_bulk   — record one OR many new debts (عملية جديدة)
  * qurtoba_register_customer_payment      — record a customer payment (سداد) [SENSITIVE]

Both resolve the target QurtobaCustomer from the active WhatsApp/Messenger
conversation: `conversation.social_partner.qurtoba_customer`.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from modules.aistudio.tools import tool

from qurtoba.tools._amounts import normalize_amount, _ar_to_ascii

logger = logging.getLogger(__name__)


def _react_created_on_source(conversation, source_message_id, emoji='👍') -> None:
    """React (👍) on the customer's PHONE-NUMBER message once its transfer is really CREATED.

    This is IN ADDITION to the batch 👍 text ack — a per-transaction confirmation reacting
    directly on the number the customer sent, fired only on a genuine create (never on
    pending/duplicate/reject). Best-effort: needs the source message's WhatsApp id (social_id)
    and the account's send service; silently no-ops if either is missing, and never raises
    into the create flow. Also records the reaction so the internal chat UI shows it."""
    try:
        if conversation is None or not source_message_id:
            return
        from modules.chat.models import Message as ChatMessage, MessageReaction
        msg = ChatMessage.objects_all.filter(id=str(source_message_id).strip()).first()
        wamid = getattr(msg, 'social_id', None) if msg is not None else None
        if not wamid:
            return  # no WhatsApp message id → can't react (the batch text 👍 still fires)
        account = getattr(conversation, 'social_account', None)
        partner = getattr(conversation, 'social_partner', None)
        svc = getattr(account, 'service', None) if account is not None else None
        phone = getattr(partner, 'phone', None) if partner is not None else None
        if svc is None or not phone or not hasattr(svc, 'send_reaction'):
            return
        # Record for the internal chat UI (best-effort), then send to WhatsApp.
        reaction = None
        try:
            from qurtoba.extensions import _get_system_partner
            reaction, _ = MessageReaction.objects.get_or_create(
                message=msg, user=_get_system_partner(conversation),
                emoji=emoji, direction='outbound',
            )
        except Exception:
            pass
        resp = svc.send_reaction(phone, wamid, emoji)
        try:
            rid = resp.get('message_id') if isinstance(resp, dict) else None
            if reaction is not None and rid:
                MessageReaction.objects.filter(id=reaction.id).update(social_id=rid)
        except Exception:
            pass
    except Exception:
        logger.warning('qurtoba: source-message reaction failed', exc_info=True)


def _send_start_ack(conversation) -> None:
    """
    Send an instant 👍 to the chat the moment we begin creating a real transaction
    record — a "received, I'm handling this now" signal, sent BEFORE the slower
    push/cash-sys work. Best-effort; never raises into the tool flow.

    Sent ONCE per tool call (the caller guards with an ack_state flag) and ONLY
    when an actual record/pending is about to be written — never on a rejection,
    so the partner never sees 👍 followed by a problem message.
    """
    try:
        if conversation is None:
            return
        account = getattr(conversation, 'social_account', None)
        partner = getattr(conversation, 'social_partner', None)
        svc = getattr(account, 'service', None) if account is not None else None
        if svc is None or partner is None:
            return
        from qurtoba.extensions import _get_system_partner
        svc.send_and_broadcast(
            partner=partner,
            content='👍',
            message_type='text',
            conversation=conversation,
            system_partner=_get_system_partner(conversation),
            websocket=True,
        )
    except Exception:
        logger.warning('qurtoba: start-ack 👍 failed', exc_info=True)


def _send_quoted_text(conversation, social_partner, src_message_id, text) -> bool:
    """
    Send `text` to the chat as a reply that QUOTES the inbound message src_message_id
    (resolved to WAMID/social_id + local id) — done by the TOOL, via the omnichannel
    service, as the system partner. If the message can't be resolved it still sends,
    just unquoted. Best-effort; never raises. Returns True if a message was dispatched.
    """
    try:
        if conversation is None or social_partner is None or not text:
            return False
        from modules.chat.services.omnichannel_send_service import OmnichannelSendService
        from qurtoba.extensions import _get_system_partner

        reply_wamid = None
        reply_local_id = None
        if src_message_id:
            try:
                from modules.chat.models import Message as ChatMessage
                msg = (ChatMessage.objects_all
                       .filter(id=str(src_message_id).strip(), conversation=conversation)
                       .first())
                if msg is not None:
                    reply_wamid = getattr(msg, 'social_id', None)
                    reply_local_id = msg.id
            except Exception:
                logger.warning('qurtoba: could not resolve src message %s for quoted reply',
                               src_message_id, exc_info=True)

        OmnichannelSendService().send_and_broadcast(
            partner=social_partner,
            content={'text': str(text)},
            message_type='text',
            conversation=conversation,
            system_partner=_get_system_partner(conversation),
            reply_to_message_id=reply_wamid,
            reply_to_id=reply_local_id,
            websocket=True,
        )
        return True
    except Exception:
        logger.warning('qurtoba: quoted reply failed', exc_info=True)
        return False


def _send_account_correction_reply(conversation, social_partner, src_message_id, corrected_number) -> bool:
    """
    Reply the corrected destination number to the chat — done by the TOOL, never the
    LLM, quoting the message the partner typed the number in. Best-effort.
    Returns True if a message was dispatched.
    """
    if not corrected_number:
        return False
    sent = _send_quoted_text(conversation, social_partner, src_message_id, str(corrected_number))
    if sent:
        logger.info('qurtoba: tool sent account-correction reply "%s"', corrected_number)
    return sent


# ---------------------------------------------------------------------------
# Type catalogs (mirrors QurtobaRecord.DEBT_TYPES / COLLECTION_TYPES)
# ---------------------------------------------------------------------------

DEBT_TYPES = ['كاش', 'كاش(5)', 'كاش(10)', 'كاش(20)', 'فورى', 'أمان', 'طاير', 'مصاريف خدمه']
CASH_TYPES = {'كاش', 'كاش(5)', 'كاش(10)', 'كاش(20)'}
PAYMENT_TYPES = ['شراء كاش', 'شراء فورى']  # سداد types only — تحصيل is collector-only
# Our fixed Fawry collection account. Every Fawry payment (شراء فورى) MUST go to this
# account — the agent rejects a Fawry receipt whose رقم الحساب differs and tells the
# customer to transfer to this number. Enforced here too so the stored record is consistent.
FAWRY_PAYMENT_ACCOUNT = '2697418'


def _check_type_allowed_for_account(conversation, effective_type: str):
    """
    Look up the per-WhatsApp-account toggle for `effective_type`.

    Returns (allowed: bool, message: Optional[str]).
    - allowed=True, message=None when the type is enabled OR when the social
      account is not a WhatsApp account (toggles only exist on WhatsApp).
    - allowed=False, message=<Arabic standard reply> when the admin disabled it.
    """
    from qurtoba.extensions import QURTOBA_TYPE_FLAG_MAP

    flag = QURTOBA_TYPE_FLAG_MAP.get(effective_type)
    if not flag:
        return True, None  # type is not gated (e.g. payment types)

    account = getattr(conversation, 'social_account', None) if conversation else None
    if account is None:
        return True, None

    # Only WhatsApp accounts carry these flags; for other channels assume allowed.
    if account._meta.label_lower != 'whatsapp.whatsappaccount':
        return True, None

    if getattr(account, flag, True):
        return True, None

    return False, (
        f'الخدمة {effective_type} متوقفة حالياً، برجاء المحاولة في وقت لاحق '
        f'وسيتم إبلاغك عند توفرها.'
    )


def _normalize_cash_bracket(type: str, amount: float) -> str:
    """
    The four كاش variants are amount BRACKETS (filters), not commissions:
      - كاش       : amount  < 10,000
      - كاش(10)   : 10,000 ≤ amount < 20,000
      - كاش(20)   : amount ≥ 20,000
      - كاش(5)    : reserved / unused for now
    If the caller passes ANY cash variant we promote it to the correct bracket
    based on the amount, so the AI can always just send type='كاش'.
    """
    if type not in CASH_TYPES:
        return type
    if amount >= 20000:
        return 'كاش(20)'
    if amount >= 10000:
        return 'كاش(10)'
    return 'كاش'


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_conversation_and_customer(context):
    """Return (conversation, customer, error_dict_or_None)."""
    conv = getattr(context, 'conversation', None)
    partner = getattr(context, 'partner', None)
    if partner is None and conv is not None:
        partner = getattr(conv, 'social_partner', None)

    if conv is None or partner is None:
        return None, None, {
            'success': False,
            'error': 'No active conversation in context. This tool requires a live customer chat.',
            'error_type': 'no_conversation',
        }

    customer = getattr(partner, 'qurtoba_customer', None)
    if customer is None:
        return conv, None, {
            'success': False,
            'error': 'The current chat partner is not linked to any Qurtoba customer.',
            'error_type': 'partner_not_linked',
            'partner_name': getattr(partner, 'name', None),
            'partner_phone': getattr(partner, 'phone', None),
        }
    return conv, customer, None


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


def _message_text_raw(msg) -> str:
    """A chat Message's text + caption + transcription, joined VERBATIM (no transliteration)."""
    content = getattr(msg, 'content', None)
    parts = []
    if isinstance(content, dict):
        if isinstance(content.get('text'), str):
            parts.append(content['text'])
        att = content.get('attachment')
        if isinstance(att, dict):
            for k in ('caption', 'filename', 'name'):
                if isinstance(att.get(k), str):
                    parts.append(att[k])
        if isinstance(content.get('transcription'), str):
            parts.append(content['transcription'])
    elif isinstance(content, str):
        parts.append(content)
    return ' '.join(parts)


def _message_text_ascii(msg) -> str:
    """Like _message_text_raw but with Arabic-Indic digits transliterated to ASCII (so a phone
    written in Arabic numerals or with separators still matches on digit presence)."""
    return _ar_to_ascii(_message_text_raw(msg))


def _message_digits(msg) -> str:
    """All ASCII digits contained in a chat Message (text + caption + transcription)."""
    return ''.join(ch for ch in _message_text_ascii(msg) if ch.isdigit())


def _account_as_written_differs(final_account, msg) -> bool:
    """True when the canonical number (01XXXXXXXXX) is NOT how the customer literally typed it
    in `msg` — i.e. normalization changed it (spaces / +20 / 0020 / dashes / missing leading 0),
    so the correction is worth confirming back.

    This is the RELIABLE correction signal: the LLM often hands the tool an already-cleaned
    number, so comparing the tool's input to the canonical form misses real corrections. We
    instead compare the canonical form to how the number appears in the partner's own message.
    Returns False when the canonical form appears verbatim, when msg is missing, or when the
    number isn't in the message at all.
    """
    if msg is None or not final_account:
        return False
    raw = _message_text_raw(msg)
    if not raw:
        return False
    # Verbatim check on the RAW text with ASCII-ONLY digit runs ([0-9], so Arabic-Indic digits are
    # NOT counted): the partner typed the canonical number only if it appears as a clean ASCII run.
    #  - «٠١٠٢٥٢٩٤٥٩٤» (Arabic) → no ASCII run → counts as a correction (send the English number).
    #  - «+201006000100» → the ASCII run is «201006000100», not «01006000100» → a correction.
    #  - «01025294594» (clean ASCII) → matches → NOT a correction.
    if final_account in re.findall(r'[0-9]+', raw):
        return False
    # Otherwise confirm it IS the same number (subscriber tail present once Arabic digits are
    # transliterated) — written differently (Arabic numerals / code / spaces / dashes).
    subscriber = final_account[1:] if final_account.startswith('0') else final_account
    digits = ''.join(ch for ch in _ar_to_ascii(raw) if ch.isdigit())
    return bool(subscriber and subscriber in digits)


def _find_message_with_account(final_account, conversation, *, limit=50):
    """Locate the partner's most-recent inbound message that literally contains this
    destination number — in ANY written form (ASCII / Arabic-Indic / +20 / 0020 / spaces /
    dashes). This is the TOOL's OWN correction control: the LLM frequently hands us an
    already-cleaned number AND no usable source_message_id, which blinds the input-vs-canonical
    fallback (both are already clean → no correction ever fires). Searching the customer's
    actual message restores the signal regardless of what the LLM did. Returns the Message
    or None.
    """
    if not final_account or conversation is None:
        return None
    subscriber = final_account[1:] if final_account.startswith('0') else final_account
    if not subscriber:
        return None
    try:
        from modules.chat.models import Message as ChatMessage
        rows = (ChatMessage.objects_all
                .filter(conversation=conversation, direction='inbound', active=True, type='text')
                .order_by('-created_at')[:limit])
    except Exception:
        return None
    for m in rows:
        # _message_digits transliterates Arabic-Indic digits, so the 10-digit subscriber
        # tail is found whether the customer typed 01…, +2010…, ٠١٠…, or with separators.
        if subscriber in _message_digits(m):
            return m
    return None


def _source_message_matches_account(source_message_id, final_account, conversation) -> str:
    """Validate that the cited source message actually contains the destination phone.

    Returns one of:
      * 'ok'         — the phone (its 10-digit subscriber tail) appears in the message.
      * 'mismatch'   — the cited message does NOT contain that phone (the 4125 class:
                       the agent cited the amount/name message, not the number message).
      * 'unverified' — no id given, or the message can't be resolved. Never reject on
                       'unverified' alone — only an *actively wrong* citation is rejected.
    """
    if not source_message_id or not final_account:
        return 'unverified'
    try:
        from modules.chat.models import Message as ChatMessage
        qs = ChatMessage.objects_all.filter(id=str(source_message_id).strip())
        if conversation is not None:
            qs = qs.filter(conversation=conversation)
        msg = qs.first()
    except Exception:
        return 'unverified'
    if msg is None:
        return 'unverified'
    digits = _message_digits(msg)
    subscriber = final_account[1:] if final_account.startswith('0') else final_account
    if subscriber and subscriber in digits:
        return 'ok'
    return 'mismatch'


def _validate_debt_item(
    type: str,
    value: Any,
    account_number: Optional[str],
) -> Dict[str, Any]:
    """Validate a single debt-item payload. Returns {'ok': True, ...} or {'ok': False, 'error_type': ..., 'error': ...}."""
    if type not in DEBT_TYPES:
        return {
            'ok': False,
            'error_type': 'invalid_type',
            'error': f"النوع غير صحيح: '{type}'. الأنواع المسموحة: {DEBT_TYPES}",
        }
    # Deterministic value normalization (Arabic/ASCII digits, `.`/`,` thousands
    # vs decimal, word multipliers like "20الف"). This is the single source of
    # truth for the amount — the agent no longer normalizes in its head.
    norm = normalize_amount(value)
    if not norm['ok']:
        return {'ok': False, 'error_type': 'invalid_value', 'error': 'المبلغ يجب أن يكون رقم موجب.'}
    amount = norm['value']
    # Egypt has NO fractional amounts and the minimum transfer is 1 EGP. The planner
    # already drops non-integers, but the LLM can call create DIRECTLY with a value it
    # misread off a receipt/tally (13.75, 0.5) or a non-finite number — reject those
    # here so a fractional / NaN / sub-1 amount can never become a real transfer.
    import math as _math
    if not _math.isfinite(amount) or amount != int(amount):
        return {'ok': False, 'error_type': 'invalid_value',
                'error': 'المبلغ لازم يكون رقم صحيح بدون كسور.'}
    if amount < 1:
        return {'ok': False, 'error_type': 'invalid_value', 'error': 'أقل مبلغ للتحويل جنيه واحد.'}

    is_cash = type in CASH_TYPES
    # Promote any cash variant to the correct bracket based on amount
    effective_type = _normalize_cash_bracket(type, amount) if is_cash else type
    final_account = _normalize_phone(account_number) if is_cash else (account_number or None)
    if is_cash:
        if not final_account or not final_account.startswith('01') or len(final_account) != 11:
            return {
                'ok': False,
                'error_type': 'invalid_account_number',
                'error': 'من فضلك ارسل رقم صحيح.',
            }
    return {
        'ok': True,
        'amount': amount,
        'final_account': final_account,
        'is_cash': is_cash,
        'effective_type': effective_type,
    }


def _create_one_debt(
    customer,
    social_partner,
    type: str,
    amount: float,
    final_account: Optional[str],
    is_cash: bool,
    notes: Optional[str],
    override_grade_limit: bool,
    conversation=None,
    source_message_id: Optional[str] = None,
    account_input: Optional[str] = None,
    ack_state: Optional[dict] = None,
    consumed_message_ids: Optional[List[str]] = None,
    confirm_repeat: bool = False,
) -> Dict[str, Any]:
    """
    Create a single debt record after validation.

    Enforces:
      - per-account service toggle (WhatsAppAccountQurtobaExtension)
      - grade limit (grade × 1000) — unless override_grade_limit=True
    """
    # مصاريف خدمه is AUTO-recorded by the Cash-SYS background service from each
    # transfer's fee — the agent must never create it manually.
    if type == 'مصاريف خدمه':
        return {
            'success': False,
            'error_type': 'auto_only',
            'error': 'مصاريف الخدمة تُضاف تلقائياً من النظام — لا تُسجَّل يدوياً.',
        }

    src = (str(source_message_id).strip() if source_message_id else None)

    # Did normalization change the destination number the partner typed?
    # (e.g. "00201038857982" -> "01038857982", or "010 06000 100" -> "01006000100").
    # Detect against the PARTNER'S MESSAGE, not the tool input — the LLM usually hands us an
    # already-cleaned number, so comparing input vs canonical misses the change. We compare the
    # canonical number to how it literally appears in the source message; fall back to the raw
    # tool input only when the message can't be resolved. Used to confirm the correction back.
    account_corrected = False
    _raw_in = (account_input or '').strip()
    # Message id to QUOTE in the correction reply. Starts as the LLM's cited id, but the TOOL
    # overrides it with the message it finds itself — so the quote is right even when the LLM
    # gave no (usable) source id.
    correction_src_id = src
    if is_cash and final_account:
        _src_msg = None
        if src:
            try:
                from modules.chat.models import Message as ChatMessage
                _src_msg = ChatMessage.objects_all.filter(id=src, conversation=conversation).first()
            except Exception:
                _src_msg = None
        if _src_msg is None:
            # The LLM pre-cleaned the number and/or gave no usable source id — find the
            # partner's own message so the TOOL (never the LLM) decides AND fires the
            # correction, and so we quote the message the partner actually typed it in.
            _src_msg = _find_message_with_account(final_account, conversation)
            if _src_msg is not None:
                correction_src_id = str(_src_msg.id)
        if _src_msg is not None:
            account_corrected = _account_as_written_differs(final_account, _src_msg)
        else:
            account_corrected = bool(_raw_in and _raw_in != final_account)

    # Pre-check: is this type enabled on the current WhatsApp account?
    allowed, disabled_msg = _check_type_allowed_for_account(conversation, type)
    if not allowed:
        return {
            'success': False,
            'error_type': 'service_disabled',
            'error': disabled_msg,
            'disabled_type': type,
        }

    # --- Source-message validation (B2) ---------------------------------------
    # The cited message MUST contain the destination phone. This catches the
    # mislink class where the agent pointed at the amount/name message instead of
    # the number message (the record-4125 wrong-value cascade). Skipped on
    # admin-approved overrides (the id may be stale) and for non-cash types.
    if is_cash and not override_grade_limit:
        verdict = _source_message_matches_account(src, final_account, conversation)
        if verdict == 'mismatch':
            return {
                'success': False,
                'error_type': 'source_mismatch',
                'error': 'الرسالة المُشار إليها لا تحتوي رقم الحساب — راجِع ربط الرقم بالمبلغ.',
                'expected_account': final_account,
                'cited_message_id': src,
            }
        source_unverified = verdict == 'unverified'
    else:
        source_unverified = bool(is_cash and not src)

    # --- Idempotency durable check (B4) ---------------------------------------
    # A record (or a pending-review row) already created from THIS message for
    # THIS account+amount means we re-ran on the full conversation context — do
    # not create a second transaction. Scoped to agent calls only: the admin
    # grade-limit approval path (override_grade_limit=True) is creating the record
    # FROM a still-'pending' row and has its own double-create guard, so skip here.
    conversation_uuid = str(getattr(conversation, 'id', '') or '') or None
    if src and not override_grade_limit:
        from qurtoba.models import QurtobaRecord, QurtobaPendingTransaction
        dup_rec = QurtobaRecord.objects.filter(
            origin_message_id=src, account_number=final_account, value=amount,
        ).order_by('-id').first()
        if dup_rec is not None:
            return {
                'success': True, 'duplicate': True, 'record_id': dup_rec.pk,
                'type': dup_rec.type, 'value': dup_rec.value,
                'account_number': dup_rec.account_number,
            }
        dup_pend = QurtobaPendingTransaction.objects.filter(
            source_message_id=src, account_number=final_account, value=amount,
            review_state='pending',
        ).order_by('-id').first()
        if dup_pend is not None:
            return {
                'success': True, 'duplicate': True, 'pending_review': True,
                'pending_id': dup_pend.pk, 'type': dup_pend.type,
                'value': dup_pend.value, 'account_number': dup_pend.account_number,
            }

    # --- Same-day cash repeat check (B5) ---------------------------------------
    # B4 above only catches an EXACT re-run of the SAME message (same source_message_id).
    # It misses the far more common real case: the customer resends the transfer as a
    # brand-new message (new id) minutes later. That must not be silently re-created NOR
    # silently dropped — the agent needs a DETERMINISTIC signal, not its own judgment call
    # (relying on the LLM to "notice" a repeat has failed in practice). Deliberately narrow:
    # كاش family only (كاش/كاش(10)/كاش(20)/كاش(5) — never فورى/أمان/طاير, per product decision),
    # same account_number + value, and STRICTLY today's calendar day in local time — a match
    # from yesterday or earlier must NEVER be flagged, no matter how identical the values are.
    # Bypassed by confirm_repeat=True: the agent sets this ONLY after the customer explicitly
    # confirmed «تأكيد تكرار العملية؟» — then this same call proceeds to create for real.
    if is_cash and final_account and not override_grade_limit and not confirm_repeat:
        from qurtoba.models import QurtobaRecord
        from django.utils import timezone as _tz
        _now_local = _tz.localtime(_tz.now())
        _day_start = _now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        dup_today = QurtobaRecord.objects.filter(
            customer=customer, type__startswith='كاش',
            account_number=final_account, value=amount,
            created_at__gte=_day_start,
        ).order_by('-id').first()
        if dup_today is not None:
            return {
                'success': True, 'same_day_duplicate': True,
                'existing_record_id': dup_today.pk, 'type': dup_today.type,
                'value': dup_today.value, 'account_number': dup_today.account_number,
                'created_at': dup_today.created_at.isoformat(),
            }

    # We're past all rejection gates — a real record (or a review-pending row) is
    # about to be written. Send the instant "received, handling now" 👍 ONCE per
    # tool call, here, so the partner never sees 👍 before a rejection.
    if ack_state is not None and not ack_state.get('sent'):
        _send_start_ack(conversation)
        ack_state['sent'] = True

    # Pre-check grade limit before any write
    customer.refresh_from_db(fields=['balance', 'grade'])
    current_balance = customer.balance or 0
    grade_limit = (customer.grade or 0) * 1000
    projected = current_balance + amount

    # Grade limit is the credit ceiling (grade × 1000). When grade is 0 or null
    # the ceiling is 0 — the customer has NO credit allowance, so ANY transaction
    # that leaves them owing (projected balance > 0) MUST go to review and is
    # NEVER created directly. It is created directly only when existing credit
    # already covers it (projected balance <= the ceiling).
    #   e.g. limit 0, balance -50000 (credit), asks 60000 → projected 10000 > 0 → review.
    #        limit 0, balance -50000 (credit), asks 30000 → projected -20000 <= 0 → create.
    if projected > grade_limit and not override_grade_limit:
        # Grade-limit overflow → don't reject, queue it for admin review.
        # No QurtobaRecord is created here. An admin will approve or deny from
        # the pending-review form view; on approve, _create_one_debt is called
        # again with override_grade_limit=True so this branch is skipped.
        try:
            from qurtoba.models import QurtobaPendingTransaction
            from qurtoba.extensions import _notify_all_users_pending
            conversation_uuid = None
            if conversation is not None:
                conversation_uuid = str(getattr(conversation, 'id', '') or '') or None
            pending = QurtobaPendingTransaction.objects.create(
                customer=customer,
                partner=social_partner,
                conversation_uuid=conversation_uuid,
                source_message_id=(str(source_message_id).strip() if source_message_id else None),
                type=type,
                value=amount,
                account_number=final_account,
                notes=notes or None,
                reason='grade_limit_exceeded',
                review_state='pending',
            )
            _notify_all_users_pending(pending, kind='transaction')
            # Confirm the corrected number now: the transaction WILL be created on
            # approval, so the customer should see the canonical number immediately.
            # (The approval path itself never re-sends — it passes no account_input,
            # so account_corrected is false there.)
            if account_corrected:
                _send_account_correction_reply(conversation, social_partner, correction_src_id, final_account)
            # React on the phone message here TOO, but with a DARK 👍🏿 (dark-skin-tone thumbs up)
            # instead of the normal 👍 used on a genuine create — so the team can tell at a glance
            # which numbers went to REVIEW (pending) vs were created outright. Every accepted
            # transfer still gets a reaction (no bare/missing one), just a different shade.
            _react_created_on_source(conversation, src, '👍🏿')
            return {
                'success': True,
                'pending_review': True,
                'pending_id': pending.pk,
                'type': pending.type,
                'value': pending.value,
                'account_number': pending.account_number,
                'account_corrected': account_corrected,
                'account_input': _raw_in or None,
                'pending_qurtoba_push': False,
                'pending_cash_sys': False,
            }
        except Exception as exc:
            # If pending creation itself fails, fall back to the old hard-reject
            # so the agent has a deterministic answer instead of silently dropping.
            return {
                'success': False,
                'error_type': 'pending_create_failed',
                'error': 'تعذّر تسجيل الطلب للمراجعة — حاول لاحقاً.',
                'debug': str(exc),
            }

    from qurtoba.models import QurtobaRecord
    from django.core.cache import cache

    # SETNX claim (B4): an atomic in-flight lock closing the concurrent-delivery
    # race that the durable check above can't (both deliveries read "no record"
    # before either writes). Mirrors tasks.py::_claim_event. Released on failure.
    claim_key = (
        f'qurtoba:txn:claim:{conversation_uuid}:{src}:{final_account}:{amount:.2f}'
        if src else
        f'qurtoba:txn:claim:{conversation_uuid}:{final_account}:{amount:.2f}:{type}'
    )
    # A human-CONFIRMED repeat («تأكيد تكرار العملية؟» → yes) is a deliberate second
    # transaction. Give it a distinct claim so it can't be swallowed by the FIRST create's
    # still-live 900 s in-flight lock (the src-less path shares one key) — otherwise the
    # confirmed repeat silently returns duplicate_in_flight and never creates.
    if confirm_repeat:
        claim_key += ':cr'
    if not cache.add(claim_key, 1, 900):
        return {
            'success': True, 'duplicate': True, 'record_id': None,
            'type': type, 'value': amount, 'account_number': final_account,
            'note': 'duplicate_in_flight',
        }
    try:
        record = QurtobaRecord.objects.create(
            customer=customer,
            type=type,
            value=amount,
            account_number=final_account,
            is_down=False,
            is_seller=False,
            partner=social_partner,
            notes=(notes or '')[:100] or None,
        )
    except Exception as e:
        cache.delete(claim_key)  # release so a legitimate retry can re-claim
        return {'success': False, 'error_type': 'create_failed', 'error': str(e)}

    # Link the inbound chat message that triggered this transaction, so the
    # execution receipt can later reply-quote it and the status tool can find
    # this record from that message. Safe no-op if the id is missing/unknown.
    if source_message_id:
        try:
            record.set_origin_message_from_uuid(source_message_id, conversation)
        except Exception:
            pass
    # Watermark (B4): mark EVERY inbound message this transaction consumed — the
    # phone message AND (for a split pair) the amount message — so a re-run never
    # re-actions them AND a consumed amount can't linger 'unprocessed' to leak into
    # the next burst's positional pairing (the 8310/13600 mis-route class).
    # consumed_message_ids comes from the authoritative pairing (consumed_ids_by_source).
    try:
        from modules.chat.models import Message as ChatMessage
        wm_ids = {x for x in ([src] + list(consumed_message_ids or [])) if x}
        if wm_ids:
            for _msg in ChatMessage.objects_all.filter(id__in=wm_ids):
                _msg.mark_ai_consumed(record)
    except Exception:
        pass

    # React 👍 directly on the customer's phone-number message now that this transfer is
    # really CREATED (this is the genuine-create path — pending/duplicate/reject returned
    # earlier and never reach here). This is IN ADDITION to the batch 👍 text ack.
    _react_created_on_source(conversation, src, '👍')

    # Number correction is confirmed by the TOOL itself (never the LLM): when
    # normalization changed the destination number, reply the corrected number —
    # quoting the message the partner typed it in. Deterministic, and fires ONLY
    # here, on a real transaction being created (not on pending-review / duplicates).
    if account_corrected:
        _send_account_correction_reply(conversation, social_partner, correction_src_id, final_account)

    customer.refresh_from_db(fields=['balance'])
    new_balance = customer.balance or 0
    over_by = max(0.0, new_balance - grade_limit) if grade_limit else 0.0

    return {
        'success': True,
        'record_id': record.pk,
        'type': record.type,
        'value': record.value,
        'account_number': record.account_number,
        'account_corrected': account_corrected,
        'account_input': _raw_in or None,
        'previous_balance': current_balance,
        'new_balance': new_balance,
        'grade_limit': grade_limit,
        'extends_by': over_by,
        'over_limit': over_by > 0,
        'source_unverified': source_unverified,
        'pending_qurtoba_push': True,
        'pending_cash_sys': is_cash,
    }


# ---------------------------------------------------------------------------
# Shared batch creator — used by the bulk tool AND the split tool
# ---------------------------------------------------------------------------
def _create_debts_batch(conv, customer, items, override_grade_limit, source_message_id=None):
    """Create each item in `items` via _create_one_debt; return the batch summary.

    Shared core of qurtoba_create_new_transactions_bulk.
    Fires ONE 👍 for the whole batch (one shared ack_state). The caller runs any
    burst-specific pre-checks (e.g. the bulk completeness guard) — this helper does not.
    """
    social_partner = getattr(conv, 'social_partner', None)
    results: List[Dict[str, Any]] = []
    created_count = 0
    rejected_count = 0
    pending_count = 0
    duplicate_count = 0
    same_day_duplicate_count = 0
    # One 👍 for the whole batch — fired by the first item that actually creates.
    ack_state = {'sent': False}

    # Authoritative {phone source_message_id -> [all consumed message ids]} for this
    # burst, so each create watermarks BOTH its phone and amount message (a split
    # pair's amount message would otherwise linger and corrupt the next burst).
    try:
        from qurtoba.tools.planning import consumed_ids_by_source
        _consumed_map = consumed_ids_by_source(conv)
    except Exception:
        _consumed_map = {}

    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            rejected_count += 1
            results.append({
                'index': index,
                'status': 'rejected',
                'error_type': 'invalid_item',
                'error': 'كل عنصر يجب أن يكون كائن (object) بحقول type و value.',
            })
            continue

        type_ = raw_item.get('type')
        value_ = raw_item.get('value')
        account_ = raw_item.get('account_number')
        notes_ = raw_item.get('notes')
        # Per-item phone-message id — each transaction quotes ITS OWN number message.
        # Fall back to the batch-level id only when the item didn't supply one.
        item_source_message_id = raw_item.get('source_message_id') or source_message_id
        item_confirm_repeat = bool(raw_item.get('confirm_repeat', False))

        validation = _validate_debt_item(type_, value_, account_)
        if not validation['ok']:
            rejected_count += 1
            results.append({
                'index': index,
                'status': 'rejected',
                'error_type': validation['error_type'],
                'error': validation['error'],
                'input': {'type': type_, 'value': value_, 'account_number': account_},
            })
            continue

        outcome = _create_one_debt(
            customer=customer,
            social_partner=social_partner,
            type=validation['effective_type'],
            amount=validation['amount'],
            final_account=validation['final_account'],
            is_cash=validation['is_cash'],
            notes=notes_,
            override_grade_limit=override_grade_limit,
            conversation=conv,
            source_message_id=item_source_message_id,
            account_input=account_,
            ack_state=ack_state,
            consumed_message_ids=_consumed_map.get(item_source_message_id),
            confirm_repeat=item_confirm_repeat,
        )

        if outcome.get('success'):
            if outcome.get('same_day_duplicate'):
                # Looks like a repeat of a كاش transfer already created TODAY (B5) — NOT
                # created. The agent must ask «تأكيد تكرار العملية؟» before retrying this
                # SAME item with confirm_repeat=true (same source_message_id — no need to
                # guess a different one).
                same_day_duplicate_count += 1
                results.append({
                    'index': index,
                    'status': 'same_day_duplicate',
                    'existing_record_id': outcome.get('existing_record_id'),
                    'type': outcome.get('type'),
                    'value': outcome.get('value'),
                    'account_number': outcome.get('account_number'),
                    'created_at': outcome.get('created_at'),
                    'input': {'type': type_, 'value': value_, 'account_number': account_},
                })
            elif outcome.get('duplicate'):
                # Already created from this message on a prior run — idempotent
                # no-op. Don't re-tally balances (none returned for a duplicate).
                duplicate_count += 1
                results.append({
                    'index': index,
                    'status': 'duplicate',
                    'record_id': outcome.get('record_id'),
                    'type': outcome.get('type'),
                    'value': outcome.get('value'),
                    'account_number': outcome.get('account_number'),
                    'input': {'type': type_, 'value': value_, 'account_number': account_},
                })
            elif outcome.get('pending_review'):
                pending_count += 1
                results.append({
                    'index': index,
                    'status': 'pending_review',
                    'pending_id': outcome.get('pending_id'),
                    'type': outcome.get('type'),
                    'value': outcome.get('value'),
                    'account_number': outcome.get('account_number'),
                    'account_corrected': outcome.get('account_corrected', False),
                    'account_input': outcome.get('account_input'),
                    'input': {'type': type_, 'value': value_, 'account_number': account_},
                })
            else:
                created_count += 1
                results.append({
                    'index': index,
                    'status': 'created',
                    'record_id': outcome['record_id'],
                    'type': outcome['type'],
                    'value': outcome['value'],
                    'account_number': outcome['account_number'],
                    'account_corrected': outcome.get('account_corrected', False),
                    'account_input': outcome.get('account_input'),
                    'previous_balance': outcome['previous_balance'],
                    'new_balance': outcome['new_balance'],
                    'pending_cash_sys': outcome['pending_cash_sys'],
                })
        else:
            rejected_count += 1
            results.append({
                'index': index,
                'status': 'rejected',
                'error_type': outcome.get('error_type'),
                'error': outcome.get('error'),
                'disabled_type': outcome.get('disabled_type'),
                'current_balance': outcome.get('current_balance'),
                'grade_limit': outcome.get('grade_limit'),
                'projected_balance': outcome.get('projected_balance'),
                'extends_by': outcome.get('extends_by'),
                'input': {'type': type_, 'value': value_, 'account_number': account_},
            })

    customer.refresh_from_db(fields=['balance'])
    final_balance = customer.balance or 0
    grade_limit = (customer.grade or 0) * 1000
    remaining_credit = grade_limit - final_balance if grade_limit else None

    # Debug log — one line per create batch: exactly what committed vs. was blocked, so a
    # «تم بدون عملية» / duplicate / wrong-value report can be traced to the tool decision.
    try:
        from qurtoba.tools._debuglog import log_event
        log_event(
            'create', conversation=conv, customer=customer,
            n=len(items),
            created=created_count or None,
            pending=pending_count or None,
            rejected=rejected_count or None,
            dup=duplicate_count or None,
            same_day_dup=same_day_duplicate_count or None,
            balance=final_balance,
            items=[{'st': r.get('status'),
                    'acc': r.get('account_number') or (r.get('input') or {}).get('account_number'),
                    'val': r.get('value') or (r.get('input') or {}).get('value'),
                    'rid': r.get('record_id') or r.get('pending_id') or r.get('existing_record_id'),
                    'err': r.get('error_type')}
                   for r in results] or None,
        )
    except Exception:
        pass

    return {
        'success': True,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'total': len(items),
        'created_count': created_count,
        'pending_count': pending_count,
        'rejected_count': rejected_count,
        'duplicate_count': duplicate_count,
        'same_day_duplicate_count': same_day_duplicate_count,
        'final_balance': final_balance,
        'grade_limit': grade_limit,
        'remaining_credit': remaining_credit,
        'results': results,
    }


# ---------------------------------------------------------------------------
# Create debt transaction(s) — one OR many (عملية جديدة)
# ---------------------------------------------------------------------------

@tool(
    name='qurtoba_create_new_transactions_bulk',
    side_effect=True,  # mutating: creates debt records — never carry its result forward
    display_name='Create Many Qurtoba Transactions at Once',
    description=(
        'Use this tool to register new debt transaction(s) (عملية جديدة) for the customer '
        'linked to the current chat — ONE transaction or MANY in a single call. This is the '
        'ONLY create-transaction tool: for a single op, pass an array with one item. Use it '
        'whenever the customer sends one or several requests (each line / each separated '
        'entry is a separate transaction). Each transaction increases the customer\'s '
        'outstanding balance. '
        'CASH BRACKET RULE: cash variants are amount FILTERS, not commissions. Always pass '
        'type="كاش" for any cash transfer; the tool auto-promotes by amount: < 10,000 stays '
        '"كاش", 10,000–19,999 → "كاش(10)", ≥ 20,000 → "كاش(20)". "كاش(5)" is reserved — do '
        'NOT pass it. '
        'INPUT: '
        '1) transactions — an array; each item is an object with: '
        '   - type (string, required): one of كاش, فورى, أمان, طاير, مصاريف خدمه (for any '
        '     cash transfer just use "كاش" — the bracket is picked automatically) '
        '   - value (number, required): the amount in EGP (positive) '
        '   - account_number (string, optional): destination phone (11-digit starting 01) for '
        '     cash types, or customer\'s saved account string for non-cash. '
        '   - source_message_id (string, STRONGLY preferred): the chat message id (UUID) of '
        '     THIS transaction\'s OWN phone-number message, taken verbatim from its '
        '     "[message_id: <uuid>]" marker. CRITICAL: in a burst each phone number is in its '
        '     own message, so EACH item must carry the id of ITS OWN phone message — do NOT '
        '     reuse one id for all items. The execution receipt for each transaction replies '
        '     to exactly this message. '
        '   - notes (string, optional). '
        '2) override_grade_limit (optional, default False) — if False, items that would push '
        'the customer over their credit ceiling (grade × 1000) are rejected one by one; '
        'items that fit are still created. '
        '3) source_message_id (optional, FALLBACK ONLY) — used for an item only when that item '
        'has no per-item source_message_id. Prefer the per-item field above; only pass this '
        'top-level one when all transactions genuinely came from a single message. '
        'BEHAVIOUR: '
        '- Items are processed IN ORDER. Each item sees the balance AFTER the previously '
        'created items in the batch, so a partial batch is possible. '
        '- An over-limit item is NOT rejected — it is queued for admin review, and its '
        'results[] entry has status="pending_review" with pending_id. Treat it as a normal '
        'success in your 👍 reply; do NOT enumerate it in the rejection list. '
        '- A rejected item may have error_type="service_disabled" — meaning the merchant has '
        'turned that specific transfer type off on their WhatsApp account. The `error` field '
        'contains a ready Arabic message; quote it in your per-item summary. '
        '- AUTO-ACK: when the first item begins creating, the tool sends ONE instant 👍 to '
        'the chat itself. So on an all-success batch stay SILENT (do NOT send 👍/text). '
        '- The tool ALWAYS returns a results array, one entry per input item, with '
        'status one of {created, pending_review, rejected, duplicate, same_day_duplicate}. The '
        'response summary contains created_count, pending_count, rejected_count, '
        'duplicate_count, same_day_duplicate_count. On success stay silent; '
        'only mention items whose status is "rejected". Number correction is handled by '
        'the tool itself (it replies the corrected number per created item) — never send it. '
        '- status="same_day_duplicate" (كاش only): a كاش transfer with this EXACT '
        'account_number + value was already created TODAY for this customer — NOT created '
        'this call. Ask the customer «تأكيد تكرار العملية؟» before doing anything else. If '
        'they confirm, retry passing confirm_repeat=true on that SAME item (same '
        'source_message_id, no need to change it) to actually create the second transaction. '
        'NEVER say «تم»/success for this item until a retry actually returns status="created". '
        'This check is كاش-only and same-calendar-day-only by design — a repeat from an '
        'earlier day, or any فورى/أمان/طاير repeat, is never flagged here. '
        '- The tool returns final_balance, grade_limit, and remaining_credit after the batch. '
        'WHEN TO USE: '
        '- The customer\'s single message contains multiple amounts/lines (e.g. '
        '"01000000001 5000 / 01000000002 3000"). Parse all lines and pass them as one array. '
        '- Do NOT call this tool multiple times for the same set of transactions; one call '
        'handles the whole batch atomically per-item. '
        '- For a SINGLE transaction, call this same tool with a one-item array. '
        'VALUE — pass a plain positive NUMBER (int), never words/currency/separators: '
        '"خمسين الف"→pass 50000, "خمسمائة"→500, "ميتين"→200, "ألفين"→2000, "خمسة آلاف"→5000, '
        '"20الف"→20000, "27.460"/"27,460"→27460, "٥٠٠٠"→5000. You (the model) read Arabic number '
        'words and separators yourself and pass the resulting integer — the tool does NOT parse '
        'words, and a spelled amount handed in raw will be rejected. Egypt has NO fractions: '
        'never send a decimal amount (13.75 is a commission tally, not a transfer). '
        'WORKED EXAMPLES: '
        '(1) single: customer «01025294594 3000 كاش» → transactions=[{type:"كاش", value:3000, '
        'account_number:"01025294594", source_message_id: that msg id}]. '
        '(2) bulk from a planner burst: planner returned 3 pairs → pass all 3 in ONE array, each '
        'with ITS OWN source_message_id (the phone message), one call, one 👍. '
        '(3) spelled amount from read_amounts: «01019525475» + «خمسين الف» → you read 50000 → '
        'transactions=[{type:"كاش", value:50000, account_number:"01019525475", source_message_id: '
        'the phone msg id}]. '
        '(4) reroute/no-wallet: system said «محتاجين رقم تانى», customer sends a new number → pass '
        'the SAME known amount + the new number as a fresh one-item array. '
        'RESULT STATUSES per item: created (done), pending_review (over-limit → treat as success, '
        'stay silent), rejected (state the real reason), duplicate / same_day_duplicate (a كاش '
        'match already exists today → ask «تأكيد تكرار العملية؟», then retry that item with '
        'confirm_repeat=true). NEVER say «تم» until an item actually returns status "created".'
    ),
    category='qurtoba',
    requires_auth=True,
    parameters_schema={
        'type': 'object',
        'properties': {
            'transactions': {
                'type': 'array',
                'minItems': 1,
                'maxItems': 50,
                'items': {
                    'type': 'object',
                    'properties': {
                        'type': {
                            'type': 'string',
                            'enum': DEBT_TYPES,
                            'description': 'Transaction type — one of the 8 debt types.',
                        },
                        'value': {
                            'type': 'number',
                            'exclusiveMinimum': 0,
                            'description': 'Amount in EGP, positive number.',
                        },
                        'account_number': {
                            'type': ['string', 'null'],
                            'description': 'Destination phone for cash types (pass as written; '
                                           'the tool auto-normalizes +20/0020/20/020/spaces to '
                                           '01XXXXXXXXX); saved account string for non-cash.',
                        },
                        'source_message_id': {
                            'type': ['string', 'null'],
                            'description': 'UUID of THIS transaction\'s own phone-number message '
                                           '(from its "[message_id: <uuid>]" marker). Each item '
                                           'gets its OWN id — never reuse one id across items.',
                        },
                        'notes': {
                            'type': ['string', 'null'],
                        },
                        'confirm_repeat': {
                            'type': 'boolean',
                            'default': False,
                            'description': 'Set true ONLY when retrying THIS SAME item after '
                                           'the customer explicitly confirmed a '
                                           '"same_day_duplicate" result (you asked «تأكيد '
                                           'تكرار العملية؟» and they said yes). Keep the same '
                                           'source_message_id — do not invent a different one.',
                        },
                    },
                    'required': ['type', 'value'],
                },
            },
            'override_grade_limit': {
                'type': 'boolean',
                'default': False,
            },
            'source_message_id': {
                'type': ['string', 'null'],
                'description': 'FALLBACK ONLY — used for an item that lacks a per-item '
                               'source_message_id. Prefer the per-item field inside each '
                               'transaction; only set this when all came from one message.',
            },
        },
        'required': ['transactions'],
    },
)
def qurtoba_create_new_transactions_bulk(
    context,
    transactions: List[Dict[str, Any]],
    override_grade_limit: bool = False,
    source_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    conv, customer, err = _resolve_conversation_and_customer(context)
    if err:
        return err

    if not isinstance(transactions, list) or not transactions:
        return {
            'success': False,
            'error_type': 'empty_batch',
            'error': 'يجب تمرير قائمة بمعاملة واحدة على الأقل.',
        }

    # --- Completeness guard (self-contained pairs only) -----------------------
    # The LLM builds this `transactions` list from the messages and can silently DROP one
    # (caught it omitting 01031170652←1360 → that transfer just vanished, no error). Scan the
    # RECENT unconsumed inbound messages that each contain exactly ONE number + ONE amount —
    # those are UNAMBIGUOUS. If any such pair is missing from the LLM's list, surface it in
    # `possibly_missing` — but DO NOT block the batch. A hard reject here recreated cancelled
    # money (the LLM re-adds the pair the customer just cancelled) and silently dropped valid
    # ops (a legitimate subset — cancel / same-day-duplicate hold — got the whole call rejected).
    # Non-fatal: create what was provided; the agent reconciles `possibly_missing` (add a truly
    # dropped pair; ignore one it intentionally omitted for cancel/duplicate). Skipped on admin
    # override (single-item approval path).
    _possibly_missing: List[Dict[str, Any]] = []
    if not override_grade_limit and conv is not None:
        try:
            from datetime import timedelta as _td
            from django.utils import timezone as _tz
            from modules.chat.models import Message as _M
            from qurtoba.tools.planning import _classify_message as _cls

            def _key(ph, val):
                return (_normalize_phone(ph), round(float(val), 2))

            expected = {}
            for _mm in _M.objects_all.filter(
                    conversation=conv, direction='inbound', active=True, type='text',
                    ai_consumed_at__isnull=True, created_at__gte=_tz.now() - _td(minutes=10)):
                _c = _mm.content
                _txt = _c.get('text') if isinstance(_c, dict) else None
                if not _txt:
                    continue
                _cc = _cls(_txt)
                if len(_cc['phones']) == 1 and len(_cc['amounts']) == 1:  # one number, one amount
                    k = _key(_cc['phones'][0], _cc['amounts'][0])
                    if k[0]:
                        expected[k] = expected.get(k, 0) + 1
            provided = {}
            for t in transactions:
                if isinstance(t, dict) and t.get('account_number') is not None:
                    _nv = normalize_amount(t.get('value'))
                    if _nv.get('ok'):
                        k = _key(t.get('account_number'), _nv['value'])
                        provided[k] = provided.get(k, 0) + 1
            missing = []
            for k, cnt in expected.items():
                for _ in range(max(0, cnt - provided.get(k, 0))):
                    missing.append({'account_number': k[0], 'value': k[1]})
            _possibly_missing = missing
        except Exception:
            logger.warning('bulk completeness check failed; proceeding without it', exc_info=True)
    # --------------------------------------------------------------------------

    result = _create_debts_batch(conv, customer, transactions, override_grade_limit, source_message_id)
    # Non-fatal completeness signal: surface any recent self-contained pair the LLM's array
    # dropped, WITHOUT blocking the created ops. INTERNAL — the agent decides whether to add
    # a genuinely-dropped pair or ignore one it intentionally left out; never shown verbatim.
    if _possibly_missing and isinstance(result, dict):
        result['possibly_missing'] = _possibly_missing
    return result


# ---------------------------------------------------------------------------
# Tool 3 — Register customer payment (سداد) — SENSITIVE / HUMAN-IN-THE-LOOP
# ---------------------------------------------------------------------------

@tool(
    name='qurtoba_register_customer_payment',
    side_effect=True,  # mutating: creates a pending-payment row — never carry its result forward
    display_name='Register Customer Payment (سداد) — Review Queue',
    description=(
        'Use this tool ONLY to register a payment the customer has made TO the merchant '
        '(سداد) — i.e. money the customer claims to have sent. The tool DOES NOT directly '
        'reduce the customer balance. Instead it creates a PENDING PAYMENT row that an '
        'admin must review (verify the screenshot, confirm the bank/wallet credit) and '
        'then approve or deny. Approval creates the actual QurtobaRecord with is_down=True. '
        'INPUTS YOU MUST PROVIDE: '
        '1) type — exactly one of: "شراء كاش" or "شراء فورى". (Never use "تحصيل" — that\'s '
        'the collector-visit flow.) '
        '2) value — payment amount in EGP (positive number). '
        '3) customer_confirmation_text — verbatim text from the customer/partner confirming '
        'the payment amount and method (e.g. "نعم 500 شراء فورى"). '
        '4) screenshot_chat_message_id — the chat message id (UUID) of the inbound message in '
        'this conversation that contains the receipt screenshot the customer sent, taken '
        'verbatim from its "[message_id: <uuid>]" marker. If you don\'t '
        'have such a message in conversation_history, ASK for it first: '
        '"أرسل صورة الإيصال أولاً." Do NOT invent or guess this id. '
        '5) account_number — for "شراء كاش": the phone the money was transferred to (read '
        'from the receipt; pass as written, the tool auto-normalizes +20/0020/20/020/spaces '
        'to 01XXXXXXXXX). For "شراء فورى" the tool ALWAYS pins it to our fixed Fawry account '
        '2697418 (you should only register a Fawry payment after verifying the receipt\'s '
        'رقم الحساب == 2697418; if it differs, do NOT call this — tell the customer to '
        'transfer to 2697418). '
        'IMAGE RECEIPTS: when the customer sends a receipt screenshot, read it directly — '
        'a Fawry receipt (Fawry/فوري logo, "عملية ناجحة", المبلغ الكلي, رقم الحساب) → '
        '"شراء فورى" with value=المبلغ الكلي; a cash/wallet transfer (value → phone number) '
        '→ "شراء كاش" with value + that number. Pass the screenshot image\'s message id. '
        'BEHAVIOUR: '
        '- The tool resolves the screenshot via Message → MessageAttachment → '
        'Attachment.from_chat_attachment (reuses the same file, no re-download). '
        '- It creates one QurtobaPendingPayment row with state="pending" and notifies all '
        'active users. The customer balance does NOT change yet. '
        '- Returns success=True, pending_review=True, pending_id=<int>. IMPORTANT: this tool '
        'does NOT auto-send any acknowledgement (unlike the transfer-create tool). So after a '
        'successful registration you MUST send a SHORT confirmation yourself (e.g. «وصلني '
        'الإيصال، تحت المراجعة») — do NOT stay silent, or the customer sees nothing and re-sends. '
        'May also return duplicate=True (an identical payment is already pending review today) — '
        'reply that it is already under review; do not treat it as a new one. '
        '- Rejections: error_type one of {invalid_type, invalid_value, invalid_account_number, '
        'missing_confirmation, screenshot_required, screenshot_invalid}. screenshot_required = '
        'no screenshot message id; screenshot_invalid = the id does not exist / is not from '
        'this partner / has no attached image.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=20,
)
def qurtoba_register_customer_payment(
    context,
    type: str,
    value: float,
    customer_confirmation_text: str,
    screenshot_chat_message_id: str,
    account_number: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    conv, customer, err = _resolve_conversation_and_customer(context)
    if err:
        return err

    # --- Rule 1: confirmation text must be present and non-trivial ---
    confirmation = (customer_confirmation_text or '').strip()
    if len(confirmation) < 3:
        return {
            'success': False,
            'error': (
                'يجب التقاط نص التأكيد الصريح من المحادثة قبل تسجيل السداد.'
            ),
            'error_type': 'missing_confirmation',
        }

    # --- Rule 2: type must be a payment type — never تحصيل (collector-only) ---
    if type not in PAYMENT_TYPES:
        return {
            'success': False,
            'error': f"Invalid payment type '{type}'. Allowed: {PAYMENT_TYPES}",
            'error_type': 'invalid_type',
        }

    # --- Rule 3: amount must be a positive number ---
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return {'success': False, 'error': 'value must be a positive number.', 'error_type': 'invalid_value'}
    if amount <= 0:
        return {'success': False, 'error': 'value must be greater than zero.', 'error_type': 'invalid_value'}

    # --- Rule 4: account number per payment type ---
    final_account = account_number
    if type == 'شراء كاش':
        # Cash payment: the phone the money was transferred to (from the receipt).
        final_account = _normalize_phone(account_number)
        if not final_account or not final_account.startswith('01') or len(final_account) != 11:
            return {
                'success': False,
                'error': 'من فضلك ارسل رقم صحيح لمصدر السداد (شراء كاش).',
                'error_type': 'invalid_account_number',
            }
    elif type == 'شراء فورى':
        # Fawry payment always settles to OUR fixed Fawry account. The agent has
        # already verified the receipt's رقم الحساب == this number before calling;
        # we pin it here so the stored record is always consistent.
        final_account = FAWRY_PAYMENT_ACCOUNT

    # --- Rule 5: screenshot is mandatory — resolve from chat Message id ---
    if not screenshot_chat_message_id:
        return {
            'success': False,
            'error_type': 'screenshot_required',
            'error': 'أرسل صورة الإيصال أولاً.',
        }

    try:
        from modules.chat.models import Message as ChatMessage
        msg = (
            ChatMessage.objects
            .filter(id=str(screenshot_chat_message_id).strip(), conversation=conv, direction='inbound')
            .first()
        )
    except Exception:
        msg = None

    if msg is None:
        return {
            'success': False,
            'error_type': 'screenshot_invalid',
            'error': 'الرسالة المرجعية للإيصال غير موجودة في هذه المحادثة.',
        }

    chat_attachment = getattr(msg, 'attachment', None)
    if chat_attachment is None or not getattr(chat_attachment, 'file', None):
        return {
            'success': False,
            'error_type': 'screenshot_invalid',
            'error': 'الرسالة المرجعية لا تحتوي على صورة إيصال.',
        }

    # Bridge chat attachment → base attachment (same storage path, no copy).
    try:
        from modules.base.models.attachment import Attachment as BaseAttachment
        base_attachment = BaseAttachment.from_chat_attachment(chat_attachment)
    except Exception as exc:
        return {
            'success': False,
            'error_type': 'screenshot_invalid',
            'error': f'تعذّر ربط صورة الإيصال: {exc}',
        }

    # --- Idempotency: same receipt registered twice in quick succession -------
    # Payment registration is silent (no auto-👍) and admin-reviewed, so a customer who
    # sees no immediate reply often RE-SENDS the receipt within a minute → a duplicate
    # review row. Dedupe only a RECENT (≤5 min) identical pending payment — deliberately
    # NOT all day, so a genuine second same-value payment later in the day is NOT blocked.
    try:
        from datetime import timedelta as _td2
        from qurtoba.models import QurtobaPendingPayment as _QPP
        from django.utils import timezone as _tz2
        _recent = _tz2.now() - _td2(minutes=5)
        _dup_pay = _QPP.objects.filter(
            customer=customer, type=type, value=amount, account_number=final_account,
            review_state='pending', created_at__gte=_recent,
        ).order_by('-id').first()
        if _dup_pay is not None:
            return {
                'success': True, 'duplicate': True, 'pending_review': True,
                'pending_id': _dup_pay.pk, 'type': _dup_pay.type, 'value': _dup_pay.value,
                'account_number': _dup_pay.account_number,
                'note': 'سبق تسجيل نفس السداد وهو تحت المراجعة بالفعل.',
            }
    except Exception:
        logger.warning('qurtoba payment duplicate-check failed; proceeding', exc_info=True)

    # --- Create the PendingPayment row + notify ---
    audit_notes = f'[AI سداد] confirmation: {confirmation[:240]}'
    if notes:
        audit_notes = f'{audit_notes}\n{notes}'

    try:
        from qurtoba.models import QurtobaPendingPayment
        from qurtoba.extensions import _notify_all_users_pending

        pending = QurtobaPendingPayment.objects.create(
            customer=customer,
            partner=getattr(conv, 'social_partner', None),
            conversation_uuid=str(getattr(conv, 'id', '') or '') or None,
            source_message_id=str(screenshot_chat_message_id).strip() or None,
            type=type,
            value=amount,
            account_number=final_account,
            notes=audit_notes,
            customer_confirmation_text=confirmation,
            screenshot_attachment=base_attachment,
            reason='payment_review',
            review_state='pending',
        )
        _notify_all_users_pending(pending, kind='payment')
    except Exception as exc:
        return {
            'success': False,
            'error_type': 'pending_create_failed',
            'error': 'تعذّر تسجيل السداد للمراجعة — حاول لاحقاً.',
            'debug': str(exc),
        }

    return {
        'success': True,
        'pending_review': True,
        'pending_id': pending.pk,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'type': pending.type,
        'value': pending.value,
        'account_number': pending.account_number,
        'note': 'تم تسجيل السداد للمراجعة. سيتم اعتماده بعد التحقق من الإيصال.',
    }


# ---------------------------------------------------------------------------
# Tool 4 — Check whether a transaction was executed via the cash app
# ---------------------------------------------------------------------------

def _fmt0(value) -> str:
    try:
        return f'{float(value or 0):,.0f}'
    except (TypeError, ValueError):
        return str(value or 0)


def _status_line_for(record) -> str:
    """One ready-to-send Arabic status line for a single record."""
    is_cash = record.type in CASH_TYPES
    state = getattr(record, 'cash_sys_state', None)
    dest = record.account_number or '—'

    if is_cash:
        # Multi-step chain states take precedence over the legacy done flag.
        if state == 'rerouted':
            original = record.cash_sys_original_value if record.cash_sys_original_value is not None else record.value
            fulfilled = record.cash_sys_fulfilled if record.cash_sys_fulfilled is not None else record.value
            return (
                f'🔄 تم تحويل {_fmt0(fulfilled)} من {_fmt0(original)} — '
                f'الباقي على رقم جديد ({record.type})'
            )
        if state == 'partial':
            fulfilled = record.cash_sys_fulfilled
            return f'⏳ جارٍ التحويل — تم تحويل {_fmt0(fulfilled)} حتى الآن ({record.type} ← {dest})'
        if state == 'canceled':
            return f'❌ تم الإلغاء — {record.type} {_fmt0(record.value)} ← {dest}'
        done = bool(record.cash_sys_done)
        mark = '✅ تم التنفيذ عبر الكاش' if done else '⏳ قيد التنفيذ'
    else:
        done = bool(record.qurtoba_synced)
        mark = '✅ تم الإرسال' if done else '⏳ قيد التنفيذ'

    return f'{mark} — {record.type} {_fmt0(record.value)} ← {dest}'


@tool(
    name='qurtoba_check_transaction_status',
    display_name='Check Qurtoba Transaction Status (executed via cash app?)',
    description=(
        'Use this tool when the partner asks whether a transfer actually went through / was '
        'executed by the cash app (e.g. "هل تم؟", "وصل؟", "اتنفذت؟", "التحويل تم؟"). '
        'It reports whether the transaction has been executed via the cash app (cash_sys_done) '
        'or is still in progress. '
        'INPUTS: '
        '1) source_message_id (optional) — if the partner REPLIED TO / quoted a specific '
        'earlier transfer message, pass that message id (UUID, from its "[message_id: <uuid>]" '
        'marker). The tool then reports the exact transaction created from THAT message. '
        'If you do not pass it (the partner asked generally), the tool reports ONLY the '
        'customer\'s latest 3 transactions today, REGARDLESS of status (not filtered by '
        'pending/executed) — this is meant for a vague "وصل؟" with nothing specific to check, '
        'NOT for "which ones didn\'t go through" / "how many are still pending" / any question '
        'about a SUBSET of today\'s transactions. For that, use '
        'qurtoba_get_customer_daily_transactions instead and filter its transactions[] by '
        '`bucket` yourself — this tool\'s 3-record cap will give an incomplete or misleading '
        'answer whenever more than 3 records are relevant. '
        'OUTPUT: '
        '- pretty_ar: a single ready-to-send Arabic block. Each line reflects the order '
        'state: "✅ تم التنفيذ عبر الكاش" (cash app executed it fully), "⏳ قيد التنفيذ" / '
        '"⏳ جارٍ التحويل …" (still in progress / partially sent), '
        '"🔄 تم تحويل X من Y — الباقي على رقم جديد" (rerouted: part sent, rest needs a new '
        'number), or "❌ تم الإلغاء". Send pretty_ar AS THE WHOLE REPLY in ONE message — '
        'do not add anything. '
        '- Structured: records[] with {record_id, type, value, account_number, cash_sys_done, '
        'qurtoba_synced, executed}. '
        'NOTES: '
        '- Do NOT invent a status. If no matching transaction is found, the tool returns '
        'found=false with a ready Arabic line; send it verbatim. '
        '- This tool is READ-ONLY; it never creates or modifies a transaction.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=60,
)
def qurtoba_check_transaction_status(
    context,
    source_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    conv, customer, err = _resolve_conversation_and_customer(context)
    if err:
        return err

    from qurtoba.models import QurtobaRecord

    records = []
    scoped_to_message = False
    if source_message_id:
        scoped_to_message = True
        records = list(
            QurtobaRecord.objects
            .filter(customer=customer, origin_message_id=str(source_message_id).strip())
            .order_by('-date', '-time', '-id')[:5]
        )
    if not records:
        # General ask (or the quoted message had no transaction): latest today.
        from django.utils import timezone
        records = list(
            QurtobaRecord.objects
            .filter(customer=customer, date=timezone.localdate(), is_down=False)
            .order_by('-time', '-id')[:3]
        )

    if not records:
        line = 'لا توجد عملية مطابقة. لو عايز، ابعت تفاصيل التحويل أو رد على رسالته.'
        return {
            'success': True,
            'found': False,
            'customer_id': customer.pk,
            'customer_name': customer.name,
            'records': [],
            'pretty_ar': line,
        }

    rows = []
    lines = []
    for r in records:
        is_cash = r.type in CASH_TYPES
        executed = bool(r.cash_sys_done) if is_cash else bool(r.qurtoba_synced)
        rows.append({
            'record_id': r.pk,
            'type': r.type,
            'value': float(r.value or 0),
            'account_number': r.account_number,
            'cash_sys_done': bool(r.cash_sys_done),
            'cash_sys_state': getattr(r, 'cash_sys_state', None),
            'cash_sys_fulfilled': r.cash_sys_fulfilled,
            'cash_sys_reroute_amount': r.cash_sys_reroute_amount,
            'qurtoba_synced': bool(r.qurtoba_synced),
            'executed': executed,
        })
        lines.append(_status_line_for(r))

    pretty_ar = '\n'.join(lines)
    return {
        'success': True,
        'found': True,
        'scoped_to_message': scoped_to_message,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'records': rows,
        'pretty_ar': pretty_ar,
    }


# ---------------------------------------------------------------------------
# Tool 5 — Check the review status of a سداد (payment receipt)
# ---------------------------------------------------------------------------

def _payment_status_line(p) -> str:
    """One ready-to-send Arabic line for a single pending-payment row."""
    val = _fmt0(p.value)
    if p.review_state == 'pending':
        return f'⏳ السداد قيد المراجعة — {val} ({p.type}). هيتم اعتماده بعد التحقق من الإيصال.'
    if p.review_state == 'approved':
        return f'✅ تم اعتماد السداد — {val} ({p.type}) وتسجيله على حسابك.'
    if p.review_state == 'denied':
        reason = (p.denial_reason or '').strip()
        base = f'❌ تم رفض السداد — {val} ({p.type}).'
        return f'{base}\nالسبب: {reason}' if reason else base
    return f'السداد {val} ({p.type}) — الحالة: {p.review_state}.'


@tool(
    name='qurtoba_check_payment_status',
    display_name='Check Customer Payment (سداد) Status',
    description=(
        'Use this tool when the customer asks whether a سداد (payment receipt they sent) '
        'was accepted/registered yet — e.g. "الإيصال اتقبل؟"، "السداد اتسجّل؟"، "تمام السداد؟"، '
        '"اعتمدتوا التحويل؟". It looks up the QurtobaPendingPayment created from their receipt '
        'and reports its review state. '
        'INPUT: '
        '1) source_message_id (optional) — the chat message id (UUID) of the RECEIPT IMAGE '
        'message, taken verbatim from its "[message_id: <uuid>]" marker (use the quoted/replied '
        'message when the customer replies to their receipt). Omit it to report the customer\'s '
        'most recent سداد. '
        'OUTPUT: '
        '- pretty_ar: a single ready-to-send Arabic line per payment — "⏳ قيد المراجعة" '
        '(under review), "✅ تم اعتماد السداد" (approved/created), or "❌ تم رفض السداد" with the '
        'denial reason appended. Send pretty_ar AS THE WHOLE REPLY in ONE message; do not invent '
        'a status or add anything. '
        '- Structured: payments[] with {pending_id, type, value, account_number, review_state, '
        'denial_reason, created_record_id}. '
        'This tool is READ-ONLY; it never creates, approves, or denies anything.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=60,
)
def qurtoba_check_payment_status(
    context,
    source_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    conv, customer, err = _resolve_conversation_and_customer(context)
    if err:
        return err

    from qurtoba.models import QurtobaPendingPayment

    payments = []
    scoped_to_message = False
    if source_message_id:
        scoped_to_message = True
        payments = list(
            QurtobaPendingPayment.objects
            .filter(customer=customer, source_message_id=str(source_message_id).strip())
            .order_by('-created_at')[:5]
        )
    if not payments:
        # General ask (or the quoted message had no payment): latest سداد for this customer.
        payments = list(
            QurtobaPendingPayment.objects
            .filter(customer=customer)
            .order_by('-created_at')[:3]
        )

    if not payments:
        line = 'مفيش سداد مسجّل باسمك. لو حوّلت، ابعت صورة الإيصال.'
        return {
            'success': True,
            'found': False,
            'customer_id': customer.pk,
            'customer_name': customer.name,
            'payments': [],
            'pretty_ar': line,
        }

    rows = []
    lines = []
    for p in payments:
        rows.append({
            'pending_id': p.pk,
            'type': p.type,
            'value': float(p.value or 0),
            'account_number': p.account_number,
            'review_state': p.review_state,
            'denial_reason': (p.denial_reason or '') if p.review_state == 'denied' else '',
            'created_record_id': p.created_record_id,
        })
        lines.append(_payment_status_line(p))

    return {
        'success': True,
        'found': True,
        'scoped_to_message': scoped_to_message,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'payments': rows,
        'pretty_ar': '\n'.join(lines),
    }
