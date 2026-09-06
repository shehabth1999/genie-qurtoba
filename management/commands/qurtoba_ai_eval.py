"""Run the sandboxed end-to-end agent evaluation (see qurtoba/eval/runner.py)."""
import json
import os
import time
from datetime import datetime

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Drive the real Qurtoba WhatsApp agent through the scenario catalogue in a sandbox and score it.'

    def add_arguments(self, parser):
        parser.add_argument('--only', help='comma-separated scenario ids (default: all)')
        parser.add_argument('--out', help='output directory (default: /home/genie_qurtoba/ai_eval_<timestamp>)')
        parser.add_argument('--keep', action='store_true', help='leave the sandbox rows of the LAST scenario in place')
        parser.add_argument('--list', action='store_true', help='list scenarios and exit')
        parser.add_argument('--workflow', type=int, help='workflow id to drive (default: the runner\'s WORKFLOW_ID, 2)')

    def handle(self, *args, **opts):
        from qurtoba.eval import runner as _runner
        from qurtoba.eval.runner import get_sandbox, run_scenario
        from qurtoba.eval.scenarios import SCENARIOS

        if opts['list']:
            for s in SCENARIOS:
                self.stdout.write(f"{s['id']:4s} {s['title']}")
            return

        if opts.get('workflow'):
            _runner.WORKFLOW_ID = int(opts['workflow'])
        only = {x.strip() for x in (opts['only'] or '').split(',') if x.strip()}
        chosen = [s for s in SCENARIOS if not only or s['id'] in only]
        out = opts['out'] or f"/home/genie_qurtoba/ai_eval_{datetime.now().strftime('%Y%m%d_%H%M')}"
        os.makedirs(out, exist_ok=True)

        sandbox = get_sandbox()
        partner, customer, conversation, *_ = sandbox
        self.stdout.write(f'sandbox: workflow={_runner.WORKFLOW_ID} partner={partner.pk} customer={customer.pk} conversation={conversation.id} → {out}')

        results = []
        started = time.time()
        for i, scn in enumerate(chosen, 1):
            t = time.time()
            rep = run_scenario(scn, sandbox, keep=(opts['keep'] and i == len(chosen)))
            results.append(rep)
            with open(os.path.join(out, f"{scn['id']}.json"), 'w', encoding='utf-8') as fh:
                json.dump(rep, fh, ensure_ascii=False, indent=1, default=str)
            status = 'ERROR' if rep['error'] else ('PASS' if rep['failed'] == 0 else 'FAIL')
            self.stdout.write(f"[{i}/{len(chosen)}] {scn['id']:4s} {status:5s} {rep['passed']}/{rep['passed'] + rep['failed']} checks "
                              f"({time.time() - t:.0f}s) — {scn['title']}")
            self.stdout.flush()
        summary = {
            'scenarios': len(results),
            'passed_all': sum(1 for r in results if not r['error'] and r['failed'] == 0),
            'errors': sum(1 for r in results if r['error']),
            'checks_passed': sum(r['passed'] for r in results),
            'checks_failed': sum(r['failed'] for r in results),
            'elapsed_s': round(time.time() - started),
            'results': [{'id': r['id'], 'title': r['title'], 'passed': r['passed'], 'failed': r['failed'], 'error': bool(r['error'])} for r in results],
        }
        with open(os.path.join(out, 'summary.json'), 'w', encoding='utf-8') as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=1)
        self.stdout.write(json.dumps({k: v for k, v in summary.items() if k != 'results'}, ensure_ascii=False))
