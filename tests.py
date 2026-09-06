"""Unit tests for the deterministic transaction planner (no database needed).

Run:  python manage.py test qurtoba.tests -v 2
"""
from django.test import SimpleTestCase

from qurtoba.tools.planning import (
    _build_events, _classify_message, _is_glued_name_label, _lead_is_amount_words,
    _pair_events, qurtoba_plan_transactions,
)


def _plan(messages):
    fn = getattr(qurtoba_plan_transactions, '__wrapped__', qurtoba_plan_transactions)
    return fn(None, messages=messages)


class AmountLabelWordsTests(SimpleTestCase):
    """A word that NAMES the amount, glued to the number, is an amount — not a name+serial."""

    def test_label_words_glued_to_the_number_are_amounts(self):
        cases = {
            '01023551947\n*مبلغ15.100مصري*': 15100,       # 2026-09-05 live miss
            '01005301545\nالقيمه11.088 جنيه مصري': 11088,  # live corpus
            '01005301545\nبمبلغ5000': 5000,
            '01005301545\nالمبلغ:20000': 20000,
            '01005301545\nحواله24000': 24000,
            '01005301545\nوالقيمة7.500': 7500,
            '01005301545\nللمبلغ 3000': 3000,
            '01005301545\nالمطلوب6000': 6000,
        }
        for text, value in cases.items():
            cls = _classify_message(text)
            self.assertEqual(cls['phones'], ['01005301545'] if '01005301545' in text else ['01023551947'], text)
            self.assertEqual(cls['amounts'], [value], text)
            self.assertEqual(cls['ignored'], [], text)

    def test_type_wallet_currency_and_multiplier_leads_still_read(self):
        for text, value in {'فودافون2.772': 2772, 'كاش12080': 12080, '30الف': 30000,
                            '13300جنيه': 13300, 'بالكاش 4000': 4000}.items():
            self.assertEqual(_classify_message(text)['amounts'], [value], text)

    def test_names_with_trailing_digits_stay_labels(self):
        for tok in ('عبدالله12', 'طه13.40', 'بلال12', 'سمية12', 'SI5413', 'ك29', 'الطه13'):
            self.assertTrue(_is_glued_name_label(tok), tok)
            cls = _classify_message(tok)
            self.assertEqual(cls['amounts'], [], tok)
            self.assertEqual([i['reason'] for i in cls['ignored']], ['name_label'], tok)

    def test_lead_word_matching_is_anchored_at_the_start(self):
        for lead in ('مبلغ', 'بمبلغ', 'والقيمه', 'للمبلغ', 'كاش', 'بالكاش', 'فودافون', 'الف'):
            self.assertTrue(_lead_is_amount_words(lead), lead)
        for lead in ('بلال', 'سميه', 'طه', 'عبدالله', 'ك', 'ال', 'مبلغطه'):
            self.assertFalse(_lead_is_amount_words(lead), lead)


