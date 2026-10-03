"""Property tests (Hypothesis) of the message codecs: what a builder writes, the matching decoder reads back.

Where the library has both directions of a layout — a Config Set and the Status that echoes its fields, a vendor
action and the Scene Action / Scheduler statuses that carry it, Time Set and the text `describe` gives it, the
Location fields — any valid input survives the round trip. Where it only decodes (sensor data, descriptors,
property lists, fault and scene register statuses), the wire layout is built here from the Mesh Model
specification and read back. The describe functions never raise on any bytes and never print a key a
key-carrying Config message holds, however it is cut short.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from jhmesh import config_messages as C
from jhmesh import messages as M
from jhmesh import vendor_models as V
from jhmesh.pdu import decode_opcode, encode_opcode

unicast = st.integers(0x0001, 0x7FFF)
u8 = st.integers(0, 0xFF)
u16 = st.integers(0, 0xFFFF)
key_index = st.integers(0, 0xFFF)
status_code = u8
groups = st.integers(0xC000, 0xFFFB)
sig_models = st.integers(0, 0xFFFF)
vendor_models = st.integers(0x10000, 0xFFFFFFFF)
models = st.one_of(sig_models, vendor_models)
keys16 = st.binary(min_size=16, max_size=16)


def params_of(access: bytes) -> bytes:
    """The parameters of an access PDU (its opcode stripped)."""
    return decode_opcode(access)[2]


# ============================================================================= describe never raises, never leaks keys


@given(access=st.binary(max_size=40), devkey=st.booleans())
def test_describe_takes_any_bytes(access: bytes, devkey: bool) -> None:
    assert isinstance(M.describe(access, devkey=devkey), str)


@given(
    opcode=st.integers(0, 0xFFFF), params=st.binary(max_size=40), devkey=st.booleans()
)
def test_describe_config_takes_any_bytes(
    opcode: int, params: bytes, devkey: bool
) -> None:
    assert isinstance(C.describe_config(opcode, params, devkey=devkey), str)


@given(opcode=st.integers(0, 0xFFFF), params=st.binary(max_size=40))
def test_decode_config_refuses_only_with_value_error(
    opcode: int, params: bytes
) -> None:
    try:
        C.decode_config(opcode, params)
    except ValueError:
        pass


@given(opcode=st.integers(0, 0x3F), params=st.binary(max_size=24))
def test_describe_vendor_model_refuses_only_with_value_error(
    opcode: int, params: bytes
) -> None:
    try:
        V.describe_vendor_model(opcode, params)
    except ValueError:
        pass


def _key_windows(key: bytes) -> set[str]:
    """Every 4-byte run of the key in hex, either case: none of them may appear in a log line."""
    runs = {key[i : i + 4].hex() for i in range(len(key) - 3)}
    return runs | {r.upper() for r in runs}


@given(
    key=keys16,
    build=st.sampled_from(
        ("appkey_add", "appkey_update", "netkey_add", "netkey_update")
    ),
    index=key_index,
    cut=st.integers(0, 40),
    devkey=st.booleans(),
)
def test_describe_never_prints_the_key_of_a_key_carrying_message(
    key: bytes, build: str, index: int, cut: int, devkey: bool
) -> None:
    access = getattr(C, build)(key, index)
    access = access[: max(1, len(access) - cut)] if cut <= len(access) else access[:1]
    op, _cid, params = decode_opcode(access) if len(access) >= 2 else (None, None, b"")
    texts = [M.describe(access, devkey=devkey)]
    if op is not None:
        texts.append(C.describe_config(op, params, devkey=devkey))
    for text in texts:
        assert not any(w in text for w in _key_windows(key)), text


# ============================================================================= config_messages


@given(model=models)
def test_model_id_round_trips(model: int) -> None:
    assert C.decode_model_id(C.encode_model_id(model)) == model
    assert C.model_id(C.model_id_str(model)) == model
    assert C.is_vendor_model(model) == (model > 0xFFFF)


@given(indexes=st.lists(key_index, max_size=12))
def test_key_index_lists_round_trip(indexes: list[int]) -> None:
    packed = b"".join(
        C._pack_key_indexes(indexes[i], indexes[i + 1])
        for i in range(0, len(indexes) - 1, 2)
    )
    if len(indexes) % 2:
        packed += indexes[-1].to_bytes(2, "little")
    assert C._unpack_key_index_list(packed, "test") == indexes


@given(
    key=keys16, app=key_index, net=key_index, status=status_code, update=st.booleans()
)
def test_appkey_add_and_its_status(
    key: bytes, app: int, net: int, status: int, update: bool
) -> None:
    access = (C.appkey_update if update else C.appkey_add)(key, app, net)
    p = params_of(access)
    assert p[3:] == key
    assert C.decode_appkey_status(bytes([status]) + p[:3]) == C.AppKeyStatus(
        status, net, app
    )
    assert C.decode_config(
        C.CONFIG_APPKEY_STATUS, bytes([status]) + params_of(C.appkey_delete(app, net))
    ) == (C.AppKeyStatus(status, net, app))


@given(key=keys16, net=key_index, status=status_code, update=st.booleans())
def test_netkey_add_and_its_status(
    key: bytes, net: int, status: int, update: bool
) -> None:
    p = params_of((C.netkey_update if update else C.netkey_add)(key, net))
    assert p[2:] == key
    assert C.decode_netkey_status(bytes([status]) + p[:2]) == C.NetKeyStatus(
        status, net
    )
    assert params_of(C.netkey_delete(net)) == p[:2]


@given(transition=st.sampled_from((2, 3)), net=key_index, status=status_code)
def test_key_refresh_phase_set_and_its_status(
    transition: int, net: int, status: int
) -> None:
    p = params_of(C.key_refresh_phase_set(transition, net))
    assert C.decode_key_refresh_phase_status(
        bytes([status]) + p
    ) == C.KeyRefreshPhaseStatus(status, net, transition)
    assert params_of(C.key_refresh_phase_get(net)) == p[:2]


@given(
    element=unicast,
    model=models,
    app=key_index,
    status=status_code,
    unbind=st.booleans(),
)
def test_model_app_bind_and_its_status(
    element: int, model: int, app: int, status: int, unbind: bool
) -> None:
    p = params_of(
        (C.model_app_unbind if unbind else C.model_app_bind)(element, model, app)
    )
    assert C.decode_model_app_status(bytes([status]) + p) == C.ModelAppStatus(
        status, element, app, model
    )


@given(
    element=unicast,
    address=st.one_of(st.integers(0, 0x7FFF), st.integers(0xC000, 0xFFFF)),
    model=models,
    app=key_index,
    credential=st.booleans(),
    ttl=st.one_of(st.integers(0, 0x7F), st.just(C.PUBLISH_TTL_DEFAULT)),
    period_steps=st.integers(0, 63),
    period_resolution=st.integers(0, 3),
    retransmit_count=st.integers(0, 7),
    retransmit_steps=st.integers(0, 31),
    status=status_code,
)
def test_model_publication_set_and_its_status(
    element: int,
    address: int,
    model: int,
    app: int,
    credential: bool,
    ttl: int,
    period_steps: int,
    period_resolution: int,
    retransmit_count: int,
    retransmit_steps: int,
    status: int,
) -> None:
    access = C.model_publication_set(
        element,
        address,
        model,
        app_key_index=app,
        credential=credential,
        ttl=ttl,
        period_steps=period_steps,
        period_resolution=period_resolution,
        retransmit_count=retransmit_count,
        retransmit_interval_steps=retransmit_steps,
    )
    assert C.decode_model_publication_status(
        bytes([status]) + params_of(access)
    ) == C.ModelPublicationStatus(
        status,
        element,
        address,
        app,
        credential,
        ttl,
        period_steps,
        period_resolution,
        retransmit_count,
        retransmit_steps,
        model,
    )
    assert params_of(C.model_publication_get(element, model)) == params_of(access)[
        :2
    ] + C.encode_model_id(model)


@given(
    element=unicast,
    group=groups,
    model=models,
    status=status_code,
    build=st.sampled_from(
        (
            "model_subscription_add",
            "model_subscription_delete",
            "model_subscription_overwrite",
        )
    ),
)
def test_model_subscription_changes_and_their_status(
    element: int, group: int, model: int, status: int, build: str
) -> None:
    p = params_of(getattr(C, build)(element, group, model))
    assert C.decode_model_subscription_status(
        bytes([status]) + p
    ) == C.ModelSubscriptionStatus(status, element, group, model)


@given(
    element=unicast,
    model=models,
    status=status_code,
    addresses=st.lists(u16, max_size=10),
)
def test_model_subscription_list(
    element: int, model: int, status: int, addresses: list[int]
) -> None:
    get = C.model_subscription_get(element, model)
    op = (
        C.CONFIG_VENDOR_MODEL_SUBSCRIPTION_LIST
        if C.is_vendor_model(model)
        else C.CONFIG_SIG_MODEL_SUBSCRIPTION_LIST
    )
    wire = (
        bytes([status])
        + params_of(get)
        + b"".join(a.to_bytes(2, "little") for a in addresses)
    )
    assert C.decode_config(op, wire) == C.ModelSubscriptionList(
        status, element, model, addresses
    )


@given(
    element=unicast,
    model=models,
    status=status_code,
    indexes=st.lists(key_index, max_size=10),
)
def test_model_app_list(
    element: int, model: int, status: int, indexes: list[int]
) -> None:
    get = C.model_app_get(element, model)
    op = (
        C.CONFIG_VENDOR_MODEL_APP_LIST
        if C.is_vendor_model(model)
        else C.CONFIG_SIG_MODEL_APP_LIST
    )
    packed = b"".join(
        C._pack_key_indexes(indexes[i], indexes[i + 1])
        for i in range(0, len(indexes) - 1, 2)
    )
    if len(indexes) % 2:
        packed += indexes[-1].to_bytes(2, "little")
    assert C.decode_config(
        op, bytes([status]) + params_of(get) + packed
    ) == C.ModelAppList(status, element, model, indexes)


@given(
    relay=st.booleans(),
    count=st.integers(0, 7),
    steps=st.integers(0, 31),
    beacon=st.booleans(),
    proxy=st.booleans(),
    ttl=st.one_of(st.just(0), st.integers(2, 0x7F)),
)
def test_node_settings_sets_and_their_statuses(
    relay: bool, count: int, steps: int, beacon: bool, proxy: bool, ttl: int
) -> None:
    """Relay, Network Transmit, Beacon, GATT Proxy and Default TTL Status carry exactly the Set's parameters."""
    assert C.decode_relay_status(
        params_of(C.relay_set(relay, count, steps))
    ) == C.RelayStatus(int(relay), count, steps)
    assert C.decode_network_transmit_status(
        params_of(C.network_transmit_set(count, steps))
    ) == (C.NetworkTransmitStatus(count, steps))
    assert C.decode_beacon_status(params_of(C.beacon_set(beacon))) == C.BeaconStatus(
        int(beacon)
    )
    assert C.decode_gatt_proxy_status(
        params_of(C.gatt_proxy_set(proxy))
    ) == C.GattProxyStatus(int(proxy))
    assert C.decode_default_ttl_status(
        params_of(C.default_ttl_set(ttl))
    ) == C.DefaultTtlStatus(ttl)


