"""
Purge "phantom" QurtobaRecords — rows created in Genie that never reached the
Qurtoba ledger, and the chat artifacts that falsely tell the customer they did.

WHY THESE EXIST
    A Genie-born record is pushed to Qurtoba by post_create → push_record_to_qurtoba_task.
    When that push died from a raised exception rather than a returned error (e.g.
    `FATAL: too many connections for role`), it skipped its own retry/_mark_error/
    QurtobaSyncProblem handling entirely. The result was a record with
    qurtoba_synced=False AND qurtoba_sync_error=NULL: no ledger row, no Cash-SYS
    order, no money moved — and no trace that anything had failed.

WHY PURGE INSTEAD OF RE-POSTING
    By the time a phantom is noticed, the customer has usually been told it failed.
    Record 22470 (28,000) is the case in point: the chat already read
    «تم الغاء التحويل» / «لم يتم تسجيل العمليه عليك». Posting it late would have
    invented a debt for a transfer the customer believed was cancelled. The phantom
    is the thing that is wrong, so the phantom is what gets removed.

WHAT IT TOUCHES
    * QurtobaRecord            — hard-deleted (it is a local-only row; nothing
                                 downstream references it, Message.qurtoba_record
                                 is SET_NULL).
    * 👍 MessageReaction       — deleted. It is the "received, executing" ack for
                                 a transfer that never executed.
    * Chat messages            — SOFT-deleted (is_deleted=True), never hard-deleted.
                                 The customer's own words stay recoverable and the
                                 WhatsApp thread stays intact; they simply stop
                                 rendering. Reversible with --undo.

SAFETY
    * Dry-run by default. --apply is required to change anything.
    * Always writes a full JSON backup (every field of every row it touches)
      before the first write. --undo replays it.
    * Refuses outright to touch a record that DID reach Qurtoba
      (qurtoba_synced=True or qurtoba_record_id set) — those are real money.
"""
import datetime as dt
import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


def _text(msg):
    c = msg.content
    return c.get('text') if isinstance(c, dict) else c


