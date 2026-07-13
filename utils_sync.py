import logging
import datetime as dt

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def push_record_to_qurtoba(record_pk: int) -> str | None:
    """
    Single POST attempt to Qurtoba for a Genie-created QurtobaRecord.
    On success: stores qurtoba_record_id + pulls authoritative balance from Qurtoba.
    Returns None on success, error string on failure (caller handles retry).
    """
    from qurtoba.models import QurtobaRecord

    base  = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
    token = getattr(settings, 'QURTOBA_TOKEN', '')
    if not base or not token:
        return 'QURTOBA_BASE_URL / QURTOBA_TOKEN not configured'

    try:
        record = QurtobaRecord.objects.select_related('customer').get(pk=record_pk)
    except QurtobaRecord.DoesNotExist:
        return None  # deleted before task ran — nothing to do

    # Idempotency: already pushed → never create a duplicate Qurtoba record.
    # This makes it safe for BOTH the async post_create enqueue AND an explicit
    # synchronous push (approve flow) to call us — whichever runs second no-ops.
    if record.qurtoba_synced and record.qurtoba_record_id:
        return None

    customer = record.customer
    if not customer or not getattr(customer, 'qurtoba_id', None):
        return 'Customer has no Qurtoba ID — cannot push'

    headers = {'Authorization': f'Token {token}', 'Content-Type': 'application/json'}
    payload = _build_payload(record, customer)

    try:
        resp = requests.post(
            f'{base}/transactions/api2/record/',
            json=payload, headers=headers, timeout=10,
        )
        if resp.ok:
            body = resp.json()
            if body.get('status'):
                qurtoba_id = (body.get('data') or {}).get('id')
                # Qurtoba can return status:True with data:None when it did NOT
                # actually create the record — e.g. record registration is
                # disabled by the accountant ("تسجيل التحويلات موقوفه"). Marking
                # such a record as synced is silent data loss: the payment never
                # hits the ledger, the customer's Rest never updates, and the
                # balance stays wrong (this is the 100K سداد → balance 0 bug).
                # Treat a missing id as a retryable failure so it never silently
                # vanishes.
                if not qurtoba_id:
                    return (
                        'Qurtoba returned status=True but no record id '
                        '(record not created — registration disabled?): '
                        f'{body.get("message", "")}'
                    )
                _mark_success(record_pk, qurtoba_id)
                # Pull authoritative balance from Qurtoba (recalculated synchronously).
                _sync_customer_balance(base, token, customer)
                # For cash types, Genie must call Cash-SYS directly.
                # Qurtoba's send_to_cash_sys() is gated behind `if not is_genie`, so it
                # never fires when Genie pushes — we call Cash-SYS here instead.
                if qurtoba_id:
                    # رقم الحساب (account_number) is the Cash-SYS transfer phone — NOT customer.phone_no
                    _send_to_cash_sys(qurtoba_id, record.type, record.value, record.account_number or '')
                return None  # success
            return f'Qurtoba rejected: {body.get("message", "")}'
        return f'HTTP {resp.status_code}: {resp.text[:200]}'
    except Exception as exc:
        return str(exc)


def _build_payload(record, customer) -> dict:
    now = dt.datetime.now()
    return {
        # Stable idempotency key: the accountant server dedupes on this, so a
        # retry of this push never creates a duplicate ledger row.
        'external_ref':  str(record.pk),
        'customerData':  customer.qurtoba_id,
        'type':          record.type,
        'accountNumber': record.account_number or '',
        'value':         record.value,
        'isDone':        record.is_done,
        'isDown':        record.is_down,
        'isSeller':      record.is_seller,
        'date':          str(record.date or now.date()),
        'time':          str(record.time or now.time().replace(microsecond=0)),
        'datetime':      record.datetime_field.isoformat() if record.datetime_field else now.isoformat(),
        # Never send notes to Qurtoba (for ANY type) — the full note stays on the
        # Genie record; Qurtoba just doesn't need it (and its 150-char cap was
        # rejecting سداد pushes).
        'notes':         '',
        'seller':        record.seller_qurtoba_id,
        'accountant':    record.accountant_qurtoba_id,
    }


def _sync_customer_balance(base: str, token: str, customer) -> None:
    """
    Backward-compatible thin wrapper: delegates to QurtobaCustomer.recompute_balance(),
    which now hits Qurtoba's REST API directly (with a local-sum fallback). The
    `base` and `token` args are accepted for signature compatibility but ignored —
    settings are read inside the model method.
    """
    try:
        customer.recompute_balance()
    except Exception as exc:
        logger.warning('Failed to refresh balance for customer %s: %s', customer.qurtoba_id, exc)


