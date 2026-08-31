"""The recovery loop: claims, triage, and verifying the money came back.

The claims here are stronger than "it runs". A recovery loop that marks things
recovered without evidence is worse than no loop at all, because it closes real
losses on the strength of an assumption.
"""

from __future__ import annotations

from datetime import date

import pytest

from baaki.corpus.defects import HARD_PLAN, inject
from baaki.corpus.generate import generate
from baaki.corpus.periods import next_cycle
from baaki.match import pipeline
from baaki.models import EntityType, Reason
from baaki.recovery import triage as triage_mod
from baaki.recovery import verify as verify_mod
from baaki.recovery.claims import ClaimState, Disposition, Ledger

SEED = 34
ORDERS = 1_500
JULY = date(2026, 7, 1)
AUGUST = date(2026, 8, 1)


@pytest.fixture
def cycle_one():
    g = inject(generate(seed=SEED, n_orders=ORDERS, month_start=JULY), seed=SEED, plan=HARD_PLAN)
    result = pipeline.run_offline(g.corpus)
    ledger = Ledger()
    ledger.open_from_findings(result.findings, on=JULY, cycle="2026-07")
    return g, result, ledger


# -- claims -----------------------------------------------------------------


def test_only_recoverable_findings_become_claims(cycle_one):
    """Filing a ticket for a late settlement wastes everyone's time.

    Timing and attribution items are real and worth reporting. They are not
    worth claiming, because there is nobody to claim them from.
    """
    _g, result, ledger = cycle_one
    claimed_reasons = {Reason(c.reason) for c in ledger.claims.values()}
    for reason in claimed_reasons:
        assert reason in {f.reason for f in result.findings if f.recoverable}
    assert Reason.LATE_SETTLEMENT not in claimed_reasons
    assert Reason.BANK_CREDIT_UNIDENTIFIED not in claimed_reasons


def test_rerunning_a_cycle_does_not_duplicate_claims(cycle_one):
    _g, result, ledger = cycle_one
    before = len(ledger.claims)
    ledger.open_from_findings(result.findings, on=JULY, cycle="2026-07")
    assert len(ledger.claims) == before


def test_ledger_survives_a_round_trip(cycle_one, tmp_path):
    _g, _result, ledger = cycle_one
    triage_mod.triage(ledger, on=JULY)
    path = ledger.save(tmp_path / "claims.json")

    reloaded = Ledger.load(path)
    assert len(reloaded.claims) == len(ledger.claims)
    assert reloaded.totals()["claimed_paise"] == ledger.totals()["claimed_paise"]
    assert all(c.history for c in reloaded.claims.values())


# -- triage -----------------------------------------------------------------


def test_claims_too_small_to_chase_are_dropped_explicitly(cycle_one):
    """Dropping is a recorded decision, not an item falling off the list."""
    _g, _result, ledger = cycle_one
    outcome = triage_mod.triage(ledger, on=JULY)

    dropped = outcome["dropped"]
    assert dropped
    for claim in dropped:
        assert claim.state == ClaimState.NOT_PURSUED.value
        assert claim.disposition == Disposition.DROP.value
        assert claim.history[-1].note  # the reasoning is on the record


def test_a_large_claim_is_always_worth_chasing_alone(cycle_one):
    _g, _result, ledger = cycle_one
    outcome = triage_mod.triage(ledger, on=JULY)
    big = max(ledger.claims.values(), key=lambda c: c.claimed_paise)
    assert big.state == ClaimState.FILED.value
    assert big in outcome["chase"]


def test_batching_changes_the_economics(cycle_one):
    """Individually worthless, collectively worth one ticket.

    This is the whole argument for batching: the cost of asking is paid once,
    not once per claim.
    """
    _g, _result, ledger = cycle_one
    rates = triage_mod.learned_rates(ledger)
    small = [c for c in ledger.claims.values() if c.claimed_paise < triage_mod.SOLO_COST_PAISE]
    assert small, "expected some claims too small to chase alone"

    for claim in small[:5]:
        assert triage_mod.assess(claim, rates).disposition is Disposition.BATCH

    reason = small[0].reason
    group = [c for c in small if c.reason == reason]
    total = sum(c.claimed_paise for c in group)
    batch_cost = triage_mod.analyst_cost_paise(
        triage_mod.BATCH_SETUP_MINUTES + triage_mod.BATCH_MINUTES_PER_ITEM * len(group)
    )
    solo_cost = triage_mod.SOLO_COST_PAISE * len(group)
    assert batch_cost < solo_cost
    assert total >= 0


