"""Frozen, configurable audit rules.

Every threshold lives here so that the rules are inspectable and so that no
number can be quietly changed inside the comparison code. The defaults are kept
fixed for the public demo; they are not re-tuned to the live responses observed
during local testing.
"""

from __future__ import annotations

from dataclasses import dataclass


# The four dimensions that participate in corroboration, in display order.
SCORED_DIMENSIONS = ("genres", "themes", "audience", "style")

# Dimensions shown to the user but never used in corroboration.
#   instruments : near-binary vocabulary ("Vocals" is shared by almost everyone)
#   influenced_by : proper names; often near-zero overlap and treated as display-only
DISPLAY_ONLY_DIMENSIONS = ("instruments", "influenced_by")


@dataclass(frozen=True)
class AuditConfig:
    """All tunable rules for the auditor.

    strong_threshold
        Minimum overlap coefficient for a dimension to count as corroborated.
    weak_threshold
        Strictly-greater-than floor for a dimension to count as weakly
        supported. A dimension scoring exactly 0.0 is `unsupported`.
    min_dimensions_evaluable
        Below this many dimensions with usable data on both sides, the pair is
        `UNVERIFIABLE` regardless of how good the surviving dimensions look.
    min_dimensions_corroborated
        How many dimensions must be corroborated before the pair can be
        `CORROBORATED`.
    """

    strong_threshold: float = 0.30
    weak_threshold: float = 0.0
    min_dimensions_evaluable: int = 2
    min_dimensions_corroborated: int = 2

    def __post_init__(self) -> None:
        if not 0.0 < self.strong_threshold <= 1.0:
            raise ValueError("strong_threshold must be in (0, 1]")
        if not 0.0 <= self.weak_threshold < self.strong_threshold:
            raise ValueError("weak_threshold must be in [0, strong_threshold)")
        if self.min_dimensions_evaluable < 1:
            raise ValueError("min_dimensions_evaluable must be >= 1")
        if not 1 <= self.min_dimensions_corroborated <= len(SCORED_DIMENSIONS):
            raise ValueError("min_dimensions_corroborated out of range")


DEFAULT_CONFIG = AuditConfig()

# Human-readable statements of the rules, emitted next to every verdict so a
# reader never has to trust the source code alone.
STATE_DEFINITIONS = {
    "CORROBORATED": (
        "at least {n_corrob} of the four dimensions independently meet the "
        "overlap threshold of {strong:.2f}"
    ),
    "AFFINITY_ONLY": (
        "Qloo returned an affinity, but fewer than {n_corrob} dimensions meet "
        "the overlap threshold of {strong:.2f}"
    ),
    "UNVERIFIABLE": (
        "the saved metadata is missing, too sparse, or unusable on fewer than "
        "{n_eval} dimensions, so no evidence claim can be made"
    ),
}


def state_definitions(config: AuditConfig) -> dict:
    return {
        state: text.format(
            n_corrob=config.min_dimensions_corroborated,
            n_eval=config.min_dimensions_evaluable,
            strong=config.strong_threshold,
        )
        for state, text in STATE_DEFINITIONS.items()
    }
