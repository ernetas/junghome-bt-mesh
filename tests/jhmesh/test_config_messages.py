"""jhmesh.config_messages: Config Server builders produce the spec's byte layouts, status decoders invert them.

Vectors are hand-assembled from Mesh Profile 1.0.1 §4.3.2 (field order, LE integers, packed key indexes, the
11/13-byte Publication Set), the app's publication defaults (docs/android/network-logic.md §1.5: TTL 0xFF, no
period, no retransmission, AppKey 0) and the fixture network (tests/fixtures/MeshNetwork.json: element 0149's
Generic OnOff Client and 0527:1015 client publish to C005; 0148's OnOff server is in room C00F). The AppKey Add
vector is the spec's sample Message #6 (§8.3.6).
"""

from __future__ import annotations

import pytest

from jhmesh import config_messages as C

h = bytes.fromhex

BUTTON_A = 0x0149
LIGHT = 0x0148
GATEWAY_GROUP = 0xC005
ROOM_WC = 0xC00F
ELEMENT_GROUP = 0xC061
SAMPLE_APPKEY = h("63964771734fbd76e3b40519d1d94a48")  # §8.1 sample AppKey

# the fixture push-button as Composition Data page 0: CID 0527, PID 0001, VID 0002, CRPL 40, relay + proxy;
# element 0 (loc 0001): Config Server, OnOff Server, LBC User Property Server; element 1 (loc 0040): LBC client
COMPOSITION_PAGE0 = (
    h("00")
    + h("2705 0100 0200 2800 0300")
    + h("0100 02 01 0000 0010 2705 1310")
    + h("4000 00 01 2705 1510")
)


# ----------------------------------------------------------------------------- model identifiers


@pytest.mark.parametrize(
    ("model", "normalised", "vendor", "cdb", "wire"),
    [
        ("1000", 0x1000, False, "1000", h("0010")),
        (0x1001, 0x1001, False, "1001", h("0110")),
        ("05271013", 0x05271013, True, "05271013", h("27051310")),
        (0x05271015, 0x05271015, True, "05271015", h("27051510")),
        (0, 0, False, "0000", h("0000")),
    ],
)
def test_model_id_forms(model, normalised, vendor, cdb, wire):
    assert C.model_id(model) == normalised
    assert C.is_vendor_model(model) is vendor
    assert C.model_id_str(model) == cdb
    assert C.encode_model_id(model) == wire
    assert C.decode_model_id(wire) == normalised


def test_model_id_rejects_out_of_range_and_bad_widths():
    with pytest.raises(ValueError, match="out of range"):
        C.model_id(-1)
    with pytest.raises(ValueError, match="out of range"):
        C.model_id(0x1_0000_0000)
    with pytest.raises(ValueError, match="2 or 4 bytes"):
        C.decode_model_id(h("270513"))


# ----------------------------------------------------------------------------- builders


def test_appkey_add_matches_the_spec_sample():
    """§8.3.6 Message #6: NetKeyIndex 0x456 and AppKeyIndex 0x123 pack to 56 34 12 (net in the low 12 bits)."""
    assert C.appkey_add(SAMPLE_APPKEY, app_key_index=0x123, net_key_index=0x456) == h(
        "0056341263964771734fbd76e3b40519d1d94a48"
    )
    assert C.appkey_add(SAMPLE_APPKEY) == h("00000000") + SAMPLE_APPKEY  # the app: 0/0
    with pytest.raises(ValueError, match="16 bytes"):
        C.appkey_add(SAMPLE_APPKEY[:15])
    with pytest.raises(ValueError, match="12-bit"):
        C.appkey_add(SAMPLE_APPKEY, app_key_index=0x1000)
    with pytest.raises(ValueError, match="12-bit"):
        C.appkey_add(SAMPLE_APPKEY, net_key_index=-1)


def test_key_management_builders():
    """AppKey Update / Delete, NetKey Add / Update / Delete and Key Refresh Phase Get / Set (§4.3.2.37-58): the
    messages of a key refresh, which the app's KeyRenewal sends (NetKey Update, then Key Refresh Phase Set 2 / 3)."""
    netkey = bytes(range(0xA0, 0xB0))
    assert C.appkey_update(SAMPLE_APPKEY, 0x123, 0x456) == h("01563412") + SAMPLE_APPKEY
    assert C.appkey_update(SAMPLE_APPKEY) == h("01000000") + SAMPLE_APPKEY
    assert C.appkey_delete(0x123, 0x456) == h("8000563412")
    assert C.appkey_delete() == h("8000000000")
    assert C.netkey_add(netkey, 0x456) == h("80405604") + netkey
    assert C.netkey_add(netkey) == h("80400000") + netkey
    assert C.netkey_update(netkey, 0x456) == h("80455604") + netkey
    assert C.netkey_update(netkey) == h("80450000") + netkey
    assert C.netkey_delete(0x456) == h("80415604")
    assert C.key_refresh_phase_get() == h("80150000")
    assert C.key_refresh_phase_get(0x456) == h("80155604")
    assert C.key_refresh_phase_set(2) == h("8016000002")
    assert C.key_refresh_phase_set(3, 0x456) == h("8016560403")
    for bad in (
        lambda: C.appkey_update(SAMPLE_APPKEY[:15]),
        lambda: C.netkey_add(netkey + b"\x00"),
        lambda: C.netkey_update(b""),
    ):
        with pytest.raises(ValueError, match="16 bytes"):
            bad()
    with pytest.raises(ValueError, match="12-bit"):
        C.netkey_add(netkey, 0x1000)
    with pytest.raises(ValueError, match="12-bit"):
        C.netkey_delete(-1)
    with pytest.raises(ValueError, match="12-bit"):
        C.appkey_delete(net_key_index=0x1000)
    with pytest.raises(ValueError, match="transition must be 2 or 3"):
        C.key_refresh_phase_set(1)
    assert {0x00, 0x01, 0x8040, 0x8045} == C.KEY_CARRYING_OPCODES


def test_composition_data_get():
    assert C.composition_data_get() == h("800800")
    assert C.composition_data_get(1) == h("800801")
    with pytest.raises(ValueError, match="page"):
        C.composition_data_get(256)


