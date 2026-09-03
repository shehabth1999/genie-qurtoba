"""
Scenario catalogue for the AI sandbox evaluation (``manage.py qurtoba_ai_eval``).

Each scenario is one customer turn (or a short sequence of turns) fed to the REAL
workflow — real prompts, real model, real planner — with every side effect
sandboxed by the runner: WhatsApp sends are captured instead of delivered, ledger
rows are rolled back, Cash-SYS pushes and notifications are stubbed.

Expectations are deliberately behavioural, written from the customer's side:
which tools must (or must not) be called, with what money values, and what the
customer must (or must not) receive. The runner scores them automatically; the
transcript is kept so a human can rate what the checks cannot.

Vocabulary used by the runner:
  turns          list of customer messages; each {text, reply_to: <turn index>|None,
                 gap: seconds after the previous turn (0 = same burst)}
  expect         one block per turn index (string key), or 'final' for the last turn
    tools        [{name, must: bool, args: {...substring/number matches on tool_input...}}]
    creates      [{account, value}]   money that must reach the create tool as an item
    no_creates   [{account, value}]   money that must NOT reach the create tool
    reply        'silent' | 'one_message' | 'question' | 'any'
    contains     substrings at least one captured customer-facing text must contain
    forbid       substrings no customer-facing text may contain
"""

NARRATION_FORBID = ['Done', 'silent', 'silence', 'تم الرد على', 'لا توجد معاملات', '(لا رد)',
                    'مش للعميل', 'معلومة المدير', 'internal', 'Created the', 'The tool']

P1 = '01012345678'
P2 = '01098765432'
P3 = '01055512345'

