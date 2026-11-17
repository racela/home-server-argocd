# Set up Zigbee2MQTT with Home Assistant

The `mosquitto` and `zigbee2mqtt` Argo CD applications share the `zigbee2mqtt`
namespace. The broker and the Zigbee2MQTT web interface are ClusterIP services.
Both applications store their data on Longhorn.

## 1. Sync and check the applications

Set the SMLIGHT to **Zigbee Coordinator** mode over **Ethernet**. Argo CD syncs
the two [SealedSecrets](../setup/config/mosquitto) with the broker application.
The Sealed Secrets controller creates the runtime Secrets using the home
cluster's sealing key. Restore that key if rebuilding the cluster.

After the manifests reach the branch tracked by Argo CD, check the broker and
then the Zigbee2MQTT pods. Zigbee2MQTT's logs should show a successful connection
to Mosquitto and startup of the Zigbee adapter.

```sh
kubectl -n zigbee2mqtt wait --for=condition=Synced --timeout=120s \
  sealedsecret/mosquitto-auth sealedsecret/zigbee2mqtt-config
kubectl -n zigbee2mqtt rollout status deployment/mosquitto
kubectl -n zigbee2mqtt rollout status statefulset/zigbee2mqtt
kubectl -n zigbee2mqtt logs statefulset/zigbee2mqtt -c zigbee2mqtt
```

## 2. Connect Home Assistant and pair a test device

In Home Assistant, go to **Settings → Devices & services → Add integration →
MQTT**. Use broker `mosquitto.zigbee2mqtt.svc.cluster.local`, port `1883`, user
`zigbee`, and the password from the `zigbee2mqtt-config` Secret. Retrieve it
in your own terminal with `kubectl` pointed at the home cluster.

```sh
kubectl -n zigbee2mqtt get secret zigbee2mqtt-config \
  -o 'jsonpath={.data.secret\.yaml}' | base64 -d | jq -r '.mqtt_password'
```

Zigbee2MQTT enables MQTT discovery.

From your workstation, open a local port forward to the Zigbee2MQTT interface.

```sh
kubectl -n zigbee2mqtt port-forward svc/zigbee2mqtt 8080:8080
```

Open `http://localhost:8080`, select **Permit join**, and reset a test bulb to
put it in pairing mode. After it joins, close joining and verify that Home
Assistant discovers the bulb and can switch it on and off. When moving the rest
of the Zigbee devices, update the affected automations and dashboard entities.
Keep the existing Tuya integration for devices still paired to its gateways.

When changing the MQTT password, reseal both Secrets with matching credentials.
Preserve the Zigbee network key to keep devices paired. Restart each workload
after the controller updates its Secret so the new pod mounts the updated file.
