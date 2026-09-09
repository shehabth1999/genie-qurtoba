"""Pull ledger rows Qurtoba created but never delivered, and insert them here.

Their push is fire-and-forget: a row refused (bad token, a type we rejected) or sent
while we were unreachable is never re-sent. On 2026-09-09 an admin delete destroyed the
API token and 27 rows were lost in 56 minutes; separately every «الدفع» settlement had
been refused for months by a type check.

    python manage.py qurtoba_backfill_records --from 2026-09-08 --to 2026-09-09
    python manage.py qurtoba_backfill_records --from 2026-09-08 --to 2026-09-09 --apply

Dry run by default: it prints exactly what it would insert and writes nothing.
Idempotent — a row already here is skipped by Qurtoba's own record id, so re-running is
safe. Rows with no customer are skipped (their push never sends those either); pass
--include-orphans to take them too.
"""
import datetime

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Insert Qurtoba ledger rows that never reached Genie (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--from', dest='date_from', required=True, help='first day, YYYY-MM-DD')
        parser.add_argument('--to', dest='date_to', default=None, help='last day (default: same as --from)')
        parser.add_argument('--apply', action='store_true', help='actually write (default: dry run)')
        parser.add_argument('--include-orphans', action='store_true',
                            help='also take rows that carry no customer (their push never sends these)')
        parser.add_argument('--customer', type=int, default=None, help="only this customer's Qurtoba id")

    def handle(self, *args, **opts):
        base = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
        token = getattr(settings, 'QURTOBA_TOKEN', '')
        if not base or not token:
            raise CommandError('QURTOBA_BASE_URL / QURTOBA_TOKEN not configured')
        headers = {'Authorization': f'Token {token}'}

        d0 = datetime.date.fromisoformat(opts['date_from'])
        d1 = datetime.date.fromisoformat(opts['date_to']) if opts['date_to'] else d0
        if d1 < d0:
            raise CommandError('--to is before --from')

        from qurtoba.ingest import ingest_row, normalize_api_row
        from qurtoba.models import QurtobaCustomer, QurtobaRecord

        here = set(QurtobaRecord.objects.exclude(qurtoba_record_id__isnull=True)
                   .values_list('qurtoba_record_id', flat=True))
        totals = {'seen': 0, 'here': 0, 'missing': 0, 'created': 0, 'invalid': 0, 'skipped_no_customer': 0,
                  'skipped_unknown_customer': 0}
        touched, failures = set(), []

        day = d0
        while day <= d1:
            rows = self._fetch(base, headers, day, opts['customer'])
            rows.sort(key=lambda r: r.get('id') or 0)
            self.stdout.write(f'\n{day}: {len(rows)} row(s) on Qurtoba')
            for row in rows:
                totals['seen'] += 1
                rid = row.get('id')
                if rid in here:
                    totals['here'] += 1
                    continue
                cust_id = (row.get('customerData') or {}).get('id') if isinstance(row.get('customerData'), dict) else row.get('customerData')
                if not cust_id and not opts['include_orphans']:
                    totals['skipped_no_customer'] += 1
                    continue
                totals['missing'] += 1
                customer = QurtobaCustomer.objects.filter(qurtoba_id=cust_id).first() if cust_id else None
                label = (f"  #{rid} {row.get('time')} {str(row.get('type') or ''):12s} "
                         f"{float(row.get('value') or 0):>13,.0f}  cust {cust_id or '—'} "
                         f"({getattr(customer, 'name', 'not linked here')})")
                if cust_id and customer is None:
                    totals['skipped_unknown_customer'] += 1
                    self.stdout.write(self.style.WARNING(label + '  → SKIP, customer unknown here'))
                    continue
                if not opts['apply']:
                    self.stdout.write(label + '  → would insert')
                    continue
                # The balance is pulled once per customer at the end, not per row.
                obj, outcome, detail = ingest_row(normalize_api_row(row), pull_balance=False)
                if outcome == 'created':
                    totals['created'] += 1
                    if customer:
                        touched.add(customer.pk)
                    self.stdout.write(self.style.SUCCESS(label + f'  → inserted as #{obj.pk}'))
                elif outcome == 'duplicate':
                    totals['here'] += 1
                else:
                    totals['invalid'] += 1
                    failures.append((rid, detail))
                    self.stdout.write(self.style.ERROR(label + f'  → REFUSED {detail}'))
            day += datetime.timedelta(days=1)

        if opts['apply'] and touched:
            self.stdout.write('\nre-pulling the authoritative balance for each affected customer')
            for pk in sorted(touched):
                c = QurtobaCustomer.objects.get(pk=pk)
                before = c.balance
                c.recompute_balance()
                c.refresh_from_db(fields=['balance'])
                self.stdout.write(f'  {c.name}: {before:,.0f} → {c.balance:,.0f}')

        self.stdout.write('\n' + self.style.MIGRATE_HEADING('summary'))
        for k, v in totals.items():
            self.stdout.write(f'  {k:26s} {v}')
        if failures:
            self.stdout.write(self.style.ERROR(f'  {len(failures)} row(s) refused: {failures[:3]}'))
        if not opts['apply']:
            self.stdout.write(self.style.WARNING('\nDRY RUN — nothing was written. Re-run with --apply.'))

    @staticmethod
    def _fetch(base, headers, day, customer_id):
        rows, url, params, pages = [], f'{base}/transactions/api2/record/', {'date': day.isoformat()}, 0
        if customer_id:
            params['customerData'] = customer_id
        while url and pages < 60:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            body = r.json()
            rows += body.get('results', [])
            url = body.get('next')
            params = None
            pages += 1
        # never trust the server's filter with money: re-check the day ourselves
        return [x for x in rows if str(x.get('date')) == day.isoformat()]
