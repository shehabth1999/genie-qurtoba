# -*- coding: utf-8 -*-
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import gettext
from modules.base.model_inheritance import ModelExtension
from modules.base.decorators import action


# ---------------------------------------------------------------------------
# Reusable utility
# ---------------------------------------------------------------------------

def _get_conv_and_customer(queryset):
    """Extract (conversation, QurtobaCustomer) from a ConversationExtension queryset."""
    conv = queryset.first() if hasattr(queryset, 'first') else (queryset[0] if queryset else None)
    partner = getattr(conv, 'social_partner', None) if conv else None
    customer_id = getattr(partner, 'qurtoba_customer_id', None) if partner else None
    if not customer_id:
        return conv, None
    from qurtoba.models import QurtobaCustomer
    customer = QurtobaCustomer.objects.filter(pk=customer_id).first()
    return conv, customer


def system_sender():
    """The Partner every automatic message is sent AS — resolved by role, never by id or email.

    Order: the platform's AI/system partner (``ai_agent=True``, preferring ``system_user=True``),
    then any active staff partner (one that has a login). Nothing here can be a customer.
    2026-09-09: the admin account the automation used to send as was deleted; the sender
    must never again be one person's account.
    """
    from modules.base.models.partner import Partner
    ai = (Partner.all_objects.filter(ai_agent=True, active=True)
          .order_by('-system_user', 'pk').first())
    if ai is not None:
        return ai
    return Partner.objects.filter(user__isnull=False, active=True).order_by('pk').first()


def _get_system_partner(conversation):
    """
    Return the internal Partner to use as sender on `conversation`.
    Priority: the AI/system partner → an internal member of the conversation (a staff
    partner with a login) → any staff partner. Never the customer, never the creator.
    """
    from modules.chat.models import ConversationMember
    from modules.base.models.partner import Partner

    sender = system_sender()
    if sender is not None and getattr(sender, 'ai_agent', False):
        return sender

    # ConversationMember.user is a Partner; an internal member is one with a login
    social_id = getattr(conversation, 'social_partner_id', None)
    member_ids = list(
        ConversationMember.objects
        .filter(conversation=conversation, active=True, user__isnull=False)
        .exclude(user_id=social_id)
        .values_list('user_id', flat=True)
    )
    if member_ids:
        staff = Partner.objects.filter(pk__in=member_ids, user__isnull=False, active=True).order_by('pk').first()
        if staff is not None:
            return staff
    return sender


def check_balance_and_send(conversation, customer):
    """
    Reusable: formats the customer's Qurtoba balance and sends it as an
    outbound message on the given conversation via OmnichannelSendService —
    which both (1) delivers through the right channel API (WhatsApp /
    Messenger / Instagram / TikTok) and (2) records in ChatBridge with
    WebSocket push to the chat frontend.

    Can be imported and called from anywhere:
        from qurtoba.extensions import check_balance_and_send
    """
    from modules.chat.services.omnichannel_send_service import OmnichannelSendService

    # Ask Qurtoba first: the stored column is a cache, refreshed when a record is saved.
    # A settlement made inside Qurtoba writes no record here, so without this the customer
    # is told a debt the office already cleared (2026-09-09: «عليك 213,666» against a real
    # balance of zero). Best effort — on failure the last known figure is used.
    try:
        customer.recompute_balance()
        customer.refresh_from_db(fields=['balance'])
    except Exception:
        logger.warning('balance refresh failed for customer %s — sending the last known figure',
                       getattr(customer, 'pk', None), exc_info=True)
    balance = customer.balance or 0

    # Customer-facing message: only ليك / عليك with the ABSOLUTE value — never
    # show a negative number to the customer, and never expose the credit limit.
    #   balance > 0  ⇒ the customer owes us            ⇒ "عليك X جنيه"
    #   balance < 0  ⇒ the customer has credit with us ⇒ "ليك X جنيه"
    if balance > 0:
        text = f'عليك {abs(balance):,.0f} جنيه'
    elif balance < 0:
        text = f'ليك {abs(balance):,.0f} جنيه'
    else:
        text = 'مفيش مديونية'

    system_partner = _get_system_partner(conversation)

    from qurtoba.ai_guard import system_send
    with system_send():
        OmnichannelSendService().send_and_broadcast(
            partner=conversation.social_partner,
            content={'text': text},
            message_type='text',
            conversation=conversation,
            system_partner=system_partner,
            websocket=True,
        )


# ---------------------------------------------------------------------------
# Pending-review notifications — broadcasts a new pending item to all active
# users so any admin can pick it up from the queue. Single warning log on
# failure; never raises.
# ---------------------------------------------------------------------------

