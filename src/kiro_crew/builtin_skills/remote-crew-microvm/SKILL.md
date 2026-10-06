---
name: remote-crew-microvm
description: "Drive the AWS Lambda MicroVM remote-crew lane: check whether it is configured, hand the operator the cloud.json block when it is not, launch a crew, and read its lifecycle. Use for microvm, lambda microvm, remote crew, suspend a crew, resume a crew, cheap cloud crew, crew archive."
always: false
triggers: microvm, micro vm, lambda microvm, remote crew, cloud crew, suspend crew, resume crew, reopen crew, crew archive, cloud.json, remote provisioner, launch a crew in the cloud
inject_on_trigger: false
---
# Running a remote crew on an AWS Lambda MicroVM

A crew on this lane runs inside a MicroVM that the platform suspends when the crew
is idle and terminates at a maximum lifetime it will not extend. That is the whole
reason to choose it over the Fargate lane: a Fargate task bills for every hour it
exists, and this one bills for the hours somebody is using it.

It is also why the lane has parts the other lanes do not. Read the two numbers
below before you touch anything, because almost every surprise on this lane comes
from one of them.

**Before anything else: the lane is offered only when its block is complete.**

An incomplete `microvm` block publishes no provisioner row at all, which is the
deliberate choice: a row whose every launch is refused is worse than no row. So
the first thing to do is READ the provisioner list, not assume. If the `microvm`
row is absent, the block is incomplete and the missing field is the thing to find
-- most often `activation_role_arn`, which the AWS call that enrols the crew
refuses to run without, or `bundle_dir`, which a block that will build an image
also requires.

Completeness is judged on every request, so a correct edit shows up immediately
with no restart.

**Two numbers that decide everything.**

| | |
|---|---|
| **28,800 seconds** | the maximum a MicroVM may live, from its start. Not adjustable, nothing extends it, and it covers suspended time as well as running time. A crew parked overnight is gone by morning |
| **15 minutes** | idle with no running chat slot, after which the lane suspends the crew itself |

## First: is the lane even configured?

Do this before anything else, and do not infer it from the presence of an AWS
profile. The lane is offered only when its configuration is **complete**; an
incomplete block leaves it unregistered rather than registered and refusing.

```
GET /api/cloud/provisioners
```

Look for a row with `"id": "microvm"`. Three outcomes, and they mean different
things:

- **the row is there** — the lane is offered and configured. Note its
  `confirm_before_launch` string exactly as written; you need it verbatim to launch.
- **no `microvm` row, but `aws_ec2` is there** — this is the expected answer today.
  The lane is withheld until its guest half lands, and it is also what an
  incomplete block looks like. Do not try to tell the two apart by guessing: report
  that the lane is not available yet, and only walk through the configuration below
  if the operator asks for it knowing it will not launch.
- **the request fails** — this is not a lane problem. Report it as it is.

## The operator has to write the config; you cannot

The cloud configuration file is the **operator's** file. The product only reads it,
and the agent file-edit tool refuses it: a value in it chooses what image runs and
where the crew's home is written. So your job is to hand them something they can
paste without thinking, and then verify it took.

Say this, filling in nothing you have to guess:

> Two steps, both yours because the file is sealed against my edits.
>
> **1. Deploy the archive stack once per account and region.** The MicroVM's disk
> goes away with the VM, so the crew's home is archived to S3, and that bucket
> outlives every crew that writes to it. The lane never creates it — the one
> resource whose loss is unrecoverable is created deliberately, not as a side
> effect of a launch. The template ships with Kiro Crew as
> `kirocrew-microvm-base.yaml`; deploy it with `aws cloudformation deploy` and keep
> five outputs: `ArchiveBucketName`, `ArchiveKmsKeyArn`, `RecipeBucketName`,
> `ImageBuildRoleArn` and `HybridActivationRoleArn`.
>
> **2. Put the crew's model credential in Secrets Manager.** A crew refuses to
> serve without one, so this is a prerequisite and not a later step. Its value is
> a Kiro identity document, not an API key.
>
> The operator chooses the path and then names it in the config as
> `identity_secret_ref`. It is configured rather than derived from the crew's
> launch tag, because that tag is not theirs to choose: a launch with no tag is
> given `kc-<random>`, so a path built from it cannot exist before the launch that
> invents it, and the crew would stop at its secrets stage while the VM billed to
> its wall. Give the operator the exact path they will paste back; never pass a
> credential value through a command line, a log or a config file.
>
> **3. Add this block to `~/.kiro/crew/cloud.json`**, filling in the eight values:
>
> ```json
> {
>   "microvm": {
>     "base_image_arn": "<the AWS-managed MicroVM base image ARN>",
>     "build_role_arn": "<ImageBuildRoleArn>",
>     "recipe_bucket": "<RecipeBucketName>",
>     "bundle_dir": "<the directory packaging.build produced>",
>     "archive_bucket": "<ArchiveBucketName>",
>     "kms_key_id": "<ArchiveKmsKeyArn>",
>     "activation_role_arn": "<HybridActivationRoleArn>"
>   }
> }
> ```
>
> `bundle_dir` is the crew bundle `packaging.build` produced -- the SAME artifact the
> Fargate lane builds its crew layer from. It is required on the build path and
> ignored when `image_identifier` pins a prebuilt image.
>
> Identifiers only -- nothing in the block is a secret, and nothing in the product writes
> this file.

