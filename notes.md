# Notes — Debug Findings & Feature Enhancements

Running collection of things found while debugging real conversations. Not bugs that
break correctness/safety necessarily — some are UX/enhancement ideas to revisit later.
Newest entry on top.

---

## 2026-07-12 — BUG: agent asked confirmation on 11 CLEAN self-contained pairs (conv 13f58d64)

**Symptom:** customer sent 11 messages, each a clean self-contained «رقم\nمبلغ\nاسم\nطهN.NN» pair. Agent replied
«تأكيد النهائي… كاش؟» (blanket confirmation); customer: «ليه بتبعت تاكيد».

**Root cause (deterministic, found via ~/qurtoba_agent.log):** the planner returned 11 HIGH pairs, `list_pattern:false`
— but ALSO one spurious `orphan amount 12`. It came from message «...عاصم كاش عبدالله12...»: the «12» glued to the
NAME «عبدالله» (a serial) was extracted as a 2nd amount → that message had amounts=[13080, 12] → not a clean 1-1 pair
→ orphan 12. The orphan tripped the planner note «اسأل عنها قبل التنفيذ», and the agent escalated that to a
full-batch confirmation.

**Fix 1 (the real one) — `_is_glued_name_label` in planning.py:** a token that LEADS with an Arabic name and has digits
trailing («عبدالله12», «طه13», «رقم5») is a serial/label, NOT an amount, and is dropped in `_classify_message`. Kept
safe (amount LEADS): «2350..اسامة»→2350, «13300جنيه»→13300, «30الف»→30000, «كاش12080»→12080 (type/currency/multiplier
leads are allowed). Verified on the real burst → now 11 pairs, **0 orphans**, list_pattern false → agent executes, no
confirm. Regression-tested against the d8bc5e42 name-laden messages (7915/1380/1360/10960/27200/2350/7125/12280) — all
amounts still extracted.

**Fix 2 (backstop) — prompts:** cash/fawry «Final confirmation» clarified: it is NOT about count — a BATCH of many
clean self-contained pairs (all high, no list_pattern, no orphan) EXECUTES with no confirmation; confirm ONLY for a
single op assembled across 3+ messages or a real list_pattern/low/orphan ambiguity. Added «ليه بتبعت تاكيد» as the
named anti-pattern. Deployed (planning.py restart + cash/fawry nodes pushed).

**Fix 3 — general "noise number" hardening (so the whole CLASS is covered, not just «عبدالله12»):** the classifier
now drops these non-amount numbers deterministically before pairing:
  - digit glued to the END of a name → «عبدالله12», «طه13», «رقم5» (`_is_glued_name_label`; amount must LEAD).
  - bracketed/parenthesised serials → «( vivo - shehab - 652 )», «(124)», «[3]» (balanced-bracket strip in
    `_classify_message`).
  - latin ref codes → «W2399», «A103», «TX-88» (caught by the glued-label rule — latin lead, not a keyword).
  - reference/receipt-serial phrases → «رقم العملية 5», «الرقم المرجعي 12345» (`_REF_NOTE_RE`, alongside `_FEE_NOTE_RE`).
  - already handled earlier: fractional tallies «عمار 13.75»/«طه13.40» (is_integer drop), fee notes «لو هيخصم 15».
  Kept safe — the amount is untouched when the NUMBER leads: «2350..اسامة»→2350, «13300جنيه»→13300, «30الف»→30000,
  «كاش12080»→12080, «14.880ج.م»→14880. Verified: (124)/vivo/W2399/رقم العملية 5 all no longer leak; the full 11-burst =
  11 pairs / 0 orphans; the d8bc5e42 name-laden battery (7915/1380/1360/10960/27200/2350/7125/12280) all still extract.
  SAFE DEGRADATION: even an unseen noise pattern that slips through becomes an ORPHAN (the agent asks one question) or is
  re-validated at create — it can never silently become a WRONG transaction.

---

## 2026-07-12 — CONFIG: same-time split cap lowered 4 → 3 (owner request)