def _notify_all_users_pending(pending_record, *, kind: str) -> None:
    """
    Broadcast a "new pending item" notification to every active user.

    `kind` selects the wording:
      - 'transaction' → عملية بانتظار المراجعة (تجاوز الحد)
      - 'payment'     → سداد بانتظار المراجعة
    """
    try:
        from django.contrib.auth import get_user_model
        from modules.notifications.services import post_notification
        from modules.base.models import MenuItem

        User = get_user_model()
        partner_ids = list(
            User.objects.filter(is_active=True)
                        .exclude(partner__isnull=True)
                        .values_list('partner_id', flat=True)
        )
        if not partner_ids:
            return

        customer = getattr(pending_record, 'customer', None)
        customer_name = getattr(customer, 'name', '') if customer else ''
        type_label    = getattr(pending_record, 'type', '')
        value_label   = getattr(pending_record, 'value', '')

        if kind == 'transaction':
            subject = 'عملية بانتظار المراجعة'
            body    = f'تجاوز الحد — {type_label} {value_label} للعميل {customer_name}.'
        else:
            subject = 'سداد بانتظار المراجعة'
            body    = f'{type_label} {value_label} من العميل {customer_name}.'

        try:
            url = MenuItem.get_url_for_model(
                model=pending_record, view_type='form', id=pending_record.pk
            )
        except Exception:
            url = '/'

        # Deduplicate (User.partner could be repeated across rows if data is dirty).
        unique_partner_ids = list({pid for pid in partner_ids if pid})

        post_notification(
            partner_ids=unique_partner_ids,
            subject=subject,
            body=body,
            notification_type='inbox',
            is_push=True,
            record=pending_record,
            url=url,
        )
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            'Failed to notify users about pending %s #%s: %s',
            kind, getattr(pending_record, 'pk', '?'), exc,
        )


# ---------------------------------------------------------------------------
# PartnerQurtobaExtension — links base.Partner to QurtobaCustomer
# ---------------------------------------------------------------------------

