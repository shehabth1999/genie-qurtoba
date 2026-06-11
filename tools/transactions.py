"""
Qurtoba transaction tools for AI Studio agents.

Tools exposed:
  * qurtoba_create_new_transaction         — record ONE new debt (عملية جديدة)
  * qurtoba_create_new_transactions_bulk   — record MANY new debts at once
  * qurtoba_register_customer_payment      — record a customer payment (سداد) [SENSITIVE]

All three resolve the target QurtobaCustomer from the active WhatsApp/Messenger
conversation: `conversation.social_partner.qurtoba_customer`.
"""
import logging
from typing import Any, Dict, List, Optional

from modules.aistudio.tools import tool

logger = logging.getLogger(__name__)


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
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return {'ok': False, 'error_type': 'invalid_value', 'error': 'المبلغ يجب أن يكون رقم موجب.'}
    if amount <= 0:
        return {'ok': False, 'error_type': 'invalid_value', 'error': 'المبلغ يجب أن يكون أكبر من صفر.'}

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

    # Did normalization change the destination number the partner typed?
    # (e.g. "00201038857982" -> "01038857982", or spaces/+20 stripped). The agent
    # uses this to confirm the correction back on the partner's message.
    _raw_in = (account_input or '').strip()
    account_corrected = bool(is_cash and final_account and _raw_in and _raw_in != final_account)

    # Pre-check: is this type enabled on the current WhatsApp account?
    allowed, disabled_msg = _check_type_allowed_for_account(conversation, type)
    if not allowed:
        return {
            'success': False,
            'error_type': 'service_disabled',
            'error': disabled_msg,
            'disabled_type': type,
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
        return {'success': False, 'error_type': 'create_failed', 'error': str(e)}

    # Link the inbound chat message that triggered this transaction, so the
    # execution receipt can later reply-quote it and the status tool can find
    # this record from that message. Safe no-op if the id is missing/unknown.
    if source_message_id:
        try:
            record.set_origin_message_from_uuid(source_message_id, conversation)
        except Exception:
            pass

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
        'pending_qurtoba_push': True,
        'pending_cash_sys': is_cash,
    }


# ---------------------------------------------------------------------------
# Tool 1 — Create ONE new debt transaction (عملية جديدة)
# ---------------------------------------------------------------------------

