"""
Sandboxed end-to-end runner for the Qurtoba WhatsApp agent.

What is real: the workflow graph in AI Studio, the prompts, the model, the planner,
the create tool's validation, the outbound gate. What is sandboxed:

  * a dedicated sandbox partner + sandbox Qurtoba customer + conversation, so no
    real customer's chat, balance or ledger is touched;
  * inbound rows are inserted WITHOUT a WhatsApp id, so chat.Message.post_create
    never schedules the Celery batching task (the run is driven in-process);
  * every WhatsApp send (text, media, reaction, template) is captured instead of
    delivered — the capture still asks the outbound gate for its verdict
    (``ai_guard.decide`` → send / block / forward-as-a-quote, or the older
    ``block_reason`` while ``decide`` is not deployed) and still writes the
    outbound chat row, so multi-turn scenarios see the agent's own previous
    question exactly as production would;
  * the Qurtoba push task and the human-alert notification are stubbed;
  * ledger rows created for the sandbox customer are deleted after each scenario.

Usage: ``manage.py qurtoba_ai_eval [--only A1,B2] [--out DIR] [--keep]``.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

RUN_TOKEN = str(int(time.time()))   # a fresh LangGraph thread per run, never a resumed one
SANDBOX_PARTNER_NAME = 'SANDBOX · AI EVAL'
SANDBOX_CUSTOMER_NAME = 'SANDBOX عميل اختبار الذكاء'
SANDBOX_PHONE = '201000000099'
WORKFLOW_ID = 2
WHATSAPP_ACCOUNT_ID = 3

# WhatsApp Web customer-group sandbox (owner decision 2026-09-23): a never-connected WhatsApp Web
# account, one group, three members — the sandbox customer (same phone as the 1:1 sandbox partner, so
# the group links to the sandbox customer on its own), an employee of the customer, office staff.
GROUP_WORKFLOW_ID = None            # set by the command (--workflow for a group run)
WA_WEB_SANDBOX_NAME = 'SANDBOX · WhatsApp Web'
WA_WEB_SANDBOX_PHONE = '201000000097'
GROUP_SANDBOX_JID = '120363000000000099@g.us'
GROUP_SANDBOX_SUBJECT = 'SANDBOX · جروب اختبار'
GROUP_MEMBERS = {
    'customer': ('201000000099', 'SANDBOX · العميل'),
    'employee': ('201000000096', 'SANDBOX · موظف العميل'),
    'staff': ('201000000095', 'SANDBOX · موظف المكتب'),
}


# ── captured side effects ────────────────────────────────────────────────────

@dataclass
class Capture:
    sends: List[Dict[str, Any]] = field(default_factory=list)
    reactions: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    pushes: List[int] = field(default_factory=list)
    notifications: List[Dict[str, Any]] = field(default_factory=list)
    counter: int = 0

    def agent_sends(self) -> List[Dict[str, Any]]:
        return [s for s in self.sends if _is_agent_send(s)]

    def agent_texts(self) -> List[str]:
        return [s['text'] for s in self.agent_sends()]

    def tool_texts(self) -> List[str]:
        return [s.get('text') or s.get('caption') or '' for s in self.sends
                if s['system_send'] and not s['blocked']]

    def blocked(self) -> List[Dict[str, Any]]:
        return [s for s in self.sends if s['blocked']]


def _is_agent_send(s: Dict[str, Any]) -> bool:
    """A customer-visible AGENT text: written by the AI partner outside a tool's
    ``system_send()`` and let through by the gate — delivered as-is ('send') or
    re-attached by the gate as a quote on the customer's message ('forward')."""
    if s.get('kind') == 'text' and s.get('automation') and not s.get('blocked'):
        return True     # workflow v2: the automation's fixed line IS the agent's reply
    return (s.get('kind') == 'text' and s.get('by_ai') and not s.get('system_send')
            and s.get('action', 'block' if s.get('blocked') else 'send') in ('send', 'forward'))


def _gate_verdict(content, message_type, conversation, system_partner, *, reply_to_id=None, reply_to_message_id=None) -> Dict[str, Any]:
    """Ask the outbound gate. Prefers the new ``ai_guard.decide`` contract
    ({'action': 'send'|'block'|'forward', 'reason', 'forward_to'}); falls back to the
    older ``block_reason`` while that contract is not deployed yet."""
    from qurtoba import ai_guard
    decide = getattr(ai_guard, 'decide', None)
    if callable(decide):
        verdict = decide(content, message_type, conversation, system_partner,
                         reply_to_id=reply_to_id, reply_to_message_id=reply_to_message_id) or {}
        action = verdict.get('action') or 'send'
        if action not in ('send', 'block', 'forward'):
            action = 'send'
        return {'action': action, 'reason': verdict.get('reason'), 'forward_to': verdict.get('forward_to'),
                'contract': 'decide'}
    reason = ai_guard.block_reason(content, message_type, conversation, system_partner)
    return {'action': 'block' if reason else 'send', 'reason': reason, 'forward_to': None, 'contract': 'block_reason'}


