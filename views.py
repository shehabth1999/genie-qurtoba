# -*- coding: utf-8 -*-
import hmac
import hashlib
import logging
import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from inertia import render as inertia_render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import QurtobaCustomer, QurtobaRecord, _sync_customer_accounts, qurtoba_customer_sync_context
from .serializers import QurtobaCustomerSerializer, QurtobaRecordSerializer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sync API views (Qurtoba → Genie push)
# ---------------------------------------------------------------------------

class QurtobaCustomerListView(APIView):
    """
    POST /customers/api2/customers/
    Receives a new CustomerInfo forwarded from Qurtoba.
    Upserts by phone_no (unique in Qurtoba source).
    """

    def post(self, request):
        data = request.data
        phone_no = data.get('phoneNo') or data.get('phone_no')

        if phone_no:
            customer = QurtobaCustomer.objects.filter(phone_no=phone_no).first()
        else:
            customer = None

        if customer:
            ser = QurtobaCustomerSerializer(customer, data=data, partial=True)
        else:
            ser = QurtobaCustomerSerializer(data=data)

        if ser.is_valid():
            with qurtoba_customer_sync_context():
                obj = ser.save()
            _sync_customer_accounts(obj)
            return Response({'status': True}, status=status.HTTP_200_OK)

        return Response({'status': False, 'errors': ser.errors}, status=status.HTTP_400_BAD_REQUEST)


class QurtobaCustomerDetailView(APIView):
    """
    POST /customers/api2/customers/<pk>/
    Receives an updated CustomerInfo forwarded from Qurtoba.
    pk is the Qurtoba CustomerInfo primary key.
    Upserts by qurtoba_id, falls back to phone_no to backfill the ID.
    """

    def post(self, request, pk):
        data = request.data
        phone_no = data.get('phoneNo') or data.get('phone_no')

        customer = QurtobaCustomer.objects.filter(qurtoba_id=pk).first()
        if not customer and phone_no:
            customer = QurtobaCustomer.objects.filter(phone_no=phone_no).first()

        if customer:
            ser = QurtobaCustomerSerializer(customer, data=data, partial=True)
            if ser.is_valid():
                obj = ser.save()
                if obj.qurtoba_id != pk:
                    obj.qurtoba_id = pk
                    obj.save(update_fields=['qurtoba_id'])
                _sync_customer_accounts(obj)
                return Response({'status': True}, status=status.HTTP_200_OK)
            return Response({'status': False, 'errors': ser.errors}, status=status.HTTP_400_BAD_REQUEST)

        ser = QurtobaCustomerSerializer(data=data)
        if ser.is_valid():
            with qurtoba_customer_sync_context():
                obj = ser.save()
            obj.qurtoba_id = pk
            obj.save(update_fields=['qurtoba_id'])
            _sync_customer_accounts(obj)
            return Response({'status': True}, status=status.HTTP_201_CREATED)

        return Response({'status': False, 'errors': ser.errors}, status=status.HTTP_400_BAD_REQUEST)


class QurtobaRecordListView(APIView):
    """
    POST /transactions/api2/record/
    Receives a new Record (transaction) forwarded from Qurtoba.
    Always creates; stores raw payload for debugging.
    """

    def post(self, request):
        from django.utils import timezone
        data = request.data
        ser = QurtobaRecordSerializer(data=data)
        if ser.is_valid():
            obj = ser.save()
            # Resolve customer FK and attach — QurtobaRecord.save() will then
            # call recompute_balance() automatically (handles isDone flip too)
            customer = self._resolve_customer(obj.customer_data_qurtoba_id)
            update_fields = ['raw_data', 'qurtoba_synced', 'qurtoba_posted_at']
            obj.raw_data = dict(data)
            # Mark as already synced — this record originated in Qurtoba, not Genie.
            # Prevents post_create() from trying to push it back to Qurtoba.
            obj.qurtoba_synced = True
            obj.qurtoba_posted_at = timezone.now()
            # Store Qurtoba's record ID so Cash-SYS webhook can match it back
            qurtoba_record_id = data.get('_record_id')
            if qurtoba_record_id:
                try:
                    obj.qurtoba_record_id = int(qurtoba_record_id)
                    update_fields.append('qurtoba_record_id')
                except (ValueError, TypeError):
                    pass
            if customer:
                obj.customer = customer
                update_fields.append('customer')
            obj.save(update_fields=update_fields)
            # Pull authoritative balance from Qurtoba after receiving its push.
            # recompute_balance() only counts local Genie records — if Qurtoba has more
            # records than Genie has synced, the local balance would be wrong.
            if customer:
                from qurtoba.utils_sync import _sync_customer_balance
                _sync_customer_balance(
                    getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/'),
                    getattr(settings, 'QURTOBA_TOKEN', ''),
                    customer,
                )
            return Response({'status': True}, status=status.HTTP_201_CREATED)

        return Response({'status': False, 'errors': ser.errors}, status=status.HTTP_400_BAD_REQUEST)

    @staticmethod
    def _resolve_customer(customer_qurtoba_id):
        """Look up local QurtobaCustomer by Qurtoba's CustomerInfo.pk."""
        if not customer_qurtoba_id:
            return None
        return QurtobaCustomer.objects.filter(qurtoba_id=customer_qurtoba_id).first()


