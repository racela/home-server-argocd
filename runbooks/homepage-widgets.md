# Homepage widgets

The dashboard keeps ingress discovery for Argo CD, Deluge, Gatus, Home
Assistant and Immich. Their `gethomepage.dev/widget.*` annotations configure
the widgets; credentials are substituted by Homepage from its environment.
Immich's chart evaluates annotations as Helm templates, so its placeholder is
escaped. Home Assistant's chart copies annotations literally and needs no escape.
Tailscale is a static Operations card for the home subnet router. Longhorn
is an information widget using the internal frontend API.

| Widget | Output |
|---|---|
| Argo CD | Total apps, synced, out of sync, degraded |
| Deluge | Downloading/seeding torrents, download/upload rates, download progress |
| Gatus | Up/down endpoint counts and uptime |
| Home Assistant | People home, lights on, switches on |
| Immich | Photos, videos, storage used, users; widget API version 2 |
| Tailscale | Router address, last seen, key expiry, update available |
| Longhorn | Total and per-node storage usage |
| Greeting and clock | Large welcome and 12-hour Manila clock, smaller full date, dark slate theme |

Gatus and Longhorn require no credentials in this cluster. The other five
service widgets need the credentials below; until provisioned, those widgets
will show API errors. The Secret reference is optional so the rest of the
dashboard can still start. No credentials are included in the PR.

## Provision credentials

Use dedicated dashboard credentials wherever the service supports them.
Do not reuse the Tailscale operator's OAuth secret: the Homepage widget
expects a Tailscale API access token.

1. Sync the Argo CD application after merging. This creates an API-token-only
   `homepage` account that can read applications in the `default` project.
   As an Argo CD administrator, generate its token under Settings → Accounts
   → homepage → Tokens. It has no login, sync or write permissions from the
   role added here; existing default Argo CD policies still apply.
2. Use the Deluge **Web UI** password, not the daemon's authentication file.
3. Create a Home Assistant long-lived access token in the profile of a
   dedicated non-administrator user. The widget uses the standard activity
   fields so it does not depend on any particular sensor entity IDs.
4. Create an Immich API key with only `server.statistics` permission on an
   account allowed to read server statistics.
5. Create a Tailscale API access token. Open the `local-subnet-router`
   machine's details and copy its device ID (ending in `CNTRL`). Track the
   API token's expiry separately from the device key expiry shown in Homepage.
6. From the repo root, with `kubectl` pointed at the home cluster and
   `kubeseal` installed, run:

   ```sh
   python3 runbooks/seal-homepage-widget-credentials.py
   ```

   Enter values at the hidden prompts. The helper passes plaintext only
   through memory/stdin and writes the encrypted SealedSecret to
   `apps/config/homepage/widget-credentials.yaml`. All six fields are required;
   rerun the helper with the complete set when rotating credentials.
7. Review and commit the generated SealedSecret in a PR. `homepage-config`
   reconciles it using the repository's normal GitOps process.
8. After the Secret is available, restart Homepage to load the environment:

   ```sh
   kubectl rollout restart deployment/homepage -n homepage
   kubectl rollout status deployment/homepage -n homepage
   ```

   Restart after later rotations too: updating a Secret does not update the
   environment of an already-running pod.

The token used for Argo CD is generated only after its account configuration
is synced. Never commit a plain Kubernetes Secret or credentials in ingress
annotations, Helm values, shell history, or PR text.

## Verify after sync

Open `https://homepage.rafa.local`, refresh discovery, and compare the widgets
with their source applications. Check Argo CD counts against its application
list, Deluge rates against active transfers, and Immich totals against its
administration statistics. Home Assistant activity should reflect current
states; Tailscale should identify the subnet router. Longhorn should show
aggregate storage plus each node. The clock explicitly uses `Asia/Manila`,
even when the browser uses another timezone.

These are read-only displays. API access is still governed by each supplied
credential; Deluge's Web UI password and a Home Assistant token are not
read-only credentials simply because the widget only reads data.

Widget reference: [Homepage service widgets](https://gethomepage.dev/widgets/services/)
and [information widgets](https://gethomepage.dev/widgets/info/).
