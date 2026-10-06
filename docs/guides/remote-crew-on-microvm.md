# Setting up Remote Crew on an AWS Lambda MicroVM

You run a crew inside an AWS Lambda MicroVM instead of on an EC2 box you keep
alive or a Fargate task that bills while you sleep. The platform suspends the VM
when the crew is idle and terminates it at a maximum lifetime it will not extend,
so the lane is built around two things the other lanes do not have: a lifecycle
loop that suspends and resumes, and an S3 archive that is the crew's continuity
across a VM it is going to lose.

**Read this before you set anything up: the stack below takes a deliberate act.**

Nothing in the product creates a bucket, a key or a role for you, and nothing
writes `cloud.json`. The resources that outlive every crew -- the archive bucket
and its key -- are created once, by you, so that losing them is never something a
launch can do.

The lane is offered as soon as its block is complete. An incomplete block
publishes no provisioner row, which is deliberate: a row whose every launch is
refused is worse than no row, so a half-finished edit leaves the lane absent from
`GET /api/cloud/provisioners` rather than present and failing.

## The `microvm` block

Add a top-level `"microvm"` object to `~/.kiro/crew/cloud.json`.

| Field | Required | Value |
|---|---|---|
| `base_image_arn` | yes | the AWS-managed MicroVM **base** image your crew's image is built on. Not a finished image: see below. List what your region offers with `aws lambda-microvms list-managed-microvm-images`, and paste the `imageArn` verbatim -- the account field is the literal `aws` and the separator before the name is a colon |
| `build_role_arn` | yes | `ImageBuildRoleArn` from the stack above. The role Lambda assumes to run the build |
| `recipe_bucket` | yes | `RecipeBucketName` from the stack above. Where the recipe zip goes for the build role to read |
| `bundle_dir` | yes, on the build path | the crew bundle `packaging.build` produced. The **same** directory the Fargate lane builds its crew layer from; this lane zips it instead of running `docker build` over it. Ignored when you pin a prebuilt image, which is the only case with nothing to build |
| `image_identifier`, `image_version` | no | a **prebuilt** image to launch instead of building one, if you manage images yourself. Both or neither: an identifier with no version means "newest at call time", so a build landing mid-launch would retarget your launch onto an image nobody chose |
| `archive_bucket` | yes | `ArchiveBucketName` from the stack above |
| `kms_key_id` | yes | `ArchiveKmsKeyArn` from the stack above. Required, because the bucket policy denies a put that does not name this key — an omitted key is a 403 at your first pack, not a default-encrypted object |
| `activation_role_arn` | yes | `HybridActivationRoleArn` from the stack above. Required: `ssm:CreateActivation` refuses the call without a role, so a block that omits it is a lane whose every launch fails at its first AWS call. Either the ARN or the bare role name works — the lane reduces an ARN to the name the API wants, because `iamRole`'s own pattern has no colon in it |
| `identity_secret_ref` | yes | the Secrets Manager name or ARN of the crew's **model credential**, which you create yourself. Configured rather than derived: a launch with no tag is given `kc-<random>`, so a path built from the tag cannot exist before the launch that invents it, and the crew would stop at its secrets stage while the VM billed to its wall |
| `secret_path_prefix` | no | Secrets Manager path prefix for per-crew control secrets. Defaults to `kirocrew/crew` |
| `wall_seconds` | no | the VM lifetime to ask for. Omitted takes the platform maximum. Settable **downward only**; the maximum is not adjustable |
| `execution_role_arn` | with `log_group`, yes | `ExecutionRoleArn` from the stack above — the role the platform runs the VM as. `run-microvm` refuses `--logging` without it, so a block that sets `log_group` and omits this gets no guest logs at all |
| `log_group` | no, but take it | the CloudWatch log group the GUEST's own output goes to. A different setting from the image's, which receives the BUILD's output. Without it a guest that dies before its SSM node registers is completely dark: the control plane sees an activation with zero registrations and a VM in `RUNNING`, and a MicroVM has no other channel in |

```json
{
  "profile": "my-dev-profile",
  "region": "us-east-1",
  "microvm": {
    "base_image_arn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
    "build_role_arn": "arn:aws:iam::123456789012:role/kirocrew-microvm-build",
    "recipe_bucket": "kirocrew-microvm-recipes-123456789012-us-east-1",
    "bundle_dir": "/home/you/.kiro/crew/bundles/my-crew",
    "archive_bucket": "kirocrew-microvm-archive-123456789012-us-east-1",
    "kms_key_id": "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555",
    "activation_role_arn": "arn:aws:iam::123456789012:role/kirocrew-microvm-crew"
  }
}
```

### Your crew's image is built, not chosen

This is the part that surprises people, and it is the same shape the Fargate lane
already has. You do not supply a finished image. You supply a **base**, and the
lane builds your crew's image from that base plus your crew's own signed bundle —
the two inputs the Fargate lane builds a task definition from. The build runs on
Lambda from a recipe zip; nothing is built on your machine and nothing is pushed
from it.

