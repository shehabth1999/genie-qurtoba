"""Customer groups on the WhatsApp Web channel (owner decision 2026-09-23).

One WhatsApp group = one Qurtoba customer. The group's own placeholder partner (``is_wa_group``,
the conversation's ``social_partner``) carries the ``qurtoba_customer`` link, so everything that
reads "the chat's partner" keeps its 1:1 meaning inside a group: the partner is the group, never the
member who happened to speak last (``runtime_patches`` keeps the wa_web bridge from swapping the
speaker in as the run's partner).

Members are Partners too. The Partner's own «Is an Employee» flag (core ``employee``, on the contact
form) marks office staff for EVERY group at once; everyone else is the customer's side (the default). The connected number itself is
always staff (what it types from the phone is stored as outbound anyway). Staff lines are context
the model reads — the money path never acts on them: they are watermarked the moment a turn
loads them, and every inbound read of the money path goes through ``exclude_staff``.

1:1 chats (Cloud API or WhatsApp Web) are untouched by everything here.
"""
import contextlib
import contextvars
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

CHANNEL = 'wa_web'
WA_WEB_ACCOUNT_LABEL = 'wa_web.wawebaccount'

# The wa_web bridge treats an empty workflow output as a failure (apology to the group, AI switched
# off, failure e-mail). A deliberately silent Qurtoba turn is turned into this marker by
# runtime_patches, and the outbound gate drops it without sending anything.
SILENT_SENTINEL = 'QURTOBA_SILENT_TURN'

LINK_NOTE_TTL = 3600


def is_group(conversation) -> bool:
    """A customer group on the WhatsApp Web channel."""
    return bool(conversation is not None and getattr(conversation, 'type', None) == CHANNEL
                and getattr(conversation, 'is_group', False))


def chat_partner(conversation, partner=None):
    """The partner that stands for the customer in this chat: the group's placeholder in a group,
    else the partner the caller has (or the chat's own partner)."""
    if is_group(conversation):
        return getattr(conversation, 'social_partner', None) or partner
    return partner if partner is not None else getattr(conversation, 'social_partner', None)


def staff_q(prefix: str = 'sender'):
    """Q matching rows whose `prefix` partner is office staff (itself or its star-linked parent)."""
    from django.db.models import Q
    return Q(**{f'{prefix}__employee': True}) | Q(**{f'{prefix}__parent_id__employee': True})


def exclude_staff(qs, conversation):
    """The inbound rows the money path may act on: in a group, never a staff member's."""
    if not is_group(conversation):
        return qs
    return qs.exclude(staff_q())


# In a group, a burst is paired (number ↔ amount, by position) only within ONE sender: the money path
# runs once per member who wrote, with this scope set (automation.transfers.run).
_SENDER_SCOPE = contextvars.ContextVar('qurtoba_group_sender_scope', default=None)


@contextlib.contextmanager
def sender_scope(sender_id):
    token = _SENDER_SCOPE.set(sender_id)
    try:
        yield
    finally:
        _SENDER_SCOPE.reset(token)


def money_rows(qs, conversation):
    """The inbound rows the money path may read: never a staff line in a group, and — inside a
    per-sender run — only that member's lines."""
    qs = exclude_staff(qs, conversation)
    scope = _SENDER_SCOPE.get()
    if scope is not None and is_group(conversation):
        qs = qs.filter(sender_id=scope)
    return qs


def open_senders(conversation, message_ids) -> list:
    """The members (partner ids, oldest first) behind the given rows and the chat's still-open customer
    lines — the per-sender runs a group turn needs. Staff are never among them."""
    if not is_group(conversation):
        return []
    from datetime import timedelta
    from django.conf import settings as dj
    from django.utils import timezone
    cut = timezone.now() - timedelta(minutes=getattr(dj, 'AI_UNPROCESSED_WINDOW_MIN', 6))
    qs = customer_inbound(conversation)
    rows = list(qs.filter(id__in=[str(i) for i in message_ids or []]).values_list('sender_id', 'created_at'))
    rows += list(qs.filter(ai_consumed_at__isnull=True, created_at__gte=cut, qurtoba_offline_cancelled_at__isnull=True)
                 .values_list('sender_id', 'created_at'))
    out = []
    for sid, _at in sorted(rows, key=lambda r: r[1]):
        if sid and sid not in out:
            out.append(sid)
    return out


