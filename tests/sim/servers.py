"""The models of a simulated node: the Configuration Server and just enough of the application servers.

- Configuration Server (device key, primary element): AppKey Add, Composition Data Get, Model App Bind / Unbind,
  Model Publication Get / Set, Model Subscription Add / Delete / Overwrite / Delete All and the SIG / Vendor
  Subscription and App Gets, Default TTL, Relay, Network Transmit, Beacon, GATT Proxy (Get / Set each), Node
  Reset, NetKey Update and Key Refresh Phase Get / Set.
- Generic OnOff, Light Lightness, Light CTL and CTL Temperature (Get / Set / Set Unacknowledged / Status, with the
  OnOff ↔ Lightness binding and the transaction identifier of a repeated Set).
- The LBC vendor property servers (Admin 05271011, Manufacturer 05271012, User 05271013): Get / Set / Status,
  preloaded with the catalogue's properties of the node's product.
- Scene Server / Scene Setup Server: Store, Recall, Get, Register Get, Delete.

Replies go to the source of the request under the key it came with; a state change is published to the model's
publication address. `Quirks.set_reply_by_publication` is JUNG's firmware: an acknowledged Set that changes the
state is answered *only* by that publication, a unicast Status only when nothing changed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jhmesh import config_messages as C
from jhmesh import messages as M
from jhmesh import properties as P
from jhmesh.crypto import AppKeyMaterial, NetKeyMaterial
from jhmesh.pdu import encode_opcode, is_unicast

if TYPE_CHECKING:
    from jhmesh.cdb import Node

    from .node import Received, SimNode

JUNG_CID = M.JUNG_CID
TID_WINDOW = 6.0  # §3.3.2.2.3: a Set with the TID of the previous one within 6 s is the same transaction
LBC_MODELS = {"05271011": "admin", "05271012": "manufacturer", "05271013": "user"}
LBC_OPS = {  # kind -> (Get, Set, Set Unack, Status)
    "admin": (0x02, 0x03, 0x04, 0x05),
    "manufacturer": (0x08, 0x09, 0x0A, 0x0B),
    "user": (0x0E, 0x0F, 0x10, 0x11),
}
STATUS_INVALID_ADDRESS, STATUS_INVALID_MODEL, STATUS_INVALID_APPKEY = 0x01, 0x02, 0x03
STATUS_INVALID_NETKEY, STATUS_KEY_INDEX_STORED = 0x04, 0x06
STATUS_CANNOT_UPDATE = 0x0B


@dataclass
class ModelConfig:
    """What the Configuration Server holds for one model: AppKey bindings, publication, subscriptions."""

    bind: list[int] = field(default_factory=list)
    # [address u16][AppKey index + credential flag u16][TTL][period][retransmit], as the wire carries it
    publication: bytes = bytes(7)
    subscriptions: list[int] = field(default_factory=list)

    @property
    def publish_address(self) -> int:
        return int.from_bytes(self.publication[:2], "little")

    @property
    def publish_ttl(self) -> int:
        return self.publication[4]

    @property
    def publish_app_key(self) -> int:
        return int.from_bytes(self.publication[2:4], "little") & 0xFFF


@dataclass
class ConfigState:
    """The node-wide Configuration Server states, in wire fields (retransmissions and 10 ms steps minus one)."""

    default_ttl: int = 5
    relay: tuple[int, int, int] = (1, 2, 8)  # state, retransmit count, interval steps
    network_transmit: tuple[int, int] = (2, 9)  # count, interval steps
    beacon: int = 1
    gatt_proxy: int = 1
    models: dict[tuple[int, str], ModelConfig] = field(default_factory=dict)


@dataclass
class ElementState:
    """The application state of one element."""

    on: bool = False
    lightness: int = 0
    last_lightness: int = 0xFFFF
    temperature: int = 3000
    delta_uv: int = 0
    scenes: dict[int, tuple[bool, int, int]] = field(default_factory=dict)
    current_scene: int = 0
    # LBC vendor properties by id (one namespace: an id names one property whichever server is asked) ->
    # (user access, value)
    properties: dict[int, tuple[int, bytes]] = field(default_factory=dict)
    last_tid: dict[int, tuple[int, float]] = field(
        default_factory=dict
    )  # src -> (tid, time)


def _publication_from_cdb(raw: dict[str, Any]) -> bytes:
    publish = raw.get("publish")
    if not isinstance(publish, dict) or "address" not in publish:
        return bytes(7)
    address = int(str(publish["address"]), 16)
    index = int(publish.get("index", 0)) | (int(publish.get("credentials", 0)) << 12)
    return (
        address.to_bytes(2, "little")
        + index.to_bytes(2, "little")
        + bytes([int(publish.get("ttl", 0xFF)) & 0xFF, 0, 0])
    )


def _preloaded_properties(product: int | None) -> dict[int, bytes]:
    """The catalogue's LBC properties of the product, each at the canonical form of an all-zero value."""
    out: dict[int, bytes] = {}
    if product is None:
        return out
    for spec in P.for_product(product):
        if not spec.vendor:
            continue
        try:
            out[spec.id] = spec.codec.encode(spec.codec.decode(bytes(32)))
        except (ValueError, TypeError, KeyError):
            continue
    return out


