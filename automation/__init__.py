"""Deterministic automation for the Qurtoba WhatsApp accountant (workflow v2).

Every RULE of the accountant — reading a burst, pairing numbers with amounts,
creating the transfers, the fixed Arabic replies, balance / statement / status /
cancellation / courtesy — runs here as plain Python, without a language model.
The AI-Studio workflow nodes are thin wrappers (see ``nodes.py``); a small model
only ever sees a turn this package could not classify (``router.Intent.FREETEXT``)
or a payment receipt image.

Modules
    lexicon        keyword tables + text normalisation
    replies        the fixed customer-facing Arabic lines (verbatim from the prompts)
    arabic_numbers amounts written in Arabic words → int
    router         burst → intent (pure ``classify_rows`` + DB wrapper ``route``)
    transfers      the money path: planner → decisions → create tool → quoted replies
    intents        balance / statement / status / cancel / social / off-hours handlers
    context        tool context, sending helpers, chat trace rows
    nodes          the bodies the workflow function nodes import
"""
