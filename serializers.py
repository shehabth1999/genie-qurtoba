# -*- coding: utf-8 -*-
from rest_framework import serializers
from .models import QurtobaCustomer, QurtobaRecord


class QurtobaCustomerSerializer(serializers.ModelSerializer):
    # camelCase → snake_case mappings (exact field names from Qurtoba CustomerInfo serializer)
    surName   = serializers.CharField(source='sur_name',  required=False, allow_null=True, allow_blank=True)
    shopName  = serializers.CharField(source='shop_name', required=False, allow_null=True, allow_blank=True)
    # shopKind: CharField with string choices in Qurtoba, but default=1 (int bug) so accept both
    shopKind  = serializers.CharField(source='shop_kind', required=False, allow_null=True, allow_blank=True)
    deviceNo  = serializers.IntegerField(source='device_no', required=False, allow_null=True)
    phoneNo   = serializers.CharField(source='phone_no',  required=False, allow_null=True, allow_blank=True)
    # FK fields: Qurtoba returns integer IDs (ForeignKey with __all__ serializer → PK)
    seller    = serializers.IntegerField(source='seller_qurtoba_id',    required=False, allow_null=True)
    assistant = serializers.IntegerField(source='assistant_qurtoba_id', required=False, allow_null=True)
    areas     = serializers.IntegerField(source='areas_qurtoba_id',     required=False, allow_null=True)

    class Meta:
        model = QurtobaCustomer
        fields = [
            'name',
            'sur_name',  'surName',
            'shop_name', 'shopName',
            'shop_kind', 'shopKind',
            'device_no', 'deviceNo',
            'phone_no',  'phoneNo',
            'address', 'area',
            'accounts', 'accounts_data',
            'grade',
            'seller_qurtoba_id',    'seller',
            'assistant_qurtoba_id', 'assistant',
            'areas_qurtoba_id',     'areas',
            'date', 'time',
            'notes', 'notes_plus',
        ]
        extra_kwargs = {f: {'required': False} for f in [
            'name', 'sur_name', 'shop_name', 'shop_kind', 'device_no',
            'phone_no', 'address', 'area', 'accounts', 'accounts_data',
            'grade', 'seller_qurtoba_id', 'assistant_qurtoba_id',
            'areas_qurtoba_id', 'date', 'time', 'notes', 'notes_plus',
        ]}


class QurtobaRecordSerializer(serializers.ModelSerializer):
    # camelCase → snake_case mappings (exact field names from Qurtoba Record serializer)
    accountNumber = serializers.CharField(source='account_number', required=False, allow_null=True, allow_blank=True)
    isDone        = serializers.BooleanField(source='is_done',   required=False)
    isDown        = serializers.BooleanField(source='is_down',   required=False)
    isSeller      = serializers.BooleanField(source='is_seller', required=False)
    seller        = serializers.IntegerField(source='seller_qurtoba_id',        required=False, allow_null=True)
    customerData  = serializers.IntegerField(source='customer_data_qurtoba_id', required=False, allow_null=True)
    accountant    = serializers.IntegerField(source='accountant_qurtoba_id',    required=False, allow_null=True)
    # datetime clashes with Python's datetime module — remapped to datetime_field
    datetime      = serializers.DateTimeField(source='datetime_field', required=False, allow_null=True)

    class Meta:
        model = QurtobaRecord
        fields = [
            'type',
            'account_number', 'accountNumber',
            'value', 'rest',
            'is_done',   'isDone',
            'is_down',   'isDown',
            'is_seller', 'isSeller',
            'seller_qurtoba_id',        'seller',
            'customer_data_qurtoba_id', 'customerData',
            'accountant_qurtoba_id',    'accountant',
            'date', 'time',
            'datetime_field', 'datetime',
            'date_receive', 'time_receive', 'datetime_receive',
            'notes',
        ]
        extra_kwargs = {f: {'required': False} for f in [
            'type', 'account_number', 'value', 'rest',
            'is_done', 'is_down', 'is_seller',
            'seller_qurtoba_id', 'customer_data_qurtoba_id', 'accountant_qurtoba_id',
            'date', 'time', 'datetime_field',
            'date_receive', 'time_receive', 'datetime_receive',
            'notes',
        ]}