class Servers:
    """Every model of one node, and the Configuration Server's view of them."""

    def __init__(self, node: SimNode, cdb_node: Node, *, fresh: bool) -> None:
        self.node = node
        self.cdb_node = cdb_node
        self.config = ConfigState()
        self.models: dict[int, list[str]] = {
            e.address: list(e.models) for e in cdb_node.elements
        }
        self.state: dict[int, ElementState] = {
            e.address: ElementState() for e in cdb_node.elements
        }
        self.published: list[
            tuple[int, int, bytes]
        ] = []  # (element, address, access) of every publication
        for element in cdb_node.elements:
            for raw in element.raw_models:
                config = ModelConfig()
                if not fresh:
                    config.bind = [int(i) for i in raw.get("bind", [])]
                    config.publication = _publication_from_cdb(raw)
                    config.subscriptions = [
                        int(str(a), 16) for a in raw.get("subscribe", [])
                    ]
                self.config.models[(element.address, raw["modelId"])] = config
            for model in element.models:
                self.config.models.setdefault((element.address, model), ModelConfig())
        properties = _preloaded_properties(cdb_node.pid)
        for address, models in self.models.items():
            if any(model in models for model in LBC_MODELS):
                self.state[address].properties = {
                    pid: (3, v) for pid, v in properties.items()
                }

    # ------------------------------------------------------------------ addressing
    def subscribed(self, group: int) -> bool:
        return any(group in m.subscriptions for m in self.config.models.values())

    def _targets(self, dst: int) -> list[int]:
        """The elements a message to `dst` is for (their models' subscriptions decide for a group)."""
        if dst in self.models:
            return [dst]
        if dst == 0xFFFF:
            return list(self.models)
        return sorted(
            {
                el
                for (el, _m), cfg in self.config.models.items()
                if dst in cfg.subscriptions
            }
        )

    def _bound(self, element: int, model: str, key: str) -> bool:
        cfg = self.config.models.get((element, model))
        return cfg is not None and key.startswith("app") and int(key[3:]) in cfg.bind

    def _hosts(self, element: int, model: str, msg: Received) -> bool:
        """Whether `model` on `element` takes `msg`: it is there, bound to the message's AppKey, and addressed."""
        if model not in self.models.get(element, ()):
            return False
        if not self._bound(element, model, msg.key):
            return False
        if msg.dst in (element, 0xFFFF):
            return True
        return msg.dst in self.config.models[(element, model)].subscriptions

    # ------------------------------------------------------------------ dispatch
    def dispatch(self, msg: Received) -> None:
        if msg.key == "dev":
            if msg.dst == self.node.addr and msg.company_id is None:
                self._config(msg)
            return
        for element in self._targets(msg.dst):
            if msg.company_id == JUNG_CID:
                self._vendor(element, msg)
            elif msg.company_id is None:
                self._sig(element, msg)

    def reply(self, element: int, msg: Received, access: bytes) -> None:
        """Answer `msg` from `element` to its source, under the key it came with."""
        if not is_unicast(msg.src):
            return
        key = (
            self.node.dev_tx()
            if msg.key == "dev"
            else self.node.app_tx(int(msg.key[3:]))
        )
        self.node.send(element, msg.src, access, key)

    def publish(self, element: int, model: str, access: bytes) -> bool:
        """Publish `access` to the model's publication address; False when it has none (or no AppKey for it)."""
        cfg = self.config.models.get((element, model))
        if cfg is None or not cfg.publish_address:
            return False
        index = cfg.publish_app_key
        if index not in self.node.app_keys:
            return False
        ttl = cfg.publish_ttl
        self.published.append((element, cfg.publish_address, access))
        self.node.send(
            element,
            cfg.publish_address,
            access,
            self.node.app_tx(index),
            None if ttl == 0xFF else ttl,
        )
        return True

    def _answer_set(
        self, element: int, model: str, msg: Received, changed: bool, status: bytes
    ) -> None:
        """Answer an acknowledged Set (JUNG: only by the publication when the state changed) and publish."""
        published = self.publish(element, model, status) if changed else False
        if not (published and self.node.mesh.quirks.set_reply_by_publication):
            self.reply(element, msg, status)

    def _repeat(self, element: int, msg: Received, tid: int) -> bool:
        """Whether a Set is the repetition of the previous transaction from the same source (§3.3.2.2.3)."""
        now = asyncio.get_running_loop().time()
        st = self.state[element]
        last = st.last_tid.get(msg.src)
        st.last_tid[msg.src] = (tid, now)
        return last is not None and last[0] == tid and now - last[1] < TID_WINDOW

    # ------------------------------------------------------------------ SIG models
    def _sig(self, element: int, msg: Received) -> None:  # noqa: PLR0911, PLR0915  # one branch per message
        op, p = msg.opcode, msg.params
        st = self.state[element]
        if op in (M.GEN_ONOFF_GET, M.GEN_ONOFF_SET, M.GEN_ONOFF_SET_UNACK):
            if not self._hosts(element, "1000", msg):
                return
            if op == M.GEN_ONOFF_GET:
                self.reply(element, msg, self._onoff_status(st))
                return
            changed = False
            if not self._repeat(element, msg, p[1]):
                changed = st.on != bool(p[0])
                self._set_on(element, bool(p[0]))
            status = self._onoff_status(st)
            if op == M.GEN_ONOFF_SET:
                self._answer_set(element, "1000", msg, changed, status)
            elif changed:
                self.publish(element, "1000", status)
            return
        if op in (
            M.LIGHT_LIGHTNESS_GET,
            M.LIGHT_LIGHTNESS_SET,
            M.LIGHT_LIGHTNESS_SET_UNACK,
        ):
            if not self._hosts(element, "1300", msg):
                return
            if op == M.LIGHT_LIGHTNESS_GET:
                self.reply(element, msg, self._lightness_status(st))
                return
            changed = False
            if not self._repeat(element, msg, p[2]):
                value = int.from_bytes(p[:2], "little")
                changed = value != st.lightness
                self._set_lightness(element, value)
            status = self._lightness_status(st)
            if op == M.LIGHT_LIGHTNESS_SET:
                self._answer_set(element, "1300", msg, changed, status)
            elif changed:
                self.publish(element, "1300", status)
            return
        if op in (M.LIGHT_CTL_GET, M.LIGHT_CTL_SET, M.LIGHT_CTL_SET_UNACK):
            if not self._hosts(element, "1303", msg):
                return
            if op == M.LIGHT_CTL_GET:
                self.reply(element, msg, self._ctl_status(element))
                return
            changed = False
            if not self._repeat(element, msg, p[6]):
                lightness = int.from_bytes(p[:2], "little")
                temperature = int.from_bytes(p[2:4], "little")
                temp_state = self.state.get(element + 1, st)
                changed = (lightness, temperature) != (
                    st.lightness,
                    temp_state.temperature,
                )
                temp_state.temperature = temperature
                temp_state.delta_uv = int.from_bytes(p[4:6], "little", signed=True)
                self._set_lightness(element, lightness)
            status = self._ctl_status(element)
            if op == M.LIGHT_CTL_SET:
                self._answer_set(element, "1303", msg, changed, status)
            elif changed:
                self.publish(element, "1303", status)
            return
        if op in (
            M.LIGHT_CTL_TEMP_GET,
            M.LIGHT_CTL_TEMP_SET,
            M.LIGHT_CTL_TEMP_SET_UNACK,
        ):
            if not self._hosts(element, "1306", msg):
                return
            if op == M.LIGHT_CTL_TEMP_GET:
                self.reply(element, msg, self._temperature_status(st))
                return
            changed = False
            if not self._repeat(element, msg, p[4]):
                temperature = int.from_bytes(p[:2], "little")
                changed = temperature != st.temperature
                st.temperature = temperature
                st.delta_uv = int.from_bytes(p[2:4], "little", signed=True)
            status = self._temperature_status(st)
            if op == M.LIGHT_CTL_TEMP_SET:
                self._answer_set(element, "1306", msg, changed, status)
            elif changed:
                self.publish(element, "1306", status)
            return
        self._scene(element, msg)

    def _set_on(self, element: int, on: bool) -> None:
        st = self.state[element]
        st.on = on
        if "1300" in self.models[element]:
            st.lightness = (st.last_lightness or 0xFFFF) if on else 0

    def _set_lightness(self, element: int, value: int) -> None:
        st = self.state[element]
        st.lightness = value
        if value:
            st.last_lightness = value
        st.on = value > 0

    @staticmethod
    def _onoff_status(st: ElementState) -> bytes:
        return encode_opcode(M.GEN_ONOFF_STATUS) + bytes([int(st.on)])

    @staticmethod
    def _lightness_status(st: ElementState) -> bytes:
        return encode_opcode(M.LIGHT_LIGHTNESS_STATUS) + st.lightness.to_bytes(
            2, "little"
        )

    def _ctl_status(self, element: int) -> bytes:
        st = self.state[element]
        temperature = self.state.get(element + 1, st).temperature
        return (
            encode_opcode(M.LIGHT_CTL_STATUS)
            + st.lightness.to_bytes(2, "little")
            + temperature.to_bytes(2, "little")
        )

    @staticmethod
    def _temperature_status(st: ElementState) -> bytes:
        return (
            encode_opcode(M.LIGHT_CTL_TEMP_STATUS)
            + st.temperature.to_bytes(2, "little")
            + st.delta_uv.to_bytes(2, "little", signed=True)
        )

    # ------------------------------------------------------------------ scenes
    def _scene(self, element: int, msg: Received) -> None:
        op, p = msg.opcode, msg.params
        st = self.state[element]
        setup_ops = (
            M.SCENE_STORE,
            M.SCENE_STORE_UNACK,
            M.SCENE_DELETE,
            M.SCENE_DELETE_UNACK,
        )
        server_ops = (
            M.SCENE_GET,
            M.SCENE_RECALL,
            M.SCENE_RECALL_UNACK,
            M.SCENE_REGISTER_GET,
        )
        model = "1204" if op in setup_ops else "1203" if op in server_ops else None
        if model is None or not self._hosts(element, model, msg):
            return
        if op in setup_ops:
            number = int.from_bytes(p[:2], "little")
            if op in (M.SCENE_STORE, M.SCENE_STORE_UNACK):
                st.scenes[number] = (st.on, st.lightness, st.temperature)
                st.current_scene = number
                code = 0
            else:
                code = (
                    0 if st.scenes.pop(number, None) is not None else M.SCENE_NOT_FOUND
                )
                if st.current_scene == number:
                    st.current_scene = 0
            if op in (M.SCENE_STORE, M.SCENE_DELETE):
                self.reply(element, msg, self._register_status(st, code))
            return
        if op == M.SCENE_REGISTER_GET:
            self.reply(element, msg, self._register_status(st, 0))
            return
        if op == M.SCENE_GET:
            self.reply(element, msg, self._scene_status(st, 0))
            return
        number = int.from_bytes(p[:2], "little")
        stored = st.scenes.get(number)
        if stored is None:
            code = M.SCENE_NOT_FOUND
        else:
            code = 0
            on, lightness, temperature = stored
            st.on, st.lightness, st.temperature, st.current_scene = (
                on,
                lightness,
                temperature,
                number,
            )
        if op == M.SCENE_RECALL:
            self.reply(element, msg, self._scene_status(st, code))

    @staticmethod
    def _register_status(st: ElementState, code: int) -> bytes:
        return (
            encode_opcode(M.SCENE_REGISTER_STATUS)
            + bytes([code])
            + st.current_scene.to_bytes(2, "little")
            + b"".join(n.to_bytes(2, "little") for n in sorted(st.scenes))
        )

    @staticmethod
    def _scene_status(st: ElementState, code: int) -> bytes:
        return (
            encode_opcode(M.SCENE_STATUS)
            + bytes([code])
            + st.current_scene.to_bytes(2, "little")
        )

    # ------------------------------------------------------------------ LBC vendor property servers
    def _vendor(self, element: int, msg: Received) -> None:
        for model, kind in LBC_MODELS.items():
            get, set_ack, set_unack, status = LBC_OPS[kind]
            if msg.opcode not in (get, set_ack, set_unack):
                continue
            if not self._hosts(element, model, msg) or len(msg.params) < 2:
                return
            pid = int.from_bytes(msg.params[:2], "little")
            props = self.state[element].properties
            if msg.opcode != get:
                if kind == "admin":
                    access, value = msg.params[2], msg.params[3:]
                else:
                    access, value = props.get(pid, (3, b""))[0], msg.params[2:]
                props[pid] = (access, bytes(value))
                if msg.opcode == set_unack:
                    return
            elif pid not in props:
                return  # a property the server does not hold: no answer
            access, value = props[pid]
            self.reply(
                element,
                msg,
                encode_opcode(status, JUNG_CID)
                + pid.to_bytes(2, "little")
                + bytes([access])
                + value,
            )
            return

    # ------------------------------------------------------------------ Configuration Server
    def _lookup(
        self, element: int, model_bytes: bytes
    ) -> tuple[str, ModelConfig | None, int]:
        model = C.model_id_str(C.decode_model_id(model_bytes))
        if element not in self.models:
            return model, None, STATUS_INVALID_ADDRESS
        if model not in self.models[element]:
            return model, None, STATUS_INVALID_MODEL
        return model, self.config.models[(element, model)], C.STATUS_SUCCESS

    def _config(self, msg: Received) -> None:  # noqa: C901, PLR0915  # one branch per message
        op, p = msg.opcode, msg.params
        cfg = self.config
        node = self.node

        def answer(status_op: int, body: bytes) -> None:
            self.reply(node.addr, msg, encode_opcode(status_op) + body)

        if op == C.CONFIG_APPKEY_ADD:
            idx = int.from_bytes(p[:3], "little")
            net_index, app_index = idx & 0xFFF, idx >> 12
            key = bytes(p[3:19])
            have = node.app_keys.get(app_index)
            if net_index != 0:
                code = STATUS_INVALID_NETKEY
            elif have is not None and have.key != key:
                code = STATUS_KEY_INDEX_STORED
            else:
                code = C.STATUS_SUCCESS
                node.app_keys[app_index] = AppKeyMaterial.derive(key)
            answer(C.CONFIG_APPKEY_STATUS, bytes([code]) + p[:3])
        elif op == C.CONFIG_COMPOSITION_DATA_GET:
            answer(C.CONFIG_COMPOSITION_DATA_STATUS, self._composition())
        elif op in (C.CONFIG_MODEL_APP_BIND, C.CONFIG_MODEL_APP_UNBIND):
            element = int.from_bytes(p[:2], "little")
            index = int.from_bytes(p[2:4], "little") & 0xFFF
            _model, mc, code = self._lookup(element, p[4:])
            if mc is not None:
                if index not in node.app_keys:
                    code = STATUS_INVALID_APPKEY
                elif op == C.CONFIG_MODEL_APP_BIND:
                    if index not in mc.bind:
                        mc.bind.append(index)
                elif index in mc.bind:
                    mc.bind.remove(index)
            answer(C.CONFIG_MODEL_APP_STATUS, bytes([code]) + p)
        elif op in (C.CONFIG_MODEL_PUBLICATION_GET, C.CONFIG_MODEL_PUBLICATION_SET):
            element = int.from_bytes(p[:2], "little")
            model_bytes = p[2:] if op == C.CONFIG_MODEL_PUBLICATION_GET else p[9:]
            _model, mc, code = self._lookup(element, model_bytes)
            if mc is not None and op == C.CONFIG_MODEL_PUBLICATION_SET:
                index = int.from_bytes(p[4:6], "little") & 0xFFF
                if index not in node.app_keys and int.from_bytes(p[2:4], "little"):
                    code = STATUS_INVALID_APPKEY
                else:
                    mc.publication = bytes(p[2:9])
            fields = mc.publication if mc is not None else bytes(7)
            answer(
                C.CONFIG_MODEL_PUBLICATION_STATUS,
                bytes([code]) + p[:2] + fields + model_bytes,
            )
        elif op in (
            C.CONFIG_MODEL_SUBSCRIPTION_ADD,
            C.CONFIG_MODEL_SUBSCRIPTION_DELETE,
            C.CONFIG_MODEL_SUBSCRIPTION_OVERWRITE,
            C.CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL,
        ):
            element = int.from_bytes(p[:2], "little")
            if op == C.CONFIG_MODEL_SUBSCRIPTION_DELETE_ALL:
                address, model_bytes = 0, p[2:]
            else:
                address, model_bytes = int.from_bytes(p[2:4], "little"), p[4:]
            _model, mc, code = self._lookup(element, model_bytes)
            if mc is not None:
                if op == C.CONFIG_MODEL_SUBSCRIPTION_ADD:
                    if address not in mc.subscriptions:
                        mc.subscriptions.append(address)
                elif op == C.CONFIG_MODEL_SUBSCRIPTION_DELETE:
                    if address in mc.subscriptions:
                        mc.subscriptions.remove(address)
                elif op == C.CONFIG_MODEL_SUBSCRIPTION_OVERWRITE:
                    mc.subscriptions = [address]
                else:
                    mc.subscriptions = []
            answer(
                C.CONFIG_MODEL_SUBSCRIPTION_STATUS,
                bytes([code]) + p[:2] + address.to_bytes(2, "little") + model_bytes,
            )
        elif op in (
            C.CONFIG_SIG_MODEL_SUBSCRIPTION_GET,
            C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_GET,
        ):
            element = int.from_bytes(p[:2], "little")
            _model, mc, code = self._lookup(element, p[2:])
            addresses = mc.subscriptions if mc is not None else []
            status = (
                C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST
                if op == C.CONFIG_SIG_MODEL_SUBSCRIPTION_GET
                else C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST
            )
            answer(
                status,
                bytes([code])
                + p
                + b"".join(a.to_bytes(2, "little") for a in addresses),
            )
        elif op in (C.CONFIG_SIG_MODEL_APP_GET, C.CONFIG_VENDOR_MODEL_APP_GET):
            element = int.from_bytes(p[:2], "little")
            _model, mc, code = self._lookup(element, p[2:])
            keys = mc.bind if mc is not None else []
            status = (
                C.CONFIG_SIG_MODEL_APP_LIST
                if op == C.CONFIG_SIG_MODEL_APP_GET
                else C.CONFIG_VENDOR_MODEL_APP_LIST
            )
            answer(status, bytes([code]) + p + pack_key_indexes(keys))
        elif op in (C.CONFIG_DEFAULT_TTL_GET, C.CONFIG_DEFAULT_TTL_SET):
            if op == C.CONFIG_DEFAULT_TTL_SET:
                cfg.default_ttl = p[0]
            answer(C.CONFIG_DEFAULT_TTL_STATUS, bytes([cfg.default_ttl]))
        elif op in (C.CONFIG_RELAY_GET, C.CONFIG_RELAY_SET):
            if op == C.CONFIG_RELAY_SET:
                cfg.relay = (p[0], p[1] & 0x07, p[1] >> 3)
            state, count, steps = cfg.relay
            answer(C.CONFIG_RELAY_STATUS, bytes([state, count | steps << 3]))
        elif op in (C.CONFIG_NETWORK_TRANSMIT_GET, C.CONFIG_NETWORK_TRANSMIT_SET):
            if op == C.CONFIG_NETWORK_TRANSMIT_SET:
                cfg.network_transmit = (p[0] & 0x07, p[0] >> 3)
            count, steps = cfg.network_transmit
            answer(C.CONFIG_NETWORK_TRANSMIT_STATUS, bytes([count | steps << 3]))
        elif op in (C.CONFIG_BEACON_GET, C.CONFIG_BEACON_SET):
            if op == C.CONFIG_BEACON_SET:
                cfg.beacon = p[0]
            answer(C.CONFIG_BEACON_STATUS, bytes([cfg.beacon]))
        elif op in (C.CONFIG_GATT_PROXY_GET, C.CONFIG_GATT_PROXY_SET):
            if op == C.CONFIG_GATT_PROXY_SET:
                cfg.gatt_proxy = p[0]
            answer(C.CONFIG_GATT_PROXY_STATUS, bytes([cfg.gatt_proxy]))
        elif op == C.CONFIG_NODE_RESET:
            answer(C.CONFIG_NODE_RESET_STATUS, b"")
            # the status goes out first; the node then forgets the network
            asyncio.get_running_loop().call_later(0.5, node.reset)
        elif op == C.CONFIG_NETKEY_UPDATE:
            index = int.from_bytes(p[:2], "little") & 0xFFF
            new = bytes(p[2:18])
            if index != 0:
                code = STATUS_INVALID_NETKEY
            elif node.kr_phase == 0 and new != node.net_key.key:
                node.key_refresh(1, NetKeyMaterial.derive(new))
                code = C.STATUS_SUCCESS
            elif (
                node.kr_phase == 1
                and node.new_net_key is not None
                and node.new_net_key.key == new
            ):
                code = C.STATUS_SUCCESS
            else:
                code = STATUS_CANNOT_UPDATE
            answer(C.CONFIG_NETKEY_STATUS, bytes([code]) + p[:2])
        elif op in (C.CONFIG_KEY_REFRESH_PHASE_GET, C.CONFIG_KEY_REFRESH_PHASE_SET):
            index = int.from_bytes(p[:2], "little") & 0xFFF
            code = C.STATUS_SUCCESS if index == 0 else STATUS_INVALID_NETKEY
            if code == C.STATUS_SUCCESS and op == C.CONFIG_KEY_REFRESH_PHASE_SET:
                transition = p[2]
                if transition == 2 and node.kr_phase in (1, 2):
                    node.key_refresh(2)
                elif transition == 3 and node.kr_phase in (1, 2):
                    node.key_refresh(3)
                elif not (transition == 3 and node.kr_phase == 0):
                    code = STATUS_CANNOT_UPDATE
            answer(
                C.CONFIG_KEY_REFRESH_PHASE_STATUS,
                bytes([code]) + p[:2] + bytes([node.kr_phase]),
            )

    def _composition(self) -> bytes:
        """Composition Data page 0 (§4.2.1.1) of the node's elements and models."""
        n = self.cdb_node
        features = 0x0003  # relay + proxy
        out = bytes([0]) + b"".join(
            v.to_bytes(2, "little")
            for v in (n.cid or JUNG_CID, n.pid or 0, 0x0001, 0x0100, features)
        )
        for element in n.elements:
            sig = [m for m in element.models if not C.is_vendor_model(m)]
            vendor = [m for m in element.models if C.is_vendor_model(m)]
            out += element.location.to_bytes(2, "little") + bytes(
                [len(sig), len(vendor)]
            )
            out += b"".join(C.encode_model_id(m) for m in sig + vendor)
        return out


def pack_key_indexes(keys: list[int]) -> bytes:
    """AppKey indexes as a Model App List carries them (§4.3.1.1): two per 3 octets, an odd last one in 2."""
    out = b"".join(
        (keys[i] | keys[i + 1] << 12).to_bytes(3, "little")
        for i in range(0, len(keys) - 1, 2)
    )
    return out + (keys[-1].to_bytes(2, "little") if len(keys) % 2 else b"")
