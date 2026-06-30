# ARCHIVE — Split-transfer feature (qurtoba_split_transfer)

**Archived:** 2026-06-29
**Why:** The owner decided the AI should NOT divide one total across several numbers. When a
customer asks for that ("قسم/وزّع X على الأرقام دي"), the agent now escalates via
`alert_qurtoba_human` + «لحظة» instead. The "same amount to EACH number" case stays as a normal
bulk. This feature is preserved here in case we re-enable it later.

**What was removed:**
- Code: `qurtoba_split_transfer` tool + helpers (`_split_total_evenly`, `_split_clarification_text`,
  `_send_split_clarification`) and the `clarified` param — from `tools/transactions.py`.
- Registration: `qurtoba_split_transfer` from `tools/__init__.py` (import + `__all__`).
- DB: the `aistudio_tooldefinition` row for `qurtoba_split_transfer` (and it must be removed from the
  agent node's `selected_tools` in the AI Studio UI).
- Prompt (`prompts/ag_static_v2.md`): `<case name="split_total">`, examples A3 & A4, the
  `qurtoba_split_transfer` tool-catalog entry, the split branch of the Execute step, and the split
  pointer at the end of the bulk tool entry.

**Kept (still live):** `_create_debts_batch`, `_send_quoted_text`, `_send_account_correction_reply`,
and `qurtoba_create_new_transactions_bulk` — the split tool used `_create_debts_batch`, but bulk
needs it, so it stays.

---

## To restore
1. Paste the Python block below back into `tools/transactions.py` (helpers near the other
   `_send_*`/`_split_*` helpers; the `@tool` after the bulk tool). Re-add `_create_debts_batch`
   docstring mention if desired.
2. Re-add `qurtoba_split_transfer` to `tools/__init__.py` (import + `__all__`).
3. `fu.sh` → `sync_tools` re-registers it; then add the tool to the Main Agent's tool list in the UI.
4. Paste the prompt block below back into `prompts/ag_static_v2.md` (replace the new escalation
   `<case name="multi_number_one_amount">` rule with the old `<case name="split_total">` + examples).

---

## Python code (from tools/transactions.py)

```python
def _split_clarification_text(total_int, n) -> str:
    """The ambiguity question for a split: divide the total across the numbers, or full to each."""
    return (
        f'تأكيد: تقصد أقسم {total_int} جنيه على الـ{n} أرقام (كل رقم ياخد جزء من المبلغ)؟ '
        f'رد بـ«نعم» للتقسيم، أو قول «{total_int} لكل رقم» لو تقصد المبلغ كامل لكل رقم.'
    )


def _send_split_clarification(conversation, social_partner, src_message_id, total_int, n) -> bool:
    """
    Ask the customer — via the TOOL, quoting their request — whether `total_int` should be
    SPLIT across the n numbers or sent in FULL to each. Nothing is created until they confirm.
    Best-effort; never raises. Returns True if the question was dispatched.
    """
    sent = _send_quoted_text(conversation, social_partner, src_message_id,
                             _split_clarification_text(total_int, n))
    logger.info('qurtoba: tool sent split clarification (total=%s, n=%s, sent=%s)', total_int, n, sent)
    return sent


def _split_total_evenly(total_int: int, n: int) -> List[int]:
    """Split `total_int` whole pounds across `n` parts that sum EXACTLY to total_int.

    Each part is a whole integer; the parts differ by at most 1; the extra pound(s)
    (the remainder) land on the LAST parts, so 75/2 → [37, 38] and 100/3 → [33, 33, 34].
    Caller MUST guarantee n >= 1 and total_int >= n, which makes every part >= 1.
    """
    base = total_int // n
    rem = total_int - base * n          # 0 <= rem < n
    return [base] * (n - rem) + [base + 1] * rem


# ---------------------------------------------------------------------------
# Split ONE cash total across several numbers (قسم/وزّع التحويلة) — cash only
# ---------------------------------------------------------------------------

@tool(
    name='qurtoba_split_transfer',
    side_effect=True,  # mutating: creates debt records — never carry its result forward
    display_name='Split one cash total across several numbers',
    description=(
        'Use this tool ONLY when the customer asks to DIVIDE one single total across several '
        'destination numbers (e.g. «قسم التحويلة دي على الأرقام دي» / «وزّع المبلغ ده عليهم» '
        'followed by a list of numbers and ONE total amount). CASH ONLY (كاش). '
        'THE TOOL does the division into whole pounds — you must NEVER compute the per-number '
        'amount yourself and NEVER pass pre-divided amounts; pass only the numbers and the one total. '
        'INPUT: '
        '1) account_numbers — array of the destination Egyptian mobiles, AS THE CUSTOMER WROTE '
        'them (the tool normalizes +20/0020/spaces to 01XXXXXXXXX). At least 2 numbers. '
        '2) total_value — the SINGLE total amount in EGP to divide across them (positive whole number). '
        '3) source_message_id (STRONGLY preferred) — the chat message id (UUID) of the message that '
        'holds the request (it contains all the numbers); each created transaction quotes it. '
        '4) clarified (bool, default False) — the split-vs-each AMBIGUITY gate (see CLARIFICATION). '
        '5) override_grade_limit (optional, default False) — leave False to enforce the credit ceiling. '
        '6) notes (optional). '
        'CLARIFICATION (critical): «حولي الرقمين دول 90001» is AMBIGUOUS — split 90001 ACROSS the '
        'numbers, or 90001 to EACH number? NEVER guess with money. If the customer was not explicit, '
        'call with clarified=False (the default): the TOOL itself asks the customer (quoting their '
        'message) and creates NOTHING, returning awaiting_clarification=True — you then stay SILENT '
        '(the tool already asked; do not write the question yourself). When the customer confirms the '
        'split (e.g. «نعم» / «قسّمها»), call again with clarified=True to execute. Set clarified=True '
        'up-front ONLY when the request is already explicit about splitting («قسم/وزّع … على الأرقام '
        'دي»). If the customer instead means the FULL amount to EACH number, that is NOT a split → use '
        'qurtoba_create_new_transactions_bulk with that amount per number. '
        'BEHAVIOUR (when clarified=True): '
        '- The tool splits total_value into whole-pound parts that sum EXACTLY to the total, each '
        'part >= 1, differing by at most 1 (90000 across 3 → 30000/30000/30000; 75 across 2 → 37/38). '
        'It then creates ONE كاش transaction per number (auto-bracketed by amount) and sends ONE 👍 — '
        'so on success stay SILENT, exactly like the bulk tool. '
        '- Number correction is handled by the tool itself per created number — never send it. '
        '- Returns the usual created/pending/rejected summary PLUS split_total, parts, per_number. '
        '- REJECTS (success=False) with a ready Arabic `error`: need_two_numbers (fewer than 2 '
        'numbers), non_integer_total (total has a fraction), total_too_small (total smaller than the '
        'count, can\'t give each >= 1). Send that message to the chat. '
        'GUARDRAILS: '
        '- Use this ONLY for one-total-many-numbers. If each number has its OWN amount, that is a '
        'normal burst → use qurtoba_create_new_transactions_bulk (with the planner), NOT this tool. '
        '- Never compute or pass the per-number amounts. Never set clarified=True yourself unless the '
        'split intent is explicit or the customer just confirmed it.'
    ),
    category='qurtoba',
    requires_auth=True,
    parameters_schema={
        'type': 'object',
        'properties': {
            'account_numbers': {
                'type': 'array',
                'minItems': 2,
                'maxItems': 50,
                'items': {'type': 'string'},
                'description': 'Destination Egyptian mobiles to split across, as written '
                               '(tool normalizes +20/0020/spaces to 01XXXXXXXXX). At least 2.',
            },
            'total_value': {
                'type': 'number',
                'exclusiveMinimum': 0,
                'description': 'The SINGLE total amount in EGP to divide across the numbers. '
                               'Whole number — the tool does the division; never pre-divide.',
            },
            'source_message_id': {
                'type': ['string', 'null'],
                'description': 'UUID of the message holding the split request (from its '
                               '"[message_id: <uuid>]" marker). It contains all the numbers.',
            },
            'clarified': {
                'type': 'boolean',
                'default': False,
                'description': 'Split-vs-each ambiguity gate. False (default) → the tool ASKS the '
                               'customer (split across, or full to each?) and creates NOTHING '
                               '(awaiting_clarification). True → execute the split — set True only '
                               'when the request is explicitly «قسم/وزّع» or the customer just '
                               'confirmed the split.',
            },
            'override_grade_limit': {
                'type': 'boolean',
                'default': False,
            },
            'notes': {
                'type': ['string', 'null'],
            },
        },
        'required': ['account_numbers', 'total_value'],
    },
)
def qurtoba_split_transfer(
    context,
    account_numbers: List[str],
    total_value: float,
    source_message_id: Optional[str] = None,
    clarified: bool = False,
    override_grade_limit: bool = False,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    conv, customer, err = _resolve_conversation_and_customer(context)
    if err:
        return err

    # Need at least two numbers to split across.
    if not isinstance(account_numbers, list) or len(account_numbers) < 2:
        return {
            'success': False,
            'error_type': 'need_two_numbers',
            'error': 'محتاج رقمين على الأقل عشان أقسم المبلغ عليهم.',
        }

    # Deterministic amount normalization (Arabic/ASCII digits, separators). The split is in
    # whole pounds, so the total itself must be a whole number (no fractional split).
    norm = normalize_amount(total_value)
    if not norm['ok']:
        return {'success': False, 'error_type': 'invalid_value', 'error': 'المبلغ يجب أن يكون رقم موجب.'}
    total = norm['value']
    if total <= 0:
        return {'success': False, 'error_type': 'invalid_value', 'error': 'المبلغ يجب أن يكون أكبر من صفر.'}
    total_int = int(round(total))
    if total_int != total:
        return {
            'success': False,
            'error_type': 'non_integer_total',
            'error': 'مبلغ التقسيم لازم يكون رقم صحيح بدون كسور.',
        }

    n = len(account_numbers)
    # Each part must be >= 1, so the total can't be smaller than the count.
    if total_int < n:
        return {
            'success': False,
            'error_type': 'total_too_small',
            'error': 'المبلغ صغير على التقسيم — لازم يكون على الأقل بعدد الأرقام.',
        }

    # --- Clarify intent BEFORE creating anything ------------------------------
    # «حولي الرقمين دول 90001» is AMBIGUOUS: split 90001 ACROSS the numbers, or send
    # 90001 to EACH? Never guess with money. Unless the agent already confirmed the
    # intent (clarified=True — explicit «قسم/وزّع», or the customer answered «نعم»),
    # the TOOL itself asks (quoting the request) and creates NOTHING. The agent then
    # re-calls with clarified=True once the customer confirms the split.
    if not clarified:
        sent = _send_split_clarification(
            conv, getattr(conv, 'social_partner', None), source_message_id, total_int, n)
        return {
            'success': True,
            'awaiting_clarification': True,
            'clarification_sent': sent,
            'split': True,
            'split_total': total_int,
            'count': n,
            'note': 'asked the customer to confirm split-across vs full-to-each; nothing created. '
                    'Re-call with clarified=True only after the customer confirms the split.',
        }

    parts = _split_total_evenly(total_int, n)

    # One كاش item per (number, part). Same source_message_id for every item — the request
    # message contains all the numbers, so each passes _create_one_debt's source check. The
    # كاش bracket (كاش/كاش(10)/كاش(20)) is picked per part by _validate_debt_item.
    items = [
        {
            'type': 'كاش',
            'value': part,
            'account_number': num,
            'source_message_id': source_message_id,
            'notes': notes,
        }
        for num, part in zip(account_numbers, parts)
    ]

    batch = _create_debts_batch(conv, customer, items, override_grade_limit, source_message_id)
    # Augment with the split breakdown (do NOT clobber the batch's own 'total' = item count).
    batch.update({
        'split': True,
        'split_total': total_int,
        'parts': parts,
        'per_number': [
            {'account_number': num, 'value': part}
            for num, part in zip(account_numbers, parts)
        ],
    })
    return batch
```

### tools/__init__.py registration (removed lines)
```python
# in:  from .transactions import ( ... )
    qurtoba_split_transfer,
# in:  __all__ = [ ... ]
    "qurtoba_split_transfer",
```

---

## Prompt parts (from prompts/ag_static_v2.md)

### Execute step (the split branch that was in step 9)
```
**But** if it's ONE total to divide across several numbers (قسم/وزّع … على الأرقام دي) → `qurtoba_split_transfer` with the numbers + the single total (see <case name="split_total">) — never the planner/bulk for that, never split the amount yourself.
```

### `<case name="split_total">`
```xml
<case name="split_total">**Distinct from `<case name="split">`.** Several numbers share ONE amount. Call `qurtoba_split_transfer` with every number + the single `total_value` + the request message's `source_message_id`. **Do NOT call the planner and do NOT call the bulk tool** for it (the planner pairs positionally and would dump the whole total on the first number). **Never compute the per-number amount yourself — the tool divides it.**
  **Three readings — read intent, never guess money:**
  - **Explicit split** («قسم/وزّع … على الأرقام دي») → call with `clarified=true` → the tool divides and executes.
  - **Explicit each** («{مبلغ} لكل رقم» / «نفس المبلغ لكل واحد») → that's NOT a split → use the **bulk** tool with that amount on every number.
  - **Ambiguous** (just several numbers + ONE amount, no «قسم» and no «لكل رقم» — e.g. «حولي الرقمين دول 90001») → call `qurtoba_split_transfer` with `clarified=false` (the default). **The tool itself asks** the customer (split-across vs full-to-each) and creates nothing; you **send nothing**. When the customer answers «نعم/قسّمها» → call again with `clarified=true`; if they answer «لكل رقم» → use the bulk tool with that amount per number.
  **Never run the split until the intent is confirmed.** The rule: many numbers + ONE amount, intent unclear → ask via `clarified=false`; confirmed split → `clarified=true`; amount-to-each → bulk.</case>
```

### Bulk tool entry — split pointer (last sentence that was removed)
```
To DIVIDE one single total across several numbers, use `qurtoba_split_transfer` instead — not this tool.
```

### `<tool name="qurtoba_split_transfer">` catalog entry
```xml
<tool name="qurtoba_split_transfer">Split ONE single total across several numbers — **cash only**. Use when several numbers share ONE total amount. Pass `account_numbers` (all numbers, as written), `total_value` (the one total), `source_message_id` (the request message's id), and `clarified` (see next). **The TOOL does the division into whole pounds — never compute the per-number amount yourself, never pass pre-divided amounts.** **AMBIGUITY GATE (`clarified`, default false):** «حولي الرقمين دول 90001» could mean split 90001 ACROSS them OR 90001 to EACH — never guess. If the customer was NOT explicit, call with `clarified=false`: the **tool itself asks** the customer (split-across vs full-to-each) and creates NOTHING (returns `awaiting_clarification=true`) — you then **send nothing** (the tool asked; do not write the question yourself). When the customer confirms the split («نعم/قسّمها»), call again with `clarified=true` to execute. Set `clarified=true` up-front ONLY when the request is already explicit («قسم/وزّع … على الأرقام دي»). If the customer means the FULL amount to EACH number → that's NOT a split → use the bulk tool with that amount per number. On execute it creates one كاش op per number and sends one 👍 (stay silent; number correction is handled by the tool). May reject `need_two_numbers` / `non_integer_total` / `total_too_small` — send the Arabic message. Never call the planner for this.</tool>
```

### Examples A3 & A4
```xml
<example id="A3" title="EXPLICIT split (قسم) → split tool, clarified=true, NOT bulk">
<input>قسم التحويلة دي على الأرقام دي ⏎ 01025294594 ⏎ 01010754380 ⏎ 01005459442 ⏎ 90000</input>
<logic>Explicit «قسم» → unambiguous split → `qurtoba_split_transfer` with account_numbers=[the 3 numbers], total_value=90000, clarified=true, source_message_id=this message. The TOOL divides (30000 each) — never compute the per-number amount, never use the planner/bulk. Contrast A2 where each number had its OWN amount. See <case name="split_total">.</logic>
<reply>(none)</reply>
</example>

<example id="A4" title="AMBIGUOUS one-amount-many-numbers → tool asks (clarified=false), you stay silent">
<input>[message_id: q1] 01210753280 / 01025294594 ⏎ [message_id: q2] حولي الرقمين دول 90001</input>
<logic>Two numbers + ONE amount, NO «قسم» and NO «لكل رقم» → ambiguous (split 90001 across them, or 90001 each?). Call `qurtoba_split_transfer` with account_numbers=[both], total_value=90001, source_message_id=q2, **clarified=false** (default). The TOOL asks the customer itself and creates NOTHING (awaiting_clarification) → you send NOTHING. NEXT turn: customer «نعم» → call again with clarified=true (executes 45000/45001); customer «90001 لكل رقم» → use the bulk tool with 90001 on each number.</logic>
<reply>(none)</reply>
</example>
```
