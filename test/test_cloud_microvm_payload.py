"""The run-hook payload and the wall-clock arithmetic the guest arms itself from."""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm.api import MAX_LIFETIME_SECONDS, MAX_RUN_HOOK_PAYLOAD_BYTES
from kiro_crew.cloud.microvm.payload import (
    WALL_HARD_SECONDS,
    WALL_SOFT_SECONDS,
    RunHookPayload,
    compute_wall_leads,
    decode_payload,
    soft_pack_due,
    wall_deadline,
)


class TestWallLeads:
    def test_a_full_length_vm_gets_the_stated_edges(self):
        leads = compute_wall_leads(MAX_LIFETIME_SECONDS)
        assert leads.soft_at == WALL_SOFT_SECONDS
        assert leads.hard_at == WALL_HARD_SECONDS

    def test_the_edges_are_ordered_inside_the_life(self):
        leads = compute_wall_leads(MAX_LIFETIME_SECONDS)
        assert 0 < leads.soft_at < leads.hard_at < leads.wall_seconds

    @pytest.mark.parametrize("wall", [60, 300, 900, 3600, 7200, 28_800])
    def test_every_lifetime_produces_edges_inside_its_own_life(self, wall):
        """A short crew must not inherit the eight-hour crew's leads."""
        leads = compute_wall_leads(wall)
        assert 0 < leads.soft_at < leads.hard_at < wall

    def test_a_short_lifetime_does_not_pack_past_its_own_end(self):
        leads = compute_wall_leads(900)
        assert leads.hard_at < 900
        # The eight-hour crew's soft edge is 27,000s, which is past a 900s crew's
        # whole life. The clamp is what keeps the edge reachable.
        assert leads.soft_at < WALL_SOFT_SECONDS

    @pytest.mark.parametrize("wall", [3, 4, 5, 10])
    def test_a_very_short_lifetime_still_produces_an_ordered_pair(self, wall):
        leads = compute_wall_leads(wall)
        assert 0 < leads.soft_at < leads.hard_at < wall

    @pytest.mark.parametrize("wall", [0, -1, MAX_LIFETIME_SECONDS + 1])
    def test_a_lifetime_outside_the_platform_bound_is_refused(self, wall):
        with pytest.raises(ValueError):
            compute_wall_leads(wall)

    def test_the_deadline_is_the_start_plus_the_lifetime(self):
        leads = compute_wall_leads(900)
        assert wall_deadline(1000.0, leads) == 1900.0

    def test_the_backstop_reads_the_soft_edge_not_the_hard_one(self):
        """Waiting for the hard edge leaves twenty minutes for a pack to finish."""
        leads = compute_wall_leads(MAX_LIFETIME_SECONDS)
        start = 1_000_000.0
        assert not soft_pack_due(start, leads, now=start + leads.soft_at - 1)
        assert soft_pack_due(start, leads, now=start + leads.soft_at)
        assert soft_pack_due(start, leads, now=start + leads.hard_at)


def _payload(**overrides) -> RunHookPayload:
    base = dict(
        tag="kc-a1b2c3",
        activation_id="0d4a4a6a-1111-2222-3333-444455556666",
        activation_code="abcdefghijklmnopqrstuvwx",
        region="us-east-1",
        control_secret_ref="kirocrew/crew/kc-a1b2c3/CONTROL_SECRET",
        identity_secret_ref="kirocrew/identity/demo-crew",
        archive_bucket="kirocrew-microvm-archive-123456789012-us-east-1",
        archive_key="crews/kc-a1b2c3/home.tar.gz",
        archive_etag='"d41d8cd98f00b204e9800998ecf8427e"',
        archive_restore=True,
        kms_key_id="arn:aws:kms:us-east-1:123456789012:key/" "11111111-2222-3333-4444-555555555555",
        wall=compute_wall_leads(MAX_LIFETIME_SECONDS),
        generation=3,
    )
    base.update(overrides)
    return RunHookPayload(**base)  # type: ignore[arg-type]


class TestPayload:
    def test_a_worst_case_payload_fits_the_budget(self):
        """4,096 is the API reference's constraint and the number to budget against."""
        encoded = _payload().encode()
        assert len(encoded.encode("utf-8")) < MAX_RUN_HOOK_PAYLOAD_BYTES

    def test_an_oversized_payload_is_refused_before_the_launch(self):
        """A payload refused at RunMicrovm fails a launch that already minted one."""
        with pytest.raises(ValueError, match="over the"):
            _payload(archive_key="crews/" + "x" * 5000 + "/home.tar.gz").encode()

    def test_the_payload_round_trips(self):
        data = decode_payload(_payload().encode())
        assert data["tag"] == "kc-a1b2c3"
        assert data["gen"] == 3
        assert data["ssm"]["id"].startswith("0d4a4a6a")
        assert data["archive"]["etag"].startswith('"d41d8cd9')

    def test_an_unversioned_document_is_refused(self):
        with pytest.raises(ValueError, match="version-1"):
            decode_payload('{"tag": "a"}')

    def test_both_wall_edges_travel_to_the_guest(self):
        """The guest arms its own watchdog, so a laptop that sleeps loses nothing."""
        wall = decode_payload(_payload().encode())["wall"]
        assert wall["softAt"] == WALL_SOFT_SECONDS
        assert wall["hardAt"] == WALL_HARD_SECONDS
        assert wall["secs"] == MAX_LIFETIME_SECONDS

    def test_the_payload_carries_a_reference_and_never_a_secret_value(self):
        """The payload is an API argument and may persist in CloudTrail history."""
        data = decode_payload(_payload().encode())
        assert data["secretRef"] == "kirocrew/crew/kc-a1b2c3/CONTROL_SECRET"
        flat = _payload().encode()
        assert "CONTROL_SECRET" in flat
        assert data["identityRef"] == "kirocrew/identity/demo-crew"
        # No field on the dataclass could hold a VALUE, which is the structural
        # half of the guarantee. Stated as a naming rule rather than a list, so a
        # field added later is covered: every secret-related field is a reference
        # and says so in its name, and one that did hold a value could not be
        # named ``_ref`` without lying.
        secret_fields = [n for n in RunHookPayload.__dataclass_fields__ if "secret" in n]
        assert secret_fields, "the guarantee is vacuous if no field matches"
        assert all(n.endswith("_ref") for n in secret_fields), secret_fields

    def test_the_embedding_model_download_is_turned_off_at_boot(self):
        """Without it the first pack archives a 639 MB file that is still being written."""
        env = decode_payload(_payload().encode())["env"]
        assert env["KIROCREW_SKIP_MODEL_DOWNLOAD"] == "1"

    def test_an_empty_etag_travels_as_empty_and_not_as_a_star(self):
        """The guest turns an empty ETag into ``If-None-Match: *`` itself."""
        data = decode_payload(_payload(archive_etag="").encode())
        assert data["archive"]["etag"] == ""
