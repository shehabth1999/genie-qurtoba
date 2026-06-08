# Qurtoba ↔ Genie ERP — Business Requirements & User Stories

**System:** Qurtoba financial management integrated into Genie ERP  
**Business:** Egyptian B2B sales/distribution company  
**Operators:** Accountants, collectors (مناديب), supervisors, customer service agents

---

## Business Context

Qurtoba is a standalone financial system used daily to manage customer credit sales.
Genie ERP is the company's main CRM/communication platform (WhatsApp, chat, sales).

The integration goal: make financial data visible to customer-facing staff inside Genie
without requiring them to switch systems, and allow them to record transactions directly
from a customer conversation.

---

## Actors

| Actor | Role | System |
|---|---|---|
| Accountant (محاسب) | Creates debt records, runs reports | Qurtoba + Genie |
| Collector (محصل / مندوب) | Visits customers, collects cash | Qurtoba mobile app |
| Customer service agent | Handles WhatsApp conversations | Genie chat |
| Supervisor | Monitors balances, delays, reports | Genie (read pages) |

---

## User Stories

### US-01 — Real-time customer sync
**As a** Genie user  
**I want** to see up-to-date customer data from Qurtoba  
**So that** I don't need to switch to Qurtoba to look up a customer  

**Acceptance criteria:**
- Every CustomerInfo create/update in Qurtoba → automatically synced to Genie within seconds
- Manual "مزامنة العملاء" button on customer list forces full re-sync
- Customer name stored as "Name (device_no)" for easy identification (e.g. "Ahmed Salah (133)")

---

### US-02 — View customer مديونيات (outstanding balance)
**As a** Genie user  
**I want** to see a customer's outstanding balance at a glance  
**So that** I can assess credit risk before taking an order or starting a conversation  

**Acceptance criteria:**
- `مديونيات` shown on customer list view and prominently in the form header (readonly)
- Balance = SUM(debt records) − SUM(payment records) where isDone=False
- Balance updates automatically after every transaction save
- When balance reaches 0 → all records flip to isDone=True (Qurtoba reconciliation behaviour)

---

### US-03 — Grade / credit limit enforcement
**As an** accountant  
**I want** to be warned when a new transaction will exceed the customer's credit limit  
**So that** I don't accidentally over-extend credit  

**Acceptance criteria:**
- `grade × 1000 = credit ceiling` (grade is advisory, set by management)
- When creating a debt transaction, if `balance + amount > grade × 1000`:
  - Inline red error shown on the amount field
  - Footer shows: الرصيد الحالي | الحد الائتماني | تجاوز بـ
  - Footer updates live as user types the amount
- Projected balance shown live in الرصيد الحالي footer field

---

### US-04 — Record a debt transaction (مديونية)
**As an** accountant  
**I want** to register a new credit sale for a customer directly in Genie  
**So that** I don't need to open Qurtoba separately  

