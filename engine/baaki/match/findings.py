"""What the engine emits, and the rules about what it is allowed to claim.

A finding is not an opinion. Every one carries the records it was derived from,
so an analyst can check the arithmetic without trusting the engine, and a
dispute raised with the gateway has something attached to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..models import REASON_META, Reason, Severity


class Stage(str, Enum):
    """Which layer produced a finding, in descending order of trust."""

    DETERMINISTIC = "deterministic"
    """Exact joins and recomputed arithmetic. Reproducible, no model."""

    ALGORITHMIC = "algorithmic"
    """Scored matching -- narration parsing, subset sums. Thresholded, no model."""

    LLM = "llm"
    """The residue the first two stages could not close."""


@dataclass(slots=True, frozen=True)
class Evidence:
    """One cited record. ``source`` names the file an analyst would open."""

    source: str
    record_id: str
    field: str
    value: str

    def render(self) -> str:
        return f"{self.source}[{self.record_id}].{self.field} = {self.value}"


@dataclass(slots=True)
class Finding:
    """A single thing that does not add up."""

    reason: Reason
    entity_type: str
    entity_id: str
    impact_paise: int
    stage: Stage
    explanation: str
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 1.0
    requires_human: bool = False

    @property
    def severity(self) -> Severity:
        return REASON_META[self.reason]["severity"]

    @property
    def recoverable(self) -> bool:
        """Whether the impact is money that can actually be clawed back.

        Late settlements and split credits cost working capital and analyst
        time respectively, not principal. Reporting them inside a single
        "money found" headline would inflate it, so the two totals stay apart.
        """
        return REASON_META[self.reason]["recoverable"]

    @property
    def suggested_action(self) -> str:
        return REASON_META[self.reason]["action"]

    @property
    def key(self) -> tuple[str, str]:
        """Identity for deduplication and for scoring against ground truth."""
        return (self.reason.value, self.entity_id)


@dataclass(slots=True)
class Residue:
    """Records the deterministic and algorithmic stages could not resolve.

    This is the handoff to the LLM stage, and it is deliberately explicit. An
    engine that quietly absorbs what it cannot explain is indistinguishable
    from one that got everything right.
    """

    unmatched_settlements: list[str] = field(default_factory=list)
    unmatched_bank_credits: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.unmatched_settlements or self.unmatched_bank_credits or self.ambiguous)

    def size(self) -> int:
        return (
            len(self.unmatched_settlements) + len(self.unmatched_bank_credits) + len(self.ambiguous)
        )
