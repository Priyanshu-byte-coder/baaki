"""Scoring findings against ground truth.

A finding matches a planted defect when the ``(reason, entity_id)`` pair agrees.
Matching on entity alone would let the engine claim credit for spotting that
*something* was wrong with a payment while naming the wrong cause, and the cause
is what determines whether the merchant raises a fee correction or chases a
missing credit.

The headline number is **precision**, not recall. In reconciliation a missed
defect costs an analyst the time to find it by hand; a false positive that is
auto-closed silently writes off real money. The two are not symmetric and the
report does not average them into an F1 that hides which one is failing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..models import REASON_META, InjectedDefect, Reason
from ..match.findings import Finding


@dataclass(slots=True)
class ReasonScore:
    reason: Reason
    planted: int = 0
    found: int = 0
    false_positives: int = 0
    planted_paise: int = 0
    found_paise: int = 0
    impact_exact: int = 0

    @property
    def recall(self) -> float:
        return self.found / self.planted if self.planted else float("nan")

    @property
    def precision(self) -> float:
        reported = self.found + self.false_positives
        return self.found / reported if reported else float("nan")

    @property
    def impact_accuracy(self) -> float:
        """Fraction of matched findings whose rupee impact was exactly right.

        Naming the defect is half the job. An engine that flags the right
        payment but computes the wrong recoverable amount produces a claim the
        gateway will reject.
        """
        return self.impact_exact / self.found if self.found else float("nan")


@dataclass(slots=True)
class Score:
    by_reason: dict[Reason, ReasonScore] = field(default_factory=dict)
    missed: list[InjectedDefect] = field(default_factory=list)
    spurious: list[Finding] = field(default_factory=list)

    @property
    def planted(self) -> int:
        return sum(s.planted for s in self.by_reason.values())

    @property
    def found(self) -> int:
        return sum(s.found for s in self.by_reason.values())

    @property
    def false_positives(self) -> int:
        return sum(s.false_positives for s in self.by_reason.values())

    @property
    def recall(self) -> float:
        return self.found / self.planted if self.planted else float("nan")

    @property
    def precision(self) -> float:
        reported = self.found + self.false_positives
        return self.found / reported if reported else float("nan")

    def money(self, *, recoverable_only: bool = True) -> tuple[int, int]:
        """``(planted, correctly_found)`` rupee impact in paise."""
        planted = found = 0
        for reason, s in self.by_reason.items():
            if recoverable_only and not REASON_META[reason]["recoverable"]:
                continue
            planted += s.planted_paise
            found += s.found_paise
        return planted, found


def score(findings: list[Finding], truth: list[InjectedDefect]) -> Score:
    """Compare an engine run against the injection log."""
    result = Score()
    for reason in Reason:
        result.by_reason[reason] = ReasonScore(reason=reason)

    truth_by_key: dict[tuple[str, str], InjectedDefect] = {}
    collisions: list[tuple[str, str]] = []
    for defect in truth:
        key = (defect.reason.value, defect.entity_id)
        if key in truth_by_key:
            collisions.append(key)
        truth_by_key[key] = defect
        bucket = result.by_reason[defect.reason]
        bucket.planted += 1
        bucket.planted_paise += defect.impact_paise

    if collisions:
        # Refuse to score rather than report a number that cannot be right.
        #
        # ``planted`` counts every defect while matching is keyed on
        # (reason, entity). Two defects sharing a key make the denominator
        # larger than the number of things that can ever be matched, so recall
        # is capped below 100% while the missed list stays empty -- a report
        # that is internally contradictory and still looks plausible. This
        # actually happened: two injectors planted PARTIAL_BANK_CREDIT on one
        # settlement and the run read 99.2% recall with zero misses.
        raise ValueError(
            f"ground truth contains {len(collisions)} duplicate (reason, entity) "
            f"key(s), e.g. {collisions[:3]}. Two injectors planted the same defect "
            f"on the same entity; fix the claim namespacing rather than the scorer."
        )

    # Deduplicate findings first: two reports of the same (reason, entity) are
    # one finding to an analyst, and counting them twice would flatter recall.
    seen: set[tuple[str, str]] = set()
    unique: list[Finding] = []
    for finding in findings:
        if finding.key in seen:
            continue
        seen.add(finding.key)
        unique.append(finding)

    matched: set[tuple[str, str]] = set()
    for finding in unique:
        defect = truth_by_key.get(finding.key)
        bucket = result.by_reason[finding.reason]
        if defect is None:
            bucket.false_positives += 1
            result.spurious.append(finding)
            continue
        matched.add(finding.key)
        bucket.found += 1
        bucket.found_paise += defect.impact_paise
        if finding.impact_paise == defect.impact_paise:
            bucket.impact_exact += 1

    for key, defect in truth_by_key.items():
        if key not in matched:
            result.missed.append(defect)

    return result


def by_stage(findings: list[Finding]) -> dict[str, int]:
    """How many findings each stage produced. The shape of this is the argument.

    A pipeline that resolves almost everything deterministically and calls a
    model only on a thin residue is cheaper, faster and auditable. If the LLM
    row here is large, the design is wrong.
    """
    counts: dict[str, int] = defaultdict(int)
    for finding in findings:
        counts[finding.stage.value] += 1
    return dict(counts)
