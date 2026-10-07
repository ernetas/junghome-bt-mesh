# Developer documentation

For people who change the integration or the `jhmesh` library. Using the integration is covered by the
[user guide](../user/README.md); every detail of its behaviour by the [reference](../ha-integration.md).

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | The repository layout, every module of the integration, the `jhmesh` library, the behaviour worth knowing before changing the hub, what is not done yet |
| [Testing](testing.md) | Running the tests, lint and types; the synthetic fixtures, the fake proxy link, the simulated mesh and the soak; the documentation checks |
| [Releases](release.md) | Tagging a version, what the release workflow checks and publishes, PyPI trusted publishing, Renovate |
| [HACS default store](hacs-default-pr.md) | What the HACS default store requires, where each requirement holds, the pull request to `hacs/default` |
| [Research notes](../research/README.md) | How the system was reverse-engineered, the command-line tools, the protocol and app notes |
| [Parity ledger](../parity/README.md) | Every feature of the app and the air, and how this repository covers it |
| [On-air sweep](../on-air-sweep.md) | The checklist for everything still unverified on the maintainer's installation |

## Where documentation goes

- **A change a user notices** (a new entity, action, option, repair, or a change of behaviour): the matching page
  of the [user guide](../user/README.md) in plain words, the [reference](../ha-integration.md) in full detail, and a
  bullet in `CHANGELOG.md` under the unreleased version. Keep the reference's headings as they are: links and the
  repair issues' *learn more* links point at them.
- **A new or renamed entity**: run `.venv/bin/python tools/gen_entity_reference.py` and commit
  `docs/user/entities.md` (`tests/test_docs_reference.py` fails until the page matches; see
  [Testing](testing.md#documentation-checks)).
- **Anything not seen working on a real installation** carries the marker the conventions use for it in its
  docstring, its description string and the docs; `tools/on_air.py` lists every marker and the phrases it knows,
  and the [on-air sweep](../on-air-sweep.md) is how they get checked.
- **A change of the code's structure**: the module table in [Architecture](architecture.md#integration-modules).
- **Never** write key material, real MAC addresses, UUIDs, IP addresses, names of an installation, dates or clock
  times into the documentation.