An image is **cached by its inputs**: its name is a digest of the base and the
bundle, so a relaunch with the same two reuses the image you already have, and a
changed bundle builds a new one. That matters because a build is minutes and a
charge on your account, while a cache hit costs neither.

Two things worth knowing when a launch is slow or refuses:

- **the first launch after a bundle change builds an image**, so it takes minutes
  rather than seconds. The next one does not;
- **an image can exist and still not be launchable.** A build that finished and
  produced no active version is rebuilt rather than reused, because the service
  refuses a launch against an image with no active version.

If you manage images yourself, set `image_identifier` and `image_version` and the
lane launches that instead, skipping the build entirely.

**The recipe is the Fargate lane's recipe.** The zip this lane uploads is
`Dockerfile.crew` plus your bundle's own files at the zip's root, which is
byte-for-byte the build context `docker build` gets when the Fargate lane builds a
crew layer. The same Dockerfile, read from disk rather than copied; the same
required bundle members (`manifest.json`, `agent.json`, `mcp.json`, `skills/`); the
same bundle digest, from `packaging.build`'s own function. So a change to the crew
layer reaches both lanes at once, and this lane cannot end up building last
month's.

Because the build runs on Lambda rather than on your machine, a bundle short a
member would otherwise cost minutes and fail in a log you do not hold. The lane
reads the layout at preflight instead and names what is missing.

**Every crew route on this lane requires the per-crew secret**, and the image is
what says so. `SMC_REQUIRE_AUTH_ALL_ROUTES=1` is baked into the MicroVM layer
rather than passed at launch: a value supplied per launch is one a caller can
omit, and this lane has no security group to fall back on. The VM's HTTPS
endpoint is reachable from the internet and the ingress connector does not govern
it -- the only control the platform enforces there is a port list -- so the
container authenticates for itself. The front also binds loopback and sits off
the hook port, so the endpoint reaches the platform's hooks and nothing else.

Fargate is unchanged: that lane's flag is absent, which keeps the posture its
private subnet and zero-ingress security group already assert.

**Identifiers only, never a secret value.** The per-crew control secret is named
by its path here and minted into Secrets Manager at launch; the run payload the
platform hands the guest carries only its ARN. That is not tidiness: the payload
is an argument to `RunMicrovm`, and AWS does not document whether it is marked
sensitive, so it may sit in your account's CloudTrail request history.

## Launching

The lane publishes a provisioner row as soon as its block is complete, so this
call works.

```bash
curl -X POST "$KIROCREW_URL/api/cloud/launch" \
  -H "Content-Type: application/json" \
  -d '{"provider_id": "microvm",
       "confirm_recipient": "<the string the provisioner list showed you>"}'
```

`confirm_recipient` must equal, exactly, the `confirm_before_launch` string that
`GET /api/cloud/provisioners` publishes for this lane. It names the image base,
the crew bundle and the archive bucket in full, deliberately rather than as a
fingerprint: a value you cannot read is one you cannot refuse.

### Stage the wheel once

The base recipe installs Kiro Crew from a wheel in the build context, and a
launch reads that wheel rather than building one -- building inside a launch would
let the launch decide which version of the product your crew runs. Stage it once
from a checkout:

```bash
python scripts/build_microvm_image_zip.py --bundle <your bundle dir> --out /tmp/recipe.zip
```

That writes the zip too, which is useful for inspecting what a build will
receive, but the part a launch needs is the wheel it leaves in
`runtime/vendor/`. A launch that finds none refuses and says so.

The four launch steps are the shared ones every lane uses, relabelled for what
actually happens here. Nothing is installed at launch, and the step that takes the
time is waiting for the guest to enrol itself as a managed node.

Once a crew is up it is an ordinary remote instance: it registers with
`connection_method="ssm"` against its own `mi-` managed node, and the Instances
pane, federated session search, the fenced proxy route and running a session's
turns on the remote peer all work with no change.

## The lifecycle, and the two cron entries

The lane needs a loop, and the loop is two separate cron entries:

| Entry | Schedule | What it does |
|---|---|---|
| `microvm-lifecycle-tick` | every minute | poll each crew, suspend the idle ones, pack the ones near their wall |
| `microvm-sweep` | every 15 minutes | report MicroVMs and SSM activations no crew record names. **Dry run by default** |

They are separate on purpose. The sweep walks account-wide paginated lists, and a
bug in that walk must not be able to take the tick down with it — the tick is what
keeps a crew's work safe, and the sweep is what keeps the bill honest.

**Idle is measured from your own chat slots, not from the VM's traffic.** The
platform measures idle as inbound traffic on the VM's proxy endpoint, and a crew
you reach through an SSM port-forward sends none of it — so a crew running a
forty-minute turn is maximally idle by that measure. The lane therefore **disables
the platform's idle policy at launch** and decides for itself: no running slot, and
nothing active for fifteen minutes.

