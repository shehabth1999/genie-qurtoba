# Cash-SYS Integration — Genie ERP

**Cash-SYS** is a fully independent system for executing mobile cash transfers (كاش/فورى) via SIM cards.  
Genie ERP interacts with it through three API calls only (catalog, create client, activate trial). Everything else (payments, plan upgrades, invoicing) is handled inside Cash-SYS itself.

> **Cash orders are NOT posted by Genie.** Genie pushes the record to Qurtoba
> (`push_record_to_qurtoba`); **Qurtoba** is the single system that posts the cash
> order to Cash-SYS (`/api/v1/integration/orders/`), carrying the customer name +
> `notes_plus`. Genie posting orders too would double-create the physical transfer.
> Cash-SYS still sends `order_done`/`order_canceled` webhooks back to Genie (resolved
> via `external_ref == qurtoba_record_id`).

---

## Ports & URLs (local dev)

| System    | Port   | Base URL                  |
|-----------|--------|---------------------------|
| Qurtoba   | `6000` | `http://localhost:6000`   |
| Cash-SYS  | `7000` | `http://localhost:7000`   |
| Genie ERP | `8000` | `http://localhost:8000`   |

---

## Tokens

Two separate tokens — different users, different permissions:

| Env var | Cash-SYS user | What it's used for |
|---|---|---|
| `CASH_SYS_TOKEN` | `system_integration` | POST cash orders, GET catalog |
| `CASH_SYS_CLIENT_TOKEN` | `genie_integration` | Create clients, activate trials |

### Genie `.env`

```ini
CASH_SYS_BASE_URL=http://localhost:7000
CASH_SYS_TOKEN=31a35a02a079b6cb0392b89e02f9355814890784
CASH_SYS_CLIENT_TOKEN=82909783bf03fb0fe207b21d5364f18b1b6671e1
CASH_SYS_WEBHOOK_SECRET=0db582b278b71923ed9a481575d367d7ca048aa23cdc9cf051a353aed051e5d6
```

---

## API 1 — Pull Plans & VIP Pages (Catalog)

**Purpose:** Cache the available pricing plans and VIP page options locally in Genie so they can be shown in dropdowns without a live call every time.

```
GET /api/v1/integration/catalog/
Authorization: Token CASH_SYS_TOKEN
```

**Response:**
```json
{
  "plans": [
    {
      "id": 1,
      "name": "Starter",
      "type": "plan",
      "price": "0.00",
      "device_limit": 5,
      "sim_limit": 10,
      "account_limit": 5,
      "is_active": true,
      "vip_pages": [
        { "id": 1, "name": "All Devices", "key": "all_devices", "price": "0.00" }
      ]
    }
  ],
  "vip_pages": [
    { "id": 1, "name": "All Devices", "key": "all_devices", "price": "0.00" }
  ]
}
```

**Genie implementation:**

```python
from qurtoba.tasks import pull_cash_sys_catalog_task

# Run now (async via Celery):
pull_cash_sys_catalog_task.delay()
```

Cached in `CashSysPlan` and `CashSysVipPage` tables. Refreshed daily at 03:00 AM via Celery Beat (`pull-cash-sys-catalog`). Also fires once on server startup via `apps.py ready()`.

---

## API 2 — Create Client

**Purpose:** Register a new customer in Cash-SYS so they can use the platform.

```
POST /api/v1/cashstuff/clients/
Authorization: Token CASH_SYS_CLIENT_TOKEN
Content-Type: application/json
```

**Request body:**

| Field | Required | Description |
|---|---|---|
| `code` | ✅ | Unique client code (e.g. device_no from Qurtoba) |
| `name` | ✅ | Client full name |
| `company` | ✅ | Company / shop name |
| `phone` | ✅ | 11-digit Egyptian mobile number starting with `01` |
| `district` | ❌ | Area / district |
| `governorate` | ❌ | City / governorate |
| `address` | ❌ | Full address |
| `notes` | ❌ | Free notes |

```json
{
  "code":        "101",
  "name":        "Ahmed Ali",
  "company":     "Ahmed Shop",
  "phone":       "01012345678",
  "district":    "Maadi",
  "governorate": "Cairo",
  "address":     "123 Test Street",
  "notes":       ""
}
```

**Response (201):** Full client snapshot including `id` — store this `id` for Step 3.

```json
{ "id": 80, "name": "Ahmed Ali", "phone": "01012345678", "stage": "new", ... }
```

**Genie implementation:**

```python
from qurtoba.utils_sync import create_cash_sys_client

client_data, err = create_cash_sys_client({
    "code":    "101",
    "name":    "Ahmed Ali",
    "company": "Ahmed Shop",
    "phone":   "01012345678",
})
# client_data['id'] → use in activate_cash_sys_trial()
```

---

## API 3 — Activate Free Trial

**Purpose:** Give the newly created client 14 days of free access. Creates their tenant account automatically if they don't have one yet.

```
POST /api/v1/cashstuff/clients/<client_id>/activate-trial/
Authorization: Token CASH_SYS_CLIENT_TOKEN
```

No request body needed.

**Response (200):**

```json
{
  "id": 80,
  "stage": "trial",
  "trial_status": "active",
  "trial_started_at": "2026-05-17T16:40:55.979981+00:00",
  "account_created": true,
  "phone": "01012345678",
  "password": "123456"
}
```

