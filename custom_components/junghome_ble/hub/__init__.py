"""The parts of a hub (`coordinator.JungHomeHub`), one component each with its own state (review-4 A4-3).

- `liveness`: the nodes' reachability and heartbeats (`Liveness`, `hub.liveness`).
- `energy`: the metered loads' readings, counters and polls (`Energy`, `hub.energy`).
- `clock`: the time and location broadcasts (`Clock`, `hub.clock`).
- `export_watch`: the unknown nodes, the gateway's export refresh and its trust (`ExportWatch`, `hub.export_watch`).
- `refresh`: the connect-time reads of every link (`Refresh`, `hub.refresh`).
- `issues`: the repair issues the hub raises and clears, and their fixes (`Issues`, `hub.issues`).
- `link`: the proxy link: its loop, connection, watchdog, keep-alive and grace (`LinkManager`, `hub.link`).
- `lifecycle`: the timers and tasks the hub stops, by name, and the back-off (`Lifecycle`, `hub.lifecycle`).
- `gestures`: the keys' gestures and the event listeners (`ButtonGestures`, `hub.gestures`).

The hub builds its components and delegates what entities, services, actions, the configurator and the diagnostics
call. Nothing is imported here, so a component loads only what it uses; none imports a platform module.
"""
