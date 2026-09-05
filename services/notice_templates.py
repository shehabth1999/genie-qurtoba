"""
WhatsApp utility templates for the Cash-SYS notices the webhook flow sends.

Inside the 24-hour customer-service window the notices go out as free-form text
(fast, no approval needed, quoted on the customer's message). Outside that
window WhatsApp refuses free-form sends (error 131047), so each notice has an
approved *utility* template twin here. The send carries the same ``context``
(quote) as the direct notice, but VERIFIED 2026-09-05 on the test line: WhatsApp
accepts the field and renders NO quoted bubble for template messages — a
template notice arrives unquoted. From 2026-10-01 Meta bills both kinds the
same inside the window, so this is purely a delivery guarantee.

Kinds (one template each, account-scoped, Arabic ar_EG):

    (the receipt image is NOT templated — it always goes as a direct image send)
    service_fee      — «تم اضافه {fee} جنيه مصاريف خدمه …»
    reroute_partial  — part sent, remainder needs another number
    reroute_full     — nothing sent, number over its limit
    cancel_no_wallet — order cancelled, number has no wallet
    cancel_request   — order cancelled on request

Public API:
    ensure_templates(account, receipt_attachment=None) -> list[WhatsAppTemplate]
    submit_templates(account, names=None) -> dict[name, status|error]
    template_for(kind, account) -> WhatsAppTemplate | None   (approved only)
    window_open(conversation) -> bool
    send_notice(kind, ctx, params, header_url=None) -> dict
    install_context_hook() -> bool   (adds ``context.message_id`` to template sends)
"""
from __future__ import annotations

import contextvars
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── THE ONE SWITCH ───────────────────────────────────────────────────────────
# How the Cash-SYS webhook notices reach the customer:
#   'direct'   — always the old way: free-form text / image, quoted (templates unused)
#   'template' — always the approved utility template (quoted); direct only when
#                a template is not approved yet
#   'auto'     — direct inside the 24 h window; template outside it or when
#                WhatsApp refuses the direct send (error 131047)
NOTICE_DELIVERY = 'auto'

WINDOW_HOURS = 24
REENGAGEMENT_ERROR_CODES = {131047}   # "Re-engagement message" — outside the 24 h window

# Meta rules baked into the wording: no leading/trailing whitespace, a body never
# starts or ends with a variable, variable names lowercase/underscore ≤ 20 chars.
NOTICE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    'service_fee': {
        'name': 'qurtoba_service_fee_v2',
        'header_format': 'NONE',
        'body': (
            'تم اضافه {{fee}} جنيه مصاريف خدمه\n'
            '( الرقم عليه محفظه اخرى غير فودافون كاش )'
        ),
        'params': ['fee'],
        'examples': ['30'],
    },
    'reroute_partial': {
        'name': 'qurtoba_reroute_partial_v2',
        'header_format': 'NONE',
        'body': (
            '*تم تحويل ( {{sent}} ) و الباقى ( {{remaining}} )*\n'
            '\n'
            'محتاجين رقم تانى علشان نكمل\n'
            'الرقم مش قابل تحويل تانى\n'
            '( الرقم تجاوز الحد اليومى او الشهرى )'
        ),
        'params': ['sent', 'remaining'],
        'examples': ['6,000', '4,000'],
    },
    'reroute_full': {
        'name': 'qurtoba_reroute_full_v2',
        'header_format': 'NONE',
        'body': (
            '*محتاجين رقم تانى نبعت عليه الرصيد*\n'
            '\n'
            'الرقم مش قابل تحويل\n'
            '( تجاوز الحد اليومى او الشهرى )'
        ),
        'params': [],
        'examples': [],
    },
    'cancel_no_wallet': {
        'name': 'qurtoba_cancel_no_wallet_v2',
        'header_format': 'NONE',
        'body': (
            '*محتاجين رقم تانى نبعت عليه الرصيد*\n'
            '\n'
            '*الرقم مش عليه محفظة*'
        ),
        'params': [],
        'examples': [],
    },
    'cancel_request': {
        'name': 'qurtoba_cancel_request_v2',
        'header_format': 'NONE',
        'body': (
            'تم الغاء التحويل\n'
            '\n'
            'و لم يتم تسجيل العمليه عليك'
        ),
        'params': [],
        'examples': [],
    },
}



# ── creation / submission ────────────────────────────────────────────────────

def _language():
    from modules.base.models import Language
    return (Language.objects.filter(code='ar_EG').first()
            or Language.objects.filter(code='ar').first())