class PartnerQurtobaExtension(ModelExtension):
    """Link base.Partner to a QurtobaCustomer (many partners → one customer)."""

    _inherit = 'base.partner'
    _depends = ['base']

    qurtoba_customer = models.ForeignKey(
        'qurtoba.QurtobaCustomer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='partners',
        verbose_name=_('Qurtoba Customer'),
    )

    @property
    def has_qurtoba_customer(self) -> bool:
        """True when this partner is linked to a Qurtoba customer."""
        return self.qurtoba_customer_id is not None

    # -- WhatsApp template variables ---------------------------------------
    #
    # A WhatsAppTemplate whose "apply to" (content_type) is base.Partner reads
    # its {{placeholders}} straight off the record with a bare getattr —
    # WhatsAppTemplate.get_body_parameters() — so a property is indistinguishable
    # from a real column and needs no migration.
    #
    # Two hard rules for anything exposed here:
    #   1. NEVER return None or ''. Meta rejects a template whose example value
    #      is blank, and the examples are read off the newest Partner by pk
    #      (WhatsAppTemplate.get_example_field_values), which is almost never a
    #      Qurtoba-linked one. Unlinked partners must still yield a real string.
    #   2. Read the numbers from qurtoba.services.daily_totals, so the reminder
    #      can never disagree with the daily statement tool.

    @property
    def qurtoba_date(self) -> str:
        """The business day this reminder covers, as «الاثنين 24 اغسطس».

        Named explicitly in the message because the reminder goes out just
        after midnight — «اليوم» would be ambiguous at 00:10, and the day name
        makes it unmistakable which day closed.
        """
        from qurtoba.services.daily_totals import fmt_day_ar
        return fmt_day_ar()

    @property
    def qurtoba_partner_display(self) -> str:
        """Who we are greeting: the contact name, else their number."""
        from qurtoba.tools._phone import _normalize_phone
        name = (self.name or '').strip()
        if name:
            return name
        return _normalize_phone(self.phone) or 'عميلنا'

    @property
    def qurtoba_phone(self) -> str:
        """This partner's number in the local 01XXXXXXXXX form the customer reads."""
        from qurtoba.tools._phone import _normalize_phone
        return _normalize_phone(self.phone) or (self.phone or '—')

    @property
    def qurtoba_day_count(self) -> str:
        """How many transactions THIS number requested on the reported day."""
        from qurtoba.services.daily_totals import partner_day_totals
        return str(partner_day_totals(self)['count'])

    @property
    def qurtoba_total(self) -> str:
        """Total transferred by THIS number on the reported day — not the customer's."""
        from qurtoba.services.daily_totals import fmt_amount, partner_day_totals
        return fmt_amount(partner_day_totals(self)['debit'])

    @property
    def qurtoba_cust_name(self) -> str:
        """The Qurtoba account this number belongs to."""
        customer = self.qurtoba_customer
        return (getattr(customer, 'name', '') or '').strip() or '—'

    @property
    def qurtoba_customer_balance(self) -> str:
        """The whole account's balance — every number on it, not just this one.

        ABSOLUTE value on purpose. The sign carries the direction, and the
        direction is spelled out separately by qurtoba_balance_state; printing
        «-5,000 جنيه (ليك)» would show the customer a negative number for money
        that is owed TO them. Same rule as check_balance_and_send() above.
        """
        from qurtoba.services.daily_totals import fmt_amount
        customer = self.qurtoba_customer
        return fmt_amount(abs(getattr(customer, 'balance', 0) or 0))

    @property
    def qurtoba_balance_state(self) -> str:
        """Which way the balance runs, as the customer-facing word.

        Computed here rather than written into the WhatsApp template, because a
        template is one fixed string for everybody — it cannot say «عليك» to one
        customer and «ليك» to another. Mirrors check_balance_and_send():
            balance > 0  ⇒ the customer owes us            ⇒ عليك
            balance < 0  ⇒ the customer has credit with us ⇒ ليك
            balance == 0 ⇒ nothing outstanding             ⇒ خالص
        Never returns '' — a blank example value gets the template rejected by
        Meta, and «(  )» would read as a rendering fault.
        """
        balance = getattr(self.qurtoba_customer, 'balance', 0) or 0
        if balance > 0:
            return 'عليك'
        if balance < 0:
            return 'ليك'
        return 'خالص'

    @property
    def qurtoba_balance(self) -> str:
        """The account balance as one ready sentence: «عليك 412,907 جنيه».

        The direction word has to lead — Arabic puts «عليك»/«ليك» before the
        amount, not after it in brackets — and a zero balance needs different
        words entirely rather than «خالص 0 جنيه». Neither fits a fixed template
        string with the number slotted in, so the whole phrase is built here.
        Wording matches check_balance_and_send() so the nightly summary and the
        on-demand balance reply never word the same fact differently.
        """
        from qurtoba.services.daily_totals import fmt_amount
        balance = getattr(self.qurtoba_customer, 'balance', 0) or 0
        if balance > 0:
            return f'عليك {fmt_amount(abs(balance))} جنيه'
        if balance < 0:
            return f'ليك {fmt_amount(abs(balance))} جنيه'
        return 'مفيش مديونية'


# ---------------------------------------------------------------------------
# MessageQurtobaExtension — watermark on chat.Message marking which inbound
# messages the AI already turned into a Qurtoba transaction. Two jobs:
#   1. Idempotency backstop — a message already consumed must never produce a
#      second transaction when the workflow re-runs on the full conversation.
#   2. Live context — feeds <unprocessed_transactions> so the agent sees exactly
#      which burst lines are still open and never re-actions a done one.
# Added via the model-extension mechanism (no edit to the chat module's source).
# ---------------------------------------------------------------------------

