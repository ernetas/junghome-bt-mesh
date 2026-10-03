# Security

## What this integration holds

A Bluetooth Mesh network is secured by keys, and the JUNG HOME export this integration works from holds **all of
them**: the network key, the application key and every device's device key. Whoever has the export can control every
device of the mesh, reconfigure it, or take it over.

- Every file that holds key material is written atomically and readable by the owner only (mode 0600), whatever the
  umask; a file a user pointed the integration at keeps its own mode where that is stricter still:

  | File | Holds | Mode |
  |---|---|---|
  | The export: `<config>/junghome_ble/<mesh uuid>.json` (fetched or uploaded), or the file you pointed the entry at | every key: network, application, every device key | 0600 (a file of your own: never looser than 0600 once written) |
  | Its backups `<export>.bak`, `.bak.1`, `.bak.2` and `<export>.pre-adopt` | earlier versions of the export, every key | 0600 |
  | The app's last upload `<export>.app` (an entry set up from the gateway) | every key | 0600 |
  | `.storage/junghome_ble.vault.<mesh uuid>`, and any `….unreadable.<time>` copy | Home Assistant's provisioner identity, the device keys of the devices it added | 0600 |
  | `.storage/junghome_ble.seq.<mesh uuid>` and its `.backup` copy | sequence numbers; the new network key while a key refresh is followed | 0600 |
  | `.storage/junghome_ble.seq.<mesh uuid>.floor` | the `seq_store_lost` repair's sequence-number floor (no key, kept with the store) | 0600 |
  | CLI: `tools/.jhmesh_state_<ADDR>.json` and its `.bak` | the CLI's sequence numbers; the new network key while it follows a key refresh | 0600 (an older, looser one is tightened when loaded) |
  | CLI: `tools/.jhmesh_devkey_<ADDR>.json` (or `provision --key-file`) | the device key of a node `mesh_poc.py provision` added (never printed) | 0600 |
  | CLI: a file written by `mesh_poc.py export write --out` | every key | 0600 |

  The sequence-number store and the vault, with any `.unreadable.<time>` copy of a vault that did not read back, are
  kept when the entry is removed — the integration never deletes them; delete them by hand when they are no longer
  needed. Home Assistant's own `.storage` directory keeps the mode Home Assistant gives it.
  Home Assistant backups contain all of these: keep them as private as the export itself.
- Keys never appear in logs, diagnostics downloads or error messages. The `junghome_ble.export_network` action
  returns the whole export, keys included, and is limited to administrators; so are `add_device` (it hands a new
  device the network's keys) and `remove_device`.
- The gateway connection is pinned to the certificate the gateway presented at setup, re-confirmed over the mesh;
  its access token is stored in the config entry.

## Reporting a vulnerability

Please report security problems privately through GitHub's *Report a vulnerability* (Security → Advisories) on this
repository rather than in a public issue. There is no bounty; reports are answered on a best-effort basis.
