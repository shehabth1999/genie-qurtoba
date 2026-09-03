"""
Zero one customer's records for one day on BOTH ledgers — Genie and Qurtoba.

For a TEST customer whose day was spent testing the agent: every record of that
day with a value is set to 0 on the Qurtoba accountant server (PATCH
/transactions/api2/record/{id}/) and locally, so the customer's balance drops
back to what it was before the tests.

Safety, in the shape the 2026-09-03 zero/undo incident taught:
  * DRY RUN by default — lists exactly what would change on each side. Nothing is
    written without --apply.
  * A full JSON backup (local rows + the remote rows as Qurtoba returned them) is
    written before the first write; --undo replays it on both sides.
  * Records where money demonstrably moved (cash_sys_fulfilled > 0 or Cash-SYS
    transfers recorded) are REFUSED unless --force-partial: zeroing them would
    erase money that really went out.
  * Only ONE customer and ONE day per run, both named on the command line.

Usage:
  manage.py zero_customer_day --qurtoba-customer 841                 # dry run, today
  manage.py zero_customer_day --qurtoba-customer 841 --apply         # do it
  manage.py zero_customer_day --undo /path/to/backup.json            # put it back
"""
import json
import os
from datetime import date

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Zero every record of one customer for one day on Genie AND Qurtoba (dry run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--qurtoba-customer', type=int, help="Qurtoba customerData id (e.g. 841 for the test customer)")
        parser.add_argument('--date', help='YYYY-MM-DD (default: today, Cairo)')
        parser.add_argument('--apply', action='store_true', help='Actually write (default: dry run)')
        parser.add_argument('--force-partial', action='store_true',
                            help='Also zero records with money already sent (cash_sys_fulfilled/transfers). Dangerous.')
        parser.add_argument('--backup-dir', default='/srv/genie/qurtoba/secrets/zero_backups')
        parser.add_argument('--undo', metavar='BACKUP_JSON', help='Restore both ledgers from a backup written by a previous --apply')

    # ── helpers ──────────────────────────────────────────────────────────
    def _api(self):
        base = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
        token = getattr(settings, 'QURTOBA_TOKEN', '')
        if not base or not token:
            raise CommandError('QURTOBA_BASE_URL / QURTOBA_TOKEN not configured')
        return base, {'Authorization': f'Token {token}', 'Content-Type': 'application/json'}

    def _remote_rows(self, qurtoba_customer, day):
        base, headers = self._api()
        url = f'{base}/transactions/api2/record/'
        params = {'customerData': qurtoba_customer, 'date': day.isoformat()}
        rows = []
        while url:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            d = r.json()
            rows += d.get('results', [])
            url = d.get('next')
            params = None
        # the server honours the filter, but never trust it with money: re-check both keys
        return [x for x in rows
                if (x.get('customerData') or {}).get('id') == qurtoba_customer and str(x.get('date')) == day.isoformat()]

    def _patch_remote(self, record_id, value):
        from qurtoba.utils_sync import edit_qurtoba_record_value
        return edit_qurtoba_record_value(int(record_id), value)

    @staticmethod
    def _money_moved(rec):
        return float(rec.cash_sys_fulfilled or 0) > 0 or bool(rec.cash_sys_transactions)

    # ── undo ─────────────────────────────────────────────────────────────
    def _undo(self, path):
        from qurtoba.models import QurtobaCustomer, QurtobaRecord
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        errors = 0
        for row in data['remote']:
            err = self._patch_remote(row['id'], row['value'])
            if err:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  remote {row['id']} ← {row['value']}: {err}"))
            else:
                self.stdout.write(f"  remote {row['id']} ← {row['value']}")
        for row in data['local']:
            QurtobaRecord.objects.filter(pk=row['id']).update(
                value=row['value'], cash_sys_original_value=row['cash_sys_original_value'],
                cash_sys_state=row['cash_sys_state'], cash_sys_canceled_reason=row['cash_sys_canceled_reason'],
            )
            self.stdout.write(f"  local {row['id']} ← {row['value']}")
        cust = QurtobaCustomer.objects.filter(qurtoba_id=data['qurtoba_customer']).first()
        if cust is not None and hasattr(cust, 'recompute_balance'):
            try:
                cust.recompute_balance()
                cust.save(update_fields=['balance'])
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f'  balance recompute failed: {exc}'))
        self.stdout.write(self.style.SUCCESS(f'undo done from {path} (errors: {errors})'))

    # ── main ─────────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        if opts.get('undo'):
            return self._undo(opts['undo'])
        if not opts.get('qurtoba_customer'):
            raise CommandError('--qurtoba-customer is required (or --undo)')
        from qurtoba.models import QurtobaCustomer, QurtobaRecord

        qid = opts['qurtoba_customer']
        day = date.fromisoformat(opts['date']) if opts.get('date') else timezone.localdate()
        cust = QurtobaCustomer.objects.filter(qurtoba_id=qid).first()
        if cust is None:
            raise CommandError(f'no local customer with qurtoba_id={qid}')

        local = list(QurtobaRecord.objects.filter(customer=cust, date=day, value__gt=0).order_by('time', 'id'))
        remote = [x for x in self._remote_rows(qid, day) if float(x.get('value') or 0) > 0]
        remote_by_id = {int(x['id']): x for x in remote}
        local_qids = {int(r.qurtoba_record_id) for r in local if r.qurtoba_record_id}

        blocked = [r for r in local if self._money_moved(r) and not opts['force_partial']]
        blocked_ids = {r.pk for r in blocked}
        local_todo = [r for r in local if r.pk not in blocked_ids]
        remote_only = [x for rid, x in remote_by_id.items() if rid not in local_qids]
        remote_todo = [x for x in remote if int(x['id']) not in {int(r.qurtoba_record_id) for r in blocked if r.qurtoba_record_id}]

        self.stdout.write(f'customer: {cust.name} (qurtoba {qid}, local {cust.pk}) — day {day} — balance now {cust.balance:,.0f}')
        self.stdout.write(f'\nLOCAL records with value ({len(local)}):')
        for r in local:
            flag = '  REFUSED (money moved)' if r.pk in blocked_ids else ''
            self.stdout.write(f"  {r.pk:>7} qid={r.qurtoba_record_id!s:>7} {str(r.time)[:8]} {r.type:<9} {float(r.value):>10,.0f} {r.account_number or '—':<13} {r.cash_sys_state or ''}{flag}")
        self.stdout.write(f'\nQURTOBA records with value ({len(remote)}):')
        for x in remote:
            tag = '' if int(x['id']) in local_qids else '  (no local row)'
            self.stdout.write(f"  {x['id']:>7} {x['time'][:8]} {x['type']:<9} {float(x['value']):>10,.0f} {x.get('accountNumber') or '—':<13}{tag}")
        total = sum(float(x['value']) for x in remote_todo)
        self.stdout.write(f'\nWould zero: {len(remote_todo)} Qurtoba rows ({total:,.0f} EGP) and {len(local_todo)} local rows; '
                          f'refused: {len(blocked)}; remote-only: {len(remote_only)}')
        if not opts['apply']:
            self.stdout.write(self.style.WARNING('\nDRY RUN — nothing changed. Re-run with --apply to execute.'))
            return

        # ---- backup, then write remote first (authoritative), then local -------
        os.makedirs(opts['backup_dir'], exist_ok=True)
        path = os.path.join(opts['backup_dir'], f'zero_{qid}_{day.isoformat()}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.json')
        backup = {
            'created_at': timezone.now().isoformat(), 'qurtoba_customer': qid, 'date': day.isoformat(),
            'balance_before': float(cust.balance or 0),
            'remote': [{'id': int(x['id']), 'value': float(x['value']), 'raw': x} for x in remote_todo],
            'local': [{'id': r.pk, 'value': float(r.value), 'qurtoba_record_id': r.qurtoba_record_id,
                       'cash_sys_original_value': r.cash_sys_original_value, 'cash_sys_state': r.cash_sys_state,
                       'cash_sys_canceled_reason': r.cash_sys_canceled_reason} for r in local_todo],
        }
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(backup, fh, ensure_ascii=False, indent=1, default=str)
        self.stdout.write(f'\nbackup written: {path}')

        errors = 0
        for x in remote_todo:
            err = self._patch_remote(x['id'], 0)
            if err:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  Qurtoba {x['id']}: {err}"))
            else:
                self.stdout.write(f"  Qurtoba {x['id']} → 0")
        for r in local_todo:
            QurtobaRecord.objects.filter(pk=r.pk).update(
                value=0.0,
                cash_sys_original_value=r.cash_sys_original_value if r.cash_sys_original_value is not None else r.value,
                cash_sys_state='canceled', cash_sys_canceled_reason='test_zeroed',
            )
            self.stdout.write(f'  local {r.pk} → 0')
        if hasattr(cust, 'recompute_balance'):
            try:
                cust.recompute_balance()
                cust.save(update_fields=['balance'])
                cust.refresh_from_db()
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f'  balance recompute failed: {exc}'))
        self.stdout.write(self.style.SUCCESS(
            f'\ndone — Qurtoba errors: {errors}; balance now {cust.balance:,.0f}; undo with: '
            f'manage.py zero_customer_day --undo {path}'))