# ---------------------------------------------------------------------------
# Internal helper — proxy GET/POST to Qurtoba
# ---------------------------------------------------------------------------

def _qurtoba_get(path, params=None):
    """Proxy a GET request to Qurtoba. Returns (json_data, error_string)."""
    base = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
    token = getattr(settings, 'QURTOBA_TOKEN', '')
    if not base or not token:
        return None, 'Qurtoba not configured (QURTOBA_BASE_URL / QURTOBA_TOKEN missing)'
    try:
        resp = requests.get(
            f'{base}{path}',
            params=params,
            headers={'Authorization': f'Token {token}'},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json(), None
    except Exception as e:
        logger.error('Qurtoba GET %s failed: %s', path, e)
        return None, str(e)


def _qurtoba_post(path, params=None):
    """Proxy a POST request to Qurtoba. Returns (json_data, error_string)."""
    base = getattr(settings, 'QURTOBA_BASE_URL', '').rstrip('/')
    token = getattr(settings, 'QURTOBA_TOKEN', '')
    if not base or not token:
        return None, 'Qurtoba not configured'
    try:
        resp = requests.post(
            f'{base}{path}',
            params=params,
            headers={'Authorization': f'Token {token}'},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json(), None
    except Exception as e:
        logger.error('Qurtoba POST %s failed: %s', path, e)
        return None, str(e)


def _sellers_list():
    """Fetch seller list for dropdowns."""
    data, _ = _qurtoba_get('/customers/api2/sellers/')
    if not data:
        return []
    return data if isinstance(data, list) else data.get('results', data.get('data', []))


# ---------------------------------------------------------------------------
# Inertia page views — served to the browser, React handles the UI
# ---------------------------------------------------------------------------

@login_required
def customer_dues_view(request):
    """مستحقات العملاء — Customer Dues page."""
    params = {k: v for k, v in request.GET.items() if v}
    data, error = _qurtoba_get('/transactions/api2/dues/', params)
    sellers, _ = _qurtoba_get('/customers/api2/sellers/')
    return inertia_render(request, 'qurtoba::CustomerDues/index', {
        'rows': (data or {}).get('data', []),
        'sellers': sellers if isinstance(sellers, list) else [],
        'filters': params,
        'error': error,
    })


@login_required
def seller_dues_view(request):
    """مستحقات المناديب — Seller / Collector Dues page."""
    data, error = _qurtoba_get('/transactions/api2/collector-dues/')
    return inertia_render(request, 'qurtoba::SellerDues/index', {
        'sellers': (data or {}).get('result', []),
        'error': error,
    })


@login_required
def accountant_report_view(request):
    """تقارير المحاسب — Accountant Report page."""
    from datetime import date
    today = date.today().isoformat()
    params = {
        'df': request.GET.get('df', today),
        'dt': request.GET.get('dt', today),
    }
    data, error = _qurtoba_get('/transactions/api2/reports/', params)
    return inertia_render(request, 'qurtoba::AccountantReport/index', {
        'rows': (data or {}).get('data', []),
        'df': params['df'],
        'dt': params['dt'],
        'error': error,
    })


@login_required
def delayed_customers_view(request):
    """المتأخرات — Delayed / Overdue Customers page."""
    params = {k: v for k, v in request.GET.items() if v}
    data, error = _qurtoba_get('/transactions/api2/rest/', params)
    sellers, _ = _qurtoba_get('/customers/api2/sellers/')
    rows = data if isinstance(data, list) else (data or {}).get('results', [])
    return inertia_render(request, 'qurtoba::DelayedCustomers/index', {
        'rows': rows,
        'sellers': sellers if isinstance(sellers, list) else [],
        'filters': params,
        'error': error,
    })


# ---------------------------------------------------------------------------
# API proxy endpoints — called by React after page load (filter changes)
# ---------------------------------------------------------------------------

class CustomerDuesAPIView(APIView):
    def get(self, request):
        params = {k: v for k, v in request.GET.items() if v}
        data, error = _qurtoba_get('/transactions/api2/dues/', params)
        if error:
            return Response({'error': error}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data)


class SellerDuesAPIView(APIView):
    def get(self, request):
        data, error = _qurtoba_get('/transactions/api2/collector-dues/')
        if error:
            return Response({'error': error}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data)


class SellerTransactionsAPIView(APIView):
    def get(self, request):
        params = {k: v for k, v in request.GET.items() if v}
        # Choose endpoint based on params provided
        if 'from' in params and 'to' in params:
            path = '/transactions/api2/transactions_collector_from_to/'
        elif 'date' in params:
            path = '/transactions/api2/transactions_collector_date/'
        else:
            path = '/transactions/api2/transactions_collector/'
        data, error = _qurtoba_get(path, params)
        if error:
            return Response({'error': error}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data)


class SellerSettleAPIView(APIView):
    def post(self, request):
        seller_id = request.data.get('id') or request.query_params.get('id')
        value = request.data.get('value') or request.query_params.get('value')
        data, error = _qurtoba_post(
            '/transactions/api2/collector-dues/',
            params={'id': seller_id, 'value': value},
        )
        if error:
            return Response({'error': error}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data)


class AccountantReportAPIView(APIView):
    def get(self, request):
        params = {k: v for k, v in request.GET.items() if v}
        data, error = _qurtoba_get('/transactions/api2/reports/', params)
        if error:
            return Response({'error': error}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data)


class DelayedCustomersAPIView(APIView):
    def get(self, request):
        params = {k: v for k, v in request.GET.items() if v}
        data, error = _qurtoba_get('/transactions/api2/rest/', params)
        if error:
            return Response({'error': error}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data)


# ---------------------------------------------------------------------------
# Account-tasks (فورى / أمان) — operator todo page
#
# فورى/أمان transfers have no Cash-SYS automation, so an operator works them
# off a focused queue: pull the not-yet-done items, do the transfer by hand,
# then mark each completed/canceled. State lives in
# QurtobaRecord.account_task_state (Genie-local — never synced out).
# ---------------------------------------------------------------------------

_ACCOUNT_TASK_PAGE_SIZE = 50
_ACCOUNT_TASK_MAX_PAGE_SIZE = 100


@login_required
def account_tasks_view(request):
    """فورى / أمان — operator todo page shell. React pulls the data via the API."""
    return inertia_render(request, 'qurtoba::AccountTasks/index', {
        'account_types': QurtobaRecord.ACCOUNT_TASK_TYPES,
    })


class AccountTasksAPIView(APIView):
    """
    GET /qurtoba/api/account-tasks/
    Paginated, filtered list of فورى/أمان transfers for the operator todo page.

    Query params:
      state        : pending (default) | completed | canceled | all
      account_type : فورى | أمان | '' (both)
      search       : matches account_number / customer name / notes (+ exact value)
      page         : 1-based page number
      page_size    : default 50, capped at 100
    """

    def get(self, request):
        from django.core.paginator import Paginator, EmptyPage
        from django.db.models import Q

        state        = (request.GET.get('state') or 'pending').strip()
        account_type = (request.GET.get('account_type') or '').strip()
        search       = (request.GET.get('search') or '').strip()

        try:
            page = max(1, int(request.GET.get('page') or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(request.GET.get('page_size') or _ACCOUNT_TASK_PAGE_SIZE)
        except (TypeError, ValueError):
            page_size = _ACCOUNT_TASK_PAGE_SIZE
        page_size = max(1, min(page_size, _ACCOUNT_TASK_MAX_PAGE_SIZE))

        # Base queryset — only the manual-fulfilment types. The composite index
        # (account_task_state, type) keeps the default "pending" view fast.
        qs = (
            QurtobaRecord.objects
            .filter(type__in=QurtobaRecord.ACCOUNT_TASK_TYPES)
            .select_related('customer')
        )
        if account_type in QurtobaRecord.ACCOUNT_TASK_TYPES:
            qs = qs.filter(type=account_type)
        if state in ('pending', 'completed', 'canceled'):
            qs = qs.filter(account_task_state=state)
        # state == 'all' → no state filter

        if search:
            cond = (
                Q(account_number__icontains=search)
                | Q(customer__name__icontains=search)
                | Q(notes__icontains=search)
            )
            digits = ''.join(ch for ch in search if ch.isdigit())
            if digits:
                try:
                    cond |= Q(value=float(digits))
                except ValueError:
                    pass
            qs = qs.filter(cond)

        qs = qs.only(
            'id', 'type', 'account_number', 'value', 'notes',
            'date', 'time', 'account_task_state', 'customer',
        ).order_by('-date', '-time', '-id')

        paginator = Paginator(qs, page_size)
        try:
            page_obj = paginator.page(page)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages) if paginator.num_pages else None

        results = []
        if page_obj is not None:
            for r in page_obj.object_list:
                results.append({
                    'id': r.id,
                    'type': r.type,
                    'account_number': r.account_number or '',
                    'value': r.value or 0,
                    'notes': r.notes or '',
                    'customer_id': r.customer_id,
                    'customer_name': r.customer.name if r.customer_id else '—',
                    'state': r.account_task_state,
                    'date': r.date.isoformat() if r.date else None,
                    'time': r.time.strftime('%H:%M') if r.time else None,
                })

        return Response({
            'results': results,
            'page': page if page_obj is None else page_obj.number,
            'num_pages': paginator.num_pages,
            'total': paginator.count,
            'page_size': page_size,
            'has_next': bool(page_obj and page_obj.has_next()),
            'has_prev': bool(page_obj and page_obj.has_previous()),
        })


class AccountTaskActionView(APIView):
    """
    POST /qurtoba/api/account-tasks/<int:pk>/<action>/
    action ∈ complete | cancel — flips the operator fulfilment state.
    Uses .update() to skip QurtobaRecord.save() balance side-effects; nothing
    is pushed to Qurtoba/Cash-SYS.
    """

    _ACTION_STATE = {'complete': 'completed', 'cancel': 'canceled'}

    def post(self, request, pk, action):
        from django.utils import timezone

        new_state = self._ACTION_STATE.get(action)
        if new_state is None:
            return Response({'error': 'Unknown action'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user if request.user.is_authenticated else None
        updated = (
            QurtobaRecord.objects
            .filter(pk=pk, type__in=QurtobaRecord.ACCOUNT_TASK_TYPES)
            .update(
                account_task_state=new_state,
                account_task_done_at=timezone.now(),
                account_task_done_by=user,
            )
        )
        if not updated:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'status': True, 'id': pk, 'state': new_state})


# ---------------------------------------------------------------------------
# Cash-SYS webhook receiver
# ---------------------------------------------------------------------------

@method_decorator(csrf_exempt, name='dispatch')
class CashSysWebhookView(APIView):
    """
    POST /qurtoba/cash-sys/webhook/
    Receives order_progress / order_done / order_canceled events from Cash-SYS as a
    cash order is fulfilled (possibly across several partial transfers, with a
    possible recipient-number reroute). Dispatches immediately to Celery; responds
    200 fast. Only records with a linked partner get a WhatsApp notification.
    """
    permission_classes = []  # Auth via HMAC signature only

    def post(self, request):
        # ── Log every incoming webhook ──────────────────────────────────────
        event      = request.data.get('event', '?')
        order_id   = request.data.get('order_id', '?')
        ext_ref    = request.data.get('external_ref', '?')
        logger.info(
            '[CashSys Webhook] RECEIVED event=%s order_id=%s external_ref=%s body_bytes=%d',
            event, order_id, ext_ref, len(request.body),
        )

        # ── HMAC signature check ─────────────────────────────────────────────
        secret = getattr(settings, 'CASH_SYS_WEBHOOK_SECRET', '')
        if secret:
            sig      = request.headers.get('X-Cash-Signature', '')
            expected = 'sha256=' + hmac.new(
                secret.encode('utf-8'), request.body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, sig):
                logger.warning(
                    '[CashSys Webhook] INVALID SIGNATURE order_id=%s ext_ref=%s '
                    'received=%s expected=%s',
                    order_id, ext_ref, sig[:16] + '...', expected[:16] + '...',
                )
                return Response({'detail': 'Invalid signature'}, status=status.HTTP_401_UNAUTHORIZED)
            logger.info('[CashSys Webhook] signature OK order_id=%s', order_id)
        else:
            logger.warning('[CashSys Webhook] CASH_SYS_WEBHOOK_SECRET not set — skipping signature check')

        # ── Route event ──────────────────────────────────────────────────────
        # A single order is now a multi-step chain: order_progress (partial
        # transfer), order_done (chain settled), order_canceled (a part canceled,
        # reroute:true ⇒ recipient number over its limit → reissue to a new number).
        data = request.data
        evt = data.get('event')
        from qurtoba.tasks import (
            handle_cash_sys_order_progress,
            handle_cash_sys_order_done,
            handle_cash_sys_order_canceled,
        )
        handler = {
            'order_progress': handle_cash_sys_order_progress,
            'order_done':     handle_cash_sys_order_done,
            'order_canceled': handle_cash_sys_order_canceled,
        }.get(evt)

        if handler is None:
            logger.info('[CashSys Webhook] ignored event=%s', event)
            return Response({'status': 'ignored'}, status=status.HTTP_200_OK)

        task = handler.delay(dict(data))
        logger.info(
            '[CashSys Webhook] dispatched event=%s task_id=%s order_id=%s ext_ref=%s',
            evt, task.id, order_id, ext_ref,
        )
        return Response({'status': 'ok'}, status=status.HTTP_200_OK)