class CommentVersusValueTests(SimpleTestCase):
    """Every digit-bearing piece the classifier drops is reported with a reason."""

    def test_name_tally_next_to_a_real_pair(self):
        cls = _classify_message('٠01112140364\n10000\nعاصم كاش محمد سعد الرباط\nطه13.40')
        self.assertEqual(cls['phones'], ['01112140364'])
        self.assertEqual(cls['amounts'], [10000])
        self.assertEqual(cls['ignored'], [{'text': 'طه13.40', 'reason': 'name_label'}])

    def test_reference_lines_bracketed_fees_fractions_and_broken_phones(self):
        cls = _classify_message('رقم العملية: SI5413\nالنوع: تحويل فودافون\nالقيمة: 35092 ج.م\nالرقم: 01285154871')
        self.assertEqual(cls['phones'], ['01285154871'])
        self.assertEqual(cls['amounts'], [35092])
        self.assertEqual(cls['ignored'], [{'text': 'SI5413', 'reason': 'name_label'}])

        cls = _classify_message('رقم العملية 5413\n01285154871\n5000')
        self.assertEqual(cls['amounts'], [5000])
        self.assertEqual(cls['ignored'], [{'text': 'رقم العملية 5413', 'reason': 'reference_note'}])

        cls = _classify_message('01285154871 (124)\n5000\nلو هيخصم 15 اخصمها')
        self.assertEqual(cls['amounts'], [5000])
        self.assertEqual([(i['text'], i['reason']) for i in cls['ignored']],
                         [('(124)', 'bracketed'), ('لو هيخصم 15 اخصمها', 'fee_note')])

        cls = _classify_message('عمار 13.75')
        self.assertEqual(cls['amounts'], [])
        self.assertEqual([i['reason'] for i in cls['ignored']], ['fraction'])

        cls = _classify_message('0100600100\n700')
        self.assertEqual(cls['phones'], [])
        self.assertEqual(cls['amounts'], [700])
        self.assertEqual([i['reason'] for i in cls['ignored']], ['broken_phone'])

    def test_digitless_noise_is_not_reported(self):
        cls = _classify_message('01285154871\nاحمد محمد\n5000 جنيه')
        self.assertEqual(cls['ignored'], [])


class BurstPairingTests(SimpleTestCase):

    BURST = [
        {'text': '01009021516\nزينب وحيد\nفودافون \n44880', 'message_id': 'm1'},
        {'text': 'رقم المستلم: 01090878331\nالقيمة: 15,014', 'message_id': 'm2'},
        {'text': '01004680539\n*مبلغ 16.130مصري*', 'message_id': 'm3'},
        {'text': '01023551947\n*مبلغ15.100مصري*', 'message_id': 'm4'},
        {'text': '01127108611\n11000ج.م', 'message_id': 'm5'},
        {'text': '0 11 27969725', 'message_id': 'm6'},
        {'text': '٢٠٢٠٠', 'message_id': 'm7'},
    ]

    def test_live_burst_2026_09_05_pairs_every_number_with_its_own_amount(self):
        pairs, _mids, orphans, ambiguous, list_pattern = _pair_events(_build_events(self.BURST, {}))
        self.assertEqual([(p['account_number'], p['value'], p['confidence']) for p in pairs], [
            ('01009021516', 44880.0, 'high'), ('01090878331', 15014.0, 'high'),
            ('01004680539', 16130.0, 'high'), ('01023551947', 15100.0, 'high'),
            ('01127108611', 11000.0, 'high'), ('01127969725', 20200.0, 'high'),
        ])
        self.assertEqual(orphans, [])
        self.assertEqual(ambiguous, [])
        self.assertFalse(list_pattern)

    def test_tool_reports_ignored_pieces_and_hints_on_an_orphan_phone(self):
        out = _plan([{'text': '01023551947\nعبدالله15100', 'message_id': 'a'},
                     {'text': '01004680539\n5000\nطه13.40', 'message_id': 'b'}])
        self.assertTrue(out['success'])
        self.assertEqual([(p['account_number'], p['value']) for p in out['pairs']], [('01004680539', 5000.0)])
        self.assertEqual(out['orphans'], [{'kind': 'phone', 'value': '01023551947', 'message_id': 'a'}])
        self.assertEqual(out['ignored'], [
            {'message_id': 'a', 'text': 'عبدالله15100', 'reason': 'name_label'},
            {'message_id': 'b', 'text': 'طه13.40', 'reason': 'name_label'},
        ])
        self.assertIn('عبدالله15100', out['note'])
        self.assertIn('المبلغ لـ', out['note'])

    def test_tool_note_is_clean_when_nothing_is_ambiguous(self):
        out = _plan(self.BURST)
        self.assertEqual(out['orphans'], [])
        self.assertEqual(out['ignored'], [])
        self.assertEqual(out['note'], 'كل رقم متطابق مع مبلغه.')


