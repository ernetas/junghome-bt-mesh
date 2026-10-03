# CRY — crypto, PDU, advert, sniffer

> Fixed and removed from this file: CRY-01, CRY-02, CRY-03, CRY-04, CRY-05, CRY-06. What changed and why is in `10-implementation-log.md`.

## Shard summary

**Counts:** P0: 0 · P1: 0 · P2: 1 (CRY-01) · P3: 5 (CRY-02 … CRY-06)

**Checked and found clean (no need to re-review):**
- `crypto.py`: s1, k1, k2 (T0 empty, NID = T1[15] & 0x7F), k3 (low 64 bits), k4 (6-bit AID), identity key (`nkik`) and beacon key (`nkbk`) salts, Node Identity hash (6 zero bytes of padding ‖ random ‖ address, low 64 bits). All are pinned by spec §8 sample vectors in `tests/jhmesh/test_crypto.py`. `repr()` of `NetKeyMaterial` / `AppKeyMaterial` hides every key. The only `asdict` user (`diagnostics.py`) doesn't touch key material.
- `pdu.py` network layer: network nonce (0x00, CTL|TTL, SEQ, SRC, 0x0000, IV) and proxy nonce (0x03, 0x00 pad, …), 32/64-bit NetMIC by CTL, PECB = e(PrivacyKey, 5×0x00 ‖ IV ‖ first 7 octets of EncDST‖EncTransport‖NetMIC), IVI-based IV selection with the `iv < 0` guard, minimum length per CTL, the unicast-SRC check, and the unassigned-DST-only-under-proxy-nonce rule (the previous fix pass fixes verified correct). Spec message #1 is round-tripped in tests.
- Upper transport: application/device nonce layout (type, ASZMIC<<7, SEQ, SRC, DST, IV). I checked it byte-for-byte against spec message #6 (device key; `upper_encrypt_dev` gives `ee9dddfd…e0e17308`), and the resulting segments `8026ac01…`/`8026ac21…` match the spec (already asserted in `tests/jhmesh/test_client.py`). `upper_decrypt` returns None (InvalidTag) for every short payload from 0 to 8 bytes, with no crash.
- Lower transport: segmented header bit layout (SZMIC bit 23, SeqZero 22..10, SegO 9..5, SegN 4..0), the SegN < 32 guard, the 12-byte non-last-segment rule, Segment Ack layout (OBO ‖ SeqZero ‖ 2 RFU, 32-bit BlockAck), and `seq_auth_from` (13-bit window, wrap, negative guard). SegO > SegN and SegN-contradiction guards exist in both the client and the sniffer.
- Access opcodes: `encode_opcode` / `decode_opcode` ranges, the RFU 0x7F value, and truncation handling (the previous fix pass is correct).
- Proxy protocol: `proxy_frame` SAR bits, and callers pass `mtu - 3` so frames fit ATT_MTU − 3. Proxy config Set Filter / Add Addresses opcodes. Pre-auth buffer bound (apart from CRY-01).
- Beacons: Secure Network Beacon layout, flags bits, CMAC over Flags‖NetworkID‖IV, constant-time compare.
- `advert.py`: record lengths for types 1/2/3, little-endian fields, reversed MAC, and EUI-64 → MAC extraction.
- `sniffer.py`: pcap magic/endianness/nanosecond handling, Nordic v3 header offsets, CRC-OK flag, AD structure walker bounds, copy detection keyed on (src, seq, ivi), multi-AppKey-per-AID and both-devkey attempts, IV following only on authenticated beacons, and the malformed-PDU catch (every `ValueError` path of `parse_lower` / `seq_auth_from` / `decode_opcode` is caught; no other exception type is reachable from authenticated input). Nothing in `Decoded.text` exposes plaintext of device-key messages (it goes through `AccessMessage.__str__` → `describe(devkey=True)`).
- Nonce reuse: every `network_encrypt` caller in `client.py` draws a fresh SEQ (segment retransmissions included). The proxy nonce type separates proxy-config PDUs from network PDUs that share a SEQ. Upper-transport ciphertext is reused only for the identical plaintext under the identical SeqAuth.