@given(
    destination=st.one_of(st.integers(0, 0x7FFF), st.integers(0xC000, 0xFFFF)),
    period_log=st.integers(0, 0x11),
    count_log=st.one_of(st.integers(0, 0x11), st.just(C.HEARTBEAT_INDEFINITE)),
    ttl=st.integers(0, 0x7F),
    features=st.integers(0, 0xF),
    net=key_index,
    status=status_code,
)
def test_heartbeat_publication_set_and_its_status(
    destination: int,
    period_log: int,
    count_log: int,
    ttl: int,
    features: int,
    net: int,
    status: int,
) -> None:
    p = params_of(
        C.heartbeat_publication_set(
            destination, period_log, count_log, ttl, features, net
        )
    )
    assert C.decode_heartbeat_publication_status(
        bytes([status]) + p
    ) == C.HeartbeatPublicationStatus(
        status, destination, count_log, period_log, ttl, features, net
    )


@given(
    source=st.one_of(st.just(0), unicast),
    destination=st.one_of(st.integers(0, 0x7FFF), st.integers(0xC000, 0xFFFF)),
    period_log=st.integers(0, 0x11),
    count_log=u8,
    hops=st.tuples(u8, u8),
    status=status_code,
)
def test_heartbeat_subscription_set_and_its_status(
    source: int,
    destination: int,
    period_log: int,
    count_log: int,
    hops: tuple[int, int],
    status: int,
) -> None:
    p = params_of(C.heartbeat_subscription_set(source, destination, period_log))
    wire = bytes([status]) + p + bytes([count_log, *hops])
    assert C.decode_heartbeat_subscription_status(
        wire
    ) == C.HeartbeatSubscriptionStatus(
        status, source, destination, period_log, count_log, *hops
    )


