import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import responses
from werkzeug.datastructures import MultiDict

from greenhouse import appconfig, chart, forms
from greenhouse.appconfig import AppConfig
from greenhouse.controller import Settings, next_cycle_start
from greenhouse.models import Reading
from greenhouse.sensors import API_BASE, CallPolicy, SensorPushClient
from greenhouse.store import CONFIG, STATUS, MemoryStore
from greenhouse.web import IAP_HEADER, Services, ago_until, create_app
from greenhouse.auth import AuthError
from tests.fakes import Clock, FakeSensors, utc

NOON = utc(2026, 7, 15, 16, 20)  # 12:20 in New York
ADMIN = {IAP_HEADER: "good-iap"}
NY = ZoneInfo("America/New_York")


def verify(token):
    if token != "good-iap":
        raise AuthError("bad")
    return "jeff@x"


@pytest.fixture
def clock():
    return Clock(NOON)


@pytest.fixture
def store():
    config = AppConfig(active_sensor_id="s1", actuation_enabled=True)
    return MemoryStore({CONFIG: appconfig.to_dict(config)})


@pytest.fixture
def sensors(clock):
    return FakeSensors(clock, temps={"s1": 85.0})


@pytest.fixture
def admin(store, sensors, clock):
    app = create_app("admin", Services(store=store, sensors=sensors, verifier=verify,
                                       secret_key="k", now=clock))
    return app.test_client()


def token(client, path="/admin/"):
    page = client.get(path, headers=ADMIN).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def post(client, path, data):
    return client.post(path, data=dict(data, csrf_token=token(client)), headers=ADMIN)


SETTINGS_FORM = {
    "high_f": "98", "low_f": "88", "timezone": "America/New_York",
    "active_start": "06:30", "active_end": "22:00",
    "cycle_interval_minutes": "60", "cycle_offset_minutes": "10", "cycle_duration_minutes": "3",
    "stale_after_minutes": "15", "reassert_after_minutes": "45",
    "ifttt_on_event": "start_greenhouse_fans", "ifttt_off_event": "stop_greenhouse_fans",
    "sensorpush_calls_per_minute": "1", "sensor_poll_minutes": "4",
}


# --- forms (pure) --------------------------------------------------------------------------

def test_parse_settings_builds_new_config():
    new, errors = forms.parse_settings(MultiDict(SETTINGS_FORM), AppConfig())
    assert errors == {}
    assert new.controller.high_f == 98 and new.controller.active_start == time(6, 30)
    assert new.controller.stale_after == timedelta(minutes=15)
    assert new.reassert_after == timedelta(minutes=45)
    assert new.sensorpush_calls_per_minute == 1 and new.sensor_poll_interval == timedelta(minutes=4)


@pytest.mark.parametrize(
    "changes, field",
    [
        ({"high_f": "hot"}, "high_f"),
        ({"high_f": "500"}, "high_f"),
        ({"active_start": "25:00"}, "active_start"),
        ({"cycle_duration_minutes": "2.5"}, "cycle_duration_minutes"),
        ({"ifttt_on_event": "bad event!"}, "ifttt_on_event"),
        ({"ifttt_off_event": "start_greenhouse_fans"}, "ifttt_off_event"),
        ({"sensorpush_calls_per_minute": "0"}, "sensorpush_calls_per_minute"),
        ({"sensor_poll_minutes": "15"}, "sensor_poll_minutes"),  # not shorter than stale_after
    ],
)
def test_parse_settings_field_errors(changes, field):
    new, errors = forms.parse_settings(MultiDict(SETTINGS_FORM | changes), AppConfig())
    assert new is None and field in errors


@pytest.mark.parametrize(
    "changes", [{"high_f": "85", "low_f": "90"}, {"cycle_interval_minutes": "7"}, {"timezone": "Nowhere/Land"}]
)
def test_parse_settings_cross_field_errors(changes):
    new, errors = forms.parse_settings(MultiDict(SETTINGS_FORM | changes), AppConfig())
    assert new is None and "form" in errors


def test_settings_diff_lists_only_changes():
    new, _ = forms.parse_settings(MultiDict(SETTINGS_FORM), AppConfig())
    diff = forms.settings_diff(AppConfig(), new)
    assert diff["high_f"] == [100.0, 98.0]
    assert "ifttt_on_event" not in diff


@pytest.mark.parametrize(
    "form, expected",
    [
        ({"mode": "on", "hours": "1"}, ("on", NOON + timedelta(hours=1), None)),
        ({"mode": "auto"}, ("auto", None, None)),
        ({"mode": "on", "hours": "13"}, (None, None, "An override lasts between 15 minutes and 12 hours.")),
        ({"mode": "off", "hours": "0.1"}, (None, None, "An override lasts between 15 minutes and 12 hours.")),
        ({"mode": "toggle"}, (None, None, "Choose on, off or auto.")),
    ],
)
def test_parse_override(form, expected):
    assert forms.parse_override(MultiDict(form), NOON) == expected


