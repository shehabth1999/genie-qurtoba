# Bug Report — Qurtoba Partner Flow: spurious "empty output" retry → duplicate WhatsApp reply

> **Audience:** backend / AI-platform developer.
> **Status:** diagnosed, not yet fixed. This document is the hand-off.
> **Severity:** High — partner receives the same WhatsApp message twice and the agent's
> `one_reply` law is silently broken; every occurrence also burns a full extra model call.

---

## 1. TL;DR

When the AI agent answers a turn by calling the **`whatsapp_reply_to_message`** tool
(instead of returning normal assistant text), the agent's final text output is empty *by
design* — the tool already sent the message as a side effect. The workflow runner mistakes
this empty output for a failure and **re-runs the entire turn**, which calls the send-tool a
second time and delivers a **duplicate WhatsApp message** to the partner.

- **What fails:** the retry guard's assumption that *"a turn with no text output = a failed turn."*
- **Which tool surfaces it:** `whatsapp_reply_to_message` (any tool that sends the reply itself).
- **Result:** duplicate outbound message + violation of the `one_reply` rule + ~37k wasted tokens per retry.

---

## 2. How it was triggered (real production trace)

- **Conversation:** `b6ab0c14-9ae6-4282-80e7-a423a29930ca`
- **Workflow:** `2` (type `partner_flow`), model **Claude Haiku 4.5**
- **Partner inbound** (message `81cd7ee7-2494-4bbc-8491-a1881f2a681b`):
  ```
  0100600100
  500
  ```

`0100600100` is **10 digits** — not a valid Egyptian number (must be 11, `01XXXXXXXXX`).
This is exactly the case where the prompt instructs the agent to reply
"من فضلك ارسل رقم صحيح" using `whatsapp_reply_to_message` as a **quoted reply** on the bad
number (see prompt examples `K1`, and `<mention_and_clarify>` / `<phones>` in `agent_static.md`).

### Timeline

| Time (16:39–16:40) | Event |
|--------------------|-------|
| 39:41 | Workflow run **#1** starts; service-availability function node returns OK (all services enabled). |
| 39:46 | 1st Anthropic call returns — agent decides to reply via the tool. |
| 39:47 | Outbound text message **`e0303089`** created + broadcast → **reply #1 sent to partner**. |
| 39:51 | Agent finishes: *"37305 tokens, 7 tools"*. Final text is empty → `__output__ = ''`. |
| 39:51 | Log: `Workflow ... completed successfully` **immediately followed by** `Workflow attempt 1 failed ... retrying... Error: empty output`. |
| 39:52 | Workflow run **#2 (retry)** starts — brand-new execution id, agent re-billed. |
| 40:02 | Outbound text message **`40d7e9a9`** created → **reply #2 (DUPLICATE) sent to partner**. |
| 40:06 | Agent finishes again: *"37564 tokens, 7 tools"*, empty output again. |

**Two outbound messages (`e0303089`, `40d7e9a9`) for one partner turn = the duplicate.**

---

## 3. Root cause

The defect is a wrong assumption baked into the retry guard: **"no text output = failure."**
That is false for any tool that performs the user-visible action itself.

The three links in the chain:

1. **The tool that triggers it — `whatsapp_reply_to_message`**
   `modules/aistudio_whatsapp/tools/messaging.py` (~lines 150–159). It **sends** the WhatsApp
   message as a side effect via `api_service.send_and_broadcast(...)`. After it runs, the agent
   has nothing left to *say*, so its final assistant message is empty.

2. **The agent node records that empty text as the output**
   `modules/aistudio/engines/node_executor.py` (~lines 1463–1471 and 1529–1530):
   `final_message.content` is `''` → `__output__ = ''`.

3. **The retry guard treats empty output as an error**
   `modules/aistudio_whatsapp/tasks.py` (~lines 337–374):
   ```python
   is_error = (
       not result.success
       or not result.output            # ← empty output counted as failure
       or has_error_in_output(str(result.output))
       or orphan_tag
   )
   ```
   `not result.output` is `True` → logs `"empty output"` → loops and **re-runs the whole turn**,
   which calls the send-tool again → the second (duplicate) WhatsApp message.

> Note: the final post-loop check (`output_is_sendable`, `tasks.py` ~line 377) correctly avoids
> sending the *empty agent text* as a message. But that does not help here — the **tool** already
> sent the real reply on each attempt, so the damage is the re-run itself, not the empty text.

