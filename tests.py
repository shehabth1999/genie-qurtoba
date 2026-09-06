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
