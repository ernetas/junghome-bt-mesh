# 75 — Mesh topology as an image entity

Phase P3 · Wave 23 · Size M · Closes: U4-14 (topology card).

Follow the [conventions](README.md#conventions) in full.

## Goal

The owner sees the mesh at a glance: which node Home Assistant is connected through, how far every other node is
(heartbeat hops), which nodes relay and which are proxies, which are unreachable — as a picture on a dashboard, with no
custom frontend card to install.

## Background

The integration already knows, per node: the hops of its last heartbeat (`hub/liveness.py`, the diagnostics'
heartbeat section), when it was last heard, whether it is reachable, its features from the export (relay, proxy,
friend, low power), its name and room; and the proxy node of the current link (`hub/link.py`). The *Mesh overview*
(brief 45) shows counts and lists, not the shape. A Lovelace card needs a JavaScript resource the user installs; an
`image` entity (`homeassistant.components.image.ImageEntity`) shows on any dashboard with the stock picture card.

## Read first

`hub/liveness.py` (heartbeats, hops, reachability), `hub/link.py` (the current proxy), `diagnostics.py`
(`_heartbeats` and the link history), `sensor.py` (the mesh-network device's diagnostics, how they update), the
mesh-network device (`device_info.py`), brief 45 and its *Mesh overview*, Home Assistant's `image` platform
(`ImageEntity`, `image_last_updated`, `async_image`, `content_type`).

## Steps

1. A pure function (new module, no Home Assistant import) that turns a topology snapshot — nodes with name, room,
   features, hops (or unknown), reachable, last heard; the proxy of the link; Home Assistant itself — into an SVG:
   Home Assistant at the centre or top, the proxy node next to it, every other node placed in rings or rows by hop
   count, unknown hops apart; reachable / unreachable / proxy / relay told apart by shape and colour **and** by a
   text label or legend (not colour alone); node names escaped; a stable layout (sorted, so the picture does not jump
   between updates). Light and dark readable (neutral background with a border, or `prefers-color-scheme` in the
   SVG). Bounded size for 60+ nodes.
2. An `image` entity *Mesh topology* on the mesh-network device, entity category *diagnostic*, enabled by default
   (decision M9 applies only to config entities). `content_type` `image/svg+xml`; `image_last_updated` moves only
   when the snapshot changes (debounced, at most once a minute), not on every heartbeat.
3. The same snapshot in the diagnostics (`topology`), so a downloaded file shows what the picture shows.
4. Docs: `docs/user/` (where the health of the mesh is described: how to put the picture on a dashboard with the
   stock picture-entity card), `docs/ha-integration.md` (the entity table), `docs/user/entities.md` through its
   generator (`tools/gen_entity_reference.py`; never by hand). Name and any state strings in `strings.json`,
   `en.json`, `icons.json` and every other `translations/*.json`, translated. CHANGELOG under
   `## 1.4.0 (unreleased)` (create above `## 1.3.0`; never edit released sections), *Added*.

## Tests to add

The SVG function: golden files for a small mesh (proxy, relays, an unreachable node, unknown hops), escaping of a name
with `<&">`, stable output for a shuffled input, well-formed XML, size bound with 80 nodes. The entity: created on the
mesh-network device, `async_image` returns the SVG, `image_last_updated` changes only when the snapshot changes,
debounce; diagnostics include `topology`. Snapshot updates reviewed.

## Acceptance criteria

Gates green; translations complete; the picture readable in the golden files (look at them).

## Verifiable on air here?

Yes, by looking: the picture should match the installation (the proxy node of the link, the nodes' hop counts in the
diagnostics). Unverified on air until then.
