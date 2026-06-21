# -*- coding: utf-8 -*-
import logging
import threading
from contextlib import contextmanager
import requests
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext as _
import re
from modules.base.decorators import action, onchange
from modules.base.fields import AttachmentForeignKeyField
from modules.base.models.base import BaseModel

logger = logging.getLogger(__name__)

# Thread-local flag: set True only inside the Qurtoba sync receive view or sync action.
# QurtobaCustomer.pre_create() checks this — if not active, creation is blocked
# so the Genie UI cannot create customers manually.
_customer_sync_ctx = threading.local()


@contextmanager
def qurtoba_customer_sync_context():
    """Mark the current thread as a legitimate Qurtoba sync operation."""
    _customer_sync_ctx.active = True
    try:
        yield
    finally:
        _customer_sync_ctx.active = False


def _fk_id(val):
    """Accept int or {'id': N, 'name': '...'} returned by Qurtoba API."""
    if isinstance(val, dict):
        return val.get('id')
    return val


_ACCOUNT_TYPE_PREFIXES = ('فورى', 'أمان', 'كاش', 'أخرى', 'طاير')

def _is_account_type(s):
    return bool(s) and any(s.startswith(p) for p in _ACCOUNT_TYPE_PREFIXES)

def _parse_accounts(accounts_str):
    """
    Parse QurtobaCustomer.accounts (CSV) into [(type, number)] pairs.
    Format: 'فورى,6081844,أمان,970604,'  →  [('فورى','6081844'), ('أمان','970604')]
    Non-type tokens (notes like 'ماكينه', 'تطبيق') are skipped automatically.
    """
    if not accounts_str:
        return []
    parts = [p.strip() for p in accounts_str.split(',')]
    result = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if _is_account_type(part) and i + 1 < len(parts):
            number = parts[i + 1].strip()
            if number:
                result.append((part, number))
            i += 2
        else:
            i += 1
    return result


def _apply_name_format(name, device_no):
    """Return 'Name (device_no)' — strips existing suffix first."""
    base = re.sub(r'\s*\(\d+\)\s*$', '', name or '').strip()
    return f'{base} ({device_no})' if base and device_no else base



def _fetch_all_pages(url, headers, timeout=30):
    """Walk DRF paginated results and return a flat list."""
    items = []
    while url:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(str(e))
        if isinstance(data, list):
            items.extend(data)
            break
        items.extend(data.get('results', data.get('data', [])))
        url = data.get('next')
    return items


# ---------------------------------------------------------------------------
# QurtobaCustomerAccount — parsed account entries from CustomerInfo.accounts
# ---------------------------------------------------------------------------

class QurtobaCustomerAccount(BaseModel):
    """
    Each row is one account entry parsed from QurtobaCustomer.accounts (CSV).
    Source format: 'فورى,6081844,أمان,970604,'
    Synced automatically when customers are synced or pushed.
    Used as the account selector on the quick transaction form.
    """

    class Meta:
        verbose_name = 'Customer Account'
        verbose_name_plural = 'Customer Accounts'
        ordering = ['type', 'account_number']
        unique_together = [('customer', 'type', 'account_number')]

    customer       = models.ForeignKey(
        'QurtobaCustomer',
        on_delete=models.CASCADE,
        related_name='account_entries',
        verbose_name='Customer',
    )
    type           = models.CharField(max_length=50,  verbose_name='Type')
    account_number = models.CharField(max_length=100, verbose_name='Account Number')

    def __str__(self):
        return f'{self.type} — {self.account_number}'


def _sync_customer_accounts(customer):
    """Re-parse customer.accounts CSV and upsert QurtobaCustomerAccount rows."""
    parsed = _parse_accounts(customer.accounts or '')
    existing_ids = set()
    for acc_type, acc_number in parsed:
        obj, _ = QurtobaCustomerAccount.objects.get_or_create(
            customer=customer,
            type=acc_type,
            account_number=acc_number,
        )
        existing_ids.add(obj.pk)
    # Remove any account entries no longer in the CSV
    QurtobaCustomerAccount.objects.filter(customer=customer).exclude(pk__in=existing_ids).delete()


# ---------------------------------------------------------------------------
# QurtobaCustomer
# ---------------------------------------------------------------------------