`AI_SAME_TIME_MAX_TX` default 4 → **3** in `planning.py`. Now ≤3 split transactions in the SAME second process
(2–3 still flagged for confirmation), 4+ same-second split are withheld → resend. Updated all matching text: the
planner overflow/needs_resend notes («أكتر من 3 تحويلات», «بحد أقصى 3»، «3 ب 3»), the tool description («max 3 at a
time»), and core/cash/fawry prompts («≤3 at a time»). Self-contained pairs still never capped. Verified: 3 same-second
split → allowed, 4 → blocked. Deployed (planning.py restart + cash/fawry nodes pushed + core reloaded).

---

## 2026-07-10 — ROOT CAUSE: legit multi-tx burst infinitely refused (conv 13f58d64) — same-time window too wide

**Symptom (real, re-tested today via ~/qurtoba_agent.log):** customer محمد sent ~7 cash transfers as SPLIT messages
(number and amount in separate messages), streamed ~1/second from 09:27:53→:58, with «الحرمين»/«لو هيخصم 15» as
separators. The AI refused the WHOLE batch ("ابعت كل رقم ومبلغه في رسالة، وبحد أقصى 4") over and over; the customer
resent, it accumulated (23→38→44 unprocessed msgs), never processed. Debug log showed the planner returning ONE pair
(the self-contained 30الف) and dumping 7–14 into `resend` with `overflow:true` every call.