@contextlib.contextmanager
def sandbox_patches(capture: Capture, conversation, ai_partner):
    """Replace every outward-facing call with a recorder for the duration of a run."""
    from modules.whatsapp.services.api import WhatsAppAPIService
    from modules.chat.models import Conversation, Message
    from qurtoba import ai_guard
    from qurtoba.tasks import push_record_to_qurtoba_task

    originals = {
        'sab': WhatsAppAPIService.send_and_broadcast,
        'text': WhatsAppAPIService.send_text_message,
        'media': WhatsAppAPIService.send_media_message,
        'react': WhatsAppAPIService.send_reaction,
        'tmpl': WhatsAppAPIService.send_template_message,
        'alert': Conversation.alert_human,
        'push_delay': push_record_to_qurtoba_task.delay,
        'push_async': push_record_to_qurtoba_task.apply_async,
    }

    def _record_send(content, message_type, system_partner, reply_to_id, caption, filename, reply_to_message_id=None):
        capture.counter += 1
        text = content.get('text') if isinstance(content, dict) else (content if isinstance(content, str) else None)
        by_ai = bool(getattr(system_partner, 'ai_agent', False))
        system_send = ai_guard.in_system_send()
        try:
            from qurtoba.automation.context import in_automation_reply
            automation = in_automation_reply()
        except Exception:
            automation = False
        verdict = _gate_verdict(content, message_type, conversation, system_partner,
                                reply_to_id=reply_to_id, reply_to_message_id=reply_to_message_id)
        action = verdict['action']
        forward_to = verdict.get('forward_to') if action == 'forward' else None
        if action == 'forward' and forward_to is None:
            # the gate said "quote it on the customer's message" but named none —
            # nothing to attach to, so it goes out as it was
            action = 'send'
        blocked = verdict.get('reason') if action == 'block' else None
        forwarded = action == 'forward'
        quoted = bool(reply_to_id or reply_to_message_id) or forwarded
        entry = {
            'n': capture.counter, 'kind': 'text' if message_type == 'text' else message_type,
            'text': text, 'caption': caption, 'filename': filename,
            'url': content.get('url') if isinstance(content, dict) else None,
            'by_ai': by_ai, 'system_send': system_send, 'automation': automation,
            'action': action, 'blocked': blocked, 'forwarded': forwarded, 'quoted': quoted,
            'gate_reason': verdict.get('reason'), 'gate_contract': verdict.get('contract'),
            'reply_to_id': str(forward_to.id) if forwarded else (str(reply_to_id) if reply_to_id else None),
            'sender': getattr(system_partner, 'name', None),
        }
        capture.sends.append(entry)
        chat_message_id = None
        if not blocked:
            try:
                from django.contrib.contenttypes.models import ContentType
                if forwarded:
                    reply_to = forward_to
                else:
                    reply_to = Message.objects_all.filter(id=reply_to_id).first() if reply_to_id else None
                sa = conversation.social_account
                row = Message.objects_all.create(
                    conversation=conversation, sender=system_partner or ai_partner,
                    type='text' if message_type == 'text' else message_type,
                    content=content if isinstance(content, dict) else {'text': content},
                    direction='outbound', status='delivered',
                    social_id=(f'wa_web:sandbox:{capture.counter}' if getattr(conversation, 'type', None) == 'wa_web'
                               else f'wamid.sandbox.{capture.counter}'), reply_to=reply_to,
                    social_sent_at=timezone.now(),
                    social_account_content_type=ContentType.objects.get_for_model(sa) if sa else None,
                    social_account_object_id=sa.id if sa else None,
                )
                chat_message_id = str(row.id)
                entry['chat_message_id'] = chat_message_id
            except Exception:
                logger.exception('sandbox: could not write outbound row')
        return {'success': not blocked, 'message_id': f'wamid.sandbox.{capture.counter}',
                'chat_message_id': chat_message_id, 'conversation_id': str(conversation.id),
                'error': blocked and f'blocked:{blocked}' or None}

    def fake_sab(self, partner, content, *, message_type='text', caption=None, filename=None,
                 reply_to_message_id=None, reply_to_id=None, preview_url=False, conversation=None,
                 system_partner=None, websocket=True, skip_bridge=False):
        return _record_send(content, message_type, system_partner, reply_to_id, caption, filename,
                            reply_to_message_id=reply_to_message_id)

    def fake_text(self, partner, text, *args, **kwargs):
        return _record_send({'text': text}, 'text', ai_partner, None, None, None)

    def fake_media(self, partner, media_link, media_type, caption=None, filename=None, reply_to_message_id=None):
        return _record_send({'url': media_link}, media_type, ai_partner, None, caption, filename)

    def fake_react(self, to_number, message_id, emoji):
        capture.reactions.append({'to': to_number, 'message_id': message_id, 'emoji': emoji})
        return {'success': True, 'messages': [{'id': 'wamid.sandbox.reaction'}]}

    def fake_tmpl(self, template, partner, *args, **kwargs):
        capture.counter += 1
        capture.sends.append({'n': capture.counter, 'kind': 'template', 'text': getattr(template, 'template_name', '?'),
                              'by_ai': False, 'system_send': True, 'blocked': None, 'action': 'send', 'forwarded': False,
                              'quoted': False, 'reply_to_id': None, 'caption': None, 'filename': None})
        return {'message_id': f'wamid.sandbox.{capture.counter}', 'display_data': {}}

    def fake_alert(self, notify_dm=True, notify_push=True, mark_favorite=True, message=None):
        capture.alerts.append({'message': message, 'push': notify_push})

    def fake_push(*args, **kwargs):
        pk = (args[0] if args else None) or (kwargs.get('args') or [None])[0]
        capture.pushes.append(pk)

    # Reaction rows created by the tools would enqueue a real Celery task that
    # tries to deliver the reaction to a sandbox WhatsApp id and fails — 76
    # failed tasks on the 3-slot production worker in one day of evaluations.
    try:
        from modules.whatsapp.tasks import process_handling_reaction
        originals['react_delay'] = process_handling_reaction.delay
        originals['react_async'] = process_handling_reaction.apply_async
        process_handling_reaction.delay = lambda *a, **k: None
        process_handling_reaction.apply_async = lambda *a, **k: None
    except Exception:
        pass

    # WhatsApp Web (customer groups): the channel's send path, captured BELOW the tenant gate exactly like
    # the Cloud one — the gate's verdict is asked here, the silent-turn marker is dropped unsent.
    wa_web_service = None
    try:
        from django.apps import apps as _apps
        if _apps.is_installed('modules.wa_web'):
            from modules.wa_web.services.send_service import WaWebService as wa_web_service
            originals['wa_web_send'] = wa_web_service.send_omnichannel

            def fake_wa_web_send(self, partner, content, *, message_type='text', caption=None, filename=None,
                                 reply_to_social_id=None, conversation=None, system_partner=None, websocket=True,
                                 preview_url=False, bulk=False, paced=False, attachment=None):
                from qurtoba.groups import SILENT_SENTINEL
                text = content.get('text') if isinstance(content, dict) else (content if isinstance(content, str) else None)
                if text is not None and text.strip() == SILENT_SENTINEL:
                    return {'success': True, 'message_id': None, 'social_id': None, 'channel': 'wa_web', 'silent': True}
                reply_to_id = None
                if reply_to_social_id:
                    reply_to_id = (Message.objects_all.filter(social_id=str(reply_to_social_id))
                                   .values_list('id', flat=True).first())
                res = _record_send(content, message_type, system_partner, str(reply_to_id) if reply_to_id else None,
                                   caption, filename, reply_to_message_id=reply_to_social_id)
                return {'success': res.get('success'), 'message_id': res.get('chat_message_id'),
                        'social_id': res.get('message_id'), 'channel': 'wa_web', 'error': res.get('error')}

            wa_web_service.send_omnichannel = fake_wa_web_send
            from modules.wa_web.tasks.outbound import send_wa_web_reaction
            originals['wa_react_delay'] = send_wa_web_reaction.delay
            send_wa_web_reaction.delay = lambda *a, **k: None
    except Exception:
        logger.exception('sandbox: WhatsApp Web capture not installed')

    WhatsAppAPIService.send_and_broadcast = fake_sab
    WhatsAppAPIService.send_text_message = fake_text
    WhatsAppAPIService.send_media_message = fake_media
    WhatsAppAPIService.send_reaction = fake_react
    WhatsAppAPIService.send_template_message = fake_tmpl
    Conversation.alert_human = fake_alert
    push_record_to_qurtoba_task.delay = fake_push
    push_record_to_qurtoba_task.apply_async = fake_push

    # Staff notifications (split / image requests) are recorded, never delivered: a sandbox run must not
    # put «SANDBOX …» in the office's inbox or on their phones (it did on 2026-09-26).
    import modules.notifications.services as _notif_services
    originals['post_notification'] = _notif_services.post_notification
    # The 1:1 scenarios exercise the money logic the groups share; private chats being closed on the
    # office number (2026-09-26) must not turn every one of them into the fixed «الجروبات بس» line.
    import qurtoba.switches as _switches
    originals['private_closed'] = _switches.private_closed
    _switches.private_closed = lambda conversation: False

    def fake_notification(*args, **kwargs):
        capture.notifications.append({'subject': kwargs.get('subject'), 'body': kwargs.get('body'),
                                      'partner_ids': list(kwargs.get('partner_ids') or [])})
        return None
    _notif_services.post_notification = fake_notification
    try:
        yield
    finally:
        WhatsAppAPIService.send_and_broadcast = originals['sab']
        WhatsAppAPIService.send_text_message = originals['text']
        WhatsAppAPIService.send_media_message = originals['media']
        WhatsAppAPIService.send_reaction = originals['react']
        WhatsAppAPIService.send_template_message = originals['tmpl']
        Conversation.alert_human = originals['alert']
        push_record_to_qurtoba_task.delay = originals['push_delay']
        push_record_to_qurtoba_task.apply_async = originals['push_async']
        _notif_services.post_notification = originals['post_notification']
        _switches.private_closed = originals['private_closed']
        if 'react_delay' in originals:
            try:
                from modules.whatsapp.tasks import process_handling_reaction
                process_handling_reaction.delay = originals['react_delay']
                process_handling_reaction.apply_async = originals['react_async']
            except Exception:
                pass
        if wa_web_service is not None and 'wa_web_send' in originals:
            wa_web_service.send_omnichannel = originals['wa_web_send']
        if 'wa_react_delay' in originals:
            try:
                from modules.wa_web.tasks.outbound import send_wa_web_reaction
                send_wa_web_reaction.delay = originals['wa_react_delay']
            except Exception:
                pass


