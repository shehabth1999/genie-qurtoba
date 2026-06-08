# Qurtoba 3-Server Integration — Wiring & TODO

How **Genie ERP**, the **Qurtoba accountant**, and **Cash-SYS** connect to each
other, every API token involved, and a per-server checklist to bring the whole
loop up from scratch.

> Verified against code on 2026-06-08. File:line refs point at the real auth /
> endpoint / webhook locations on each server.

---

## 1. The three servers


| #     | Server                                               | Dev URL                 | Role                                                                                                                       |
| ----- | ---------------------------------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **G** | **Genie ERP** (this app; hosts the `qurtoba` module) | `http://localhost:8000` | Customer-facing hub: WhatsApp AI agent, operator UI (فورى/أمان todo page), receipts. Orchestrates the cash cycle.          |
| **Q** | **Qurtoba accountant** (`qurtobaSys_django`)         | `http://localhost:6000` | Canonical **ledger** — source of truth for customers, records, and balances (Rest).                                        |
| **C** | **Cash-SYS** (`Cash-SYS`)                            | `http://localhost:7000` | Cash-transfer **automation** — executes كاش transfers (possibly across several partial sends) and reports back by webhook. |


**Two different cycles** ride on this topology:

- **كاش (cash) = automated cycle** — Genie → Qurtoba → Cash-SYS → (webhooks) → Genie. Fully hands-off.
- **فورى / أمان = manual cycle** — *no Cash-SYS*. An operator fulfils each transfer by hand on the new **account-tasks** page (`/qurtoba/account-tasks/`). Genie-local only; no external server call. This is the "accounts cycle" counterpart to the cash cycle.

---

## 2. Topology & tokens (every arrow is one credential)

```
                       ┌──────────────────────────────────────────────┐
                       │                  GENIE (G) :8000             │
                       │            qurtoba module + AI agent          │
                       └───┬───────────────▲───────────────┬──────────┘
        QURTOBA_TOKEN      │               │               │   CASH_SYS_TOKEN
        (push record,      │               │               │   (create cash order,
         edit value,       │               │               │    pull catalog)
         read balance)     │   GENIE_TOKEN │               │   CASH_SYS_CLIENT_TOKEN
                           │   (forward new│               │   (create client,
                           ▼   cust/record)│               ▼    activate trial)
        ┌──────────────────────────────┐   │   ┌──────────────────────────────┐
        │     QURTOBA ACCOUNTANT (Q)    │   │   │          CASH-SYS (C)         │
        │            :6000              │   │   │             :7000             │
        └───────────────┬──────────────┘   │   └───────────────┬──────────────┘
                        │  CASH_SYS_TOKEN   │                   │
                        │  (Q's OWN direct  │  GENIE_WEBHOOK_*  │
                        │   cash orders for │  (order_progress/ │
                        │   non-Genie recs) │   done/canceled,  │
                        ▼                   └───────────────────┘  HMAC-signed
                  CASH-SYS (C)              ◄── webhooks land back on G at
                                               POST /qurtoba/cash-sys/webhook/
```

### Credential matrix (6 secrets)