class MessageQurtobaExtension(ModelExtension):
    """Marks chat.Message rows the AI consumed into a Qurtoba transaction."""

    _inherit = 'chat.message'
    _depends = ['base']

    # NOTE: `social_sent_at` and its composite index `chat_msg_conv_sent_seq_idx` now live
    # on core chat.Message (see modules/chat/models.py + migration 0028). `ingest_seq` was
    # dropped entirely. This extension no longer owns those — it only adds the AI-consumption
    # watermark below. The true-send-order query still reads `social_sent_at` (core field).

    ai_consumed_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        verbose_name=_('AI Consumed At'),
        help_text=_('Set when the AI created a Qurtoba transaction from this message.'),
    )
    qurtoba_record = models.ForeignKey(
        'qurtoba.QurtobaRecord',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='consumed_messages',
        verbose_name=_('Qurtoba Record'),
    )

    # CANCELLED ON ARRIVAL (owner decision 2026-09-14): set on every inbound message of a turn handled while
    # we were offline — the manual off-hours switch was on ('off_hours'), the AI was switched off ('ai_off'), or
    # the WhatsApp number was not linked to any Qurtoba customer yet ('not_linked').
    # Such a request was refused on the spot and can never become a transfer or a payment later: the create
    # tools refuse it as a source (qurtoba.switches.offline_cancellation) and the money path never re-plans it.
    qurtoba_offline_cancelled_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_('Cancelled while offline at'),
    )
    qurtoba_offline_reason = models.CharField(
        max_length=20, null=True, blank=True,
        verbose_name=_('Offline reason'),
    )

    # `social_sent_at` (the provider's true send time, used to order inbound WhatsApp bursts)
    # is now a core chat.Message field — no longer declared here. `ingest_seq` was dropped:
    # its only purpose was to guess sub-second order, which is unrecoverable, so the planner
    # clusters same-second messages and asks instead of guessing.

    def pre_create(self):
        """A WhatsApp Web customer group: an inbound kind the gateway cannot read is stored as
        'unsupported', and core then calls ``escalate_to_human()`` — which switches the AI off for the
        WHOLE group for good. Core's «do not process it with AI» still applies; only that escalation is
        suppressed (qurtoba.groups.suppress_escalation, read by the runtime patch on escalate_to_human)."""
        try:
            if getattr(self, 'direction', None) == 'inbound' and getattr(self, 'original_type', None) == 'unsupported':
                from qurtoba.groups import is_group, suppress_escalation
                if is_group(getattr(self, 'conversation', None)):
                    suppress_escalation(True)
        except Exception:
            pass

    def post_create(self):
        try:
            from qurtoba.groups import suppress_escalation
            suppress_escalation(False)
        except Exception:
            pass

    # ── WhatsApp customer groups: staff marking and the group's customer (owner decision 2026-09-23) ──

    @action
    def action_qurtoba_toggle_staff(self):
        """«موظف ⇄ عميل»: the senders of the selected group messages become office staff — in EVERY
        group at once — or, if they already all are, customers again."""
        from modules.base.models import Partner
        msgs = [m for m in self if getattr(m, 'direction', None) == 'inbound' and getattr(m, 'sender_id', None)]
        if not msgs:
            return {'status': False, 'open_mode': 'message', 'data': {},
                    'message': gettext('اختار رسالة واردة من عضو في الجروب')}
        senders = {m.sender_id: m.sender for m in msgs}
        make_staff = any(not getattr(p, 'employee', False) for p in senders.values())
        Partner._base_manager.filter(pk__in=list(senders)).update(employee=make_staff)
        names = '، '.join((p.name or '') for p in senders.values())
        role = gettext('موظف') if make_staff else gettext('عميل')
        try:
            from qurtoba.staff_notes import post_staff_note
            for conv in {m.conversation for m in msgs}:
                post_staff_note(conv, [f'👤 {names} ← {role} (في كل الجروبات)'],
                                subject=gettext('تغيير نوع عضو'), body=f'{names} ← {role}',
                                dedupe_key=f'staff_toggle:{conv.id}:{sorted(senders)}:{make_staff}', dedupe_ttl=30)
        except Exception:
            pass
        return {'status': True, 'open_mode': 'message', 'data': {},
                'message': gettext('%(names)s بقى %(role)s في كل الجروبات') % {'names': names, 'role': role}}

    def mark_ai_consumed(self, record=None):
        """Best-effort: flag this message as consumed into `record`.

        Uses ``.update()`` so it triggers no signals (no re-batching) and never
        raises into the tool flow. No-op if the watermark column isn't synced yet.
        """
        try:
            from django.utils import timezone
            from modules.chat.models import Message
            Message.objects_all.filter(pk=self.pk).update(
                ai_consumed_at=timezone.now(),
                qurtoba_record=record,
            )
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# WhatsAppAccountQurtobaExtension — per-account toggles for which Qurtoba
# transfer types the AI agent is allowed to create on this WhatsApp account.
#
# An admin can disable any type independently. When the corresponding flag is
# False the qurtoba transaction tools refuse to create that type and the agent
# responds with: "الخدمة <type> متوقفة حالياً، برجاء المحاولة في وقت لاحق
# وسيتم إبلاغك عند توفرها".
#
# Only transfer/debt types are gated here. Payments (سداد) are NOT toggleable —
# the customer must always be able to record a payment.
# ---------------------------------------------------------------------------

# Map QurtobaRecord.type → WhatsAppAccount flag name. Used by tools to look up.
QURTOBA_TYPE_FLAG_MAP = {
    'كاش':         'qurtoba_allow_cash',
    'كاش(5)':      'qurtoba_allow_cash_5',
    'كاش(10)':     'qurtoba_allow_cash_10',
    'كاش(20)':     'qurtoba_allow_cash_20',
    'فورى':        'qurtoba_allow_fawry',
    'أمان':        'qurtoba_allow_aman',
    'طاير':        'qurtoba_allow_tayer',
    'مصاريف خدمه': 'qurtoba_allow_service_fee',
}


