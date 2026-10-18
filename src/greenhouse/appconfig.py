"""Runtime configuration stored in Firestore (``config/settings``), editable without a deploy."""

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, time, timedelta

from greenhouse.controller import Settings


@dataclass(frozen=True)
class AppConfig:
    controller: Settings = field(default_factory=Settings)
    # Off until someone turns it on: a fresh deployment runs in shadow mode.
    actuation_enabled: bool = False
    active_sensor_id: str | None = None
    reassert_after: timedelta = timedelta(minutes=30)
    ifttt_on_event: str = "start_greenhouse_fans"
    ifttt_off_event: str = "stop_greenhouse_fans"
    # The worker fetches a sample this often and reuses the last one in between. Three minutes leaves
    # the admin pages a window between fetches that is at least a minute from each.
    sensor_poll_interval: timedelta = timedelta(minutes=3)
    # The season switch. Off: the worker sends nothing, doesn't read the sensor, and every alert is
    # snoozed (greenhouse.alerts). Unlike actuation_enabled, it isn't the emergency stop.
    control_enabled: bool = True
    # Whether the alert snoozes match control_enabled. Every season flip and every finished
    # reconcile bumps alerts_revision; a reconcile marks sync only if the revision is unchanged since
    # it started, so overlapping reconciles can't hide a stray snooze. See greenhouse.alerts.
    alerts_revision: int = 0
    alerts_in_sync: bool = True
    alerts_swept_at: datetime | None = None

    def event_for(self, on: bool) -> str:
        return self.ifttt_on_event if on else self.ifttt_off_event


_TIME_FIELDS = {"active_start", "active_end"}
_MINUTE_FIELDS = {"stale_after": "stale_after_minutes"}


def to_dict(config: AppConfig) -> dict:
    controller = {}
    for f in fields(Settings):
        value = getattr(config.controller, f.name)
        if f.name in _TIME_FIELDS:
            controller[f.name] = value.strftime("%H:%M")
        elif f.name in _MINUTE_FIELDS:
            controller[_MINUTE_FIELDS[f.name]] = value.total_seconds() / 60
        else:
            controller[f.name] = value
    data = {k: v for k, v in asdict(config).items() if k != "controller"}
    data["reassert_after_minutes"] = config.reassert_after.total_seconds() / 60
    del data["reassert_after"]
    data["sensor_poll_minutes"] = config.sensor_poll_interval.total_seconds() / 60
    del data["sensor_poll_interval"]
    data["controller"] = controller
    return data


def from_dict(data: dict | None) -> AppConfig:
    if not data:
        return AppConfig()
    raw = dict(data.get("controller") or {})
    kwargs = {}
    for f in fields(Settings):
        key = _MINUTE_FIELDS.get(f.name, f.name)
        if key not in raw:
            continue
        value = raw[key]
        if f.name in _TIME_FIELDS:
            value = time.fromisoformat(value)
        elif f.name in _MINUTE_FIELDS:
            value = timedelta(minutes=value)
        kwargs[f.name] = value
    config = AppConfig(controller=Settings(**kwargs))
    updates = {}
    for key in (
        "actuation_enabled",
        "active_sensor_id",
        "ifttt_on_event",
        "ifttt_off_event",
        "control_enabled",
        "alerts_revision",
        "alerts_in_sync",
        "alerts_swept_at",
    ):
        if key in data:
            updates[key] = data[key]
    if "reassert_after_minutes" in data:
        updates["reassert_after"] = timedelta(minutes=data["reassert_after_minutes"])
    if "sensor_poll_minutes" in data:
        updates["sensor_poll_interval"] = timedelta(minutes=data["sensor_poll_minutes"])
    return replace(config, **updates)
