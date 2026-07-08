# Notes — Debug Findings & Feature Enhancements

Running collection of things found while debugging real conversations. Not bugs that
break correctness/safety necessarily — some are UX/enhancement ideas to revisit later.
Newest entry on top.

---

## 2026-07-08 — FIXED: agent asked "what's the amount?" after the SYSTEM already told it

Customer sent «01068340689 تحويل (10640)» → created fine. The SYSTEM then sent (Cash-SYS, not the AI):
«*محتاجين رقم تانى نبعت عليه الرصيد*\n\n*الرقم مش عليه محفظة*» (need another number, this one has no
wallet — the transfer was fully reversed/cancelled). Customer replied with just «01006001000» (the new
number). Agent asked **«المبلغ لـ 01006001000 كام؟»** — as if this were a brand-new, contextless request.
Customer repeated the number, got asked AGAIN, only got through on the third message when they explicitly
retyped the amount.

**Root cause:** the prompt's TRANSACTION LIFECYCLE section only ever documented ONE "need another
number" pattern — the **reroute** case (`_send_reroute_ask` in `tasks.py`, partial send, limit exceeded,
"الباقى" stated in the message). It never covered the **second, different** system pattern —
`_send_cancel_notice(reason='no_wallet')` — where the ENTIRE original transaction is reversed (debt
zeroed) rather than partially sent, using different wording («الرقم مش عليه محفظة» instead of «تجاوز
الحد»). The agent had no instruction covering this second pattern at all, so a bare phone number
following it looked like a fresh, amount-less request.

**Status: FIXED.** `_shared/core.md` TRANSACTION LIFECYCLE section rewritten to cover BOTH system
patterns under one rule: whenever the system's own last outbound message contains «محتاجين رقم تانى»
(in either wording), the amount is **already known** — reroute → the "الباقى" figure in that same
message; no-wallet/cancelled → the FULL amount from the original request that triggered it (a few
messages back in history). Either way: create immediately with the known amount + new number, **never**
ask «المبلغ كام؟». Added a worked example matching this exact conversation (10640 → 01068340689 →
no-wallet-cancel → 01006001000, same amount, no question). Deployed via `./fu.sh`, verified the shared
core module picked up both the rule and the example before restarting.

---

## 2026-07-08 — CHANGE: 010 (Vodafone) service fee now capped at 30 when total ≤ 60,000

Per explicit request. `_service_fee_plan` (`tasks.py`) — **010 recipients only**:
- Total transferred **≤ 60,000** → summed fee is now **capped at `SERVICE_FEE_010_CAP` (30)** — e.g. a
  summed fee of 10,000 is charged as 30, not 10,000.
- Total transferred **> 60,000** → unchanged, summed fee charged uncapped.

**Non-010 recipients are completely untouched**: ≤60,000 still picks the highest floored fee (uncapped),
>60,000 still creates one fee record per transfer (uncapped). Confirmed with the user this cap applies
to 010 only, not universally, before implementing.

Verified with 5 direct logic tests (010/non-010 × ≤60k/>60k + a sum-under-cap case) — all matched
expected output exactly. Compiled clean, deployed via `./fu.sh`, services confirmed active.

---

## 2026-07-08 — FIXED: social_sent_at never populated for text messages (since it was introduced) + brain escape-hatch closed

**Two separate root causes found and fixed while investigating a bad burst reply.**

### Bug A: `social_sent_at` null for virtually every message — the ordering-safety net had no data

Checked: 126/261 historical inbound messages have `social_sent_at` populated, but the most recent
populated one was from **2026-06-30**, over a week before this session. Every message since — including
every message in today's entire debugging session — has it null. Confirmed via git: today's commit
`49be96fe2` (the "team shipped fix" reviewed earlier this session) added
`social_sent_at=social_sent_at` to the **two media call sites** in `process_receive_whatsapp_message`
(`whatsapp/tasks.py`) — CTWA media and regular media (`receive_message`) — but **never added it to the
plain-text path** (`receive_text_message`, ~line 687) or to the two CTWA text-fallback paths (~423, ~439).
Since virtually all customer traffic is plain text, this meant the entire true-send-order mechanism
(built specifically to survive concurrent-worker created_at scrambling) has been silently running on
`created_at` fallback only, with zero real protection, for the whole session.

