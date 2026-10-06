"""The idle-policy contract between the lane and the platform, pinned both ways.

The lane disables the platform's own idle suspend when it launches a MicroVM: it
sends an ``--idle-policy`` with ``autoResumeEnabled=False`` and both durations set
to ``MAX_LIFETIME_SECONDS``. The reason sits one layer down. This lane's turns
reach the crew over an SSM port-forward and the guest's front binds 127.0.0.1, so
the VM's proxy endpoint never sees turn traffic. Platform idle, which is measured
as inbound traffic on that proxy endpoint, would therefore read every crew as
permanently idle and suspend one mid-turn. The lane computes its own idle verdict
from the gateway's chat slots instead (``idle_verdict`` in
``kiro_crew.cloud.microvm.lifecycle``).

Both halves of that decision are pinned against the installed botocore service
model so that a botocore upgrade which changes the contract fails here rather
than in production: the shape of the policy the lane must send, and the
documented measurement that justifies opting out of it.
"""

from __future__ import annotations

import gzip
import json
import os

import botocore
import pytest

from kiro_crew.cloud.microvm import api


def _load_service_model() -> dict:
    """The installed lambda-microvms service model, read from its gzipped data."""
    base = os.path.join(os.path.dirname(botocore.__file__), "data", "lambda-microvms", "2025-09-09")
    with gzip.open(os.path.join(base, "service-2.json.gz")) as handle:
        return json.load(handle)


@pytest.fixture()
def model() -> dict:
    return _load_service_model()


@pytest.fixture()
def captured(monkeypatch):
    """Capture the argv each call builds, and answer with a usable shape.

    Copied from the fixture style in ``test_cloud_microvm_api.py``: the thing
    under test is what the lane *says*, so the test reads the argv the lane hands
    ``checked_json`` rather than any response.
    """
    calls: list[list[str]] = []

    def fake(args, profile="", region="", *, action="", timeout=0):
        calls.append(list(args))
        if "run-microvm" in args or "get-microvm" in args:
            return {
                "microvmId": "mvm-1",
                "state": "PENDING",
                "endpoint": "https://mvm-1.example/",
                "imageArn": "arn:aws:lambda:us-east-1:1:microvm-image/x",
                "imageVersion": "1",
                "maximumDurationInSeconds": 900,
                "startedAt": "2026-10-06T00:00:00Z",
            }
        return {}

    monkeypatch.setattr(api, "checked_json", fake)
    return calls


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _idle_policy_sent(argv: list[str]) -> dict:
    """The JSON object the lane passes after ``--idle-policy``."""
    return json.loads(_value_after(argv, "--idle-policy"))


def _run(captured: list[list[str]]) -> list[str]:
    api.run_microvm(
        image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
        image_version="1",
        run_hook_payload="{}",
        maximum_duration_in_seconds=900,
        client_token="tok",
        region="us-east-1",
    )
    return captured[0]


class TestModelContract:
    """What the installed botocore model requires of an IdlePolicy."""

    def test_the_model_still_requires_every_idle_policy_field(self, model):
        """All three fields are required, so omitting one is not an option; if
        this set changes, the lane's payload needs revisiting."""
        required = model["shapes"]["IdlePolicy"]["required"]
        assert sorted(required) == sorted(
            ["maxIdleDurationSeconds", "suspendedDurationSeconds", "autoResumeEnabled"]
        )

    def test_the_run_request_still_carries_an_idle_policy_member(self, model):
        assert "idlePolicy" in model["shapes"]["RunMicrovmRequest"]["members"]

    def test_the_model_still_floors_the_idle_duration_at_sixty(self, model):
        """The max-idle duration has a minimum of 60 and no maximum, which is the
        bound the durations the lane sends must satisfy."""
        shape = model["shapes"]["IdlePolicyMaxIdleDurationSecondsInteger"]
        assert shape["min"] == 60
        assert "max" not in shape

    def test_the_model_still_documents_proxy_endpoint_measurement(self, model):
        """Idle time is measured as inbound traffic on the proxy endpoint, and
        that is the fact that justifies the lane opting out. If AWS changes this
        measurement, this assertion fails and makes someone re-read the decision."""
        doc = model["shapes"]["IdlePolicy"]["documentation"]
        assert "inbound traffic through the MicroVM proxy endpoint" in doc


class TestLanePayload:
    """What the lane actually sends in its ``--idle-policy`` argument."""

    def test_the_lane_disables_platform_auto_resume(self, captured):
        """Platform idle cannot see turn traffic on an SSM port-forwarded crew,
        so auto-resume is sent off and the lane judges idle itself."""
        policy = _idle_policy_sent(_run(captured))
        assert policy["autoResumeEnabled"] is False

    def test_the_lane_pins_both_durations_to_the_max_lifetime(self, captured):
        """Both durations equal MAX_LIFETIME_SECONDS, which pushes any
        platform-side suspend past the VM's own lifetime."""
        policy = _idle_policy_sent(_run(captured))
        assert policy["maxIdleDurationSeconds"] == api.MAX_LIFETIME_SECONDS
        assert policy["suspendedDurationSeconds"] == api.MAX_LIFETIME_SECONDS

    def test_the_durations_the_lane_sends_satisfy_the_models_floor(self, captured, model):
        """The durations the lane sends clear the model's minimum of 60, so the
        payload is accepted on shape as well as on intent."""
        floor = model["shapes"]["IdlePolicyMaxIdleDurationSecondsInteger"]["min"]
        policy = _idle_policy_sent(_run(captured))
        assert policy["maxIdleDurationSeconds"] >= floor
        assert policy["suspendedDurationSeconds"] >= floor
