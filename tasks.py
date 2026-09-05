import datetime as dt
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

    # Full replace — genuinely atomic so a malformed row (e.g. a missing key)
    # rolls back the delete instead of leaving the catalog empty until the next run.
    from django.db import transaction as db_tx
    try:
        with db_tx.atomic():
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
    except Exception as exc:
        # Bad catalog shape — keep the existing catalog (rolled back) and retry.
        logger.error('[CashSys Catalog] Failed to apply catalog (kept previous): %s', exc)
        countdown = [30, 60, 120][min(self.request.retries, 2)]
        raise self.retry(exc=exc, countdown=countdown)

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


class AmbiguousWebhookTarget(Exception):
    """The webhook cannot be tied to exactly one record. Never guess on money."""


def _root_id_from_ref(ref) -> int | None:
    """Parse the chain root out of a Cash-SYS ref ("{root}" or "{root}#partN")."""
    if ref in (None, ''):
        return None
    try:
        return int(str(ref).split('#')[0].strip())
    except (ValueError, TypeError):
        return None


def _resolve_root_record(data: dict):
    """
    Find the ONE Genie QurtobaRecord this webhook is about.

    Every caller of this uses the result to move money — zero a debt, edit a
    ledger value. Resolving to the wrong record therefore zeroes the wrong
    customer's transaction, so this function guesses at NOTHING: it either
    returns exactly one record, or it raises/returns None and the failure is made
    visible. Two rules enforce that.

    RULE 1 — NO `x or y` FALLBACK BETWEEN ID FIELDS.
    This used to read `root_external_ref or external_ref`, which silently swapped
    to a different identifier the moment the first was null or empty. Those two
    fields do not always denote the same order, so the fallback could resolve a
    DIFFERENT transaction and zero it. Now: whichever fields are present are each
    parsed to a root, and if they disagree the webhook is refused rather than
    resolved to one of them arbitrarily.

    RULE 2 — REFUSE AMBIGUITY.
    `qurtoba_record_id` is NOT unique in Genie: the pull side has inserted the
    same Qurtoba row more than once (57 groups / 146 rows observed, up to 4 copies
    of one id). `.first()` on that lookup, under `Meta.ordering = ['-date','-time']`
    which ties across copies, picks arbitrarily — i.e. a coin flip over which
    record gets zeroed. When more than one matches we refuse and raise, so a human
    resolves it instead of the database picking.
    """
    from qurtoba.models import QurtobaRecord

    # Collect a root from EACH id field that is actually present — no fallback.
    roots = {}
    for key in ('root_external_ref', 'external_ref'):
        if key in data and data.get(key) not in (None, ''):
            parsed = _root_id_from_ref(data.get(key))
            if parsed is None:
                logger.error('[CashSys] unparseable %s=%r order_id=%s — refusing',
                             key, data.get(key), data.get('order_id'))
                raise AmbiguousWebhookTarget(
                    f'unparseable {key}={data.get(key)!r}'
                )
            roots[key] = parsed

    if not roots:
        logger.error('[CashSys] webhook carries NO usable external ref order_id=%s — refusing',
                     data.get('order_id'))
        raise AmbiguousWebhookTarget('no root_external_ref / external_ref in payload')

    distinct = set(roots.values())
    if len(distinct) > 1:
        # Two ids that point at different orders. Previously the `or` hid this
        # completely and one of them was used.
        logger.error('[CashSys] CONFLICTING refs %s order_id=%s — refusing to guess',
                     roots, data.get('order_id'))
        raise AmbiguousWebhookTarget(f'conflicting external refs: {roots}')

    qurtoba_record_id = distinct.pop()

    matches = list(
        QurtobaRecord.objects
        .select_related('customer', 'partner', 'origin_message')
        .filter(qurtoba_record_id=qurtoba_record_id)
    )

    if len(matches) > 1:
        logger.error(
            '[CashSys] AMBIGUOUS qurtoba_record_id=%s matches %d Genie records %s '
            '(order_id=%s) — refusing to act on money',
            qurtoba_record_id, len(matches), [m.pk for m in matches], data.get('order_id'),
        )
        raise AmbiguousWebhookTarget(
            f'qurtoba_record_id={qurtoba_record_id} matches {len(matches)} Genie '
            f'records {[m.pk for m in matches]} — duplicate pull; cannot tell which '
            f'to zero. Resolve the duplicates, then retry from the UI.'
        )

    if not matches:
        logger.warning(
            '[CashSys] NO RECORD FOUND qurtoba_record_id=%s order_id=%s '
            '— record may not have synced to Genie yet',
            qurtoba_record_id, data.get('order_id'),
        )
        return None

    return matches[0]


def _claim_event(record, event: str, order_id, txn_id):
    """Claim an incoming webhook for exactly-once processing.

    Returns ``(skip, commit_key, cache_key)``:
      • ``skip=True``  → already processed durably, or another delivery is
        in-flight right now → caller must return without processing.
      • ``skip=False`` → caller owns processing and MUST call ``_commit_event``
        on success or ``_release_event`` on failure (so a retry can re-claim).

    A Cash-SYS retry typically arrives while the first delivery is still being
    processed, so two duplicate webhooks can run concurrently. The durable
    ``cash_sys_event_log`` append alone is a racy read-modify-write (both tasks
    read the log before either writes ⇒ both process ⇒ duplicate transaction).
    ``cache.add`` is an atomic SETNX in-flight lock that closes that race; the
    durable log is the backstop once the lock expires. Crucially the durable
    commit happens only AFTER the work succeeds, so a failed delivery can be
    retried instead of being marked done and silently lost.
    """
    from django.core.cache import cache

    key = f'{event}:{order_id}:{txn_id}'
    if key in list(record.cash_sys_event_log or []):
        logger.info('[CashSys] duplicate event %s for record %d — skipping (log)', key, record.pk)
        return True, None, None

    cache_key = f'qurtoba:cashsys:evt:{record.pk}:{key}'
    if not cache.add(cache_key, 1, timeout=600):
        logger.info('[CashSys] duplicate event %s for record %d — skipping (in-flight)', key, record.pk)
        return True, None, None

    return False, key, cache_key


def _commit_event(record, key: str):
    """Durably mark a webhook event processed — only after it fully succeeded.

    Locks the row for the read-modify-write so two legitimate concurrent events
    on the same record (e.g. two partials) can't lose each other's log entry.
    """
    from django.db import transaction as db_tx
    from qurtoba.models import QurtobaRecord

    with db_tx.atomic():
        locked = QurtobaRecord.objects.select_for_update().only(
            'id', 'cash_sys_event_log'
        ).get(pk=record.pk)
        log = list(locked.cash_sys_event_log or [])
        if key not in log:
            log.append(key)
            QurtobaRecord.objects.filter(pk=record.pk).update(cash_sys_event_log=log)
    record.cash_sys_event_log = log


def _release_event(cache_key):
    """Release the in-flight lock so a retry of a FAILED event can re-claim it."""
    if cache_key:
        from django.core.cache import cache
        cache.delete(cache_key)


def _webhook_retry_or_record(task, record, event: str, data: dict, exc):
    """A webhook handler crashed. The view already acked 200 to Cash-SYS, so this
    would otherwise be a silent loss. Retry with backoff; on exhaustion record a
    visible, UI-retryable ``QurtobaSyncProblem`` + notify admins — mirroring
    ``push_record_to_qurtoba_task``.
    """
    countdown = _RETRY_COUNTDOWNS[min(task.request.retries, len(_RETRY_COUNTDOWNS) - 1)]
    try:
        raise task.retry(exc=exc, countdown=countdown)
    except task.MaxRetriesExceededError:
        logger.exception('[CashSys] %s permanently failed record=%s order=%s: %s',
                         event, getattr(record, 'pk', None), data.get('order_id'), exc)
        try:
            if record is not None:
                from qurtoba.models import QurtobaSyncProblem
                QurtobaSyncProblem.record(record, f'cash_sys_{event}', str(exc), payload=data)
        except Exception as e2:
            logger.error('[CashSys] failed to record problem for %s record=%s: %s',
                         event, getattr(record, 'pk', None), e2)


