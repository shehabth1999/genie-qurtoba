"""The fixed customer-facing Arabic lines the automation sends.

Every line here is the wording the agent prompts already prescribe (core.md REPLY
PROTOCOL / SHARED ROLES, cash & fawry prompts, the static tools), so the customer
sees exactly what they saw before — only the sender changed from a model to code.
Variants are rotated deterministically (by message id) where the prompt said «vary».
"""
from typing import List


def _fmt(value) -> str:
    try:
        return f'{float(value):,.0f}'
    except (TypeError, ValueError):
        return str(value)


# ── courtesy (SHARED ROLES) ───────────────────────────────────────────────────

SALAM: List[str] = [
    'وعليكم السلام ورحمة الله وبركاته 🌹\nتحت أمرك — ابعت الرقم والمبلغ وأنا أنفذ فوراً.',
    'وعليكم السلام 🌹\nأهلاً بيك — ابعت الرقم والمبلغ وهننفذ على طول.',
]
GREETINGS: List[str] = [
    'أهلاً وسهلاً 🌹\nتحت أمرك في أي تحويل — ابعت الرقم والمبلغ.',
    'أهلاً بيك 🌹\nجاهزين — ابعت الرقم والمبلغ وهننفذ على طول.',
]
MORNING: List[str] = [
    'صباح النور 🌹\nتحت أمرك — ابعت الرقم والمبلغ وأنا أنفذ فوراً.',
    'صباح الخير والنور 🌹\nجاهزين لأي تحويل.',
]
EVENING: List[str] = [
    'مساء النور 🌹\nتحت أمرك — ابعت الرقم والمبلغ وأنا أنفذ فوراً.',
    'مساء الخير والنور 🌹\nجاهزين لأي تحويل.',
]
THANKS: List[str] = [
    'العفو، تحت أمرك 🌹',
    'ولا يهمك، تحت أمرك في أي وقت 🌹',
    'العفو يا فندم، في الخدمة دايماً 🌹',
]
WELLBEING: List[str] = [
    'الحمد لله تمام، تحت أمرك 🌹',
    'الحمد لله بخير، تحت أمرك في أي تحويل 🌹',
]
AVAILABLE: List[str] = [
    'شغالين وجاهزين 👌 ابعت الرقم والمبلغ وأنا أنفذ فوراً.',
    'أيوة موجودين وجاهزين 👌 تحت أمرك.',
]
SCOPE = 'أنا متخصص في معاملات قرطبة بس، فمش هقدر أساعدك في ده.'
WAIT = 'لحظة'
# A split request («قسم/وزّع المبلغ على الأرقام») is done by hand at the office. qurtoba_request_split posts an
# internal note mentioning the staff, notifies them like a receipt waiting for review, and sends the customer
# this ONE short line (owner decision 2026-09-13).
SPLIT_RECEIVED = 'تمام ✅ طلب التقسيم وصل وهيتنفذ يدوي.'

# A request that reached us while offline was cancelled on arrival; asking to execute it later gets this line
# (owner decision 2026-09-14). Only a request sent again NOW is executed.
OFFLINE_CANCELLED_OFF_HOURS = 'الطلب ده وصل وإحنا خارج مواعيد العمل واتلغى — لو لسه محتاجه ابعته تاني دلوقتي وهيتنفذ فوراً.'
OFFLINE_CANCELLED_AI_OFF = 'الطلب ده وصل والخدمة متوقفة واتلغى — لو لسه محتاجه ابعته تاني دلوقتي وهيتنفذ فوراً.'
OFFLINE_CANCELLED_PAYMENT_OFF_HOURS = 'الإيصال ده وصل وإحنا خارج مواعيد العمل واتلغى — ابعت صورة الإيصال تاني دلوقتي علشان يتسجل.'
OFFLINE_CANCELLED_PAYMENT_AI_OFF = 'الإيصال ده وصل والخدمة متوقفة واتلغى — ابعت صورة الإيصال تاني دلوقتي علشان يتسجل.'
OFFLINE_CANCELLED_NOT_LINKED = 'الطلب ده وصل قبل ربط حسابك بقرطبة واتلغى — لو لسه محتاجه ابعته تاني دلوقتي وهيتنفذ فوراً.'
OFFLINE_CANCELLED_PAYMENT_NOT_LINKED = 'الإيصال ده وصل قبل ربط حسابك بقرطبة واتلغى — ابعت صورة الإيصال تاني دلوقتي علشان يتسجل.'
PER_NUMBER_QUESTION = 'تقصد {amount} لكل رقم، ولا تقسيمه عليهم؟'
NOT_UNDERSTOOD = 'مش فاهم الرسالة دي — ابعت الرقم في سطر والمبلغ في سطر'
SAY_AGAIN_SIMPLER = 'ابعت الرقم في سطر والمبلغ في سطر تحته، وأنا أنفذ على طول.'

