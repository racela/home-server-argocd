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
6. Manually create and seal an Opaque Secret named
   `homepage-widget-credentials` in namespace `homepage`, using strict scope.
   Include these keys:

   | Key | Value |
   |---|---|
   | `HOMEPAGE_VAR_ARGOCD_TOKEN` | Token for the Argo CD `homepage` account |
   | `HOMEPAGE_VAR_DELUGE_PASSWORD` | Deluge Web UI password |
   | `HOMEPAGE_VAR_HOMEASSISTANT_TOKEN` | Home Assistant long-lived access token |
   | `HOMEPAGE_VAR_IMMICH_KEY` | Immich API key with `server.statistics` permission |
   | `HOMEPAGE_VAR_TAILSCALE_KEY` | Tailscale API access token |
   | `HOMEPAGE_VAR_TAILSCALE_DEVICE_ID` | Subnet router device ID (not itself a credential) |

   Save only the encrypted SealedSecret as
   `apps/config/homepage/widget-credentials.yaml`.
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

## Plex, Actual Budget and Blocky

Plex uses its built-in widget: current streams, movie count and TV count.
Add `HOMEPAGE_VAR_PLEX_TOKEN` to the existing `homepage-widget-credentials`
Secret and manually reseal the complete Secret without dropping its other
keys. Use a Plex API token (`X-Plex-Token`), not the short-lived claim token
used to register the server. After the sealed update syncs, restart Homepage.

Actual's official API is the `@actual-app/api` JavaScript SDK, not a REST
budget endpoint. `homepage-metrics` uses SDK 26.10.0 to download a temporary
local copy and read the current Manila month's budget every five minutes.
It does not call budget/transaction mutation or bank-sync methods. The
available amount sums positive expense-category balances, including rollover;
overspent sums the absolute negative balances. Income is excluded. Hidden
expense categories are included so hiding a category cannot hide a deficit.
These are available category funds, not “to budget” or account balances.

Currency defaults to PHP; change `ACTUAL_CURRENCY` in metrics-deployment.yaml
if the budget uses another currency. Actual stores amounts in hundredths,
which are divided by 100 before currency formatting. At month rollover,
old-month results are not returned. Failed or stale updates return an error,
never a misleading zero. Only the month and formatted aggregate amounts are
served; no transactions, category names or account balances are exposed.

Manually seal a **separate** Secret named `homepage-actual-credentials` in
namespace `homepage`, containing:

| Key | Value |
|---|---|
| `ACTUAL_SERVER_PASSWORD` | Password used to connect to the Actual server |
| `ACTUAL_SYNC_ID` | Budget Settings → Show advanced settings → Sync ID |
| `ACTUAL_ENCRYPTION_PASSWORD` | Budget encryption password, only if end-to-end encryption is enabled; otherwise omit |

Save the SealedSecret as `apps/config/homepage/actual-credentials.yaml`.
After syncing it, restart `deployment/homepage-metrics` in `homepage`.
The server password grants SDK access to the budget; it is not a restricted
read-only API key. Only the metrics service receives it. Missing credentials
leave the Actual widget unavailable without stopping Blocky metrics.

The metrics service has no ingress, no service-account token, and an ingress
NetworkPolicy allowing only Homepage pods in its own namespace. Its HTTP
interface exposes only read endpoints. Budget copies are on an ephemeral
volume and deleted after each refresh; interrupted refreshes are cleaned at
the start of the next attempt. The maximum refresh runtime is two minutes.

The init container uses `npm ci` with the embedded package-lock.json, so a new
pod requires npm registry/native-addon download access. Dependencies and code
are copied into an ephemeral volume. Increment `metrics-config-version` in
the Deployment whenever the ConfigMap changes to trigger a rollout. Keep the
SDK compatible with the Actual server when upgrading. The service's readiness
probe checks the process, not upstream credentials or API availability.

Blocky is upgraded from v0.26.2 to v0.35.0 because the former does not expose
`/api/stats`. `statistics.enable: true` enables the rolling 24-hour collection.
The new headless Service exposes every Blocky pod (including not-ready pods)
so the metrics service can sum queries, blocked queries, cached responses and
errors across nodes. Percentages use summed counts; blocking is On, Off or
Mixed according to the individual instances. No blocking enable/disable API
is called. An unresponsive pod or invalid statistics produces an error rather
than a partial total. DNS-removed/deleted pods no longer contribute; the DNS
node count shows how many instances were aggregated.

Statistics are in memory: each Blocky restart resets that instance's history,
so the first 24 hours after rollout are a partial window. The 256Mi memory
limit gives collection more headroom than the previous 150Mi limit. Homepage
refreshes Blocky every 30 seconds; the metrics service caches requests for
15 seconds so the two widget rows share one aggregate snapshot.

Validate changes locally with:

```sh
node --test tests/test_homepage_metrics.cjs
```

References: [Actual SDK](https://actualbudget.org/docs/api/),
[Blocky statistics](https://0xerr0r.github.io/blocky/latest/configuration/#statistics),
[Homepage Plex widget](https://gethomepage.dev/widgets/services/plex/).