**Status: FIXED.** Added `social_sent_at=social_sent_at` to all 3 missing `receive_text_message` call
sites in `modules/whatsapp/tasks.py` (main inbound text path + both CTWA text fallbacks), matching the
pattern already used for the two media paths. Compiled clean, restarted via `./fu.sh`. Not
retroactively fixable for historical rows (already-arrived messages stay null) — but the ordering
mechanism now actually has data going forward, closing exactly the gap the same-time/split-pair guards
depend on.

### Bug B: the agent bounced a cash-transfer burst to `payments_agent`, which then handed back to `brain`

Same burst — checked the live LangGraph state (`check_agent_state.py`): `current_agent` was `brain`,
and this run's `node_results` included **`payments_agent`** even though the burst was pure كاش transfers
(phone+amount pairs, no receipt image, no سداد wording — should never have reached payments_agent at
all). `payments_agent` correctly recognized it wasn't its lane, but instead of forwarding to `cash_agent`
directly (also available to it), it called `brain` — which it could do because **every specialist node's
`handoff.targets` in the DB still listed `brain`** as a valid callable peer, left over from the platform
wiring predating the "no agent ever returns to the brain" design rule. My earlier prompt cleanup removed
the explicit "never call brain" *sentence* from the prompts (per instruction to stop over-explaining
plumbing) but never touched this platform-level tool list — so the LLM still had the tool available and
used it.

**Status: FIXED.** Removed `agent_chat_1783507168900` (brain) from `handoff.targets` in the
`WorkflowNode.configuration` for all 3 specialist nodes (`cash_agent`, `fawry_aman_tayer_agent`,
`payments_agent`) directly in the DB — verified each now lists only its 2 specialist peers. This closes
it deterministically at the tool-availability level rather than relying on a prompt instruction: even if
an agent misclassifies and lands somewhere wrong, it can now only hand off sideways to another
specialist, never back to brain. `brain`'s own targets (the 3 specialists) are untouched.

**Not yet addressed:** WHY brain (or whichever agent decided the handoff) misrouted a pure-كاش burst to
payments_agent in the first place is still an open question — no receipt image or سداد wording was
present, so this looks like a one-off model misclassification rather than a demonstrated prompt gap.
Worth watching for a repeat pattern before changing brain's routing prompt.

---

## 2026-07-08 — FIXED: "which transfers didn't go through?" only listed 3 out of 18 pending

Customer asked «اي التحويلات ال متمتش؟» (which transfers didn't go through). Agent called
`qurtoba_check_transaction_status` with no argument and replied listing exactly 3 transactions as
«⏳ قيد التنفيذ». **Ground truth at that moment: 20 transactions today, 18 still not executed** — the
reply silently dropped 15 of them.

**Root cause:** `qurtoba_check_transaction_status` with no `source_message_id` doesn't filter by status
at all — it just returns `.order_by('-time','-id')[:3]`, i.e. the literal latest 3 records of today
regardless of whether they're done or not. It happened to be individually correct (those 3 genuinely are
pending) but wildly incomplete for a "which ones" question. This is a wrong-tool-for-the-question bug,
not a wrong-data bug — the tool did exactly what it's built for (a quick "did MY transfer go through"
check), it was just the wrong tool for "which/how many of ALL of today's are still pending."

**The right data already existed.** `qurtoba_get_customer_daily_transactions` already returns full,
uncapped `transactions[]` with a `bucket: "executed"|"in_flight"` field per record — exactly the
structured, per-agent-filterable data needed. The gap was purely in the prompt: the agent was only ever
told to paste its `pretty_ar` verbatim (correct for "show me everything"), never told it could read
`transactions[]` directly and filter it for a specific subset question.

