"""The mesh configurator's parts behind `mesh_config.MeshConfigurator`.

`plan` — the plan model (`ConfigStep`, `ordered`, `replay`, what a stop applied, `KeyPlan`, `PlanError`); `wiring` —
the modes and models, the wiring read from an export, the export's paths and digest, and the planners; both pure, no
Home Assistant import. `store` — the export, the provisioner identity, the plan journal, dry runs and the gateway's
copy (`ExportStore`); `executor` — sending a plan and recording a stop (`PlanExecutor`); `rooms`, `scenes`,
`thresholds`, `nodes` — the operations. Nothing is imported here, so `plan` and `wiring` load without Home Assistant.
"""