class Command(BaseCommand):
    help = 'Delete Genie-born QurtobaRecords that never reached Qurtoba, plus their chat artifacts.'

    def add_arguments(self, parser):
        parser.add_argument('--ids', help='Comma-separated QurtobaRecord ids. Omit to auto-detect all phantoms.')
        parser.add_argument('--apply', action='store_true', help='Actually make changes (default: dry run).')
        parser.add_argument('--backup-dir', default='/srv/genie/qurtoba/secrets/purge_backups',
                            help='Where the JSON backup is written.')
        parser.add_argument('--undo', metavar='BACKUP_JSON', help='Restore from a backup file written by a previous run.')
        parser.add_argument('--keep-inbound', action='store_true',
                            help="Leave the customer's own inbound message visible; remove only the outbound artifacts.")
        parser.add_argument('--window-seconds', type=int, default=90,
                            help='How far after the inbound message to scan for outbound artifacts (default 90).')

    # ------------------------------------------------------------------ undo
    def _undo(self, path):
        from modules.chat.models import Message, MessageReaction
        from qurtoba.models import QurtobaRecord

        if not os.path.exists(path):
            raise CommandError(f'Backup not found: {path}')
        with open(path) as fh:
            data = json.load(fh)

        with transaction.atomic():
            for row in data.get('records', []):
                if QurtobaRecord.objects.filter(pk=row['id']).exists():
                    self.stdout.write(f"  record {row['id']} already present — skipped")
                    continue
                clean = {k: v for k, v in row.items() if not k.startswith('_')}
                QurtobaRecord.objects.create(**clean)
                self.stdout.write(self.style.SUCCESS(f"  restored record {row['id']}"))

            for row in data.get('messages', []):
                Message.objects_all.filter(pk=row['id']).update(
                    is_deleted=row.get('is_deleted', False),
                    deleted_at=row.get('deleted_at'),
                )
                self.stdout.write(self.style.SUCCESS(f"  un-hid message {row['id'][:8]}"))

            for row in data.get('reactions', []):
                if not MessageReaction.objects.filter(pk=row['id']).exists():
                    MessageReaction.objects.create(**{k: v for k, v in row.items() if not k.startswith('_')})
                    self.stdout.write(self.style.SUCCESS(f"  restored reaction {row['id']}"))
        self.stdout.write(self.style.SUCCESS('Undo complete.'))

    # ----------------------------------------------------------------- main
    def handle(self, *args, **opts):
        from modules.chat.models import Message, MessageReaction
        from qurtoba.models import QurtobaRecord

        if opts.get('undo'):
            return self._undo(opts['undo'])

        apply_changes = opts['apply']
        keep_inbound = opts['keep_inbound']
        window = dt.timedelta(seconds=opts['window_seconds'])

        # ---- select ---------------------------------------------------
        if opts.get('ids'):
            ids = [int(x) for x in opts['ids'].split(',') if x.strip()]
            records = list(QurtobaRecord.objects.filter(pk__in=ids))
            missing = set(ids) - {r.pk for r in records}
            if missing:
                raise CommandError(f'No such record(s): {sorted(missing)}')
        else:
            records = list(QurtobaRecord.objects.filter(
                qurtoba_synced=False,
                qurtoba_record_id__isnull=True,
                customer_data_qurtoba_id__isnull=True,
            ).order_by('created_at'))

        # ---- refuse anything that actually reached Qurtoba -------------
        for r in records:
            if r.qurtoba_synced or r.qurtoba_record_id:
                raise CommandError(
                    f'REFUSING: record {r.pk} reached Qurtoba '
                    f'(synced={r.qurtoba_synced}, qurtoba_record_id={r.qurtoba_record_id}). '
                    f'That is real money on the ledger — it must not be purged here.'
                )

        if not records:
            self.stdout.write('No phantom records found. Nothing to do.')
            return

        # ---- plan -----------------------------------------------------
        backup = {'created_at': timezone.now().isoformat(), 'records': [], 'messages': [], 'reactions': []}
        plan = []

        for r in records:
            entry = {'record': r, 'messages': [], 'reactions': []}
            origin = Message.objects_all.filter(qurtoba_record_id=r.pk).first()
            if origin is None and r.origin_message_id:
                origin = Message.objects_all.filter(pk=r.origin_message_id).first()

            if origin:
                entry['reactions'] += list(MessageReaction.objects.filter(message=origin))
                if not keep_inbound:
                    entry['messages'].append(origin)
                # Outbound artifacts that followed it: the 👍 ack, the account
                # correction the tool sent, and the empty-output bug messages.
                # Scoped to this conversation and this short window so unrelated
                # traffic is never swept up.
                for m in Message.objects_all.filter(
                    conversation_id=origin.conversation_id,
                    direction='outbound',
                    created_at__gte=origin.created_at,
                    created_at__lte=origin.created_at + window,
                    is_deleted=False,
                ).order_by('created_at'):
                    t = (_text(m) or '').strip()
                    if t in ('👍', 'None', '') or t == (r.account_number or '\0'):
                        entry['messages'].append(m)
            plan.append(entry)

        # ---- report ---------------------------------------------------
        mode = self.style.ERROR('APPLY') if apply_changes else self.style.WARNING('DRY RUN')
        self.stdout.write(f'\n=== purge_phantom_qurtoba_records [{mode}] ===\n')
        n_msgs = n_reacts = 0
        for e in plan:
            r = e['record']
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\nRECORD {r.pk} | {r.type} | {r.value} | acct={r.account_number} | created={r.created_at}"))
            self.stdout.write(f"    synced={r.qurtoba_synced} qurtoba_id={r.qurtoba_record_id} "
                              f"cash_sys_state={r.cash_sys_state}  -> DELETE")
            for m in e['messages']:
                self.stdout.write(f"    hide msg  {str(m.pk)[:8]} | {m.direction:8} | {m.created_at} | {str(_text(m))[:45]!r}")
                n_msgs += 1
            for x in e['reactions']:
                self.stdout.write(f"    del react {x.pk} | {x.emoji} | on {str(x.message_id)[:8]}")
                n_reacts += 1

        self.stdout.write(
            f"\nTOTAL: {len(plan)} record(s) deleted, {n_msgs} message(s) hidden, {n_reacts} reaction(s) removed.")

        if not apply_changes:
            self.stdout.write(self.style.WARNING('\nDRY RUN — nothing changed. Re-run with --apply to execute.'))
            return

        # ---- backup then apply ----------------------------------------
        os.makedirs(opts['backup_dir'], exist_ok=True)
        path = os.path.join(opts['backup_dir'],
                            f"purge_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json")

        for e in plan:
            backup['records'].append({
                k: (v.isoformat() if isinstance(v, (dt.datetime, dt.date, dt.time)) else v)
                for k, v in e['record'].__dict__.items() if not k.startswith('_')
            })
            for m in e['messages']:
                backup['messages'].append({
                    'id': str(m.pk), 'is_deleted': m.is_deleted,
                    'deleted_at': m.deleted_at.isoformat() if m.deleted_at else None,
                    '_text': str(_text(m))[:200], '_direction': m.direction,
                })
            for x in e['reactions']:
                backup['reactions'].append({
                    'id': x.pk, 'message_id': str(x.message_id), 'emoji': x.emoji,
                    'direction': x.direction, 'user_id': x.user_id, 'social_id': x.social_id,
                })

        with open(path, 'w') as fh:
            json.dump(backup, fh, ensure_ascii=False, indent=2, default=str)
        os.chmod(path, 0o600)
        self.stdout.write(self.style.SUCCESS(f'\nBackup written: {path}'))

        with transaction.atomic():
            now = timezone.now()
            for e in plan:
                for x in e['reactions']:
                    x.delete()
                ids = [m.pk for m in e['messages']]
                if ids:
                    Message.objects_all.filter(pk__in=ids).update(is_deleted=True, deleted_at=now)
                e['record'].delete()

        self.stdout.write(self.style.SUCCESS('Purge complete.'))
        self.stdout.write(f'Undo with:\n  uv run python manage.py '
                          f'purge_phantom_qurtoba_records --undo {path}')
