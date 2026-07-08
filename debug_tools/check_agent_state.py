#!/usr/bin/env python
"""
Show the CURRENT live agent state for a WhatsApp conversation — which agent is
the sticky handler right now, the last node that ran, the handoff chain, etc.

Reads directly from the LangGraph checkpoint (Postgres, LANGGRAPH_DB_URL), the
same state the workflow engine itself reads/writes. Read-only — safe to run
anytime, does not touch the DB.

NOT under qurtoba/scripts/ on purpose: `./fu.sh` auto-executes every .py file
in each app's scripts/ folder during sync_all — this one must never run there.

Usage:
    cd /home/genie/genie
    ./.venv/bin/python /home/genie/extensions/qurtoba/debug_tools/check_agent_state.py <conversation_id>

Example:
    ./.venv/bin/python /home/genie/extensions/qurtoba/debug_tools/check_agent_state.py \
        d8bc5e42-6288-4c40-8479-3e1d78446a1f
"""
import os
import sys
import json

# --- Bootstrap Django (mirrors manage.py) ---------------------------------
sys.path.insert(0, '/home/genie/genie')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')
import django
django.setup()

import logging
logging.disable(logging.CRITICAL)  # silence app-init log spam

from langgraph.checkpoint.postgres import PostgresSaver


def get_langgraph_conn_string():
    """Read LANGGRAPH_DB_URL the same way the app does: via python-decouple,
    which parses .env directly WITHOUT populating os.environ. Both
    os.getenv('LANGGRAPH_DB_URL') and settings.LANGGRAPH_DB_URL are None in
    every shell/script context (verified) — only decouple.config() sees it.
    NO silent fallback to the main Django DB: that DB doesn't hold these
    checkpoints, and falling back to it would show real-looking-but-wrong
    (Django DB's leftover/None) state and quietly mislead debugging.
    """
    from decouple import config as decouple_config
    url = decouple_config('LANGGRAPH_DB_URL', default=None)
    if not url:
        raise RuntimeError(
            "LANGGRAPH_DB_URL not found in .env — refusing to fall back to "
            "the main Django DB (it doesn't hold these checkpoints)."
        )
    return url


def show_agent_state(conversation_id: str, thread_prefix: str = 'whatsapp'):
    thread_id = f'{thread_prefix}_{conversation_id}'
    conn_string = get_langgraph_conn_string()

    with PostgresSaver.from_conn_string(conn_string) as saver:
        config = {'configurable': {'thread_id': thread_id}}
        tup = saver.get_tuple(config)

        if not tup:
            print(f'NO CHECKPOINT FOUND for thread_id={thread_id!r}')
            print('(no workflow has ever run for this conversation, or it uses a')
            print(' different thread prefix — webbot uses "webbot_", not "whatsapp_")')
            return

        cv = tup.checkpoint.get('channel_values', {})

        print(f'=== Agent state for conversation {conversation_id} ===')
        print(f'thread_id:          {thread_id}')
        print(f'current_agent:      {cv.get("current_agent")}')
        print(f'__last_node__:      {cv.get("__last_node__")}')
        print(f'__step__:           {cv.get("__step__")}')
        print(f'__handoff_chain__:  {cv.get("__handoff_chain__")}')
        print(f'__started_at__:     {cv.get("__started_at__")}')
        print(f'__workflow_id__:    {cv.get("__workflow_id__")}')

        msg = cv.get('message')
        if msg:
            print(f'\nlast input message:\n  {msg[:300]}')

        out = cv.get('__output__')
        print(f'\n__output__ (final reply this run, first 300 chars):')
        print(f'  {(out or "")[:300]!r}')

        node_results = cv.get('__node_results__') or {}
        if node_results:
            print(f'\nnode_results keys this run: {list(node_results.keys())}')

        print(f'\ncheckpoint metadata: {tup.metadata}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    show_agent_state(sys.argv[1])
