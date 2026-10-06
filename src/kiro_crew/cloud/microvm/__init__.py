"""The AWS Lambda MicroVM remote-crew lane.

A crew on this lane runs inside a Firecracker MicroVM that the platform suspends
when the crew is idle and terminates at a non-adjustable eight-hour maximum
lifetime. That shape is the whole reason the lane exists -- a Fargate task has no
suspend, so it bills for every hour the owner is asleep -- and it is also the
whole reason the lane needs more than a launch engine: a compute that goes away on
a clock needs somewhere for the crew's home to live, and a rule about who may
write it.

The parts, and which question each answers:

``states``
    Where a crew can be, and the only function that moves it.
``record``
    What the control plane remembers about one crew, and where that is stored.
``api``
    The ``lambda-microvms`` calls, through the one ``aws`` CLI chokepoint.
``image``
    A crew's image is BUILT from an AWS-managed base plus that crew's own signed
    bundle, and cached by a digest of those two inputs. Identity is the inputs,
    because a name that merely labels a build lets a cache serve the wrong content.
``recipe``
    What goes IN that build: the Fargate lane's own ``Dockerfile.crew`` plus the
    crew bundle, zipped. A join rather than a second path -- the Dockerfile, the
    required layout and the bundle digest are all read from the lane that already
    answered them.
``payload``
    What the platform hands the guest at boot, including the wall-clock edges the
    guest arms itself from.
``pack``
    The archive, and the ETag condition that makes a reopen safe.
``lifecycle``
    Suspend, resume, pack and poll: a second port beside ``LaunchEngine``, which
    only this lane implements.
``engine``
    The five-method ``LaunchEngine``.
``sweeper``
    The two classes of orphan this lane can leak, dry-run by default.
``tick``
    The two cron entries that drive the loop.
The docker-backed stand-in that makes all of the above testable with no AWS account
is NOT here: it drives ``docker`` with a caller-supplied argv, which does not belong in
the shipped package, so it lives beside the tests that use it in
``test/microvm_harness/local_engine.py``.

Fargate is untouched by any of it. Every lane-neutral piece -- the launch step
machine, the progress UI, the instances registry, the SSM port-forward -- is
reused in place rather than moved, so this lane's review is about this lane.
"""

from __future__ import annotations

from kiro_crew.cloud.microvm.engine import (
    MICROVM_PROVISIONER_ID,
    MicroVmLaunchEngine,
    MicroVmLaunchSpec,
)
from kiro_crew.cloud.microvm.lifecycle import CrewLifecycle, GuestState, MicroVmLifecycle
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

__all__ = [
    "MICROVM_PROVISIONER_ID",
    "CrewLifecycle",
    "CrewRecord",
    "CrewStore",
    "GuestState",
    "MicroVmLaunchEngine",
    "MicroVmLaunchSpec",
    "MicroVmLifecycle",
]
