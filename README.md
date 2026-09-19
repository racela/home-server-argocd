# home-server-argocd

GitOps repository for my home Kubernetes cluster. Everything running on
the cluster - infrastructure services and applications - is declared
here and reconciled by [ArgoCD](https://argo-cd.readthedocs.io/).

The cluster itself (Talos Linux machine configs, node bootstrap, secret
rotation) lives in a separate private repo, `home-server-talos`. This
repo starts where that one ends: a working Kubernetes cluster with
ArgoCD on it.

Open items and ideas are tracked in [`TODO.txt`](TODO.txt).

## How it works

`app-of-apps.yaml` defines a single root ArgoCD `Application`
(`root-app`) that recursively watches two directories:

- `setup/argocd-apps/` - cluster infrastructure
- `apps/argocd-apps/` - user-facing applications

Each subdirectory there holds ArgoCD `Application` manifests. Most apps
come as a pair: one `Application` that installs an upstream Helm chart,
and a second `<name>-config` `Application` that applies the plain
manifests from the matching `config/` directory (ingresses, secrets,
PVs, CronJobs, custom resources). Sync waves order the rollout so that,
for example, Longhorn and cert-manager are up before the apps that need
storage and certificates. Automated sync with prune and self-heal is on,
so a merge to `master` is a deploy and manual `kubectl` changes get
reverted.

To bring up a fresh cluster, install ArgoCD and apply the root app once:

```
kubectl apply -f app-of-apps.yaml
```

ArgoCD manages its own Helm release from `setup/argocd-apps/argocd`
after that.

## Repository layout

```
app-of-apps.yaml         root Application
setup/
  argocd-apps/           Applications for cluster infrastructure
  config/                plain manifests those Applications apply
apps/
  argocd-apps/           Applications for workloads
  config/                plain manifests those Applications apply
runbooks/                recovery procedures, deliberately NOT synced
.github/workflows/       CI validation of Application manifests
renovate.json            dependency update policy
```

## What's deployed

Infrastructure (`setup/`):

| Component | Purpose |
|---|---|
| ArgoCD | GitOps controller (self-managed) |
| Longhorn | Replicated block storage, weekly backups to B2 |
| csi-driver-nfs | NFS-backed volumes for media and the Immich library |
| MetalLB | Bare-metal LoadBalancer IPs |
| ingress-nginx | HTTP(S) ingress |
| cert-manager | TLS certificates (internal CA and Let's Encrypt) |
| Sealed Secrets | Encrypted secrets committed to git |
| Blocky | Local DNS |
| metrics-server | Resource metrics |
| etcd-backup | Nightly etcd snapshot to B2 |

Applications (`apps/`):

| App | Notes |
|---|---|
| Immich | Photos; library on NFS, database in CloudNativePG |
| CloudNativePG + barman-cloud plugin | Postgres cluster with continuous WAL archiving to B2 |
| Plex | Media server, config on Longhorn, media on NFS |
| Home Assistant | Home automation, config on Longhorn |
| Gatus | Status monitoring with Discord alerts |
| Otterwiki | Personal wiki |
| Tailscale operator | Remote access |

`apps/argocd-apps/nintendo-museum-alerts` is currently commented out.

## Secrets

Secrets are committed as `SealedSecret` resources, encrypted against the
cluster's Sealed Secrets controller key, so only that cluster can
decrypt them. Plain `Secret` manifests must never be committed. The
controller's private key is not in this repo - losing it means
re-sealing every secret.

## Backups

All backups go to a single Backblaze B2 bucket with versioning enabled.

| Data | Method | Schedule (UTC) | Retention |
|---|---|---|---|
| Immich database (CNPG) | barman-cloud: continuous WAL archive plus base backup | WAL continuous, base backup daily 19:00 | 7 days |
| Immich library | restic CronJob | daily 19:00 | 7 daily snapshots |
| etcd | restic CronJob | daily 19:00 | 7 daily snapshots |
| Home Assistant and Plex config | Longhorn RecurringJob | Saturdays 19:00 | 8 backups |

19:00 UTC is 3am in the Philippines. Because the Immich database and
library are backed up independently, restoring them means matching a
database recovery point to a library snapshot - see
[`runbooks/immich-disaster-recovery.md`](runbooks/immich-disaster-recovery.md).

## Runbooks

`runbooks/` holds recovery manifests and procedures. It sits outside
every Application's `path:` on purpose: a restore Job runs the moment it
is applied, so it must never be picked up by GitOps automatically. Read
the header comment in each file before applying it.

## Dependency updates and CI

[Renovate](https://docs.renovatebot.com/) opens PRs for Helm chart
versions referenced in the Application manifests, and auto-merges
patch-level updates for stable (non-0.x) charts. On every PR, a workflow
(`.github/workflows/test-renovate.yml`) takes the changed Application,
installs its chart with this repo's real values using chart-testing,
and where possible tests the upgrade from the currently deployed
version, so a broken bump fails before merge. Direct pushes to `master`
are possible for the repo owner.
