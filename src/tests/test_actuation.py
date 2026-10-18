from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from greenhouse import actuation as a
from greenhouse.actuation import Command, ControllerDoc, LeaseHeld, LeaseLost

T0 = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
REASSERT = timedelta(minutes=30)


def s(seconds):
    return T0 + timedelta(seconds=seconds)


def planned(doc, tick="t1", now=T0, desired=True, enabled=True):
    doc = a.acquire_lease(doc, tick, now)
    return a.plan_dispatch(doc, tick, now, desired, False, True, REASSERT, enabled)


def accepted(on=True, at=T0, generation=1):
    return ControllerDoc(
        generation=generation,
        last_command=Command(on=on, generation=generation, attempted_at=at, result=a.ACCEPTED),
        last_accepted_on=on,
        last_accepted_at=at,
        last_dispatch_at=at,
        baselined=True,
    )


# --- lease ---------------------------------------------------------------------------------

def test_lease_blocks_a_second_tick():
    doc = a.acquire_lease(ControllerDoc(), "t1", T0)
    with pytest.raises(LeaseHeld):
        a.acquire_lease(doc, "t2", s(10))


def test_expired_lease_can_be_taken_over():
    doc = a.acquire_lease(ControllerDoc(), "t1", T0)
    doc = a.acquire_lease(doc, "t2", s(61))
    assert doc.lease.owner == "t2"


def test_release_only_by_owner():
    doc = a.acquire_lease(ControllerDoc(), "t1", T0)
    assert a.release_lease(doc, "t2").lease is not None
    assert a.release_lease(doc, "t1").lease is None


def test_plan_without_lease_raises():
    with pytest.raises(LeaseLost):
        a.plan_dispatch(ControllerDoc(), "t1", T0, True, False, True, REASSERT, True)


# --- send predicate ------------------------------------------------------------------------

def test_first_desired_state_is_sent():
    doc, check, dispatch = planned(ControllerDoc(baselined=True))
    assert check.should_send and dispatch == a.Dispatch(True, 1)
    assert doc.last_command.result == a.PENDING and doc.generation == 1


def test_no_desired_state_sends_nothing():
    doc, check, dispatch = planned(ControllerDoc(), desired=None)
    assert dispatch is None and not check.should_send


def test_consecutive_unchanged_ticks_send_nothing_and_allocate_no_generation():
    doc = accepted()
    for minute in range(1, 30):
        doc, check, dispatch = planned(doc, tick=f"t{minute}", now=s(60 * minute))
        doc = a.release_lease(doc, f"t{minute}")
        assert dispatch is None, minute
        assert check.why == "unchanged"
    assert doc.generation == 1 and doc.last_command.result == a.ACCEPTED


def test_reassert_after_interval():
    doc, check, dispatch = planned(accepted(), now=s(30 * 60))
    assert dispatch is not None and check.why == "reassert interval elapsed"


def test_changed_desired_state_is_sent():
    doc, check, dispatch = planned(accepted(on=True), now=s(60), desired=False)
    assert dispatch == a.Dispatch(False, 2)


def test_minimum_spacing_between_dispatches():
    doc, check, dispatch = planned(accepted(on=True), now=s(29), desired=False)
    assert dispatch is None and check.why == "minimum spacing between dispatches"


@pytest.mark.parametrize("result", [a.PENDING, a.FAILED, a.ABANDONED, a.UNKNOWN])
def test_unsettled_previous_commands_are_resent(result):
    doc = accepted()
    doc = replace(doc, last_command=replace(doc.last_command, result=result))
    doc, check, dispatch = planned(doc, now=s(60))
    assert dispatch is not None and check.why == f"previous command {result}"


def test_shadow_mode_plans_but_writes_no_pending_command():
    doc, check, dispatch = planned(ControllerDoc(baselined=True), enabled=False)
    assert check.should_send and dispatch is None
    assert doc.last_command is None and doc.generation == 0


def test_plan_persists_controller_state():
    doc = a.acquire_lease(ControllerDoc(), "t1", T0)
    doc, _, _ = a.plan_dispatch(doc, "t1", T0, None, True, False, REASSERT, True)
    assert (doc.temp_demand, doc.baselined) == (True, False)


# --- pre-dispatch guard --------------------------------------------------------------------

def test_guard_passes_for_owner_with_time_left():
    doc, _, dispatch = planned(ControllerDoc(baselined=True))
    _, refusal = a.guard_dispatch(doc, "t1", dispatch.generation, s(5), True, True)
    assert refusal is None


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"now": s(46)}, "lease too close to expiry"),
        ({"now": s(61)}, "lease lost"),
        ({"tick": "intruder"}, "lease lost"),
        ({"enabled": False}, "actuation disabled"),
        ({"budget_ok": False}, "execution budget exhausted"),
        ({"generation": 99}, "generation superseded"),
    ],
)
def test_guard_refusals_mark_the_command_aborted(kwargs, reason):
    doc, _, dispatch = planned(ControllerDoc(baselined=True))
    args = {"tick": "t1", "generation": dispatch.generation, "now": s(5), "enabled": True, "budget_ok": True}
    args.update(kwargs)
    doc2, refusal = a.guard_dispatch(
        doc, args["tick"], args["generation"], args["now"], args["enabled"], args["budget_ok"]
    )
    assert refusal == reason
    if args["generation"] == dispatch.generation:
        assert doc2.last_command.aborted_before_send


