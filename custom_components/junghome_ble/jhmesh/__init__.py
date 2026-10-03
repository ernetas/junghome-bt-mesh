"""Bluetooth Mesh (GATT proxy) client for JUNG HOME installations: crypto, PDUs, messages, device model.

The package imports nothing itself: `import jhmesh` costs no `bleak` or `cryptography` work, and each module is
imported by name (`from jhmesh.client import ProxyClient`).

Public API: the names in each module's `__all__`. A name starting with an underscore is private, whatever module it is
in, and may change or go in any release; so may a module-level name `__all__` leaves out (an import a module uses
itself, its logger). The modules:

- `cdb` (an export: keys, nodes, elements), `devices` (what each node is), `properties` (the JUNG device properties),
  `advert` (a node's advertisement, without keys);
- `messages`, `config_messages`, `vendor_models` (build and decode access messages);
- `client` (`ProxyClient` over any GATT proxy link), `state` (`LocalState`: our address, sequence numbers, IV index,
  replay list — `client` re-exports it), `standalone` (`client` over a plain `bleak` adapter);
- `provisioning`, `commission`, `onboarding` (add a node), `vault`, `vaultrefresh`, `keyrefresh` (a provisioner of
  your own, its keys, key refresh), `export`, `merge` (write and merge the app's files), `audit` (check the nodes'
  configuration against the export), `sniffer` (decode captures);
- `pdu`, `crypto`, `fileio`: the layers below, public for tools that need them.
"""

__all__: list[str] = []