def test_model_app_bind_and_unbind():
    assert C.model_app_bind(BUTTON_A, 0x1001) == h(
        "803d 4901 0000 0110".replace(" ", "")
    )
    assert C.model_app_bind(BUTTON_A, "05271015") == h(
        "803d 4901 0000 2705 1510".replace(" ", "")
    )
    assert C.model_app_unbind(LIGHT, "1000", app_key_index=0x123) == h(
        "803f 4801 2301 0010".replace(" ", "")
    )
    with pytest.raises(ValueError, match="unicast"):
        C.model_app_bind(ROOM_WC, 0x1000)
    with pytest.raises(ValueError, match="12-bit"):
        C.model_app_bind(LIGHT, 0x1000, app_key_index=0x1000)


def test_model_publication_get():
    assert C.model_publication_get(BUTTON_A, 0x1001) == h(
        "8018 4901 0110".replace(" ", "")
    )
    assert C.model_publication_get(BUTTON_A, "05271015") == h(
        "8018 4901 2705 1510".replace(" ", "")
    )


def test_model_publication_set_app_defaults():
    """The app's ConnectToAddress publication: ttl 0xFF, period 0, retransmit 0/0, AppKey 0, master credentials."""
    assert C.model_publication_set(BUTTON_A, GATEWAY_GROUP, "1001") == h(
        "03 4901 05c0 0000 ff 00 00 0110".replace(" ", "")
    )
    assert len(C.model_publication_set(BUTTON_A, GATEWAY_GROUP, "1001")) == 12
    vendor = C.model_publication_set(BUTTON_A, GATEWAY_GROUP, "05271015")
    assert vendor == h("03 4901 05c0 0000 ff 00 00 2705 1510".replace(" ", ""))
    assert len(vendor) == 14
    # publishing disabled: what RemoveConnectionForAddress sends
    assert C.model_publication_set(BUTTON_A, 0x0000, 0x1001) == h(
        "03 4901 0000 0000 ff 00 00 0110".replace(" ", "")
    )


def test_model_publication_set_all_fields():
    """AppKey index 0x123 + credential flag → 0x1123 LE; period 30 steps @ 1 s → 0x5E; retransmit 2 x 5 steps → 0x2A."""
    assert C.model_publication_set(
        LIGHT,
        ELEMENT_GROUP,
        0x1000,
        app_key_index=0x123,
        credential=True,
        ttl=7,
        period_steps=30,
        period_resolution=1,
        retransmit_count=2,
        retransmit_interval_steps=5,
    ) == h("03 4801 61c0 2311 07 5e 2a 0010".replace(" ", ""))


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"ttl": 0x80}, "publish TTL"),
        ({"ttl": -1}, "publish TTL"),
        ({"period_steps": 64}, "period steps"),
        ({"period_resolution": 4}, "period resolution"),
        ({"retransmit_count": 8}, "retransmit count"),
        ({"retransmit_interval_steps": 32}, "retransmit interval"),
        ({"app_key_index": 0x1000}, "12-bit"),
    ],
)
def test_model_publication_set_rejects_out_of_range_fields(kwargs, match):
    with pytest.raises(ValueError, match=match):
        C.model_publication_set(BUTTON_A, GATEWAY_GROUP, 0x1001, **kwargs)


def test_model_publication_set_rejects_bad_addresses():
    with pytest.raises(ValueError, match="element address must be unicast"):
        C.model_publication_set(0x0000, GATEWAY_GROUP, 0x1001)
    with pytest.raises(ValueError, match="publish address out of range"):
        C.model_publication_set(BUTTON_A, 0x10000, 0x1001)


def test_model_subscription_builders():
    assert C.model_subscription_add(LIGHT, ROOM_WC, "1000") == h(
        "801b 4801 0fc0 0010".replace(" ", "")
    )
    assert C.model_subscription_delete(LIGHT, ROOM_WC, 0x1000) == h(
        "801c 4801 0fc0 0010".replace(" ", "")
    )
    assert C.model_subscription_overwrite(LIGHT, ROOM_WC, 0x1000) == h(
        "801e 4801 0fc0 0010".replace(" ", "")
    )
    assert C.model_subscription_add(LIGHT, ELEMENT_GROUP, "05271013") == h(
        "801b 4801 61c0 2705 1310".replace(" ", "")
    )
    assert C.model_subscription_delete_all(LIGHT, 0x1000) == h(
        "801d 4801 0010".replace(" ", "")
    )
    assert C.model_subscription_get(LIGHT, 0x1000) == h(
        "8029 4801 0010".replace(" ", "")
    )
    assert C.model_subscription_get(LIGHT, "05271013") == h(
        "802b 4801 2705 1310".replace(" ", "")
    )
    assert C.model_subscription_add(LIGHT, 0xFFFB, 0x1000)[4:6] == h("fbff")


@pytest.mark.parametrize("address", [0x0148, 0xBFFF, 0xFFFC, 0xFFFF])
def test_model_subscription_rejects_non_group_addresses(address):
    with pytest.raises(ValueError, match="group address"):
        C.model_subscription_add(LIGHT, address, 0x1000)


def test_node_feature_builders():
    """GATT Proxy / Default TTL / Relay / Network Transmit / Beacon as ConfigureDevice sends them (§3.2)."""
    assert C.gatt_proxy_get() == h("8012")
    assert C.gatt_proxy_set(True) == h("801301")
    assert C.gatt_proxy_set(False) == h("801300")
    assert C.default_ttl_get() == h("800c")
    assert C.default_ttl_set() == h("800d05")
    assert C.default_ttl_set(0) == h("800d00")
    assert C.relay_get() == h("8026")
    assert C.relay_set() == h("8027014b")  # relay on, count 3, 9 steps → 0x4B
    assert C.relay_set(False, 0, 0) == h("80270000")
    assert C.network_transmit_get() == h("8023")
    assert C.network_transmit_set() == h("802453")  # count 3, 10 steps → 0x53
    assert C.network_transmit_set(1, 31) == h("8024f9")
    assert C.beacon_get() == h("8009")
    assert C.beacon_set(True) == h("800a01")
    assert C.beacon_set(False) == h("800a00")
    assert C.node_reset() == h("8049")


