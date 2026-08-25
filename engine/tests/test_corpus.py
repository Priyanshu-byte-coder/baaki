"""Invariants the adversary must hold before any matcher result means anything.

If the corpus generator is wrong, every downstream metric is noise. These tests
guard the two properties the evaluation rests on: a clean corpus really is
clean, and fault injection changes exactly what it declared and nothing else.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import pytest

from baaki.corpus.defects import DEFAULT_PLAN, inject
from baaki.corpus.generate import generate
from baaki.models import PaymentStatus, Reason

ORDERS = 2_000
SEED = 7


@pytest.fixture(scope="module")
def clean():
    return generate(seed=SEED, n_orders=ORDERS)


@pytest.fixture(scope="module")
def faulted():
    return inject(generate(seed=SEED, n_orders=ORDERS), seed=SEED)


def _net_by_settlement(corpus) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for row in corpus.settlement_rows:
        totals[row.settlement_id] += row.net_paise
    return totals


# -- clean corpus -----------------------------------------------------------


def test_settlement_header_equals_sum_of_rows(clean):
    totals = _net_by_settlement(clean.corpus)
    off = [s.settlement_id for s in clean.corpus.settlements if totals[s.settlement_id] != s.net_paise]
    assert off == []


def test_every_positive_settlement_has_a_bank_credit(clean):
    linked = clean.settlement_to_bank
    missing = [
        s.settlement_id
        for s in clean.corpus.settlements
        if s.net_paise > 0 and s.settlement_id not in linked
    ]
    assert missing == []


def test_bank_credits_match_their_settlement_exactly(clean):
    by_id = {b.bank_txn_id: b for b in clean.corpus.bank_txns}
    for settlement in clean.corpus.settlements:
        bank_id = clean.settlement_to_bank.get(settlement.settlement_id)
        if bank_id:
            assert by_id[bank_id].credit_paise == settlement.net_paise


def test_running_balance_is_coherent(clean):
    balance = 5_000_000
    for txn in clean.corpus.bank_txns:
        balance += txn.credit_paise - txn.debit_paise
        assert txn.balance_paise == balance


def test_clean_corpus_only_expects_unidentified_credits(clean):
    """A clean corpus must have nothing flaggable except statement noise.

    This is what makes the false-positive count meaningful: any other finding
    the engine reports against a clean corpus is, by construction, wrong.
    """
    reasons = {d.reason for d in clean.truth}
    assert reasons == {Reason.BANK_CREDIT_UNIDENTIFIED}


def test_ground_truth_rows_are_all_linked(clean):
    assert [d.defect_id for d in clean.truth if not d.entity_id] == []


def test_failed_payments_carry_no_fee(clean):
    for payment in clean.corpus.payments:
        if payment.status is PaymentStatus.FAILED:
            assert payment.fee_paise == 0 and payment.tax_paise == 0


# -- determinism ------------------------------------------------------------


def test_same_seed_reproduces_the_corpus():
    a = generate(seed=11, n_orders=500)
    b = generate(seed=11, n_orders=500)
    fingerprint = lambda g: [  # noqa: E731
        (t.bank_txn_id, t.narration, t.credit_paise, t.balance_paise) for t in g.corpus.bank_txns
    ]
    assert fingerprint(a) == fingerprint(b)


def test_different_seed_changes_the_corpus():
    a = generate(seed=11, n_orders=500)
    b = generate(seed=12, n_orders=500)
    assert [t.credit_paise for t in a.corpus.bank_txns] != [
        t.credit_paise for t in b.corpus.bank_txns
    ]


def test_injection_is_reproducible():
    fingerprint = lambda g: [  # noqa: E731
        (d.defect_id, d.reason.value, d.entity_id, d.impact_paise) for d in g.truth
    ]
    a = inject(generate(seed=11, n_orders=500), seed=11)
    b = inject(generate(seed=11, n_orders=500), seed=11)
    assert fingerprint(a) == fingerprint(b)


# -- fault injection --------------------------------------------------------


def test_every_injector_plants_its_full_requested_count(faulted):
    """Regression guard for the claim-collision bug.

    Payment level faults once claimed their enclosing ``settlement_id``. With
    only a few dozen settlements in a month, the first injector to run claimed
    almost all of them and every later injector planted nothing at all, which
    made per-code recall read as a perfect score over an empty denominator.
    """
    planted = Counter(d.reason.value.lower() for d in faulted.truth)
    short = {
        name: (planted.get(name, 0), requested)
        for name, requested in DEFAULT_PLAN.items()
        if planted.get(name, 0) != requested
    }
    assert short == {}


def test_injection_is_isolated_to_one_defect_per_entity(faulted):
    counts = Counter(d.entity_id for d in faulted.truth)
    assert [entity for entity, n in counts.items() if n > 1] == []


def test_only_declared_mismatches_break_the_header_invariant(faulted):
    """Coherent propagation: no injector may create a defect it did not declare.

    Every fault moves the payment row, the settlement line, the settlement
    header and the bank credit together. The only settlements whose header
    disagrees with their rows must be the ones deliberately planted as
    ``SETTLEMENT_AMOUNT_MISMATCH``.
    """
    totals = _net_by_settlement(faulted.corpus)
    broken = {
        s.settlement_id for s in faulted.corpus.settlements if totals[s.settlement_id] != s.net_paise
    }
    declared = {
        d.entity_id for d in faulted.truth if d.reason is Reason.SETTLEMENT_AMOUNT_MISMATCH
    }
    assert broken == declared


def test_balance_stays_coherent_after_injection(faulted):
    balance = 5_000_000
    for txn in faulted.corpus.bank_txns:
        balance += txn.credit_paise - txn.debit_paise
        assert txn.balance_paise == balance


def test_non_recoverable_reasons_carry_no_principal(faulted):
    """Late settlement and split credits cost time, not money."""
    for defect in faulted.truth:
        if defect.reason in (Reason.LATE_SETTLEMENT, Reason.PARTIAL_BANK_CREDIT):
            assert defect.impact_paise == 0


def test_on_hold_settlements_lose_their_utr(faulted):
    """The discriminator between a held settlement and a missing one.

    Both leave a settlement with no bank credit. Only the status and the
    absent UTR separate them, and an engine that flags both as missing money
    is wrong on the held ones.
    """
    held = {d.entity_id for d in faulted.truth if d.reason is Reason.SETTLEMENT_ON_HOLD}
    for settlement in faulted.corpus.settlements:
        if settlement.settlement_id in held:
            assert settlement.utr is None
            assert settlement.status.value == "on_hold"