# ═══════════════════════════ workflow v2 automation ═══════════════════════════

from qurtoba.automation import lexicon as L  # noqa: E402
from qurtoba.automation.arabic_numbers import parse_arabic_amount  # noqa: E402
from qurtoba.automation.router import batch_ids_from_input  # noqa: E402
from qurtoba.automation.transfers import render_ai_summary, _is_noise_line  # noqa: E402
from qurtoba.automation.transfers import decide, resolve_noncash, _multi_number  # noqa: E402


class LexiconTests(SimpleTestCase):

    def test_yes_no_answers(self):
        for t in ('أيوة', 'ايوه كرر', 'تمام', 'تأكيد', 'اه', 'نعم يا باشا', 'ok', 'ماشي كده', 'اها كرر الكل', 'ايوه اعملها كلها'):
            self.assertTrue(L.is_yes(t), t)
            self.assertFalse(L.is_no(t), t)
        for t in ('لأ', 'لا خلاص', 'بلاش', 'no'):
            self.assertTrue(L.is_no(t), t)
            self.assertFalse(L.is_yes(t), t)
        for t in ('100 ج', 'ايوة بس الرقم التاني', 'عايز اعرف'):
            self.assertFalse(L.is_yes(t), t)

    def test_norm_unifies_spellings(self):
        self.assertEqual(L.norm('إلغاء التحويلة'), 'الغاء التحويله')
        self.assertEqual(L.norm('٥٠٠ جنيه'), '500 جنيه')


class RouterTests(SimpleTestCase):

    def test_batch_ids_from_channel_markers(self):
        data = {'message': '[message_id: 5e884d7d-4073-4b1d-91aa-e042809f48ce]\n01009021516',
                'content': [{'role': 'user', 'content': [{'type': 'text', 'text': '[message_id: f88bd2f1-422b-4117-8d23-ab31a2caa067]\n15,014'}]}]}
        self.assertEqual(batch_ids_from_input(data), ['5e884d7d-4073-4b1d-91aa-e042809f48ce', 'f88bd2f1-422b-4117-8d23-ab31a2caa067'])


class ArabicNumberTests(SimpleTestCase):

    def test_spelled_amounts(self):
        cases = {'خمسين الف': 50000, 'خمسمائة': 500, 'الفين جنيه': 2000, 'الف و خمسميه': 1500, '27 الف': 27000,
                 'مية و خمسين': 150, 'تلاتة الاف': 3000, 'ألف': 1000, '٢٧٠٠٠ ألف': 27000, 'خمسة و عشرين الف': 25000,
                 'مليون': 1000000, 'اتنين مليون': 2000000}
        for text, value in cases.items():
            self.assertEqual(parse_arabic_amount(text), value, text)

    def test_names_and_unknown_words_are_refused(self):
        for text in ('سمية', 'حلمية', 'تلاته الاف و نص', 'خمسين الف تقريبا', ''):
            self.assertIsNone(parse_arabic_amount(text), text)


