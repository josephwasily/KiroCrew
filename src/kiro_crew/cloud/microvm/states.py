"""The MicroVM crew state machine: one explicit edge table, one writer.

A MicroVM crew is not a Fargate task with a suspend button bolted on. It has a
hard 28,800-second wall clock that covers running *and* suspended time, a disk
that disappears with the VM, and an S3 archive whose ETag is the only thing that
makes a reopen safe. Each of those turns a failure into a distinct position the
owner can be left in, and a position the product cannot name is a position it
cannot recover from.

So the states here are not a tidy enumeration of a happy path. Every one of them
exists because something can go wrong at that point and leave the crew somewhere
that needs a different answer:

``TERMINATED_UNARCHIVED``
    The VM is gone and the archive write did not succeed. The crew's work is at
    risk, which is the opposite report from ``STOPPED`` even though both describe
    a crew with no VM.

``RESUME_TARGET_GONE``
    A *suspended* VM reached the wall and the platform terminated it. Suspending
    does not stop the clock, so a crew parked overnight is gone in the morning
    with nothing in the record saying why unless this state says it.

``RESTORE_FAILED``
    A reopen fetched the archive and could not lay it down. The crew must go back
    to ``STOPPED`` **at the same ETag**, because the next pack from a half-restored
    home would overwrite the last good archive with less than it held.

:data:`EFFECTIVE_UNKNOWN` is derived and never stored, for the one thing a stored
state cannot express: the control plane has not heard from this crew recently
enough to repeat what it last said. A laptop that slept for an hour holds a record
saying ``RUNNING`` that is a guess, not an observation, and a product that reports
the guess as fact offers an "Open crew" button for a VM the wall already took.

:func:`transition` is the only writer. Every edge is in :data:`EDGES` and nothing
else may move a crew between states, so adding a path means adding a row that a
reviewer can read rather than finding the assignment that made it.
"""

from __future__ import annotations

from typing import Optional

#: A launch is in flight: the VM exists (or is being created) and nothing has
#: confirmed the guest answers yet.
PENDING = "pending"

#: The VM is up and the crew's gateway answered within the observation window.
RUNNING = "running"

#: The VM is suspended. Its disk is intact and the home is NOT archived, so a
#: resume is cheap -- but the wall clock is still running against it.
SUSPENDED = "suspended"

#: The VM is terminated and the home is archived under a known ETag. This is the
#: only state a reopen may start from.
STOPPED = "stopped"

#: The VM is gone and the archive write did not succeed. Terminal, and a report
#: the owner must see: it is the one state that means work may have been lost.
TERMINATED_UNARCHIVED = "terminated_unarchived"

#: A suspended VM hit the platform's maximum lifetime and was terminated with it.
#: Terminal. Distinct from :data:`TERMINATED_UNARCHIVED` because nothing failed --
#: the crew was parked past a limit that suspending does not pause.
RESUME_TARGET_GONE = "resume_target_gone"

#: A reopen could not lay the archive down. Recoverable, and recovery is a return
#: to :data:`STOPPED` carrying the SAME ETag, never a fresh pack.
RESTORE_FAILED = "restore_failed"

#: The launch itself failed. Terminal, and kept rather than deleted because a
#: launch that failed after creating anything may have left a VM or an activation
#: behind, and the sweeper reads records to decide what is an orphan.
LAUNCH_FAILED = "launch_failed"

#: The retention window closed and the archive was deleted. Terminal.
EXPIRED = "expired"

#: Every state a record may hold.
STORED_STATES: tuple[str, ...] = (
    PENDING,
    RUNNING,
    SUSPENDED,
    STOPPED,
    TERMINATED_UNARCHIVED,
    RESUME_TARGET_GONE,
    RESTORE_FAILED,
    LAUNCH_FAILED,
    EXPIRED,
)

#: The states from which no event leads anywhere. Derived from :data:`EDGES`
#: rather than listed, so a new edge out of one of them stops it being terminal
#: without anyone remembering to edit a second list.
#:
#: Assigned below the table, since it reads it.

# ── Events ───────────────────────────────────────────────────────────────────

#: A launch has begun. The only event with no origin state.
EVENT_LAUNCH_STARTED = "launch_started"
#: The guest answered. Readiness is a separate predicate; this is reachability.
EVENT_ONLINE = "online"
#: The launch could not be completed.
EVENT_LAUNCH_FAILED = "launch_failed"
#: The idle verdict (or the owner) asked for a suspend and it succeeded.
EVENT_SUSPENDED = "suspended"
#: A suspended VM was resumed and answers again.
EVENT_RESUMED = "resumed"
#: A suspended VM was found terminated by the platform.
EVENT_RESUME_TARGET_GONE = "resume_target_gone"
#: The archive write succeeded and the VM was terminated after it.
EVENT_PACK_OK = "pack_ok"
#: The archive write failed, and the VM is gone anyway.
EVENT_PACK_FAILED = "pack_failed"
#: A reopen of an archived crew has begun.
EVENT_REOPEN_STARTED = "reopen_started"
#: A reopen's restore step failed.
EVENT_RESTORE_FAILED = "restore_failed"
#: A failed restore was walked back to the archived state it came from.
EVENT_RESTORE_RECOVERED = "restore_recovered"
#: The retention window closed and the archive was deleted.
EVENT_EXPIRED = "expired"

