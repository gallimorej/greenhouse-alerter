"""The season switch and the separate hourly-cycle and temperature-control switches, outside the store.

Store-backed cases (paused ticks, resume, snooze reconciliation) are in test_worker.py, which CI also
runs against the Firestore emulator.
"""

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from werkzeug.datastructures import MultiDict

from greenhouse import appconfig, forms
from greenhouse.alerts import MonitoringSnoozes, Snooze, _parse_timestamp, sync_snoozes
from greenhouse.appconfig import AppConfig
from greenhouse.auth import AuthError
from greenhouse.controller import ControllerState, Override, Reason, Settings, decide, next_cycle_start
from greenhouse.models import Reading
from greenhouse.store import CONFIG, MemoryStore
from greenhouse.web import IAP_HEADER, Services, create_app
from tests.fakes import Clock, FakeSensors, FakeSnoozes, utc
from tests.test_admin_ui import SETTINGS_FORM

NY = ZoneInfo("America/New_York")
ADMIN = {IAP_HEADER: "good-iap"}
NOON = utc(2026, 7, 15, 16, 20)  # 12:20 in New York
POLICIES = ("projects/p/alertPolicies/1",)
FRESH = ControllerState(temp_demand=False, baselined=False)
BASELINED = ControllerState(temp_demand=False, baselined=True)


def at(hour, minute):
    return datetime(2026, 7, 15, hour, minute, tzinfo=NY).astimezone(timezone.utc)


def reading(now, temp, age=timedelta(minutes=1)):
    return Reading(sensor_id="s1", observed_at=now - age, temperature_f=temp)


TEMP_OFF = Settings(temperature_control_enabled=False)
CYCLES_OFF = Settings(cycles_enabled=False)
BOTH_OFF = Settings(cycles_enabled=False, temperature_control_enabled=False)


# --- controller ------------------------------------------------------------------------------

def test_temperature_control_off_ignores_heat_but_cycles_run():
    now = at(12, 20)
    d = decide(now, TEMP_OFF, reading(now, 105), BASELINED)
    assert d.desired_on is False and d.reason is Reason.TEMP_OFF and d.state.temp_demand is False
    now = at(12, 5)
    d = decide(now, TEMP_OFF, reading(now, 105), BASELINED)
    assert d.desired_on is True and d.reason is Reason.CYCLE


def test_temperature_control_off_needs_no_baseline():
    now = at(12, 5)
    d = decide(now, TEMP_OFF, None, FRESH)
    assert d.desired_on is True and d.reason is Reason.CYCLE
    assert d.state.baselined is False


def test_temperature_control_on_still_waits_for_a_baseline():
    now = at(12, 5)
    d = decide(now, Settings(), None, FRESH)
    assert d.desired_on is None and d.reason is Reason.NOT_BASELINED


def test_fresh_reading_baselines_even_with_temperature_control_off():
    now = at(12, 20)
    assert decide(now, TEMP_OFF, reading(now, 80), FRESH).state.baselined is True


def test_cycles_off_skips_the_cycle_but_temperature_still_works():
    now = at(12, 5)
    d = decide(now, CYCLES_OFF, reading(now, 80), BASELINED)
    assert d.desired_on is False and d.in_cycle is False
    d = decide(now, CYCLES_OFF, reading(now, 101), BASELINED)
    assert d.desired_on is True and d.reason is Reason.TOO_HOT


def test_both_off_holds_the_fans_off():
    now = at(12, 5)
    assert decide(now, BOTH_OFF, reading(now, 105), BASELINED).desired_on is False


def test_turning_temperature_control_back_on_with_a_stale_reading_freezes_at_off():
    now = at(12, 20)
    hot_before = ControllerState(temp_demand=True, baselined=True)
    off = decide(now, TEMP_OFF, reading(now, 105), hot_before)
    assert off.state.temp_demand is False
    back_on = decide(now, Settings(), reading(now, 105, age=timedelta(hours=2)), off.state)
    assert back_on.desired_on is False and back_on.reason is Reason.STALE


@pytest.mark.parametrize("settings", [TEMP_OFF, CYCLES_OFF, BOTH_OFF])
def test_override_wins_with_either_switch_off(settings):
    now = at(12, 20)
    d = decide(now, settings, None, FRESH, Override(on=True, expires_at=now + timedelta(hours=1)))
    assert d.desired_on is True and d.reason is Reason.OVERRIDE


def test_next_cycle_is_none_with_cycles_off():
    assert next_cycle_start(at(12, 20), CYCLES_OFF) is None
    assert next_cycle_start(at(12, 20), Settings()) is not None


# --- config ----------------------------------------------------------------------------------

