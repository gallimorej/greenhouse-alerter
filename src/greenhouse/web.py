"""Flask app factory for both Cloud Run services; ``ROLE`` picks which routes exist.

- worker: ``POST /tasks/tick``, called by Cloud Scheduler with an OIDC token
- admin: ``/admin/...``, behind IAP; every change is a CSRF-protected POST and is audited in ``events``

Production: ``gunicorn "greenhouse.web:create_app()"``. Tests pass ``services`` with fakes.
"""

import logging
import secrets as pysecrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Flask, abort, current_app, flash, g, redirect, render_template, request, url_for
from flask_wtf.csrf import CSRFProtect

from greenhouse import config as envconfig
from greenhouse import controller
from greenhouse.actuation import LeaseHeld
from greenhouse.auth import AuthError, IapVerifier, SchedulerVerifier, Verifier, parse_allowlist
from greenhouse.fans import IftttClient
from greenhouse.sensors import ADMIN_POLICY, SensorPushClient, SensorSource, SensorUnavailable
from greenhouse.models import Reading
from greenhouse.store import LINK_CHECK, Store
from greenhouse.worker import run_tick

log = logging.getLogger(__name__)

IAP_HEADER = "X-Goog-IAP-JWT-Assertion"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
    ),
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


@dataclass
class Services:
    store: Store
    sensors: SensorSource
    verifier: Verifier
    fans: IftttClient | None = None
    secret_key: str | None = None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


csrf = CSRFProtect()


def services() -> Services:
    return current_app.extensions["greenhouse"]


# --- worker --------------------------------------------------------------------------------

worker_bp = Blueprint("worker", __name__)
# The worker has no browser session to protect; it's authenticated by the scheduler's OIDC token.
csrf.exempt(worker_bp)


@worker_bp.before_request
def require_scheduler():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        abort(401)
    try:
        g.caller = services().verifier(header.removeprefix("Bearer "))
    except AuthError as exc:
        log.warning("worker auth rejected", extra={"json": {"event": "auth_rejected", "why": str(exc)}})
        abort(403)


@worker_bp.post("/tasks/tick")
def tick():
    s = services()
    result = run_tick(s.store, s.sensors, s.fans, now=s.now)
    return {"outcome": result.outcome}, 200


# --- admin ---------------------------------------------------------------------------------

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.before_request
def require_iap_user():
    token = request.headers.get(IAP_HEADER)
    if not token:
        abort(401)
    try:
        g.user = services().verifier(token)
    except AuthError as exc:
        log.warning("admin auth rejected", extra={"json": {"event": "auth_rejected", "why": str(exc)}})
        abort(403)


# How long a passed link test stays valid for confirming it.
LINK_CHECK_VALID = timedelta(minutes=5)
# Account sensor and gateway listings are cached, so with one SensorPush call a minute a reload can
# fetch what the last load couldn't.
LISTING_TTL = timedelta(minutes=3)


def _admin_sensor_call(quiet: bool = False) -> bool:
    s = services()
    if s.store.consume_sensorpush_call("admin", s.now()):
        return True
    if not quiet:
        flash(
            "SensorPush allows one request a minute, shared with the fan controller. "
            "Try again in a minute.",
            "warning",
        )
    return False


@admin_bp.get("/")
def dashboard():
    s = services()
    data = s.store.get_dashboard()
    return render_template(
        "dashboard.html",
        now=s.now(),
        config=data["config"],
        controller=data["controller"],
        status=data["status"] or {},
        override=data["override"],
        events=s.store.recent_events(15),
    )


@admin_bp.post("/actuation")
def set_actuation():
    s = services()
    enabled = request.form.get("enabled") == "on"
    s.store.update_config(
        lambda c: replace(c, actuation_enabled=enabled),
        g.user,
        "actuation_enabled" if enabled else "actuation_disabled",
        s.now(),
    )
    flash(f"Fan control {'enabled' if enabled else 'disabled (shadow mode)'}.", "info")
    return redirect(url_for("admin.dashboard"))


@admin_bp.post("/reconcile")
def reconcile():
    s = services()
    try:
        outcome = s.store.reconcile(g.user, s.now())
        flash(f"Reconciled: {outcome}. The next command will be resent.", "info")
    except LeaseHeld:
        flash("A tick is still running. Wait a minute and try again.", "warning")
    return redirect(url_for("admin.dashboard"))


@admin_bp.get("/sensors")
def sensors():
    s = services()
    config = s.store.get_config()
    # Either listing stays None when there was no SensorPush call left for it this minute.
    sensor_list, gateways, error = None, None, None
    try:
        sensor_list = _listing("sensors", s.sensors.sensors, quiet=False)
        if sensor_list is not None:
            sensor_list = sorted(sensor_list, key=lambda x: x.name.lower())
        gateways = _listing("gateways", s.sensors.gateways, quiet=sensor_list is None)
    except SensorUnavailable as exc:
        error = str(exc)
    return render_template(
        "sensors.html",
        now=s.now(),
        config=config,
        sensors=sensor_list,
        gateways=gateways,
        error=error,
    )


def _listing(name: str, fetch, quiet: bool):
    cache = current_app.extensions["greenhouse_listings"]
    now = services().now()
    hit = cache.get(name)
    if hit and hit[0] > now:
        return hit[1]
    if not _admin_sensor_call(quiet=quiet):
        return None
    value = fetch(ADMIN_POLICY)
    cache[name] = (now + LISTING_TTL, value)
    return value


