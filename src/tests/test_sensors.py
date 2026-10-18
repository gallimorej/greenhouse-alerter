from datetime import datetime, timezone

import pytest
import requests
import responses
from responses import matchers

from greenhouse.sensors import API_BASE, CallPolicy, SensorPushClient, SensorUnavailable

NO_WAIT = CallPolicy(timeout_s=1, attempts=3, backoff_s=0)
ONE_SHOT = CallPolicy(timeout_s=1, attempts=1)


def mock_login(rsps, token="tok-1"):
    rsps.post(
        f"{API_BASE}/oauth/authorize",
        json={"authorization": "auth-code"},
        match=[matchers.json_params_matcher({"email": "e@x", "password": "pw"})],
    )
    rsps.post(
        f"{API_BASE}/oauth/accesstoken",
        json={"accesstoken": token},
        match=[matchers.json_params_matcher({"authorization": "auth-code"})],
    )


def sample(**overrides):
    base = {
        "observed": "2026-07-15T16:20:00.000Z",
        "temperature": 101.3,
        "humidity": 55.1,
        "barometric_pressure": 29.92,
    }
    return base | overrides


@responses.activate
def test_latest_reading_parses_htp_xw_sample():
    mock_login(responses)
    responses.post(
        f"{API_BASE}/samples",
        json={"sensors": {"s1": [sample()]}},
        match=[
            matchers.header_matcher({"Authorization": "tok-1"}),
            matchers.json_params_matcher({"sensors": ["s1"], "limit": 1}),
        ],
    )
    r = SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)
    assert r.sensor_id == "s1"
    assert r.observed_at == datetime(2026, 7, 15, 16, 20, tzinfo=timezone.utc)
    assert (r.temperature_f, r.humidity, r.pressure) == (101.3, 55.1, 29.92)


@responses.activate
def test_reading_without_pressure_is_fine():
    mock_login(responses)
    s = sample()
    del s["barometric_pressure"]
    responses.post(f"{API_BASE}/samples", json={"sensors": {"s1": [s]}})
    assert SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT).pressure is None


@pytest.mark.parametrize(
    "body",
    [
        {"sensors": {}},
        {"sensors": {"s1": []}},
        {},
        {"sensors": {"s1": [{"observed": "2026-07-15T16:20:00Z"}]}},
        {"sensors": {"s1": [sample(temperature="n/a")]}},
        {"sensors": {"s1": [sample(observed=None)]}},
        {"sensors": {"s1": [sample(observed="")]}},
        {"sensors": {"s1": [sample(observed=1752596400)]}},
        {"sensors": {"s1": [sample(observed="2026-07-15T16:20:00")]}},
        {"sensors": {"s1": [{k: v for k, v in sample().items() if k != "observed"}]}},
        {"sensors": {"s1": ["not-a-sample"]}},
        {"sensors": {"s1": {"not": "a list"}}},
        {"sensors": ["s1"]},
    ],
)
@responses.activate
def test_missing_or_malformed_samples_raise_sensor_unavailable(body):
    mock_login(responses)
    responses.post(f"{API_BASE}/samples", json=body)
    with pytest.raises(SensorUnavailable):
        SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)


@responses.activate
def test_token_is_cached_between_calls():
    mock_login(responses)
    responses.post(f"{API_BASE}/samples", json={"sensors": {"s1": [sample()]}})
    client = SensorPushClient("e@x", "pw")
    client.latest_reading("s1", ONE_SHOT)
    client.latest_reading("s1", ONE_SHOT)
    authorize_calls = [c for c in responses.calls if c.request.url.endswith("/oauth/authorize")]
    assert len(authorize_calls) == 1


@responses.activate
def test_expired_token_is_refreshed_once():
    mock_login(responses, token="tok-1")
    responses.post(f"{API_BASE}/samples", status=401)
    client = SensorPushClient("e@x", "pw")
    client._access_token(ONE_SHOT)
    responses.replace(responses.POST, f"{API_BASE}/oauth/accesstoken", json={"accesstoken": "tok-2"})
    responses.post(
        f"{API_BASE}/samples",
        json={"sensors": {"s1": [sample()]}},
        match=[matchers.header_matcher({"Authorization": "tok-2"})],
    )
    assert client.latest_reading("s1", ONE_SHOT).temperature_f == 101.3