**Acceptance criteria:**
- Form shows: customer selector, account selector (from customer's registered payment accounts), type (debt types only), amount, date
- Account selector (`الحساب`) filters to current customer's accounts only
- Selecting an account auto-fills type and account number (both clear when account is deselected)
- `الحساب` field hidden when type is any كاش variant (cash transactions have no account)
- `رقم الحساب` always visible; editable when no account selected or كاش type; readonly when account selected
- `النوع` field always editable — user can change type even after account auto-fills it
- Transaction saved to Genie DB and automatically forwarded to Qurtoba via the existing push channel
- Customer balance updates immediately after save

---

### US-05 — Record a customer payment (شراء كاش / شراء فورى)
**As an** accountant  
**I want** to record when a customer pays directly (online payment)  
**So that** their balance is reduced accurately  

**Acceptance criteria:**
- Separate "تسجيل تحصيل" form with only customer self-payment types (شراء كاش, شراء فورى)
- `is_down=True, is_seller=False` set automatically (hidden from user)
- Balance reduces immediately after save

---

### US-06 — View customer transaction history
**As any** Genie user  
**I want** to see all transactions for a specific customer  
**So that** I can understand their payment history  

**Acceptance criteria:**
- Transactions tab on customer form shows all linked records
- "المعاملات" action button on customer form opens slideover list filtered to this customer
- Debt records and collection records separated by domain-filtered menu items

---

### US-07 — WhatsApp: check customer balance during conversation
**As a** customer service agent on WhatsApp  
**I want** to check a customer's balance without leaving the chat  
**So that** I can answer financial questions instantly  

**Acceptance criteria:**
- "فحص الرصيد" button visible in the WhatsApp conversation panel
- Pressing it sends an auto-message on the conversation showing the balance details
- Message sent via WhatsApp API in real-time (WebSocket push, no page refresh)
- Message shows: customer name, مديونيات, credit limit, remaining/overage
- Conversation must have a linked QurtobaCustomer via social_partner.qurtoba_customer

---

### US-08 — WhatsApp: create transaction from conversation
**As a** customer service agent  
**I want** to record a transaction or payment directly from a WhatsApp conversation  
**So that** I don't need to navigate away from the chat  

**Acceptance criteria:**

| Button | Form | Direction | Use case |
|---|---|---|---|
| **عملية جديدة** | Quick transaction form (account selector) | isDown=False | Record a new debt/credit sale |
| **سداد** | Quick collection form (شراء كاش/شراء فورى) | isDown=True | Record a customer payment |

- Forms open as slideovers pre-filled with the conversation's linked customer
- Account selector visible on عملية جديدة (hidden for كاش types); absent from سداد form
- Grade limit enforced on عملية جديدة form
- After save: form closes automatically, parent view refreshes

---

### US-09 — Supervisor reports (read from Qurtoba API live)
**As a** supervisor  
**I want** to view live financial dashboards without leaving Genie  
**So that** I can monitor the business in one place  

**Acceptance criteria:**
Four read-only React pages that pull live data from Qurtoba API:

| Page | Content |
|---|---|
| مستحقات العملاء | All customer outstanding balances grouped by area, color-coded |
| مستحقات المناديب | Collector balances with drill-down + "سداد مبلغ" settle button |
| تقارير المحاسب | Date-range transaction summary with totals by type |
| المتأخرات | Overdue customers sorted by days, red highlight for >30 days |

---

### US-10 — Link WhatsApp contact to Qurtoba customer
**As a** Genie admin  
**I want** to link a WhatsApp contact (Partner) to their Qurtoba account  
**So that** conversation buttons can access their financial data  

**Acceptance criteria:**
- `qurtoba_customer` field visible on the Partner/contact form
- Partners tab on QurtobaCustomer form shows linked partners (smart button with count)
- If no qurtoba_customer linked, WhatsApp action buttons show an error toast

---

## Business Rules

### Balance Calculation
```
balance = SUM(value WHERE isDown=False AND isDone=False)
        - SUM(value WHERE isDown=True  AND isDone=False)

When balance == 0 → ALL records for this customer → isDone=True (reconciliation)
```
- Source of truth: Qurtoba's `Rest` table (one row per customer)
- Genie mirrors this via `QurtobaCustomer.recompute_balance()` called after every record save

### Transaction Types
- **isDown=False** (adds to debt): كاش, كاش(5), كاش(10), كاش(20), فورى, أمان, طاير, مصاريف خدمه
- **isDown=True, isSeller=False** (customer self-pays): شراء كاش, شراء فورى
- **isDown=True, isSeller=True** (collector collects): تحصيل — from mobile app only
- **مندوب**: collector settles with office — from collector-dues API only
- Types available in Genie create forms: debt form shows only debt types; collection form shows only شراء كاش/شراء فورى

### Account Numbers
- Stored as CSV in `CustomerInfo.accounts` field: `"فورى,6081844,أمان,970604,"`
- Parsed into `QurtobaCustomerAccount` rows (one per type-number pair)
- Account selector hidden for كاش types (cash transactions don't use accounts)
- Selecting an account auto-fills type and account_number via onchange

### Grade (Credit Limit)
- `grade × 1000 = credit ceiling`
- Advisory only — no enforcement in Qurtoba itself
- Enforced in Genie via form onchange with inline error
- Grade 6 → limit 6,000 | Grade 300 → limit 300,000

### Loop Prevention
- Qurtoba's `genie_erp` account has `is_genie=True` → writes from Genie never forwarded back
- Prevents infinite sync loop

---

## Step 2 — Planned Features (not yet built)

Based on current state, the following are identified as next steps:

1. **Seller/Collector model** — parse `MandopInfo` from Qurtoba and store locally, enabling proper relation widget instead of raw `seller_qurtoba_id` integer
2. **Transaction from Qurtoba → Genie → WhatsApp notification** — when a new transaction is recorded in Qurtoba, auto-notify the customer via WhatsApp
3. **Bidirectional balance correction** — use `action_update_balance` (fetches from Qurtoba's Rest API) to correct any drift between Genie's computed balance and Qurtoba's authoritative Rest.value
4. **Customer mobile app integration** — Qurtoba's customer app sends GET to view their own transactions; surface this in Genie for agents
5. **Collector settlement page** — "سداد مبلغ" on مستحقات المناديب page (already wired to Qurtoba API)
