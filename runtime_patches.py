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

    # WhatsApp Web customer groups (owner decision 2026-09-23) — only when the channel is installed.
    if not _wa_web_installed():
        return
    for name, fn in (
        ('wa_web: a group run is about the group (its customer), not the member who spoke', _group_run_is_the_group),
        ('wa_web: a silent turn stays silent (no apology, no escalation)', _silent_wa_web_turns),
        ('wa_web: groups only on the Cloud API number', _wa_web_groups_only),
        ('wa_web: an unreadable message never switches a group\'s AI off', _no_group_escalation_on_unsupported),
    ):
        try:
            fn()
            logger.info('qurtoba runtime patch: %s', name)
        except Exception:
            logger.exception('qurtoba runtime patch FAILED: %s', name)


def _wa_web_installed() -> bool:
    try:
        from django.apps import apps
        return apps.is_installed('modules.wa_web') and apps.is_installed('modules.aistudio_wa_web')
    except Exception:
        return False


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


# ── 5. WhatsApp Web: the run's partner in a group is the GROUP ────────────────
#
# One group = one Qurtoba customer, linked on the group's placeholder partner (qurtoba.groups).
# Core (f678ecce) hands the member who spoke last in as the run's ``partner``; for Qurtoba that
# would make every «linked?» check, tool and template read an unlinked member (or staff) instead of
# the group's customer. Returning None keeps the bridge's own default: partner = the group.
# Who spoke is still on every message's sender (staff filtering) and in ``state.group``.

def _group_run_is_the_group() -> None:
    import modules.aistudio_wa_web.group_context as gc
    if getattr(gc.speaker_of, '_qurtoba_group_partner', False):
        return

    def speaker_of(messages):
        return None

    speaker_of._qurtoba_group_partner = True
    speaker_of._qurtoba_original = gc.speaker_of
    gc.speaker_of = speaker_of


# ── 6. WhatsApp Web: an empty output is a deliberate silent turn ──────────────
#
# The wa_web bridge counts an empty workflow output as a FAILURE: it re-runs the workflow, then posts
# the apology to the group, escalates (AI off for that group, for good) and e-mails the failure
# (aistudio_wa_web/tasks.py, «is_error = … or not result.output»). Every Qurtoba turn whose words went
# out through the tools ends with ''. The Cloud bridge has an «intentional empty» branch; wa_web does
# not — so a successful empty run becomes SILENT_SENTINEL, which the outbound gate drops unsent.

def _silent_wa_web_turns() -> None:
    import modules.aistudio.services as svc
    orig = svc.execute_workflow_sync
    if getattr(orig, '_qurtoba_silent', False):
        return

    def execute_workflow_sync(workflow_id, input_data, **kwargs):
        result = orig(workflow_id, input_data, **kwargs)
        try:
            if (kwargs.get('trigger_source') == 'wa_web' and getattr(result, 'success', False)
                    and getattr(result, 'status', 'completed') == 'completed'
                    and not str(getattr(result, 'output', None) or '').strip()):
                from qurtoba.groups import SILENT_SENTINEL
                result.output = SILENT_SENTINEL
        except Exception:
            logger.warning('qurtoba: silent-turn marker failed', exc_info=True)
        return result

    execute_workflow_sync._qurtoba_silent = True
    execute_workflow_sync._qurtoba_original = orig
    svc.execute_workflow_sync = execute_workflow_sync


# ── 7. WhatsApp Web on the Cloud API number keeps the groups only ─────────────
#
# The office number is linked to WhatsApp Web for the customers' groups while its 1:1 chats stay on
# the Cloud API (coexistence). Every private message would otherwise be stored twice — two chats per
# customer, one of them outside every Qurtoba rule. The switch «واتساب ويب: الجروبات بس» on the Cloud
# account of the same number decides (default on).

def _wa_web_groups_only() -> None:
    from modules.wa_web.services.ingest import Ingest
    orig = Ingest.message
    if getattr(orig, '_qurtoba_groups_only', False):
        return

    def message(self, msg, *args, **kwargs):
        try:
            if isinstance(msg, dict) and not msg.get('is_group') and _groups_only(self.account):
                return None
        except Exception:
            logger.warning('qurtoba: groups-only check failed — storing the message', exc_info=True)
        return orig(self, msg, *args, **kwargs)

    message._qurtoba_groups_only = True
    message._qurtoba_original = orig
    Ingest.message = message


def _groups_only(account) -> bool:
    from django.core.cache import cache
    key = f'qurtoba:wa_web_groups_only:{getattr(account, "pk", None)}'
    cached = cache.get(key)
    if cached is not None:
        return bool(cached)
    from qurtoba.groups import twin_cloud_account
    twin = twin_cloud_account(account)
    value = bool(getattr(twin, 'qurtoba_wa_web_groups_only', True)) if twin is not None else False
    cache.set(key, int(value), 60)
    return value


# ── 8. An unreadable group message must not switch the whole group's AI off ──
#
# Core: an inbound row stored as original_type='unsupported' → conversation.escalate_to_human() (AI off
# for the conversation, for good). Right for a coexistence placeholder in a 1:1 chat; in a customer group
# any new WhatsApp message kind (an event, a new card type…) would silence the AI for every member. The
# chat.Message pre_create hook flags exactly that save; the escalation is skipped, core still leaves
# the row to a human (it returns before the AI).

def _no_group_escalation_on_unsupported() -> None:
    from modules.chat.models import Conversation
    orig = Conversation.escalate_to_human
    if getattr(orig, '_qurtoba_group_guard', False):
        return

    def escalate_to_human(self, *args, **kwargs):
        try:
            from qurtoba.groups import escalation_suppressed
            if escalation_suppressed():
                logger.info('qurtoba: unreadable message in group %s — escalation skipped, the AI stays on', self.pk)
                return False
        except Exception:
            pass
        return orig(self, *args, **kwargs)

    escalate_to_human._qurtoba_group_guard = True
    escalate_to_human._qurtoba_original = orig
    Conversation.escalate_to_human = escalate_to_human