@responses.activate
def test_worker_policy_does_not_retry():
    mock_login(responses)
    responses.post(f"{API_BASE}/samples", status=503)
    with pytest.raises(SensorUnavailable):
        SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)
    assert sum(c.request.url.endswith("/samples") for c in responses.calls) == 1


@responses.activate
def test_admin_policy_retries_rate_limits():
    mock_login(responses)
    responses.post(f"{API_BASE}/samples", status=429)
    responses.post(f"{API_BASE}/samples", json={"sensors": {"s1": [sample()]}})
    assert SensorPushClient("e@x", "pw").latest_reading("s1", NO_WAIT).temperature_f == 101.3


@responses.activate
def test_connection_errors_become_sensor_unavailable():
    responses.post(f"{API_BASE}/oauth/authorize", body=requests.ConnectionError("boom"))
    with pytest.raises(SensorUnavailable):
        SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)


@responses.activate
def test_sensors_and_gateways_listing():
    mock_login(responses)
    responses.post(
        f"{API_BASE}/devices/sensors",
        json={
            "s1": {
                "name": "Greenhouse HTP.xw",
                "type": "HTP.xw",
                "active": True,
                "battery_voltage": 3.1,
                "rssi": -60,
            },
            "s2": {"name": "Old HT1", "active": False},
        },
    )
    responses.post(
        f"{API_BASE}/devices/gateways",
        json={"g1": {"name": "Barn", "last_seen": "2026-07-15T16:19:00Z"}},
    )
    client = SensorPushClient("e@x", "pw")
    sensors = {s.id: s for s in client.sensors(ONE_SHOT)}
    assert sensors["s1"].name == "Greenhouse HTP.xw" and sensors["s1"].active
    assert sensors["s1"].type == "HTP.xw"
    assert sensors["s2"].battery_voltage is None and sensors["s2"].type is None
    (gateway,) = client.gateways(ONE_SHOT)
    assert gateway.last_seen == datetime(2026, 7, 15, 16, 19, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "kwargs",
    [{"body": "<html>Bad gateway</html>", "content_type": "text/html"}, {"json": ["a", "list"]}],
)
@responses.activate
def test_successful_but_unparseable_response_raises_sensor_unavailable(kwargs):
    mock_login(responses)
    responses.post(f"{API_BASE}/samples", status=200, **kwargs)
    with pytest.raises(SensorUnavailable):
        SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)


@responses.activate
def test_login_response_without_token_raises_sensor_unavailable():
    responses.post(f"{API_BASE}/oauth/authorize", json={"authorization": "auth-code"})
    responses.post(f"{API_BASE}/oauth/accesstoken", json={"unexpected": True})
    with pytest.raises(SensorUnavailable):
        SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)


@pytest.mark.parametrize("method, path", [("sensors", "/devices/sensors"), ("gateways", "/devices/gateways")])
@responses.activate
def test_malformed_device_listings_raise_sensor_unavailable(method, path):
    mock_login(responses)
    responses.post(f"{API_BASE}{path}", json={"id-1": "not-an-object"})
    with pytest.raises(SensorUnavailable):
        getattr(SensorPushClient("e@x", "pw"), method)(ONE_SHOT)


@pytest.mark.parametrize("status", [401, 403])
@responses.activate
def test_unauthorized_after_refresh_raises_sensor_unavailable(status):
    mock_login(responses)
    responses.post(f"{API_BASE}/samples", status=status)
    with pytest.raises(SensorUnavailable):
        SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)
    authorize_calls = [c for c in responses.calls if c.request.url.endswith("/oauth/authorize")]
    assert len(authorize_calls) == 2


@pytest.mark.parametrize("token", [None, "", 123])
@responses.activate
def test_empty_or_non_string_access_token_raises_sensor_unavailable(token):
    responses.post(f"{API_BASE}/oauth/authorize", json={"authorization": "auth-code"})
    responses.post(f"{API_BASE}/oauth/accesstoken", json={"accesstoken": token})
    with pytest.raises(SensorUnavailable):
        SensorPushClient("e@x", "pw").latest_reading("s1", ONE_SHOT)
