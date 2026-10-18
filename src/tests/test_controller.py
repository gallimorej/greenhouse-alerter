from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from greenhouse.controller import (
    ControllerState,
    Override,
    Reason,
    Settings,
    decide,
    in_active_hours,
    in_cycle,
)
from greenhouse.models import Reading

NY = ZoneInfo("America/New_York")
SETTINGS = Settings()
BASELINED = ControllerState(temp_demand=False, baselined=True)
HOT = ControllerState(temp_demand=True, baselined=True)


def at(hour, minute, day=(2026, 7, 15)):
    return datetime(*day, hour, minute, tzinfo=NY).astimezone(timezone.utc)


def reading(now, temp, age=timedelta(minutes=1)):
    return Reading(sensor_id="s1", observed_at=now - age, temperature_f=temp)


# --- Temperature demand (hysteresis) -------------------------------------------------------------

@pytest.mark.parametrize(
    "temp, prior, expected_on, expected_reason",
    [
        (100.1, BASELINED, True, Reason.TOO_HOT),
        (100.0, BASELINED, False, Reason.HOLDING),  # exactly at high is in the band
        (100.0, HOT, True, Reason.HOLDING),
        (95.0, BASELINED, False, Reason.HOLDING),
        (95.0, HOT, True, Reason.HOLDING),
        (90.0, HOT, True, Reason.HOLDING),  # exactly at low is in the band
        (89.9, HOT, False, Reason.COOL),
    ],
)
def test_hysteresis(temp, prior, expected_on, expected_reason):
    now = at(12, 20)
    d = decide(now, SETTINGS, reading(now, temp), prior)
    assert d.desired_on is expected_on
    assert d.reason is expected_reason
    assert d.state.temp_demand is expected_on


# --- Cycle window --------------------------------------------------------------------------------

@pytest.mark.parametrize("minute, cycling", [(4, False), (5, True), (6, True), (7, False)])
def test_cycle_boundaries(minute, cycling):
    now = at(12, minute)
    d = decide(now, SETTINGS, reading(now, 80), BASELINED)
    assert d.in_cycle is cycling
    assert d.desired_on is cycling
    assert d.reason is (Reason.CYCLE if cycling else Reason.COOL)


def test_cycle_runs_at_night():
    now = at(3, 5)
    d = decide(now, SETTINGS, reading(now, 80), BASELINED)
    assert (d.desired_on, d.reason) == (True, Reason.CYCLE)


def test_cycle_ends_on_time_when_in_band():
    """Deliberate change from the old app: a cycle in the band ends at :07."""
    now = at(12, 7)
    d = decide(now, SETTINGS, reading(now, 95), BASELINED)
    assert (d.desired_on, d.reason) == (False, Reason.HOLDING)


def test_cycle_does_not_end_hot_fans():
    now = at(12, 7)
    d = decide(now, SETTINGS, reading(now, 95), HOT)
    assert d.desired_on is True


# --- Active hours --------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hour, minute, active", [(5, 59, False), (6, 0, True), (22, 59, True), (23, 0, False)]
)
def test_active_hour_boundaries(hour, minute, active):
    now = at(hour, minute)
    d = decide(now, SETTINGS, reading(now, 101), BASELINED)
    assert d.in_active_hours is active
    assert d.desired_on is active
    assert d.reason is (Reason.TOO_HOT if active else Reason.NIGHT)


def test_night_turns_hot_fans_off_at_23():
    now = at(23, 0)
    d = decide(now, SETTINGS, reading(now, 105), HOT)
    assert (d.desired_on, d.reason) == (False, Reason.NIGHT)
    assert d.state.temp_demand is False


def test_overnight_active_window():
    s = Settings(active_start=time(20, 0), active_end=time(4, 0))
    assert in_active_hours(datetime(2026, 7, 15, 21, 0), s)
    assert in_active_hours(datetime(2026, 7, 15, 3, 59), s)
    assert not in_active_hours(datetime(2026, 7, 15, 4, 0), s)
    assert not in_active_hours(datetime(2026, 7, 15, 12, 0), s)


# --- Stale or missing readings -------------------------------------------------------------------

@pytest.mark.parametrize("prior", [BASELINED, HOT])
def test_stale_reading_freezes_temp_demand(prior):
    now = at(12, 20)
    stale = reading(now, 70, age=timedelta(minutes=21))
    d = decide(now, SETTINGS, stale, prior)
    assert d.sensor_fresh is False
    assert d.desired_on is prior.temp_demand
    assert d.reason is Reason.STALE
    assert d.state.temp_demand is prior.temp_demand


def test_reading_exactly_at_stale_limit_is_fresh():
    now = at(12, 20)
    d = decide(now, SETTINGS, reading(now, 101, age=timedelta(minutes=20)), BASELINED)
    assert d.sensor_fresh is True


def test_future_reading_beyond_skew_is_stale():
    now = at(12, 20)
    d = decide(now, SETTINGS, reading(now, 101, age=-timedelta(minutes=3)), BASELINED)
    assert d.sensor_fresh is False


