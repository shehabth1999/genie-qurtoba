"""
Create (or refresh) the end-of-day WhatsApp reminder template as a DRAFT.

The template is left in `draft` on purpose — nothing is pushed to Meta from
here. Open it in the UI, read the Arabic, then submit it for approval yourself.
A rejected template counts against the account, so the wording gets a human
read first.

    python manage.py create_daily_reminder_template
    python manage.py create_daily_reminder_template --preview        # render only
    python manage.py create_daily_reminder_template --account 3
"""
import re

from django.core.management.base import BaseCommand, CommandError


TEMPLATE_NAME = 'qurtoba_daily_summary'
STATEMENT_TEMPLATE_NAME = 'qurtoba_daily_statement_xlsx'   # same body, the day's Excel statement as the header
DEFAULT_ACCOUNT_PHONE = '201006003836'  # محاسب قرطبة

HEADER = 'كشف نهاية اليوم'

# Every {{placeholder}} is a @property on base.Partner — see
# PartnerQurtobaExtension in qurtoba/extensions.py. The template's content_type
# ("apply to") is base.Partner, and WhatsAppTemplate.get_body_parameters does a
# bare getattr per name, so properties resolve exactly like columns.
#
# Formatting is deliberately minimal: bold on the two figures and the العميل
# label, everything else plain. Meta passes body text through verbatim, and the
# markers WhatsApp honours in a TEMPLATE are only *bold* / _italic_ /
# ~strike~ / ```mono``` — blockquote and list syntax arrive as literal
# characters, so none is used here.
#
# qurtoba_balance carries the whole «عليك 412,907 جنيه» sentence rather
# than a bare number: the direction word has to lead in Arabic, and a zero
# balance reads «مفيش مديونية» instead of an amount. A fixed template string
# with a number slotted in cannot express either.
BODY = (
    '*العميل :* {{qurtoba_cust_name}}\n'
    '\n'
    'ملخص عمليات يوم : {{qurtoba_date}}\n'
    '━━━━━━━━━━━━━\n'
    '💸 إجمالي التحويلات للرقم :\n'
    '{{qurtoba_phone}} : ( *{{qurtoba_total}}* )\n'
    '\n'
    '━━━━━━━━━━━━━\n'
    '🏦 إجمالي الحساب الان :\n'
    '      ( {{qurtoba_balance}} )'
)

FOOTER = 'مكتب قرطبة — كشف تلقائي فى نهاية اليوم'


# Meta's hard limits on a NAMED template parameter. The 20-char cap is not
# obvious and is not reported clearly: an over-long name comes back as a generic
# code-100 "recipient is invalid or the page lacks a permission", which sends you
# hunting the phone number instead of the parameter. Checked here so a bad name
# fails at build time rather than after Meta has approved an unsendable template.
_META_PARAM_MAX_LEN = 20
_META_PARAM_RE = re.compile(r'^[a-z0-9_]+$')


def _validate_placeholders(body, model):
    """Every {{placeholder}} must satisfy Meta AND resolve on the target model."""
    names = re.findall(r'\{\{\s*(\w+)\s*\}\}', body)
    problems = []
    for n in names:
        if len(n) > _META_PARAM_MAX_LEN:
            problems.append(f'{n!r} is {len(n)} chars — Meta allows at most {_META_PARAM_MAX_LEN}')
        if not _META_PARAM_RE.match(n):
            problems.append(f'{n!r} must match ^[a-z0-9_]+$')
        if not hasattr(model, n):
            problems.append(f'{n!r} is not an attribute of {model.__name__}')
    if problems:
        raise CommandError('Template placeholders rejected:\n  - ' + '\n  - '.join(problems))
    return names