# --- controller helper ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "local, expected",
    [((12, 20), (13, 5)), ((12, 4), (12, 5)), ((12, 5), (13, 5)), ((23, 30), (0, 5))],
)
def test_next_cycle_start(local, expected):
    now = datetime(2026, 7, 15, *local, tzinfo=NY).astimezone(timezone.utc)
    nxt = next_cycle_start(now, Settings()).astimezone(NY)
    assert (nxt.hour, nxt.minute) == expected and nxt > now


# --- settings page -------------------------------------------------------------------------

def test_settings_page_renders_current_values(admin):
    page = admin.get("/admin/settings", headers=ADMIN).get_data(as_text=True)
    assert 'name="high_f" type="number" value="100.0"' in page


def test_save_settings_updates_config_and_audits(admin, store):
    resp = post(admin, "/admin/settings", SETTINGS_FORM)
    assert resp.status_code == 302
    config = store.get_config()
    assert config.controller.high_f == 98 and config.controller.cycle_offset_minutes == 10
    assert config.actuation_enabled is True and config.active_sensor_id == "s1"  # untouched
    event = store.recent_events()[0]
    assert event["kind"] == "settings_changed" and event["actor"] == "jeff@x"
    assert event["detail"]["changed"]["high_f"] == [100.0, 98.0]


def test_invalid_settings_rejected_and_redisplayed(admin, store):
    resp = post(admin, "/admin/settings", SETTINGS_FORM | {"high_f": "85", "low_f": "90"})
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert "low_f must be below high_f" in page and 'value="85"' in page
    assert store.get_config().controller.high_f == 100


def test_saving_settings_does_not_clobber_a_concurrent_kill_switch(admin, store, clock):
    csrf = token(admin, "/admin/settings")
    store.update_config(lambda c: c.__class__(**{**c.__dict__, "actuation_enabled": False}), "other", "actuation_disabled", clock())
    admin.post("/admin/settings", data=dict(SETTINGS_FORM, csrf_token=csrf), headers=ADMIN)
    assert store.get_config().actuation_enabled is False


def test_settings_require_csrf(admin, store):
    assert admin.post("/admin/settings", data=SETTINGS_FORM, headers=ADMIN).status_code == 400


# --- manual override -----------------------------------------------------------------------

def test_override_on_shows_pending_then_applied(admin, store, clock):
    post(admin, "/admin/override", {"mode": "on", "hours": "1"})
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Fans ON</strong> by hand" in page and "pending" in page
    store.docs[STATUS] = {"updated_at": clock.advance(seconds=30)}
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Fans ON</strong> by hand" in page and "pending" not in page
    assert store.recent_events()[0]["kind"] == "override_set"


def test_override_back_to_auto(admin, store, clock):
    post(admin, "/admin/override", {"mode": "off", "hours": "2"})
    clock.advance(seconds=1)
    post(admin, "/admin/override", {"mode": "auto"})
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Automatic." in page
    assert store.recent_events()[0]["kind"] == "override_cleared"


def test_expired_override_reads_as_automatic(admin, store, clock):
    post(admin, "/admin/override", {"mode": "on", "hours": "0.25"})
    clock.advance(minutes=16)
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Automatic." in page


def test_override_longer_than_12_hours_is_refused(admin, store):
    post(admin, "/admin/override", {"mode": "on", "hours": "24"})
    assert store.get_dashboard()["override"] is None


def test_dashboard_shows_next_cycle(admin):
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Next scheduled cycle 45 min from now" in page


# --- history -------------------------------------------------------------------------------

def readings_every(minutes, count, end, temp=80.0):
    return [Reading("s1", end - timedelta(minutes=minutes * i), temp + (i % 7)) for i in range(count)][::-1]


def test_history_renders_chart_and_table(admin, sensors, store, clock):
    sensors.history_readings = readings_every(5, 288, clock())
    store.add_event("command_accepted", "worker", clock() - timedelta(hours=3), {"on": True})
    store.add_event("command_accepted", "worker", clock() - timedelta(hours=2), {"on": False})
    page = admin.get("/admin/history?range=24h", headers=ADMIN).get_data(as_text=True)
    assert '<svg class="chart"' in page and 'class="temp-line"' in page
    assert page.count('class="fan-on"') == 1
    assert "High 100°" in page and "Low 90°" in page
    assert "Table view" in page and "<td>" in page