class QurtobaCustomer(BaseModel):
    """
    Mirrors Qurtoba's CustomerInfo model.
    Populated by the real-time push from Qurtoba and by the manual sync action.
    qurtoba_id  = Qurtoba's CustomerInfo.pk  (the "Customer ID" in Qurtoba system)
    balance     = مديونيات — kept in sync with Qurtoba's Rest.value
    """

    class Meta:
        verbose_name = 'Qurtoba Customer'
        verbose_name_plural = 'Qurtoba Customers'
        ordering = ['name']

    # ---- Identity ----
    qurtoba_id   = models.IntegerField(unique=True, null=True, blank=True,
                                       verbose_name='Customer ID (Qurtoba)')
    name         = models.CharField(max_length=255, verbose_name='Name')
    sur_name     = models.CharField(max_length=255, null=True, blank=True)
    shop_name    = models.CharField(max_length=255, null=True, blank=True)
    device_no    = models.IntegerField(null=True, blank=True, verbose_name='Device No')
    shop_kind    = models.CharField(max_length=100, null=True, blank=True)
    phone_no     = models.CharField(max_length=50,  null=True, blank=True, verbose_name='Phone')
    address      = models.TextField(null=True, blank=True)
    area         = models.CharField(max_length=255, null=True, blank=True)

    # ---- Financial ----
    balance      = models.FloatField(default=0, null=True, blank=True,
                                     verbose_name='مديونيات')
    accounts     = models.TextField(null=True, blank=True)
    accounts_data = models.TextField(null=True, blank=True)
    grade        = models.IntegerField(null=True, blank=True)

    # ---- Relations (Qurtoba raw IDs — no local model for these yet) ----
    seller_qurtoba_id    = models.IntegerField(null=True, blank=True, verbose_name='Seller ID')
    assistant_qurtoba_id = models.IntegerField(null=True, blank=True)
    areas_qurtoba_id     = models.IntegerField(null=True, blank=True)

    # ---- Misc ----
    date       = models.DateField(null=True, blank=True)
    time       = models.TimeField(null=True, blank=True)
    notes      = models.TextField(null=True, blank=True)
    notes_plus = models.TextField(null=True, blank=True)

    def __str__(self):
        return f'{self.name} [{self.qurtoba_id}]' if self.qurtoba_id else self.name

    # ---- Prompt-facing computed properties ----
    # The dynamic AI prompt injects these via {{partner.qurtoba_customer.<prop>}}.
    # They MUST exist here or the template renders blank and the agent goes blind.

    @property
    def grade_limit_display(self):
        """Credit ceiling = grade × 1000 (0 if no grade)."""
        return (self.grade or 0) * 1000

    @property
    def available_credit(self):
        """How much credit remains before hitting the ceiling (can be negative)."""
        return self.grade_limit_display - (self.balance or 0)

    @property
    def accounts_pretty(self):
        """
        Human-readable list of the customer's registered فورى/أمان/طاير accounts,
        one per line, e.g.:
            فورى 6081844
            أمان 970604
        Reads the parsed QurtobaCustomerAccount rows; falls back to parsing the
        raw `accounts` CSV if no rows exist yet. Returns a clear marker when empty.
        """
        rows = list(self.account_entries.all().order_by('type', 'account_number')) \
            if hasattr(self, 'account_entries') else []
        pairs = [(r.type, r.account_number) for r in rows]
        if not pairs:
            pairs = _parse_accounts(self.accounts or '')
        if not pairs:
            return '(لا توجد حسابات مسجلة)'
        return '\n'.join(f'{t} {n}' for t, n in pairs)

    def _today_aggregate(self):
        """(count, debit_total, credit_total) for today's records on Qurtoba side."""
        from django.utils import timezone
        from django.db.models import Sum, Q
        today = timezone.localdate()
        qs = self.records.filter(date=today, is_done=False)
        agg = qs.aggregate(
            debit=Sum('value', filter=Q(is_down=False)),
            credit=Sum('value', filter=Q(is_down=True)),
        )
        return qs.count(), float(agg['debit'] or 0), float(agg['credit'] or 0)

    @property
    def today_count(self):
        return self._today_aggregate()[0]

    @property
    def today_debit(self):
        return self._today_aggregate()[1]

    @property
    def today_credit(self):
        return self._today_aggregate()[2]

    def _pull_balance_from_qurtoba(self):
        """
        Hit Qurtoba's REST API for the authoritative balance.

        Returns float or None (on failure / missing config / no qurtoba_id).
        Does NOT save — the caller does that.
        """
        if not self.qurtoba_id:
            return None

        from django.conf import settings
        base = getattr(settings, 'QURTOBA_BASE_URL', '') or ''
        token = getattr(settings, 'QURTOBA_TOKEN', '') or ''
        if not base or not token:
            return None

        try:
            import requests
            resp = requests.get(
                f'{base.rstrip("/")}/transactions/api2/rest-customer/{self.qurtoba_id}/',
                headers={'Authorization': f'Token {token}'},
                timeout=5,
            )
            if not resp.ok:
                logger.warning(
                    'recompute_balance: Qurtoba returned HTTP %s for customer %s',
                    resp.status_code, self.qurtoba_id,
                )
                return None
            data = resp.json()
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                return None
            raw = data.get('value')
            if raw is None:
                raw = data.get('rest')
            if raw is None:
                return None
            return float(raw)
        except Exception as exc:
            logger.warning(
                'recompute_balance: Qurtoba API fetch failed for customer %s: %s',
                self.qurtoba_id, exc,
            )
            return None

    def recompute_balance(self):
        """
        Refresh مديونيات from Qurtoba's REST API — Qurtoba is the canonical
        source of truth for the customer's balance.

        Behaviour:
          1) Try Qurtoba REST `/transactions/api2/rest-customer/{qurtoba_id}/`.
             On success: store the returned value in self.balance and return.
          2) Fallback (only when the API call fails OR no qurtoba_id):
             local sum of records that exist on the Qurtoba side
             (excludes orphan records that never reached Qurtoba), so the
             local view never drifts on top of phantom records.

        Records counted in the local-sum fallback:
          - customer_data_qurtoba_id IS NOT NULL  (came FROM Qurtoba sync), OR
          - qurtoba_synced = True                 (Genie-created AND push confirmed).

        Records explicitly EXCLUDED from the fallback sum:
          - is_done = True                        (archived).
          - qurtoba_synced = False AND customer_data_qurtoba_id IS NULL
            (orphan: never reached Qurtoba).
        """
        pulled = self._pull_balance_from_qurtoba()
        if pulled is not None:
            self.balance = pulled
            self.save(update_fields=['balance'])
            return

        # Fallback — local sum on Qurtoba-side records only
        on_qurtoba = (
            models.Q(qurtoba_synced=True)
            | models.Q(customer_data_qurtoba_id__isnull=False)
        )
        agg = self.records.filter(on_qurtoba, is_done=False).aggregate(
            debit=models.Sum(
                models.Case(
                    models.When(is_down=False, then=models.F('value')),
                    default=models.Value(0),
                    output_field=models.FloatField(),
                )
            ),
            credit=models.Sum(
                models.Case(
                    models.When(is_down=True, then=models.F('value')),
                    default=models.Value(0),
                    output_field=models.FloatField(),
                )
            ),
        )
        self.balance = (agg['debit'] or 0) - (agg['credit'] or 0)
        self.save(update_fields=['balance'])

    @onchange('name')
    def _onchange_format_name(self):
        """When name is edited in the UI, keep 'Name (device_no)' format."""
        self.name = _apply_name_format(self.name, self.device_no)

    def pre_create(self):
        super().pre_create()
        # Block manual creation from the Genie UI.
        # Customers may only be created via Qurtoba webhook or the sync button.
        if not getattr(_customer_sync_ctx, 'active', False):
            from django.core.exceptions import ValidationError
            raise ValidationError(
                _('لا يمكن إنشاء عملاء يدويًا. استخدم زر "مزامنة العملاء" لاستيراد العملاء من قرطبة.')
            )

    # ------------------------------------------------------------------ actions

    @action
    def action_sync_customers(queryset):
        """Pull ALL customers from Qurtoba (paginated) and upsert locally."""
        base = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
        token = getattr(settings, 'QURTOBA_TOKEN', '')
        if not base or not token:
            return {
                'status': False, 'open_mode': 'message',
                'message': _('Qurtoba not configured'), 'data': {},
            }

        headers = {'Authorization': f'Token {token}'}
        try:
            customers = _fetch_all_pages(f'{base}/customers/api2/customers/', headers)
        except RuntimeError as e:
            return {
                'status': False, 'open_mode': 'message',
                'message': _('فشل الاتصال بقرطبة: %(err)s') % {'err': str(e)}, 'data': {},
            }

        synced = 0
        for c in customers:
            qurtoba_id = c.get('id')
            phone_no   = c.get('phoneNo') or c.get('phone_no')
            raw_name   = c.get('name') or ''
            raw_dev_no = c.get('deviceNo') or c.get('device_no')
            fields = {
                'name':                 _apply_name_format(raw_name, raw_dev_no),
                'sur_name':             c.get('surName') or c.get('sur_name'),
                'shop_name':            c.get('shopName') or c.get('shop_name'),
                'device_no':            raw_dev_no,
                'shop_kind':            c.get('shopKind') or c.get('shop_kind'),
                'phone_no':             phone_no,
                'address':              c.get('address'),
                'area':                 c.get('area'),
                'accounts':             c.get('accounts'),
                'accounts_data':        c.get('accounts_data'),
                'grade':                c.get('grade'),
                'seller_qurtoba_id':    _fk_id(c.get('seller')),
                'assistant_qurtoba_id': _fk_id(c.get('assistant')),
                'areas_qurtoba_id':     _fk_id(c.get('areas')),
                'notes':                c.get('notes'),
                'notes_plus':           c.get('notes_plus'),
            }
            defaults = {k: v for k, v in fields.items() if v is not None}
            if qurtoba_id:
                defaults['qurtoba_id'] = qurtoba_id

            obj = None
            if qurtoba_id:
                obj = QurtobaCustomer.objects.filter(qurtoba_id=qurtoba_id).first()
            if not obj and phone_no:
                obj = QurtobaCustomer.objects.filter(phone_no=phone_no).first()

            if obj:
                for k, v in defaults.items():
                    setattr(obj, k, v)
                obj.save()
            elif defaults.get('name'):
                with qurtoba_customer_sync_context():
                    obj = QurtobaCustomer.objects.create(**defaults)
            else:
                continue

            # Sync parsed account entries for this customer
            _sync_customer_accounts(obj)
            synced += 1

        # Re-link any records that were saved before their customer existed
        _relink_orphaned_records()

        return {
            'status': True, 'open_mode': 'message',
            'message': _('تمت المزامنة: %(n)d عميل') % {'n': synced},
            'data': {}, 'on_success': {'type': 'refresh'},
        }

    @action
    def action_update_balance(queryset):
        """Fetch live مديونيات from Qurtoba Rest API for each selected customer."""
        base = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
        token = getattr(settings, 'QURTOBA_TOKEN', '')
        if not base or not token:
            return {
                'status': False, 'open_mode': 'message',
                'message': _('Qurtoba not configured'), 'data': {},
            }

        headers = {'Authorization': f'Token {token}'}
        updated = 0
        for customer in queryset:
            if not customer.qurtoba_id:
                continue
            try:
                resp = requests.get(
                    f'{base}/transactions/api2/rest-customer/{customer.qurtoba_id}/',
                    headers=headers, timeout=15,
                )
                if resp.ok:
                    data = resp.json()
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    raw = data.get('value') or data.get('rest') or 0
                    customer.balance = float(raw)
                    customer.save(update_fields=['balance'])
                    updated += 1
            except Exception as e:
                logger.error('Balance update failed for %s: %s', customer.qurtoba_id, e)

        return {
            'status': True, 'open_mode': 'message',
            'message': _('تم تحديث الرصيد: %(n)d عميل') % {'n': updated},
            'data': {}, 'on_success': {'type': 'refresh'},
        }

    @action
    def action_sync_accounts(queryset):
        """
        Re-parse each selected customer's `accounts` CSV into QurtobaCustomerAccount
        rows. Creates entries that are new AND deletes entries no longer in the CSV
        (full reconcile, handled by _sync_customer_accounts).
        Works on the whole table if nothing is selected (selection_required=False).
        """
        customers = list(queryset)
        if not customers:
            customers = list(QurtobaCustomer.objects.all())

        synced = 0
        total_accounts = 0
        for customer in customers:
            try:
                _sync_customer_accounts(customer)
                synced += 1
                total_accounts += QurtobaCustomerAccount.objects.filter(customer=customer).count()
            except Exception as e:
                logger.error('Account sync failed for customer %s: %s', customer.pk, e)

        return {
            'status': True, 'open_mode': 'message',
            'message': _('تمت مزامنة الحسابات: %(c)d عميل، %(a)d حساب') % {
                'c': synced, 'a': total_accounts,
            },
            'data': {}, 'on_success': {'type': 'refresh'},
        }

    @action
    def action_new_transaction(queryset):
        """Open the quick debt form pre-filled with this customer."""
        customer = list(queryset)[0]
        grade_limit     = customer.grade * 1000 if customer.grade is not None else 0
        current_balance = customer.balance or 0
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'auto_close': True,
            'data': {
                'menu_item_key': 'qurtoba_action_quick_debt',
                'view_type': 'form',
                'type': 'action',
                'title': _('تسجيل مديونية'),
                'context': {
                    'default_fields': {
                        'customer':         customer,
                        'grade_limit':      grade_limit,
                        'customer_balance': current_balance,
                        'extends_by':       0,
                    }
                },
            },
        }

    @action
    def action_new_collection(queryset):
        """Open the quick collection form pre-filled with this customer."""
        customer = list(queryset)[0]
        grade_limit     = customer.grade * 1000 if customer.grade is not None else 0
        current_balance = customer.balance or 0
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'auto_close': True,
            'data': {
                'menu_item_key': 'qurtoba_action_quick_collection',
                'view_type': 'form',
                'type': 'action',
                'title': _('تسجيل تحصيل'),
                'context': {
                    'default_fields': {
                        'customer':         customer,
                        'grade_limit':      grade_limit,
                        'customer_balance': current_balance,
                        'extends_by':       0,
                        'is_down':          True,
                        'is_seller':        False,   # شراء كاش/شراء فورى = customer self-pay
                    }
                },
            },
        }

    @action
    def action_view_transactions(queryset):
        """Open a slideover listing all transactions for this customer."""
        customer = list(queryset)[0]
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'auto_close': True,
            'data': {
                'type': 'action',
                'view_type': 'list',
                'view_key': 'qurtoba_record_list_view',
                'title': _('معاملات: %(name)s') % {'name': customer.name},
                'domain': {'customer': {'operator': 'eq', 'value': customer.id}},
            },
        }