def _require_ledger_id(record, what: str) -> None:
    """
    Refuse to proceed with a money change when the record has no ledger id.

    Both callers previously wrapped their accountant call in
    `if record.qurtoba_record_id:` and then applied the local change regardless.
    That is the worst possible shape for a money operation: the Qurtoba ledger is
    left untouched while Genie — and the customer — are told it was applied. Fail
    here instead, so the webhook retries and, on exhaustion, leaves a visible
    QurtobaSyncProblem.
    """
    if not record.qurtoba_record_id:
        msg = (
            f'{what}: record {record.pk} has no qurtoba_record_id, so the ledger '
            f'cannot be updated. Refusing to apply the change locally — doing so '
            f'would tell the customer their money moved while the ledger disagrees.'
        )
        logger.error('[CashSys] %s', msg)
        _record_money_api_failure(
            record, 'push_record', msg,
            {'intent': what, 'qurtoba_record_id': None,
             'value': record.value, 'customer_id': record.customer_id},
        )
        raise RuntimeError(msg)


def _record_money_api_failure(record, operation: str, error: str, payload: dict) -> None:
    """
    Make a failed money-affecting API call VISIBLE immediately.

    These calls change what a customer owes. When one does not reach Qurtoba the
    only previous trace was a log line, and the sync-problem row was written only
    after every retry had been exhausted — so for the whole retry window the
    ledger was wrong with nothing in the UI saying so. Best-effort and idempotent:
    the retries update the same row rather than creating new ones.
    """
    try:
        from qurtoba.models import QurtobaSyncProblem
        QurtobaSyncProblem.record(record, operation, error, payload=payload)
    except Exception as exc:
        logger.error('[CashSys] could not record money-API failure for record=%s: %s',
                     getattr(record, 'pk', None), exc)


def _record_unresolved_webhook(event: str, data: dict, error: str) -> None:
    """A webhook we could not tie to exactly one record — must never vanish."""
    try:
        from qurtoba.models import QurtobaSyncProblem
        key = data.get('order_id') or data.get('root_external_ref') or data.get('external_ref') or 'unknown'
        QurtobaSyncProblem.record_orphan(f'cash_sys_{event}', error, key, payload=data)
    except Exception as exc:
        logger.error('[CashSys] could not record unresolved webhook %s: %s', event, exc)


def _retry_if_unresolved(task, event: str, data: dict) -> None:
    """The QurtobaRecord couldn't be resolved yet — almost always a race where the
    Cash-SYS webhook beat the push/sync that creates it (e.g. the accountant-
    initiated flow fires forward_to_genie and the Cash-SYS order concurrently).
    The view already 200-acked Cash-SYS, so returning here loses the event for
    good. Retry with backoff to give the record time to appear; on exhaustion log
    a durable error so it's at least visible.
    """
    if task.request.retries >= task.max_retries:
        # Do NOT just log and drop. This is a real Cash-SYS outcome for a real
        # order; dropping it leaves the ledger permanently out of step with what
        # actually happened to the money, with no visible trace.
        refs = {k: data.get(k) for k in ('root_external_ref', 'external_ref') if k in data}
        logger.error('[CashSys] %s: record unresolved after %d retries. refs=%s order=%s',
                     event, task.request.retries, refs, data.get('order_id'))
        _record_unresolved_webhook(
            event, data,
            f'Cash-SYS reported "{event}" for an order that matches no Genie record '
            f'after {task.request.retries} retries (refs={refs}). The ledger was NOT '
            f'updated. Investigate and apply manually.',
        )
        return
    countdown = _RETRY_COUNTDOWNS[min(task.request.retries, len(_RETRY_COUNTDOWNS) - 1)]
    raise task.retry(countdown=countdown)


def _normalize_brief(txn: dict, record) -> dict:
    """Normalize a Cash-SYS transfer brief into the shape we persist + render."""
    txn = txn or {}
    return {
        'id':          txn.get('id'),
        'value':       txn.get('value'),
        'transfer_to': txn.get('transfer_to') or (record.account_number or '-'),
        'fee':         txn.get('fee'),
        'sim_number':  txn.get('sim_number'),
        'sim_code':    txn.get('sim_code'),
        'device_name': txn.get('device_name'),
        'operator':    txn.get('operator'),
        'executed_at': txn.get('executed_at'),
        'is_manual':   bool(txn.get('is_manual')),
        'attachment_id': None,
        'sent':        False,
    }


def _merge_briefs(record, new_briefs: list) -> list:
    """
    Merge transfer briefs into record.cash_sys_transactions, deduping by id
    (preserving the existing 'sent'/'attachment_id' state). Persists + returns
    the merged list.
    """
    from django.db import transaction as db_tx
    from qurtoba.models import QurtobaRecord

    # Row-lock the read-modify-write: two legitimate concurrent partial events on
    # the same record would otherwise each read the list and clobber the other's
    # append (a lost transfer brief ⇒ a receipt that never gets sent).
    with db_tx.atomic():
        locked = QurtobaRecord.objects.select_for_update().only(
            'id', 'cash_sys_transactions'
        ).get(pk=record.pk)
        existing = list(locked.cash_sys_transactions or [])
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


class _SystemSender:
    """OmnichannelSendService whose sends are the system's own voice.

    Every Cash-SYS notice, receipt and reroute ask goes out through the
    ``svc`` in _notify_context; wrapping it here marks all of them for the
    outbound gate (qurtoba.ai_guard) in one place, so a real cancel notice is
    never mistaken for the agent replaying one.
    """

    def __init__(self, service):
        self._service = service

    def send_and_broadcast(self, *args, **kwargs):
        from qurtoba.ai_guard import system_send
        with system_send():
            return self._service.send_and_broadcast(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._service, name)


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
        'svc': _SystemSender(OmnichannelSendService()),
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


# Images go to WhatsApp as a *link*, so the provider downloads the file before
# delivering it; a light follow-up text (مصاريف خدمه / تغيير رقم) can overtake the
# still-downloading receipt and arrive first. This pause lets the image land first.
#
# TODO(receipt-ordering): replace this fixed delay with a real guarantee — either
# wait for the image message's 'delivered' webhook status before sending the text,
# or pre-upload the media (send by media_id instead of link) so delivery is fast
# and in-order. The webhook already tracks sent/delivered/read status.
_RECEIPT_DELIVERY_DELAY = 8  # seconds


def _pause_after_receipts(images_sent):
    """Pause so a sent receipt image lands before the follow-up text. No-op when
    no image was actually sent (e.g. nothing pending, or the text fallback ran)."""
    if images_sent:
        import time
        time.sleep(_RECEIPT_DELIVERY_DELAY)


