"""Unit tests for the WhatsApp Web customer groups (owner decision 2026-09-23). No database needed.

Run:  python manage.py test qurtoba.tests_groups -v 2
"""
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase


def _group_conv(**kw):
    return NS(id='g1', pk='g1', type='wa_web', is_group=True, social_partner=NS(pk=7, qurtoba_customer_id=None), **kw)


class GroupDetectionTests(SimpleTestCase):
    def test_only_a_wa_web_group_is_a_customer_group(self):
        from qurtoba.groups import is_group
        self.assertTrue(is_group(_group_conv()))
        self.assertFalse(is_group(NS(type='wa_web', is_group=False)))
        self.assertFalse(is_group(NS(type='whatsapp', is_group=False)))
        self.assertFalse(is_group(NS(type='internal', is_group=True)))     # a staff team chat is not ours
        self.assertFalse(is_group(None))

    def test_the_chat_partner_of_a_group_is_the_group(self):
        from qurtoba.groups import chat_partner
        conv = _group_conv()
        speaker = NS(pk=99)
        self.assertIs(chat_partner(conv, speaker), conv.social_partner)
        one_to_one = NS(type='whatsapp', is_group=False, social_partner=NS(pk=5))
        self.assertIs(chat_partner(one_to_one, speaker), speaker)
        self.assertIs(chat_partner(one_to_one, None), one_to_one.social_partner)

    def test_staff_lines_are_excluded_only_in_a_group(self):
        from qurtoba.groups import exclude_staff
        qs = MagicMock()
        self.assertIs(exclude_staff(qs, NS(type='whatsapp', is_group=False)), qs)
        qs.exclude.assert_not_called()
        exclude_staff(qs, _group_conv())
        qs.exclude.assert_called_once()

    def test_staff_is_the_employee_tick_or_its_parent_or_the_connected_number(self):
        from qurtoba import groups
        self.assertTrue(groups.is_staff(NS(employee=True)))
        self.assertTrue(groups.is_staff(NS(employee=False, parent_id=NS(employee=True))))
        with patch.object(groups, 'is_own_number', return_value=False):
            self.assertFalse(groups.is_staff(NS(employee=False, parent_id=None)))
        with patch.object(groups, 'is_own_number', return_value=True):
            self.assertTrue(groups.is_staff(NS(employee=False, parent_id=None)))
        self.assertFalse(groups.is_staff(None))

    def test_egyptian_mobiles_compare_on_their_last_ten_digits(self):
        from qurtoba.groups import _local10
        self.assertEqual(_local10('01012345678'), _local10('201012345678'))
        self.assertEqual(_local10('+20 101 234 5678'), '1012345678')
        self.assertEqual(_local10('123'), '')


class SwitchesFollowTheOfficeNumberTests(SimpleTestCase):
    def _acc(self, label):
        return NS(pk=1, _meta=NS(label_lower=label))

    def test_a_wa_web_account_obeys_its_cloud_twin(self):
        from qurtoba import switches
        twin = NS(pk=3)
        with patch('qurtoba.groups.twin_cloud_account', return_value=twin):
            self.assertIs(switches.switch_account(self._acc('wa_web.wawebaccount')), twin)

    def test_no_twin_fails_closed(self):
        from qurtoba import switches
        with patch('qurtoba.groups.twin_cloud_account', return_value=None):
            self.assertIs(switches.switch_account(self._acc('wa_web.wawebaccount')), switches._NO_TWIN)
            conv = NS(social_account=self._acc('wa_web.wawebaccount'))
            self.assertEqual(switches.account_flags(conv), {'ai_enabled': False, 'off_hours': False})

    def test_other_channels_have_nothing_to_switch(self):
        from qurtoba import switches
        self.assertIsNone(switches.switch_account(self._acc('messenger.messengerpage')))