# ── sandbox fixtures ─────────────────────────────────────────────────────────

def get_sandbox():
    """(partner, customer, conversation, account, ai_partner, admin_partner) — created once, reused."""
    from modules.base.models import Partner
    from modules.chat.services.chat_bridge_service import ChatBridgeService
    from modules.whatsapp.models import WhatsAppAccount
    from qurtoba.models import QurtobaCustomer

    account = WhatsAppAccount.objects.get(pk=WHATSAPP_ACCOUNT_ID)
    from qurtoba.extensions import system_sender
    ai_partner = system_sender()
    admin_partner = (Partner.objects.filter(user__isnull=False, user__is_superuser=True, active=True).order_by('pk').first()
                     or Partner.objects.filter(user__isnull=False, active=True).order_by('pk').first())

    customer = QurtobaCustomer.objects.filter(name=SANDBOX_CUSTOMER_NAME).first()
    if customer is None:
        # QurtobaCustomer.pre_create forbids manual creation (customers come from the
        # Qurtoba sync). The sandbox customer is deliberately NOT a Qurtoba customer,
        # so it is inserted without the lifecycle hooks.
        QurtobaCustomer.objects.bulk_create([QurtobaCustomer(
            name=SANDBOX_CUSTOMER_NAME, phone_no='01000000099', grade=900, balance=0.0, accounts='',
        )])
        customer = QurtobaCustomer.objects.get(name=SANDBOX_CUSTOMER_NAME)
    partner = Partner.all_objects.filter(name=SANDBOX_PARTNER_NAME).first()
    if partner is None:
        partner = Partner(name=SANDBOX_PARTNER_NAME, phone=SANDBOX_PHONE)
        partner.whatsapp_account_id = account.id
        partner.qurtoba_customer = customer
        partner.save()
    else:
        changed = False
        if getattr(partner, 'qurtoba_customer_id', None) != customer.pk:
            partner.qurtoba_customer = customer; changed = True
        if getattr(partner, 'whatsapp_account_id', None) != account.id:
            partner.whatsapp_account_id = account.id; changed = True
        if changed:
            partner.save()

    conversation, _ = ChatBridgeService().get_or_create_social_conversation(
        'whatsapp', partner, social_account=account, system_partner=admin_partner)
    if not conversation.handled_by_ai:
        type(conversation).objects.filter(pk=conversation.pk).update(handled_by_ai=True)
        conversation.handled_by_ai = True
    return partner, customer, conversation, account, ai_partner, admin_partner


