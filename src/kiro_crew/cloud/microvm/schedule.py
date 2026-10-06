"""Install the lane's cron jobs, and take them away when the lane goes away.

:data:`~kiro_crew.cloud.microvm.tick.CRON_SPECS` says what should run and how
often. This is what makes that true of a running gateway, because a schedule
nothing installed is documentation.

**Why the lane needs a schedule at all.** Two of the lane's promises are
periodic rather than event-driven. A crew is suspended when it has been idle for
fifteen minutes, which nothing asks for -- it has to be noticed. And a crew near
its wall is packed by the gateway when the guest's own watchdog did not, which is
also a thing noticed rather than requested. Without an installed tick both
reduce to "someone runs a command", and the failure is silent: the crew keeps
billing, then loses its home at the platform's maximum lifetime.

**Idempotent, by the store's own check and not by ours.**
``CronService.add_job_if_absent`` does the absence test and the append under ONE
store lock after a fresh sync, so two registrars racing cannot both see a name as
absent. A read-then-add here would be that race.

**Owned, so removal is unambiguous.** Every job this module installs carries
:data:`OWNER` in ``created_by``, and :func:`uninstall` removes exactly the jobs
carrying it. Matching on the name prefix instead would let a user's own job named
``microvm-...`` be deleted by a lane they turned off.
"""

from __future__ import annotations

import logging
import shlex
import sys
from typing import Any, Callable, Optional

from kiro_crew.cloud.microvm.tick import CRON_SPECS

logger = logging.getLogger(__name__)

#: The ``created_by`` tag on every job this module installs. One string, so the
#: installer and the remover cannot disagree about what the lane owns.
OWNER = "kirocrew:microvm-lane"

#: The module whose ``__main__`` each job runs. Named here rather than built into
#: each command string so the two jobs cannot drift onto different entry points.
TICK_MODULE = "kiro_crew.cloud.microvm.tick"


def command_for(name: str) -> str:
    """The shell command one spec's job runs.

    A COMMAND job rather than a message job: these are maintenance passes with
    no conversation to have, and a message job would spend a model turn to decide
    to call the same function.
    """
    # THIS interpreter, quoted, and not a bare ``python``. The job runs through
    # the sandboxed command path, where ``python`` need not be the environment
    # Kiro Crew is installed in -- and a job whose interpreter cannot import the
    # package fails every time it fires, silently, because these jobs are
    # ``silent: True``.
    return f"{shlex.quote(sys.executable)} -m {TICK_MODULE} {_verb_for(name)}"


def _verb_for(name: str) -> str:
    """Which pass a spec names, from the spec's own id."""
    return "sweep" if name.endswith("sweep") else "tick"


def desired_jobs() -> tuple[dict[str, Any], ...]:
    """The jobs the lane wants installed, derived from :data:`CRON_SPECS`.

    Derived rather than listed, so the guide, the installer and the schedule the
    gateway actually runs cannot disagree. A spec added to ``CRON_SPECS`` is
    installed by the next registration with no edit here.

    Every spec is installed. Both passes have production entry points, so there
    is no gap between what the lane declares and what it can run.
    """
    return tuple(
        {
            "name": name,
            "cron_expr": expr,
            "command": command_for(name),
            "message": reason,
            "created_by": OWNER,
            "silent": True,
            "hide_in_chat": True,
        }
        for name, expr, reason in CRON_SPECS
    )


def _owned(job: Any) -> bool:
    return str(getattr(job, "created_by", "") or "") == OWNER


def install(service: Any) -> list[str]:
    """Install every job in :data:`CRON_SPECS` that is not already there.

    Returns the names of the jobs the lane owns afterwards, which is the full set
    on a first call and the same set on every call after it. Returning what is
    PRESENT rather than what was added is deliberate: a caller checking the
    schedule is asking the former, and a second registration legitimately adds
    nothing.
    """
    if hasattr(service, "raise_if_store_unreadable"):
        service.raise_if_store_unreadable()
    for spec in desired_jobs():
        name = spec["name"]
        service.add_job_if_absent(
            lambda job, _name=name: str(getattr(job, "name", "")) == _name and _owned(job),
            **spec,
        )
    present = installed(service)
    logger.info("microvm lane schedule: %d job(s) installed", len(present))
    return present


def installed(service: Any) -> list[str]:
    """The names of the jobs this lane owns, in :data:`CRON_SPECS` order."""
    if hasattr(service, "raise_if_store_unreadable"):
        service.raise_if_store_unreadable()
    owned = {str(getattr(job, "name", "")) for job in service.list_jobs() if _owned(job)}
    return [name for name, _expr, _why in CRON_SPECS if name in owned]


def uninstall(service: Any) -> list[str]:
    """Remove every job this lane owns. Returns the names removed.

    By OWNER, not by name: a user's own job that happens to be called
    ``microvm-sweep`` is theirs, and a lane being turned off must not delete it.
    """
    if hasattr(service, "raise_if_store_unreadable"):
        service.raise_if_store_unreadable()
    removed: list[str] = []
    for job in list(service.list_jobs()):
        if not _owned(job):
            continue
        job_id = str(getattr(job, "id", "") or "")
        if job_id and service.remove_job(job_id):
            removed.append(str(getattr(job, "name", "")))
    if removed:
        logger.info("microvm lane schedule: removed %s", ", ".join(removed))
    return removed


def reconcile(
    service: Any,
    *,
    lane_configured: bool,
) -> list[str]:
    """Make the installed schedule match whether the lane exists.

    One function with the whole rule in it, called from wherever the lane's
    configuration is read, because the two halves have to agree: installing on
    registration and never removing leaves a tick polling a lane whose block was
    deleted, and every pass of it is an error about a config that is gone.

    Returns the job names present afterwards -- the full set when the lane is
    configured, empty when it is not.
    """
    if lane_configured:
        return install(service)
    uninstall(service)
    return []


def reconcile_safely(
    service_factory: Callable[[], Optional[Any]],
    *,
    lane_configured: bool,
) -> list[str]:
    """:func:`reconcile`, with no failure of its own allowed to matter.

    The caller is a gateway that must come up whether or not a schedule could be
    written: a cron store that is unreadable is a reason to report and keep
    serving, not a reason to refuse every request. The factory is called inside
    the guard too, so a gateway with no cron service at all is the quiet case
    rather than an import-time error.
    """
    try:
        service = service_factory()
        if service is None:
            return []
        return reconcile(service, lane_configured=lane_configured)
    except Exception as exc:  # noqa: BLE001 - a schedule is not worth a dead gateway
        logger.warning("microvm lane schedule could not be reconciled: %r", exc)
        return []
