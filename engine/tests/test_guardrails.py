"""Adversarial tests for the tail-stage guardrails.

These are written as attacks rather than as happy paths. Each one is a shape of
plausible, fluent, well-formed model output that must not be allowed to become
a finding. None of them need an API key -- the guardrails are pure functions
over a proposal, which is the point of keeping them separate from the client.
"""

from __future__ import annotations

import pytest

from baaki.corpus.generate import generate
from baaki.match.guardrails import (
    AUTONOMY_CEILING_PAISE,
    Flag,
    Proposal,
    Scope,
    Verdict,
    check,
    to_finding,
)

SEED = 7


@pytest.fixture
def world():
    """Function-scoped on purpose.

    Several of these tests mutate the scope to simulate a model misreading it.
    A module-scoped fixture carries that mutation into every later test in the
    file, which showed up as an unrelated assertion failing three tests down.
    """
    g = generate(seed=SEED, n_orders=800)
    corpus = g.corpus
    settlement = next(s for s in corpus.settlements if s.net_paise > 0)
    bank_id = g.settlement_to_bank[settlement.settlement_id]
    credit = next(b for b in corpus.bank_txns if b.bank_txn_id == bank_id)
    scope = Scope(corpus, [settlement.settlement_id], [credit.bank_txn_id])
    return corpus, settlement, credit, scope


def _sound(settlement, credit) -> Proposal:
    return Proposal(
        reason="SETTLED_NOT_IN_BANK",
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        explanation=(
            "The narration carries no reference, but the credit matches the settlement "
            "value on the same day and no other settlement is a candidate."
        ),
        cited_ids=[settlement.settlement_id, credit.bank_txn_id],
        proposed_credit_ids=[credit.bank_txn_id],
        impact_paise=0,
        model_confidence=0.99,
    )


def test_a_sound_proposal_is_accepted(world):
    _corpus, settlement, credit, scope = world
    result = check(_sound(settlement, credit), scope)
    assert result.verdict is Verdict.ACCEPT
    assert result.flags == []
    assert result.confidence > 0.8


def test_hallucinated_entity_is_rejected(world):
    """A confident citation of a record that was never shown."""
    _corpus, settlement, credit, scope = world
    proposal = _sound(settlement, credit)
    proposal.cited_ids = [settlement.settlement_id, "bank_0007999999"]

    result = check(proposal, scope)
    assert result.verdict is Verdict.REJECT
    assert Flag.UNGROUNDED_ENTITY in result.flags
    assert result.confidence == 0.0
    assert to_finding(result) is None


def test_a_real_record_outside_the_prompt_scope_is_still_ungrounded(world):
    """Guessing an identifier that happens to exist is not grounding.

    On a large book, plausible identifiers are cheap to guess. Grounding is
    checked against what the model was actually shown.
    """
    corpus, settlement, credit, scope = world
    other = next(
        b.bank_txn_id for b in corpus.bank_txns if b.bank_txn_id != credit.bank_txn_id
    )
    proposal = _sound(settlement, credit)
    proposal.cited_ids = [settlement.settlement_id, other]

    result = check(proposal, scope)
    assert result.verdict is Verdict.REJECT
    assert Flag.UNGROUNDED_ENTITY in result.flags


def test_a_match_whose_money_does_not_add_up_is_rejected(world):
    """The central guarantee: arithmetic decides matches, not confidence.

    This is the guardrail form of the bug that closed a 1,10,272 settlement
    against a 36,757 credit. Even with perfect grounding and a fluent
    explanation, a proposed match whose credits do not sum to the settlement
    net cannot become a finding.
    """
    corpus, settlement, credit, scope = world
    proposal = _sound(settlement, credit)
    # Same records, but claim the settlement is worth something it is not by
    # pointing at a settlement whose net differs from this credit.
    scope.settlement_net[settlement.settlement_id] = credit.credit_paise + 5_000_00

    result = check(proposal, scope)
    assert result.verdict is Verdict.REJECT
    assert Flag.ARITHMETIC_FAILED in result.flags
    assert "do not sum" in result.note


def test_invented_reason_code_is_rejected(world):
    _corpus, settlement, credit, scope = world
    proposal = _sound(settlement, credit)
    proposal.reason = "SETTLEMENT_LOOKS_ODD"

    result = check(proposal, scope)
    assert result.verdict is Verdict.REJECT
    assert Flag.UNKNOWN_REASON in result.flags


def test_a_claim_with_no_citations_never_reaches_accept(world):
    _corpus, settlement, credit, scope = world
    proposal = _sound(settlement, credit)
    proposal.cited_ids = []
    proposal.proposed_credit_ids = []

    result = check(proposal, scope)
    assert Flag.NO_EVIDENCE in result.flags
    assert result.verdict is not Verdict.ACCEPT


def test_large_amounts_require_a_human_even_when_everything_checks_out(world):
    _corpus, settlement, credit, scope = world
    proposal = _sound(settlement, credit)
    proposal.impact_paise = AUTONOMY_CEILING_PAISE + 1

    result = check(proposal, scope)
    assert result.verdict is Verdict.REVIEW
    assert Flag.EXCEEDS_AUTONOMY in result.flags

    finding = to_finding(result)
    assert finding is not None
    assert finding.requires_human is True


def test_model_self_reported_confidence_is_ignored(world):
    """A model asked how sure it is answers fluently and without calibration.

    Two proposals identical but for the number the model put on itself must
    score the same, because that number is never read.
    """
    _corpus, settlement, credit, scope = world
    certain = _sound(settlement, credit)
    certain.model_confidence = 1.0
    unsure = _sound(settlement, credit)
    unsure.model_confidence = 0.01

    assert check(certain, scope).confidence == check(unsure, scope).confidence


def test_a_hallucinating_proposal_cannot_score_above_zero(world):
    """Fatal flags floor the score, so nothing downstream can rank it up."""
    _corpus, settlement, credit, scope = world
    proposal = _sound(settlement, credit)
    proposal.cited_ids = ["setl_does_not_exist"] * 8  # many citations, all fake

    result = check(proposal, scope)
    assert result.confidence == 0.0
    assert result.verdict is Verdict.REJECT