@given(period_log=st.integers(1, 0x11), extra=st.floats(0.001, 1, exclude_max=True))
def test_heartbeat_period_log_inverts_the_period(period_log: int, extra: float) -> None:
    seconds = C.heartbeat_period_seconds(period_log)
    assert C.heartbeat_period_log(seconds) == period_log
    if period_log < 0x11:  # anything longer than one period needs the next one
        assert C.heartbeat_period_log(seconds * (1 + extra)) == period_log + 1


@given(
    header=st.tuples(u16, u16, u16, u16, u16),
    elements=st.lists(
        st.tuples(
            u16, st.lists(sig_models, max_size=6), st.lists(vendor_models, max_size=3)
        ),
        max_size=6,
    ),
)
def test_composition_data_page_0(
    header: tuple[int, int, int, int, int],
    elements: list[tuple[int, list[int], list[int]]],
) -> None:
    wire = b"\x00" + b"".join(v.to_bytes(2, "little") for v in header)
    for loc, sig, vendor in elements:
        wire += loc.to_bytes(2, "little") + bytes([len(sig), len(vendor)])
        wire += b"".join(m.to_bytes(2, "little") for m in sig) + b"".join(
            C.encode_model_id(m) for m in vendor
        )
    assert C.decode_config(C.CONFIG_COMPOSITION_DATA_STATUS, wire) == C.CompositionData(
        0,
        *header,
        [C.CompositionElement(loc, sig, vendor) for loc, sig, vendor in elements],
    )