_CASH_TYPES = {'كاش', 'كاش(5)', 'كاش(10)', 'كاش(20)'}


def _record_cash_sys_problem(qurtoba_record_id, error: str, payload=None) -> None:
    """Surface a permanently-failed Cash-SYS order creation as a visible, UI-retryable
    QurtobaSyncProblem instead of letting it die in a daemon-thread log line (the
    order would otherwise never reach Cash-SYS with no one the wiser)."""
    try:
        from qurtoba.models import QurtobaRecord, QurtobaSyncProblem
        rec = QurtobaRecord.objects.filter(qurtoba_record_id=qurtoba_record_id).first()
        if rec:
            QurtobaSyncProblem.record(rec, 'cash_sys_order', error, payload=payload or {})
        else:
            logger.error('cash_sys: no Genie record for qurtoba_id=%s — cannot record problem (%s)',
                         qurtoba_record_id, error)
    except Exception as exc:
        logger.error('cash_sys: failed to record problem for qurtoba_id=%s: %s', qurtoba_record_id, exc)


def _send_to_cash_sys(qurtoba_record_id: int, record_type: str, value, phone_no: str) -> None:
    """
    POST a cash order to Cash-SYS directly from Genie.
    Called after Genie successfully pushes a record to Qurtoba.
    Qurtoba's own send_to_cash_sys() is gated behind `if not is_genie` and never
    fires for Genie-pushed records — so Genie must call Cash-SYS itself.
    external_ref = str(qurtoba_record_id) so the webhook handler can look it up.
    """
    if record_type not in _CASH_TYPES:
        return
    # phone_no = record.account_number (رقم الحساب) — the actual transfer destination phone.
    # Qurtoba sometimes stores 12-digit numbers (extra digit); normalize to first 11 if starts with 01.
    phone_str = ''.join(filter(str.isdigit, str(phone_no or ''))).strip()
    if not phone_str:
        logger.warning('cash_sys: رقم الحساب is empty for qurtoba_record_id=%d type=%s — skipping', qurtoba_record_id, record_type)
        return
    if phone_str.startswith('01') and len(phone_str) > 11:
        phone_str = phone_str[:11]
    if not phone_str.startswith('01') or len(phone_str) != 11:
        logger.warning('cash_sys: invalid رقم الحساب %r for qurtoba_record_id=%d — Cash-SYS will reject', phone_no, qurtoba_record_id)
    cs_base  = getattr(settings, 'CASH_SYS_BASE_URL', '').rstrip('/')
    cs_token = getattr(settings, 'CASH_SYS_TOKEN', '')
    if not cs_base or not cs_token:
        logger.warning('CASH_SYS_BASE_URL / CASH_SYS_TOKEN not set in Genie — cannot call Cash-SYS')
        return
    import threading, time as _time

    def _post():
        hdrs    = {'Authorization': f'Token {cs_token}', 'Content-Type': 'application/json'}
        payload = {
            'external_ref': str(qurtoba_record_id),
            'phone_number':  phone_str,
            'value':         int(value),
            'order_type':    record_type,
        }
        url = f'{cs_base}/api/v1/integration/orders/'
        for attempt, delay in enumerate([0, 5, 15, 30]):
            if delay: _time.sleep(delay)
            try:
                resp = requests.post(url, json=payload, headers=hdrs, timeout=10)
                if resp.ok:
                    logger.info('cash_sys order created from Genie for qurtoba_id=%d: id=%s',
                                qurtoba_record_id, resp.json().get('id'))
                    return
                if resp.status_code == 400:
                    logger.error('cash_sys rejected qurtoba_id=%d (400): %s', qurtoba_record_id, resp.text[:200])
                    _record_cash_sys_problem(qurtoba_record_id, f'Cash-SYS rejected (400): {resp.text[:200]}', payload)
                    return
                logger.warning('cash_sys attempt %d HTTP %s for qurtoba_id=%d', attempt+1, resp.status_code, qurtoba_record_id)
            except Exception as exc:
                logger.warning('cash_sys attempt %d failed for qurtoba_id=%d: %s', attempt+1, qurtoba_record_id, exc)
        logger.error('cash_sys gave up for qurtoba_id=%d', qurtoba_record_id)
        _record_cash_sys_problem(qurtoba_record_id, 'Cash-SYS order creation failed after all retries', payload)

    threading.Thread(target=_post, daemon=True).start()


