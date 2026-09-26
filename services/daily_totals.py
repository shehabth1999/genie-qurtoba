"""
Per-phone daily totals for the end-of-day WhatsApp reminder.

One QurtobaCustomer can be reached on several WhatsApp numbers, so "how much
went out today" has two different answers: the total for the NUMBER that is
being messaged, and the total for the CUSTOMER's whole account. The reminder
shows both, and this module is the single place either is computed — the
template properties on Partner and the dispatch task both read from here, so
the audience and the numbers in the message can never disagree.

Counting rules are deliberately identical to the daily statement tool
(qurtoba.tools.reports): the reporting day, attribution by the `partner` FK,
and amounts of zero or less withheld.
"""
import datetime
from typing import Optional

from django.db.models import Count, Q, Sum
from django.utils import timezone


# The reminder fires just after midnight Cairo. Celery beat runs on UTC
# (CELERY_TIMEZONE='UTC') while the business day is Africa/Cairo, and Egypt
# observes DST — so one fixed UTC crontab lands at 00:10 Cairo in summer but
# 23:10 Cairo in winter, i.e. on either side of midnight. Rather than chase
# that, the reporting day is defined as "the business day that has just
# ended", which resolves correctly on both sides:
#
#   fired 00:10 Cairo (summer) → hour 0  < 12 → report yesterday  ✓
#   fired 23:10 Cairo (winter) → hour 23 ≥ 12 → report today      ✓
#
# Both name the day that is closing. The cutoff sits at noon so any plausible
# scheduling drift stays on the correct side of it.
_JUST_ENDED_CUTOFF_HOUR = 12


def reporting_day(now=None) -> datetime.date:
    """The business day being reported on — the one that has just ended.

    Pass `now` (an aware datetime) to compute it for a specific moment; the
    default reads the clock. Always evaluated in Africa/Cairo, never UTC.
    """
    local = timezone.localtime(now) if now is not None else timezone.localtime()
    if local.hour < _JUST_ENDED_CUTOFF_HOUR:
        return local.date() - datetime.timedelta(days=1)
    return local.date()


def partner_day_totals(partner, day: Optional[datetime.date] = None) -> dict:
    """Totals for the transactions THIS phone requested on `day`.

    Returns {'count', 'debit', 'credit'}. Only rows attributed to this partner
    are counted — a record with partner=NULL was entered inside Qurtoba by an
    accountant and belongs to no phone. Amounts of zero or less are excluded,
    matching what the customer is actually shown.
    """
    from qurtoba.models import QurtobaRecord

    if day is None:
        day = reporting_day()

    partner_id = getattr(partner, 'pk', None)
    if not partner_id:
        return {'count': 0, 'debit': 0.0, 'credit': 0.0}

    agg = (
        QurtobaRecord.objects
        .filter(partner_id=partner_id, date=day, value__gt=0)
        .aggregate(
            count=Count('id'),
            debit=Sum('value', filter=Q(is_down=False)),
            credit=Sum('value', filter=Q(is_down=True)),
        )
    )
    return {
        'count': int(agg['count'] or 0),
        'debit': float(agg['debit'] or 0),
        'credit': float(agg['credit'] or 0),
    }


def groups_chatted_on(day: Optional[datetime.date] = None):
    """Conversation ids of the WhatsApp Web customer GROUPS the nightly statement goes to (owner decision
    2026-09-26): linked to a Qurtoba customer, and the customer's side (not office staff, not a join/leave
    notice) wrote at least one message in the group on `day`."""
    from modules.chat.models import Message
    from qurtoba.groups import staff_q

    if day is None:
        day = reporting_day()
    tz = timezone.get_current_timezone()
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=1)
    return sorted(
        str(cid) for cid in (
            Message.objects_all
            .filter(direction='inbound', created_at__gte=start, created_at__lt=end,
                    conversation__type='wa_web', conversation__is_group=True,
                    conversation__social_partner__qurtoba_customer__isnull=False)
            .exclude(type='system').exclude(staff_q())
            .order_by()
            .values_list('conversation_id', flat=True)
            .distinct()
        ) if cid
    )


def partners_chatted_on(day: Optional[datetime.date] = None):
    """Partner ids the end-of-day messages go to: every Qurtoba-linked number that sent US at least one
    WhatsApp message on `day`, and nobody else.

    Owner decision 2026-09-14. Until then the Excel statement went to every linked number whose CUSTOMER had
    any record that day, including rows the office keyed into Qurtoba itself: 7 statements a night while only
    1 or 2 of those customers had written to us. A record alone never qualifies a number now, neither an office
    row nor a chat-born record dated that day without a message that day. Both nightly messages, the summary
    text and the Excel file, use this one list, so they always reach exactly the same people.
    """
    from modules.chat.models import Message

    if day is None:
        day = reporting_day()
    tz = timezone.get_current_timezone()
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=1)
    return sorted(
        pid for pid in (
            Message.objects_all
            .filter(direction='inbound', created_at__gte=start, created_at__lt=end,
                    conversation__type='whatsapp',
                    conversation__social_partner__qurtoba_customer__isnull=False)
            # .order_by() clears Meta ordering so DISTINCT dedupes on the partner id alone.
            .order_by()
            .values_list('conversation__social_partner_id', flat=True)
            .distinct()
        ) if pid
    )


# Arabic day and month names, spelled the way the office writes them (plain
# alef, no hamza: «الاثنين», «اغسطس»). Hard-coded rather than taken from
# strftime + locale: the Arabic locale is not guaranteed to be installed on the
# worker, and a missing locale would silently fall back to English day names in
# a customer-facing message.
_AR_WEEKDAYS = (
    'الاثنين', 'الثلاثاء', 'الاربعاء', 'الخميس',   # date.weekday(): Monday = 0
    'الجمعة', 'السبت', 'الاحد',
)
_AR_MONTHS = (
    'يناير', 'فبراير', 'مارس', 'ابريل', 'مايو', 'يونيو',
    'يوليو', 'اغسطس', 'سبتمبر', 'اكتوبر', 'نوفمبر', 'ديسمبر',
)


def fmt_day_ar(day: Optional[datetime.date] = None) -> str:
    """A date as «الاثنين 24 اغسطس» — day name, day number, month name."""
    if day is None:
        day = reporting_day()
    return f'{_AR_WEEKDAYS[day.weekday()]} {day.day} {_AR_MONTHS[day.month - 1]}'


def fmt_amount(value) -> str:
    """1234.0 → '1,234'. Never returns an empty string.

    Meta rejects a template whose example value is blank, and the example is
    read off whatever Partner happens to be newest — usually one with no
    Qurtoba link at all. So every template-facing value falls back to a real
    character rather than '' or None.
    """
    try:
        return f'{int(round(float(value))):,}'
    except (TypeError, ValueError):
        return '0'