# ============================================================================= vendor_models


actions = st.one_of(
    st.just(V.NO_ACTION),
    st.builds(lambda on: V.Action(V.ACTION_SWITCH, on=on), st.booleans()),
    st.builds(lambda lx: V.Action(V.ACTION_LIGHTNESS, lightness=lx), u16),
    st.builds(
        lambda lx, k: V.Action(V.ACTION_LIGHTNESS_CT, lightness=lx, temperature_k=k),
        u16,
        u16,
    ),
    st.builds(
        lambda b, s: V.Action(V.ACTION_BLINDS, blind=b, slat=s),
        st.integers(V.LEVEL_MIN, V.LEVEL_MAX),
        st.integers(V.LEVEL_MIN, V.LEVEL_MAX),
    ),
    st.builds(
        lambda centi: V.Action(V.ACTION_TEMPERATURE, temperature_c=centi / 100), u16
    ),
)


@given(action=actions)
def test_action_round_trips(action: V.Action) -> None:
    encoded = action.encode()
    assert len(encoded) == 1 + V.ACTION_PAYLOAD_LENGTH
    assert V.decode_action(encoded) == action
    assert isinstance(action.describe(), str)


@given(scene=st.integers(1, 0xFFFF), action=actions)
def test_scene_action_set_and_its_status(scene: int, action: V.Action) -> None:
    access = V.scene_action_set(scene, action)
    op, cid, p = decode_opcode(access)
    assert (op, cid) == (V.SCENE_ACTION_SETUP_SET, V.JUNG_CID)
    status = V.decode_scene_action_status(p)  # the Status carries the Set's layout
    assert status == V.SceneActionStatus(
        scene, None if action == V.NO_ACTION else action
    )
    assert V.scene_action_reply_to(scene)(p)