| Credential                                          | Stored on (env)                 | Identity on the target               | Generate with                                                               | Authorizes                                                              |
| --------------------------------------------------- | ------------------------------- | ------------------------------------ | --------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `QURTOBA_TOKEN`                                     | **G**                           | Q's `is_genie=True` user (DRF token) | on **Q**: `python manage.py create_genie_user`                              | G→Q: create/PATCH record, read balance, list customers/sellers          |
| `GENIE_TOKEN`                                       | **Q**                           | a Genie service user (DRF token)     | on **G**: `uv run python manage.py drf_create_token <svc_user>`             | Q→G: forward new customers/records into Genie                           |
| `CASH_SYS_TOKEN`                                    | **G** *and* **Q**               | C's `system_integration` user        | on **C**: `python manage.py setup_system_integration --admin-phone <phone>` | →C: `POST /api/v1/integration/orders/`, catalog pull                    |
| `CASH_SYS_CLIENT_TOKEN`                             | **G**                           | C's `genie_integration` user         | on **C**: `python manage.py setup_genie_integration`                        | G→C: `clients/` create + `activate-trial/`                              |
| `GENIE_WEBHOOK_SECRET` (=`CASH_SYS_WEBHOOK_SECRET`) | **C** *and* **G** (same string) | HMAC shared secret                   | `python -c "import secrets;print(secrets.token_hex(32))"`                   | sign/verify C→G webhooks                                                |
| `GENIE_WEBHOOK_URL`                                 | **C**                           | —                                    | n/a (it's a URL)                                                            | where C POSTs webhooks: `http://<genie>:8000/qurtoba/cash-sys/webhook/` |


> ⚠️ **Don't confuse the two directions.** `QURTOBA_TOKEN` (held by G) and
> `GENIE_TOKEN` (held by Q) are **different tokens on different servers** — one
> for each direction of the G↔Q sync. Same story is *not* needed for C↔G:
> G→C uses bearer tokens, C→G uses an **HMAC secret**, not a token.

---

## 3. The flows (what actually travels each wire)

### Flow A — Customer onboarding / trial (G → C)

`create_cash_sys_client()` → `POST /api/v1/cashstuff/clients/`, then
`activate_cash_sys_trial()` → `.../activate-trial/`. Auth: `**CASH_SYS_CLIENT_TOKEN`**.
Code: `qurtoba/utils_sync.py:222,261`.

### Flow B — Master data sync (Q ↔ G)

- Q is source of truth. New/updated customers & records on Q → `forward_to_genie()`
→ `POST {GENIE_BASE_URL}/customers/api2/customers/` and `/transactions/api2/record/`.
Auth: `**GENIE_TOKEN**`. (Q skips the forward when the caller `is_genie` → no loop.)
- G receives them: `qurtoba/views.py` `QurtobaCustomerListView`, `QurtobaRecordListView`.
- G can also bulk-pull: `action_sync_customers` → `GET /customers/api2/customers/`;
balances via `GET /transactions/api2/rest-customer/<id>/`. Auth: `**QURTOBA_TOKEN**`.

### Flow C — AI cash transfer (the automated cycle) — G → Q → C → G

1. WhatsApp customer asks → AI agent creates a `QurtobaRecord` (كاش type) in **G**.
2. `post_create` → `push_record_to_qurtoba()` → `POST /transactions/api2/record/` on **Q**
  (auth `QURTOBA_TOKEN`) → gets `qurtoba_record_id`, refreshes balance.
   `qurtoba/utils_sync.py:10`.
3. Because the record is Genie-pushed, **Q does NOT call Cash-SYS** (`if not is_genie`
  gate). Instead **G** calls it: `_send_to_cash_sys()` → `POST /api/v1/integration/orders/`
   on **C** with `external_ref = str(qurtoba_record_id)`. Auth `CASH_SYS_TOKEN`.
   `qurtoba/utils_sync.py:95`.
4. **C** executes the transfer(s) and fires webhooks back to **G** (HMAC `X-Cash-Signature`,
  header `X-Cash-Event`): `order_progress` (partial), `order_done` (settled),
   `order_canceled` (a part canceled). Lands at `POST /qurtoba/cash-sys/webhook/`
   → `CashSysWebhookView` (`qurtoba/views.py`), verified with `**CASH_SYS_WEBHOOK_SECRET`**.
5. G's Celery handlers (`qurtoba/tasks.py`) build receipt images, send WhatsApp,
  apply service fees, and on done batch all receipts.

### Flow D — Reroute (recipient number hit its limit)

`order_canceled` with `reroute:true` + `reroute_amount`. G settles the original at the
sent amount and **edits Q's ledger** to match: `edit_qurtoba_record_value()` →
`PATCH /transactions/api2/record/<id>/` on **Q** (auth `QURTOBA_TOKEN`,
skips re-forward → no loop). `qurtoba/utils_sync.py:150`. Then G asks the customer for a
new number and creates a fresh standalone order (new `external_ref`).

### Flow E — فورى / أمان (the manual cycle) — G only

No Q-record push to Cash-SYS, no webhooks. The operator works `/qurtoba/account-tasks/`,
copies number+amount, does the transfer by hand, marks **تم / إلغاء**. State lives in
`QurtobaRecord.account_task_state` (Genie-local, never synced out). This is the gap the
account-tasks page closes.

---

## 4. TODO — per server

### ☐ On **Qurtoba accountant (Q, :6000)**

- [ ] `python manage.py create_genie_user` → copy the printed token (this becomes **G**'s `QURTOBA_TOKEN`).
- [ ] In Q's `.env`: `GENIE_BASE_URL=http://<genie-host>:8000` and `GENIE_TOKEN=<token created on G in step G-2>`.
- [ ] In Q's `.env` (for Q's **own** direct cash orders, non-Genie path): `CASH_SYS_BASE_URL=http://<cash-host>:7000`, `CASH_SYS_TOKEN=<system_integration token>`.
- [ ] Confirm endpoints exist: `transactions/api2/record/` (POST + PATCH `<id>/`), `customers/api2/customers/`, `transactions/api2/rest-customer/<id>/`, `customers/api2/sellers/`.
- [ ] Confirm `forward_to_genie` is gated by `if not request.user.is_genie` (loop guard) — `main/utils/genie.py`.

### ☐ On **Cash-SYS (C, :7000)**

- [ ] `python manage.py setup_system_integration --admin-phone <admin>` → token → **G**'s `CASH_SYS_TOKEN`.
- [ ] `python manage.py setup_genie_integration` → token → **G**'s `CASH_SYS_CLIENT_TOKEN`.
- [ ] In C's `.env`: `GENIE_WEBHOOK_URL=http://<genie-host>:8000/qurtoba/cash-sys/webhook/`.
- [ ] In C's `.env`: `GENIE_WEBHOOK_SECRET=<shared secret>` (the SAME value G uses as `CASH_SYS_WEBHOOK_SECRET`).
- [ ] Confirm Celery worker + beat run (webhooks fire from Celery tasks: `apps/realtime/tasks.py`).
- [ ] Confirm order endpoint accepts only `كاش / كاش(5) / كاش(10) / كاش(20)` and a unique `external_ref`.

### ☐ On **Genie (G, :8000)**

- [ ] Install + migrate the module: module `state='installed'`, then `uv run python manage.py migrate qurtoba` (brings in `0002` account-task fields), `uv run python manage.py sync_all`.
- [ ] **G-2:** create the service user + DRF token that **Q** will use as `GENIE_TOKEN`: `uv run python manage.py drf_create_token <svc_user>` (the inbound `customers/api2/`* & `transactions/api2/record/` views require `IsAuthenticated` — `settings.py:919-927`).
- [ ] In G's `.env`: `QURTOBA_BASE_URL=http://<qurtoba-host>:6000`, `QURTOBA_TOKEN=<token from Q create_genie_user>`.
- [ ] In G's `.env`: `CASH_SYS_BASE_URL=http://<cash-host>:7000`, `CASH_SYS_TOKEN=<system_integration>`, `CASH_SYS_CLIENT_TOKEN=<genie_integration>`.
- [ ] In G's `.env`: `CASH_SYS_WEBHOOK_SECRET=<shared secret>` (must equal C's `GENIE_WEBHOOK_SECRET`).
- [ ] Run the worker (`celery -A project worker -l info -P solo`) — record push, cash-sys call, and all webhook handling are Celery/threaded.