class WhatsAppAccountQurtobaExtension(ModelExtension):
    """Per-WhatsApp-account toggles for which Qurtoba transfer types the AI can create."""

    _inherit = 'whatsapp.whatsappaccount'
    _depends = ['base']

    # Manual master switch for the AI agent on this account. OFF = the AI does nothing at
    # all: no transaction, no payment, no reply — and messages that arrive meanwhile are
    # marked handled, so switching it back on never replays them. Read by qurtoba.switches
    # (the workflow gate node, the money path, and every AI create tool).
    ai_agent_enabled = models.BooleanField(
        default=True,
        verbose_name=_('تفعيل الرد الآلي (AI)'),
        help_text=_('تشغيل/إيقاف رد الوكيل الذكي يدويًا لهذا الحساب. عند الإيقاف لا يتم تنفيذ أي معاملة ولا يتم الرد تلقائياً.'),
    )

    # Manual off-hours switch — owner decision 2026-09-13: flipped by hand, NEVER by the clock.
    # ON = no transaction or payment of any kind; the customer gets the off-hours notice.
    # Read by qurtoba.switches.
    qurtoba_off_hours = models.BooleanField(
        default=False,
        verbose_name=_('وضع خارج مواعيد العمل'),
        help_text=_('تشغيل يدوي فقط (لا يعمل بالتوقيت). أثناء التشغيل لا يتم تنفيذ أي معاملة أو سداد، ويستلم العميل رسالة خارج مواعيد العمل.'),
    )

    # The same office number is also linked to WhatsApp Web for the customers' groups (owner decision
    # 2026-09-23). ON: WhatsApp Web keeps the GROUPS only — 1:1 chats already arrive here through the
    # Cloud API, and storing them a second time would duplicate every chat (runtime_patches).
    qurtoba_wa_web_groups_only = models.BooleanField(
        default=True,
        verbose_name=_('واتساب ويب: الجروبات بس'),
        help_text=_('لما يكون شغال: واتساب ويب لنفس الرقم بيستقبل رسايل الجروبات بس، والشات الفردي يفضل على الـ API.'),
    )

    # Owner decision 2026-09-26: all service happens in the customer GROUPS. ON: a private chat gets
    # the fixed «الشغل في الجروبات بس» line (once per 6 h), never the AI or a transfer, and no nightly
    # statement goes to private numbers — only to the groups.
    qurtoba_private_closed = models.BooleanField(
        default=True,
        verbose_name=_('الشات الخاص مقفول — الشغل في الجروبات بس'),
        help_text=_('لما يكون شغال: أي رسالة على الخاص بيرد عليها رد ثابت إن الشغل في الجروبات بس، من غير ذكاء اصطناعي ولا تحويلات، وكشف نهاية اليوم بيتبعت للجروبات بس.'),
    )

    qurtoba_allow_cash = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ كاش'),
        help_text=_('السماح للوكيل الذكي بإنشاء معاملات كاش (أقل من 10,000) لهذا الحساب.'),
    )
    qurtoba_allow_cash_5 = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ كاش(5)'),
        help_text=_('السماح للوكيل الذكي بإنشاء معاملات كاش(5) — محجوزة حالياً.'),
    )
    qurtoba_allow_cash_10 = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ كاش(10)'),
        help_text=_('السماح للوكيل الذكي بإنشاء معاملات كاش(10) (10,000 إلى أقل من 20,000).'),
    )
    qurtoba_allow_cash_20 = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ كاش(20)'),
        help_text=_('السماح للوكيل الذكي بإنشاء معاملات كاش(20) (20,000 فأكثر).'),
    )
    qurtoba_allow_fawry = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ فورى'),
    )
    qurtoba_allow_aman = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ أمان'),
    )
    qurtoba_allow_tayer = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ طاير'),
    )
    qurtoba_allow_service_fee = models.BooleanField(
        default=True,
        verbose_name=_('السماح بـ مصاريف خدمه'),
    )

    # Stable display order — the order shown to the AI in the prompt
    _QURTOBA_TYPE_DISPLAY_ORDER = [
        'كاش', 'كاش(10)', 'كاش(20)', 'كاش(5)',
        'فورى', 'أمان', 'طاير', 'مصاريف خدمه',
    ]

    @classmethod
    def get_service_availability_data(cls, phone='201505459442'):
        """
        Fetch the WhatsApp account by phone number and return the Qurtoba
        service-availability snapshot — ready to inject into the AI prompt.

        Usage:
            data = WhatsAppAccount.get_service_availability_data('201505459442')

        Returns (always the same shape — never None, even if the account is
        missing or has no Qurtoba flags):
            {
                'phone':           str,
                'account_found':   bool,
                'available':       [<types currently enabled>],
                'disabled':        [<types currently disabled>],
                'flags':           {<flag_name>: bool, ...},
                'pretty_ar':       '<Arabic block ready for prompt>',
                'has_any_enabled': bool,
                'has_any_disabled': bool,
            }
        """
        from modules.whatsapp.models.account import WhatsAppAccount

        account = WhatsAppAccount.objects.filter(phone_number=phone).first()

        result = {
            'phone': phone,
            'account_found': account is not None,
            'available': [],
            'disabled': [],
            'flags': {},
            'pretty_ar': '',
            'has_any_enabled': False,
            'has_any_disabled': False,
        }

        if account is None:
            result['pretty_ar'] = f'(لم يتم العثور على حساب واتساب لرقم {phone})'
            return result

        for type_name in cls._QURTOBA_TYPE_DISPLAY_ORDER:
            flag_attr = QURTOBA_TYPE_FLAG_MAP[type_name]
            value = bool(getattr(account, flag_attr, True))
            result['flags'][flag_attr] = value
            (result['available'] if value else result['disabled']).append(type_name)

        result['has_any_enabled'] = bool(result['available'])
        result['has_any_disabled'] = bool(result['disabled'])

        if result['available'] and result['disabled']:
            result['pretty_ar'] = (
                f'الخدمات المتاحة حالياً: {"، ".join(result["available"])}\n'
                f'الخدمات المتوقفة حالياً: {"، ".join(result["disabled"])}'
            )
        elif result['available']:
            result['pretty_ar'] = (
                f'الخدمات المتاحة حالياً: {"، ".join(result["available"])}\n'
                f'(لا توجد خدمات متوقفة)'
            )
        elif result['disabled']:
            result['pretty_ar'] = (
                f'(لا توجد خدمات متاحة حالياً)\n'
                f'الخدمات المتوقفة حالياً: {"، ".join(result["disabled"])}'
            )
        else:
            result['pretty_ar'] = '(لا توجد إعدادات خدمات)'

        return result


