from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from greenhouse.fans import IftttError
from greenhouse.models import Reading
from greenhouse.sensors import SensorInfo, GatewayInfo, SensorUnavailable


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> datetime:
        self.t += timedelta(**kwargs)
        return self.t


@dataclass
class FakeSensors:
    clock: Clock
    temps: dict[str, float] = field(default_factory=lambda: {"s1": 80.0})
    age: timedelta = timedelta(minutes=1)
    fail: bool = False
    calls: int = 0
    listed: list[SensorInfo] = field(default_factory=lambda: [
        SensorInfo("s1", "Greenhouse HTP.xw", "HTP.xw", True, 3.1, -60.0),
        SensorInfo("s2", "Garage", "HT1", True, 2.9, -70.0),
    ])

    def latest_reading(self, sensor_id, policy=None):
        self.calls += 1
        if self.fail or sensor_id not in self.temps:
            raise SensorUnavailable(f"No samples for sensor {sensor_id}")
        return Reading(sensor_id, self.clock() - self.age, self.temps[sensor_id], 50.0, 29.9)

    def sensors(self, policy=None):
        self.calls += 1
        return list(self.listed)

    def gateways(self, policy=None):
        self.calls += 1
        return [GatewayInfo("g1", "Greenhouse Gateway", self.clock() - timedelta(minutes=1))]


@dataclass
class FakeFans:
    fail_status: int | None = None
    raise_timeout: bool = False
    sent: list[str] = field(default_factory=list)
    on_send: object = None  # callable run mid-send, to simulate concurrent changes

    def trigger(self, event, timeout_s=8.0):
        if self.on_send:
            self.on_send()
        if self.raise_timeout:
            raise IftttError(f"IFTTT event {event!r} timed out", None)
        if self.fail_status:
            raise IftttError(f"IFTTT event {event!r} returned {self.fail_status}", self.fail_status)
        self.sent.append(event)
        return 200


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)