def test_node_feature_builders_reject_bad_values():
    with pytest.raises(ValueError, match="GATT proxy"):
        C.gatt_proxy_set(2)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="default TTL"):
        C.default_ttl_set(1)
    with pytest.raises(ValueError, match="default TTL"):
        C.default_ttl_set(128)
    with pytest.raises(ValueError, match="relay retransmit count"):
        C.relay_set(True, 8, 0)
    with pytest.raises(ValueError, match="relay retransmit interval"):
        C.relay_set(True, 0, 32)
    with pytest.raises(ValueError, match="network transmit count"):
        C.network_transmit_set(-1, 0)


# ----------------------------------------------------------------------------- status decoders


def test_appkey_status():
    s = C.decode_appkey_status(h("00563412"))
    assert s == C.AppKeyStatus(0, 0x456, 0x123)
    assert s.ok
    assert s.status_name == "Success"
    dup = C.decode_appkey_status(h("06000000"))
    assert (dup.ok, dup.status_name, dup.net_key_index, dup.app_key_index) == (
        False,
        "Key Index Already Stored",
        0,
        0,
    )
    assert C.decode_appkey_status(h("ff000000")).status_name == "status 0xFF"
    with pytest.raises(ValueError, match="need 4 bytes"):
        C.decode_appkey_status(h("000000"))


def test_netkey_and_key_refresh_phase_status():
    s = C.decode_netkey_status(h("005604"))
    assert s == C.NetKeyStatus(0, 0x456)
    assert s.ok
    assert s.status_name == "Success"
    assert C.decode_netkey_status(h("04ffff")) == C.NetKeyStatus(
        4, 0xFFF
    )  # RFU bits masked
    assert not C.decode_netkey_status(h("04ffff")).ok
    with pytest.raises(ValueError, match="need 3 bytes"):
        C.decode_netkey_status(h("0000"))
    k = C.decode_key_refresh_phase_status(h("00000002"))
    assert k == C.KeyRefreshPhaseStatus(0, 0, 2)
    assert (k.ok, k.phase) == (True, 2)
    assert C.decode_key_refresh_phase_status(h("10560403")) == C.KeyRefreshPhaseStatus(
        0x10, 0x456, 3
    )
    with pytest.raises(ValueError, match="need 4 bytes"):
        C.decode_key_refresh_phase_status(h("000000"))


def test_composition_data_page0():
    d = C.decode_composition_data(COMPOSITION_PAGE0)
    assert (d.page, d.cid, d.pid, d.vid, d.crpl, d.features) == (0, 0x0527, 1, 2, 40, 3)
    assert (d.relay, d.proxy, d.friend, d.low_power) == (True, True, False, False)
    assert d.elements == [
        C.CompositionElement(0x0001, [0x0000, 0x1000], [0x05271013]),
        C.CompositionElement(0x0040, [], [0x05271015]),
    ]
    assert [e.model_ids for e in d.elements] == [
        ["0000", "1000", "05271013"],
        ["05271015"],
    ]
    assert (
        C.decode_composition_data(COMPOSITION_PAGE0[:11]).elements == []
    )  # no elements at all


def test_composition_data_rejects_other_pages_and_truncation():
    with pytest.raises(ValueError, match="page 0"):
        C.decode_composition_data(b"\x01" + COMPOSITION_PAGE0[1:])
    with pytest.raises(ValueError, match="need 11 bytes"):
        C.decode_composition_data(COMPOSITION_PAGE0[:10])
    with pytest.raises(ValueError, match="element header"):
        C.decode_composition_data(COMPOSITION_PAGE0[:13])
    with pytest.raises(ValueError, match="model list"):
        C.decode_composition_data(COMPOSITION_PAGE0[:20])


def test_model_app_status():
    s = C.decode_model_app_status(h("00 4901 0000 2705 1510".replace(" ", "")))
    assert s == C.ModelAppStatus(0, BUTTON_A, 0, 0x05271015)
    s = C.decode_model_app_status(h("0d 4801 2301 0010".replace(" ", "")))
    assert (s.status_name, s.element, s.app_key_index, s.model) == (
        "Cannot Bind",
        LIGHT,
        0x123,
        0x1000,
    )
    with pytest.raises(ValueError, match="need 7 bytes"):
        C.decode_model_app_status(h("00490100000110")[:6])
    with pytest.raises(ValueError, match="2 or 4 bytes"):
        C.decode_model_app_status(h("00 4901 0000 270513".replace(" ", "")))


def test_model_publication_status_round_trips_the_set():
    """A Status is the Set's parameters behind a status byte — decode what the builders produce."""
    for kwargs in (
        {},
        {
            "app_key_index": 0x123,
            "credential": True,
            "ttl": 7,
            "period_steps": 30,
            "period_resolution": 1,
            "retransmit_count": 2,
            "retransmit_interval_steps": 5,
        },
    ):
        for model in (0x1001, 0x05271015):
            params = C.model_publication_set(BUTTON_A, GATEWAY_GROUP, model, **kwargs)[
                1:
            ]
            s = C.decode_model_publication_status(b"\x00" + params)
            assert s == C.ModelPublicationStatus(
                0,
                BUTTON_A,
                GATEWAY_GROUP,
                kwargs.get("app_key_index", 0),
                kwargs.get("credential", False),
                kwargs.get("ttl", 0xFF),
                kwargs.get("period_steps", 0),
                kwargs.get("period_resolution", 0),
                kwargs.get("retransmit_count", 0),
                kwargs.get("retransmit_interval_steps", 0),
                model,
            )
    assert (
        C.decode_model_publication_status(
            h("07 4901 05c0 0000 ff 00 00 0110".replace(" ", ""))
        ).status_name
        == "Invalid Publish Parameters"
    )
    with pytest.raises(ValueError, match="need 12 bytes"):
        C.decode_model_publication_status(bytes(11))
    with pytest.raises(ValueError, match="2 or 4 bytes"):
        C.decode_model_publication_status(bytes(13))