**Status: FIXED.**
- `tools/reports.py` (`qurtoba_get_customer_daily_transactions` description): added explicit guidance —
  filtered questions ("which didn't go through", "how many pending", "show only كاش") → read
  `transactions[]`, filter by `bucket`, compose your own reply; don't force pretty_ar (shows everything)
  onto a filtered question.
- `tools/transactions.py` (`qurtoba_check_transaction_status` description): documented the 3-record cap
  explicitly and redirected "which/how many still pending" questions to the daily-transactions tool.
- `_shared/core.md` STATUS section: new explicit routing rule + a worked example matching this exact
  scenario (20 transactions, 18 in_flight → list all 18, not 3).
- No new tool needed — reused the existing structured data, fixed the routing/prompt gap.

Compiled clean, deployed via `./fu.sh`, verified live (tool descriptions + core.md all confirmed to
contain the new guidance).

---

## 2026-07-08 — FIXED: duplicate detection moved from LLM judgment to a deterministic code check

**Same conversation, fresh test round.** Customer created 1000→01006000100 and 200→01025294594 (كاش),
then ~90s later resent the EXACT same two requests. The agent never flagged them as repeats — no
`تأكيد تكرار العملية؟`, no creation, no acknowledgment — it only responded to an unrelated ambiguous
part of the same burst and silently dropped the two duplicate-looking pairs. Root cause: the existing
"Duplicate" guard was purely prompt-level, relying on the LLM to notice "did I just do this?" from
conversation history — which is exactly the kind of judgment call that's proven unreliable all session
(see the «تم»-hallucination entry above).

**Status: FIXED.** Per explicit request, moved detection to a deterministic backend function —
`_create_one_debt` in `tools/transactions.py` (new "B5" check):
- Runs ONLY for **كاش** (any tier — كاش/كاش(10)/كاش(20)/كاش(5)). **فورى/أمان/طاير are never checked** —
  by design, per explicit instruction.
- Matches on `(customer, account_number, value)` — same account+amount, regardless of which message/id
  triggered either the original or the repeat.
- Scoped to **today's calendar day only**, computed via `timezone.localtime()` (Africa/Cairo, DST-aware —
  same mechanism as the earlier `<current_time>` fix). A matching transaction from yesterday or any
  earlier day is **never** flagged, no matter how identical the values.
- Returns `same_day_duplicate:true` (not `duplicate:true` — kept distinct from the existing exact-retry
  B4 check) with the existing record's id/type/value/account/created_at. **No transaction is created.**
- New `confirm_repeat` param (per-item, on `qurtoba_create_new_transactions_bulk`): the agent sets this
  ONLY after the customer explicitly confirms «تأكيد تكرار العملية؟» — then the SAME item (same
  `source_message_id`, no need to guess a different one — sidesteps the earlier `source_mismatch`
  hallucination bug too) is actually created.
- Tool description, `_shared/core.md`, and `cash/prompt.md` all updated to document the new
  `same_day_duplicate` status and the ask→confirm→retry flow, with a concrete example matching this
  exact scenario.

Verified against real data before deploying: today's real كاش 7450→01126044871 correctly matches;
a فورى 900→2924523 record does NOT get caught by the كاش-only filter; the day-boundary logic can never
include anything before local midnight (`Africa/Cairo`, DST-aware). Pushed cash/prompt.md's new text to
the live node (byte-verified), restarted via `./fu.sh` (services active as of 17:28:50 CEST), and
confirmed the live tool registry picked up the new `confirm_repeat` schema field + updated description.

---

## 2026-07-08 — FIXED: agent said «تم» twice for a transaction that never happened

**Same conversation, same session.** After the duplicate-confirm question («تأكيد تكرار العملية؟ 7450 جنيه
على 01126044871 أنا شايف اللي فات دلوقتي.») — which fired CORRECTLY — the customer confirmed with a bare
«اها كررها» (yeah, repeat it). Exact sequence that followed (all confirmed from message-level tool call/tool
result pairs, not guessed):
1. Agent sent **«تم»** as plain text — BEFORE calling any tool at all.
2. It then called `qurtoba_create_new_transactions_bulk` with `source_message_id` = the **«اها كررها»
   message itself** (no phone/amount in it) → tool correctly REJECTED: `error_type:"source_mismatch"`,
   `created_count:0`.