def reset_sandbox(conversation, customer, keep_rows: bool = False):
    from django.core.cache import cache
    from modules.chat.models import Message
    from qurtoba.models import QurtobaPendingPayment, QurtobaPendingTransaction, QurtobaRecord

    if not keep_rows:
        Message.objects_all.filter(conversation=conversation).delete()
        QurtobaRecord.objects.filter(customer=customer).delete()
        QurtobaPendingTransaction.objects.filter(customer=customer).delete()
        QurtobaPendingPayment.objects.filter(customer=customer).delete()
        type(customer).objects.filter(pk=customer.pk).update(balance=0.0)
        customer.balance = 0.0
    for pattern in (f'*{conversation.id}*', f'*conversation_{conversation.id}*'):
        try:
            cache.delete_pattern(pattern)
        except Exception:
            pass
    for fld in ('summary', 'goal'):
        if hasattr(conversation, fld):
            try:
                type(conversation).objects.filter(pk=conversation.pk).update(**{fld: ''})
            except Exception:
                pass


def get_group_sandbox():
    """(group_partner, customer, conversation, wa_web_account, ai_partner, admin_partner, members) — created once,
    reused. Nothing here talks to the WhatsApp Web gateway: plain rows, the group built with fetch/roster off."""
    from modules.base.models import Partner
    from modules.wa_web.models import WaWebAccount, WaWebGroupMember
    from modules.wa_web.services.group_service import GroupService
    _p, customer, _c, _a, ai_partner, admin_partner = get_sandbox()
    acc = WaWebAccount.objects.filter(name=WA_WEB_SANDBOX_NAME).first()
    if acc is None:
        acc = WaWebAccount.objects.create(name=WA_WEB_SANDBOX_NAME, phone_number=WA_WEB_SANDBOX_PHONE,
                                          handled_by_ai=True, ai_in_groups=True, ai_in_private=False)
    conv, _ = GroupService(acc).get_or_create_group(GROUP_SANDBOX_JID, subject=GROUP_SANDBOX_SUBJECT,
                                                    fetch=False, roster=False)
    members = {}
    for role, (phone, name) in GROUP_MEMBERS.items():
        jid = f'{phone}@s.whatsapp.net'
        p = Partner.all_objects.filter(wa_web_account=acc, wa_id=jid).first()
        if p is None:
            p = Partner(name=name, phone=phone, wa_web_account=acc, wa_id=jid, wa_phone_jid=jid)
            p.save()
        staff = role == 'staff'
        if bool(p.employee) != staff:
            Partner.all_objects.filter(pk=p.pk).update(employee=staff)
            p.employee = staff
        WaWebGroupMember.objects.get_or_create(conversation=conv, jid=jid, defaults={
            'partner': p, 'phone_jid': jid, 'role': 'participant', 'is_self': False, 'joined_at': timezone.now()})
        members[role] = p
    if not conv.handled_by_ai:
        type(conv)._base_manager.filter(pk=conv.pk).update(handled_by_ai=True)
        conv.handled_by_ai = True
    return conv.social_partner, customer, conv, acc, ai_partner, admin_partner, members


def set_group_state(conversation, customer, members, *, linked: bool = True, present=('customer', 'employee', 'staff')):
    """Put the sandbox group in a scenario's starting state: linked or not, which members are in it."""
    from modules.wa_web.models import WaWebGroupMember
    gp = conversation.social_partner
    type(gp)._base_manager.filter(pk=gp.pk).update(qurtoba_customer=customer if linked else None)
    gp.qurtoba_customer_id = customer.pk if linked else None
    for role, p in members.items():
        WaWebGroupMember.objects.filter(conversation=conversation, partner=p).update(
            left_at=None if role in present else timezone.now())


@contextlib.contextmanager
def group_twin(wa_web_account):
    """The sandbox WhatsApp Web account obeys Cloud account WHATSAPP_ACCOUNT_ID's switches (in production the
    twin is found by the shared phone number)."""
    from modules.whatsapp.models import WhatsAppAccount
    from qurtoba import groups
    orig = groups.twin_cloud_account
    twin = WhatsAppAccount._base_manager.get(pk=WHATSAPP_ACCOUNT_ID)
    groups.twin_cloud_account = lambda acc: twin if getattr(acc, 'pk', None) == wa_web_account.pk else orig(acc)
    try:
        yield
    finally:
        groups.twin_cloud_account = orig


def insert_inbound(conversation, partner, text: str, reply_to=None, sent_at=None, msg_type: str = 'text'):
    """An inbound row exactly as the bridge writes it.

    Created WITHOUT a WhatsApp id (so chat.Message.post_create never schedules the
    Celery batching), then given a sandbox id with a plain UPDATE (no signals): the
    quoted-reply tool, reactions and the planner's burst ordering all need it."""
    from django.contrib.contenttypes.models import ContentType
    from modules.chat.models import Message
    sa = conversation.social_account
    content = {'text': text} if msg_type == 'text' else {'transcription': text} if msg_type in ('audio', 'voice') else {'caption': text}
    row = Message.objects_all.create(
        conversation=conversation, sender=partner, type=msg_type, content=content,
        direction='inbound', status='saved', reply_to=reply_to,
        social_sent_at=sent_at or timezone.now(),
        social_account_content_type=ContentType.objects.get_for_model(sa) if sa else None,
        social_account_object_id=sa.id if sa else None,
    )
    Message.objects_all.filter(pk=row.pk).update(
        social_id=(f'wa_web:sandbox:in.{time.time_ns()}' if getattr(conversation, 'type', None) == 'wa_web'
                   else f'wamid.sandbox.in.{time.time_ns()}'),
        **({'created_at': sent_at} if sent_at else {}),
    )
    row.refresh_from_db()
    return row


def insert_outbound_system(conversation, sender, text: str, reply_to=None):
    from django.contrib.contenttypes.models import ContentType
    from modules.chat.models import Message
    sa = conversation.social_account
    return Message.objects_all.create(
        conversation=conversation, sender=sender, type='text', content={'text': text},
        direction='outbound', status='delivered', reply_to=reply_to, social_id=f'wamid.sandbox.setup.{time.time_ns()}',
        social_sent_at=timezone.now(),
        social_account_content_type=ContentType.objects.get_for_model(sa) if sa else None,
        social_account_object_id=sa.id if sa else None,
    )


