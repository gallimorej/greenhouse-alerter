import html
import re

import pytest
from markupsafe import escape

from greenhouse import appconfig
from greenhouse.appconfig import AppConfig
from greenhouse.store import CONFIG, MemoryStore
from greenhouse.web import IAP_HEADER, Services, create_app
from greenhouse.sensors import SensorInfo
from tests.fakes import Clock, FakeSensors, utc
from tests.test_admin_ui import SETTINGS_FORM

NOON = utc(2026, 7, 15, 16, 20)
ADMIN = {IAP_HEADER: "good-iap"}
SENSOR_ID = "s1"


def verify(token):
    return "jeff@x"


@pytest.fixture
def clock():
    return Clock(NOON)


@pytest.fixture
def store():
    return MemoryStore({CONFIG: appconfig.to_dict(AppConfig())})


@pytest.fixture
def sensors(clock):
    return FakeSensors(clock, temps={"s1": 85.0, "s2": 70.0})


@pytest.fixture
def admin(store, sensors, clock):
    app = create_app("admin", Services(store=store, sensors=sensors, verifier=verify, secret_key="k", now=clock))
    return app.test_client()


def page(client, path="/admin/"):
    return client.get(path, headers=ADMIN).get_data(as_text=True)


def post(client, path, data):
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page(client)).group(1)
    return client.post(path, data=dict(data, csrf_token=csrf), headers=ADMIN)


def link_form(sensors_page, sensor_id):
    """The hidden fields of the sensor's Link… form, decoded as the browser would submit them."""
    form = re.search(
        rf'<form method="post" action="/admin/sensors/test"[^>]*>((?:(?!</form>).)*value="{sensor_id}"(?:(?!</form>).)*)</form>',
        sensors_page,
        re.S,
    ).group(1)
    return {
        name: html.unescape(value)
        for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', form)
        if name != "csrf_token"
    }


def link_from_sensors_page(admin, clock, sensor_id=SENSOR_ID):
    fields = link_form(page(admin, "/admin/sensors"), sensor_id)
    clock.advance(minutes=1)  # the listing used this minute's SensorPush call
    confirm = post(admin, "/admin/sensors/test", fields).get_data(as_text=True)
    post(admin, "/admin/sensors/link", {"sensor_id": sensor_id})
    return fields, confirm


def test_linking_from_the_sensors_page_stores_and_shows_the_name(admin, store, clock):
    fields, confirm = link_from_sensors_page(admin, clock)
    assert fields == {"sensor_id": "s1", "sensor_name": "Greenhouse HTP.xw"}
    assert "Greenhouse HTP.xw" in confirm
    config = store.get_config()
    assert config.active_sensor_id == "s1" and config.active_sensor_name == "Greenhouse HTP.xw"
    event = store.recent_events()[0]
    assert event["kind"] == "sensor_linked" and event["detail"]["sensor_name"] == "Greenhouse HTP.xw"
    dashboard = page(admin)
    assert "<p>Greenhouse HTP.xw</p>" in dashboard
    assert '<p class="muted mono">s1</p>' in dashboard


def test_a_name_with_html_characters_survives_the_form(admin, store, sensors, clock):
    name = 'Tom & Jerry\'s "big" house'
    sensors.listed = [SensorInfo("s1", name, "HTP.xw", True, 3.1, -60.0)]
    fields, _ = link_from_sensors_page(admin, clock)
    assert fields["sensor_name"] == name
    assert store.get_config().active_sensor_name == name
    assert f"<p>{escape(name)}</p>" in page(admin)


def test_a_link_without_a_name_shows_the_id_only(admin, store):
    post(admin, "/admin/sensors/test", {"sensor_id": "s1"})
    post(admin, "/admin/sensors/link", {"sensor_id": "s1"})
    config = store.get_config()
    assert config.active_sensor_id == "s1" and config.active_sensor_name is None
    dashboard = page(admin)
    assert '<p class="muted mono">s1</p>' in dashboard and "Sensor not linked" not in dashboard


def test_unlinking_clears_the_name(admin, store, clock):
    link_from_sensors_page(admin, clock)
    post(admin, "/admin/sensors/unlink", {})
    config = store.get_config()
    assert config.active_sensor_id is None and config.active_sensor_name is None
    assert "Sensor not linked" in page(admin)


def test_a_long_name_is_truncated(admin, store):
    post(admin, "/admin/sensors/test", {"sensor_id": "s1", "sensor_name": "x" * 150})
    post(admin, "/admin/sensors/link", {"sensor_id": "s1"})
    assert store.get_config().active_sensor_name == "x" * 100


def test_the_name_is_escaped(admin, store):
    post(admin, "/admin/sensors/test", {"sensor_id": "s1", "sensor_name": "<script>x</script>"})
    post(admin, "/admin/sensors/link", {"sensor_id": "s1"})
    dashboard = page(admin)
    assert "<script>x</script>" not in dashboard and "&lt;script&gt;x&lt;/script&gt;" in dashboard


def test_saving_settings_keeps_the_name(admin, store, clock):
    link_from_sensors_page(admin, clock)
    post(admin, "/admin/settings", SETTINGS_FORM)
    assert store.get_config().active_sensor_name == "Greenhouse HTP.xw"


def test_config_without_a_name_reads_none_and_round_trips():
    assert appconfig.from_dict({"active_sensor_id": "s1"}).active_sensor_name is None
    config = AppConfig(active_sensor_id="s1", active_sensor_name="Greenhouse HTP.xw")
    assert appconfig.from_dict(appconfig.to_dict(config)) == config
