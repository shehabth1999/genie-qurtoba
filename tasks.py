import logging
from decimal import Decimal

import requests
from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def pull_cash_sys_catalog_task(self):
    """
    Pull plans and vip pages from Cash-SYS and full-replace the local cache.

    Endpoint: GET {CASH_SYS_BASE_URL}/api/v1/integration/catalog/
    Auth    : Authorization: Token {CASH_SYS_TOKEN}

    Called periodically (e.g. every 6 hours via Celery beat) or on demand.
    Each call does a full replace — delete all rows, then insert fresh from API.
    """
    from qurtoba.models import CashSysPlan, CashSysVipPage

    base  = getattr(settings, 'CASH_SYS_BASE_URL', '').rstrip('/')
    token = getattr(settings, 'CASH_SYS_TOKEN', '')

    if not base or not token:
        logger.error('[CashSys Catalog] CASH_SYS_BASE_URL or CASH_SYS_TOKEN not configured — skipping')
        return

    try:
        resp = requests.get(
            f'{base}/api/v1/integration/catalog/',
            headers={'Authorization': f'Token {token}'},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error('[CashSys Catalog] Failed to fetch catalog: %s', exc)
        countdown = [30, 60, 120][min(self.request.retries, 2)]
        raise self.retry(exc=exc, countdown=countdown)

    plans_raw    = data.get('plans', [])
    vip_pages_raw = data.get('vip_pages', [])

    # Full replace — atomic: delete all, bulk create
    CashSysPlan.objects.all().delete()
    CashSysPlan.objects.bulk_create([
        CashSysPlan(
            cash_sys_id   = p['id'],
            name          = p['name'],
            type          = p['type'],
            price         = Decimal(p['price']),
            device_limit  = p['device_limit'],
            sim_limit     = p['sim_limit'],
            account_limit = p['account_limit'],
            vip_pages     = p.get('vip_pages', []),
            is_active     = p.get('is_active', True),
        )
        for p in plans_raw
    ])

    CashSysVipPage.objects.all().delete()
    CashSysVipPage.objects.bulk_create([
        CashSysVipPage(
            cash_sys_id = vp['id'],
            name        = vp['name'],
            key         = vp['key'],
            price       = Decimal(vp['price']),
        )
        for vp in vip_pages_raw
    ])

    logger.info(
        '[CashSys Catalog] Synced %d plans, %d vip pages',
        len(plans_raw), len(vip_pages_raw),
    )


# ───────────────────────── Cash-SYS chain helpers ──────────────────────────
#
# An order is no longer "one order = one transfer". Cash-SYS may fulfil it in
# several partial transfers (each → order_progress), settle the chain (order_done
# with a transactions[] array — fulfilled may be < value), or cancel a part
# (order_canceled; reroute:true ⇒ the recipient number hit its receive limit and
# the remainder must go to a NEW number the customer supplies). All three webhooks
# are grouped by root_external_ref, which maps back to qurtoba_record_id.


def _resolve_root_record(data: dict):
    """
    Find the Genie QurtobaRecord for a webhook, grouped by the chain root.
    Cash-SYS children use refs like "{root}#partN"; the root maps to
    str(qurtoba_record_id). Returns the record or None.
    """
    from qurtoba.models import QurtobaRecord

    ref = data.get('root_external_ref') or data.get('external_ref') or ''
    root = str(ref).split('#')[0].strip()
    try:
        qurtoba_record_id = int(root)
    except (ValueError, TypeError):
        logger.error('[CashSys] bad root_external_ref=%r — cannot process', ref)
        return None

    record = QurtobaRecord.objects.select_related('customer', 'partner', 'origin_message').filter(
        qurtoba_record_id=qurtoba_record_id
    ).first()
    if not record:
        logger.warning(
            '[CashSys] NO RECORD FOUND qurtoba_record_id=%s order_id=%s '
            '— record may not have synced to Genie yet',
            qurtoba_record_id, data.get('order_id'),
        )
    return record


def _event_already_processed(record, event: str, order_id, txn_id) -> bool:
    """
    Idempotent dedupe over (event, order_id, txn_id) — Cash-SYS retries any non-2xx
    up to 3 times. Returns True if this exact event was already handled.
    """
    from qurtoba.models import QurtobaRecord

    key = f'{event}:{order_id}:{txn_id}'
    log = list(record.cash_sys_event_log or [])
    if key in log:
        logger.info('[CashSys] duplicate event %s for record %d — skipping', key, record.pk)
        return True
    log.append(key)
    QurtobaRecord.objects.filter(pk=record.pk).update(cash_sys_event_log=log)
    record.cash_sys_event_log = log
    return False


def _normalize_brief(txn: dict, record) -> dict:
    """Normalize a Cash-SYS transfer brief into the shape we persist + render."""
    txn = txn or {}
    return {
        'id':          txn.get('id'),
        'value':       txn.get('value'),
        'transfer_to': txn.get('transfer_to') or (record.account_number or '-'),
        'fee':         txn.get('fee'),
        'sim_number':  txn.get('sim_number'),
        'device_name': txn.get('device_name'),
        'executed_at': txn.get('executed_at'),
        'attachment_id': None,
        'sent':        False,
    }


def _merge_briefs(record, new_briefs: list) -> list:
    """
    Merge transfer briefs into record.cash_sys_transactions, deduping by id
    (preserving the existing 'sent'/'attachment_id' state). Persists + returns
    the merged list.
    """
    from qurtoba.models import QurtobaRecord

    existing = list(record.cash_sys_transactions or [])
    by_id = {b.get('id'): b for b in existing if b.get('id') is not None}
    for nb in new_briefs:
        bid = nb.get('id')
        if bid is not None and bid in by_id:
            continue  # already tracked — keep its sent/attachment state
        existing.append(nb)
        if bid is not None:
            by_id[bid] = nb
    QurtobaRecord.objects.filter(pk=record.pk).update(cash_sys_transactions=existing)
    record.cash_sys_transactions = existing
    return existing


def _resolve_origin_message(record, conv):
    """
    The chat message the receipt / reroute notice should QUOTE.

    Prefer the linked origin_message. If it's missing — e.g. the transaction was
    parked for credit-limit review and approved later, or the agent simply forgot
    to pass source_message_id — fall back to the inbound message in this
    conversation whose text contains the destination phone (account_number). This
    guarantees the background notice always replies to the NUMBER message, exactly
    like the cash-app receipt. Backfills record.origin_message when found so the
    status tool and any later notices stay consistent.
    """
    if record.origin_message_id and getattr(record, 'origin_message', None):
        return record.origin_message

    acct_digits = ''.join(ch for ch in str(record.account_number or '') if ch.isdigit())
    if len(acct_digits) < 9 or conv is None:
        return None
    needle = acct_digits[-10:]  # tolerate +20 / leading-zero variants

    from modules.chat.models import Message
    candidates = (
        Message.objects.filter(conversation=conv, direction='inbound')
        .order_by('-created_at')[:50]
    )
    best = None
    for m in candidates:
        c = m.content if isinstance(m.content, dict) else {}
        digits = ''.join(ch for ch in (c.get('text') or '') if ch.isdigit())
        if needle and needle in digits:
            best = m
            if getattr(m, 'social_id', None):
                break  # prefer a message we can quote on WhatsApp (has WAMID)
    if best is None:
        return None

    try:
        from qurtoba.models import QurtobaRecord
        QurtobaRecord.objects.filter(pk=record.pk).update(origin_message=best)
        record.origin_message = best
        logger.info('[CashSys Notify] backfilled origin_message=%s for record %d via phone match',
                    best.id, record.pk)
    except Exception:
        logger.warning('[CashSys Notify] origin_message backfill failed for record %d', record.pk, exc_info=True)
    return best


def _notify_context(record):
    """
    Resolve the send context for a record: (conv, system_partner, reply_wamid,
    reply_local_id, svc). Returns None when the record can't be notified (no
    partner / no supported conversation / no social account).
    """
    if not record.partner_id:
        logger.info(
            '[CashSys Notify] no partner on record %d — skipping '
            '(transaction was not created from a chat)', record.pk,
        )
        return None
    from modules.chat.services.omnichannel_send_service import OmnichannelSendService
    from qurtoba.extensions import _get_system_partner

    partner = record.partner
    conv = (
        partner.conversations
        .filter(type__in=['whatsapp', 'messenger', 'instagram', 'tiktok'])
        .order_by('-updated_at')
        .first()
    )
    if not conv:
        logger.warning('[CashSys Notify] partner %d has no supported conversation — skipping', partner.pk)
        return None
    if not conv.social_account:
        logger.warning('[CashSys Notify] conversation %d has no social_account — skipping', conv.pk)
        return None

    # Reply to (quote) the original transfer-request message.
    #   reply_wamid    = WhatsApp WAMID (Message.social_id) → quote on WhatsApp.
    #   reply_local_id = local chat Message id → quote shows in our chat UI too.
    origin = _resolve_origin_message(record, conv)
    reply_wamid = getattr(origin, 'social_id', None) if origin else None
    reply_local_id = origin.id if origin else None

    return {
        'conv': conv,
        'system_partner': _get_system_partner(conv),
        'reply_wamid': reply_wamid,
        'reply_local_id': reply_local_id,
        'svc': OmnichannelSendService(),
    }


def _build_and_save_receipt_for_txn(record, brief: dict):
    """
    Render the receipt image for ONE transfer brief, persist it as a
    base.Attachment, and return (attachment, public_url). (None, None) on failure.
    """
    try:
        from django.core.files.base import ContentFile
        from modules.base.models.attachment import Attachment
        from modules.chat.utils.file_utils import get_media_url_for_attachment
        from qurtoba.services.receipt_image import render_receipt_png_for_txn

        png = render_receipt_png_for_txn(brief)
        suffix = brief.get('id') or 'x'
        filename = f'qurtoba_receipt_{record.pk}_{suffix}.png'
        attachment = Attachment(name=filename, mime_type='image/png', type='image', size=len(png))
        attachment.file.save(filename, ContentFile(png), save=True)
        return attachment, get_media_url_for_attachment(attachment)
    except Exception as exc:
        logger.warning('[CashSys Notify] receipt render failed for record %d txn %s: %s',
                       record.pk, brief.get('id'), exc)
        return None, None


def _txn_text_fallback(record, brief: dict) -> str:
    return (
        f"✅ تم تنفيذ التحويل بنجاح\n"
        f"المبلغ: {float(brief.get('value') or 0):,.0f} جنيه\n"
        f"الرسوم: {brief.get('fee') or 0} جنيه\n"
        f"رقم الحساب: {brief.get('transfer_to') or record.account_number or '-'}\n"
        f"شريحة التنفيذ: {brief.get('sim_number') or '-'}"
    )


def _send_done_receipts(record):
    """
    Send a receipt image for every NOT-yet-sent transfer brief on the record, all
    at once (back-to-back), each as a reply quoting the original request message.
    Marks each brief sent=True so retries / companion events never re-send.
    Idempotent + best-effort: a per-brief failure falls back to a text receipt.
    """
    from qurtoba.models import QurtobaRecord

    briefs = list(record.cash_sys_transactions or [])
    pending = [b for b in briefs if not b.get('sent')]
    if not pending:
        return
    ctx = _notify_context(record)
    if not ctx:
        return

    first_attachment = None
    for brief in pending:
        attachment, url = _build_and_save_receipt_for_txn(record, brief)
        try:
            if url:
                result = ctx['svc'].send_and_broadcast(
                    partner=ctx['conv'].social_partner,
                    content={'url': url, 'filename': attachment.name},
                    message_type='image',
                    conversation=ctx['conv'],
                    system_partner=ctx['system_partner'],
                    reply_to_message_id=ctx['reply_wamid'],
                    reply_to_id=ctx['reply_local_id'],
                    websocket=True,
                )
            else:
                result = ctx['svc'].send_and_broadcast(
                    partner=ctx['conv'].social_partner,
                    content={'text': _txn_text_fallback(record, brief)},
                    message_type='text',
                    conversation=ctx['conv'],
                    system_partner=ctx['system_partner'],
                    reply_to_message_id=ctx['reply_wamid'],
                    reply_to_id=ctx['reply_local_id'],
                    websocket=True,
                )
        except Exception as exc:
            logger.exception('[CashSys Notify] send failed for record %d txn %s: %s',
                             record.pk, brief.get('id'), exc)
            continue

        if result.get('success'):
            brief['sent'] = True
            if attachment is not None:
                brief['attachment_id'] = attachment.id
                first_attachment = first_attachment or attachment
            logger.info('[CashSys Notify] receipt sent record=%d txn=%s msg=%s',
                        record.pk, brief.get('id'), result.get('message_id'))
        else:
            logger.warning('[CashSys Notify] receipt delivery FAILED record=%d txn=%s err=%s',
                           record.pk, brief.get('id'), result.get('error'))

    # Persist the updated sent/attachment flags; keep one receipt on the record.
    update = {'cash_sys_transactions': briefs}
    if first_attachment is not None and not record.receipt_attachment_id:
        update['receipt_attachment'] = first_attachment
        record.receipt_attachment = first_attachment
    QurtobaRecord.objects.filter(pk=record.pk).update(**update)
    record.cash_sys_transactions = briefs


def _send_reroute_ask(record, fulfilled, reroute_amount):
    """
    Tell the customer their recipient number is over its receive limit and we
    need a new number — sent AFTER any done receipts. Wording depends on whether
    any part was actually transferred.
    """
    ctx = _notify_context(record)
    if not ctx:
        return
    if fulfilled and float(fulfilled) > 0:
        text = (
            f"هذا الرقم تجاوز الحد الاقصي للمعاملات، تم تحويل {float(fulfilled):,.0f} "
            f"محتاجين رقم تاني عشان نكمل باقي عملية التحويل"
        )
    else:
        text = "هذا الرقم تجاوز الحد الاقصي للمعاملات محتاجين رقم تاني نحول عليه"
    try:
        ctx['svc'].send_and_broadcast(
            partner=ctx['conv'].social_partner,
            content={'text': text},
            message_type='text',
            conversation=ctx['conv'],
            system_partner=ctx['system_partner'],
            reply_to_message_id=ctx['reply_wamid'],
            reply_to_id=ctx['reply_local_id'],
            websocket=True,
        )
        logger.info('[CashSys Notify] reroute ask sent record=%d fulfilled=%s remainder=%s',
                    record.pk, fulfilled, reroute_amount)
    except Exception as exc:
        logger.exception('[CashSys Notify] reroute ask failed record=%d: %s', record.pk, exc)


# ─────────────────────── Auto service fee (مصاريف خدمه) ─────────────────────
#
# Each executed Cash-SYS transfer carries a `fee` (مصاريف الخدمة) shown on the
# receipt. The SYSTEM auto-records it as a separate `مصاريف خدمه` debt record and
# posts a static note quoting the original request message. The AI agent never
# creates these.
SERVICE_FEE_TYPE = 'مصاريف خدمه'
SERVICE_FEE_THRESHOLD = 60000   # total transferred ≤ this → one fee (highest); above → one per transfer
SERVICE_FEE_MESSAGE = "تم اضافه {x} جنيه مصاريف خدمه\n( الرقم عليه محفظه اخرى غير فودافون كاش )"


def _floor_fee(value):
    """Drop the decimal fraction (6.8 → 6, 1.9 → 1). Returns int, or None if unparseable."""
    try:
        return int(float(value))  # truncates toward zero == floor for non-negative fees
    except (TypeError, ValueError):
        return None


def _service_fee_plan(briefs):
    """
    Pure decision: given the transfer briefs, return the list of مصاريف خدمه amounts
    to create (each an int, fraction dropped, ≥ 2).
      - floor each fee; keep only ≥ 2 (0/1 and 1.9→1 skipped).
      - total transferred ≤ 60,000 → one fee = the highest; else → all (one per transfer).
    """
    fees = []
    total_transferred = 0.0
    for b in (briefs or []):
        try:
            total_transferred += float(b.get('value') or 0)
        except (TypeError, ValueError):
            pass
        ff = _floor_fee(b.get('fee'))
        if ff is not None and ff >= 2:
            fees.append(ff)
    if not fees:
        return []
    return [max(fees)] if total_transferred <= SERVICE_FEE_THRESHOLD else fees


def _create_service_fees(record):
    """
    Auto-create the مصاريف خدمه debt record(s) for an executed order and post the
    static fee note (quoting the request message). Rules:
      - floor each transfer's fee; keep only floored fee ≥ 2 (0/1 — and 1.9→1 — skipped).
      - total transferred ≤ 60,000 → ONE fee record = the highest floored fee.
      - total transferred > 60,000 → ONE fee record per transfer (not summed).
    Idempotent via record.cash_sys_service_fee_done. The fee record is a normal
    Genie debt → pushed to the Qurtoba accountant; it is NOT a cash type so it
    never triggers another Cash-SYS order.
    """
    if record.cash_sys_service_fee_done:
        return
    from qurtoba.models import QurtobaRecord

    chosen = _service_fee_plan(record.cash_sys_transactions or [])

    # Mark done up-front so a webhook retry / companion event never double-charges.
    QurtobaRecord.objects.filter(pk=record.pk).update(cash_sys_service_fee_done=True)
    record.cash_sys_service_fee_done = True

    if not chosen:
        return

    ctx = _notify_context(record)
    for fee in chosen:
        try:
            fee_rec = QurtobaRecord.objects.create(
                customer=record.customer,
                type=SERVICE_FEE_TYPE,
                value=fee,
                account_number=None,
                is_down=False,
                is_seller=False,
                partner=record.partner,
                notes=f'[auto] مصاريف خدمة لعملية #{record.pk}',
            )
            if record.origin_message_id:
                QurtobaRecord.objects.filter(pk=fee_rec.pk).update(origin_message_id=record.origin_message_id)
        except Exception as exc:
            logger.exception('[CashSys Fee] create failed record=%d fee=%s: %s', record.pk, fee, exc)
            continue
        logger.info('[CashSys Fee] created مصاريف خدمه %s for record=%d', fee, record.pk)
        if ctx:
            try:
                # Service-fee note is a standalone message — NOT a quoted reply.
                ctx['svc'].send_and_broadcast(
                    partner=ctx['conv'].social_partner,
                    content={'text': SERVICE_FEE_MESSAGE.format(x=fee)},
                    message_type='text',
                    conversation=ctx['conv'],
                    system_partner=ctx['system_partner'],
                    websocket=True,
                )
            except Exception as exc:
                logger.exception('[CashSys Fee] message failed record=%d fee=%s: %s', record.pk, fee, exc)


# ───────────────────────── Cash-SYS webhook handlers ───────────────────────


@shared_task(bind=True, max_retries=3)
def handle_cash_sys_order_progress(self, data: dict):
    """
    A partial transfer completed and left a remainder. Record progress under the
    chain root. NEVER mark the order done and NEVER send a receipt/message here —
    receipts are sent together at the terminal moment (done / reroute-cancel).
    """
    from qurtoba.models import QurtobaRecord

    record = _resolve_root_record(data)
    if not record:
        return
    txn = data.get('transaction') or {}
    if _event_already_processed(record, 'order_progress', data.get('order_id'), txn.get('id')):
        return

    _merge_briefs(record, [_normalize_brief(txn, record)])
    fulfilled = data.get('fulfilled')
    state = record.cash_sys_state if record.cash_sys_state in ('rerouted', 'done') else 'partial'
    QurtobaRecord.objects.filter(pk=record.pk).update(
        cash_sys_state=state,
        cash_sys_fulfilled=fulfilled if fulfilled is not None else record.cash_sys_fulfilled,
    )
    logger.info('[CashSys Progress] record=%d fulfilled=%s remaining=%s — no message',
                record.pk, fulfilled, data.get('remaining'))


@shared_task(bind=True, max_retries=3)
def handle_cash_sys_order_done(self, data: dict):
    """
    The chain settled. Mark done for `fulfilled` (which may be < value when the
    rest was rerouted), merge all transfer briefs, and send EVERY done receipt
    image together as replies quoting the original request. Never sets is_done —
    that field is managed by the accounting flow only.
    """
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    from qurtoba.models import QurtobaRecord

    record = _resolve_root_record(data)
    if not record:
        return
    if _event_already_processed(record, 'order_done', data.get('order_id'), 'done'):
        return

    # Briefs: prefer the full transactions[] array; fall back to the single brief.
    raw_txns = data.get('transactions') or ([data['transaction']] if data.get('transaction') else [])
    briefs = [_normalize_brief(t, record) for t in raw_txns]
    _merge_briefs(record, briefs)

    done_at = parse_datetime(data.get('done_at') or '') or timezone.now()
    fulfilled = data.get('fulfilled', data.get('value'))
    last = raw_txns[-1] if raw_txns else {}
    # Don't clobber a reroute that already settled this record.
    state = 'rerouted' if record.cash_sys_state == 'rerouted' else 'done'
    QurtobaRecord.objects.filter(pk=record.pk).update(
        cash_sys_done=True,
        cash_sys_done_at=done_at,
        cash_sys_state=state,
        cash_sys_fulfilled=fulfilled,
        cash_sys_fee=last.get('fee'),
        cash_sys_sim=last.get('sim_number'),
        cash_sys_device=last.get('device_name'),
    )
    record.refresh_from_db()

    _send_done_receipts(record)         # images first …
    _create_service_fees(record)        # … then the auto service-fee note(s)
    logger.info('[CashSys Done] record=%d order_id=%s value=%s fulfilled=%s txns=%d',
                record.pk, data.get('order_id'), data.get('value'), fulfilled, len(briefs))


@shared_task(bind=True, max_retries=3)
def handle_cash_sys_order_canceled(self, data: dict):
    """
    A part was canceled. For reroute:true (number_limit) settle the already-sent
    portion as done, edit the source record value down to `fulfilled`, propagate
    the edit to the accountant (port 6000), send the done receipt(s), then ask the
    customer for a new number for the remainder. For reroute:false just mark
    canceled — no reissue.
    """
    record = _resolve_root_record(data)
    if not record:
        return
    if _event_already_processed(record, 'order_canceled', data.get('order_id'), data.get('part_index')):
        return

    reason = data.get('cancel_reason')
    if data.get('reroute'):
        _apply_reroute(record, data)
    else:
        from qurtoba.models import QurtobaRecord
        QurtobaRecord.objects.filter(pk=record.pk).update(
            cash_sys_state='canceled', cash_sys_canceled_reason=reason,
        )
        logger.info('[CashSys Canceled] record=%d reason=%s (no reroute)', record.pk, reason)


def _apply_reroute(record, data: dict):
    """
    Settle the sent portion and prepare the remainder for reissue to a new number.

    Per the agreed split: the ORIGINAL record keeps the SENT part — edit its value
    down to `fulfilled` (e.g. 10000 → 6000), mark it done, keep its receipts. The
    remainder (`reroute_amount`) becomes a fully independent new order created later
    when the customer sends a new number. Propagate the value edit to the accountant.
    """
    from qurtoba.models import QurtobaRecord
    from qurtoba.utils_sync import edit_qurtoba_record_value

    fulfilled = data.get('root_fulfilled')
    if fulfilled is None:
        fulfilled = record.cash_sys_fulfilled or 0
    reroute_amount = data.get('reroute_amount') or 0

    # Remember the original value once, then settle the record at the sent amount.
    if record.cash_sys_original_value is None:
        record.cash_sys_original_value = record.value
    record.value = fulfilled
    record.cash_sys_done = True
    record.cash_sys_state = 'rerouted'
    record.cash_sys_fulfilled = fulfilled
    record.cash_sys_reroute_amount = reroute_amount
    record.cash_sys_canceled_reason = 'number_limit'
    record.save()  # recomputes customer balance with the corrected value

    # Propagate the value edit to the accountant (port 6000).
    if record.qurtoba_record_id:
        err = edit_qurtoba_record_value(record.qurtoba_record_id, fulfilled)
        if err:
            logger.error('[CashSys Reroute] accountant edit FAILED record=%d qid=%s: %s',
                         record.pk, record.qurtoba_record_id, err)

    logger.info('[CashSys Reroute] record=%d original=%s fulfilled=%s remainder=%s',
                record.pk, record.cash_sys_original_value, fulfilled, reroute_amount)

    # Send any not-yet-sent done receipts, then ask for a new number.
    record.refresh_from_db()
    _send_done_receipts(record)
    _send_reroute_ask(record, fulfilled, reroute_amount)


_RETRY_COUNTDOWNS = [5, 15, 30]  # seconds before retry attempt 2, 3, 4


@shared_task(bind=True, max_retries=3)
def push_record_to_qurtoba_task(self, record_pk: int):
    from qurtoba.utils_sync import push_record_to_qurtoba, _mark_error

    error = push_record_to_qurtoba(record_pk)
    if error:
        countdown = _RETRY_COUNTDOWNS[min(self.request.retries, len(_RETRY_COUNTDOWNS) - 1)]
        try:
            raise self.retry(exc=Exception(error), countdown=countdown)
        except self.MaxRetriesExceededError:
            _mark_error(record_pk, error)
            # Retries exhausted → log a sync-problem row + notify admins so the
            # failed push is visible and retryable from the UI (not silently lost).
            try:
                from qurtoba.models import QurtobaRecord, QurtobaSyncProblem
                rec = QurtobaRecord.objects.filter(pk=record_pk).first()
                if rec:
                    QurtobaSyncProblem.record(
                        rec, 'push_record', error,
                        payload={
                            'type': rec.type,
                            'value': rec.value,
                            'account_number': rec.account_number,
                            'is_down': rec.is_down,
                            'customer_id': rec.customer_id,
                        },
                    )
            except Exception as exc:
                logger.error('Failed to record QurtobaSyncProblem for record %s: %s', record_pk, exc)
