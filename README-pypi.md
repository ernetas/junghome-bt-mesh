# jhmesh

A Bluetooth Mesh client for JUNG HOME installations, written against the public Bluetooth SIG Mesh Profile / Mesh
Model specifications: network- and application-layer crypto (AES-CMAC / CCM / ECB, k1–k4, nonces), lower-transport
segmentation and reassembly with Segment Acknowledgement, a GATT-proxy client on top of `bleak`, the SIG and JUNG
vendor model messages (Generic OnOff / Level / CTL, Sensor, Scene, Health, Config, the vendor property models) and a
loader for the apps' Mesh CDB / share export files. It is the library behind the Home Assistant integration in the
same repository and its command-line tools.

- Repository, documentation and issue tracker: <https://github.com/ernetas/junghome-bt-mesh>
- Licence: MIT. Independent project, not affiliated with Albrecht JUNG GmbH & Co. KG; "JUNG" and "JUNG HOME" are
  used only to name the devices it interoperates with. Use it on networks you own.

```
pip install jhmesh
```

The public surface is the package's modules: `jhmesh.cdb` (parse an export, the keys and the node list),
`jhmesh.devices` (what each node is: light, socket, blind, thermostat, detector, button), `jhmesh.messages` /
`jhmesh.config_messages` / `jhmesh.vendor_models` (build and decode access messages), `jhmesh.client` (`ProxyClient`:
connect through any node's GATT proxy, send, receive, request/response with acks), `jhmesh.standalone` (the same
over a plain `bleak` scanner for scripts outside Home Assistant), `jhmesh.provisioning` (provision a new node over
PB-GATT with P-256: No OOB, or Static OOB and the HMAC-SHA256 algorithm when the device offers them) with
`jhmesh.commission` (the JUNG app's post-provisioning configuration, planned as data) and `jhmesh.onboarding`
(adding a node end to end: addresses clear of every provisioner, commissioning over the proxy link, read-back and
recording in the export), `jhmesh.vault` (a provisioner entry of your own, with ranges clear of the app's, and the
device keys of the nodes you provisioned) and `jhmesh.sniffer` (decode passive nRF Sniffer captures with the
export's keys). The typed API ships `py.typed`.

Python 3.13 or newer. Requires `cryptography` and `bleak`.