def _send_done_receipts(record):
    """
    Send a receipt image for every NOT-yet-sent transfer brief on the record, all
    at once (back-to-back), each as a reply quoting the original request message.
    Marks each brief sent=True so retries / companion events never re-send.
    Idempotent + best-effort: a per-brief failure falls back to a text receipt.

    Uses a Redis in-flight lock so concurrent callers (e.g. order_done +
    order_canceled/reroute arriving simultaneously) never both send the same
    receipt image.  The lock is released after the DB write so a subsequent
    legitimate caller sees the updated sent=True flags and skips cleanly.

    Returns the number of receipt IMAGES actually delivered (the text-fallback
    case does not count) — callers use it to decide whether to pause before
    sending a follow-up text so the heavy image lands first.
    """
    from django.core.cache import cache
    from qurtoba.models import QurtobaRecord

    send_lock_key = f'qurtoba:receipt_send:{record.pk}'
    if not cache.add(send_lock_key, 1, timeout=300):
        logger.info(
            '[CashSys Notify] receipt send already in-flight for record %d — skipping (lock)',
            record.pk,
        )
        return 0

    try:
        briefs = list(record.cash_sys_transactions or [])
        pending = [b for b in briefs if not b.get('sent')]
        if not pending:
            return 0

        ctx = _notify_context(record)
        if not ctx:
            return 0

        first_attachment = None
        images_sent = 0
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
                    images_sent += 1   # only a real image counts (not the text fallback)
                logger.info('[CashSys Notify] receipt sent record=%d txn=%s msg=%s',
                            record.pk, brief.get('id'), result.get('message_id'))
            else:
                logger.warning('[CashSys Notify] receipt delivery FAILED record=%d txn=%s err=%s',
                               record.pk, brief.get('id'), result.get('error'))

        # Persist the updated sent/attachment flags WITHOUT clobbering briefs that a
        # concurrent partial event may have appended while we were sending: re-read
        # under a row lock and apply only the per-brief flags we just changed. (The
        # `briefs` list was read before the slow sends, so writing it back wholesale
        # would drop any brief merged in meanwhile.)
        from django.db import transaction as db_tx

        flag_updates = {
            b.get('id'): b for b in pending
            if b.get('id') is not None and (b.get('sent') or b.get('attachment_id'))
        }
        with db_tx.atomic():
            locked = QurtobaRecord.objects.select_for_update().only(
                'id', 'cash_sys_transactions', 'receipt_attachment'
            ).get(pk=record.pk)
            fresh = list(locked.cash_sys_transactions or [])
            for b in fresh:
                u = flag_updates.get(b.get('id'))
                if u:
                    if u.get('sent'):
                        b['sent'] = True
                    if u.get('attachment_id'):
                        b['attachment_id'] = u['attachment_id']
            update = {'cash_sys_transactions': fresh}
            if first_attachment is not None and not locked.receipt_attachment_id:
                update['receipt_attachment'] = first_attachment
                record.receipt_attachment = first_attachment
            QurtobaRecord.objects.filter(pk=record.pk).update(**update)
        record.cash_sys_transactions = fresh
        return images_sent
    finally:
        cache.delete(send_lock_key)


def send_text_reply_for_record(record, text):
    """
    Send a plain-text reply to the chat that originated `record`, quoting the
    record's origin message when available. Synchronous; safe no-op when the
    record has no notifiable chat context. Returns True on send.
    """
    ctx = _notify_context(record)
    if not ctx:
        return False
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
        return True
    except Exception as exc:
        logger.warning('send_text_reply_for_record failed record=%s: %s',
                       getattr(record, 'pk', None), exc)
        return False


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
        remainder_txt = f"{float(reroute_amount):,.0f}" if reroute_amount else "......"
        text = (
            f"*تم تحويل ( {float(fulfilled):,.0f} ) و الباقى ( {remainder_txt} )*\n\n"
            f"محتاجين رقم تانى علشان نكمل\n"
            f"الرقم مش قابل تحويل تانى\n"
            f"( الرقم تجاوز الحد اليومى او الشهرى )"
        )
    else:
        text = (
            "*محتاجين رقم تانى نبعت عليه الرصيد*\n\n"
            "الرقم مش قابل تحويل \n"
            "( تجاوز الحد اليومى او الشهرى )"
        )
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


# Customer-facing notice per full-reversal cancel reason. The debt is already
# zeroed on the accountant ledger before this is sent, so the wording is truthful:
#   cancel_request → reassure nothing was recorded.
#   no_wallet      → ask for a different number (the current one has no wallet).
_CANCEL_NOTICE_MESSAGES = {
    'cancel_request': "تم الغاء التحويل\n\nو لم يتم تسجيل العمليه عليك",
    'no_wallet': "*محتاجين رقم تانى نبعت عليه الرصيد*\n\n*الرقم مش عليه محفظة*",
}


def _send_cancel_notice(record, reason):
    """Send the WhatsApp notice for a full-reversal cancel (no_wallet /
    cancel_request), quoting the original transfer-request message. Best-effort;
    never raises. Unknown reasons send nothing."""
    text = _CANCEL_NOTICE_MESSAGES.get(reason)
    if not text:
        return
    ctx = _notify_context(record)
    if not ctx:
        return
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
        logger.info('[CashSys Notify] cancel notice sent record=%d reason=%s', record.pk, reason)
    except Exception as exc:
        logger.exception('[CashSys Notify] cancel notice failed record=%d: %s', record.pk, exc)


# ─────────────────────── Auto service fee (مصاريف خدمه) ─────────────────────
#
# Each executed Cash-SYS transfer carries a `fee` (مصاريف الخدمة) shown on the
# receipt. The SYSTEM auto-records it as a separate `مصاريف خدمه` debt record and
# posts a static note quoting the original request message. The AI agent never
# creates these.
SERVICE_FEE_TYPE = 'مصاريف خدمه'
SERVICE_FEE_THRESHOLD = 60000   # total transferred ≤ this → one fee (highest); above → one per transfer
SERVICE_FEE_010_CAP = 30        # 010 (Vodafone) recipient, total ≤ threshold → summed fee capped at this
SERVICE_FEE_MESSAGE = "تم اضافه {x} جنيه مصاريف خدمه\n( الرقم عليه محفظه اخرى غير فودافون كاش )"


def _floor_fee(value):
    """Drop the decimal fraction (6.8 → 6, 1.9 → 1). Returns int, or None if unparseable."""
    try:
        return int(float(value))  # truncates toward zero == floor for non-negative fees
    except (TypeError, ValueError):
        return None


def _is_010_number(raw):
    """True if the recipient mobile is a Vodafone «010» number.

    Tolerates the country-code / formatting variants Cash-SYS may send
    (2010…, 002010…, +2010…, or the bare 10-digit 10…) and the canonical
    01XXXXXXXXX form we already store on cash records.
    """
    if not raw:
        return False
    d = ''.join(ch for ch in str(raw) if ch.isdigit())
    if d.startswith('0020'):
        d = d[2:]                 # 0020 10… → 010…
    elif d.startswith('20') and len(d) >= 12:
        d = '0' + d[2:]           # 20 10… → 010…
    elif len(d) == 10 and d.startswith('1'):
        d = '0' + d               # 10… (no leading 0) → 010…
    return d.startswith('010')


def _executed_briefs(briefs):
    """
    Keep only the transfer briefs that represent money that actually went out
    (a positive `value`). Briefs reach `cash_sys_transactions` only through
    order_progress / order_done, i.e. for executed transfers, but a reroute or
    cancel settles the record at the amount SENT and must never charge a fee for
    a part that did not move — so the fee plan is fed executed briefs only.
    `sent` is deliberately not used here: it marks the receipt as delivered, and a
    failed receipt delivery does not make the fee any less owed.
    """
    out = []
    for b in (briefs or []):
        try:
            if float((b or {}).get('value') or 0) > 0:
                out.append(b)
        except (TypeError, ValueError):
            continue
    return out


