"""SensorPush cloud API client.

API reference: https://www.sensorpush.com/gateway-cloud-api. Every call is a POST with a JSON body;
the access token goes in the ``Authorization`` header as-is (no "Bearer" prefix).
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import requests

from greenhouse.models import Reading

API_BASE = "https://api.sensorpush.com/api/v1"

# The access token's lifetime isn't returned with it. Refresh it every 30 minutes, and right away on
# any 401/403.
TOKEN_TTL = timedelta(minutes=30)


class SensorUnavailable(Exception):
    """No usable reading: the API failed, or the sensor reported nothing."""


@dataclass(frozen=True)
class CallPolicy:
    """How hard one caller tries. The worker uses a single short attempt; admin pages can retry."""

    timeout_s: float = 10.0
    attempts: int = 3
    backoff_s: float = 1.0


WORKER_POLICY = CallPolicy(timeout_s=8.0, attempts=1)
ADMIN_POLICY = CallPolicy()

# A week at one sample a minute is 10,080 samples, so one call covers it.
HISTORY_LIMIT = 12000

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class SensorInfo:
    id: str
    name: str
    type: str | None
    active: bool
    battery_voltage: float | None
    rssi: float | None


@dataclass(frozen=True)
class GatewayInfo:
    id: str
    name: str
    last_seen: datetime | None


class SensorSource(Protocol):
    def latest_reading(self, sensor_id: str, policy: CallPolicy = WORKER_POLICY) -> Reading: ...

    def sensors(self, policy: CallPolicy = ADMIN_POLICY) -> list[SensorInfo]: ...

    def gateways(self, policy: CallPolicy = ADMIN_POLICY) -> list["GatewayInfo"]: ...

    def history(
        self, sensor_id: str, start: datetime, policy: CallPolicy = ADMIN_POLICY, limit: int = ...
    ) -> tuple[list[Reading], bool]: ...


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_float(value) -> float | None:
    return None if value is None else float(value)


class SensorPushClient:
    def __init__(self, email: str, password: str, session: requests.Session | None = None):
        self._email = email
        self._password = password
        self._session = session or requests.Session()
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    def _post(self, path: str, body: dict, policy: CallPolicy, token: str | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = token
        last_error: Exception | None = None
        for attempt in range(policy.attempts):
            if attempt:
                time.sleep(policy.backoff_s * 2 ** (attempt - 1))
            try:
                resp = self._session.post(
                    f"{API_BASE}{path}", json=body, headers=headers, timeout=policy.timeout_s
                )
            except requests.RequestException as exc:
                last_error = exc
                continue
            if resp.status_code in RETRYABLE_STATUS:
                last_error = SensorUnavailable(f"SensorPush {path} returned {resp.status_code}")
                continue
            if resp.status_code in (401, 403) and token:
                raise _AuthExpired()
            if not resp.ok:
                raise SensorUnavailable(f"SensorPush {path} returned {resp.status_code}")
            return resp.json()
        raise SensorUnavailable(f"SensorPush {path} failed: {last_error}") from last_error

    def _access_token(self, policy: CallPolicy, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.monotonic() >= self._token_expires:
                auth = self._post(
                    "/oauth/authorize", {"email": self._email, "password": self._password}, policy
                )
                token = self._post(
                    "/oauth/accesstoken", {"authorization": auth["authorization"]}, policy
                )
                self._token = token["accesstoken"]
                self._token_expires = time.monotonic() + TOKEN_TTL.total_seconds()
            return self._token

    def _authed(self, path: str, body: dict, policy: CallPolicy) -> dict:
        try:
            return self._post(path, body, policy, token=self._access_token(policy))
        except _AuthExpired:
            return self._post(path, body, policy, token=self._access_token(policy, force=True))

    def latest_reading(self, sensor_id: str, policy: CallPolicy = WORKER_POLICY) -> Reading:
        data = self._authed("/samples", {"sensors": [sensor_id], "limit": 1}, policy)
        samples = (data.get("sensors") or {}).get(sensor_id) or []
        if not samples:
            raise SensorUnavailable(f"No samples for sensor {sensor_id}")
        sample = samples[0]
        # Field names confirmed against the live API with HT1 sensors (2026-10-17). HT1s don't report
        # pressure; "barometric_pressure" is the documented name, still to confirm once the HTP.xw is linked.
        try:
            return Reading(
                sensor_id=sensor_id,
                observed_at=_parse_time(sample["observed"]),
                temperature_f=float(sample["temperature"]),
                humidity=_optional_float(sample.get("humidity")),
                pressure=_optional_float(sample.get("barometric_pressure")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SensorUnavailable(f"Malformed sample for sensor {sensor_id}: {exc}") from exc

    def history(
        self, sensor_id: str, start: datetime, policy: CallPolicy = ADMIN_POLICY, limit: int = HISTORY_LIMIT
    ) -> tuple[list[Reading], bool]:
        """Samples from ``start`` until now, oldest first, and whether the API truncated them.

        Only startTime is sent: with stopTime, the live API returned no samples at all (checked
        2026-10-17). startTime plus newest-first ordering is all this needs.
        """
        body = {
            "sensors": [sensor_id],
            "startTime": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "limit": limit,
        }
        data = self._authed("/samples", body, policy)
        readings = []
        for sample in (data.get("sensors") or {}).get(sensor_id) or []:
            try:
                readings.append(Reading(
                    sensor_id=sensor_id,
                    observed_at=_parse_time(sample["observed"]),
                    temperature_f=float(sample["temperature"]),
                    humidity=_optional_float(sample.get("humidity")),
                    pressure=_optional_float(sample.get("barometric_pressure")),
                ))
            except (KeyError, TypeError, ValueError):
                continue  # one bad sample shouldn't blank the whole chart
        readings.sort(key=lambda r: r.observed_at)
        truncated = bool(data.get("truncated")) or len(readings) >= limit
        return readings, truncated

    def sensors(self, policy: CallPolicy = ADMIN_POLICY) -> list[SensorInfo]:
        data = self._authed("/devices/sensors", {}, policy)
        return [
            SensorInfo(
                id=sensor_id,
                name=info.get("name", sensor_id),
                type=info.get("type"),
                active=bool(info.get("active", False)),
                battery_voltage=_optional_float(info.get("battery_voltage")),
                rssi=_optional_float(info.get("rssi")),
            )
            for sensor_id, info in data.items()
        ]

    def gateways(self, policy: CallPolicy = ADMIN_POLICY) -> list[GatewayInfo]:
        data = self._authed("/devices/gateways", {}, policy)
        return [
            GatewayInfo(
                id=gateway_id,
                name=info.get("name", gateway_id),
                last_seen=_parse_time(info.get("last_seen")),
            )
            for gateway_id, info in data.items()
        ]


class _AuthExpired(Exception):
    pass