def test_model_subscription_status_and_lists():
    s = C.decode_model_subscription_status(h("00 4801 0fc0 0010".replace(" ", "")))
    assert s == C.ModelSubscriptionStatus(0, LIGHT, ROOM_WC, 0x1000)
    s = C.decode_model_subscription_status(h("08 4801 61c0 2705 1310".replace(" ", "")))
    assert (s.status_name, s.address, s.model) == (
        "Not a Subscribe Model",
        ELEMENT_GROUP,
        0x05271013,
    )
    with pytest.raises(ValueError, match="need 7 bytes"):
        C.decode_model_subscription_status(bytes(6))
    sig = C.decode_model_subscription_list(
        C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST,
        h("00 4801 0010 61c0 f5fe 0fc0".replace(" ", "")),
    )
    assert sig == C.ModelSubscriptionList(
        0, LIGHT, 0x1000, [ELEMENT_GROUP, 0xFEF5, ROOM_WC]
    )
    vendor = C.decode_model_subscription_list(
        C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST,
        h("00 4801 2705 1310 61c0".replace(" ", "")),
    )
    assert vendor == C.ModelSubscriptionList(0, LIGHT, 0x05271013, [ELEMENT_GROUP])
    empty = C.decode_model_subscription_list(
        C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST, h("02 4901 0110".replace(" ", ""))
    )
    assert (empty.status_name, empty.addresses) == ("Invalid Model", [])
    with pytest.raises(ValueError, match="not a Model Subscription List"):
        C.decode_model_subscription_list(C.CONFIG_MODEL_SUBSCRIPTION_STATUS, bytes(7))
    with pytest.raises(ValueError, match="need 5 bytes"):
        C.decode_model_subscription_list(C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST, bytes(4))
    with pytest.raises(ValueError, match="odd"):
        C.decode_model_subscription_list(C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST, bytes(6))


def test_single_field_statuses():
    assert C.decode_gatt_proxy_status(b"\x02") == C.GattProxyStatus(
        C.FEATURE_NOT_SUPPORTED
    )
    assert C.decode_default_ttl_status(b"\x05") == C.DefaultTtlStatus(5)
    assert C.decode_relay_status(h("014b")) == C.RelayStatus(1, 3, 9)
    assert C.decode_network_transmit_status(h("53")) == C.NetworkTransmitStatus(3, 10)
    assert C.decode_beacon_status(b"\x00") == C.BeaconStatus(0)
    assert C.decode_node_reset_status(b"") == C.NodeResetStatus()
    for dec in (
        C.decode_gatt_proxy_status,
        C.decode_default_ttl_status,
        C.decode_network_transmit_status,
        C.decode_beacon_status,
    ):
        with pytest.raises(ValueError, match="need 1 bytes"):
            dec(b"")
    with pytest.raises(ValueError, match="need 2 bytes"):
        C.decode_relay_status(b"\x01")


def test_decode_config_dispatches_on_the_opcode():
    assert C.decode_config(C.CONFIG_APPKEY_STATUS, h("00000000")) == C.AppKeyStatus(
        0, 0, 0
    )
    assert isinstance(
        C.decode_config(C.CONFIG_COMPOSITION_DATA_STATUS, COMPOSITION_PAGE0),
        C.CompositionData,
    )
    assert C.decode_config(
        C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST, h("00 4801 0010 0fc0".replace(" ", ""))
    ) == C.ModelSubscriptionList(0, LIGHT, 0x1000, [ROOM_WC])
    assert C.decode_config(
        C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST, h("00 4801 2705 1310".replace(" ", ""))
    ) == C.ModelSubscriptionList(0, LIGHT, 0x05271013, [])
    assert C.decode_config(C.CONFIG_NODE_RESET_STATUS, b"") == C.NodeResetStatus()
    assert C.decode_config(C.CONFIG_NETKEY_STATUS, h("000000")) == C.NetKeyStatus(0, 0)
    assert C.decode_config(
        C.CONFIG_KEY_REFRESH_PHASE_STATUS, h("00000000")
    ) == C.KeyRefreshPhaseStatus(0, 0, 0)
    assert C.decode_config(C.CONFIG_NETKEY_UPDATE, bytes(18)) is None  # a request
    assert (
        C.decode_config(C.CONFIG_MODEL_PUBLICATION_SET, bytes(11)) is None
    )  # a request, not a status
    assert C.decode_config(0x8204, b"\x01") is None  # not a Config opcode at all


# ----------------------------------------------------------------------------- describe_config


