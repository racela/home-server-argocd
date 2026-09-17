# Backup & disaster recovery

Full history of the backup/DR initiative for this home server: what's
backed up, why, the architecture decisions behind each piece, and every
real bug hit building it (with root cause and fix) — for future
reference, not just tribal knowledge in commit messages.

Cluster: 3-node Talos on Proxmox (`talos-01/02/03`, all control-plane),
GitOps via ArgoCD (this repo). Talos machine config itself lives in the
separate private [`home-server-talos`](https://github.com/racela/home-server-talos)
repo, which has its own README for that layer.

## Starting point / why this was needed

An inventory of the live cluster at the start of this effort found:

- **Longhorn** (replicated block storage) backing home-assistant,
  plex-config, deluge-config, otterwiki, immich-redis, and CNPG's
  Postgres — actual usage only **~5.8GB** total.
- A separate **NFS share** (`192.168.1.111`, a VM/LXC on the same
  Proxmox host) backing immich's photo library and Plex media —
  **154GB immich photos + 113GB Plex movies + 674GB Plex series**
  (~940GB total).
- **sealed-secrets** already deployed, but **zero `SealedSecret`
  manifests actually in git** — all 25+ live Secrets existed only in
  etcd, unrecoverable from git alone.
- Talos's own machine config (the cluster's actual PKI/identity) living
  as **unversioned plaintext on one laptop only** — flagged as the
  single biggest point of failure in the whole setup, worse than
  anything else found.

Only ~160GB (immich + Longhorn state) is genuinely irreplaceable;
~787GB of Plex media was explicitly scoped **out** (re-downloadable, not
worth backing up).

## Key architecture decisions

- **Backblaze B2** over a physical drive or AWS Glacier Deep Archive.
  B2: ~$6/TB/month, free egress up to 3x monthly storage, no minimum
  retention. Glacier Deep Archive: ~$1/TB/month but 12-48hr retrieval
  and a **180-day minimum retention charge** even on early delete —
  structurally incompatible with restic/kopia's normal prune/incremental
  read patterns. A physical drive would've needed a Proxmox passthrough
  dance and a standing backup VM for no real benefit once B2 was in the
  picture (B2 already speaks S3-compatible protocol directly).
- **Velero was considered and rejected**, differently for different
  data: CNPG needs its own DB-aware backup regardless (Velero doesn't
  replace that); for the immich NFS library specifically, Velero's
  File System Backup path needs a node-agent DaemonSet on all 3
  (schedulable) nodes, each with a **standing 512Mi memory reservation
  24/7** whether or not a backup is running — checked against real
  headroom (`kubectl top nodes`: 62-66% memory used across all 3 nodes
  already) and judged not worth it for something that only needs to run
  nightly. Plain restic CronJobs instead.
- **Scoped credentials everywhere**, even at real setup cost: a
  dedicated Talos `os:etcd:backup` role instead of `os:admin` for etcd
  snapshots (see Phase 4), a dedicated read-only Proxmox role for the
  NFS LXC vs. full-write for the Talos VMs (see Phase 5 / the Terraform
  README), least-privilege B2 application keys.
- **Retention windows converge on ~7 days** across Longhorn, CNPG, and
  restic — not because 7 is magic, but because the realistic failure
  modes here (hardware failure wants the *latest* backup regardless of
  window; slow-burn corruption is low-stakes since the actual photos
  live on NFS, not in Postgres, and there's one active user who'd notice
  quickly) don't need deep reach-back, and one consistent mental model
  beats bespoke tuning per system.

## Phase 0 — Talos config off the single machine

