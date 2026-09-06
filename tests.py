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
from qurtoba.automation.transfers import render_ai_summary, _is_noise_line, _broken_number_amount, match_corrections  # noqa: E402
from qurtoba.automation.transfers import decide, _number_inside_prose  # noqa: E402


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
        self.assertEqual(d['list_confirm']['phones'], ['01023551947'])

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
        plan = self._plan(answers=[{'message_id': 'ans', 'text': 'أيوة', 'kind': 'reply', 'about_phone': None}])
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
        no = [{'message_id': 'a', 'text': 'لا', 'kind': 'reply', 'about_phone': '01118696547'}]
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
        for t in ('.', '👍', '', '...', 'اخصم مصاريف الخدمة', 'لو هيخصم 15 اخصمها', 'الرسوم عليا', 'خصم المصاريف من المبلغ'):
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


class CorrectedNumberTests(SimpleTestCase):
    """2026-09-06: after «ابعت رقم صحيح 11 رقم» the corrected number was asked «المبلغ؟» again."""

    def test_amount_of_a_rejected_message(self):
        self.assertEqual(_broken_number_amount('0106013464\nالفين جنيه'), 2000.0)
        self.assertEqual(_broken_number_amount('0100600100\n590'), 590.0)
        self.assertIsNone(_broken_number_amount('01006001000\n590'))     # valid number → not rejected
        self.assertIsNone(_broken_number_amount('0106013464'))            # no amount

    def test_bare_number_after_the_bad_number_line_takes_the_amount(self):
        items = match_corrections(
            [{'message_id': 'n', 'value': '01060134646', 'at': 10}],
            [{'message_id': 'b', 'amount': 2000.0, 'at': 5, 'asked': True}])
        self.assertEqual(items, [{'type': 'كاش', 'value': 2000.0, 'account_number': '01060134646',
                                  'source_message_id': 'n', 'correction_of': 'b'}])
        # not yet told it was wrong → not a correction; two rejected messages → ambiguous → ask
        self.assertEqual(match_corrections([{'message_id': 'n', 'value': '01060134646', 'at': 10}],
                                           [{'message_id': 'b', 'amount': 2000.0, 'at': 5, 'asked': False}]), [])
        self.assertEqual(match_corrections([{'message_id': 'n', 'value': '01060134646', 'at': 10}],
                                           [{'message_id': 'b', 'amount': 2000.0, 'at': 5, 'asked': True},
                                            {'message_id': 'c', 'amount': 500.0, 'at': 6, 'asked': True}]), [])


class CorrectionConfirmTests(SimpleTestCase):

    def test_hawel_is_a_yes_and_the_line_reads_well(self):
        from qurtoba.automation import replies as R
        self.assertTrue(L.is_yes('حول'))
        self.assertTrue(L.is_yes('حول يا باشا'))
        self.assertFalse(L.is_yes('حول 500'))
        self.assertEqual(R.CORRECTION_CONFIRM.format(amount='2,000', phone='01060134646'),
                         'الرقم اللي فات كان غلط 🙏\nتقصد تحويل 2,000 على الرقم ده 01060134646؟\nلو أيوة ابعت «حول» وننفذها فوراً.')


class AdversarialFixTests(SimpleTestCase):

    def test_two_rejected_numbers_make_a_correction_ambiguous(self):
        items = match_corrections(
            [{'message_id': 'n', 'value': '01012345678', 'at': 10}],
            [{'message_id': 'a', 'amount': 2000.0, 'at': 5, 'asked': True},
             {'message_id': 'b', 'amount': 300.0, 'at': 6, 'asked': False}])
        self.assertEqual(items, [])

    def test_question_confirm_line(self):
        from qurtoba.automation import replies as R
        line = R.QUESTION_CONFIRM.format(amount='500', phone='01012345678')
        self.assertIn('«حول»', line)
        self.assertIn('01012345678', line)

    def test_same_number_twice_in_one_message_is_one_number(self):
        cls = _classify_message('01012345678\n01012345678\n800')
        self.assertEqual(cls['phones'], ['01012345678'])
        self.assertEqual(cls['amounts'], [800])