#: Every event :func:`transition` accepts.
EVENTS: tuple[str, ...] = (
    EVENT_LAUNCH_STARTED,
    EVENT_ONLINE,
    EVENT_LAUNCH_FAILED,
    EVENT_SUSPENDED,
    EVENT_RESUMED,
    EVENT_RESUME_TARGET_GONE,
    EVENT_PACK_OK,
    EVENT_PACK_FAILED,
    EVENT_REOPEN_STARTED,
    EVENT_RESTORE_FAILED,
    EVENT_RESTORE_RECOVERED,
    EVENT_EXPIRED,
)

#: ``(from state, event) -> to state``. The whole machine, and the only place a
#: path between two states is written down.
#:
#: ``None`` as the origin is the launch of a crew that has no record yet. It is a
#: key rather than a special case in :func:`transition`, so "where can a crew
#: start" is answered by reading this table like every other question about it.
EDGES: dict[tuple[Optional[str], str], str] = {
    (None, EVENT_LAUNCH_STARTED): PENDING,
    (PENDING, EVENT_ONLINE): RUNNING,
    (PENDING, EVENT_LAUNCH_FAILED): LAUNCH_FAILED,
    # A reopen enters PENDING and its restore runs there, so the restore failure
    # edge leaves PENDING and not STOPPED.
    (PENDING, EVENT_RESTORE_FAILED): RESTORE_FAILED,
    (RUNNING, EVENT_SUSPENDED): SUSPENDED,
    (RUNNING, EVENT_PACK_OK): STOPPED,
    (RUNNING, EVENT_PACK_FAILED): TERMINATED_UNARCHIVED,
    (SUSPENDED, EVENT_RESUMED): RUNNING,
    (SUSPENDED, EVENT_RESUME_TARGET_GONE): RESUME_TARGET_GONE,
    # A suspended crew is packed by resuming it first: the archive is a tar of a
    # quiesced home, and a suspended VM cannot run tar. So SUSPENDED has no pack
    # edge of its own, deliberately -- the lifecycle resumes, then packs.
    (STOPPED, EVENT_REOPEN_STARTED): PENDING,
    (STOPPED, EVENT_EXPIRED): EXPIRED,
    (RESTORE_FAILED, EVENT_RESTORE_RECOVERED): STOPPED,
}

#: States with no outgoing edge. Read from :data:`EDGES` so it cannot drift.
TERMINAL_STATES: frozenset[str] = frozenset(
    state for state in STORED_STATES if not any(origin == state for origin, _ in EDGES)
)

#: The derived answer for a crew whose last observation is older than the caller's
#: staleness bound. NEVER stored: it is a statement about the control plane's
#: knowledge, not about the crew, and writing it would make the next reader think
#: the crew itself had moved.
EFFECTIVE_UNKNOWN = "unknown"

#: How old an observation may be before :func:`effective_state` stops repeating it.
#:
#: Three minutes is the tick interval (one minute) with room for two missed ticks:
#: one missed tick is a busy host, three is a control plane that is not running.
#: A laptop that slept, a gateway that was restarted and a crashed tick all land
#: here, and all three mean the same thing to a reader -- nobody has looked.
DEFAULT_STALE_AFTER_SECONDS = 180


class IllegalTransition(ValueError):
    """An event that :data:`EDGES` has no row for.

    Raised rather than ignored, and rather than coerced to the nearest sensible
    state. A caller asking a ``STOPPED`` crew to suspend has a bug in the caller;
    answering it with ``SUSPENDED`` would record a VM that does not exist, and
    answering it with ``STOPPED`` would hide the bug until a reopen failed.
    """


def transition(current: Optional[str], event: str) -> str:
    """The state *current* moves to on *event*, or raise :class:`IllegalTransition`.

    The only writer of a crew's state. Pure: it reads the table and returns a
    string, so every caller stores the result itself and no hidden path can move a
    crew while this function is not looking.

    *current* is ``None`` for a crew with no record yet, which is a key in
    :data:`EDGES` rather than a branch here.
    """
    if event not in EVENTS:
        raise IllegalTransition(f"unknown event {event!r}")
    if current is not None and current not in STORED_STATES:
        raise IllegalTransition(f"unknown state {current!r}")
    try:
        return EDGES[(current, event)]
    except KeyError:
        raise IllegalTransition(
            f"a crew in state {current!r} cannot take event {event!r}: "
            f"{sorted(e for (s, e) in EDGES if s == current)} are the events it accepts"
        ) from None


def effective_state(
    stored: str,
    *,
    age_seconds: Optional[float],
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """What to REPORT for a crew whose last observation is *age_seconds* old.

    Returns :data:`EFFECTIVE_UNKNOWN` for a live state nobody has confirmed
    recently, and the stored state otherwise. ``age_seconds`` of ``None`` means
    the crew has never been observed, which is as unknown as an old observation.

    A terminal state is reported as itself however old it is. Nothing can move a
    terminated crew, so an old observation of one is not a stale reading -- it is
    the answer, and degrading it to ``unknown`` would ask the owner to wait for
    news that will never come.
    """
    if stored in TERMINAL_STATES:
        return stored
    if age_seconds is None or age_seconds > stale_after_seconds:
        return EFFECTIVE_UNKNOWN
    return stored
