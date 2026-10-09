"""Offline A-share calendar; unknown years are never assumed complete.

Official SSE notices, verified 2026-10-06:
https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml
https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml
"""
from datetime import date, datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))
_HOLIDAYS = {
    2025: (("01-01", "01-01"), ("01-28", "02-04"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("05-31", "06-02"), ("10-01", "10-08")),
    2026: (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"),
           ("10-01", "10-07")),
}


def shanghai_now():
    return datetime.now(SHANGHAI)


def is_trading_day(day: date) -> bool | None:
    if day.weekday() >= 5:
        return False
    if day.year not in _HOLIDAYS:
        return None
    key = day.strftime("%m-%d")
    return not any(start <= key <= end for start, end in _HOLIDAYS[day.year])


def previous_trading_day(day: date) -> tuple[date, bool]:
    while is_trading_day(day) is False:
        day -= timedelta(days=1)
    return day, is_trading_day(day) is True


def latest_closed_trading_day(day: date, now: datetime | None = None) -> tuple[date, bool]:
    """An unfinished trading day must not become the closing-history cutoff."""
    now = (now or shanghai_now()).astimezone(SHANGHAI)
    if day == now.date() and (now.hour, now.minute) < (15, 5):
        day -= timedelta(days=1)
    return previous_trading_day(day)


def trading_window(day: date, count=10) -> list[str] | None:
    result = []
    while len(result) < count:
        state = is_trading_day(day)
        if state is None:
            return None
        if state:
            result.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(result))


def confirmed_close_date(value, now: datetime | None = None, *, captured_at=None) -> str | None:
    """Never infer a trading date from fetch time or a naive timestamp."""
    try:
        stamp = datetime.fromisoformat(str(value)).astimezone(SHANGHAI)
        original = datetime.fromisoformat(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    now = (now or shanghai_now()).astimezone(SHANGHAI)
    if original.tzinfo is None or stamp > now or is_trading_day(stamp.date()) is not True:
        return None
    if captured_at is not None:
        try:
            capture = datetime.fromisoformat(str(captured_at))
            if capture.tzinfo is None or capture > now or capture < stamp:
                return None
        except (TypeError,ValueError,OverflowError):
            return None
    # Some providers stop their quote clock at 15:00. Accept it only when this
    # exact snapshot was fetched after the close buffer, not merely read later.
    if (stamp.hour, stamp.minute) < (15, 5):
        try:
            capture = datetime.fromisoformat(str(captured_at))
            if capture.tzinfo is None:
                return None
            capture = capture.astimezone(SHANGHAI)
        except (TypeError, ValueError, OverflowError):
            return None
        if (stamp.hour, stamp.minute) < (15, 0) or capture > now or capture < stamp:
            return None
        if capture.date() == stamp.date() and (capture.hour, capture.minute) < (15, 5):
            return None
    return stamp.date().isoformat()
