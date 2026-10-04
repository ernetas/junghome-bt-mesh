"""The parts of a hub (`coordinator.JungHomeHub`), one component each with its own state (review-4 A4-3).

- `liveness`: the nodes' reachability and heartbeats (`Liveness`, `hub.liveness`).
- `energy`: the metered loads' readings, counters and polls (`Energy`, `hub.energy`).
- `clock`: the time and location broadcasts (`Clock`, `hub.clock`).
- `export_watch`: the unknown nodes, the gateway's export refresh and its trust (`ExportWatch`, `hub.export_watch`).

The hub builds its components and delegates what entities, services, actions, the configurator and the diagnostics
call. Nothing is imported here, so a component loads only what it uses; none imports a platform module.
"""
