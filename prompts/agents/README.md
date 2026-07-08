# Qurtoba Agents — v3-style, split prompts

New prompt style (v3: plain structured text, no XML) carrying **all** of v2's data + every example,
split from one monolith into a router + 3 focused specialists that share a common core.

## Layout
```
agents/
  _shared/core.md          ← prepended to EVERY agent: laws, reading rules, message linking,
                             silence contract, lifecycle, tools, and the SHARED ROLES
                             (greeting/thanks, balance, daily report, status, cancellation,
                             human alert, working hours, scope).
  big_brain/prompt.md      ← brain — router; classifies the burst, forwards to ONE
                             specialist, or answers a pure social/info turn itself.
  cash/prompt.md             ← cash_agent — كاش transfers (phone/wallet).
  fawry_aman_tayer/prompt.md ← fawry_aman_tayer_agent — فورى / أمان / طاير (registered accounts).
  payments/prompt.md         ← payments_agent — سداد with a receipt image.
```

## Composition
Each agent's runtime system prompt = **`_shared/core.md`  +  that agent's `prompt.md`**.
The core is written once; specialists only add their specialty (create/analyze/guard/examples).

## Per-file format
Every `prompt.md` starts with three fields, then the body:
```
name:        <the agent id — also the tool name a peer uses to forward to it>
description: <one line: what it does / when to route to it>
prompt:      <the specialty system prompt, on top of _shared/core.md>
```

## Forwarding graph (one-way; NOTHING returns to the brain)
```
                    ┌─────────────┐
     inbound  ───►  │    brain    │  (router; also answers pure social/info turns)
                    └──────┬──────┘
          ┌────────────────┼────────────────────┐
          ▼                ▼                     ▼
     cash_agent   fawry_aman_tayer_agent   payments_agent
       (كاش)        (فورى / أمان / طاير)      (سداد + receipt)
          ▲  ▲             ▲   ▲                 ▲   ▲
          │  └─────────────┘   └─────────────────┘   │
          └──── peers call each other as tools to forward an out-of-lane item ────┘
```
- The brain forwards **once** to a specialist (platform capability — no need to spell routing out in its prompt).
- A specialist can **call a peer as a tool** to hand off an out-of-lane part (e.g. cash agent gets a fawry item → calls `fawry_aman_tayer_agent`).
- **No agent ever calls back to the brain.** The receiving agent owns the decision and the reply.
- Every agent handles ALL SHARED ROLES itself (greeting, balance, daily, status, cancel, human alert) — those are never forwarded.

## Source lineage
- **Style / structure:** `../ag_static_v3.md` (v3).
- **Data + examples:** `../ag_static_v2.md` (v2, the last stable) — all 23 examples preserved and distributed:
  - universal reading/behavior/silence/lifecycle/status/cancel/alert examples → `_shared/core.md`
  - A1–A4, B1, M2, VOICE1, C1–C4, C6, M3, K1 → `cash`
  - D1, C5, E3, E4, VOICE2, VOICE3 → `fawry_aman_tayer`
  - H1–H6 → `payments`
- Off-hours agent is separate and unchanged (`../ag_off_hours_static_v2.md` / `_v3.md`).
- v2 remains the last stable prompt; these files are the v3 successor and don't overwrite it.
