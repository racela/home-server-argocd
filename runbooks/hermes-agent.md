# Hermes Agent

Argo CD discovers `hermes-agent` on the default branch. It runs the
[official Nous Research image](https://hub.docker.com/r/nousresearch/hermes-agent)
pinned by release and digest. One gateway owns the SQLite database and OAuth
state on the Longhorn volume. The managed Webapp config lives in Git. Hermes
keeps its writable user config, conversations and OAuth state on the volume.

## Link your ChatGPT plan

1. Open [Nous Portal → Linked accounts](https://portal.nousresearch.com/help/chatgpt-plan-sharing).
   Connect your ChatGPT account, approve plan sharing, and confirm that
   **Plan sharing** is on for your personal account. Eligible inference can
   use your existing plan without a paid Nous subscription. Choose a model
   covered by ChatGPT plan sharing in the Portal catalog.
2. From a terminal with access to the cluster, check the rollout and run the
   model wizard.

   ```sh
   kubectl -n hermes-agent rollout status deployment/hermes-agent --timeout=300s
   kubectl -n hermes-agent exec -it deployment/hermes-agent -- hermes model
   ```

   Select **Nous Portal**, open the device sign-in link in your browser,
   and sign in to the same Nous account you linked in step 1. Select the
   eligible model you identified in the catalog. If offered, decline the
   Nous Tool Gateway to keep this setup on plan-covered inference. Hosted
   tools and models outside plan sharing can use separate Nous credits.

3. Open the Webapp using two terminals. Keep the first command running while
   you use Hermes. Open the one-use URL printed by the second command
   within 60 seconds and keep it private.

   ```sh
   # Terminal 1
   kubectl -n hermes-agent port-forward --address 127.0.0.1 deployment/hermes-agent 9120:9120
   # Terminal 2
   kubectl -n hermes-agent exec deployment/hermes-agent -- hermes webapp --no-open
   ```

   Start a new chat and ask it to reply with `Hermes is working`. Check the
   request in [Nous Portal](https://portal.nousresearch.com/) to verify plan
   sharing. A new browser session may need a fresh sign-in link.

## Operations

The Webapp listens on pod loopback. Kubernetes port forwarding requires
cluster access. The pod has no Kubernetes API token, and the NetworkPolicy
allows DNS and outbound HTTPS to public IPv4 addresses.

Update the release tag and digest together in `deployment.yaml`. Argo CD
recreates the pod on updates and retains the claim and namespace on prune.
The volume contains OAuth credentials as well as chats and memory. Treat
exports and backups of the volume as sensitive. The PVC is outside the
weekly off-cluster backup group.

If ChatGPT plan sharing expires, use **Reconnect ChatGPT** in Nous Portal.
If Hermes loses its Nous session, rerun the model wizard inside the pod.