def test_old_owner_resuming_after_takeover_cannot_send():
    doc, _, dispatch = planned(ControllerDoc(baselined=True), tick="old")
    # The old tick stalls past its lease; a new tick takes over and plans its own command.
    doc = a.acquire_lease(doc, "new", s(61))
    doc, _, new_dispatch = a.plan_dispatch(doc, "new", s(61), True, False, True, REASSERT, True)
    assert new_dispatch.generation == 2
    doc, refusal = a.guard_dispatch(doc, "old", dispatch.generation, s(62), True, True)
    assert refusal == "lease lost"
    assert doc.last_command.generation == 2 and not doc.last_command.aborted_before_send


# --- results -------------------------------------------------------------------------------

def test_accepted_result_updates_last_accepted_and_clears_uncertainty():
    doc, _, dispatch = planned(replace(ControllerDoc(baselined=True), command_state_uncertain=True))
    doc = a.record_result(doc, dispatch.generation, True, 200, s(2))
    assert doc.last_command.result == a.ACCEPTED
    assert (doc.last_accepted_on, doc.last_accepted_at) == (True, s(2))
    assert not doc.command_state_uncertain


def test_failed_result_keeps_uncertainty_and_is_resent_next_tick():
    doc, _, dispatch = planned(replace(ControllerDoc(baselined=True), command_state_uncertain=True))
    doc = a.record_result(doc, dispatch.generation, False, 500, s(2))
    assert doc.last_command.result == a.FAILED and doc.command_state_uncertain
    doc = a.release_lease(doc, "t1")
    _, check, again = planned(doc, tick="t2", now=s(60))
    assert again is not None


def test_stale_result_for_superseded_generation_is_ignored():
    doc, _, dispatch = planned(ControllerDoc(baselined=True))
    doc = replace(doc, generation=5)
    assert a.record_result(doc, dispatch.generation, True, 200, s(2)) == doc


# --- reconcile -----------------------------------------------------------------------------

def test_reconcile_refuses_while_lease_is_live():
    doc = a.acquire_lease(ControllerDoc(), "t1", T0)
    with pytest.raises(LeaseHeld):
        a.reconcile(doc, s(10))


def test_reconcile_after_pre_dispatch_abort_marks_abandoned():
    doc, _, dispatch = planned(ControllerDoc(baselined=True))
    doc, _ = a.guard_dispatch(doc, "t1", dispatch.generation, s(5), False, True)
    doc, outcome = a.reconcile(doc, s(90))
    assert doc.last_command.result == a.ABANDONED
    assert doc.command_state_uncertain and doc.generation == 2
    assert outcome == "pending command marked abandoned"


def test_reconcile_after_crash_following_dispatch_marks_unknown():
    doc, _, dispatch = planned(ControllerDoc(baselined=True))
    doc, refusal = a.guard_dispatch(doc, "t1", dispatch.generation, s(5), True, True)
    assert refusal is None  # sent... then the process died before recording the result
    doc, _ = a.reconcile(doc, s(90))
    assert doc.last_command.result == a.UNKNOWN


def test_late_result_after_reconcile_is_ignored():
    doc, _, dispatch = planned(ControllerDoc(baselined=True))
    doc, _ = a.reconcile(doc, s(90))
    late = a.record_result(doc, dispatch.generation, True, 200, s(91))
    assert late == doc and late.last_command.result == a.UNKNOWN


def test_reconcile_always_marks_uncertain_even_without_pending():
    doc, outcome = a.reconcile(accepted(), s(90))
    assert doc.command_state_uncertain and outcome == "no pending command"


def test_resume_after_manual_operation_resends_despite_reassert_interval():
    """Last accepted ON; Jeff turned the fans OFF by hand; the controller still wants ON."""
    doc, _ = a.reconcile(accepted(on=True), s(60))
    doc, check, dispatch = planned(doc, tick="t2", now=s(120), desired=True)
    assert dispatch == a.Dispatch(True, 3)
    assert check.why == "command state uncertain after reconcile"
    doc = a.record_result(doc, 3, True, 200, s(121))
    assert not doc.command_state_uncertain


def test_uncertain_flag_survives_a_failed_resend():
    doc, _ = a.reconcile(accepted(on=True), s(60))
    doc, _, dispatch = planned(doc, tick="t2", now=s(120))
    doc = a.record_result(doc, dispatch.generation, False, None, s(121))
    assert doc.command_state_uncertain


# --- serialization -------------------------------------------------------------------------

def test_round_trip():
    doc, _, _ = planned(ControllerDoc(baselined=True))
    doc = replace(doc, command_state_uncertain=True)
    assert a.from_dict(a.to_dict(doc)) == doc
    assert a.from_dict(None) == ControllerDoc()