# ---------------------------------------------------------------------------
# ConversationQurtobaExtension — action buttons in the chat conversation panel
# ---------------------------------------------------------------------------

class ConversationQurtobaExtension(ModelExtension):
    """
    Adds Qurtoba action buttons to the chat/WhatsApp conversation panel.
    Customer is resolved from conversation.social_partner.qurtoba_customer.
    """

    _inherit = 'chat.conversation'
    _depends = ['base']

    def template_context_extras(self):
        """Live values surfaced into ``{{ conversation.* }}`` for the agent prompt.

        ``unprocessed_transactions``: the inbound text lines not yet turned into a
        Qurtoba transaction (``ai_consumed_at`` is null), each tagged with its
        ``[message_id]``.

        Scope is a PURE RECENCY window: a message stays "open" (and linkable by its
        [message_id]) until it is CONSUMED into a transaction or ages out. We do NOT
        cut the window at the agent's last reply — a number the agent just asked a
        clarifying question about was sent BEFORE that reply, and it MUST stay visible
        so the resulting transaction can carry its source_message_id (required for the
        auto receipt mention when Cash-SYS pays it). Stale/abandoned bursts fall out
        of the window by time + the watermark; the planner handles any overlap. The
        window (``AI_UNPROCESSED_WINDOW_MIN``, default 6 min) comfortably covers a
        clarification round-trip while excluding an old burst. Best-effort — any
        failure renders an empty block rather than breaking prompt rendering.
        """
        try:
            from datetime import timedelta
            from django.conf import settings as dj_settings
            from django.utils import timezone
            from modules.chat.models import Message

            from django.db.models import F
            from django.db.models.functions import Coalesce
            window_min = getattr(dj_settings, 'AI_UNPROCESSED_WINDOW_MIN', 6)
            cutoff = timezone.now() - timedelta(minutes=window_min)
            # Recency window stays on created_at (arrival-based, correct). The SORT key is
            # the true send order (social_sent_at, created_at fallback), id as a deterministic
            # tiebreak, so a forwarded burst reaches the planner in the order it was sent.
            rows = list(
                Message.objects_all
                .filter(conversation=self, direction='inbound', active=True,
                        type='text', ai_consumed_at__isnull=True,
                        created_at__gte=cutoff)
                .annotate(_ord=Coalesce('social_sent_at', 'created_at'))
                # social_sent_at is second-precision; a same-second burst ties on _ord, so
                # break the tie by created_at (microsecond arrival = true within-second order)
                # before id. id alone is random and scrambled same-second phone/amount pairs.
                .order_by(F('_ord').desc(), F('created_at').desc(), F('id').desc())[:40]
            )[::-1]  # back to chronological (true send) order
            # A message that repeats a transfer already created today is the case the
            # model handles worst: it sees the earlier 👍 in history, decides "duplicate"
            # on its own and answers with a bare 👍 or nothing — no tool call, so the
            # tool's «تحب أكررها؟» never fires and the customer gets silence (sandbox
            # 2026-09-03, scenario E2, twice). A deterministic, message-specific warning
            # here outranks any general rule in the prompt.
            repeat_note = ''
            try:
                from qurtoba.models import QurtobaRecord
                from qurtoba.tools.planning import _classify_message
                customer = getattr(getattr(self, 'social_partner', None), 'qurtoba_customer', None)
                day_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)

                def _is_repeat_of_today(text):
                    if customer is None:
                        return False
                    cls = _classify_message(text)
                    if len(cls['phones']) != 1 or len(cls['amounts']) != 1:
                        return False
                    return QurtobaRecord.objects.filter(
                        customer=customer, account_number=cls['phones'][0],
                        value=cls['amounts'][0], created_at__gte=day_start,
                    ).exists()
                repeat_note = (' ← ⚠️ تكرار لتحويل اتعمل النهارده فعلاً: ابعتها لأداة الإنشاء زي أي طلب '
                               '(الأداة هي اللي تسأل «تحب أكررها؟»). ممنوع تحكم إنها مكررة بنفسك، '
                               'ممنوع 👍، ممنوع الصمت.')
            except Exception:
                _is_repeat_of_today = lambda text: False  # noqa: E731

            lines = []
            for m in rows:
                c = m.content
                txt = c.get('text') if isinstance(c, dict) else None
                if not txt:
                    continue
                flat = ' '.join(str(txt).split())
                suffix = repeat_note if repeat_note and _is_repeat_of_today(flat) else ''
                lines.append(f"[message_id: {m.id}] {flat}{suffix}")
            if not lines:
                return {'unprocessed_transactions': ''}
            # Loud, deterministic priority flag emitted EVERY run there are still-open
            # inbound lines. In a long, system-message-heavy chat the model has drifted and
            # answered an OLD courtesy/blessing while a fresh transaction burst sat unhandled
            # (replied «العفو» to «الله ينور» and ignored the numbers). These lines are the
            # customer's CURRENT request and outrank any greeting/thanks/blessing in history.
            header = ('⚠️ رسائل العميل دي لسه متعالجتش وهي طلبه الحالي — عالِجها الأول '
                      '(شغّل الـplanner/التحويلات) قبل أي رد اجتماعي، ومتردّش على تحية/دعاء '
                      'قديم بدل ما تعالجها:')
            return {'unprocessed_transactions': header + '\n' + '\n'.join(lines)}
        except Exception:
            return {'unprocessed_transactions': ''}

    @action
    def action_qurtoba_new_debt(self):
        """
        سداد — open payment/collection form (شراء كاش / شراء فورى).
        Reduces customer balance: isDown=True, isSeller=False.
        """
        conv, customer = _get_conv_and_customer(self)
        if not customer:
            return {
                'status': False,
                'open_mode': 'message',
                'message': gettext('هذه المحادثة غير مرتبطة بعميل قرطبة'),
                'data': {},
            }
        grade_limit     = customer.grade * 1000 if customer.grade else None
        current_balance = customer.balance or 0
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'auto_close': True,
            'data': {
                'menu_item_key': 'qurtoba_action_quick_collection',
                'view_type': 'form',
                'type': 'action',
                'title': gettext('سداد'),
                'context': {
                    'default_fields': {
                        'customer':         customer,
                        'partner':          conv.social_partner if conv else None,
                        'grade_limit':      grade_limit,
                        'customer_balance': current_balance,
                        'extends_by':       0,
                        'is_down':          True,
                        'is_seller':        False,
                    }
                },
            },
        }

    @action
    def action_qurtoba_new_transaction(self):
        """
        عملية جديدة — open transaction form with account selector (debt types, isDown=False).
        Adds to customer balance.
        """
        conv, customer = _get_conv_and_customer(self)
        if not customer:
            return {
                'status': False,
                'open_mode': 'message',
                'message': gettext('هذه المحادثة غير مرتبطة بعميل قرطبة'),
                'data': {},
            }
        grade_limit     = customer.grade * 1000 if customer.grade else None
        current_balance = customer.balance or 0
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'auto_close': True,
            'data': {
                'menu_item_key': 'qurtoba_action_quick_transaction',
                'view_type': 'form',
                'type': 'action',
                'title': gettext('عملية جديدة'),
                'context': {
                    'default_fields': {
                        'customer':         customer,
                        'partner':          conv.social_partner if conv else None,
                        'grade_limit':      grade_limit,
                        'customer_balance': current_balance,
                        'extends_by':       0,
                        'is_down':          False,
                        'is_seller':        False,
                    }
                },
            },
        }

    @action
    def action_qurtoba_link_group(self):
        """«ربط الجروب بعميل قرطبة»: pick the Qurtoba customer this WhatsApp group is for (or clear it).
        The link belongs to the GROUP, never to its members' numbers; many groups may share a customer.
        Opens the wizard pre-filled with the current link; action_qurtoba_save_group_link saves it."""
        from qurtoba.groups import is_group
        conv, customer = _get_conv_and_customer(self)
        if conv is None or not is_group(conv):
            return {'status': False, 'open_mode': 'message', 'data': {},
                    'message': gettext('الزرار ده لجروبات واتساب بس')}
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'data': {
                'view_key': 'qurtoba_group_link_wizard_form',
                'view_type': 'form',
                'id': None,
                'action_name': 'action_qurtoba_save_group_link',
                'model': 'chat.conversation',        # Save runs on THIS group, with the wizard as `form`
                'selected_ids': [str(conv.id)],
                'type': 'action',
                'title': gettext('ربط الجروب بعميل قرطبة'),
                'context': {'default_fields': {
                    'group_name': conv.name or '',
                    'customer': {'id': customer.pk, 'name': customer.name} if customer else None,
                }},
            },
        }

    @action
    def action_qurtoba_save_group_link(queryset, form=None):
        """Save of the «ربط الجروب بعميل قرطبة» wizard: ``queryset`` is the group, ``form`` the
        QurtobaGroupLinkWizard (its customer, or empty to unlink)."""
        from qurtoba.groups import is_group, link_group
        conv = queryset.first() if hasattr(queryset, 'first') else None
        if conv is None or not is_group(conv) or form is None:
            return {'status': False, 'open_mode': 'message', 'data': {},
                    'message': gettext('الزرار ده لجروبات واتساب بس')}
        try:
            from modules.base.middleware import get_current_user
            user = get_current_user()
        except Exception:
            user = None
        customer_id = getattr(form, 'customer_id', None)
        link_group(conv, customer_id, by=getattr(user, 'name', None) or getattr(user, 'username', None))
        return {'status': True, 'open_mode': 'message', 'data': {}, 'on_success': {'type': 'refresh'},
                'message': gettext('الجروب اتربط بالعميل') if customer_id else gettext('اتشال ربط الجروب')}

    @action
    def action_qurtoba_check_balance(self):
        """
        Fetch the customer's balance and send it as an outbound message
        on this conversation (auto-dispatched via WhatsApp/Messenger).
        Reusable: calls check_balance_and_send() which can be used elsewhere.
        """
        conv, customer = _get_conv_and_customer(self)
        if not customer:
            return {
                'status': False,
                'open_mode': 'message',
                'message': gettext('هذه المحادثة غير مرتبطة بعميل قرطبة'),
                'data': {},
            }
        try:
            check_balance_and_send(conv, customer)
        except Exception as e:
            return {
                'status': False,
                'open_mode': 'message',
                'message': gettext('فشل إرسال الرصيد: %(err)s') % {'err': str(e)},
                'data': {},
            }
        return {
            'status': True,
            'open_mode': 'message',
            'message': gettext('تم إرسال رصيد العميل على المحادثة'),
            'data': {},
        }

    @action
    def action_qurtoba_transactions(self):
        """Open a slideover with all transactions for the linked customer."""
        conv, customer = _get_conv_and_customer(self)
        if not customer:
            return {
                'status': False,
                'open_mode': 'message',
                'message': gettext('هذه المحادثة غير مرتبطة بعميل قرطبة'),
                'data': {},
            }
        return {
            'status': True,
            'open_mode': 'slideover',
            'data': {
                'menu_item_key': 'qurtoba_menu_records',
                'view_type': 'list',
                'type': 'action',
                'title': gettext('معاملات: %(name)s') % {'name': customer.name},
                'domain': {
                    'filters': {
                        'operator': 'and',
                        'filters': [
                            {'field': 'customer_id', 'operator': 'eq', 'value': customer.id}
                        ]
                    }
                },
            },
        }
