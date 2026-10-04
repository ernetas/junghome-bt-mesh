"""The parts of a hub (`coordinator.JungHomeHub`), one component each with its own state (review-4 A4-3).

`liveness` — reachability and heartbeats (`Liveness`, `hub.liveness`). The hub builds its components and delegates
what entities, services, actions, the configurator and the diagnostics call. Nothing is imported here, so a
component loads only what it uses; none imports a platform module.
"""
