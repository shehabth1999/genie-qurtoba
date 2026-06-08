from django.contrib import admin
from .models import QurtobaCustomer, QurtobaRecord


@admin.register(QurtobaCustomer)
class QurtobaCustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone_no', 'area', 'shop_kind', 'grade', 'device_no', 'qurtoba_id')
    search_fields = ('name', 'phone_no', 'area')
    list_filter = ('shop_kind', 'area')


@admin.register(QurtobaRecord)
class QurtobaRecordAdmin(admin.ModelAdmin):
    list_display = ('type', 'value', 'rest', 'is_done', 'customer_data_qurtoba_id', 'date', 'created_at')
    search_fields = ('type', 'account_number')
    list_filter = ('type', 'is_done', 'is_seller')
