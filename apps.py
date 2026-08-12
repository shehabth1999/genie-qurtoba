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

        self._register_system_only_templates()

    @staticmethod
    def _register_system_only_templates():
        """
        Declare the Cash-SYS outcome notices as SYSTEM-ONLY, so the agent can
        never send them itself.

        These state, as fact, what happened to a customer's money — a transfer
        was reversed, a number could not receive it, part was sent. Only the
        Cash-SYS webhook knows that; the agent does not. On 2026-08-06 it emitted
        them anyway, copied out of chat history, for a transfer that had just been
        created and never cancelled — telling a customer their 28,000 was
        cancelled and unrecorded when neither was true.

        Registering the exact strings makes them undeliverable through the AI
        reply path. The webhook does not go through that path, so real notices are
        unaffected. Line-level registration matters: the agent reproduced them as
        separate one-line messages, not as the whole template.
        """
        try:
            from modules.aistudio.utils.omni_channel_utils import register_system_templates
            from qurtoba.tasks import _CANCEL_NOTICE_MESSAGES

            register_system_templates(*_CANCEL_NOTICE_MESSAGES.values())

            # The ack emoji is SYSTEM-ONLY and unconditional. 👍 means "received,
            # executing" and is sent by the transfer-create tool itself; the agent
            # sending one is a second, duplicate acknowledgement for the same
            # transfer (Law 4: "you NEVER send 👍").
            #
            # Registered as its own rule rather than relying on the tool-result
            # check, because that check only suppresses when the create came back
            # completely clean. On 2026-08-07 11:06 the agent made two create calls
            # — the first REJECTED (source_mismatch), the second created — then
            # copied the 👍 it could see in the transcript. A part-rejected bulk
            # must keep the agent's voice for the rejection, yet must STILL never
            # produce a 👍, so this cannot be conditional on the result.
            #
            # 👍🏿 (dark) is the pending-review variant and is equally system-only.
            register_system_templates('👍', '👍🏿')
            # The reroute notices are interpolated with live amounts, so their
            # fixed lines are registered individually.
            register_system_templates(
                "محتاجين رقم تانى علشان نكمل",
                "الرقم مش قابل تحويل تانى",
                "( الرقم تجاوز الحد اليومى او الشهرى )",
                "*محتاجين رقم تانى نبعت عليه الرصيد*",
                "الرقم مش قابل تحويل",
                "( تجاوز الحد اليومى او الشهرى )",
            )
        except Exception:
            # Never block startup on a guard registration.
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