Migrated the 3 node configs (previously plaintext YAML with the full
cluster PKI embedded, living only on one laptop) to
[talhelper](https://github.com/budimanjojo/talhelper) + SOPS/age,
now in `home-server-talos`. Full detail — including two real
pre-existing config bugs it uncovered (a stale Longhorn mount patch, and
an install image that was supposed to have Longhorn's required
extensions baked in but never actually got applied) and the CNI-URL
mistake that got caught and fixed (commit `9adc91c`) — lives in that
repo's own README and commit history (`762b729`, `9adc91c`, `0be74ce`).

Worth calling out here since it set the tone for the rest of the
initiative: **three separate accidental secret exposures happened during
this phase** (a too-narrow `grep -vE` redaction filter, a `--dry-run`
config diff printing unchanged context lines containing a plaintext CA
key, and later in Phase 1 a `kubectl` annotation dump exposing a live
Tailscale OAuth credential). None left the local machine or got
committed, but each one changed methodology going forward — extract
fields via `yq` checking only presence/length, redirect risky output to
a file first and only display a filtered version, and query secret
**key names** only, never full objects/annotations.

## Phase 1 — Secrets sealed with SealedSecrets

**Triage:** of 25+ live Secrets, only 10 actually needed sealing —
everything else (cert-manager `-tls` secrets, CNPG-owned secrets,
ArgoCD internals, Tailscale operator runtime state) was confirmed
operator/Helm-managed via `ownerReferences`/labels, not assumed. Sealing
an operator-owned secret would fight that controller's own
reconciliation (e.g. CNPG rotating its internal CA).

**Sealed-secrets key rotation disabled before sealing anything**: since
nothing had been sealed yet, this was the moment to stop the
controller's default 30-day key rotation rather than deal with rotating
keys later. Two attempts:
1. First tried `commandArgs: [--update-status, --key-renew-period=0]` in
   the Helm values — synced fine but had **zero effect on the live pod**.
   Root cause: `commandArgs` isn't a real field on this chart at all —
   a silent no-op.
2. Fixed by using the chart's actual fields: `updateStatus: true` and
   `keyrenewperiod: "0"` (quoted string). Confirmed via the live
   Deployment's container `args` after a hard ArgoCD refresh — took
   effect (commit `d8bc787`).

The single resulting key was exported and saved to the password
manager as the whole YAML object (not just `tls.crt`/`tls.key`) — the
`sealedsecrets.bitnami.com/sealed-secrets-key: active` label is how the
controller discovers its own key, so a stripped file would need
hand-reconstruction during a real disaster. No SOPS encryption layer
added on top — unlike `talsecret.yaml` (which talhelper reads routinely,
so had a functional reason to be in git), this is a pure break-glass
artifact with no reason to be in git, and the password manager is
already an encrypted store.

**Recurring bug: "already exists and is not managed by SealedSecret"** —
hit on 9 of the 10 sealed secrets. This is sealed-secrets' own safety
behavior: it refuses to overwrite a pre-existing `Secret` object it
didn't originally create. Fix, applied consistently (and reused in every
later phase that added a secret):
```sh
# 1. hash the live secret BEFORE touching anything
kubectl get secret <name> -n <ns> -o jsonpath='{.data}' | sha256sum
# 2. back it up
kubectl get secret <name> -n <ns> -o yaml > backup-<name>.yaml
# 3. delete the unmanaged Secret
kubectl delete secret <name> -n <ns>
# 4. force a fresh reconciliation pass — annotating force-resync does NOT
#    reliably work once the controller has already exhausted retries and
#    logged "Error updating, giving up"; a full restart does:
kubectl rollout restart deployment/sealed-secrets -n kube-system
# 5. hash the recreated secret, confirm it matches step 1 exactly, and
#    confirm ownerReferences now shows the SealedSecret controller
```

**Serious credential exposure during triage**: checking whether a
Tailscale secret was auto-managed pulled its full `.metadata.annotations`,
which printed the **plaintext OAuth `client_id`/`client_secret`** via the
`kubectl.kubernetes.io/last-applied-configuration` annotation — a live
external-service credential, more serious than the Phase 0 leaks since
it could be used against a real external account. Rotation was
explicitly deferred by the user and **was never done this session** —
still an open item.

**Also fixed**: a blanket `*-secret.yaml` gitignore rule was silently
hiding 7 of the 10 sealed manifests from git (it can't distinguish an
encrypted `SealedSecret` from a plaintext `Secret` by filename) —
removed the rule (commit `03c1822`). Backed up ArgoCD's own git-repo SSH
credential (`argocd/repo-2263676092`) to the password manager as a whole
YAML file rather than sealing it — sealing and committing it here would
create a chicken-and-egg problem (a rebuilt cluster needs this secret to
clone the repo that would contain the SealedSecret providing it). Also
found and deleted an orphaned `monitoring` namespace (Grafana/prometheus
secrets with no actual running workload behind them).

**Open items from this phase**: `nintendo-museum-alerts` app stays
disabled with an inert SealedSecret; the Tailscale OAuth client still
needs rotating.

## Phase 2 — B2 bucket

