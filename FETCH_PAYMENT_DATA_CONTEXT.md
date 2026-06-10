# Data-extraction request — سداد فورى (شراء فورى) payment, id=1

**Goal:** dump the raw rows only. Do NOT fix anything. Run the shells below and
paste the FULL output back verbatim.

Affected approval:
`?model=qurtoba.qurtobapendingpayment&id=1` — a **سداد فورى of 100,000** that was
approved, but the matching `شراء فورى` transaction is **missing on the Qurtoba
canonical server** (the one every transaction must be pushed to). We need the raw
data from both databases to find out why it never got created there.

Note on the Arabic type string: it is stored as **`شراء فورى`** (final letter is
`ى` alef-maksura, not `ي`). The queries below match on a substring (`فور`) so they
catch any spelling variant.

---

## 1) Genie tenant — Django shell

```python
from qurtoba.models import QurtobaPendingPayment, QurtobaRecord

def dump(obj):
    if obj is None:
        return None
    return {k: v for k, v in obj.__dict__.items() if not k.startswith('_')}

p = QurtobaPendingPayment.objects.get(pk=1)
print("=== QurtobaPendingPayment id=1 ===")
print(dump(p))

print("\n=== created_record (the pushed QurtobaRecord) ===")
print(dump(p.created_record))

print("\n=== ALL سداد (is_down=True) records for this customer ===")
for x in QurtobaRecord.objects.filter(customer_id=p.customer_id, is_down=True).order_by('-id')[:30]:
    print(x.id, '|', x.type, '|', x.value,
          '| synced=', x.qurtoba_synced,
          '| qurtoba_record_id=', x.qurtoba_record_id,
          '| err=', (x.qurtoba_sync_error or '')[:120],
          '| cdq_id=', x.customer_data_qurtoba_id)

print("\n=== ANY record whose type contains فور, this customer ===")
for x in QurtobaRecord.objects.filter(customer_id=p.customer_id, type__icontains='فور').order_by('-id')[:30]:
    print(x.id, '|', x.type, '|', x.value, '| is_down=', x.is_down,
          '| synced=', x.qurtoba_synced, '| qurtoba_record_id=', x.qurtoba_record_id)

c = p.customer
print("\n=== customer ===")
print('id=', c.id, 'name=', c.name, 'qurtoba_id=', c.qurtoba_id, 'balance=', c.balance)
print('>>> COPY THIS qurtoba_id FOR STEP 3:', c.qurtoba_id)
```

## 2) Genie tenant — what Qurtoba currently reports for this customer

```python
import requests
from django.conf import settings
base  = settings.QURTOBA_BASE_URL.rstrip('/')
token = settings.QURTOBA_TOKEN
qid   = c.qurtoba_id
r = requests.get(f'{base}/transactions/api2/rest-customer/{qid}/',
                 headers={'Authorization': f'Token {token}'}, timeout=10)
print('rest-customer:', r.status_code, r.text)
```

## 3) Qurtoba canonical server (port 6000 / qurtobaSys_django) — Django shell

Replace `QID` with the `qurtoba_id` printed in step 1.

```python
from transactions.models import Record, Rest
QID = QID  # <-- paste the customer's qurtoba_id

print("=== Rest row ===")
print(list(Rest.objects.filter(customer_id=QID).values('customer_id','value','date','time')))

print("\n=== records with type containing فور ===")
print(list(Record.objects.filter(customerData_id=QID, type__icontains='فور')
           .values('id','type','value','isDown','isDone','date','time','datetime')))

print("\n=== ALL is_down/payment records ===")
print(list(Record.objects.filter(customerData_id=QID, isDown=True)
           .values('id','type','value','isDown','isDone','datetime')))

print("\n=== 20 most recent records for this customer ===")
print(list(Record.objects.filter(customerData_id=QID).order_by('-id')
           .values('id','type','value','isDown','isDone','datetime')[:20]))

# Is transaction registration currently disabled?
try:
    from config.models import Config            # adjust path if import fails
    print("\nrecord registration state:", Config.objects.get(name='record').state)
except Exception as e:
    print("\nConfig lookup failed (find the model):", e)
```

---

## What to return
Paste the **entire raw output of all three steps** back, unedited. No diagnosis
needed yet — we will analyze it here once we have the data.