@given(
    scenes=st.lists(st.integers(1, 0xFFFF), max_size=20), trailing=st.binary(max_size=6)
)
def test_scene_action_list_status(scenes: list[int], trailing: bytes) -> None:
    wire = (
        b"\x00\x00"
        + b"".join(s.to_bytes(2, "little") for s in scenes)
        + b"\x00\x00"
        + trailing
    )
    assert V.decode_scene_action_status(wire) == V.SceneActionStatus(
        V.SCENE_LIST, scenes=tuple(scenes)
    )


time_of_day = st.one_of(
    st.just(V.UNSET_TIME), st.tuples(st.integers(0, 23), st.integers(0, 59))
)
schedules = st.builds(
    V.Schedule,
    index=st.integers(0, V.SLOTS - 1),
    type=st.sampled_from(sorted(V.SCHEDULE_TYPES)),
    days=st.frozensets(st.sampled_from(V.DAYS)),
    not_before=time_of_day,
    not_after=time_of_day,
    offset_min=st.integers(-128, 127),
)


@given(schedule=schedules)
def test_schedule_set_and_its_status(schedule: V.Schedule) -> None:
    op, cid, p = decode_opcode(schedule.encode())
    assert (op, cid) == (V.JH_SCHEDULER_SET, V.JUNG_CID)
    assert V.decode_scheduler_status(p) == V.SchedulerStatus(
        schedule.index, V.SUB_SCHEDULE, schedule=schedule
    )
    assert isinstance(V.describe_vendor_model(V.JH_SCHEDULER_STATUS, p), str)


@given(index=st.integers(0, V.SLOTS - 1), action=actions)
def test_scheduler_action_set_and_its_status(index: int, action: V.Action) -> None:
    p = params_of(V.scheduler_action_set(index, action))
    assert V.decode_scheduler_status(p) == V.SchedulerStatus(
        index, V.SUB_ACTION, action=action
    )


@given(
    central_id=u8,
    slots=st.lists(st.integers(0, 3), min_size=V.SLOTS, max_size=V.SLOTS),
)
def test_scheduler_list_status(central_id: int, slots: list[int]) -> None:
    get = params_of(V.scheduler_list_get(central_id))
    w = V._Writer().add(get[0], 8, "header").add(central_id, 8, "central id")
    for code in slots:
        w.add(code, 2, "slot")
    assert V.decode_scheduler_status(w.bytes()) == V.SchedulerStatus(
        0, V.SUB_LIST, slots=tuple(slots), central_schedule_id=central_id
    )


