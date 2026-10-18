"""Fan decision rules.

``decide`` is a pure function: it gets the time, settings, latest reading, persisted controller state
and any manual override, and returns what the fans should be doing and why. It never talks to a
sensor, a database or IFTTT, so every rule can be table-tested.

Rules, in precedence order:

1. Temperature demand (hysteresis):
   a. Temperature control switched off: off.
   b. Outside active hours: off. Temperature control is off at night, even if the reading is stale.
   c. Inside active hours with no fresh reading: frozen at its previous value.
   d. Inside active hours with a fresh reading: on above ``high_f``, off below ``low_f``, otherwise
      unchanged.
2. Cycle demand: on during the scheduled cycle window, around the clock, with or without a reading,
   unless cycles are switched off.
3. Desired state:
   a. An unexpired manual override wins.
   b. With temperature control on, before the first fresh reading ever (not yet baselined), send
      nothing. With it off there's no temperature demand to guess, so cycles run without a reading.
   c. Otherwise, cycle demand OR temperature demand.

The season switch (all control off) isn't a rule here: the worker doesn't call ``decide`` at all while
the season is paused.
"""

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from greenhouse.models import Reading

# Readings stamped this far in the future are treated as clock skew, not as fresh data.
MAX_CLOCK_SKEW = timedelta(minutes=2)


@dataclass(frozen=True)
class Settings:
    high_f: float = 100.0
    low_f: float = 90.0
    timezone: str = "America/New_York"
    active_start: time = time(6, 0)
    # Exclusive: with the default, 22:59 is the last active minute.
    active_end: time = time(23, 0)
    cycle_interval_minutes: int = 60
    cycle_offset_minutes: int = 5
    cycle_duration_minutes: int = 2
    stale_after: timedelta = timedelta(minutes=20)
    # Owned by the dashboard switches, not the settings form.
    cycles_enabled: bool = True
    temperature_control_enabled: bool = True

    def __post_init__(self):
        if not self.low_f < self.high_f:
            raise ValueError("low_f must be below high_f")
        if self.active_start == self.active_end:
            raise ValueError("active hours must not be empty")
        if self.cycle_interval_minutes <= 0 or 1440 % self.cycle_interval_minutes:
            raise ValueError("cycle_interval_minutes must evenly divide a day")
        if not 0 <= self.cycle_offset_minutes < self.cycle_interval_minutes:
            raise ValueError("cycle_offset_minutes must be within the interval")
        if not 0 <= self.cycle_duration_minutes < self.cycle_interval_minutes:
            raise ValueError("cycle_duration_minutes must be shorter than the interval")
        if self.stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        ZoneInfo(self.timezone)  # raises for an unknown zone


@dataclass(frozen=True)
class ControllerState:
    """Persisted between ticks."""

    temp_demand: bool = False
    baselined: bool = False


@dataclass(frozen=True)
class Override:
    on: bool
    expires_at: datetime


class Reason(str, Enum):
    OVERRIDE = "manual override"
    NOT_BASELINED = "waiting for the first fresh reading"
    CYCLE = "scheduled cycle"
    TOO_HOT = "above the high threshold"
    HOLDING = "between thresholds, holding"
    COOL = "below the low threshold"
    NIGHT = "outside active hours"
    STALE = "no fresh reading, temperature demand frozen"
    TEMP_OFF = "temperature control off"


@dataclass(frozen=True)
class Decision:
    # None means "send nothing": the controller has no basis for a command yet.
    desired_on: bool | None
    reason: Reason
    state: ControllerState
    sensor_fresh: bool
    in_active_hours: bool
    in_cycle: bool


def is_fresh(reading: Reading | None, now: datetime, stale_after: timedelta) -> bool:
    if reading is None:
        return False
    age = now - reading.observed_at
    return -MAX_CLOCK_SKEW <= age <= stale_after


def in_active_hours(local: datetime, settings: Settings) -> bool:
    t = local.time()
    start, end = settings.active_start, settings.active_end
    if start < end:
        return start <= t < end
    # The window wraps past midnight, e.g. 20:00-04:00.
    return t >= start or t < end


def next_cycle_start(now: datetime, settings: Settings) -> datetime | None:
    """The next minute after ``now`` at which a cycle begins, or None when cycles are off.

    Steps through real minutes rather than adding to local time, so a repeated or skipped hour at a
    daylight-saving change gives the cycle that actually runs next.
    """
    if not settings.cycles_enabled or settings.cycle_duration_minutes == 0:
        return None
    zone = ZoneInfo(settings.timezone)
    t = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    for _ in range(settings.cycle_interval_minutes + 61):  # one interval, plus a DST hour
        t += timedelta(minutes=1)
        local = t.astimezone(zone)
        minute_of_day = local.hour * 60 + local.minute
        if (minute_of_day - settings.cycle_offset_minutes) % settings.cycle_interval_minutes == 0:
            return t.astimezone(now.tzinfo)
    return None


def in_cycle(local: datetime, settings: Settings) -> bool:
    minute_of_day = local.hour * 60 + local.minute
    into_interval = (minute_of_day - settings.cycle_offset_minutes) % settings.cycle_interval_minutes
    return into_interval < settings.cycle_duration_minutes


def decide(
    now: datetime,
    settings: Settings,
    reading: Reading | None,
    state: ControllerState,
    override: Override | None = None,
) -> Decision:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ZoneInfo(settings.timezone))
    fresh = is_fresh(reading, now, settings.stale_after)
    active = in_active_hours(local, settings)
    cycling = settings.cycles_enabled and in_cycle(local, settings)
    temperature_on = settings.temperature_control_enabled

    if not temperature_on:
        temp_demand, temp_reason = False, Reason.TEMP_OFF
    elif not active:
        temp_demand, temp_reason = False, Reason.NIGHT
    elif not fresh:
        temp_demand, temp_reason = state.temp_demand, Reason.STALE
    elif reading.temperature_f > settings.high_f:
        temp_demand, temp_reason = True, Reason.TOO_HOT
    elif reading.temperature_f < settings.low_f:
        temp_demand, temp_reason = False, Reason.COOL
    else:
        temp_demand, temp_reason = state.temp_demand, Reason.HOLDING

    new_state = replace(state, temp_demand=temp_demand, baselined=state.baselined or fresh)

    if override is not None and override.expires_at > now:
        desired, reason = override.on, Reason.OVERRIDE
    elif temperature_on and not new_state.baselined:
        desired, reason = None, Reason.NOT_BASELINED
    elif temp_demand:
        desired, reason = True, temp_reason
    elif cycling:
        desired, reason = True, Reason.CYCLE
    else:
        desired, reason = False, temp_reason

    return Decision(
        desired_on=desired,
        reason=reason,
        state=new_state,
        sensor_fresh=fresh,
        in_active_hours=active,
        in_cycle=cycling,
    )