def test_exploration_files_one_claim_from_a_rejected_batch(cycle_one):
    """A policy that only files what it expects to win never learns otherwise.

    Every prior started as a guess. If a guess is pessimistic, expected value
    drops the category forever and no evidence ever arrives to correct it.
    """
    _g, _result, ledger = cycle_one
    outcome = triage_mod.triage(ledger, on=JULY)

    assert outcome["explored"], "expected at least one exploration probe"
    for probe in outcome["explored"]:
        assert probe.state == ClaimState.FILED.value
        assert "exploration probe" in probe.history[-1].note
        # The probe is the largest of its rejected group -- most information per
        # analyst minute spent.
        siblings = [
            c for c in outcome["dropped"] if c.reason == probe.reason
        ]
        for sibling in siblings:
            assert sibling.claimed_paise <= probe.claimed_paise


def test_exploration_stops_once_a_reason_has_a_track_record():
    ledger = Ledger()
    assert triage_mod._exploration_probe([], "MDR_OVERCHARGE", 0) is None

    from baaki.recovery.claims import Claim

    group = [
        Claim(
            claim_id=f"clm_{i}",
            reason="MDR_OVERCHARGE",
            entity_type="payment",
            entity_id=f"pay_{i}",
            claimed_paise=100 * (i + 1),
            opened_on=JULY.isoformat(),
        )
        for i in range(3)
    ]
    assert triage_mod._exploration_probe(group, "MDR_OVERCHARGE", 0) is group[-1]
    assert (
        triage_mod._exploration_probe(
            group, "MDR_OVERCHARGE", triage_mod.EXPLORATION_THRESHOLD
        )
        is None
    )


def test_learned_rates_are_shrunk_toward_the_prior(cycle_one):
    """One observation must not swing the whole policy."""
    _g, _result, ledger = cycle_one
    rates = triage_mod.learned_rates(ledger)
    for reason, prior in triage_mod.PRIOR_RECOVERY.items():
        assert rates[reason] == pytest.approx(prior), "no history yet, so pure prior"


# -- verification -----------------------------------------------------------


def test_a_claim_is_only_recovered_when_the_money_is_found(cycle_one):
    """Filing does not recover anything. Finding the adjustment does."""
    _g, _result, ledger = cycle_one
    triage_mod.triage(ledger, on=JULY)
    filed = ledger.filed()
    assert filed
    assert all(c.recovered_paise == 0 for c in filed)

    # A later period with no adjustments at all repays nothing.
    barren = generate(seed=SEED + 99, n_orders=500, month_start=AUGUST)
    report = verify_mod.verify(ledger, barren.corpus, on=date(2026, 8, 20))
    assert report.recovered_paise == 0
    assert report.matched == 0


def test_the_loop_detects_what_the_gateway_actually_repaid():
    """End to end, scored against the generator's own record of what it repaid.

    The verifier never sees ``cycle.repaid``.
    """
    g1 = inject(generate(seed=SEED, n_orders=ORDERS, month_start=JULY), seed=SEED, plan=HARD_PLAN)
    result = pipeline.run_offline(g1.corpus)
    ledger = Ledger()
    ledger.open_from_findings(result.findings, on=JULY, cycle="2026-07")
    triage_mod.triage(ledger, on=JULY)

    cycle2 = next_cycle(
        ledger, seed=SEED + 1, month_start=AUGUST, label="2026-08",
        n_orders=ORDERS, plan=HARD_PLAN,
    )
    report = verify_mod.verify(ledger, cycle2.generated.corpus, on=date(2026, 8, 31))
    score = verify_mod.score_recovery(cycle2, ledger, report)

    assert score["repaid_claims"] > 0, "the fixture should repay something"
    assert score["false_positives"] == 0
    assert score["detection_rate"] == 1.0
    assert score["detected_paise"] == score["repaid_paise"]


