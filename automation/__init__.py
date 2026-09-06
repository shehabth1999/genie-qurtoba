"""Deterministic money path for the Qurtoba WhatsApp accountant (workflow v2).

Python CREATES: every clean transfer of a turn is created the moment it arrives —
planner, decisions, create tool, answers to our own earlier questions — with no
language model in the path. Python does NOT interpret the customer: whatever the money
path did not settle (a number without an amount, an unreadable amount, a held high
value, a question, a greeting, a complaint) is handed to the thinking model with the
office's suggested wording, and the model decides what to say.

Modules
    lexicon        yes/no answers, transfer-type words, normalisation
    replies        the fixed customer-facing Arabic lines (verbatim from the prompts)
    arabic_numbers amounts written in Arabic words → int
    router         turn loading; receipt-image / off-hours / not-linked routing only
    transfers      the money path: planner → decide → create tool → leftovers for the model
    context        tool calls with chat trace rows, system sends, watermarks
    nodes          the bodies the workflow function nodes import
"""