# Set by the chat.Message pre_create hook while an unreadable ('unsupported') group message is being
# stored; the runtime patch on Conversation.escalate_to_human skips the escalation meanwhile.
_SUPPRESS_ESCALATION = contextvars.ContextVar('qurtoba_group_suppress_escalation', default=False)


def suppress_escalation(on: bool) -> None:
    _SUPPRESS_ESCALATION.set(bool(on))


def escalation_suppressed() -> bool:
    return bool(_SUPPRESS_ESCALATION.get())


def customer_inbound(conversation):
    """Active inbound rows of the chat that are not staff lines."""
    from modules.chat.models import Message
    return exclude_staff(
        Message.objects_all.filter(conversation=conversation, direction='inbound', active=True), conversation)


def is_staff(partner) -> bool:
    """Office staff: the partner (or its star-linked parent) is ticked «Is an Employee», or it is a
    connected WhatsApp Web number itself."""
    if partner is None:
        return False
    if getattr(partner, 'employee', False):
        return True
    try:
        parent = getattr(partner, 'parent_id', None)
        if parent is not None and getattr(parent, 'employee', False):
            return True
    except Exception:
        pass
    return is_own_number(partner)


def is_own_number(partner) -> bool:
    acc_id = getattr(partner, 'wa_web_account_id', None)
    wa_id = getattr(partner, 'wa_id', None)
    if not acc_id or not wa_id:
        return False
    try:
        from modules.wa_web.models import WaWebAccount
        acc = WaWebAccount.objects.filter(pk=acc_id).values('wa_jid', 'phone_number').first()
    except Exception:
        return False
    if not acc:
        return False
    if acc.get('wa_jid') and acc['wa_jid'] == wa_id:
        return True
    phone = _digits(acc.get('phone_number'))
    return bool(phone) and _digits(wa_id.split('@')[0]) == phone


def settle_staff_window(conversation, since) -> int:
    """Watermark the still-open staff lines of a group since `since`, so the money path never
    reconsiders them and no safety net ever sees them as unanswered. The model still reads them
    (chat history + the context block). Returns how many were settled; a 1:1 chat has none."""
    if not is_group(conversation):
        return 0
    try:
        from modules.chat.models import Message
        ids = [str(i) for i in Message.objects_all.filter(
            conversation=conversation, direction='inbound', active=True, ai_consumed_at__isnull=True,
            created_at__gte=since).filter(staff_q()).values_list('id', flat=True)[:200]]
        if not ids:
            return 0
        from qurtoba.automation.context import consume, log
        n = consume(conversation, ids)
        log('group_staff_lines', conversation, count=n, ids=[i[:8] for i in ids])
        return n
    except Exception:
        logger.warning('qurtoba.groups: could not settle staff lines', exc_info=True)
        return 0


def staff_lines_since(conversation, since) -> list:
    """Recent staff lines of a group (oldest first) — context for the model, never requests."""
    if not is_group(conversation) or since is None:
        return []
    try:
        from modules.chat.models import Message
        return list(Message.objects_all.filter(conversation=conversation, direction='inbound', active=True,
                                               created_at__gte=since)
                    .filter(staff_q()).select_related('sender').order_by('created_at')[:20])
    except Exception:
        logger.warning('qurtoba.groups: staff lines lookup failed', exc_info=True)
        return []


# ── the connected number is staff ──────────────────────────────────────────────────────────────────