@pytest.mark.parametrize(
    ("opcode", "params", "text"),
    [
        (
            0x8019,
            h("00 4901 05c0 0000 ff 00 00 0110".replace(" ", "")),
            "Config Model Publication Status Success: elem=0149 publish=C005 model=1001 appkey=0 cred=0 ttl=255 period=0/0 retx=0/0",
        ),
        (0x8003, h("00000000"), "Config AppKey Status Success: netkey=0 appkey=0"),
        (
            0x801F,
            h("00 4801 0fc0 0010".replace(" ", "")),
            "Config Model Subscription Status Success: elem=0148 address=C00F model=1000",
        ),
        (
            0x803E,
            h("0d 4901 0000 2705 1510".replace(" ", "")),
            "Config Model App Status Cannot Bind: elem=0149 appkey=0 model=05271015",
        ),
        (
            0x802A,
            h("00 4801 0010 61c0 f5fe 0fc0".replace(" ", "")),
            "Config SIG Model Subscription List Success: elem=0148 model=1000 addresses=[C061,FEF5,C00F]",
        ),
        (
            0x802C,
            h("00 4801 2705 1310 61c0".replace(" ", "")),
            "Config Vendor Model Subscription List Success: elem=0148 model=05271013 addresses=[C061]",
        ),
        (0x8014, b"\x01", "Config GATT Proxy Status enabled"),
        (0x8014, b"\x02", "Config GATT Proxy Status not supported"),
        (0x8014, b"\x07", "Config GATT Proxy Status state 7"),
        (0x800E, b"\x05", "Config Default TTL Status ttl=5"),
        (0x8028, h("014b"), "Config Relay Status enabled retx=3/9"),
        (0x8025, h("53"), "Config Network Transmit Status count=3 steps=10"),
        (0x800B, b"\x01", "Config Beacon Status enabled"),
        (0x800B, b"\x00", "Config Beacon Status disabled"),
        (0x804A, b"", "Config Node Reset Status"),
        (
            0x02,
            COMPOSITION_PAGE0,
            "Config Composition Data Status cid=0527 pid=0001 vid=0002 crpl=40 features=0003 elements=[loc=0001 models=0000,1000,05271013; loc=0040 models=05271015]",
        ),
        # requests, as seen on the air from the phone
        (
            0x00,
            h("000000") + SAMPLE_APPKEY,
            "Config AppKey Add netkey=0 appkey=0 key=<16 bytes>",
        ),
        # key refresh: the new keys never show, in any direction
        (
            0x01,
            h("563412") + SAMPLE_APPKEY,
            "Config AppKey Update netkey=1110 appkey=291 key=<16 bytes>",
        ),
        (0x8000, h("563412"), "Config AppKey Delete netkey=1110 appkey=291"),
        (
            0x8040,
            h("0000") + bytes(range(0xA0, 0xB0)),
            "Config NetKey Add netkey=0 key=<16 bytes>",
        ),
        (
            0x8045,
            h("5604") + bytes(range(0xA0, 0xB0)),
            "Config NetKey Update netkey=1110 key=<16 bytes>",
        ),
        (0x8041, h("5604"), "Config NetKey Delete netkey=1110"),
        (0x8044, h("005604"), "Config NetKey Status Success: netkey=1110"),
        (0x8044, h("040000"), "Config NetKey Status Invalid NetKey Index: netkey=0"),
        (0x8015, h("0000"), "Config Key Refresh Phase Get netkey=0"),
        (0x8016, h("000002"), "Config Key Refresh Phase Set netkey=0 transition=2"),
        (
            0x8017,
            h("00000003"),
            "Config Key Refresh Phase Status Success: netkey=0 phase=3",
        ),
        (0x8008, b"\x00", "Config Composition Data Get page=0"),
        (
            0x803D,
            h("4901 0000 2705 1510".replace(" ", "")),
            "Config Model App Bind elem=0149 appkey=0 model=05271015",
        ),
        (
            0x803F,
            h("4801 0000 0010".replace(" ", "")),
            "Config Model App Unbind elem=0148 appkey=0 model=1000",
        ),
        (
            0x03,
            h("4901 05c0 0000 ff 00 00 0110".replace(" ", "")),
            "Config Model Publication Set elem=0149 publish=C005 model=1001 appkey=0 cred=0 ttl=255 period=0/0 retx=0/0",
        ),
        (
            0x8018,
            h("4901 0110".replace(" ", "")),
            "Config Model Publication Get elem=0149 model=1001",
        ),
        (
            0x801B,
            h("4801 0fc0 0010".replace(" ", "")),
            "Config Model Subscription Add elem=0148 address=C00F model=1000",
        ),
        (
            0x801C,
            h("4801 0fc0 0010".replace(" ", "")),
            "Config Model Subscription Delete elem=0148 address=C00F model=1000",
        ),
        (
            0x801E,
            h("4801 0fc0 0010".replace(" ", "")),
            "Config Model Subscription Overwrite elem=0148 address=C00F model=1000",
        ),
        (
            0x801D,
            h("4801 2705 1310".replace(" ", "")),
            "Config Model Subscription Delete All elem=0148 model=05271013",
        ),
        (
            0x8029,
            h("4801 0010".replace(" ", "")),
            "Config SIG Model Subscription Get elem=0148 model=1000",
        ),
        (
            0x802B,
            h("4801 2705 1310".replace(" ", "")),
            "Config Vendor Model Subscription Get elem=0148 model=05271013",
        ),
        (0x8013, b"\x01", "Config GATT Proxy Set enabled"),
        (0x800A, b"\x00", "Config Beacon Set disabled"),
        (0x800D, b"\x05", "Config Default TTL Set ttl=5"),
        (0x8027, h("014b"), "Config Relay Set enabled retx=3/9"),
        (0x8024, h("53"), "Config Network Transmit Set count=3 steps=10"),
        (0x8049, b"", "Config Node Reset"),
        (0x8012, b"", "Config GATT Proxy Get"),
        (0x8009, b"\xaa", "Config Beacon Get aa"),
    ],
)
def test_describe_config(opcode, params, text):
    assert C.describe_config(opcode, params) == text


def test_describe_config_never_raises():
    assert C.describe_config(0x8019, b"\x00") == "Config Model Publication Status ?? 00"
    assert (
        C.describe_config(0x00, b"\x00") == "Config AppKey Add ?? <1 byte>"
    )  # a key-carrying message never dumps its bytes, not even truncated
    assert C.describe_config(0x8045, h("0000") + bytes(10)) == (
        "Config NetKey Update ?? <12 bytes>"
    )
    assert (
        C.describe_config(0x8016, h("0000")) == "Config Key Refresh Phase Set ?? 0000"
    )
    assert C.describe_config(0x8044, b"") == "Config NetKey Status ?? "
    assert C.byte_count(b"") == "<0 bytes>"
    assert C.describe_config(0x803D, bytes(5)) == "Config Model App Bind ?? 0000000000"
    assert (
        C.describe_config(0x8018, bytes(3)) == "Config Model Publication Get ?? 000000"
    )
    assert (
        C.describe_config(0x801B, bytes(5))
        == "Config Model Subscription Add ?? 0000000000"
    )
    assert C.describe_config(0x8013, b"") == "Config GATT Proxy Set ?? "
    assert C.describe_config(0x800D, b"") == "Config Default TTL Set ?? "
    assert (
        C.describe_config(0x02, b"\x01\x02") == "Config Composition Data Status ?? 0102"
    )
    assert (
        C.describe_config(0x8204, b"\x01") == "Config op 8204 01"
    )  # not a Config opcode