class WaWebGateTests(SimpleTestCase):
    """The tenant gate on WaWebService.send_omnichannel (every AI send on WhatsApp Web)."""

    def _install(self):
        from qurtoba import ai_guard
        calls = []

        class FakeService:
            def send_omnichannel(self, partner, content, *, message_type='text', conversation=None,
                                 system_partner=None, **kwargs):
                calls.append({'content': content, 'kwargs': kwargs})
                return {'success': True, 'message_id': 'm1', 'social_id': 'wa_web:1:X', 'channel': 'wa_web'}

        module = NS(WaWebService=FakeService)
        with patch.dict('sys.modules', {'modules.wa_web.services.send_service': module}), \
             patch('django.apps.apps.is_installed', return_value=True):
            self.assertTrue(ai_guard.install_wa_web())
        return FakeService, calls

    def test_the_silent_marker_is_never_sent(self):
        from qurtoba.groups import SILENT_SENTINEL
        svc, calls = self._install()
        res = svc().send_omnichannel(NS(), {'text': SILENT_SENTINEL}, conversation=_group_conv(),
                                     system_partner=NS(ai_agent=True), paced=True)
        self.assertTrue(res['success'])
        self.assertTrue(res.get('silent'))
        self.assertEqual(calls, [])

    def test_a_blocked_text_never_reaches_the_gateway(self):
        from qurtoba import ai_guard
        svc, calls = self._install()
        with patch.object(ai_guard, 'decide', return_value={'action': 'block', 'reason': 'internal_text'}):
            res = svc().send_omnichannel(NS(), {'text': 'error_type: x'}, conversation=_group_conv(),
                                         system_partner=NS(ai_agent=True))
        self.assertFalse(res['success'])
        self.assertTrue(res['blocked'])
        self.assertEqual(calls, [])

    def test_a_forwarded_text_goes_out_quoted_on_the_customer_line(self):
        from qurtoba import ai_guard
        svc, calls = self._install()
        inbound = NS(id='in-1', social_id='wa_web:1:ABC')
        with patch.object(ai_guard, 'decide', return_value={'action': 'forward', 'reason': 'unquoted_agent_text',
                                                            'forward_to': inbound}), \
             patch.object(ai_guard, 'mark_reply_delivered'):
            res = svc().send_omnichannel(NS(), {'text': 'وعليكم السلام'}, conversation=_group_conv(),
                                         system_partner=NS(ai_agent=True), paced=True)
        self.assertTrue(res['success'])
        self.assertEqual(calls[0]['kwargs']['reply_to_social_id'], 'wa_web:1:ABC')
        self.assertTrue(calls[0]['kwargs']['paced'])


class RuntimePatchTests(SimpleTestCase):
    def _with_fake(self, module, attr, fake, patcher):
        real = getattr(module, attr)
        setattr(module, attr, fake)
        try:
            patcher()
            return getattr(module, attr)
        finally:
            setattr(module, attr, real)

    def test_an_empty_wa_web_run_becomes_the_silent_marker(self):
        import modules.aistudio.services as svc
        from qurtoba import runtime_patches
        from qurtoba.groups import SILENT_SENTINEL
        wrapped = self._with_fake(svc, 'execute_workflow_sync',
                                  lambda wf, data, **kw: NS(success=True, status='completed', output=''),
                                  runtime_patches._silent_wa_web_turns)
        self.assertEqual(wrapped(1, {}, trigger_source='wa_web').output, SILENT_SENTINEL)
        self.assertEqual(wrapped(1, {}, trigger_source='whatsapp').output, '')

    def test_a_failed_wa_web_run_is_left_alone(self):
        import modules.aistudio.services as svc
        from qurtoba import runtime_patches
        wrapped = self._with_fake(svc, 'execute_workflow_sync',
                                  lambda wf, data, **kw: NS(success=False, status='failed', output=''),
                                  runtime_patches._silent_wa_web_turns)
        self.assertEqual(wrapped(1, {}, trigger_source='wa_web').output, '')

    def test_a_group_run_keeps_the_group_as_its_partner(self):
        import modules.aistudio_wa_web.group_context as gc
        from qurtoba import runtime_patches
        wrapped = self._with_fake(gc, 'speaker_of', lambda messages: NS(pk=42), runtime_patches._group_run_is_the_group)
        self.assertIsNone(wrapped([NS(direction='inbound')]))
        self.assertTrue(getattr(gc.speaker_of, '_qurtoba_group_partner', False))   # installed live at startup

    def test_private_chats_are_dropped_on_the_shared_number(self):
        from qurtoba import runtime_patches

        class Ingest:
            def __init__(self, account):
                self.account = account

            def message(self, msg, **kw):
                return 'stored'

        module = NS(Ingest=Ingest)
        with patch.dict('sys.modules', {'modules.wa_web.services.ingest': module}):
            runtime_patches._wa_web_groups_only()
        with patch.object(runtime_patches, '_groups_only', return_value=True):
            self.assertIsNone(Ingest(NS(pk=1)).message({'is_group': False, 'hex': 'A'}))
            self.assertEqual(Ingest(NS(pk=1)).message({'is_group': True, 'hex': 'B'}), 'stored')
        with patch.object(runtime_patches, '_groups_only', return_value=False):
            self.assertEqual(Ingest(NS(pk=1)).message({'is_group': False, 'hex': 'C'}), 'stored')

    def test_an_unreadable_group_message_does_not_switch_the_ai_off(self):
        from qurtoba import runtime_patches
        from qurtoba.groups import suppress_escalation
        escalated = []

        class Conversation:
            pk = 'c1'

            def escalate_to_human(self, *a, **kw):
                escalated.append(True)
                return True

        module = NS(Conversation=Conversation)
        with patch.dict('sys.modules', {'modules.chat.models': module}):
            runtime_patches._no_group_escalation_on_unsupported()
        suppress_escalation(True)
        try:
            self.assertFalse(Conversation().escalate_to_human())
        finally:
            suppress_escalation(False)
        self.assertEqual(escalated, [])
        self.assertTrue(Conversation().escalate_to_human())
        self.assertEqual(escalated, [True])


