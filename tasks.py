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


def _send_wa_web_media(ctx, attachment, url, message_type):
    """A receipt image into a WhatsApp Web chat (a customer group). The gateway takes the FILE itself
    (inline, with its real mimetype) — the Cloud API's {'url'} content shape is refused there."""
    from modules.wa_web.services.send_service import WaWebService
    from qurtoba.ai_guard import system_send
    conv = ctx['conv']
    with system_send():
        return WaWebService(conv.social_account).send_omnichannel(
            conv.social_partner,
            {'url': url, 'filename': getattr(attachment, 'name', None), 'attachment': {'url': url}},
            message_type=message_type,
            filename=getattr(attachment, 'name', None),
            reply_to_social_id=ctx.get('reply_wamid'),
            conversation=conv,
            system_partner=ctx['system_partner'],
            websocket=True,
            attachment=attachment,
        )


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
    # The chat the transfer was ASKED in comes first: a transfer placed in a customer's WhatsApp group
    # is answered in that group (its partner is the group's placeholder, which is no participant of any
    # chat, so the lookup below finds nothing for it — owner decision 2026-09-23).
    conv = None
    try:
        origin_conv = record.origin_message.conversation if record.origin_message_id else None
        if origin_conv is not None and origin_conv.type in ('whatsapp', 'wa_web', 'messenger', 'instagram', 'tiktok'):
            conv = origin_conv
    except Exception:
        logger.warning('[CashSys Notify] origin conversation of record %d not readable', record.pk, exc_info=True)
    conv = conv or (
        partner.conversations
        .filter(type__in=['whatsapp', 'wa_web', 'messenger', 'instagram', 'tiktok'])
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
                if url and getattr(ctx['conv'], 'type', None) == 'wa_web':
                    result = _send_wa_web_media(ctx, attachment, url, 'image')
                elif url:
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


def _set_reroute_marker(conversation, amount, kind, record):
    """Remember that the customer owes us a NEW NUMBER for `amount`.

    The workflow-v2 automation (qurtoba.automation.transfers) reads this: while the
    notice is the last thing that happened, the customer's next BARE phone number is
    the reroute answer and is created with this amount — exactly the rule the prompt
    gave the model («the amount is ALREADY KNOWN — never ask المبلغ كام»).
    """
    try:
        if conversation is None or not amount or float(amount) <= 0:
            return
        import time as _time
        from django.core.cache import cache
        cache.set(f'qurtoba:reroute_owed:{conversation.id}',
                  {'amount': float(amount), 'record_id': getattr(record, 'pk', None),
                   'kind': kind, 'ts': _time.time()},
                  24 * 3600)
    except Exception:
        logger.warning('[CashSys Notify] reroute marker not set for record=%s', getattr(record, 'pk', None),
                       exc_info=True)


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
        _set_reroute_marker(ctx['conv'], reroute_amount, 'reroute', record)
    except Exception as exc:
        logger.exception('[CashSys Notify] reroute ask failed record=%d: %s', record.pk, exc)


# Customer-facing notice per full-reversal cancel reason. The debt is already
# zeroed on the accountant ledger before this is sent, so the wording is truthful:
#   cancel_request → reassure nothing was recorded.
#   no_wallet      → ask for a different number (the current one has no wallet).
#   agent          → an operator cancelled it inside the Cash app: same truth, neutral wording.
#   anything else  → the neutral line too. On 2026-09-18 (chat 13f58d64) three `agent` cancels —
#                    22,610 / 7,000 / 22,240 — zeroed the ledger and told the customer nothing.
_CANCEL_NOTICE_MESSAGES = {
    'cancel_request': "تم الغاء التحويل\n\nو لم يتم تسجيل العمليه عليك",
    'no_wallet': "*محتاجين رقم تانى نبعت عليه الرصيد*\n\n*الرقم مش عليه محفظة*",
    'agent': "تم إلغاء التحويل من إدارة قرطبة\n\nو لم يتم تسجيل العمليه عليك",
}
_CANCEL_NOTICE_FALLBACK = _CANCEL_NOTICE_MESSAGES['agent']


def _send_cancel_notice(record, reason):
    """Send the WhatsApp notice for a full-reversal cancel, quoting the original
    transfer-request message. Best-effort; never raises. A reason without its own
    wording gets the neutral line — a cancel is never silent."""
    text = _CANCEL_NOTICE_MESSAGES.get(reason) or _CANCEL_NOTICE_FALLBACK
    if reason not in _CANCEL_NOTICE_MESSAGES:
        logger.warning('[CashSys Notify] cancel reason %r has no wording — neutral line sent (record=%d)',
                       reason, record.pk)
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
        if reason == 'no_wallet':
            # the whole transfer was reversed → the owed amount is the FULL original amount
            _set_reroute_marker(ctx['conv'], getattr(record, 'value', None), 'no_wallet', record)
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
SERVICE_FEE_MESSAGE = (
    "تم اضافه {x} جنيه مصاريف خدمه\n"
    "ل رقم {number}\n"
    "( الرقم عليه محفظه اخرى غير فودافون كاش )"
)


def _service_fee_text(fee, number) -> str:
    """The customer-facing fee note. Names the recipient number so the customer can tell
    which transfer the fee belongs to; the number line is dropped only when unknown."""
    if not number:
        return SERVICE_FEE_MESSAGE.replace("ل رقم {number}\n", "").format(x=fee)
    return SERVICE_FEE_MESSAGE.format(x=fee, number=number)


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
                    content={'text': _service_fee_text(fee, recipient)},
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


def _done_after_cancel(record, data: dict, fulfilled, done_at, last: dict) -> None:
    """Cash-SYS reported money moved on an order this system had already zeroed as cancelled.

    Ledger: set the value to `fulfilled` (a failed accountant edit RAISES so the webhook retries —
    the customer must never stay at 0 while money left). Record: done at `fulfilled`, state stays
    `canceled` so nothing downstream treats it as a clean transfer. Office: one internal note that
    mentions the staff plus a sync-problem row; customer: nothing (they were told it was cancelled)."""
    from qurtoba.models import QurtobaRecord, QurtobaSyncProblem
    from qurtoba.utils_sync import edit_qurtoba_record_value
    try:
        amount = float(fulfilled or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount > 0 and record.qurtoba_record_id:
        err = edit_qurtoba_record_value(record.qurtoba_record_id, amount)
        if err:
            logger.error('[CashSys DoneAfterCancel] accountant edit FAILED record=%d qid=%s: %s',
                         record.pk, record.qurtoba_record_id, err)
            raise RuntimeError(f'accountant edit failed for qid={record.qurtoba_record_id}: {err}')
    QurtobaRecord.objects.filter(pk=record.pk).update(
        value=amount if amount > 0 else record.value,
        cash_sys_done=amount > 0,
        cash_sys_done_at=done_at,
        cash_sys_fulfilled=fulfilled,
        cash_sys_fee=last.get('fee'),
        cash_sys_sim=last.get('sim_number'),
        cash_sys_sim_code=last.get('sim_code'),
        cash_sys_device=last.get('device_name'),
        cash_sys_operator=last.get('operator'),
    )
    record.refresh_from_db()
    try:
        record.save()          # recompute_balance() pulls the corrected Rest from the accountant ledger
    except Exception:
        logger.warning('[CashSys DoneAfterCancel] balance recompute failed record=%d', record.pk, exc_info=True)
    msg = (f'Cash-SYS sent «done» ({amount:g}) for order {data.get("order_id")} AFTER «canceled» '
           f'({record.cash_sys_canceled_reason}). Ledger settled at {amount:g}; the customer was told it was '
           f'cancelled and got no receipt — review by hand.')
    try:
        QurtobaSyncProblem.record(record, 'cash_sys_order_done', msg,
                                  payload={'order_id': data.get('order_id'), 'fulfilled': fulfilled,
                                           'canceled_reason': record.cash_sys_canceled_reason,
                                           'account_number': record.account_number, 'customer_id': record.customer_id})
    except Exception:
        logger.warning('[CashSys DoneAfterCancel] sync problem row failed record=%d', record.pk, exc_info=True)
    try:
        from qurtoba.staff_notes import post_staff_note
        ctx = _notify_context(record)
        if ctx:
            customer = getattr(record, 'customer', None)
            post_staff_note(
                ctx['conv'],
                ['⚠️ Cash-SYS بعت «تم» بعد «إلغاء» على نفس الطلب',
                 f'العميل: {getattr(customer, "name", "") or ""}',
                 f'الرقم: {record.account_number} — اتنفذ فعلياً {amount:g} (سبب الإلغاء: {record.cash_sys_canceled_reason})',
                 'العميل اتبلغ إن التحويل اتلغى ومبعتلوش إيصال — محتاج مراجعة يدوية.'],
                subject='⚠️ تم بعد إلغاء — Cash-SYS',
                body=f'{getattr(customer, "name", "") or ""}: {amount:g} على {record.account_number} اتنفذ بعد الإلغاء — مراجعة يدوية.',
                reply_to=None, dedupe_key=f'done_after_cancel:{record.pk}',
            )
    except Exception:
        logger.warning('[CashSys DoneAfterCancel] staff note failed record=%d', record.pk, exc_info=True)
    logger.warning('[CashSys DoneAfterCancel] record=%d order_id=%s fulfilled=%s', record.pk, data.get('order_id'), fulfilled)


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
        if record.cash_sys_state == 'canceled':
            # «done» after «canceled» on the same order (record 41228, 2026-09-19 21:56: order 10096 sent
            # canceled/no_wallet, then done for 1 EGP). The customer already read «مش عليه محفظة»; a
            # receipt now contradicts it, and the ledger says 0 while money moved. Settle at what really
            # went out, send nothing to the customer, and hand it to the office.
            _done_after_cancel(record, data, fulfilled, done_at, last)
            _commit_event(record, commit_key)
            return
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
    if not error:
        return
    # Celery 5 raises the given `exc` itself — not MaxRetriesExceededError — once the retries are
    # used up, so an `except MaxRetriesExceededError` around self.retry() never runs (record 39709,
    # 2026-09-14: four rejections, no error marked, no problem row, the customer's 👍 the last word).
    # Decide BEFORE retrying: the last allowed attempt has already happened → settle it here.
    if self.request.retries >= self.max_retries:
        _push_exhausted(record_pk, error)
        return
    countdown = _RETRY_COUNTDOWNS[min(self.request.retries, len(_RETRY_COUNTDOWNS) - 1)]
    raise self.retry(exc=Exception(error), countdown=countdown)


def _push_exhausted(record_pk: int, error: str) -> None:
    """Every push attempt failed: mark it, surface it, tell the customer and the office.

    Best-effort, step by step: if the DB is the very thing failing, each step is on its own so one
    failure never hides the next. The sweeper (reconcile_unsynced_qurtoba_records) stays the backstop."""
    from qurtoba.utils_sync import _mark_error
    try:
        _mark_error(record_pk, error)
    except Exception as exc:
        logger.error('Failed to mark sync error on record %s: %s', record_pk, exc)
    rec = None
    try:
        from qurtoba.models import QurtobaRecord
        rec = QurtobaRecord.objects.filter(pk=record_pk).first()
    except Exception as exc:
        logger.error('Failed to load record %s after push failure: %s', record_pk, exc)
    if rec is None:
        return
    _sync_problem(rec, error)
    # Locally the record never reached the ledger or Cash-SYS: a re-send by the customer is a fresh
    # order, not a repeat, and no notice/receipt logic must treat it as live.
    try:
        from qurtoba.models import QurtobaRecord
        QurtobaRecord.objects.filter(pk=rec.pk).update(cash_sys_state='canceled', cash_sys_canceled_reason='push_failed')
    except Exception as exc:
        logger.error('Failed to mark record %s push_failed: %s', record_pk, exc)
    ctx = None
    try:
        ctx = _notify_context(rec)
    except Exception:
        logger.warning('push_exhausted: notify context failed record=%d', rec.pk, exc_info=True)
    if ctx:
        try:
            from qurtoba.automation.replies import PUSH_FAILED
            ctx['svc'].send_and_broadcast(
                partner=ctx['conv'].social_partner, content={'text': PUSH_FAILED}, message_type='text',
                conversation=ctx['conv'], system_partner=ctx['system_partner'],
                reply_to_message_id=ctx['reply_wamid'], reply_to_id=ctx['reply_local_id'], websocket=True,
            )
        except Exception:
            logger.warning('push_exhausted: customer line failed record=%d', rec.pk, exc_info=True)
        try:
            from qurtoba.staff_notes import post_staff_note
            customer = getattr(rec, 'customer', None)
            post_staff_note(
                ctx['conv'],
                ['⚠️ تحويل ما اتسجلش في قرطبة بعد كل المحاولات',
                 f'العميل: {getattr(customer, "name", "") or ""}',
                 f'{rec.type} {rec.value:g} → {rec.account_number} (سجل {rec.pk})',
                 f'السبب: {str(error)[:200]}',
                 'العميل اتبلغ يبعته تاني لو لسه محتاجه. السجل ظاهر في مشاكل المزامنة.'],
                subject='⚠️ تحويل ما اتسجلش في قرطبة',
                body=f'{getattr(customer, "name", "") or ""}: {rec.value:g} على {rec.account_number} — فشل التسجيل بعد كل المحاولات.',
                dedupe_key=f'push_exhausted:{rec.pk}',
            )
        except Exception:
            logger.warning('push_exhausted: staff note failed record=%d', rec.pk, exc_info=True)
    logger.error('[Qurtoba Push] record %s failed after every retry: %s', record_pk, error)


@shared_task(bind=True, max_retries=0)
def retry_message_status(self, social_id: str, status: str, attempt: int = 1):
    """A WhatsApp «sent»/«delivered» callback that found no chat row yet (the send was still being
    written — the same-second race behind 19 rows stuck at «saved» on 14–19 Sep) is applied here a few
    seconds later, up to three times. Scheduled by qurtoba.runtime_patches on the inline handler."""
    from modules.chat.models import Message
    from modules.chat.services.chat_bridge_service import ChatBridgeService
    if not Message.objects.filter(social_id=social_id).exists():
        if attempt < 3:
            retry_message_status.apply_async(args=[social_id, status, attempt + 1], countdown=10 * attempt)
        else:
            logger.warning('[StatusRetry] no chat row for %s after %d tries (status=%s)', str(social_id)[:40], attempt, status)
        return
    bridge = ChatBridgeService()
    if status == 'sent':
        bridge.mark_as_sent(social_id)
    elif status == 'delivered':
        bridge.mark_delivered_cumulative(social_id)
    logger.info('[StatusRetry] applied %s to %s on attempt %d', status, str(social_id)[:40], attempt)


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
# The same reminder with the day's full statement (Excel) in the DOCUMENT header — used as
# soon as Meta approves it; the text template above stays untouched as the fallback.
# The customer's Excel statement ALONE, sent as its own message right after the summary text: a DOCUMENT
# header and the body «كشف حساب يوم {{qurtoba_date}} 📎». Owner decision 2026-09-14: never text and file in one
# message, so the combined template 'qurtoba_daily_statement_xlsx' is retired and never used. The first
# file-only template 'qurtoba_daily_statement_file' (#20) was withdrawn from Meta the same day for this body.
QURTOBA_DAILY_FILE_TEMPLATE = 'qurtoba_daily_excel'
QURTOBA_DAILY_PAIR_SPACING_S = 8     # seconds between two recipients' pairs of messages
QURTOBA_DAILY_FILE_DELAY_S = 4       # the file follows its summary text by this much, so it arrives second


@shared_task(bind=True, max_retries=0)
def send_qurtoba_daily_reminder(self, report_date=None, dry_run=False):
    """End-of-day statements, 00:10 Cairo, for the day that has just closed.

    Owner decision 2026-09-26: all service happens in the customer GROUPS. Every linked group whose customer
    side wrote to us that day gets the summary text and then its Excel (_dispatch_group_statements). The
    private numbers get the old template pair only while the office number's «الشات الخاص مقفول» switch is OFF.
    """
    import datetime as _dt
    from qurtoba.services.daily_totals import reporting_day

    day = _dt.date.fromisoformat(report_date) if report_date else reporting_day()
    groups = _dispatch_group_statements(day, dry_run=dry_run)
    if _private_statements_closed():
        logger.info('[Qurtoba Daily] private chats are closed — groups only for %s: %s', day, groups)
        return {'report_date': str(day), 'private': 'closed', 'groups': groups}
    private = _send_private_statements(day.isoformat(), dry_run)
    return {**private, 'groups': groups}


def _private_statements_closed() -> bool:
    try:
        from modules.whatsapp.models import WhatsAppAccount
        acc = WhatsAppAccount._base_manager.filter(pk=3).first()
        return bool(getattr(acc, 'qurtoba_private_closed', True)) if acc is not None else True
    except Exception:
        logger.warning('[Qurtoba Daily] private-closed check failed — treating private chats as closed', exc_info=True)
        return True


QURTOBA_GROUP_STATEMENT_SPACING_S = 8      # seconds between two groups' statements


def _dispatch_group_statements(day, dry_run=False):
    """One per-group task for every linked group whose customer side wrote on `day`, spaced out."""
    from qurtoba.services.daily_totals import groups_chatted_on
    conv_ids = groups_chatted_on(day)
    if dry_run or not conv_ids:
        return {'groups': len(conv_ids), 'dry_run': bool(dry_run), 'would_send_to': conv_ids if dry_run else []}
    for slot, cid in enumerate(conv_ids):
        send_qurtoba_group_statement.apply_async(args=[cid, day.isoformat()],
                                                 countdown=slot * QURTOBA_GROUP_STATEMENT_SPACING_S)
    return {'groups': len(conv_ids)}


def group_statement_text(conversation, day) -> str:
    """The nightly summary for a GROUP — the same wording as the private template (#2: header, body,
    footer), the per-number line reading the group's transfers (records of a group sit on its partner)."""
    from qurtoba.services.daily_totals import fmt_amount, fmt_day_ar, partner_day_totals
    gp = conversation.social_partner
    customer = gp.qurtoba_customer
    customer.refresh_from_db(fields=['balance'])
    balance = customer.balance or 0
    balance_txt = (f'عليك {fmt_amount(abs(balance))} جنيه' if balance > 0
                   else f'ليك {fmt_amount(abs(balance))} جنيه' if balance < 0 else 'مفيش مديونية')
    total = fmt_amount(partner_day_totals(gp, day)['debit'])
    name = (getattr(customer, 'name', '') or '').strip() or '—'
    group_name = (conversation.name or '').strip() or '—'
    return ('*كشف نهاية اليوم*\n\n'
            f'*العميل :* {name}\n\n'
            f'ملخص عمليات يوم : {fmt_day_ar(day)}\n'
            '━━━━━━━━━━━━━\n'
            '💸 إجمالي تحويلات الجروب :\n'
            f'{group_name} : ( *{total}* )\n\n'
            '━━━━━━━━━━━━━\n'
            '🏦 إجمالي الحساب الان :\n'
            f'      ( {balance_txt} )\n\n'
            '_مكتب قرطبة — كشف تلقائي فى نهاية اليوم_')


@shared_task(bind=True, max_retries=0, name='qurtoba.tasks.send_qurtoba_group_statement')
def send_qurtoba_group_statement(self, conversation_id, day_iso):
    """The two nightly messages into ONE customer group: the summary text, then 4 s later the Excel alone.
    max_retries=0 on purpose — a retry would send the statement twice."""
    import datetime as _dt
    import time as _time
    from modules.chat.models import Conversation
    from modules.chat.services.omnichannel_send_service import OmnichannelSendService
    from qurtoba.ai_guard import system_send
    from qurtoba.extensions import _get_system_partner
    from qurtoba.services.daily_totals import fmt_day_ar
    from qurtoba.tools.reports import _send_statement_document

    day = _dt.date.fromisoformat(day_iso)
    conv = Conversation._base_manager.select_related('social_partner').filter(pk=conversation_id).first()
    customer = getattr(getattr(conv, 'social_partner', None), 'qurtoba_customer', None)
    if conv is None or customer is None:
        return {'sent': False, 'reason': 'not_a_linked_group'}
    out = {'conversation': str(conv.pk), 'customer': customer.pk}
    try:
        with system_send():
            res = OmnichannelSendService().send_and_broadcast(
                partner=conv.social_partner, content={'text': group_statement_text(conv, day)},
                message_type='text', conversation=conv, system_partner=_get_system_partner(conv), websocket=True)
        out['text'] = bool(isinstance(res, dict) and res.get('success'))
    except Exception as exc:  # noqa: BLE001 — the file still goes
        logger.exception('[Qurtoba Daily] group %s: summary text failed', conversation_id)
        out['text'] = False
        out['text_error'] = str(exc)[:200]
    _time.sleep(QURTOBA_DAILY_FILE_DELAY_S)
    try:
        xlsx, _url, _name = build_customer_day_statement(customer, day)
        ok, err = _send_statement_document(conv, customer.pk, xlsx, day.isoformat(),
                                           f'كشف حساب يوم {fmt_day_ar(day)} 📎')
        out['file'] = ok
        if err:
            out['file_error'] = err
    except Exception as exc:  # noqa: BLE001
        logger.exception('[Qurtoba Daily] group %s: statement file failed', conversation_id)
        out['file'] = False
        out['file_error'] = str(exc)[:200]
    logger.info('[Qurtoba Daily] group statement %s', out)
    return out


def _send_private_statements(report_date=None, dry_run=False):
    """
    End-of-day messages, TWO per recipient: the summary text (QURTOBA_DAILY_REMINDER_TEMPLATE, unchanged) and
    then, as a separate message, the customer's Excel statement alone (QURTOBA_DAILY_FILE_TEMPLATE).

    Audience, owner decision 2026-09-14: only the numbers that wrote to us on the business day being reported
    (qurtoba.services.daily_totals.partners_chatted_on). A customer whose day was keyed into Qurtoba by the
    office, or who did not message us that day, gets nothing.

    Each template is used only once Meta has APPROVED it: until the file template is approved the summary text
    still goes out on its own. The retired combined template is never used.

    Runs just after midnight Cairo, so `report_date` defaults to the day that has just CLOSED. Pass an ISO date
    to re-run a specific day, or dry_run=True to see who would receive what without sending.

    max_retries=0 on purpose: a retry would re-send template messages that already went out.
    """
    import datetime as _dt

    from modules.base.models import Partner
    from modules.whatsapp.models import WhatsAppTemplate
    from qurtoba.services.daily_totals import partners_chatted_on, reporting_day

    day = _dt.date.fromisoformat(report_date) if report_date else reporting_day()
    text_name = getattr(settings, 'QURTOBA_DAILY_REMINDER_TEMPLATE', QURTOBA_DAILY_REMINDER_TEMPLATE)
    file_name = getattr(settings, 'QURTOBA_DAILY_FILE_TEMPLATE', QURTOBA_DAILY_FILE_TEMPLATE)
    text_template = WhatsAppTemplate.objects.filter(template_name=text_name, status='approved').first()
    file_template = (WhatsAppTemplate.objects
                     .filter(template_name=file_name, status='approved', header_format='DOCUMENT').first())
    names = {'text_template': getattr(text_template, 'template_name', None),
             'file_template': getattr(file_template, 'template_name', None)}
    if text_template is None and file_template is None:
        logger.warning('[Qurtoba Daily] neither %r nor %r is APPROVED — nothing sent for %s', text_name, file_name, day)
        return {'sent': 0, 'reason': 'template_not_approved', 'report_date': str(day)}
    if text_template is None:
        logger.warning('[Qurtoba Daily] %r is not approved — only the statement file goes out for %s', text_name, day)
    if file_template is None:
        logger.info('[Qurtoba Daily] %r is not approved yet — only the summary text goes out for %s', file_name, day)

    partner_ids = partners_chatted_on(day)
    if not partner_ids:
        logger.info('[Qurtoba Daily] nobody wrote to us on %s — nothing to send', day)
        return {'sent': 0, 'reason': 'empty_audience', 'report_date': str(day), **names}

    # A partner with no Qurtoba link would render «—» for the account name and a zero balance.
    sendable = list(Partner.objects.filter(id__in=partner_ids, qurtoba_customer__isnull=False)
                    .values_list('id', flat=True))
    skipped = len(partner_ids) - len(sendable)
    if not sendable:
        return {'sent': 0, 'reason': 'no_linked_partners', 'report_date': str(day), **names}

    if dry_run:
        logger.info('[Qurtoba Daily] DRY RUN for %s — %s to %s', day, names, sendable)
        return {'sent': 0, 'reason': 'dry_run', 'report_date': str(day), **names,
                'would_send_to': sendable, 'skipped_unlinked': skipped}

    sender_partner = _reminder_sender_partner(text_template or file_template)
    if sender_partner is None:
        logger.error('[Qurtoba Daily] no sender partner resolvable — nothing sent for %s', day)
        return {'sent': 0, 'reason': 'no_sender_partner', 'report_date': str(day), **names}

    result = _dispatch_daily_messages(text_template, file_template, sendable, sender_partner, day)
    logger.info('[Qurtoba Daily] %s for %s: %d summary text(s) and %d statement file(s) queued, %d failed',
                names, day, result['texts'], result['files'], len(result['failed']))
    return {'sent': len(sendable), 'report_date': str(day), 'skipped_unlinked': skipped, **names, **result}


def build_customer_day_statement(customer, day):
    """ONE Excel per customer per day — every number on the account shares the same file
    (no «رقمك» marker), so the office can read it as one document. Returns
    (xlsx bytes, public URL, display name)."""
    from django.utils import timezone
    from qurtoba.tools.reports import _build_statement_xlsx, collect_customer_day, store_statement_xlsx

    data = collect_customer_day(customer, None, day)
    customer.refresh_from_db(fields=['balance'])
    xlsx = _build_statement_xlsx(
        customer_name=customer.name, report_date_iso=day.isoformat(), groups=data['groups'],
        total_debit=data['total_debit'], total_credit=data['total_credit'],
        current_balance=customer.balance or 0,
        generated_at=timezone.localtime().strftime('%Y-%m-%d %H:%M'),
    )
    url, display_name = store_statement_xlsx(customer.pk, xlsx, day.isoformat())
    return xlsx, url, display_name


def _dispatch_daily_messages(text_template, file_template, partner_ids, sender_partner, day):
    """Per recipient: the summary text first, then, as its own message a few seconds later, the Excel
    statement alone. Either template may be None (not approved yet) and is then skipped.

    Every send is scheduled on the core per-message task, which sends it, records the chat message and retries
    a failed send. One file is built per customer per day and shared by that customer's numbers. Recipients are
    spaced so each pair arrives in order and pairs never interleave.
    """
    from modules.base.models import Partner
    from modules.whatsapp.tasks import _build_template_params, process_sending_whatsapp_template
    from modules.whatsapp.utils.phone import resolve_delivery_partner

    texts, files, failed = 0, 0, []
    statement_files = {}                         # customer id → (url, display name)
    for slot, pid in enumerate(partner_ids):
        base = slot * QURTOBA_DAILY_PAIR_SPACING_S
        try:
            contact = Partner.objects.select_related('qurtoba_customer').get(id=pid)
        except Exception as exc:  # noqa: BLE001 — one bad recipient must not stop the rest
            failed.append({'partner_id': pid, 'part': 'contact', 'error': str(exc)[:200]})
            continue
        # The file goes out AFTER the text has been sent (a callback on the text task), not on its own
        # countdown: on 2026-09-19 00:10 the Excel arrived before the summary because the text send
        # was retried (Meta 500) while the file's timer kept running.
        file_sig = None
        if file_template is not None:
            try:
                customer = contact.qurtoba_customer
                if customer.pk not in statement_files:
                    _xlsx, url, display_name = build_customer_day_statement(customer, day)
                    if not url:
                        raise RuntimeError('no public media URL for the statement file')
                    statement_files[customer.pk] = (url, display_name)
                url, display_name = statement_files[customer.pk]
                receiver = resolve_delivery_partner(contact, file_template.whatsapp_account)
                message_content, body_params, _header = _build_template_params(file_template, contact)
                file_sig = process_sending_whatsapp_template.si(
                    file_template.id, receiver.id, sender_partner.id, message_content, body_params,
                    [{'type': 'document', 'url': url, 'filename': display_name}], None,
                ).set(countdown=QURTOBA_DAILY_FILE_DELAY_S)
            except Exception as exc:  # noqa: BLE001
                logger.exception('[Qurtoba Daily] statement file for partner %s failed: %s', pid, exc)
                failed.append({'partner_id': pid, 'part': 'file', 'error': str(exc)[:200]})
        if text_template is not None:
            try:
                receiver = resolve_delivery_partner(contact, text_template.whatsapp_account)
                message_content, body_params, header_params = _build_template_params(text_template, contact)
                process_sending_whatsapp_template.apply_async(
                    args=[text_template.id, receiver.id, sender_partner.id, message_content, body_params,
                          header_params, None],
                    countdown=base,
                    link=file_sig, link_error=file_sig,      # the file follows the text, sent or failed
                )
                texts += 1
                if file_sig is not None:
                    files += 1
                    file_sig = None
            except Exception as exc:  # noqa: BLE001
                logger.exception('[Qurtoba Daily] summary text for partner %s failed: %s', pid, exc)
                failed.append({'partner_id': pid, 'part': 'text', 'error': str(exc)[:200]})
        if file_sig is not None:                 # no text template today: the file goes alone
            file_sig.apply_async(countdown=base + QURTOBA_DAILY_FILE_DELAY_S)
            files += 1
    return {'texts': texts, 'files': files, 'failed': failed}


@shared_task(bind=True, max_retries=0, name='qurtoba.tasks.sync_missing_qurtoba_records')
def sync_missing_qurtoba_records(self, report_date=None, days_back=0):
    """SELF-HEAL: pull any ledger row Qurtoba created that never reached us.

    Their push is fire-and-forget. A row is lost whenever the POST cannot be accepted —
    the credential is gone (2026-09-09: an admin delete destroyed the API token and 27
    rows vanished in 56 minutes), we are mid-deploy, or we answer 4xx for a payload we do
    not understand. Nothing on their side retries, so the row is simply never seen and
    the customer's balance silently drifts.

    Runs every few minutes over today (and yesterday during the night, so the end-of-day
    statement is complete). Idempotent on Qurtoba's own record id, so a row already here
    costs one set lookup. Rows carrying no customer are skipped, exactly as their push does.
    """
    import datetime as _dt

    import requests
    from django.utils import timezone
    from qurtoba.ingest import ingest_row, normalize_api_row
    from qurtoba.models import QurtobaCustomer, QurtobaRecord

    base = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
    token = getattr(settings, 'QURTOBA_TOKEN', '')
    if not base or not token:
        return {'skipped': 'no_api_config'}

    today = timezone.localdate()
    days = [_dt.date.fromisoformat(report_date)] if report_date else \
        [today - _dt.timedelta(days=n) for n in range(0, max(0, int(days_back)) + 1)]

    headers = {'Authorization': f'Token {token}'}
    here = set(QurtobaRecord.objects.exclude(qurtoba_record_id__isnull=True)
               .values_list('qurtoba_record_id', flat=True))
    healed, refused, touched = [], [], set()

    for day in days:
        rows, url, params, pages = [], f'{base}/transactions/api2/record/', {'date': day.isoformat()}, 0
        try:
            while url and pages < 60:
                r = requests.get(url, headers=headers, params=params, timeout=30)
                r.raise_for_status()
                body = r.json()
                rows += body.get('results', [])
                url = body.get('next')
                params = None
                pages += 1
        except Exception as exc:
            logger.warning('[Qurtoba Heal] could not list %s: %s', day, exc)
            continue

        for row in sorted([x for x in rows if str(x.get('date')) == day.isoformat()],
                          key=lambda x: x.get('id') or 0):
            rid = row.get('id')
            if rid in here:
                continue
            cd = row.get('customerData')
            cust_id = cd.get('id') if isinstance(cd, dict) else cd
            if not cust_id:
                continue                       # their push never sends these either
            customer = QurtobaCustomer.objects.filter(qurtoba_id=cust_id).first()
            if customer is None:
                continue                       # a customer this Genie does not know
            obj, outcome, detail = ingest_row(normalize_api_row(row), pull_balance=False)
            if outcome == 'created':
                healed.append(rid)
                here.add(rid)
                touched.add(customer.pk)
            elif outcome == 'invalid':
                refused.append({'record_id': rid, 'error': str(detail)[:200]})

    for pk in touched:
        try:
            QurtobaCustomer.objects.get(pk=pk).recompute_balance()
        except Exception:
            logger.exception('[Qurtoba Heal] balance refresh failed for customer %s', pk)

    if healed or refused:
        logger.warning('[Qurtoba Heal] pulled %d row(s) their push never delivered: %s%s',
                       len(healed), healed[:20], f' | refused: {refused}' if refused else '')
    return {'healed': len(healed), 'record_ids': healed[:50], 'refused': refused,
            'customers_refreshed': len(touched), 'days': [str(d) for d in days]}


def _reminder_sender_partner(template):
    """The internal Partner the reminder is sent 'from'.

    The bulk sender needs one to attribute the outbound message to. There is no
    interactive user behind a beat job, so fall back through the account's own
    partner, then any staff partner.
    """
    from qurtoba.extensions import system_sender

    account = template.whatsapp_account
    candidate = getattr(account, 'partner', None)
    if candidate is not None and getattr(candidate, 'active', True):
        return candidate
    return system_sender()


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

    conv = Conversation.objects.filter(id=conversation_id).only('id', 'handled_by_ai', 'type', 'is_group').first()
    if conv is None or not conv.handled_by_ai:
        return []
    now = _tz.now()
    last_out = (
        Message.objects_all.filter(conversation_id=conversation_id, direction='outbound', active=True)
        .order_by('-created_at').values_list('created_at', flat=True).first()
    )
    from qurtoba.groups import exclude_staff
    qs = exclude_staff(Message.objects_all, conv).filter(
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
        _bridge_task(conversation_id).apply_async(args=[chat_key, conversation_id], countdown=1)
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
                # already re-run once and STILL unanswered: stop retrying, never leave it silent —
                # the holding line to the customer and one note to the office (once per message)
                if cache.add(f'qurtoba:abdication_told:{msg.id}', 1, timeout=3600):
                    _tell_unanswered(msg, 'a transaction message got no tool call and no reply after a re-run')
                continue
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
            _bridge_task(conversation_id).apply_async(args=[chat_key, conversation_id], countdown=1)
            stats['abdicated'] += 1
    except Exception:
        logger.exception('[StrandedRecovery] abdication scan failed')

    # ── Runs that died before the never-silent node could run ────────────────
    # When primary AND backup model fail, the agent node raises, the execution is marked failed and
    # function_model_done never runs. The customer would wait for nothing (14 Sep 2026): tell them
    # once and tell the office, per failed run.
    try:
        stats['failed_runs'] = _failed_runs_fallback()
    except Exception:
        logger.exception('[StrandedRecovery] failed-run scan failed')

    if stats['recovered'] or stats['cleared'] or stats.get('abdicated') or stats.get('failed_runs'):
        logger.info('[StrandedRecovery] %s', stats)
    return stats


def _bridge_task(conversation_id):
    """The AI bridge task of the conversation's own channel — a WhatsApp Web group lock must be re-run by
    the WhatsApp Web bridge (its partner, its group workflow, its gates), never by the Cloud one."""
    import importlib
    from modules.chat.models import Conversation
    conv_type = Conversation.objects.filter(pk=conversation_id).values_list('type', flat=True).first() or 'whatsapp'
    try:
        return importlib.import_module(f'modules.aistudio_{conv_type}.tasks').process_workflow_messages
    except Exception:
        from modules.aistudio_whatsapp.tasks import process_workflow_messages
        return process_workflow_messages


_FLOW_THREAD_PREFIXES = ('whatsapp_', 'wa_web_')


def _failed_runs_fallback(minutes: int = 15) -> int:
    """For every WhatsApp flow run of the last `minutes` that ended failed and whose customer got
    nothing since it started: the holding line quoted on their newest message + a staff note."""
    from datetime import timedelta
    from django.core.cache import cache
    from django.utils import timezone
    from modules.aistudio.models import WorkflowExecution
    from modules.chat.models import Conversation, Message
    from qurtoba.automation import replies as R
    from qurtoba.automation.context import send_quoted
    from qurtoba.staff_notes import post_staff_note

    told = 0
    since = timezone.now() - timedelta(minutes=minutes)
    from django.db.models import Q
    thread_q = Q()
    for prefix in _FLOW_THREAD_PREFIXES:
        thread_q |= Q(trigger_context__thread_id__startswith=prefix)
    runs = (WorkflowExecution.objects.filter(thread_q, created_at__gte=since)
            .exclude(status__in=('completed', 'running', 'pending', 'paused'))
            .order_by('created_at')[:50])
    for run in runs:
        if not cache.add(f'qurtoba:failed_run_told:{run.pk}', 1, timeout=3600):
            continue
        thread = str((run.trigger_context or {}).get('thread_id') or '')
        conv_id = next((thread[len(p):] for p in _FLOW_THREAD_PREFIXES if thread.startswith(p)), None)
        conv = Conversation.objects.filter(pk=conv_id).first() if conv_id else None
        if conv is None or not getattr(conv, 'handled_by_ai', False):
            continue
        started = run.started_at or run.created_at
        if Message.objects_all.filter(conversation=conv, direction='outbound', is_internal=False,
                                      created_at__gt=started).exclude(type__in=('tool', 'tool_call')).exists():
            continue                                    # something did reach the customer
        from qurtoba.groups import customer_inbound
        newest = customer_inbound(conv).order_by('-created_at').first()
        if newest is None:
            continue
        send_quoted(conv, str(newest.id), R.MODEL_DOWN, once_minutes=30)
        post_staff_note(
            conv,
            ['⚠️ الرد الآلي وقع على رسالة العميل',
             f'الخطأ: {str(run.error_message or run.status)[:200]}',
             'العميل اتبلغ «ثواني وهنرد على حضرتك» — محتاج رد يدوي.'],
            subject='⚠️ الرد الآلي وقع — رد يدوي مطلوب',
            body=f'{getattr(conv.social_partner, "name", "") or ""}: الرد الآلي فشل — محتاج رد يدوي.',
            reply_to=newest, dedupe_key=f'model_down:{conv.id}', dedupe_ttl=1800,
        )
        logger.warning('[StrandedRecovery] failed run %s in chat %s — customer told, staff noted', run.pk, str(conv.id)[:8])
        told += 1
    return told


def _tell_unanswered(msg, why: str) -> None:
    """Never silent: «ثواني وهنرد على حضرتك» quoted on the customer's message + an internal staff note."""
    try:
        from qurtoba.automation import replies as R
        from qurtoba.automation.context import send_quoted
        from qurtoba.staff_notes import post_staff_note
        conv = msg.conversation
        send_quoted(conv, str(msg.id), R.MODEL_DOWN, once_minutes=30)
        text = (msg.content or {}).get('text', '') if isinstance(msg.content, dict) else ''
        post_staff_note(
            conv,
            ['⚠️ رسالة عميل من غير رد', f'«{str(text)[:120]}»', f'السبب: {why}',
             'العميل اتبلغ «ثواني وهنرد على حضرتك» — محتاج رد يدوي.'],
            subject='⚠️ رسالة عميل من غير رد', body=f'{getattr(conv.social_partner, "name", "") or ""}: {str(text)[:60]}',
            reply_to=msg, dedupe_key=f'unanswered:{msg.id}',
        )
    except Exception:
        logger.warning('[StrandedRecovery] could not tell about unanswered message %s', str(msg.id)[:8], exc_info=True)


def _abdicated_transaction_messages(min_age_s: int = 60, max_age_min: int = 6):
    """Unconsumed self-contained transaction messages with no trace of handling after them."""
    from datetime import timedelta
    from django.utils import timezone
    from modules.chat.models import Message
    from qurtoba.groups import staff_q
    from qurtoba.tools.planning import _classify_message

    now = timezone.now()
    rows = (
        Message.objects_all
        .filter(direction='inbound', type='text', active=True, ai_consumed_at__isnull=True,
                created_at__lte=now - timedelta(seconds=min_age_s),
                created_at__gte=now - timedelta(minutes=max_age_min),
                conversation__handled_by_ai=True, conversation__type__in=('whatsapp', 'wa_web'),
                conversation__social_partner__qurtoba_customer__isnull=False)
        .exclude(staff_q())
        .select_related('conversation')
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
