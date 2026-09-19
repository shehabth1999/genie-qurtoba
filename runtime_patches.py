"""Runtime patches of CORE behaviour, installed from apps.py — the pattern ai_guard uses.

Core is never edited on this box; what the tenant needs from core is changed here, at import
time, on the objects core itself looks up by name. Each patch is idempotent and independent;
a failed one is logged and the others still install.

Why (chat 13f58d64, 14–19 Sep 2026):
  * every graph is compiled with LangGraph ``debug=True`` (workflow_engine.py), so LangGraph
    ``print()``s the whole state on every step; Celery redirects that to WARNING and the journal
    kept two days of it — and the lines that mattered were gone;
  * the model client had no timeout (SDK default 600 s × 2 retries) and DeepSeek's «900-second
    timeout» error is a plain ValueError core does not treat as "provider unavailable", so five
    turns waited 15 minutes each and never failed over to the backup model;
  * a WhatsApp «sent»/«delivered» status that arrives before the outbound chat row is committed
    is dropped for good (19 of 76 rows stuck at «saved»).
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


def install() -> None:
    for name, fn in (
        ('langgraph state dumps silenced', _silence_langgraph_debug),
        ('LLM calls bounded (timeout / retries)', _bound_llm_calls),
        ('LLM failover widened (timeouts, DeepSeek queue error)', _widen_llm_failover),
        ('WhatsApp status retry on a missing row', _retry_missed_statuses),
    ):
        try:
            fn()
            logger.info('qurtoba runtime patch: %s', name)
        except Exception:
            logger.exception('qurtoba runtime patch FAILED: %s', name)


# ── 1. LangGraph debug output ──────────────────────────────────────────────────

def _silence_langgraph_debug() -> None:
    import langgraph.pregel.main as m
    if not getattr(m, '_qurtoba_print_silenced', False):
        m.print = lambda *a, **k: None          # module global shadows the builtin the dumps use
        m._qurtoba_print_silenced = True
    # «Task … wrote to unknown channel __remaining_steps__, ignoring it» — one line per node per run
    logging.getLogger('langgraph.pregel._algo').setLevel(logging.ERROR)


# ── 2. Timeouts on every chat model ───────────────────────────────────────────

def _llm_limits():
    from django.conf import settings
    return (float(getattr(settings, 'QURTOBA_LLM_TIMEOUT_S', 60)),
            int(getattr(settings, 'QURTOBA_LLM_MAX_RETRIES', 1)))


def _bound_llm_calls() -> None:
    """``build_chat_model`` imports ``init_chat_model`` from ``langchain.chat_models`` at call time
    and calls ``_make_reasoning_chat_openai`` by module name — both are wrapped where they live.
    ChatAnthropic and ChatOpenAI accept ``timeout`` and ``max_retries``; a class that does not
    gets the original call."""
    import langchain.chat_models as lcm
    orig = lcm.init_chat_model
    if not getattr(orig, '_qurtoba_bounded', False):
        def init_chat_model_bounded(*args, **kwargs):
            timeout, retries = _llm_limits()
            kw = dict(kwargs)
            kw.setdefault('timeout', timeout)
            kw.setdefault('max_retries', retries)
            try:
                return orig(*args, **kw)
            except Exception:
                if kw.get('timeout') == timeout and kw.get('max_retries') == retries and (
                        'timeout' not in kwargs and 'max_retries' not in kwargs):
                    logger.warning('qurtoba: chat model rejected timeout/max_retries — built without them', exc_info=True)
                    return orig(*args, **kwargs)
                raise
        init_chat_model_bounded._qurtoba_bounded = True
        lcm.init_chat_model = init_chat_model_bounded

    from modules.aistudio.engines import node_executor as ne
    orig2 = ne._make_reasoning_chat_openai
    if not getattr(orig2, '_qurtoba_bounded', False):
        def make_reasoning_bounded(**kwargs):
            timeout, retries = _llm_limits()
            kwargs.setdefault('timeout', timeout)
            kwargs.setdefault('max_retries', retries)
            return orig2(**kwargs)
        make_reasoning_bounded._qurtoba_bounded = True
        ne._make_reasoning_chat_openai = make_reasoning_bounded


# ── 3. Failover on what core does not classify as "unavailable" ───────────────

_QUEUE_ERROR_MARKS = ('timeout limit', 'unable to start processing', 'timed out', 'request timed out')


def _widen_llm_failover() -> None:
    from modules.aistudio.engines import node_executor as ne
    orig = ne._is_llm_unavailable_error
    if getattr(orig, '_qurtoba_widened', False):
        return

    def is_unavailable(exc) -> bool:
        if orig(exc):
            return True
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        if type(exc).__name__ in ('TimeoutError', 'APITimeoutError', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout'):
            return True
        msg = str(exc).lower()
        return isinstance(exc, ValueError) and any(mark in msg for mark in _QUEUE_ERROR_MARKS)

    is_unavailable._qurtoba_widened = True
    ne._is_llm_unavailable_error = is_unavailable


# ── 4. Delivery statuses that arrive before the chat row ──────────────────────

def _retry_missed_statuses() -> None:
    from modules.whatsapp.services.webhook import WhatsAppWebhookService as S
    raw = S.__dict__.get('_update_message_status')
    if raw is None or getattr(raw, '_qurtoba_retrying', False):
        return
    fn = raw.__func__ if isinstance(raw, staticmethod) else raw

    def update_message_status(status_data, whatsapp_account):
        result = fn(status_data, whatsapp_account)
        try:
            status = (status_data or {}).get('status')
            mid = (status_data or {}).get('id')
            if status in ('sent', 'delivered') and mid:
                from modules.chat.models import Message
                if not Message.objects.filter(social_id=mid).exists():
                    from qurtoba.tasks import retry_message_status
                    retry_message_status.apply_async(args=[mid, status, 1], countdown=10)
        except Exception:
            logger.warning('qurtoba: status retry scheduling failed', exc_info=True)
        return result

    wrapped = staticmethod(update_message_status)
    wrapped._qurtoba_retrying = True
    S._update_message_status = wrapped
