# 71 — IV Update initiation and the SAR receive acknowledgement timer

Phase P1 · Wave 22 · Size M · Closes: P I-11 (IV Update initiation), P I-8 (SAR receive ack timer).

Follow the [conventions](README.md#conventions) in full.

## Goal

1. Home Assistant can start an IV Update when a sender of its mesh is running out of sequence numbers, as an explicit,
   guarded action, so the mesh does not depend on a JUNG device doing it (which nobody has seen).
2. The segmented-message receiver acknowledges as Mesh Protocol 1.1 §3.5.3.4 (SAR receiver behaviour) specifies,
   with the acknowledgement timer, instead of only on completion.

## Background

On the installation the sender furthest through the sequence space is an ordinary mains node at about 87 % of IV
index 0: every restart (power cut) makes a node skip a persisted block of roughly 180 000–260 000 numbers ahead.
Mesh Protocol §3.10.5 expects a node at risk of exhausting its sequence numbers to start an IV Update; whether JUNG
firmware does is unverified (`Issues.check_sequence_space`, the `sequence_space_low` repair, `docs/on-air-sweep.md`
group F). Home Assistant follows IV Updates (`jhmesh/state.py` `LocalState.apply_beacon`, its timing guards from
brief 09) but never starts one. It reaches the mesh only as a GATT Proxy Client.

## Read first

`jhmesh/state.py` (IV state machine, timing constants, `apply_beacon`, recovery), `jhmesh/client.py` (beacon
handling, transmit IV index, `_on_segment`, `_send_segmented`), `jhmesh/pdu.py` (`parse_beacon`, `segment_ack`,
beacon authentication), `seq_store.py` (what is persisted about the IV state), `hub/issues.py`
(`check_sequence_space`, `check_iv_index`), `actions/` (how an admin action with a confirmation field is built),
brief 09, `docs/ha-integration.md` (IV index sections), `docs/on-air-sweep.md` (group E, group F).

## Steps

1. **Spec check first.** Establish from Mesh Protocol 1.1 (§3.10.5 IV Update procedure, §3.9.3 Secure Network
   beacon, §6.7 Proxy Server behaviour) whether a Proxy Server processes a Secure Network beacon received from a
   Proxy Client as it would one from the advertising bearer (and relays the new IV index to the mesh). Quote the
   section numbers and what they say (paraphrased) in the module docstring. If the spec does not provide this, stop
   after step 6 for the IV part, record a decision in `docs/review-4/plan.md`'s backlog row ("not possible as a proxy
   client: §…") and do only the SAR part.
2. `jhmesh`: `LocalState.start_iv_update()` — allowed only in Normal Operation, at least 96 h after the last IV
   change this state saw (or when no change was ever seen and the IV index is known from a beacon on this link), never
   during a key refresh; moves to IV index + 1 with *IV Update in Progress*, persists before anything is sent. The
   client builds and sends the authenticated Secure Network beacon (IV Update flag set, new IV index) through the
   proxy, and repeats it per the spec's beacon interval while in progress. Transmission uses the old index while in
   progress (the existing rule). The return to Normal Operation follows §3.10.5 (no earlier than 96 h, no later than
   144 h), triggered by the existing beacon handling or Home Assistant's own timer, whichever the spec allows.
3. Home Assistant: an admin-only action `junghome_ble.start_iv_update` with a required `confirm: true`, refused
   (translated error) unless some sender is past `SEQUENCE_SPACE_WARN` (the `sequence_space_low` condition) or
   `force: true` is given; refused while not linked, during a key refresh, or when the 96 h guard fails, each with
   its own translated reason. The response says the new IV index and when Normal Operation is due. The
   `sequence_space_low` repair text mentions the action (every translation updated).
4. Diagnostics: the IV state shows who started the update (Home Assistant or a beacon) and when.
5. `docs/on-air-sweep.md`: an item in group E (cannot be undone: the IV index only goes up) with the steps and what to
   capture; the action and the library function are *unverified on air*.
6. **SAR receiver** (§3.5.3.4): the acknowledgement timer (SAR Acknowledgement Delay Increment / Retransmissions
   count as the spec's defaults, or the node's SAR Receiver state where it is configured), a Segment Acknowledgment
   for segments received so far when it fires, the incomplete timer discarding the message, acknowledgements only for
   unicast destinations, and the "already complete" acknowledgement for a retransmitted segment of a finished
   message. `segment_ack` exists in `pdu.py`.
7. CHANGELOG under `## 1.3.0 (unreleased)` (create it above `## 1.2.0` if missing; never edit released sections):
   *Added* (the action), *Changed* or *Fixed* (SAR). Docs: `docs/ha-integration.md` IV and sequence sections,
   `docs/user/maintenance.md` (the repair's learn-more section).

## Tests to add

`jhmesh` (100 % line and branch): the state machine's guards (96 h, key refresh, unknown IV), the beacon bytes
against a spec sample or a vector built from the spec's algorithm with fixture keys, persistence before send, the
return to Normal Operation; SAR receive: ack timer fires with a partial block ack, incomplete timer, group
destinations unacknowledged, duplicate segment of a completed message. Home Assistant: the action's refusals, the
confirm field, the response; the repair text placeholder set unchanged.

## Acceptance criteria

Gates green (including `jhmesh` 100 % branch coverage); nothing starts an IV Update without the explicit action.

## Verifiable on air here?

The IV Update cannot be undone: group E only, the maintainer's decision. SAR: regression only (a segmented status from
a node exercises the receive path; the capture shows the acknowledgements).
