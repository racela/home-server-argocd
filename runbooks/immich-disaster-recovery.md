# Immich disaster recovery runbook

Immich has two independent backups: the database (CloudNativePG +
barman-cloud, continuous WAL archiving to `cnpg/` in B2) and the asset
library (nightly restic backup of the whole `/data` PVC to `immich/` in
B2, `library-backup-cronjob.yaml`). Because they're independent and on
different schedules, **a real restore must pair a specific point in
time on each side, not just "restore both to latest."**

## Why the timestamps have to match

The DB backup is continuous - you can recover it to any timestamp, not
just a fixed checkpoint. The library backup is a single point-in-time
snapshot taken once a night. If you restore the DB to "latest" but the
files to last night's snapshot, the DB will have rows referencing
photos uploaded since that snapshot that don't exist in the restored
files - orphaned references, silent broken thumbnails/missing assets.
Restoring both to the exact same moment avoids this.

If you don't care about a specific moment (e.g. testing that the
mechanism works at all, not recovering from an actual incident), it's
fine to just use `latest` on both sides - the pairing only matters for
a real recovery where any mismatch would leave inconsistent data.

## Step 1 - pick the target moment

List available restic snapshots and note the exact timestamp of the
one you want:

```
kubectl run restic-list --rm -it --restart=Never -n immich \
  --image=restic/restic:0.19.1 \
  --overrides='{"spec":{"containers":[{"name":"restic-list","image":"restic/restic:0.19.1","command":["restic","snapshots"],"envFrom":[{"secretRef":{"name":"immich-restic-credentials"}}],"env":[{"name":"RESTIC_REPOSITORY","value":"s3:https://s3.us-west-004.backblazeb2.com/rafa-home-server-backups/immich/"}]}]}}'
```

Convert the snapshot's timestamp to RFC 3339 UTC - that's the value
you'll use for both the restic `SNAPSHOT_ID` and the CNPG
`recoveryTarget.targetTime`.

## Step 2 - restore the database (new Cluster, not in-place)

Edit `runbooks/immich-db-restore-pitr.yaml`: set `TARGET_TIME` to the
timestamp from step 1. Apply it - this bootstraps a **new** Cluster
(`db-cluster-restore`), it does not touch the live `db-cluster`:

```
kubectl apply -f runbooks/immich-db-restore-pitr.yaml
kubectl get cluster db-cluster-restore -n cloudnative-pg -w
```

Wait for `Ready`. Note: `managed.roles` and `shared_preload_libraries`
in that file are copied from the live `cluster.yaml` - if that spec has
changed since this runbook was written, update both to match before
applying, or the recovered cluster won't have the roles/extensions
Immich expects.

## Step 3 - restore the library files

Edit `runbooks/immich-library-restore-job.yaml`: set `SNAPSHOT_ID` to
the specific snapshot ID from step 1 (not `latest`, if you need it to
match the DB target time). By default this restores **in-place** onto
the live `immich-library-pvc` - only do that during an actual incident.
For a dry-run/verification instead, point the Job at a throwaway PVC
first (same pattern as the earlier Longhorn HA/Plex restore test - see
`LONGHORN-BACKUP-RESTORE-TEST-2026-09.md`): swap the `volumes` entry
for a new PVC on the `longhorn` StorageClass sized for a test, apply,
check the restored contents from a debug pod, then delete it. Don't
run it against the live PVC unless you mean to actually recover.

## Step 4 - verify before trusting it

- DB: `kubectl exec -n cloudnative-pg db-cluster-restore-1 -- psql -U immich -d immich -c '\dt'`
  and spot-check row counts against what you'd expect for that point in
  time.
- Files: from a debug pod mounted on wherever you restored to, confirm
  real content exists under `library/` (or `upload/`), `profile/`, and
  `thumbs/` - not just that the restore job exited 0.

## Step 5 - clean up or cut over

If this was a verification/drill: delete the throwaway Cluster and PVC
(`kubectl delete -f runbooks/immich-db-restore-pitr.yaml`, delete the
test PVC/Job) so nothing stray is left running.

If this was a real recovery and you're cutting production over to the
restored data, that needs more than this runbook covers on its own:
repointing `DB_HOSTNAME` in the immich Application's Helm values to
`db-cluster-restore-rw...`, reconciling which Cluster name is
considered live going forward (rename, or delete the old one and
re-create under the original name), and re-applying the `Database` CRD
(`immich-db.yaml`) against the new cluster. Treat that as its own
deliberate step, not something to do automatically as part of a
verification pass.