def test_describe_config_never_leaks_a_key():
    """Every key-carrying Config message, well-formed or truncated at any length, keeps the key bytes out."""
    key = bytes(range(0xA0, 0xB0))
    for opcode, head in (
        (C.CONFIG_APPKEY_ADD, h("000000")),
        (C.CONFIG_APPKEY_UPDATE, h("000000")),
        (C.CONFIG_NETKEY_ADD, h("0000")),
        (C.CONFIG_NETKEY_UPDATE, h("0000")),
    ):
        params = head + key
        for cut in range(len(params) + 1):
            text = C.describe_config(opcode, params[:cut])
            assert key.hex() not in text
            assert key[:4].hex() not in text, (opcode, cut, text)


# ----------------------------------------------------------------------------- heartbeat publication


def test_heartbeat_publication_builders():
    assert C.heartbeat_publication_get() == h("8038")
    # dst 0D03, 4 beats every 4 s, TTL 5, no feature triggers, NetKey 0 — the probe that proved the firmware beats
    assert C.heartbeat_publication_set(0x0D03, 3, count_log=3) == h(
        "8039 030d 03 03 05 0000 0000"
    )
    assert C.heartbeat_publication_set(0x0D02, 7) == h("8039 020d ff 07 05 0000 0000")
    assert C.heartbeat_publication_set(0, C.HEARTBEAT_PERIOD_OFF, count_log=0) == h(
        "8039 0000 00 00 05 0000 0000"
    )
    assert C.heartbeat_period_seconds(0) == 0
    assert C.heartbeat_period_seconds(1) == 1
    assert C.heartbeat_period_seconds(7) == 64
    for bad in (
        {"destination": 1, "period_log": 0x12},
        {"destination": 1, "period_log": 1, "count_log": 0x12},
        {"destination": 1, "period_log": 1, "ttl": 128},
        {"destination": 1, "period_log": 1, "features": 0x10},
    ):
        with pytest.raises(ValueError, match=r"PeriodLog|CountLog|TTL|features"):
            C.heartbeat_publication_set(**bad)


def test_heartbeat_publication_status_decode_and_describe():
    status = C.decode_heartbeat_publication_status(h("00 030d 03 03 05 0000 0000"))
    assert status == C.HeartbeatPublicationStatus(0, 0x0D03, 3, 3, 5, 0, 0)
    assert status.ok
    assert status.enabled
    assert not C.decode_heartbeat_publication_status(
        h("00 0000 00 00 05 0000 0000")
    ).enabled
    assert not C.decode_heartbeat_publication_status(
        h("00 030d 00 03 05 0000 0000")
    ).enabled  # count exhausted
    assert not C.decode_heartbeat_publication_status(
        h("00 030d ff 00 05 0000 0000")
    ).enabled  # period 0
    assert (
        C.decode_config(
            C.CONFIG_HEARTBEAT_PUBLICATION_STATUS, h("00 030d 03 03 05 0000 0000")
        )
        == status
    )
    assert C.describe_config(
        C.CONFIG_HEARTBEAT_PUBLICATION_STATUS, h("00 030d 03 03 05 0000 0000")
    ) == (
        "Config Heartbeat Publication Status Success: dst=0D03 count_log=3 period_log=3 (4s) ttl=5"
        " features=0000 netkey=0"
    )
    assert C.describe_config(
        C.CONFIG_HEARTBEAT_PUBLICATION_SET, h("020d ff 07 05 0000 0000")
    ) == (
        "Config Heartbeat Publication Set dst=0D02 count_log=255 period_log=7 (64s) ttl=5 features=0000 netkey=0"
    )
    assert (
        C.describe_config(C.CONFIG_HEARTBEAT_PUBLICATION_GET, b"")
        == "Config Heartbeat Publication Get"
    )
    assert C.describe_config(C.CONFIG_HEARTBEAT_PUBLICATION_STATUS, h("00")).startswith(
        "Config Heartbeat Publication Status ??"
    )
    with pytest.raises(ValueError, match="Heartbeat Publication Status"):
        C.decode_heartbeat_publication_status(h("0001"))


def test_heartbeat_subscription_builders_and_status():
    # the hop probe: 0148 counted 01A4's four beats to all-nodes (docs/hidden-features.md §10)
    assert C.heartbeat_subscription_get() == h("803a")
    assert C.heartbeat_subscription_set(0x01A4, 0xFFFF, 5) == h("803b a401 ffff 05")
    assert C.heartbeat_subscription_off() == h("803b 0000 0000 00")
    with pytest.raises(ValueError, match="PeriodLog"):
        C.heartbeat_subscription_set(0x01A4, 0xFFFF, 0x12)
    status = C.decode_heartbeat_subscription_status(h("00 a401 ffff 03 03 01 01"))
    assert status == C.HeartbeatSubscriptionStatus(0, 0x01A4, 0xFFFF, 3, 3, 1, 1)
    assert status.ok
    assert status.active
    assert (
        C.decode_config(
            C.CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS, h("00 a401 ffff 03 03 01 01")
        )
        == status
    )
    # switched off: the node keeps the last count and hops until the next Set, then reads all zero
    assert not C.decode_heartbeat_subscription_status(
        h("00 0000 0000 00 03 01 01")
    ).active
    assert not C.decode_heartbeat_subscription_status(h("00" * 9)).active
    assert C.describe_config(
        C.CONFIG_HEARTBEAT_SUBSCRIPTION_STATUS, h("00 a401 ffff 05 00 7f 00")
    ) == (
        "Config Heartbeat Subscription Status Success: src=01A4 dst=FFFF period_log=5 (16s) count_log=0"
        " hops=127..0"
    )
    assert C.describe_config(
        C.CONFIG_HEARTBEAT_SUBSCRIPTION_SET, h("a401 ffff 05")
    ) == ("Config Heartbeat Subscription Set src=01A4 dst=FFFF period_log=5 (16s)")
    assert (
        C.describe_config(C.CONFIG_HEARTBEAT_SUBSCRIPTION_GET, b"")
        == "Config Heartbeat Subscription Get"
    )
    with pytest.raises(ValueError, match="Heartbeat Subscription Status"):
        C.decode_heartbeat_subscription_status(h("00a401"))
    assert C.describe_config(C.CONFIG_HEARTBEAT_SUBSCRIPTION_SET, h("a401")).startswith(
        "Config Heartbeat Subscription Set ??"
    )