def ensure_templates(account, receipt_attachment=None) -> List[Any]:
    """Create or refresh the six templates as DRAFT rows (nothing sent to Meta)."""
    from modules.whatsapp.models import WhatsAppTemplate
    language = _language()
    if language is None:
        raise RuntimeError('No Arabic language row (ar_EG / ar) found')
    rows = []
    for kind, spec in NOTICE_TEMPLATES.items():
        tpl = WhatsAppTemplate.objects.filter(
            whatsapp_account=account, template_name=spec['name'], language=language,
        ).first()
        if tpl is not None and tpl.status in ('approved', 'pending'):
            rows.append(tpl)          # never overwrite something Meta already has
            continue
        if tpl is None:
            tpl = WhatsAppTemplate(whatsapp_account=account, language=language)
        tpl.name = spec['name']
        tpl.template_name = spec['name']
        tpl.category = 'utility'
        tpl.status = 'draft'
        # core encodes "no header" as TEXT with empty content (a HEADER
        # component is emitted for any other format — Meta rejects NONE)
        tpl.header_format = spec['header_format'] if spec['header_format'] != 'NONE' else 'TEXT'
        tpl.header_content = ''
        if spec['header_format'] == 'IMAGE' and receipt_attachment is not None:
            tpl.header_media = receipt_attachment
        tpl.body_text = spec['body']
        tpl.footer_text = ''      # no footer, office request 2026-09-05
        tpl.content_type = None       # parameters are filled explicitly at send time
        tpl.save()                    # pre_save extracts body_text_numbered_mapping
        tpl.refresh_from_db()
        stored = list((tpl.body_text_numbered_mapping or {}).values())
        if stored != spec['params']:
            raise RuntimeError(f"{spec['name']}: stored variables {stored} != expected {spec['params']}")
        rows.append(tpl)
    return rows


def submit_templates(account, names: Optional[List[str]] = None) -> Dict[str, str]:
    """Submit every draft/rejected notice template to Meta. Returns {name: status-or-error}."""
    from modules.whatsapp.models import WhatsAppTemplate
    out: Dict[str, str] = {}
    for kind, spec in NOTICE_TEMPLATES.items():
        if names and spec['name'] not in names:
            continue
        tpl = WhatsAppTemplate.objects.filter(whatsapp_account=account, template_name=spec['name']).first()
        if tpl is None:
            out[spec['name']] = 'missing (run ensure_templates first)'
            continue
        if tpl.status in ('approved', 'pending'):
            out[spec['name']] = tpl.status
            continue
        try:
            examples = list(spec['examples'])   # positional, in mapping order
            account.service.create_template(tpl, body_examples=examples or None)
            tpl.refresh_from_db()
            out[spec['name']] = f'{tpl.status} (meta id {tpl.template_id})'
        except Exception as exc:
            out[spec['name']] = f'ERROR: {str(exc)[:300]}'
    return out


def template_for(kind: str, account):
    """The APPROVED template for a notice kind on this account, or None."""
    from modules.whatsapp.models import WhatsAppTemplate
    spec = NOTICE_TEMPLATES.get(kind)
    if not spec or account is None:
        return None
    return WhatsAppTemplate.objects.filter(
        whatsapp_account=account, template_name=spec['name'], status='approved',
    ).first()


# ── the 24-hour window ───────────────────────────────────────────────────────

def window_open(conversation) -> bool:
    """True while WhatsApp still accepts free-form text: an inbound in the last 24 h."""
    try:
        from django.utils import timezone
        from modules.chat.models import Message
        last = (Message.objects_all
                .filter(conversation=conversation, direction='inbound')
                .order_by('-created_at').values_list('created_at', flat=True).first())
        return bool(last and timezone.now() - last < timedelta(hours=WINDOW_HOURS))
    except Exception:
        return True   # when unsure, behave as before (free-form)


def delivery_mode() -> str:
    mode = str(NOTICE_DELIVERY or 'direct').strip().lower()
    return mode if mode in ('direct', 'template', 'auto') else 'direct'


def use_template_now(conversation) -> bool:
    """Should this notice go as a template right away (an approved one exists)?"""
    mode = delivery_mode()
    if mode == 'template':
        return True
    if mode == 'auto':
        return not window_open(conversation)
    return False


def fallback_allowed() -> bool:
    """May a refused direct send (131047) be retried as a template?"""
    return delivery_mode() in ('auto', 'template')


# ── quote injection for template sends ───────────────────────────────────────

_template_reply_to: contextvars.ContextVar = contextvars.ContextVar('qurtoba_template_reply_to', default=None)


class template_reply_to:
    """Inside this block, every template POST carries ``context.message_id``."""

    def __init__(self, wamid: Optional[str]):
        self.wamid = wamid

    def __enter__(self):
        self._token = _template_reply_to.set(self.wamid)
        return self

    def __exit__(self, *exc):
        _template_reply_to.reset(self._token)
        return False