**The wall is hard and it covers suspended time.** The maximum lifetime runs from
the VM's start whatever state it is in, nothing extends it, and at the bound the
platform terminates the VM and the disk goes with it. So a crew parked overnight is
gone in the morning — which the lane reports as its own state,
`resume_target_gone`, rather than as a failure, because nothing failed.

The guest arms its own pack timer from the run payload, at seven hours thirty with
a hard edge at seven hours forty. That is not belt-and-braces: your gateway is on a
machine that sleeps, and a wall timer that lived only there would lose a crew every
time you closed the lid. The gateway's own check is the backstop for a guest too
old to have armed one.

## The archive

Pack is a tar of the crew home written to S3 under a conditional write. The rule,
once:

- the **first** pack of a crew's life sends `If-None-Match: *`;
- every later pack sends `If-Match <the ETag the previous pack returned>`;
- a `412` is **never** retried, and never retried after re-reading the current
  ETag. That would be last-write-wins wearing a conditional write's clothes.

With one gateway a 412 should be impossible, so if you see one it is a bug report
rather than something to clear. The crew is left running with its home intact.

What the archive contains is **both halves of every session**, plus
`session_map.json` — the join between them — plus `uploads/`, `artifacts/`, memory
and its SQLite sidecars, skills, crons, lessons and settings. That is deliberately
**not** the set the portability export writes: export excludes transcripts,
`uploads/` and `session_map.json` on purpose, and for this lane those three *are*
the crew. A reopened crew with its settings and none of its conversations is a new
crew wearing the old one's name.

What it leaves out: the listener credentials and the dashboard socket (a restored
crew holds its own, not the packed generation's), and the embedding model — 639 MB
against a 54 KB archive without it. The crew boots with
`KIROCREW_SKIP_MODEL_DOWNLOAD=1` so it neither archives the model nor re-fetches
it.

A stopped crew's archive is kept **14 days**. Your gateway expires it on that
clock, and the bucket's own lifecycle rule is the backstop for an install that is
never started again.

## Security

The VM's HTTPS endpoint is **always reachable from the internet**, and launching
with the `NO_INGRESS` connector does not close it — that connector governs network
ingress, not the platform's own endpoint. The one control the service genuinely
enforces there is the port list on a minted auth token, and a holder of a correctly
scoped token is still an arbitrary internet caller to your crew.

So on this lane **every route the crew serves authenticates its caller.** The two
platform lifecycle hooks that cannot carry a header answer a fixed reply that reads
and discloses nothing. Nothing the guest serves is reachable unauthenticated
through the VM endpoint.

The MicroVM is the isolation boundary for the crew's own work, which is why the
guest runs with `KIROCREW_ALLOW_UNSANDBOXED=1`: there is a Firecracker boundary
around the whole machine rather than a user namespace around one process.

## Testing it without an AWS account

The lane has a three-layer local harness, and it is the thing to run before you
change any of this:

```bash
python -m pip install 'moto[server]==5.2.3'
PYTHONPATH=. python -m test.microvm_harness.e2e --out ./microvm-e2e.json
```

It runs the full cycle — launch, a turn's API path, a marker file, suspend, resume,
pack, terminate, restore, the marker surviving, terminate — against moto for S3, a
loopback fake for the MicroVM control plane, and `docker` running the published
crew image as the guest. Every MicroVM call goes through the lane's own code with
`--endpoint-url` pointed at the fake, so what it exercises is the engine.

**What it cannot prove, and must not be read as proving:** real provisioning
latency or capacity pressure, the real maximum lifetime, the platform's own
auto-suspend and auto-resume trigger (`docker pause` is operator-driven, so the
*handling* is tested and the *trigger* is not), the public HTTPS endpoint or its
TLS or its connectors, real auth-token semantics, the MicroVM image build, SSM
activations and Session Manager at all — and, because a container cannot enforce
them, nothing about the agent sandbox, cgroup ceilings or user namespaces. The crew
gateway logs that last one itself on every start inside a container.

## Known gaps

- No wizard step, no dashboard form, no `cloud` CLI verb. Hand-edit the block.
- No SPA renderer claims the `microvm` kind, so the lane is API-reachable and
  absent from the Set-up selector. The Remote Crew panel does show a chat pane for
  a running crew on this lane, which is a different surface from the launcher.
- A refused conditional write on the archive has no operator-visible destination
  yet. It is logged as an error and the crew is left running with its home intact,
  which is the safe half; the report an owner should see is not built.
- The frontend does not send `confirm_recipient` on any lane, so a dashboard launch
  of a lane that publishes a confirmation is not wired yet. Launch through the API.
- The cron entries are documented here and added through the normal cron surface;
  the lane does not install them for you.
- Per-crew IAM roles are not created. One lane role serves every crew, which is
  containment from the account and not between crews — the right shape for a
  single owner, and the thing to revisit the day this lane serves a roster.