def resend_cash_sys_order(object_id, payload_str=None) -> str | None:
    """
    SYNCHRONOUS re-post of a Cash-SYS order — used by the QurtobaSyncProblem
    foreground retry (UI "resend"). Returns None on success, an error string on
    failure. Idempotent: Cash-SYS dedupes on external_ref, so a resend of an
    order that actually reached Cash-SYS never double-creates a transfer.

    payload_str is the stored problem payload (JSON string with external_ref /
    phone_number / value / order_type). If it's missing or unparsable we rebuild
    the payload from the live QurtobaRecord (object_id = Genie record pk).
    """
    import json
    from qurtoba.models import QurtobaRecord

    payload = None
    if payload_str:
        try:
            payload = json.loads(payload_str) if isinstance(payload_str, str) else dict(payload_str)
        except (TypeError, ValueError):
            payload = None

    # Rebuild from the record when the stored payload is absent/broken.
    if not isinstance(payload, dict) or not payload.get('order_type'):
        rec = QurtobaRecord.objects.filter(pk=int(object_id)).first()
        if not rec:
            return f'no Genie record for pk={object_id} — cannot resend Cash-SYS order'
        if rec.type not in _CASH_TYPES:
            return f'record type {rec.type!r} is not a Cash-SYS (كاش) type — nothing to resend'
        phone_str = ''.join(filter(str.isdigit, str(rec.account_number or ''))).strip()
        if phone_str.startswith('01') and len(phone_str) > 11:
            phone_str = phone_str[:11]
        payload = {
            'external_ref': str(rec.qurtoba_record_id or rec.pk),
            'phone_number': phone_str,
            'value':        int(rec.value),
            'order_type':   rec.type,
        }

    phone_str = ''.join(filter(str.isdigit, str(payload.get('phone_number') or ''))).strip()
    if phone_str.startswith('01') and len(phone_str) > 11:
        phone_str = phone_str[:11]
    if not phone_str.startswith('01') or len(phone_str) != 11:
        return f'invalid رقم الحساب {payload.get("phone_number")!r} — Cash-SYS will reject'
    payload['phone_number'] = phone_str

    cs_base  = getattr(settings, 'CASH_SYS_BASE_URL', '').rstrip('/')
    cs_token = getattr(settings, 'CASH_SYS_TOKEN', '')
    if not cs_base or not cs_token:
        return 'CASH_SYS_BASE_URL / CASH_SYS_TOKEN not configured in Genie'

    hdrs = {'Authorization': f'Token {cs_token}', 'Content-Type': 'application/json'}
    url  = f'{cs_base}/api/v1/integration/orders/'
    last_err = None
    for attempt, delay in enumerate([0, 5, 15]):
        if delay:
            import time as _time
            _time.sleep(delay)
        try:
            resp = requests.post(url, json=payload, headers=hdrs, timeout=10)
            if resp.ok:
                logger.info('cash_sys order resent from Genie for ref=%s: id=%s',
                            payload.get('external_ref'), resp.json().get('id') if resp.content else '')
                return None
            last_err = f'HTTP {resp.status_code}: {resp.text[:200]}'
            if resp.status_code == 400:
                logger.error('cash_sys resend rejected ref=%s: %s', payload.get('external_ref'), last_err)
                return last_err
            logger.warning('cash_sys resend attempt %d %s ref=%s', attempt + 1, last_err, payload.get('external_ref'))
        except Exception as exc:
            last_err = str(exc)
            logger.warning('cash_sys resend attempt %d failed ref=%s: %s', attempt + 1, payload.get('external_ref'), exc)
    return last_err or 'Cash-SYS resend failed'


def edit_qurtoba_record_value(qurtoba_record_id: int, new_value) -> str | None:
    """
    Edit an existing record's value on the Qurtoba accountant server (port 6000).
    Used on a reroute: the original order is settled at the sent amount (e.g.
    10000 → 6000) and the accountant ledger must reflect the same value.

    PATCH {QURTOBA_BASE_URL}/transactions/api2/record/{id}/  body {'value': new_value}
    Returns None on success, an error string on failure.
    """
    base  = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
    token = getattr(settings, 'QURTOBA_TOKEN', '')
    if not base or not token:
        return 'QURTOBA_BASE_URL / QURTOBA_TOKEN not configured'
    if not qurtoba_record_id:
        return 'missing qurtoba_record_id'

    headers = {'Authorization': f'Token {token}', 'Content-Type': 'application/json'}
    url = f'{base}/transactions/api2/record/{int(qurtoba_record_id)}/'
    payload = {'value': new_value}

    last_err = None
    for attempt, delay in enumerate([0, 5, 15]):
        if delay:
            import time as _time
            _time.sleep(delay)
        try:
            resp = requests.patch(url, json=payload, headers=headers, timeout=10)
            if resp.ok:
                logger.info('accountant edit ok qurtoba_id=%s value=%s', qurtoba_record_id, new_value)
                return None
            last_err = f'HTTP {resp.status_code}: {resp.text[:200]}'
            if resp.status_code == 400:
                logger.error('accountant edit rejected qurtoba_id=%s: %s', qurtoba_record_id, last_err)
                return last_err
            logger.warning('accountant edit attempt %d %s for qurtoba_id=%s', attempt + 1, last_err, qurtoba_record_id)
        except Exception as exc:
            last_err = str(exc)
            logger.warning('accountant edit attempt %d failed for qurtoba_id=%s: %s', attempt + 1, qurtoba_record_id, exc)
    return last_err or 'accountant edit failed'