def mark_own_number_staff(account) -> None:
    """Tick «Is an Employee» on the connected number's own partner (idempotent, never raises)."""
    try:
        from modules.wa_web.services.ingest import system_partner
        p = system_partner(account)
        if p is not None and getattr(p, 'wa_web_account_id', None) == account.pk and not getattr(p, 'employee', False):
            type(p)._base_manager.filter(pk=p.pk).update(employee=True)
    except Exception:
        logger.warning('qurtoba.groups: could not mark the connected number as staff', exc_info=True)


# ── the group's customer ───────────────────────────────────────────────────────────────────────────

def _digits(value) -> str:
    return re.sub(r'\D', '', str(value or ''))


def _local10(value) -> str:
    """The last 10 digits of an Egyptian mobile («01…» or «201…» → «1XXXXXXXXX»)."""
    d = _digits(value)
    return d[-10:] if len(d) >= 10 else ''


def _member_partners(conversation) -> list:
    try:
        from modules.wa_web.models import WaWebGroupMember
        return [m.partner for m in WaWebGroupMember.objects.filter(conversation=conversation, left_at__isnull=True,
                                                                   is_self=False).select_related('partner')
                if m.partner is not None]
    except Exception:
        logger.warning('qurtoba.groups: member lookup failed', exc_info=True)
        return []


def customers_of_partner(p) -> List[int]:
    """The Qurtoba customer ids a member stands for: its own link, its star-linked parent's link, or its
    phone number on a Qurtoba customer."""
    from qurtoba.models import QurtobaCustomer
    if p is None:
        return []
    cid = getattr(p, 'qurtoba_customer_id', None)
    if cid:
        return [cid]
    parent = getattr(p, 'parent_id', None)
    if parent is not None and getattr(parent, 'qurtoba_customer_id', None):
        return [parent.qurtoba_customer_id]
    tail = _local10(getattr(p, 'phone', None) or (getattr(p, 'wa_id', None) or '').split('@')[0])
    if not tail:
        return []
    return sorted({c[0] for c in QurtobaCustomer.objects.filter(phone_no__endswith=tail).values_list('id', 'phone_no')
                   if _local10(c[1]) == tail})


def link_group(conversation, customer_id, *, by=None) -> None:
    """Link a customer group to a Qurtoba customer (staff action), with one internal note."""
    group_partner = getattr(conversation, 'social_partner', None)
    if group_partner is None:
        return
    type(group_partner)._base_manager.filter(pk=group_partner.pk).update(qurtoba_customer_id=customer_id)
    try:
        from qurtoba.models import QurtobaCustomer
        from qurtoba.staff_notes import post_staff_note
        name = getattr(QurtobaCustomer.objects.filter(pk=customer_id).first(), 'name', '') or str(customer_id)
        post_staff_note(
            conversation,
            [f'🔗 الجروب اتربط بعميل قرطبة: {name}' + (f' (بواسطة {by})' if by else '')],
            subject='🔗 جروب اتربط بعميل', body=f'الجروب «{getattr(conversation, "name", "") or ""}» ← {name}',
            dedupe_key=f'group_linked_by_staff:{conversation.id}:{customer_id}', dedupe_ttl=60,
        )
    except Exception:
        logger.warning('qurtoba.groups: link note failed', exc_info=True)
    _log('group_linked', conversation, customer=customer_id, how='staff')


def candidate_customers(conversation, extra_partners=()) -> List[int]:
    """Qurtoba customer ids found among the group's non-staff members (see customers_of_partner)."""
    ids = set()
    seen = set()
    for p in list(_member_partners(conversation)) + [p for p in extra_partners if p is not None]:
        if p.pk in seen or is_staff(p) or getattr(p, 'is_wa_group', False):
            continue
        seen.add(p.pk)
        ids.update(customers_of_partner(p))
    return sorted(ids)


