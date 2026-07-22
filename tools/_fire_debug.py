"""
Best-effort DEBUG file logger for AI workflow FIRING + SENDING — to diagnose
"the agent replied twice / talks too much".

Writes one line per event to ~/ai_fire_debug.log so a single conversation's
QUEUE -> FIRE(claim) -> SENT trace is greppable and it is obvious whether a
message batch fired the workflow once or twice:

    grep 'conv=<id>'   ~/ai_fire_debug.log   # full trace for one conversation
    grep 'stage=FIRE'  ~/ai_fire_debug.log   # every workflow fire (1 per batch = healthy)
    grep 'stage=QUEUE' ~/ai_fire_debug.log   # every schedule

Never raises into the caller — logging must not affect message handling.
"""
import os

_LOG_PATH = os.path.expanduser('~/ai_fire_debug.log')


def log_fire(stage, *, conversation=None, chat_key=None, **fields):
    """Append one debug line. `stage` is QUEUE / FIRE / SENT (free-form)."""
    try:
        from django.utils import timezone
        ts = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')
        conv_id = getattr(conversation, 'id', None) or conversation or '-'
        parts = [f'[{ts}]', f'stage={stage}', f'conv={conv_id}']
        if chat_key:
            parts.append(f'chat_key={chat_key}')
        for k, v in fields.items():
            sv = str(v)
            if len(sv) > 300:
                sv = sv[:300] + '…'
            parts.append(f'{k}={sv}')
        with open(_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(' '.join(parts) + '\n')
    except Exception:
        pass