class NoticesGoWhereTheTransferWasAskedTests(SimpleTestCase):
    def test_the_origin_group_is_the_notice_chat(self):
        from qurtoba import tasks
        group = NS(pk='g1', type='wa_web', social_account=NS(pk=9), social_partner=NS(pk=7))
        origin = NS(id='m1', social_id='wa_web:9:ABC', conversation=group)
        record = NS(pk=5, partner_id=7, partner=NS(pk=7, conversations=MagicMock()),
                    origin_message_id='m1', origin_message=origin, account_number='01012345678')
        with patch('qurtoba.extensions._get_system_partner', return_value=NS(pk=1)), \
             patch('modules.chat.services.omnichannel_send_service.OmnichannelSendService', MagicMock()):
            ctx = tasks._notify_context(record)
        self.assertIs(ctx['conv'], group)
        self.assertEqual(ctx['reply_wamid'], 'wa_web:9:ABC')
        record.partner.conversations.filter.assert_not_called()


class GroupPromptTests(SimpleTestCase):
    def test_the_group_prompt_uses_the_channel_neutral_reply_tool(self):
        from qurtoba.management.commands import qurtoba_workflow_v2 as b
        text = b._group_prompt(b._thinker_prompt(), b._GROUP_PROMPT_PATH)
        self.assertNotIn('whatsapp_reply_to_message', text)
        self.assertIn('qurtoba_reply_to_message', text)
        self.assertIn("In the customer's WhatsApp GROUP", text)
        self.assertLess(text.index('</context>'), text.index("In the customer's WhatsApp GROUP"))

    def test_a_mobile_that_lost_its_zero_is_a_broken_number_not_an_amount(self):
        from qurtoba.tools.planning import _classify_message
        cls = _classify_message('ارجو تسليم \n101877357\nحلا\nالقيمة 20690 ج مصري')
        self.assertEqual(cls['phones'], [])
        self.assertEqual(cls['amounts'], [20690.0])
        self.assertIn('broken_phone', [i.get('reason') for i in cls['ignored']])


class ImageRequestTests(SimpleTestCase):
    """«فين الصورة» → «لحظة» + the staff are told, never the model (owner decision 2026-09-26)."""

    def test_short_image_questions_are_caught(self):
        from qurtoba.automation.image_request import is_image_request
        for text in ('الصورة', 'الصوره', 'فين الصورة؟', 'ابعت صورة التحويل', 'الاسكرين لو سمحت',
                     'فين الايصال', 'سكرين', 'screenshot'):
            self.assertTrue(is_image_request(text), text)

    def test_orders_questions_and_long_text_are_not(self):
        from qurtoba.automation.image_request import is_image_request
        for text in ('01012345678\n500', 'صورة 500', 'وصل؟', 'تم؟', 'حسابي كام',
                     'الصورة اللي بعتها امبارح كانت مش واضحة خالص يا باشا', 'تصوير'):
            self.assertFalse(is_image_request(text), text)