Then **verify rather than assume**: re-read the provisioner list. The block is
judged on every request, so a correct edit shows up immediately with no restart,
and an incomplete one still shows no `microvm` row. If the row is still absent
after they say they saved it, the block is incomplete; the most common cause is a
missing `activation_role_arn`, which the lane requires because the AWS call that
enrolls the crew refuses to run without a role; the next most common is a missing
`bundle_dir`, which a block that will build an image also requires.

**The recipe is the Fargate lane's recipe, not a second one.** If asked what goes
in the zip: `Dockerfile.crew` read from disk, plus the bundle's own files at the
zip's root -- byte-for-byte the build context `docker build` gets on that lane.
Same Dockerfile, same required members (`manifest.json`, `agent.json`, `mcp.json`,
`skills/`), same bundle digest from `packaging.build`'s own function. Do not
suggest writing a MicroVM-specific Dockerfile or bundle layout: two answers to
"what is in a crew image" drift, and the looser one ships the wrong content.

A bundle short a member is refused at preflight, by name, rather than minutes into
a build whose log the operator does not hold.

**The crew's image is built, not chosen — say this if they ask for an image id.**
`base_image_arn` is a BASE. The lane builds the crew's image from that base plus
the crew's own signed bundle, the way the Fargate lane builds a task definition
from the same two inputs. The build runs on AWS from a recipe zip; nothing is built
on anyone's machine.

The practical consequence, and the one you will be asked about: **the first launch
after the crew's bundle changes takes minutes, and the next one does not.** An
image is cached by a digest of its two inputs, so an unchanged crew reuses the
image it already has. Do not read a multi-minute first launch as a stuck one, and
do not relaunch to "clear" it — a second build costs the owner a second build.

An image can also exist and still not be launchable: a build that finished and
produced no active version is rebuilt rather than reused, because the service
refuses a launch against an image with no active version. If an operator insists a
built image is there and the lane is building anyway, that is why.

## Launching

```
POST /api/cloud/launch
{"provider_id": "microvm", "confirm_recipient": "<the confirm_before_launch string, verbatim>"}
```

`confirm_recipient` must equal the `confirm_before_launch` value the provisioner
list published, character for character. It names the image and the archive bucket
in full, deliberately rather than as a fingerprint: it exists so a person reads it
and recognises a wrong one, so **show it to the operator and let them confirm it**
rather than copying it through silently. A mismatch answers 400 and names what it
would have used.

The lane is reachable through this route and **absent from the Set-up selector**,
because no form is drawn for it yet. That is expected, not broken. If someone asks
why they cannot find it in the UI, that is the answer.

The launch runs four steps and the one that takes time is the third: the crew has
to enroll itself as a managed node before anything can reach it. Do not read a
pending launch as a stuck one for at least a couple of minutes.

Once it is up the crew is an ordinary remote instance. The Instances pane,
federated session search, and running a session's turns on the remote peer all
work with no extra step.

## Reading what a crew is doing

A crew on this lane is in one of these, and the distinctions are the point:

| State | What it means for the owner |
|---|---|
| `pending` | launching or reopening; nothing can reach it yet |
| `running` | usable |
| `suspended` | parked, cheap, resumable — **and still spending its lifetime** |
| `stopped` | terminated, home archived, reopenable |
| `terminated_unarchived` | terminated and the archive write FAILED. Work may be lost. Say so plainly |
| `resume_target_gone` | a suspended crew hit the maximum lifetime and the platform took it. **Nothing failed** — do not tell the owner to retry |
| `restore_failed` | a reopen could not lay the archive down; recoverable |
| `expired` | the archive's retention window closed and it was deleted |
| `unknown` | nobody has checked recently enough to say. This is never stored; it means the control plane has not looked, not that the crew is broken |

Two of those deserve care in how you report them:

- **`unknown` is not a failure.** It is the honest answer when the last
  observation is stale, which happens whenever the gateway's host was asleep.
  Do not offer to open a crew whose state is `unknown`; check it first.
- **`resume_target_gone` is not a retryable error.** Suspending does not pause the
  lifetime clock. The right message is that the crew was parked past a limit, not
  that something went wrong.

## Connecting to a crew