class MeaningGoesToTheModelTests(SimpleTestCase):

    def test_only_a_bare_word_is_a_yes_or_no_for_python(self):
        for t in ('حول', 'أيوة', 'تأكيد', 'لا', 'بلاش'):
            self.assertTrue(L.is_bare_yes(t) or L.is_bare_no(t), t)
        for t in ('تمام يا معلم اعملها', 'لا مش عايز اكررها', 'ايوه بس خليها 300', 'ماشي نفذها ربنا يخليك'):
            self.assertFalse(L.is_bare_yes(t) or L.is_bare_no(t), t)

    def test_a_worded_reply_to_our_question_goes_to_the_model(self):
        plan = {'success': True, 'pairs': [], 'orphans': [], 'ambiguous': [], 'ignored': [], 'needs_resend': False,
                'answers': [{'message_id': 'a', 'text': 'تمام يا معلم اعملها', 'kind': 'reply', 'about_phone': '01012345678',
                             'question_text': 'تحب أكررها؟'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending={'x': {'account_number': '01012345678', 'value': 500.0}}, reroute=None, texts={})
        self.assertFalse(d['confirm_repeats'])
        self.assertEqual([t['message_id'] for t in d['to_model']], ['a'])


class LayoutNotMeaningTests(SimpleTestCase):

    def test_number_inside_prose_goes_to_the_model_but_order_layouts_do_not(self):
        for t in ('انا بعت لـ 01012345678 امبارح 500 وصلت؟', '01012345678 500 ده اتحول ولا لسه', 'ابعت 500 على 01012345678 لو سمحت'):
            self.assertTrue(_number_inside_prose(t, _classify_message(t)), t)
        for t in ('01012345678\n500', '01012345678 500', '01012345678\n5000\nعاصم كاش محمد سعد الرباط', '01012345678 كاش 500',
                  '01012345678\n500 جنيه\nطارق', 'رقم المستلم: 01090878331\nالقيمة: 15,014',
                  '01017154397 المبلغ  20 ألف  اسامه البنا', 'الرقم 01011637469\n\nالقيمه 30000ج.م فدفون كاش'):
            self.assertFalse(_number_inside_prose(t, _classify_message(t)), t)


class ThousandAndTests(SimpleTestCase):
    """2026-09-06 live: «27 ألف و 700» was created as 27 pounds. «X ألف و Y» = X×1000 + Y."""

    def test_thousand_and_rest(self):
        from qurtoba.tools._amounts import normalize_amount
        cases = {'27 ألف و 700': 27700, '70 ألف و 225': 70225, '20 ألف و 625': 20625, '41 ألف و 400': 41400,
                 '33 ألف و 100': 33100, '46 ألف و 10': 46010, 'الفين و 500': 2500, '3 آلاف و نص': 3500,
                 '50 ألف': 50000, '٢٧ ألف و ٧٠٠': 27700, '27الف و700': 27700}
        for text, value in cases.items():
            r = normalize_amount(text)
            self.assertTrue(r['ok'], text)
            self.assertEqual(r['value'], value, text)

    def test_live_messages_pair_correctly(self):
        for text, value in {'01009659589\n27 ألف و 700\n💰كاش🔟 - جنى(112)': 27700,
                            '01080658932\n70 ألف و 225\n💰كاش🔟 - خيرى(173)': 70225,
                            '01148485123\n50 ألف \n💰كاش🔟 - وفا(843)': 50000}.items():
            cls = _classify_message(text)
            self.assertEqual(cls['amounts'], [value], text)


class TallyLineTests(SimpleTestCase):

    def test_a_line_with_a_fraction_is_a_label_line(self):
        cls = _classify_message('W2405\n01069214107\n35.343 ج م\nفودافون\n961 نصار 6.08')
        self.assertEqual(cls['phones'], ['01069214107'])
        self.assertEqual(cls['amounts'], [35343])
        cls = _classify_message('W2399\n01276956929\n39.125 مصري مصري\nفودافوان\nعامر فون 6.08')
        self.assertEqual(cls['amounts'], [39125])


class ThrottleRetryTests(SimpleTestCase):

    def test_throttled_send_is_retried_then_released(self):
        from unittest import mock
        from qurtoba import ai_guard
        calls = []
        def original(self, partner, content, message_type='text', conversation=None, system_partner=None, **kw):
            calls.append(1)
            return {'success': False, 'error': 'Failed: (#131056) pair rate limit'} if len(calls) < 2 else {'success': True}
        with mock.patch.object(ai_guard.time, 'sleep', lambda s: None) if hasattr(ai_guard, 'time') else mock.patch('time.sleep', lambda s: None):
            res = ai_guard._deliver(original, None, None, {'text': 'x'}, None, None, 'text', {})
        self.assertTrue(res['success'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(ai_guard._is_throttled({'success': False, 'error': 'تعذّر إرسال الرسالة عبر المزوّد. (#131056)'}))
        self.assertFalse(ai_guard._is_throttled({'success': False, 'error': 'invalid number'}))


class ListConfirmScopeTests(SimpleTestCase):

    def test_only_positional_guesses_are_confirmed_clean_pairs_are_created(self):
        plan = {'success': True, 'orphans': [], 'ignored': [], 'answers': [], 'needs_resend': False, 'list_pattern': True,
                'ambiguous': [{'account_number': '01055512345', 'value': 300.0, 'source_message_id': 'c', 'reason': 'list_pairing'}],
                'pairs': [{'account_number': '01012345678', 'value': 500.0, 'source_message_id': 'a', 'confidence': 'high'},
                          {'account_number': '01098765432', 'value': 600.0, 'source_message_id': 'b', 'confidence': 'high'},
                          {'account_number': '01055512345', 'value': 300.0, 'source_message_id': 'c', 'confidence': 'low'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending={}, reroute=None, texts={})
        self.assertEqual([i['account_number'] for i in d['items']], ['01012345678', '01098765432'])
        self.assertEqual(d['list_confirm']['phones'], ['01055512345'])


class PendingLifetimeTests(SimpleTestCase):

    def test_a_stale_marker_is_not_pending(self):
        import time
        from qurtoba.automation import pending as P
        self.assertTrue(P._fresh({'ts': time.time() - 60}))
        self.assertFalse(P._fresh({'ts': time.time() - 20 * 60}))
        self.assertFalse(P._fresh({}))


class QuotePairingTests(SimpleTestCase):
    """2026-09-06: «1000 جنى» quoted on the customer's own «01275362968» was asked «الرقم للمبلغ 1,000؟»."""

    def test_amount_quoted_on_a_number_is_that_numbers_amount(self):
        msgs = [{'text': '01275362968', 'message_id': 'n'}, {'text': '1000 جنى', 'message_id': 'a'}]
        pairs, mids, orphans, _amb, _lp = _pair_events(_build_events(msgs, {}, None, {'a': ('n', '01275362968')}))
        self.assertEqual([(p['account_number'], p['value'], p['source_message_id']) for p in pairs], [('01275362968', 1000.0, 'n')])
        self.assertEqual(mids, [{'n', 'a'}])
        self.assertEqual(orphans, [])

    def test_number_quoted_on_an_amount_is_the_same_pair(self):
        msgs = [{'text': '500', 'message_id': 'a'}, {'text': '01012345678', 'message_id': 'n'}]
        pairs, mids, orphans, _amb, _lp = _pair_events(_build_events(msgs, {}, None, {'n': ('a', '500')}))
        self.assertEqual([(p['account_number'], p['value'], p['source_message_id']) for p in pairs], [('01012345678', 500.0, 'n')])
        self.assertEqual(orphans, [])


class AttackRoundTwoTests(SimpleTestCase):

    def test_country_code_fragment_is_not_an_amount(self):
        for t in ('+2 01012345678\n500', '+20 01012345678\n500', '002 01012345678 500'):
            cls = _classify_message(t)
            self.assertEqual(cls['phones'], ['01012345678'], t)
            self.assertEqual(cls['amounts'], [500], t)

    def test_tam_is_not_a_yes_and_hold_words_are_detected(self):
        self.assertFalse(L.is_bare_yes('تم'))
        for t in ('01012345678 500 الغي', '01012345678 500 متبعتش', 'تحصيل 500 من 01012345678', 'سداد 500 على 01012345678', '01012345678 500 بكرة'):
            self.assertTrue(L.HOLD.search(L.norm(t)), t)
        self.assertFalse(L.HOLD.search(L.norm('01012345678\n500\nعاصم كاش')))

    def test_duplicate_guard_is_per_quoted_message(self):
        from qurtoba import ai_guard
        self.assertNotEqual(ai_guard._duplicate_key('c', 'x', 'a'), ai_guard._duplicate_key('c', 'x', 'b'))


class HighValueRevalueGuardTests(SimpleTestCase):

    def test_a_smaller_amount_on_the_held_number_is_never_created(self):
        plan = {'success': True, 'ambiguous': [], 'ignored': [], 'orphans': [], 'answers': [], 'needs_resend': False,
                'pairs': [{'account_number': '01012345678', 'value': 100.0, 'source_message_id': 's', 'confidence': 'high'}]}
        d = decide(plan, hv_threshold=1e5, repeat_pending={}, reroute=None, texts={'s': '01012345678\n\n100الف'}, hv_pending='01012345678')
        self.assertEqual(d['items'], [])
        self.assertIn('100,000', d['replies'][0][1])