**Root cause:** the `same_time_overflow` guard (the «max 4 same-time split» rule the owner asked for) clustered by a
**5-SECOND sliding window** (`AI_SAME_TIME_WINDOW_SEC=5`). A burst streamed over 5 seconds (≤2 split per actual second)
was lumped into ONE fake "instant" → 7 phones + 7 amounts → min 7 > 4 → whole batch withheld. Meta only scrambles order
WITHIN a second, not across seconds, so the 5s window was wrong. (This was flagged as planning-audit Finding #6.)

**Fix:** `_cluster_split_by_second` — cluster split events by the EXACT send-second (truncate to second, group by
equality), used by both `_same_time_split_mids` and `_same_time_overflow_mids` (window_s now ignored). Proven on the
REAL burst: overflow now 0, and the planner returns all **8 correct pairs** (01012745373←24200, 01105430994←13450,
01226086860←6760, 01004194823←50030, 01228293960←20040, 01019525475←14120, 01061360502←8865, 01070458397←30000),
0 orphans, `list_pattern=True` so the agent CONFIRMS the matching before executing. Protection preserved: 5 split in
the SAME exact second → still blocked (>4); 4 same-second → not blocked but flagged for confirm. Deployed (planning.py
internal logic; app restarted). NOTE (secondary, watch): the old log also showed the agent sending MULTIPLE outbound
messages per turn (Law-3 violation) + empty outbounds while confused by the refusal loop — the primary fix removes that
confused state (burst → one confirmation reply), so this should self-resolve; revisit only if it recurs on a clean burst.

---

## 2026-07-09 — DEEP AUDIT (4 parallel reviewers) + fixes across tools & prompts

Ran four independent deep audits (planning.py / transactions.py / _amounts.py+minor tools / all prompts) and
fixed the confirmed defects. Two audits independently flagged the 27M bug and the completeness-guard collision.

### Money-critical (fixed, verified)
- **`_amounts.py` — «٢٧٠٠٠ ألف» → 27,000,000 (1000× over-charge).** The multiplier loop always did ×1000 for
  «الف» even when the count was ALREADY ≥1000 — violating the documented rule (core: «ألف» never means million).
  Reachable straight through the deterministic planner (`_classify_message`→pair→bulk). FIX: in `normalize_amount`,
  `if multiplier>1 and count>=multiplier: value=count` (redundant unit) else `count*multiplier`. Verified:
  ٢٧٠٠٠ ألف→27000, 1000 الف→1000, 2700 ألف→2700, BUT 27 الف→27000 and 500 الف→500000 still scale. All prior
  cases (27.460→27460, 20الف→20000, الفين→2000, مليون→1M, خمسين الف→refuse) unchanged.
- **Create path accepted fractions / NaN / <1 EGP.** `_validate_debt_item` only checked `>0`, so a misread
  receipt value (13.75, 0.5, nan) could become a real transfer. FIX: reject non-finite / non-integer / <1.
  Also hardened `normalize_amount` numeric fast-path against NaN/Inf and a glued minus («-500»→500 now refuses).
- **Same-second 2–4 split transactions paired silently at high confidence (amount-swap).** The resend guard only
  fired at ≥5 pairs, so a same-second cluster of 2–4 split (phone/amount in separate msgs) transfers was created
  with possibly-swapped amounts, no confirm. FIX (planning.py): any surviving pair whose mids intersect the
  same-second split cluster → `confidence='low'` + `list_pattern=True`, so the agent CONFIRMS the matching first
  (self-contained pairs unaffected; >4 still hard-withheld).
- **Broken phone → giant amount.** «0100600100» (10-digit, failed 11-digit normalization) was read as amount
  100,600,100 and paired with a waiting phone. FIX (`_classify_message`): a leftover `0\d{9,11}` token is a broken
  number → noise/orphan, never an amount.

### Correctness / safety (fixed)
- **Completeness guard recreated cancelled money + blocked valid subsets.** The bulk `incomplete_list` hard-reject
  fired whenever any recent self-contained pair was absent from the array — but cancellation and same-day-duplicate
  holds LEGITIMATELY omit a pair. Rejecting the whole batch (and telling the LLM «resend all») could recreate a
  transfer the customer just cancelled, or silently block the valid ops. FIX: made it NON-FATAL — create the
  provided ops and surface dropped pairs as `possibly_missing` (internal); the agent adds a true drop, ignores an
  intentional omission. Documented in core + cash + fawry prompts.
- **Payment registration was SILENT (no ack) + no idempotency.** `qurtoba_register_customer_payment` never sends a
  👍 (only the transfer-create tool does), yet the payments prompt ended every success in ∅ and the tool desc said
  «reply 👍» — net: customer sends a receipt, gets NOTHING, re-sends → duplicate review row. FIX: payments prompt
  now sends a short «وصلني الإيصال، تحت المراجعة» after success (∅ removed); tool desc corrected (no auto-ack);
  added a same-day duplicate guard (same type+value+account pending today → `duplicate:true`).
- **confirm_repeat could be swallowed by a lingering in-flight claim.** A human-confirmed repeat on the src-less
  path collided with the first create's still-live 900s claim key → `duplicate_in_flight`, never created. FIX:
  append `:cr` to the claim key when `confirm_repeat=true`.
- **Balance tool leaked Law-6 fields + false description.** `qurtoba_send_customer_balance_to_chat` returned
  grade_limit/remaining/over_limit to the model and its desc claimed the message shows the credit limit (it shows
  only the debt line). FIX: return balance only; corrected the description.
- **Prompt gaps:** brain now has the negative rule «phone+amount is ALWAYS a transfer, NEVER a payment»; cash/fawry
  planner sections now cover `read_amounts`/`amount` fallback + `possibly_missing` (their emphatic «ask per orphan»
  contradicted the read-the-spelled-amount rule); fawry duplicate section corrected (same_day_duplicate/confirm_repeat
  are CASH-ONLY — stop asking the LLM to eyeball non-cash repeats); `_looks_like_spelled_amount` note softened so a
  NAME (سمية/سامية) is never read as a number; garbled `static_reply` default message fixed.

### Deferred / flagged (NOT changed — need product decision or bigger design)
- **Non-cash (فورى/أمان/طاير) has no deterministic duplicate guard** (same_day_duplicate is cash-only by product
  decision). Prompt now honest about it; extending the deterministic check to non-cash is a PRODUCT decision.
- **Fallback watermark leak (split spelled amount):** planner pairs it via `amount` fallback, but
  `consumed_ids_by_source` re-derives from DB with no fallback → the spelled amount msg isn't watermarked and can
  linger (re-surfaces in read_amounts; same-day-dup guard bounds the harm for cash). Needs the create tool to accept
  explicit consumed ids, or fallback persistence.
- **B5 same_day_duplicate not atomic** (concurrent same account+value in two msgs can double-create without the
  confirm gate) — needs an account+value lock independent of `src`.
- **same_day_duplicate/duplicate/pending don't watermark** their messages → can linger in the next burst's pairing.
- **5-second same-time window** treats a 5s span as one instant → can block a legit fast typist's burst (fails safe).
- **Upper-bound cap** on amounts (no ceiling; a 10-digit misread was 100M) — a defensive max would help.
- Minor: single-message «P P / A A» pairs at high confidence with no list_pattern; `_debuglog` cross-process line
  interleave under load; `cash_sys.py` unguarded `client_data['id']`; reports.py record-vs-pending day-boundary tz.

All fixed items: compile-checked, unit-tested (multiplier table, create-path guards, broken-phone, same-second flag,
detector), deployed via `./fu.sh` + app restart, 4 agent prompt.md pushed to their live WorkflowNodes, live registry
+ nodes + SHARED_CORE all verified. 5 services active.

### Side-effect self-review (traced every change; fixed 3 of my own)
- **Payment dup guard was ALL-DAY → would silently block a legit second identical payment.** Narrowed to a ≤5-min
  accidental-resend window (the confirmation reply is the real anti-resend fix).
- **Fawry duplicate rewrite over-removed the rapid-resend caution.** Restored clause (c): if you can CLEARLY see the
  exact same (type+account+amount) was just created (its 👍 is visible), ask «تأكيد تكرار» before repeating.
- **Core Law 4 «success sends nothing» could still silence the payments agent.** Added a one-line carve-out in core
  Law 4 pointing to the payment confirmation exception.
- Verified clean (no regression): full 33-case parser battery (incl مليون/2.5m/20k) all correct; split/self-contained/
  3-pair bursts pair correctly; broken-phone «0100600100 5000» → 5000 survives as an orphan amount (agent asks for a
  number), broken number dropped as noise — acceptable trade-off vs the old 100M mis-amount; fee-note still strips «15»;
  create-path guard rejects 13.75/0.5/nan but passes 5000/50000/«٢٧٠٠٠ ألف»→27000. Accepted trade-offs (flagged, not
  bugs): same-second ≤4 SPLIT now asks a confirm (self-contained never flagged); completeness guard non-fatal relies on
  the agent acting on `possibly_missing` (planner is authoritative so hand-built drops are rare); a broken number is
  silently dropped (its amount still surfaces as an orphan).

---

## 2026-07-09 — PERF: LLM passes a per-message `amount` fallback (no-fail) + ~/qurtoba_agent.log debug log

Per request ("I don't want the tool to catch the number itself; let the LLM pass a fallback number when it
calls the planner so Python never fails; and write a clean, direct debug log with message ids + metadata to
the home folder").

**1. Fallback amount — the "no fail" path.** `qurtoba_plan_transactions` message items now accept an optional
`amount` (number|null). When a value is written in Arabic WORDS the deterministic parser can't convert, the
LLM reads the number and passes it on THAT message (`{message_id, text:"خمسين الف", amount:50000}`). The tool
uses the LLM's number as that message's value and pairs it normally — no orphan, no `read_amounts`, no second
round-trip. Rules:
  - Fallback is used ONLY when Python parsed NO amount for that message; if the parser caught a digit amount,
    the fallback is ignored (Python wins — verified: `01019525475 1500` + `amount:99999` → pairs 1500).
  - `_coerce_fallback()` runs the fallback through the SAME `normalize_amount`, so 50000 / "50000" / "٥٠٠٠٠"
    all land; anything non-numeric / fractional / ≤0 is rejected (a bad fallback can never create a wrong
    amount). Spelled text passed as a fallback (`"خمسين الف"`) → None (the LLM must pass the NUMBER).
  - Captured by id BEFORE the authoritative DB re-fetch replaces the message list (DB rows don't carry the
    LLM `amount`); threaded into `_build_events(messages, msg_text, msg_fallback)`; also honors an inline
    `item['amount']` on the no-conversation path.
  - `read_amounts` still fires for any spelled amount the LLM did NOT supply — graceful fallback of the
    fallback. Tool description + `_shared/core.md` updated to teach "pass `amount` for worded values".
  - CAVEAT (rare, noted): a truly SPLIT spelled amount (phone in msg A, «خمسين الف» alone in msg B) resolves
    correctly in the PLANNER via fallback, but `consumed_ids_by_source` (create-path watermark, re-derives
    from DB with NO fallback) classifies the bare spelled msg as a name → the amount message id may not get
    watermarked and could linger. Self-contained (phone + spelled amount in ONE message — our real case) is
    unaffected: its `source_message_id` is watermarked directly. Left as a known edge, not fixed.

**2. `~/qurtoba_agent.log` — direct debug log.** New `tools/_debuglog.py` (`log_event`): best-effort, never
raises/blocks, size-rotates at 5 MB → `.1`. One compact JSON line per event, Arabic kept readable, short
stable keys, null fields dropped. Wired into:
  - planner → `ev:"planner"` with conv, in_msgs/in_ids, pairs (acc/val/conf/src), orphans, `fb_used`,
    `read_amounts`, list_pattern, overflow, needs_resend, resend.
  - create batch → `ev:"create"` with conv, cust, counts (created/pending/rejected/dup/same_day_dup),
    balance, and per-item `{st, acc, val, rid, err}`.
  Read live: `tail -f ~/qurtoba_agent.log`; filter a chat: `grep '"conv": "d8bc5e42"' ~/qurtoba_agent.log`;
  filter a message/phone: `grep '<uuid-or-phone>' ~/qurtoba_agent.log`. Path override via
  `QURTOBA_DEBUG_LOG_PATH` setting.

Compile-checked, unit-tested (coerce table, self-contained+fb, split+fb, bare-no-fb orphan, digit-wins),
log write verified, deployed via `./fu.sh` (all 5 services active), live `ToolDefinition` confirmed to carry
the `amount` param + FALLBACK description.

---

## 2026-07-09 — ENHANCEMENT: tools now hand an uncatchable value back to the LLM + richer examples

Per request ("make the tools more efficient; when they fail to catch a value, tell the LLM to catch it;
add deeper examples"). Two-part change:

**1. `read_amounts` — the planner hands spelled amounts back to the LLM.** New field on
`qurtoba_plan_transactions` output: `read_amounts: [{message_id, text}]`. When a message carries an amount
written in Arabic WORDS the deterministic parser can't convert (خمسين الف، خمسمائة، ميتين — now correctly
REFUSED rather than mis-read as 1000 per the earlier fix), the tool surfaces it and the `note` explicitly
tells the agent: "read the value yourself (خمسين الف=50000, خمسمائة=500…) and create the op; don't ask the
customer for an amount that's already there in words." Added `_looks_like_spelled_amount()` (multiplier +
hundreds word detector) — verified it fires on خمسين الف/خمسمائة/ميتين and NOT on names (الحرمين/شاكر),
phones, plain numbers, or fee-notes. So the phone still orphans (no wrong number invented) but the value
isn't lost — it's routed to the one component that CAN read it (the LLM).

**2. Deeper examples in both tool descriptions.**
- `qurtoba_plan_transactions`: documented `read_amounts` + fee-note skipping + 4 worked examples drawn
  from real bursts this session (split-pair auto-pairing; الحرمين + fee-note → 3 pairs; self-contained
  «30الف»; «خمسين الف» → read_amounts → LLM reads 50000).
- `qurtoba_create_new_transactions_bulk`: added an explicit VALUE contract (pass a plain int; you read
  Arabic words/separators yourself; خمسين الف→50000, ٥٠٠٠→5000, no fractions), 4 worked examples
  (single / bulk-from-planner / spelled-amount-from-read_amounts / reroute), and a RESULT-STATUS legend
  (created / pending_review / rejected / duplicate / same_day_duplicate → confirm_repeat flow; never «تم»
  before status "created").

Also wired `read_amounts` into `_shared/core.md`'s planner bullet so all agents act on it. Compiled clean,
both tool descriptions verified live in the registry, deployed via `./fu.sh`, services active.

---

## 2026-07-09 — FIXED (TOOL, dangerous): «خمسين الف» parsed as 1000 instead of 50,000

Follow-up to the silent-«خمسمائة» investigation — user asked "is this a tool problem too?" It was, and
WORSE than the silent drop. `normalize_amount` (`tools/_amounts.py`) on a spelled count + multiplier word:
```
خمسين الف (50,000)  -> 1000   🔴  (50× under-charge)
خمسة الاف (5,000)   -> 1000   🔴
عشرة الاف (10,000)  -> 1000   🔴
خمسمائة  (500)     -> could not parse (safe-ish: orphan)
```
Root cause: the multiplier loop reads «الف»→×1000 but can't read the SPELLED count «خمسين» (only digit
counts like «20الف» work). With the count dropped and no digit left, it fell into the "bare multiplier =
1000" branch and returned 1000 — a valid-looking, catastrophically wrong amount that WOULD create a wrong
transaction through the planner. This is far more dangerous than the pure-word case (which fails safely).

