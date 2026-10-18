import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import responses
from werkzeug.datastructures import MultiDict

from greenhouse import actuation, appconfig, chart, forms
from greenhouse.actuation import Command, ControllerDoc
from greenhouse.appconfig import AppConfig
from greenhouse.controller import Settings, next_cycle_start
from greenhouse.models import Reading
from greenhouse.sensors import API_BASE, CallPolicy, SensorPushClient
from greenhouse.store import CONFIG, CONTROLLER, STATUS, MemoryStore
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
    "sensor_poll_minutes": "4",
}


# --- forms (pure) --------------------------------------------------------------------------

def test_parse_settings_builds_new_config():
    new, errors = forms.parse_settings(MultiDict(SETTINGS_FORM), AppConfig())
    assert errors == {}
    assert new.controller.high_f == 98 and new.controller.active_start == time(6, 30)
    assert new.controller.stale_after == timedelta(minutes=15)
    assert new.reassert_after == timedelta(minutes=45)
    assert new.sensor_poll_interval == timedelta(minutes=4)


@pytest.mark.parametrize(
    "changes, field",
    [
        ({"high_f": "hot"}, "high_f"),
        ({"high_f": "500"}, "high_f"),
        ({"active_start": "25:00"}, "active_start"),
        ({"cycle_duration_minutes": "2.5"}, "cycle_duration_minutes"),
        ({"ifttt_on_event": "bad event!"}, "ifttt_on_event"),
        ({"ifttt_off_event": "start_greenhouse_fans"}, "ifttt_off_event"),
        ({"high_f": "nan"}, "high_f"),
        ({"low_f": "-inf"}, "low_f"),
        ({"cycle_interval_minutes": "nan"}, "cycle_interval_minutes"),
        ({"stale_after_minutes": "inf"}, "stale_after_minutes"),
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
        ({"mode": "on", "hours": "nan"}, (None, None, "Enter how long, in hours.")),
        ({"mode": "on", "hours": "inf"}, (None, None, "Enter how long, in hours.")),
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


def test_next_cycle_runs_in_the_repeated_hour_when_dst_ends():
    # 2026-11-01: 01:00-01:59 happens twice in New York. After the first 01:05 cycle, the next
    # one is the repeated 01:05 an hour later, not 02:05.
    first_105 = datetime(2026, 11, 1, 5, 5, tzinfo=timezone.utc)  # 01:05 EDT
    nxt = next_cycle_start(first_105 + timedelta(minutes=10), Settings())
    assert nxt == datetime(2026, 11, 1, 6, 5, tzinfo=timezone.utc)  # 01:05 EST
    assert nxt.astimezone(NY).strftime("%H:%M %Z") == "01:05 EST"


def test_next_cycle_is_the_same_for_a_local_time_caller():
    first_105 = datetime(2026, 11, 1, 5, 5, tzinfo=timezone.utc)
    local_now = (first_105 + timedelta(minutes=10)).astimezone(NY)
    nxt = next_cycle_start(local_now, Settings())
    # Compared in UTC: Python never equates a fold=1 local time with another zone (PEP 495).
    assert nxt.astimezone(timezone.utc) == datetime(2026, 11, 1, 6, 5, tzinfo=timezone.utc)
    assert nxt.tzinfo == local_now.tzinfo


def test_next_cycle_skips_the_missing_hour_when_dst_starts():
    # 2026-03-08: 02:00-02:59 doesn't exist in New York; after 01:05 EST the next is 03:05 EDT.
    nxt = next_cycle_start(datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc), Settings())
    assert nxt.astimezone(NY).strftime("%H:%M %Z") == "03:05 EDT"
    assert nxt - datetime(2026, 3, 8, 6, 5, tzinfo=timezone.utc) == timedelta(hours=1)


def test_no_next_cycle_when_cycles_are_off():
    assert next_cycle_start(NOON, Settings(cycle_duration_minutes=0)) is None


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


def test_settings_audit_records_the_value_actually_replaced(admin, store, clock):
    csrf = token(admin, "/admin/settings")
    # Someone else changes high_f after this form was loaded and before it's saved.
    store.update_config(
        lambda c: c.__class__(**{**c.__dict__, "controller": Settings(high_f=105)}), "other", "settings_changed", clock()
    )
    clock.advance(seconds=1)
    admin.post("/admin/settings", data=dict(SETTINGS_FORM, csrf_token=csrf), headers=ADMIN)
    event = store.recent_events()[0]
    assert event["actor"] == "jeff@x" and event["detail"]["changed"]["high_f"] == [105.0, 98.0]


def test_settings_flash_names_the_fields_actually_changed(admin, store, clock):
    csrf = token(admin, "/admin/settings")
    # Someone else saves the same form first; this save then changes nothing.
    admin.post("/admin/settings", data=dict(SETTINGS_FORM, csrf_token=csrf), headers=ADMIN)
    clock.advance(seconds=1)
    store.update_config(lambda c: c, "other", "noop", clock())
    with admin.session_transaction() as session:
        session.pop("_flashes", None)
    admin.post("/admin/settings", data=dict(SETTINGS_FORM | {"high_f": "97"}, csrf_token=csrf), headers=ADMIN)
    page = admin.get("/admin/settings", headers=ADMIN).get_data(as_text=True)
    assert "Saved: high_f." in page


def test_settings_require_csrf(admin, store):
    assert admin.post("/admin/settings", data=SETTINGS_FORM, headers=ADMIN).status_code == 400


# --- manual override -----------------------------------------------------------------------

def _after_tick(store, clock, doc):
    store.docs[STATUS] = {"updated_at": clock.advance(seconds=30)}
    store.docs[CONTROLLER] = actuation.to_dict(doc)


def test_override_on_shows_pending_then_accepted(admin, store, clock):
    post(admin, "/admin/override", {"mode": "on", "hours": "1"})
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Fans ON</strong> by hand" in page and "Pending" in page
    command = Command(True, 1, clock(), result="accepted", http_status=200)
    _after_tick(store, clock, ControllerDoc(generation=1, last_command=command, last_accepted_on=True,
                                            last_accepted_at=clock()))
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Command accepted by IFTTT" in page and "Pending" not in page
    assert store.recent_events()[0]["kind"] == "override_set"


def test_override_is_not_shown_applied_until_a_command_is_accepted(admin, store, clock):
    post(admin, "/admin/override", {"mode": "on", "hours": "1"})
    # A tick ran and planned the command, but it hasn't been accepted.
    _after_tick(store, clock, ControllerDoc(generation=1, last_command=Command(True, 1, clock()),
                                            last_accepted_on=False, last_accepted_at=clock()))
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Sending the command" in page and "accepted" not in page


def test_failed_override_command_is_shown(admin, store, clock):
    post(admin, "/admin/override", {"mode": "on", "hours": "1"})
    command = Command(True, 1, clock(), result="failed", http_status=500)
    _after_tick(store, clock, ControllerDoc(generation=1, last_command=command, last_accepted_on=False))
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "didn&#39;t go through (failed, HTTP 500)" in page or "didn't go through (failed, HTTP 500)" in page


def test_override_while_fan_control_is_disabled_says_not_sent(admin, store, clock):
    store.update_config(lambda c: c.__class__(**{**c.__dict__, "actuation_enabled": False}), "x", "actuation_disabled", clock())
    post(admin, "/admin/override", {"mode": "on", "hours": "1"})
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    # Straight away, before any tick: shadow mode will never send it.
    assert "Not sent: commands are stopped" in page and "Pending" not in page
    _after_tick(store, clock, ControllerDoc())
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Not sent: commands are stopped" in page


def test_dashboard_says_when_cycles_are_off(admin, store, clock):
    store.update_config(
        lambda c: c.__class__(**{**c.__dict__, "controller": Settings(cycle_duration_minutes=0)}),
        "x", "settings_changed", clock(),
    )
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Scheduled cycles are off" in page and "Next scheduled cycle" not in page


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


def test_history_shows_fans_on_from_a_command_before_the_range(admin, store, clock):
    store.add_event("command_accepted", "worker", clock() - timedelta(days=3), {"on": True})
    page = admin.get("/admin/history?range=24h", headers=ADMIN).get_data(as_text=True)
    assert page.count('class="fan-on"') == 1


def test_history_includes_the_newest_commands_in_a_busy_week(admin, store, clock):
    # More commands than the old 2,000-event query limit; the newest must still be charted.
    for i in range(2_400):
        at = clock() - timedelta(days=7) + timedelta(minutes=4 * i + 1)
        store.add_event("command_accepted", "worker", at, {"on": i % 2 == 0})
    page = admin.get("/admin/history?range=7d", headers=ADMIN).get_data(as_text=True)
    assert page.count('class="fan-on"') == 1_200


def test_history_warns_when_events_hit_the_read_limit(admin, store, clock, monkeypatch):
    monkeypatch.setattr("greenhouse.web.EVENTS_MAX", 3)
    monkeypatch.setattr("greenhouse.store.EVENTS_MAX", 3)
    for i in range(5):
        store.add_event("command_accepted", "worker", clock() - timedelta(hours=i + 1), {"on": True})
    page = admin.get("/admin/history?range=24h", headers=ADMIN).get_data(as_text=True)
    assert "newest fan commands may be missing" in page


def test_chart_breaks_the_line_across_missing_samples():
    end = NOON
    readings = readings_every(5, 40, end - timedelta(hours=12)) + readings_every(5, 40, end)
    svg = chart.render(readings, [], end - timedelta(hours=24), end, 100, 90, "America/New_York").svg
    assert svg.count('class="temp-line"') == 2


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
