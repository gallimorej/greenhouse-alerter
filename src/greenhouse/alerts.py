"""Silencing every alert while the season is paused, with Cloud Monitoring snoozes.

The season switch (``AppConfig.control_enabled``) is the desired state. ``Snoozer.reconcile`` makes the
project's snoozes match it, and is safe to run any number of times, from any instance:

- paused: a snooze named SNOOZE_NAME covers every alert policy, starting now and ending far ahead
- active: no such snooze is still running

The app's snoozes are found by their display name, not by a stored id, so one created by a request
whose response was lost is found on the next pass instead of being duplicated or orphaned. The store's
revision check (``Store.alerts_begin`` / ``alerts_finish``) decides whether a pass may mark the alerts
in sync. The admin action runs a pass straight after a flip; the worker runs one on every tick while
they're out of sync, and every SWEEP_EVERY regardless, which catches a pass that crashed before its
closing transaction.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from greenhouse.store import Store

log = logging.getLogger(__name__)

SNOOZE_NAME = "greenhouse-season-pause"
# Cloud Monitoring documents no maximum. Paused ticks extend it anyway once it's within EXTEND_WITHIN
# of ending.
SNOOZE_LENGTH = timedelta(days=400)
EXTEND_WITHIN = timedelta(days=1)
SWEEP_EVERY = timedelta(minutes=15)

API = "https://monitoring.googleapis.com/v3"


class AlertsError(Exception):
    pass


@dataclass(frozen=True)
class Snooze:
    name: str
    display_name: str
    start: datetime
    end: datetime


class SnoozeApi(Protocol):
    def list(self, timeout_s: float) -> list[Snooze]: ...

    def create(self, policies: Sequence[str], start: datetime, end: datetime, timeout_s: float) -> None: ...

    def end(self, snooze: Snooze, end: datetime, timeout_s: float) -> None: ...


def _timestamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    # The API returns RFC 3339 with up to nanoseconds; Python parses up to microseconds.
    head, _, frac = value.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}+00:00")


class MonitoringSnoozes:
    """The Cloud Monitoring snoozes API, as the service's own identity."""

    def __init__(self, project: str, session=None):
        self._parent = f"projects/{project}"
        self._session = session

    def _http(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/monitoring"])
            self._session = AuthorizedSession(credentials)
        return self._session

    def _call(self, method: str, url: str, timeout_s: float, **kwargs) -> dict:
        try:
            response = self._http().request(method, url, timeout=timeout_s, **kwargs)
        except Exception as exc:  # transport errors, timeouts, credential refresh failures
            raise AlertsError(f"snooze {method} failed: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise AlertsError(f"snooze {method} returned {response.status_code}")
        return response.json() if response.content else {}

    def list(self, timeout_s):
        snoozes, token = [], None
        while True:
            params = {"pageToken": token} if token else {}
            data = self._call("GET", f"{API}/{self._parent}/snoozes", timeout_s, params=params)
            for s in data.get("snoozes", []):
                snoozes.append(Snooze(
                    name=s["name"],
                    display_name=s.get("displayName", ""),
                    start=_parse_timestamp(s["interval"]["startTime"]),
                    end=_parse_timestamp(s["interval"]["endTime"]),
                ))
            token = data.get("nextPageToken")
            if not token:
                return snoozes

    def create(self, policies, start, end, timeout_s):
        body = {
            "displayName": SNOOZE_NAME,
            "criteria": {"policies": list(policies)},
            "interval": {"startTime": _timestamp(start), "endTime": _timestamp(end)},
        }
        self._call("POST", f"{API}/{self._parent}/snoozes", timeout_s, json=body)

    def end(self, snooze, end, timeout_s):
        body = {"interval": {"startTime": _timestamp(snooze.start), "endTime": _timestamp(end)}}
        self._call(
            "PATCH", f"{API}/{snooze.name}", timeout_s, params={"updateMask": "interval.endTime"}, json=body
        )


def sync_snoozes(api: SnoozeApi, policies: Sequence[str], paused: bool, now: datetime, timeout_s: float) -> str:
    """Make the app's snoozes match ``paused``. Returns what it did."""
    mine = [s for s in api.list(timeout_s) if s.display_name == SNOOZE_NAME]
    if paused:
        if any(s.start <= now and s.end > now + EXTEND_WITHIN for s in mine):
            return "already snoozed"
        api.create(policies, now, now + SNOOZE_LENGTH, timeout_s)
        return "snoozed"
    # Ended by setting the end time to now. If the API refuses (a snooze's end may have to be a minute
    # after its start), the pass fails and the alerts stay out of sync until a later pass ends it.
    running = [s for s in mine if s.end > now]
    for s in running:
        api.end(s, now, timeout_s)
    return f"ended {len(running)}" if running else "nothing snoozed"


@dataclass(frozen=True)
class Snoozer:
    api: SnoozeApi
    policies: tuple[str, ...]

    def reconcile(self, store: Store, now: datetime, timeout_s: float = 5.0) -> bool:
        """One pass. Returns whether the alerts are now marked in sync."""
        revision, paused = store.alerts_begin()
        try:
            action = sync_snoozes(self.api, self.policies, paused, now, timeout_s)
            ok = True
        except AlertsError as exc:
            log.warning(
                "alert snooze reconcile failed",
                extra={"json": {"event": "alerts_reconcile_failed", "paused": paused, "error": str(exc)}},
            )
            action, ok = None, False
        synced = store.alerts_finish(revision, ok, now)
        if ok:
            log.info(
                "alert snoozes reconciled",
                extra={"json": {"event": "alerts_reconciled", "paused": paused, "action": action, "synced": synced}},
            )
        return synced


def due(config, now: datetime) -> bool:
    """Whether the worker should run a pass this tick."""
    swept = config.alerts_swept_at
    return not config.alerts_in_sync or swept is None or now - swept >= SWEEP_EVERY