# ---------------------------------------------------------------------------
# QurtobaRecord
# ---------------------------------------------------------------------------

class QurtobaRecord(BaseModel):
    """
    Mirrors Qurtoba's Record (transaction) model.
    customer             = FK to local QurtobaCustomer (resolved by customer_data_qurtoba_id)
    customer_data_qurtoba_id = Qurtoba's raw CustomerInfo.pk (kept as fallback reference)
    is_down=False → new debt (مديونية)    → customer.balance ++
    is_down=True  → payment/collection   → customer.balance --
    """

    class Meta:
        verbose_name = 'Qurtoba Record'
        verbose_name_plural = 'Qurtoba Records'
        ordering = ['-date', '-time']
        indexes = [
            # Fast path for the operator todo page: filter by فورى/أمان type
            # + fulfilment state without scanning the whole table.
            models.Index(fields=['account_task_state', 'type'], name='qr_acct_task_state_idx'),
        ]

    # ---- Relation to local customer (the smart link) ----
    customer = models.ForeignKey(
        QurtobaCustomer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='records',
        verbose_name='Customer',
    )
    # Raw Qurtoba IDs kept for reference / re-linking
    customer_data_qurtoba_id = models.IntegerField(null=True, blank=True,
                                                    verbose_name='Customer ID (Qurtoba)')
    seller_qurtoba_id        = models.IntegerField(null=True, blank=True,
                                                    verbose_name='Seller ID (Qurtoba)')
    accountant_qurtoba_id    = models.IntegerField(null=True, blank=True)

    # ---- Account selector (Genie-side only) ----
    # Linked to QurtobaCustomerAccount; onchange auto-fills type + account_number
    selected_account = models.ForeignKey(
        'QurtobaCustomerAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='records',
        verbose_name='Account',
    )

    # ---- Transaction type choices ----
    # isDown=False types add to customer debt; isDown=True types reduce it
    DEBT_TYPES = [
        ('كاش',          'كاش'),
        ('كاش(5)',        'كاش(5)'),
        ('كاش(10)',       'كاش(10)'),
        ('كاش(20)',       'كاش(20)'),
        ('فورى',         'فورى'),
        ('أمان',         'أمان'),
        ('طاير',         'طاير'),
        ('مصاريف خدمه', 'مصاريف خدمه'),
    ]
    COLLECTION_TYPES = [
        ('تحصيل',     'تحصيل'),      # collector visits customer
        ('شراء كاش',  'شراء كاش'),   # customer pays online (cash)
        ('شراء فورى', 'شراء فورى'),  # customer pays online (fawry)
    ]
    SETTLEMENT_TYPES = [
        ('مندوب', 'مندوب'),  # collector settles with office
    ]
    TYPE_CHOICES = DEBT_TYPES + COLLECTION_TYPES + SETTLEMENT_TYPES

    # Account-transfer types fulfilled MANUALLY by an operator (no Cash-SYS
    # automation): فورى / أمان. These drive the operator "todo" page.
    ACCOUNT_TASK_TYPES = ['فورى', 'أمان']

    # Operator-side fulfilment state for فورى/أمان transfers. This is a
    # Genie-local workflow flag ONLY — it is NEVER synced to Qurtoba/Cash-SYS,
    # and is deliberately SEPARATE from `is_done` (which means "money collected"
    # in the existing dues/collection flow, not "transfer executed").
    ACCOUNT_TASK_STATE_CHOICES = [
        ('pending',   'قيد التنفيذ'),
        ('completed', 'تم'),
        ('canceled',  'ملغي'),
    ]

    # ---- Transaction data ----
    type           = models.CharField(max_length=50, choices=TYPE_CHOICES, verbose_name='Type')
    account_number = models.CharField(max_length=255, null=True, blank=True)
    value          = models.FloatField(null=True, blank=True, verbose_name='Amount')
    rest           = models.FloatField(null=True, blank=True, verbose_name='Rest')
    is_done        = models.BooleanField(default=False, verbose_name='Done')
    is_down        = models.BooleanField(default=False, verbose_name='Is Payment')
    is_seller      = models.BooleanField(default=False)

    # ---- Timestamps ----
    date             = models.DateField(null=True, blank=True)
    time             = models.TimeField(null=True, blank=True)
    datetime_field   = models.DateTimeField(null=True, blank=True)
    date_receive     = models.DateField(null=True, blank=True)
    time_receive     = models.TimeField(null=True, blank=True)
    datetime_receive = models.DateTimeField(null=True, blank=True)

    notes    = models.TextField(null=True, blank=True)
    raw_data = models.JSONField(null=True, blank=True)

    # ---- Partner link: set when transaction is opened from a WhatsApp chat in Genie ----
    # Only records with this field set will receive a WhatsApp notification from Cash-SYS.
    partner = models.ForeignKey(
        'base.Partner',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='qurtoba_records',
        verbose_name='Partner',
    )

    # ---- Cash-SYS settlement (كاش types only, populated when Cash-SYS fires order_done) ----
    cash_sys_done    = models.BooleanField(default=False, verbose_name='Cash-SYS Done')
    cash_sys_done_at = models.DateTimeField(null=True, blank=True, verbose_name='Cash Done At')
    cash_sys_fee     = models.FloatField(null=True, blank=True, verbose_name='Cash-SYS Fee')
    cash_sys_sim     = models.CharField(max_length=20, null=True, blank=True, verbose_name='SIM Number')
    cash_sys_sim_code = models.CharField(max_length=20, null=True, blank=True, verbose_name='SIM Code')
    cash_sys_device  = models.CharField(max_length=100, null=True, blank=True, verbose_name='Device')
    cash_sys_operator = models.CharField(max_length=100, null=True, blank=True, verbose_name='Operator')

    # ---- Multi-step chain / reroute state (Cash-SYS may fulfil an order in
    #      several partial transfers, and the recipient number can change) ----
    CASH_SYS_STATE_CHOICES = [
        ('pending',  'Pending'),
        ('partial',  'Partial'),
        ('done',     'Done'),
        ('canceled', 'Canceled'),
        ('rerouted', 'Rerouted'),
    ]
    cash_sys_state          = models.CharField(
        max_length=20, choices=CASH_SYS_STATE_CHOICES, default='pending',
        db_index=True, verbose_name='Cash-SYS State',
    )
    cash_sys_fulfilled      = models.FloatField(null=True, blank=True, verbose_name='Fulfilled Amount')
    cash_sys_original_value = models.FloatField(null=True, blank=True, verbose_name='Original Value (pre-reroute)')
    cash_sys_reroute_amount = models.FloatField(null=True, blank=True, verbose_name='Reroute Amount (awaiting new number)')
    cash_sys_canceled_reason = models.CharField(max_length=20, null=True, blank=True, verbose_name='Cancel Reason')
    # Per-transfer briefs accumulated across the chain — drives batched receipts
    # and per-transfer send dedupe. Each entry:
    #   {id, value, transfer_to, fee, sim, device, executed_at, attachment_id, sent}
    cash_sys_transactions   = models.JSONField(null=True, blank=True, verbose_name='Cash-SYS Transfers')
    # Processed webhook keys "(event:order_id:txn_id)" for idempotent dedupe.
    cash_sys_event_log      = models.JSONField(default=list, blank=True, verbose_name='Processed Events')
    root_external_ref       = models.CharField(max_length=64, null=True, blank=True, db_index=True, verbose_name='Root External Ref')
    # Idempotency: the auto service-fee (مصاريف خدمه) records for this order were already created.
    cash_sys_service_fee_done = models.BooleanField(default=False, verbose_name='Service Fee Processed')

    # ---- Operator account-task state (فورى / أمان manual fulfilment) ----
    # Genie-local only — NOT synced to Qurtoba/Cash-SYS. Drives the operator
    # todo page. Independent of `is_done` (money-collection flag).
    account_task_state   = models.CharField(
        max_length=20, choices=ACCOUNT_TASK_STATE_CHOICES, default='pending',
        db_index=True, verbose_name='Account Task State',
    )
    account_task_done_at = models.DateTimeField(null=True, blank=True, verbose_name='Account Task Done At')
    account_task_done_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+', verbose_name='Account Task Done By',
    )

    # ---- Qurtoba sync status (for records created in Genie) ----
    qurtoba_record_id  = models.IntegerField(null=True, blank=True, verbose_name='Qurtoba Record ID')
    qurtoba_synced     = models.BooleanField(default=False, verbose_name='Posted to Qurtoba')
    qurtoba_posted_at  = models.DateTimeField(null=True, blank=True, verbose_name='Posted at')
    qurtoba_sync_error = models.TextField(null=True, blank=True, verbose_name='Sync Error')

    # ---- Chat linkage (set when the transaction is created from a WhatsApp message) ----
    # The inbound chat message that triggered this transaction. Lets the
    # execution receipt be sent back as a REPLY that quotes the partner's
    # original request, and lets the status tool resolve "did this go through?"
    # from a quoted message.
    origin_message = models.ForeignKey(
        'chat.Message',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='qurtoba_records',
        verbose_name='Source Message',
    )
    # The generated execution receipt image, persisted to the transaction.
    receipt_attachment = models.ForeignKey(
        'base.Attachment',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
        verbose_name='Receipt Image',
    )

    @property
    def grade_limit(self):
        """Credit ceiling for this customer: grade × 1000. Returns 0 if no grade set."""
        if self.customer_id and self.customer.grade is not None:
            return self.customer.grade * 1000
        return 0

    @property
    def customer_balance(self):
        """Current مديونيات (outstanding balance) for this customer."""
        if self.customer_id:
            return self.customer.balance or 0
        return None

    @property
    def extends_by(self):
        """How much the projected balance exceeds the grade limit (0 if within limit)."""
        if self.is_down or not self.customer_id or not self.value:
            return 0
        limit = self.grade_limit
        if limit is None:
            return 0
        projected = (self.customer_balance or 0) + float(self.value)
        return max(0.0, projected - limit)

    def __str__(self):
        customer_name = self.customer.name if self.customer_id else f'#{self.customer_data_qurtoba_id}'
        return f'{self.type} — {self.value} ({customer_name})'

    def set_origin_message_from_uuid(self, uuid_str, conversation=None, *, save=True):
        """
        Resolve a chat Message by its UUID (the value the AI reads from the
        `[message_id: <uuid>]` marker) and link it as this record's source
        message. Safe no-op when the id is missing/unresolvable so tool calls
        never fail just because the linkage couldn't be made.

        When `conversation` is given the lookup is scoped to it (so a partner
        can only point at their own messages).
        """
        if not uuid_str:
            return False
        try:
            from modules.chat.models import Message as ChatMessage
            qs = ChatMessage.objects.filter(id=str(uuid_str).strip())
            if conversation is not None:
                qs = qs.filter(conversation=conversation)
            msg = qs.first()
        except Exception:
            msg = None
        if msg is None:
            return False
        self.origin_message = msg
        if save:
            QurtobaRecord.objects.filter(pk=self.pk).update(origin_message=msg)
        return True

    _CASH_SYS_TYPES = {'كاش', 'كاش(5)', 'كاش(10)', 'كاش(20)'}

    def pre_save(self):
        super().pre_save()
        # Validate رقم الحساب only for cash-type records born in Genie (form saves).
        # Records that came FROM Qurtoba have customer_data_qurtoba_id set — skip those.
        if self.customer_data_qurtoba_id is not None:
            return  # webhook-received record — save as-is
        if self.type not in self._CASH_SYS_TYPES:
            return  # non-cash type — no phone required
        phone = ''.join(filter(str.isdigit, str(self.account_number or '')))
        if phone.startswith('01') and len(phone) > 11:
            phone = phone[:11]
        if not phone or not phone.startswith('01') or len(phone) != 11:
            from django.core.exceptions import ValidationError
            raise ValidationError({
                'account_number': _(
                    'رقم الحساب مطلوب ويجب أن يكون رقم هاتف صحيح (11 رقم يبدأ بـ 01) '
                    'للمعاملات النقدية (كاش)'
                )
            })

    def save(self, *args, **kwargs):
        from django.utils import timezone

        # Default empty value to 0
        if not self.value:
            self.value = 0

        # Auto-stamp date + time when missing — daily reports need the time to
        # render per-line timestamps. Records pushed from Qurtoba already
        # carry both, so this only fires for Genie-originated records.
        if not self.date:
            self.date = timezone.localdate()
        if not self.time:
            self.time = timezone.localtime().time().replace(microsecond=0)

        super().save(*args, **kwargs)
        if self.customer_id:
            try:
                customer = self.customer
                customer.recompute_balance()
                # Stamp rest = customer's balance snapshot AFTER this record
                # (mirrors Qurtoba: rest is a computed snapshot, not used in calculation)
                if self.rest != customer.balance:
                    QurtobaRecord.objects.filter(pk=self.pk).update(rest=customer.balance)
                    self.rest = customer.balance
            except Exception as e:
                logger.error('Post-save balance update failed for customer %s: %s', self.customer_id, e)

    def post_create(self):
        super().post_create()
        # customer_data_qurtoba_id is set only on records that came FROM Qurtoba.
        # Records born in Genie have it None — push those to Qurtoba.
        if self.customer_data_qurtoba_id is None and self.customer_id:
            from qurtoba.tasks import push_record_to_qurtoba_task
            push_record_to_qurtoba_task.delay(self.pk)

    @onchange('type')
    def _onchange_type_cash(self):
        """
        When type changes to a كاش variant: clear selected_account and account_number.
        This forces the SimpleVisibilityProcessor to re-evaluate invisible conditions
        after the server response updates the form values.
        """
        cash_types = ('كاش', 'كاش(5)', 'كاش(10)', 'كاش(20)')
        if self.type and self.type in cash_types:
            self.selected_account_id = None
            self.account_number = None

    @onchange('customer')
    def _onchange_customer_grade_info(self):
        """
        Push grade_limit, customer_balance, and account selector domain
        into the form when customer changes.
        """
        if not self.customer_id:
            return {
                'value': {
                    'grade_limit': 0,
                    'customer_balance': 0,
                    'selected_account': None,
                },
                'domain': {'selected_account': None},
            }
        try:
            customer = QurtobaCustomer.objects.get(pk=self.customer_id)
        except QurtobaCustomer.DoesNotExist:
            return

        grade_limit     = customer.grade * 1000 if customer.grade is not None else 0
        current_balance = customer.balance or 0

        result = {
            'value': {
                'grade_limit':      grade_limit,
                'customer_balance': current_balance,
                'selected_account': None,   # clear previous selection on customer change
            },
            'domain': {
                'selected_account': {
                    'filters': {
                        'operator': 'and',
                        'filters': [
                            {'field': 'customer', 'operator': 'eq', 'value': self.customer_id}
                        ]
                    }
                }
            },
        }
        if grade_limit is not None:
            result['warning'] = {
                'title': f'درجة الائتمان: {customer.grade}',
                'message': f'الحد: {grade_limit:,.0f} | الرصيد الحالي: {current_balance:,.0f}',
            }
            if current_balance >= grade_limit:
                result['errors'] = {
                    'value': _(
                        'تنبيه: الرصيد الحالي %(bal).0f يساوي أو يتجاوز الحد %(limit).0f'
                    ) % {'bal': current_balance, 'limit': grade_limit}
                }
        return result

    @onchange('selected_account')
    def _onchange_selected_account(self):
        """Auto-fill type and account_number when account selected; clear both when cleared."""
        if not self.selected_account_id:
            self.account_number = None
            return
        try:
            acc = QurtobaCustomerAccount.objects.get(pk=self.selected_account_id)
            self.type           = acc.type
            self.account_number = acc.account_number
        except QurtobaCustomerAccount.DoesNotExist:
            self.account_number = None

    @onchange('value')
    def _validate_grade_limit(self):
        """
        Live-updates footer fields when amount changes:
          - customer_balance → shows projected balance (current + value)
          - extends_by       → how much over the grade limit
        isDown=False only — payments don't add to debt.
        Always uses a fresh DB lookup (reliable for forms opened via actions).
        """
        if self.is_down:
            return {'value': {'extends_by': 0}}
        if not self.value:
            # Value cleared — reset projected balance back to current balance
            current = 0
            if self.customer_id:
                try:
                    c = QurtobaCustomer.objects.get(pk=self.customer_id)
                    current = c.balance or 0
                except QurtobaCustomer.DoesNotExist:
                    pass
            return {'value': {'extends_by': 0, 'customer_balance': current}}

        # Always fetch fresh from DB — most reliable path for all form origins
        limit   = 0
        current = 0
        if self.customer_id:
            try:
                customer = QurtobaCustomer.objects.get(pk=self.customer_id)
                limit   = customer.grade * 1000 if customer.grade is not None else 0
                current = customer.balance or 0
            except QurtobaCustomer.DoesNotExist:
                pass

        projected = current + float(self.value)
        # Overflow over the credit ceiling — applies even when the ceiling is 0
        # (grade 0/null): a customer with no allowance overflows as soon as the
        # projected balance goes positive.
        over      = max(0.0, projected - limit)

        result = {
            'value': {
                'customer_balance': projected,  # show projected balance live
                'extends_by':       over,
            }
        }

        if projected > limit:
            result['errors'] = {
                'value': _(
                    'تجاوز الحد الائتماني: '
                    'الرصيد %(bal).0f + المبلغ = %(proj).0f '
                    '(الحد %(limit).0f — تجاوز بـ %(over).0f)'
                ) % {
                    'bal':   current,
                    'proj':  projected,
                    'limit': limit,
                    'over':  over,
                }
            }
        return result

    @action
    def action_new_debt(queryset):
        """Open the debt-creation form in a slideover (from the debt list header)."""
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'auto_close': True,
            'data': {
                'menu_item_key': 'qurtoba_menu_debt',
                'view_type': 'form',
                'type': 'action',
                'title': _('تسجيل مديونية'),
            },
        }

    @action
    def action_new_collection(queryset):
        """Open the collection-recording form in a slideover (from the collections list header)."""
        return {
            'status': True,
            'open_mode': 'slideover',
            'on_success': {'type': 'refresh'},
            'auto_close': True,
            'data': {
                'menu_item_key': 'qurtoba_menu_collections',
                'view_type': 'form',
                'type': 'action',
                'title': _('تسجيل تحصيل'),
                'context': {
                    'default_fields': {
                        'is_down': True,
                        'is_seller': True,
                        'type': 'تحصيل',
                    }
                },
            },
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CashSysPlan / CashSysVipPage — local cache of Cash-SYS pricing catalog
# ---------------------------------------------------------------------------

class CashSysVipPage(models.Model):
    """
    Cached VIP page from Cash-SYS.
    Full replace on each pull via pull_cash_sys_catalog_task.
    """

    class Meta:
        verbose_name = 'Cash-SYS VIP Page'
        verbose_name_plural = 'Cash-SYS VIP Pages'
        ordering = ['name']

    cash_sys_id = models.IntegerField(unique=True, verbose_name='Cash-SYS ID')
    name        = models.CharField(max_length=100)
    key         = models.CharField(max_length=50)
    price       = models.DecimalField(max_digits=8, decimal_places=2)
    synced_at   = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class CashSysPlan(models.Model):
    """
    Cached pricing plan from Cash-SYS.
    Full replace on each pull via pull_cash_sys_catalog_task.
    vip_pages stores the embedded vip page list as JSON (denormalised snapshot).
    """

    class Meta:
        verbose_name = 'Cash-SYS Plan'
        verbose_name_plural = 'Cash-SYS Plans'
        ordering = ['price']

    cash_sys_id   = models.IntegerField(unique=True, verbose_name='Cash-SYS ID')
    name          = models.CharField(max_length=255)
    type          = models.CharField(max_length=20)   # 'plan' | 'extra'
    price         = models.DecimalField(max_digits=10, decimal_places=2)
    device_limit  = models.PositiveIntegerField(default=0)
    sim_limit     = models.PositiveIntegerField(default=0)
    account_limit = models.PositiveIntegerField(default=0)
    vip_pages     = models.JSONField(default=list)
    is_active     = models.BooleanField(default=True)
    synced_at     = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.name} ({self.price} EGP)'

    def pricing_info(self) -> dict:
        """
        Return pricing details for this plan including its included VIP pages.

        Returns:
            {
                "id":            int,
                "name":          str,
                "type":          "plan" | "extra",
                "price":         float,
                "device_limit":  int,
                "sim_limit":     int,
                "account_limit": int,
                "is_active":     bool,
                "vip_pages": [
                    {"id": int, "name": str, "key": str, "price": float},
                    ...
                ]
            }
        """
        return {
            "id":            self.cash_sys_id,
            "name":          self.name,
            "type":          self.type,
            "price":         float(self.price),
            "device_limit":  self.device_limit,
            "sim_limit":     self.sim_limit,
            "account_limit": self.account_limit,
            "is_active":     self.is_active,
            "vip_pages": [
                {
                    "id":    page.get("id"),
                    "name":  page.get("name"),
                    "key":   page.get("key"),
                    "price": float(page.get("price", 0)),
                }
                for page in (self.vip_pages or [])
            ],
        }

    @classmethod
    def get_catalog(cls) -> dict:
        """
        Return all active plans with their pricing and the full list of system VIP pages.

        Returns:
            {
                "plans": [<pricing_info()>, ...],   # ordered by price
                "vip_pages": [
                    {"id": int, "name": str, "key": str, "price": float},
                    ...
                ]
            }
        """
        plans = [plan.pricing_info() for plan in cls.objects.filter(is_active=True)]

        vip_pages = [
            {
                "id":    vp.cash_sys_id,
                "name":  vp.name,
                "key":   vp.key,
                "price": float(vp.price),
            }
            for vp in CashSysVipPage.objects.all()
        ]

        return {"plans": plans, "vip_pages": vip_pages}