def test_old_config_reads_with_everything_on():
    old = {"actuation_enabled": True, "controller": {"high_f": 100.0, "low_f": 90.0}}
    config = appconfig.from_dict(old)
    assert config.control_enabled is True and config.alerts_in_sync is True
    assert config.controller.cycles_enabled and config.controller.temperature_control_enabled


def test_new_fields_round_trip():
    config = replace(
        AppConfig(controller=BOTH_OFF),
        control_enabled=False,
        alerts_revision=7,
        alerts_in_sync=False,
        alerts_swept_at=NOON,
    )
    assert appconfig.from_dict(appconfig.to_dict(config)) == config


def test_settings_form_keeps_the_switches():
    current = AppConfig(controller=BOTH_OFF)
    new, errors = forms.parse_settings(MultiDict(SETTINGS_FORM), current)
    assert errors == {}
    assert not new.controller.cycles_enabled and not new.controller.temperature_control_enabled
    assert "temperature_control_enabled" not in forms.settings_diff(current, new)


# --- snooze client ---------------------------------------------------------------------------

def test_sync_ends_only_our_running_snoozes():
    now = NOON
    api = FakeSnoozes(snoozes=[
        Snooze("projects/p/snoozes/a", "greenhouse-season-pause", now - timedelta(days=3), now + timedelta(days=99)),
        Snooze("projects/p/snoozes/b", "someone else's", now - timedelta(days=3), now + timedelta(days=99)),
        Snooze("projects/p/snoozes/c", "greenhouse-season-pause", now - timedelta(days=9), now - timedelta(days=8)),
    ])
    assert sync_snoozes(api, POLICIES, False, now, 1.0) == "ended 1"
    ends = {s.name: s.end for s in api.snoozes}
    assert ends["projects/p/snoozes/a"] == now
    assert ends["projects/p/snoozes/b"] == now + timedelta(days=99)


def test_a_snooze_is_ended_now_even_just_after_it_started():
    start = NOON
    api = FakeSnoozes(snoozes=[Snooze("projects/p/snoozes/a", "greenhouse-season-pause", start, start + timedelta(days=400))])
    now = start + timedelta(seconds=10)
    assert sync_snoozes(api, POLICIES, False, now, 1.0) == "ended 1"
    assert api.snoozes[0].end == now
    assert sync_snoozes(api, POLICIES, False, now + timedelta(seconds=1), 1.0) == "nothing snoozed"


def test_parse_timestamp_handles_nanoseconds():
    assert _parse_timestamp("2026-10-18T19:15:03.123456789Z") == datetime(
        2026, 10, 18, 19, 15, 3, 123456, tzinfo=timezone.utc
    )
    assert _parse_timestamp("2026-10-18T19:15:03Z") == datetime(2026, 10, 18, 19, 15, 3, tzinfo=timezone.utc)


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, timeout, **kwargs):
        self.calls.append((method, url, kwargs))
        status, body = self.responses.pop(0)

        class Response:
            status_code = status
            content = b"x" if body else b""

            def json(self):
                return body

        return Response()


def test_monitoring_client_pages_creates_and_ends():
    page1 = {"snoozes": [{"name": "projects/p/snoozes/1", "displayName": "greenhouse-season-pause",
                          "interval": {"startTime": "2026-10-18T00:00:00Z", "endTime": "2027-11-22T00:00:00Z"}}],
             "nextPageToken": "t"}
    session = RecordingSession([(200, page1), (200, {"snoozes": []}), (200, {}), (200, {})])
    api = MonitoringSnoozes("p", session=session)
    [snooze] = api.list(1.0)
    assert session.calls[1][2]["params"] == {"pageToken": "t"}
    api.create(POLICIES, NOON, NOON + timedelta(days=1), 1.0)
    method, url, kwargs = session.calls[2]
    assert method == "POST" and url.endswith("/projects/p/snoozes")
    assert kwargs["json"]["criteria"] == {"policies": list(POLICIES)}
    assert kwargs["json"]["interval"]["startTime"] == "2026-07-15T16:20:00Z"
    api.end(snooze, NOON, 1.0)
    method, url, kwargs = session.calls[3]
    assert method == "PATCH" and url.endswith("/projects/p/snoozes/1")
    assert kwargs["params"] == {"updateMask": "interval.endTime"}


def test_monitoring_client_raises_on_http_errors():
    from greenhouse.alerts import AlertsError

    api = MonitoringSnoozes("p", session=RecordingSession([(403, {"error": {}})]))
    with pytest.raises(AlertsError, match="403"):
        api.list(1.0)


# --- admin UI --------------------------------------------------------------------------------

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
def snoozes():
    return FakeSnoozes()


@pytest.fixture
def admin(store, clock, snoozes):
    from greenhouse.alerts import Snoozer

    services = Services(store=store, sensors=FakeSensors(clock), verifier=verify, secret_key="k",
                        now=clock, alerts=Snoozer(snoozes, POLICIES))
    return create_app("admin", services).test_client()


