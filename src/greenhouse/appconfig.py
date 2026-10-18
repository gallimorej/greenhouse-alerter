"""Runtime configuration stored in Firestore (``config/settings``), editable without a deploy."""

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import time, timedelta

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
    # Admin-side SensorPush calls per minute; the worker's one call per tick is on top of this.
    sensorpush_admin_calls_per_minute: int = 4

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
    for key in ("actuation_enabled", "active_sensor_id", "ifttt_on_event", "ifttt_off_event",
                "sensorpush_admin_calls_per_minute"):
        if key in data:
            updates[key] = data[key]
    if "reassert_after_minutes" in data:
        updates["reassert_after"] = timedelta(minutes=data["reassert_after_minutes"])
    return replace(config, **updates)
