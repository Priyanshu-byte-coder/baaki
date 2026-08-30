"""Did the money actually come back?

This is the step that turns a report into a loop. A claim is not recovered
because we filed it, and not because the gateway said it would be. It is
recovered when an adjustment line carrying the money is found in a later
settlement -- the same standard the rest of the engine holds itself to.

Two passes, in descending order of certainty, exactly as in bank matching:

    A  the adjustment quotes the original entity in its reference
    B  the amount matches one outstanding claim, uniquely, in the window

Uniqueness is enforced both ways on pass B. Two claims of the same value with
one adjustment between them is not a coin flip to be won; it is left open and
reported, because marking the wrong claim recovered closes a real loss.

Claims that have waited past the chase limit are written off explicitly. An
item aging out is a decision that gets recorded, not one that quietly stops
being counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..models import Corpus, EntityType
from ..money import rupees
from .claims import Claim, ClaimState, Ledger

#: How long a filed claim is chased before it is written off. Roughly two
#: settlement cycles plus slack: past that, the money is not coming and the
#: open item is only making the aging report look worse than it is.
CHASE_LIMIT_DAYS = 75

#: Tolerance when matching an adjustment to a claim by value, in paise. Zero,
#: for the same reason every other tolerance in this project is zero.
VALUE_TOLERANCE_PAISE = 0


@dataclass(slots=True)
class VerifyReport:
    matched_by_reference: int = 0
    matched_by_value: int = 0
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)
    unexplained_adjustments: list[str] = field(default_factory=list)
    written_off: list[str] = field(default_factory=list)
    recovered_paise: int = 0
    partial: int = 0

    @property
    def matched(self) -> int:
        return self.matched_by_reference + self.matched_by_value


def _adjustments(corpus: Corpus) -> dict[str, int]:
    """Every adjustment line in the period, as ``entity_id -> paise``."""
    out: dict[str, int] = {}
    for row in corpus.settlement_rows:
        if row.entity_type is EntityType.ADJUSTMENT and row.net_paise > 0:
            out[row.entity_id] = out.get(row.entity_id, 0) + row.net_paise
    return out


def _settle(claim: Claim, amount: int, note: str, on: date) -> bool:
    """Apply a repayment. Returns True when the claim is now fully recovered."""
    claim.recovered_paise += amount
    full = claim.recovered_paise >= claim.claimed_paise
    claim.transition(
        ClaimState.RECOVERED if full else ClaimState.PARTIAL,
        note,
        amount_paise=amount,
        on=on,
    )
    return full


def verify(ledger: Ledger, corpus: Corpus, *, on: date) -> VerifyReport:
    """Reconcile filed claims against the adjustments in a later period."""
    report = VerifyReport()
    outstanding = [
        c
        for c in ledger.claims.values()
        if c.state in (ClaimState.FILED.value, ClaimState.PARTIAL.value)
    ]
    if not outstanding:
        return report

    open_adjustments = _adjustments(corpus)

    # -- pass A: the adjustment names what it is repaying --------------------
    for claim in list(outstanding):
        hit = next(
            (
                entity
                for entity in open_adjustments
                if claim.entity_id and claim.entity_id in entity
            ),
            None,
        )
        if hit is None:
            continue
        amount = open_adjustments.pop(hit)
        full = _settle(
            claim,
            amount,
            f"Repaid {rupees(amount)} by adjustment {hit}, which quotes "
            f"{claim.entity_id} in its reference.",
            on,
        )
        report.matched_by_reference += 1
        report.recovered_paise += amount
        if not full:
            report.partial += 1
        outstanding.remove(claim)

    # -- pass B: value, uniquely, in both directions --------------------------
    for claim in list(outstanding):
        wanted = claim.outstanding_paise
        candidates = [
            entity
            for entity, amount in open_adjustments.items()
            if abs(amount - wanted) <= VALUE_TOLERANCE_PAISE
        ]
        if not candidates:
            continue
        if len(candidates) > 1:
            report.ambiguous.append((claim.claim_id, candidates))
            continue

        rivals = [
            other.claim_id
            for other in outstanding
            if other is not claim and other.outstanding_paise == wanted
        ]
        if rivals:
            # One adjustment, several claims it could be repaying. Guessing here
            # marks one real loss closed and leaves another open forever.
            report.ambiguous.append((claim.claim_id, candidates))
            continue

        entity = candidates[0]
        amount = open_adjustments.pop(entity)
        full = _settle(
            claim,
            amount,
            f"Repaid {rupees(amount)} by adjustment {entity}, matched on value "
            f"with no other candidate in the period.",
            on,
        )
        report.matched_by_value += 1
        report.recovered_paise += amount
        if not full:
            report.partial += 1
        outstanding.remove(claim)

    # -- what is left --------------------------------------------------------
    report.unexplained_adjustments = sorted(open_adjustments)

    for claim in outstanding:
        if claim.age_days(on) > CHASE_LIMIT_DAYS:
            claim.transition(
                ClaimState.WRITTEN_OFF,
                f"No repayment {claim.age_days(on)} days after opening, past the "
                f"{CHASE_LIMIT_DAYS}-day chase limit.",
                on=on,
            )
            report.written_off.append(claim.claim_id)

    return report


def score_recovery(cycle, ledger: Ledger) -> dict:
    """Score the verifier against what the generator actually repaid.

    The verifier never sees ``cycle.repaid``. This asks the only question that
    matters about a recovery loop: of the money that genuinely came back, how
    much did we correctly attribute -- and did we ever mark something recovered
    that was not?
    """
    truth = cycle.repaid
    truth_total = sum(truth.values())

    found = 0
    correct = 0
    false_positive = 0
    missed: list[str] = []

    for claim_id, repaid in truth.items():
        claim = ledger.claims.get(claim_id)
        if claim is None:
            missed.append(claim_id)
            continue
        if claim.recovered_paise > 0:
            correct += 1
            found += min(claim.recovered_paise, repaid)
        else:
            missed.append(claim_id)

    for claim in ledger.claims.values():
        if claim.recovered_paise > 0 and claim.claim_id not in truth:
            false_positive += 1

    return {
        "repaid_claims": len(truth),
        "repaid_paise": truth_total,
        "detected_claims": correct,
        "detected_paise": found,
        "missed_claims": len(missed),
        "false_positives": false_positive,
        "detection_rate": (correct / len(truth)) if truth else 1.0,
        "value_rate": (found / truth_total) if truth_total else 1.0,
    }
