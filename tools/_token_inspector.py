"""
Token-usage inspector for the AI Studio ReAct agents (Qurtoba).

WHY THIS EXISTS
---------------
Each agent turn runs as a ReAct loop: every tool call the model makes is a
SEPARATE LLM request that re-sends the WHOLE prompt again — system prompt +
all tool schemas + the entire conversation history. So a turn that fires N
tools costs N+1 full-context *reads*. The node executor only records the LAST
step's usage, which massively under-reports the real input (read) tokens.

This inspector walks EVERY AIMessage the ReAct loop produced, sums the exact
per-step usage reported by Anthropic (input / output / cache-read /
cache-creation), and writes one readable block per turn to a clean log file in
the home folder so the owner can see exactly where the read tokens go.

It is best-effort and NEVER raises into the agent flow.

Log file: ~/token_usage.log  (override with QURTOBA_TOKEN_LOG)
"""
import os
import datetime as _dt

_LOG_PATH = os.environ.get('QURTOBA_TOKEN_LOG') or os.path.expanduser('~/token_usage.log')


def _approx_tokens(text) -> int:
    """Rough offline token estimate. Anthropic ~3.5 chars/token for English;
    Arabic is denser, so we use ~3.0 to avoid under-counting. Used ONLY for the
    relative system/tools/history breakdown — the aggregate totals are the
    model's own exact numbers, not this estimate."""
    if not text:
        return 0
    try:
        return max(1, int(len(str(text)) / 3.0))
    except Exception:
        return 0


def _usage_of(msg):
    """Return (input, output, cache_read, cache_creation) for one AIMessage,
    or None when the message carries no usage metadata."""
    um = getattr(msg, 'usage_metadata', None)
    if not um:
        return None
    try:
        inp = int(um.get('input_tokens') or 0)
        out = int(um.get('output_tokens') or 0)
        details = um.get('input_token_details') or {}
        cread = int(details.get('cache_read') or 0)
        ccreate = int(details.get('cache_creation') or 0)
        return inp, out, cread, ccreate
    except Exception:
        return None


def _breakdown(messages_to_send, adapted_tools):
    """Approximate how the FIRST request's input splits across system prompt,
    tool schemas, and conversation history (by character share)."""
    sys_chars = 0
    hist_chars = 0
    try:
        for m in (messages_to_send or []):
            content = getattr(m, 'content', '')
            text = content if isinstance(content, str) else str(content)
            # LangChain SystemMessage → the system prompt; everything else → history.
            if type(m).__name__ == 'SystemMessage':
                sys_chars += len(text)
            else:
                hist_chars += len(text)
    except Exception:
        pass
    tool_chars = 0
    try:
        for t in (adapted_tools or []):
            tool_chars += len(getattr(t, 'name', '') or '')
            tool_chars += len(getattr(t, 'description', '') or '')
            tool_chars += len(str(getattr(t, 'args_schema', '') or ''))
    except Exception:
        pass
    return {
        'system': _approx_tokens('x' * sys_chars),
        'tools': _approx_tokens('x' * tool_chars),
        'history': _approx_tokens('x' * hist_chars),
        'n_tools': len(adapted_tools or []),
        'n_history_msgs': len(messages_to_send or []),
    }


def log_react_run(final_messages, num_input_messages=0, *, context=None,
                  node_id=None, model_name=None, messages_to_send=None,
                  adapted_tools=None):
    """Append one turn's token accounting to the inspector log. Best-effort."""
    try:
        # 1) exact per-step usage from every AIMessage the loop produced
        steps = []
        for m in (final_messages or []):
            u = _usage_of(m)
            if u is not None:
                steps.append(u)
        if not steps:
            return

        tot_in = sum(s[0] for s in steps)
        tot_out = sum(s[1] for s in steps)
        tot_cread = sum(s[2] for s in steps)
        tot_ccreate = sum(s[3] for s in steps)
        uncached_in = tot_in - tot_cread  # full-price read tokens

        # 2) conversation / message identity (never fabricate — read what's there)
        conv_id = phone = last_msg_id = None
        try:
            conv = getattr(context, 'conversation', None)
            conv_id = str(getattr(conv, 'id', '') or '') or None
            part = getattr(context, 'partner', None) or getattr(context, 'social_partner', None)
            phone = getattr(part, 'phone', None)
            pm = getattr(context, 'partner_message', None)
            last_msg_id = str(getattr(pm, 'id', '') or '') or None
        except Exception:
            pass

        bd = _breakdown(messages_to_send, adapted_tools)

        now = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines = []
        lines.append('=' * 72)
        lines.append(f'[{now}] node={node_id} model={model_name}')
        lines.append(f'conversation={conv_id} phone={phone} last_msg={last_msg_id}')
        lines.append(f'LLM calls this turn (ReAct steps): {len(steps)}')
        # per-step table
        lines.append('  step |   input |  output | cache_read | cache_create')
        for i, (inp, out, cr, cc) in enumerate(steps, 1):
            lines.append(f'  {i:>4} | {inp:>7} | {out:>7} | {cr:>10} | {cc:>11}')
        lines.append('  ' + '-' * 56)
        lines.append(f'  TOTAL input(read)={tot_in}  of which cached={tot_cread}  '
                     f'UNCACHED(full price)={uncached_in}')
        lines.append(f'  TOTAL output={tot_out}  cache_creation={tot_ccreate}')
        lines.append(f'  approx first-call read split: system~{bd["system"]}  '
                     f'tools~{bd["tools"]} ({bd["n_tools"]} tools)  '
                     f'history~{bd["history"]} ({bd["n_history_msgs"]} msgs)')
        # diagnosis
        if tot_cread == 0 and len(steps) > 1:
            lines.append('  ⚠ prompt cache NOT hit: each ReAct step re-read the FULL prompt '
                         'at full price — biggest lever is prompt caching + fewer tool steps.')
        elif len(steps) > 1:
            lines.append(f'  note: {len(steps)} steps × full context. History dominates when '
                         'summarization is off — read tokens scale with steps × history size.')
        lines.append('')

        with open(_LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(lines))
    except Exception:
        # never disturb the agent
        pass