def backdate(conversation, customer, seconds: int):
    """Move every sandbox row `seconds` into the past (instead of sleeping between turns)."""
    if seconds <= 0:
        return
    from modules.chat.models import Message
    from qurtoba.models import QurtobaPendingTransaction, QurtobaRecord
    delta = timedelta(seconds=seconds)
    Message.objects_all.filter(conversation=conversation).update(created_at=F('created_at') - delta)
    Message.objects_all.filter(conversation=conversation, social_sent_at__isnull=False).update(social_sent_at=F('social_sent_at') - delta)
    QurtobaRecord.objects.filter(customer=customer).update(created_at=F('created_at') - delta)
    QurtobaPendingTransaction.objects.filter(customer=customer).update(created_at=F('created_at') - delta)


# ── one turn ─────────────────────────────────────────────────────────────────

def _partner_message(rows, expose_ids: bool, group: bool = False):
    content = []
    for m in rows:
        text = m.content.get('text', '') if isinstance(m.content, dict) else str(m.content)
        if group and getattr(m, 'sender_id', None):
            text = f"{m.sender.name}: {text}"          # as the WhatsApp Web bridge writes group lines
        prefix = ''
        if m.reply_to is not None:
            q = m.reply_to.content.get('text', '') if isinstance(m.reply_to.content, dict) else ''
            who = 'you' if m.reply_to.direction == 'outbound' else 'customer'
            prefix += f'[Replying to {who}: "{" ".join(str(q).split())[:80]}"]\n'
        if expose_ids:
            prefix += f'[message_id: {m.id}]\n'
        content.append({'type': 'text', 'text': prefix + text})
    return [{'role': 'user', 'content': content}]


def run_turn(scenario_id: str, turn_index: int, rows, sandbox, capture: Capture) -> Dict[str, Any]:
    """Execute the workflow for a batch of inbound rows and simulate the channel's send step."""
    from modules.aistudio.services.workflow_executor import execute_workflow_sync
    from modules.aistudio.utils.omni_channel_utils import clean_and_validate_xml_tags, normalize_dashes
    from modules.chat.models import Message
    partner, customer, conversation, account, ai_partner, admin_partner = sandbox[:6]
    group = getattr(conversation, 'type', None) == 'wa_web'

    expose_ids = False if group else bool(getattr(account, 'send_message_ids_to_ai', False))
    pm = _partner_message(rows, expose_ids, group=group)
    texts = [c['text'] for c in pm[0]['content']]
    input_data = {'message': texts[0]} if len(texts) == 1 else {'message': texts[0], 'content': pm}
    history = conversation.get_conversation_history_as_langchain(
        limit=getattr(account, 'history_ingest_limit', None) or 25,
        exclude_ids=[str(m.id) for m in rows], accepts_images=False, **({'prefix_senders': True} if group else {}))
    if group:
        from modules.aistudio_wa_web.group_context import build_group_state
        input_data['group'] = build_group_state(conversation, account, rows)

    t0 = timezone.now()
    started = time.time()
    sends_before = len(capture.sends)
    twin = group_twin(account) if group else contextlib.nullcontext()
    with sandbox_patches(capture, conversation, ai_partner), twin:
        # a group run: the run's partner is the GROUP (runtime_patches keeps the bridge from swapping the
        # speaker in), the channel is wa_web, the workflow is the group variant
        result = execute_workflow_sync(
            workflow_id=(GROUP_WORKFLOW_ID or WORKFLOW_ID) if group else WORKFLOW_ID, input_data=input_data,
            partner=partner, conversation=conversation, conversation_history=history, partner_message=pm,
            thread_id=f"sandbox_{'wa_web_' if group else ''}{conversation.id}_{scenario_id}_{RUN_TOKEN}",
            trigger_source='wa_web' if group else 'whatsapp',
        )
        output = result.output
        output_text = ''
        if result.success and output not in (None, ''):
            output_text = normalize_dashes(clean_and_validate_xml_tags(str(output)))
            output_text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', output_text)
            output_text = re.sub(r'^-{3,}\s*$', '', output_text, flags=re.M).strip()
        # the channel task's send step: paragraphs through the (captured, gated) send path
        agent_paragraphs = [p.strip() for p in output_text.split('\n\n') if p.strip()] if output_text else []
        for p in agent_paragraphs:
            if group:
                from modules.wa_web.services.send_service import WaWebService
                WaWebService(account).send_omnichannel(partner, {'text': p}, message_type='text',
                                                       conversation=conversation, system_partner=ai_partner, paced=True)
            else:
                account.service.send_and_broadcast(partner=partner, content=p, message_type='text',
                                                   conversation=conversation, system_partner=ai_partner, websocket=False)
    elapsed = time.time() - started

    trace = list(Message.objects_all.filter(conversation=conversation, type__in=['tool_call', 'tool'],
                                            created_at__gte=t0).order_by('created_at'))
    tool_calls = []
    for m in trace:
        c = m.content or {}
        if m.type == 'tool_call':
            tool_calls.append({'name': c.get('tool_name'), 'input': c.get('tool_input'), 'ai_content': c.get('ai_content'), 'output': None})
        elif tool_calls and m.type == 'tool' and tool_calls[-1]['output'] is None and tool_calls[-1]['name'] == c.get('tool_name'):
            out = c.get('tool_output')
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except Exception:
                    pass
            tool_calls[-1]['output'] = out

    from qurtoba.models import QurtobaPendingTransaction, QurtobaRecord
    records = list(QurtobaRecord.objects.filter(customer=customer, created_at__gte=t0).values('id', 'type', 'value', 'account_number', 'cash_sys_state'))
    pendings = list(QurtobaPendingTransaction.objects.filter(customer=customer, created_at__gte=t0).values('id', 'type', 'value', 'account_number'))

    return {
        'turn': turn_index,
        'inbound': [{'id': str(m.id), 'text': m.content.get('text')} for m in rows],
        'workflow': {'success': result.success, 'status': result.status, 'error': result.error,
                     'had_side_effect': result.had_side_effect, 'raw_output': (str(output)[:600] if output else '')},
        'agent_paragraphs': agent_paragraphs,
        'sends': capture.sends[sends_before:],
        'reactions': list(capture.reactions), 'alerts': list(capture.alerts), 'pushes': list(capture.pushes),
        'tool_calls': tool_calls,
        'records': records, 'pendings': pendings,
        'elapsed_s': round(elapsed, 1),
    }