class HighValueWordingTests(SimpleTestCase):
    """«المبلغ 150 ألف مظبوط ؟؟ / برجاء التاكيد ل تنفيذ العملية» (owner wording 2026-09-26)."""

    def test_amount_words(self):
        from qurtoba.automation.replies import amount_words
        self.assertEqual(amount_words(45000), '45,000')
        self.assertEqual(amount_words(99999), '99,999')
        self.assertEqual(amount_words(100000), '100 ألف')
        self.assertEqual(amount_words(150000), '150 ألف')
        self.assertEqual(amount_words('150000.0'), '150 ألف')
        self.assertEqual(amount_words(150500), '150 ألف و500')
        self.assertEqual(amount_words(1000000), '1 مليون')
        self.assertEqual(amount_words(2500000), '2 مليون و500 ألف')

    def test_line_and_detection(self):
        from qurtoba.automation.replies import high_value, is_high_value_question
        line = high_value(150000)
        self.assertEqual(line, 'المبلغ 150 ألف مظبوط ؟؟\n\nبرجاء التاكيد ل تنفيذ العملية')
        self.assertTrue(is_high_value_question(line))
        self.assertTrue(is_high_value_question('مبلغ كبير — محتاج منك كلمة «تأكيد» على الرسالة دي قبل ما ننفّذه'))
        self.assertFalse(is_high_value_question('المبلغ لـ 01012345678؟'))

    def test_mazboot_is_a_yes(self):
        from qurtoba.automation import lexicon as L
        self.assertTrue(L.is_bare_yes('مظبوط'))


class SplitNumberTests(SimpleTestCase):
    """«رقم المستلم: 2095565 0112» (right-to-left text) is 01122095565 — shown back to the customer."""

    def test_reversed_and_spaced_groups_rebuild_the_number(self):
        from qurtoba.tools.planning import _classify_message
        c = _classify_message('رقم العملية: #110305MS\nرقم المستلم: 2095565 0112\nالقيمة: 51,501')
        self.assertEqual((c['phones'], c['amounts']), (['01122095565'], [51501.0]))
        self.assertEqual(c['reassembled'][0]['phone'], '01122095565')
        c = _classify_message('0112 209 5565\n500')
        self.assertEqual((c['phones'], c['amounts']), (['01122095565'], [500.0]))
        self.assertTrue(c['reassembled'])

    def test_plain_numbers_and_amount_lines_are_untouched(self):
        from qurtoba.tools.planning import _classify_message
        c = _classify_message('01080755798\n500')
        self.assertEqual((c['phones'], c['amounts'], c['reassembled']), (['01080755798'], [500.0], []))
        c = _classify_message('150 2000\n01012345678')
        self.assertEqual((c['phones'], c['amounts']), (['01012345678'], [150.0, 2000.0]))


class GroupStatementTests(SimpleTestCase):
    """The nightly statement goes to the groups (owner decision 2026-09-26), same wording as template #2."""

    def test_text_mirrors_the_private_template(self):
        import datetime
        from qurtoba import tasks
        customer = NS(name='حسين بركات (696)', balance=12500.0, refresh_from_db=lambda **k: None)
        conv = NS(name='تيست', social_partner=NS(qurtoba_customer=customer, pk=1))
        text = tasks.group_statement_text(conv, datetime.date(2026, 9, 26))
        self.assertTrue(text.startswith('*كشف نهاية اليوم*'))
        self.assertIn('*العميل :* حسين بركات (696)', text)
        self.assertNotIn('إجمالي تحويلات', text)       # owner 2026-09-26: balance only
        self.assertIn('( عليك 12,500 جنيه )', text)
        self.assertTrue(text.endswith('_مكتب قرطبة — كشف تلقائي فى نهاية اليوم_'))

    def test_private_closed_line(self):
        from qurtoba.automation.replies import PRIVATE_CLOSED
        self.assertIn('الجروبات بس', PRIVATE_CLOSED)
