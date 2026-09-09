"""Guards against losing chat history, or an integration credential, by deleting a person.

``Message.sender`` and ``Conversation.created_by`` cascade on Partner (core), and a User
cascades to its Partner. On 2026-09-09 the admin account the automation sent as was
deleted and every message it had sent — notices, questions, receipts — went with it.

The same delete also destroyed the API token Qurtoba authenticates its record push
with (``authtoken_token`` cascades on user), so every push was refused with 401 from
18:33 that day and 27 ledger rows never arrived.

Deleting a Partner (or the User that owns one) that has sent messages, created
conversations, or owns an API token is refused with a clear message: deactivate the
account instead.
"""
import logging

from django.db.models.signals import pre_delete

logger = logging.getLogger(__name__)


def _history_of(partner):
    if partner is None:
        return 0, 0
    try:
        sent = partner.sent_messages.count()
    except Exception:
        sent = 0
    try:
        convs = partner.created_conversations.count()
    except Exception:
        convs = 0
    return sent, convs


def _has_api_token(user) -> bool:
    """Does this login own an API token? Deleting it revokes the credential every
    integration that holds it is using (2026-09-09: the Qurtoba record push died)."""
    if user is None:
        return False
    try:
        from rest_framework.authtoken.models import Token
        return Token.objects.filter(user=user).exists()
    except Exception:
        return False


def _refuse(partner, user=None):
    from django.core.exceptions import ValidationError
    sent, convs = _history_of(partner)
    name = getattr(partner, 'name', None) or getattr(user, 'email', '') or '—'
    if user is None:
        user = getattr(partner, 'user', None)
    if _has_api_token(user):
        raise ValidationError(
            f'لا يمكن حذف «{name}»: الحساب ده عليه مفتاح API (توكن) بيستخدمه سيرفر قرطبة في إرسال '
            'العمليات، وحذفه بيوقف الترحيل فوراً. اعمل له إلغاء تفعيل (active = False) بدل الحذف.'
        )
    if not sent and not convs:
        return
    raise ValidationError(
        f'لا يمكن حذف «{name}»: عليه {sent} رسالة مرسلة و{convs} محادثة، وحذفه يمسحها كلها. '
        'اعمل له إلغاء تفعيل (active = False) بدل الحذف.'
    )


def _on_partner_delete(sender, instance, **kwargs):
    _refuse(instance)


def _on_user_delete(sender, instance, **kwargs):
    _refuse(getattr(instance, 'partner', None), user=instance)


def install():
    from django.contrib.auth import get_user_model
    from modules.base.models import Partner
    pre_delete.connect(_on_partner_delete, sender=Partner, dispatch_uid='qurtoba_partner_delete_guard')
    pre_delete.connect(_on_user_delete, sender=get_user_model(), dispatch_uid='qurtoba_user_delete_guard')
    logger.info('qurtoba.safety: partner/user delete guard installed')