### ☐ Generate the shared webhook secret once

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# paste the SAME value into:  C .env GENIE_WEBHOOK_SECRET  AND  G .env CASH_SYS_WEBHOOK_SECRET
```

---

## 5. Verify end-to-end (smoke test)

1. **Q→G master sync:** create/edit a customer on Q → it appears in Genie (`qurtoba.qurtobacustomer`). If not: check Q's `GENIE_BASE_URL/GENIE_TOKEN` and that G's token user is active.
2. **G→Q record:** create a كاش record in Genie for a synced customer → `qurtoba_synced=True`, `qurtoba_record_id` set, balance refreshed. If not: check `QURTOBA_TOKEN`.
3. **G→C order:** same record → a Cash-SYS order with `external_ref=<qurtoba_record_id>` exists. If not: check `CASH_SYS_TOKEN`/`CASH_SYS_BASE_URL` and the worker logs (`[CashSys ...]`).
4. **C→G webhook:** let Cash-SYS complete it → Genie logs `[CashSys Webhook] signature OK` and the WhatsApp receipt is sent. If `INVALID SIGNATURE`: the two secrets don't match.
5. **Reroute:** trigger a `number_limit` cancel → Genie PATCHes Q's record value and asks for a new number.
6. **فورى/أمان:** create a فورى/أمان record → it shows on `/qurtoba/account-tasks/`; mark تم → it leaves the pending queue (no external call).

---

## 6. Production notes

- All three base URLs must be **mutually reachable** over the network; replace `localhost` with real hosts/VPS IPs or DNS, and use **https** in prod.
- The HMAC secret is the only thing guarding the public `/qurtoba/cash-sys/webhook/` endpoint — keep it strong and identical on both sides.
- Tokens are long-lived. On Q, the genie user's token is intentionally **non-regenerable / non-deletable** (account signals) — rotating it means re-running setup on both sides.
- Each tenant VPS runs its own G+Q(+C) trio; the credentials above are **per-tenant**, not global.

```

```

