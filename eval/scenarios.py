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
    quoted_replies   N — exactly N agent texts reached the customer as a QUOTE (via
                 whatsapp_reply_to_message, or the gate forwarding a greeting as a quote
                 on the customer's message)
    quoted_on    [turn indexes] — each listed turn's inbound must be quoted by an agent text
    no_success_list  true — no agent text recites what succeeded (the 👍 already says it)

Since 2026-09-05 (office requirements) every scored turn also carries a GLOBAL check:
no agent text may be delivered unquoted — only the reply tool (or the gate's forward)
delivers; a faulty message gets its OWN quoted reply, a clean one gets nothing.
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
            '0': {'no_creates': [{'account': P1, 'value': None}], 'reply': 'question', 'contains': ['المبلغ'],
                  'quoted_replies': 1, 'quoted_on': [0], 'forbid': NARRATION_FORBID},
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
            'reply': 'question', 'contains_any': ['تأكيد', 'أكد', 'صح'], 'quoted_replies': 1,
            'no_success_list': True, 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'C3', 'title': 'دفعة فيها مبلغ غير مقروء → تنفيذ الواضح ورد واحد مقتبس على الرسالة الغلط فقط',
        'turns': [{'text': f'{P1}\n\n46,0010 مصرى'}, {'text': f'{P2}\n\n600', 'gap': 0}, {'text': f'{P3}\n\n10', 'gap': 0}],
        'expect': {'final': {
            'creates': [{'account': P2, 'value': 600}, {'account': P3, 'value': 10}],
            'no_creates': [{'account': P1, 'value': 460010}, {'account': P1, 'value': 46010}],
            'reply': 'one_message', 'quoted_replies': 1, 'quoted_on': [0], 'contains': ['46,0010'],
            'no_success_list': True, 'forbid': NARRATION_FORBID + ['باقي التحويلات اتنفذت', P2, P3],
        }},
    },

    # ── D. high value ────────────────────────────────────────────────────────
    {
        'id': 'D1', 'title': 'مبلغ ≥ 100,000 → احتجاز وسؤال تأكيد واحد مقتبس على رسالة التحويل بلا 👍',
        'turns': [{'text': f'{P1}\n\n150000'}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': True}],
            'no_records': True,
            'reply': 'one_message', 'quoted_replies': 1, 'quoted_on': [0], 'contains': ['تأكيد'],
            'no_success_list': True, 'forbid': NARRATION_FORBID,
            'no_ack': True,
        }},
    },
    {
        'id': 'D2', 'title': 'تأكيد المبلغ الكبير → تنفيذ بصمت',
        'turns': [{'text': f'{P1}\n\n150000'}, {'text': 'تأكيد', 'gap': 60, 'reply_to': 0}],
        'expect': {'1': {
            'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': True, 'args': {'confirm_high_value': True}}],
            'creates': [{'account': P1, 'value': 150000}], 'reply': 'silent', 'no_success_list': True,
            'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'D3', 'title': 'رد غامض على التأكيد («100 ج») → توضيح واحد مقتبس، لا تكرار حرفي',
        'turns': [{'text': f'{P1}\n\n100الف'}, {'text': '100 ج', 'gap': 60, 'reply_to': 0}],
        'expect': {'1': {
            'no_creates': [{'account': P1, 'value': 100000}, {'account': P1, 'value': 100}],
            'reply': 'one_message', 'quoted_replies': 1, 'contains': ['100'],
            'no_success_list': True, 'forbid': NARRATION_FORBID + ['تأكيد تحويل 100000 جنيه إلى'],
        }},
    },

    # ── E. repeats and duplicates ────────────────────────────────────────────
    {
        'id': 'E1', 'title': 'نفس الرسالة مكررة ٣ مرات في ثانيتين → تنفيذ واحد، النسخ صامتة، لا رد إطلاقاً',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': f'{P1}\n\n500', 'gap': 0, 'offset': 1}, {'text': f'{P1}\n\n500', 'gap': 0, 'offset': 1}],
        'expect': {'final': {
            'records_count': {'account': P1, 'value': 500, 'count': 1},
            'reply': 'silent', 'no_success_list': True,
            'forbid': NARRATION_FORBID + ['تحب أكررها', 'اتسجّل', 'اتسجل', 'وصلت 3 مرات'],
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
            'tools': [{'name': 'qurtoba_clear_pending_transfers', 'must': True},
                      {'name': 'whatsapp_reply_to_message', 'must': False}],
            'no_creates': [{'account': P1, 'value': None}],
            # the clear tool posts «تم الإيقاف…» itself (quoted); the agent stays silent
            'tool_texts_contain': ['الإيقاف'], 'agent_reply': 'silent', 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'F2', 'title': 'إلغاء بعد الإنشاء → تنبيه بشري و«لحظة»',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': 'الغاء', 'gap': 60}],
        'expect': {'1': {
            'tools': [{'name': 'alert_qurtoba_human', 'must': True}],
            'reply': 'one_message', 'quoted_replies': 1, 'contains': ['لحظة'], 'forbid': NARRATION_FORBID,
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
            'reply': 'one_message', 'quoted_replies': 1, 'forbid': NARRATION_FORBID,
        }},
    },

    # ── H. courtesy / availability / scope ───────────────────────────────────
    {
        'id': 'H1', 'title': 'تحية فقط → رد تحية بلا أدوات',
        'turns': [{'text': 'السلام عليكم'}],
        'expect': {'final': {'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': False}], 'reply': 'one_message',
                             'quoted_replies': 1, 'contains': ['السلام'], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'H2', 'title': 'شغالين؟ → متاحون دائماً، لا رفض بالوقت',
        'turns': [{'text': 'شغالين؟'}],
        'expect': {'final': {'reply': 'one_message', 'quoted_replies': 1, 'forbid': NARRATION_FORBID + ['مقفول', 'بنفتح', 'بنقفل']}},
    },
    {
        'id': 'H3', 'title': 'خارج النطاق → رسالة النطاق مرة واحدة',
        'turns': [{'text': 'ممكن تقولي الطقس النهارده عامل ايه؟'}],
        'expect': {'final': {'reply': 'one_message', 'quoted_replies': 1, 'contains': ['قرطبة'], 'forbid': NARRATION_FORBID}},
    },

    # ── I. safety rejections ─────────────────────────────────────────────────
    {
        'id': 'I1', 'title': 'رقم ناقص (10 أرقام) → رفض مقتبس، لا إنشاء',
        'turns': [{'text': '0100600100\n\n500'}],
        'expect': {'final': {'no_records': True, 'reply': 'one_message', 'quoted_replies': 1, 'quoted_on': [0],
                             'contains': ['رقم صحيح'], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'I2', 'title': 'انستاباي → غير مدعوم',
        'turns': [{'text': f'انستاباي {P1} 500'}],
        'expect': {'final': {'no_records': True, 'reply': 'one_message', 'quoted_replies': 1, 'quoted_on': [0],
                             'contains': ['انستاباي'], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'I3', 'title': 'دفعة صحيحة + رقم غلط → تنفيذ الصحيح بصمت ورد واحد مقتبس على رسالة الرقم الغلط فقط',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': '0100600100\n\n600', 'gap': 0}],
        'expect': {'final': {
            'creates': [{'account': P1, 'value': 500}],
            'reply': 'one_message', 'quoted_replies': 1, 'quoted_on': [1], 'contains': ['صحيح'],
            'no_success_list': True, 'forbid': NARRATION_FORBID + [P1],
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
        'id': 'J2', 'title': 'بعد الإشعار: رقم مع مبلغه → تنفيذ المبلغ المكتوب وسؤال واحد مقتبس عليه عن الـ5,000',
        'setup': {'prior_create': {'account': P1, 'value': 5000}, 'system_notice': 'no_wallet'},
        'turns': [{'text': f'{P2}\n\n5'}],
        'expect': {'final': {
            'creates': [{'account': P2, 'value': 5}], 'no_creates': [{'account': P2, 'value': 5000}],
            'reply': 'one_message', 'quoted_replies': 1, 'quoted_on': [0], 'contains_any': ['5,000', '5000', '٥٠٠٠'],
            'no_success_list': True, 'forbid': NARRATION_FORBID + ['noise', 'ضجيج'],
        }},
    },
    {
        'id': 'J3', 'title': 'رقم اترفض «مش عليه محفظة» ثم اتبعت تاني بمبلغ → يتسجل عادي بصمت (Cash-SYS يقرر)',
        'setup': {'prior_create': {'account': P1, 'value': 5000}, 'system_notice': 'no_wallet'},
        'turns': [{'text': f'{P1}\n\n700'}],
        'expect': {'final': {
            'creates': [{'account': P1, 'value': 700}], 'no_creates': [{'account': P1, 'value': 5000}],
            # The 700 is registered silently; the agent MAY ask once (quoted) what to do
            # with the still-owed 5,000 from the bounced transfer — never refuse the number.
            'reply': 'any', 'no_success_list': True,
            'forbid': NARRATION_FORBID + ['اترفض النهارده', 'ابعت رقم تاني', 'مش عليه محفظة'],
        }},
    },

    # ── K. leaks and discipline (checked on every scenario, plus these) ──────
    {
        'id': 'K1', 'title': 'سؤال يجيبه تول (الرصيد) لا يُعاد كصدى ولا كملاحظة',
        'turns': [{'text': 'الحساب كام'}],
        'expect': {'final': {'agent_reply': 'silent', 'forbid': NARRATION_FORBID + ['الحساب كام (']}},
    },
    {
        'id': 'K2', 'title': 'صباح الخير → رد واحد مقتبس على رسالة العميل (عبر الأداة أو تحويل البوابة)',
        'turns': [{'text': 'صباح الخير'}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': False}],
            'reply': 'one_message', 'quoted_replies': 1, 'quoted_on': [0], 'forbid': NARRATION_FORBID,
        }},
    },
]


# ── L. latency: the real burst from conversation d8bc5e42 on 2026-09-05 14:44 ──
SCENARIOS.append({
    'id': 'L1', 'title': 'دفعة حقيقية: تكرار + عملية جديدة + رقم ناقص بمبلغ بالحروف (قياس الزمن)',
    'turns': [
        {'text': f'{P1}\n\n1000'},
        {'text': f'{P1}\n\n1000', 'gap': 90},
        {'text': '01006001000\n\n60', 'gap': 0, 'offset': 0},
        {'text': '0106013464\n\nالفين جنيه', 'gap': 0, 'offset': 1},
    ],
    'expect': {'1': {
        'tools': [{'name': 'qurtoba_plan_transactions', 'must': True}],
        'creates': [{'account': '01006001000', 'value': 60}],
        'tool_texts_contain': ['تحب أكررها'],
        'quoted_replies': 1, 'quoted_on': [3],
        'no_success_list': True, 'forbid': NARRATION_FORBID,
    }},
})


# ── V. workflow v2 (money first, thinking second) ──
# Clean transfers are created by Python before any model runs; open items and non-money
# messages go to the thinking model, which answers through the reply tool.
SCENARIOS += [
    {
        'id': 'V1', 'title': 'v2: سؤال الرصيد → الموديل يستدعي أداة الرصيد فقط',
        'turns': [{'text': 'حسابي كام؟'}],
        'expect': {'final': {
            'tools': [{'name': 'qurtoba_send_customer_balance_to_chat', 'must': True}],
            'no_records': True, 'reply': 'silent', 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'V2', 'title': 'v2: تحية → رد واحد مقتبس من الموديل',
        'turns': [{'text': 'السلام عليكم'}],
        'expect': {'final': {
            'no_records': True, 'reply': 'one_message', 'quoted_replies': 1, 'contains': ['السلام'], 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'V3', 'title': 'v2: «تم؟» → أداة الحالة، سطر واحد',
        'turns': [{'text': f'{P1}\n\n500'}, {'text': 'تم؟', 'gap': 60, 'reply_to': 0}],
        'expect': {'1': {
            'tools': [{'name': 'qurtoba_check_transaction_status', 'must': True}],
            'reply': 'one_message', 'quoted_replies': 1, 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'V4', 'title': 'v2: إلغاء دفعة لم تُنشأ → أداة المسح، رسالة الإيقاف',
        'turns': [{'text': P1}, {'text': 'الغي', 'gap': 5}],
        'expect': {'1': {
            'tools': [{'name': 'qurtoba_clear_pending_transfers', 'must': True}],
            'no_records': True, 'tool_texts_contain': ['تم الإيقاف'], 'reply': 'silent', 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'V5', 'title': 'v2: مبلغ بالحروف → إنشاء صامت بدون موديل',
        'turns': [{'text': f'{P1}\nخمسين الف'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 50000}], 'reply': 'silent', 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'V6', 'title': 'v2: كلمة المبلغ ملزوقة بالرقم (حادثة 5 سبتمبر) → إنشاء صحيح',
        'turns': [{'text': f'{P1}\n*مبلغ15.100مصري*'}, {'text': '0 10 98765432', 'gap': 0}, {'text': '٢٠٢٠٠', 'gap': 0}],
        'expect': {'final': {
            'creates': [{'account': P1, 'value': 15100}, {'account': P2, 'value': 20200}],
            'no_creates': [{'account': P1, 'value': 20200}], 'reply': 'silent', 'forbid': NARRATION_FORBID,
        }},
    },
    {
        'id': 'V7', 'title': 'v2: رقم مع اسم ملزوق برقم → سؤال موجّه بدل «المبلغ؟»',
        'turns': [{'text': f'{P1}\nعبدالله15100'}],
        'expect': {'final': {'no_records': True, 'contains': ['هو 15,100'], 'reply': 'question', 'quoted_on': [0], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'V8', 'title': 'v2: انستاباي → رسالة غير مدعوم، بدون إنشاء',
        'turns': [{'text': f'انستاباي {P1} 500'}],
        'expect': {'final': {'no_records': True, 'contains': ['انستاباي'], 'reply': 'one_message', 'quoted_on': [0], 'forbid': NARRATION_FORBID}},
    },
    {
        'id': 'V9', 'title': 'v2: نص حر → الموديل الصغير فقط (بدون أدوات مال)',
        'turns': [{'text': 'عايز اعرف ليه الرصيد زاد كده من غير ما احول حاجه'}],
        'expect': {'final': {
            'no_records': True,
            'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': False}],
            'forbid': NARRATION_FORBID,
        }},
    },
]


# ── X. adversarial — try to make the money path do the WRONG thing ──────────
P4 = '01011061657'
P5 = '01275362968'
SAFE = NARRATION_FORBID + ['Done', 'Output empty']
SCENARIOS += [
    {   # amount arrives BEFORE its number, twice, interleaved with names
        'id': 'X1', 'title': 'x: مبلغ قبل الرقم مرتين مع أسماء بينهم',
        'turns': [{'text': '500'}, {'text': 'طارق', 'gap': 0}, {'text': P1, 'gap': 0}, {'text': '600', 'gap': 0}, {'text': 'جمال', 'gap': 0}, {'text': P2, 'gap': 0}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 500}, {'account': P2, 'value': 600}],
                             'no_creates': [{'account': P1, 'value': 600}, {'account': P2, 'value': 500}], 'forbid': SAFE}},
    },
    {   # 4 split pairs in one second → over the limit → nothing created, resend asked
        'id': 'X2', 'title': 'x: 4 عمليات مقسومة في نفس الثانية → لا تنفيذ، طلب إعادة',
        'turns': [{'text': P1}, {'text': P2, 'gap': 0}, {'text': P3, 'gap': 0}, {'text': P4, 'gap': 0},
                  {'text': '100', 'gap': 0}, {'text': '200', 'gap': 0}, {'text': '300', 'gap': 0}, {'text': '400', 'gap': 0}],
        'expect': {'final': {'no_records': True, 'contains_any': ['رسالة واحدة', 'كل رقم ومبلغه', 'المطابقة'], 'forbid': SAFE}},
    },
    {   # two amounts, one number → never two transfers from one number, never the wrong one
        'id': 'X3', 'title': 'x: رقم واحد ومبلغين → سؤال، لا تخمين',
        'turns': [{'text': f'{P1}\n500\n700'}],
        'expect': {'final': {'no_creates': [{'account': P1, 'value': 700}], 'forbid': SAFE}},
    },
    {   # zero / negative / fraction amounts must never become transfers
        'id': 'X4', 'title': 'x: صفر وسالب وكسر → لا تحويل',
        'turns': [{'text': f'{P1}\n0'}, {'text': f'{P2}\n-500', 'gap': 0}, {'text': f'{P3}\n13.75', 'gap': 0}],
        'expect': {'final': {'no_records': True, 'forbid': SAFE}},
    },
    {   # 12-digit number with an amount → nothing, ask for a correct number; amount must not float
        'id': 'X5', 'title': 'x: رقم 12 خانة مع مبلغ ثم رقم صحيح مجرد → سؤال «حول» لا تنفيذ صامت',
        'turns': [{'text': '011188888099\n900'}, {'text': P1, 'gap': 20}],
        'expect': {'1': {'no_records': True, 'contains_any': ['حول', 'المبلغ'], 'forbid': SAFE}},
    },
    {   # «حول» with nothing pending must not create anything
        'id': 'X6', 'title': 'x: «حول» بدون سؤال معلق → لا شيء',
        'turns': [{'text': 'حول'}],
        'expect': {'final': {'no_records': True, 'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': False}], 'forbid': SAFE}},
    },
    {   # «أيوة» with nothing pending must not create anything
        'id': 'X7', 'title': 'x: «أيوة» بدون سؤال معلق → لا شيء',
        'turns': [{'text': 'أيوة'}],
        'expect': {'final': {'no_records': True, 'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': False}], 'forbid': SAFE}},
    },
    {   # two rejected numbers, then one bare number → ambiguous → must ask, never guess
        'id': 'X8', 'title': 'x: رقمين غلط ثم رقم مجرد → غموض → سؤال لا تنفيذ',
        'turns': [{'text': '0106013464\n2000'}, {'text': '0100600100\n300', 'gap': 0}, {'text': P1, 'gap': 25}],
        'expect': {'1': {'no_records': True, 'contains_any': ['المبلغ', 'حول'], 'forbid': SAFE}},
    },
    {   # cancel word inside the burst: nothing after it should be created blindly? (office: cancel = alert / clear)
        'id': 'X9', 'title': 'x: «الغي» وسط دفعة لم تُنشأ',
        'turns': [{'text': P1}, {'text': 'الغي', 'gap': 3}],
        'expect': {'1': {'no_records': True, 'forbid': SAFE}},
    },
    {   # same burst twice → repeats held, then «لا» → nothing extra created
        'id': 'X10', 'title': 'x: نفس الدفعة مرتين ثم «لا» → لا تكرار',
        'turns': [{'text': f'{P1}\n500'}, {'text': f'{P2}\n600', 'gap': 0},
                  {'text': f'{P1}\n500', 'gap': 70}, {'text': f'{P2}\n600', 'gap': 0}, {'text': 'لا', 'gap': 20}],
        'expect': {'4': {'no_records': True, 'tools': [{'name': 'qurtoba_confirm_pending_repeats', 'must': False}], 'forbid': SAFE}},
    },
    {   # same burst twice then «اها كرر الكل» → exactly 2 more
        'id': 'X11', 'title': 'x: نفس الدفعة مرتين ثم «اها كرر الكل» → تكرار الاثنين',
        'turns': [{'text': f'{P1}\n500'}, {'text': f'{P2}\n600', 'gap': 0},
                  {'text': f'{P1}\n500', 'gap': 70}, {'text': f'{P2}\n600', 'gap': 0}, {'text': 'اها كرر الكل', 'gap': 20}],
        'expect': {'4': {'creates': [{'account': P1, 'value': 500}, {'account': P2, 'value': 600}], 'forbid': SAFE}},
    },
    {   # high value, then customer sends «تأكيد» for a DIFFERENT number → must not execute the held one
        'id': 'X12', 'title': 'x: مبلغ كبير محجوز ثم «تأكيد» مقتبس على رسالة أخرى',
        'turns': [{'text': f'{P1}\n150000'}, {'text': f'{P2}\n500', 'gap': 30}, {'text': 'تأكيد', 'gap': 20, 'reply_to': 1}],
        'expect': {'1': {'creates': [{'account': P2, 'value': 500}]}, '2': {'no_creates': [{'account': P1, 'value': 150000}], 'no_records': True, 'forbid': SAFE}},
    },
    {   # answer to an old question after the window expired → a bare amount is an orphan, not the old number's amount
        'id': 'X13', 'title': 'x: رد بعد انتهاء النافذة (8 دقائق) → لا ربط بالرقم القديم',
        'turns': [{'text': P1}, {'text': '700', 'gap': 1200}],
        'expect': {'1': {'no_creates': [{'account': P1, 'value': 700}], 'forbid': SAFE}},
    },
    {   # arabic-indic digits + spaces + country code + thousands dot, all in one burst
        'id': 'X14', 'title': 'x: أرقام هندية ومسافات وكود دولة ونقطة آلاف',
        'turns': [{'text': '٠١٠١٢٣٤٥٦٧٨\n١٫٥٠٠'}, {'text': '+20 109 876 5432\n2.500', 'gap': 0}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 1500}, {'account': P2, 'value': 2500}], 'forbid': SAFE}},
    },
    {   # number written twice in the same message with one amount → one transfer only
        'id': 'X15', 'title': 'x: نفس الرقم مكرر في رسالة واحدة مع مبلغ → عملية واحدة',
        'turns': [{'text': f'{P1}\n{P1}\n800'}],
        'expect': {'final': {'records_count': {'account': P1, 'value': 800, 'count': 1}, 'forbid': SAFE}},
    },
    {   # a phone-looking amount (10 digits) and an amount-looking phone
        'id': 'X16', 'title': 'x: مبلغ ضخم بيشبه رقم (9 خانات) → لا يُقرأ كرقم',
        'turns': [{'text': f'{P1}\n123456789'}],
        'expect': {'final': {'no_creates': [{'account': '0123456789', 'value': None}], 'forbid': SAFE}},
    },
    {   # «لكل رقم» with three numbers → three creates of the same amount
        'id': 'X17', 'title': 'x: ثلاث أرقام و«500 لكل رقم» → ثلاث عمليات',
        'turns': [{'text': f'{P1}\n{P2}\n{P3}\n500 لكل رقم'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 500}, {'account': P2, 'value': 500}, {'account': P3, 'value': 500}], 'forbid': SAFE}},
    },
    {   # «قسم» → nothing created, human alerted
        'id': 'X18', 'title': 'x: «قسم 1500 على الأرقام» → لا تنفيذ، تنبيه بشري',
        'turns': [{'text': f'{P1}\n{P2}\n{P3}\nقسم 1500 عليهم'}],
        'expect': {'final': {'no_records': True, 'tools': [{'name': 'alert_qurtoba_human', 'must': True}], 'forbid': SAFE}},
    },
    {   # greeting + transfer + balance in one burst → transfer created, balance answered, greeting not a transfer
        'id': 'X19', 'title': 'x: تحية + تحويل + سؤال رصيد في دفعة واحدة',
        'turns': [{'text': 'السلام عليكم'}, {'text': f'{P1}\n500', 'gap': 0}, {'text': 'وحسابي كام', 'gap': 0}],
        'expect': {'final': {'records_count': {'account': P1, 'value': 500, 'count': 1},
                             'tools': [{'name': 'qurtoba_send_customer_balance_to_chat', 'must': True}], 'forbid': SAFE}},
    },
    {   # a sentence with a number in it is not a transfer
        'id': 'X20', 'title': 'x: جملة فيها رقم مش تحويل',
        'turns': [{'text': f'انا بعت لـ {P1} امبارح 500 وصلت؟'}],
        'expect': {'final': {'no_records': True, 'tools': [{'name': 'qurtoba_check_transaction_status', 'must': True}], 'forbid': SAFE}},
    },
    {   # instapay with a valid phone and amount → never a cash transfer
        'id': 'X21', 'title': 'x: انستاباي مع رقم صحيح ومبلغ → لا تنفيذ',
        'turns': [{'text': f'انستا باي\n{P1}\n500'}],
        'expect': {'final': {'no_records': True, 'contains_any': ['انستاباي', 'غير مدعوم'], 'forbid': SAFE}},
    },
    {   # voice message with a number → never executed
        'id': 'X22', 'title': 'x: رسالة صوتية فيها رقم ومبلغ → لا تنفيذ',
        'turns': [{'text': f'حول لرقم {P1} خمسمية', 'type': 'audio'}],
        'expect': {'final': {'no_records': True, 'forbid': SAFE}},
    },
    {   # the same amount answer arrives for a number, then the number again with a different amount
        'id': 'X23', 'title': 'x: رقم ثم مبلغ (إجابة) ثم نفس الرقم بمبلغ مختلف → عمليتان مختلفتان',
        'turns': [{'text': P1}, {'text': '300', 'gap': 15}, {'text': f'{P1}\n450', 'gap': 70}],
        'expect': {'1': {'creates': [{'account': P1, 'value': 300}]}, '2': {'creates': [{'account': P1, 'value': 450}], 'forbid': SAFE}},
    },
    {   # bad number then a NEW complete transfer then a bare number → the bare number is NOT the correction
        'id': 'X24', 'title': 'x: رقم غلط ثم تحويل جديد كامل ثم رقم مجرد → لا تصحيح متأخر',
        'turns': [{'text': '0106013464\n2000'}, {'text': f'{P2}\n700', 'gap': 30}, {'text': P1, 'gap': 30}],
        'expect': {'1': {'creates': [{'account': P2, 'value': 700}]}, '2': {'no_creates': [{'account': P1, 'value': 2000}], 'no_records': True, 'forbid': SAFE}},
    },
]

SCENARIOS += [
    {   # spelled amount over two numbers — meaning, the model creates it
        'id': 'X25', 'title': 'x: رقمين و«الفين لكل رقم» → الموديل يفهم وينشئ الاثنين',
        'turns': [{'text': f'{P1}\n{P2}\n\nالفين لكل رقم'}],
        'expect': {'final': {'creates': [{'account': P1, 'value': 2000}, {'account': P2, 'value': 2000}], 'forbid': SAFE}},
    },
]


# ── Y. attack the word lists: same meaning, other words ────────────────────
ACC = 'فورى,6081844,أمان,970604'
SCENARIOS += [
    # multi-number wording
    {'id': 'Y1', 'title': 'y: «الفين على الاتنين»', 'turns': [{'text': f'{P1}\n{P2}\nالفين على الاتنين'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 2000}, {'account': P2, 'value': 2000}], 'forbid': SAFE}}},
    {'id': 'Y2', 'title': 'y: «كل واحد ياخد 500»', 'turns': [{'text': f'{P1}\n{P2}\nكل واحد ياخد 500'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}, {'account': P2, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Y3', 'title': 'y: «ابعت 700 للرقمين دول»', 'turns': [{'text': f'ابعت 700 للرقمين دول\n{P1}\n{P2}'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 700}, {'account': P2, 'value': 700}], 'forbid': SAFE}}},
    {'id': 'Y4', 'title': 'y: «نص نص» = تقسيم → لا تنفيذ، تنبيه', 'turns': [{'text': f'{P1}\n{P2}\n1000 نص نص'}],
     'expect': {'final': {'no_records': True, 'tools': [{'name': 'alert_qurtoba_human', 'must': True}], 'forbid': SAFE}}},
    {'id': 'Y5', 'title': 'y: «وزعهم بالتساوي» = تقسيم → لا تنفيذ', 'turns': [{'text': f'{P1}\n{P2}\n1000 وزعهم بالتساوي'}],
     'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    # non-cash wording
    {'id': 'Y6', 'title': 'y: «حول 500 على الفوري بتاعي»', 'setup': {'accounts': ACC}, 'turns': [{'text': 'حول 500 على الفوري بتاعي'}],
     'expect': {'final': {'creates': [{'account': '6081844', 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Y7', 'title': 'y: «فوررى 300» (typo)', 'setup': {'accounts': ACC}, 'turns': [{'text': 'فوررى 300'}],
     'expect': {'final': {'creates': [{'account': '6081844', 'value': 300}], 'forbid': SAFE}}},
    {'id': 'Y8', 'title': 'y: «Fawry 250»', 'setup': {'accounts': ACC}, 'turns': [{'text': 'Fawry 250'}],
     'expect': {'final': {'creates': [{'account': '6081844', 'value': 250}], 'forbid': SAFE}}},
    {'id': 'Y9', 'title': 'y: «امان 400» بدون همزة', 'setup': {'accounts': ACC}, 'turns': [{'text': 'امان 400'}],
     'expect': {'final': {'creates': [{'account': '970604', 'value': 400}], 'forbid': SAFE}}},
    {'id': 'Y10', 'title': 'y: «6081844 ⏎ 900» حساب فوري بدون كلمة النوع', 'setup': {'accounts': ACC}, 'turns': [{'text': '6081844\n900'}],
     'expect': {'final': {'creates': [{'account': '6081844', 'value': 900}], 'no_records_type': 'كاش', 'forbid': SAFE}}},
    # instapay wording
    {'id': 'Y11', 'title': 'y: «على الانستا» → رفض', 'turns': [{'text': f'{P1}\n500 على الانستا'}],
     'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Y12', 'title': 'y: «insta pay» → رفض', 'turns': [{'text': f'insta pay {P1} 500'}],
     'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Y13', 'title': 'y: «IPN» → رفض', 'turns': [{'text': f'IPN\n{P1}\n500'}],
     'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    # yes / no wording
    {'id': 'Y14', 'title': 'y: تكرار ثم «تمام يا معلم اعملها»', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P1}\n500', 'gap': 70}, {'text': 'تمام يا معلم اعملها', 'gap': 20}],
     'expect': {'2': {'records_count': {'account': P1, 'value': 500, 'count': 1}, 'forbid': SAFE}}},
    {'id': 'Y15', 'title': 'y: تكرار ثم «ماشي نفذها ربنا يخليك»', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P1}\n500', 'gap': 70}, {'text': 'ماشي نفذها ربنا يخليك', 'gap': 20}],
     'expect': {'2': {'records_count': {'account': P1, 'value': 500, 'count': 1}, 'forbid': SAFE}}},
    {'id': 'Y16', 'title': 'y: تكرار ثم «لا مش عايز اكررها» → لا تنفيذ', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P1}\n500', 'gap': 70}, {'text': 'لا مش عايز اكررها', 'gap': 20}],
     'expect': {'2': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Y17', 'title': 'y: تكرار ثم «انسى» → لا تنفيذ', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P1}\n500', 'gap': 70}, {'text': 'انسى', 'gap': 20}],
     'expect': {'2': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Y18', 'title': 'y: تكرار ثم «ايوه بس خليها 300» → لا تكرار الـ500', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P1}\n500', 'gap': 70}, {'text': 'ايوه بس خليها 300', 'gap': 20}],
     'expect': {'2': {'no_creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Y19', 'title': 'y: مبلغ كبير ثم «أكيد طبعا نفذ»', 'turns': [{'text': f'{P1}\n150000'}, {'text': 'أكيد طبعا نفذ', 'gap': 30}],
     'expect': {'1': {'creates': [{'account': P1, 'value': 150000}], 'forbid': SAFE}}},
    {'id': 'Y20', 'title': 'y: مبلغ كبير ثم «لأ خلاص كفاية» → لا تنفيذ', 'turns': [{'text': f'{P1}\n150000'}, {'text': 'لأ خلاص كفاية', 'gap': 30}],
     'expect': {'1': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Y21', 'title': 'y: تصحيح رقم ثم «yes please» → تنفيذ', 'turns': [{'text': '0106013464\n2000'}, {'text': P1, 'gap': 20}, {'text': 'yes please', 'gap': 15}],
     'expect': {'2': {'creates': [{'account': P1, 'value': 2000}], 'forbid': SAFE}}},
    # question without a mark / order with a mark
    {'id': 'Y22', 'title': 'y: سؤال بلا علامة «01… 500 ده اتحول» → لا تنفيذ', 'turns': [{'text': f'{P1} 500 ده اتحول ولا لسه'}],
     'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Y23', 'title': 'y: أمر بعلامة «01… ⏎ 500 ⏎ ممكن؟» → ينفذ فوراً', 'turns': [{'text': f'{P1}\n500\nممكن؟'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    # fee / reference wording
    {'id': 'Y24', 'title': 'y: «الرسوم عليا» → لا مبلغ ثاني', 'turns': [{'text': f'{P1}\n5000\nالرسوم عليا 15'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 5000}], 'no_creates': [{'account': P1, 'value': 15}], 'forbid': SAFE}}},
    {'id': 'Y25', 'title': 'y: «اتحمل الـ 20 بتوع الخدمة» → لا مبلغ ثاني', 'turns': [{'text': f'{P1}\n5000\nاتحمل الـ 20 بتوع الخدمة'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 5000}], 'no_creates': [{'account': P1, 'value': 20}], 'forbid': SAFE}}},
    {'id': 'Y26', 'title': 'y: «كود 4444» → ليس مبلغ', 'turns': [{'text': f'{P1}\n5000\nكود 4444'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 5000}], 'no_creates': [{'account': P1, 'value': 4444}], 'forbid': SAFE}}},
    # cancel wording
    {'id': 'Y27', 'title': 'y: «خلاص متبعتش» بعد رقم بلا مبلغ → لا تنفيذ', 'turns': [{'text': P1}, {'text': 'خلاص متبعتش', 'gap': 5}, {'text': '700', 'gap': 10}],
     'expect': {'2': {'no_creates': [{'account': P1, 'value': 700}], 'forbid': SAFE}}},
    {'id': 'Y28', 'title': 'y: «سيبك منها» بعد رقم بلا مبلغ → لا تنفيذ', 'turns': [{'text': P1}, {'text': 'سيبك منها', 'gap': 5}, {'text': '700', 'gap': 10}],
     'expect': {'2': {'no_creates': [{'account': P1, 'value': 700}], 'forbid': SAFE}}},
    # reroute wording
    {'id': 'Y29', 'title': 'y: بعد إشعار المحفظة «حطها على 01… بدل الأول»', 'setup': {'prior_create': {'account': P1, 'value': 5000}, 'system_notice': 'no_wallet'},
     'turns': [{'text': f'حطها على {P2} بدل الأول'}],
     'expect': {'final': {'creates': [{'account': P2, 'value': 5000}], 'forbid': SAFE}}},
    {'id': 'Y30', 'title': 'y: بعد الإشعار رقم جديد بمبلغ مختلف → المبلغ المكتوب', 'setup': {'prior_create': {'account': P1, 'value': 5000}, 'system_notice': 'no_wallet'},
     'turns': [{'text': f'{P2}\n3000'}],
     'expect': {'final': {'creates': [{'account': P2, 'value': 3000}], 'no_creates': [{'account': P2, 'value': 5000}], 'forbid': SAFE}}},
]


# ── Z. full-system attack (report only) ────────────────────────────────────
ACC2 = 'فورى,6081844,فورى,6099999,أمان,970604'
SCENARIOS += [
    {'id': 'Z1', 'title': 'z: رقم ثم اسم ثم المبلغ بعد 50 ثانية', 'turns': [{'text': P1}, {'text': 'طارق', 'gap': 0}, {'text': '500', 'gap': 50}],
     'expect': {'2': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z2', 'title': 'z: رقم، تحية، ثم المبلغ بعد 20 ثانية', 'turns': [{'text': P1}, {'text': 'السلام عليكم', 'gap': 0}, {'text': '700', 'gap': 20}],
     'expect': {'2': {'creates': [{'account': P1, 'value': 700}], 'forbid': SAFE}}},
    {'id': 'Z3', 'title': 'z: رقمين ثم «500 و 600» في رسالة واحدة', 'turns': [{'text': P1}, {'text': P2, 'gap': 0}, {'text': '500 و 600', 'gap': 0}],
     'expect': {'final': {'no_creates': [{'account': P1, 'value': 600}, {'account': P2, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z4', 'title': 'z: «500ج»', 'turns': [{'text': f'{P1}\n500ج'}], 'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z5', 'title': 'z: رقم بشرطات «010-1234-5678»', 'turns': [{'text': '010-1234-5678\n500'}], 'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z6', 'title': 'z: «+2 01012345678»', 'turns': [{'text': '+2 01012345678\n500'}], 'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z8', 'title': 'z: «1,5» فاصلة عشرية', 'turns': [{'text': f'{P1}\n1,5'}], 'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z9', 'title': 'z: «5.000.000» → احتجاز مبلغ كبير', 'turns': [{'text': f'{P1}\n5.000.000'}],
     'expect': {'final': {'no_records': True, 'contains_any': ['مبلغ كبير', 'تأكيد'], 'forbid': SAFE}}},
    {'id': 'Z11', 'title': 'z: «الف» وحدها = 1000', 'turns': [{'text': f'{P1}\nالف'}], 'expect': {'final': {'creates': [{'account': P1, 'value': 1000}], 'forbid': SAFE}}},
    {'id': 'Z12', 'title': 'z: «نص مليون» → احتجاز', 'turns': [{'text': f'{P1}\nنص مليون'}], 'expect': {'final': {'no_creates': [{'account': P1, 'value': 500}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z13', 'title': 'z: «الغي» داخل نفس رسالة الرقم والمبلغ', 'turns': [{'text': f'{P1}\n500\nالغي'}], 'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z14', 'title': 'z: تنفيذ ثم «لا مش ده» → تنبيه بشري', 'turns': [{'text': f'{P1}\n500'}, {'text': 'لا مش ده', 'gap': 8}],
     'expect': {'1': {'tools': [{'name': 'alert_qurtoba_human', 'must': True}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z15', 'title': 'z: نفس الرقم بمبلغين مختلفين في دفعة → عمليتان', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P1}\n700', 'gap': 0}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}, {'account': P1, 'value': 700}], 'forbid': SAFE}}},
    {'id': 'Z17', 'title': 'z: رسالة واحدة فيها زوجان', 'turns': [{'text': f'{P1}\n500\n{P2}\n600'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}, {'account': P2, 'value': 600}], 'forbid': SAFE}}},
    {'id': 'Z18', 'title': 'z: رقمين ومبلغ في سطر واحد بلا كلمات', 'turns': [{'text': f'{P1} 500 {P2}'}],
     'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z19', 'title': 'z: رقم مبلغ رقم مبلغ متداخلة (≤3) → تنفيذ', 'turns': [{'text': P1}, {'text': '600', 'gap': 0}, {'text': P2, 'gap': 0}, {'text': '500', 'gap': 0}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 600}, {'account': P2, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z20', 'title': 'z: 5 عمليات مقسومة في نفس الثانية → إعادة إرسال', 'turns': [{'text': P1}, {'text': '100', 'gap': 0}, {'text': P2, 'gap': 0}, {'text': '200', 'gap': 0}, {'text': P3, 'gap': 0}, {'text': '300', 'gap': 0}, {'text': P4, 'gap': 0}, {'text': '400', 'gap': 0}, {'text': P5, 'gap': 0}, {'text': '500', 'gap': 0}],
     'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z22', 'title': 'z: صورة بلا كلام (إيصال؟)', 'turns': [{'text': 'صورة', 'type': 'image'}], 'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z23', 'title': 'z: صوتية «حسابي كام» → الرصيد', 'turns': [{'text': 'حسابي كام يا باشا', 'type': 'audio'}],
     'expect': {'final': {'tools': [{'name': 'qurtoba_send_customer_balance_to_chat', 'must': True}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z25', 'title': 'z: «الرصيد» وحدها', 'turns': [{'text': 'الرصيد'}], 'expect': {'final': {'tools': [{'name': 'qurtoba_send_customer_balance_to_chat', 'must': True}], 'forbid': SAFE}}},
    {'id': 'Z26', 'title': 'z: «كشف حساب امبارح»', 'turns': [{'text': 'كشف حساب امبارح'}], 'expect': {'final': {'tools': [{'name': 'qurtoba_get_customer_daily_transactions', 'must': True}], 'forbid': SAFE}}},
    {'id': 'Z28', 'title': 'z: «اللي اتنفذ النهارده كام؟»', 'turns': [{'text': f'{P1}\n500'}, {'text': 'اللي اتنفذ النهارده كام؟', 'gap': 30}],
     'expect': {'1': {'tools': [{'name': 'qurtoba_get_customer_daily_transactions', 'must': True}], 'forbid': SAFE}}},
    {'id': 'Z29', 'title': 'z: «الايصال فين» بعد تحويل', 'turns': [{'text': f'{P1}\n500'}, {'text': 'الايصال فين', 'gap': 40}],
     'expect': {'1': {'tools': [{'name': 'qurtoba_check_transaction_status', 'must': True}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z30', 'title': 'z: «تم» مقتبس على الرقم', 'turns': [{'text': f'{P1}\n500'}, {'text': 'تم', 'gap': 40, 'reply_to': 0}],
     'expect': {'1': {'tools': [{'name': 'qurtoba_check_transaction_status', 'must': True}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z31', 'title': 'z: «الغي» مقتبس على تحويل منفذ → تنبيه', 'turns': [{'text': f'{P1}\n500'}, {'text': 'الغي', 'gap': 40, 'reply_to': 0}],
     'expect': {'1': {'tools': [{'name': 'alert_qurtoba_human', 'must': True}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z32', 'title': 'z: «الغي الـ 500» بعد تحويلين', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P2}\n600', 'gap': 0}, {'text': 'الغي الـ 500', 'gap': 30}],
     'expect': {'2': {'tools': [{'name': 'alert_qurtoba_human', 'must': True}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z34', 'title': 'z: بعد إشعار المحفظة رقم جديد بعد 5 دقائق', 'setup': {'prior_create': {'account': P1, 'value': 5000}, 'system_notice': 'no_wallet'},
     'turns': [{'text': P2, 'gap': 300}], 'expect': {'final': {'creates': [{'account': P2, 'value': 5000}], 'forbid': SAFE}}},
    {'id': 'Z35', 'title': 'z: مبلغ كبير ثم «حول»', 'turns': [{'text': f'{P1}\n150000'}, {'text': 'حول', 'gap': 20}],
     'expect': {'1': {'creates': [{'account': P1, 'value': 150000}], 'forbid': SAFE}}},
    {'id': 'Z36', 'title': 'z: مبلغ كبير ثم إعادة إرسال نفس الرسالة → لا سؤال مكرر', 'turns': [{'text': f'{P1}\n150000'}, {'text': f'{P1}\n150000', 'gap': 30}],
     'expect': {'1': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z37', 'title': 'z: «انا مش فاهم» بعد سؤالنا', 'turns': [{'text': P1}, {'text': 'انا مش فاهم', 'gap': 20}],
     'expect': {'1': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z38', 'title': 'z: «؟» مقتبس على سؤالنا', 'turns': [{'text': P1}, {'text': '؟', 'gap': 20, 'reply_to': 0}],
     'expect': {'1': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z39', 'title': 'z: «ازيك عامل ايه» + رقم ومبلغ في نفس الدفعة', 'turns': [{'text': 'ازيك عامل ايه'}, {'text': f'{P1}\n500', 'gap': 0}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z42', 'title': 'z: «فوري 6099999 500» مع حسابين فوري → الحساب المذكور', 'setup': {'accounts': ACC2}, 'turns': [{'text': 'فوري 6099999 500'}],
     'expect': {'final': {'creates': [{'account': '6099999', 'value': 500}], 'no_creates': [{'account': '6081844', 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z43', 'title': 'z: «فوري 700» مع حسابين → يسأل، ثم «الاول» ينفذ', 'setup': {'accounts': ACC2}, 'turns': [{'text': 'فوري 700'}, {'text': 'الاول', 'gap': 20}],
     'expect': {'0': {'no_records': True}, '1': {'creates': [{'account': '6081844', 'value': 700}], 'forbid': SAFE}}},
    {'id': 'Z44', 'title': 'z: «طاير 300» بلا حساب مسجل', 'setup': {'accounts': ACC2}, 'turns': [{'text': 'طاير 300'}],
     'expect': {'final': {'no_records': True, 'contains_any': ['لا يوجد حساب', 'طاير'], 'forbid': SAFE}}},
    {'id': 'Z45', 'title': 'z: «500 فوري و 300 كاش على 01…» مختلط', 'setup': {'accounts': ACC}, 'turns': [{'text': f'500 فوري و 300 كاش على {P1}'}],
     'expect': {'final': {'creates': [{'account': '6081844', 'value': 500}, {'account': P1, 'value': 300}], 'forbid': SAFE}}},
    {'id': 'Z46', 'title': 'z: «دفعت 500 سداد» بلا صورة → لا تحويل', 'turns': [{'text': 'دفعت 500 سداد'}], 'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z48', 'title': 'z: «محتاج 500» مع حساب واحد مسجل → فوري', 'setup': {'accounts': 'فورى,6081844'}, 'turns': [{'text': 'محتاج 500'}],
     'expect': {'final': {'creates': [{'account': '6081844', 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z49', 'title': 'z: تحية ورقم ومبلغ وشكر في رسالة واحدة', 'turns': [{'text': f'صباح الخير\n{P1}\n500\nشكرا'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z50', 'title': 'z: أرقام هندية بمسافات', 'turns': [{'text': '٠١٠ ١٢٣٤ ٥٦٧٨\n٥٠٠'}], 'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z51', 'title': 'z: «كاش(10)» مكتوبة في الرسالة', 'turns': [{'text': f'{P1}\n500\nكاش(10)'}], 'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'no_creates': [{'account': P1, 'value': 10}], 'forbid': SAFE}}},
    {'id': 'Z53', 'title': 'z: «مصاريف خدمة 10» في الرسالة → ليس مبلغ', 'turns': [{'text': f'{P1}\n500\nمصاريف خدمة 10'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'no_creates': [{'account': P1, 'value': 10}], 'forbid': SAFE}}},
    {'id': 'Z54', 'title': 'z: «تحصيل 500 من 01…» → لا تحويل كاش', 'turns': [{'text': f'تحصيل 500 من {P1}'}], 'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z55', 'title': 'z: «سداد 500 على 01…» → لا تحويل كاش', 'turns': [{'text': f'سداد 500 على {P1}'}], 'expect': {'final': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z56', 'title': 'z: «ارجع لي الـ 500» → تنبيه بشري', 'turns': [{'text': f'{P1}\n500'}, {'text': 'ارجع لي الـ 500', 'gap': 40}],
     'expect': {'1': {'tools': [{'name': 'alert_qurtoba_human', 'must': True}], 'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z57', 'title': 'z: «خليها 700 بدل 500» بعد التنفيذ → تنبيه لا تعديل صامت', 'turns': [{'text': f'{P1}\n500'}, {'text': 'خليها 700 بدل 500', 'gap': 40}],
     'expect': {'1': {'no_creates': [{'account': P1, 'value': 700}], 'forbid': SAFE}}},
    {'id': 'Z58', 'title': 'z: «01… ⏎ 500 ⏎ تم» → تنفيذ بلا فحص حالة', 'turns': [{'text': f'{P1}\n500\nتم'}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z60', 'title': 'z: نفس التحويل 3 مرات على 3 دقائق', 'turns': [{'text': f'{P1}\n500'}, {'text': f'{P1}\n500', 'gap': 90}, {'text': f'{P1}\n500', 'gap': 90}],
     'expect': {'2': {'no_records': True, 'forbid': SAFE}}},
    {'id': 'Z61', 'title': 'z: 12 عملية كاملة في دفعة واحدة', 'turns': [{'text': f'0101234567{i}\n{100*(i+1)}', 'gap': 0} for i in range(12)][:1] + [{'text': f'0101234567{i}\n{100*(i+1)}', 'gap': 0} for i in range(1, 12)],
     'expect': {'final': {'records_count': {'account': '01012345670', 'value': 100, 'count': 1}, 'creates': [{'account': '01012345679', 'value': 1000}], 'forbid': SAFE}}},
    {'id': 'Z62', 'title': 'z: رقم ثم «المبلغ 500» بعدها بكلمة', 'turns': [{'text': P1}, {'text': 'المبلغ 500', 'gap': 5}],
     'expect': {'final': {'creates': [{'account': P1, 'value': 500}], 'forbid': SAFE}}},
    {'id': 'Z63', 'title': 'z: «01… ⏎ 500» ثم «نفس الرقم 300» → عملية ثانية', 'turns': [{'text': f'{P1}\n500'}, {'text': 'نفس الرقم 300', 'gap': 30}],
     'expect': {'1': {'creates': [{'account': P1, 'value': 300}], 'forbid': SAFE}}},
    {'id': 'Z64', 'title': 'z: «ابعتلي ارقام الفوري بتاعتي»', 'setup': {'accounts': ACC2}, 'turns': [{'text': 'ابعتلي ارقام الفوري بتاعتي'}],
     'expect': {'final': {'no_records': True, 'contains_any': ['6081844', '6099999'], 'forbid': SAFE}}},
    {'id': 'Z65', 'title': 'z: «عايز اكلم حد» → تنبيه و«لحظة»', 'turns': [{'text': 'عايز اكلم حد من المكتب'}],
     'expect': {'final': {'tools': [{'name': 'alert_qurtoba_human', 'must': True}], 'contains_any': ['لحظة'], 'forbid': SAFE}}},
    {'id': 'Z66', 'title': 'z: «الرقم ده اتحول عليه كام النهارده» 01…', 'turns': [{'text': f'{P1}\n500'}, {'text': f'الرقم {P1} اتحول عليه كام النهارده', 'gap': 30}],
     'expect': {'1': {'no_records': True, 'forbid': SAFE}}},
]