@given(
    index=st.integers(0, V.SLOTS - 1),
    schedule_type=st.sampled_from(sorted(V.SCHEDULE_TYPES)),
    days=st.frozensets(st.sampled_from(V.DAYS)),
    at=st.tuples(st.integers(0, 31), st.integers(0, 63)),
    offset=st.integers(-128, 127),
)
def test_effective_time_status(
    index: int,
    schedule_type: int,
    days: frozenset[str],
    at: tuple[int, int],
    offset: int,
) -> None:
    w = V._Writer().add(V._header(index, V.SUB_EFFECTIVE_TIME), 8, "header")
    w.add(schedule_type, 4, "type").add(0, 4, "pad").add(
        V.days_mask(days), 7, "days"
    ).add(0, 1, "pad")
    w.add(at[1], 6, "minute").add(at[0], 5, "hour").add(0, 13, "unused").add(
        offset & 0xFF, 8, "offset"
    )
    status = V.decode_scheduler_status(w.bytes())
    assert status.effective == V.EffectiveTime(index, schedule_type, days, at, offset)


# ============================================================================= messages


@given(
    latitude=st.one_of(st.none(), st.floats(-90, 90)),
    longitude=st.one_of(st.none(), st.floats(-180, 180)),
    altitude=st.one_of(st.none(), st.integers(-32768, M.ALTITUDE_TOO_HIGH - 1)),
    ack=st.booleans(),
)
def test_location_global_round_trips(
    latitude: float | None, longitude: float | None, altitude: int | None, ack: bool
) -> None:
    p = params_of(M.generic_location_global_set(latitude, longitude, altitude, ack=ack))
    lat, lon, alt = M.location_global(p)
    for sent, got, span in ((latitude, lat, 90), (longitude, lon, 180)):
        if sent is None:
            assert got is None
        else:
            assert got is not None
            # the field is floor(degrees / span * (2^31 - 1)): at most one step below what was sent
            assert -1e-9 <= sent - got <= span / 0x7FFFFFFF * (1 + 1e-9) + 1e-9
    assert alt == altitude
    # read back and written again, a location moves by at most one step of the field: the decoded degrees are a
    # float that `floor` may put just below the original field (no caller writes a decoded location back today)
    again = M.location_global_fields(lat, lon, alt)
    sent_fields = M.location_global_fields(latitude, longitude, altitude)
    assert all(0 <= a - b <= 1 for a, b in zip(sent_fields, again, strict=True))


