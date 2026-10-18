"""Parse and validate admin form posts into new config values. Pure functions; nothing is saved here."""

import re
from dataclasses import replace
from datetime import datetime, time, timedelta

from greenhouse import appconfig
from greenhouse.appconfig import AppConfig
from greenhouse.controller import Settings

EVENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OVERRIDE_MAX = timedelta(hours=12)


def _float(form, name, errors, lo=None, hi=None):
    try:
        value = float(form.get(name, ""))
    except ValueError:
        errors[name] = "Enter a number."
        return None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        errors[name] = f"Must be between {lo:g} and {hi:g}."
        return None
    return value


def _int(form, name, errors, lo, hi):
    value = _float(form, name, errors, lo, hi)
    if value is None:
        return None
    if value != int(value):
        errors[name] = "Enter a whole number."
        return None
    return int(value)


def _time(form, name, errors):
    try:
        return time.fromisoformat(form.get(name, ""))
    except ValueError:
        errors[name] = "Enter a time as HH:MM."
        return None


def parse_settings(form, current: AppConfig) -> tuple[AppConfig | None, dict[str, str]]:
    errors: dict[str, str] = {}
    values = {
        "high_f": _float(form, "high_f", errors, 40, 140),
        "low_f": _float(form, "low_f", errors, 30, 130),
        "timezone": form.get("timezone", "").strip(),
        "active_start": _time(form, "active_start", errors),
        "active_end": _time(form, "active_end", errors),
        "cycle_interval_minutes": _int(form, "cycle_interval_minutes", errors, 5, 1440),
        "cycle_offset_minutes": _int(form, "cycle_offset_minutes", errors, 0, 1439),
        "cycle_duration_minutes": _int(form, "cycle_duration_minutes", errors, 0, 120),
    }
    stale = _int(form, "stale_after_minutes", errors, 5, 240)
    reassert = _int(form, "reassert_after_minutes", errors, 5, 720)
    calls = _int(form, "sensorpush_calls_per_minute", errors, 1, 10)
    poll = _int(form, "sensor_poll_minutes", errors, 1, 30)
    if poll is not None and stale is not None and poll >= stale:
        errors["sensor_poll_minutes"] = "Must be shorter than the time a reading counts as stale."
    on_event = form.get("ifttt_on_event", "").strip()
    off_event = form.get("ifttt_off_event", "").strip()
    for name, event in (("ifttt_on_event", on_event), ("ifttt_off_event", off_event)):
        if not EVENT_NAME.match(event):
            errors[name] = "Letters, digits, - and _ only (max 64)."
    if not errors and on_event == off_event:
        errors["ifttt_off_event"] = "The on and off events must differ."
    if errors:
        return None, errors
    try:
        settings = Settings(**values, stale_after=timedelta(minutes=stale))
    except Exception as exc:  # Settings validates cross-field rules (low < high, cycle fits, timezone)
        return None, {"form": str(exc) or type(exc).__name__}
    return replace(
        current,
        controller=settings,
        reassert_after=timedelta(minutes=reassert),
        ifttt_on_event=on_event,
        ifttt_off_event=off_event,
        sensorpush_calls_per_minute=calls,
        sensor_poll_interval=timedelta(minutes=poll),
    ), {}


def settings_diff(old: AppConfig, new: AppConfig) -> dict[str, list]:
    """Flat {field: [old, new]} of changed values, for the audit event."""
    a, b = appconfig.to_dict(old), appconfig.to_dict(new)
    flat_a = {**{k: v for k, v in a.items() if k != "controller"}, **a["controller"]}
    flat_b = {**{k: v for k, v in b.items() if k != "controller"}, **b["controller"]}
    return {k: [flat_a.get(k), v] for k, v in flat_b.items() if flat_a.get(k) != v}


def parse_override(form, now: datetime) -> tuple[str | None, datetime | None, str | None]:
    """(mode, expires_at, error). mode is "on", "off" or "auto"."""
    mode = form.get("mode")
    if mode not in ("on", "off", "auto"):
        return None, None, "Choose on, off or auto."
    if mode == "auto":
        return mode, None, None
    try:
        hours = float(form.get("hours", "1"))
    except ValueError:
        return None, None, "Enter how long, in hours."
    duration = timedelta(hours=hours)
    if not timedelta(minutes=15) <= duration <= OVERRIDE_MAX:
        return None, None, "An override lasts between 15 minutes and 12 hours."
    return mode, now + duration, None

