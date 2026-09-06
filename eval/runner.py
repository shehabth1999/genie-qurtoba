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


# ── captured side effects ────────────────────────────────────────────────────

@dataclass
class Capture:
    sends: List[Dict[str, Any]] = field(default_factory=list)
    reactions: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    pushes: List[int] = field(default_factory=list)
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
                    social_id=f'wamid.sandbox.{capture.counter}', reply_to=reply_to,
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

    WhatsAppAPIService.send_and_broadcast = fake_sab
    WhatsAppAPIService.send_text_message = fake_text
    WhatsAppAPIService.send_media_message = fake_media
    WhatsAppAPIService.send_reaction = fake_react
    WhatsAppAPIService.send_template_message = fake_tmpl
    Conversation.alert_human = fake_alert
    push_record_to_qurtoba_task.delay = fake_push
    push_record_to_qurtoba_task.apply_async = fake_push
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
        if 'react_delay' in originals:
            try:
                from modules.whatsapp.tasks import process_handling_reaction
                process_handling_reaction.delay = originals['react_delay']
                process_handling_reaction.apply_async = originals['react_async']
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
    ai_partner = Partner.all_objects.filter(ai_agent=True, email='genie@genie-erp.com').first()
    admin_partner = Partner.all_objects.filter(pk=2).first() or Partner.objects.filter(user__isnull=False).order_by('pk').first()

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
        social_id=f'wamid.sandbox.in.{time.time_ns()}',
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

def _partner_message(rows, expose_ids: bool):
    content = []
    for m in rows:
        text = m.content.get('text', '') if isinstance(m.content, dict) else str(m.content)
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
    partner, customer, conversation, account, ai_partner, admin_partner = sandbox

    expose_ids = bool(getattr(account, 'send_message_ids_to_ai', False))
    pm = _partner_message(rows, expose_ids)
    texts = [c['text'] for c in pm[0]['content']]
    input_data = {'message': texts[0]} if len(texts) == 1 else {'message': texts[0], 'content': pm}
    history = conversation.get_conversation_history_as_langchain(
        limit=getattr(account, 'history_ingest_limit', None) or 25,
        exclude_ids=[str(m.id) for m in rows], accepts_images=False)

    t0 = timezone.now()
    started = time.time()
    sends_before = len(capture.sends)
    with sandbox_patches(capture, conversation, ai_partner):
        result = execute_workflow_sync(
            workflow_id=WORKFLOW_ID, input_data=input_data, partner=partner,
            conversation=conversation, conversation_history=history, partner_message=pm,
            thread_id=f'sandbox_{conversation.id}_{scenario_id}_{RUN_TOKEN}', trigger_source='whatsapp',
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
    reply = expect.get('reply') or expect.get('agent_reply')
    if reply == 'silent':
        add('agent silent (no customer-visible agent text)', len(agent_texts) == 0, f'agent_texts={agent_texts} blocked={[b["blocked"] for b in blocked]}')
    elif reply == 'one_message':
        add('exactly one agent message', len(agent_texts) == 1, f'agent_texts={agent_texts}')
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

def run_scenario(scn: Dict[str, Any], sandbox, keep: bool = False) -> Dict[str, Any]:
    partner, customer, conversation, account, ai_partner, admin_partner = sandbox
    reset_sandbox(conversation, customer)
    capture = Capture()
    report = {'id': scn['id'], 'title': scn['title'], 'turns': [], 'checks': [], 'error': None}
    rows_by_turn: Dict[int, Any] = {}
    try:
        setup = scn.get('setup') or {}
        if setup.get('prior_create'):
            from qurtoba.models import QurtobaRecord
            pc = setup['prior_create']
            m0 = insert_inbound(conversation, partner, f"{pc['account']}\n\n{pc['value']}")
            rec = QurtobaRecord.objects.create(customer=customer, type='كاش', account_number=pc['account'],
                                               value=float(pc['value']), partner=partner, origin_message_id=m0.id,
                                               date=timezone.localdate(), time=timezone.localtime().time())
            m0.mark_ai_consumed(rec)
            if setup.get('system_notice') == 'no_wallet':
                from qurtoba.tasks import _CANCEL_NOTICE_MESSAGES
                insert_outbound_system(conversation, admin_partner, '👍')
                insert_outbound_system(conversation, admin_partner, _CANCEL_NOTICE_MESSAGES['no_wallet'], reply_to=m0)
                QurtobaRecord.objects.filter(pk=rec.pk).update(cash_sys_state='canceled', cash_sys_canceled_reason='no_wallet',
                                                              cash_sys_original_value=float(pc['value']), value=0.0)
            backdate(conversation, customer, 120)

        batch: List[Any] = []
        turns = scn['turns']
        # Messages inside one batch are stamped a few seconds apart (a person typing),
        # not in the same second — the planner treats a same-second split as a
        # deliberate ≤3 burst and executes it. A scenario can override with `offset`.
        batch_clock = None
        for i, t in enumerate(turns):
            gap = t.get('gap', 0 if i else 0)
            if i and gap and gap > 0:
                # flush the previous batch as its own run, then move time forward
                res = run_turn(scn['id'], len(report['turns']), batch, sandbox, capture)
                report['turns'].append(res)
                batch = []
                batch_clock = None
                backdate(conversation, customer, gap)
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
            row = insert_inbound(conversation, partner, t['text'], reply_to=reply_to, sent_at=batch_clock, msg_type=t.get('type', 'text'))
            rows_by_turn[i] = row
            batch.append(row)
        if batch:
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
    return report