**Status: FIXED.** In the "no digits" branch of `normalize_amount`, before returning the bare multiplier,
check for leftover letters in the string — an unparsed spelled count. If present → return
`reason='spelled_count_unknown'`, ok=False (refuse) instead of a wrong 1000. Verified: خمسين الف/خمسة
الاف/عشرة الاف now all refuse; and EVERY legit case still works — الف→1000, الفين→2000, مليون→1M,
30الف→30000, 20 الف→20000, 27.460→27460, plain/arabic digits, names still→noise. Classifier confirmed to
treat «01012745373 خمسين الف» as an orphan phone (agent asks / reads it) rather than a phantom 1000 op.

Combined with the prompt fix from the prior entry (LLM reads Arabic number words itself + never stays
silent on a phone-bearing message), both layers are now safe: the tool refuses to guess a wrong number,
and the agent reads the spelled amount correctly. NOT done (still): a full spelled-Arabic-number parser in
the tool — deferred; the LLM-reads-it layer covers it, and the tool now fails safe instead of wrong.

Deployed via `./fu.sh`, services active.

---

## 2026-07-09 — FIXED: agent stayed completely silent on «01069411663 خمسمائة»

Conversation `d8bc5e42-...`. Customer re-sent «01069411663\nخمسمائة» (500 spelled in Arabic words) —
500→01069411663 كاش had been created 17s earlier in a bulk. Agent got NO response at all. Confirmed via
LangGraph state + journal: the workflow RAN (cash_agent, 09:34:49), called **zero tools**, produced empty
output, logged "Workflow returned empty output … send_message_ids_to_ai active — intentional no-reply".
So the agent deliberately did nothing.