# ── transfers (REPLY PROTOCOL) ────────────────────────────────────────────────

ORPHAN_PHONE = 'المبلغ لـ {phone}؟'
ORPHAN_PHONE_HINT = 'المبلغ لـ {phone} هو {amount}؟'
ORPHAN_AMOUNT = 'الرقم للمبلغ {amount}؟'
LIST_CONFIRM = 'تأكيد: {phone} ← {amount}؟'
LIST_CONFIRM_HEADER = 'الأرقام والمبالغ وصلت كقائمتين — دي المطابقة اللي فهمتها:'
LIST_CONFIRM_TAIL = 'صح؟ (رد بـ«أيوة» وأنفذها، أو ابعت كل رقم ومبلغه في رسالة واحدة)'
UNREADABLE_AMOUNT = 'المبلغ متكتب «{raw}» ومش قادر أقراه — ابعته تاني: الرقم في سطر والمبلغ في سطر بالأرقام بس'
HIGH_VALUE = 'مبلغ كبير — محتاج منك كلمة «تأكيد» على الرسالة دي قبل ما ننفّذه'
BAD_NUMBER = 'الرقم ده مش صحيح — ابعت رقم صحيح 11 رقم'
CORRECTION_CONFIRM = ('الرقم اللي فات كان غلط 🙏\n'
                      'تقصد تحويل {amount} على الرقم ده {phone}؟\n'
                      'لو أيوة ابعت «حول» وننفذها فوراً.')
CORRECTION_DECLINED = 'تمام، مفيش تحويل على الرقم ده.'
QUESTION_CONFIRM = ('الرسالة دي سؤال ولا طلب تحويل؟ 🙏\n'
                    'لو عايز تحويل {amount} على {phone} ابعت «حول» وننفذه فوراً.')
NEITHER_OPTION = 'تمام، يبقى {amount} على {phone}؟'
UNCLEAR_ANSWER = 'رديت بـ«{text}» على «{question}» — قصدك أيوة ولا لأ؟'
UNCLEAR_HV_ANSWER = 'رديت بـ«{text}» على تأكيد الـ{amount} — قصدك نأكد الـ{amount} ولا المبلغ {text} بس؟'
DECLINED = 'تمام، مش هننفذها.'
REPEAT_DECLINED = 'تمام، مش هتتكرر.'
RESEND_FLOOD = ('وصلت رسائل كتير في نفس اللحظة والأرقام والمبالغ في رسائل منفصلة، فترتيبها مش مضمون — '
                'عشان ما يحصلش خطأ في المطابقة ابعت كل رقم ومبلغه في رسالة واحدة، وبحد أقصى 3 تحويلات في المرة.')
VOICE_CASH = 'من فضلك ابعت رقم المحفظة والمبلغ مكتوبين — تحويلات الكاش محتاجة الرقم بالظبط.'
UNSUPPORTED_TYPE = ('خدمة {type} غير مدعومة حالياً. الأنواع المتاحة: كاش (برقم تليفون) / فورى / أمان / طاير. '
                    'على أي نوع تحب تحوّل؟')
INSTAPAY = 'خدمة انستاباي غير مدعومة حالياً. الأنواع المتاحة: كاش (برقم تليفون) / فورى / أمان / طاير.'
TYPE_QUESTION = 'على أي نوع تحب تحوّل؟ كاش (برقم تليفون 11 خانة) / فورى / أمان / طاير.'