@given(
    when=st.datetimes(
        min_value=datetime(2000, 1, 2),  # noqa: DTZ001  # naive bounds; `timezones` makes each value aware
        max_value=datetime(9998, 12, 30),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
    # what `datetime.timezone` holds: below +24:00 (the field goes to +47:45)
    quarters=st.integers(-64, 95),
    authority=st.booleans(),
    tai_utc_delta=st.integers(-255, 255),
)
def test_time_set_reads_back(
    when: datetime, quarters: int, authority: bool, tai_utc_delta: int
) -> None:
    zone = timedelta(minutes=15 * quarters)
    access = M.time_set(when, zone, tai_utc_delta=tai_utc_delta, authority=authority)
    text = M.describe(access)
    assert text.startswith("Time Set "), text
    iso = text.removeprefix("Time Set ").split(" ", 1)[0]
    try:
        back = datetime.fromisoformat(iso)
    except ValueError:  # pragma: no cover — a failure shows the text itself
        pytest.fail(text)
    assert back.utcoffset() == zone
    # sub-seconds are carried in 1/256 s, truncated
    assert 0 <= (when - back).total_seconds() < 1 / 256
    assert f"authority={int(authority)}" in text
    assert f"tai_utc_delta={tai_utc_delta}" in text


@given(quarters=st.integers(-64, 191), authority=st.booleans())
def test_time_set_reads_back_with_any_zone_the_field_holds(
    quarters: int, authority: bool
) -> None:
    """The whole Zone Offset range (-16:00..+47:45), also what `datetime.timezone` cannot hold (from +24:00)."""
    when = datetime(2026, 1, 1, 12, tzinfo=UTC)
    text = M.describe(
        M.time_set(when, timedelta(minutes=15 * quarters), authority=authority)
    )
    assert text.startswith("Time Set "), text
    stamp = text.removeprefix("Time Set ").split(" ", 1)[0]
    local, sign, offset = stamp[:-6], stamp[-6], stamp[-5:]
    minutes = int(offset[:2]) * 60 + int(offset[3:])
    assert minutes == abs(quarters) * 15
    assert (sign == "-") == (quarters < 0)
    assert datetime.fromisoformat(local) == (
        when + timedelta(minutes=15 * quarters)
    ).replace(tzinfo=None)


def _marshal_sensor(entries: list[tuple[int, bytes, bool]]) -> bytes:
    """Sensor Status marshalling (Mesh Model §4.2.14): Format A when asked and possible, else Format B."""
    out = b""
    for prop, raw, format_a in entries:
        if format_a:
            out += ((prop << 5) | ((len(raw) - 1) << 1)).to_bytes(2, "little")
        else:
            # Length 0x7F: a zero-length value
            length = 0x7F if not raw else len(raw) - 1
            out += bytes([(length << 1) | 1]) + prop.to_bytes(2, "little")
        out += raw
    return out


sensor_entries = st.one_of(
    st.tuples(st.integers(0, 0x7FF), st.binary(min_size=1, max_size=16), st.just(True)),
    st.tuples(u16, st.binary(max_size=127), st.just(False)),
)


@given(entries=st.lists(sensor_entries, max_size=6), cut=st.integers(1, 4))
def test_sensor_values_round_trip_and_refuse_a_truncated_status(
    entries: list[tuple[int, bytes, bool]], cut: int
) -> None:
    wire = _marshal_sensor(entries)
    assert M.sensor_values(wire) == [(prop, raw) for prop, raw, _ in entries]
    assume(entries and len(entries[-1][1]) >= cut)
    with pytest.raises(ValueError, match="truncated"):
        M.sensor_values(wire[:-cut])


@given(data=st.binary(max_size=30))
def test_sensor_values_refuse_only_with_value_error(data: bytes) -> None:
    try:
        M.sensor_values(data)
    except ValueError:
        pass


@given(
    descriptors=st.lists(
        st.tuples(u16, st.integers(0, 0xFFF), st.integers(0, 0xFFF), u8, u8, u8),
        max_size=5,
    )
)
def test_sensor_descriptors_round_trip(
    descriptors: list[tuple[int, int, int, int, int, int]],
) -> None:
    wire = b"".join(
        pid.to_bytes(2, "little")
        + (pos | neg << 12).to_bytes(3, "little")
        + bytes([fn, period, interval])
        for pid, pos, neg, fn, period, interval in descriptors
    )
    assert M.sensor_descriptors(wire) == [M.SensorDescriptor(*d) for d in descriptors]


@given(ids=st.lists(u16, max_size=20), odd=st.booleans())
def test_property_ids_round_trip(ids: list[int], odd: bool) -> None:
    wire = b"".join(i.to_bytes(2, "little") for i in ids) + (b"\x01" if odd else b"")
    assert M.property_ids(wire) == ids


@given(status=u8, current=u16, scenes=st.lists(u16, max_size=16))
def test_scene_register_status_round_trips(
    status: int, current: int, scenes: list[int]
) -> None:
    wire = (
        bytes([status])
        + current.to_bytes(2, "little")
        + b"".join(s.to_bytes(2, "little") for s in scenes)
    )
    assert M.decode_scene_register_status(wire) == M.SceneRegister(
        status, current, tuple(scenes)
    )


@given(test_id=u8, company_id=u16, faults=st.binary(max_size=10), ack=st.booleans())
def test_health_fault_test_and_the_fault_status(
    test_id: int, company_id: int, faults: bytes, ack: bool
) -> None:
    p = params_of(M.health_fault_test(test_id, company_id, ack=ack))
    assert M.decode_health_fault_status(p + faults) == M.HealthFaults(
        test_id, company_id, tuple(faults)
    )
    assert params_of(M.health_fault_get(company_id)) == p[1:]
    assert params_of(M.health_fault_clear(company_id, ack=ack)) == p[1:]


@given(
    level=u8,
    discharge=st.integers(0, 0xFFFFFF),
    charge=st.integers(0, 0xFFFFFF),
    flags=st.tuples(*[st.integers(0, 3)] * 4),
)
def test_battery_status_fields(
    level: int, discharge: int, charge: int, flags: tuple[int, int, int, int]
) -> None:
    presence, indicator, charging, service = flags
    wire = (
        bytes([level])
        + discharge.to_bytes(3, "little")
        + charge.to_bytes(3, "little")
        + bytes([presence | indicator << 2 | charging << 4 | service << 6])
    )
    assert M.battery_status(wire) == {
        "level": level if level <= 100 else None,
        "discharge_minutes": None if discharge == 0xFFFFFF else discharge,
        "charge_minutes": None if charge == 0xFFFFFF else charge,
        "presence": M.BATTERY_PRESENCE[presence],
        "indicator": M.BATTERY_INDICATOR[indicator],
        "charging": M.BATTERY_CHARGING[charging],
        "serviceability": M.BATTERY_SERVICEABILITY[service],
    }


@given(
    kind=st.sampled_from(("admin", "manufacturer", "user")),
    property_id=u16,
    value=st.binary(max_size=8),
    user_access=st.integers(0, 3),
    ack=st.booleans(),
)
def test_vendor_property_set_and_status_layouts(
    kind: str, property_id: int, value: bytes, user_access: int, ack: bool
) -> None:
    """A Status is `[pid][access][value]` for every server; a Set carries the access byte on the admin server only
    (what `vendor_property_status` writes, the gateway's LED trick, is exactly what a node answers)."""
    op, cid, p = decode_opcode(
        M.vendor_property_set(
            kind, property_id, value, ack=ack, user_access=user_access
        )
    )
    assert cid == M.JUNG_CID
    assert op == M.VENDOR_PROPERTY_SET_OPCODES[kind][0 if ack else 1]
    head = property_id.to_bytes(2, "little") + (
        bytes([user_access]) if kind == "admin" else b""
    )
    assert p == head + value
    op, cid, p = decode_opcode(
        M.vendor_property_status(kind, property_id, value, user_access=user_access)
    )
    assert (op, cid) == (M.VENDOR_PROPERTY_STATUS_OPCODES[kind], M.JUNG_CID)
    assert p == property_id.to_bytes(2, "little") + bytes([user_access]) + value
    assert params_of(M.vendor_property_get(kind, property_id)) == property_id.to_bytes(
        2, "little"
    )


@given(scene=st.integers(1, 0xFFFF), ack=st.booleans())
def test_scene_builders_carry_the_scene_number(scene: int, ack: bool) -> None:
    for access in (M.scene_store(scene, ack=ack), M.scene_delete(scene, ack=ack)):
        assert params_of(access) == scene.to_bytes(2, "little")
    assert (
        params_of(M.scene_recall(scene, ack=ack, tid=7))[:3]
        == scene.to_bytes(2, "little") + b"\x07"
    )
    assert encode_opcode(decode_opcode(M.scene_get())[0]) == M.scene_get()


@given(zone_minutes=st.integers(-24 * 60, 48 * 60))
def test_time_set_refuses_what_the_zone_field_cannot_carry(zone_minutes: int) -> None:
    zone = timedelta(minutes=zone_minutes)
    when = datetime(
        2026, 1, 1, tzinfo=timezone(zone) if abs(zone) < timedelta(hours=24) else UTC
    )
    fits = zone_minutes % 15 == 0 and -64 <= zone_minutes // 15 <= 191
    if fits:
        assert M.time_set(when, zone)[-1] == zone_minutes // 15 + 64
    else:
        with pytest.raises(ValueError, match="zone offset"):
            M.time_set(when, zone)