def ensure_group_link(conversation, partner=None, speakers=()) -> Optional[int]:
    """The group's Qurtoba customer id, linking it automatically when exactly ONE customer is found among
    the non-staff members. Otherwise the office is asked (once an hour) to link it by hand.

    `partner` is the group's placeholder the run holds; it is updated in memory too so the workflow's
    «linked?» condition sees the new link in this very turn."""
    if not is_group(conversation):
        return getattr(partner, 'qurtoba_customer_id', None) if partner is not None else None
    group_partner = getattr(conversation, 'social_partner', None)
    if group_partner is None:
        return None
    current = type(group_partner)._base_manager.filter(pk=group_partner.pk).values_list(
        'qurtoba_customer_id', flat=True).first()
    if current:
        _sync_in_memory(current, group_partner, partner)
        return current
    found = candidate_customers(conversation, extra_partners=speakers)
    if len(found) == 1:
        cid = found[0]
        type(group_partner)._base_manager.filter(pk=group_partner.pk, qurtoba_customer__isnull=True).update(
            qurtoba_customer_id=cid)
        _sync_in_memory(cid, group_partner, partner)
        _link_note(conversation, cid)
        _log('group_linked', conversation, customer=cid, how='auto')
        return cid
    _ask_to_link(conversation, found)
    return None


def _sync_in_memory(cid, *partners) -> None:
    for p in partners:
        if p is not None and getattr(p, 'pk', None) is not None:
            try:
                p.qurtoba_customer_id = cid
            except Exception:
                pass


def _link_note(conversation, customer_id) -> None:
    try:
        from qurtoba.models import QurtobaCustomer
        from qurtoba.staff_notes import post_staff_note
        c = QurtobaCustomer.objects.filter(pk=customer_id).first()
        name = getattr(c, 'name', '') or str(customer_id)
        post_staff_note(
            conversation,
            [f'🔗 الجروب اتربط تلقائيًا بعميل قرطبة: {name}',
             'اتربط برقم واحد من أعضاء الجروب. لو غلط غيّره من «ربط الجروب بعميل».'],
            subject='🔗 جروب جديد اتربط بعميل',
            body=f'الجروب «{getattr(conversation, "name", "") or ""}» اتربط بـ {name} — راجِع.',
            dedupe_key=f'group_linked:{conversation.id}', dedupe_ttl=LINK_NOTE_TTL * 24,
        )
    except Exception:
        logger.warning('qurtoba.groups: link note failed', exc_info=True)


def _ask_to_link(conversation, found) -> None:
    try:
        from qurtoba.staff_notes import post_staff_note
        why = ('لقيت أكتر من عميل قرطبة بين أعضاء الجروب' if found
               else 'مفيش عضو في الجروب مربوط بعميل قرطبة')
        post_staff_note(
            conversation,
            ['⚠️ الجروب ده مش مربوط بعميل قرطبة — مفيش أي تحويل هيتعمل منه.',
             f'السبب: {why}.',
             'اربطه من «ربط الجروب بعميل» في الشات.'],
            subject='⚠️ جروب مش مربوط بعميل',
            body=f'الجروب «{getattr(conversation, "name", "") or ""}» محتاج يتربط بعميل قرطبة.',
            dedupe_key=f'group_unlinked:{conversation.id}', dedupe_ttl=LINK_NOTE_TTL,
        )
    except Exception:
        logger.warning('qurtoba.groups: unlinked note failed', exc_info=True)


# ── the Cloud API twin: one set of switches for the office number ─────────────────────────────────

def twin_cloud_account(account):
    """The Cloud API WhatsAppAccount with the same phone number as this WhatsApp Web account (None if none)."""
    phone = _digits(getattr(account, 'phone_number', None))
    if not phone:
        return None
    try:
        from modules.whatsapp.models import WhatsAppAccount
        for acc in WhatsAppAccount._base_manager.filter(active=True).only('id', 'phone_number'):
            if _digits(acc.phone_number) == phone:
                return acc
    except Exception:
        logger.warning('qurtoba.groups: twin account lookup failed', exc_info=True)
    return None


def _log(event: str, conversation, **fields) -> None:
    try:
        from qurtoba.tools._debuglog import log_event
        log_event(event, conversation=conversation, **fields)
    except Exception:
        pass