def test_heartbeat_log_helpers():
    assert [C.heartbeat_period_log(s) for s in (0, 1, 2, 3, 4, 10, 64, 65)] == [
        1,
        1,
        2,
        3,
        3,
        5,
        7,
        8,
    ]
    assert C.heartbeat_period_log(10**9) == 0x11
    # the Subscription CountLog (§4.2.18.7): n stands for 2^(n-1) .. 2^n - 1, not the Publication rule (MSG-01)
    assert C.heartbeat_count_range(0) == (0, 0)
    assert C.heartbeat_count_range(1) == (1, 1)
    assert C.heartbeat_count_range(2) == (2, 3)
    assert C.heartbeat_count_range(3) == (4, 7)
    assert C.heartbeat_count_range(4) == (8, 15)
    assert C.heartbeat_count_range(0x10) == (
        0x8000,
        0xFFFE,
    )  # the counter is 16-bit: 0xFFFF is "indefinite", not 65535
    assert C.heartbeat_count_range(0xFF) == (0xFFFF, 0xFFFF)
    with pytest.raises(ValueError, match="prohibited"):
        C.heartbeat_count_range(0x11)


def test_heartbeat_count_range_matches_measured_probe():
    """MSG-01: the hop probe sent 4 beats and the subscriber reported CountLog 3
    (docs/hidden-features.md §10): the range the log stands for must contain what was counted."""
    lo, hi = C.heartbeat_count_range(3)
    assert lo <= 4 <= hi


def test_describe_config_with_devkey_never_dumps_undecoded_bytes():
    """`messages.describe(devkey=True)` routes every Config opcode it names here; the byte-count guard has to
    hold for an opcode this module does not name (Friend Set 0x8010, say) and for a malformed message
    that carries no key, since under a device key any undecoded bytes may be key material."""
    assert (
        C.describe_config(0x8010, h("0000" + "aa" * 16))
        == "Config op 8010 0000" + "aa" * 16
    )
    assert (
        C.describe_config(0x8010, h("0000" + "aa" * 16), devkey=True)
        == "Config op 8010 <18 bytes>"
    )
    assert C.describe_config(0x8010, b"", devkey=True) == "Config op 8010 <0 bytes>"
    assert (
        C.describe_config(0x8016, h("0000")) == "Config Key Refresh Phase Set ?? 0000"
    )
    assert (
        C.describe_config(0x8016, h("0000"), devkey=True)
        == "Config Key Refresh Phase Set ?? <2 bytes>"
    )
    assert (
        C.describe_config(0x00, b"\x00", devkey=True) == "Config AppKey Add ?? <1 byte>"
    )
    assert (
        C.describe_config(0x800E, b"\x05", devkey=True)
        == "Config Default TTL Status ttl=5"
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: C.model_publication_set(0x0100, 0x8123, 0x1000),
        lambda: C.heartbeat_publication_set(0x9000, 5),
        lambda: C.heartbeat_subscription_set(0xC000, 0x0100, 5),
        lambda: C.heartbeat_subscription_set(0x0101, 0x8001, 5),
    ],
)
def test_builders_refuse_prohibited_addresses(build):
    """MSG-07: a virtual publish / heartbeat destination and a group heartbeat source are prohibited (§4.3.2.62,
    §4.3.2.66; a virtual publication needs the Virtual Address Set, not supported); 0x0000 stays accepted."""
    with pytest.raises(ValueError, match=r"virtual|unicast"):
        build()
    assert C.heartbeat_subscription_off() == C.heartbeat_subscription_set(0, 0, 0)


def test_model_app_get_and_list():
    """SIG / Vendor Model App Get and List: the list packs AppKey indexes two per 3 octets."""
    assert C.model_app_get(LIGHT, "1000") == h("804b 4801 0010".replace(" ", ""))
    assert C.model_app_get(LIGHT, 0x05271013) == h(
        "804d 4801 2705 1310".replace(" ", "")
    )
    sig = C.decode_model_app_list(
        C.CONFIG_SIG_MODEL_APP_LIST, h("00 4801 0010 0000".replace(" ", ""))
    )
    assert sig == C.ModelAppList(0, LIGHT, 0x1000, [0])
    # 1 and 2 share three octets (the first in the low 12 bits), 3 is alone in two
    vendor = C.decode_model_app_list(
        C.CONFIG_VENDOR_MODEL_APP_LIST,
        h("00 4801 2705 1310 012000 0300".replace(" ", "")),
    )
    assert vendor == C.ModelAppList(0, LIGHT, 0x05271013, [1, 2, 3])
    refused = C.decode_model_app_list(
        C.CONFIG_SIG_MODEL_APP_LIST, h("02 4901 0110".replace(" ", ""))
    )
    assert (refused.status_name, refused.app_key_indexes) == ("Invalid Model", [])
    with pytest.raises(ValueError, match="not a Model App List"):
        C.decode_model_app_list(C.CONFIG_MODEL_APP_STATUS, bytes(7))
    with pytest.raises(ValueError, match="need 7 bytes"):
        C.decode_model_app_list(C.CONFIG_VENDOR_MODEL_APP_LIST, bytes(6))
    with pytest.raises(ValueError, match="odd key index bytes"):
        C.decode_model_app_list(C.CONFIG_SIG_MODEL_APP_LIST, bytes(6))
    assert C.decode_config(C.CONFIG_SIG_MODEL_APP_LIST, bytes(5)) == C.ModelAppList(
        0, 0, 0, []
    )
    assert C.decode_config(
        C.CONFIG_VENDOR_MODEL_APP_LIST, h("00 4801 2705 1310 0000".replace(" ", ""))
    ) == C.ModelAppList(0, LIGHT, 0x05271013, [0])
    assert C.describe_config(0x804B, h("48010010")) == (
        "Config SIG Model App Get elem=0148 model=1000"
    )
    assert C.describe_config(0x804D, h("480127051310")) == (
        "Config Vendor Model App Get elem=0148 model=05271013"
    )
    assert C.describe_config(0x804E, h("004801270513100120000300")) == (
        "Config Vendor Model App List Success: elem=0148 model=05271013 appkeys=[1,2,3]"
    )
    assert C.describe_config(0x804C, h("004801001000")) == (
        "Config SIG Model App List ?? 004801001000"
    )