class TransferDecisionTests(SimpleTestCase):

    def _plan(self, **kw):
        base = {'success': True, 'pairs': [], 'orphans': [], 'ambiguous': [], 'ignored': [], 'answers': [],
                'needs_resend': False, 'list_pattern': False}
        base.update(kw)
        return base

    def test_clean_pairs_become_items_low_pairs_become_questions(self):
        plan = self._plan(pairs=[
            {'account_number': '01009021516', 'value': 44880.0, 'source_message_id': 'm1', 'confidence': 'high'},
            {'account_number': '01023551947', 'value': 20200.0, 'source_message_id': 'm4', 'confidence': 'low', 'reason': 'list_pairing'},
        ], orphans=[{'kind': 'phone', 'value': '01127969725', 'message_id': 'm6'}])
        d = decide(plan, hv_threshold=100000, repeat_pending=False, reroute=None, texts={})
        self.assertEqual([(i['account_number'], i['value']) for i in d['items']], [('01009021516', 44880.0)])
        self.assertEqual([r[0] for r in d['replies']], ['m4', 'm6'])
        self.assertIn('01023551947 ← 20,200', d['replies'][0][1])
        self.assertEqual(d['replies'][1][1], 'المبلغ لـ 01127969725؟')
        self.assertEqual(d['list_confirm'], {'phones': ['01023551947']})

    def test_orphan_phone_with_a_label_candidate_gets_a_targeted_question(self):
        plan = self._plan(orphans=[{'kind': 'phone', 'value': '01023551947', 'message_id': 'a'}],
                          ignored=[{'message_id': 'a', 'text': 'عبدالله15100', 'reason': 'name_label'}])
        d = decide(plan, hv_threshold=100000, repeat_pending=False, reroute=None, texts={})
        self.assertEqual(d['replies'], [('a', 'المبلغ لـ 01023551947 هو 15,100؟')])

    def test_unreadable_separator_is_asked_never_executed(self):
        plan = self._plan(pairs=[{'account_number': '01012345678', 'value': 460010.0, 'source_message_id': 's',
                                  'confidence': 'low', 'reason': 'separator_ambiguous'}])
        d = decide(plan, hv_threshold=100000, repeat_pending=False, reroute=None, texts={'s': '01012345678\n46,0010 مصرى'})
        self.assertEqual(d['items'], [])
        self.assertIn('46,0010', d['replies'][0][1])

    def test_yes_answer_confirms_a_held_high_value_or_a_list_pairing(self):
        plan = self._plan(pairs=[{'account_number': '01012345678', 'value': 150000.0, 'source_message_id': 's', 'confidence': 'high'}],
                          answers=[{'message_id': 'ans', 'text': 'تأكيد', 'kind': 'confirmation_reply', 'about_phone': '01012345678'}])
        d = decide(plan, hv_threshold=100000, repeat_pending=False, reroute=None, texts={})
        self.assertTrue(d['items'][0].get('confirm_high_value'))
        self.assertIn('ans', d['consume'])
        plan = self._plan(pairs=[{'account_number': '01012345678', 'value': 500.0, 'source_message_id': 's', 'confidence': 'low', 'reason': 'list_pairing'}],
                          answers=[{'message_id': 'ans', 'text': 'أيوة', 'kind': 'reply', 'about_phone': '01012345678'}])
        d = decide(plan, hv_threshold=100000, repeat_pending=False, reroute=None, texts={})
        self.assertEqual(len(d['items']), 1)
        self.assertEqual(d['replies'], [])

    def test_yes_and_no_to_a_repeat_question(self):
        plan = self._plan(answers=[{'message_id': 'ans', 'text': 'أيوة كرر', 'kind': 'reply', 'about_phone': None}])
        self.assertTrue(decide(plan, hv_threshold=1e5, repeat_pending=True, reroute=None, texts={})['confirm_repeats'])
        plan = self._plan(answers=[{'message_id': 'ans', 'text': 'لأ', 'kind': 'reply', 'about_phone': None}])
        d = decide(plan, hv_threshold=1e5, repeat_pending=True, reroute=None, texts={})
        self.assertTrue(d['clear_repeats'])
        self.assertEqual(d['replies'], [('ans', 'تمام، مش هتتكرر.')])

    def test_bare_phone_after_a_reroute_notice_takes_the_owed_amount(self):
        plan = self._plan(orphans=[{'kind': 'phone', 'value': '01006004320', 'message_id': 'n'}])
        d = decide(plan, hv_threshold=1e5, repeat_pending=False, reroute={'amount': 13100.0}, texts={})
        self.assertEqual(d['items'], [{'type': 'كاش', 'value': 13100.0, 'account_number': '01006004320',
                                       'source_message_id': 'n', 'reroute': True}])
        self.assertTrue(d['reroute_used'])
        # a phone WITH an amount is a complete op, never the reroute answer
        plan = self._plan(pairs=[{'account_number': '01006004320', 'value': 5.0, 'source_message_id': 'n', 'confidence': 'high'}])
        d = decide(plan, hv_threshold=1e5, repeat_pending=False, reroute={'amount': 100000.0}, texts={})
        self.assertEqual(d['items'][0]['value'], 5.0)
        self.assertFalse(d['reroute_used'])

    def test_amount_only_uses_the_single_registered_account(self):
        plan = self._plan(orphans=[{'kind': 'amount', 'value': 500.0, 'message_id': 'a'}])
        d = decide(plan, hv_threshold=1e5, repeat_pending=False, reroute=None, texts={}, accounts=[('فورى', '6081844')])
        self.assertEqual(d['items'], [{'type': 'فورى', 'value': 500.0, 'account_number': '6081844', 'source_message_id': 'a'}])
        d = decide(plan, hv_threshold=1e5, repeat_pending=False, reroute=None, texts={}, accounts=[('فورى', '111'), ('أمان', '222')])
        self.assertEqual(d['items'], [])
        self.assertIn('أي حساب؟', d['replies'][0][1])
        self.assertEqual(d['pending']['amount'], 500.0)
        d = decide(plan, hv_threshold=1e5, repeat_pending=False, reroute=None, texts={}, accounts=[])
        self.assertEqual(d['replies'], [('a', 'الرقم للمبلغ 500؟')])


