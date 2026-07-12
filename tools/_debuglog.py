"""Lightweight append-only debug log for the Qurtoba transaction tools.

Writes ONE compact JSON line per event to ``~/qurtoba_agent.log`` (override with the
``QURTOBA_DEBUG_LOG_PATH`` Django setting) so a human can grep by conversation id,
message id, phone or record id and see EXACTLY what a tool decided — with enough
metadata to reproduce a bug fast, without digging through gunicorn/celery output.

Design goals:
  * DIRECT — one line per event, short stable keys, Arabic kept readable (no \\uXXXX).
  * SAFE   — best-effort; never raises, never blocks a tool call (all writes guarded).
  * BOUNDED — rotates to ``.1`` past ``_MAX_BYTES`` so it can't fill the disk.

Line shape (keys always in this order where present):
  {"ts": "...", "ev": "planner", "conv": "d8bc5e42", "cust": 123, ...event fields}

Read it live with:  tail -f ~/qurtoba_agent.log
Filter a chat with:  grep '"conv": "d8bc5e42"' ~/qurtoba_agent.log
Filter a message:    grep '<message-uuid>' ~/qurtoba_agent.log
"""
import json
import os
import threading

_MAX_BYTES = 5 * 1024 * 1024   # 5 MB → rotate to .1 (keeps one previous file)
_lock = threading.Lock()


def _log_path() -> str:
    try:
        from django.conf import settings
        p = getattr(settings, 'QURTOBA_DEBUG_LOG_PATH', None)
        if p:
            return os.path.expanduser(p)
    except Exception:
        pass
    return os.path.expanduser('~/qurtoba_agent.log')


def _now_str() -> str:
    """Cairo-local 'YYYY-MM-DD HH:MM:SS' — matches how the team reads the chat."""
    try:
        from django.utils import timezone
        return timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        try:
            from datetime import datetime
            return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return '?'


def _short(v, n=8):
    """First `n` chars of an id/uuid so lines stay skimmable (full id still greppable
    where it matters — message/record ids are logged in full)."""
    s = str(v)
    return s[:n] if len(s) > n else s


def _rotate_if_big(path: str) -> None:
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            os.replace(path, path + '.1')
    except OSError:
        pass


def log_event(event: str, conversation=None, customer=None, **fields) -> None:
    """Append one JSON line describing a tool decision. Best-effort; swallows all errors.

    `event`  — short verb tag ('planner', 'create', 'create_batch', 'error', ...).
    `conversation` / `customer` — optional model instances; their ids are added as
    `conv` (short) and `cust`. Everything else is passed as-is in **fields.
    """
    try:
        rec = {'ts': _now_str(), 'ev': event}
        if conversation is not None:
            cid = getattr(conversation, 'id', conversation)
            if cid is not None:
                rec['conv'] = _short(cid)
        if customer is not None:
            rec['cust'] = getattr(customer, 'pk', customer)
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except Exception:
        return
    try:
        path = _log_path()
        with _lock:
            _rotate_if_big(path)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass
