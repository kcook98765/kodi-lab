"""Canonical closed Bridge capability sets for the current ORM schema."""

import itertools
import json


def _sets(*, reviewed: bool) -> str:
    values = ("log_read", "screenshot", "ui_observation", "workspace_deployment")
    return (
        "("
        + ",".join(
            "'" + json.dumps(subset, separators=(",", ":")) + "'"
            for count in range(len(values) + 1)
            for subset in itertools.combinations(values, count)
            if not reviewed or "screenshot" in subset
        )
        + ")"
    )


BRIDGE_CAPABILITY_SETS_SQL = _sets(reviewed=False)
REVIEWED_BRIDGE_CAPABILITY_SETS_SQL = _sets(reviewed=True)