# ── scoring ──────────────────────────────────────────────────────────────────

def _norm_phone(x):
    try:
        from qurtoba.tools.transactions import _normalize_phone
        return _normalize_phone(x) or x
    except Exception:
        return x


def _create_items(turn):
    items = []
    for tc in turn['tool_calls']:
        if tc['name'] == 'qurtoba_create_new_transactions_bulk':
            for it in (tc.get('input') or {}).get('transactions', []) or []:
                try:
                    items.append({'account': _norm_phone(it.get('account_number')), 'value': float(it.get('value')),
                                  'confirm_high_value': bool(it.get('confirm_high_value')), 'type': it.get('type')})
                except Exception:
                    items.append({'account': it.get('account_number'), 'value': None})
    return items


def _contains_kv(obj, key, value) -> bool:
    if isinstance(obj, dict):
        if key in obj and obj[key] == value:
            return True
        return any(_contains_kv(v, key, value) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_kv(v, key, value) for v in obj)
    return False


# Wording that lists what went right. The tool's 👍 already says it; an agent text
# that recites the registered items is the old "one combined message" habit.
SUCCESS_LIST_FORBID = ['اتسجّل', 'اتسجل عندنا', 'اتسجلت', 'اتنفذت', 'اتنفّذت', 'تم تسجيل', 'تم التنفيذ',
                       'تم تنفيذ', 'باقي التحويلات', 'باقى التحويلات']


def score_turn(turn: Dict[str, Any], expect: Dict[str, Any],
               inbound_ids: Optional[Dict[int, str]] = None) -> List[Dict[str, Any]]:
    """``inbound_ids`` maps a SCENARIO turn index → that inbound row's id (all turns,
    not only this batch), so ``quoted_on`` can name a message from an earlier batch."""
    checks = []
    sends = turn['sends']
    agent_sends = [s for s in sends if _is_agent_send(s)]
    agent_texts = [s['text'] or '' for s in agent_sends]
    tool_texts = [(s.get('text') or s.get('caption') or '') for s in sends if s['system_send'] and not s['blocked']]
    blocked = [s for s in sends if s['blocked']]
    unquoted = [s for s in agent_sends if s.get('action') == 'send' and not s.get('quoted')]
    quoted_sends = [s for s in agent_sends if s.get('quoted')]
    inbound_ids = inbound_ids or {}
    agent_blob = '\n'.join(agent_texts)
    tool_blob = '\n'.join(tool_texts)
    called = [tc['name'] for tc in turn['tool_calls']]
    items = _create_items(turn)

    def add(name, ok, detail=''):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})

    for t in expect.get('tools', []):
        present = t['name'] in called
        if t.get('must', True):
            ok = present
            if ok and t.get('args'):
                ok = any(all(_contains_kv(tc.get('input'), k, v) for k, v in t['args'].items())
                         for tc in turn['tool_calls'] if tc['name'] == t['name'])
            add(f"tool {t['name']} called" + (f" with {t['args']}" if t.get('args') else ''), ok, f'called={called}')
        else:
            add(f"tool {t['name']} NOT called", not present, f'called={called}')
    for c in expect.get('creates', []):
        ok = any(i['account'] == _norm_phone(c['account']) and (c['value'] is None or i['value'] == float(c['value'])) for i in items)
        add(f"create {c['account']} ← {c['value']}", ok, f'items={items}')
    for c in expect.get('no_creates', []):
        hit = any(i['account'] == _norm_phone(c['account']) and (c['value'] is None or i['value'] == float(c['value'])) for i in items)
        add(f"NO create {c['account']} ← {c['value']}", not hit, f'items={items}')
    if 'creates_count' in expect:
        cc = expect['creates_count']
        n = sum(1 for i in items if i['account'] == _norm_phone(cc['account']) and i['value'] == float(cc['value']))
        add(f"create {cc['account']} ← {cc['value']} exactly {cc['count']}×", n == cc['count'], f'n={n}')
    if expect.get('no_records'):
        add('no ledger record written', not turn['records'] and not turn['pendings'], f"records={turn['records']} pendings={turn['pendings']}")
    if 'records_count' in expect:
        rc = expect['records_count']
        n = sum(1 for r in turn['records'] if _norm_phone(r['account_number']) == _norm_phone(rc['account']) and float(r['value']) == float(rc['value']))
        add(f"ledger records {rc['account']} ← {rc['value']} exactly {rc['count']}×", n == rc['count'], f"records={turn['records']}")
    for s in expect.get('tool_texts_any', []) and [expect['tool_texts_any']] or []:
        add(f'tool-sent text contains one of {s}', any(x in tool_blob for x in s), f'tool_texts={tool_texts}')
    if expect.get('no_ack'):
        acked = any((s.get('text') or '').strip() in ('👍', '👍🏿') for s in sends if not s['blocked']) or bool(turn['reactions'])
        add('no 👍 acknowledgement', not acked, f"reactions={turn['reactions']}")
    if expect.get('acked'):
        liked = any((s.get('text') or '').strip() in ('👍', '👍🏿') for s in sends if not s['blocked']) or bool(turn['reactions'])
        if not liked:
            # core delivers a saved reaction row itself (post_save), outside the stubbed senders
            try:
                from modules.chat.models import MessageReaction
                liked = MessageReaction.objects.filter(message_id__in=[i['id'] for i in turn.get('inbound') or []],
                                                       direction='outbound', emoji='👍').exists()
            except Exception:
                pass
        add('👍 acknowledgement', liked, f"reactions={turn['reactions']} sends={[s.get('text') for s in sends]}")
    reply = expect.get('reply') or expect.get('agent_reply')
    if reply == 'silent':
        add('agent silent (no customer-visible agent text)', len(agent_texts) == 0, f'agent_texts={agent_texts} blocked={[b["blocked"] for b in blocked]}')
    elif reply == 'one_message':
        add('exactly one agent message', len(agent_texts) == 1, f'agent_texts={agent_texts}')
    elif reply == 'at_least_one':
        # a direct question must never be met with silence (2026-09-08: «لغيت ؟» got nothing)
        add('the customer got an answer', len(agent_texts) + len(tool_texts) > 0,
            f'agent_texts={agent_texts} tool_texts={tool_texts}')
    elif reply == 'question':
        add('exactly one agent message and it asks', len(agent_texts) == 1 and any(q in agent_texts[0] for q in ('؟', '?')), f'agent_texts={agent_texts}')
    for s in expect.get('contains', []):
        add(f'agent text contains «{s}»', s in agent_blob, f'agent_texts={agent_texts}')
    if expect.get('contains_any'):
        opts = expect['contains_any']
        add(f'agent text contains one of {opts}', any(o in agent_blob for o in opts), f'agent_texts={agent_texts}')
    for s in expect.get('forbid', []):
        add(f'agent text free of «{s}»', s not in agent_blob, f'agent_texts={agent_texts}') if s in agent_blob else None
    if not any(c['check'].startswith('agent text free of') for c in checks) and expect.get('forbid'):
        add('agent text free of forbidden phrases', True, '')
    for s in expect.get('tool_texts_contain', []):
        add(f'tool-sent text contains «{s}»', s in tool_blob, f'tool_texts={tool_texts}')
    if 'quoted_replies' in expect:
        n = expect['quoted_replies']
        add(f'exactly {n} quoted agent reply(ies)', len(quoted_sends) == n,
            f"quoted={[((q['text'] or '')[:60], q['reply_to_id'], 'fwd' if q.get('forwarded') else 'tool') for q in quoted_sends]} agent_texts={agent_texts}")
    for ti in expect.get('quoted_on', []):
        target = inbound_ids.get(int(ti))
        hit = bool(target) and any(q.get('reply_to_id') == target for q in quoted_sends)
        add(f'an agent reply is quoted on turn {ti}\'s message', hit,
            f"target={target} quoted_on={[q['reply_to_id'] for q in quoted_sends]} agent_texts={agent_texts}")
    if expect.get('no_success_list'):
        hits = [w for w in SUCCESS_LIST_FORBID if w in agent_blob]
        add('no success list in agent text (the 👍 already says it)', not hits, f'hits={hits} agent_texts={agent_texts}')
    # GLOBAL: since 2026-09-05 nothing the agent writes reaches the customer unquoted —
    # only the reply tool (or the gate re-attaching a greeting to the customer's
    # message) delivers. A plain 'send' of agent text is a protocol breach.
    add('no unquoted agent text delivered', not unquoted,
        f"unquoted={[((u['text'] or '')[:80]) for u in unquoted]}" if unquoted else '')
    if blocked:
        add('gate blocked an agent text (system caught a leak — the model still produced it)', False,
            f"blocked={[(b['blocked'], (b['text'] or '')[:80]) for b in blocked]}")
    return checks


