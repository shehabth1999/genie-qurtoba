"""
Repair records that Cash-SYS CANCELLED but which still carry their full value.

WHY THESE EXIST
    `handle_cash_sys_order_canceled` used an ALLOW-LIST of cancel reasons that
    zero the ledger — only 'no_wallet' and 'cancel_request'. Every other reason
    fell into an else branch that marked the record canceled and did, in its own
    words, "no ledger touch". Cash-SYS also sends **'agent'** (an operator
    cancelling inside the Cash app), which was not on the list, so those
    cancellations left the debt standing at full value on BOTH ledgers.

    Nothing looked wrong: the record says canceled, and no error was ever raised.
    Measured at the time of the fix: 28 records / 194,370 EGP across 16 customers,
    oldest 2026-06-17.

    The handler is fixed (any non-reroute cancel now zeroes). This command repairs
    the rows that accumulated before that.

WHAT IT DOES
    For each affected record: PATCH the Qurtoba ledger row to 0, set the local
    value to 0, then recompute the customer's balance from Qurtoba.

SAFETY
    * Dry-run by default; --apply is required.
    * SKIPS any record where money actually moved (cash_sys_fulfilled > 0 or
      cash_sys_transactions set) — those are reroutes and must settle at the
      amount sent, not zero.
    * SKIPS any record with no qurtoba_record_id (nothing to patch; zeroing only
      locally would put the two ledgers out of step).
    * Writes a full JSON backup before the first write; --undo restores values on
      both sides.
"""
import datetime as dt
import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Zero the ledger for Cash-SYS-cancelled records whose value was never cleared.'

    def add_arguments(self, parser):
        parser.add_argument('--reason', default='agent',
                            help="Cancel reason to repair (default: agent). 'all' = every reason.")
        parser.add_argument('--ids', help='Comma-separated QurtobaRecord ids instead of a reason scan.')
        parser.add_argument('--apply', action='store_true', help='Actually write (default: dry run).')
        parser.add_argument('--backup-dir', default='/srv/genie/qurtoba/secrets/zero_backups')
        parser.add_argument('--undo', metavar='BACKUP_JSON', help='Restore from a backup file.')

    # ------------------------------------------------------------------ undo
    def _undo(self, path):
        from qurtoba.models import QurtobaRecord, QurtobaCustomer
        from qurtoba.utils_sync import edit_qurtoba_record_value

        if not os.path.exists(path):
            raise CommandError(f'Backup not found: {path}')
        with open(path) as fh:
            data = json.load(fh)

        for row in data.get('records', []):
            pk, val, qid = row['pk'], row['value'], row['qurtoba_record_id']
            if qid:
                err = edit_qurtoba_record_value(qid, val)
                if err:
                    self.stdout.write(self.style.ERROR(f'  qid {qid} restore FAILED: {err}'))
                    continue
            QurtobaRecord.objects.filter(pk=pk).update(value=val)
            self.stdout.write(self.style.SUCCESS(f'  restored #{pk} -> {val}'))
        for cid in {r['customer_id'] for r in data.get('records', []) if r.get('customer_id')}:
            try:
                QurtobaCustomer.objects.get(pk=cid).recompute_balance()
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f'  recompute failed for customer {cid}: {exc}'))
        self.stdout.write(self.style.SUCCESS('Undo complete.'))

    # ----------------------------------------------------------------- main
    def handle(self, *args, **opts):
        from qurtoba.models import QurtobaRecord, QurtobaCustomer
        from qurtoba.utils_sync import edit_qurtoba_record_value

        if opts.get('undo'):
            return self._undo(opts['undo'])

        apply_changes = opts['apply']

        if opts.get('ids'):
            ids = [int(x) for x in opts['ids'].split(',') if x.strip()]
            qs = QurtobaRecord.objects.filter(pk__in=ids)
        else:
            qs = QurtobaRecord.objects.exclude(cash_sys_canceled_reason__isnull=True).exclude(value=0)
            if opts['reason'] != 'all':
                qs = qs.filter(cash_sys_canceled_reason=opts['reason'])
        qs = qs.select_related('customer').order_by('created_at')

        targets, skipped = [], []
        for r in qs:
            # Money moved -> this is a reroute; it must settle at the amount sent.
            if float(r.cash_sys_fulfilled or 0) > 0 or r.cash_sys_transactions:
                skipped.append((r, f'money moved (fulfilled={r.cash_sys_fulfilled})'))
                continue
            if not r.qurtoba_record_id:
                skipped.append((r, 'no qurtoba_record_id — cannot patch the ledger'))
                continue
            targets.append(r)

        mode = self.style.ERROR('APPLY') if apply_changes else self.style.WARNING('DRY RUN')
        self.stdout.write(f'\n=== zero_cancelled_leftovers [{mode}] reason={opts["reason"]} ===\n')

        total = 0.0
        by_customer = {}
        for r in targets:
            total += float(r.value or 0)
            by_customer.setdefault(r.customer_id, [0, 0.0])
            by_customer[r.customer_id][0] += 1
            by_customer[r.customer_id][1] += float(r.value or 0)
            self.stdout.write(
                f'  #{r.pk:<6} | {r.created_at:%Y-%m-%d %H:%M} | {str(r.type):9} | '
                f'{r.value:>10,.0f} | cust={r.customer_id:<5} | qid={r.qurtoba_record_id} '
                f'| reason={r.cash_sys_canceled_reason}')

        if skipped:
            self.stdout.write(self.style.WARNING(f'\n  SKIPPED ({len(skipped)}):'))
            for r, why in skipped:
                self.stdout.write(f'    #{r.pk} | {r.value:>10,.0f} | {why}')

        self.stdout.write(f'\n  TOTAL to zero: {len(targets)} record(s), {total:,.0f} EGP, '
                          f'{len(by_customer)} customer(s)')
        for cid, (n, s) in sorted(by_customer.items()):
            name = QurtobaCustomer.objects.filter(pk=cid).values_list('name', flat=True).first() or '?'
            self.stdout.write(f'    customer {cid:<5} {str(name)[:28]:28} {n:>3} record(s)  {s:>12,.0f}')

        if not targets:
            self.stdout.write('\nNothing to do.')
            return
        if not apply_changes:
            self.stdout.write(self.style.WARNING('\nDRY RUN — nothing changed. Re-run with --apply.'))
            return

        # ---- backup ----
        os.makedirs(opts['backup_dir'], exist_ok=True)
        path = os.path.join(opts['backup_dir'],
                            f"cancelled_leftovers_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, 'w') as fh:
            json.dump({
                'created_at': timezone.now().isoformat(),
                'records': [{'pk': r.pk, 'value': float(r.value or 0),
                             'qurtoba_record_id': r.qurtoba_record_id,
                             'customer_id': r.customer_id, 'type': r.type,
                             'account_number': r.account_number,
                             'reason': r.cash_sys_canceled_reason,
                             'created_at': r.created_at.isoformat()} for r in targets],
            }, fh, ensure_ascii=False, indent=2, default=str)
        os.chmod(path, 0o600)
        self.stdout.write(self.style.SUCCESS(f'\nBackup written: {path}'))

        # ---- apply: ledger FIRST, then local (same order as the webhook) ----
        ok = err = 0
        for r in targets:
            e = edit_qurtoba_record_value(r.qurtoba_record_id, 0)
            if e:
                err += 1
                self.stdout.write(self.style.ERROR(
                    f'  #{r.pk} qid={r.qurtoba_record_id} LEDGER EDIT FAILED: {e} — local left unchanged'))
                continue
            QurtobaRecord.objects.filter(pk=r.pk).update(value=0)
            ok += 1

        for cid in by_customer:
            try:
                c = QurtobaCustomer.objects.get(pk=cid)
                old = c.balance
                c.recompute_balance()
                self.stdout.write(f'  customer {cid} balance: {old} -> {c.balance}')
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f'  recompute failed for {cid}: {exc}'))

        self.stdout.write(self.style.SUCCESS(f'\nDone: zeroed={ok} failed={err}'))
        self.stdout.write(f'Undo with:\n  uv run python manage.py zero_cancelled_leftovers --undo {path}')