class NonCashResolutionTests(SimpleTestCase):

    def test_account_guard(self):
        acc = [('فورى', '6081844'), ('أمان', '970604')]
        self.assertEqual(resolve_noncash('1000 فوري', 'فورى', acc), {'item': {'type': 'فورى', 'value': 1000.0, 'account_number': '6081844'}})
        self.assertEqual(resolve_noncash('فوري 6081844 700', 'فورى', acc)['item']['value'], 700.0)
        self.assertIn('مسجل كحساب فورى وليس أمان', resolve_noncash('امان 6081844 500', 'أمان', acc)['reply'])
        self.assertIn('غير مسجل', resolve_noncash('فوري 5555555 500', 'فورى', [('فورى', '6081844')])['reply'])
        self.assertIn('لا يوجد حساب طاير', resolve_noncash('طاير 300', 'طاير', acc)['reply'])
        r = resolve_noncash('فوري 700', 'فورى', [('فورى', '111'), ('فورى', '222')])
        self.assertEqual(r['reply'], 'أي حساب فورى؟ 1) 111 2) 222')
        self.assertEqual(r['pending']['amount'], 700.0)
        self.assertEqual(resolve_noncash('فوري', 'فورى', [('فورى', '111')])['reply'], 'المبلغ لـ فورى 111؟')
        self.assertEqual(resolve_noncash('الفين فوري', 'فورى', [('فورى', '111')])['item']['value'], 2000.0)

    def test_multi_number_messages(self):
        m = _multi_number('01012345678\n01098765432\n500 لكل رقم')
        self.assertEqual((m['mode'], m['amount'], len(m['phones'])), ('each', 500.0, 2))
        self.assertEqual(_multi_number('01012345678 01098765432 قسم 1000 عليهم')['mode'], 'split')
        self.assertEqual(_multi_number('01012345678\n01098765432\n1000')['mode'], 'ask')
        self.assertIsNone(_multi_number('01012345678\n500'))

    def test_broken_phone_next_to_an_amount_is_never_routed_to_a_registered_account(self):
        # 2026-09-06 test line: «0106001000 ⏎ 590» created فورى 590 to the registered account
        plan = {'success': True, 'pairs': [], 'answers': [], 'ambiguous': [], 'needs_resend': False,
                'orphans': [{'kind': 'amount', 'value': 590.0, 'message_id': 'a'}],
                'ignored': [{'message_id': 'a', 'text': '0106001000', 'reason': 'broken_phone'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending=False, reroute=None, texts={}, accounts=[('فورى', '2924523')])
        self.assertEqual(d['items'], [])
        self.assertEqual(d['replies'], [('a', 'الرقم ده مش صحيح — ابعت رقم صحيح 11 رقم')])


class RepeatHoldTests(SimpleTestCase):
    """2026-09-06 test line: a held same-day repeat was re-submitted on every turn and re-asked."""

    PAIR = {'account_number': '01118696547', 'value': 10100.0, 'source_message_id': 's', 'confidence': 'high'}
    HELD = {'كاش(10)|01118696547|10100.00': {'type': 'كاش(10)', 'value': 10100.0, 'account_number': '01118696547'}}

    def _plan(self, answers=None):
        return {'success': True, 'pairs': [dict(self.PAIR)], 'orphans': [], 'ambiguous': [], 'ignored': [],
                'answers': answers or [], 'needs_resend': False, 'list_pattern': False}

    def test_held_pair_is_not_resubmitted(self):
        d = decide(self._plan(), hv_threshold=1e5, repeat_pending=self.HELD, reroute=None, texts={})
        self.assertEqual(d['items'], [])
        self.assertEqual(d['replies'], [])

    def test_no_drops_the_held_pair_and_yes_leaves_it_to_the_tool(self):
        no = [{'message_id': 'a', 'text': 'لا تجاهل', 'kind': 'reply', 'about_phone': '01118696547'}]
        d = decide(self._plan(no), hv_threshold=1e5, repeat_pending=self.HELD, reroute=None, texts={})
        self.assertTrue(d['clear_repeats'])
        self.assertEqual(d['items'], [])
        self.assertIn('s', d['consume'])
        yes = [{'message_id': 'a', 'text': 'أيوة', 'kind': 'reply', 'about_phone': '01118696547'}]
        d = decide(self._plan(yes), hv_threshold=1e5, repeat_pending=self.HELD, reroute=None, texts={})
        self.assertTrue(d['confirm_repeats'])
        self.assertEqual(d['items'], [])


class HighValueAndRerouteTests(SimpleTestCase):

    def test_amount_reply_to_the_high_value_question_is_unclear(self):
        plan = {'success': True, 'ambiguous': [], 'ignored': [], 'orphans': [], 'needs_resend': False,
                'pairs': [{'account_number': '01012345678', 'value': 100.0, 'source_message_id': 's', 'confidence': 'high',
                           'reason': 'answer_to_question', 'answer_message_id': 'a'}],
                'answers': [{'message_id': 'a', 'text': '100 ج', 'kind': 'amount_reply', 'value': 100.0, 'about_phone': '01012345678',
                             'about_message_id': 's', 'question_text': 'مبلغ كبير — محتاج منك كلمة «تأكيد» على الرسالة دي', 'applied_to': '01012345678'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending={}, reroute=None, texts={'s': '01012345678\n100الف'})
        self.assertEqual(d['items'], [])
        self.assertEqual(d['replies'], [('a', 'رديت بـ«100 ج» على تأكيد الـ100,000 — قصدك نأكد الـ100,000 ولا المبلغ 100 ج بس؟')])

    def test_self_contained_pair_while_a_reroute_is_owed_creates_and_asks_once(self):
        plan = {'success': True, 'ambiguous': [], 'ignored': [], 'orphans': [], 'answers': [], 'needs_resend': False,
                'pairs': [{'account_number': '01098765432', 'value': 5.0, 'source_message_id': 'n', 'confidence': 'high'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending={}, reroute={'amount': 5000.0}, texts={})
        self.assertEqual([(i['account_number'], i['value']) for i in d['items']], [('01098765432', 5.0)])
        self.assertEqual(d['replies'], [('n', 'والـ 5,000 بتاع التحويل اللي اترفض — يتحول على نفس الرقم ده ولا رقم تاني؟')])

    def test_pair_held_by_the_high_value_question_waits_for_the_confirmation(self):
        plan = {'success': True, 'ambiguous': [], 'ignored': [], 'orphans': [], 'answers': [], 'needs_resend': False,
                'pairs': [{'account_number': '01012345678', 'value': 100000.0, 'source_message_id': 's', 'confidence': 'high'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending={}, reroute=None, texts={}, hv_pending='01012345678')
        self.assertEqual(d['items'], [])
        plan['answers'] = [{'message_id': 'a', 'text': 'تأكيد', 'kind': 'reply', 'about_phone': '01012345678'}]
        d = decide(plan, hv_threshold=1e5, repeat_pending={}, reroute=None, texts={}, hv_pending='01012345678')
        self.assertTrue(d['items'][0]['confirm_high_value'])


class AiHandoverTests(SimpleTestCase):

    def test_noise_lines_do_not_wake_the_model(self):
        for t in ('.', '👍', '', '...'):
            self.assertTrue(_is_noise_line(t), t)
        for t in ('طارق', 'الغاء', 'حسابي كام', 'عاصم كاش', 'ليه الرصيد زاد؟', 'ممكن تبعتلي الايصال تاني', 'تم؟', '؟'):
            self.assertFalse(_is_noise_line(t), t)

    def test_summary_block_for_the_model(self):
        s = render_ai_summary({'created': [{'type': 'كاش', 'value': 4110.0, 'account_number': '01017983810'}],
                               'leftovers': [{'message_id': 'm6', 'kind': 'planner', 'text': '0 11 27969725',
                                              'suggested_reply': 'المبلغ لـ 01127969725؟'}],
                               'others': [{'message_id': 'm9', 'type': 'text', 'text': 'حسابي كام'}]})
        self.assertIn('CREATED', s)
        self.assertIn('4,110 → 01017983810', s)
        self.assertIn('[message_id: m6]', s)
        self.assertIn('المبلغ لـ 01127969725؟', s)
        self.assertIn('حسابي كام', s)
        self.assertEqual(render_ai_summary({}), 'Nothing open: every message was a clean transfer and is created.')


class BrokenNumberTests(SimpleTestCase):
    """2026-09-06: «0106013464 ⏎ الفين جنيه» leaked its 2,000 into the next split pair."""

    def test_broken_number_keeps_its_amount_out_of_the_pairing(self):
        msgs = [{'text': '0106013464\nالفين جنيه', 'message_id': 'b'},
                {'text': '01011061657', 'message_id': 'p'}, {'text': '8310', 'message_id': 'a'}]
        pairs, _m, orphans, _amb, _lp = _pair_events(_build_events(msgs, {}, {'b': 2000.0}))
        self.assertEqual([(p['account_number'], p['value']) for p in pairs], [('01011061657', 8310.0)])
        self.assertEqual(orphans, [])

    def test_broken_number_gets_the_bad_number_line(self):
        plan = {'success': True, 'pairs': [], 'orphans': [], 'ambiguous': [], 'answers': [], 'needs_resend': False,
                'ignored': [{'message_id': 'b', 'text': '0106013464', 'reason': 'broken_phone'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending={}, reroute=None, texts={})
        self.assertEqual(d['replies'], [('b', 'الرقم ده مش صحيح — ابعت رقم صحيح 11 رقم')])


class GateEchoTests(SimpleTestCase):

    def test_verbatim_echo_of_the_customer_is_blocked(self):
        from unittest import mock
        from qurtoba import ai_guard
        with mock.patch.object(ai_guard, '_last_inbound_text', return_value='حاضر ف الانتظار'):
            self.assertTrue(ai_guard.is_pure_echo('حاضر ف الانتظار', 'c'))
            self.assertTrue(ai_guard.is_pure_echo('حاضر ف الانتظار.', 'c'))
            self.assertFalse(ai_guard.is_pure_echo('تمام، تحت أمرك', 'c'))
            self.assertFalse(ai_guard.is_pure_echo('', 'c'))