# ── driver ───────────────────────────────────────────────────────────────────

def _apply_before(before, conversation, customer, members, flag_override) -> None:
    """Turn-level state changes between batches: `link` (the sandbox group linked or not), `off_hours` /
    `ai_enabled` (sandbox-only switch overrides) and `handled_by_ai` (a human took the chat over)."""
    if not before:
        return
    from modules.chat.models import Conversation
    if 'link' in before and members:
        gp = conversation.social_partner
        type(gp)._base_manager.filter(pk=gp.pk).update(qurtoba_customer=customer if before['link'] else None)
        gp.qurtoba_customer_id = customer.pk if before['link'] else None
    for key in ('off_hours', 'ai_enabled'):
        if key in before:
            flag_override[key] = bool(before[key])
    # a service (type) switched off / back on — sandbox-only, the live toggles are never touched
    disabled = flag_override.setdefault('__disabled_types__', set())
    disabled.update(before.get('disable') or [])
    disabled.difference_update(before.get('enable') or [])
    if 'handled_by_ai' in before:
        # through a normal save, like the chat's AI toggle (ToggleAiBotConversation) — save hooks run
        conv = Conversation._base_manager.get(pk=conversation.pk)
        conv.handled_by_ai = bool(before['handled_by_ai'])
        conv.save(update_fields=['handled_by_ai'])
    for sr in before.get('staff_record') or []:
        # a member of staff registered it by hand (the chat's «عملية جديدة»): no source message
        from qurtoba.models import QurtobaRecord
        # `via: 'qurtoba'` = done in the Qurtoba app: it reaches Genie with no chat partner; `minutes_ago` ages it
        rec = QurtobaRecord.objects.create(customer=customer, type=sr.get('type', 'كاش'), account_number=sr['account'],
                                           value=float(sr['value']),
                                           partner=None if sr.get('via') == 'qurtoba' else conversation.social_partner,
                                           date=timezone.localdate(), time=timezone.localtime().time())
        if sr.get('minutes_ago'):
            QurtobaRecord.objects.filter(pk=rec.pk).update(
                created_at=timezone.now() - timedelta(minutes=int(sr['minutes_ago'])))