def post(client, path, data):
    page = client.get("/admin/", headers=ADMIN).get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return client.post(path, data=dict(data, csrf_token=token), headers=ADMIN, follow_redirects=True)


def page(client):
    return client.get("/admin/", headers=ADMIN).get_data(as_text=True)


def test_switch_needs_csrf(admin, store):
    response = admin.post("/admin/switch/temperature", data={"enabled": "off"}, headers=ADMIN)
    assert response.status_code == 400
    assert store.get_config().controller.temperature_control_enabled


def test_switches_change_config_and_are_audited(admin, store):
    post(admin, "/admin/switch/temperature", {"enabled": "off"})
    post(admin, "/admin/switch/cycles", {"enabled": "off"})
    controller = store.get_config().controller
    assert not controller.temperature_control_enabled and not controller.cycles_enabled
    kinds = {e["kind"] for e in store.recent_events()}  # same clock tick, so no order
    assert {"cycles_disabled", "temperature_disabled"} <= kinds
    html = page(admin)
    assert "fans are held off" in html and "Scheduled cycles are off" in html


def test_unknown_switch_is_404(admin):
    assert post(admin, "/admin/switch/heater", {"enabled": "on"}).status_code == 404


def test_temperature_off_hides_the_sensor_banner(admin, store):
    store.update_config(lambda c: replace(c, active_sensor_id=None), "x", "sensor_unlinked", NOON)
    assert "No sensor linked" in page(admin)
    post(admin, "/admin/switch/temperature", {"enabled": "off"})
    assert "No sensor linked" not in page(admin)


def test_saving_settings_keeps_temperature_control_off(admin, store):
    post(admin, "/admin/switch/temperature", {"enabled": "off"})
    page_html = admin.get("/admin/settings", headers=ADMIN).get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page_html).group(1)
    admin.post("/admin/settings", data=dict(SETTINGS_FORM, csrf_token=token), headers=ADMIN)
    config = store.get_config()
    assert config.controller.high_f == 98  # the form saved
    assert config.controller.temperature_control_enabled is False


def test_settings_save_keeps_a_switch_flipped_while_the_form_was_open(admin, store):
    page_html = admin.get("/admin/settings", headers=ADMIN).get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page_html).group(1)
    original = store.get_config

    def flip_after_read():
        config = original()
        store.update_config(
            lambda c: replace(c, controller=replace(c.controller, cycles_enabled=False)), "x", "cycles_disabled", NOON
        )
        return config

    store.get_config = flip_after_read
    admin.post("/admin/settings", data=dict(SETTINGS_FORM, csrf_token=token), headers=ADMIN)
    store.get_config = original
    assert store.get_config().controller.cycles_enabled is False


def test_pause_snoozes_and_resume_ends(admin, store, snoozes, clock):
    html = post(admin, "/admin/season", {"active": "off"}).get_data(as_text=True)
    assert "Alerts are silenced" in html and "paused for the season" in html
    config = store.get_config()
    assert config.control_enabled is False and config.alerts_in_sync
    assert len(snoozes.running(clock())) == 1
    clock.advance(hours=2)
    html = post(admin, "/admin/season", {"active": "on"}).get_data(as_text=True)
    assert "Control resumed" in html
    assert snoozes.running(clock()) == []
    assert [e["kind"] for e in store.recent_events()][:2] == ["control_resumed", "control_paused"]


def test_snooze_failure_keeps_the_pause_and_shows_retry(admin, store, snoozes):
    snoozes.fail = True
    html = post(admin, "/admin/season", {"active": "off"}).get_data(as_text=True)
    assert store.get_config().control_enabled is False
    assert "Alerts aren't silenced yet" in html and "Fix alerts now" in html
    snoozes.fail = False
    html = post(admin, "/admin/alerts/reconcile", {}).get_data(as_text=True)
    assert "Alerts match the season switch" in html and "Fix alerts now" not in html


def test_override_is_refused_and_disabled_while_paused(admin, store):
    post(admin, "/admin/season", {"active": "off"})
    html = post(admin, "/admin/override", {"mode": "on", "hours": "1"}).get_data(as_text=True)
    assert "paused for the season" in html
    assert store.read_raw("config/override") is None
    assert re.search(r'value="on" disabled', page(admin))


def test_override_set_before_pause_shows_not_sent(admin, store):
    post(admin, "/admin/override", {"mode": "on", "hours": "1"})
    post(admin, "/admin/season", {"active": "off"})
    assert "Not sent: control is paused for the season" in page(admin)


def test_commands_button_is_labelled_emergency_stop(admin):
    assert "Emergency stop (shadow mode)" in page(admin)