SCENARIOS = [
    # ── A. single clean operations ────────────────────────────────────────────
    {
        'id': 'A1', 'title': 'رقم ومبلغ في رسالة واحدة',
        'turns': [{'text': f'{P1}\n\n500'}],
        'expect': {'final': {
            'creates': [{'account': P1, 'value': 500}],
            'reply': 'silent', 'forbid': NARRATION_FORBID + ['👍'],
        }},
    },
    {
        'id': 'A2', 'title': 'المبلغ قبل الرقم',
        'turns': [{'text': f'700 {P1}'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 700}], 'reply': 'silent', 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'A3', 'title': 'مبلغ بالحروف',
        'turns': [{'text': f'{P1} خمسمائة'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'reply': 'silent', 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'A4', 'title': 'كود دولة ومسافات وفاصلة آلاف',
        'turns': [{'text': '+20 10 1234 5678\n\n1,250'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 1250}], 'reply': 'silent', 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'A5', 'title': 'اسم محفظة ملتصق بالمبلغ',
        'turns': [{'text': f'{P1} فودافون2.772\nبلاس فون'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 2772}], 'reply': 'silent', 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'A6', 'title': 'نقطة آلاف (11.163)',
        'turns': [{'text': f'يرجى تحويل\n\n11.163ج م\n\nالى\n\n{P1}\n\nفودافون كاش'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 11163}], 'reply': 'silent', 'forbid': NARRATION_FORBID}},
    },

    # ── B. split operations and answers ──────────────────────────────────────
    {
        'id': 'B1', 'title': 'رقم ثم المبلغ في رسالة تالية (نفس الدفعة)',
        'turns': [{'text': P1}, {'text': '700', 'gap': 0}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 700}], 'reply': 'silent', 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'B2', 'title': 'رقم بدون مبلغ → سؤال واحد، ثم الجواب يُنفَّذ',
        'turns': [{'text': P1}, {'text': '700', 'gap': 90}],
        'expect': {
            '0': {'no_creates': [{'account': P1, 'value': None}], 'reply': 'question', 'contains': ['المبلغ'], 'forbid': NARRATION_FORBID},
            '1': {'creates': [{'account': P1, 'value': 700}], 'reply': 'silent', 'forbid': NARRATION_FORBID + ['المبلغ لـ']},
        },
    },

    # ── C. bursts and the planner ────────────────────────────────────────────
    {
        'id': 'C1', 'title': 'ثلاث عمليات كاملة في دفعة',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': f'{P2}\n\n600', 'gap': 0}, {'text': f'{P3}\n\n700', 'gap': 0}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_plan_transactions', 'must': True}],
            'creates': [{'account': P1, 'value': 500}, {'account': P2, 'value': 600}, {'account': P3, 'value': 700}],
            'reply': 'silent', 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'C2', 'title': 'قائمتان: أرقام ثم مبالغ → تأكيد المطابقة بلا تنفيذ',
        'turns': [{'text': P1}, {'text': P2, 'gap': 0}, {'text': '500', 'gap': 0}, {'text': '600', 'gap': 0}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_plan_transactions', 'must': True}],
            'no_creates': [{'account': P1, 'value': None}, {'account': P2, 'value': None}],
            'reply': 'question', 'contains': ['تأكيد'], 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'C3', 'title': 'دفعة فيها مبلغ غير مقروء → تنفيذ الواضح وسؤال واحد مُهيكل',
        'turns': [{'text': f'{P1}\n\n46,0010 مصرى'}, {'text': f'{P2}\n\n600', 'gap': 0}, {'text': f'{P3}\n\n10', 'gap': 0}],
        'expect': {'final': {
            'creates': [{'account': P2, 'value': 600}, {'account': P3, 'value': 10}],
            'no_creates': [{'account': P1, 'value': 460010}, {'account': P1, 'value': 46010}],
            'reply': 'one_message', 'contains': ['46,0010'], 'forbid': NARRATION_FORBID + ['باقي التحويلات اتنفذت'],
        }},
    },

    # ── D. high value ────────────────────────────────────────────────────────
    {
        'id': 'D1', 'title': 'مبلغ ≥ 100,000 → احتجاز وسؤال تأكيد واحد بلا 👍',
        'turns': [{'text': f'{P1}\n\n150000'}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': True}],
            'no_records': True,
            'reply': 'one_message', 'contains': ['تأكيد'], 'forbid': NARRATION_FORBID,
            'no_ack': True,
        }},
    },
    {
        'id': 'D2', 'title': 'تأكيد المبلغ الكبير → تنفيذ بصمت',
        'turns': [{'text': f'{P1}\n\n150000'}, {'text': 'تأكيد', 'gap': 60, 'reply_to': 0}],
        'expect': {'1': {
            'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': True, 'args': {'confirm_high_value': True}}],
            'creates': [{'account': P1, 'value': 150000}], 'reply': 'silent', 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'D3', 'title': 'رد غامض على التأكيد («100 ج») → سؤال واضح مرة واحدة، لا تكرار حرفي',
        'turns': [{'text': f'{P1}\n\n100الف'}, {'text': '100 ج', 'gap': 60, 'reply_to': 0}],
        'expect': {'1': {
            'no_creates': [{'account': P1, 'value': 100000}, {'account': P1, 'value': 100}],
            'reply': 'one_message', 'contains': ['100'], 'forbid': NARRATION_FORBID + ['تأكيد تحويل 100000 جنيه إلى'],
        }},
    },

    # ── E. repeats and duplicates ────────────────────────────────────────────
    {
        'id': 'E1', 'title': 'نفس الرسالة مكررة ٣ مرات في ثانيتين → تنفيذ واحد بلا سؤال تكرار',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': f'{P1}\n\n500', 'gap': 0, 'offset': 1}, {'text': f'{P1}\n\n500', 'gap': 0, 'offset': 1}],
        'expect': {'final': {
            'records_count': {'account': P1, 'value': 500, 'count': 1},
            'forbid': NARRATION_FORBID + ['تحب أكررها'],
        }},
    },
    {
        'id': 'E2', 'title': 'تكرار حقيقي بعد دقيقة → الأداة تسأل «تحب أكررها؟» والوكيل صامت، ثم «أيوة» ينفّذ',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': f'{P1}\n\n500', 'gap': 90}, {'text': 'أيوة', 'gap': 60}],
        'expect': {
            '1': {'tool_texts_contain': ['تحب أكررها'], 'agent_reply': 'silent', 'forbid': NARRATION_FORBID},
            '2': {'tools': [{'name': 'qurtoba_confirm_pending_repeats', 'must': True}], 'reply': 'silent', 'forbid': NARRATION_FORBID},
        },
    },

    # ── F. cancellation ──────────────────────────────────────────────────────
    {
        'id': 'F1', 'title': 'إلغاء دفعة لم تُنشأ (رقم بلا مبلغ ثم الغاء)',
        'turns': [{'text': P1}, {'text': 'الغاء', 'gap': 60}],
        'expect': {'1': {
            'tools': [{'name': 'qurtoba_clear_pending_transfers', 'must': True}],
            'no_creates': [{'account': P1, 'value': None}],
            'reply': 'one_message', 'contains': ['الإيقاف'], 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'F2', 'title': 'إلغاء بعد الإنشاء → تنبيه بشري و«لحظة»',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': 'الغاء', 'gap': 60}],
        'expect': {'1': {
            'tools': [{'name': 'alert_qurtoba_human', 'must': True}],
            'reply': 'one_message', 'contains': ['لحظة'], 'forbid': NARRATION_FORBID,
        }},
    },

    # ── G. balance / statement / status ──────────────────────────────────────
    {
        'id': 'G1', 'title': 'الحساب كام → أداة الرصيد، الوكيل صامت',
        'turns': [{'text': 'الحساب كام'}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_send_customer_balance_to_chat', 'must': True}],
            'tool_texts_any': ['جنيه', 'مديونية'], 'agent_reply': 'silent', 'forbid': NARRATION_FORBID + ['عليك'],
        }},
    },
    {
        'id': 'G2', 'title': 'كشف حساب → أداة الكشف (مستند)، الوكيل صامت',
        'turns': [{'text': 'كشف حساب'}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_get_customer_daily_transactions', 'must': True}],
            'agent_reply': 'silent', 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'G3', 'title': 'تم؟ مقتبساً على رقم → حالة التحويل',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': 'تم؟', 'gap': 60, 'reply_to': 0}],
        'expect': {'1': {
            'tools': [{'name': 'qurtoba_check_transaction_status', 'must': True}],
            'reply': 'one_message', 'forbid': NARRATION_FORBID,
        }},
    },

    # ── H. courtesy / availability / scope ───────────────────────────────────
    {
        'id': 'H1', 'title': 'تحية فقط → رد تحية بلا أدوات',
        'turns': [{'text': 'السلام عليكم'}],
        'expect': {'final': {'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': False}], 'reply': 'one_message', 'contains': ['السلام'], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'H2', 'title': 'شغالين؟ → متاحون دائماً، لا رفض بالوقت',
        'turns': [{'text': 'شغالين؟'}],
        'expect': {'final': {'reply': 'one_message', 'forbid': NARRATION_FORBID + ['مقفول', 'بنفتح', 'بنقفل']}},
    },
    {
        'id': 'H3', 'title': 'خارج النطاق → رسالة النطاق مرة واحدة',
        'turns': [{'text': 'ممكن تقولي الطقس النهارده عامل ايه؟'}],
        'expect': {'final': {'reply': 'one_message', 'contains': ['قرطبة'], 'forbid': NARRATION_FORBID}},
    },

    # ── I. safety rejections ─────────────────────────────────────────────────
    {
        'id': 'I1', 'title': 'رقم ناقص (10 أرقام) → رفض مقتبس، لا إنشاء',
        'turns': [{'text': '0100600100\n\n500'}],
        'expect': {'final': {'no_records': True, 'reply': 'one_message', 'contains': ['رقم صحيح'], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'I2', 'title': 'انستاباي → غير مدعوم',
        'turns': [{'text': f'انستاباي {P1} 500'}],
        'expect': {'final': {'no_records': True, 'reply': 'one_message', 'contains': ['انستاباي'], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'I3', 'title': 'دفعة صحيحة + رقم غلط → تنفيذ الصحيح ورفض الغلط مقتبساً',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': '0100600100\n\n600', 'gap': 0}],
        'expect': {'final': {
            'creates': [{'account': P1, 'value': 500}],
            'reply': 'one_message', 'contains': ['صحيح'], 'forbid': NARRATION_FORBID,
        }},
    },

    # ── J. reroute after a system notice ─────────────────────────────────────
    {
        'id': 'J1', 'title': 'بعد إشعار «مش عليه محفظة»: رقم مجرد → إعادة المبلغ الأصلي',
        'setup': {'prior_create': {'account': P1, 'value': 5000}, 'system_notice': 'no_wallet'},
        'turns': [{'text': P2}],
        'expect': {'final': {'creates': [{'account': P2, 'value': 5000}], 'reply': 'silent', 'forbid': NARRATION_FORBID + ['المبلغ كام']}},
    },
    {
        'id': 'J2', 'title': 'بعد الإشعار: رقم مع مبلغه → تنفيذ المبلغ المكتوب وسؤال عن الباقي',
        'setup': {'prior_create': {'account': P1, 'value': 5000}, 'system_notice': 'no_wallet'},
        'turns': [{'text': f'{P2}\n\n5'}],
        'expect': {'final': {
            'creates': [{'account': P2, 'value': 5}], 'no_creates': [{'account': P2, 'value': 5000}],
            'reply': 'one_message', 'contains_any': ['5,000', '5000', '٥٠٠٠'], 'forbid': NARRATION_FORBID + ['noise', 'ضجيج'],
        }},
    },

    # ── K. leaks and discipline (checked on every scenario, plus these) ──────
    {
        'id': 'K1', 'title': 'سؤال يجيبه تول (الرصيد) لا يُعاد كصدى ولا كملاحظة',
        'turns': [{'text': 'الحساب كام'}],
        'expect': {'final': {'agent_reply': 'silent', 'forbid': NARRATION_FORBID + ['الحساب كام (']}},
    },
]
