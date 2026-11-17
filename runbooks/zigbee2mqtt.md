# Set up Zigbee2MQTT with Home Assistant

The `mosquitto` and `zigbee2mqtt` Argo CD applications share the `zigbee2mqtt`
namespace. The broker and the Zigbee2MQTT web interface are ClusterIP services.
Both applications store their data on Longhorn.

## 1. Create the runtime Secrets

Set the SMLIGHT to **Zigbee Coordinator** mode over **Ethernet**. Use its
current LAN address and the port from its Zigbee2MQTT configuration. Keep the
coordinator in this mode while Zigbee2MQTT is using it.

Run this once in Bash with `kubectl` pointed at the home cluster. It requires
Docker and Python 3. Generate a strong MQTT password in your password manager
and keep it for the Home Assistant setup. The command creates the broker's
password file and a random Zigbee network key. Back up the `zigbee2mqtt-config`
Secret securely with the Zigbee2MQTT volume. Changing the network key after
pairing requires pairing the devices again.

```bash
(
  set -euo pipefail
  umask 077
  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' EXIT

  read -r -p 'Coordinator TCP URL (tcp://ADDRESS:6638): ' coordinator_url
  read -r -s -p 'MQTT password: ' mqtt_password
  printf '\n'
  if [[ "$coordinator_url" != tcp://*:* || -z "$mqtt_password" ]]; then
    printf 'Enter a coordinator TCP URL and a password.\n' >&2
    exit 1
  fi

  printf 'zigbee:%s\n' "$mqtt_password" |
    docker run --rm -i --entrypoint /bin/sh eclipse-mosquitto:2.1.2-alpine -ec '
      umask 077
      cat > /tmp/passwords
      mosquitto_passwd -U /tmp/passwords
      cat /tmp/passwords
    ' > "$workdir/passwords"

  printf '%s' "$coordinator_url" > "$workdir/serial-port"
  printf '%s' "$mqtt_password" | python3 -c '
import json
import secrets
import sys

json.dump({"mqtt_password": sys.stdin.read(), "network_key": list(secrets.token_bytes(16))}, sys.stdout)
' > "$workdir/secret.yaml"

  kubectl create namespace zigbee2mqtt --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n zigbee2mqtt create secret generic mosquitto-auth \
    --from-file=passwords="$workdir/passwords"
  kubectl -n zigbee2mqtt create secret generic zigbee2mqtt-config \
    --from-file=serial-port="$workdir/serial-port" \
    --from-file=secret.yaml="$workdir/secret.yaml"
)
```

These Secrets live in the cluster. On a retry after partial setup, use the same
MQTT password and create only the missing Secret. Preserve the existing network
key when updating the coordinator address or broker credentials.

## 2. Sync and check the applications

After the manifests reach the branch tracked by Argo CD, check the broker and
then the Zigbee2MQTT pods. Zigbee2MQTT's logs should show a successful connection
to Mosquitto and startup of the Zigbee adapter.

```sh
kubectl -n zigbee2mqtt rollout status deployment/mosquitto
kubectl -n zigbee2mqtt rollout status statefulset/zigbee2mqtt
kubectl -n zigbee2mqtt logs statefulset/zigbee2mqtt -c zigbee2mqtt
```

## 3. Connect Home Assistant and pair a test device

In Home Assistant, go to **Settings → Devices & services → Add integration →
MQTT**. Use broker `mosquitto.zigbee2mqtt.svc.cluster.local`, port `1883`, user
`zigbee`, and the password saved earlier. Zigbee2MQTT enables MQTT discovery.

From your workstation, open a local port forward to the Zigbee2MQTT interface.

```sh
kubectl -n zigbee2mqtt port-forward svc/zigbee2mqtt 8080:8080
```

Open `http://localhost:8080`, select **Permit join**, and reset a test bulb to
put it in pairing mode. After it joins, close joining and verify that Home
Assistant discovers the bulb and can switch it on and off. When moving the rest
of the Zigbee devices, update the affected automations and dashboard entities.
Keep the existing Tuya integration for devices still paired to its gateways.
