from dataclasses import replace
from datetime import timedelta

import pytest

from greenhouse import actuation, appconfig
from greenhouse.store import CONFIG, CONTROLLER, STATUS
from greenhouse.worker import Deadline, run_tick
from tests.fakes import Clock, FakeFans, FakeSensors, utc

# 12:20 New York time, inside active hours and outside the :05-:07 cycle.
NOON = utc(2026, 7, 15, 16, 20)
ON, OFF = "start_greenhouse_fans", "stop_greenhouse_fans"


@pytest.fixture
def make(store_factory):
    def build(temp=80.0, enabled=True, sensor="s1", **config_changes):
        clock = Clock(NOON)
        # Fetch every tick unless a test is about polling; most tests are about decisions.
        config_changes.setdefault("sensor_poll_interval", timedelta(minutes=1))
        config = replace(
            appconfig.AppConfig(), actuation_enabled=enabled, active_sensor_id=sensor, **config_changes
        )
        store = store_factory({CONFIG: appconfig.to_dict(config)})
        sensors = FakeSensors(clock, temps={"s1": temp})
        return store, sensors, FakeFans(), clock

    return build


def tick(store, sensors, fans, clock, **kw):
    return run_tick(store, sensors, fans, now=clock, **kw)


def controller(store):
    return actuation.from_dict(store.read_raw(CONTROLLER))


def test_hot_greenhouse_turns_fans_on_once(make):
    store, sensors, fans, clock = make(temp=101)
    assert tick(store, sensors, fans, clock).outcome == "sent"
    assert fans.sent == [ON]
    for _ in range(10):
        clock.advance(minutes=1)
        assert tick(store, sensors, fans, clock).outcome == "planned"
    assert fans.sent == [ON]
    assert controller(store).lease is None
    kinds = [e["kind"] for e in store.recent_events()]
    assert kinds == ["command_accepted"]


def test_reassert_after_thirty_minutes(make):
    store, sensors, fans, clock = make(temp=101)
    tick(store, sensors, fans, clock)
    clock.advance(minutes=30)
    tick(store, sensors, fans, clock)
    assert fans.sent == [ON, ON]


def test_cools_down_through_band_then_off(make):
    store, sensors, fans, clock = make(temp=101)
    tick(store, sensors, fans, clock)
    for temp in (95, 91, 90):
        sensors.temps["s1"] = temp
        clock.advance(minutes=1)
        tick(store, sensors, fans, clock)
    assert fans.sent == [ON]
    sensors.temps["s1"] = 89
    clock.advance(minutes=1)
    tick(store, sensors, fans, clock)
    assert fans.sent == [ON, OFF]


def test_shadow_mode_sends_nothing_but_records_status(make):
    store, sensors, fans, clock = make(temp=101, enabled=False)
    result = tick(store, sensors, fans, clock)
    assert result.outcome == "planned" and fans.sent == []
    status = store.read_raw(STATUS)
    assert status["desired_on"] is True and status["send_due"] is True
    assert status["actuation_enabled"] is False
    assert controller(store).last_command is None


def test_no_sensor_linked_never_calls_sensorpush_and_waits_for_baseline(make):
    store, sensors, fans, clock = make(sensor=None)
    clock.t = utc(2026, 7, 15, 16, 5)  # mid-cycle
    result = tick(store, sensors, fans, clock)
    assert sensors.calls == 0 and fans.sent == []
    assert result.detail["sensor_error"] == "no sensor linked"
    assert result.detail["reason"] == "waiting for the first fresh reading"


def test_sensor_failure_after_baseline_freezes_and_cycles_continue(make):
    store, sensors, fans, clock = make(temp=80)
    tick(store, sensors, fans, clock)
    assert fans.sent == [OFF]
    sensors.fail = True
    clock.t = utc(2026, 7, 15, 17, 5)  # 13:05 local, cycle starts
    tick(store, sensors, fans, clock)
    clock.advance(minutes=2)
    tick(store, sensors, fans, clock)
    assert fans.sent == [OFF, ON, OFF]
    assert store.read_raw(STATUS)["sensor_fresh"] is False


def test_stale_reading_is_not_fresh(make):
    store, sensors, fans, clock = make(temp=101)
    sensors.age = timedelta(days=99)  # the dead HT1 still returns a July sample as "latest"
    result = tick(store, sensors, fans, clock)
    assert result.detail["sensor_fresh"] is False and fans.sent == []


