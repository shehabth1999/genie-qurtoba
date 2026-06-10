# Context report — سداد approved but customer balance stays 0

**Hand this file to the Claude agent running on the VPS.** It explains the bug,
the exact code paths, and the precise diagnostics to run against the live data
for the affected row.

## Incident

- URL: `https://qurtoba.genie-erp.com/genie/41/?model=qurtoba.qurtobapendingpayment&module=qurtoba&view_type=form&id=1`
- Action: operator pressed **اعتماد** (approve) on `QurtobaPendingPayment id=1`.
- The payment is a **سداد فورى of 100,000** (`is_down=True`, type `شراء فورى`).
- Customer outstanding balance (مديونيات) was **0** before approval.
- **Expected:** after approval the balance becomes **−100,000** (customer overpaid → credit).
- **Actual:** balance is **still 0**.

## What is NOT the cause (already verified in code)

There is **no clamp at 0** anywhere. The balance is `debts − payments` and is
allowed to go negative. Verified on the Qurtoba canonical side
(`E:\Qurtoba\old sys\qurtobaSys_django`):

- Create: `transactions/api/serializations/ser_transactions.py` `SRecordSets.save()`
  → `sum = value1 - value2`; `Rest.value = sum`. No clamp.
- Edit (PATCH): `transactions/api/controllers/transactions.py` `partial_update`
  → `total = value1 - value2`. No clamp.
- Read: `transactions/api/controllers/delayed_payment.py` `RestCustomer`
  (`/transactions/api2/rest-customer/<customer>/`) returns the raw `Rest.value`. No clamp.

For a 100K سداد on a zero-debt customer the math correctly yields **−100K**. So
"value can't go below 0" is a wrong hypothesis — the real failure is that **the
payment never reached Qurtoba's ledger**, so `Rest` was never recomputed.

## The flow (Genie side)

`modules.../qurtoba/models.py` `QurtobaPendingPayment.action_approve_pending_payment`:
1. Creates `QurtobaRecord(is_down=True, value=100000, type='شراء فورى', customer=…)`.
2. `QurtobaRecord.save()` calls `customer.recompute_balance()` **synchronously** —
   at this moment the record has NOT been pushed to Qurtoba yet, so the pull
   returns the **stale** pre-payment balance (0).
3. `post_create` enqueues `push_record_to_qurtoba_task` (async, Celery).
4. The task runs `utils_sync.push_record_to_qurtoba()` → POST to Qurtoba
   `createRecord` → Qurtoba runs `SRecordSets.save()` → `Rest = −100000` → Genie
   re-pulls the balance → **−100000**.

If step 4 does not complete successfully, the balance keeps the stale 0 from step 2.

## Two code-level causes that produce exactly this symptom

### A) False success: Qurtoba returns `status:True, data:None`  ← FIXED in this commit
`qurtobaSys_django/.../controllers/transactions.py` `createRecord` (line ~196):
```python
state = Config.objects.get(name="record").state
if state == False:
    return Response({"message": "تسجيل التحويلات موقوفه…", "status": True, "data": None})
```
When the accountant has **record registration disabled**, Qurtoba replies
`status:True` but **did not create the record** (`data:None`). The old Genie code
did `_mark_success(record_pk, None)` → marked `qurtoba_synced=True` with
`qurtoba_record_id=None` and reported success. The payment silently vanished and
`Rest` stayed 0.
**Fix applied** in `qurtoba/utils_sync.py::push_record_to_qurtoba`: a missing
record id is now treated as a retryable failure (not marked synced).

### B) Genuine push failure → orphan excluded from the local fallback
`qurtoba/models.py` `QurtobaCustomer.recompute_balance()` fallback only sums
records where `qurtoba_synced=True OR customer_data_qurtoba_id IS NOT NULL`.
A Genie-born سداد whose push failed (no `qurtoba_id`, Qurtoba down, network) is an
**orphan** (`qurtoba_synced=False AND customer_data_qurtoba_id IS NULL`) and is
**excluded by design** → balance stays 0. By-design, but same visible symptom.

## Diagnostics to run on the VPS

### 1) Genie side — Django shell on the Genie tenant
```python
from qurtoba.models import QurtobaPendingPayment
p = QurtobaPendingPayment.objects.get(pk=1)
print('pending:', p.review_state, p.type, p.value, 'customer=', p.customer_id, p.customer.name)
print('created_record_id:', p.created_record_id)

r = p.created_record
if r:
    print('record.is_down       :', r.is_down)        # must be True (payment)
    print('record.value         :', r.value)          # 100000
    print('record.qurtoba_synced:', r.qurtoba_synced) # KEY
    print('record.qurtoba_record_id:', r.qurtoba_record_id)  # KEY — None == not really created
    print('record.qurtoba_sync_error:', r.qurtoba_sync_error)# KEY — the failure reason
    print('record.customer_data_qurtoba_id:', r.customer_data_qurtoba_id)

c = p.customer
print('customer.qurtoba_id:', c.qurtoba_id)  # None → push can never succeed
print('customer.balance   :', c.balance)
```

### 2) Genie side — what Qurtoba currently reports for this customer
```python
import requests
from django.conf import settings
base  = settings.QURTOBA_BASE_URL.rstrip('/'); token = settings.QURTOBA_TOKEN
resp = requests.get(f'{base}/transactions/api2/rest-customer/{c.qurtoba_id}/',
                    headers={'Authorization': f'Token {token}'}, timeout=10)
print(resp.status_code, resp.text)   # the canonical Rest.value Qurtoba returns
```

### 3) Qurtoba side — does the record/Rest actually exist?
On the Qurtoba (`qurtobaSys_django`, port 6000) shell:
```python
from transactions.models import Record, Rest
from config.models import Config           # adjust import path if different
print('record registration state:', Config.objects.get(name='record').state)  # False == cause A
print('Rest row:', Rest.objects.filter(customer_id=<qurtoba_id>).values('value','date','time').first())
print('payment records:', list(Record.objects.filter(
        customerData_id=<qurtoba_id>, isDown=True, isDone=False
      ).values('id','value','isDone','datetime')))
```

## Decision tree

- `qurtoba_record_id is None` **and** `qurtoba_synced=True` → **cause A**
  (false success; registration was disabled). The fix in this commit stops it
  recurring. To repair the stuck row: re-push the record (see below) once
  registration is re-enabled.
- `qurtoba_synced=False` with a `qurtoba_sync_error` → **cause B** (push failed);
  read the error. Common: `Customer has no Qurtoba ID`, HTTP error, or the new
  `status=True but no record id` message.
- `customer.qurtoba_id is None` → push can never succeed; the customer must be
  linked/synced to Qurtoba first.
- Qurtoba `Config record state == False` → registration is off; turn it back on,
  then re-push.

## Re-pushing a stuck record (after the root cause is cleared)
```python
from qurtoba.tasks import push_record_to_qurtoba_task
# reset the false-synced flag first if it was wrongly marked:
from qurtoba.models import QurtobaRecord
QurtobaRecord.objects.filter(pk=r.pk).update(qurtoba_synced=False, qurtoba_record_id=None)
push_record_to_qurtoba_task.delay(r.pk)
# then re-check c.refresh_from_db(); c.recompute_balance(); print(c.balance)
```

## Deploy note
The `utils_sync.py` fix is task code → **restart the Genie Celery worker** for it
to take effect on the VPS.
