#!/usr/bin/env python3
"""Prompt locally for widget credentials and write only a namespace-bound SealedSecret.

Run from the repository root after following homepage-widgets.md. No plaintext
credential files are created, and credentials are never passed as command arguments.
"""
import getpass
import json
from pathlib import Path
import subprocess
import tempfile

FIELDS = {
    "HOMEPAGE_VAR_ARGOCD_TOKEN": "Argo CD homepage account API token",
    "HOMEPAGE_VAR_DELUGE_PASSWORD": "Deluge Web UI password",
    "HOMEPAGE_VAR_HOMEASSISTANT_TOKEN": "Home Assistant long-lived access token",
    "HOMEPAGE_VAR_IMMICH_KEY": "Immich API key (server.statistics permission)",
    "HOMEPAGE_VAR_TAILSCALE_KEY": "Tailscale API access token",
    "HOMEPAGE_VAR_TAILSCALE_DEVICE_ID": "Tailscale subnet router device ID (ends in CNTRL)",
}


def main():
    output = Path(__file__).resolve().parents[1] / "apps/config/homepage/widget-credentials.yaml"
    credentials = {}
    for key, label in FIELDS.items():
        value = getpass.getpass(f"{label}: ").strip()
        if not value:
            raise SystemExit(f"{key} must not be empty; nothing was written.")
        credentials[key] = value
    secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": "homepage-widget-credentials", "namespace": "homepage"},
        "stringData": credentials,
    }
    # kubeseal fetches the current cluster's public certificate. Strict scope
    # binds the ciphertext to both the destination namespace and Secret name.
    result = subprocess.run(
        ["kubeseal", "--controller-name", "sealed-secrets",
         "--controller-namespace", "kube-system", "--scope", "strict", "--format", "yaml"],
        input=json.dumps(secret), text=True, capture_output=True,
    )
    if result.returncode:
        raise SystemExit("kubeseal failed; nothing was written. Check the cluster context and controller access.")
    # Only encrypted output reaches disk; replace the destination atomically.
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False) as tmp:
        tmp.write(result.stdout)
        temporary = Path(tmp.name)
    temporary.replace(output)
    print(f"Wrote encrypted credentials to {output}. Review and commit this file.")


if __name__ == "__main__":
    main()
