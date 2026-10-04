# 76 — Download Home Assistant's export through a signed, short-lived link

Phase P3 · Wave 23 · Size S–M · Closes: U4-17 (signed export download).

Follow the [conventions](README.md#conventions) in full.

## Goal

An administrator can download the export Home Assistant holds — with Home Assistant's own changes carried over — to
re-import it into the JUNG HOME app or keep it as a backup, without shell access to the host, and without the file
(which holds every key of the installation) ever being reachable by an unsigned or lasting URL.

## Background

An entry set up from a file keeps its export on the host (`ExportStore`, `configurator/store.py`, the export watch in
`hub/export_watch.py`); Home Assistant writes its own changes (rooms, scenes, keys, thresholds) into it, and
`ExportStore._carry_over` merges them over a newer app export. The app never receives those changes unless a gateway
entry uploads them. `docs/user/faq.md` warns that the export holds every key. Home Assistant signs a path for a short
time with `homeassistant.components.http.auth.async_sign_path` (the `authSig` query parameter), which an
`HomeAssistantView` with `requires_auth = True` accepts.

## Read first

`configurator/store.py` (`ExportStore`, how the file is read and written, the atomic writer), `mesh_config.py` (the
module docstring on the app and HA's changes), `actions/` (an admin action with a response; `common._run`), the
diagnostics' redaction (`diagnostics.py`: never let key material into a log or a response), Home Assistant's
`http` component (`HomeAssistantView`, `async_sign_path`, `KEY_HASS_USER`), `docs/user/faq.md` and
`docs/user/maintenance.md` on the export file.

## Steps

1. An `HomeAssistantView` at `/api/junghome_ble/export/{entry_id}` (`requires_auth = True`) that serves the entry's
   export as it is on disk (after any pending write is flushed) with `Content-Disposition: attachment` and the file
   name the app uses for its exports, `Cache-Control: no-store`; only for an admin user (403 otherwise); 404 for an
   unknown entry or one without a file export (a gateway entry may serve the export it last fetched, if it keeps one —
   check; otherwise 404 with a clear message).
2. An admin-only action `junghome_ble.download_export` (target: the entry or one of its devices) that answers
   `{"url": <signed path>, "expires_in": 300}` from `async_sign_path` with a 5-minute expiry. Never log the URL; the
   log line says only that a link was made, for whom.
3. No button or other entity: the action is the whole interface (the developer tools or a script call it; the
   response is the link). Document how to call it from *Developer tools → Actions* with *Return response*.
4. Mark the re-import into the app as *unverified with the app* (the `app` marker kind); `docs/on-air-sweep.md` gets an
   item for it (a person imports the downloaded file into the app on a spare phone or after a backup of the app's
   project), cited per the checklist test.
5. Docs: `docs/user/maintenance.md` (download, what the file holds, delete it after use), `docs/user/faq.md` (the
   keys warning points at the link's expiry), `docs/ha-integration.md` (the action and the view); strings in
   `strings.json`, `en.json`, `services.yaml`, `icons.json` and every translation, translated. CHANGELOG under
   `## 1.4.0 (unreleased)` (create above `## 1.3.0` if missing; never edit released sections), *Added*.

## Tests to add

The view: admin gets the bytes with the right headers; non-admin 403; unauthenticated 401; a signed path works and
expires (freeze time); unknown entry 404; a pending write is flushed first. The action: admin only, response shape,
URL never in the log (caplog). The privacy scan stays clean.

## Acceptance criteria

Gates green; nothing logs or returns key material other than the file itself to an authenticated admin.

## Verifiable on air here?

The download, yes (no mesh traffic). The re-import into the app: a person with the app; unverified with the app.
