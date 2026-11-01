"""Small date and time helpers. Clinic time is stored as naive local wall-clock text."""
import calendar
from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

FMT = "%Y-%m-%d %H:%M"


def parse_dt(text):
    return datetime.strptime(text, FMT)


def to_str(dt):
    return dt.strftime(FMT)


def parse_date(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def epoch(dt):
    """Seconds for a naive local datetime, treated as UTC. Only used to compare overlaps."""
    return calendar.timegm(dt.timetuple())


def hhmm_to_minutes(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(total):
    return f"{total // 60:02d}:{total % 60:02d}"


def valid_timezone(tz_name):
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:
        return False


def now_local(tz_name="UTC"):
    """Wall-clock time now in the clinic's timezone. Daylight saving is handled by the timezone database."""
    try:
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None, second=0, microsecond=0)
    except Exception:  # unknown timezone name: fall back to UTC rather than crash
        return datetime.utcnow().replace(second=0, microsecond=0)


def fmt_time(dt):
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour} {suffix}" if dt.minute == 0 else f"{hour}:{dt.minute:02d} {suffix}"


def fmt_date(d):
    return f"{d.day} {d.strftime('%B %Y')}"


def fmt_day_date(d):
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def add_months_safe(d, year):
    """Same month and day in another year. 29 February becomes 28 February in non-leap years."""
    try:
        return date(year, d.month, d.day)
    except ValueError:
        return date(year, d.month, 28)


def add_months(d, n):
    """Move a date forward by n months. The day is kept, or the last day of a shorter month (31 January + 1 month = 28 February)."""
    month_index = d.month - 1 + n
    year, month = d.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))