def test_scoring_a_later_cycle_does_not_blame_it_for_earlier_recoveries():
    """Regression: the ledger is cumulative, a settlement period is not.

    Scoring cycle three against every claim the ledger has ever recovered
    counts all of cycle two's genuine recoveries as false positives. The
    two-cycle test could not catch this -- there was no earlier cycle to be
    wrongly blamed for -- so it took a three-cycle run to surface, reporting
    thirty-three phantom false recoveries.
    """
    ledger = Ledger()
    period = JULY
    seen_false_positives = []

    for index in range(3):
        if index == 0:
            g = inject(
                generate(seed=SEED, n_orders=ORDERS, month_start=period),
                seed=SEED,
                plan=HARD_PLAN,
            )
            cycle = None
        else:
            cycle = next_cycle(
                ledger, seed=SEED + index, month_start=period,
                label=f"{period:%Y-%m}", n_orders=ORDERS, plan=HARD_PLAN,
            )
            g = cycle.generated

        result = pipeline.run_offline(g.corpus)
        if cycle is not None:
            closing = date(period.year, period.month, 28)
            report = verify_mod.verify(ledger, g.corpus, on=closing)
            score = verify_mod.score_recovery(cycle, ledger, report)
            seen_false_positives.append(score["false_positives"])

        ledger.open_from_findings(result.findings, on=period, cycle=f"{period:%Y-%m}")
        triage_mod.triage(ledger, on=period)
        period = date(period.year + (period.month == 12), (period.month % 12) + 1, 1)

    assert seen_false_positives == [0, 0], seen_false_positives


def test_only_filed_claims_can_be_repaid():
    """A claim triage dropped must never come back as recovered.

    Otherwise the recovery rate would be measuring our own triage rather than
    the gateway's behaviour.
    """
    g1 = inject(generate(seed=SEED, n_orders=ORDERS, month_start=JULY), seed=SEED, plan=HARD_PLAN)
    result = pipeline.run_offline(g1.corpus)
    ledger = Ledger()
    ledger.open_from_findings(result.findings, on=JULY, cycle="2026-07")
    triage_mod.triage(ledger, on=JULY)

    dropped = {c.claim_id for c in ledger.claims.values()
               if c.state == ClaimState.NOT_PURSUED.value}
    cycle2 = next_cycle(
        ledger, seed=SEED + 1, month_start=AUGUST, label="2026-08",
        n_orders=ORDERS, plan=HARD_PLAN,
    )
    assert not (dropped & set(cycle2.repaid)), "a dropped claim was repaid by the fixture"


def test_repayments_do_not_read_as_settlement_mismatches():
    """An adjustment lifts the header and the bank credit with it.

    If it did not, every repayment would show up as a brand new
    SETTLEMENT_AMOUNT_MISMATCH and the second cycle's report would be nonsense.
    """
    g1 = inject(generate(seed=SEED, n_orders=ORDERS, month_start=JULY), seed=SEED, plan=HARD_PLAN)
    result = pipeline.run_offline(g1.corpus)
    ledger = Ledger()
    ledger.open_from_findings(result.findings, on=JULY, cycle="2026-07")
    triage_mod.triage(ledger, on=JULY)

    cycle2 = next_cycle(
        ledger, seed=SEED + 1, month_start=AUGUST, label="2026-08",
        n_orders=ORDERS, plan=HARD_PLAN,
    )
    corpus = cycle2.generated.corpus
    adjustments = [
        r for r in corpus.settlement_rows if r.entity_type is EntityType.ADJUSTMENT
    ]
    assert adjustments, "the fixture should have written adjustment lines"

    totals: dict[str, int] = {}
    for row in corpus.settlement_rows:
        totals[row.settlement_id] = totals.get(row.settlement_id, 0) + row.net_paise

    planted = {
        d.entity_id
        for d in cycle2.generated.truth
        if d.reason is Reason.SETTLEMENT_AMOUNT_MISMATCH
    }
    broken = {
        s.settlement_id for s in corpus.settlements if totals[s.settlement_id] != s.net_paise
    }
    assert broken == planted