# ---------------------------------------------------------------------------
# Pending review queues
#
# Two queues, both seeded by the chat-agent tools instead of hard-rejecting:
#   - QurtobaPendingTransaction : debt items that would exceed the credit
#                                  ceiling. Staff approve → real record is
#                                  created with override_grade_limit=True.
#   - QurtobaPendingPayment     : every سداد. Mandatory screenshot. Staff
#                                  approve → real record(is_down=True) is
#                                  created (and pushed to Qurtoba by the
#                                  existing post_create path).
#
# Reviewer can deny → state flips to denied, no record is ever created.
# ---------------------------------------------------------------------------

_REVIEW_STATE_CHOICES = [
    ('pending',  'قيد المراجعة'),
    ('approved', 'موافق عليه'),
    ('denied',   'مرفوض'),
]


class QurtobaPendingTransaction(BaseModel):
    """Debt transaction parked for admin review (always: credit-limit overflow)."""

    class Meta:
        verbose_name = 'Pending Qurtoba Transaction'
        verbose_name_plural = 'Pending Qurtoba Transactions'
        ordering = ['-created_at']

    REASON_CHOICES = [('grade_limit_exceeded', 'تجاوز الحد الائتماني')]

    # Identity
    customer = models.ForeignKey(
        QurtobaCustomer, on_delete=models.CASCADE,
        related_name='pending_transactions', verbose_name='Customer',
    )
    partner = models.ForeignKey(
        'base.Partner', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='qurtoba_pending_transactions',
        verbose_name='Chat Partner',
    )
    conversation_uuid = models.CharField(max_length=64, null=True, blank=True)
    # The inbound chat message (UUID) that triggered this transfer — preserved
    # across the review→approve cycle so the approved record links its
    # origin_message and the execution receipt can reply-quote the number message.
    source_message_id = models.CharField(max_length=64, null=True, blank=True, verbose_name='Source Message')

    # Payload (mirrors QurtobaRecord debt fields)
    type           = models.CharField(max_length=50, choices=QurtobaRecord.DEBT_TYPES, verbose_name='Type')
    value          = models.FloatField(verbose_name='Amount')
    account_number = models.CharField(max_length=255, null=True, blank=True)
    notes          = models.TextField(null=True, blank=True)

    # Review state
    reason         = models.CharField(max_length=40, choices=REASON_CHOICES, default='grade_limit_exceeded')
    review_state   = models.CharField(max_length=20, choices=_REVIEW_STATE_CHOICES, default='pending', db_index=True)
    reviewer       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at    = models.DateTimeField(null=True, blank=True)
    denial_reason  = models.TextField(null=True, blank=True)

    # Set when approved → links to the real record that was created
    created_record = models.ForeignKey(
        QurtobaRecord, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='Created Record',
    )

    def __str__(self):
        return f'PendingTxn #{self.pk} — {self.type} {self.value} ({self.customer.name})'

    @property
    def customer_grade(self):
        """Customer credit grade — raw value as stored (NOT × 1000); 0 when unset."""
        if self.customer_id:
            return self.customer.grade or 0
        return 0

    @property
    def customer_balance(self):
        """Customer current outstanding balance (مديونيات / rest)."""
        if self.customer_id:
            return self.customer.balance or 0
        return 0

    @action
    def action_approve_pending_transaction(queryset):
        """
        Approve selected pending transactions: create the real records (with
        grade-limit override) via _create_one_debt. The push to Qurtoba (and
        Cash-SYS for cash types) happens through the SINGLE async path
        (post_create → push_record_to_qurtoba_task); this action never pushes
        directly and never creates a QurtobaSyncProblem.
        """
        from django.utils import timezone as _tz
        from qurtoba.tools.transactions import _create_one_debt, CASH_TYPES
        approved = 0
        skipped = 0
        for pending in queryset:
            if pending.review_state != 'pending' or pending.created_record_id:
                skipped += 1
                continue
            is_cash = pending.type in CASH_TYPES
            outcome = _create_one_debt(
                customer=pending.customer,
                social_partner=pending.partner,
                type=pending.type,
                amount=float(pending.value or 0),
                final_account=pending.account_number,
                is_cash=is_cash,
                notes=(pending.notes or '')[:100] or None,
                override_grade_limit=True,
                conversation=None,
                # Restore the chat message linkage captured when the transaction
                # was parked, so the approved record's origin_message is set and
                # the execution receipt replies to the number message.
                source_message_id=pending.source_message_id,
            )
            if outcome.get('success'):
                pending.review_state = 'approved'
                pending.reviewed_at = _tz.now()
                try:
                    pending.created_record = QurtobaRecord.objects.get(pk=outcome['record_id'])
                except QurtobaRecord.DoesNotExist:
                    pending.created_record = None
                pending.save(update_fields=['review_state', 'reviewed_at', 'created_record'])
                approved += 1
            else:
                skipped += 1
        message = f'تمت الموافقة على {approved} معاملة' + (f' وتعذّر {skipped}.' if skipped else '.')
        return {
            'status': True,
            'open_mode': 'message',
            'message': message,
            'data': {},
            'on_success': {'type': 'refresh'},
        }

    @action
    def action_deny_pending_transaction(queryset):
        """Deny selected pending transactions: mark denied, do not create records."""
        from django.utils import timezone as _tz
        denied = 0
        for pending in queryset:
            if pending.review_state != 'pending':
                continue
            pending.review_state = 'denied'
            pending.reviewed_at = _tz.now()
            pending.save(update_fields=['review_state', 'reviewed_at'])
            denied += 1
        return {
            'status': True,
            'open_mode': 'message',
            'message': f'تم رفض {denied} معاملة.',
            'data': {},
            'on_success': {'type': 'refresh'},
        }