3. Agent sent **«تم» again**, this time via `whatsapp_reply_to_message`, quoting the same message —
   *immediately after* receiving the rejection.

**No double money movement** (confirmed via the daily report moments later — only one 7450 record exists),
but the customer was told "Done" twice for an action that was rejected and never executed. A trust-breaking
hallucination, not just a pairing bug.

**Root cause (two parts):**
- Wrong `source_message_id` on retry: a bare confirmation reply has no account number, so reusing its id is
  guaranteed to hit `source_mismatch`. The prompt never told the agent to reuse the ORIGINAL request's
  message id instead.
- The agent never checked the tool's result before speaking — violating the ALREADY-EXISTING Law 4
  ("success sends nothing, never تم") and the REPLIES CONTRACT ("rejection → real reason only"). It had the
  rule and ignored it under this specific pressure (an unrelated daily-report question landed in the same
  batch, may have contributed to the confusion).

**Status: FIXED (2026-07-08), same session, user flagged as urgent.**
- `_shared/core.md` Law 4: added explicit **"NEVER claim success before it happened"** — no تم/success
  before a create call's result returns, and never after a rejection; retry correctly or state the real
  reason. Added a matching SHARED EXAMPLE.
- `cash/prompt.md`: added **"Confirmed-repeat retry"** rule — after a bare yes to the duplicate-confirm
  question, reuse the ORIGINAL phone-number message as `source_message_id`, never the confirmation reply.
- `fawry_aman_tayer/prompt.md`: added the full **Duplicate** guard (it had none before — only cash did) +
  the same **Confirmed-repeat retry** rule, for parity across both transfer agents.
- Pushed all three updated texts to the live nodes (verified byte-match) and restarted via `./fu.sh`
  (services confirmed active as of 17:06:16 CEST).

---

## 2026-07-08 — LIVE, currently unresolved: watermark-gap pairing corruption + 2 dropped transfers

Same conversation (`d8bc5e42-...`), same test session, AFTER bugs #1/#2 fixes were deployed and confirmed
working (fu.sh restart at 13:59 UTC; this all happened at 14:24-14:28 UTC, i.e. against the fixed system).

**Bug A — wrong-value records already created (financially significant).** A bare amount message
(`b4e9c0a7`, "حول ليا 900") got resolved via a clarifying Q&A (كاش/فورى? → «فوري يا باشا») and correctly
auto-picked the customer's single registered فورى account (2924523) — today's account-auto-resolve fix
worked. BUT the original `900` message was never watermarked `ai_consumed_at` — creates that come from a
Q&A round-trip (not the planner) only mark the message that carries `source_message_id`; the tool's
watermarking (`consumed_ids_by_source`) only knows about pairs the PLANNER discovered. So `900` stayed in
`<unprocessed_transactions>` for ~90s and got swept into the NEXT unrelated 3-pair burst, shifting every
pairing by one slot:
```
#10416  كاش   900.0    → 01141493124   (true amount was 1160)
#10417  كاش   1160.0   → 01065686569   (true amount was 38500)
#10418  كاش(20) 38500.0 → 01126044871  (true amount was 7450)
'7450 جنيه' left stranded, unconsumed
```
**Root cause: systemic, not one-off.** ANY transaction created via a clarifying-question resolution
(ambiguous type, missing account — including today's own account-auto-resolve fix) can leave its
originating message unwatermarked, silently poisoning whichever burst runs within the next
`AI_UNPROCESSED_WINDOW_MIN` (6 min) window. `qurtoba_create_new_transactions_bulk`'s schema has NO
agent-facing parameter to mark extra messages consumed — `consumed_message_ids` is purely internal,
computed from `consumed_ids_by_source(conv)` (planner-derived only).

**Bug B — 2 clean, unambiguous transfers silently dropped (currently live/unanswered).** Next burst
(14:27:23-24): a messy message mixed a spelled-out Arabic amount word ("خمسين الف"=fifty thousand — the
classifier ONLY parses digit-based amounts + digit+«ألف», it has NO word-number support, so this is
invisible to the system), a malformed 10-digit "phone" (`0100600100`, one digit short) that got MISREAD as
a giant amount (100,600,100) instead of flagged as invalid, plus 2 other messages each a clean
self-contained pair (`1000→01006000100`, `200→01025294594`). The planner's confused output (an absurd
100M-value orphan + a duplicate use of `1000` across two different accounts) seems to have led the agent
to just ask about the OLD stale 7450 orphan (again) and take NO action on anything else — no create, no
ask, no acknowledgment of the 2 clean transfers. `Workflow returned empty output ... intentional no-reply`
(not a crash — `send_message_ids_to_ai` makes empty-output-after-a-tool-call a valid intentional silence,
so nothing flagged this as an error). Confirmed: zero new `QurtobaRecord`s, all 3 messages still
`ai_consumed_at=None`, conversation silent 16+ minutes as of this check.

