# 02 — Key-holding files owner-only; one atomic writer

Phase P0 · Wave 1 · Size S · Closes: D1 (Q4-1 = P4-5), D-low S4-10; folds in P I-10 (CLI device key), S I10.

Follow the [conventions](README.md#conventions) in full.

## Goal

Every file that can hold mesh key material is created with mode 0600 whatever the umask, an existing looser file is
tightened, a test proves it for each file, and all private writes go through one atomic-writer helper.

## Background

Home Assistant custom integration `custom_components/junghome_ble` with the mesh library `jhmesh` and CLI `tools/`.

- The three sequence-number stores (`.storage/junghome_ble.seq.<mesh>`, `.backup`, `.floor`) are built without
  `private=True` (`coordinator.py:546-548`, `:575-577`, `:596-598`), so HA writes them 0644. The vault uses
  `private=True` (`identity.py:137`, `:144`).
- `LocalState.to_stored` puts the new NetKey of a followed key refresh into the record (`jhmesh/client.py:566-568`)
  and `LocalState._write` opens the temp file with `tmp.open("w")` (`client.py:507-512`): umask-default mode, so the CLI
  state file and its `.bak` are world-readable while they hold a key. `export.py` already writes 0600 "whatever the
  umask" (`write_private`, `export.py:265-347`).
- S4-10: `ProjectFile.save` uses temp name `.{name}.{pid}.tmp` without the thread id (`export.py:1598`), unlike
  `write_private` (`:273`) / `write_private_with_backup` (`:330`); `touch` writes `now` even when older than the loaded
  timestamp (`:1503-1507`); `_keep_backup` copies without fsync (`:308-318`).
- The CLI prints a provisioned device key to stdout (`tools/cli_ops.py:880-888`).
- `SECURITY.md:9-13` says the store holds a network key during a key refresh but not its mode.

## Read first

`jhmesh/client.py:490-570`; `jhmesh/export.py:60-70`, `:265-347`, `:1495-1611`; `coordinator.py:520-600`;
`identity.py:130-145`; `tools/cli_ops.py:870-890`; `SECURITY.md`; `tests/jhmesh/test_client_state.py`,
`tests/test_key_refresh.py`, `tests/test_config_flow.py:707`, `tests/test_cli.py:614-630` (existing mode checks).

## Steps

1. One helper in `jhmesh/export.py` (or a small `jhmesh/fileio.py` if importing export from client would cycle):
   `atomic_write(path, data, *, private: bool)` — temp name with pid and thread id, `os.open(…, O_WRONLY|O_CREAT|O_TRUNC,
   0o600)`, write, fsync, `os.replace`, fsync the directory. Route `write_private`, `write_private_with_backup`,
   `ProjectFile.save` and `LocalState._write` (and its `.bak`) through it.
2. `_keep_backup`: fsync the copy. `touch`: `max(now, loaded + 1 ms)`.
3. `LocalState`: on load, `chmod 0o600` a state file or `.bak` that is group/other readable, log once at INFO.
4. Pass `private=True` to the three `SeqStore` constructors; check `SeqStore.written` is still set after a write.
   Leave HA's `.storage` directory mode alone (HA owns it).
5. CLI: write a provisioned device key to a 0600 file and print its path, not the key.
6. `SECURITY.md`: list every key-holding file (export and its backups, `.app` copy, vault, the three seq stores, CLI
   state and `.bak`), what it holds and its mode.

## Tests to add

- One parametrised test over every key-holding file: with umask 022, after a followed key refresh (HA level, fake
  link) and after `async_vault_keeper` writes, `stat.S_IMODE` is 0600 for the seq store, backup, floor, vault, export,
  export `.bak`. A new store added later must be added to the list (comment).
- Library: `LocalState.set_key_refresh` then persist under umask 022 → state file and `.bak` are 0600; a pre-existing
  0644 file is 0600 after load.
- `touch` never goes backwards with a clock set behind the loaded timestamp (patched clock).
- Temp names of two threads differ; the backup copy is fsynced (patch `os.fsync` and count).
- CLI: the device key is not on stdout; the file is 0600.

## Acceptance criteria

Gates green; no file holding a key is 0644 in any test scenario; `jhmesh` stays at 100 % line + branch.

## Verifiable on air here?

Local only (file modes). Optionally inspect `ls -l .storage/junghome_ble.seq.*` on the HA host after deploying.

## Risks / off-by-default / "unverified on air"

`private=True` changes HA's write path (still atomic). The new helper is on the nonce-safety path: keep fsync before
replace and the order of writes unchanged.

## Depends on

None.

## Files touched

`custom_components/junghome_ble/jhmesh/client.py`, `jhmesh/export.py`, `coordinator.py`, `tools/cli_ops.py`,
`SECURITY.md`, tests (`tests/jhmesh/test_client_state.py`, a new `tests/test_file_modes.py`, `tests/test_cli.py`),
`CHANGELOG.md`, `docs/ha-integration.md` (storage section).