Nothing special, and that is the design. A crew on this lane registers as an
ordinary remote instance over AWS SSM, against the `mi-` managed node its guest
enrols as, so everything downstream already works: the Instances pane, **Settings
-> Remote Crew**, federated session search, and running a session's turns on the
remote peer.

So do not look for a MicroVM-specific way to reach a crew. If you can reach a
remote instance, you can reach this one; if you cannot, the problem is the
instances layer and not this lane.

One constraint that bites people: a session's remote binding happens **only at
birth**. You cannot convert an existing local session to run on a remote crew --
its transcript would stay where it is while execution moved to an empty slot on
the peer. Make a new session bound to the crew instead.

## Suspend, resume, and the archive

Suspend and resume are cheap and the lane does them on its own: no running chat
slot plus fifteen idle minutes and it suspends. You generally do not need to ask
for either.

**What idle means here is not what the platform means by it**, and this is worth
being able to explain. The platform measures idle as inbound traffic on the VM's
own endpoint. A crew reached through an SSM forward sends none of that, so by the
platform's measure a crew running a forty-minute turn is maximally idle -- and
would be suspended mid-turn. The lane therefore disables the platform's idle
policy at launch and decides for itself from the gateway's own chat slots. If
someone asks why the platform's idle settings look switched off, that is why.

What you should understand before touching the archive:

- the crew's home is archived under a **conditional write**, and a refused write
  is **never retried**. If you see a conflict reported, that means two writers
  raced for one crew's archive, which should be impossible with a single gateway.
  Treat it as a defect to report, not a thing to clear;
- the archive carries both halves of every session plus uploads and artifacts, so
  a reopened crew keeps its conversations. It is deliberately **not** what the
  portability export writes;
- a stopped crew's archive is kept **14 days**;
- a teardown stops a VM. It does **not** delete the archive — that is expiry's
  job, on its own clock.

## Tearing a crew down, and the sweeper

A teardown terminates the crew's VM and deletes the SSM activation it enrolled
with. It does **not** delete the archive -- that is expiry's job, on a 14-day
clock -- so a torn-down crew is still reopenable until then. Say that when
somebody worries they have lost work by stopping a crew.

The state a teardown leaves is `terminated_unarchived`, not `stopped`, and the
difference matters: the VM is gone and nothing archived its home on the way out,
so whatever was unsaved since the last pack is gone with the disk. `stopped` is
the state a *pack* produces, and only that state can be reopened.

Separately there is a **sweeper**, and it reports two things that cost real money
when missed: a MicroVM that no crew record names (a launch whose id nobody wrote
down, billing until its lifetime expires), and an SSM activation past its expiry
that enrolled nothing (a launch that failed after minting one).

Two properties to know before you suggest running it:

- **it is dry-run by default**, and that is deliberate. It reports a plan and
  changes nothing;
- **it refuses to act on a plan it could not fully build.** An unreadable crew
  record store looks exactly like an empty one, and against an empty one every
  running crew is an orphan -- so a sweep that cannot read its own records plans
  nothing rather than planning to destroy everything.

If a sweep reports a MicroVM as an orphan, check whether a launch is in flight
before anyone acts on it. A VM younger than five minutes is never reported for
exactly that reason, but a slow enrolment can outlast it.

## The cost guard

The lane's cost argument is the suspend, and the thing that bounds the worst case
is the VM's **maximum lifetime**. Two numbers, and only one of them is yours:

- **28,800 seconds is the platform's maximum** and it is not adjustable. Nothing
  extends it, and it counts suspended time as well as running time;
- **`wall_seconds` in the config block sets a shorter one.** Omitted takes the
  platform maximum. It is settable **downward only**.

So when someone is nervous about cost, the lever is `wall_seconds`, and the honest
framing is that it is a ceiling on the damage rather than a budget: a crew still
bills while it runs, the suspend is what makes the bill small, and the lifetime is
what stops a forgotten crew billing all day. A short `wall_seconds` is the right
answer for a trial crew and the wrong one for a working crew, because the crew is
terminated at that edge whatever it is doing -- it packs itself first, but it does
not get to continue.

## The lifecycle loop has to be running

The lane suspends idle crews and packs crews approaching their lifetime from two
scheduled entries. They are not installed for the operator. If a crew is never
suspending, or a crew was lost at its lifetime edge with no archive, check that
first — a lane with no loop is a lane with none of the two things it exists for.

## What not to do

- **Do not try to edit the cloud configuration file.** It is sealed, the refusal
  is correct, and the fix is to hand the operator the block above.
- **Do not launch to "see if it works".** A MicroVM bills from the moment it
  starts and the lifetime clock does not stop for a crew nobody wanted.
- **Do not treat a suspended crew as free.** It costs no compute and still spends
  its lifetime.
- **Do not report `terminated_unarchived` as a normal stop.** It is the one state
  that means the owner may have lost work, and it reads identically to `stopped`
  if you only look at "has a VM".
