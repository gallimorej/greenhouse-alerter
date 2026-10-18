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
from zoneinfo import ZoneInfo

from flask import Blueprint, Flask, abort, current_app, flash, g, redirect, render_template, request, url_for
from flask_wtf.csrf import CSRFProtect

from greenhouse import config as envconfig
from greenhouse import controller, forms
from greenhouse.chart import render as render_chart
from greenhouse.actuation import ACCEPTED, RESEND_RESULTS, ControllerDoc, LeaseHeld
from greenhouse.auth import AuthError, IapVerifier, SchedulerVerifier, Verifier, parse_allowlist
from greenhouse.fans import IftttClient
from greenhouse.sensors import CallPolicy, SensorPushClient, SensorSource, SensorUnavailable
from greenhouse.models import Reading
from greenhouse.store import EVENTS_MAX, LINK_CHECK, Store
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


# One granted SensorPush call is one HTTP request, so admin pages don't retry, even after an expired
# token.
ADMIN_CALL = CallPolicy(timeout_s=10.0, attempts=1, reauth_retry=False)
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
    now = s.now()
    data = s.store.get_dashboard()
    status = data["status"] or {}
    override = data["override"]
    if override is not None and override.expires_at <= now:
        override = None
    progress = override_progress(
        override, data["override_changed_at"], status, data["controller"], data["config"].actuation_enabled
    )
    return render_template(
        "dashboard.html",
        now=now,
        config=data["config"],
        controller=data["controller"],
        status=status,
        override=override,
        override_progress=progress,
        next_cycle=controller.next_cycle_start(now, data["config"].controller),
        events=s.store.recent_events(15),
    )


def override_progress(
    override, changed_at: datetime | None, status: dict, doc: ControllerDoc, actuation_enabled: bool
) -> str | None:
    """Where a manual change stands, judged from the command record rather than tick timestamps.

    pending: no tick since the change. disabled: fan control is off, so nothing is sent.
    applied: the last accepted command matches the override. failed: the command for it didn't go
    through and will be resent. sending: a tick has run but no matching command is accepted yet.
    None: nothing to report (no recent change, or back to automatic and a tick has run).
    """
    if changed_at is None:
        return None
    if override is not None and not actuation_enabled:
        return "disabled"  # shadow mode never sends it, so don't say a tick will
    if not status or status.get("updated_at") is None or status["updated_at"] <= changed_at:
        return "pending"
    if override is None:
        return None
    last = doc.last_command
    if last is not None and last.on == override.on and last.result in RESEND_RESULTS - {"pending"}:
        return "failed"
    settled = last is None or last.result == ACCEPTED
    if settled and doc.last_accepted_on == override.on and not doc.command_state_uncertain:
        return "applied"
    return "sending"


@admin_bp.post("/override")
def set_override():
    s = services()
    now = s.now()
    mode, expires_at, error = forms.parse_override(request.form, now)
    if error:
        flash(error, "error")
    elif mode == "auto":
        s.store.clear_override(g.user, now)
        flash("Back to automatic control. The next tick applies it.", "info")
    else:
        s.store.set_override(mode == "on", expires_at, g.user, now)
        flash(f"Fans {mode.upper()} until {ago_until(expires_at, now)}. The next tick applies it.", "info")
    return redirect(url_for("admin.dashboard"))


@admin_bp.get("/settings")
def settings():
    config = services().store.get_config()
    return render_template("settings.html", config=config, form=None, errors={})


@admin_bp.post("/settings")
def save_settings():
    s = services()
    current = s.store.get_config()
    new, errors = forms.parse_settings(request.form, current)
    if errors:
        return render_template("settings.html", config=current, form=request.form, errors=errors), 400
    diff = forms.settings_diff(current, new)
    if not diff:
        flash("No changes.", "info")
        return redirect(url_for("admin.settings"))

    def apply(latest):
        # Only the fields this form owns; the kill switch and sensor link keep their latest values.
        return replace(
            latest,
            controller=new.controller,
            reassert_after=new.reassert_after,
            ifttt_on_event=new.ifttt_on_event,
            ifttt_off_event=new.ifttt_off_event,
            sensor_poll_interval=new.sensor_poll_interval,
        )

    committed = {}

    def audit(old, saved):
        # Against the config this transaction actually replaced, not the one the form was built on.
        # A retried transaction calls this again, so the last call is the one that committed.
        committed["diff"] = forms.settings_diff(old, saved)
        return {"changed": committed["diff"]}

    s.store.update_config(apply, g.user, "settings_changed", s.now(), audit)
    changed = ", ".join(sorted(committed["diff"])) or "nothing new (already saved)"
    flash(f"Saved: {changed}. The next tick uses them.", "info")
    return redirect(url_for("admin.settings"))


HISTORY_RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
HISTORY_TTL = timedelta(minutes=5)


@admin_bp.get("/history")
def history():
    s = services()
    now = s.now()
    range_key = request.args.get("range", "24h")
    if range_key not in HISTORY_RANGES:
        abort(400)
    config = s.store.get_config()
    sensor_id = config.active_sensor_id
    start = now - HISTORY_RANGES[range_key]
    readings, truncated, error = [], False, None
    if sensor_id:
        cache = current_app.extensions["greenhouse_history"]
        key = (sensor_id, range_key)
        hit = cache.get(key)
        if hit and hit[0] > now:
            readings, truncated = hit[1], hit[2]
        elif _admin_sensor_call():
            try:
                readings, truncated = s.sensors.history(sensor_id, start, ADMIN_CALL)
                cache[key] = (now + HISTORY_TTL, readings, truncated)
            except SensorUnavailable as exc:
                error = str(exc)
    # Every command in the range, plus the last one before it, which says whether the fans were on
    # when the range starts.
    before = s.store.last_event_before(start, "command_accepted")
    events = [before] if before else []
    in_range = s.store.events_since(start)
    events += [e for e in in_range if e.get("kind") == "command_accepted"]
    events_truncated = len(in_range) >= EVENTS_MAX
    chart = render_chart(
        readings, events, start, now, config.controller.high_f, config.controller.low_f, config.controller.timezone
    )
    return render_template(
        "history.html",
        now=now,
        config=config,
        range_key=range_key,
        ranges=list(HISTORY_RANGES),
        chart=chart,
        truncated=truncated,
        events_truncated=events_truncated,
        error=error,
        tz=ZoneInfo(config.controller.timezone),
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
    value = fetch(ADMIN_CALL)
    cache[name] = (now + LISTING_TTL, value)
    return value


def _fresh_reading(sensor_id: str):
    """(reading, problem): problem is None only when the reading is fresh enough to link."""
    s = services()
    if not _admin_sensor_call():
        return None, "rate limited"
    try:
        reading = s.sensors.latest_reading(sensor_id, ADMIN_CALL)
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

def ago_until(then: datetime, now: datetime) -> str:
    minutes = round((then - now).total_seconds() / 60)
    if minutes < 60:
        return f"{minutes} min from now"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min from now" if minutes else f"{hours} h from now"


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
    app.add_template_filter(ago_until)
    app.extensions["greenhouse_history"] = {}

    @app.after_request
    def security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    return app