@tool(
    name='qurtoba_create_new_transaction',
    display_name='Create New Qurtoba Transaction (Debt)',
    description=(
        'Use this tool to register ONE new debt transaction (عملية جديدة) on the Qurtoba '
        'customer linked to the current chat. This INCREASES the customer\'s outstanding '
        'balance (مديونيات). Use it for: cash transfer to a phone (كاش), Fawry/Aman top-ups '
        '(فورى/أمان), Tayyar loans (طاير), or service fees (مصاريف خدمه). '
        'CASH BRACKET RULE: the four كاش variants are amount FILTERS, NOT commissions. '
        'Always pass type="كاش" for any cash transfer; the tool auto-promotes by amount: '
        '< 10,000 stays "كاش", 10,000–19,999 → "كاش(10)", ≥ 20,000 → "كاش(20)". The variant '
        '"كاش(5)" is reserved and unused — do NOT pass it. '
        'INPUTS YOU MUST PROVIDE: '
        '1) type — one of: كاش, فورى, أمان, طاير, مصاريف خدمه. (For any cash transfer just '
        'use "كاش" — the tool picks the right bracket.) '
        '2) value — the amount in EGP (positive number). '
        '3) account_number — REQUIRED for cash: the destination Egyptian mobile. Pass it '
        'AS THE PARTNER WROTE IT — the tool auto-normalizes country-code / formatted forms '
        '(+20, 0020, 20, 020, spaces, "+") to the canonical 01XXXXXXXXX, e.g. '
        '"00201038857982" -> "01038857982". Only if it cannot be turned into a valid '
        'Egyptian mobile is it rejected (error_type="invalid_account_number", error="من فضلك '
        'ارسل رقم صحيح"). For non-cash types pass the customer\'s saved account string or omit. '
        '4) override_grade_limit (optional, default False) — leave False so the tool '
        'enforces the customer\'s credit ceiling (grade × 1000). Only set True if a manager '
        'has explicitly authorized exceeding the limit. '
        '5) source_message_id (optional but STRONGLY preferred) — the chat message id (UUID), '
        'taken verbatim from the "[message_id: <uuid>]" marker. **ALWAYS pass the id of the '
        'message that CONTAINS THE PHONE/ACCOUNT NUMBER** (the destination number). If the '
        'phone and the amount arrive in two different messages, use the PHONE message\'s id — '
        'never the amount-only message. Passing it lets the execution receipt be sent back '
        'later as a reply that quotes the phone message, and lets the status check find this '
        'transaction when the partner replies to that phone message. '
        'BEHAVIOUR: '
        '- If the merchant has disabled this transaction type on their WhatsApp account, '
        'the tool REJECTS with error_type="service_disabled" and a ready-made Arabic '
        'message in the `error` field. Send that exact message back to the chat verbatim. '
        '- If the new transaction would push the customer over their credit ceiling and '
        'override_grade_limit is False, the tool DOES NOT REJECT. It returns success=True '
        'with pending_review=True and pending_id=<int>. The request is now queued for an '
        'admin to approve or deny. Treat this exactly like a normal success — reply 👍 to '
        'the partner. The real transaction will be created (and pushed to Qurtoba) only '
        'after an admin approves it from the review queue. '
        '- On normal success returns: record_id, type, value, previous_balance, new_balance, '
        'grade_limit, extends_by, pending_cash_sys (true if Cash-SYS will execute the order). '
        '- AUTO-ACK: the moment it starts creating the record, the tool sends an instant 👍 '
        'to the chat itself ("received, handling now"). So on success you MUST stay SILENT '
        '(empty reply) — do NOT send 👍 or any text. The tool does NOT send 👍 on a rejection. '
        '- It ALSO returns account_corrected (bool) and account_number (the canonical '
        'corrected number). If account_corrected is true, the destination number was '
        'normalized (e.g. country-code/spaces removed); reply with the corrected number '
        'ONLY — just send "{account_number}" (the bare number, nothing else). '
        'GUARDRAILS: '
        '- Do NOT use this for customer payments — use qurtoba_register_customer_payment. '
        '- Do NOT invent a phone number for cash types; if the customer did not state one, ask. '
        '- For MULTIPLE transactions in one customer message, prefer qurtoba_create_new_transactions_bulk.'
    ),
    category='qurtoba',
    requires_auth=True,
)
def qurtoba_create_new_transaction(
    context,
    type: str,
    value: float,
    account_number: Optional[str] = None,
    notes: Optional[str] = None,
    override_grade_limit: bool = False,
    source_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    conv, customer, err = _resolve_conversation_and_customer(context)
    if err:
        return err

    validation = _validate_debt_item(type, value, account_number)
    if not validation['ok']:
        return {
            'success': False,
            'error_type': validation['error_type'],
            'error': validation['error'],
        }

    result = _create_one_debt(
        customer=customer,
        social_partner=getattr(conv, 'social_partner', None),
        type=validation['effective_type'],
        amount=validation['amount'],
        final_account=validation['final_account'],
        is_cash=validation['is_cash'],
        notes=notes,
        override_grade_limit=override_grade_limit,
        conversation=conv,
        source_message_id=source_message_id,
        account_input=account_number,
        ack_state={'sent': False},
    )
    # Add identity info to the response
    result['customer_id'] = customer.pk
    result['customer_name'] = customer.name
    if result.get('success'):
        result['note'] = (
            'تم حفظ المعاملة وسيتم إرسالها إلى قرطبة في الخلفية' +
            (' وإلى Cash-SYS للتنفيذ.' if result.get('pending_cash_sys') else '.')
        )
    return result


# ---------------------------------------------------------------------------
# Tool 2 — BULK create debt transactions
# ---------------------------------------------------------------------------

@tool(
    name='qurtoba_create_new_transactions_bulk',
    display_name='Create Many Qurtoba Transactions at Once',
    description=(
        'Use this tool to register MANY new debt transactions in a single call for the '
        'customer linked to the current chat. Use it whenever the customer sends multiple '
        'requests in one message (each line / each separated entry is a separate '
        'transaction). Each transaction increases the customer\'s outstanding balance. '
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
        'status one of {created, pending_review, rejected}. The response summary contains '
        'created_count, pending_count, rejected_count. On success stay silent; '
        'only mention items whose status is "rejected" (and per-item account_corrected). '
        '- The tool returns final_balance, grade_limit, and remaining_credit after the batch. '
        'WHEN TO USE: '
        '- The customer\'s single message contains multiple amounts/lines (e.g. '
        '"01000000001 5000 / 01000000002 3000"). Parse all lines and pass them as one array. '
        '- Do NOT call this tool multiple times for the same set of transactions; one call '
        'handles the whole batch atomically per-item. '
        '- For a single transaction prefer qurtoba_create_new_transaction.'
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

    social_partner = getattr(conv, 'social_partner', None)
    results: List[Dict[str, Any]] = []
    created_count = 0
    rejected_count = 0
    pending_count = 0
    # One 👍 for the whole batch — fired by the first item that actually creates.
    ack_state = {'sent': False}

    for index, raw_item in enumerate(transactions):
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
        )

        if outcome.get('success'):
            if outcome.get('pending_review'):
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

    return {
        'success': True,
        'customer_id': customer.pk,
        'customer_name': customer.name,
        'total': len(transactions),
        'created_count': created_count,
        'pending_count': pending_count,
        'rejected_count': rejected_count,
        'final_balance': final_balance,
        'grade_limit': grade_limit,
        'remaining_credit': remaining_credit,
        'results': results,
    }


# ---------------------------------------------------------------------------
# Tool 3 — Register customer payment (سداد) — SENSITIVE / HUMAN-IN-THE-LOOP
# ---------------------------------------------------------------------------

@tool(
    name='qurtoba_register_customer_payment',
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
        '- Returns success=True, pending_review=True, pending_id=<int>. Treat exactly like '
        'any success — reply 👍. The customer will be informed when an admin approves. '
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
        'If you do not pass it (the partner asked generally), the tool reports the customer\'s '
        'most recent transactions today. '
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