def install_context_hook() -> bool:
    """Wrap WhatsAppAPIService._make_request once so a template send inside
    ``template_reply_to(wamid)`` is delivered as a quoted reply. Idempotent."""
    try:
        from functools import wraps
        from modules.whatsapp.services.api import WhatsAppAPIService
    except Exception:
        logger.exception('notice_templates: WhatsAppAPIService unavailable; hook NOT installed')
        return False
    original = WhatsAppAPIService._make_request
    if getattr(original, '_qurtoba_template_context', False):
        return True

    @wraps(original)
    def with_context(self, method, endpoint, data=None, files=None):
        wamid = _template_reply_to.get()
        if wamid and isinstance(data, dict) and data.get('type') == 'template' and 'context' not in data:
            data = {**data, 'context': {'message_id': wamid}}
        return original(self, method, endpoint, data, files)

    with_context._qurtoba_template_context = True
    WhatsAppAPIService._make_request = with_context
    logger.info('notice_templates: template quote hook installed on WhatsAppAPIService._make_request')
    return True


# ── sending ──────────────────────────────────────────────────────────────────

def send_notice(kind: str, ctx: Dict[str, Any], params: Dict[str, Any], header_url: Optional[str] = None) -> Dict[str, Any]:
    """Send a notice as its approved template, quoted on the customer's message.

    ``ctx`` is the tasks._notify_context() dict (conv, system_partner, reply_wamid,
    reply_local_id, svc). Returns {'success', 'message_id', 'chat_message_id', 'error'}.
    """
    conv = ctx['conv']
    account = getattr(conv, 'social_account', None)
    tpl = template_for(kind, account)
    if tpl is None:
        return {'success': False, 'message_id': None, 'chat_message_id': None,
                'error': f'no approved template for {kind}'}
    spec = NOTICE_TEMPLATES[kind]
    body_params = {k: str(params.get(k, '')) for k in spec['params']}
    header_params = None
    if spec['header_format'] == 'IMAGE':
        if not header_url:
            return {'success': False, 'message_id': None, 'chat_message_id': None,
                    'error': 'receipt template needs header_url'}
        header_params = [{'type': 'image', 'url': header_url}]
    try:
        with template_reply_to(ctx.get('reply_wamid')):
            response = account.service.send_template_message(
                tpl, conv.social_partner, body_params or None, header_params,
                None, message_content=tpl.replace_variables(body_params),
            )
        message_id = response.get('message_id')
        if not message_id:
            return {'success': False, 'message_id': None, 'chat_message_id': None, 'error': 'no message_id'}
        chat_message_id = None
        try:
            from modules.chat.services.chat_bridge_service import ChatBridgeService
            result = ChatBridgeService().receive_message(
                message_id=message_id, message_type='template',
                content={'template': response.get('display_data', {})},
                platform_type='whatsapp', customer_partner=conv.social_partner,
                social_account=account, inbound=False, system_partner=ctx.get('system_partner'),
            )
            msgs = (result or {}).get('messages') or []
            chat_message_id = msgs[0].get('id') if msgs else None
        except Exception:
            logger.exception('notice_templates: template sent but chat row failed (%s)', kind)
        logger.info('[CashSys Notify] %s sent as TEMPLATE %s (quoted=%s)', kind, tpl.template_name, bool(ctx.get('reply_wamid')))
        return {'success': True, 'message_id': message_id, 'chat_message_id': chat_message_id, 'error': None}
    except Exception as exc:
        logger.exception('notice_templates: template send failed (%s)', kind)
        return {'success': False, 'message_id': None, 'chat_message_id': None, 'error': str(exc)[:300]}


def send_text_or_template(kind: str, ctx: Dict[str, Any], text: str, params: Dict[str, Any],
                          reply: bool = True) -> Dict[str, Any]:
    """Deliver per NOTICE_DELIVERY: direct text (old way), the template, or
    auto (direct inside the window, template outside / on a 131047 refusal)."""
    conv = ctx['conv']
    if use_template_now(conv) and template_for(kind, getattr(conv, 'social_account', None)):
        sent = send_notice(kind, ctx, params)
        if sent.get('success'):
            return sent
        logger.warning('notice_templates: template %s failed (%s) — falling back to direct text', kind, sent.get('error'))
    kwargs = dict(partner=conv.social_partner, content={'text': text}, message_type='text',
                  conversation=conv, system_partner=ctx.get('system_partner'), websocket=True)
    if reply:
        kwargs.update(reply_to_message_id=ctx.get('reply_wamid'), reply_to_id=ctx.get('reply_local_id'))
    result = ctx['svc'].send_and_broadcast(**kwargs) or {}
    if (not result.get('success') and result.get('error_code') in REENGAGEMENT_ERROR_CODES
            and fallback_allowed()):
        fallback = send_notice(kind, ctx, params)
        if fallback.get('success'):
            return fallback
    return result