Bucket: `rafa-home-server-backups`, region **us-west-004** (lowest
transpacific latency from the Philippines among the 4 offered regions;
pricing is identical across all of them, and the choice is **permanent**
— can't change region after account creation). Server-side encryption
enabled (free, no performance cost). Object Lock **not** enabled — same
structural conflict as Glacier's minimum retention: it would block
restic/kopia's own pruning.

Application key scoped to just this one bucket, with "Allow List All
Bucket Names" enabled — some S3-compatible clients (restic included)
call a list-buckets-style operation on startup even when only touching
one bucket, and can fail outright without that permission even though
actual read/write/delete access stays restricted to the single bucket.

Credential-injection pattern used everywhere downstream: the user runs
`kubectl create secret generic ... --from-literal=...` directly in their
own terminal so plaintext never passes through the assistant; it's then
read back from the live cluster and piped through `kubeseal` to produce
the committed manifest.

## Phase 3 — PVC / application data backup

### Longhorn

`BackupTarget` pointed at B2 via the native S3-compatible API
(`s3://rafa-home-server-backups@us-west-004/`), credentials as a sealed
`b2-backup-credentials` secret (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/
`AWS_ENDPOINTS`).

Final scope, after three rounds of user pushback on both which volumes
and what schedule:
- **`weekly-backup`** (retain 8, ~2 months coverage): home-assistant +
  plex-config only. Chosen over daily because these change rarely enough
  that losing a few days doesn't matter.
- Everything else excluded: `immich-redis` (pure disposable cache),
  `deluge-config` (mildly annoying to redo, not worth it),
  `otterwiki` (has its own git-remote auto-push on every edit, making a
  PVC-level backup redundant), CNPG's volume (superseded entirely by
  barman-cloud below, deliberately dropped from Longhorn's group once
  that was confirmed working — done by clearing the volume's `labels:`
  to `{}` rather than deleting the manifest, since ArgoCD prunes what it
  manages and a full delete would have destroyed the live Volume object).

Longhorn doesn't propagate PVC labels to the underlying `Volume` object
automatically — labels had to be applied directly to the dynamically
named `pvc-<uuid>` Volume resources, not the PVC. Manually-triggered
backups get no `RecurringJob` label at all, so they're invisible to a
job's retain-count pruning and just accumulate until cleared manually —
confirmed against Longhorn's actual controller behavior, not assumed
from ambiguous docs.

### CNPG (Postgres) via barman-cloud

Used the newer **barman-cloud CNPG-I plugin**, not the in-tree
`barmanObjectStore` field (deprecated since CNPG 1.26, removed in 1.30 —
the exact version already running here). Continuous WAL archiving plus
a daily `ScheduledBackup` (`immediate: true` for the first one).

**Retention went through three rounds of revision**, all driven by the
user questioning the assistant's numbers rather than accepting them:
30-day full backups (~81GB) → considered weekly-full + daily-differential
(not actually supported by barman-cloud at all — no
differential/incremental support upstream) → weekly-full (~13GB) →
final: **daily-full, 7-day retention** (~19GB), matching Longhorn's
window for one consistent mental model, since the storage difference
between the options wasn't actually the deciding factor once compared
properly.

**The UTC/timezone bug** — found by the user, affecting three schedules
at once. CNPG's cron scheduler has no timezone support; it runs in the
operator pod's own local time, which is plain UTC. Every schedule
written as "3am" (intending Philippine time, UTC+8) was actually firing
at **11am Philippine time** — exactly the wrong end of the day. Fixed by
shifting all three back 8 hours to 19:00 UTC, with the weekly job's
day-of-week also shifted across midnight (Sunday 3am PHT = Saturday
19:00 UTC, not Sunday) — commit `a647beb`. Also confirmed CNPG/barman's
retention is a single cluster-wide recovery window covering the *entire*
backup catalog — unlike Longhorn's per-job retain-count model, there's
no exemption for manually-triggered backups.

Adding the plugin to the Cluster spec triggers a rolling restart to
inject the sidecar — a real, brief interruption since this is a
single-instance Postgres cluster, approved in advance by the user.

### immich NFS library (restic CronJob, not Velero)

Longhorn has no visibility into this PVC at all — it uses the separate
`nfs-csi` driver, a structural gap, not an inefficiency. Velero's CSI
snapshot path doesn't work either (NFS has no native snapshot concept),
leaving File System Backup as the only option — which is what got
rejected on resource-headroom grounds above.

