# -*- coding: utf-8 -*-
from django.urls import path
from .views import (
    # Sync endpoints (Qurtoba → Genie push)
    QurtobaCustomerListView,
    QurtobaCustomerDetailView,
    QurtobaRecordListView,
    # Inertia page views
    customer_dues_view,
    seller_dues_view,
    accountant_report_view,
    delayed_customers_view,
    # API proxy endpoints (React → Genie → Qurtoba)
    CustomerDuesAPIView,
    SellerDuesAPIView,
    SellerTransactionsAPIView,
    SellerSettleAPIView,
    AccountantReportAPIView,
    DelayedCustomersAPIView,
    # Cash-SYS webhook
    CashSysWebhookView,
)

urlpatterns = [
    # ------------------------------------------------------------------
    # Sync endpoints — mirrors Qurtoba's URL structure exactly
    # ------------------------------------------------------------------
    path('customers/api2/customers/', QurtobaCustomerListView.as_view()),
    path('customers/api2/customers/<int:pk>/', QurtobaCustomerDetailView.as_view()),
    path('transactions/api2/record/', QurtobaRecordListView.as_view()),

    # ------------------------------------------------------------------
    # Custom Inertia pages (Genie ERP UI)
    # ------------------------------------------------------------------
    path('qurtoba/customer-dues/', customer_dues_view, name='qurtoba_customer_dues'),
    path('qurtoba/seller-dues/', seller_dues_view, name='qurtoba_seller_dues'),
    path('qurtoba/accountant-report/', accountant_report_view, name='qurtoba_accountant_report'),
    path('qurtoba/delayed/', delayed_customers_view, name='qurtoba_delayed'),

    # ------------------------------------------------------------------
    # API proxy endpoints — called by React after page load
    # ------------------------------------------------------------------
    path('qurtoba/api/customer-dues/', CustomerDuesAPIView.as_view()),
    path('qurtoba/api/seller-dues/', SellerDuesAPIView.as_view()),
    path('qurtoba/api/seller-transactions/', SellerTransactionsAPIView.as_view()),
    path('qurtoba/api/seller-settle/', SellerSettleAPIView.as_view()),
    path('qurtoba/api/accountant-report/', AccountantReportAPIView.as_view()),
    path('qurtoba/api/delayed/', DelayedCustomersAPIView.as_view()),

    # Cash-SYS fires this when an integration order is done
    path('qurtoba/cash-sys/webhook/', CashSysWebhookView.as_view(), name='qurtoba_cash_sys_webhook'),
]
