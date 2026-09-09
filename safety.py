"""Guards against losing chat history by deleting a person.

``Message.sender`` and ``Conversation.created_by`` cascade on Partner (core), and a User
cascades to its Partner. On 2026-09-09 the admin account the automation sent as was
deleted and every message it had sent — notices, questions, receipts — went with it.

Deleting a Partner (or the User that owns one) that has sent messages or created
conversations is refused with a clear message: deactivate the account instead.
"""
import logging

from django.db.models.signals import pre_delete

logger = logging.getLogger(__name__)


def _history_of(partner):
    try:
        sent = partner.sent_messages.count()
    except Exception:
        sent = 0
    try:
        convs = partner.created_conversations.count()
    except Exception:
        convs = 0
    return sent, convs


def _refuse(partner):
    from django.core.exceptions import ValidationError
    sent, convs = _history_of(partner)
    if not sent and not convs:
        return
    raise ValidationError(
        f'لا يمكن حذف «{partner.name}»: عليه {sent} رسالة مرسلة و{convs} محادثة، وحذفه يمسحها كلها. '
        'اعمل له إلغاء تفعيل (active = False) بدل الحذف.'
    )


def _on_partner_delete(sender, instance, **kwargs):
    _refuse(instance)


def _on_user_delete(sender, instance, **kwargs):
    partner = getattr(instance, 'partner', None)
    if partner is not None:
        _refuse(partner)


def install():
    from django.contrib.auth import get_user_model
    from modules.base.models import Partner
    pre_delete.connect(_on_partner_delete, sender=Partner, dispatch_uid='qurtoba_partner_delete_guard')
    pre_delete.connect(_on_user_delete, sender=get_user_model(), dispatch_uid='qurtoba_user_delete_guard')
    logger.info('qurtoba.safety: partner/user delete guard installed')
