"""One place where a Qurtoba ledger row becomes a Genie row.

Both entry points use it, so they can never drift apart:
  * the live push   — POST /transactions/api2/record/  (views.QurtobaRecordListView)
  * the backfill    — management command qurtoba_backfill_records

Idempotent on Qurtoba's own primary key: a row already here is reported as a
duplicate and nothing is written.
"""
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def normalize_api_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """A row as the LIST endpoint returns it → the payload shape the push sends.

    The two differ: the list endpoint nests `customerData` as an object, names the id
    `id`, and returns `accountant` / `seller` as display names rather than ids.
    """
    out = dict(row or {})
    if 'id' in out and '_record_id' not in out:
        out['_record_id'] = out.pop('id')
    cd = out.get('customerData')
    if isinstance(cd, dict):
        out['customerData'] = cd.get('id')
    for key in ('accountant', 'seller'):
        v = out.get(key)
        if isinstance(v, dict):
            out[key] = v.get('id')
        elif not isinstance(v, int):
            out.pop(key, None)          # a display name is not an id — drop it
    out.pop('external_ref', None)
    return out


def ingest_row(payload: Dict[str, Any], *, pull_balance: bool = True) -> Tuple[Optional[Any], str, Any]:
    """Create the Genie record for one Qurtoba row.

    Returns (record | None, outcome, detail) where outcome is one of
    'created' | 'duplicate' | 'invalid'. Never raises on a bad payload.
    """
    from django.conf import settings
    from django.utils import timezone
    from qurtoba.models import QurtobaCustomer, QurtobaRecord
    from qurtoba.serializers import QurtobaRecordSerializer

    raw_id = payload.get('_record_id')
    record_id = None
    if raw_id not in (None, ''):
        try:
            record_id = int(raw_id)
        except (TypeError, ValueError):
            record_id = None
    if record_id is not None:
        existing = QurtobaRecord.objects.filter(qurtoba_record_id=record_id).first()
        if existing:
            return existing, 'duplicate', existing.pk

    ser = QurtobaRecordSerializer(data=payload)
    if not ser.is_valid():
        return None, 'invalid', ser.errors

    obj = ser.save()
    update_fields = ['raw_data', 'qurtoba_synced', 'qurtoba_posted_at']
    obj.raw_data = dict(payload)
    # The row originated in Qurtoba: mark it synced so post_create never pushes it back.
    obj.qurtoba_synced = True
    obj.qurtoba_posted_at = timezone.now()
    if record_id is not None:
        obj.qurtoba_record_id = record_id
        update_fields.append('qurtoba_record_id')
    customer = None
    if obj.customer_data_qurtoba_id:
        customer = QurtobaCustomer.objects.filter(qurtoba_id=obj.customer_data_qurtoba_id).first()
    if customer:
        obj.customer = customer
        update_fields.append('customer')
    obj.save(update_fields=update_fields)     # → recompute_balance()

    if customer and pull_balance:
        # Qurtoba is the source of truth: pull the authoritative balance, because it may
        # hold rows this side has never seen.
        try:
            from qurtoba.utils_sync import _sync_customer_balance
            _sync_customer_balance(getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/'),
                                   getattr(settings, 'QURTOBA_TOKEN', ''), customer)
        except Exception:
            logger.exception('[Qurtoba Sync] balance pull failed for customer %s', customer.pk)
    return obj, 'created', obj.pk
