from datetime import datetime

import pytest

from dme import clock
from dme.clock import TZ, add_business_days, add_business_hours, resolve_date_phrase


def chi(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


TUE = chi(2026, 11, 17, 10, 0)  # Tuesday, 10:00


def test_holidays_and_weekends_are_not_business_days():
    assert not clock.is_business_day(chi(2026, 11, 26).date())  # Thanksgiving
    assert not clock.is_business_day(chi(2026, 11, 28).date())  # Saturday
    assert clock.is_business_day(chi(2026, 11, 27).date())  # Friday after is a business day


def test_business_hours_across_thanksgiving():
    # Wed 15:00 + 4h: 2h on Wednesday, Thursday is a holiday, 2h on Friday.
    assert add_business_hours(chi(2026, 11, 25, 15, 0), 4) == chi(2026, 11, 27, 11, 0)
    # Started outside hours: the clock starts at the next opening.
    assert add_business_hours(chi(2026, 11, 25, 20, 0), 1) == chi(2026, 11, 27, 10, 0)


def test_business_days_across_thanksgiving_and_weekend():
    assert add_business_days(chi(2026, 11, 25, 10, 0), 2) == chi(2026, 11, 30, 10, 0)
    assert add_business_days(chi(2026, 12, 24, 10, 0), 1) == chi(2026, 12, 28, 10, 0)


def test_patient_contact_window():
    assert clock.in_contact_window(chi(2026, 11, 21, 8, 0))  # weekends allowed
    assert clock.in_contact_window(chi(2026, 11, 17, 20, 59))
    assert not clock.in_contact_window(chi(2026, 11, 17, 21, 0))
    assert not clock.in_contact_window(chi(2026, 11, 17, 7, 59))
    assert clock.next_contact_time(chi(2026, 11, 17, 22, 0)) == chi(2026, 11, 18, 8, 0)
    assert clock.next_contact_time(chi(2026, 11, 17, 6, 0)) == chi(2026, 11, 17, 8, 0)


@pytest.mark.parametrize(
    "phrase, commitment, expected, level",
    [
        ("Thursday", "firm", chi(2026, 11, 19, 9, 0), "firm"),
        ("tomorrow", "firm", chi(2026, 11, 18, 9, 0), "firm"),
        ("in two days", "tentative", chi(2026, 11, 19, 9, 0), "tentative"),
        ("by Friday", "firm", chi(2026, 11, 20, 17, 0), "firm"),
        ("after 2", "firm", chi(2026, 11, 17, 14, 0), "firm"),
        ("next week", "firm", chi(2026, 11, 23, 9, 0), "tentative"),  # a range: always tentative
        ("Thursday", "", chi(2026, 11, 19, 9, 0), "tentative"),  # no commitment stated
        ("2026-12-15", "firm", chi(2026, 12, 15, 9, 0), "firm"),
    ],
)
def test_date_phrases(phrase, commitment, expected, level):
    assert resolve_date_phrase(phrase, commitment, TUE) == (expected, level)


def test_after_2_when_already_past_moves_to_next_business_day():
    assert resolve_date_phrase("after 2pm", "firm", chi(2026, 11, 25, 15, 0)) == (chi(2026, 11, 27, 14, 0), "firm")


def test_weekday_phrase_on_that_weekday_means_next_week():
    assert resolve_date_phrase("Tuesday", "firm", TUE)[0] == chi(2026, 11, 24, 9, 0)


@pytest.mark.parametrize("phrase", ["", "sometime soon", "when the truck is fixed", "after 99"])
def test_unresolvable(phrase):
    assert resolve_date_phrase(phrase, "firm", TUE) == (None, "unresolvable")


def test_callback_and_deadline_move_off_holidays_delivery_keeps_the_date():
    mon = chi(2026, 11, 23, 10, 0)  # Thursday of that week is Thanksgiving
    assert resolve_date_phrase("Thursday", "firm", mon) == (chi(2026, 11, 27, 9, 0), "firm")
    assert resolve_date_phrase("Thursday", "firm", mon, purpose="delivery") == (chi(2026, 11, 26, 9, 0), "firm")
    xmas_week = chi(2026, 12, 21, 10, 0)  # Friday is Christmas
    assert resolve_date_phrase("by Friday", "firm", xmas_week, purpose="deadline") == (chi(2026, 12, 28, 17, 0), "firm")
    assert resolve_date_phrase("tomorrow", "firm", chi(2026, 11, 20, 10, 0))[0] == chi(2026, 11, 23, 9, 0)


def test_hedge_prefixes_are_stripped_repeatedly_and_make_it_tentative():
    assert resolve_date_phrase("maybe by Friday", "firm", TUE) == (chi(2026, 11, 20, 17, 0), "tentative")
    assert resolve_date_phrase("probably on this Thursday", "firm", TUE) == (chi(2026, 11, 19, 9, 0), "tentative")


def test_after_2_said_on_a_weekend_means_next_business_day():
    saturday = chi(2026, 11, 21, 10, 0)
    assert resolve_date_phrase("after 2", "firm", saturday) == (chi(2026, 11, 23, 14, 0), "firm")
    # Even for a delivery: "after 2" names a time on the next working day, not Saturday.
    assert resolve_date_phrase("after 2", "firm", saturday, purpose="delivery")[0] == chi(2026, 11, 23, 14, 0)


@pytest.mark.parametrize("phrase", ["2026-02-30", "2026-11-31", "in ² days", "in 99999999 days", "in ٣ days"])
def test_impossible_or_absurd_phrases_are_unresolvable_not_errors(phrase):
    assert resolve_date_phrase(phrase, "firm", TUE) == (None, "unresolvable")


def test_in_365_days_is_still_resolved():
    moment, level = resolve_date_phrase("in 365 days", "firm", TUE)
    assert moment is not None and moment.date() == chi(2027, 11, 17).date() and level == "firm"


# Sentence punctuation strips the final "." of "p.m.", so the dotted meridiem must match without it.
@pytest.mark.parametrize(
    "dotted, plain",
    [
        ("after 2 p.m.", "after 2pm"),
        ("after 2 p.m", "after 2pm"),
        ("after 2:30 p.m.", "after 2:30pm"),
        ("after 9 a.m.", "after 9am"),
        ("after 12 p.m.", "after 12pm"),
    ],
)
def test_dotted_meridiem_resolves_like_the_undotted_one(dotted, plain):
    expected = resolve_date_phrase(plain, "firm", TUE)
    assert expected[0] is not None
    assert resolve_date_phrase(dotted, "firm", TUE) == expected


def test_dotted_meridiem_values():
    assert resolve_date_phrase("after 2 p.m.", "firm", TUE) == (chi(2026, 11, 17, 14, 0), "firm")
    # 9 am has already passed at 10:00, so it is tomorrow morning.
    assert resolve_date_phrase("after 9 a.m.", "firm", TUE) == (chi(2026, 11, 18, 9, 0), "firm")


@pytest.mark.parametrize("phrase", ["after 13 p.m.", "after 0 a.m."])
def test_dotted_meridiem_keeps_the_clock_time_checks(phrase):
    assert resolve_date_phrase(phrase, "firm", TUE) == (None, "unresolvable")


def test_trailing_punctuation_still_stripped_for_other_phrases():
    assert resolve_date_phrase("Friday.", "firm", TUE) == (chi(2026, 11, 20, 9, 0), "firm")
    assert resolve_date_phrase("after 2.", "firm", TUE) == (chi(2026, 11, 17, 14, 0), "firm")


@pytest.mark.parametrize("phrase, hour", [("after 12am", 0), ("after 12pm", 12), ("after 11am", 11), ("after 1pm", 13)])
def test_after_12am_is_midnight_not_noon(phrase, hour):
    from datetime import datetime

    from dme import clock

    now = datetime(2026, 11, 17, 7, 0, tzinfo=clock.TZ)  # a Tuesday morning
    moment, _ = clock.resolve_date_phrase(phrase, "firm", now)
    assert moment.hour == hour
