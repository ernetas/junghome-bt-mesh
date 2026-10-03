# CFG — mesh config writes, config flow, gateway API, TLS

> Fixed and removed from this file: CFG-01, CFG-02, CFG-04, CFG-05, CFG-09, CFG-11, CFG-03, CFG-06, CFG-07, CFG-08, CFG-10, CFG-12, CFG-13, CFG-14, CFG-15. What changed and why is in `10-implementation-log.md`.

## Shard summary

**Counts:** P0: 0 · P1: 4 (CFG-01, CFG-04, CFG-05, CFG-11) · P2: 3 (CFG-02, CFG-06, CFG-13) · P3: 8 (CFG-03, CFG-07, CFG-08, CFG-09, CFG-10, CFG-12, CFG-14, CFG-15).

**Themes:** (1) Apply-and-record after a stopped plan (from the previous fix pass) is right for pub/sub replay but wrong for bookkeeping that lives outside the replayed steps: the link row (CFG-01) and a room created by the call (CFG-02). (2) Gateway sync: the timestamp-only "newer" guard misses meta-only app changes and node deletions (CFG-11) and is skipped when the pre-plan fetch fails (CFG-05). The `/project/cdb` fallback can replace a full export with a meta-less one (CFG-04). CFG-04, CFG-05 and CFG-11 should be fixed together around one persisted "last synced digest".

**Checked and found clean:**
- `ordered()`: additive before destructive; superseded `Publication Set 0x0000` / `Subscription Delete` dropped correctly; stable order keeps App Bind before the same model's Publication Set; no duplicated adds (`ProjectFile.subscribe` is a no-op when present).
- Element/model addressing: Config messages go to `node.unicast` and carry the element address; multi-element nodes (DALI 0232 with keys 0234/0235; 2-channel scene stores on the first Scene Setup Server) are addressed correctly. Subscription Add/Delete are used, never Overwrite/Delete All.
- `ConfigStep.matches`: echo checks for Subscription/Publication/App Status; a late duplicate can't acknowledge the next step. An undecodable status counts as a refusal.
- Publication parameters: the PDU (TTL 0xFF, no period, no retransmit, AppKey 0) matches what `set_publication` records in the CDB.
- Key-mode write/read-back, the property-mode reset ordering (only after all Config steps) and `APPLIED_KEY_WIRED` wording.
- Scene store/remove/delete flows (apart from CFG-03/06/07/08); the sibling-channel register rule.
- Concurrency: `MeshConfigurator.lock` plus `services.ENTRY_LOCKS` (kept across reloads) serialise plans per entry.
- Config flow: incoming-file lifecycle (discarded on every failure, abort and flow removal); `_mesh_uuid_taken` on create; reconfigure mesh-UUID check (see CFG-10 for the unknown case); unique-id update on reconfigure; register progress task (HA cancels it on flow removal); options flow doesn't mutate entry data in place; every error/abort/step/menu key used exists in `strings.json`.
- `gateway_api.py`: every request pinned; mismatch raised before any byte is written; 401 mapping; timeouts on every call; no token or body in logs or exception texts; `api_for_entry` refuses path/upload entries and malformed pins.
- `tls.py`: `aiohttp.Fingerprint` checked right after the handshake; TOFU probe with `PROBE_DIGEST` sends nothing; the per-digest `lru_cache` keeps pool keys stable and can't reuse a connection verified under another pin; no blocking SSL-context creation in the loop (aiohttp's cached unverified context); mesh-read pin wins over the entry's and TOFU.
