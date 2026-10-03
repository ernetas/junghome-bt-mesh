# 29 — Mesh 1.1 private beacons and identities on the proxy link, with spec vectors

Phase P1 · Wave 7 · Size S–M · Closes: — (improvements P I-4, P I-5, report 1 brief F).

Follow the [conventions](README.md#conventions) in full (worktree, commit subject naming the finding ids, the
`Claude-Session` trailer, gates, strings in both files, "unverified on air", no key material, no dates or clock
times, CHANGELOG bullet + docs update).

## Goal

Recognise Private Network Identity and Private Node Identity proxy adverts and accept Mesh Private beacons on the
GATT link, so a firmware update that enables Mesh 1.1 privacy does not leave HA blind to IV and key-refresh changes;
pin the private beacon construction to the spec's sample data instead of a circular test.

## Background

`jhmesh` is the mesh library inside `custom_components/junghome_ble`. `classify_service_data`
(`jhmesh/client.py:882-896`) knows identification types 0x00 / 0x01 only; `_parse_beacon` (`client.py:1788-1804`)
ignores beacon type 0x02, so a proxy with Proxy Privacy on gives HA no IV / KR information. The private beacon
(`jhmesh/pdu.py:573-591`, used only by the sniffer) is algebraically the spec's AES-CCM, but its only test
(`tests/jhmesh/test_sniffer.py:154-161`) encodes with the same construction.

## Read first

- `jhmesh/pdu.py:534-591`, `jhmesh/client.py:882-896`, `:1788-1864` (after brief 04 the key-refresh follower lives in
  `jhmesh/keyrefresh.py`), `jhmesh/sniffer.py:650-680`, `jhmesh/crypto.py:89-108`, `tests/jhmesh/test_crypto.py`.
- Mesh Protocol 1.1: §3.10.4 (private beacon), §7.2.2.2.4–.5 (private identities), §8.4.6 / §8.6 (sample data).

## Steps

1. Sample-vector tests for the private beacon and the Private Network / Node Identity hashes, with the vectors copied
   from the specification text (never computed with this code).
2. `NetKeyMaterial.private_network_identity(random)` and `private_node_identity(random, addr)` per the spec.
3. `classify_service_data` accepts types 0x02 and 0x03.
4. `_parse_beacon` also tries `parse_private_beacon` for each RX key and feeds the same IV and key-refresh handling
   (the follower's proof from brief 04 accepts a private beacon under the new key like a secure one).

## Tests to add

- The spec vectors.
- Client: a private Network Identity advert classifies as ours; a private beacon under the current key moves the IV
  state like a secure beacon; one under the new key counts as key-refresh proof.
- 100 % line + branch on the new code.

## Acceptance criteria

Gates green, library job included.

## Verifiable on air here?

Regression only: the installation's firmware does not use privacy as far as known; connect and receive beacons as
before.

## Risks / off-by-default / "unverified on air"

Low: the new branches only run on payload types HA ignores today. Mark private support "unverified on air".

## Depends on

04 (`_parse_beacon` and the follower's proof).

## Files touched

`jhmesh/client.py`, `jhmesh/pdu.py`, `jhmesh/crypto.py`, `tests/jhmesh/test_crypto.py`, `tests/jhmesh/test_client.py`,
`tests/jhmesh/test_pdu.py` (or the existing beacon tests), `CHANGELOG.md`, `docs/ha-integration.md`.
