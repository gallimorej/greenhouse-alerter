"""Persistent state: configuration, controller/lease/command state, status, events and API budget.

All logic is in ``Store``. A backend only supplies ``_transact(paths, fn)``: read the documents at
``paths``, call ``fn(docs)`` (which must be pure, because Firestore retries contended transactions),
then atomically write the documents ``fn`` returns. ``MemoryStore`` backs tests and local runs;
``FirestoreStore`` backs Cloud Run.
"""

import copy
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from greenhouse import actuation, appconfig
from greenhouse.actuation import Dispatch, SendCheck
from greenhouse.appconfig import AppConfig
from greenhouse.controller import ControllerState, Decision, Override

CONFIG = "config/settings"
OVERRIDE = "config/override"
CONTROLLER = "controller/state"
STATUS = "controller/status"
BUDGET = "sensorpush/budget"
LINK_CHECK = "sensorpush/link_check"
EVENTS = "events"

# Ticks run about a minute apart, so a fetch is due slightly before a full interval has passed.
POLL_SLACK = timedelta(seconds=15)
# If the worker hasn't fetched for this long past its interval (no sensor linked, job paused), it
# isn't running, and the admin pages stop yielding to it.
WORKER_IDLE_AFTER = timedelta(minutes=2)

Docs = dict[str, dict | None]
Writes = dict[str, dict]


@dataclass(frozen=True)
class TickPlan:
    config: AppConfig
    decision: Decision
    check: SendCheck
    dispatch: Dispatch | None


def _override_from(data: dict | None) -> Override | None:
    if not data:
        return None
    return Override(on=bool(data["on"]), expires_at=data["expires_at"])


def _event(kind: str, actor: str, now: datetime, detail: dict | None = None) -> tuple[str, dict]:
    return f"{EVENTS}/{uuid.uuid4().hex}", {
        "at": now,
        "kind": kind,
        "actor": actor,
        "detail": detail or {},
    }