def test_history_is_cached_for_five_minutes(admin, sensors, clock):
    sensors.history_readings = readings_every(5, 10, clock())
    admin.get("/admin/history", headers=ADMIN)
    calls = sensors.calls
    admin.get("/admin/history", headers=ADMIN)
    assert sensors.calls == calls
    clock.advance(minutes=6)
    admin.get("/admin/history", headers=ADMIN)
    assert sensors.calls == calls + 1
    assert all(p.attempts == 1 and not p.reauth_retry for p in sensors.policies)


def test_history_rejects_unknown_range(admin):
    assert admin.get("/admin/history?range=1y", headers=ADMIN).status_code == 400


def test_history_without_sensor(admin, store, clock):
    store.update_config(lambda c: c.__class__(**{**c.__dict__, "active_sensor_id": None}), "x", "sensor_unlinked", clock())
    page = admin.get("/admin/history", headers=ADMIN).get_data(as_text=True)
    assert "No sensor linked" in page and "No samples in this range" in page


def test_history_api_failure_is_shown(admin, sensors):
    sensors.fail = True
    page = admin.get("/admin/history", headers=ADMIN).get_data(as_text=True)
    assert "Couldn&#39;t load history" in page or "Couldn't load history" in page


# --- chart (pure) --------------------------------------------------------------------------

def test_bucketize_averages_and_bounds():
    start = NOON - timedelta(hours=1)
    rs = [Reading("s", start + timedelta(minutes=m), float(m)) for m in range(60)]
    buckets = chart.bucketize(rs, start, NOON, n=6)
    assert len(buckets) == 6 and buckets[0].count == 10
    assert (buckets[0].min_f, buckets[0].max_f, buckets[0].avg_f) == (0, 9, 4.5)


def test_on_periods_from_events():
    start, end = NOON - timedelta(hours=10), NOON
    events = [
        {"kind": "command_accepted", "at": start - timedelta(hours=1), "detail": {"on": True}},
        {"kind": "command_accepted", "at": start + timedelta(hours=1), "detail": {"on": False}},
        {"kind": "settings_changed", "at": start + timedelta(hours=2), "detail": {}},
        {"kind": "command_accepted", "at": start + timedelta(hours=5), "detail": {"on": True}},
        {"kind": "command_accepted", "at": start + timedelta(hours=5, minutes=10), "detail": {"on": True}},
    ]
    assert chart.on_periods(events, start, end) == [
        (start, start + timedelta(hours=1)),
        (start + timedelta(hours=5), end),
    ]


def test_chart_escapes_and_has_hover_titles():
    start = NOON - timedelta(hours=2)
    rs = [Reading("s", start + timedelta(minutes=10 * i), 90.0 + i) for i in range(12)]
    c = chart.render(rs, [], start, NOON, 100, 90, "America/New_York")
    assert "<title>" in c.svg and "°F (low" in c.svg
    assert 'role="img"' in c.svg


def test_ago_until():
    assert ago_until(NOON + timedelta(minutes=45), NOON) == "45 min from now"
    assert ago_until(NOON + timedelta(hours=2), NOON) == "2 h from now"
    assert ago_until(NOON + timedelta(hours=1, minutes=5), NOON) == "1 h 5 min from now"


# --- SensorPush history client -------------------------------------------------------------

@responses.activate
def test_history_client_sends_start_time_only_and_sorts_oldest_first():
    responses.post(f"{API_BASE}/oauth/authorize", json={"authorization": "a"})
    responses.post(f"{API_BASE}/oauth/accesstoken", json={"accesstoken": "t"})
    responses.post(
        f"{API_BASE}/samples",
        json={"truncated": False, "sensors": {"s1": [
            {"observed": "2026-07-15T16:10:00.000Z", "temperature": 81},
            {"observed": "2026-07-15T16:00:00.000Z", "temperature": "bad"},
            {"observed": None, "temperature": 90},
            {"observed": "2026-07-15T15:55:00", "temperature": 90},
            "not-a-sample",
            {"observed": "2026-07-15T15:50:00.000Z", "temperature": 79},
        ]}},
        match=[responses.matchers.json_params_matcher(
            {"sensors": ["s1"], "startTime": "2026-07-14T16:20:00.000Z", "limit": 12000}
        )],
    )
    readings, truncated = SensorPushClient("e", "p").history(
        "s1", NOON - timedelta(hours=24), CallPolicy(attempts=1)
    )
    assert [r.temperature_f for r in readings] == [79, 81] and truncated is False


@pytest.mark.parametrize("preferred", [95.0, 100.0, 104.0, 108.0])
def test_end_label_clears_threshold_labels(preferred):
    thresholds = [100.0, 160.0]  # threshold label baselines sit at y + 4
    y = chart._end_label_y(preferred, thresholds)
    assert all(abs((t + 4) - y) >= chart.LABEL_GAP for t in thresholds)


def test_end_label_left_alone_when_clear():
    assert chart._end_label_y(50.0, [100.0, 160.0]) == 50.0