`phone` + `password` are only returned when `account_created: true` (first activation). **Store the password — it is not shown again.**

**Genie implementation:**

```python
from qurtoba.utils_sync import activate_cash_sys_trial

trial_data, err = activate_cash_sys_trial(client_id=80)
# trial_data['password'] → shown only on first activation
```

---

## Full Flow — Create + Activate in One Shot

```python
from qurtoba.utils_sync import create_cash_sys_client, activate_cash_sys_trial

# Step 1 — create client
client_data, err = create_cash_sys_client({
    "code": "101", "name": "Ahmed Ali",
    "company": "Ahmed Shop", "phone": "01012345678",
})
if err:
    # handle error
    pass

# Step 2 — activate free trial
trial_data, err = activate_cash_sys_trial(client_data['id'])
if err:
    # handle error (e.g. trial_already_active, trial_exhausted)
    pass

# Credentials (only on first activation)
password = trial_data.get('password')   # None if account already existed
```

---

## Trial Lifecycle — Verified from Cash-SYS Source

```
Day 0    ── activate-trial called ──►  stage: trial
                                        full access (5 devices, 10 SIMs, 5 accountants)
                                        VIP pages: all_devices, ask_num, transactions, sim_tracker

Day 14   ── trial expires ──────────►  stage: trial (expired)
                                        access blocked (TrialEnforcementMiddleware → 402)
                                        data still intact (hold period)

Day 28   ── tombstone runs ─────────►  stage: deleted
                                        ALL data deleted:
                                          SIMs, Devices, Transactions, Deposits
                                          Invoices, Collections, Tickets
                                        Admin user deactivated
                                        All accountant users deactivated

                                        ⚠ Safety: if client has ANY non-cancelled invoice
                                          → tombstone is SKIPPED (paying customer)
```

**Source:** `tombstone_expired_trials.py` — `grace_cutoff = now() - timedelta(days=TRIAL_DAYS * 2)`

**Default limits during trial** (from `settings.TRIAL_PLAN`):

| Resource | Limit |
|---|---|
| Devices | 5 |
| SIMs | 10 |
| Accountants | 5 |
| All Devices page | ✅ |
| Ask Number page | ✅ |
| Transactions page | ✅ |
| Statistics page | ❌ |
| SIM Tracker page | ✅ |

> **Note:** Payment, plan upgrades, and invoicing after the trial are handled entirely inside Cash-SYS. Genie only handles the create + activate steps above.

---

## Error Responses

| HTTP | `error` key | Meaning |
|---|---|---|
| `400` | `trial_already_active` | Trial was already activated for this client |
| `400` | `trial_exhausted` | Trial expired and was exhausted — cannot reactivate |
| `400` | `no_user_plan` | Internal issue — client has no UserPlan row |
| `401` | — | Invalid token |
| `403` | — | Token valid but missing `clients.create` or `trials.activate` permission |
| `404` | — | Client ID not found |

---

## Order Cancellation — reasons & effects

When a Cash-SYS order is canceled it fires an `order_canceled` webhook to Genie carrying
`cancel_reason` (and `reroute`, which is `true` **only** for `number_limit`). Genie reacts per reason:

| `cancel_reason` | Set by | Ledger effect on Genie | WhatsApp to customer |
|---|---|---|---|
| `customer` | customer self-cancel (`me/orders/<id>/cancel/`) | none — marked canceled only | none |
| `agent` | sub-user / agent (`orders/<id>/cancel/`) | none — marked canceled only | none |
| `number_limit` | system (recipient hit receive limit) | settle at amount **sent**; reroute the remainder | «الرقم تجاوز الحد… محتاجين رقم تانى» |
| `no_wallet` *(new)* | agent | **value → 0** (full reversal; Rest drops by the full value) | «محتاجين رقم تانى… الرقم مش عليه محفظة» |
| `cancel_request` *(new)* | agent | **value → 0** (full reversal) | «تم الغاء التحويل… لم يتم تسجيل العمليه عليك» |

**`no_wallet` and `cancel_request` are restricted to a pristine main order** — a chain root with no
child orders and no transactions (nothing was sent). Cash-SYS rejects them with **HTTP 400** otherwise
(`apps/customer/services.py` → `_is_pristine_main_order`, enforced in the `cancel` API action). This
keeps «لم يتم تسجيل العمليه عليك» truthful: the full debt is zeroed on the accountant ledger
(port 6000) via `edit_qurtoba_record_value(id, 0)`, then the balance is re-pulled from Qurtoba.

Genie handler: `tasks.py` → `handle_cash_sys_order_canceled` → `_apply_zero_cancel` + `_send_cancel_notice`.

---

## Smoke Test

```bash
# 1. Catalog
curl http://localhost:7000/api/v1/integration/catalog/ \
  -H "Authorization: Token 31a35a02a079b6cb0392b89e02f9355814890784"

# 2. Create client
curl -X POST http://localhost:7000/api/v1/cashstuff/clients/ \
  -H "Authorization: Token 82909783bf03fb0fe207b21d5364f18b1b6671e1" \
  -H "Content-Type: application/json" \
  -d '{"code":"101","name":"Test","company":"Test Co","phone":"01012345678"}'

# 3. Activate trial (replace 80 with returned id)
curl -X POST http://localhost:7000/api/v1/cashstuff/clients/80/activate-trial/ \
  -H "Authorization: Token 82909783bf03fb0fe207b21d5364f18b1b6671e1"
```
