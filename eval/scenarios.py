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
            'tools': [{'name': 'qurtoba_create_new_transactions_bulk', 'must': False},
                      {'name': 'qurtoba_plan_transactions', 'must': False}],
            'forbid': NARRATION_FORBID,
        }},
    },
]