def _fresh_reading(sensor_id: str):
    """(reading, problem): problem is None only when the reading is fresh enough to link."""
    s = services()
    if not _admin_sensor_call():
        return None, "rate limited"
    try:
        reading = s.sensors.latest_reading(sensor_id, ADMIN_POLICY)
    except SensorUnavailable as exc:
        return None, str(exc)
    stale_after = s.store.get_config().controller.stale_after
    if not controller.is_fresh(reading, s.now(), stale_after):
        return reading, f"the latest sample is older than {stale_after} (or in the future)"
    return reading, None


@admin_bp.post("/sensors/test")
def test_sensor():
    sensor_id = request.form.get("sensor_id", "").strip()
    if not sensor_id:
        abort(400)
    s = services()
    reading, problem = _fresh_reading(sensor_id)
    if problem is None:
        s.store.record_link_check(sensor_id, _reading_doc(reading), g.user, s.now())
    return render_template(
        "link_confirm.html", now=s.now(), sensor_id=sensor_id, reading=reading, problem=problem
    )


def _reading_doc(reading: Reading) -> dict:
    return {
        "observed_at": reading.observed_at,
        "temperature_f": reading.temperature_f,
        "humidity": reading.humidity,
        "pressure": reading.pressure,
    }


def _passed_link_check(sensor_id: str) -> tuple[dict | None, str | None]:
    """(reading, problem) from this admin's own recent link test, checked again against now."""
    s = services()
    check = s.store.read_raw(LINK_CHECK)
    now = s.now()
    if not check or check["sensor_id"] != sensor_id or check["actor"] != g.user:
        return None, "test the sensor first"
    if now - check["checked_at"] > LINK_CHECK_VALID:
        return None, "the test reading has expired; test the sensor again"
    reading = check["reading"]
    stale_after = s.store.get_config().controller.stale_after
    observed = Reading(sensor_id, reading["observed_at"], reading["temperature_f"])
    if not controller.is_fresh(observed, now, stale_after):
        return None, "the test reading is no longer fresh; test the sensor again"
    return reading, None


@admin_bp.post("/sensors/link")
def link_sensor():
    s = services()
    sensor_id = request.form.get("sensor_id", "").strip()
    if not sensor_id:
        abort(400)
    # Checked against the server-side record of the test, not trusted from the confirm page; that saves
    # a second SensorPush call.
    reading, problem = _passed_link_check(sensor_id)
    if problem:
        flash(f"Not linked: {problem}.", "error")
        return redirect(url_for("admin.sensors"))
    previous = s.store.get_config().active_sensor_id
    s.store.update_config(
        lambda c: replace(c, active_sensor_id=sensor_id),
        g.user,
        "sensor_linked",
        s.now(),
        {"sensor_id": sensor_id, "previous": previous, "temperature_f": reading["temperature_f"]},
    )
    flash("Sensor linked. The next tick will use it.", "info")
    return redirect(url_for("admin.dashboard"))


@admin_bp.post("/sensors/unlink")
def unlink_sensor():
    s = services()
    previous = s.store.get_config().active_sensor_id
    s.store.update_config(
        lambda c: replace(c, active_sensor_id=None),
        g.user,
        "sensor_unlinked",
        s.now(),
        {"previous": previous},
    )
    flash("Sensor unlinked. Temperature control is frozen; scheduled cycles continue.", "warning")
    return redirect(url_for("admin.dashboard"))


# --- factory -------------------------------------------------------------------------------

def ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "never"
    seconds = int((now - then).total_seconds())
    if seconds < 0:
        return "in the future"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return f"{seconds} s ago"


def build_services(role: str) -> Services:
    from google.cloud import firestore

    from greenhouse.store import FirestoreStore

    envconfig.require_env(role)
    secrets = envconfig.load_secrets(envconfig.ROLE_SECRETS[role])
    client = firestore.Client(
        project=envconfig.project_id(), database=envconfig.env("FIRESTORE_DATABASE")
    )
    sensors = SensorPushClient(secrets["SENSORPUSH_USER"], secrets["SENSORPUSH_PASSWORD"])
    if role == "worker":
        return Services(
            store=FirestoreStore(client),
            sensors=sensors,
            fans=IftttClient(secrets["IFTTT_KEY"]),
            verifier=SchedulerVerifier(
                audience=envconfig.env("WORKER_AUDIENCE"),
                service_account=envconfig.env("SCHEDULER_SERVICE_ACCOUNT"),
            ),
        )
    return Services(
        store=FirestoreStore(client),
        sensors=sensors,
        secret_key=secrets["FLASK_SECRET_KEY"],
        verifier=IapVerifier(
            audience=envconfig.env("IAP_AUDIENCE"),
            allowed_emails=parse_allowlist(envconfig.env("ADMIN_EMAILS")),
        ),
    )


def create_app(role: str | None = None, services: Services | None = None) -> Flask:
    from greenhouse import logging_setup

    role = role or envconfig.env("ROLE")
    if role not in envconfig.ROLE_SECRETS:
        raise envconfig.ConfigError(f"unknown ROLE {role!r}")
    if services is None:
        logging_setup.configure()
        services = build_services(role)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=services.secret_key or pysecrets.token_urlsafe(32),
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        WTF_CSRF_TIME_LIMIT=None,
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    app.extensions["greenhouse"] = services
    app.extensions["greenhouse_listings"] = {}
    csrf.init_app(app)

    if role == "worker":
        app.register_blueprint(worker_bp)
    else:
        app.register_blueprint(admin_bp)

        @app.get("/")
        def root():
            return redirect(url_for("admin.dashboard"))

    app.add_template_filter(ago)

    @app.after_request
    def security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    return app