---

## 4. Why this "failure expectation" is wrong (and how to not repeat it)

The retry heuristic conflates two genuinely different situations:

- **Real failure** — the agent produced no output **and took no externally-visible action**
  (model truncation, crash, returned nothing). *Retrying is correct.*
- **Legitimate empty output** — the agent's action **was** a side-effecting tool that already
  sent/performed the reply (`whatsapp_reply_to_message`, and any future communication send tool).
  *Retrying is harmful — it duplicates the action.*

To stop this whole class of bug from coming back, the rule must key off **the action, not the text**:

1. **Empty output must not count as failure when a side-effecting communication/messaging tool
   succeeded this turn.** The agent node already enumerates the tool messages it ran
   (`node_executor.py` ~1481–1516). Surface a flag — e.g. set `result.sent_via_tool = True` when a
   tool of category `communication` returns `success=True` — and make the `tasks.py` guard skip the
   empty-output retry when that flag is set.
2. **Make send-tools self-describe as "answered".** Any tool that itself sends the outbound message
   should mark the turn complete, independently of trailing text, so new send-tools added later don't
   re-step the same trap.
3. **Add send idempotency as defense-in-depth.** Reject an identical outbound (same conversation +
   same text within N seconds) so that *any* future double-run — not only this one — cannot deliver
   the same message twice.

---

## 5. Secondary symptom (likely same root cause)

`Object not found: Message matching query does not exist.` — emitted by
`modules/whatsapp/tasks.py` → `process_handling_message` (~lines 174–175). It fires around the
duplicate / racing sends. Most likely a knock-on effect of the re-run, not an independent bug.
Re-check after the primary fix; if it persists, investigate the message-id lookup in that task.

---

## 6. Prompt change requested — pass the **message_id (UUID)**, not the message content

Add the following to the Qurtoba agent prompt (Arabic, to match `agent_static.md` /
`agent_dynamic.md`) so the agent never passes the message text / phone / amount where the tool
expects the message **UUID**. Suggested home: inside `<tools>` next to `whatsapp_reply_to_message`,
or inside `<mention_and_clarify>`.

```xml
<reply_tool_id_rule priority="high">
  عند استدعاء whatsapp_reply_to_message مرّر في حقل message_id **معرّف الرسالة (UUID)**
  المأخوذ حرفياً من علامة [message_id: ...] بجوار رسالة الشريك — **وليس نص الرسالة ولا رقم
  التليفون ولا المبلغ**. الـ message_id صيغته UUID مثل
  81cd7ee7-2494-4bbc-8491-a1881f2a681b. أمّا حقل text فهو **نص ردّك أنت فقط**.
  لا تخلط بينهما: message_id = معرّف الرسالة التي ترد عليها، text = كلامك.
  لا تخترع معرّفاً ولا تستعمل معرّف رسالة outbound.
</reply_tool_id_rule>
```

> The tool already defends with a UUID regex that strips the `[message_id: ...]` wrapper
> (`messaging.py` `_UUID_RE`), but the explicit instruction removes the ambiguity at the source.

---

## 7. Honest caveats on the trace evidence

- The log line *"7 tools"* means **7 tools were loaded/available**, not that 7 were *called*.
- The identification of `whatsapp_reply_to_message` as the specific tool is therefore an
  **inference** — a strong one: it is the only tool that sends a message as a side effect, an
  outbound text was created while `__output__` stayed empty, and the 10-digit number is precisely
  the prompt's "reply with a quoted bad-number warning" case.
- To nail it down beyond doubt, capture the actual `tool_calls` / tool names per attempt
  (e.g. log the names of the tools the agent actually invoked in the agent node).

---

## 8. Suggested fix checklist (for the implementer)

- [ ] Agent node: set `sent_via_tool = True` (or equivalent) when a successful tool of category
      `communication` ran this turn — `modules/aistudio/engines/node_executor.py`.
- [ ] Retry guard: treat empty output as **success** when `sent_via_tool` is set —
      `modules/aistudio_whatsapp/tasks.py` (~337–374).
- [ ] (Defense-in-depth) Idempotent outbound send — drop identical (conversation, text) within N seconds.
- [ ] Prompt: add the `<reply_tool_id_rule>` block above to the Qurtoba agent prompt.
- [ ] Re-verify the `Message matching query does not exist` warning is gone after the fix.
- [ ] Regression test: invalid 10-digit number → exactly **one** outbound reply, no retry.
