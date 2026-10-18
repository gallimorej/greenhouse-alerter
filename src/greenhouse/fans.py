"""IFTTT Webhooks client for the fan switch.

The webhook key is part of the URL path, so nothing here may let the URL reach a log or an
exception message: requests' own exceptions include the full URL, so they're replaced, not chained.
"""

import requests

IFTTT_URL = "https://maker.ifttt.com/trigger/{event}/with/key/{key}"


class IftttError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class IftttClient:
    def __init__(self, key: str, session: requests.Session | None = None):
        self._key = key
        self._session = session or requests.Session()

    def trigger(self, event: str, timeout_s: float = 8.0) -> int:
        """Fire one event, once. Returns the HTTP status; raises IftttError on any failure."""
        url = IFTTT_URL.format(event=event, key=self._key)
        try:
            resp = self._session.post(url, timeout=timeout_s)
        except requests.Timeout:
            raise IftttError(f"IFTTT event {event!r} timed out after {timeout_s}s") from None
        except requests.RequestException as exc:
            raise IftttError(f"IFTTT event {event!r} failed: {type(exc).__name__}") from None
        if not resp.ok:
            raise IftttError(f"IFTTT event {event!r} returned {resp.status_code}", resp.status_code)
        return resp.status_code
