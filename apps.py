import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class QurtobaConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'qurtoba'

    def ready(self):
        super().ready()
        from . import extensions  # noqa: F401
        from . import tools  # noqa: F401  — registers @tool decorators with AI Studio

        # Durable catcher for the "AI reply seen as inbound" bug — logs every
        # occurrence (with creation stack trace) to logs/ai_inbound_catcher.log.
        try:
            from . import ai_inbound_catcher
            ai_inbound_catcher.register()
        except Exception:
            logger.exception('qurtoba: ai_inbound_catcher failed to register')

        self._apply_business_settings()
        self._install_tool_db_hygiene()
        self._install_ai_guard()
        self._takeover_celery_config()

        # Kick off a catalog pull on first startup so the tables are never empty.
        # Skipped during manage.py migrate / test runs to avoid hitting Cash-SYS
        # before the DB is ready.
        if os.environ.get('RUN_MAIN') or os.environ.get('CELERY_WORKER_RUNNING'):
            self._schedule_catalog_pull()

    @staticmethod
    def _apply_business_settings():
        """Tenant business rules that core reads from settings, owned here.

        AI_HIGH_VALUE_CONFIRM_THRESHOLD — a single transfer at or above this
        amount (EGP) is never placed automatically: the create tool holds it,
        the customer must confirm, and only then is it created. Raised from the
        tool's 200,000 default to 100,000 on 2026-09-03 at the office's request.
        """
        from django.conf import settings as dj_settings
        try:
            dj_settings.AI_HIGH_VALUE_CONFIRM_THRESHOLD = 100_000
        except Exception:
            logger.exception('qurtoba: could not apply AI_HIGH_VALUE_CONFIRM_THRESHOLD')

    @staticmethod
    def _install_tool_db_hygiene():
        """Close the worker thread's DB connections after every AI tool call.

        The engine runs each tool in a pool thread (``sync_to_async(...,
        thread_sensitive=False)``). Django connections are thread-local and the
        core hook that closes stale connections runs only in the task's main
        thread, so every pool thread keeps an idle connection open for the
        life of the process. On 2026-09-05 a 32-scenario evaluation piled those
        up to the role's limit of 40 and every later tool call failed with
        «FATAL: too many connections for role "qurtoba"» — the model then
        narrated successes that never happened (the outbound gate dropped
        them). The same build-up can happen in the Celery worker on a busy day.

        The fix wraps ``tool_info.func`` at adaptation time: after the tool
        returns (or raises) in a non-main thread, that thread's connections are
        closed. Sync/async-ness is preserved because the adapter reads it from
        the (wrapped) function. Idempotent; never blocks startup.
        """
        try:
            import asyncio
            import threading
            from functools import wraps

            from django.db import connections
            from modules.aistudio.tools.langchain_adapter import ToolAdapter

            original = ToolAdapter.__dict__['adapt_tool_for_langchain']
            original_fn = original.__func__ if isinstance(original, staticmethod) else original
            if getattr(original_fn, '_qurtoba_db_hygiene', False):
                return

            def _close_thread_connections():
                if threading.current_thread() is threading.main_thread():
                    return
                try:
                    connections.close_all()
                except Exception:
                    pass

            def _wrap(func):
                if getattr(func, '_qurtoba_db_hygiene', False):
                    return func
                if asyncio.iscoroutinefunction(func):
                    @wraps(func)
                    async def wrapped_async(*args, **kwargs):
                        try:
                            return await func(*args, **kwargs)
                        finally:
                            _close_thread_connections()
                    wrapped_async._qurtoba_db_hygiene = True
                    return wrapped_async

                @wraps(func)
                def wrapped_sync(*args, **kwargs):
                    try:
                        return func(*args, **kwargs)
                    finally:
                        _close_thread_connections()
                wrapped_sync._qurtoba_db_hygiene = True
                return wrapped_sync

            def adapt_with_hygiene(tool_info, context_objects):
                try:
                    if getattr(tool_info, 'func', None) is not None:
                        tool_info.func = _wrap(tool_info.func)
                except Exception:
                    logger.exception('qurtoba: could not wrap tool %s for DB hygiene',
                                     getattr(tool_info, 'name', '?'))
                return original_fn(tool_info, context_objects)

            adapt_with_hygiene._qurtoba_db_hygiene = True
            ToolAdapter.adapt_tool_for_langchain = staticmethod(adapt_with_hygiene)
            logger.info('qurtoba: tool DB-connection hygiene installed on ToolAdapter')
        except Exception:
            logger.exception('qurtoba: tool DB hygiene NOT installed')

    @staticmethod
    def _install_ai_guard():
        """
        Own every rule about what the AI may send to a Qurtoba customer.

        Core carries no product text and no reply-suppression logic; the gate in
        qurtoba.ai_guard wraps the one method every WhatsApp send goes through.

        System-only templates: the Cash-SYS outcome notices state, as fact, what
        happened to a customer's money. Only the webhook knows that. On 2026-08-06
        the agent replayed them out of chat history for a transfer that had never
        been cancelled. The 👍 is the create tool's acknowledgement and equally
        system-only — unconditional, because a part-rejected bulk must keep the
        agent's voice for the rejection yet must STILL never produce a 👍.
        """
        try:
            from qurtoba import ai_guard
            from qurtoba.tasks import _CANCEL_NOTICE_MESSAGES

            ai_guard.register_system_templates(*_CANCEL_NOTICE_MESSAGES.values())
            ai_guard.register_system_templates('👍', '👍🏿')
            # The reroute notices are interpolated with live amounts, so their
            # fixed lines are registered individually.
            ai_guard.register_system_templates(
                "محتاجين رقم تانى علشان نكمل",
                "الرقم مش قابل تحويل تانى",
                "( الرقم تجاوز الحد اليومى او الشهرى )",
                "*محتاجين رقم تانى نبعت عليه الرصيد*",
                "الرقم مش قابل تحويل",
                "( تجاوز الحد اليومى او الشهرى )",
            )
            ai_guard.install()
        except Exception:
            logger.exception('qurtoba: ai_guard failed to install — AI replies are UNGATED')

    @staticmethod
    def _takeover_celery_config():
        """
        Everything this tenant needs from Celery, declared here instead of in
        core settings.

        Beat entries: django_celery_beat's DatabaseScheduler reads
        ``app.conf.beat_schedule`` — the very same dict as
        ``settings.CELERY_BEAT_SCHEDULE`` — in ``setup_schedule()``, which runs
        after ``django.setup()`` and therefore after this ``ready()``.
        The stranded-conversation entry keeps the name core once used for its own
        sweeper so the persisted PeriodicTask row is re-pointed, not duplicated.

        Worker DB connections: a worker process keeps one pooled connection per
        thread for ``CONN_MAX_AGE`` seconds; with the AI engine's own pools that
        pushed the Postgres role over its connection limit (incident 2026-08-06).
        Task-scoped connections cost one handshake per task and never pile up.
        """
        from django.conf import settings as dj_settings

        try:
            from zoneinfo import ZoneInfo
            from celery.schedules import crontab

            class CairoCrontab(crontab):
                """A crontab django_celery_beat stores in Africa/Cairo, not the app tz.

                CELERY_TIMEZONE is UTC, so a plain crontab(hour=21, minute=10) is
                00:10 Cairo in summer and 23:10 in winter (DST ends late October)
                — and the end-of-day reminder would go out before the day ended.
                django_celery_beat reads ``schedule.tz`` when it persists the row.
                """

                @property
                def tz(self):
                    return ZoneInfo('Africa/Cairo')

            schedule = getattr(dj_settings, 'CELERY_BEAT_SCHEDULE', None)
            if schedule is None:
                schedule = {}
                dj_settings.CELERY_BEAT_SCHEDULE = schedule
            schedule.update({
                'reconcile-unsynced-qurtoba-records': {
                    'task': 'qurtoba.tasks.reconcile_unsynced_qurtoba_records',
                    'schedule': 300.0,
                },
                'qurtoba-daily-reminder': {
                    'task': 'qurtoba.tasks.send_qurtoba_daily_reminder',
                    # Just after midnight Cairo time, every day of the year.
                    'schedule': CairoCrontab(hour=0, minute=10),
                },
                'recover-stranded-conversations': {
                    'task': 'qurtoba.tasks.recover_stranded_conversations',
                    'schedule': 60.0,
                },
            })
        except Exception:
            logger.exception('qurtoba: beat schedule takeover failed')

        try:
            # task_name / args on every TaskResult row — the only way to tell
            # which task produced a stored failure.
            if not getattr(dj_settings, 'CELERY_RESULT_EXTENDED', False):
                dj_settings.CELERY_RESULT_EXTENDED = True
        except Exception:
            logger.exception('qurtoba: could not enable CELERY_RESULT_EXTENDED')

        try:
            argv0 = os.path.basename(sys.argv[0] or '')
            is_celery = 'celery' in argv0 and any(c in sys.argv for c in ('worker', 'beat'))
            if is_celery:
                dj_settings.DATABASES['default']['CONN_MAX_AGE'] = 0
                from django.db import connections
                for alias in connections:
                    connections[alias].settings_dict['CONN_MAX_AGE'] = 0
        except Exception:
            logger.exception('qurtoba: could not scope worker DB connections to tasks')

    @staticmethod
    def _schedule_catalog_pull():
        try:
            from qurtoba.tasks import pull_cash_sys_catalog_task
            pull_cash_sys_catalog_task.delay()
        except Exception:
            pass  # Celery not ready yet (e.g. management commands) — beat will handle it
