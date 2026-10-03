"""Hypothesis settings profiles for the property tests, loaded by both conftests (`tests/` and `tests/jhmesh/`).

Home-Assistant-free, so the library job (which only loads `tests/jhmesh/conftest.py`) and the integration run pick
the same profile. `HYPOTHESIS_PROFILE` chooses one; without it, `ci` when the `CI` variable is set (GitHub Actions
sets it), `dev` otherwise:

- `ci`: a modest number of examples per property (the suite's run time stays in seconds) and no example database
  (nothing written into the checkout), with the reproduction blob printed on a failure;
- `dev`: the same budget, with the example database in `.hypothesis/` (git-ignored), so a failure found once
  is tried first on every later run;
- `thorough`: many more examples, for a run by hand after a change to the transport or the sequence store.

No deadline anywhere: the examples do AES-CCM, JSON round trips of a whole export or a state machine's worth of
file writes, and a slow runner must not turn that into a flaky "deadline exceeded".
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

_COMMON = {
    "deadline": None,
    # the state machines and the export round trips generate big inputs on purpose
    "suppress_health_check": (HealthCheck.too_slow, HealthCheck.data_too_large),
}

settings.register_profile(
    "ci", max_examples=60, database=None, print_blob=True, **_COMMON
)
settings.register_profile("dev", max_examples=60, **_COMMON)
settings.register_profile("thorough", max_examples=2000, **_COMMON)


def name() -> str:
    """The profile `HYPOTHESIS_PROFILE` names, else `ci` under CI and `dev` elsewhere."""
    return os.environ.get("HYPOTHESIS_PROFILE") or (
        "ci" if os.environ.get("CI") else "dev"
    )


def load() -> None:
    """Load the profile `name()` picks."""
    settings.load_profile(name())