class QurtobaPendingPayment(BaseModel):
    """Customer payment (سداد) parked for admin review. Screenshot mandatory."""

    class Meta:
        verbose_name = 'Pending Qurtoba Payment'
        verbose_name_plural = 'Pending Qurtoba Payments'
        ordering = ['-created_at']

    PAYMENT_TYPE_CHOICES = [
        ('شراء كاش',  'شراء كاش'),
        ('شراء فورى', 'شراء فورى'),
    ]
    REASON_CHOICES = [('payment_review', 'سداد بانتظار المراجعة')]

    # Identity
    customer = models.ForeignKey(
        QurtobaCustomer, on_delete=models.CASCADE,
        related_name='pending_payments', verbose_name='Customer',
    )
    partner = models.ForeignKey(
        'base.Partner', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='qurtoba_pending_payments',
        verbose_name='Chat Partner',
    )
    conversation_uuid = models.CharField(max_length=64, null=True, blank=True)
    # The inbound chat message (UUID) that carried the receipt image. Lets the
    # status tool answer "is this image's payment done?" and lets the approved
    # record reply-quote the receipt message.
    source_message_id = models.CharField(max_length=64, null=True, blank=True, verbose_name='Source Message')

    # Payload
    type                       = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES, verbose_name='Type')
    value                      = models.FloatField(verbose_name='Amount')
    account_number             = models.CharField(max_length=255, null=True, blank=True)
    notes                      = models.TextField(null=True, blank=True)
    customer_confirmation_text = models.TextField(verbose_name='Customer Confirmation')

    # Mandatory screenshot — bridged from chat.MessageAttachment via
    # Attachment.from_chat_attachment (shares the storage path, no copy).
    # Uses AttachmentForeignKeyField (not a plain FK) so the form view's image
    # widget receives the FULL attachment object (with `url`) — a plain FK
    # serializes to just {id} and the image renders blank.
    screenshot_attachment = AttachmentForeignKeyField(
        upload_to='qurtoba/payment_screenshots',
        allowed_types=['image'],
        on_delete=models.PROTECT,
        null=False, blank=False,
        related_name='+', verbose_name='Screenshot',
    )

    # Review state
    reason        = models.CharField(max_length=40, choices=REASON_CHOICES, default='payment_review')
    review_state  = models.CharField(max_length=20, choices=_REVIEW_STATE_CHOICES, default='pending', db_index=True)
    reviewer      = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at   = models.DateTimeField(null=True, blank=True)
    denial_reason = models.TextField(null=True, blank=True)

    created_record = models.ForeignKey(
        QurtobaRecord, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='Created Record',
    )

    def __str__(self):
        return f'PendingPayment #{self.pk} — {self.type} {self.value} ({self.customer.name})'

    @property
    def customer_grade(self):
        """Customer credit grade — raw value as stored (NOT × 1000); 0 when unset."""
        if self.customer_id:
            return self.customer.grade or 0
        return 0

    @property
    def customer_balance(self):
        """Customer current outstanding balance (مديونيات / rest)."""
        if self.customer_id:
            return self.customer.balance or 0
        return 0

    @staticmethod
    def _confirm_message(value, customer_name, rest):
        """
        Python-generated approval line: '{value} | {customer} | {rest} باقى'.
        When rest < 0 the merchant owes the customer → '... | {abs} ليك'.
        """
        v = int(round(float(value or 0)))
        r = int(round(float(rest or 0)))
        tail = f'{abs(r)} ليك' if r < 0 else f'{r} باقى'
        return f'{v} | {customer_name} | {tail}'

    @action
    def action_approve_pending_payment(queryset):
        """
        Approve pending payments: create the real QurtobaRecord(is_down=True) and
        notify the chat. The push to Qurtoba happens through the SINGLE async path
        (QurtobaRecord.post_create → push_record_to_qurtoba_task); this action never
        pushes directly (that caused a double transaction) and never creates a
        QurtobaSyncProblem (only the Celery task does, on retry-exhaustion).
        """
        from django.db import transaction
        from django.utils import timezone as _tz
        approved = 0
        skipped = 0
        for pending in queryset:
            if pending.review_state != 'pending':
                skipped += 1
                continue
            # Re-approve guard: a pending already linked to a record is done.
            if pending.created_record_id:
                skipped += 1
                continue
            audit_note = f'سداد {pending.type} {int(pending.value or 0)}'[:100]
            # Balance BEFORE the payment → the resulting "rest" is deterministic
            # (balance_before - value) and immune to the async Qurtoba push timing.
            balance_before = float(getattr(pending.customer, 'balance', 0) or 0)
            try:
                # Create the record + mark the pending approved atomically, so a
                # mid-flow failure can't leave an orphan record that a re-approve
                # would duplicate. post_create's on_commit enqueues the push when
                # this block commits.
                with transaction.atomic():
                    record = QurtobaRecord.objects.create(
                        customer=pending.customer,
                        type=pending.type,
                        value=pending.value,
                        account_number=pending.account_number,
                        is_down=True,
                        is_seller=False,
                        partner=pending.partner,
                        notes=audit_note,
                    )
                    # Link the receipt image message so the confirmation reply-quotes it.
                    if pending.source_message_id:
                        try:
                            record.set_origin_message_from_uuid(pending.source_message_id)
                        except Exception:
                            pass
                    pending.review_state = 'approved'
                    pending.reviewed_at = _tz.now()
                    pending.created_record = record
                    pending.save(update_fields=['review_state', 'reviewed_at', 'created_record'])
            except Exception as exc:
                logger.warning('Failed to approve pending payment %s: %s', pending.pk, exc)
                skipped += 1
                continue
            approved += 1

            # Notify the chat directly (Python-generated, no AI): value | name | rest.
            # Done AFTER the atomic commit — an external send must not sit in a txn.
            try:
                rest = balance_before - float(pending.value or 0)
                msg = QurtobaPendingPayment._confirm_message(
                    pending.value, pending.customer.name, rest,
                )
                from qurtoba.tasks import send_text_reply_for_record
                send_text_reply_for_record(record, msg)
            except Exception as exc:
                logger.warning('Payment approve chat notify failed for pending %s: %s', pending.pk, exc)

        message = f'تمت الموافقة على {approved} سداد' + (f' وتعذّر {skipped}.' if skipped else '.')
        return {
            'status': True,
            'open_mode': 'message',
            'message': message,
            'data': {},
            'on_success': {'type': 'refresh'},
        }

    @action
    def action_deny_pending_payment(queryset):
        """Deny pending payments: mark denied, do not create any QurtobaRecord."""
        from django.utils import timezone as _tz
        denied = 0
        for pending in queryset:
            if pending.review_state != 'pending':
                continue
            pending.review_state = 'denied'
            pending.reviewed_at = _tz.now()
            pending.save(update_fields=['review_state', 'reviewed_at'])
            denied += 1
        return {
            'status': True,
            'open_mode': 'message',
            'message': f'تم رفض {denied} سداد.',
            'data': {},
            'on_success': {'type': 'refresh'},
        }


