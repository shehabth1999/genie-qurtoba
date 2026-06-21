from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('qurtoba', '0005_alter_qurtobasyncproblem_payload'),
    ]

    operations = [
        migrations.AddField(
            model_name='qurtobarecord',
            name='cash_sys_sim_code',
            field=models.CharField(blank=True, max_length=20, null=True, verbose_name='SIM Code'),
        ),
        migrations.AddField(
            model_name='qurtobarecord',
            name='cash_sys_operator',
            field=models.CharField(blank=True, max_length=100, null=True, verbose_name='Operator'),
        ),
    ]
