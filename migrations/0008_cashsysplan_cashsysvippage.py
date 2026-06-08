from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('qurtoba', '0007_qurtobarecord_cash_sys_device_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='CashSysVipPage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('cash_sys_id', models.IntegerField(unique=True, verbose_name='Cash-SYS ID')),
                ('name', models.CharField(max_length=100)),
                ('key', models.CharField(max_length=50)),
                ('price', models.DecimalField(decimal_places=2, max_digits=8)),
                ('synced_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Cash-SYS VIP Page',
                'verbose_name_plural': 'Cash-SYS VIP Pages',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='CashSysPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('cash_sys_id', models.IntegerField(unique=True, verbose_name='Cash-SYS ID')),
                ('name', models.CharField(max_length=255)),
                ('type', models.CharField(max_length=20)),
                ('price', models.DecimalField(decimal_places=2, max_digits=10)),
                ('device_limit', models.PositiveIntegerField(default=0)),
                ('sim_limit', models.PositiveIntegerField(default=0)),
                ('account_limit', models.PositiveIntegerField(default=0)),
                ('vip_pages', models.JSONField(default=list)),
                ('is_active', models.BooleanField(default=True)),
                ('synced_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Cash-SYS Plan',
                'verbose_name_plural': 'Cash-SYS Plans',
                'ordering': ['price'],
            },
        ),
    ]
