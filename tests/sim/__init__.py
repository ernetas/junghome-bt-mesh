"""A simulated JUNG HOME mesh for end-to-end tests of the `jhmesh` client, on a virtual clock.

Home-Assistant-free (it imports `jhmesh` and pytest only), so the library tests (`tests/jhmesh`, which the CI
`library` job runs on its own) and the integration tests can both use it. The pieces:

- `clock`: `run(main)` on an event loop whose time jumps to the next timer (no real sleeps);
- `topology`: who hears whom, from a `docs/hop-matrix.md`-style table (`FIXTURE_HOP_MATRIX` lays the fixture
  network out as a chain, so the far end is four relays away);
- `loss`: seeded loss / duplication / reordering on the air and the GATT link, and targeted drop rules;
- `node`: a node's keys, IV state, message cache, replay list, relay, lower / upper transport (segmentation both
  ways, acknowledgements);
- `servers`: Configuration Server, Generic OnOff, Light Lightness / CTL / CTL Temperature, LBC vendor
  properties, Scenes;
- `proxy`: the GATT proxy bearer, filter and beacons, and the bleak-like client `ProxyClient.attach` takes;
- `mesh`: the whole network, network-wide procedures (IV Update, a provisioner's key refresh) and the invariants
  checked at teardown.
"""

from .clock import VirtualTimeLoop, patch_monotonic, run
from .loss import DropRule, LossModel, Packet
from .mesh import CLIENT_ADDRESS, PROVISIONER, Mesh, Provisioner, Quirks
from .node import Received, SimNode
from .proxy import ProxyNode, SimGattClient
from .topology import FIXTURE_HOP_MATRIX, Topology, parse_hop_matrix

__all__ = [
    "CLIENT_ADDRESS",
    "FIXTURE_HOP_MATRIX",
    "PROVISIONER",
    "DropRule",
    "LossModel",
    "Mesh",
    "Packet",
    "Provisioner",
    "ProxyNode",
    "Quirks",
    "Received",
    "SimGattClient",
    "SimNode",
    "Topology",
    "VirtualTimeLoop",
    "parse_hop_matrix",
    "patch_monotonic",
    "run",
]
