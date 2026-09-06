"""`qurtoba_answer_pending` — the model's way to settle what the money path is HOLDING.

The deterministic money path never guesses what a sentence means. When it needs a
yes/no it asks a fixed question and stores what it is waiting for; a bare «حول» /
«أيوة» / «لا» is applied by Python itself, but anything longer («تمام يا معلم
اعملها», «لا مش عايز اكررها») is meaning — the model reads it and calls this tool.
Execution stays deterministic: the tool creates from its OWN stored state, never
from an amount the model types.
"""
from typing import Any, Dict, Optional

from modules.aistudio.tools import tool


@tool(
    name='qurtoba_answer_pending',
    display_name='Answer the money path\'s pending question (yes / no)',
    side_effect=True,
    description=(
        'The system asked the customer a fixed yes/no question and is HOLDING a transfer behind it '
        '(a corrected number «ابعت حول», a positional list «تأكيد المطابقة», a high value «مبلغ كبير — '
        'محتاج تأكيد», a same-day repeat «تحب أكررها؟», a question-shaped message). When the customer '
        'answers in their own words, call this with decision="yes" or "no". The tool executes or drops '
        'the HELD item from its own stored state (it never takes an amount from you) and 👍s what it '
        'creates. Returns {handled, kind, created[], note}. kind=none → nothing was pending; then answer '
        'the customer yourself. Never call it to create something new — use the create tool for that.'
    ),
    category='qurtoba',
    requires_auth=True,
    rate_limit=20,
    parameters_schema={
        'type': 'object',
        'properties': {
            'decision': {'type': 'string', 'enum': ['yes', 'no'],
                         'description': 'What the customer meant: yes (execute the held item) or no (drop it).'},
        },
        'required': ['decision'],
    },
)
def qurtoba_answer_pending(context, decision: str) -> Dict[str, Any]:
    from qurtoba.automation.pending import answer_pending
    conv = getattr(context, 'conversation', None)
    partner = getattr(context, 'partner', None) or getattr(conv, 'social_partner', None)
    if conv is None or partner is None:
        return {'success': False, 'error_type': 'no_conversation', 'error': 'No active conversation in context.'}
    if decision not in ('yes', 'no'):
        return {'success': False, 'error_type': 'invalid_decision', 'error': 'decision must be "yes" or "no".'}
    try:
        return answer_pending(conv, partner, decision, answer_message_id=None)
    except Exception as exc:  # never raise into the agent loop
        return {'success': False, 'error_type': 'exception', 'error': str(exc)}