def _mark_success(record_pk: int, qurtoba_id) -> None:
    from qurtoba.models import QurtobaRecord
    from django.utils import timezone
    QurtobaRecord.objects.filter(pk=record_pk).update(
        qurtoba_synced=True,
        qurtoba_record_id=qurtoba_id,
        qurtoba_posted_at=timezone.now(),
        qurtoba_sync_error=None,
    )


def _mark_error(record_pk: int, error: str) -> None:
    from qurtoba.models import QurtobaRecord
    QurtobaRecord.objects.filter(pk=record_pk).update(
        qurtoba_synced=False,
        qurtoba_sync_error=error or '',
    )


# ---------------------------------------------------------------------------
# Cash-SYS client management — fully standalone, no DB, no Qurtoba coupling
# ---------------------------------------------------------------------------

def _cash_sys_client_headers() -> dict | None:
    """Return auth headers for the genie_integration token, or None if not configured."""
    token = getattr(settings, 'CASH_SYS_CLIENT_TOKEN', '')
    if not token:
        return None
    return {'Authorization': f'Token {token}', 'Content-Type': 'application/json'}


def create_cash_sys_client(data: dict) -> tuple[dict | None, str | None]:
    """
    POST /api/v1/cashstuff/clients/
    Auth: CASH_SYS_CLIENT_TOKEN (genie_integration user)

    data (required: code, name, company, phone — optional: district, governorate, address, notes):
        {
            "code":        "101",
            "name":        "Ahmed Ali",
            "company":     "Ahmed Shop",
            "phone":       "01012345678",
            "district":    "Maadi",       # optional
            "governorate": "Cairo",       # optional
            "address":     "...",         # optional
            "notes":       ""             # optional
        }

    Returns (client_data, None) on success — client_data includes 'id' field.
    Returns (None, error_string) on failure.
    """
    base = getattr(settings, 'CASH_SYS_BASE_URL', '').rstrip('/')
    headers = _cash_sys_client_headers()
    if not base or not headers:
        return None, 'CASH_SYS_BASE_URL or CASH_SYS_CLIENT_TOKEN not configured'
    try:
        resp = requests.post(
            f'{base}/api/v1/cashstuff/clients/',
            json=data,
            headers=headers,
            timeout=15,
        )
        if resp.ok:
            return resp.json(), None
        return None, f'HTTP {resp.status_code}: {resp.text[:200]}'
    except Exception as exc:
        logger.error('create_cash_sys_client failed: %s', exc)
        return None, str(exc)


def activate_cash_sys_trial(client_id: int) -> tuple[dict | None, str | None]:
    """
    POST /api/v1/cashstuff/clients/<client_id>/activate-trial/
    Auth: CASH_SYS_CLIENT_TOKEN (genie_integration user)
    No request body required.

    Creates tenant account automatically if client has none.
    Returns (response_data, None) on success.
    response_data includes: id, stage, trial_status, trial_started_at, account_created
    and optionally phone + password when the account was just created.

    Returns (None, error_string) on failure.
    """
    base = getattr(settings, 'CASH_SYS_BASE_URL', '').rstrip('/')
    headers = _cash_sys_client_headers()
    if not base or not headers:
        return None, 'CASH_SYS_BASE_URL or CASH_SYS_CLIENT_TOKEN not configured'
    try:
        resp = requests.post(
            f'{base}/api/v1/cashstuff/clients/{client_id}/activate-trial/',
            headers=headers,
            timeout=15,
        )
        if resp.ok:
            return resp.json(), None
        return None, f'HTTP {resp.status_code}: {resp.text[:200]}'
    except Exception as exc:
        logger.error('activate_cash_sys_trial failed for client %s: %s', client_id, exc)
        return None, str(exc)