# ----------------------------------------------------------------------------- keys held, Node Identity, Friend (F4-15)


def test_key_list_gets_and_lists():
    """NetKey Get / AppKey Get and their lists: indexes packed two per 3 octets, never a key."""
    assert C.netkey_get() == h("8042")
    assert C.appkey_get() == h("80010000")
    assert C.appkey_get(0x456) == h("80015604")
    with pytest.raises(ValueError, match="12-bit"):
        C.appkey_get(0x1000)
    assert C.decode_netkey_list(h("0000")) == C.NetKeyList([0])
    assert C.decode_netkey_list(h("0120000300")) == C.NetKeyList([1, 2, 3])
    with pytest.raises(ValueError, match="need 2 bytes"):
        C.decode_netkey_list(b"\x00")
    with pytest.raises(ValueError, match="odd key index bytes"):
        C.decode_netkey_list(bytes(4))
    assert C.decode_appkey_list(h("00 5604 012000".replace(" ", ""))) == (
        C.AppKeyList(0, 0x456, [1, 2])
    )
    refused = C.decode_appkey_list(h("040100"))
    assert (refused.ok, refused.status_name, refused.app_key_indexes) == (
        False,
        "Invalid NetKey Index",
        [],
    )
    with pytest.raises(ValueError, match="need 3 bytes"):
        C.decode_appkey_list(h("0000"))
    assert C.decode_config(C.CONFIG_NETKEY_LIST, h("0000")) == C.NetKeyList([0])
    assert C.decode_config(C.CONFIG_APPKEY_LIST, h("0000000000")) == C.AppKeyList(
        0, 0, [0]
    )
    assert C.describe_config(0x8042, b"") == "Config NetKey Get"
    assert C.describe_config(0x8001, h("0100")) == "Config AppKey Get netkey=1"
    assert C.describe_config(0x8043, h("0120000300")) == (
        "Config NetKey List netkeys=[1,2,3]"
    )
    assert C.describe_config(0x8002, h("0000000020000300")) == (
        "Config AppKey List Success: netkey=0 appkeys=[0,2,3]"
    )
    assert C.describe_config(0x8002, h("0401")) == "Config AppKey List ?? 0401"


def test_node_identity_get_set_and_status():
    assert C.node_identity_get() == h("80460000")
    assert C.node_identity_get(0x456) == h("80465604")
    assert C.node_identity_set(True) == h("8047000001")
    assert C.node_identity_set(False, 0x456) == h("8047560400")
    with pytest.raises(ValueError, match="12-bit"):
        C.node_identity_set(True, -1)
    assert C.decode_node_identity_status(h("00000001")) == C.NodeIdentityStatus(
        0, 0, C.NODE_IDENTITY_RUNNING
    )
    unsupported = C.decode_config(C.CONFIG_NODE_IDENTITY_STATUS, h("00560402"))
    assert unsupported == C.NodeIdentityStatus(0, 0x456, C.NODE_IDENTITY_NOT_SUPPORTED)
    with pytest.raises(ValueError, match="need 4 bytes"):
        C.decode_node_identity_status(h("000000"))
    assert C.describe_config(0x8046, h("0000")) == "Config Node Identity Get netkey=0"
    assert C.describe_config(0x8047, h("000001")) == (
        "Config Node Identity Set netkey=0 running"
    )
    assert C.describe_config(0x8047, h("0000")) == "Config Node Identity Set ?? 0000"
    assert C.describe_config(0x8048, h("00000000")) == (
        "Config Node Identity Status Success: netkey=0 stopped"
    )
    assert C.describe_config(0x8048, h("04000002")) == (
        "Config Node Identity Status Invalid NetKey Index: netkey=0 not supported"
    )
    assert C.describe_config(0x8048, h("00000007")) == (
        "Config Node Identity Status Success: netkey=0 state 7"
    )


def test_friend_get_and_status():
    assert C.friend_get() == h("800f")
    assert C.decode_friend_status(b"\x02") == C.FriendStatus(C.FEATURE_NOT_SUPPORTED)
    assert C.decode_config(C.CONFIG_FRIEND_STATUS, b"\x00") == C.FriendStatus(0)
    with pytest.raises(ValueError, match="need 1 bytes"):
        C.decode_friend_status(b"")
    assert C.describe_config(0x800F, b"") == "Config Friend Get"
    assert C.describe_config(0x8011, b"\x02") == "Config Friend Status not supported"
    assert (
        C.describe_config(0x8010, b"\x01") == "Config op 8010 01"
    )  # Friend Set: not built


def test_a_models_get_names_the_status_that_answers_it() -> None:
    """The audit, the pre-flight and the CLI build a model's Gets alike: the list status by the model kind."""
    assert C.model_get("publication", 0x0100, "1000") == (
        C.model_publication_get(0x0100, "1000"),
        C.CONFIG_MODEL_PUBLICATION_STATUS,
    )
    assert C.model_get("subscriptions", 0x0100, "1000")[1] == (
        C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST
    )
    assert C.model_get("subscriptions", 0x0100, "05271013") == (
        C.model_subscription_get(0x0100, "05271013"),
        C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST,
    )
    assert C.model_get("app_keys", 0x0100, "1000")[1] == C.CONFIG_SIG_MODEL_APP_LIST
    assert C.model_get("app_keys", 0x0100, 0x05271013)[1] == (
        C.CONFIG_VENDOR_MODEL_APP_LIST
    )
    with pytest.raises(KeyError):
        C.model_get("scenes", 0x0100, "1000")