def run_scenario(scn: Dict[str, Any], sandbox, keep: bool = False) -> Dict[str, Any]:
    members = None
    if scn.get('channel') == 'group':
        # a WhatsApp Web customer group: its own sandbox; each turn names its sender (customer / employee / staff)
        *six, members = get_group_sandbox()
        sandbox = tuple(six)
        gsetup = scn.get('setup') or {}
        set_group_state(sandbox[2], sandbox[1], members, linked=not gsetup.get('unlinked'),
                        present=tuple(gsetup.get('members') or ('customer', 'employee', 'staff')))
    partner, customer, conversation, account, ai_partner, admin_partner = sandbox
    reset_sandbox(conversation, customer)
    from modules.chat.models import Conversation as _Conv
    _Conv._base_manager.filter(pk=conversation.pk).update(handled_by_ai=True)
    # per-scenario switch overrides (`before: {off_hours: …}`) — the sandbox never touches the live switches
    import qurtoba.switches as _sw
    # the sandbox runs OPEN with the AI on unless a scenario says otherwise — never the live switches' state
    flag_override: Dict[str, Any] = {'off_hours': False, 'ai_enabled': True}
    _orig_flags = _sw.account_flags
    _sw.account_flags = lambda conv: {**_orig_flags(conv), **{k: v for k, v in flag_override.items() if not k.startswith('__')}}
    import qurtoba.tools.transactions as _tx
    _orig_type_check = _tx._check_type_allowed_for_account

    def _type_check(conv, effective_type):
        if effective_type in flag_override.get('__disabled_types__', ()):
            return False, (f'الخدمة {effective_type} متوقفة حالياً، برجاء المحاولة في وقت لاحق '
                           f'وسيتم إبلاغك عند توفرها.')
        return _orig_type_check(conv, effective_type)
    _tx._check_type_allowed_for_account = _type_check
    capture = Capture()
    report = {'id': scn['id'], 'title': scn['title'], 'turns': [], 'checks': [], 'error': None}
    rows_by_turn: Dict[int, Any] = {}
    try:
        setup = scn.get('setup') or {}
        # registered فورى/أمان/طاير accounts for this scenario ('فورى,6081844,أمان,970604')
        from qurtoba.models import QurtobaCustomer, _sync_customer_accounts
        QurtobaCustomer.objects.filter(pk=customer.pk).update(accounts=setup.get('accounts') or '')
        customer.refresh_from_db()
        _sync_customer_accounts(customer)
        if setup.get('prior_create'):
            from qurtoba.models import QurtobaRecord
            pc = setup['prior_create']
            m0 = insert_inbound(conversation, members['customer'] if members else partner, f"{pc['account']}\n\n{pc['value']}")
            rec = QurtobaRecord.objects.create(customer=customer, type='كاش', account_number=pc['account'],
                                               value=float(pc['value']), partner=partner, origin_message_id=m0.id,
                                               date=timezone.localdate(), time=timezone.localtime().time())
            m0.mark_ai_consumed(rec)
            if setup.get('system_notice') == 'no_wallet':
                from qurtoba.tasks import _CANCEL_NOTICE_MESSAGES
                insert_outbound_system(conversation, admin_partner, '👍')
                notice_msg = insert_outbound_system(conversation, admin_partner, _CANCEL_NOTICE_MESSAGES['no_wallet'], reply_to=m0)
                QurtobaRecord.objects.filter(pk=rec.pk).update(cash_sys_state='canceled', cash_sys_canceled_reason='no_wallet',
                                                              cash_sys_original_value=float(pc['value']), value=0.0)
            backdate(conversation, customer, 120)

        batch: List[Any] = []
        turns = scn['turns']
        notice_msg = locals().get('notice_msg')
        # Messages inside one batch are stamped a few seconds apart (a person typing),
        # not in the same second — the planner treats a same-second split as a
        # deliberate ≤3 burst and executes it. A scenario can override with `offset`.
        batch_clock = None
        skip_batch = False
        for i, t in enumerate(turns):
            gap = t.get('gap', 0 if i else 0)
            if i and gap and gap > 0:
                # flush the previous batch as its own run, then move time forward
                if skip_batch:
                    # `no_run`: the bridge never ran the AI for it (AI in groups off, a human took over)
                    report['turns'].append({'turn': len(report['turns']), 'skipped': True,
                                            'inbound': [{'id': str(r.id), 'text': (r.content or {}).get('text')} for r in batch],
                                            'sends': [], 'reactions': [], 'alerts': [], 'pushes': [], 'tool_calls': [],
                                            'records': [], 'pendings': [], 'agent_paragraphs': [], 'workflow': {}})
                else:
                    res = run_turn(scn['id'], len(report['turns']), batch, sandbox, capture)
                    report['turns'].append(res)
                batch = []
                batch_clock = None
                skip_batch = False
                backdate(conversation, customer, gap)
            _apply_before(t.get('before'), conversation, customer, members, flag_override)
            skip_batch = skip_batch or bool(t.get('no_run'))
            if t.get('reply_to') == 'notice':
                reply_to = notice_msg                       # the customer quotes our rejection notice
            else:
                reply_to = rows_by_turn.get(t['reply_to']) if t.get('reply_to') is not None else None
            if batch_clock is None:
                n_batch = 1
                j = i + 1
                while j < len(turns) and not turns[j].get('gap'):
                    n_batch += 1; j += 1
                # first message of the batch: now for a single message (a reply must
                # postdate the question the tool just asked), earlier for a burst
                batch_clock = timezone.now() - timedelta(seconds=3 * (n_batch - 1))
            else:
                batch_clock = batch_clock + timedelta(seconds=t.get('offset', 3))
            sender = members[t.get('sender', 'customer')] if members else partner
            row = insert_inbound(conversation, sender, t['text'], reply_to=reply_to, sent_at=batch_clock, msg_type=t.get('type', 'text'))
            rows_by_turn[i] = row
            batch.append(row)
        if batch and not skip_batch:
            res = run_turn(scn['id'], len(report['turns']), batch, sandbox, capture)
            report['turns'].append(res)

        expect = scn.get('expect') or {}
        inbound_ids = {i: str(r.id) for i, r in rows_by_turn.items()}
        for ti, turn in enumerate(report['turns']):
            key = str(ti)
            exp = expect.get(key) or (expect.get('final') if ti == len(report['turns']) - 1 else None)
            if exp:
                for c in score_turn(turn, exp, inbound_ids):
                    c['turn'] = ti
                    report['checks'].append(c)
    except Exception as e:
        report['error'] = f'{e}\n{traceback.format_exc()[-1500:]}'
    finally:
        if not keep:
            reset_sandbox(conversation, customer)
    report['passed'] = sum(1 for c in report['checks'] if c['ok'])
    report['failed'] = sum(1 for c in report['checks'] if not c['ok'])
    _sw.account_flags = _orig_flags
    _tx._check_type_allowed_for_account = _orig_type_check
    _Conv._base_manager.filter(pk=conversation.pk).update(handled_by_ai=True)
    return report