Plain daily restic CronJob instead: mounts the PVC read-only, `restic
backup` to a B2-hosted repo, `restic forget --keep-daily 7 --prune`.
Trade-off made explicit up front: no Velero orchestration means restore
is a manual one-shot `Job` (`runbooks/immich-library-restore-job.yaml`,
deliberately kept **outside** every ArgoCD-synced path so `kubectl
apply`-ing it never happens automatically).

Before ever running this against real data, the safety boundary was
verified **empirically**, not just from YAML: `kubectl exec`'d into the
live immich pod with the same PVC mounted, confirmed only immich's own
folders are visible under the CSI `subdir` scope (no `movies`/`series`),
and confirmed the mount is a real OS-level boundary (`cd .. && pwd`
lands in the container's own root, not a NAS sibling directory) — so
this backup is structurally incapable of ever touching the ~787GB of
Plex media on the same NFS share.

**Throughput bug**: first full backup ran at ~76-90Mbps against a
600Mbps home connection — CPU barely touched, ruling out
compression/encryption as the bottleneck. Root cause: restic's default
of only 5 concurrent connections to the backend, not enough to saturate
a fat pipe against the real latency between the Philippines and B2's
US-West region. Let the slow first run finish rather than restart and
risk wasting the already-uploaded data (one-time cost; every future run
is a tiny incremental delta), then applied `-o s3.connections=15` for
future runs (commit `7c7a9e2`).

### Otterwiki (git-remote push to GitHub, added after Phase 5)

Otterwiki stores its wiki pages as their own git repository on disk
(`/app-data/repository`), separate from its Helm chart's PVC-snapshot
concerns entirely — so instead of a Longhorn/restic job, it uses the
app's own built-in `GIT_REMOTE_PUSH_*` feature to auto-push every edit
straight to a dedicated private GitHub repo (`racela/otterwiki`),
authenticated with a repo-scoped SSH deploy key (write access, nothing
else).

The private key couldn't go through the Helm chart's own `values:`
without landing in plaintext in this repo's git history — the chart's
`config` map only treats `SECRET_KEY`/`MAIL_PASSWORD` as secret-worthy,
and its `env` map only accepts literal values, no `secretKeyRef`. Fix:
the chart's Deployment template already unconditionally wires in
`envFrom: secretRef: {name: otterwiki-config, optional: true}`,
regardless of whether Helm ever creates anything by that name. So the
key was supplied out-of-band instead, as an ordinary `SealedSecret`
(`apps/config/otterwiki/otterwiki-config.yaml`, synced by its own
`otterwiki-config` companion Application) — same naming convention as
`immich-config`/`longhorn-config` elsewhere in this repo. The chart
itself was never modified.

**Known gap, accepted as-is**: only `/app-data/repository` (the wiki
pages) gets pushed — `/app-data/db.sqlite` (user accounts) and
`settings.cfg` are not. This is a single local user with no account
created (edits are anonymous, commits show `Anonymous`/`www-data` as
author), which is a deliberate choice, not a bug — if the PVC were ever
lost, every page would be recoverable from GitHub with no further setup
needed.

## Phase 4 — etcd snapshot automation

**The core decision**: automating `talosctl etcd snapshot` needs a
Talos-level credential with real node/OS access — a different risk class
than every prior namespace-scoped K8s credential. Rather than accept
"the cluster is local-only" as sufficient justification for a
broad credential, used Talos's own purpose-built `os:etcd:backup` role —
one of four predefined Talos roles, and the only one that grants
literally nothing except the etcd-snapshot RPC (`os:admin` = full
access, `os:operator` = read + reboot/shutdown/etcd-backup, `os:reader`
= read-only, `os:etcd:backup` = just the snapshot method).

Verified as genuinely restricted, not just cosmetically, with **both a
positive and a negative test** against the live cluster using the
generated credential:
```
talosctl -n <ip> etcd snapshot ...   → succeeds
talosctl -n <ip> containers          → PermissionDenied (correctly denied)
talosctl -n <ip> get mc              → PermissionDenied (correctly denied)
```

Stored as a mounted config-file secret (`talosctl` needs an actual YAML
file, not key-value literals), reusing the **same restic repo password**
already used for the immich backup (confirmed safe via hash comparison
before sealing — independent restic repos stay cryptographically
independent even sharing a passphrase).

**Three real bugs, in order, each blocking the next from being visible:**

1. **Wrong container invocation** —
   `exec: "talosctl": executable file not found in $PATH`. The image has
   no shell at all to debug interactively; queried the ghcr.io registry
   API directly for the image's actual `Entrypoint` (`["/talosctl"]`,
   never on `$PATH` under the bare name). Fix: stop overriding
   `command:`, just pass `args:` (commit `210fd0d`).