**Two root causes:**
1. **Spelled-out Arabic numbers not parsed** — `normalize_amount('خمسمائة')` → ok=False. To the
   deterministic parser this looks like "a phone with no amount." (Same class as the earlier «خمسين الف».)
   The LLM itself CAN read خمسمائة=500, but nothing told it to when the parser couldn't.
2. **Silence used as an escape hatch** — because `send_message_ids_to_ai` makes an empty reply a VALID
   "intentional no-reply" (no error, no escalation), the agent, when uncertain (spelled amount + a number
   it had just used), chose the laziest safe path: nothing. Customer saw zero acknowledgment.

Correct behavior was: read خمسمائة=500 → attempt the create → tool returns `same_day_duplicate` → ask
«تأكيد تكرار العملية؟». Or if the amount were truly unreadable → «المبلغ لـ 01069411663 كام؟». Either
way: RESPOND.

**Status: FIXED (prompt).** `_shared/core.md` Law 5 (Empty = empty):
- Added: silence is ONLY for a completed success (👍 already sent), NEVER an escape hatch when unsure. A
  message containing a phone number MUST be acted on — create / confirm-repeat / ask-amount — never
  dropped silently. Uncertain ≠ silent.
- Added: **read Arabic number WORDS yourself** (خمسمائة=500، ميتين=200، ألفين=2000…) — the tools can't
  parse spelled-out numbers but the LLM can; «01069411663 خمسمائة» = a 500 transfer.
