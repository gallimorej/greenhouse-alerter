from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from greenhouse.alerts import SNOOZE_NAME, AlertsError, Snooze
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
    history_readings: list[Reading] = field(default_factory=list)
    policies: list = field(default_factory=list)
    listed: list[SensorInfo] = field(default_factory=lambda: [
        SensorInfo("s1", "Greenhouse HTP.xw", "HTP.xw", True, 3.1, -60.0),
        SensorInfo("s2", "Garage", "HT1", True, 2.9, -70.0),
    ])

    def latest_reading(self, sensor_id, policy=None):
        self.calls += 1
        self.policies.append(policy)
        if self.fail or sensor_id not in self.temps:
            raise SensorUnavailable(f"No samples for sensor {sensor_id}")
        return Reading(sensor_id, self.clock() - self.age, self.temps[sensor_id], 50.0, 29.9)

    def history(self, sensor_id, start, policy=None, limit=12000):
        self.calls += 1
        self.policies.append(policy)
        if self.fail:
            raise SensorUnavailable("SensorPush /samples returned 503")
        return list(self.history_readings), False

    def sensors(self, policy=None):
        self.calls += 1
        self.policies.append(policy)
        return list(self.listed)

    def gateways(self, policy=None):
        self.calls += 1
        self.policies.append(policy)
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


@dataclass
class FakeSnoozes:
    """The Cloud Monitoring snoozes API, in memory."""

    snoozes: list[Snooze] = field(default_factory=list)
    fail: bool = False
    # The POST lands but its response is lost, as with a timeout after the server committed.
    lose_create_response: bool = False
    created: list[tuple] = field(default_factory=list)
    # Refuse an end time less than this after the start, as the API may.
    min_length: timedelta = timedelta(0)

    def list(self, timeout_s):
        if self.fail:
            raise AlertsError("snooze GET returned 503")
        return list(self.snoozes)

    def create(self, policies, start, end, timeout_s):
        if self.fail:
            raise AlertsError("snooze POST returned 503")
        self.created.append(tuple(policies))
        self.snoozes.append(Snooze(f"projects/p/snoozes/{len(self.snoozes) + 1}", SNOOZE_NAME, start, end))
        if self.lose_create_response:
            raise AlertsError("snooze POST failed: ReadTimeout")

    def end(self, snooze, end, timeout_s):
        if self.fail:
            raise AlertsError("snooze PATCH returned 503")
        if end - snooze.start < self.min_length:
            raise AlertsError("snooze PATCH returned 400")
        self.snoozes = [replace(s, end=end) if s.name == snooze.name else s for s in self.snoozes]

    def running(self, now):
        return [s for s in self.snoozes if s.display_name == SNOOZE_NAME and s.start <= now < s.end]