class Store:
    def _transact(self, paths: list[str], fn: Callable[[Docs], tuple[Writes, object]]):
        raise NotImplementedError

    def recent_events(self, limit: int = 20) -> list[dict]:
        raise NotImplementedError

    # --- reads ------------------------------------------------------------------------------

    def read_raw(self, path: str) -> dict | None:
        return self._transact([path], lambda d: ({}, d[path]))

    def get_config(self) -> AppConfig:
        return self._transact([CONFIG], lambda d: ({}, appconfig.from_dict(d[CONFIG])))

    def get_dashboard(self) -> dict:
        def read(d):
            return {}, {
                "config": appconfig.from_dict(d[CONFIG]),
                "controller": actuation.from_dict(d[CONTROLLER]),
                "status": d[STATUS],
                "override": _override_from(d[OVERRIDE]),
            }

        return self._transact([CONFIG, CONTROLLER, STATUS, OVERRIDE], read)

    # --- admin writes -----------------------------------------------------------------------

    def update_config(
        self,
        change: Callable[[AppConfig], AppConfig],
        actor: str,
        kind: str,
        now: datetime,
        detail: dict | None = None,
    ) -> AppConfig:
        def write(d):
            config = change(appconfig.from_dict(d[CONFIG]))
            path, event = _event(kind, actor, now, detail)
            return {CONFIG: appconfig.to_dict(config), path: event}, config

        return self._transact([CONFIG], write)

    def set_override(self, on: bool, expires_at: datetime, actor: str, now: datetime) -> None:
        def write(d):
            path, event = _event("override_set", actor, now, {"on": on, "expires_at": expires_at})
            return {OVERRIDE: {"on": on, "expires_at": expires_at, "set_by": actor}, path: event}, None

        self._transact([], write)

    def clear_override(self, actor: str, now: datetime) -> None:
        def write(d):
            path, event = _event("override_cleared", actor, now)
            return {OVERRIDE: {}, path: event}, None

        self._transact([], write)

    def reconcile(self, actor: str, now: datetime) -> str:
        def write(d):
            doc, outcome = actuation.reconcile(actuation.from_dict(d[CONTROLLER]), now)
            path, event = _event("reconcile", actor, now, {"outcome": outcome})
            return {CONTROLLER: actuation.to_dict(doc), path: event}, outcome

        return self._transact([CONTROLLER], write)

    def add_event(self, kind: str, actor: str, now: datetime, detail: dict | None = None) -> None:
        self._transact([], lambda d: (dict([_event(kind, actor, now, detail)]), None))

    # --- SensorPush call budget -------------------------------------------------------------

    def consume_sensorpush_call(self, caller: str, now: datetime) -> bool:
        """Take one SensorPush call from the shared per-minute cap, if it's this caller's turn.

        The worker may call only once its poll interval has passed. While the worker is running and
        its fetch is due, admin pages wait, so they can't starve the controller of readings.
        """

        def write(d):
            config = appconfig.from_dict(d[CONFIG])
            budget = d[BUDGET] or {}
            minute = now.strftime("%Y-%m-%dT%H:%M")
            calls = budget.get("calls", 0) if budget.get("minute") == minute else 0
            last_worker = budget.get("last_worker_at")
            since = None if last_worker is None else now - last_worker
            worker_due = since is None or since >= config.sensor_poll_interval - POLL_SLACK
            if caller == "worker" and not worker_due:
                return {}, False
            worker_running = since is not None and since < config.sensor_poll_interval + WORKER_IDLE_AFTER
            if caller == "admin" and worker_due and worker_running:
                return {}, False
            if calls >= config.sensorpush_calls_per_minute:
                return {}, False
            budget = {
                "minute": minute,
                "calls": calls + 1,
                "last_worker_at": now if caller == "worker" else last_worker,
            }
            return {BUDGET: budget}, True

        return self._transact([BUDGET, CONFIG], write)

    def record_link_check(self, sensor_id: str, reading: dict, actor: str, now: datetime) -> None:
        """Remember a passed link test, so confirming it doesn't spend another SensorPush call."""
        doc = {"sensor_id": sensor_id, "reading": reading, "actor": actor, "checked_at": now}
        self._transact([], lambda d: ({LINK_CHECK: doc}, None))

    # --- worker tick ------------------------------------------------------------------------

    def tick_acquire(self, tick_id: str, now: datetime) -> AppConfig:
        def write(d):
            doc = actuation.acquire_lease(actuation.from_dict(d[CONTROLLER]), tick_id, now)
            return {CONTROLLER: actuation.to_dict(doc)}, appconfig.from_dict(d[CONFIG])

        return self._transact([CONTROLLER, CONFIG], write)

    def tick_plan(
        self,
        tick_id: str,
        now: datetime,
        decide: Callable[[AppConfig, ControllerState, Override | None], Decision],
        status_extra: dict,
    ) -> TickPlan:
        def write(d):
            config = appconfig.from_dict(d[CONFIG])
            doc = actuation.from_dict(d[CONTROLLER])
            state = ControllerState(temp_demand=doc.temp_demand, baselined=doc.baselined)
            decision = decide(config, state, _override_from(d[OVERRIDE]))
            doc, check, dispatch = actuation.plan_dispatch(
                doc,
                tick_id,
                now,
                decision.desired_on,
                decision.state.temp_demand,
                decision.state.baselined,
                config.reassert_after,
                config.actuation_enabled,
            )
            status = dict(
                status_extra,
                updated_at=now,
                tick_id=tick_id,
                desired_on=decision.desired_on,
                reason=decision.reason.value,
                sensor_fresh=decision.sensor_fresh,
                in_active_hours=decision.in_active_hours,
                in_cycle=decision.in_cycle,
                send_due=check.should_send,
                send_why=check.why,
                actuation_enabled=config.actuation_enabled,
                dispatch_generation=dispatch.generation if dispatch else None,
            )
            return (
                {CONTROLLER: actuation.to_dict(doc), STATUS: status},
                TickPlan(config, decision, check, dispatch),
            )

        return self._transact([CONFIG, CONTROLLER, OVERRIDE], write)

    def tick_guard(self, tick_id: str, generation: int, now: datetime, budget_ok: bool) -> str | None:
        def write(d):
            config = appconfig.from_dict(d[CONFIG])
            doc, refusal = actuation.guard_dispatch(
                actuation.from_dict(d[CONTROLLER]),
                tick_id,
                generation,
                now,
                config.actuation_enabled,
                budget_ok,
            )
            return {CONTROLLER: actuation.to_dict(doc)}, refusal

        return self._transact([CONFIG, CONTROLLER], write)

    def tick_record(
        self, generation: int, accepted: bool, http_status: int | None, now: datetime, on: bool
    ) -> None:
        def write(d):
            doc = actuation.record_result(
                actuation.from_dict(d[CONTROLLER]), generation, accepted, http_status, now
            )
            kind = "command_accepted" if accepted else "command_failed"
            path, event = _event(kind, "worker", now, {
                "on": on, "generation": generation, "http_status": http_status,
            })
            return {CONTROLLER: actuation.to_dict(doc), path: event}, None

        self._transact([CONTROLLER], write)

    def tick_release(self, tick_id: str) -> None:
        def write(d):
            doc = actuation.release_lease(actuation.from_dict(d[CONTROLLER]), tick_id)
            return {CONTROLLER: actuation.to_dict(doc)}, None

        self._transact([CONTROLLER], write)


class MemoryStore(Store):
    """Thread-safe, in-process store with the same atomicity as a Firestore transaction."""

    def __init__(self, docs: Docs | None = None):
        self.docs: dict[str, dict] = copy.deepcopy(docs or {})
        self._lock = threading.Lock()

    def _transact(self, paths, fn):
        with self._lock:
            snapshot = {p: copy.deepcopy(self.docs.get(p)) for p in paths}
            writes, result = fn(snapshot)
            for path, data in writes.items():
                self.docs[path] = copy.deepcopy(data)
            return result

    def recent_events(self, limit=20):
        with self._lock:
            events = [d for p, d in self.docs.items() if p.startswith(f"{EVENTS}/")]
        return sorted(events, key=lambda e: e["at"], reverse=True)[:limit]


class FirestoreStore(Store):
    def __init__(self, client, timeout_s: float = 5.0):
        self._client = client
        self._timeout_s = timeout_s

    def _transact(self, paths, fn):
        from google.cloud import firestore

        client, timeout = self._client, self._timeout_s

        @firestore.transactional
        def run(transaction):
            docs = {}
            for path in paths:
                snap = client.document(path).get(transaction=transaction, timeout=timeout)
                docs[path] = snap.to_dict() if snap.exists else None
            writes, result = fn(docs)
            for path, data in writes.items():
                transaction.set(client.document(path), data)
            return result

        return run(client.transaction())

    def recent_events(self, limit=20):
        from google.cloud import firestore

        query = (
            self._client.collection(EVENTS)
            .order_by("at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [snap.to_dict() for snap in query.stream(timeout=self._timeout_s)]