def test_ifttt_failure_is_resent_next_tick(make):
    store, sensors, fans, clock = make(temp=101)
    fans.fail_status = 500
    assert tick(store, sensors, fans, clock).outcome == "failed"
    assert controller(store).last_command.result == actuation.FAILED
    fans.fail_status = None
    clock.advance(minutes=1)
    assert tick(store, sensors, fans, clock).outcome == "sent"
    assert fans.sent == [ON]


def test_ifttt_timeout_is_failed_not_accepted(make):
    store, sensors, fans, clock = make(temp=101)
    fans.raise_timeout = True
    tick(store, sensors, fans, clock)
    assert controller(store).last_command.result == actuation.FAILED


def test_overlapping_tick_is_skipped(make):
    store, sensors, fans, clock = make(temp=101)
    store.tick_acquire("other-tick", clock())
    result = tick(store, sensors, fans, clock)
    assert result.outcome == "skipped" and fans.sent == [] and sensors.calls == 0
    assert controller(store).lease.owner == "other-tick"  # our finally must not release theirs


def test_kill_switch_flipped_mid_tick_prevents_send(make):
    store, sensors, fans, clock = make(temp=101)
    original = store.tick_guard

    def flip_then_guard(*args, **kwargs):
        store.update_config(lambda c: replace(c, actuation_enabled=False), "jeff", "actuation_disabled", clock())
        return original(*args, **kwargs)

    store.tick_guard = flip_then_guard
    result = tick(store, sensors, fans, clock)
    assert result.outcome == "refused" and result.detail["refusal"] == "actuation disabled"
    assert fans.sent == []
    assert controller(store).last_command.aborted_before_send


def test_exhausted_budget_refuses_to_send(make):
    store, sensors, fans, clock = make(temp=101)
    times = iter([0.0, 0.0, 0.0, 15.0, 15.0, 15.0, 15.0])
    deadline = Deadline(timedelta(seconds=20), clock=lambda: next(times, 15.0))
    result = tick(store, sensors, fans, clock, deadline=deadline)
    assert result.outcome == "refused" and result.detail["refusal"] == "execution budget exhausted"
    assert fans.sent == []


def test_crash_after_dispatch_then_reconcile_then_resume(make):
    store, sensors, fans, clock = make(temp=101)
    original = store.tick_record

    def crash(*args, **kwargs):
        raise RuntimeError("process killed after IFTTT accepted the call")

    store.tick_record = crash
    with pytest.raises(RuntimeError):
        tick(store, sensors, fans, clock)
    store.tick_record = original
    assert fans.sent == [ON]
    assert controller(store).last_command.result == actuation.PENDING

    clock.advance(seconds=90)
    outcome = store.reconcile("jeff", clock())
    assert outcome == "pending command marked unknown"
    assert controller(store).command_state_uncertain

    clock.advance(minutes=1)
    assert tick(store, sensors, fans, clock).outcome == "sent"
    assert not controller(store).command_state_uncertain


def test_manual_override_is_applied_by_the_next_tick(make):
    store, sensors, fans, clock = make(temp=80)
    tick(store, sensors, fans, clock)
    store.set_override(True, clock() + timedelta(hours=1), "jeff", clock())
    clock.advance(minutes=1)
    tick(store, sensors, fans, clock)
    assert fans.sent == [OFF, ON]
    clock.advance(hours=1)
    tick(store, sensors, fans, clock)
    assert fans.sent == [OFF, ON, OFF]


def test_linked_sensor_change_takes_effect_next_tick(make):
    store, sensors, fans, clock = make(temp=80)
    sensors.temps["s9"] = 101
    tick(store, sensors, fans, clock)
    store.update_config(lambda c: replace(c, active_sensor_id="s9"), "jeff", "sensor_linked", clock())
    clock.advance(minutes=1)
    result = tick(store, sensors, fans, clock)
    assert result.detail["sensor_id"] == "s9" and fans.sent == [OFF, ON]


def test_worker_fetches_on_its_poll_interval_and_reuses_the_reading_between(make):
    store, sensors, fans, clock = make(temp=80, sensor_poll_interval=timedelta(minutes=2))
    fetched = []
    for _ in range(5):
        fetched.append(tick(store, sensors, fans, clock).detail["sensor_fetched"])
        clock.advance(minutes=1)
    assert fetched == [True, False, True, False, True] and sensors.calls == 3
    status = store.read_raw(STATUS)
    assert status["reading"]["temperature_f"] == 80 and status["sensor_fetched_at"] == NOON + timedelta(minutes=4)