def test_no_sensor_linked_freezes_and_cycles_continue():
    now = at(12, 5)
    d = decide(now, SETTINGS, None, BASELINED)
    assert (d.desired_on, d.reason) == (True, Reason.CYCLE)
    now = at(12, 10)
    d = decide(now, SETTINGS, None, BASELINED)
    assert (d.desired_on, d.reason) == (False, Reason.STALE)


def test_stale_and_hot_at_22_59_then_23_00_goes_off():
    late = at(22, 59)
    d = decide(late, SETTINGS, None, HOT)
    assert d.desired_on is True
    d = decide(at(23, 0), SETTINGS, None, d.state)
    assert (d.desired_on, d.reason) == (False, Reason.NIGHT)


def test_recovery_after_stale_resumes_from_frozen_value():
    now = at(12, 20)
    frozen = decide(now, SETTINGS, None, HOT).state
    assert frozen.temp_demand is True
    d = decide(now, SETTINGS, reading(now, 95), frozen)  # in band: keeps the frozen ON
    assert d.desired_on is True
    d = decide(now, SETTINGS, reading(now, 85), d.state)
    assert d.desired_on is False


# --- Startup baseline ----------------------------------------------------------------------------

def test_not_baselined_sends_nothing_even_during_cycle():
    now = at(12, 5)
    d = decide(now, SETTINGS, None, ControllerState())
    assert (d.desired_on, d.reason) == (None, Reason.NOT_BASELINED)
    assert d.state.baselined is False


def test_first_fresh_reading_baselines():
    now = at(12, 20)
    d = decide(now, SETTINGS, reading(now, 101), ControllerState())
    assert d.state.baselined is True
    assert d.desired_on is True


def test_fresh_reading_at_night_baselines():
    now = at(2, 20)
    d = decide(now, SETTINGS, reading(now, 70), ControllerState())
    assert d.state.baselined is True
    assert (d.desired_on, d.reason) == (False, Reason.NIGHT)


def test_baseline_survives_losing_the_sensor():
    now = at(12, 20)
    d = decide(now, SETTINGS, None, BASELINED)
    assert d.state.baselined is True
    assert d.desired_on is False


# --- Manual override -----------------------------------------------------------------------------

@pytest.mark.parametrize("on", [True, False])
def test_override_wins(on):
    now = at(12, 5)  # mid-cycle and hot
    d = decide(now, SETTINGS, reading(now, 105), HOT, Override(on, now + timedelta(hours=1)))
    assert (d.desired_on, d.reason) == (on, Reason.OVERRIDE)
    assert d.state.temp_demand is True  # hysteresis keeps tracking underneath


def test_override_before_baseline_is_honored():
    now = at(12, 20)
    d = decide(now, SETTINGS, None, ControllerState(), Override(True, now + timedelta(hours=1)))
    assert (d.desired_on, d.reason) == (True, Reason.OVERRIDE)


def test_expired_override_is_ignored():
    now = at(12, 20)
    d = decide(now, SETTINGS, reading(now, 80), BASELINED, Override(True, now))
    assert (d.desired_on, d.reason) == (False, Reason.COOL)


# --- Time zones ----------------------------------------------------------------------------------

def test_dst_spring_forward_uses_local_wall_clock():
    # 2026-03-08: clocks jump 02:00 -> 03:00 in New York. 07:00 UTC is 03:00 EDT.
    now = datetime(2026, 3, 8, 7, 5, tzinfo=timezone.utc)
    d = decide(now, SETTINGS, reading(now, 70), BASELINED)
    assert d.in_cycle is True
    assert d.in_active_hours is False


def test_dst_fall_back_both_1am_hours_cycle():
    # 2026-11-01: 01:00-02:00 happens twice in New York (05:00 and 06:00 UTC).
    for utc_hour in (5, 6):
        now = datetime(2026, 11, 1, utc_hour, 5, tzinfo=timezone.utc)
        assert now.astimezone(NY).hour == 1
        assert decide(now, SETTINGS, reading(now, 70), BASELINED).in_cycle is True


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        decide(datetime(2026, 7, 15, 12, 0), SETTINGS, None, BASELINED)


# --- Settings validation -------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"high_f": 90, "low_f": 90},
        {"high_f": 80, "low_f": 90},
        {"active_start": time(6), "active_end": time(6)},
        {"cycle_interval_minutes": 7},
        {"cycle_offset_minutes": 60},
        {"cycle_duration_minutes": 60},
        {"stale_after": timedelta(0)},
        {"timezone": "Mars/Olympus_Mons"},
    ],
)
def test_invalid_settings(kwargs):
    with pytest.raises(Exception):
        Settings(**kwargs)


def test_custom_cycle_interval():
    s = Settings(cycle_interval_minutes=30, cycle_offset_minutes=10, cycle_duration_minutes=3)
    assert [m for m in range(60) if in_cycle(datetime(2026, 7, 15, 12, m), s)] == [10, 11, 12, 40, 41, 42]
