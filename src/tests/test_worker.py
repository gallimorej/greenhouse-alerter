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


def test_sensorpush_budget_reserves_worker_and_caps_admin(make):
    store, *_ = make()
    now = NOON
    assert all(store.consume_sensorpush_call("admin", now) for _ in range(4))
    assert not store.consume_sensorpush_call("admin", now)
    assert store.consume_sensorpush_call("worker", now)
    assert store.consume_sensorpush_call("admin", now + timedelta(minutes=1))