def test_reused_reading_keeps_its_age_so_it_still_goes_stale(make):
    store, sensors, fans, clock = make(temp=101, sensor_poll_interval=timedelta(minutes=2))
    sensors.age = timedelta(minutes=19, seconds=30)
    assert tick(store, sensors, fans, clock).detail["sensor_fresh"] is True
    clock.advance(minutes=1)
    result = tick(store, sensors, fans, clock)
    assert result.detail["sensor_fetched"] is False and result.detail["sensor_fresh"] is False


def test_new_sensor_waits_for_its_first_fetch(make):
    store, sensors, fans, clock = make(temp=80, sensor_poll_interval=timedelta(minutes=2))
    sensors.temps["s9"] = 101
    tick(store, sensors, fans, clock)
    store.update_config(lambda c: replace(c, active_sensor_id="s9"), "jeff", "sensor_linked", clock())
    clock.advance(minutes=1)
    waiting = tick(store, sensors, fans, clock)
    assert waiting.detail["sensor_fetched"] is False and waiting.detail["temperature_f"] is None
    clock.advance(minutes=1)
    assert tick(store, sensors, fans, clock).detail["temperature_f"] == 101


def test_sensorpush_calls_share_one_per_rolling_minute(make):
    store, *_ = make(sensor_poll_interval=timedelta(minutes=3))
    t0 = NOON

    def at(seconds):
        return t0 + timedelta(seconds=seconds)

    assert store.consume_sensorpush_call("worker", at(0))
    assert not store.consume_sensorpush_call("admin", at(30))  # within 60 s of the worker's call
    assert not store.consume_sensorpush_call("worker", at(60))  # not due yet
    assert store.consume_sensorpush_call("admin", at(60))
    assert not store.consume_sensorpush_call("admin", at(110))  # within 60 s of the last call
    # 65 s after the last call, but the worker's next fetch (at 165 s) is under a minute away.
    assert not store.consume_sensorpush_call("admin", at(125))
    assert store.consume_sensorpush_call("worker", at(165))


def test_cap_holds_across_a_clock_minute_boundary(make):
    store, *_ = make(sensor=None)
    assert store.consume_sensorpush_call("admin", utc(2026, 7, 15, 16, 20, 59))
    assert not store.consume_sensorpush_call("admin", utc(2026, 7, 15, 16, 21, 0))
    assert store.consume_sensorpush_call("admin", utc(2026, 7, 15, 16, 21, 59))


def test_admin_pages_stop_yielding_to_an_idle_worker(make):
    store, *_ = make(sensor_poll_interval=timedelta(minutes=2))
    assert store.consume_sensorpush_call("admin", NOON)  # the worker has never fetched
    assert store.consume_sensorpush_call("worker", NOON + timedelta(minutes=1))
    # The job is paused: once the worker is overdue by WORKER_IDLE_AFTER, admin pages may call.
    assert not store.consume_sensorpush_call("admin", NOON + timedelta(minutes=4))
    assert store.consume_sensorpush_call("admin", NOON + timedelta(minutes=5))


def test_a_stored_cap_cannot_raise_the_limit(store_factory):
    # An older config document that still carries a cap above one is ignored.
    store = store_factory({CONFIG: {"sensorpush_calls_per_minute": 5}})
    assert store.consume_sensorpush_call("admin", NOON)
    assert not store.consume_sensorpush_call("admin", NOON + timedelta(seconds=30))


def test_worker_sensor_call_is_a_single_request(make):
    store, sensors, fans, clock = make()
    tick(store, sensors, fans, clock)
    (policy,) = sensors.policies
    assert policy.attempts == 1 and not policy.reauth_retry


def test_every_event_expires_after_retention(make):
    from greenhouse.store import EVENT_RETENTION

    store, sensors, fans, clock = make(temp=101)
    tick(store, sensors, fans, clock)
    store.add_event("note", "jeff", clock())
    events = store.recent_events()
    assert len(events) == 2
    assert all(e["expire_at"] == e["at"] + EVENT_RETENTION for e in events)
