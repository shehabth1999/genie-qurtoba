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


def _get_system_partner(conversation):
    """
    Return the internal agent/staff Partner to use as sender.
    Priority: active internal member → conversation creator → any staff partner.
    Uses ChatBridgeService pattern: system_partner = the agent/staff.
    """
    from modules.chat.models import ConversationMember
    from modules.base.models.partner import Partner

    # 1. Try an active internal member (has a user, not the external social_partner)
    member = (
        ConversationMember.objects
        .filter(conversation=conversation, active=True)
        .exclude(user=None)
        .select_related('user')
        .first()
    )
    if member and member.user_id:
        # User.partner is a reverse relation: User → Partner FK
        partner = Partner.objects.filter(user__id=member.user_id).first()
        if partner:
            return partner

    # 2. Fall back to the conversation creator (always a Partner from BaseModel)
    if conversation.created_by_id:
        return conversation.created_by

    # 3. Last resort: any active non-system partner
    return Partner.objects.filter(system_user=False, active=True).first()


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

    class Meta:
        # Composite index backing the true-send-order query for inbound WhatsApp
        # bursts (qurtoba reads social_sent_at + ingest_seq to order them — see
        # ConversationQurtobaExtension.template_context_extras and tools/planning.py).
        # Registered Odoo-style via the extension Meta mechanism and materialized by
        # sync_schema. The name MUST stay 'chat_msg_conv_sent_seq_idx': the identical
        # index already exists in the DB (created by the now-removed chat migration
        # 0027), so keeping the name makes sync_schema a no-op rather than a duplicate.
        indexes = [
            models.Index(
                fields=['conversation_id', 'social_sent_at', 'ingest_seq'],
                name='chat_msg_conv_sent_seq_idx',
            ),
        ]

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

    # True message-ordering keys for inbound WhatsApp bursts — moved here from
    # chat.Message (core) so the qurtoba feature owns its own schema. created_at
    # stays = arrival/insert time (the agent freshness gate uses it); these capture
    # the provider's real SEND order instead. Written at webhook receipt; read by the
    # qurtoba planner and the <unprocessed_transactions> block. Null for old rows /
    # channels without them — ordering then falls back to created_at. The DB columns
    # already exist (ex-migration 0027), so sync_schema no-ops on them.
    social_sent_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        verbose_name=_('Social Sent At'),
        help_text=_("Provider's true send time (e.g. WhatsApp message timestamp, "
                    "second-granularity). Null when unavailable; ordering falls "
                    "back to created_at."),
    )
    ingest_seq = models.BigIntegerField(
        null=True, blank=True,
        verbose_name=_('Ingest Seq'),
        help_text=_("Monotonic per-conversation sequence captured at webhook receipt, "
                    "before concurrent task dispatch. Tiebreaker for same-second "
                    "forwarded bursts."),
    )

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

    # Manual master switch for the AI agent on this account. Toggled by hand
    # (enable/disable); the AI flow is wired to this by the integrator.
    ai_agent_enabled = models.BooleanField(
        default=True,
        verbose_name=_('تفعيل الرد الآلي (AI)'),
        help_text=_('تشغيل/إيقاف رد الوكيل الذكي يدويًا لهذا الحساب.'),
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
            # the true send order (social_sent_at + ingest_seq, created_at fallback) so a
            # forwarded burst reaches the planner in the order the customer actually sent.
            rows = list(
                Message.objects_all
                .filter(conversation=self, direction='inbound', active=True,
                        type='text', ai_consumed_at__isnull=True,
                        created_at__gte=cutoff)
                .annotate(_ord=Coalesce('social_sent_at', 'created_at'))
                .order_by(F('_ord').desc(), F('ingest_seq').desc(nulls_last=True))[:40]
            )[::-1]  # back to chronological (true send) order
            lines = []
            for m in rows:
                c = m.content
                txt = c.get('text') if isinstance(c, dict) else None
                if not txt:
                    continue
                lines.append(f"[message_id: {m.id}] {' '.join(str(txt).split())}")
            return {'unprocessed_transactions': '\n'.join(lines)}
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
