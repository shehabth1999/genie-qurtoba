# Qurtoba Module — Complete Technical Reference

**Module path:** `E:\genie-erp\projects\qurtoba\`  
**Django app label:** `qurtoba`  
**Type:** External Genie ERP extension  
**Source verified from:** `E:\Qurtoba\old sys\qurtobaSys_django\` (read directly)  
**Last updated:** May 2026 (Step 1 complete)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Auth & Tokens](#2-auth--tokens)
3. [Balance Calculation — Verified Formula](#3-balance-calculation--verified-formula)
4. [Transaction Types](#4-transaction-types)
5. [Models](#5-models)
6. [Serializers — camelCase Mapping](#6-serializers--camelcase-mapping)
7. [Push Endpoints (Qurtoba → Genie)](#7-push-endpoints-qurtoba--genie)
8. [Pull Proxy Endpoints (Genie → Qurtoba)](#8-pull-proxy-endpoints-genie--qurtoba)
9. [Views & Menu Structure](#9-views--menu-structure)
10. [Form Views Reference](#10-form-views-reference)
11. [onchange Registry](#11-onchange-registry)
12. [Chat / WhatsApp Extension](#12-chat--whatsapp-extension)
13. [Extensions](#13-extensions)
14. [Sync Flow](#14-sync-flow)
15. [Loop Prevention](#15-loop-prevention)
16. [Verified Facts from Qurtoba Source](#16-verified-facts-from-qurtoba-source)
17. [Known Limitations](#17-known-limitations)
18. [File Map](#18-file-map)

---

## 1. Architecture Overview

### Local ports (all three servers on same machine)

| Server | Port | URL |
|---|---|---|
| **Qurtoba** | `6000` | `http://localhost:6000` |
| **Cash-SYS** | `7000` | `http://localhost:7000` |
| **Genie ERP** | `8000` | `http://localhost:8000` |

```
Qurtoba (localhost:6000)              Genie ERP (localhost:8000)
Django 3.x + DRF                      Django + Genie ERP framework
PostgreSQL                             PostgreSQL
─────────────────                      ─────────────────────────────
CustomerInfo  ──── push POST ────►  QurtobaCustomer
              ◄─── pull GET  ────    QurtobaCustomerAccount (parsed accounts)
Record        ──── push POST ────►  QurtobaRecord
              ◄─── pull GET  ────    (via proxy API views)
Rest (balance) ◄── pull GET  ────    QurtobaCustomer.balance
                                      base.Partner (extended with qurtoba_customer FK)
                                      chat.Conversation (extended with 4 action buttons)

Cash-SYS (localhost:7000)
  ◄── POST order   ──── Genie (after Qurtoba push succeeds, كاش types only)
  ──── webhook ────►   Genie POST /qurtoba/cash-sys/webhook/
                        (HMAC-signed, dispatched to Celery)
```

