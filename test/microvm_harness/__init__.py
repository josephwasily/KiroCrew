"""The three-layer local harness for the MicroVM lane. No AWS account, no credential.

The lane has three halves that fail in different ways, so the harness has three
layers and each one is the cheapest thing that can exercise its half honestly:

**Layer 1 -- the AWS data plane: moto on ``127.0.0.1``.** S3 with its conditional
writes, which is the one thing the archive's whole safety argument rests on and
the one thing a hand-written double cannot prove. Server mode rather than the
in-process decorator, because the guest and the control plane are different
processes here and a decorator patches only its own.

**Layer 2 -- the guest: docker running the published crew image.**
:mod:`test.microvm_harness.local_engine`. ``docker run`` / ``pause`` /
``unpause`` / ``rm -f`` against the same image the real lane runs, so the archive
round trip, the marker file and the suspend are real rather than simulated.

**Layer 3 -- the control plane: a loopback fake.**
:mod:`test.microvm_harness.fake_microvm_endpoint`. moto has no MicroVM backend, so
this is the only layer that has to be written -- and the shapes it serves are
pinned against botocore's installed model by
:mod:`test.test_cloud_microvm_contract`, not invented here.

**What the harness cannot prove, and must never be read as proving.** The ceiling
is in ``R2``'s own words and it is not a formality: no real provisioning latency or
capacity pressure, no real eight-hour wall, no platform-driven auto-suspend or
auto-resume (``docker pause`` is operator-driven, so the HANDLING is testable and
the TRIGGER is not), no public HTTPS endpoint or TLS or network connector, no real
auth-token semantics, no MicroVM image build, no SSM activation or Session Manager
at all -- and, because a container cannot enforce them, nothing about the agent
sandbox, cgroup ceilings or user namespaces. The crew gateway logs that last one
itself on every start inside a container.
"""
