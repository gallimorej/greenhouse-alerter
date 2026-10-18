"""Lease and command bookkeeping for the single fan-command owner (the worker tick).

Every function here is a pure transition on ``ControllerDoc``. A store runs each one inside a single
transaction: Firestore in production, an in-memory lock in tests. That makes each transition atomic,
and keeps the hard cases (overlapping ticks, lease takeover, crashes, ambiguous deliveries) testable
without a database.

Command results:
- pending: a dispatch was planned. It may or may not have been sent.
- accepted: IFTTT returned 2xx. That is not proof the switch moved; there's no feedback from it.
- failed: IFTTT returned an error or timed out.
- abandoned: reconciled after the pre-dispatch guard refused to send; nothing went out.
- unknown: reconciled with no evidence either way; delivery may have happened.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

PENDING, ACCEPTED, FAILED, ABANDONED, UNKNOWN = "pending", "accepted", "failed", "abandoned", "unknown"
RESEND_RESULTS = {PENDING, FAILED, ABANDONED, UNKNOWN}

LEASE_TTL = timedelta(seconds=60)
MIN_DISPATCH_SPACING = timedelta(seconds=30)
# The IFTTT timeout plus a margin: a lease with less than this left is too close to expiry to send on.
MIN_LEASE_REMAINING = timedelta(seconds=15)


class LeaseHeld(Exception):
    """Another tick holds an unexpired lease."""


class LeaseLost(Exception):
    """This tick no longer owns the lease (expired and taken over, or never held)."""


@dataclass(frozen=True)
class Lease:
    owner: str
    expires_at: datetime


@dataclass(frozen=True)
class Command:
    on: bool
    generation: int
    attempted_at: datetime
    result: str = PENDING
    http_status: int | None = None
    aborted_before_send: bool = False


@dataclass(frozen=True)
class ControllerDoc:
    temp_demand: bool = False
    baselined: bool = False
    generation: int = 0
    lease: Lease | None = None
    last_command: Command | None = None
    last_accepted_on: bool | None = None
    last_accepted_at: datetime | None = None
    last_dispatch_at: datetime | None = None
    # Set by reconcile: the last accepted command may no longer describe the switch (for example,
    # after the fans were run by hand), so the next eligible tick resends whatever is desired.
    command_state_uncertain: bool = False


@dataclass(frozen=True)
class Dispatch:
    on: bool
    generation: int


@dataclass(frozen=True)
class SendCheck:
    should_send: bool
    why: str


def lease_free(doc: ControllerDoc, now: datetime) -> bool:
    return doc.lease is None or doc.lease.expires_at <= now


def owns_lease(doc: ControllerDoc, tick_id: str, now: datetime) -> bool:
    return doc.lease is not None and doc.lease.owner == tick_id and doc.lease.expires_at > now


def acquire_lease(doc: ControllerDoc, tick_id: str, now: datetime, ttl: timedelta = LEASE_TTL) -> ControllerDoc:
    if not lease_free(doc, now) and doc.lease.owner != tick_id:
        raise LeaseHeld(doc.lease.owner)
    return replace(doc, lease=Lease(tick_id, now + ttl))


def release_lease(doc: ControllerDoc, tick_id: str) -> ControllerDoc:
    if doc.lease is not None and doc.lease.owner == tick_id:
        return replace(doc, lease=None)
    return doc


def send_check(
    doc: ControllerDoc, desired_on: bool | None, now: datetime, reassert_after: timedelta
) -> SendCheck:
    """Whether to dispatch, judged against the previous command (never a new pending one)."""
    if desired_on is None:
        return SendCheck(False, "no desired state")
    if doc.last_dispatch_at is not None and now - doc.last_dispatch_at < MIN_DISPATCH_SPACING:
        return SendCheck(False, "minimum spacing between dispatches")
    if doc.command_state_uncertain:
        return SendCheck(True, "command state uncertain after reconcile")
    last = doc.last_command
    if last is not None and last.result in RESEND_RESULTS:
        return SendCheck(True, f"previous command {last.result}")
    if doc.last_accepted_on is None or doc.last_accepted_on != desired_on:
        return SendCheck(True, "desired state changed")
    if doc.last_accepted_at is None or now - doc.last_accepted_at >= reassert_after:
        return SendCheck(True, "reassert interval elapsed")
    return SendCheck(False, "unchanged")


def plan_dispatch(
    doc: ControllerDoc,
    tick_id: str,
    now: datetime,
    desired_on: bool | None,
    temp_demand: bool,
    baselined: bool,
    reassert_after: timedelta,
    actuation_enabled: bool,
) -> tuple[ControllerDoc, SendCheck, Dispatch | None]:
    """Record the new controller state and, if a send is due and allowed, a pending command."""
    if not owns_lease(doc, tick_id, now):
        raise LeaseLost(tick_id)
    doc = replace(doc, temp_demand=temp_demand, baselined=baselined)
    check = send_check(doc, desired_on, now, reassert_after)
    if not check.should_send or not actuation_enabled:
        return doc, check, None
    generation = doc.generation + 1
    command = Command(on=desired_on, generation=generation, attempted_at=now)
    doc = replace(doc, generation=generation, last_command=command, last_dispatch_at=now)
    return doc, check, Dispatch(desired_on, generation)


def guard_dispatch(
    doc: ControllerDoc,
    tick_id: str,
    generation: int,
    now: datetime,
    actuation_enabled: bool,
    budget_ok: bool,
    control_enabled: bool = True,
) -> tuple[ControllerDoc, str | None]:
    """Last check before sending. Returns (doc, refusal reason); a None reason means send."""
    if not owns_lease(doc, tick_id, now):
        refusal = "lease lost"
    elif doc.lease.expires_at - now < MIN_LEASE_REMAINING:
        refusal = "lease too close to expiry"
    elif not actuation_enabled:
        refusal = "actuation disabled"
    elif not control_enabled:
        refusal = "control paused"
    elif not budget_ok:
        refusal = "execution budget exhausted"
    elif doc.generation != generation:
        refusal = "generation superseded"
    else:
        return doc, None
    if doc.last_command is not None and doc.last_command.generation == generation:
        doc = replace(doc, last_command=replace(doc.last_command, aborted_before_send=True))
    return doc, refusal


def record_result(
    doc: ControllerDoc, generation: int, accepted: bool, http_status: int | None, now: datetime
) -> ControllerDoc:
    """Store the IFTTT outcome, unless a newer generation (or a reconcile) has superseded it."""
    last = doc.last_command
    if last is None or last.generation != generation or doc.generation != generation:
        return doc
    if accepted:
        return replace(
            doc,
            last_command=replace(last, result=ACCEPTED, http_status=http_status),
            last_accepted_on=last.on,
            last_accepted_at=now,
            command_state_uncertain=False,
        )
    return replace(doc, last_command=replace(last, result=FAILED, http_status=http_status))


def reconcile(doc: ControllerDoc, now: datetime) -> tuple[ControllerDoc, str]:
    """Settle a stopped controller (rollback step c'): invalidate in-flight work, mark uncertainty."""
    if not lease_free(doc, now):
        raise LeaseHeld(doc.lease.owner)
    generation = doc.generation + 1
    last = doc.last_command
    outcome = "no pending command"
    if last is not None and last.result == PENDING:
        result = ABANDONED if last.aborted_before_send else UNKNOWN
        last = replace(last, result=result)
        outcome = f"pending command marked {result}"
    doc = replace(
        doc, generation=generation, last_command=last, lease=None, command_state_uncertain=True
    )
    return doc, outcome


# --- (de)serialization for stores ------------------------------------------------------------

def to_dict(doc: ControllerDoc) -> dict:
    def lease(l):
        return None if l is None else {"owner": l.owner, "expires_at": l.expires_at}

    def command(c):
        return None if c is None else {
            "on": c.on,
            "generation": c.generation,
            "attempted_at": c.attempted_at,
            "result": c.result,
            "http_status": c.http_status,
            "aborted_before_send": c.aborted_before_send,
        }

    return {
        "temp_demand": doc.temp_demand,
        "baselined": doc.baselined,
        "generation": doc.generation,
        "lease": lease(doc.lease),
        "last_command": command(doc.last_command),
        "last_accepted_on": doc.last_accepted_on,
        "last_accepted_at": doc.last_accepted_at,
        "last_dispatch_at": doc.last_dispatch_at,
        "command_state_uncertain": doc.command_state_uncertain,
    }


def from_dict(data: dict | None) -> ControllerDoc:
    if not data:
        return ControllerDoc()
    lease = data.get("lease")
    command = data.get("last_command")
    return ControllerDoc(
        temp_demand=bool(data.get("temp_demand", False)),
        baselined=bool(data.get("baselined", False)),
        generation=int(data.get("generation", 0)),
        lease=Lease(lease["owner"], lease["expires_at"]) if lease else None,
        last_command=Command(**command) if command else None,
        last_accepted_on=data.get("last_accepted_on"),
        last_accepted_at=data.get("last_accepted_at"),
        last_dispatch_at=data.get("last_dispatch_at"),
        command_state_uncertain=bool(data.get("command_state_uncertain", False)),
    )
