import re
from datetime import timedelta

import pytest

from greenhouse import appconfig
from greenhouse.auth import AuthError
from greenhouse.store import CONFIG, MemoryStore
from greenhouse.web import IAP_HEADER, Services, ago, create_app
from tests.fakes import Clock, FakeFans, FakeSensors, utc

NOON = utc(2026, 7, 15, 16, 20)
ADMIN = {IAP_HEADER: "good-iap"}


def verifier(good: str, email: str):
    def verify(token):
        if token != good:
            raise AuthError("bad token")
        return email

    return verify


@pytest.fixture
def clock():
    return Clock(NOON)


@pytest.fixture
def store():
    return MemoryStore({CONFIG: appconfig.to_dict(appconfig.AppConfig())})


@pytest.fixture
def sensors(clock):
    return FakeSensors(clock, temps={"s1": 85.0, "s2": 70.0})


@pytest.fixture
def worker(store, sensors, clock):
    services = Services(store=store, sensors=sensors, fans=FakeFans(),
                        verifier=verifier("good-oidc", "scheduler@x"), now=clock)
    app = create_app("worker", services)
    return app.test_client()


@pytest.fixture
def admin_app(store, sensors, clock):
    services = Services(store=store, sensors=sensors, verifier=verifier("good-iap", "jeff@x"),
                        secret_key="test-secret", now=clock)
    return create_app("admin", services)


@pytest.fixture
def admin(admin_app):
    return admin_app.test_client()


def csrf_token(client, path="/admin/"):
    page = client.get(path, headers=ADMIN).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def post(client, path, data=None):
    data = dict(data or {}, csrf_token=csrf_token(client))
    return client.post(path, data=data, headers=ADMIN)


# --- worker ---------------------------------------------------------------------------------

def test_tick_requires_a_token(worker):
    assert worker.post("/tasks/tick").status_code == 401


def test_tick_rejects_a_bad_token(worker):
    assert worker.post("/tasks/tick", headers={"Authorization": "Bearer forged"}).status_code == 403


def test_scheduler_tick_succeeds_without_csrf(worker):
    resp = worker.post("/tasks/tick", headers={"Authorization": "Bearer good-oidc"})
    assert resp.status_code == 200 and resp.json["outcome"] == "planned"


def test_worker_has_no_admin_routes(worker):
    assert worker.get("/admin/", headers=ADMIN).status_code == 404


def test_tick_is_post_only(worker):
    assert worker.get("/tasks/tick", headers={"Authorization": "Bearer good-oidc"}).status_code == 405


# --- admin auth and hardening ---------------------------------------------------------------

def test_admin_requires_iap_assertion(admin):
    assert admin.get("/admin/").status_code == 401


def test_admin_rejects_unverified_assertion(admin):
    assert admin.get("/admin/", headers={IAP_HEADER: "forged"}).status_code == 403


def test_admin_has_no_tick_route(admin):
    assert admin.post("/tasks/tick", headers={"Authorization": "Bearer good-oidc"}).status_code == 404


def test_admin_post_without_csrf_is_rejected(admin, store):
    resp = admin.post("/admin/actuation", data={"enabled": "on"}, headers=ADMIN)
    assert resp.status_code == 400
    assert store.get_config().actuation_enabled is False


def test_security_headers(admin):
    resp = admin.get("/admin/", headers=ADMIN)
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Strict-Transport-Security"].startswith("max-age=")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_dashboard_shows_shadow_mode_and_no_sensor_banners(admin):
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Shadow mode" in page and "No sensor linked" in page and "jeff@x" in page


# --- controls -------------------------------------------------------------------------------

def test_enable_and_disable_actuation_is_audited(admin, store, clock):
    assert post(admin, "/admin/actuation", {"enabled": "on"}).status_code == 302
    assert store.get_config().actuation_enabled is True
    clock.advance(seconds=1)
    post(admin, "/admin/actuation", {"enabled": "off"})
    assert store.get_config().actuation_enabled is False
    events = store.recent_events()
    assert [e["kind"] for e in events][:2] == ["actuation_disabled", "actuation_enabled"]
    assert all(e["actor"] == "jeff@x" for e in events)


def test_reconcile_refused_while_tick_holds_lease(admin, store, clock):
    store.tick_acquire("running", clock())
    resp = post(admin, "/admin/reconcile")
    assert resp.status_code == 302
    follow = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "still running" in follow


def test_reconcile_sets_uncertain(admin, store):
    post(admin, "/admin/reconcile")
    page = admin.get("/admin/", headers=ADMIN).get_data(as_text=True)
    assert "Fan state uncertain" in page


# --- sensor linking -------------------------------------------------------------------------

def test_sensors_page_lists_account_sensors_and_gateways(admin):
    page = admin.get("/admin/sensors", headers=ADMIN).get_data(as_text=True)
    assert "Greenhouse HTP.xw" in page and "HTP.xw" in page and "Greenhouse Gateway" in page


def test_link_flow_shows_live_reading_then_links(admin, store):
    page = post(admin, "/admin/sensors/test", {"sensor_id": "s1"}).get_data(as_text=True)
    assert "85.0°F" in page and "Confirm" in page
    resp = post(admin, "/admin/sensors/link", {"sensor_id": "s1"})
    assert resp.status_code == 302
    assert store.get_config().active_sensor_id == "s1"
    event = store.recent_events()[0]
    assert event["kind"] == "sensor_linked" and event["detail"]["sensor_id"] == "s1"


def test_stale_sensor_cannot_be_linked(admin, store, sensors):
    sensors.age = timedelta(days=99)
    page = post(admin, "/admin/sensors/test", {"sensor_id": "s1"}).get_data(as_text=True)
    assert "Can&#39;t link this sensor" in page or "Can't link this sensor" in page
    post(admin, "/admin/sensors/link", {"sensor_id": "s1"})
    assert store.get_config().active_sensor_id is None


def test_sensor_without_samples_cannot_be_linked(admin, store):
    post(admin, "/admin/sensors/link", {"sensor_id": "nope"})
    assert store.get_config().active_sensor_id is None


def test_unlink(admin, store, clock):
    post(admin, "/admin/sensors/link", {"sensor_id": "s1"})
    clock.advance(seconds=1)
    post(admin, "/admin/sensors/unlink")
    assert store.get_config().active_sensor_id is None
    assert store.recent_events()[0]["detail"]["previous"] == "s1"


def test_admin_sensorpush_calls_are_capped(admin, sensors):
    for _ in range(2):  # 2 calls per sensors page; the cap is 4 a minute
        admin.get("/admin/sensors", headers=ADMIN)
    calls = sensors.calls
    page = admin.get("/admin/sensors", headers=ADMIN).get_data(as_text=True)
    assert sensors.calls == calls
    assert "rate limit" in page


# --- helpers --------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "delta, text",
    [(timedelta(seconds=5), "5 s ago"), (timedelta(minutes=3), "3 min ago"),
     (timedelta(hours=2), "2 h ago"), (timedelta(days=99), "99 d ago"), (-timedelta(minutes=1), "in the future")],
)
def test_ago(delta, text):
    assert ago(NOON - delta, NOON) == text
    assert ago(None, NOON) == "never"


def test_unknown_role_is_rejected():
    with pytest.raises(Exception):
        create_app("root", Services(store=None, sensors=None, verifier=None))
