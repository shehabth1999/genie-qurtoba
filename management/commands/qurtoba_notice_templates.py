"""
Create, submit and inspect the WhatsApp utility templates for the Cash-SYS notices.

    manage.py qurtoba_notice_templates --create            # draft rows (nothing sent to Meta)
    manage.py qurtoba_notice_templates --submit            # submit drafts/rejected to Meta
    manage.py qurtoba_notice_templates --status            # what the DB knows
    manage.py qurtoba_notice_templates --refresh           # ask Meta for the current statuses
    manage.py qurtoba_notice_templates --preview           # render each with example values
    options: --account <id> (default: phone 201006003836), --receipt-attachment <id>
"""
from django.core.management.base import BaseCommand, CommandError

DEFAULT_ACCOUNT_PHONE = '201006003836'


class Command(BaseCommand):
    help = 'Create / submit / inspect the Cash-SYS notice templates (qurtoba_*).'

    def add_arguments(self, parser):
        parser.add_argument('--account', type=int)
        parser.add_argument('--receipt-attachment', type=int, help='Attachment id used as the image example')
        parser.add_argument('--create', action='store_true')
        parser.add_argument('--submit', action='store_true')
        parser.add_argument('--status', action='store_true')
        parser.add_argument('--refresh', action='store_true', help='Pull each template status from Meta into the DB')
        parser.add_argument('--preview', action='store_true')

    def handle(self, *args, **opts):
        from modules.base.models.attachment import Attachment
        from modules.whatsapp.models import WhatsAppAccount, WhatsAppTemplate
        from qurtoba.services.notice_templates import (
            NOTICE_TEMPLATES, ensure_templates, submit_templates,
        )
        account = (WhatsAppAccount.objects.filter(pk=opts['account']).first() if opts['account']
                   else WhatsAppAccount.objects.filter(phone_number__contains=DEFAULT_ACCOUNT_PHONE).first())
        if account is None:
            raise CommandError('WhatsApp account not found; pass --account <id>')

        if opts['preview']:
            for kind, spec in NOTICE_TEMPLATES.items():
                body = spec['body']
                for name, ex in zip(spec['params'], spec['examples']):
                    body = body.replace('{{%s}}' % name, ex)
                self.stdout.write(f"--- {kind} ({spec['name']}, header {spec['header_format']}) ---")
                self.stdout.write(body)
                self.stdout.write('')
            return

        if opts['create']:
            att = Attachment.objects.filter(pk=opts['receipt_attachment']).first() if opts['receipt_attachment'] else None
            rows = ensure_templates(account, receipt_attachment=att)
            for t in rows:
                self.stdout.write(f"  {t.template_name:28s} #{t.pk} status={t.status} header={t.header_format} vars={list((t.body_text_numbered_mapping or {}).values())}")
            self.stdout.write(self.style.SUCCESS(f'{len(rows)} template rows ready on account {account.pk}.'))

        if opts['submit']:
            res = submit_templates(account)
            for name, st in res.items():
                style = self.style.SUCCESS if 'ERROR' not in st else self.style.ERROR
                self.stdout.write(style(f'  {name:28s} {st}'))

        names = [s['name'] for s in NOTICE_TEMPLATES.values()]
        if opts['refresh']:
            for t in WhatsAppTemplate.objects.filter(whatsapp_account=account, template_name__in=names).exclude(template_id__isnull=True):
                try:
                    account.service.get_template_status(t)
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f'  {t.template_name}: refresh failed: {str(exc)[:200]}'))

        if opts['status'] or opts['refresh'] or not (opts['create'] or opts['submit']):
            for t in WhatsAppTemplate.objects.filter(whatsapp_account=account, template_name__in=names).order_by('template_name'):
                self.stdout.write(f"  {t.template_name:28s} #{t.pk} status={t.status:9s} meta_id={t.template_id or '-'} header={t.header_format}")
