"""The three guest-side operations the gateway's lifecycle tick needs.

``MicroVmLifecycle`` reaches into a running crew for exactly three things: read
the guest's reduced state, stop its gateway child and confirm it exited, and ask
it to write its own archive. All three have to happen INSIDE the VM -- the home
is on the guest's disk and the slot accounting is the backend's -- so they cannot
be control-plane functions.

This module is how the control plane asks. It is a CLI rather than an HTTP route
on purpose: the channel is ``ssm:SendCommand``, which runs a shell command as the
crew user, and the front's routes all require the per-crew control secret that
the gateway would then have to carry into a maintenance pass. A command over SSM
is already authenticated by the node's own IAM identity.

Each verb is a thin wrapper over a function that already exists and is already
tested -- ``hooks.running_slots``, ``hooks.pack_data_home``, and the same
SIGTERM-then-confirm-exit sequence the wall watchdog seals with. Nothing here
decides anything; the verdicts live in the gateway's ``lifecycle.py``.

Output is ONE line of JSON on stdout, because the caller parses it out of an SSM
command invocation's ``StandardOutputContent``. Anything this module wants to say
to a human goes to stderr, where it reaches the command's error output without
corrupting the parse.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Optional

from container.microvm import hooks

#: Where the guest remembers when it last had a running slot.
#:
#: Persisted rather than computed, because "how long has this crew been idle" is
#: not a question a single observation can answer. The tick is the only caller and
#: it runs every minute, so this file is updated on that cadence and the number is
#: as fresh as the polling that reads it.
IDLE_STATE = f"{hooks.STATE_DIR}/idle.json"


def _read_json(path: str) -> "dict[str, Any]":
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def observe_idle(slots: int, *, now: Optional[float] = None) -> float:
    """Update the idle mark from *slots* and return seconds idle.

    A crew with a running slot resets the mark and is zero seconds idle. A crew
    with none is idle since the mark.

    The FIRST call initialises the mark to now and therefore reports zero, which
    is the safe direction: a guest whose idle history was lost -- a fresh boot, a
    resume, a cleared state dir -- must not be suspended on its first poll for
    having no record of being busy.
    """
    moment = time.time() if now is None else now
    state = _read_json(IDLE_STATE)
    last_busy = state.get("last_busy_at")
    if slots > 0 or not isinstance(last_busy, (int, float)):
        last_busy = moment
    hooks.write_state(IDLE_STATE, {"last_busy_at": float(last_busy)})
    return max(0.0, moment - float(last_busy))


def _self_packed_etag() -> str:
    """The ETag a completed self-pack recorded, or ``""``.

    Read from the watchdog's own state file rather than tracked here, so there is
    one writer of that fact and this is only a reader of it.
    """
    state = _read_json(hooks.WATCHDOG_STATE)
    if not state.get("packed"):
        return ""
    result = state.get("result")
    if not isinstance(result, dict):
        return ""
    return str(result.get("etag") or "")


def guest_state() -> "dict[str, Any]":
    """The reduction the gateway reads, in the shape ``GuestState`` takes.

    ``ready`` is the guest's OWN answer rather than something the gateway
    re-derives from a health check plus a slot list: two derivations of one
    predicate drift, and the drift shows up as a roster offering a turn to a crew
    that cannot take one.

    An unreadable slot probe is reported as BUSY (one running slot) and not as
    idle. ``hooks.running_slots`` raises rather than returning zero for exactly
    this reason, and a maintenance pass that read a failed probe as "idle" would
    suspend a crew mid-turn.
    """
    boot = _read_json(hooks.BOOT_STATE)
    try:
        slots = hooks.running_slots()
        ready = True
    except Exception:  # noqa: BLE001 - an unreadable probe is busy, never idle
        slots = 1
        ready = False
    return {
        "ready": ready and str(boot.get("stage") or "") == "started",
        "running_slots": slots,
        "idle_for_seconds": observe_idle(slots),
        "restarts": int(boot.get("restarts") or 0),
        "generation": int(boot.get("generation") or 0),
        "self_pack_armed": os.path.exists(hooks.WATCHDOG_STATE),
        # The ETag the guest's own wall pack wrote, so the host can condition its
        # next write on it. Empty unless a pack actually completed: the conflict
        # path records no ETag, and reporting one from it would hand the host a
        # generation that is not the archive's.
        "self_packed_etag": _self_packed_etag(),
    }


def seal() -> "dict[str, Any]":
    """Stop the crew's gateway and confirm it EXITED.

    A signal is a request, not an exit. The supervisor and the backend flush
    transcripts and checkpoint SQLite on the way down, and archiving while that is
    in flight captures a torn database -- which restores as a crew whose history
    ends mid-sentence or will not open at all. So this waits, and reports whether
    the wait succeeded rather than assuming it.
    """
    subprocess.run(
        ["pkill", "-TERM", "-f", "container.supervisor"],
        check=False,
        timeout=30,
    )
    deadline = time.time() + hooks.SEAL_TIMEOUT_SECONDS
    while time.time() < deadline:
        probe = subprocess.run(
            ["pgrep", "-f", "container.supervisor"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if probe.returncode != 0:
            return {"sealed": True}
        time.sleep(hooks.SEAL_POLL_SECONDS)
    # Not sealed, and said so rather than raised: the caller decides whether to
    # pack anyway, and that decision belongs to the gateway's lifecycle rather
    # than to this wrapper.
    return {"sealed": False, "reason": "the crew's gateway did not exit before the seal timeout"}


def pack(*, bucket: str, key: str, etag: str, kms_key_id: str, region: str) -> "dict[str, Any]":
    """Archive the data home under the condition the launch recorded.

    The ETag is the gateway's, passed in rather than read here: it is the
    generation the gateway believes in, and a guest that re-read it would turn a
    compare-and-set into last-write-wins.
    """
    result = hooks.pack_data_home(
        bucket=bucket,
        key=key,
        etag=etag,
        kms_key_id=kms_key_id,
        region=region,
        home=os.environ.get("SMC_DATA_HOME", hooks.DEFAULT_DATA_HOME),
    )
    return {"etag": str(result.get("etag") or ""), "bytes": int(result.get("bytes") or 0)}


def main(argv: Optional["list[str]"] = None) -> int:
    """``python3 -m container.microvm.ops {state|seal|pack}``.

    Refuses rather than defaulting when the verb is missing or unknown: the three
    differ in what they may DO -- ``state`` only reads, ``seal`` stops the crew,
    ``pack`` writes the archive -- so guessing which was meant is guessing whether
    this invocation may act.
    """
    parser = argparse.ArgumentParser(prog="microvm-ops", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("state", help="print the guest's reduced state")
    sub.add_parser("seal", help="stop the crew's gateway and confirm it exited")
    packer = sub.add_parser("pack", help="archive the data home")
    for flag in ("bucket", "key", "region"):
        packer.add_argument(f"--{flag}", required=True)
    packer.add_argument("--etag", default="")
    packer.add_argument("--kms-key-id", default="")
    args = parser.parse_args(argv)

    try:
        if args.verb == "state":
            payload = guest_state()
        elif args.verb == "seal":
            payload = seal()
        else:
            payload = pack(
                bucket=args.bucket,
                key=args.key,
                etag=args.etag,
                kms_key_id=args.kms_key_id,
                region=args.region,
            )
    except Exception as exc:  # noqa: BLE001 - the caller parses stdout, so report there
        # On stdout as JSON, because the gateway's only channel here is the
        # command's output and an exception that reached stderr alone would read
        # as an empty answer rather than as a failure.
        print(json.dumps({"error": repr(exc)}), flush=True)
        return 1
    print(json.dumps(payload), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `python -m`
    sys.exit(main())