- Added two examples (the خمسمائة same-day-duplicate case, and a bare orphan phone → always ask).

NOT done: adding spelled-out-number parsing to the deterministic classifier/planner (a bigger feature —
would matter for spelled amounts inside multi-message BURSTS that go through the planner). For single
messages the LLM-reads-it-itself fix covers it. Flagged for later if burst-level spelled amounts recur.

Deployed via `./fu.sh`, core module verified to serve the new rules, services active.

---

## 2026-07-09 — FIXED: messy forwarded burst — planner parsed a fee-note as an amount + agent refused instead of calling the planner

Conversation `13f58d64-4bf1-4b30-aa23-cafd992c7820`. Customer forwarded a burst of split transactions,
each transaction spread across separate messages (phone / amount / «الحرمين» name / «لو هيخصم 15 اخصمها»
fee note), arriving scrambled. Agent replied «البيانات مخلوطة جداً ما قدرش أفهمها … ابعتها واضحة» three
times; customer re-sent 3× and never got served.

**Is it the agent or the tool? BOTH.**

**Tool bug (planner classifier):** ran the real burst through `qurtoba_plan_transactions` — it returned
`01226086860 → 15` (confidence low) and orphaned the real 13,450. The **15 came from the fee note «لو
هيخصم 15 اخصمها»** — `normalize_amount` extracts any number and ignores surrounding words, and since it
"succeeded", the classifier's name/note branch never ran. So a fee-deduction instruction became a phantom
15 EGP transfer AND corrupted the pairing. This would have created a wrong transaction if the agent had
called the tool.

