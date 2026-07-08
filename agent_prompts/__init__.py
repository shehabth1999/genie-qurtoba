"""
Standalone, importable source for the Qurtoba agents' SHARED CORE prompt.

Why this exists: the AI-Studio workflow function box caps Python at ~10k chars,
but the shared core is ~24k. So the text lives in the extension (no limit), and
the workflow function stays tiny — it just imports and returns the string.

SINGLE SOURCE OF TRUTH: the readable markdown at
    prompts/agents/_shared/core.md
This module reads that file once at import — edit ONLY core.md and both the
readable copy and the deployed prompt update together, no drift. Restart the
workers after editing core.md so the new text is re-read.

Paste this tiny function into the AI-Studio workflow (well under 10k):

    def execute(input_data):
        from qurtoba.agent_prompts import SHARED_CORE
        return SHARED_CORE
"""
import os

_CORE_MD = os.path.join(
    os.path.dirname(__file__), os.pardir,
    "prompts", "agents", "_shared", "core.md",
)

# Read once at import; cached for the life of the process. Restart workers to reload.
with open(_CORE_MD, encoding="utf-8") as _f:
    SHARED_CORE = _f.read()