def _service_fee_plan(briefs, recipient=None):
    """
    Pure decision: given the transfer briefs, return the list of مصاريف خدمه amounts
    to create (each an int, fraction dropped, ≥ 2).
      - floor each fee; keep only ≥ 2 (0/1 and 1.9→1 skipped).
      - recipient number starts with «010» (Vodafone) → one fee = the SUM of all
        transfers' fees. If total transferred ≤ 60,000, that sum is CAPPED at
        SERVICE_FEE_010_CAP (30) — e.g. a summed fee of 10,000 is charged as 30.
        Above 60,000, the sum is charged uncapped.
      - otherwise (non-010): total transferred ≤ 60,000 → one fee = the highest,
        uncapped; else → all (one per transfer), uncapped.
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
    # 010 (Vodafone) recipient → one fee = the SUM of every transfer's fee.
    if _is_010_number(recipient):
        total_fee = sum(fees)
        if total_transferred <= SERVICE_FEE_THRESHOLD:
            total_fee = min(total_fee, SERVICE_FEE_010_CAP)
        return [total_fee]
    return [max(fees)] if total_transferred <= SERVICE_FEE_THRESHOLD else fees


def _create_service_fees(record):
    """
    Auto-create the مصاريف خدمه debt record(s) for an executed order and post the
    static fee note (quoting the request message). Rules:
      - floor each transfer's fee; keep only floored fee ≥ 2 (0/1 — and 1.9→1 — skipped).
      - recipient number starts with «010» (Vodafone) → ONE fee record = the SUM of
        all transfers' fees. Total transferred ≤ 60,000 → that sum is CAPPED at 30
        (SERVICE_FEE_010_CAP); above 60,000 → the sum is charged uncapped.
      - non-010 recipient, total transferred ≤ 60,000 → ONE fee record = the highest
        floored fee, uncapped.
      - non-010 recipient, total transferred > 60,000 → ONE fee record per transfer
        (not summed, not capped).
    Idempotent via record.cash_sys_service_fee_done. The fee record is a normal
    Genie debt → pushed to the Qurtoba accountant; it is NOT a cash type so it
    never triggers another Cash-SYS order.
    """
    if record.cash_sys_service_fee_done:
        return
    from qurtoba.models import QurtobaRecord

    # Executed transfers only (see _executed_briefs): called from the done path
    # AND from the reroute / partial-cancel settlement, where the record was
    # settled at the amount sent and only that part may carry a fee.
    executed = _executed_briefs(record.cash_sys_transactions)
    recipient = record.account_number or next(
        (b.get('transfer_to') for b in executed if b.get('transfer_to')),
        None,
    )
    chosen = _service_fee_plan(executed, recipient=recipient)

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

    # A refusal here means the payload cannot be tied to exactly ONE record
    # (missing/conflicting refs, or a duplicated qurtoba_record_id). Do not retry
    # — retrying cannot make an ambiguous payload unambiguous. Record it so a
    # human resolves it, because a real order really did change state.
    try:
        record = _resolve_root_record(data)
    except AmbiguousWebhookTarget as exc:
        _record_unresolved_webhook(
            'order_progress', data,
            f'Refused to act: {exc} — no ledger change was applied.',
        )
        return
    if not record:
        _retry_if_unresolved(self, 'order_progress', data)
        return
    txn = data.get('transaction') or {}
    skip, commit_key, cache_key = _claim_event(record, 'order_progress', data.get('order_id'), txn.get('id'))
    if skip:
        return

    try:
        _merge_briefs(record, [_normalize_brief(txn, record)])
        fulfilled = data.get('fulfilled')
        state = record.cash_sys_state if record.cash_sys_state in ('rerouted', 'done') else 'partial'
        QurtobaRecord.objects.filter(pk=record.pk).update(
            cash_sys_state=state,
            cash_sys_fulfilled=fulfilled if fulfilled is not None else record.cash_sys_fulfilled,
        )
        _commit_event(record, commit_key)
        logger.info('[CashSys Progress] record=%d fulfilled=%s remaining=%s — no message',
                    record.pk, fulfilled, data.get('remaining'))
    except Exception as exc:
        _release_event(cache_key)
        _webhook_retry_or_record(self, record, 'order_progress', data, exc)


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

    # A refusal here means the payload cannot be tied to exactly ONE record
    # (missing/conflicting refs, or a duplicated qurtoba_record_id). Do not retry
    # — retrying cannot make an ambiguous payload unambiguous. Record it so a
    # human resolves it, because a real order really did change state.
    try:
        record = _resolve_root_record(data)
    except AmbiguousWebhookTarget as exc:
        _record_unresolved_webhook(
            'order_done', data,
            f'Refused to act: {exc} — no ledger change was applied.',
        )
        return
    if not record:
        _retry_if_unresolved(self, 'order_done', data)
        return
    skip, commit_key, cache_key = _claim_event(record, 'order_done', data.get('order_id'), 'done')
    if skip:
        return

    try:
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
            cash_sys_sim_code=last.get('sim_code'),
            cash_sys_device=last.get('device_name'),
            cash_sys_operator=last.get('operator'),
        )
        record.refresh_from_db()

        images_sent = _send_done_receipts(record)   # images first …
        _pause_after_receipts(images_sent)           # … let the image land …
        _create_service_fees(record)                 # … then the auto service-fee note(s)
        _commit_event(record, commit_key)
        logger.info('[CashSys Done] record=%d order_id=%s value=%s fulfilled=%s txns=%d',
                    record.pk, data.get('order_id'), data.get('value'), fulfilled, len(briefs))
    except Exception as exc:
        _release_event(cache_key)
        _webhook_retry_or_record(self, record, 'order_done', data, exc)


@shared_task(bind=True, max_retries=3)
def handle_cash_sys_order_canceled(self, data: dict):
    """
    A part was canceled.

    reroute:true (number change / number_limit) — BUSINESS RULE:
      The current order is STOPPED and COMPLETED at the amount actually sent.
      i.e. edit its value down to `fulfilled` (the part that was transferred) and
      mark it DONE. The leftover is NOT carried by this order — when the customer
      sends a new number it becomes a COMPLETELY NEW, INDEPENDENT order through the
      normal create flow. So here we only: settle this order at `fulfilled`,
      propagate the value edit to the accountant (port 6000), send the done
      receipt(s), and ASK the customer for a new number. We never reissue here.

    reroute:false (plain customer/agent cancel) — just mark canceled, no reissue.
    """
    # A refusal here means the payload cannot be tied to exactly ONE record
    # (missing/conflicting refs, or a duplicated qurtoba_record_id). Do not retry
    # — retrying cannot make an ambiguous payload unambiguous. Record it so a
    # human resolves it, because a real order really did change state.
    try:
        record = _resolve_root_record(data)
    except AmbiguousWebhookTarget as exc:
        _record_unresolved_webhook(
            'order_canceled', data,
            f'Refused to act: {exc} — no ledger change was applied.',
        )
        return
    if not record:
        _retry_if_unresolved(self, 'order_canceled', data)
        return
    skip, commit_key, cache_key = _claim_event(record, 'order_canceled', data.get('order_id'), data.get('part_index'))
    if skip:
        return

    try:
        reason = data.get('cancel_reason')
        if data.get('reroute'):
            # Partial fulfilment: settle at the amount actually sent, not zero.
            _apply_reroute(record, data)
        else:
            # A cancellation that is NOT a reroute means the transfer did not
            # happen, so the customer must not owe it. Zero the ledger.
            #
            # This used to be an ALLOW-LIST — only 'no_wallet' and 'cancel_request'
            # zeroed, and every other reason fell into an else that merely marked
            # the record canceled and explicitly did "no ledger touch". Cash-SYS
            # also sends 'agent' (an operator cancelling inside the Cash app), and
            # that reason was not on the list, so those cancellations left the debt
            # standing at full value on both ledgers — silently, since marking it
            # canceled looks like success.
            #
            # Measured before the fix: 28 agent-cancelled records still carrying
            # 194,370 EGP across 16 customers, the oldest from 2026-06-17.
            #
            # Inverted deliberately: a new reason Cash-SYS invents tomorrow now
            # defaults to "the money did not move" rather than to "keep charging
            # the customer". _apply_zero_cancel refuses if anything WAS fulfilled,
            # and _send_cancel_notice only messages for reasons that have a
            # template — so 'agent' zeroes the ledger without texting the customer,
            # exactly as before.
            _apply_zero_cancel(record, reason)
        _commit_event(record, commit_key)
    except Exception as exc:
        _release_event(cache_key)
        _webhook_retry_or_record(self, record, 'order_canceled', data, exc)


def _apply_reroute(record, data: dict):
    """
    Number change → STOP and COMPLETE this order at the amount actually sent.

    BUSINESS RULE (number change = new order):
      • Edit THIS order's value down to `fulfilled` — the part that was really
        transferred (e.g. 10000 → 6000) — and mark it DONE. After this, the order
        reads "6000, done"; it no longer owes the remainder.
      • The leftover (`reroute_amount`, e.g. 4000) is NOT this order's concern.
        When the customer sends a new number it is created as a COMPLETELY NEW,
        INDEPENDENT order via the normal create flow. We do NOT reissue it here.
      • So: done(6000) on this order + new order(4000) on the new number = 10000.
      • `cash_sys_reroute_amount` below is stored for REFERENCE ONLY (shown in the
        status tool so the agent/customer knows the new order's amount); nothing
        consumes it to auto-create anything.
    """
    from qurtoba.models import QurtobaRecord
    from qurtoba.utils_sync import edit_qurtoba_record_value

    fulfilled = data.get('root_fulfilled')
    if fulfilled is None:
        fulfilled = record.cash_sys_fulfilled or 0
    reroute_amount = data.get('reroute_amount') or 0

    # Capture the original value once (before we overwrite it below).
    if record.cash_sys_original_value is None:
        record.cash_sys_original_value = record.value

    # ORDER MATTERS: update the accountant ledger (port 6000) FIRST, THEN save the
    # Genie record. QurtobaRecord.save() → recompute_balance() PULLS the balance
    # from port 6000 (the source of truth). If we saved first, Genie would pull the
    # STALE pre-edit balance (computed at the original value) and never re-sync,
    # leaving Genie's customer balance too high by the remainder. Editing port 6000
    # first means the save() below pulls the already-corrected Rest → both in sync.
    # Raise on failure so the handler retries instead of settling over an
    # inconsistent ledger / asking the customer for a new number prematurely.
    # Same rule as the zero-cancel: a missing ledger id is a hard failure. Skipping
    # the edit here would settle the order locally at `fulfilled` while the ledger
    # still carries the FULL original amount — the customer over-charged by the
    # remainder, silently.
    _require_ledger_id(record, 'reroute settle')
    err = edit_qurtoba_record_value(record.qurtoba_record_id, fulfilled)
    if err:
        logger.error('[CashSys Reroute] accountant edit FAILED record=%d qid=%s: %s',
                     record.pk, record.qurtoba_record_id, err)
        _record_money_api_failure(
            record, 'cash_sys_order_canceled',
            f'Ledger edit to {fulfilled} did NOT reach Qurtoba '
            f'(qid={record.qurtoba_record_id}): {err}. The ledger still holds the '
            f'original {record.cash_sys_original_value}; the customer is over-charged '
            f'by the remainder until this is applied.',
            {'intent': f'set value to {fulfilled}', 'reroute_amount': reroute_amount,
             'qurtoba_record_id': record.qurtoba_record_id, 'error': err},
        )
        raise RuntimeError(f'accountant edit failed for qid={record.qurtoba_record_id}: {err}')

    # Settle THIS order at the sent amount and mark it done (state 'rerouted' is just
    # a done-order marker explaining why value < original — cash_sys_done=True is
    # what makes it complete). save() now pulls the corrected port-6000 Rest.
    record.value = fulfilled
    record.cash_sys_done = True
    record.cash_sys_state = 'rerouted'
    record.cash_sys_fulfilled = fulfilled
    record.cash_sys_reroute_amount = reroute_amount  # reference only — see docstring
    record.cash_sys_canceled_reason = 'number_limit'
    record.save()  # recompute_balance() pulls the already-corrected (post-edit) Rest

    logger.info('[CashSys Reroute] record=%d original=%s fulfilled=%s remainder=%s',
                record.pk, record.cash_sys_original_value, fulfilled, reroute_amount)

    # Send any not-yet-sent done receipts, then ask for a new number.
    record.refresh_from_db()
    images_sent = _send_done_receipts(record)
    _pause_after_receipts(images_sent)   # let the image land before the «تغيير رقم» ask
    # Office report 2026-09-05: a split order that ends as partial + reroute
    # settled at the amount sent but never recorded «مصاريف خدمه» for the part
    # that DID go out — the fee was only ever posted from order_done. Post it
    # here for the executed briefs (same machinery, same message); the
    # cash_sys_service_fee_done flag keeps a later order_done from charging twice.
    _create_service_fees(record)
    _send_reroute_ask(record, fulfilled, reroute_amount)


def _apply_zero_cancel(record, reason):
    """Full-reversal cancel (no_wallet / cancel_request): the order never moved
    money, so zero the customer's debt entirely and notify them.

    ORDER MATTERS (same as _apply_reroute): edit the accountant ledger (port 6000)
    to value 0 FIRST, then save the Genie record — QurtobaRecord.save() →
    recompute_balance() PULLS the corrected Rest from port 6000, so the customer's
    balance drops by the full value. A failed accountant edit RAISES so the webhook
    task retries; we never silently leave the customer charged after telling them
    «لم يتم تسجيل العمليه عليك»."""
    from qurtoba.utils_sync import edit_qurtoba_record_value

    # SPLIT ORDERS: an order may be fulfilled across several partial transfers.
    # Each one arrives as order_progress and records how much has really gone out
    # (cash_sys_fulfilled + cash_sys_transactions). If a cancel then lands on the
    # REMAINDER, zeroing the whole record would erase a transfer that genuinely
    # happened and hand the customer that money for free.
    #
    # Re-read first: order_progress writes with .update(), so the instance loaded
    # by the webhook can be stale by the time we get here. Two events arriving
    # back-to-back is exactly when this matters, and reading a stale
    # cash_sys_fulfilled=None is what would cause the wrongful zero.
    try:
        record.refresh_from_db()
    except Exception as exc:
        logger.warning('[CashSys ZeroCancel] refresh failed for record=%s: %s', record.pk, exc)

    fulfilled = float(record.cash_sys_fulfilled or 0)
    txns = record.cash_sys_transactions or []
    partial = fulfilled > 0 or bool(txns)

    # `fulfilled` can be absent even when transfers exist (a progress event that
    # omitted it, or briefs merged from a done event). Falling through with
    # fulfilled=0 while transactions are present would settle at ZERO and wipe
    # money that really went out, so derive the amount from the transfers instead.
    if partial and fulfilled <= 0 and txns:
        derived = 0.0
        for t in txns:
            try:
                derived += float((t or {}).get('value') or 0)
            except (TypeError, ValueError):
                derived = 0.0
                break
        fulfilled = derived

    # If money demonstrably moved but we cannot establish HOW MUCH, do not guess.
    # Zeroing would gift the customer the sent amount; picking a number would be
    # invention. Leave the ledger untouched and let a human settle it.
    if partial and fulfilled <= 0:
        msg = (
            f'Record {record.pk}: cancel (reason={reason}) landed on an order that has '
            f'transfers recorded ({txns}) but no usable fulfilled amount. Refusing to '
            f'touch the ledger — zeroing would erase money that already went out. '
            f'Settle this one by hand.'
        )
        logger.error('[CashSys ZeroCancel] %s', msg)
        _record_money_api_failure(
            record, 'cash_sys_order_canceled', msg,
            {'reason': reason, 'value': record.value,
             'fulfilled': record.cash_sys_fulfilled, 'transactions': txns},
        )
        raise RuntimeError(msg)

    # Settle at what ACTUALLY went out — 0 for a pristine order, `fulfilled` for a
    # partially-sent one. Settling beats refusing here: refusing would leave the
    # record at its FULL original value, i.e. the customer charged for the whole
    # transfer when only part of it was sent. That is the worse of the two errors.
    target = fulfilled if partial else 0.0

    # Capture the original value once, for reference / audit.
    if record.cash_sys_original_value is None:
        record.cash_sys_original_value = record.value

    if partial:
        # A partial send normally terminates as reroute=true. Arriving here as a
        # plain cancel is unusual, so settle correctly AND make it visible.
        logger.warning(
            '[CashSys ZeroCancel] record=%d reason=%s arrived as a PLAIN cancel after a '
            'partial send (fulfilled=%s of %s) — settling at the amount sent, not 0.',
            record.pk, reason, fulfilled, record.cash_sys_original_value,
        )
        _record_money_api_failure(
            record, 'cash_sys_order_canceled',
            f'Cancel (reason={reason}) landed on a PARTIALLY SENT order: '
            f'{fulfilled} of {record.cash_sys_original_value} had already gone out. '
            f'Settled the ledger at {fulfilled} instead of 0 so the sent money is still '
            f'owed. Expected this to arrive as reroute=true — worth checking Cash-SYS.',
            {'reason': reason, 'original_value': record.cash_sys_original_value,
             'fulfilled': fulfilled, 'settled_at': target,
             'transactions': record.cash_sys_transactions},
        )

    # NEVER skip the ledger edit. This used to be `if record.qurtoba_record_id:`,
    # so a record without one silently bypassed the accountant call and fell
    # straight through to `record.value = 0` + «و لم يتم تسجيل العمليه عليك» —
    # telling the customer they were not charged while the debt stayed on the
    # ledger. A missing id is a hard failure, not a reason to continue.
    _require_ledger_id(record, 'zero-cancel')
    err = edit_qurtoba_record_value(record.qurtoba_record_id, target)
    if err:
        logger.error('[CashSys ZeroCancel] accountant edit→%s failed record=%d qid=%s: %s',
                     target, record.pk, record.qurtoba_record_id, err)
        # Surface it immediately — this is a money-affecting API call that did not
        # land. Waiting for the retries to run out first leaves a window where the
        # ledger is wrong and nothing in the UI says so. The upsert is idempotent,
        # so the retries just bump `attempts` on the same row.
        _record_money_api_failure(
            record, 'cash_sys_order_canceled',
            f'Ledger edit to {target} did NOT reach Qurtoba (qid={record.qurtoba_record_id}): '
            f'{err}. The customer has NOT been notified and the debt is still on the ledger.',
            {'intent': f'set value to {target}', 'reason': reason,
             'qurtoba_record_id': record.qurtoba_record_id, 'error': err},
        )
        raise RuntimeError(f'accountant edit to {target} failed for qid={record.qurtoba_record_id}: {err}')

    # Settle THIS order at the amount that really went out and mark it canceled.
    # save() pulls the corrected Rest back from Qurtoba.
    record.value = target
    record.cash_sys_state = 'canceled'
    record.cash_sys_canceled_reason = reason
    if partial:
        # Part of it really was sent, so the order is finished, not undone.
        record.cash_sys_done = True
    record.save()

    logger.info('[CashSys ZeroCancel] record=%d reason=%s original=%s → value %s%s',
                record.pk, reason, record.cash_sys_original_value, target,
                ' (partial: settled at amount sent)' if partial else '')

    if partial:
        # Office report 2026-09-05: settling a partially-sent order at the amount
        # sent must also record «مصاريف خدمه» for the executed part (previously
        # only order_done did). Idempotent via cash_sys_service_fee_done.
        record.refresh_from_db()
        _create_service_fees(record)

    # Only a FULL reversal may tell the customer «لم يتم تسجيل العمليه عليك» — that
    # sentence is false when part of the money did go out.
    if not partial:
        _send_cancel_notice(record, reason)


_RETRY_COUNTDOWNS = [5, 15, 30]  # seconds before retry attempt 2, 3, 4


@shared_task(bind=True, max_retries=3)
def push_record_to_qurtoba_task(self, record_pk: int):
    from qurtoba.utils_sync import push_record_to_qurtoba, _mark_error

    # ANY exception here must become a RETURNED error string, never an escaping
    # raise. The whole failure pipeline below (retry → _mark_error →
    # QurtobaSyncProblem) hangs off `if error:`, so an exception that propagates
    # out of this task silently bypasses all three: no retry, qurtoba_sync_error
    # stays NULL, and nothing appears in the sync-problems UI. That is exactly how
    # records 22511 (500) and 22470 (28,000) were lost on 2026-08-06/07 — a
    # `FATAL: too many connections for role "qurtoba"` OperationalError was raised
    # by the record read inside push_record_to_qurtoba, ~0.35 s after post_create
    # enqueued this task, and died here without a trace. The money was ack'd to the
    # customer with 👍 but never reached the Qurtoba ledger or Cash-SYS.
    # Note this catches only the call itself, NOT self.retry()'s Retry exception.
    try:
        error = push_record_to_qurtoba(record_pk)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    if error:
        countdown = _RETRY_COUNTDOWNS[min(self.request.retries, len(_RETRY_COUNTDOWNS) - 1)]
        try:
            raise self.retry(exc=Exception(error), countdown=countdown)
        except self.MaxRetriesExceededError:
            # Best-effort: if the DB is the very thing that's failing, marking the
            # error can raise too — which would again lose the record silently. The
            # sweeper (reconcile_unsynced_qurtoba_records) is the backstop for that.
            try:
                _mark_error(record_pk, error)
            except Exception as exc:
                logger.error('Failed to mark sync error on record %s: %s', record_pk, exc)
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


def _sync_problem(rec, error: str) -> None:
    """Best-effort: surface a failed/stuck push as a UI-actionable problem row."""
    try:
        from qurtoba.models import QurtobaSyncProblem
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
        logger.error('Failed to record QurtobaSyncProblem for record %s: %s', rec.pk, exc)


@shared_task
def reconcile_unsynced_qurtoba_records():
    """
    Backstop sweeper: find Genie-born records that never reached Qurtoba and push
    them again.

    WHY THIS EXISTS (beyond the in-task retry): the retry chain only covers a
    failure the task itself lives to observe. It does NOT cover a task that dies
    outright, a `.delay()` that never enqueued, a worker killed mid-push, or a
    connection squeeze that outlasts all three retries (~50 s). Every one of those
    leaves a record ack'd to the customer with 👍 but absent from the ledger and
    from Cash-SYS — invisible, because nothing ever wrote an error. Six records
    were lost that way before this existed (500 and 28,000 EGP among them).

    IT DOES NOT POST. Owner policy, and it is the right one: a transfer that
    surfaces minutes or hours late is worse than one that never happened. By then
    the customer has usually been told it failed — record 22470 (28,000) is the
    proof: the chat already said «تم الغاء التحويل» and «لم يتم تسجيل العمليه عليك»,
    so auto-pushing it would have invented a 28,000 debt for a transfer the
    customer had been told did not happen. The in-task retry chain still covers the
    only window where re-posting is safe: the first ~50 seconds, while the customer
    is still waiting on the 👍.

    So this task's whole job is VISIBILITY: turn a silent phantom into a
    QurtobaSyncProblem row an admin can see and decide on (settle by hand, or
    purge it with `manage.py purge_phantom_qurtoba_records`).

    MIN_AGE keeps it from racing the push's own retries. Set
    QURTOBA_RECONCILE_AUTOPUSH=True only if you have a deliberate reason to let it
    move money again.
    """
    from django.utils import timezone
    from django.conf import settings as dj_settings
    from qurtoba.models import QurtobaRecord

    stats = {'checked': 0, 'surfaced': 0, 'pushed': 0, 'failed': 0}

    if not getattr(dj_settings, 'QURTOBA_RECONCILE_ENABLED', True):
        logger.info('[Reconcile] disabled via QURTOBA_RECONCILE_ENABLED')
        return stats

    min_age  = int(getattr(dj_settings, 'QURTOBA_RECONCILE_MIN_AGE_MINUTES', 5))
    batch    = int(getattr(dj_settings, 'QURTOBA_RECONCILE_BATCH', 25))
    autopush = bool(getattr(dj_settings, 'QURTOBA_RECONCILE_AUTOPUSH', False))
    now      = timezone.now()

    # Genie-born (customer_data_qurtoba_id is set only on records that came FROM
    # Qurtoba) and demonstrably never landed there.
    stuck = QurtobaRecord.objects.filter(
        qurtoba_synced=False,
        qurtoba_record_id__isnull=True,
        customer_data_qurtoba_id__isnull=True,
        created_at__lte=now - dt.timedelta(minutes=min_age),
    ).order_by('created_at')[:batch]

    for rec in stuck:
        stats['checked'] += 1

        if not autopush:
            # Default path: report, never move money. QurtobaSyncProblem.record()
            # is an idempotent upsert, so repeating every 5 min neither duplicates
            # rows nor re-notifies.
            stats['surfaced'] += 1
            _sync_problem(
                rec,
                'Created in Genie but never reached Qurtoba (no ledger row, no '
                'Cash-SYS order) — NOT auto-posted, because a late transfer can '
                'contradict what the customer was already told. Settle by hand if '
                'still owed, or purge it.',
            )
            continue

        # Opt-in only.
        from qurtoba.utils_sync import push_record_to_qurtoba, _mark_error
        try:
            error = push_record_to_qurtoba(rec.pk)
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        if error:
            stats['failed'] += 1
            logger.warning('[Reconcile] record=%s push failed: %s', rec.pk, error)
            try:
                _mark_error(rec.pk, error)
            except Exception as exc:
                logger.error('[Reconcile] mark_error failed for %s: %s', rec.pk, exc)
            _sync_problem(rec, error)
        else:
            stats['pushed'] += 1
            logger.info('[Reconcile] record=%s pushed to Qurtoba successfully', rec.pk)

    if stats['checked']:
        logger.info('[Reconcile] %s', stats)
    return stats


# ---------------------------------------------------------------------------
# End-of-day reminder — one short WhatsApp template per phone that asked us
# for a transaction during the day that just closed.
# ---------------------------------------------------------------------------

# Meta name of the approved template this task sends. Kept as a setting so the
# template can be renamed or swapped without a code change.
# v2, not the original: Meta refuses to edit an approved template (subcode
# 2388039 'you can only delete or add templates'), and the first version was
# approved with parameter names over Meta's 20-char send-time limit, so it can
# never actually send. Overridable via settings.
QURTOBA_DAILY_REMINDER_TEMPLATE = 'qurtoba_daily_summary_v2'


@shared_task(bind=True, max_retries=0)
def send_qurtoba_daily_reminder(self, report_date=None, dry_run=False):
    """
    Post the end-of-day summary to every number that requested a transaction
    through Genie on the business day that just ended.

    Audience is deliberately narrow — only chat-born records carry a `partner`,
    so a customer whose day was keyed into Qurtoba by an accountant is not
    messaged: nobody asked us for anything from a phone. See
    qurtoba.services.daily_totals.partners_active_on.

    Runs just after midnight Cairo, so `report_date` defaults to the day that
    has just CLOSED, not today. Pass an ISO date to re-run for a specific day,
    or dry_run=True to see who would receive it without sending.

    max_retries=0 on purpose: a retry would re-send template messages that
    already went out, and there is no per-recipient idempotency key here.
    """
    import datetime as _dt

    from modules.base.models import Partner
    from modules.whatsapp.models import WhatsAppTemplate
    from qurtoba.services.daily_totals import partners_active_on, reporting_day

    day = _dt.date.fromisoformat(report_date) if report_date else reporting_day()

    template_name = getattr(
        settings, 'QURTOBA_DAILY_REMINDER_TEMPLATE', QURTOBA_DAILY_REMINDER_TEMPLATE,
    )
    template = (
        WhatsAppTemplate.objects
        .filter(template_name=template_name, status='approved')
        .first()
    )
    if template is None:
        logger.warning(
            '[Qurtoba Daily] no APPROVED template named %r — nothing sent for %s. '
            'Create it and get Meta approval first.', template_name, day,
        )
        return {'sent': 0, 'reason': 'template_not_approved', 'report_date': str(day)}

    partner_ids = partners_active_on(day)
    if not partner_ids:
        logger.info('[Qurtoba Daily] no phone requested a transaction on %s — nothing to send', day)
        return {'sent': 0, 'reason': 'empty_audience', 'report_date': str(day)}

    # A partner with no Qurtoba link would render «—» for the account name and
    # a zero balance, which reads as broken. Skip rather than send that.
    sendable = list(
        Partner.objects
        .filter(id__in=partner_ids, qurtoba_customer__isnull=False)
        .values_list('id', flat=True)
    )
    skipped = len(partner_ids) - len(sendable)
    if skipped:
        logger.info('[Qurtoba Daily] skipped %d unlinked partner(s) for %s', skipped, day)

    if not sendable:
        return {'sent': 0, 'reason': 'no_linked_partners', 'report_date': str(day)}

    if dry_run:
        logger.info('[Qurtoba Daily] DRY RUN for %s — would send to %s', day, sendable)
        return {'sent': 0, 'reason': 'dry_run', 'report_date': str(day),
                'would_send_to': sendable, 'skipped_unlinked': skipped}

    sender_partner = _reminder_sender_partner(template)
    if sender_partner is None:
        logger.error('[Qurtoba Daily] no sender partner resolvable for account %s — nothing sent',
                     template.whatsapp_account_id)
        return {'sent': 0, 'reason': 'no_sender_partner', 'report_date': str(day)}

    from modules.whatsapp.tasks import process_bulk_whatsapp_template_sending
    process_bulk_whatsapp_template_sending.delay(
        template_id=template.id,
        contact_ids=sendable,
        sender_partner_id=sender_partner.id,
    )
    logger.info('[Qurtoba Daily] queued %d reminder(s) for %s', len(sendable), day)
    return {'sent': len(sendable), 'report_date': str(day), 'skipped_unlinked': skipped}


def _reminder_sender_partner(template):
    """The internal Partner the reminder is sent 'from'.

    The bulk sender needs one to attribute the outbound message to. There is no
    interactive user behind a beat job, so fall back through the account's own
    partner, then any staff partner.
    """
    from modules.base.models import Partner

    account = template.whatsapp_account
    for candidate in (
        getattr(account, 'partner', None),
        getattr(template, 'created_by', None) and getattr(template.created_by, 'partner', None),
    ):
        if candidate is not None:
            return candidate
    return Partner.objects.filter(user__isnull=False).order_by('pk').first()


# ───────────────── Stranded-conversation recovery (extension-owned) ─────────
#
# The channel task claims a conversation with `pending_task:<chat_key>` and
# parks later messages in `accumulated_messages:<chat_key>` for the running
# task to collect. If that task dies before collecting them — killed worker,
# OOM, revoke — the marker outlives it (300 s) and every later message parks
# behind it too, until the accumulator TTL discards the batch. Nothing raises;
# the customer is simply ignored, transactions included (incident 2026-08-07).
#
# Core writes its markers with no timestamp, so age is read off the key's
# remaining TTL (core always sets 300 s). A run that died MID-task had already
# consumed its batch from the cache, so the parked ids are re-derived from the
# chat: inbound messages newer than the last outbound. Recovery is bounded to
# recent messages — a stale batch must never be answered late.

_LOCK_TTL_SECONDS = 300
_LOCK_SENTINELS = ('processing', 're-triggered', 'retrying')
_RECOVER_MAX_AGE_MINUTES = 30
_RECOVER_MIN_QUIET_SECONDS = 15


def _unanswered_inbound_ids(conversation_id) -> list:
    """Inbound message ids (newest 25) that nothing has answered yet."""
    from datetime import timedelta
    from django.utils import timezone as _tz
    from modules.chat.models import Conversation, Message

    conv = Conversation.objects.filter(id=conversation_id).only('id', 'handled_by_ai').first()
    if conv is None or not conv.handled_by_ai:
        return []
    now = _tz.now()
    last_out = (
        Message.objects_all.filter(conversation_id=conversation_id, direction='outbound', active=True)
        .order_by('-created_at').values_list('created_at', flat=True).first()
    )
    qs = Message.objects_all.filter(
        conversation_id=conversation_id, direction='inbound', active=True,
        created_at__gte=now - timedelta(minutes=_RECOVER_MAX_AGE_MINUTES),
    )
    if last_out is not None:
        qs = qs.filter(created_at__gt=last_out)
    rows = list(qs.order_by('created_at').values_list('id', 'created_at')[:25])
    if not rows:
        return []
    if (now - rows[-1][1]).total_seconds() < _RECOVER_MIN_QUIET_SECONDS:
        return []   # still typing — normal batching will collect it
    return [str(r[0]) for r in rows]


@shared_task
def recover_stranded_conversations():
    """Re-trigger conversations whose messages sit behind a dead processing lock."""
    from django.conf import settings as dj_settings
    from django.core.cache import cache
    from modules.aistudio_whatsapp.tasks import process_workflow_messages

    if not hasattr(cache, 'iter_keys'):
        return {'skipped': 'cache backend has no iter_keys'}

    stale_after = int(getattr(dj_settings, 'AI_LOCK_STALE_SECONDS', 180))
    stats = {'checked': 0, 'recovered': 0, 'cleared': 0}
    try:
        keys = list(cache.iter_keys('pending_task:conversation_*'))
    except Exception:
        logger.exception('[StrandedRecovery] key scan failed')
        return stats

    for key in keys:
        stats['checked'] += 1
        chat_key = key[len('pending_task:'):]
        conversation_id = chat_key[len('conversation_'):]
        acc_key = f'accumulated_messages:{chat_key}'
        try:
            lock = cache.get(key)
            ttl = cache.ttl(key)
            waiting = cache.get(acc_key, []) or []
        except Exception:
            continue
        if lock is None:
            continue
        age = (_LOCK_TTL_SECONDS - ttl) if isinstance(ttl, int) and ttl > 0 else None
        if age is None or age < stale_after:
            continue   # fresh, or unknowable — leave it to the running task
        if lock not in _LOCK_SENTINELS and not waiting:
            continue   # a scheduled task id with nothing parked: harmless
        if not waiting:
            waiting = _unanswered_inbound_ids(conversation_id)
            if not waiting:
                cache.delete(key)
                stats['cleared'] += 1
                continue
            cache.set(acc_key, waiting, timeout=_LOCK_TTL_SECONDS)
        logger.warning(
            '[StrandedRecovery] %s: %d message(s) behind stale lock %r (age %ss). Re-triggering.',
            chat_key, len(waiting), lock, age,
        )
        cache.delete(key)
        process_workflow_messages.apply_async(args=[chat_key, conversation_id], countdown=1)
        stats['recovered'] += 1

    # ── Abdicated transaction messages ───────────────────────────────────
    # The model sometimes answers a transaction message with nothing usable —
    # observed shape (sandbox 2026-09-03, scenario E2, ~1 run in 3): a message that
    # repeats a transfer created minutes earlier gets a bare 👍 and NO tool call,
    # the gate drops the 👍, and the customer's request simply disappears. Every
    # correct outcome leaves a trace after the inbound row: a tool_call row (the
    # create/planner ran), an outbound row (a question, a template, the tool's
    # 👍), or the watermark. A self-contained transaction message (phone+amount)
    # that is a minute old with none of those is re-run ONCE; the create tool's
    # own duplicate/repeat gates make a second run safe.
    try:
        stats['abdicated'] = 0
        for msg in _abdicated_transaction_messages():
            conversation_id = str(msg.conversation_id)
            chat_key = f'conversation_{conversation_id}'
            marker = f'qurtoba:abdication_retry:{msg.id}'
            if not cache.add(marker, 1, timeout=3600):
                continue   # already retried once
            if cache.get(f'pending_task:{chat_key}'):
                continue   # a run is scheduled or in flight — leave it
            acc_key = f'accumulated_messages:{chat_key}'
            waiting = list(cache.get(acc_key, []) or [])
            if str(msg.id) not in waiting:
                waiting.append(str(msg.id))
            cache.set(acc_key, waiting, timeout=_LOCK_TTL_SECONDS)
            logger.warning(
                '[StrandedRecovery] %s: transaction message %s got no tool call and no reply — re-running once.',
                chat_key, str(msg.id)[:8],
            )
            process_workflow_messages.apply_async(args=[chat_key, conversation_id], countdown=1)
            stats['abdicated'] += 1
    except Exception:
        logger.exception('[StrandedRecovery] abdication scan failed')

    if stats['recovered'] or stats['cleared'] or stats.get('abdicated'):
        logger.info('[StrandedRecovery] %s', stats)
    return stats


def _abdicated_transaction_messages(min_age_s: int = 60, max_age_min: int = 6):
    """Unconsumed self-contained transaction messages with no trace of handling after them."""
    from datetime import timedelta
    from django.utils import timezone
    from modules.chat.models import Message
    from qurtoba.tools.planning import _classify_message

    now = timezone.now()
    rows = (
        Message.objects_all
        .filter(direction='inbound', type='text', active=True, ai_consumed_at__isnull=True,
                created_at__lte=now - timedelta(seconds=min_age_s),
                created_at__gte=now - timedelta(minutes=max_age_min),
                conversation__handled_by_ai=True, conversation__type='whatsapp',
                conversation__social_partner__qurtoba_customer__isnull=False)
        .order_by('created_at')
    )
    out = []
    for m in rows:
        txt = m.content.get('text') if isinstance(m.content, dict) else ''
        if not txt:
            continue
        cls = _classify_message(' '.join(str(txt).split()))
        if len(cls['phones']) != 1 or len(cls['amounts']) != 1:
            continue
        handled = Message.objects_all.filter(
            conversation_id=m.conversation_id, created_at__gt=m.created_at,
        ).exclude(direction='inbound').exists()
        if handled:
            continue
        out.append(m)
    return out