**Agent bug:** it NEVER called the planner (confirmed — today's only tool calls were balance + daily
report). It read the burst itself, decided it was «مخلوطة», and blanket-refused — the exact thing the
prompt already said not to do «BEFORE calling the planner», but the rule was too weak to hold against a
genuinely chaotic burst.

**Status: FIXED (both).**
- `tools/planning.py` `_classify_message`: added `_FEE_NOTE_RE = re.compile(r'خصم|رسوم|عمول|مصاريف')`. A
  line matching it is treated as a note — its number is never extracted as an amount. Re-tested on the
  real burst: now returns the correct **3 pairs (24200 / 13450 / 6760), no phantom 15, no orphans**.
  Regression-checked «5480 جنيه مصري علي» still → 5480 (currency+name is legit noise, untouched).
- `cash/prompt.md`: rewrote the "USE THE PLANNER" rule into an ABSOLUTE, no-exceptions law — any 2+
  message burst with phones/amounts MUST go through the planner first; the agent is FORBIDDEN from
  reading it itself and refusing as «مخلوطة»; on `list_pattern`/low-confidence it CONFIRMS the pairing
  rather than refusing; only asks to resend the whole thing if the planner returns mostly orphans. Added
  a worked example of this exact الحرمين + fee-note burst.
- `_shared/core.md` noise list: added fee-deduction notes («لو هيخصم 15 اخصمها», خصم/رسوم/عمول/مصاريف)
  as explicit noise whose number is never an amount.

Compiled clean, planner re-tested on real data, cash prompt pushed to the live node (byte-verified),
deployed via `./fu.sh`, services active. Also confirmed the earlier `social_sent_at` fix is now
populating (these messages had it set) — though this burst collapsed into 2 send-seconds so intra-second
order is still unknown; the list_pattern-confirm path is what covers that.

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