class Command(BaseCommand):
    help = 'Create the Qurtoba end-of-day reminder WhatsApp template as a draft.'

    def add_arguments(self, parser):
        parser.add_argument('--account', type=int, default=None,
                            help='WhatsAppAccount id (default: the محاسب قرطبة line).')
        parser.add_argument('--preview', action='store_true',
                            help='Render the message with real data and exit without writing.')
        parser.add_argument('--name', default=TEMPLATE_NAME,
                            help='Template name to create/refresh (default: %(default)s). '
                                 'Meta refuses to edit an approved template (subcode 2388039) — '
                                 'a wording or variable change needs a NEW name.')
        parser.add_argument('--submit', action='store_true',
                            help='Also submit it to Meta for approval instead of leaving a draft.')
        parser.add_argument('--document', action='store_true',
                            help=f'Build the DOCUMENT-header variant ({STATEMENT_TEMPLATE_NAME}) as a SECOND template: '
                                 'the same body, with the day\'s full Excel statement attached as the header. A sample '
                                 'statement is generated and stored as the header sample Meta reviews. The text '
                                 'template is left untouched.')

    def handle(self, *args, **opts):
        from django.contrib.contenttypes.models import ContentType
        from modules.base.models import Language, Partner
        from modules.whatsapp.models import WhatsAppAccount, WhatsAppTemplate

        if opts['account']:
            account = WhatsAppAccount.objects.filter(pk=opts['account']).first()
        else:
            account = WhatsAppAccount.objects.filter(
                phone_number__contains=DEFAULT_ACCOUNT_PHONE).first()
        if account is None:
            raise CommandError('WhatsApp account not found. Pass --account <id>.')

        language = (Language.objects.filter(code='ar_EG').first()
                    or Language.objects.filter(code='ar').first())
        if language is None:
            raise CommandError('No Arabic language row found.')

        partner_ct = ContentType.objects.get_for_model(Partner)

        body = BODY
        _validate_placeholders(body, Partner)

        if opts['preview']:
            self._preview(body)
            return

        name = opts['name']
        if opts['document'] and name == TEMPLATE_NAME:
            name = STATEMENT_TEMPLATE_NAME
        template = WhatsAppTemplate.objects.filter(
            whatsapp_account=account, name=name, language=language,
        ).first()
        created = template is None
        if created:
            template = WhatsAppTemplate(
                whatsapp_account=account, name=name, language=language,
            )

        template.template_name = name
        template.category = 'utility'
        template.status = 'draft'
        if opts['document']:
            template.header_format = 'DOCUMENT'
            template.header_content = None
            template.header_media = self._sample_statement_attachment()
        else:
            template.header_format = 'TEXT'
            template.header_content = HEADER
        template.body_text = body
        template.footer_text = FOOTER
        template.content_type = partner_ct   # "apply to" — resolves {{vars}} off Partner

        # Deliberately a full save(), not update_or_create(). Django passes
        # update_fields built from the `defaults` dict, and pre_save derives
        # body_text_numbered_mapping from body_text — a field that is NOT in
        # defaults. With update_or_create the new mapping is computed in memory
        # and then dropped from the UPDATE, leaving a template whose stored
        # variable names disagree with its own body text.
        template.save()
        template.refresh_from_db()

        stored = list((template.body_text_numbered_mapping or {}).values())
        if stored != _validate_placeholders(body, Partner):
            raise CommandError(
                f'Stored variable mapping {stored} does not match the body text. '
                'Refusing to leave the template in an inconsistent state.'
            )

        self.stdout.write(self.style.SUCCESS(
            f'{"Created" if created else "Updated"} template #{template.pk} "{name}" '
            f'(draft, category=utility) on account "{account.name}".'))
        self.stdout.write(f'  apply to : {partner_ct.app_label}.{partner_ct.model}')
        self.stdout.write(f'  variables: {list((template.body_text_numbered_mapping or {}).values())}')
        self.stdout.write('')
        self._preview(body)
        self.stdout.write('')

        if not opts['submit']:
            self.stdout.write(self.style.WARNING(
                'Left as DRAFT — nothing was sent to Meta. Review it, then submit for approval. '
                'The nightly task only sends once a template with this name is APPROVED.'))
            return

        examples = template.get_example_field_values(
            list(template.body_text_numbered_mapping.values()))
        self.stdout.write(f'  examples : {examples}')
        try:
            template.whatsapp_account.service.create_template(template, body_examples=examples)
        except Exception as exc:
            raise CommandError(f'Meta rejected the template: {exc}')
        template.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(
            f'Submitted to Meta. template_id={template.template_id} status={template.status}. '
            'It sends only once Meta marks it APPROVED.'))

    def _sample_statement_attachment(self):
        """A real statement file (the customer with the most recent activity, that day) stored
        as the template's header sample — Meta reviews the header with it."""
        from django.core.files.base import ContentFile
        from django.utils import timezone
        from modules.base.models.attachment import Attachment
        from qurtoba.models import QurtobaRecord
        from qurtoba.tools.reports import _XLSX_MIME, _build_statement_xlsx, collect_customer_day

        rec = QurtobaRecord.objects.filter(value__gt=0).select_related('customer').order_by('-date', '-id').first()
        if rec is None:
            raise CommandError('No Qurtoba record exists — cannot build a sample statement.')
        customer, day = rec.customer, rec.date
        data = collect_customer_day(customer, None, day)
        xlsx = _build_statement_xlsx(customer_name=customer.name, report_date_iso=day.isoformat(),
                                     groups=data['groups'], total_debit=data['total_debit'],
                                     total_credit=data['total_credit'], current_balance=customer.balance or 0,
                                     generated_at=timezone.localtime().strftime('%Y-%m-%d %H:%M'))
        name = f'qurtoba_statement_sample_{day.isoformat()}.xlsx'
        att = Attachment(name=name, mime_type=_XLSX_MIME, type='document', size=len(xlsx))
        att.file.save(name, ContentFile(xlsx), save=True)
        self.stdout.write(f'  header   : DOCUMENT sample {name} ({len(xlsx)} bytes, customer {customer.pk}, day {day})')
        return att

    def _preview(self, body):
        """Render the body against a real linked partner so the wording can be judged."""
        from modules.base.models import Partner

        partner = (Partner.objects.filter(qurtoba_customer__isnull=False)
                   .order_by('pk').first())
        if partner is None:
            self.stdout.write(self.style.WARNING(
                'No Qurtoba-linked partner exists yet — cannot render a preview.'))
            return

        import re
        rendered = re.sub(
            r'\{\{\s*(\w+)\s*\}\}',
            lambda m: str(getattr(partner, m.group(1), f'<{m.group(1)}?>')),
            body,
        )
        self.stdout.write('--- preview (partner %s / %s) ---' % (partner.pk, partner.phone))
        self.stdout.write(HEADER)
        self.stdout.write('')
        self.stdout.write(rendered)
        self.stdout.write('')
        self.stdout.write(FOOTER)
        self.stdout.write('--- end preview ---')
