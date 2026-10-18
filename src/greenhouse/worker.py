"""One controller tick: the only code path that sends fan commands.

1. acquire the lease (txn), getting the current config
2. read the linked sensor, outside any transaction
3. plan (txn): decide, then record state, status and, if due, a pending command
4. guard (txn): refuse unless the lease, budget, kill switch and generation all still hold
5. call IFTTT, outside any transaction
6. record the result (txn), then release the lease

The guard reads the kill switch from Firestore, so turning it off stops even a tick already in flight.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from greenhouse import controller
from greenhouse.actuation import LeaseHeld, LeaseLost
from greenhouse.fans import IftttClient, IftttError
from greenhouse.models import Reading
from greenhouse.sensors import CallPolicy, SensorSource, SensorUnavailable
from greenhouse.store import Store

log = logging.getLogger(__name__)

TICK_BUDGET = timedelta(seconds=20)
SENSOR_TIMEOUT_S = 8.0
IFTTT_TIMEOUT_S = 8.0
# Leave room after the IFTTT call to record its result.
RECORD_MARGIN_S = 2.0


class Deadline:
    def __init__(self, budget: timedelta, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + budget.total_seconds()

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())


@dataclass(frozen=True)
class TickResult:
    outcome: str  # skipped | planned | sent | refused | failed
    detail: dict


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reading_status(reading: Reading | None, sensor_id: str | None, error: str | None) -> dict:
    return {
        "sensor_id": sensor_id,
        "sensor_error": error,
        "reading": None if reading is None else {
            "observed_at": reading.observed_at,
            "temperature_f": reading.temperature_f,
            "humidity": reading.humidity,
            "pressure": reading.pressure,
        },
    }


def run_tick(
    store: Store,
    sensors: SensorSource,
    fans: IftttClient,
    now: Callable[[], datetime] = _utcnow,
    deadline: Deadline | None = None,
    tick_id: str | None = None,
) -> TickResult:
    tick_id = tick_id or uuid.uuid4().hex
    deadline = deadline or Deadline(TICK_BUDGET)

    try:
        config = store.tick_acquire(tick_id, now())
    except LeaseHeld as held:
        log.info("tick skipped: lease held", extra={"json": {"event": "tick", "outcome": "skipped", "holder": str(held)}})
        return TickResult("skipped", {"holder": str(held)})

    try:
        reading, error = None, None
        sensor_id = config.active_sensor_id
        if sensor_id is None:
            error = "no sensor linked"
        elif deadline.remaining() < 1:
            error = "execution budget exhausted before sensor read"
        else:
            store.consume_sensorpush_call("worker", now())
            policy = CallPolicy(timeout_s=min(SENSOR_TIMEOUT_S, deadline.remaining() - 1), attempts=1)
            try:
                reading = sensors.latest_reading(sensor_id, policy)
            except SensorUnavailable as exc:
                error = str(exc)

        def decide(cfg, state, override):
            return controller.decide(now(), cfg.controller, reading, state, override)

        plan = store.tick_plan(tick_id, now(), decide, _reading_status(reading, sensor_id, error))
        detail = {
            "event": "tick",
            "tick_id": tick_id,
            "sensor_id": sensor_id,
            "sensor_error": error,
            "temperature_f": reading.temperature_f if reading else None,
            "sensor_fresh": plan.decision.sensor_fresh,
            "desired_on": plan.decision.desired_on,
            "reason": plan.decision.reason.value,
            "send_due": plan.check.should_send,
            "send_why": plan.check.why,
            "actuation_enabled": plan.config.actuation_enabled,
        }

        if plan.dispatch is None:
            detail["outcome"] = "planned"
            log.info("tick planned", extra={"json": detail})
            return TickResult("planned", detail)

        dispatch = plan.dispatch
        detail["generation"] = dispatch.generation
        budget_ok = deadline.remaining() >= IFTTT_TIMEOUT_S + RECORD_MARGIN_S
        refusal = store.tick_guard(tick_id, dispatch.generation, now(), budget_ok)
        if refusal is not None:
            detail.update(outcome="refused", refusal=refusal)
            log.warning("tick refused to dispatch", extra={"json": detail})
            return TickResult("refused", detail)

        event = plan.config.event_for(dispatch.on)
        try:
            status = fans.trigger(event, timeout_s=min(IFTTT_TIMEOUT_S, deadline.remaining()))
            store.tick_record(dispatch.generation, True, status, now(), dispatch.on)
            detail.update(outcome="sent", http_status=status)
            log.info("tick sent command", extra={"json": detail})
            return TickResult("sent", detail)
        except IftttError as exc:
            store.tick_record(dispatch.generation, False, exc.status, now(), dispatch.on)
            detail.update(outcome="failed", ifttt_error=str(exc), http_status=exc.status)
            log.error("tick command failed", extra={"json": detail})
            return TickResult("failed", detail)
    except LeaseLost:
        log.warning("tick lost its lease", extra={"json": {"event": "tick", "outcome": "lease_lost", "tick_id": tick_id}})
        return TickResult("refused", {"refusal": "lease lost"})
    finally:
        store.tick_release(tick_id)