2. **Fragile "does the repo exist" check** —
   `repository master key and config already initialized` on the second
   test run. `restic snapshots || restic init` assumed any failure of
   the first command means "repo doesn't exist," but a previous run
   killed mid-operation had left a stale lock file — `restic snapshots`
   was failing because the repo was *locked*, not missing, so the script
   wrongly fell through to `init`. Fixed by making both steps
   unconditional and idempotent instead: `restic unlock --remove-all ||
   true` then `restic init || true` — applied to all three restic jobs
   in the cluster even though only this one had actually hit it
   (commit `5593c82`).
3. **Restic's cache directory unwritable** —
   `unable to open cache: mkdir /.cache: permission denied`. The
   hardened pod `securityContext` (`runAsUser: 65534`, "nobody") has no
   writable home directory, and restic defaults to caching at
   `$HOME/.cache`. Fixed with `RESTIC_CACHE_DIR=/tmp/restic-cache`
   (commit `f24d5f2`). The immich-library job never hit this since it
   doesn't run under the same restrictive securityContext.

Final confirmed end-to-end: a real completed snapshot, correct 7-daily
retention policy applied, genuine restic `--json` progress output.

## Phase 5 — Terraform for Proxmox

Tracks the 3 Talos VMs and the NFS LXC container as Terraform resources
(imported, not created) — dedicated Proxmox user/token, a full-write
role scoped to the Talos VMs' pool and a **read-only** role for the NFS
container's pool (the one place with genuinely irreplaceable-ish data),
plus a documented break-glass escalation procedure for the rare case
Terraform actually needs to rebuild that container.

Full detail — the permission model, every command, and a real
multi-hour debugging arc around a Proxmox token-permission-intersection
bug — lives in
[`home-server-talos/terraform/README.md`](https://github.com/racela/home-server-talos/blob/master/terraform/README.md),
since the project itself lives in that repo. State backend is the same
B2 bucket used everywhere else in this doc. Current status: all 4
resources imported with zero drift.

## Phase 6 — restore testing

**Not started.** Explicitly flagged, more than once, as higher real
value than Phase 5 — an unverified backup is just a hope, and Phase 4
alone surfaced three real bugs that only became visible once the job
was actually *run*, not just configured. Deferred by the user for a
later session.

## Open / parked items

- **Tailscale OAuth client rotation** — deferred since the accidental
  exposure in Phase 1, never completed.
- **`nintendo-museum-alerts`** — Application stays disabled, its
  SealedSecret is inert.
- **No IPv6 route on the cluster** — discovered while installing the
  CNPG barman-cloud plugin (a Helm chart pull timed out on first,
  uncached fetch; likely a happy-eyeballs race losing to a dead IPv6
  path against dual-stack hosts). Non-blocking, never fixed.
- **Phase 6, restore testing** — see above.

## Key file locations

- `home-server-talos/` — `talconfig.yaml`, `talsecret.yaml` (Phase 0),
  `terraform/` (Phase 5, own README).
- This repo:
  - `setup/argocd-apps/sealed-secrets/application.yaml` — key rotation
    config (Phase 1).
  - `setup/config/longhorn/` — `b2-backup-credentials`, `backup-target`,
    `recurring-job-weekly` (Phase 3).
  - `apps/config/cloudnative-pg/` — `barman-cloud-credentials`,
    `objectstore`, `scheduled-backup`, `cluster.yaml` (Phase 3).
  - `apps/config/immich/` — `restic-credentials`,
    `library-backup-cronjob` (Phase 3).
  - `runbooks/immich-library-restore-job.yaml` — manual restore Job,
    deliberately outside ArgoCD's sync scope (Phase 3).
  - `apps/config/otterwiki/otterwiki-config.yaml` — `SealedSecret` with
    the git-remote push URL/key, paired with the `otterwiki-config`
    Application in `apps/argocd-apps/otterwiki/application.yaml`
    (Phase 3, added after Phase 5).
  - `setup/config/etcd-backup/` — `cronjob`, `talosconfig`,
    `restic-credentials` (Phase 4).