**Smaller parsing gaps surfaced by the same message:**
- 10-11 digit near-phone numeric strings that fail the phone pattern fall through to "amount" instead of
  "invalid phone, ask to resend" — risky if it ever pairs with a real phone (could create a wild amount).
- Spelled-out Arabic number words (خمسين/مية/etc.) are not recognized as amounts at all.

**Status: NOT YET FIXED / NOT YET DECIDED.** Flagging for review — this needs a decision on where the
watermarking fix belongs (tool-level: extend `consumed_ids_by_source` to also sweep any bare-amount
message consumed by a non-planner create; or prompt-level: never enough on its own, since the tool has no
channel for it) before touching money-movement code again.

---

---

## 2026-07-08 — Auto-select account only covers "no type" case, not "type given, account missing"

**Conversation:** `d8bc5e42-6288-4c40-8479-3e1d78446a1f` (partner: شهاب للمحاسب / 201025294594, customer: حسين بركات #170)

**What happened:**
1. `[outbound]` "الرقم أو حساب الفوري اللي تحول له من فضلك؟" — agent asks for the fawry account.
2. 18 min later `[inbound]` "السلام عليكم" → greeted, no follow-up on the earlier request.
3. `[inbound]` "محتاج 1000 فوري" — customer explicitly names the type (فورى) + amount, no account number.
4. `[outbound]` "الحساب الفوري اللي تحول 1000 جنيه له من فضلك؟" — agent asks for the account AGAIN.

**Why this is avoidable:** the customer has **exactly one** registered fawry account (`فورى 2924523` — checked via `qurtoba_customer.accounts_pretty`). There's no ambiguity to resolve.

**Root cause (prompt gap):** `fawry_aman_tayer/prompt.md` → **❓ UNKNOWN TYPE** section:
```
## ❓ UNKNOWN TYPE (amount, no type, no phone — "محتاج 500")
Rely on the customer's registered accounts:
- Exactly one registered account → execute with its type/number (no question).
- More than one → ask «أي حساب؟ ...».
- None → ask «النوع؟ ...».
```
This auto-select-when-unambiguous rule is scoped ONLY to the case where the **type itself** is unspecified ("محتاج 500"). It does NOT cover the case where the customer names the type explicitly (فورى/أمان/طاير) but omits the account number. In that case the agent falls straight to the ACCOUNT GUARD, which has no account to validate → it just asks, even when exactly one registered account of that type exists.

**Proposed enhancement:** extend the same "exactly one → auto-execute, no question" logic to: type given + account missing + exactly one registered account of that type → use it directly. Only ask when 2+ accounts of that type are registered (real ambiguity) or none exist (real gap needing admin setup).

**Status: RESOLVED (2026-07-08).** This exact gap is what caused the conversation to go completely silent later the same session (see entry below — the agent, unable to resolve the account, had no create tool anyway and looped instead of asking twice). Fixed in `fawry_aman_tayer/prompt.md` → renamed section to **❓ MISSING ACCOUNT**, extended the same "exactly one → execute directly" logic to cover type-given-account-missing (not just type-unspecified). Added 2 new examples matching this exact conversation. Deployed to the live node + local `.md` + restarted via `./fu.sh`.

---

## 2026-07-08 — Conversation goes completely silent: empty tool lists (bug #1) + loop-guard's own apology swallowed (bug #2)

**Same conversation, continued** (`d8bc5e42-6288-4c40-8479-3e1d78446a1f`). After the customer confirmed "عندي حساب واحد بس حول عليه" (I have one account, use it), the conversation never received another reply. Full trace:

**Bug #1 (config, root cause) — all 4 agent nodes had `selected_tools: []`.** Verified via `WorkflowNode.configuration['selected_tools']` for all four `agent_chat_*` nodes in workflow 2 ("Qurtoba Accountant") — every one was empty. Only the 3 auto-added peer-handoff tools were available to any agent. Consequence: **no agent could create a transfer, register a payment, check status, quote-reply, or alert a human** — only plain-text replies worked (which is why greetings/courtesy succeeded earlier in the same conversation).

Concretely: `fawry_aman_tayer_agent` had everything it needed (type=فورى, amount=1000, account=2924523 from `<live_context>`) but no `qurtoba_create_new_transactions_bulk` tool to act on it, so it handed back to `brain` describing the transfer in its handoff reason. `brain` re-classified and handed the same request back. This repeated (`chain=['fawry_aman_tayer_agent','brain','fawry_aman_tayer_agent']`, revisits=2) until the platform's ping-pong guard (`workflow_engine.py::detect_handoff_loop`) tripped and called `conversation.escalate_to_human()`.

**Status: RESOLVED by user (2026-07-08)**, attaching the correct tools per node directly in AI Studio. Verified afterward — matches the designed table exactly:
- `brain`: 6 shared tools only (balance, daily, status×2, reply, alert)
- `cash_agent` / `fawry_aman_tayer_agent`: + plan_transactions + create_new_transactions_bulk (8 each)
- `payments_agent`: + register_customer_payment (7), no plan/create_bulk

**Bug #2 (code, independent) — the loop-guard's own fallback apology got swallowed.** The guard sets `updated_state['__output__']` to a clean Arabic apology and calls `conv.escalate_to_human()` (which sets `handled_by_ai=False`). Downstream, `aistudio_whatsapp/tasks.py` re-reads `handled_by_ai` specifically to detect "did a human take over while this run was executing?" and — seeing `False` — silently skipped sending. Confirmed in logs: `"Workflow executed successfully, sending response..."` immediately followed by `"Skipping AI response ... handled_by_ai=False (human took over while workflow was running)"`. So even after bug #1's fix, ANY future handoff-loop trip would still eat its own apology — a race with itself, not a real concurrent takeover. Only the internal team (admin/mohamed/shehab) got notified; the WhatsApp customer got nothing.

**Status: RESOLVED (2026-07-08).** Added a distinct signal so callers can tell "this run escalated itself" apart from "a human genuinely took over mid-run":
- `workflow_engine.py`: loop-guard now also sets `updated_state['__escalated_this_run__'] = True` alongside the apology.
- `workflow_executor.py`: new `WorkflowResult.escalated_this_run` field + `_extract_escalated_this_run()` helper (mirrors the existing `had_side_effect` plumbing), populated at both `WorkflowResult(...)` construction sites.
- `aistudio_whatsapp/tasks.py`: the `handled_by_ai` gate now reads `if not conversation.handled_by_ai and not result.escalated_this_run:` — so this run's own apology is no longer suppressed by its own escalation.
- Verified `alert_qurtoba_human` (the tool) does NOT touch `handled_by_ai` — this bug was scoped strictly to the platform loop-guard path, nothing else.

Deployed via `./fu.sh` restart.

---
