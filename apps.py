import os
from django.apps import AppConfig


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
            pass

        # Kick off a catalog pull on first startup so the tables are never empty.
        # Skipped during manage.py migrate / test runs to avoid hitting Cash-SYS
        # before the DB is ready.
        if os.environ.get('RUN_MAIN') or os.environ.get('CELERY_WORKER_RUNNING'):
            self._schedule_catalog_pull()

    @staticmethod
    def _schedule_catalog_pull():
        try:
            from qurtoba.tasks import pull_cash_sys_catalog_task
            pull_cash_sys_catalog_task.delay()
        except Exception:
            pass  # Celery not ready yet (e.g. management commands) — beat will handle it