def _relink_orphaned_records():
    """
    After a customer sync, re-link QurtobaRecords that have
    customer_data_qurtoba_id set but no customer FK.
    """
    orphans = QurtobaRecord.objects.filter(
        customer__isnull=True,
        customer_data_qurtoba_id__isnull=False,
    )
    relinked = 0
    for rec in orphans:
        customer = QurtobaCustomer.objects.filter(
            qurtoba_id=rec.customer_data_qurtoba_id
        ).first()
        if customer:
            rec.customer = customer
            rec.save(update_fields=['customer'])
            relinked += 1
    if relinked:
        logger.info('Re-linked %d orphaned records to customers.', relinked)


# ---------------------------------------------------------------------------
# QurtobaSyncProblem — failed-push dead-letter queue
#
# When a push to the Qurtoba canonical server exhausts its Celery retries, a row
# is created here (generic link to the source record), all admins are notified,
# and an operator can retry it from the UI in the FOREGROUND (single or bulk),
# seeing the real server response. Resolved rows flip to status='done' and are
# filtered out of the list view.
# ---------------------------------------------------------------------------

class QurtobaSyncProblem(BaseModel):
    """A qurtoba push that failed after all retries — actionable from the UI."""

    class Meta:
        verbose_name = 'Qurtoba Sync Problem'
        verbose_name_plural = 'Qurtoba Sync Problems'
        ordering = ['-created_at']

    OPERATION_CHOICES = [
        ('push_record', 'Push Record'),
    ]
    STATUS_CHOICES = [
        ('failed', 'فشل'),
        ('done',   'تم'),
    ]

    # Generic link to the source record (QurtobaRecord today; any pushable model later)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id    = models.CharField(max_length=64, null=True, blank=True)
    target       = GenericForeignKey('content_type', 'object_id')
    # Denormalized "app.model" so the list renders without a join.
    model_label  = models.CharField(max_length=120, null=True, blank=True, verbose_name='Model')

    operation    = models.CharField(max_length=40, choices=OPERATION_CHOICES, default='push_record', verbose_name='Operation')
    status       = models.CharField(max_length=12, choices=STATUS_CHOICES, default='failed', db_index=True, verbose_name='Status')
    error        = models.TextField(null=True, blank=True, verbose_name='Last Error')
    attempts     = models.IntegerField(default=0, verbose_name='Attempts')
    # Stored as a pretty JSON STRING (not a JSONField) so the form/list widgets
    # render it as readable text instead of "[object Object]".
    payload      = models.TextField(null=True, blank=True, verbose_name='Payload')

    last_attempt_at = models.DateTimeField(null=True, blank=True, verbose_name='Last Attempt')
    resolved_at     = models.DateTimeField(null=True, blank=True, verbose_name='Resolved At')
    resolved_by     = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+', verbose_name='Resolved By',
    )
    notified     = models.BooleanField(default=False, verbose_name='Notified')

    def __str__(self):
        return f'SyncProblem #{self.pk} {self.model_label}#{self.object_id} [{self.status}]'

    # ---- producer ----------------------------------------------------------
    @classmethod
    def record(cls, target, operation, error, payload=None):
        """
        Upsert an OPEN (status='failed') problem row for target+operation and
        notify admins on first creation. Idempotent across retries.
        """
        from django.utils import timezone
        import json
        ct = ContentType.objects.get_for_model(type(target))
        model_label = f'{ct.app_label}.{ct.model}'
        # Stringify the payload so it stores/renders as readable text, never a raw object.
        if payload is None or isinstance(payload, str):
            payload_str = payload
        else:
            try:
                payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                payload_str = str(payload)
        problem, _created = cls.objects.update_or_create(
            content_type=ct,
            object_id=str(target.pk),
            operation=operation,
            status='failed',
            defaults={
                'error': error or '',
                'payload': payload_str,
                'model_label': model_label,
                'last_attempt_at': timezone.now(),
            },
        )
        cls.objects.filter(pk=problem.pk).update(attempts=models.F('attempts') + 1)
        problem.refresh_from_db(fields=['attempts', 'notified'])

        if not problem.notified:
            try:
                problem._notify_admins()
            except Exception as exc:
                logger.warning('QurtobaSyncProblem notify failed for %s: %s', problem.pk, exc)
            # mark notified regardless so a flaky notifier never spams on every retry
            cls.objects.filter(pk=problem.pk).update(notified=True)
            problem.notified = True
        return problem

    def _notify_admins(self):
        """Inbox + web-push notification to every active superuser."""
        from modules.notifications.services import post_notification
        from modules.base.models import User, MenuItem
        partner_ids = list(
            User.objects
            .filter(is_superuser=True, is_active=True, partner__isnull=False)
            .values_list('partner_id', flat=True)
        )
        if not partner_ids:
            return
        # Build a form-view URL so clicking the notification opens THIS problem row
        # (not the home screen). Same helper the pending-review notifications use.
        try:
            url = MenuItem.get_url_for_model(model=self, view_type='form', id=self.pk)
        except Exception:
            url = '/'
        post_notification(
            partner_ids=partner_ids,
            subject='فشل مزامنة قرطبة',
            body=f'فشل دفع {self.model_label} #{self.object_id} إلى قرطبة: {(self.error or "")[:160]}',
            notification_type='inbox',
            is_push=True,
            record=self,
            url=url,
        )

    # ---- foreground retry --------------------------------------------------
    def _retry_one(self):
        """
        Re-run the push synchronously. Returns (ok: bool, message: str).
        On success → status='done'; on failure → store the fresh error.
        """
        from django.utils import timezone
        self.attempts = (self.attempts or 0) + 1
        self.last_attempt_at = timezone.now()

        if self.operation == 'push_record':
            from qurtoba.utils_sync import push_record_to_qurtoba
            try:
                err = push_record_to_qurtoba(int(self.object_id))
            except Exception as exc:
                err = str(exc)
        else:
            err = f'Unknown operation: {self.operation}'

        if err is None:
            user = getattr(getattr(self, 'env', None), 'user', None)
            if user is not None and not getattr(user, 'is_authenticated', False):
                user = None
            self.status = 'done'
            self.resolved_at = timezone.now()
            self.resolved_by = user
            self.save(update_fields=['status', 'attempts', 'last_attempt_at', 'resolved_at', 'resolved_by'])
            return True, ''

        self.error = err
        self.save(update_fields=['error', 'attempts', 'last_attempt_at'])
        return False, err

    @action
    def action_retry_sync(queryset):
        """Retry selected sync problems in the foreground (single or bulk)."""
        done = 0
        failed = 0
        errors = []
        for prob in queryset:
            if prob.status == 'done':
                continue
            ok, msg = prob._retry_one()
            if ok:
                done += 1
            else:
                failed += 1
                if len(errors) < 3 and msg:
                    errors.append(f'#{prob.object_id}: {msg[:120]}')
        parts = [f'تم: {done} — فشل: {failed}']
        if errors:
            parts.append('\n'.join(errors))
        return {
            'status': True,
            'open_mode': 'message',
            'message': '\n'.join(parts),
            'data': {},
            'on_success': {'type': 'refresh'},
        }
