"""What the tail stage is allowed to do, and how its output is checked.

The design rule for the whole project, stated once, here:

    **An LLM never decides a match of record.**

It may read the residue the deterministic and algorithmic stages could not
close, and it may propose an explanation. Whether that proposal becomes a
finding is decided by arithmetic and set membership, not by the model's
confidence in itself. When a proposed match is accepted, it is accepted because
the credits sum to the settlement net -- the model only suggested which credits
to add up.

Every proposal runs a fixed battery:

===========================  ==================================================
``SCHEMA_INVALID``           The object is missing fields or has wrong types.
``UNKNOWN_REASON``           A reason code outside the taxonomy was invented.
``UNGROUNDED_ENTITY``        A record was cited that was not in the prompt.
``ARITHMETIC_FAILED``        The proposed credits do not sum to the settlement.
``NO_EVIDENCE``              A claim with nothing cited behind it.
``EXCEEDS_AUTONOMY``         Rupee value above the ceiling for an automatic call.
``LOW_CONFIDENCE``           Below the threshold once *we* have scored it.
===========================  ==================================================

The model's self-reported confidence is read and then discarded. A model asked
how sure it is will answer fluently and without calibration, and in a system
that moves money that number is worse than no number, because it looks like
evidence. Confidence here is computed from which checks passed.

The grounding and self-check approach is carried over from the guardrail layer
of my HelioOps project, where the lesson was that a citation which does not
resolve to something actually retrieved is the single best hallucination
signal available.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from ..models import Corpus, Reason
from ..money import rupees
from .findings import Evidence, Finding, Stage


class Flag(str, enum.Enum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    UNKNOWN_REASON = "UNKNOWN_REASON"
    UNGROUNDED_ENTITY = "UNGROUNDED_ENTITY"
    ARITHMETIC_FAILED = "ARITHMETIC_FAILED"
    NO_EVIDENCE = "NO_EVIDENCE"
    EXCEEDS_AUTONOMY = "EXCEEDS_AUTONOMY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


class Verdict(str, enum.Enum):
    ACCEPT = "accept"
    """Checks passed and the value is inside the autonomy ceiling."""

    REVIEW = "review"
    """Sound, but a human signs it off before it counts as resolved."""

    REJECT = "reject"
    """Failed a hard check. Discarded, and the residue stays unresolved."""


#: Rupee ceiling for an automatic call, in paise. Above this a proposal is
#: sound but not autonomous: a person signs it off. Reconciliation findings
#: turn into claims against a gateway and entries in a ledger, and the cost of
#: being confidently wrong scales with the amount while the cost of a signature
#: does not.
AUTONOMY_CEILING_PAISE = 25_000_00

#: Confidence below which a proposal is escalated regardless of value.
MIN_CONFIDENCE = 0.55

#: Hard checks. Any of these fails and the proposal is discarded outright.
FATAL = frozenset(
    {Flag.SCHEMA_INVALID, Flag.UNKNOWN_REASON, Flag.UNGROUNDED_ENTITY, Flag.ARITHMETIC_FAILED}
)


@dataclass(slots=True)
class Proposal:
    """One suggestion from the tail stage, before it has been checked."""

    reason: str
    entity_type: str
    entity_id: str
    explanation: str
    cited_ids: list[str] = field(default_factory=list)
    proposed_credit_ids: list[str] = field(default_factory=list)
    impact_paise: int = 0
    model_confidence: float = 0.0


@dataclass(slots=True)
class Checked:
    proposal: Proposal
    verdict: Verdict
    flags: list[Flag]
    confidence: float
    note: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict is not Verdict.REJECT


class Scope:
    """The set of records the model was shown, and may therefore talk about.

    Grounding is checked against the *prompt scope*, not against the whole
    corpus. A model that cites a real settlement it was never shown has still
    hallucinated -- it guessed an identifier that happened to exist, and on a
    50,000-record book plausible-looking identifiers are cheap to guess.
    """

    def __init__(self, corpus: Corpus, entity_ids: list[str], credit_ids: list[str]) -> None:
        self.entity_ids = set(entity_ids)
        self.credit_ids = set(credit_ids)
        self.allowed = self.entity_ids | self.credit_ids
        self.settlement_net = {
            s.settlement_id: s.net_paise
            for s in corpus.settlements
            if s.settlement_id in self.entity_ids
        }
        self.credit_value = {
            b.bank_txn_id: b.credit_paise
            for b in corpus.bank_txns
            if b.bank_txn_id in self.credit_ids
        }


def check(proposal: Proposal, scope: Scope) -> Checked:
    """Run the battery. Returns the verdict and the flags behind it."""
    flags: list[Flag] = []

    if not proposal.entity_id or not proposal.explanation.strip():
        flags.append(Flag.SCHEMA_INVALID)

    try:
        reason = Reason(proposal.reason)
    except ValueError:
        reason = None
        flags.append(Flag.UNKNOWN_REASON)

    cited = [c for c in proposal.cited_ids if c]
    if not cited:
        flags.append(Flag.NO_EVIDENCE)

    ungrounded = [c for c in cited + [proposal.entity_id] if c and c not in scope.allowed]
    ungrounded += [c for c in proposal.proposed_credit_ids if c not in scope.credit_ids]
    if ungrounded:
        flags.append(Flag.UNGROUNDED_ENTITY)

    # The substance of the whole module. A proposed match is accepted because
    # the money adds up, never because the model was sure.
    if proposal.proposed_credit_ids and Flag.UNGROUNDED_ENTITY not in flags:
        expected = scope.settlement_net.get(proposal.entity_id)
        if expected is None:
            flags.append(Flag.ARITHMETIC_FAILED)
        else:
            total = sum(scope.credit_value[c] for c in proposal.proposed_credit_ids)
            if total != expected:
                flags.append(Flag.ARITHMETIC_FAILED)

    confidence = _score(flags, proposal)
    if confidence < MIN_CONFIDENCE:
        flags.append(Flag.LOW_CONFIDENCE)
    if abs(proposal.impact_paise) > AUTONOMY_CEILING_PAISE:
        flags.append(Flag.EXCEEDS_AUTONOMY)

    if any(f in FATAL for f in flags):
        verdict = Verdict.REJECT
    elif Flag.EXCEEDS_AUTONOMY in flags or Flag.LOW_CONFIDENCE in flags:
        verdict = Verdict.REVIEW
    else:
        verdict = Verdict.ACCEPT

    return Checked(
        proposal=proposal,
        verdict=verdict,
        flags=flags,
        confidence=confidence,
        note=_note(flags, proposal, reason),
    )


def _score(flags: list[Flag], proposal: Proposal) -> float:
    """Confidence computed from checks passed, not from what the model claimed.

    Starts low on purpose. A tail proposal is the least trustworthy thing in
    the pipeline and has to earn its way up by citing records that exist and,
    where it proposes a match, by the money agreeing.
    """
    if any(f in FATAL for f in flags):
        return 0.0

    score = 0.40
    if proposal.cited_ids:
        score += 0.10 + min(0.15, 0.05 * len(proposal.cited_ids))
    if proposal.proposed_credit_ids:
        # Reaching here means the arithmetic check already passed.
        score += 0.30
    if len(proposal.explanation.split()) >= 12:
        score += 0.05
    return round(min(1.0, score), 4)


def _note(flags: list[Flag], proposal: Proposal, reason: Reason | None) -> str:
    if not flags:
        return "All checks passed."
    parts = []
    if Flag.UNGROUNDED_ENTITY in flags:
        parts.append("cites records outside the scope it was shown")
    if Flag.ARITHMETIC_FAILED in flags:
        parts.append("proposed credits do not sum to the settlement net")
    if Flag.UNKNOWN_REASON in flags:
        parts.append(f"reason code {proposal.reason!r} is not in the taxonomy")
    if Flag.NO_EVIDENCE in flags:
        parts.append("no records cited")
    if Flag.SCHEMA_INVALID in flags:
        parts.append("malformed proposal")
    if Flag.EXCEEDS_AUTONOMY in flags:
        parts.append(
            f"{rupees(abs(proposal.impact_paise))} is above the "
            f"{rupees(AUTONOMY_CEILING_PAISE)} ceiling for an automatic call"
        )
    if Flag.LOW_CONFIDENCE in flags:
        parts.append("confidence below threshold after checking")
    return "; ".join(parts).capitalize() + "."


def to_finding(checked: Checked) -> Finding | None:
    """Turn an accepted proposal into a finding. Rejected ones return None."""
    if checked.verdict is Verdict.REJECT:
        return None
    proposal = checked.proposal
    return Finding(
        reason=Reason(proposal.reason),
        entity_type=proposal.entity_type,
        entity_id=proposal.entity_id,
        impact_paise=proposal.impact_paise,
        stage=Stage.LLM,
        confidence=checked.confidence,
        requires_human=checked.verdict is Verdict.REVIEW,
        explanation=proposal.explanation.strip(),
        evidence=[Evidence("cited", record_id, "id", record_id) for record_id in proposal.cited_ids],
    )