# ── fawry / أمان / طاير account guard ────────────────────────────────────────

NO_ACCOUNT_OF_TYPE = 'لا يوجد حساب {type} مسجل لهذا العميل. تواصل مع إدارة قرطبة لإضافة الحساب أولاً.'
NOT_REGISTERED = 'الحساب {account} غير مسجل. الحساب المسجل: {registered}. لإضافة حساب جديد تواصل مع إدارة قرطبة.'
WRONG_TYPE = ('الرقم {account} مسجل كحساب {registered_type} وليس {requested_type}. '
              'للتنفيذ كـ{registered_type} أكّد، أو تواصل مع الإدارة لإضافة حساب {requested_type}.')
WHICH_ACCOUNT = 'أي حساب {type}؟ {options}'
WHICH_ACCOUNT_ANY = 'أي حساب؟ {options}'
NONCASH_AMOUNT_QUESTION = 'المبلغ لـ {type} {account}؟'

# ── status / statement / cancel ───────────────────────────────────────────────

STATUS_NOTHING = 'مفيش تحويلات النهارده لسه.'
STATEMENT_FAILED = 'معلش، مش قادر أطلع كشف الحساب دلوقتي.'
SUBSET_NONE_PENDING = 'كل تحويلات النهارده اتنفذت ✅'
SUBSET_HEADER = 'اللي لسه قيد التنفيذ:'
CANCEL_WHICH = 'أي تحويل تحب تلغيه؟ ابعت الرقم أو المبلغ'
CANCEL_STOPPED = 'تم الإيقاف. تأكد من تفاصيل المعاملة قبل إرسالها — النظام ينفّذ بسرعة.'

# ── off-hours / linkage ───────────────────────────────────────────────────────

OFF_HOURS = 'خارج مواعيد العمل حالياً. ساعات العمل من 9 صباحاً حتى 11:50 مساءً — تحت أمرك في أي وقت خلالها.'

# The off-hours AGENT's refusals (manual switch «وضع خارج مواعيد العمل»). The model sends them through
# the reply tool, quoted on the refused message; the workflow builder bakes them into its prompt from
# here, so the prompt and the fixed fallback above can never disagree about the hours.
WORKING_HOURS = 'مواعيد العمل من 9 صباحاً حتى 11:50 مساءً طوال أيام الأسبوع'
OFF_HOURS_TRANSACTION = (
    'بنعتذر لحضرتك\n\n'
    '*لا يمكن تنفيذ أي معاملات خارج مواعيد العمل*\n\n'
    f'{WORKING_HOURS}\n\n'
    'برجاء إعادة إرسال طلبك خلال مواعيد العمل وسيتم تنفيذه فوراً'
)
OFF_HOURS_PAYMENT = (
    'عذراً، لا يمكن تسجيل السداد الآن خارج مواعيد العمل.\n'
    f'{WORKING_HOURS}.\n'
    'برجاء إعادة إرسال صورة الإيصال خلال مواعيد العمل ليتم تسجيلها.'
)
OFF_HOURS_STATUS = (
    'عذراً، لا يمكن مراجعة حالة التحويلات أو إلغاؤها الآن خارج مواعيد العمل.\n'
    f'{WORKING_HOURS}.\n'
    'تقدر تطلب «كشف حساب» دلوقتي لعرض عمليات اليوم.'
)
OFF_HOURS_WHEN_OPEN = f'احنا دلوقتي خارج مواعيد العمل. {WORKING_HOURS}.'
NOT_LINKED = ('بنعتذر ل حضرتك\n\nحسابك غير مربوط بعميل قرطبة\n\n'
              'برجاء التواصل مع إدارة قرطبة لربط حسابك أو إضافة حساب لك')


def pick(variants: List[str], seed: str) -> str:
    """Deterministic «vary the wording»: the same message id always gets the same line."""
    if not variants:
        return ''
    h = sum(ord(c) for c in str(seed or ''))
    return variants[h % len(variants)]
