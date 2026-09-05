"""
Backlog of missing «مصاريف خدمه» on split Cash-SYS orders.

Office report 2026-09-05: when a Cash-SYS order executed in parts and then ended
as a reroute (number_limit) or a cancel, the record was settled at the amount
actually sent but the service fee for the executed part was never recorded —
the fee was only ever posted from the order_done path. tasks.py now posts it from
the settlement path too; this command finds the records that were settled BEFORE
that fix and posts the fee they still owe.

Selection (all must hold):
  * cash_sys_state in ('rerouted', 'canceled')
  * cash_sys_fulfilled > 0                 (money really went out)
  * cash_sys_service_fee_done = False      (never charged)
  * the executed transfer briefs yield a non-empty fee plan (fee ≥ 2 after floor)

Safety, in the shape the 2026-09-03 zero/undo incident taught: DRY RUN by
default and --apply is REFUSED unless --i-confirm is given as well. --apply
creates ledger debt records (pushed to the Qurtoba accountant) AND sends the
customer the same «تم اضافه X جنيه مصاريف خدمه» WhatsApp note the done path sends.

Usage:
  manage.py qurtoba_fee_backlog                       # dry run (default)
  manage.py qurtoba_fee_backlog --dry-run
  manage.py qurtoba_fee_backlog --apply --i-confirm   # post the fees
  manage.py qurtoba_fee_backlog --ids 123,456 ...     # restrict to these record ids
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'List (default) or post the missing مصاريف خدمه of rerouted/canceled split orders.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='List what would be posted (this is the default behaviour).')
        parser.add_argument('--apply', action='store_true',
                            help='Actually post the fees. Refused without --i-confirm.')
        parser.add_argument('--i-confirm', action='store_true', dest='i_confirm',
                            help='Explicit confirmation that ledger writes + customer messages may be made.')
        parser.add_argument('--ids', help='Comma-separated QurtobaRecord ids to restrict to.')
        parser.add_argument('--customer', type=int, help='Restrict to one QurtobaCustomer id.')

    # ── selection ─────────────────────────────────────────────────────────
    def _candidates(self, opts):
        from qurtoba.models import QurtobaRecord
        from qurtoba.tasks import _executed_briefs, _service_fee_plan

        qs = (
            QurtobaRecord.objects
            .filter(cash_sys_state__in=['rerouted', 'canceled'],
                    cash_sys_fulfilled__gt=0,
                    cash_sys_service_fee_done=False)
            .select_related('customer')
            .order_by('updated_at', 'pk')
        )
        if opts.get('ids'):
            try:
                ids = [int(x) for x in opts['ids'].split(',') if x.strip()]
            except ValueError:
                raise CommandError('--ids must be comma-separated integers')
            qs = qs.filter(pk__in=ids)
        if opts.get('customer'):
            qs = qs.filter(customer_id=opts['customer'])

        rows = []
        for rec in qs:
            executed = _executed_briefs(rec.cash_sys_transactions)
            if not executed:
                continue
            recipient = rec.account_number or next(
                (b.get('transfer_to') for b in executed if b.get('transfer_to')), None)
            plan = _service_fee_plan(executed, recipient=recipient)
            if not plan:
                continue
            amount_executed = sum(float(b.get('value') or 0) for b in executed)
            settled = rec.cash_sys_done_at or rec.updated_at
            rows.append({
                'record': rec,
                'customer': rec.customer.name if rec.customer_id else '-',
                'customer_id': rec.customer_id,
                'state': rec.cash_sys_state,
                'reason': rec.cash_sys_canceled_reason,
                'fulfilled': rec.cash_sys_fulfilled,
                'executed': amount_executed,
                'plan': plan,
                'settled': settled,
                'recipient': recipient,
            })
        return rows

    # ── entry ─────────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        apply = bool(opts.get('apply'))
        if opts.get('dry_run') and apply:
            raise CommandError('--dry-run and --apply are mutually exclusive')
        if apply and not opts.get('i_confirm'):
            raise CommandError(
                'REFUSED: --apply writes debt records to the Qurtoba ledger and messages '
                'customers. Re-run with BOTH --apply --i-confirm after reviewing the dry run.'
            )

        rows = self._candidates(opts)
        mode = 'APPLY' if apply else 'DRY RUN'
        self.stdout.write(f'[{mode}] {len(rows)} record(s) with a missing مصاريف خدمه')
        self.stdout.write(
            f"{'id':>7}  {'state':<9} {'reason':<12} {'cust':>5}  {'customer':<28} "
            f"{'fulfilled':>10} {'executed':>10}  {'fee(s)':<12} settled"
        )
        total_fee = 0
        for r in rows:
            total_fee += sum(r['plan'])
            settled = r['settled'].strftime('%Y-%m-%d %H:%M') if r['settled'] else '-'
            self.stdout.write(
                f"{r['record'].pk:>7}  {r['state']:<9} {str(r['reason'] or '-'):<12} "
                f"{str(r['customer_id'] or '-'):>5}  {r['customer'][:28]:<28} "
                f"{r['fulfilled']:>10,.0f} {r['executed']:>10,.0f}  "
                f"{'+'.join(str(f) for f in r['plan']):<12} {settled}"
            )
        self.stdout.write(f'total planned fees: {total_fee} EGP over {len(rows)} record(s)')

        if not apply:
            if rows:
                self.stdout.write('Nothing written. To post these: --apply --i-confirm')
            return

        from qurtoba.tasks import _create_service_fees
        from qurtoba.models import QurtobaRecord

        posted = failed = 0
        for r in rows:
            rec = r['record']
            try:
                # Re-read right before writing: another process (a late order_done)
                # may have posted the fee since the listing above.
                rec.refresh_from_db()
                if rec.cash_sys_service_fee_done:
                    self.stdout.write(f"  #{rec.pk}: already done meanwhile — skipped")
                    continue
                _create_service_fees(rec)
                n = QurtobaRecord.objects.filter(
                    customer_id=rec.customer_id, type='مصاريف خدمه',
                    notes=f'[auto] مصاريف خدمة لعملية #{rec.pk}',
                ).count()
                posted += 1
                self.stdout.write(f"  #{rec.pk}: posted {r['plan']} ({n} fee record(s) now on file)")
            except Exception as exc:
                failed += 1
                self.stderr.write(f"  #{rec.pk}: FAILED {exc}")
        self.stdout.write(f'done: posted={posted} failed={failed}')