**Qurtoba pushes automatically** after every non-Genie write via `forward_to_genie()` background thread.  
**Genie pulls on demand** via proxy API views (React pages) and manual sync action.  
**Cash-SYS** is called by Genie directly (Qurtoba's own Cash-SYS call is skipped for Genie-pushed records).

---

## 2. Auth & Tokens

### Token map

| Direction | Token | Env var | File |
|---|---|---|---|
| Qurtoba → Genie push | `d67d0927d616dc084ad4b46ee6084a7c45595440` | `GENIE_TOKEN` | `Qurtoba/.env.local` |
| Genie → Qurtoba pull/push | `41bb06171932ddf17791985664082a194be0de80` | `QURTOBA_TOKEN` | `Genie/.env` |
| Genie → Cash-SYS orders + catalog | `31a35a02a079b6cb0392b89e02f9355814890784` | `CASH_SYS_TOKEN` | `Genie/.env` |
| Genie → Cash-SYS create client + trial | `82909783bf03fb0fe207b21d5364f18b1b6671e1` | `CASH_SYS_CLIENT_TOKEN` | `Genie/.env` |
| Cash-SYS → Genie webhook | `0db582b278b71923ed9a481575d367d7ca048aa23cdc9cf051a353aed051e5d6` | `CASH_SYS_WEBHOOK_SECRET` | `Genie/.env` |
| Qurtoba → Cash-SYS (Qurtoba-side) | `31a35a02a079b6cb0392b89e02f9355814890784` | `CASH_SYS_TOKEN` | `Qurtoba/.env.local` |

### Genie `.env` — Qurtoba + Cash-SYS block

```ini
QURTOBA_BASE_URL=http://localhost:6000
QURTOBA_TOKEN=41bb06171932ddf17791985664082a194be0de80

CASH_SYS_BASE_URL=http://localhost:7000
CASH_SYS_TOKEN=31a35a02a079b6cb0392b89e02f9355814890784        # orders + catalog (system_integration)
CASH_SYS_CLIENT_TOKEN=82909783bf03fb0fe207b21d5364f18b1b6671e1  # create client + activate trial (genie_integration)
CASH_SYS_WEBHOOK_SECRET=0db582b278b71923ed9a481575d367d7ca048aa23cdc9cf051a353aed051e5d6
```

### Qurtoba `.env.local` — Genie + Cash-SYS block

```ini
GENIE_BASE_URL=http://localhost:8000
GENIE_TOKEN=d67d0927d616dc084ad4b46ee6084a7c45595440

CASH_SYS_BASE_URL=http://localhost:7000
CASH_SYS_TOKEN=31a35a02a079b6cb0392b89e02f9355814890784
```

### Genie `project/settings.py` — reads from env

```python
QURTOBA_BASE_URL        = config('QURTOBA_BASE_URL', default='')
QURTOBA_TOKEN           = config('QURTOBA_TOKEN', default='')
CASH_SYS_BASE_URL       = config('CASH_SYS_BASE_URL', default='')
CASH_SYS_TOKEN          = config('CASH_SYS_TOKEN', default='')          # orders + catalog
CASH_SYS_CLIENT_TOKEN   = config('CASH_SYS_CLIENT_TOKEN', default='')   # create client + trial
CASH_SYS_WEBHOOK_SECRET = config('CASH_SYS_WEBHOOK_SECRET', default='')
```

### Token → user mapping (Cash-SYS)

| Token env var | Cash-SYS user | Permissions |
|---|---|---|
| `CASH_SYS_TOKEN` | `system_integration` | POST orders, GET catalog |
| `CASH_SYS_CLIENT_TOKEN` | `genie_integration` | `clients.create` + `trials.activate` only |

**Qurtoba service account in Genie:** `qurtoba_sync` (DRF Token = Token A above)  
**Genie service account in Qurtoba:** `genie_erp` / `genie_erp_2026` — `is_genie=True` (DRF Token = Token B above)

---

## 3. Balance Calculation — Verified Formula

Confirmed from `transactions/api/serializations/ser_transactions.py` (`SRecordSets.save()`):

```python
debit   = SUM(value WHERE customerData=X, isDown=False, isDone=False)
credit  = SUM(value WHERE customerData=X, isDown=True,  isDone=False)
balance = debit - credit
Rest.objects.update_or_create(customer_id=X, defaults={'value': balance})
```

**Critical rules (confirmed from Qurtoba source):**

| Rule | Detail |
|---|---|
| `isDone=False` only | `isDone=True` records excluded |
| Balance == 0 trigger | ALL records for customer → `isDone=True` (bulk reconciliation) |
| `isSeller` irrelevant | تحصيل reduces customer balance immediately |
| `rest` on Record IS a DB field | Stored as snapshot of balance at time of record |
| `grade` is advisory | No enforcement in Qurtoba; enforced in Genie via onchange |

**Genie implementation** (`QurtobaCustomer.recompute_balance()`):
- Called automatically from `QurtobaRecord.save()` after every record save
- Also stamps `record.rest = customer.balance` (snapshot)
- Handles `isDone` bulk flip when balance reaches 0

---

## 4. Transaction Types

### Debt types — `isDown=False` (adds to balance)
`كاش` · `كاش(5)` · `كاش(10)` · `كاش(20)` · `فورى` · `أمان` · `طاير` · `مصاريف خدمه`

### Payment types — `isDown=True` (reduces balance)
| Type | `isSeller` | Origin |
|---|---|---|
| `تحصيل` | True | Collector mobile app → Qurtoba push only |
| `شراء كاش` | False | Customer self-pay / Genie form |
| `شراء فورى` | False | Customer self-pay / Genie form |
| `مندوب` | True | Collector-dues API → Qurtoba push only |

**Genie form availability:**
- Debt forms: debt types only
- Collection forms: `شراء كاش` and `شراء فورى` only
- `تحصيل` and `مندوب`: display only (never selectable in Genie create forms)

---

## 5. Models

### `QurtobaCustomer`

| Field | Type | Notes |
|---|---|---|
| `qurtoba_id` | IntegerField(unique, null) | Qurtoba CustomerInfo.pk |
| `name` | CharField | Auto-formatted `"Name (device_no)"` |
| `balance` | FloatField | مديونيات — recomputed after every record save |
| `grade` | IntegerField | Credit tier: grade×1000 = advisory limit |
| `device_no` | IntegerField | Unique internal code from Qurtoba |
| `phone_no` | CharField | Primary upsert key |
| `shop_kind` | CharField | One of 8 Qurtoba choices (or integer "1" due to Qurtoba default bug) |
| `seller_qurtoba_id` | IntegerField | Qurtoba MandopInfo.pk (raw) |
| `accounts` | TextField | CSV: `"فورى,6081844,أمان,970604,"` |
| `accounts_data` | TextField | Historical accounts log |

**Actions:** `action_sync_customers` · `action_update_balance` · `action_new_transaction` · `action_new_collection` · `action_view_transactions`  
**onchange:** `name` → `_onchange_format_name` (auto-formats to "Name (device_no)")

---

### `QurtobaCustomerAccount`

Parsed from `QurtobaCustomer.accounts` CSV. One row per `(type, number)` pair.

| Field | Type |
|---|---|
| `customer` | FK → QurtobaCustomer (CASCADE, related_name='account_entries') |
| `type` | CharField (e.g. فورى, أمان, كاش) |
| `account_number` | CharField |

**unique_together:** `(customer, type, account_number)`  
**Auto-synced** on every customer push and manual sync via `_sync_customer_accounts()`.  
**Seeded:** 672 entries from 494 customers on initial migration.  
**Parser:** `_parse_accounts(accounts_str)` — splits on comma, identifies type by Arabic prefix.

---

### `QurtobaRecord`

| Field | Type | Notes |
|---|---|---|
| `customer` | FK → QurtobaCustomer | Real relation (resolved from `customer_data_qurtoba_id`) |
| `customer_data_qurtoba_id` | IntegerField | Raw Qurtoba CustomerInfo.pk |
| `selected_account` | FK → QurtobaCustomerAccount | Genie-side account selector — NOT in سداد form |
| `type` | CharField(choices=TYPE_CHOICES) | See type reference |
| `value` | FloatField | Transaction amount |
| `rest` | FloatField | **DB field** — snapshot of customer balance after save |
| `is_done` | BooleanField | Never set manually; auto-flipped when balance=0 |
| `is_down` | BooleanField | False=debt, True=payment |
| `is_seller` | BooleanField | No effect on customer balance |
| `raw_data` | JSONField | Full incoming payload (debug) |

**Properties (no DB column):**
- `grade_limit` — `customer.grade × 1000`
- `customer_balance` — `customer.balance`
- `extends_by` — `max(0, customer_balance + value − grade_limit)`

**`save()` override** (runs on every save — form UI, Qurtoba push, sync):
1. `value = 0` if null/empty (no blocking — external pushes pass through)
2. `date = today` if not provided (auto-set, field removed from quick form schemas)
3. `super().save()` — persists the record
4. `customer.recompute_balance()` — recalculates مديونيات from all active records
5. Stamps `rest = customer.balance` (snapshot at time of save, mirrors Qurtoba behaviour)

**onchanges:**
- `customer` → `_onchange_customer_grade_info`: pushes `grade_limit`, `customer_balance`, domain for `selected_account`
- `selected_account` → `_onchange_selected_account`: auto-fills `type` + `account_number`
- `value` → `_validate_grade_limit`: updates `customer_balance` (projected) + `extends_by` in footer

**Actions:** `action_new_debt` · `action_new_collection`

---

## 6. Serializers — camelCase Mapping

### `QurtobaCustomerSerializer`
| Qurtoba (camelCase) | Genie (snake_case) | Note |
|---|---|---|
| `surName` | `sur_name` | |
| `shopName` | `shop_name` | |
| `shopKind` | `shop_kind` | CharField; Qurtoba has `default=1` bug — accepts any value |
| `deviceNo` | `device_no` | |
| `phoneNo` | `phone_no` | |
| `seller` (int or dict) | `seller_qurtoba_id` | `_fk_id()` handles both int and `{id,name}` |
| `assistant` | `assistant_qurtoba_id` | |
| `areas` | `areas_qurtoba_id` | |

### `QurtobaRecordSerializer`
| Qurtoba | Genie | Note |
|---|---|---|
| `accountNumber` | `account_number` | |
| `isDone` | `is_done` | |
| `isDown` | `is_down` | |
| `isSeller` | `is_seller` | |
| `customerData` | `customer_data_qurtoba_id` | |
| `seller` | `seller_qurtoba_id` | |
| `accountant` | `accountant_qurtoba_id` | |
| `datetime` | `datetime_field` | Renamed to avoid Python stdlib clash |

**`schema_processor.py` fix:** `process_select_fields()` skips fields that already have `options` set — custom view options are never overridden by model choices.

**Quick form date handling:** `date` field removed from all quick form schemas. Auto-set to `timezone.now().date()` in `QurtobaRecord.save()` when not provided.

---

## 7. Push Endpoints (Qurtoba → Genie)

Auth: `Authorization: Token d67d0927...`

| URL | Handler | Behaviour |
|---|---|---|
| `POST /customers/api2/customers/` | `QurtobaCustomerListView` | Upsert by `phone_no` + sync accounts |
| `POST /customers/api2/customers/<pk>/` | `QurtobaCustomerDetailView` | Upsert by `qurtoba_id`→`phone_no`, backfill ID + sync accounts |
| `POST /transactions/api2/record/` | `QurtobaRecordListView` | Create record, resolve customer FK, trigger `recompute_balance()` |

---

## 8. Pull Proxy Endpoints (Genie → Qurtoba)

Auth: `Authorization: Token 41bb0617...`

| Genie URL | Qurtoba URL | Used by |
|---|---|---|
| `GET /qurtoba/api/customer-dues/` | `/transactions/api2/dues/` | React page |
| `GET /qurtoba/api/seller-dues/` | `/transactions/api2/collector-dues/` | React page |
| `GET /qurtoba/api/seller-transactions/` | `/transactions/api2/transactions_collector/` | React drill-down |
| `POST /qurtoba/api/seller-settle/` | `POST /transactions/api2/collector-dues/?id=&value=` | React سداد مبلغ |
| `GET /qurtoba/api/accountant-report/` | `/transactions/api2/reports/` | React page |
| `GET /qurtoba/api/delayed/` | `/transactions/api2/rest/` | React page |

**Inertia page views:**
`/qurtoba/customer-dues/` · `/qurtoba/seller-dues/` · `/qurtoba/accountant-report/` · `/qurtoba/delayed/`

---

## 9. Views & Menu Structure

```
قرطبة  (sequence 90)
│
├── العملاء                          model=qurtobacustomer, view_types="list,form"
│
├── المعاملات  (group)
│   ├── تسجيل مديونية               domain: isDown=False  |  context: isDown=False
│   ├── التحصيلات                   domain: isDown=True+isSeller=True
│   └── جميع المعاملات
│
└── التقارير  (group)
    ├── مستحقات العملاء             url: /qurtoba/customer-dues/
    ├── مستحقات المناديب            url: /qurtoba/seller-dues/
    ├── تقارير المحاسب              url: /qurtoba/accountant-report/
    └── المتأخرات                   url: /qurtoba/delayed/

[hidden is_visible=False — used as menu_item_key in action slideovers]
  qurtoba_action_quick_debt
  qurtoba_action_quick_collection
  qurtoba_action_quick_transaction
```

---

## 10. Form Views Reference

| Key | Model | Menu item | Purpose |
|---|---|---|---|
| `qurtoba_customer_list_view` | qurtobacustomer | customers | List with sync button |
| `qurtoba_customer_form_view` | qurtobacustomer | customers | Full form: balance, partners smart button, tabs |
| `qurtoba_record_list_view` | qurtobarecord | records | All records, all type options |
| `qurtoba_record_form_view` | qurtobarecord | records | General edit form |
| `qurtoba_debt_list_view` | qurtobarecord | debt | Debt records (isDown=False) |
| `qurtoba_debt_form_view` | qurtobarecord | debt | Debt creation — debt types |
| `qurtoba_collection_list_view` | qurtobarecord | collections | Collection records |
| `qurtoba_collection_form_view` | qurtobarecord | collections | Collection recording |
| `qurtoba_quick_debt_form_view` | qurtobarecord | action_quick_debt | **Slim** — customer+account+type+value+footer |
| `qurtoba_quick_collection_form_view` | qurtobarecord | action_quick_collection | **Slim** — customer+account+type+value+footer |
| `qurtoba_quick_transaction_form_view` | qurtobarecord | action_quick_transaction | **Slim** — WhatsApp action form with account |

### Quick form layouts

**`qurtoba_quick_debt_form_view`** (مديونية جديدة — from customer form):
```
Title:   customer (relation, onChange → filters account dropdown + grade info)
Body:    selected_account* | account_number (always visible; readonly when account set)
         type (select, _DEBT_OPTIONS, never readonly) | value (number, onChange → live footer)
Hidden:  is_down=False, is_seller=False
Footer:  الرصيد الحالي (projected = current+value) | الحد الائتماني | تجاوز بـ
Auto:    date = today (set in save(), not in form)
```

**`qurtoba_quick_collection_form_view`** (سداد — from customer form + chat):
```
Title:   customer (relation, onChange → grade info)
Body:    type (select, _CUSTOMER_PAY_OPTIONS: شراء كاش / شراء فورى) | value (number)
Hidden:  is_down=True, is_seller=False
Footer:  الرصيد الحالي | الحد الائتماني | تجاوز بـ
Auto:    date = today (set in save(), not in form)
Note:    NO selected_account or account_number fields in this form
```

**`qurtoba_quick_transaction_form_view`** (عملية جديدة — from chat panel):
```
Title:   customer (relation, onChange → filters account dropdown + grade info)
Body:    selected_account* | account_number (always visible; readonly when account set)
         type (select, _DEBT_OPTIONS, never readonly) | value (number, onChange → live footer)
Hidden:  is_down=False, is_seller=False
Footer:  الرصيد الحالي (projected) | الحد الائتماني | تجاوز بـ
Auto:    date = today (set in save(), not in form)
```

**\* `selected_account` conditional rules:**
- `invisible` when `type in [كاش, كاش(5), كاش(10), كاش(20)]` — hidden for cash, visible for other types
- When cleared → `account_number` also cleared to null via `_onchange_selected_account`
- `account_number`: `readonly` when `selected_account` is set; editable when empty or كاش type

**`QurtobaRecord.save()` auto-rules (apply to ALL saves):**
- `value` null/empty → set to `0` (no blocking, external pushes pass through)
- `date` null → set to `timezone.now().date()` (today)

---

## 11. onchange Registry

| Model | Field | Method | Effect |
|---|---|---|---|
| `qurtoba.qurtobacustomer` | `name` | `_onchange_format_name` | Formats to "Name (device_no)" |
| `qurtoba.qurtobarecord` | `customer` | `_onchange_customer_grade_info` | Pushes grade_limit, customer_balance, domain for selected_account |
| `qurtoba.qurtobarecord` | `value` | `_validate_grade_limit` | Updates projected balance + extends_by + inline error if over limit |
| `qurtoba.qurtobarecord` | `selected_account` | `_onchange_selected_account` | Auto-fills type + account_number when set; clears account_number when cleared |

All registered in `base_onchange` table.

---

## 12. Chat / WhatsApp Extension

### Buttons in conversation panel (chat_patch.py → inherits chat_main_menu_omnichannel)

| Button | Action | Opens | Direction | What it does |
|---|---|---|---|---|
| **عملية جديدة** | `action_qurtoba_new_transaction` | `qurtoba_action_quick_transaction` | isDown=False | Debt/transaction form with account selector |
| **سداد** | `action_qurtoba_new_debt` | `qurtoba_action_quick_collection` | isDown=True | Payment form (شراء كاش / شراء فورى) — reduces balance |
| **فحص الرصيد** | `action_qurtoba_check_balance` | — | — | Sends balance message on conversation via WhatsApp API |
| **المعاملات** | `action_qurtoba_transactions` | List slideover | — | **This customer only** (domain by customer.id) |

### `check_balance_and_send(conversation, customer)` — reusable utility

```python
from qurtoba.extensions import check_balance_and_send
check_balance_and_send(conversation, customer)
```

Uses `ChatBridgeService.send_message()` with:
- `system_partner` = internal agent (from conversation member → created_by → any staff)
- `websocket=True` → real-time frontend push
- `post_create()` → automatic dispatch via `social_account.handle_message()` (WhatsApp API)

### Customer resolution chain
```
conversation.social_partner.qurtoba_customer_id
  → QurtobaCustomer.objects.filter(pk=customer_id).first()
```
If no linked customer → error toast "هذه المحادثة غير مرتبطة بعميل قرطبة"

---

## 13. Extensions

### `PartnerQurtobaExtension` (extends `base.partner`)
- Adds `qurtoba_customer = FK(QurtobaCustomer, related_name='partners')`
- Column `qurtoba_customer_id` in `base_partner` table (added via schema sync)
- Displayed in customer form Partners tab + الشركاء smart button

### `ConversationQurtobaExtension` (extends `chat.conversation`)
- Adds 4 `@action` methods (no DB fields added)
- Registered via `_sync_components()` → server actions in DB

---

## 14. Sync Flow

### Qurtoba → Genie (automatic push)
```
User writes in Qurtoba (is_genie=False check)
  → forward_to_genie(path, data)   [daemon thread, retries: 0s→5s→15s→30s]
  → POST http://localhost:8000{same_path}   Authorization: Token d67d0927...
  → Genie saves → serializer maps camelCase
  → QurtobaRecord.save() → customer.recompute_balance()
  → isDone bulk flip if balance==0
  → rest stamped with new balance
  → _sync_customer_accounts() updates parsed account entries
```

### Manual sync
```
action_sync_customers()
  → _fetch_all_pages(/customers/api2/customers/)   [paginated, follows next]
  → upsert by qurtoba_id → phone_no → create
  → _apply_name_format(name, device_no)
  → _sync_customer_accounts(obj)
  → _relink_orphaned_records()
```

### Balance refresh
```
action_update_balance()
  → GET /transactions/api2/rest-customer/<qurtoba_id>/
  → customer.balance = response.value   (Qurtoba's authoritative Rest.value)
```

---

## 15. Loop Prevention

Qurtoba `genie_erp` account: `is_genie=True`
```
Genie reads/writes Qurtoba → using genie_erp account
  → Qurtoba saves locally
  → is_genie=True → forward_to_genie() NOT called
  → No loop
```

---

## 16. Verified Facts from Qurtoba Source

All items below confirmed by reading `E:\Qurtoba\old sys\qurtobaSys_django\` directly:

| Claim | Source | Status |
|---|---|---|
| Balance formula with `isDone=False` filter | `ser_transactions.py` SRecordSets.save() | ✅ Confirmed |
| `isDone` NEVER auto-flipped on balance=0 | `transactions.py` — no such code | ✅ Confirmed (agent was wrong) |
| `rest` IS a DB FloatField on Record | `transactions/models.py` | ✅ Confirmed (agent was wrong) |
| `shopKind` has integer default bug (`default=1`) | `customers/models.py` | ✅ Confirmed |
| `grade` is advisory, no enforcement | `customers/models.py` | ✅ Confirmed |
| Forward retries: 0s → 5s → 15s → 30s | `main/utils/genie.py` | ✅ Confirmed |
| `is_genie` field on Account | `account/models.py` | ✅ Confirmed |

---

## 17. Cash-SYS Catalog Integration (Pricing Plans Pull)

### Overview

Genie pulls Cash-SYS pricing plans and VIP pages periodically and caches them locally.
This data is used as **AI agent context only** — no UI, no business logic depends on it.

### Endpoint (Cash-SYS side)

| Method | URL | Auth |
|--------|-----|------|
| `GET` | `{CASH_SYS_BASE_URL}/api/v1/integration/catalog/` | `Token {CASH_SYS_TOKEN}` |

Returns:
```json
{
  "plans": [
    {
      "id": 9, "name": "الخطة الأساسية", "type": "plan",
      "price": "500.00", "device_limit": 5, "sim_limit": 10, "account_limit": 2,
      "is_active": true,
      "vip_pages": [{"id": 17, "name": "استعلام الأرقام", "key": "ask_num", "price": "30.00"}, ...]
    }, ...
  ],
  "vip_pages": [
    {"id": 17, "name": "استعلام الأرقام", "key": "ask_num", "price": "30.00"}, ...
  ]
}
```

### Genie-side storage

| Model | Table | Purpose |
|-------|-------|---------|
| `CashSysPlan` | `qurtoba_cashsysplan` | One row per plan, vip_pages stored as JSON |
| `CashSysVipPage` | `qurtoba_cashsysvippage` | One row per VIP page |

Both tables are **full-replaced** on every sync (delete all → bulk insert).

### Celery task

```python
from qurtoba.tasks import pull_cash_sys_catalog_task
pull_cash_sys_catalog_task.delay()          # on demand
pull_cash_sys_catalog_task.apply_async()    # same
```

Schedule via Celery beat (add to `CELERY_BEAT_SCHEDULE`):
```python
'pull-cash-sys-catalog': {
    'task': 'qurtoba.tasks.pull_cash_sys_catalog_task',
    'schedule': 6 * 3600,  # every 6 hours
},
```

### User Registration (2-step, via Cash-SYS API)

| Step | Method | URL | Body |
|------|--------|-----|------|
| 1 — Create account | `POST` | `.../api/v1/integration/register/` | `phone_number, username, first_name, last_name` |
| 2 — Activate trial | `POST` | `.../api/v1/integration/register/<user_id>/activate/` | `plan_id` |

Step 1 returns `{ user_id, username, password, phone_number }`.
Step 2 returns `{ user_id, username, phone_number, plan_name, plan_type, trial_started_at }`.

Auth token used: `CASH_SYS_TOKEN` (same token used for order posting).

---

## 18. Known Limitations (unchanged)

1. **`isDone` bulk flip** — Genie implements this (when balance=0, all records→isDone=True) but Qurtoba itself never does this in `createRecord`. The flip happens in Qurtoba only via manual archive. Genie's implementation is a reasonable approximation but may diverge.

2. **Seller/MandopInfo model** — no local model; stored as raw integer IDs. Relation widget for seller not available until MandopInfo is synced.

3. **Transaction types** — `TYPE_CHOICES` is a best-effort list. Qurtoba's `type` field is a plain CharField with no model-level choices enforcement; other types may exist in production data.

4. **`shopKind` integer values** — some old Qurtoba records have `shopKind=1` (integer) due to the `default=1` bug. These are stored as-is in Genie.

---

## 18. File Map

```
E:\genie-erp\projects\qurtoba\
├── models.py               QurtobaCustomer, QurtobaCustomerAccount,
│                           QurtobaRecord, helpers, actions, onchanges
├── serializers.py          camelCase→snake_case push serializers
├── views.py                Push API views + Inertia pages + proxy API views
├── urls.py                 All URL patterns
├── extensions.py           PartnerQurtobaExtension, ConversationQurtobaExtension,
│                           check_balance_and_send(), _get_system_partner()
├── apps.py                 loads extensions on startup
├── admin.py                Both models registered
├── DOCS.md                 This file — technical reference
├── BUSINESS.md             Business requirements, user stories, business logic
├── migrations/
│   ├── 0001_initial.py
│   ├── 0002_qurtobacustomer_balance.py
│   ├── 0003_alter_qurtobacustomer_options_and_more.py
│   ├── 0004_alter_qurtobarecord_type.py
│   └── 0005_qurtobacustomeraccount_and_more.py
├── pages/                  React TSX pages (Vite discovers via EXTENSIONS_PATHS)
│   ├── CustomerDues/index.tsx
│   ├── SellerDues/index.tsx
│   ├── AccountantReport/index.tsx
│   └── DelayedCustomers/index.tsx
└── ui/
    ├── views/
    │   ├── views.py        All 11 view definitions + choice lists
    │   └── batch.py        (empty — chat buttons via extensions, not batch)
    └── menu_items/
        ├── menu_items.py   Main 3-level menu hierarchy (menu_dict)
        └── chat_patch.py   Inherits chat_main_menu_omnichannel → appends 4 buttons

Genie core files modified:
  genie/.env                              QURTOBA_BASE_URL + QURTOBA_TOKEN
  genie/project/settings.py              reads those env vars
  genie/project/urls.py                  includes qurtoba.urls
  genie/modules/base/registry/menu_item_registry.py  line 132: encoding='utf-8'
  genie/modules/base/utils/schema_processor.py       respects existing options
```
