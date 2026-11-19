"""Simulated business calendar, America/Chicago.

Business days are Mon–Fri minus HOLIDAYS; business hours 09:00–17:00. Patients
are only contacted 08:00–21:00 local time (any day). Every datetime here is
timezone-aware in TZ.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
HOLIDAYS = frozenset({date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1)})
BUSINESS_START, BUSINESS_END = time(9), time(17)
CONTACT_START, CONTACT_END = time(8), time(21)

Resolution = Literal["firm", "tentative", "unresolvable"]
Purpose = Literal["delivery", "deadline", "callback"]


def at(d: date, t: time) -> datetime:
    """Local wall-clock time `t` on day `d`, in TZ."""
    return datetime.combine(d, t, tzinfo=TZ)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def next_business_day(d: date) -> date:
    """The first business day strictly after `d`."""
    d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def in_business_hours(dt: datetime) -> bool:
    dt = dt.astimezone(TZ)
    return is_business_day(dt.date()) and BUSINESS_START <= dt.time() < BUSINESS_END


def next_business_moment(dt: datetime) -> datetime:
    """`dt` if it is inside business hours, else the next business-day opening."""
    dt = dt.astimezone(TZ)
    if in_business_hours(dt):
        return dt
    if is_business_day(dt.date()) and dt.time() < BUSINESS_START:
        return at(dt.date(), BUSINESS_START)
    return at(next_business_day(dt.date()), BUSINESS_START)


def add_business_hours(dt: datetime, hours: float) -> datetime:
    """Move forward `hours` of business time (only 09:00–17:00 on business days count)."""
    remaining = timedelta(hours=hours)
    current = next_business_moment(dt)
    while True:
        close = at(current.date(), BUSINESS_END)
        if current + remaining <= close:
            return current + remaining
        remaining -= close - current
        current = at(next_business_day(current.date()), BUSINESS_START)


def add_business_days(dt: datetime, days: int) -> datetime:
    """Same wall-clock time, `days` business days later (counting starts the day after `dt`)."""
    dt = dt.astimezone(TZ)
    d = dt.date()
    for _ in range(days):
        d = next_business_day(d)
    return at(d, dt.time())


def in_contact_window(dt: datetime) -> bool:
    """Patient contact is allowed 08:00–21:00 Chicago time."""
    return CONTACT_START <= dt.astimezone(TZ).time() < CONTACT_END


def next_contact_time(dt: datetime) -> datetime:
    """`dt` if a patient may be contacted then, else the next 08:00."""
    dt = dt.astimezone(TZ)
    if in_contact_window(dt):
        return dt
    day = dt.date() if dt.time() < CONTACT_START else dt.date() + timedelta(days=1)
    return at(day, CONTACT_START)


# Date phrases. Claude returns the phrase verbatim plus firm/tentative; code
# resolves it here. Times default to the business opening (09:00); a deadline
# ("by Friday") resolves to that day's close (17:00).

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
HEDGE_PREFIXES = ("maybe ", "probably ", "possibly ", "hopefully ", "i think ", "around ")
PREFIXES = ("by ", "on ", "this ") + HEDGE_PREFIXES
NUMBER_WORDS = {
    "a": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _next_weekday(today: date, weekday: int) -> date:
    """The next date with that weekday, strictly after today."""
    ahead = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead)


def resolve_date_phrase(
    phrase: str, commitment: str, now: datetime, purpose: Purpose = "callback"
) -> tuple[datetime | None, Resolution]:
    """Resolve a spoken time expression against `now`: (moment, "firm" | "tentative"), or (None, "unresolvable").

    Hedged phrases ("maybe by Friday") and ranges ("next week", resolved to its start)
    are tentative. Except for a delivery date, a weekend or holiday moves to the next
    business day.
    """
    now = now.astimezone(TZ)
    level: Resolution = "firm" if commitment == "firm" else "tentative"
    text = re.sub(r"\s+", " ", phrase.strip().lower()).rstrip(".!?")

    deadline = False
    stripped = True
    while stripped:  # "maybe by Friday", "probably on this Thursday", ...
        stripped = False
        for prefix in PREFIXES:
            if text.startswith(prefix):
                text, stripped = text[len(prefix) :], True
                deadline = deadline or prefix == "by "
                if prefix in HEDGE_PREFIXES:
                    level = "tentative"

    moment, is_range = _parse(text, now, BUSINESS_END if deadline else BUSINESS_START)
    if moment is None:
        return None, "unresolvable"
    if purpose != "delivery" and not is_business_day(moment.date()):
        moment = at(next_business_day(moment.date()), moment.time())
    return moment, "tentative" if is_range else level


MAX_DAYS_AHEAD = 365  # "in N days" or an ISO date beyond this is not a date we act on


def iso_date(value: object, now: datetime) -> date | None:
    """The value as a date we may act on, or None. Never raises.

    Rejects anything that is not "YYYY-MM-DD", impossible dates, and dates more than
    MAX_DAYS_AHEAD out, so "9999-12-31" never reaches date arithmetic.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value.strip()):
        return None
    try:
        day = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return day if (day - now.astimezone(TZ).date()).days <= MAX_DAYS_AHEAD else None


def _parse(text: str, now: datetime, opening: time) -> tuple[datetime | None, bool]:
    """(moment, is_range) for a phrase with its prefixes stripped, or (None, False). Never raises."""
    today = now.date()
    monday_next = today + timedelta(days=7 - today.weekday())
    if not text:
        return None, False
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        day = iso_date(text, now)
        return (at(day, opening), False) if day else (None, False)
    if text == "today":
        return at(today, opening), False
    if text == "tomorrow":
        return at(today + timedelta(days=1), opening), False
    if text in WEEKDAYS:
        return at(_next_weekday(today, WEEKDAYS.index(text)), opening), False
    if text.startswith("next ") and text[5:] in WEEKDAYS:  # the weekday of next (Mon-start) week
        return at(monday_next + timedelta(days=WEEKDAYS.index(text[5:])), opening), False
    if text == "next week":
        return at(monday_next, BUSINESS_START), True

    m = re.fullmatch(r"in ([0-9]+|[a-z]+) (business )?days?", text)
    if m and (m.group(1).isdigit() or m.group(1) in NUMBER_WORDS):
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1)]
        if n > MAX_DAYS_AHEAD:
            return None, False
        day = add_business_days(now, n).date() if m.group(2) else today + timedelta(days=n)
        return at(day, opening), False

    # "p.m." has lost its final "." to the rstrip above, so that dot is optional.
    m = re.fullmatch(r"after ([0-9]{1,2})(?::([0-9]{2}))? ?(am|pm|a\.m\.?|p\.m\.?)?", text)
    if m:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3) or ""
        if hour > 23 or minute > 59:
            return None, False
        if meridiem and not 1 <= hour <= 12:  # "after 13pm", "after 0am": not a clock time
            return None, False
        if meridiem.startswith("a"):
            hour %= 12  # 12 am is midnight
        elif meridiem.startswith("p"):
            hour = hour % 12 + 12  # 12 pm is noon
        elif hour < 8:  # "after 2" in a business call means 2 pm
            hour += 12
        moment = at(today, time(hour, minute))
        if moment <= now or not is_business_day(today):
            moment = at(next_business_day(today), time(hour, minute))
        return moment, False

    return None, False
